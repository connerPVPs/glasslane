#!/usr/bin/env python3
"""Shards Runner Agent — manages the temporary Minecraft server, bore.pub relay,
live console stream, telemetry, and file manager inside a GitHub Actions runner.
"""
import base64
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DURATION_MIN = min(290, max(1, int(os.environ.get("DURATION_MINUTES") or "290")))
SHARD_OWNER = (os.environ.get("SHARD_OWNER") or "").strip()
SHARD_NAME = re.sub(r"[^a-z0-9-]", "", (os.environ.get("SHARD_NAME") or "shard").lower())[:24] or "shard"
SHARD_ID = (os.environ.get("SHARD_ID") or "").strip()
API_BASE = (os.environ.get("SHARDS_API_URL") or "https://shards.zealot.bond").rstrip("/")
RUN_ID = (os.environ.get("GITHUB_RUN_ID") or "").strip()

ROOT_DIR = Path.cwd()
BOX_DIR = ROOT_DIR / ".devlane"
SERVER_DIR = BOX_DIR / "server"
BORE_BIN = BOX_DIR / "bore"

JAR_URL = "https://fill-data.papermc.io/v1/objects/158703f75a26f842ea656b3dc6d75bf3d1ec176b97a2c36384d0b80b3871af53/paper-1.21.10-130.jar"
BORE_URL = "https://github.com/ekzhang/bore/releases/download/v0.6.0/bore-v0.6.0-x86_64-unknown-linux-musl.tar.gz"

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
DONE_RE = re.compile(r'Done \([0-9.]+s\)! For help, type "help"')


