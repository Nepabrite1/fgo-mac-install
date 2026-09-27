#!/bin/bash
# Send 5 directed-broadcast UDP datagrams to the subnet (10.0.0.255:15195) to
# test whether broadcast discovery can traverse this network to Windows.
# Run during the listener window:   bash ~/Downloads/sendbc.sh
python3 - <<'PY'
import socket, time
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
for i in range(5):
    s.sendto(b"FGO-BC-%d" % i, ("10.0.0.255", 15195))
    time.sleep(1)
print("sent 5 directed broadcasts to 10.0.0.255:15195")
PY
