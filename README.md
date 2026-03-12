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
- **Full qualified model names** with provider in session list and message headers (e.g., `openai/gpt-4-turbo`)
- **Gateway message distinction**: Gateway-injected messages labeled clearly as "gateway" without model attribution
- **Enhanced Cron sessions**: Shortened cron ID (with copy button), next run countdown, and last run status/duration
- Per-session message stream with structured rendering:
  - WhatsApp-style chat bubbles — user messages right-aligned, assistant left-aligned
  - Dedicated right-aligned **Inter-session cards** for `provenance.kind=inter_session` payloads (not rendered as normal user chat bubbles)
  - Inter-session preview mode: starts after `<<<BEGIN_UNTRUSTED_CHILD_RESULT>>>` when present, compact default snippet, inline **(show all)** in the text panel
  - User & assistant text messages (with `\n` → line break support)
  - Grouped **system-entry bubbles** for non-text assistant/internal records, so headers, thinking, tool calls, tool results, and token stats stay visually connected
  - Smooth entry transitions in the selected session: newly arriving entries fade in quickly, and changed entries (text/tool/event updates) get a brief highlight pulse
  - Thinking blocks (individually collapsible; notice shown when Anthropic encrypts content)
  - ⚙ Tool calls with arguments (truncated at 300 chars with inline **show all**)
  - ✓/✗ Tool results with trimmed preview + **(show all)** — fetched on demand, persists across auto-refresh
  - ⚡ Session event markers (`thinking`, model changes, prompt errors, session/custom/meta fallbacks)
- **Chat-only toggle** — hides thinking/tool blocks instantly; button color reflects current state (green = all messages, red = chat only)
- Optional chat input for sending messages to the selected session via OpenClaw Gateway
  - Idempotency key per send to reduce accidental duplicates
  - Backend duplicate-ACK window for quick double-submits
  - Automatic gateway reconnect when WebSocket drops
  - Inline send status rendered above the input (no layout jump)
- User source detection for inbound user bubbles (`Direct` vs `Telegram`) with distinct labels/colors
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
  - Optional: `websocket-client` enables gateway chat send features (must be installed in the same Python interpreter used to run `server.py`)

---

## Release 1.2 Highlights

- Added gateway-backed chat send endpoint and UI (`POST /api/chat/send`)
- Added resilient gateway connection handling (auto-reconnect + lazy reconnect on send)
- Fixed duplicate user-message rendering caused by retry/fallback append behavior
- Improved session-type stability so Telegram sessions stay Telegram even after transient webchat metadata
- Added gateway timestamp-prefix cleanup for user text rendering
- Added user-source classification (`direct`/`telegram`) for differentiated bubble styling
- Added event rendering for `session`, `custom:model-snapshot`, and unknown entry fallbacks (instead of silent drops)
- Improved assistant error visibility when assistant content is empty
- Switched UI activity age to last visible JSONL entry timestamp before metadata fallback

## Release 1.3 Highlights

- Added lenient `openclaw.json` parsing for gateway/cron config reads, so trailing commas no longer break Session Watcher startup/runtime reads.
- Added alias-aware session JSONL resolution and merge logic for metadata drift cases (`sessionId` points to a new file while `sessionFile` still points to an older file).
- Updated session list loading, session detail loading, SSE change detection, and full-entry lookup to use the same merged multi-path resolver.
- Improved `show all` failure feedback in the UI by surfacing backend error text instead of a generic `(error)` label.
- Added explicit message provenance fields from backend parsing (`provenance_kind`, `provenance_source_session_key`, `provenance_source_channel`, `provenance_source_tool`) for robust frontend rendering decisions.
- Filtered delivery-mirror traffic from model-label rendering to reduce noisy model switches in the timeline.
- Hardened session rollover and gateway recovery paths to keep live updates stable across transport/file churn.
- Improved Telegram group label extraction from metadata for cleaner, more reliable session list naming.
- Simplified startup and LaunchAgent install flow in `start.sh` / `launchctl.sh` for more predictable local operation.
- Added dedicated Inter-session rendering path in the UI:
  - stays right-aligned (input side) but uses a non-bubble card style
  - shows source session/channel/tool metadata in-header
  - includes message ID copy + raw JSON actions like other message types
  - truncates long payloads with inline **(show all)** expansion

