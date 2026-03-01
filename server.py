#!/usr/bin/env python3
"""
Sessionwatcher — Live session activity dashboard for OpenClaw
Runs on http://127.0.0.1:8090
"""

import json
import os
import re
import time
import argparse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse

# ── Config ────────────────────────────────────────────────────────────────────
OPENCLAW_DIR = Path(os.environ.get("OPENCLAW_DIR", Path.home() / ".openclaw"))
AGENTS_DIR   = OPENCLAW_DIR / "agents"
DEFAULT_PORT = int(os.environ.get("SESSIONWATCHER_PORT", 8090))
DEFAULT_BIND = os.environ.get("SESSIONWATCHER_BIND", "127.0.0.1")

ACTIVE_WINDOW_H = 24   # sessions active in last N hours are shown

# ── Metadata stripping ───────────────────────────────────────────────────────

_META_PATTERN = re.compile(
    r'^(?:[^\n]*?\(untrusted metadata\):\n```(?:json)?\n.*?\n```\n\n)+',
    re.DOTALL
)

def strip_metadata(text: str) -> tuple[str, bool]:
    """Strip leading 'untrusted metadata' blocks injected by the gateway.
    Returns (clean_text, had_metadata)."""
    m = _META_PATTERN.match(text)
    if m:
        return text[m.end():].strip(), True
    return text, False

_MARKER_PATTERN = re.compile(r'\[\[[^\]]*\]\]')

def strip_markers(text: str) -> str:
    """Remove [[...]] markers like [[reply_to_current]] from message text."""
    return _MARKER_PATTERN.sub('', text).strip()

# ── Helpers ───────────────────────────────────────────────────────────────────

def now_ms() -> int:
    return int(time.time() * 1000)

def fmt_ts(ms: int) -> str:
    if not ms:
        return ""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone()
    return dt.strftime("%H:%M:%S")

def fmt_iso(iso: str) -> str:
    """Convert ISO timestamp to HH:MM:SS local time."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
        return dt.strftime("%H:%M:%S")
    except Exception:
        return iso

def time_ago(ms: int) -> str:
    diff = (now_ms() - ms) / 1000
    if diff < 60:
        return f"{int(diff)}s ago"
    if diff < 3600:
        return f"{int(diff/60)}m ago"
    if diff < 86400:
        return f"{int(diff/3600)}h ago"
    return f"{int(diff/86400)}d ago"

def friendly_model(raw: str) -> str:
    if not raw:
        return "—"
    # strip provider prefixes
    for prefix in ("openai-completions/", "anthropic/", "openrouter/", "openai/"):
        if raw.startswith(prefix):
            raw = raw[len(prefix):]
    return raw

def session_type(key: str, val: dict | None = None) -> str:
    if ":cron:" in key or key.startswith("cron:"):
        return "cron"
    if ":subagent:" in key or "subagent" in key:
        return "subagent"
    # Use rich metadata when available
    if val:
        chat_type   = val.get("chatType") or ""
        last_channel = val.get("lastChannel") or ""
        origin      = val.get("origin") or {}
        origin_from = origin.get("from", "")
        if chat_type == "group" or "group" in origin_from:
            return "group"
        if last_channel == "telegram" or origin.get("provider") == "telegram":
            return "telegram"
        if last_channel == "webchat" or origin.get("provider") == "webchat":
            return "main"
    # Fallback: key-based
    if ":telegram:group:" in key or key.startswith("telegram:group:"):
        return "group"
    if "telegram" in key:
        return "telegram"
    if key.startswith("agent:") or key == "agent:main:sessions":
        return "main"
    return "other"

def type_label(t: str) -> str:
    return {
        "group":    "TG Group",
        "telegram": "Telegram",
        "cron":     "Cron",
        "subagent": "Subagent",
        "main":     "Direct",
        "other":    "Other",
    }.get(t, t)

# ── Data loading ──────────────────────────────────────────────────────────────

def load_sessions_store(agent_dir: Path) -> dict:
    """Load sessions.json from an agent's sessions directory."""
    path = agent_dir / "sessions" / "sessions.json"
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return {}

def tail_jsonl(path: Path, n: int = 200) -> list[dict]:
    """Read last n lines from a JSONL file (handles large files efficiently)."""
    if not path.exists():
        return []
    try:
        with open(path, "rb") as f:
            # Read last ~200KB for efficiency
            f.seek(0, 2)
            fsize = f.tell()
            read_size = min(fsize, 256 * 1024)
            f.seek(max(0, fsize - read_size))
            raw = f.read().decode("utf-8", errors="replace")
        lines = [l for l in raw.splitlines() if l.strip()]
        lines = lines[-n:]
        result = []
        for line in lines:
            try:
                result.append(json.loads(line))
            except Exception:
                pass
        return result
    except Exception:
        return []

