#!/bin/bash
# FGO fix checker - runs on the Mac. Gathers state and POSTs a copy to the
# Windows receiver (10.0.0.109:8099) so the engineer can read it without paste.
# Run:  bash $(ls -t ~/Downloads/*fix_check*.sh | head -1)
OUT="/tmp/fgo_fix_check.txt"
CORE=~/fgo/app-source/firstgeneralorder/services/core.py
SGM=~/fgo/app-source/firstgeneralorder/streamgate.py
{
echo "=== FGO FIX CHECK ==="
echo "time: $(date)"
echo "--- launchctl (all FGO agents) ---"
launchctl list | grep -i firstgeneralorder
echo "--- gateway.err.log (last 12) ---"
tail -n 12 ~/Library/Logs/fgo/gateway.err.log 2>/dev/null || echo "no gateway.err.log"
echo "--- core.err.log (last 12) ---"
tail -n 12 ~/Library/Logs/fgo/core.err.log 2>/dev/null || echo "no core.err.log"
echo "--- stream.json ---"
cat ~/.local/share/first-general-order-machine/runtime/stream.json 2>/dev/null || echo "stream.json NOT PRESENT"
echo "--- installed services/core.py (correct code?) ---"
if [ -f "$CORE" ]; then
  echo "  _start_stream_server count: $(grep -cF _start_stream_server "$CORE")"
  echo "  StreamServer           count: $(grep -cF StreamServer "$CORE")"
  echo "  NULL-fix (or 8)        count: $(grep -cF 'or 8)' "$CORE")"
  echo "  mtime: $(stat -f %Sm "$CORE")"
else
  echo "  services/core.py MISSING"
fi
echo "--- installed streamgate.py ---"
if [ -f "$SGM" ]; then
  echo "  class StreamServer count: $(grep -cF 'class StreamServer' "$SGM")"
  echo "  mtime: $(stat -f %Sm "$SGM")"
else
  echo "  streamgate.py MISSING"
fi
echo "--- ~/Downloads matching files (newest first) ---"
ls -lt ~/Downloads 2>/dev/null | grep -iE "fix_check|fix-stream|core|streamgate" | head -20
} > "$OUT" 2>&1

cat "$OUT"
echo ""
echo "[posting to receiver ...]"
if curl -s -m 15 -X POST --data-binary @"$OUT" http://10.0.0.109:8099/diag >/dev/null; then
  echo "posted OK"
else
  echo "LAN post failed"
fi
