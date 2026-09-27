#!/usr/bin/env python3
"""One-shot diagnostic for the FGO realtime streaming channel, run ON the Mac.

Gathers info, prints it to the screen, AND posts a copy over the LAN to the
Windows machine (10.0.0.109:8099) so the engineer can read it without you
copy/pasting between machines. Requires no FGO packages.
"""
import os, sys, time, subprocess, urllib.request

WINDOWS_IP = "10.0.0.109"
POST_URL = "http://%s:8099/diag" % WINDOWS_IP

out = []
out.append("=== FGO STREAM DIAG ===")
out.append("time: %s" % time.strftime("%Y-%m-%d %H:%M:%S"))

# 1) Where is core.py, and does it contain the stream code?
candidates = [
    "~/fgo/app-source/firstgeneralorder/services/core.py",
    "~/fgo/app-source/firstgeneralorder/core.py",
    "~/fgo/app-source/core.py",
]
out.append("--- core.py locations & stream code ---")
found_any = False
for c in candidates:
    p = os.path.expanduser(c)
    if os.path.exists(p):
        found_any = True
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            out.append("EXISTS   : %s" % p)
            out.append("  has_stream_code  : %s" % ("_start_stream_server" in text))
            out.append("  has_StreamServer : %s" % ("StreamServer" in text))
            out.append("  lines: %d" % (text.count("\n") + 1))
        except Exception as e:
            out.append("EXISTS(read fail): %s %s" % (p, e))
    else:
        out.append("MISSING  : %s" % p)
if not found_any:
    out.append("!! NONE of the core.py candidate paths exist")

# 2) stream.json — did core announce its stream endpoint?
out.append("--- stream.json ---")
sj = os.path.expanduser("~/.local/share/first-general-order-machine/runtime/stream.json")
if os.path.exists(sj):
    try:
        out.append(open(sj, encoding="utf-8").read().strip())
    except Exception as e:
        out.append("read error: %s" % e)
else:
    out.append("stream.json NOT PRESENT")

# 3) core error log tail
out.append("--- core.err.log (last 25) ---")
log = os.path.expanduser("~/Library/Logs/fgo/core.err.log")
if os.path.exists(log):
    try:
        lines = open(log, encoding="utf-8", errors="replace").read().splitlines()[-25:]
        out.append("\n".join(lines) if lines else "(empty log)")
    except Exception as e:
        out.append("read error: %s" % e)
else:
    out.append("no ~/Library/Logs/fgo/core.err.log")

# 4) agents loaded / running
out.append("--- launchctl agents ---")
try:
    r = subprocess.run(["launchctl", "list"], capture_output=True, text=True, timeout=15)
    rows = [l for l in (r.stdout or "").splitlines() if "firstgeneralorder" in l.lower()]
    out.append("\n".join(rows) if rows else "(none matched)")
except Exception as e:
    out.append("launchctl error: %s" % e)

# 5) source vs __pycache__ staleness for core
out.append("--- source vs __pycache__ (core) ---")
src = os.path.expanduser("~/fgo/app-source/firstgeneralorder/services")
if os.path.isdir(src):
    core_py = os.path.join(src, "core.py")
    if os.path.exists(core_py):
        out.append("core.py mtime: %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(core_py))))
        for tag in ("cpython-312", "cpython-314"):
            pyc = os.path.join(src, "__pycache__", "core." + tag + ".pyc")
            if os.path.exists(pyc):
                out.append("core.%s.pyc mtime: %s" % (tag, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(pyc)))))
            else:
                out.append("no core.%s.pyc" % tag)
    else:
        out.append("no core.py in services dir")
else:
    out.append("no services dir at %s" % src)

out.append("=== END DIAG ===")
text = "\n".join(out)

# Print to screen (in case the LAN post fails)
print(text)
print("\n[posting to %s ...]" % POST_URL, flush=True)

# Post a copy to the Windows receiver. Try curl, then urllib, then report failure.
body = text.encode("utf-8")
posted = False
try:
    subprocess.run(
        ["curl", "-s", "-m", "15", "-X", "POST", "--data-binary", "@-", POST_URL],
        input=body, capture_output=True, timeout=20,
    )
    posted = True
except Exception:
    pass
if not posted:
    try:
        req = urllib.request.Request(POST_URL, data=body, method="POST")
        urllib.request.urlopen(req, timeout=15).read()
        posted = True
    except Exception as e:
        print("LAN post failed: %s" % e, flush=True)
if posted:
    print("posted OK", flush=True)
