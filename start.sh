#!/bin/bash
# Start the Sessionwatcher dashboard
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

mkdir -p "$DIR/logs"

# Kill any existing instance
EXISTING=$(lsof -ti tcp:$PORT 2>/dev/null || true)
if [ -n "$EXISTING" ]; then
  echo "Stopping existing process on port $PORT..."
  kill "$EXISTING" 2>/dev/null || true
  sleep 1
fi

echo "Starting Sessionwatcher on http://$BIND:$PORT"
if [ -n "$ACCESS_TOKEN" ]; then
  echo "Access protection: enabled"
fi
nohup python3 "$DIR/server.py" --port "$PORT" --bind "$BIND" >> "$LOG" 2>&1 &
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
