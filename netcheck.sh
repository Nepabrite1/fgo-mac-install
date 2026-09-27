#!/bin/bash
# Report what the Mac's network lookup produces for the gateway broadcast fix.
# Run: bash ~/Downloads/netcheck.sh
OUT=/tmp/netcheck.txt
{
echo "=== NETCHECK ==="
echo "time: $(date)"
python3 - <<'PY'
import socket, ipaddress
def lan_host():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
def subnet_bc():
    try:
        addr = ipaddress.ip_address(lan_host())
        net = ipaddress.ip_network(str(addr)+"/24", strict=False)
        return str(net.broadcast_address)
    except ValueError:
        return "255.255.255.255"
print("lan_host()        :", lan_host())
print("subnet_broadcast():", subnet_bc())
for t in ("8.8.8.8","1.1.1.1","10.0.0.1"):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((t,80))
            print("connect", t, "-> source", s.getsockname()[0])
    except Exception as e:
        print("connect", t, "ERR", type(e).__name__)
PY
echo "== END =="
} > "$OUT" 2>&1
cat "$OUT"
echo ""
curl -s -m 15 -X POST --data-binary @"$OUT" http://10.0.0.109:8099/diag >/dev/null && echo "posted OK" || echo "post failed"
