#!/usr/bin/env python3
"""Shards Runner Agent — PTY ANSI color console, locked MOTD & BossBar branding,
safe version switcher, plugin installer, and sub-second state sync.
"""
import base64
import glob
import json
import os
import pty
import re
import select
import shutil
import signal
import socket
import subprocess
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
SHARD_SOFTWARE = (os.environ.get("SHARD_SOFTWARE") or "paper").strip().lower()
SHARD_VERSION = (os.environ.get("SHARD_VERSION") or "1.21.10").strip()
SHARD_BUILD = (os.environ.get("SHARD_BUILD") or "latest").strip()
SHARD_JAR_URL = (os.environ.get("SHARD_JAR_URL") or "").strip()
API_BASE = (os.environ.get("SHARDS_API_URL") or "https://shards.zealot.bond").rstrip("/")
RUN_ID = (os.environ.get("GITHUB_RUN_ID") or "").strip()

ROOT_DIR = Path.cwd()
BOX_DIR = ROOT_DIR / ".devlane"
SERVER_DIR = BOX_DIR / "server"
BORE_BIN = BOX_DIR / "bore"

DEFAULT_JAR_URL = "https://fill-data.papermc.io/v1/objects/158703f75a26f842ea656b3dc6d75bf3d1ec176b97a2c36384d0b80b3871af53/paper-1.21.10-130.jar"
BORE_URL = "https://github.com/ekzhang/bore/releases/download/v0.6.0/bore-v0.6.0-x86_64-unknown-linux-musl.tar.gz"

LOCKED_MOTD = r"\u00a7bHosted on shards.zealot.bond"
LOCKED_BOSSBAR_JSON = '{"text":"Hosted on shards.zealot.bond","color":"aqua","bold":true}'

# Keep SGR color codes (\x1b[...m), strip non-color terminal control sequences
CTRL_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[A-LN-Z\\^_`a-ln-z{|}~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[()#][A-Za-z0-9]|[=>])")
PLAIN_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
ISO_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\s+")
BORE_SCRUB_RE = re.compile(r"bore\.pub(:\d+)?", re.IGNORECASE)
DONE_RE = re.compile(r'Done \([0-9.]+s\)! For help, type "help"')
JOIN_RE = re.compile(r":\s+([A-Za-z0-9_.]{2,16})\[/.*?\] logged in with entity id")
LEAVE_RE = re.compile(r":\s+([A-Za-z0-9_]{2,16}) left the game")

SHARDS_ASCII = [
    "\x1b[38;2;99;182;250m\x1b[1m  ____  _                   _     \x1b[0m",
    "\x1b[38;2;99;182;250m\x1b[1m / ___|| |__   __ _ _ __ __| |___ \x1b[0m",
    "\x1b[38;2;99;182;250m\x1b[1m \\___ \\| '_ \\ / _` | '__/ _` / __|\x1b[0m",
    "\x1b[38;2;99;182;250m\x1b[1m  ___) | | | | (_| | | | (_| \\__ \\\x1b[0m",
    "\x1b[38;2;99;182;250m\x1b[1m |____/|_| |_|\\__,_|_|  \\__,_|___/\x1b[0m",
    "\x1b[38;2;147;197;253m  Hosted on shards.zealot.bond\x1b[0m",
]


def find_java_bin():
    jh = os.environ.get("JAVA_HOME_21_X64") or os.environ.get("JAVA_HOME") or ""
    if jh and Path(jh, "bin", "java").exists():
        return str(Path(jh, "bin", "java"))
    for candidate in sorted(glob.glob("/usr/lib/jvm/*21*/bin/java"), reverse=True):
        if os.path.exists(candidate):
            return candidate
    return shutil.which("java") or "java"


