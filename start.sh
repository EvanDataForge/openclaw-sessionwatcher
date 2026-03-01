#!/bin/bash
# Start the Sessionwatcher dashboard
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${SESSIONWATCHER_PORT:-8090}"
BIND="${SESSIONWATCHER_BIND:-127.0.0.1}"
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
nohup python3 "$DIR/server.py" --port "$PORT" --bind "$BIND" >> "$LOG" 2>&1 &
PID=$!
echo "PID: $PID"
echo "$PID" > "$DIR/server.pid"
sleep 1

if kill -0 "$PID" 2>/dev/null; then
  echo "✓ Running   → http://$BIND:$PORT"
  echo "  Logs      → $LOG"
  echo "  Stop:        kill \$(cat $DIR/server.pid)"
else
  echo "✗ Failed to start. Check logs: $LOG"
  exit 1
fi
