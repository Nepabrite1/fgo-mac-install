#!/bin/bash
# FGO auto-start (macOS LaunchAgents). Starts all services + the gateway as the
# logged-in user. core/gateway/map/distance/connect/supervisor keep alive and
# auto-restart; ai/media start once at login (no auto-restart) so missing heavy
# libs on an Intel Mac don't create restart loops. No sudo needed.
# Logs go to ~/Library/Logs/fgo/
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

# Stop any service_host currently running so the agents take over cleanly.
pkill -f "firstgeneralorder.service_host" 2>/dev/null || true
sleep 1

write_agent() {
  local role="$1"
  local keepalive="$2"   # true | false
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
  <key>KeepAlive</key><$keepalive/>
  <key>StandardOutPath</key><string>$LOG_DIR/$role.out.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/$role.err.log</string>
</dict>
</plist>
EOF
  launchctl unload "$plist" 2>/dev/null || true
  launchctl load -w "$plist"
  echo "loaded $label"
}

write_agent core true
write_agent gateway true
write_agent map true
write_agent distance true
write_agent connect true
write_agent supervisor true
write_agent ai false
write_agent media false
echo "DONE. All services loaded. core/gateway/map/distance/connect/supervisor auto-restart; ai/media start once at login."
