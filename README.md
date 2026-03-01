# openclaw-sessionwatcher

A lightweight local web dashboard for monitoring live [OpenClaw](https://github.com/openclaw) agent sessions in real time.

![Dashboard](https://img.shields.io/badge/Python-3.9%2B-blue) ![No dependencies](https://img.shields.io/badge/dependencies-none-green)

---

## What it does

Sessionwatcher reads the JSONL session logs written by OpenClaw agents and presents them as a live, auto-refreshing web UI. It gives you a bird's-eye view of all active and recent sessions, and lets you drill into individual conversations to inspect messages, tool calls, thinking blocks, and more — without having to tail log files manually.

<img width="1255" height="807" alt="image" src="https://github.com/user-attachments/assets/ecb953b9-ad2e-4c46-b116-792809513fd9" />


**Features:**

- Session list with status indicators (active / stopped / stale)
- Per-session message stream with structured rendering:
  - User & assistant text messages (with `\n` → line break support)
  - 💭 Thinking blocks (individually collapsible)
  - ⚙ Tool calls with arguments
  - ✓/✗ Tool results with trimmed preview + **(show all)** inline expand
  - ⚡ Session event markers (`/thinking`, model changes)
- Raw JSON modal for every message
- Copy button for session/message IDs
- Unread indicator (orange dot) for sessions with new messages
- Smart scroll — stays at bottom during live updates, preserves position otherwise
- Auto-refresh every 10 seconds with countdown
- Zero external dependencies — pure Python stdlib + vanilla JS

---

## Requirements

- Python 3.9 or newer (no third-party packages needed)
- An OpenClaw installation with agents writing sessions to `~/.openclaw/agents/`

---

## Installation

```bash
# Clone or copy the directory next to your OpenClaw data
git clone https://github.com/your-org/openclaw-sessionwatcher
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

### Options

| Environment variable      | Default       | Description                        |
|---------------------------|---------------|------------------------------------|
| `OPENCLAW_DIR`            | `~/.openclaw` | Path to OpenClaw data directory    |
| `SESSIONWATCHER_PORT`     | `8090`        | HTTP port to listen on             |
| `SESSIONWATCHER_BIND`     | `127.0.0.1`   | Bind address (use `0.0.0.0` for LAN) |

Example — expose on LAN, custom OpenClaw dir:

```bash
OPENCLAW_DIR=/data/openclaw SESSIONWATCHER_BIND=0.0.0.0 SESSIONWATCHER_PORT=9000 ./start.sh
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
  │  GET /api/sessions/:id/messages │  message stream for one session
  │  GET /api/status            │  health check
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
- `text` → text body
- `thinking` → collapsible thinking block
- `toolCall` → tool call with formatted arguments
- `toolResult` (embedded) → result preview

### Frontend

`index.html` is a self-contained single-file app (vanilla JS, no framework, no external CDN calls). State is managed in a plain `State` object. Auto-refresh calls the API every 10 seconds and patches only changed sections to avoid flicker.

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
