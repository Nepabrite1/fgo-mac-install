#!/bin/bash
# FGO doctor: pull the 4 authoritative service files from the Windows machine
# over the LAN and install them in the correct folders, clear caches, restart.
# Run on the Mac:   bash ~/Downloads/fgo-doctor.sh
# (Run within a few minutes so the Windows file server is listening.)
set -u
SRC="$HOME/fgo/app-source/firstgeneralorder"
W="10.0.0.109:8098"

echo "== fetching files from Windows server =="
curl -s -m 20 -o "$SRC/services/core.py" "http://$W/core.py"      && echo "  core.py ok"
curl -s -m 20 -o "$SRC/streamgate.py"   "http://$W/streamgate.py"  && echo "  streamgate.py ok"
curl -s -m 20 -o "$SRC/netgate.py"      "http://$W/netgate.py"     && echo "  netgate.py ok"
curl -s -m 20 -o "$SRC/service_host.py" "http://$W/service_host.py"&& echo "  service_host.py ok"

echo "== verify (each must print 1 or more) =="
echo "  netgate STREAM_SUPPLIER: $(grep -cF STREAM_SUPPLIER "$SRC/netgate.py")"
echo "  core stream code:        $(grep -cF _start_stream_server "$SRC/services/core.py")"

echo "== clearing stale caches =="
rm -rf "$SRC/__pycache__" "$SRC/services/__pycache__"

echo "== restarting core + gateway =="
launchctl kickstart -k gui/501/com.firstgeneralorder.core
launchctl kickstart -k gui/501/com.firstgeneralorder.gateway

echo "== DONE doctor =="