def resolve_software_jar_url(software, version, build="latest", explicit_url=""):
    if explicit_url and explicit_url.startswith("https://"):
        return explicit_url, software, version, build
    sw = (software or "paper").lower()
    ver = (version or "1.21.10").strip()
    headers = {"User-Agent": "Shards/1.0 (https://shards.zealot.bond)"}

    def get_json(url):
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.load(r)

    try:
        if sw in ("paper", "folia", "velocity"):
            proj = sw
            d = get_json(f"https://api.papermc.io/v2/projects/{proj}/versions/{ver}/builds")
            builds = d.get("builds") or []
            if builds:
                b = builds[-1] if build in ("", "latest") else next((x for x in builds if str(x.get("build")) == str(build)), builds[-1])
                bnum = b.get("build")
                fname = ((b.get("downloads") or {}).get("application") or {}).get("name") or f"{proj}-{ver}-{bnum}.jar"
                return f"https://api.papermc.io/v2/projects/{proj}/versions/{ver}/builds/{bnum}/downloads/{fname}", sw, ver, str(bnum)
        elif sw == "purpur":
            d = get_json(f"https://api.purpurmc.org/v2/purpur/{ver}")
            builds = (d.get("builds") or {}).get("all") or []
            bnum = d.get("builds", {}).get("latest") if build in ("", "latest") else build
            if not bnum and builds:
                bnum = builds[-1]
            if bnum:
                return f"https://api.purpurmc.org/v2/purpur/{ver}/{bnum}/download", sw, ver, str(bnum)
        elif sw == "leaf":
            d = get_json(f"https://api.leafmc.one/v2/projects/leaf/versions/{ver}/builds")
            builds = d.get("builds") or []
            if builds:
                b = builds[-1] if build in ("", "latest") else next((x for x in builds if str(x.get("build")) == str(build)), builds[-1])
                bnum = b.get("build")
                fname = ((b.get("downloads") or {}).get("primary") or {}).get("name") or f"leaf-{ver}-{bnum}.jar"
                return f"https://api.leafmc.one/v2/projects/leaf/versions/{ver}/builds/{bnum}/downloads/{fname}", sw, ver, str(bnum)
        elif sw == "pufferfish":
            major = "1.20" if ver.startswith("1.20") else ("1.19" if ver.startswith("1.19") else "1.21")
            d = get_json(f"https://ci.pufferfish.host/job/Pufferfish-{major}/lastSuccessfulBuild/api/json")
            arts = d.get("artifacts") or []
            if arts:
                rel = arts[0].get("relativePath")
                bnum = d.get("number") or "latest"
                return f"https://ci.pufferfish.host/job/Pufferfish-{major}/lastSuccessfulBuild/artifact/{rel}", sw, ver, str(bnum)
        elif sw == "fabric":
            return f"https://meta.fabricmc.net/v2/versions/loader/{ver}/0.16.9/1.0.1/server/jar", sw, ver, "0.16.9"
        elif sw == "vanilla":
            manifest = get_json("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
            ventry = next((x for x in manifest.get("versions", []) if x.get("id") == ver), None)
            if ventry and ventry.get("url"):
                vdata = get_json(ventry["url"])
                dl = ((vdata.get("downloads") or {}).get("server") or {}).get("url")
                if dl:
                    return dl, sw, ver, "official"
        elif sw == "spigot":
            return f"https://download.getbukkit.org/spigot/spigot-{ver}.jar", sw, ver, "stable"
    except Exception:
        pass
    return DEFAULT_JAR_URL, "paper", "1.21.10", "130"


class ShardAgent:
    def __init__(self):
        self.lock = threading.Lock()
        self.sync_lock = threading.Lock()
        self.sync_event = threading.Event()
        self.logs = []
        self.next_seq = 1
        self.last_sent_seq = 0

        self.software = SHARD_SOFTWARE
        self.version = SHARD_VERSION
        self.build = SHARD_BUILD
        self.jar_url = SHARD_JAR_URL

        self.state = "creating"
        self.step = "Initializing environment..."
        self.progress = 15
        self.bore_port = 0
        self.bore_address = ""
        self.relay_ip = ""
        self.mc_proc = None
        self.mc_pty_fd = None
        self.bore_proc = None
        self.mc_ever_up = False
        self.desired_mc = "running"
        self.should_exit = False
        self.started_at = time.time()
        self.expires_at = self.started_at + (DURATION_MIN * 60)

        self.online_players = set()
        self.oidc_token = ""
        self.oidc_exp = 0
        self.cmd_results = []
        self.files_dirty = True
        self.last_files_sync = 0
        self.last_stats_time = 0
        self.last_bossbar_sync = 0
        self.stats = {
            "players_online": 0,
            "players_max": 20,
            "player_names": [],
            "ram_used_mb": 0,
            "ram_total_mb": 16384,
            "disk_used_mb": 0,
            "disk_total_mb": 15360,
            "cpu_load": 0.0,
        }

    def print_ascii_banner(self):
        for line in SHARDS_ASCII:
            self.log_line(line)

    def log_line(self, line, daemon=False):
        raw = line.rstrip("\r\n")
        clean = CTRL_ANSI_RE.sub("", raw)
        clean = ISO_PREFIX_RE.sub("", clean)
        plain = PLAIN_ANSI_RE.sub("", clean).strip()
        if not plain:
            return
        # Suppress JLine warning and internal bossbar feedback spam
        if (
            "Advanced terminal features are not available in this environment" in plain
            or "Custom bossbar shards:branding" in plain
            or "No bossbar exists with the ID" in plain
            or "is already set to that value" in plain
            or "Nothing changed. That's already the style" in plain
            or "Nothing changed. That's already the color" in plain
            or "Nothing changed. That bossbar is already visible" in plain
            or "No player was found" in plain
        ):
            return
        clean = BORE_SCRUB_RE.sub(f"{SHARD_NAME}.zealot.bond", clean)
        if daemon:
            entry = f"container@shards~ {clean}"
        else:
            entry = clean
        print(entry, flush=True)
        with self.lock:
            self.logs.append((self.next_seq, entry))
            self.next_seq += 1
            if len(self.logs) > 2500:
                self.logs = self.logs[-2000:]
        self.sync_event.set()

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
        except Exception:
            pass
        return self.oidc_token

    def resolve_safe_path(self, rel_path):
        rel = (rel_path or "").strip().lstrip("/")
        target = (SERVER_DIR / rel).resolve()
        root = SERVER_DIR.resolve()
        if target != root and root not in target.parents:
            raise ValueError("Path escapes server workspace")
        return target

    def enforce_branding_files(self):
        SERVER_DIR.mkdir(parents=True, exist_ok=True)
        (SERVER_DIR / "eula.txt").write_text("eula=true\n", encoding="utf-8")
        props_path = SERVER_DIR / "server.properties"
        if not props_path.exists():
            props_path.write_text(
                f"""#Minecraft server properties
server-port=25565
online-mode=true
max-players=20
view-distance=8
simulation-distance=6
motd={LOCKED_MOTD}
difficulty=normal
gamemode=survival
pvp=true
spawn-protection=0
enable-query=false
enforce-secure-profile=false
""",
                encoding="utf-8",
            )
            return
        try:
            raw = props_path.read_text(encoding="utf-8", errors="replace")
            lines = raw.splitlines()
            out = []
            found_motd = False
            changed = False
            for ln in lines:
                s = ln.strip()
                if s.startswith("motd="):
                    found_motd = True
                    if s != f"motd={LOCKED_MOTD}":
                        changed = True
                    out.append(f"motd={LOCKED_MOTD}")
                else:
                    out.append(ln)
            if not found_motd:
                out.append(f"motd={LOCKED_MOTD}")
                changed = True
            if changed:
                props_path.write_text("\n".join(out) + "\n", encoding="utf-8")
                self.files_dirty = True
        except Exception:
            pass

    def enforce_bossbar(self):
        if self.state != "running":
            return
        proc = self.mc_proc
        if not proc or proc.poll() is not None:
            return
        cmds = [
            f"bossbar add shards:branding {LOCKED_BOSSBAR_JSON}",
            f"bossbar set shards:branding name {LOCKED_BOSSBAR_JSON}",
            "bossbar set shards:branding color blue",
            "bossbar set shards:branding style progress",
            "bossbar set shards:branding value 100",
            "bossbar set shards:branding max 100",
            "bossbar set shards:branding visible true",
            "bossbar set shards:branding players @a",
        ]
        for c in cmds:
            self._write_mc_raw(c + "\n")

    def build_files_snapshot(self):
        if not SERVER_DIR.exists():
            return {"entries": [], "configs": {}}
        entries = []
        configs = {}
        inline_exts = {".properties", ".yml", ".yaml", ".json", ".txt", ".conf", ".toml", ".cfg"}
        inline_names = {
            "server.properties", "eula.txt", "ops.json", "whitelist.json",
            "banned-players.json", "banned-ips.json", "bukkit.yml", "spigot.yml", "commands.yml"
        }
        try:
            for root, dirs, files in os.walk(SERVER_DIR):
                rel_root = Path(root).relative_to(SERVER_DIR)
                depth = len(rel_root.parts)
                if depth > 3:
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
                    if (f in inline_names or (depth <= 2 and fp.suffix.lower() in inline_exts)) and st.st_size <= 65536:
                        try:
                            configs[rp] = fp.read_text(encoding="utf-8", errors="replace")
                        except Exception:
                            pass
        except Exception:
            pass
        return {"entries": entries[:500], "configs": configs}

    def collect_stats(self):
        now = time.time()
        if now - self.last_stats_time < 1.5:
            return
        self.last_stats_time = now
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

        try:
            load1, _, _ = os.getloadavg()
        except Exception:
            load1 = 0.0

        pnames = sorted(self.online_players)
        self.stats = {
            "players_online": len(pnames),
            "players_max": 20,
            "player_names": pnames,
            "ram_used_mb": ram_mb,
            "ram_total_mb": 16384,
            "disk_used_mb": disk_mb,
            "disk_total_mb": 15360,
            "cpu_load": round(load1, 2),
        }

    def start_bore_tunnel(self):
        if self.bore_proc and self.bore_proc.poll() is None:
            return
        try:
            self.relay_ip = socket.gethostbyname("bore.pub")
        except Exception:
            self.relay_ip = "159.223.110.159"

        bore_log = BOX_DIR / "bore.log"
        self.bore_proc = subprocess.Popen(
            [str(BORE_BIN), "local", "25565", "--to", "bore.pub", "--port", "0", "--local-host", "127.0.0.1"],
            stdout=open(bore_log, "w"),
            stderr=subprocess.STDOUT,
            cwd=str(BOX_DIR),
        )
        for _ in range(40):
            if bore_log.exists():
                txt = PLAIN_ANSI_RE.sub("", bore_log.read_text(errors="replace"))
                m = re.search(r"remote_port=(\d+)", txt)
                if m:
                    self.bore_port = int(m.group(1))
                    self.bore_address = f"bore.pub:{self.bore_port}"
                    self.log_line(f"Network route bound -> {SHARD_NAME}.zealot.bond:{self.bore_port}", daemon=True)
                    return
            time.sleep(0.3)

    def _write_mc_raw(self, data_str):
        fd = self.mc_pty_fd
        if fd is not None:
            try:
                os.write(fd, data_str.encode("utf-8"))
                return True
            except OSError:
                pass
        return False

    def start_mc_server(self, initial=False):
        if self.mc_proc and self.mc_proc.poll() is None:
            return
        self.enforce_branding_files()
        self.desired_mc = "running"
        self.online_players.clear()
        if initial and not self.mc_ever_up:
            self.state = "creating"
            self.step = f"Starting {self.software.capitalize()} {self.version}..."
            self.progress = 72
        else:
            self.state = "starting"
            self.step = f"Starting {self.software.capitalize()} {self.version}..."
            self.progress = 80

        self.print_ascii_banner()
        java_bin = find_java_bin()
        self.log_line(f"Starting {self.software.capitalize()} {self.version} (Build {self.build})...", daemon=True)

        master_fd, slave_fd = pty.openpty()
        self.mc_pty_fd = master_fd
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"

        cmd = [
            java_bin,
            "-Xms2G",
            "-Xmx12G",
            "-XX:+UseG1GC",
            "-XX:+ParallelRefProcEnabled",
            "-XX:MaxGCPauseMillis=200",
            "-XX:+UnlockExperimentalVMOptions",
            "-XX:+DisableExplicitGC",
            "-Djava.net.preferIPv4Stack=true",
            "-Dterminal.jline=false",
            "-Dterminal.ansi=true",
            "-Djansi.passthrough=true",
            "-jar",
            str(BOX_DIR / "server.jar"),
            "nogui",
        ]
        self.mc_proc = subprocess.Popen(
            cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=str(SERVER_DIR),
            env=env,
            close_fds=True,
        )
        os.close(slave_fd)
        t = threading.Thread(target=self._read_mc_pty, args=(self.mc_proc, master_fd), daemon=True)
        t.start()

    def _read_mc_pty(self, proc, master_fd):
        buf = b""
        try:
            while True:
                rlist, _, _ = select.select([master_fd], [], [], 0.25)
                if master_fd in rlist:
                    try:
                        chunk = os.read(master_fd, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        raw_line, buf = buf.split(b"\n", 1)
                        line = raw_line.decode("utf-8", errors="replace")
                        self._process_mc_line(line)
                elif proc.poll() is not None:
                    break
            if buf:
                self._process_mc_line(buf.decode("utf-8", errors="replace"))
        except Exception:
            pass
        finally:
            try:
                os.close(master_fd)
            except OSError:
                pass
            if self.mc_pty_fd == master_fd:
                self.mc_pty_fd = None
            rc = proc.wait()
            if self.mc_proc is proc:
                self.online_players.clear()
                if self.desired_mc == "stopped":
                    self.state = "stopped"
                    self.step = "Offline"
                    self.log_line("Server marked as offline.", daemon=True)
                elif self.desired_mc == "restarting":
                    self.log_line("Server stopped for restart.", daemon=True)
                elif not self.should_exit:
                    self.state = "stopped"
                    self.step = f"Offline (exit code {rc})"
                    self.log_line(f"Process exited with code {rc}.", daemon=True)
                self.files_dirty = True

    def _process_mc_line(self, line):
        self.log_line(line)
        plain = PLAIN_ANSI_RE.sub("", line)
        if not self.mc_ever_up:
            if "Loading libraries" in plain or "Downloading" in plain:
                self.step = "Loading libraries..."
                self.progress = 78
            elif "Preparing level" in plain:
                self.step = "Generating world..."
                self.progress = 86
            elif "Preparing start region" in plain:
                self.step = "Preparing spawn area..."
                self.progress = 94
        if DONE_RE.search(plain):
            self.mc_ever_up = True
            self.state = "running"
            self.step = "Running"
            self.progress = 100
            self.files_dirty = True
            self.log_line(f"Server marked as running ({SHARD_NAME}.zealot.bond)", daemon=True)
            threading.Timer(1.0, self.enforce_bossbar).start()

        jm = JOIN_RE.search(plain)
        if jm:
            self.online_players.add(jm.group(1))
            threading.Timer(0.8, self.enforce_bossbar).start()
        lm = LEAVE_RE.search(plain)
        if lm:
            self.online_players.discard(lm.group(1))

    def stop_mc_server(self, for_restart=False):
        self.desired_mc = "restarting" if for_restart else "stopped"
        proc = self.mc_proc
        if not proc or proc.poll() is not None:
            if not for_restart:
                self.state = "stopped"
                self.step = "Offline"
            return
        self.state = "restarting" if for_restart else "stopping"
        self.step = "Restarting..." if for_restart else "Stopping..."
        self.log_line("Sending stop command to server...", daemon=True)
        self._write_mc_raw("save-all\nstop\n")
        for _ in range(25):
            if proc.poll() is not None:
                break
            time.sleep(0.5)
        if proc.poll() is None:
            self.log_line("Terminating unresponsive process...", daemon=True)
            try:
                proc.kill()
            except Exception:
                pass
            proc.wait(timeout=5)
        if not for_restart:
            self.state = "stopped"
            self.step = "Offline"
        self.files_dirty = True

    def restart_mc_server(self):
        self.stop_mc_server(for_restart=True)
        time.sleep(0.8)
        self.start_mc_server(initial=False)

    def switch_version(self, software, version, build, jar_url):
        sw_url, sw, ver, bnum = resolve_software_jar_url(software, version, build, jar_url)
        self.log_line(f"Switching server version to {sw.capitalize()} {ver} (build {bnum})...", daemon=True)
        was_running = self.mc_proc and self.mc_proc.poll() is None
        if was_running:
            self.stop_mc_server(for_restart=True)
        self.state = "starting"
        self.step = f"Installing {sw.capitalize()} {ver}..."
        tmp_jar = BOX_DIR / "server.jar.new"
        try:
            subprocess.run(["curl", "-fsSL", "-A", "Shards/1.0", "-o", str(tmp_jar), sw_url], check=True)
            if tmp_jar.exists() and tmp_jar.stat().st_size > 100000:
                tmp_jar.replace(BOX_DIR / "server.jar")
                self.software = sw
                self.version = ver
                self.build = bnum
                self.jar_url = sw_url
                self.log_line(f"Installed {sw.capitalize()} {ver} (#{bnum}). Worlds and configs preserved.", daemon=True)
            else:
                raise RuntimeError("Downloaded jar was empty or invalid")
        except Exception as exc:
            self.log_line(f"Version switch failed: {exc}", daemon=True)
        self.start_mc_server(initial=False)

    def install_plugin(self, name, filename, download_url):
        plugins_dir = SERVER_DIR / "plugins"
        plugins_dir.mkdir(parents=True, exist_ok=True)
        safe_fname = re.sub(r"[^A-Za-z0-9._-]", "-", filename or "plugin.jar")
        if not safe_fname.endswith(".jar"):
            safe_fname += ".jar"
        target = plugins_dir / safe_fname
        self.log_line(f"Installing plugin {name} ({safe_fname})...", daemon=True)
        try:
            subprocess.run(["curl", "-fsSL", "-L", "-A", "Shards/1.0", "-o", str(target), download_url], check=True)
            self.files_dirty = True
            self.log_line(f"Installed plugin {safe_fname} into /home/container/plugins/.", daemon=True)
        except Exception as exc:
            self.log_line(f"Failed to install plugin {name}: {exc}", daemon=True)

    def send_mc_command(self, command):
        cmd = (command or "").strip()
        if cmd.startswith("/"):
            cmd = cmd[1:]
        if not cmd:
            return
        low = cmd.lower()
        if low.startswith("bossbar") or low.startswith("minecraft:bossbar"):
            self.log_line("Branding (MOTD & BossBar) is read-only and cannot be modified.", daemon=True)
            return
        proc = self.mc_proc
        if not proc or proc.poll() is not None:
            self.log_line("Cannot send command — server is offline.", daemon=True)
            return
        self._write_mc_raw(cmd + "\n")

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
            elif ctype == "change_version":
                threading.Thread(
                    target=self.switch_version,
                    args=(
                        cmd_obj.get("software") or "paper",
                        cmd_obj.get("version") or "1.21.10",
                        cmd_obj.get("build") or "latest",
                        cmd_obj.get("jar_url") or "",
                    ),
                    daemon=True,
                ).start()
            elif ctype == "plugin_install":
                threading.Thread(
                    target=self.install_plugin,
                    args=(
                        cmd_obj.get("name") or "Plugin",
                        cmd_obj.get("filename") or "plugin.jar",
                        cmd_obj.get("url") or "",
                    ),
                    daemon=True,
                ).start()
            elif ctype == "shard_suspend":
                self.should_exit = True
                threading.Thread(target=self.stop_mc_server, daemon=True).start()
            elif ctype == "file_write":
                target = self.resolve_safe_path(cmd_obj.get("path") or "")
                target.parent.mkdir(parents=True, exist_ok=True)
                if cmd_obj.get("encoding") == "base64":
                    target.write_bytes(base64.b64decode(cmd_obj.get("content") or ""))
                else:
                    target.write_text(cmd_obj.get("content") or "", encoding="utf-8")
                if target.name == "server.properties":
                    self.enforce_branding_files()
                self.files_dirty = True
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
            elif ctype == "file_rename":
                src = self.resolve_safe_path(cmd_obj.get("old_path") or "")
                dst = self.resolve_safe_path(cmd_obj.get("new_path") or "")
                if src == SERVER_DIR.resolve():
                    raise ValueError("Cannot rename workspace root")
                dst.parent.mkdir(parents=True, exist_ok=True)
                src.rename(dst)
                self.files_dirty = True
            elif ctype == "file_delete":
                target = self.resolve_safe_path(cmd_obj.get("path") or "")
                if target == SERVER_DIR.resolve():
                    raise ValueError("Cannot delete workspace root")
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                if target.name == "server.properties":
                    self.enforce_branding_files()
                self.files_dirty = True
        except Exception as exc:
            res["ok"] = False
            res["error"] = str(exc)
        with self.lock:
            self.cmd_results.append(res)
        self.sync_event.set()

    def sync_once(self):
        if not SHARD_OWNER or not API_BASE:
            return False
        if not self.sync_lock.acquire(blocking=False):
            return False
        try:
            self.collect_stats()
            with self.lock:
                new_logs = [{"seq": seq, "line": line} for seq, line in self.logs if seq > self.last_sent_seq]
                results = list(self.cmd_results)
                self.cmd_results.clear()

            now = time.time()
            files_snap = None
            if self.files_dirty or (now - self.last_files_sync > 10):
                files_snap = self.build_files_snapshot()
                self.files_dirty = False
                self.last_files_sync = now

            payload = {
                "owner": SHARD_OWNER,
                "shard_id": SHARD_ID,
                "name": SHARD_NAME,
                "run_id": RUN_ID,
                "software": self.software,
                "version": self.version,
                "build": self.build,
                "state": self.state,
                "step": self.step,
                "progress": self.progress,
                "bore_port": self.bore_port,
                "bore_address": self.bore_address,
                "relay_ip": self.relay_ip,
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
                with urllib.request.urlopen(req, timeout=8) as resp:
                    resp_data = json.load(resp)
                    if new_logs:
                        self.last_sent_seq = new_logs[-1]["seq"]
                    if resp_data.get("suspend"):
                        self.should_exit = True
                        return True
                    for cmd in resp_data.get("commands") or []:
                        self.handle_command(cmd)
                    return len(new_logs) > 0
            except Exception:
                return False
        finally:
            self.sync_lock.release()

    def _sync_worker(self):
        while not self.should_exit:
            self.sync_event.wait(timeout=0.35)
            self.sync_event.clear()
            self.sync_once()
            time.sleep(0.12)

    def setup_workspace(self):
        BOX_DIR.mkdir(parents=True, exist_ok=True)
        SERVER_DIR.mkdir(parents=True, exist_ok=True)
        (SERVER_DIR / "plugins").mkdir(parents=True, exist_ok=True)

        sw_url, sw, ver, bnum = resolve_software_jar_url(self.software, self.version, self.build, self.jar_url)
        self.software = sw
        self.version = ver
        self.build = bnum
        self.jar_url = sw_url

        self.step = f"Pulling {sw.capitalize()} {ver} image..."
        self.progress = 30
        self.log_line(f"Pulling {sw.capitalize()} {ver} (#{bnum}) for {SHARD_NAME}.zealot.bond...", daemon=True)

        jar_path = BOX_DIR / "server.jar"
        if not jar_path.exists():
            subprocess.run(["curl", "-fsSL", "-L", "-A", "Shards/1.0", "-o", str(jar_path), sw_url], check=True)
        if not BORE_BIN.exists():
            tar_path = BOX_DIR / "bore.tar.gz"
            subprocess.run(["curl", "-fsSL", "-o", str(tar_path), BORE_URL], check=True)
            subprocess.run(["tar", "xzf", str(tar_path), "-C", str(BOX_DIR)], check=True)
            BORE_BIN.chmod(0o755)

        self.enforce_branding_files()
        self.files_dirty = True

        self.step = f"Binding {SHARD_NAME}.zealot.bond..."
        self.progress = 55
        self.start_bore_tunnel()

    def run(self):
        sync_thread = threading.Thread(target=self._sync_worker, daemon=True)
        sync_thread.start()

        self.setup_workspace()
        self.start_mc_server(initial=True)

        while not self.should_exit and time.time() < self.expires_at:
            if self.bore_proc and self.bore_proc.poll() is not None:
                self.start_bore_tunnel()
            self.enforce_branding_files()
            now = time.time()
            if self.state == "running" and now - self.last_bossbar_sync > 12:
                self.last_bossbar_sync = now
                self.enforce_bossbar()
            time.sleep(1.5)

        self.stop_mc_server()
        self.state = "expired"
        self.step = "Expired"
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
