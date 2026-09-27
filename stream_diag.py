#!/usr/bin/env python3
"""One-shot diagnostic for the FGO realtime streaming channel, run ON the Mac.

Paste the full output back to the engineer. Requires no FGO packages.
"""
import os, sys, time

print("=== FGO STREAM DIAG ===")
print("time:", time.strftime("%Y-%m-%d %H:%M:%S"))

# 1) Where is core.py, and does it contain the stream code?
candidates = [
    "~/fgo/app-source/firstgeneralorder/services/core.py",
    "~/fgo/app-source/firstgeneralorder/core.py",
    "~/fgo/app-source/core.py",
]
print("--- core.py locations & stream code ---")
found_any = False
for c in candidates:
    p = os.path.expanduser(c)
    if os.path.exists(p):
        found_any = True
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
            print("EXISTS   :", p)
            print("  has_stream_code:", "_start_stream_server" in text)
            print("  has_StreamServer:", "StreamServer" in text)
            print("  lines:", text.count("\n") + 1)
        except Exception as e:
            print("EXISTS(read fail):", p, e)
    else:
        print("MISSING  :", p)
if not found_any:
    print("!! NONE of the core.py candidate paths exist")

# 2) stream.json — did core announce its stream endpoint?
print("--- stream.json ---")
sj = os.path.expanduser("~/.local/share/first-general-order-machine/runtime/stream.json")
if os.path.exists(sj):
    try:
        print(open(sj, encoding="utf-8").read().strip())
    except Exception as e:
        print("read error:", e)
else:
    print("stream.json NOT PRESENT")

# 3) core error log tail (look for ai.stream_unavailable or a traceback)
print("--- core.err.log (last 25) ---")
log = os.path.expanduser("~/Library/Logs/fgo/core.err.log")
if os.path.exists(log):
    try:
        lines = open(log, encoding="utf-8", errors="replace").read().splitlines()[-25:]
        print("\n".join(lines) if lines else "(empty log)")
    except Exception as e:
        print("read error:", e)
else:
    print("no ~/Library/Logs/fgo/core.err.log")

# 4) Which agents are loaded and are they running (PID column)?
print("--- launchctl agents ---")
os.system("launchctl list | grep -i firstgeneralorder")

# 5) Source vs __pycache__ staleness hint for core
print("--- source vs __pycache__ (core) ---")
src = os.path.expanduser("~/fgo/app-source/firstgeneralorder/services")
if os.path.isdir(src):
    core_py = os.path.join(src, "core.py")
    if os.path.exists(core_py):
        print("core.py mtime:", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(core_py))))
        for tag in ("cpython-312", "cpython-314"):
            pyc = os.path.join(src, "__pycache__", "core." + tag + ".pyc")
            if os.path.exists(pyc):
                print("core." + tag + ".pyc mtime:", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(pyc))))
            else:
                print("no core." + tag + ".pyc")
    else:
        print("no core.py in services dir")
else:
    print("no services dir at %s" % src)

print("=== END DIAG ===")
