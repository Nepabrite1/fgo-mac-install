#!/bin/bash
# FGO auto-start (macOS LaunchAgents). Runs core + gateway as the logged-in user
# with KeepAlive so they auto-start at login and auto-restart if they stop.
# No sudo needed. Logs go to ~/Library/Logs/fgo/
set -e

SRC="$HOME/fgo/app-source"
if [ ! -d "$SRC/firstgeneralorder" ]; then
  echo "ERROR: app-source not found at $SRC"
  echo "If your app-source is elsewhere, edit this line, or tell your engineer the path."
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERROR: python3 not found on PATH"
  exit 1
fi
PYBIN="$(python3 -c 'import sys; print(sys.executable)' 2>/dev/null || command -v python3)"

AGENT_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/fgo"
mkdir -p "$AGENT_DIR" "$LOG_DIR"

# Stop any service_host currently running (manual Terminal instances, or the
# broken core from an earlier load) so the agents take over cleanly.
pkill -f "firstgeneralorder.service_host" 2>/dev/null || true
sleep 1

# Emit --role/core as separate argv entries. launchd passes each <string> as one
# argument; a single "--role core" token is rejected by argparse.
write_agent() {
  local role="$1"
  local label="com.firstgeneralorder.$role"
  local plist="$AGENT_DIR/$label.plist"
  if [ "$role" = "gateway" ]; then
    ARGS='    <string>'$PYBIN'</string>
    <string>-m</string>
    <string>firstgeneralorder.service_host</string>
    <string>--gateway</string>'
  else
    ARGS='    <string>'$PYBIN'</string>
    <string>-m</string>
    <string>firstgeneralorder.service_host</string>
    <string>--role</string>
    <string>'$role'</string>'
  fi
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key>
  <array>
$ARGS
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONPATH</key><string>$SRC</string>
  </dict>
  <key>WorkingDirectory</key><string>$SRC</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOG_DIR/$role.out.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/$role.err.log</string>
</dict>
</plist>
EOF
  launchctl unload "$plist" 2>/dev/null || true
  launchctl load -w "$plist"
  echo "loaded $label"
}

write_agent core
write_agent gateway
echo "DONE. core + gateway auto-start at login and auto-restart."