## Release 1.4 Highlights

- **Full qualified model names**: Display now shows full qualified model names with provider (e.g., `openai/gpt-4-turbo`, `anthropic/claude-opus-4`) instead of just the model name. Backend extracts provider from message metadata and displays in session list and message headers when available.
- **Gateway message handling**: `gateway-injected` metadata is no longer misrepresented as a model name. Gateway-injected messages now display as "gateway" in the role label with no model attribution, providing clearer visibility into message routing.
- **Session count accuracy**: Fixed footer session count to match displayed session list (filtered to sessions with messages or currently selected).
- **Enhanced cron session display**:
  - Cron job IDs shown shortened (first 8 chars) with copy button in session list, matching message ID display style
  - Next scheduled run time displayed as countdown format (`2h`, `30m`, `45s`)
  - Last run status and duration shown inline (e.g., `◆ success 2.5s`)
  - Metadata sourced from `jobs.json` state tracking for real-time visibility into cron execution
- **CSS refinements**: Increased model name display width (140px → 250px) and character truncation (22 → 100 chars) to accommodate longer qualified names.

---

## Requirements

- Python 3.9 or newer (no third-party packages needed)
- An OpenClaw installation with agents writing sessions to `~/.openclaw/agents/`
- Optional for chat send from the Session Watcher UI: `websocket-client` in the runtime interpreter

---

## Installation

```bash
# Clone or copy the directory next to your OpenClaw data
git clone https://github.com/EvanDataForge/openclaw-sessionwatcher
cd openclaw-sessionwatcher
```

That's it. No `pip install`, no build step.

If you want to send chat messages from the dashboard UI, install the optional package:

```bash
./../.venv/bin/python -m pip install websocket-client
```

If you use a different interpreter for SessionWatcher, install `websocket-client` there instead.

### macOS LaunchAgent Setup (optional auto-start)

To run OpenClaw Session Watcher as a macOS LaunchAgent that starts automatically on login:

```bash
./start.sh install
```

This creates a LaunchAgent plist and starts the service. To remove auto-start:

```bash
./start.sh uninstall
```

---

## Usage

### Basic Commands

```bash
./start.sh              # Start the session watcher (default)
./start.sh stop         # Stop the running instance
./start.sh restart      # Stop and start
./start.sh install      # Install as macOS LaunchAgent (auto-start on login)
./start.sh uninstall    # Remove LaunchAgent auto-start
```

Then open **http://127.0.0.1:8090** in your browser.

### Python Options

`start.sh` automatically uses `../.venv/bin/python` if it exists, otherwise `python3`. Override with:

```bash
SESSIONWATCHER_PYTHON=/path/to/python ./start.sh
```

If you need to start the server manually without the wrapper script:

```bash
python3 server.py --port 8090 --bind 127.0.0.1
```

### Options

| Environment variable      | Default       | Description                        |
|---------------------------|---------------|------------------------------------|
| `OPENCLAW_DIR`            | `~/.openclaw` | Path to OpenClaw data directory    |
| `SESSIONWATCHER_PORT`     | `8090`        | HTTP port to listen on             |
| `SESSIONWATCHER_BIND`     | `127.0.0.1`   | Bind address (use `0.0.0.0` for LAN) |
| `SESSIONWATCHER_ACCESS_TOKEN` | _(empty)_ | Required for non-loopback/LAN bind; enables cookie-based access protection |
| `SESSIONWATCHER_PYTHON`   | `../.venv/bin/python` if present, else `python3` | Python executable used by `start.sh` |

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
- plist location: `~/Library/LaunchAgents/com.openclaw.sessionwatcher.plist`

Control commands via `start.sh`:

```bash
./start.sh install       # Install and start LaunchAgent
./start.sh uninstall     # Remove LaunchAgent auto-start
./start.sh restart       # Restart (works whether running manually or via LaunchAgent)
./start.sh stop          # Stop (works whether running manually or via LaunchAgent)
```

