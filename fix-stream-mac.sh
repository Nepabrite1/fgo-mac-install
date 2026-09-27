#!/bin/bash
# FGO Mac setup: install the newest core.py + streamgate.py into the CORRECT
# folders, clear stale caches, and restart core + gateway.
# Run ON the Mac:   bash ~/Downloads/fix-stream-mac.sh
set -u

SRC="$HOME/fgo/app-source/firstgeneralorder"
DL="$HOME/Downloads"

say() { echo "== $*"; }

# Return the newest existing file among the given candidates (handles the
# "core (1).py" Safari rename problem).
pick_newest() {
  local newest="" newest_t=0 f t
  for f in "$@"; do
    if [ -f "$f" ]; then
      t=$(stat -f %m "$f")
      if [ -n "$t" ] && [ "$t" -gt "$newest_t" ]; then newest="$f"; newest_t="$t"; fi
    fi
  done
  printf '%s' "$newest"
}

f=$(pick_newest "$DL"/core.py "$DL"/core\ \(1\).py "$DL"/core\ \(2\).py "$DL"/core\ \(3\).py)
if [ -n "$f" ]; then
  cp "$f" "$SRC/services/core.py" && say "installed core.py <- $f"
else
  say "!! core.py not found in $DL -- download it from the repo first"
fi

f=$(pick_newest "$DL"/streamgate.py "$DL"/streamgate\ \(1\).py "$DL"/streamgate\ \(2\).py "$DL"/streamgate\ \(3\).py)
if [ -n "$f" ]; then
  cp "$f" "$SRC/streamgate.py" && say "installed streamgate.py <- $f"
else
  say "!! streamgate.py not found in $DL -- download it from the repo first"
fi

# Remove the wrongly-placed top-level core.py if it exists (it belongs in services/)
rm -f "$SRC/core.py"

say "clearing stale bytecode caches"
rm -rf "$SRC/__pycache__" "$SRC/services/__pycache__"

say "restarting core"
launchctl kickstart -k gui/501/com.firstgeneralorder.core

say "restarting gateway"
launchctl kickstart -k gui/501/com.firstgeneralorder.gateway

say "DONE - core now running streaming code"
