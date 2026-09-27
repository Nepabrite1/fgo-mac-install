#!/bin/bash
# Check where the FGO gateway is actually broadcasting. Listens ~12s on the
# loopback (127.0.0.1) and on the LAN IP (10.0.0.114) separately, then POSTs
# results to the Windows receiver. Run: bash ~/Downloads/gw_lan_check.sh
set -u
OUT=/tmp/gw_lan_check.txt
LANIP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "10.0.0.114")
{
echo "=== GW LAN CHECK ==="
echo "time: $(date)"
echo "lanip: $LANIP"
echo "== gateway err log tail (current) =="
tail -n 4 ~/Library/Logs/fgo/gateway.err.log 2>/dev/null || echo "no log"
echo "== listen on loopback 127.0.0.1:15195 (12s) =="
python3 - <<'PY'
import socket, time
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.bind(("127.0.0.1",15195)); s.settimeout(2)
c=0; dl=time.time()+12
while time.time()<dl:
    try: s.recvfrom(65535); c+=1
    except socket.timeout: pass
print("loopback beacons: %d"%c)
PY
echo "== listen on LAN $LANIP:15195 (12s) =="
python3 - "$LANIP" <<'PY'
import socket, time, sys
host=sys.argv[1]
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
try:
    s.bind((host,15195)); s.settimeout(2)
except Exception as e:
    print("bind failed:", e); raise SystemExit
c=0; dl=time.time()+12
while time.time()<dl:
    try: s.recvfrom(65535); c+=1
    except socket.timeout: pass
print("lan beacons: %d"%c)
PY
echo "== END =="
} > "$OUT" 2>&1

cat "$OUT"
echo ""
curl -s -m 15 -X POST --data-binary @"$OUT" http://10.0.0.109:8099/diag >/dev/null && echo "posted OK" || echo "post failed"
