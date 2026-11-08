#!/usr/bin/env bash
# Glasslane dev lane: create a dir, boot Paper inside it, expose via bore,
# self-check through the relay, hold, then tear down. Dev/test harness only.
set -euo pipefail

DURATION="${1:-8}"
# Never exceed the workflow's 300-minute cap; leave headroom for setup/cleanup.
if [ "$DURATION" -gt 280 ]; then DURATION=280; fi

BOX="$(pwd)/.devlane"
JAR_URL="https://fill-data.papermc.io/v1/objects/158703f75a26f842ea656b3dc6d75bf3d1ec176b97a2c36384d0b80b3871af53/paper-1.21.10-130.jar"
BORE_URL="https://github.com/ekzhang/bore/releases/download/v0.6.0/bore-v0.6.0-x86_64-unknown-linux-musl.tar.gz"

echo "::group::1) create workspace"
mkdir -p "$BOX"
cd "$BOX"
echo "  workspace: $BOX"
echo "::endgroup::"

echo "::group::2) fetch Paper + bore"
curl -fsSL -o server.jar "$JAR_URL"
curl -fsSL -o bore.tar.gz "$BORE_URL"
tar xzf bore.tar.gz
chmod +x bore
echo "  java: $(java -version 2>&1 | head -1)"
echo "  jar : $(du -h server.jar | cut -f1)"
echo "  bore: $(./bore --version 2>&1 | head -1)"
echo "::endgroup::"

echo "::group::3) accept EULA, configure"
printf 'eula=true\n' > eula.txt
cat > server.properties <<'PROPS'
# Minimal dev/test config
server-port=25565
online-mode=true
max-players=10
view-distance=6
simulation-distance=4
motd=Glasslane dev lane
level-type=flat
spawn-protection=0
enable-query=false
PROPS
echo "::endgroup::"

echo "::group::4) start Minecraft server"
nohup java -Xms512M -Xmx4G -jar server.jar nogui > "$BOX/server.log" 2>&1 &
MC_PID=$!
echo "  MC pid: $MC_PID"
# Wait for the local port to listen (up to 120s)
for i in $(seq 1 60); do
  if (echo > /dev/tcp/127.0.0.1/25565) >/dev/null 2>&1; then echo "  local 25565 is up (t=${i}x2s)"; break; fi
  if ! kill -0 "$MC_PID" 2>/dev/null; then echo "::error::MC process died"; tail -40 "$BOX/server.log"; exit 1; fi
  sleep 2
done
echo "::endgroup::"

echo "::group::5) start bore raw-TCP relay"
# Port 0 lets the relay assign a free public port. We capture the assigned port
# from the log; the real system will report this back to a Worker that creates DNS.
./bore local 25565 --to bore.pub --port 0 --local-host 127.0.0.1 > "$BOX/bore.log" 2>&1 &
BORE_PID=$!
PUBLIC_PORT=""
for i in $(seq 1 30); do
  PUBLIC_PORT=$(grep -oE 'remote_port=[0-9]+' "$BOX/bore.log" 2>/dev/null | head -1 | cut -d= -f2 || true)
  [ -n "$PUBLIC_PORT" ] && break
  sleep 1
done
if [ -z "$PUBLIC_PORT" ]; then
  echo "::error::bore did not report a port"; cat "$BOX/bore.log"; exit 1
fi
export PUBLIC_PORT
echo "  GLASSLANE_ADDRESS=bore.pub:${PUBLIC_PORT}"
echo "  bore.pub resolves to: $(getent ahostsv4 bore.pub | head -1 | awk '{print $1}')"
echo "::endgroup::"

echo "::group::6) self-check: real Minecraft handshake through the relay"
python3 - <<'PY'
import socket, struct, json, os
port = int(os.environ["PUBLIC_PORT"])
def wv(n):
    o=b''
    while True:
        b=n&0x7f; n>>=7; o+=bytes([b|(0x80 if n else 0)])
        if not n: return o
def rv(s):
    num=0; sh=0
    while True:
        c=s.recv(1)
        if not c: raise EOFError
        b=c[0]; num|=(b&0x7f)<<sh
        if not (b&0x80): return num
        sh+=7
def rx(s,n):
    d=b''
    while len(d)<n:
        c=s.recv(n-len(d))
        if not c: raise EOFError
        d+=c
    return d
s=socket.create_connection(("bore.pub",port),timeout=15)
host=b"bore.pub"
body=b"\x00"+wv(765)+wv(len(host))+host+struct.pack(">H",port)+b"\x01"
s.sendall(wv(len(body))+body)
s.sendall(wv(1)+b"\x00")
ln=rv(s); data=rx(s,ln); i=0
while True:
    b=data[i]; i+=1
    if not (b&0x80): break
jl=0; sh=0
while True:
    b=data[i]; i+=1; jl|=(b&0x7f)<<sh
    if not (b&0x80): break
    sh+=7
d=json.loads(data[i:i+jl].decode("utf-8","replace"))
print("  handshake OK -> %s, players %s/%s, motd=%s" % (
    d["version"]["name"], d["players"]["online"], d["players"]["max"],
    str(d.get("description"))[:50]))
s.close()
PY
echo "::endgroup::"

echo "::group::7) hold for ${DURATION} min"
echo "  lane is live at bore.pub:${PUBLIC_PORT}"
echo "  (server console tail follows at the end of the job)"
sleep "$((DURATION * 60))"
echo "::endgroup::"

echo "::group::8) teardown"
kill "$BORE_PID" "$MC_PID" 2>/dev/null || true
wait "$BORE_PID" 2>/dev/null || true
wait "$MC_PID" 2>/dev/null || true
echo "  --- last 25 console lines ---"
tail -25 "$BOX/server.log" 2>/dev/null || true
echo "  lane expired; nothing persisted"
echo "::endgroup::"
