#!/usr/bin/env python3
"""Check FGO streaming fix + gateway state ON the Mac, and POST results to the
Windows receiver (10.0.0.109:8099). Run:  python3 ~/Downloads/fix_check.py
Requires no FGO packages."""
import os, sys, time, glob, subprocess, urllib.request

POST_URL = "http://10.0.0.109:8099/diag"
out = []
out.append("=== FGO FIX CHECK ===")
out.append("time: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))

PKG = os.path.expanduser("~/fgo/app-source/firstgeneralorder")

# 1) launchctl state for the relevant agents
out.append("--- launchctl (core/gateway/ai/media) ---")
try:
    r = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=15)
    for l in (r.stdout or "").splitlines():
        if any(k in l.lower() for k in ("com.firstgeneralorder.core", "com.firstgeneralorder.gateway",
                                        "com.firstgeneralorder.ai", "com.firstgeneralorder.media")):
            out.append(l)
except Exception as e:
    out.append("launchctl error: %s" % e)

# 2) gateway error log tail
out.append("--- gateway.err.log (last 20) ---")
for role in ("gateway", "core"):
    log = os.path.expanduser("~/Library/Logs/fgo/%s.err.log" % role)
    out.append("[%s]" % role)
    if os.path.exists(log):
        try:
            lines = open(log, encoding="utf-8", errors="replace").read().splitlines()[-20:]
            out.append("\n".join(lines) if lines else "(empty)")
        except Exception as e:
            out.append("read error: %s" % e)
    else:
        out.append("no %s.err.log" % role)

# 3) stream.json
out.append("--- stream.json ---")
sj = os.path.expanduser("~/.local/share/first-general-order-machine/runtime/stream.json")
out.append(open(sj, encoding="utf-8").read().strip() if os.path.exists(sj) else "stream.json NOT PRESENT")

# 4) Did the correct code get installed?
def probe(rel, needles):
    p = os.path.join(PKG, rel)
    if not os.path.exists(p):
        return ["MISSING : %s" % rel]
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        t = f.read()
    mt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p)))
    res = ["EXISTS:%s mtime=%s lines=%d" % (rel, mt, t.count("\n") + 1)]
    for n in needles:
        res.append("  %s : %s" % (n, n in t))
    return res

out.append("--- installed files (correct code?) ---")
out += probe("services/core.py", ["_start_stream_server", "StreamServer", "or 8)"])
out += probe("streamgate.py", ["class StreamServer", "class StreamClient"])

# 5) What did Safari name the downloads? (addresses the -2/-3 concern)
out.append("--- ~/Downloads matching files + mtimes ---")
for p in sorted(glob.glob(os.path.expanduser("~/Downloads/core*.py")) +
                glob.glob(os.path.expanduser("~/Downloads/streamgate*.py")) +
                glob.glob(os.path.expanduser("~/Downloads/fix-stream*.sh"))):
    mt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p)))
    out.append("  %s  (%s)" % (os.path.basename(p), mt))

text = "\n".join(out)
print(text, flush=True)
print("\n[posting to %s ...]" % POST_URL, flush=True)
body = text.encode("utf-8")
ok = False
try:
    subprocess.run(["curl", "-s", "-m", "15", "-X", "POST", "--data-binary", "@-", POST_URL],
                   input=body, capture_output=True, timeout=20)
    ok = True
except Exception:
    pass
if not ok:
    try:
        urllib.request.urlopen(urllib.request.Request(POST_URL, data=body, method="POST"), timeout=15).read()
        ok = True
    except Exception as e:
        print("LAN post failed: %s" % e, flush=True)
if ok:
    print("posted OK", flush=True)