def read_jsonl_full(path: Path) -> list[dict]:
    """Read ALL lines from a JSONL file (used for full session message view)."""
    if not path.exists():
        return []
    result = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    result.append(json.loads(line))
                except Exception:
                    pass
    except Exception:
        pass
    return result

def _tool_result_preview(content) -> tuple[str, str, int]:
    """Return (preview_text, full_text, total_chars) from toolResult content."""
    if isinstance(content, str):
        return content[:300], content, len(content)
    if isinstance(content, list):
        parts = []
        total = 0
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text", "")
                total += len(t)
                parts.append(t)
        full = "\n".join(parts)
        return full[:300], full, total
    s = str(content)
    return s[:300], s, len(s)

def parse_messages(entries: list[dict]) -> list[dict]:
    """Extract display-friendly message records from raw JSONL entries."""
    msgs = []
    for entry in entries:
        etype = entry.get("type", "")

        # ── Session events (thinking change, model change) ─────────────────
        if etype == "thinking_level_change":
            msgs.append({
                "id":        entry.get("id", ""),
                "role":      "event",
                "event_type": "thinking",
                "text":      f"thinking → {entry.get('thinkingLevel', '?')}",
                "ts_iso":    entry.get("timestamp", ""),
                "ts_fmt":    fmt_iso(entry.get("timestamp", "")),
                "raw_json":  json.dumps(entry, ensure_ascii=False),
            })
            continue
        if etype == "model_change":
            mid = entry.get("modelId", entry.get("model", "?"))
            prov = entry.get("provider", "")
            msgs.append({
                "id":        entry.get("id", ""),
                "role":      "event",
                "event_type": "model",
                "text":      f"model → {prov+'/'+mid if prov else mid}",
                "ts_iso":    entry.get("timestamp", ""),
                "ts_fmt":    fmt_iso(entry.get("timestamp", "")),
                "raw_json":  json.dumps(entry, ensure_ascii=False),
            })
            continue

        if etype != "message":
            continue
        msg = entry.get("message", {})
        role = msg.get("role", "")
        if role not in ("user", "assistant", "toolResult"):
            continue

        raw_json = json.dumps(entry, ensure_ascii=False)

        # ── toolResult ────────────────────────────────────────
        if role == "toolResult":
            tool_name = msg.get("toolName", "?")
            is_error  = msg.get("isError", False)
            preview, full_text, total_chars = _tool_result_preview(msg.get("content", ""))
            msgs.append({
                "id":           entry.get("id", ""),
                "role":         "toolResult",
                "tool_name":    tool_name,
                "is_error":     is_error,
                "text":         preview,
                "text_full":    full_text,
                "total_chars":  total_chars,
                "ts_iso":       entry.get("timestamp", ""),
                "ts_fmt":       fmt_iso(entry.get("timestamp", "")),
                "raw_json":     raw_json,
                "stop_reason":  "",
                # unused for toolResult but keep schema consistent
                "model": "", "input_tok": 0, "output_tok": 0, "cost": 0.0,
                "has_metadata": False, "text_full": "",
                "blocks": [],
            })
            continue

        # ── user / assistant ──────────────────────────────────
        content = msg.get("content", [])
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]

        blocks = []  # structured blocks for display
        plain_parts = []  # fallback plain text

        for block in (content if isinstance(content, list) else []):
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")

            if btype == "text":
                t = strip_markers(block.get("text", ""))
                plain_parts.append(t)
                blocks.append({"kind": "text", "text": t})

            elif btype == "thinking":
                t = block.get("thinking", "")
                has_sig = bool(block.get("thinkingSignature"))
                plain_parts.append(f"[Thinking: {t[:60]}…]")
                blocks.append({"kind": "thinking", "text": t, "encrypted": has_sig})

            elif btype == "toolCall":
                name = block.get("name", "?")
                args = block.get("arguments", {})
                args_str = json.dumps(args, ensure_ascii=False, indent=2) if isinstance(args, dict) else str(args)
                plain_parts.append(f"[Tool: {name}({args_str[:80]})]")
                blocks.append({"kind": "toolCall", "name": name, "args": args_str})

            elif btype == "toolResult":
                # embedded tool result inside assistant message (rare)
                preview, tc_full, tc = _tool_result_preview(block.get("content", ""))
                blocks.append({"kind": "toolResult", "name": block.get("toolName","?"),
                                "text": preview, "text_full": tc_full, "total_chars": tc,
                                "is_error": block.get("isError", False)})

        text = " ".join(p for p in plain_parts if p).strip()
        usage = msg.get("usage", {})
        cost_obj = usage.get("cost", {})
        cost = 0.0
        if isinstance(cost_obj, dict):
            total = cost_obj.get("total", 0)
            cost = max(0, total / 1_000_000) if isinstance(total, (int, float)) else 0.0
        elif isinstance(cost_obj, (int, float)):
            cost = max(0, float(cost_obj))

        clean_text, had_meta = strip_metadata(text) if role == "user" else (text, False)

        # Adjust blocks text for user messages with metadata
        if had_meta and blocks and blocks[0]["kind"] == "text":
            blocks[0]["text"] = clean_text

        msgs.append({
            "id":           entry.get("id", ""),
            "role":         role,
            "text":         clean_text,
            "text_full":    text,
            "has_metadata": had_meta,
            "blocks":       blocks,
            "ts_iso":       entry.get("timestamp", ""),
            "ts_fmt":       fmt_iso(entry.get("timestamp", "")),
            "model":        friendly_model(msg.get("model", "")),
            "stop_reason":  msg.get("stopReason", ""),
            "input_tok":    usage.get("input", 0),
            "output_tok":   usage.get("output", 0),
            "cost":         round(cost, 6),
            "raw_json":     raw_json,
            "tool_name":    "",
            "is_error":     False,
            "total_chars":  0,
        })
    return msgs

