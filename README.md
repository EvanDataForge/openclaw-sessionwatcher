# OpenClaw Session Watcher / Dashboard

<img align="right" src="doc/OpenClawSessionWatcherLogoBasicSmall.png" alt="OpenClaw Session Watcher Logo" />

A lightweight local agent session monitor for watching live OpenClaw sessions in real time.

<br clear="right" />

![Dashboard](https://img.shields.io/badge/Python-3.9%2B-blue) ![No dependencies](https://img.shields.io/badge/dependencies-none-green)

---

### What it does


OpenClaw Session Watcher reads the JSONL session logs written by OpenClaw agents and presents them as a live, auto-refreshing web UI. It gives you a bird's-eye view of all active and recent sessions, and lets you drill into individual conversations to inspect messages, tool calls, thinking blocks, and more — without having to tail log files manually.

![Dark mode screenshot](doc/SessionWatcherDarkMode.png)  
Dark mode is the default. An optional light mode is available for brighter environments:  
![Light mode screenshot](doc/SessionWatcherLightMode.png)  

**Features:**

- Built-in light mode and dark mode for the full UI
- Session list with status indicators (active / stopped / stale)
- Top bar branding: `OpenClaw Session Watcher` + live green status dot
- Subtle footer meta line with session count and last refresh time
- Per-session message stream with structured rendering:
  - WhatsApp-style chat bubbles — user messages right-aligned, assistant left-aligned
  - User & assistant text messages (with `\n` → line break support)
  - Grouped **system-entry bubbles** for non-text assistant/internal records, so headers, thinking, tool calls, tool results, and token stats stay visually connected
  - Smooth entry transitions in the selected session: newly arriving entries fade in quickly, and changed entries (text/tool/event updates) get a brief highlight pulse
  - Thinking blocks (individually collapsible; notice shown when Anthropic encrypts content)
  - ⚙ Tool calls with arguments (truncated at 300 chars with inline **show all**)
  - ✓/✗ Tool results with trimmed preview + **(show all)** — fetched on demand, persists across auto-refresh
  - ⚡ Session event markers (`/thinking`, model changes)
- **Chat-only toggle** — hides thinking/tool blocks instantly; button color reflects current state (green = all messages, red = chat only)
- Full message history — entire session loaded, no truncation cap
- Raw JSON modal for every message
- Copy button for session/message IDs
- Unread indicator (orange dot) for sessions with new messages
- Smart scroll — stays at bottom during live updates, preserves position otherwise
- **Live push updates** via Server-Sent Events (SSE) — selected session updates typically <1s after new log entries
  - Adaptive polling fallback (500ms → 1s → 2s → 4s → 8s → 10s) if SSE unavailable or disconnected
  - Session list refreshes every 10 seconds in the background
  - Update source indicator: `↻` for periodic refresh, `•` for live push
- **Burger menu** (top right) — About dialog + Report an Issue (opens GitHub issue template chooser)
- Zero external dependencies — pure Python stdlib + vanilla JS

---

## Requirements

- Python 3.9 or newer (no third-party packages needed)
- An OpenClaw installation with agents writing sessions to `~/.openclaw/agents/`

---

## Installation

```bash
# Clone or copy the directory next to your OpenClaw data
git clone https://github.com/EvanDataForge/openclaw-sessionwatcher
cd openclaw-sessionwatcher
```

That's it. No `pip install`, no build step.

---

## Usage

### Start

```bash
./start.sh
```

Then open **http://127.0.0.1:8090** in your browser.

Or start manually:

```bash
python3 server.py
```

### Stop

```bash
kill $(cat server.pid)
```

### macOS launchctl control

If OpenClaw Session Watcher is installed as a LaunchAgent, you can control it with:

```bash
./launchctl.sh start
./launchctl.sh stop
./launchctl.sh restart
./launchctl.sh status
./launchctl.sh logs
```

Short zsh helpers are also available in interactive shells:

```bash
sw-start
sw-stop
sw-restart
sw-status
sw-logs
```

### Options

| Environment variable      | Default       | Description                        |
|---------------------------|---------------|------------------------------------|
| `OPENCLAW_DIR`            | `~/.openclaw` | Path to OpenClaw data directory    |
| `SESSIONWATCHER_PORT`     | `8090`        | HTTP port to listen on             |
| `SESSIONWATCHER_BIND`     | `127.0.0.1`   | Bind address (use `0.0.0.0` for LAN) |
| `SESSIONWATCHER_ACCESS_TOKEN` | _(empty)_ | Required for non-loopback/LAN bind; enables cookie-based access protection |

Example — expose on LAN safely, with a custom OpenClaw dir:

```bash
OPENCLAW_DIR=/data/openclaw \
SESSIONWATCHER_BIND=0.0.0.0 \
SESSIONWATCHER_PORT=9000 \
SESSIONWATCHER_ACCESS_TOKEN='replace-with-a-long-random-token' \
./start.sh
```

Then open the UI once with:

```text
http://<your-lan-ip>:9000/?access_token=<your-token>
```

That bootstrap URL stores an `HttpOnly` cookie and immediately removes the token from the address bar.

> OpenClaw Session Watcher will refuse to bind to `0.0.0.0`, `::`, or any other non-loopback address unless `SESSIONWATCHER_ACCESS_TOKEN` is set.

### Persistent local configuration

`start.sh` automatically loads the first file that exists from this list:

- `.sessionwatcher.env`
- `.env.local`
- `.env`

This is useful if you want OpenClaw Session Watcher to always start in LAN mode without passing flags manually.

Example:

```bash
cat > .env.local <<'EOF'
SESSIONWATCHER_BIND=0.0.0.0
SESSIONWATCHER_PORT=8090
SESSIONWATCHER_ACCESS_TOKEN=replace-with-a-long-random-token
EOF
```

These files are intended for local machine config and should not be committed.

### LaunchAgent / auto-start on macOS

OpenClaw Session Watcher can run as a macOS `LaunchAgent` so it starts automatically when your user logs in.

Typical properties of the LaunchAgent setup:

- starts on login (`RunAtLoad`)
- restarts automatically if it exits (`KeepAlive`)
- writes logs to `logs/launchd.log`
- can inject `SESSIONWATCHER_BIND`, `SESSIONWATCHER_PORT`, and `SESSIONWATCHER_ACCESS_TOKEN`

Control commands:

```bash
./launchctl.sh start
./launchctl.sh stop
./launchctl.sh restart
./launchctl.sh status
./launchctl.sh logs
```

---

## How it works

### Data flow

```
~/.openclaw/agents/*/sessions/
  sessions.json       ← session metadata (label, timestamps, model, …)
  <session-id>.jsonl  ← message log (one JSON object per line)
          │
          ▼
    server.py
  ┌─────────────────────────────┐
  │  load_all_sessions()        │  reads sessions.json + tail of each JSONL
  │  parse_messages()           │  structures raw entries into display records
  │  _tool_result_preview()     │  trims large tool results to 300 chars
  │  strip_metadata()           │  removes gateway metadata headers
  │  strip_markers()            │  removes [[...]] markers from text
  └────────────┬────────────────┘
               │  JSON API
               ▼
    index.html (single-file frontend)
  ┌─────────────────────────────┐
  │  GET /api/sessions          │  session list with stats
  │  GET /api/sessions/:id/messages            │  full message stream for one session
  │  GET /api/sessions/:id/events              │  SSE stream for live file change notifications
  │  GET /api/sessions/:id/entry/:eid/full     │  full text of one entry (on demand)
  │  GET /api/status                           │  health check
  └─────────────────────────────┘
```

### Session list logic

Sessions are loaded from all agents under `$OPENCLAW_DIR/agents/`. Only sessions updated within the last 24 hours are shown (configurable via `ACTIVE_WINDOW_H` in `server.py`). The status dot colour follows this priority:

| Condition | Dot |
|---|---|
| Recent (< 10 min) + stopped | 🔴 Red |
| Recent (< 10 min) + no stop | 🟢 Green, blinking |
| Older (> 10 min) | 🟤 Dark red |

### Message parsing

Each JSONL entry is classified by its `type` field:

| JSONL type | Rendered as |
|---|---|
| `message` (role: user/assistant/toolResult) | Message bubble |
| `thinking_level_change` | ⚡ Event marker |
| `model_change` | ⚡ Event marker |
| `custom` | Skipped |

Assistant messages are further decomposed into typed blocks:
- `text` → chat bubble
- `thinking` → collapsible thinking block (encrypted content flagged automatically)
- `toolCall` → tool call with formatted arguments, truncated + expandable
- `toolResult` (embedded) → result preview, full text fetchable on demand

### Frontend

`index.html` is a self-contained single-file app (vanilla JS, no framework, no external CDN calls). It supports both light and dark themes, keeps plain chat messages in their existing chat-bubble layout, and groups non-text assistant/tool activity into distinct system-entry containers for easier scanning. State is managed in a plain `State` object.

**Live Updates:**
- Selected session opens an SSE stream (`/api/sessions/:id/events`) that pushes `changed` events when the JSONL file grows
- Detail panel reloads messages immediately on push notification (typically <1s after new log entry)
- Entry-level diffing tracks stable message/event signatures so only new or actually changed entries animate (no transition spam on initial load)
- If SSE fails or is unsupported, adaptive polling starts: 500ms → 1s → 2s → 4s → 8s, max 10s between retries
- Session list still refreshes every 10 seconds via classic polling
- Expanded tool result content is cached client-side and survives auto-refresh cycles

---

## Security

- Default bind is `127.0.0.1`, so OpenClaw Session Watcher stays local unless you opt into LAN exposure.
- If you bind to a non-loopback address, `SESSIONWATCHER_ACCESS_TOKEN` is mandatory.
- Authentication uses a one-time `/?access_token=...` bootstrap and an `HttpOnly` cookie afterwards.
- All UI and API routes are protected when an access token is configured, including `/api/status`.
- LAN requests are served by a threaded HTTP server, so slow session scans on one request should not block unrelated connections.
- OpenClaw Session Watcher is still plain HTTP. For untrusted networks or TLS, put it behind a reverse proxy or tunnel.
- Wildcard CORS is intentionally disabled.

---

## File structure

```
openclaw-sessionwatcher/
├── server.py       # Python HTTP server + data parsing
├── index.html      # Single-file frontend (HTML + CSS + JS)
├── start.sh        # Convenience start script
├── .gitignore
├── README.md
└── logs/           # Runtime logs (git-ignored)
    └── server.log
```

---

## License

MIT