class ShardAgent:
    def __init__(self):
        self.lock = threading.Lock()
        self.logs = []
        self.next_seq = 1
        self.last_sent_seq = 0

        self.state = "creating"
        self.step = "Preparing 4 vCPU / 16 GB DDR5 workspace..."
        self.progress = 15
        self.bore_port = 0
        self.bore_address = ""
        self.mc_proc = None
        self.bore_proc = None
        self.mc_Ever_up = False
        self.desired_mc = "running"
        self.should_exit = False
        self.started_at = time.time()
        self.expires_at = self.started_at + (DURATION_MIN * 60)

        self.oidc_token = ""
        self.oidc_exp = 0
        self.cmd_results = []
        self.files_dirty = True
        self.last_files_sync = 0
        self.stats = {
            "players_online": 0,
            "players_max": 20,
            "player_names": [],
            "ram_used_mb": 0,
            "ram_total_mb": 16384,
            "disk_used_mb": 0,
            "disk_total_mb": 15360,
            "cpu_Load": 0.0,
        }

    def log_line(self, line):
        clean = ANSI_RE.sub("", line.rstrip("\r\n"))
        if not clean:
            return
        ts = time.strftime("%H:%M:%S", time.gmtime())
        entry = f"[{ts}] {clean}"
        print(entry, flush=True)
        with self.lock:
            self.logs.append((self.next_seq, entry))
            self.next_seq += 1
            if len(self.logs) > 2500:
                self.logs = self.logs[-2000:]

    def get_oidc_token(self):
        now = time.time()
        if self.oidc_token and now < self.oidc_exp - 60:
            return self.oidc_token
        req_url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
        req_tok = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
        if not req_url or not req_tok:
            return os.environ.get("AGENT_TEST_TOKEN") or ""
        sep = "&" if "?" in req_url else "?"
        url = f"{req_url}{sep}audience=shards"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {req_tok}",
                "Accept": "application/json; api-version=2.0",
                "User-Agent": "ShardsAgent/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.load(resp)
                tok = data.get("value") or ""
                if tok:
                    self.oidc_token = tok
                    self.oidc_exp = now + 240
                    return tok
        except Exception as exc:
            print(f"[agent] OIDC token fetch warning: {exc}", flush=True)
        return self.oidc_token

    def resolve_safe_path(self, rel_path):
        rel = (rel_path or "").strip().lstrip("/")
        target = (SERVER_DIR / rel).resolve()
        root = SERVER_DIR.resolve()
        if target != root and root not in target.parents:
            raise ValueError("Path escapes server workspace")
        return target

    def build_files_snapshot(self):
        if not SERVER_DIR.exists():
            return {"entries": [], "configs": {}}
        entries = []
        configs = {}
        inline_exts = {".properties", ".yml", ".yaml", ".json", ".txt", ".conf", ".toml", ".cfg"}
        inline_names = {"server.properties", "eula.txt", "ops.json", "whitelist.json", "banned-players.json", "banned-ips.json", "bukkit.yml", "spigot.yml", "commands.yml"}
        try:
            for root, dirs, files in os.walk(SERVER_DIR):
                rel_root = Path(root).relative_to(SERVER_DIR)
                depth = len(rel_root.parts)
                if depth > 2:
                    dirs[:] = []
                    continue
                dirs[:] = [d for d in sorted(dirs) if not d.startswith(".cache")]
                for d in dirs:
                    rp = (rel_root / d).as_posix()
                    st = (Path(root) / d).stat()
                    entries.append({
                        "path": rp,
                        "name": d,
                        "parent": "" if str(rel_root) == "." else rel_root.as_posix(),
                        "type": "dir",
                        "size": 0,
                        "mtime": int(st.st_mtime * 1000),
                    })
                for f in sorted(files):
                    fp = Path(root) / f
                    rp = (rel_root / f).as_posix()
                    try:
                        st = fp.stat()
                    except OSError:
                        continue
                    entries.append({
                        "path": rp,
                        "name": f,
                        "parent": "" if str(rel_root) == "." else rel_root.as_posix(),
                        "type": "file",
                        "size": st.st_size,
                        "mtime": int(st.st_mtime * 1000),
                    })
                    if (f in inline_names or (depth <= 1 and fp.suffix.lower() in inline_exts)) and st.st_size <= 65536:
                        try:
                            configs[rp] = fp.read_text(encoding="utf-8", errors="replace")
                        except Exception:
                            pass
        except Exception as exc:
            print(f"[agent] snapshot warning: {exc}", flush=True)
        return {"entries": entries[:400], "configs": configs}

    def collect_stats(self):
        ram_mb = 0
        proc = self.mc_proc
        if proc and proc.poll() is None:
            try:
                status_file = Path(f"/proc/{proc.pid}/status")
                if status_file.exists():
                    for line in status_file.read_text().splitlines():
                        if line.startswith("VmRSS:"):
                            kb = int(line.split()[1])
                            ram_mb = round(kb / 1024)
                            break
            except Exception:
                pass
        disk_mb = 0
        try:
            total_bytes = 0
            if SERVER_DIR.exists():
                for root, _, files in os.walk(SERVER_DIR):
                    for f in files:
                        try:
                            total_bytes += (Path(root) / f).stat().st_size
                        except OSError:
                            pass
            disk_mb = round(total_bytes / (1024 * 1024), 1)
        except Exception:
            pass

        players_online = 0
        players_max = 20
        player_names = []
        if self.state == "running" and proc and proc.poll() is None:
            try:
                info = self.ping_mc_local()
                if info:
                    p = info.get("players") or {}
                    players_online = int(p.get("online") or 0)
                    players_max = int(p.get("max") or 20)
                    sample = p.get("sample") or []
                    player_names = [str(x.get("name")) for x in sample if x.get("name")]
            except Exception:
                pass

        try:
            load1, _, _ = os.getloadavg()
        except Exception:
            load1 = 0.0

        self.stats = {
            "players_online": players_online,
            "players_max": players_max,
            "player_names": player_names,
            "ram_used_mb": ram_mb,
            "ram_total_mb": 16384,
            "disk_used_mb": disk_mb,
            "disk_total_mb": 15360,
            "cpu_load": round(load1, 2),
        }

    @staticmethod
    def ping_mc_local():
        def wv(n):
            out = b""
            while True:
                b = n & 0x7F
                n >>= 7
                out += bytes([b | (0x80 if n else 0)])
                if not n:
                    return out

        def rv(sock):
            num = 0
            sh = 0
            while True:
                c = sock.recv(1)
                if not c:
                    raise EOFError
                b = c[0]
                num |= (b & 0x7F) << sh
                if not (b & 0x80):
                    return num
                sh += 7

        with socket.create_connection(("127.0.0.1", 25565), timeout=2.0) as s:
            host = b"127.0.0.1"
            body = b"\x00" + wv(765) + wv(len(host)) + host + struct.pack(">H", 25565) + b"\x01"
            s.sendall(wv(len(body)) + body)
            s.sendall(wv(1) + b"\x00")
            _ = rv(s)
            pkt_id = rv(s)
            if pkt_id != 0:
                return None
            jlen = rv(s)
            buf = b""
            while len(buf) < jlen:
                chunk = s.recv(jlen - len(buf))
                if not chunk:
                    break
                buf += chunk
            return json.loads(buf.decode("utf-8", "replace"))

    def start_bore_tunnel(self):
        if self.bore_proc and self.bore_proc.poll() is None:
            return
        bore_log = BOX_DIR / "bore.log"
        self.bore_proc = subprocess.Popen(
            [str(BORE_BIN), "local", "25565", "--to", "bore.pub", "--port", "0", "--local-host", "127.0.0.1"],
            stdout=open(bore_log, "w"),
            stderr=subprocess.STDOUT,
            cwd=str(BOX_DIR),
        )
        for _ in range(35):
            if bore_log.exists():
                txt = ANSI_RE.sub("", bore_log.read_text(errors="replace"))
                m = re.search(r"remote_port=(\d+)", txt)
                if m:
                    self.bore_port = int(m.group(1))
                    self.bore_address = f"bore.pub:{self.bore_port}"
                    self.log_line(f"[Shards] Tunnel established -> {self.bore_address} ({SHARD_NAME}.zealot.bond)")
                    return
            time.sleep(1)
        self.log_line("[Shards] Warning: bore.pub did not report a port within 35s; retrying in background.")

    def start_mc_server(self, initial=False):
        if self.mc_proc and self.mc_proc.poll() is None:
            return
        self.desired_mc = "running"
        if initial and not self.mc_Ever_up:
            self.state = "creating"
            self.step = "Starting Minecraft 1.21.10 server & generating world..."
            self.progress = 70
        else:
            self.state = "starting"
            self.step = "Starting Minecraft server..."
            self.progress = 80

        self.log_line(f"[Shards] Launching Paper 1.21.10 (4 vCPU, 12G heap) for {SHARD_NAME}.zealot.bond...")
        cmd = [
            "java",
            "-Xms2G",
            "-Xmx12G",
            "-XX:+UseG1GC",
            "-XX:+ParallelRefProcEnabled",
            "-XX:MaxGCPauseMillis=200",
            "-XX:+UnlockExperimentalVMOptions",
            "-XX:+DisableExplicitGC",
            "-jar",
            str(BOX_DIR / "server.jar"),
            "nogui",
        ]
        self.mc_proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(SERVER_DIR),
            text=True,
            bufsize=1,
        )
        t = threading.Thread(target=self._read_mc_stdout, args=(self.mc_proc,), daemon=True)
        t.start()

    def _read_mc_stdout(self, proc):
        try:
            for line in proc.stdout:
                self.log_line(line)
                if not self.mc_Ever_up:
                    if "Loading libraries" in line or "Downloading" in line:
                        self.step = "Loading Paper 1.21.10 libraries..."
                        self.progress = 76
                    elif "Preparing level" in line:
                        self.step = "Generating Minecraft world..."
                        self.progress = 85
                    elif "Preparing start region" in line:
                        self.step = "Preparing spawn area..."
                        self.progress = 93
                if DONE_RE.search(line):
                    self.mc_Ever_up = True
                    self.state = "running"
                    self.step = "Online"
                    self.progress = 100
                    self.files_dirty = True
                    self.log_line(f"[Shards] Server is UP! Connect at {SHARD_NAME}.zealot.bond (or {self.bore_address})")
        except Exception as exc:
            self.log_line(f"[Shards] Console stream ended: {exc}")
        finally:
            rc = proc.wait()
            if self.mc_proc is proc:
                if self.desired_mc == "stopped":
                    self.state = "stopped"
                    self.step = "Server stopped (Shard still allocated)"
                    self.log_line("[Shards] Minecraft server stopped. Click Start to boot it again.")
                elif self.desired_mc == "restarting":
                    self.log_line("[Shards] Minecraft server stopped for restart.")
                elif not self.should_exit:
                    self.state = "stopped"
                    self.step = f"Server exited (code {rc})"
                    self.log_line(f"[Shards] Minecraft process exited with code {rc}.")
                self.files_dirty = True

    def stop_mc_server(self, for_restart=False):
        self.desired_mc = "restarting" if for_restart else "stopped"
        proc = self.mc_proc
        if not proc or proc.poll() is not None:
            if not for_restart:
                self.state = "stopped"
                self.step = "Server stopped"
            return
        self.state = "restarting" if for_restart else "stopping"
        self.step = "Restarting Minecraft server..." if for_restart else "Saving worlds & stopping Minecraft server..."
        self.log_line("[Shards] Sending save-all & stop to Minecraft server...")
        try:
            if proc.stdin:
                proc.stdin.write("save-all\nstop\n")
                proc.stdin.flush()
        except Exception:
            pass
        for _ in range(25):
            if proc.poll() is not None:
                break
            time.sleep(1)
        if proc.poll() is None:
            self.log_line("[Shards] Force-stopping unresponsive JVM...")
            try:
                proc.kill()
            except Exception:
                pass
            proc.wait(timeout=5)
        if not for_restart:
            self.state = "stopped"
            self.step = "Server stopped (Shard still allocated)"
        self.files_dirty = True

    def restart_mc_server(self):
        self.stop_mc_server(for_restart=True)
        time.sleep(1)
        self.start_mc_server(initial=False)

    def send_mc_command(self, command):
        cmd = (command or "").strip()
        if cmd.startswith("/"):
            cmd = cmd[1:]
        if not cmd:
            return
        proc = self.mc_proc
        if not proc or proc.poll() is not None or not proc.stdin:
            self.log_line(f"[Shards] Cannot run '{cmd}' — Minecraft server is not running.")
            return
        self.log_line(f"> {cmd}")
        try:
            proc.stdin.write(cmd + "\n")
            proc.stdin.flush()
        except Exception as exc:
            self.log_line(f"[Shards] Failed to send command: {exc}")

    def handle_command(self, cmd_obj):
        cid = cmd_obj.get("id") or ""
        ctype = cmd_obj.get("type") or ""
        res = {"id": cid, "type": ctype, "ok": True}
        try:
            if ctype == "mc_stop":
                threading.Thread(target=self.stop_mc_server, daemon=True).start()
            elif ctype == "mc_start":
                threading.Thread(target=self.start_mc_server, daemon=True).start()
            elif ctype == "mc_restart":
                threading.Thread(target=self.restart_mc_server, daemon=True).start()
            elif ctype == "mc_command":
                self.send_mc_command(cmd_obj.get("command") or "")
            elif ctype == "shard_suspend":
                self.log_line("[Shards] Suspend requested — shutting down shard and cleaning up...")
                self.should_exit = True
                self.stop_mc_server()
            elif ctype == "file_write":
                target = self.resolve_safe_path(cmd_obj.get("path") or "")
                target.parent.mkdir(parents=True, exist_ok=True)
                if cmd_obj.get("encoding") == "base64":
                    target.write_bytes(base64.b64decode(cmd_obj.get("content") or ""))
                else:
                    target.write_text(cmd_obj.get("content") or "", encoding="utf-8")
                self.files_dirty = True
                self.log_line(f"[Shards] Saved file: {cmd_obj.get('path')}")
            elif ctype == "file_read":
                target = self.resolve_safe_path(cmd_obj.get("path") or "")
                if not target.is_file():
                    raise FileNotFoundError("File not found")
                if target.stat().st_size > 512 * 1024:
                    raise ValueError("File exceeds 512 KB editor limit")
                res["content"] = target.read_text(encoding="utf-8", errors="replace")
                res["path"] = cmd_obj.get("path")
            elif ctype == "file_mkdir":
                target = self.resolve_safe_path(cmd_obj.get("path") or "")
                target.mkdir(parents=True, exist_ok=True)
                self.files_dirty = True
            elif ctype == "file_delete":
                target = self.resolve_safe_path(cmd_obj.get("path") or "")
                if target == SERVER_DIR.resolve():
                    raise ValueError("Cannot delete workspace root")
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                self.files_dirty = True
                self.log_line(f"[Shards] Deleted: {cmd_obj.get('path')}")
        except Exception as exc:
            res["ok"] = False
            res["error"] = str(exc)
        with self.lock:
            self.cmd_results.append(res)

    def sync_once(self):
        if not SHARD_OWNER or not API_BASE:
            return
        self.collect_stats()
        with self.lock:
            new_logs = [{"seq": seq, "line": line} for seq, line in self.logs if seq > self.last_sent_seq]
            results = list(self.cmd_results)
            self.cmd_results.clear()

        now = time.time()
        files_snap = None
        if self.files_dirty or (now - self.last_files_sync > 15):
            files_snap = self.build_files_snapshot()
            self.files_dirty = False
            self.last_files_sync = now

        payload = {
            "owner": SHARD_OWNER,
            "shard_id": SHARD_ID,
            "name": SHARD_NAME,
            "run_id": RUN_ID,
            "state": self.state,
            "step": self.step,
            "progress": self.progress,
            "bore_port": self.bore_port,
            "bore_address": self.bore_address,
            "started_at": int(self.started_at * 1000),
            "expires_at": int(self.expires_at * 1000),
            "stats": self.stats,
            "logs": new_logs,
            "results": results,
        }
        if files_snap is not None:
            payload["files"] = files_snap

        tok = self.get_oidc_token()
        req = urllib.request.Request(
            f"{API_BASE}/api/agent/sync",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {tok}",
                "Content-Type": "application/json",
                "User-Agent": "ShardsAgent/1.0",
                "X-Shard-Owner": SHARD_OWNER,
                "X-Shard-Id": SHARD_ID,
                "X-Shard-Run-Id": RUN_ID,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                resp_data = json.load(resp)
                if new_logs:
                    self.last_sent_seq = new_logs[-1]["seq"]
                if resp_data.get("suspend"):
                    self.log_line("[Shards] Shard suspended from panel; exiting.")
                    self.should_exit = True
                    return
                for cmd in resp_data.get("commands") or []:
                    self.handle_command(cmd)
        except Exception as exc:
            print(f"[agent] sync warning: {exc}", flush=True)

    def setup_workspace(self):
        BOX_DIR.mkdir(parents=True, exist_ok=True)
        SERVER_DIR.mkdir(parents=True, exist_ok=True)
        (SERVER_DIR / "plugins").mkdir(parents=True, exist_ok=True)

        self.step = "Downloading Paper 1.21.10 & bore tunnel..."
        self.progress = 30
        self.log_line(f"[Shards] Initializing workspace for {SHARD_NAME}.zealot.bond...")
        self.sync_once()

        jar_path = BOX_DIR / "server.jar"
        if not jar_path.exists():
            subprocess.run(["curl", "-fsSL", "-o", str(jar_path), JAR_URL], check=True)
        if not BORE_BIN.exists():
            tar_path = BOX_DIR / "bore.tar.gz"
            subprocess.run(["curl", "-fsSL", "-o", str(tar_path), BORE_URL], check=True)
            subprocess.run(["tar", "xzf", str(tar_path), "-C", str(BOX_DIR)], check=True)
            BORE_BIN.chmod(0o755)

        (SERVER_DIR / "eula.txt").write_text("eula=true\n", encoding="utf-8")
        props_path = SERVER_DIR / "server.properties"
        if not props_path.exists():
            props_path.write_text(
                f"""# Shards Minecraft Server Properties
server-port=25565
online-mode=true
max-players=20
view-distance=8
simulation-distance=6
motd=\\u00a7b\\u00a7lShards \\u00a78\\u00bb \\u00a7f{SHARD_NAME}.zealot.bond
difficulty=normal
gamemode=survival
pvp=true
spawn-protection=0
enable-query=false
enforce-secure-profile=false
""",
                encoding="utf-8",
            )
        self.files_dirty = True

        self.step = f"Binding bore.pub tunnel & {SHARD_NAME}.zealot.bond..."
        self.progress = 50
        self.sync_once()
        self.start_bore_tunnel()
        self.sync_once()

    def run(self):
        self.setup_workspace()
        self.start_mc_server(initial=True)
        self.sync_once()

        while not self.should_exit and time.time() < self.expires_at:
            if self.bore_proc and self.bore_proc.poll() is not None:
                self.log_line("[Shards] Tunnel process exited; reconnecting bore.pub...")
                self.start_bore_tunnel()
            self.sync_once()
            time.sleep(1.8)

        self.log_line("[Shards] Shutting down shard...")
        self.stop_mc_server()
        self.state = "expired"
        self.step = "Shard session ended"
        self.sync_once()
        if self.bore_proc and self.bore_proc.poll() is None:
            try:
                self.bore_proc.terminate()
            except Exception:
                pass


if __name__ == "__main__":
    agent = ShardAgent()
    def _sig_handler(signum, frame):
        agent.should_exit = True
    signal.signal(signal.SIGTERM, _sig_handler)
    signal.signal(signal.SIGINT, _sig_handler)
    agent.run()
