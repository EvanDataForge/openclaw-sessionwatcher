#!/bin/bash
# Start the OpenClaw Session Watcher dashboard
set -e

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
LOG="$DIR/logs/server.log"
PYTHON_BIN_DEFAULT="$DIR/../.venv/bin/python"
PYTHON_BIN="${SESSIONWATCHER_PYTHON:-$PYTHON_BIN_DEFAULT}"

mkdir -p "$DIR/logs"

if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="$(command -v python3)"
fi

LAUNCH_LABEL="com.openclaw.sessionwatcher"
LAUNCH_TARGET="gui/$(id -u)/$LAUNCH_LABEL"

if [ "${SESSIONWATCHER_IGNORE_LAUNCHCTL:-0}" != "1" ] && launchctl print "$LAUNCH_TARGET" >/dev/null 2>&1; then
  echo "LaunchAgent detected ($LAUNCH_LABEL); delegating start to launchctl..."
  launchctl kickstart -k "$LAUNCH_TARGET" >/dev/null 2>&1 || true

  for _ in {1..20}; do
    LISTENER=$(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u || true)
    if [ -n "$LISTENER" ]; then
      break
    fi
    sleep 0.25
  done

  if [ -n "$LISTENER" ]; then
    echo "✓ Running   → http://$BIND:$PORT"
    if [ -n "$ACCESS_TOKEN" ]; then
      echo "  Login URL  → http://$BIND:$PORT/?access_token=<your-token>"
    fi
    echo "  Logs      → $LOG"
    echo "  Managed by launchctl. Control: $DIR/launchctl.sh {start|stop|restart|status|logs}"
    exit 0
  fi

  echo "✗ LaunchAgent is loaded, but no listener appeared on port $PORT."
  echo "  Check launchctl logs: $DIR/launchctl.sh logs"
  exit 1
fi

# Kill any existing listener on the target port.
# Use LISTEN-only lookup so we do not accidentally match client connections.
EXISTING=$(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u || true)
if [ -n "$EXISTING" ]; then
  echo "Stopping existing listener(s) on port $PORT..."
  while IFS= read -r pid; do
    [ -n "$pid" ] || continue
    kill "$pid" 2>/dev/null || true
  done <<< "$EXISTING"

  # Wait briefly until all listeners release the port.
  for _ in {1..20}; do
    REMAINING=$(lsof -nP -tiTCP:"$PORT" -sTCP:LISTEN 2>/dev/null | sort -u || true)
    if [ -z "$REMAINING" ]; then
      break
    fi
    sleep 0.25
  done

  if [ -n "$REMAINING" ]; then
    REMAINING_PIDS=$(echo "$REMAINING" | tr '\n' ' ')
    echo "✗ Port $PORT is still in use by PID(s): $REMAINING_PIDS"
    echo "  Hint: if launchctl auto-restart is enabled, run: $DIR/launchctl.sh stop"
    exit 1
  fi
fi

echo "Starting OpenClaw Session Watcher on http://$BIND:$PORT"
if [ -n "$ACCESS_TOKEN" ]; then
  echo "Access protection: enabled"
fi
nohup "$PYTHON_BIN" "$DIR/server.py" --port "$PORT" --bind "$BIND" >> "$LOG" 2>&1 &
PID=$!
echo "PID: $PID"
echo "$PID" > "$DIR/server.pid"
sleep 1

if kill -0 "$PID" 2>/dev/null; then
  echo "✓ Running   → http://$BIND:$PORT"
  if [ -n "$ACCESS_TOKEN" ]; then
    echo "  Login URL  → http://$BIND:$PORT/?access_token=<your-token>"
  fi
  echo "  Logs      → $LOG"
  echo "  Stop:        kill \$(cat $DIR/server.pid)"
else
  echo "✗ Failed to start. Check logs: $LOG"
  exit 1
fi
