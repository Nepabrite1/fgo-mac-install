#!/bin/bash
# FGO Mac setup: install the NEWEST core.py + streamgate.py + netgate.py into
# the correct folders, clear stale caches, and restart core + gateway.
# Picks the newest matching file so Safari's "core-2.py"/"core (2).py" renaming
# can never install a stale copy. Run on the Mac:
#   bash $(ls -t ~/Downloads/*fix-stream-mac*.sh | head -1)
set -u

SRC="$HOME/fgo/app-source/firstgeneralorder"
DL="$HOME/Downloads"
say() { echo "== $*"; }

# Newest existing file among the given names (by mtime).
pick_newest() {
  local newest="" newest_t=0 f t
  for f in "$@"; do
    [ -f "$f" ] || continue
    t=$(stat -f %m "$f")
    if [ -n "$t" ] && [ "$t" -gt "$newest_t" ]; then newest="$f"; newest_t="$t"; fi
  done
  printf '%s' "$newest"
}

f=$(pick_newest "$DL"/core*.py)
if [ -n "$f" ]; then
  cp "$f" "$SRC/services/core.py" && say "installed core.py <- $f"
else
  say "!! no core*.py in $DL -- download core.py from the repo first"
fi

f=$(pick_newest "$DL"/streamgate*.py)
if [ -n "$f" ]; then
  cp "$f" "$SRC/streamgate.py" && say "installed streamgate.py <- $f"
else
  say "!! no streamgate*.py in $DL -- download streamgate.py from the repo first"
fi

f=$(pick_newest "$DL"/netgate*.py)
if [ -n "$f" ]; then
  cp "$f" "$SRC/netgate.py" && say "installed netgate.py <- $f"
else
  say "!! no netgate*.py in $DL -- download netgate.py from the repo first"
fi

# Remove the wrongly-placed top-level core.py if present (it belongs in services/)
rm -f "$SRC/core.py"

say "clearing stale bytecode caches"
rm -rf "$SRC/__pycache__" "$SRC/services/__pycache__"

say "restarting core"
launchctl kickstart -k gui/501/com.firstgeneralorder.core

say "restarting gateway"
launchctl kickstart -k gui/501/com.firstgeneralorder.gateway

say "DONE - core, streamgate, and netgate installed; core + gateway restarted"
