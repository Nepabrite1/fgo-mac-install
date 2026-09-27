#!/bin/bash
# Clean restart of FGO core + gateway with ALL bytecode caches removed, then
# listens on 127.0.0.1:15195 to confirm the gateway is actually advertising its
# beacon. Posts the whole result back to the Windows receiver automatically.
# Run on the Mac:   bash ~/Downloads/gw_restart.sh
set -u
SRC="$HOME/fgo/app-source"
OUT=/tmp/gw_restart.txt
{
echo "=== GW RESTART ==="
echo "time: $(date)"
echo "== clearing ALL __pycache__ under app-source =="
find "$SRC" -type d -name __pycache__ -exec rm -rf {} +
echo "  cache cleared"

echo "== restarting core + gateway =="
launchctl kickstart -k gui/501/com.firstgeneralorder.core
launchctl kickstart -k gui/501/com.firstgeneralorder.gateway
sleep 9

echo "== launchctl state =="
launchctl list | grep -i firstgeneralorder

echo "== listening for the gateway's own beacon on 127.0.0.1:15195 (10s) =="
PYFILE=$(mktemp /tmp/gwlisten.XXXX.py)
cat > "$PYFILE" <<'PY'
import socket, time
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", 15195))
s.settimeout(2)
count = 0
deadline = time.time() + 10
while time.time() < deadline:
    try:
        data, _ = s.recvfrom(65535)
        count += 1
    except socket.timeout:
        continue
print("beacons received on 127.0.0.1:15195:", count)
PY
python3 "$PYFILE"
rm -f "$PYFILE"

echo "== gateway.err.log (last 6) =="
tail -n 6 ~/Library/Logs/fgo/gateway.err.log 2>/dev/null || echo "no gateway log"
echo "== END =="
} > "$OUT" 2>&1

cat "$OUT"
echo ""
echo "[posting ...]"
curl -s -m 15 -X POST --data-binary @"$OUT" http://10.0.0.109:8099/diag >/dev/null && echo "posted OK" || echo "post failed"
