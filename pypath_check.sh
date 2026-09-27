#!/bin/bash
# Report exactly which netgate.py the running FGO services import, and whether
# it has the subnet-broadcast fix. Also lists any site-packages copies.
# Run: bash $(ls -t ~/Downloads/*pypath*.sh | head -1)
OUT=/tmp/pypath.txt
{
echo "=== PYPATH CHECK ==="
echo "time: $(date)"
echo "== default (from home) =="
cd "$HOME"
python3 -c "import firstgeneralorder.netgate as n; print('file:', n.__file__); print('has_subnet_broadcast:', hasattr(n,'_subnet_broadcast'))" 2>&1 | head -6
echo ""
echo "== with PYTHONPATH=~/fgo/app-source =="
PYTHONPATH="$HOME/fgo/app-source" python3 -c "import firstgeneralorder.netgate as n; print('file:', n.__file__); print('has_subnet_broadcast:', hasattr(n,'_subnet_broadcast'))" 2>&1 | head -6
echo ""
echo "== default sys.path =="
python3 -c "import sys; print('\n'.join(sys.path))" 2>&1 | head -20
echo ""
echo "== all netgate.py under fgo + user python =="
find "$HOME/fgo" "$HOME/Library/Python" -name netgate.py 2>/dev/null | head -20
echo "== END =="
} > "$OUT" 2>&1
cat "$OUT"
echo ""
curl -s -m 15 -X POST --data-binary @"$OUT" http://10.0.0.109:8099/diag >/dev/null && echo "posted OK" || echo "post failed"
