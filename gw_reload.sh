#!/bin/bash
# Fully unload + reload the FGO core & gateway launch-agents so they recompile
# from the correct (fixed) source, then listen for the beacon. Posts back.
# Run: bash $(ls -t ~/Downloads/*gw_reload*.sh | head -1)
set -u
SRC="$HOME/fgo/app-source"
LA="$HOME/Library/LaunchAgents"
OUT=/tmp/gw_reload.txt
{
echo "=== GW RELOAD ==="
echo "time: $(date)"
echo "== clear ALL __pycache__ =="
find "$SRC" -type d -name __pycache__ -exec rm -rf {} +
echo "== launch agents present =="
ls -1 "$LA"/com.firstgeneralorder.gateway.plist "$LA"/com.firstgeneralorder.core.plist 2>/dev/null || ls -1 "$LA"/com.firstgeneralorder*.plist 2>/dev/null || echo "no plists in $LA"
echo "== unload + reload gateway + core =="
launchctl unload "$LA/com.firstgeneralorder.gateway.plist" 2>&1 || echo "gw unload: none/warn"
launchctl unload "$LA/com.firstgeneralorder.core.plist" 2>&1 || echo "core unload: none/warn"
sleep 3
launchctl load "$LA/com.firstgeneralorder.gateway.plist" 2>&1 || echo "gw load error"
launchctl load "$LA/com.firstgeneralorder.core.plist" 2>&1 || echo "core load error"
sleep 10
echo "== launchctl =="
launchctl list | grep -i firstgeneralorder
echo "== listen 0.0.0.0:15195 (10s) =="
python3 - <<'PY'
import socket, time
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM); s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
s.bind(("0.0.0.0",15195)); s.settimeout(2); c=0; dl=time.time()+10
while time.time()<dl:
    try: s.recvfrom(65535); c+=1
    except socket.timeout: pass
print("any-interface beacons: %d"%c)
PY
echo "== END =="
} > "$OUT" 2>&1
cat "$OUT"
echo ""
curl -s -m 15 -X POST --data-binary @"$OUT" http://10.0.0.109:8099/diag >/dev/null && echo "posted OK" || echo "post failed"