def load_all_sessions() -> list[dict]:
    """Scan all agents and return enriched session objects."""
    cutoff = now_ms() - ACTIVE_WINDOW_H * 3600 * 1000
    sessions = []

    for agent_dir in sorted(AGENTS_DIR.iterdir()):
        if not agent_dir.is_dir():
            continue
        store = load_sessions_store(agent_dir)
        sess_dir = agent_dir / "sessions"

        for key, val in store.items():
            if ":run:" in key:
                continue  # skip cron run sub-sessions

            updated_at = val.get("updatedAt", 0)
            if updated_at < cutoff:
                continue  # too old

            session_id = val.get("sessionId", "")
            jsonl_path = sess_dir / f"{session_id}.jsonl" if session_id else None
            has_file   = bool(jsonl_path and jsonl_path.exists())

            # Count messages & get stats
            msg_count   = 0
            last_model  = ""
            total_input = 0
            total_output= 0
            last_ts_iso = ""
            last_stop_reason = ""

            if has_file:
                tail = tail_jsonl(jsonl_path, 200)
                msgs = parse_messages(tail)
                real_msgs    = [m for m in msgs if m["role"] != "event"]
                msg_count    = len(real_msgs)
                total_input  = sum(m.get("input_tok", 0) for m in real_msgs)
                total_output = sum(m.get("output_tok", 0) for m in real_msgs)
                # Last assistant message model + stop_reason
                for m in reversed(real_msgs):
                    if m["role"] == "assistant" and m["model"] and m["model"] != "—":
                        last_model = m["model"]
                        break
                for m in reversed(real_msgs):
                    if m["role"] == "assistant" and m.get("stop_reason"):
                        last_stop_reason = m["stop_reason"]
                        break
                if msgs:
                    last_ts_iso = msgs[-1]["ts_iso"]

            # Prefer the JSONL last-message timestamp over sessions.json updatedAt,
            # because sessions.json is only written at session end / checkpoints,
            # not during active inference.
            last_ts_ms = 0
            if last_ts_iso:
                try:
                    dt = datetime.fromisoformat(last_ts_iso.replace("Z", "+00:00"))
                    last_ts_ms = int(dt.timestamp() * 1000)
                except Exception:
                    pass
            effective_updated_at = max(updated_at, last_ts_ms)

            stype = session_type(key, val)

            # Session label — prefer human-readable names from metadata
            origin = val.get("origin") or {}
            origin_label = origin.get("label", "").strip()
            parts = key.split(":")

            if stype == "group":
                # origin.label contains the group subject e.g. "Clawdine Twittering"
                label = origin_label or val.get("deliveryContext", {}).get("groupSubject", "") or key
            elif stype == "telegram":
                label = origin_label or f"DM {parts[-1]}" if parts else key
            elif stype == "main":
                if val.get("lastChannel") == "webchat" or (val.get("origin") or {}).get("provider") == "webchat":
                    label = "Direct / Webchat"
                else:
                    label = origin_label or "Direct"
            else:
                label = origin_label or (":".join(parts[2:]) if len(parts) > 2 else key)

            sessions.append({
                "key":        key,
                "agent":      agent_dir.name,
                "session_id": session_id,
                "type":       stype,
                "type_label": type_label(stype),
                "label":      label[:60],
                "updated_at": effective_updated_at,
                "updated_fmt":fmt_ts(effective_updated_at),
                "time_ago":   time_ago(effective_updated_at),
                "last_channel":  val.get("lastChannel", ""),
                "model":      last_model or friendly_model(val.get("model", "")),
                "context_pct":val.get("contextPct", 0),
                "msg_count":  msg_count,
                "total_cost": 0.0,
                "total_input": total_input,
                "total_output": total_output,
                "last_ts_iso": last_ts_iso,
                "last_ts_fmt": fmt_iso(last_ts_iso) if last_ts_iso else "",
                "last_stop_reason": last_stop_reason,
                "has_file":   has_file,
            })

    sessions.sort(key=lambda s: s["updated_at"], reverse=True)
    return sessions

