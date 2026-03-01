# openclaw-sessionwatcher

A lightweight local agent session monitor for watching live OpenClaw sessions in real time.

![Dashboard](https://img.shields.io/badge/Python-3.9%2B-blue) ![No dependencies](https://img.shields.io/badge/dependencies-none-green)

---

## What it does

Sessionwatcher reads the JSONL session logs written by OpenClaw agents and presents them as a live, auto-refreshing web UI. It gives you a bird's-eye view of all active and recent sessions, and lets you drill into individual conversations to inspect messages, tool calls, thinking blocks, and more — without having to tail log files manually.

<img width="1541" height="755" alt="image" src="https://github.com/user-attachments/assets/596eeab5-f42b-4089-9958-5c6b23313ed5" />

**Features:**

- Session list with status indicators (active / stopped / stale)
- Per-session message stream with structured rendering:
  - 💬 WhatsApp-style chat bubbles — user messages right-aligned, assistant left-aligned
  - User & assistant text messages (with `\n` → line break support)
  - 💭 Thinking blocks (individually collapsible; notice shown when Anthropic encrypts content)
  - ⚙ Tool calls with arguments (truncated at 300 chars with inline **show all**)
  - ✓/✗ Tool results with trimmed preview + **(show all)** — fetched on demand, persists across auto-refresh
  - ⚡ Session event markers (`/thinking`, model changes)
- **Chat-only toggle** — hides thinking/tool blocks instantly; button color reflects current state (green = all messages, red = chat only)
- Full message history — entire session loaded, no truncation cap
- Raw JSON modal for every message
- Copy button for session/message IDs
- Unread indicator (orange dot) for sessions with new messages
- Smart scroll — stays at bottom during live updates, preserves position otherwise
- Auto-refresh every 10 seconds with countdown
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
  │  GET /api/sessions/:id/messages            │  full message stream for one session
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

`index.html` is a self-contained single-file app (vanilla JS, no framework, no external CDN calls). State is managed in a plain `State` object. Auto-refresh calls the API every 10 seconds and patches only changed sections to avoid flicker. Expanded tool result content is cached client-side and survives auto-refresh cycles.

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
