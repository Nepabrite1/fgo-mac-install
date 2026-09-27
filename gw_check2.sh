#!/bin/bash
# Confirm the fixed netgate is installed and whether the gateway broadcasts on
# the LAN. Listens on 0.0.0.0:15195 (catches any beacon on any interface).
# Run: bash ~/Downloads/gw_check2.sh
set -u
OUT=/tmp/gw_check2.txt
NETG=~/fgo/app-source/firstgeneralorder/netgate.py
{
echo "=== GW CHECK2 ==="
echo "time: $(date)"
echo "== installed netgate.py markers =="
if [ -f "$NETG" ]; then
  echo "  STREAM_SUPPLIER      : $(grep -cF STREAM_SUPPLIER "$NETG")"
  echo "  _subnet_broadcast    : $(grep -cF _subnet_broadcast "$NETG")"
  echo "  bytes: $(stat -f %z "$NETG") mtime: $(stat -f %Sm "$NETG")"
else
  echo "  netgate.py MISSING"
fi
echo "== gateway launchctl =="
launchctl list | grep -i "firstgeneralorder.gateway"
echo "== listen on 0.0.0.0:15195 (12s) =="
python3 - <<'PY'
import socket, time
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
try:
    s.bind(("0.0.0.0",15195))
except Exception as e:
    print("bind failed:", e); raise SystemExit
s.settimeout(2); c=0; dl=time.time()+12
while time.time()<dl:
    try: s.recvfrom(65535); c+=1
    except socket.timeout: pass
print("any-interface beacons: %d"%c)
PY
echo "== gateway.err.log (last 5) =="
tail -n 5 ~/Library/Logs/fgo/gateway.err.log 2>/dev/null || echo "no log"
echo "== END =="
} > "$OUT" 2>&1

cat "$OUT"
echo ""
curl -s -m 15 -X POST --data-binary @"$OUT" http://10.0.0.109:8099/diag >/dev/null && echo "posted OK" || echo "post failed"