def load_session_messages(session_id: str) -> list[dict]:
    """Load recent messages for a specific session_id."""
    for agent_dir in AGENTS_DIR.iterdir():
        if not agent_dir.is_dir():
            continue
        jsonl_path = agent_dir / "sessions" / f"{session_id}.jsonl"
        if jsonl_path.exists():
            entries = read_jsonl_full(jsonl_path)
            return parse_messages(entries)
    return []

# ── HTTP Handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass  # suppress default access log

    def send_json(self, data: dict | list, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, path: Path):
        try:
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_error(404, "Not found")

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"

        if path == "/" or path == "/index.html":
            self.send_html(Path(__file__).parent / "index.html")

        elif path == "/api/sessions":
            sessions = load_all_sessions()
            self.send_json({
                "sessions":    sessions,
                "count":       len(sessions),
                "generated_at": now_ms(),
                "generated_fmt": datetime.now().strftime("%H:%M:%S"),
                "active_window_h": ACTIVE_WINDOW_H,
            })

        elif path.startswith("/api/sessions/") and path.endswith("/messages"):
            parts = path.split("/")
            # /api/sessions/<id>/messages
            if len(parts) == 5:
                session_id = parts[3]
                msgs = load_session_messages(session_id)
                self.send_json({"messages": msgs, "count": len(msgs)})
            else:
                self.send_json({"error": "invalid path"}, 400)

        elif path.startswith("/api/sessions/") and "/entry/" in path and path.endswith("/full"):
            # /api/sessions/<session_id>/entry/<entry_id>/full
            parts = path.split("/")
            # parts: ['', 'api', 'sessions', session_id, 'entry', entry_id, 'full']
            if len(parts) == 7:
                session_id = parts[3]
                entry_id   = parts[5]
                full_text  = self._load_entry_full(session_id, entry_id)
                if full_text is None:
                    self.send_json({"error": "entry not found"}, 404)
                else:
                    self.send_json({"text": full_text})
            else:
                self.send_json({"error": "invalid path"}, 400)

        elif path == "/api/status":
            sessions = load_all_sessions()
            active   = [s for s in sessions if s["has_file"]]
            self.send_json({
                "status":        "ok",
                "session_count": len(sessions),
                "active_count":  len(active),
                "generated_at":  now_ms(),
            })

        else:
            self.send_error(404, "Not found")

    def _load_entry_full(self, session_id: str, entry_id: str):
        """Find entry by ID in the session JSONL and return its full text content."""
        for agent_dir in AGENTS_DIR.iterdir():
            if not agent_dir.is_dir():
                continue
            jsonl_path = agent_dir / "sessions" / f"{session_id}.jsonl"
            if not jsonl_path.exists():
                continue
            for entry in read_jsonl_full(jsonl_path):
                if entry.get("id", "") != entry_id:
                    continue
                msg = entry.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict):
                            t = block.get("text") or block.get("thinking", "")
                            if t:
                                parts.append(t)
                    return "\n".join(parts)
                return str(content)
        return None

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET")
        self.end_headers()


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Sessionwatcher server")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--bind", default=DEFAULT_BIND)
    args = parser.parse_args()

    server = HTTPServer((args.bind, args.port), Handler)
    url = f"http://{args.bind}:{args.port}"
    print(f"Sessionwatcher running → {url}")
    print(f"OpenClaw dir: {OPENCLAW_DIR}")
    print("Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")

if __name__ == "__main__":
    main()
