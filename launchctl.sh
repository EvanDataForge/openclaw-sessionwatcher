#!/bin/bash
set -euo pipefail

LABEL="com.openclaw.sessionwatcher"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
DOMAIN="gui/$(id -u)"
TARGET="${DOMAIN}/${LABEL}"
LOG_FILE="$HOME/.openclaw/openclaw-sessionwatcher/logs/launchd.log"

is_loaded() {
  launchctl print "$TARGET" >/dev/null 2>&1
}

require_plist() {
  if [ ! -f "$PLIST" ]; then
    echo "LaunchAgent not found: $PLIST" >&2
    exit 1
  fi
}

start_agent() {
  require_plist
  if is_loaded; then
    launchctl kickstart -k "$TARGET"
  else
    launchctl bootstrap "$DOMAIN" "$PLIST"
  fi
  echo "SessionWatcher started via launchctl."
}

stop_agent() {
  if is_loaded; then
    launchctl bootout "$TARGET"
    echo "SessionWatcher stopped."
  else
    echo "SessionWatcher is not loaded."
  fi
}

restart_agent() {
  require_plist
  if is_loaded; then
    launchctl kickstart -k "$TARGET"
  else
    launchctl bootstrap "$DOMAIN" "$PLIST"
  fi
  echo "SessionWatcher restarted."
}

status_agent() {
  if is_loaded; then
    launchctl print "$TARGET" | sed -n '1,40p'
  else
    echo "SessionWatcher is not loaded."
  fi
}

logs_agent() {
  mkdir -p "$(dirname "$LOG_FILE")"
  touch "$LOG_FILE"
  tail -n 80 -f "$LOG_FILE"
}

case "${1:-status}" in
  start)
    start_agent
    ;;
  stop)
    stop_agent
    ;;
  restart)
    restart_agent
    ;;
  status)
    status_agent
    ;;
  logs)
    logs_agent
    ;;
  *)
    echo "Usage: $0 {start|stop|restart|status|logs}" >&2
    exit 1
    ;;
esac
