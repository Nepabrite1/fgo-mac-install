#!/bin/bash
# Send 5 unicast UDP datagrams from the Mac to the Windows receiver IP:15195 so
# we can test whether UDP from the Mac reaches Windows. Run during the listener
# window:   bash ~/Downloads/sendudp.sh
python3 - <<'PY'
import socket, time
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
for i in range(5):
    s.sendto(b"FGO-TEST-%d" % i, ("10.0.0.109", 15195))
    time.sleep(1)
print("sent 5 unicast UDP to 10.0.0.109:15195")
PY