---

## How it works

### Data flow

```
~/.openclaw/agents/*/sessions/
  sessions.json       ← session metadata (label, timestamps, model, …)
  <session-id>.jsonl  ← message log (one JSON object per line; resolver can merge alias files)
          │
          ▼
    server.py
  ┌─────────────────────────────┐
  │  load_all_sessions()        │  reads sessions.json + tail of each JSONL
  │  resolve_session_jsonl_paths() │ resolves canonical + alias JSONL paths
  │  _merge_session_entries()   │ merges/de-dupes entries across alias files
  │  parse_messages()           │  structures raw entries into display records
  │  _tool_result_preview()     │  trims large tool results to 300 chars
  │  _dedupe_retry_user_messages() │ collapses retry duplicate user entries
  │  strip_metadata()           │  removes gateway metadata headers
  │  strip_gateway_time_prefix()│  removes leading [Tue ... GMT+X] prefix
  │  classify_user_source()     │  marks user message source as direct/telegram
  │  strip_markers()            │  removes [[...]] markers from text
  │  load_gateway_config()      │  reads gateway settings from openclaw.json
  └────────────┬────────────────┘
               │  JSON API
               ▼
    index.html (single-file frontend)
  ┌─────────────────────────────┐
  │  GET /api/sessions          │  session list with stats
  │  GET /api/sessions/:id/messages            │  full message stream for one session
  │  GET /api/sessions/:id/events              │  SSE stream for live file change notifications
  │  GET /api/sessions/:id/entry/:eid/full     │  full text of one entry (on demand)
  │  GET /api/config/gateway                   │  gateway availability/config (token redacted)
  │  POST /api/chat/send                       │  send message to selected session key
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

"Last activity" in the UI is based on the last visible JSONL message timestamp (`last_ts_iso`) when available, and only falls back to `sessions.json.updatedAt` otherwise.

### Message parsing

Each JSONL entry is classified by its `type` field:

| JSONL type | Rendered as |
|---|---|
| `message` (role: user/assistant/toolResult) | Message bubble |
| `thinking_level_change` | ⚡ Event marker |
| `model_change` | ⚡ Event marker |
| `session` | ⚡ Event marker (`session started …`) |
| `custom` (`openclaw:prompt-error`) | ⚡ Event marker (`prompt error …`) |
| `custom` (`model-snapshot`) | ⚡ Event marker (`model snapshot …`) |
| `custom` (other) | ⚡ Event marker (`custom:<type> …`) |
| unknown non-`message` type | ⚡ Event marker (`entry:<type> …`) |

Unknown `message.role` values are rendered as `meta` event markers with a short content preview instead of being silently dropped.

Assistant messages are further decomposed into typed blocks:
- `text` → chat bubble
- `thinking` → collapsible thinking block (encrypted content flagged automatically)
- `toolCall` → tool call with formatted arguments, truncated + expandable
- `toolResult` (embedded) → result preview, full text fetchable on demand

For `message.role=user` entries, `message.provenance.kind=inter_session` is detected and rendered as a dedicated Inter-session card with metadata and compact preview/expand behavior.

ToolResult entries also expose detail status (`ok/error/failed/running/accepted/completed`) and assistant errors (`errorMessage`) are surfaced even when assistant text content is empty.

### Troubleshooting: `Gateway not connected` (HTTP 503)

1. Verify the running interpreter can import `websocket` (`websocket-client` package).
2. If using LaunchAgent auto-start, check the runtime program path in the plist:

```bash
cat ~/Library/LaunchAgents/com.openclaw.sessionwatcher.plist | grep -A1 -B1 ProgramArguments
```

3. Ensure the Python executable path points to the interpreter where `websocket-client` is installed.
4. Reload the agent after making changes:

```bash
./start.sh restart
```

Or manually:

```bash
launchctl bootout gui/$(id -u)/com.openclaw.sessionwatcher
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.openclaw.sessionwatcher.plist
```

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
