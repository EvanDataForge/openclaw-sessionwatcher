#!/bin/bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for ENV_FILE in "$DIR/.sessionwatcher.env" "$DIR/.env.local" "$DIR/.env"; do
  if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
    break
  fi
done

PORT="${SESSIONWATCHER_PORT:-8090}"
BIND="${SESSIONWATCHER_BIND:-127.0.0.1}"
ACCESS_TOKEN="${SESSIONWATCHER_ACCESS_TOKEN:-}"
OPENCLAW_DIR="${OPENCLAW_DIR:-$HOME/.openclaw}"
LOG="$DIR/logs/server.log"

PYTHON_BIN="${SESSIONWATCHER_PYTHON:-$(command -v python3)}"
if [ ! -x "$PYTHON_BIN" ]; then
  echo "Error: Python not found or not executable: $PYTHON_BIN"
  exit 1
fi

LABEL="com.openclaw.sessionwatcher"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(id -u)"
TARGET="${DOMAIN}/${LABEL}"

listener_pids() {
  lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u || true
}

is_running() {
  [ -n "$(listener_pids)" ]
}

is_loaded() {
  launchctl print "$TARGET" >/dev/null 2>&1
}

wait_for_listener() {
  for _ in {1..20}; do
    if is_running; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

print_usage() {
  echo "Usage: $0 [start|stop|restart|install|uninstall]"
}

print_running_hint() {
  echo "An instance is already running on port $PORT."
  echo "Available commands: start (default), stop, restart, install, uninstall"
  echo "Example: $0 restart"
}

start_manual() {
  mkdir -p "$DIR/logs"
  echo "Starting Session Watcher manually on http://$BIND:$PORT ..."

  local cmd=("$PYTHON_BIN" "$DIR/server.py" "--port" "$PORT" "--bind" "$BIND")
  if [ -n "$ACCESS_TOKEN" ]; then
    cmd+=("--access-token" "$ACCESS_TOKEN")
    echo "Access-token protection is enabled."
  fi

  nohup "${cmd[@]}" >> "$LOG" 2>&1 &
  echo "$!" > "$DIR/server.pid"

  if wait_for_listener; then
    echo "Start successful: http://$BIND:$PORT"
    echo "Logs: $LOG"
  else
    echo "Start failed. See logs: $LOG"
    exit 1
  fi
}

start_via_launchctl() {
  if is_loaded; then
    echo "LaunchAgent is loaded. Starting via launchctl kickstart ..."
    launchctl kickstart -k "$TARGET" >/dev/null 2>&1 || true
  elif [ -f "$PLIST" ]; then
    echo "LaunchAgent plist found. Loading and starting via launchctl ..."
    launchctl bootstrap "$DOMAIN" "$PLIST"
    launchctl kickstart -k "$TARGET" >/dev/null 2>&1 || true
  else
    start_manual
    return
  fi

  if wait_for_listener; then
    echo "Start successful (launchctl): http://$BIND:$PORT"
  else
    echo "Launchctl started, but no listener was found on port $PORT."
    exit 1
  fi
}

stop_processes_on_port() {
  local pids
  pids="$(listener_pids)"
  if [ -z "$pids" ]; then
    return 0
  fi

  echo "Stopping processes on port $PORT ..."
  while IFS= read -r pid; do
    [ -n "$pid" ] || continue
    kill "$pid" 2>/dev/null || true
  done <<< "$pids"

  sleep 1
  pids="$(listener_pids)"
  if [ -n "$pids" ]; then
    echo "Processes did not exit on TERM, sending KILL ..."
    while IFS= read -r pid; do
      [ -n "$pid" ] || continue
      kill -9 "$pid" 2>/dev/null || true
    done <<< "$pids"
    sleep 1
  fi
}

do_start() {
  if is_running; then
    print_running_hint
    return 0
  fi

  if is_loaded || [ -f "$PLIST" ]; then
    start_via_launchctl
  else
    start_manual
  fi
}

do_stop() {
  local had_any=0

  if is_loaded; then
    echo "Stopping LaunchAgent via launchctl bootout ..."
    launchctl bootout "$TARGET" >/dev/null 2>&1 || true
    had_any=1
  fi

  if is_running; then
    had_any=1
    stop_processes_on_port
  fi

  if is_running; then
    echo "Error: Port $PORT is still in use."
    exit 1
  fi

  if [ "$had_any" -eq 1 ]; then
    echo "Session Watcher is stopped."
  else
    echo "No instance is running on port $PORT."
  fi
}

do_restart() {
  if is_running; then
    echo "Instance found, performing stop + start ..."
  else
    echo "No running instance found, starting directly ..."
  fi
  do_stop
  do_start
}

do_install() {
  mkdir -p "$HOME/Library/LaunchAgents" "$DIR/logs"
  touch "$DIR/logs/launchd.log"

  echo "Installing LaunchAgent at $PLIST ..."
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON_BIN</string>
    <string>$DIR/server.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/Users/openclaw/.homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>OPENCLAW_DIR</key>
    <string>$OPENCLAW_DIR</string>
    <key>SESSIONWATCHER_BIND</key>
    <string>$BIND</string>
    <key>SESSIONWATCHER_PORT</key>
    <string>$PORT</string>
    <key>SESSIONWATCHER_ACCESS_TOKEN</key>
    <string>$ACCESS_TOKEN</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>
  <key>StandardOutPath</key>
  <string>$DIR/logs/launchd.log</string>
  <key>StandardErrorPath</key>
  <string>$DIR/logs/launchd.log</string>
</dict>
</plist>
EOF

  if is_loaded; then
    echo "LaunchAgent was already loaded, reloading ..."
    launchctl bootout "$TARGET" >/dev/null 2>&1 || true
  fi

  launchctl bootstrap "$DOMAIN" "$PLIST"
  launchctl kickstart -k "$TARGET" >/dev/null 2>&1 || true
  echo "Install complete. Autostart via launchctl is active."

  if wait_for_listener; then
    echo "Instance is running: http://$BIND:$PORT"
  else
    echo "Note: LaunchAgent is installed, but no listener is currently active on port $PORT."
  fi
}

do_uninstall() {
  echo "Removing LaunchAgent autostart ..."
  if is_loaded; then
    launchctl bootout "$TARGET" >/dev/null 2>&1 || true
    echo "LaunchAgent unloaded."
  fi

  if [ -f "$PLIST" ]; then
    rm -f "$PLIST"
    echo "Plist removed: $PLIST"
  else
    echo "No plist found: $PLIST"
  fi

  echo "Uninstall complete. Autostart via launchctl has been removed."
}

COMMAND="${1:-start}"

if [ $# -eq 0 ]; then
  echo "Available commands: start (default), stop, restart, install, uninstall"
  echo ""
fi

case "$COMMAND" in
  start)
    do_start
    ;;
  stop)
    do_stop
    ;;
  restart)
    do_restart
    ;;
  install)
    do_install
    ;;
  uninstall)
    do_uninstall
    ;;
  -h|--help|help)
    print_usage
    ;;
  *)
    echo "Unknown command: $COMMAND"
    print_usage
    exit 1
    ;;
esac
