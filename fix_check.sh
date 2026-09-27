#!/bin/bash
# FGO fix checker - runs on the Mac. Gathers state and POSTs a copy to the
# Windows receiver (10.0.0.109:8099) so the engineer can read it without paste.
# Run:  bash $(ls -t ~/Downloads/*fix_check*.sh | head -1)
OUT="/tmp/fgo_fix_check.txt"
PKG=~/fgo/app-source/firstgeneralorder
CORE="$PKG/services/core.py"
SGM="$PKG/streamgate.py"
NETG="$PKG/netgate.py"
{
echo "=== FGO FIX CHECK ==="
echo "time: $(date)"
echo "--- launchctl (all FGO agents) ---"
launchctl list | grep -i firstgeneralorder
echo "--- gateway.err.log (last 15) ---"
tail -n 15 ~/Library/Logs/fgo/gateway.err.log 2>/dev/null || echo "no gateway.err.log"
echo "--- core.err.log (last 8) ---"
tail -n 8 ~/Library/Logs/fgo/core.err.log 2>/dev/null || echo "no core.err.log"
echo "--- stream.json ---"
cat ~/.local/share/first-general-order-machine/runtime/stream.json 2>/dev/null || echo "stream.json NOT PRESENT"
echo "--- installed files ---"
if [ -f "$CORE" ]; then
  echo "  services/core.py: stream=($(grep -cF _start_stream_server "$CORE")) NULLfix=($(grep -cF 'or 8)' "$CORE")) mtime=$(stat -f %Sm "$CORE")"
else
  echo "  services/core.py MISSING"
fi
if [ -f "$SGM" ]; then
  echo "  streamgate.py: StreamServer=($(grep -cF 'class StreamServer' "$SGM")) mtime=$(stat -f %Sm "$SGM")"
else
  echo "  streamgate.py MISSING"
fi
if [ -f "$NETG" ]; then
  echo "  netgate.py: STREAM_SUPPLIER=($(grep -cF STREAM_SUPPLIER "$NETG")) bytes=$(stat -f %z "$NETG") mtime=$(stat -f %Sm "$NETG")"
else
  echo "  netgate.py MISSING"
fi
echo "--- ~/Downloads matching files (newest first) ---"
ls -lt ~/Downloads 2>/dev/null | grep -iE "fix_check|fix-stream|core|streamgate|netgate" | head -25
} > "$OUT" 2>&1

cat "$OUT"
echo ""
echo "[posting to receiver ...]"
if curl -s -m 15 -X POST --data-binary @"$OUT" http://10.0.0.109:8099/diag >/dev/null; then
  echo "posted OK"
else
  echo "LAN post failed"
fi
