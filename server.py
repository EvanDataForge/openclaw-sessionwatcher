#!/usr/bin/env python3
"""
OpenClaw Session Watcher — Live session activity dashboard for OpenClaw
Runs on http://127.0.0.1:8090
"""

import json
import os
import re
import ipaddress
import time
import socket
import argparse
import mimetypes
import threading
import uuid
import queue
import hashlib
try:
    import websocket  # type: ignore
except ImportError:
    websocket = None
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qsl, urlencode

# ── Config ────────────────────────────────────────────────────────────────────
OPENCLAW_DIR = Path(os.environ.get("OPENCLAW_DIR", Path.home() / ".openclaw"))
AGENTS_DIR   = OPENCLAW_DIR / "agents"
DEFAULT_PORT = int(os.environ.get("SESSIONWATCHER_PORT", 8090))
DEFAULT_BIND = os.environ.get("SESSIONWATCHER_BIND", "127.0.0.1")
DEFAULT_ACCESS_TOKEN = os.environ.get("SESSIONWATCHER_ACCESS_TOKEN", "").strip()

ACCESS_COOKIE_NAME = "sessionwatcher_access"
ACCESS_QUERY_PARAM = "access_token"

ACTIVE_WINDOW_H = 24   # sessions active in last N hours are shown

# ── Metadata stripping ───────────────────────────────────────────────────────

_META_PATTERN = re.compile(
    r'^(?:[^\n]*?\(untrusted metadata\):\n```(?:json)?\n.*?\n```\n\n)+',
    re.DOTALL
)
_META_BLOCK_PATTERN = re.compile(
    r'[^\n]*?\(untrusted metadata\):\n```(?:json)?\n(.*?)\n```(?:\n\n|$)',
    re.DOTALL,
)
_DIRECT_GATEWAY_IDS = {
    "webchat-ui",
    "gateway-client",
    "openclaw-control-ui",
}

def strip_metadata(text: str) -> tuple[str, bool]:
    """Strip leading 'untrusted metadata' blocks injected by the gateway.
    Returns (clean_text, had_metadata)."""
    m = _META_PATTERN.match(text)
    if m:
        return text[m.end():].strip(), True
    return text, False

def parse_untrusted_metadata_blocks(text: str) -> list[dict]:
    """Extract JSON objects from leading '(untrusted metadata)' blocks."""
    if not text:
        return []

    blocks: list[dict] = []
    for m in _META_BLOCK_PATTERN.finditer(str(text)):
        raw = str(m.group(1) or "").strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        if isinstance(obj, dict):
            blocks.append(obj)
    return blocks

def _looks_like_telegram_id(value) -> bool:
    s = str(value or "").strip()
    return bool(s) and s.isdigit() and len(s) >= 5

def classify_user_source(meta_blocks: list[dict]) -> dict:
    """Best-effort source classifier for user messages: direct vs telegram."""
    if not meta_blocks:
        return {
            "source_channel": "unknown",
            "source_label": "",
            "source_id": "",
        }

    saw_telegram = False
    saw_direct = False
    source_label = ""
    source_id = ""

    for block in meta_blocks:
        if not isinstance(block, dict):
            continue

        sid = str(block.get("id") or "").strip()
        label = str(block.get("label") or "").strip()
        provider = str(block.get("provider") or "").strip().lower()
        keys = {str(k).strip().lower() for k in block.keys()}

        if not source_id and sid:
            source_id = sid
        if not source_label and label:
            source_label = label

        # Telegram indicators from conversation/sender metadata.
        if provider == "telegram":
            saw_telegram = True
        if {
            "sender_id",
            "message_id",
            "conversation_label",
            "group_subject",
            "is_group_chat",
        } & keys:
            saw_telegram = True
        if _looks_like_telegram_id(block.get("sender_id")) or _looks_like_telegram_id(sid):
            saw_telegram = True

        # Direct/webchat indicators from sender metadata.
        sid_l = sid.lower()
        label_l = label.lower()
        if sid_l in _DIRECT_GATEWAY_IDS or label_l in _DIRECT_GATEWAY_IDS:
            saw_direct = True
        if (
            "webchat" in sid_l
            or "webchat" in label_l
            or "gateway" in sid_l
            or "gateway" in label_l
            or "control-ui" in sid_l
            or "control-ui" in label_l
        ):
            saw_direct = True

    if saw_telegram and not saw_direct:
        channel = "telegram"
    elif saw_direct and not saw_telegram:
        channel = "direct"
    elif saw_telegram:
        channel = "telegram"
    elif saw_direct:
        channel = "direct"
    else:
        channel = "unknown"

    return {
        "source_channel": channel,
        "source_label": source_label,
        "source_id": source_id,
    }

_MARKER_PATTERN = re.compile(r'\[\[[^\]]*\]\]')
_GATEWAY_TIME_PREFIX_PATTERN = re.compile(
    r'^\[[A-Za-z]{3}\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?\s+GMT[^\]]*\]\s*'
)

def strip_markers(text: str) -> str:
    """Remove [[...]] markers like [[reply_to_current]] from message text."""
    return _MARKER_PATTERN.sub('', text).strip()

def strip_gateway_time_prefix(text: str) -> str:
    """Strip leading '[Tue 2026-... GMT+X]' prefix injected by gateway envelopes."""
    if not text:
        return ""
    return _GATEWAY_TIME_PREFIX_PATTERN.sub('', str(text), count=1).strip()

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
    # Key semantics are the most stable source. Keep channel identity anchored
    # to the session key so transient lastChannel/origin updates do not relabel
    # Telegram sessions as webchat/direct.
    if ":cron:" in key or key.startswith("cron:"):
        return "cron"
    if ":subagent:" in key or "subagent" in key:
        return "subagent"
    if ":telegram:group:" in key or key.startswith("telegram:group:"):
        return "group"
    if ":telegram:" in key or key.startswith("telegram:"):
        return "telegram"

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

def is_public_host(host: str) -> bool:
    raw = str(host or "").strip().lower()
    if not raw:
        return False
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1].strip()
    if raw == "localhost":
        return False
    if raw in {"0.0.0.0", "::"}:
        return True
    try:
        return not ipaddress.ip_address(raw).is_loopback
    except ValueError:
        return True

def assert_public_bind_allowed(bind: str, access_token: str):
    if not is_public_host(bind):
        return
    if str(access_token or "").strip():
        return
    raise SystemExit(
        f'Refusing to bind OpenClaw Session Watcher to public host "{bind}" without '
        "SESSIONWATCHER_ACCESS_TOKEN. Set SESSIONWATCHER_ACCESS_TOKEN or bind to 127.0.0.1/::1/localhost."
    )

def parse_cookies(header: str | None) -> dict[str, str]:
    raw = str(header or "")
    cookies: dict[str, str] = {}
    for part in raw.split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if not key:
            continue
        cookies[key] = value.strip()
    return cookies

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

def _iter_job_dicts(payload) -> list[dict]:
    """Extract job-like dicts from common config layouts."""
    items: list[dict] = []
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                items.append(item)
        return items

    if isinstance(payload, dict):
        # shape: {"jobs": [...]}
        jobs = payload.get("jobs")
        if isinstance(jobs, list):
            for item in jobs:
                if isinstance(item, dict):
                    items.append(item)
            return items

        # shape: {"jobs": {"<id>": {...}}}
        if isinstance(jobs, dict):
            for key, val in jobs.items():
                if isinstance(val, dict):
                    item = dict(val)
                    item.setdefault("id", str(key))
                    items.append(item)
            return items

        # shape: {"<id>": {...}}
        for key, val in payload.items():
            if isinstance(val, dict):
                item = dict(val)
                item.setdefault("id", str(key))
                items.append(item)
        return items

    return items

def load_cron_name_map() -> dict[str, str]:
    """Build mapping cron_id -> cron name from OpenClaw cron config files."""
    mapping: dict[str, str] = {}

    jobs_path = OPENCLAW_DIR / "cron" / "jobs.json"
    try:
        data = json.loads(jobs_path.read_text(encoding="utf-8"))
        for job in _iter_job_dicts(data):
            jid = str(job.get("id", "")).strip()
            jname = str(job.get("name", "")).strip()
            if jid and jname:
                mapping[jid] = jname
    except Exception:
        pass

    openclaw_path = OPENCLAW_DIR / "openclaw.json"
    try:
        data = json.loads(openclaw_path.read_text(encoding="utf-8"))
        cron_cfg = data.get("cron") if isinstance(data, dict) else None
        for job in _iter_job_dicts(cron_cfg):
            jid = str(job.get("id", "")).strip()
            jname = str(job.get("name", "")).strip()
            if jid and jname and jid not in mapping:
                mapping[jid] = jname
    except Exception:
        pass

    return mapping

def load_cron_sessionkey_map() -> dict[str, str]:
    """Build mapping cron_id -> sessionKey from OpenClaw cron config files."""
    mapping: dict[str, str] = {}

    jobs_path = OPENCLAW_DIR / "cron" / "jobs.json"
    try:
        data = json.loads(jobs_path.read_text(encoding="utf-8"))
        for job in _iter_job_dicts(data):
            jid = str(job.get("id", "")).strip()
            jkey = str(job.get("sessionKey", "")).strip()
            if jid and jkey:
                mapping[jid] = jkey
    except Exception:
        pass

    openclaw_path = OPENCLAW_DIR / "openclaw.json"
    try:
        data = json.loads(openclaw_path.read_text(encoding="utf-8"))
        cron_cfg = data.get("cron") if isinstance(data, dict) else None
        for job in _iter_job_dicts(cron_cfg):
            jid = str(job.get("id", "")).strip()
            jkey = str(job.get("sessionKey", "")).strip()
            if jid and jkey and jid not in mapping:
                mapping[jid] = jkey
    except Exception:
        pass

    return mapping

def load_gateway_config() -> dict:
    """Load OpenClaw gateway configuration from openclaw.json.
    Returns dict with 'token', 'bind', 'port', 'available' keys.
    """
    openclaw_path = OPENCLAW_DIR / "openclaw.json"
    if not openclaw_path.exists():
        return {"available": False, "error": "openclaw.json not found"}
    
    try:
        data = json.loads(openclaw_path.read_text(encoding="utf-8"))
        gateway_cfg = data.get("gateway", {})
        
        token = gateway_cfg.get("auth", {}).get("token", "")
        bind = gateway_cfg.get("bind", "loopback")
        port = gateway_cfg.get("port", 18789)
        
        # Resolve "loopback" to actual address
        if bind == "loopback":
            bind = "127.0.0.1"
        
        if not token:
            return {"available": False, "error": "No gateway token configured"}
        
        return {
            "available": True,
            "token": token,
            "bind": bind,
            "port": port
        }
    except Exception as e:
        return {"available": False, "error": f"Failed to load config: {e}"}

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

def _parse_iso_ms(iso_ts: str) -> int:
    """Parse ISO-8601 timestamp to epoch milliseconds (best effort)."""
    if not iso_ts:
        return 0
    try:
        return int(datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0

def _normalize_user_text(text: str) -> str:
    """Normalize user text for short-window duplicate detection."""
    if not text:
        return ""
    return " ".join(str(text).strip().split()).casefold()

def _dedupe_retry_user_messages(msgs: list[dict]) -> list[dict]:
    """Collapse duplicate user messages created by internal retry/fallback.

    Some agent pipelines append the same user message again after an immediate
    assistant error during model/provider fallback. Keep the first user message
    and suppress the retry duplicate when it appears within a short window.
    """
    if not msgs:
        return msgs

    filtered: list[dict] = []
    last_user_norm = ""
    last_user_ts = 0
    saw_error_since_last_user = False

    for m in msgs:
        role = m.get("role", "")

        if role == "assistant":
            stop_reason = str(m.get("stop_reason", "") or "").lower()
            has_text = bool(str(m.get("text", "") or "").strip())
            if stop_reason == "error" and not has_text:
                saw_error_since_last_user = True
        elif role == "event" and m.get("event_type") == "error":
            saw_error_since_last_user = True

        if role == "user":
            norm = _normalize_user_text(m.get("text", ""))
            ts_ms = _parse_iso_ms(m.get("ts_iso", ""))

            is_retry_duplicate = (
                bool(norm)
                and saw_error_since_last_user
                and norm == last_user_norm
                and bool(last_user_ts)
                and bool(ts_ms)
                and 0 <= (ts_ms - last_user_ts) <= 5000
            )

            if is_retry_duplicate:
                saw_error_since_last_user = False
                continue

            last_user_norm = norm
            last_user_ts = ts_ms
            saw_error_since_last_user = False

        filtered.append(m)

    return filtered

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

        if etype == "custom":
            custom_type = entry.get("customType", "")
            data = entry.get("data", {})
            if custom_type == "openclaw:prompt-error":
                error = data.get("error", "?")
                model = data.get("model", "")
                text = f"prompt error: {error}" + (f" · {model}" if model else "")
                msgs.append({
                    "id":         entry.get("id", ""),
                    "role":       "event",
                    "event_type": "error",
                    "text":       text,
                    "ts_iso":     entry.get("timestamp", ""),
                    "ts_fmt":     fmt_iso(entry.get("timestamp", "")),
                    "raw_json":   json.dumps(entry, ensure_ascii=False),
                })
            # model-snapshot and other custom types: silently skip
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
            details   = msg.get("details", {})  # Extract details field
            preview, full_text, total_chars = _tool_result_preview(msg.get("content", ""))
            msgs.append({
                "id":           entry.get("id", ""),
                "role":         "toolResult",
                "tool_name":    tool_name,
                "is_error":     is_error,
                "details":      details,  # Pass details to frontend
                "text":         preview,
                "text_full":    full_text,
                "total_chars":  total_chars,
                "ts_iso":       entry.get("timestamp", ""),
                "ts_fmt":       fmt_iso(entry.get("timestamp", "")),
                "raw_json":     raw_json,
                "stop_reason":  "",
                # unused for toolResult but keep schema consistent
                "model": "", "input_tok": 0, "output_tok": 0, "cost": 0.0,
                "has_metadata": False,
                "source_channel": "unknown",
                "source_label": "",
                "source_id": "",
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
        source_info = classify_user_source(parse_untrusted_metadata_blocks(text)) if role == "user" else {
            "source_channel": "unknown",
            "source_label": "",
            "source_id": "",
        }
        usage = msg.get("usage", {})
        cost_obj = usage.get("cost", {})
        cost = 0.0
        if isinstance(cost_obj, dict):
            total = cost_obj.get("total", 0)
            cost = max(0, total / 1_000_000) if isinstance(total, (int, float)) else 0.0
        elif isinstance(cost_obj, (int, float)):
            cost = max(0, float(cost_obj))

        clean_text, had_meta = strip_metadata(text) if role == "user" else (text, False)
        if role == "user" and had_meta:
            clean_text = strip_gateway_time_prefix(clean_text)

        # Adjust blocks text for user messages with metadata
        if had_meta and blocks and blocks[0]["kind"] == "text":
            blocks[0]["text"] = clean_text

        msgs.append({
            "id":           entry.get("id", ""),
            "role":         role,
            "text":         clean_text,
            "text_full":    text,
            "has_metadata": had_meta,
            "source_channel": source_info["source_channel"],
            "source_label": source_info["source_label"],
            "source_id": source_info["source_id"],
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
    return _dedupe_retry_user_messages(msgs)

def load_all_sessions() -> list[dict]:
    """Scan all agents and return enriched session objects."""
    cutoff = now_ms() - ACTIVE_WINDOW_H * 3600 * 1000
    sessions = []
    cron_name_map = load_cron_name_map()
    cron_sessionkey_map = load_cron_sessionkey_map()

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

            # Resolve JSONL file path: prefer explicit sessionFile, fallback to sessionId
            session_file_declared = val.get("sessionFile", "")
            if session_file_declared:
                jsonl_path = Path(session_file_declared)
                session_id = jsonl_path.stem  # extract session_id from filename
            else:
                session_id = val.get("sessionId", "")
                jsonl_path = sess_dir / f"{session_id}.jsonl" if session_id else None

            has_file = bool(jsonl_path and jsonl_path.exists())

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

            cron_id = ""
            if stype == "cron":
                if key.startswith("cron:") and len(parts) > 1:
                    cron_id = parts[1].strip()
                else:
                    try:
                        cron_idx = parts.index("cron")
                        if cron_idx + 1 < len(parts):
                            cron_id = parts[cron_idx + 1].strip()
                    except ValueError:
                        cron_id = ""

            if stype == "group":
                # origin.label contains the group subject e.g. "Clawdine Twittering"
                label = origin_label or val.get("deliveryContext", {}).get("groupSubject", "") or key
            elif stype == "telegram":
                label = origin_label or f"DM {parts[-1]}" if parts else key
            elif stype == "cron":
                label = cron_name_map.get(cron_id, "") or origin_label or key
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
                "label_sub":  cron_id if cron_id else "",
                "cron_id":    cron_id,
                "updated_at": effective_updated_at,
                "updated_fmt":fmt_ts(effective_updated_at),
                "time_ago":   time_ago(effective_updated_at),
                "last_channel":  val.get("lastChannel", ""),
                "session_key": cron_sessionkey_map.get(cron_id, key) if cron_id else key,
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

def find_session_jsonl_path(session_id: str) -> Path | None:
    """Resolve a session JSONL path by session_id across all agents."""
    if not session_id:
        return None
    for agent_dir in AGENTS_DIR.iterdir():
        if not agent_dir.is_dir():
            continue
        jsonl_path = agent_dir / "sessions" / f"{session_id}.jsonl"
        if jsonl_path.exists():
            return jsonl_path
    return None

def session_file_state(path: Path) -> dict | None:
    """Return lightweight file state used to detect log updates."""
    try:
        st = path.stat()
        return {
            "last_mtime_ns": int(st.st_mtime_ns),
            "last_size": int(st.st_size),
        }
    except FileNotFoundError:
        return None
    except Exception:
        return None

# ── Gateway WebSocket Client ──────────────────────────────────────────────────

class GatewayClient:
    """WebSocket client for OpenClaw Gateway JSON-RPC protocol."""
    
    def __init__(self, host: str, port: int, token: str):
        self.host = host
        self.port = port
        self.token = token
        self.ws = None
        self.connected = False
        self.thread = None
        self.nonce = None
        self.pending_requests = {}  # request_id -> queue for responses
        self.lock = threading.Lock()
        self._stop_event = threading.Event()
        self._reconnect_delay_s = 1.5
        
    def connect(self):
        """Start WebSocket connection in background thread."""
        if self.connected:
            return True

        if not (self.thread and self.thread.is_alive()):
            self._stop_event.clear()
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

        # Wait max 5s for connection
        for _ in range(50):
            if self.connected:
                return True
            time.sleep(0.1)
        return bool(self.connected)
    
    def disconnect(self):
        """Stop WebSocket connection."""
        self._stop_event.set()
        if self.ws:
            self.ws.close()
        if self.thread:
            self.thread.join(timeout=2)
        self.connected = False
    
    def _run(self):
        """WebSocket thread main loop with automatic reconnect."""
        if websocket is None:
            print("Gateway WebSocket unavailable: install websocket-client")
            return
        url = f"ws://{self.host}:{self.port}"
        while not self._stop_event.is_set():
            try:
                self.ws = websocket.WebSocketApp(
                    url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close
                )
                self.ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                print(f"Gateway WebSocket error: {e}")
            finally:
                self.connected = False
                self.ws = None

            if self._stop_event.is_set():
                break
            time.sleep(self._reconnect_delay_s)
    
    def _on_open(self, ws):
        """WebSocket opened - wait for connect.challenge."""
        pass
    
    def _on_message(self, ws, message):
        """Handle incoming WebSocket messages."""
        try:
            data = json.loads(message)
            msg_type = data.get("type")
            
            # Handle connect.challenge
            if msg_type == "event" and data.get("event") == "connect.challenge":
                self.nonce = data.get("payload", {}).get("nonce")
                self._send_connect()
            
            # Handle connect response (hello-ok)
            elif msg_type == "res":
                req_id = data.get("id")
                if data.get("ok") and data.get("payload", {}).get("type") == "hello-ok":
                    self.connected = True
                    print("Gateway WebSocket connected")
                
                # Deliver response to waiting request
                with self.lock:
                    if req_id in self.pending_requests:
                        self.pending_requests[req_id].put(data)
            
            # Handle chat events (for live updates)
            elif msg_type == "event" and data.get("event") == "chat":
                pass  # Live chat events could be handled here
                
        except Exception as e:
            print(f"Gateway message parse error: {e}")
    
    def _on_error(self, ws, error):
        """WebSocket error."""
        self.connected = False
        print(f"Gateway WebSocket error: {error}")
    
    def _on_close(self, ws, close_status_code, close_msg):
        """WebSocket closed."""
        self.connected = False
        print(f"Gateway WebSocket closed: {close_status_code} {close_msg}")
    
    def _send_connect(self):
        """Send connect request with token auth."""
        if not self.nonce:
            return
        
        req_id = str(uuid.uuid4())
        connect_req = {
            "type": "req",
            "id": req_id,
            "method": "connect",
            "params": {
                "minProtocol": 3,
                "maxProtocol": 3,
                "client": {
                    "id": "webchat-ui",
                    "version": "1.0.0",
                    "platform": "web",
                    "mode": "webchat"
                },
                "role": "operator",
                "scopes": ["operator.admin", "operator.write", "operator.read"],
                "caps": [],
                "commands": [],
                "permissions": {},
                "auth": {
                    "token": self.token
                }
            }
        }
        
        try:
            self.ws.send(json.dumps(connect_req))
        except Exception as e:
            print(f"Failed to send connect: {e}")
    
    def send_chat(
        self,
        session_key: str,
        message: str,
        timeout_ms: int = 180000,
        idempotency_key: str | None = None,
    ) -> dict:
        """Send chat message via JSON-RPC chat.send method.
        Returns response dict or error dict.
        """
        if not self.connected and not self.connect():
            return {"ok": False, "error": "Gateway not connected"}
        if not self.ws:
            return {"ok": False, "error": "Gateway transport unavailable"}
        
        req_id = str(uuid.uuid4())
        idem = str(idempotency_key or "").strip() or str(uuid.uuid4())
        
        request = {
            "type": "req",
            "id": req_id,
            "method": "chat.send",
            "params": {
                "sessionKey": session_key,
                "message": message,
                "idempotencyKey": idem,
                "deliver": False,
                "thinking": "low",
                "timeoutMs": timeout_ms
            }
        }
        
        # Create response queue
        response_queue = queue.Queue()
        with self.lock:
            self.pending_requests[req_id] = response_queue
        
        try:
            self.ws.send(json.dumps(request))
            
            # Wait for response (max 5s for ACK)
            try:
                response = response_queue.get(timeout=5.0)
                return response
            except queue.Empty:
                return {"ok": False, "error": "Gateway timeout"}
        except Exception as e:
            return {"ok": False, "error": str(e)}
        finally:
            with self.lock:
                self.pending_requests.pop(req_id, None)

# ── HTTP Handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress default access log

    def _access_token(self) -> str:
        return str(getattr(self.server, "access_token", "") or "").strip()

    def _auth_enabled(self) -> bool:
        return bool(self._access_token())

    def _is_authorized(self) -> bool:
        if not self._auth_enabled():
            return True
        cookies = parse_cookies(self.headers.get("Cookie"))
        return cookies.get(ACCESS_COOKIE_NAME) == self._access_token()

    def _send_auth_error(self, api_request: bool, message: str, status: int = 401):
        if api_request:
            self.send_json({
                "error": message,
                "auth_required": True,
                "auth_mode": "access_token",
                "bootstrap": f"/?{ACCESS_QUERY_PARAM}=...",
            }, status=status)
            return

        body = f"""<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
    <title>OpenClaw Session Watcher Access Required</title>
  <style>
    body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, sans-serif; background: #0b0f17; color: #e8edf7; display: grid; place-items: center; min-height: 100vh; }}
    .card {{ width: min(560px, calc(100vw - 32px)); background: #121826; border: 1px solid #27324a; border-radius: 18px; padding: 24px; box-shadow: 0 18px 48px rgba(0,0,0,.35); }}
    h1 {{ margin: 0 0 12px; font-size: 22px; }}
    p {{ margin: 0 0 12px; line-height: 1.5; color: #c6d0e1; }}
    code {{ background: #0b1220; padding: 2px 6px; border-radius: 6px; color: #8dd3ff; }}
  </style>
</head>
<body>
  <div class=\"card\">
        <h1>🔐 OpenClaw Session Watcher access required</h1>
    <p>{message}</p>
    <p>Open this page once with <code>/?{ACCESS_QUERY_PARAM}=YOUR_TOKEN</code> to store the access cookie.</p>
  </div>
</body>
</html>""".encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _maybe_handle_access_gate(self, parsed, path: str) -> bool:
        if not self._auth_enabled():
            return False

        provided = None
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if key == ACCESS_QUERY_PARAM:
                provided = value
                break

        api_request = path.startswith("/api/")

        if provided is not None:
            if provided != self._access_token():
                self._send_auth_error(api_request, "Invalid OpenClaw Session Watcher access token.")
                return True

            filtered_query = [
                (key, value)
                for key, value in parse_qsl(parsed.query, keep_blank_values=True)
                if key != ACCESS_QUERY_PARAM
            ]
            location = parsed.path or "/"
            if filtered_query:
                location = f"{location}?{urlencode(filtered_query, doseq=True)}"

            self.send_response(302)
            self.send_header(
                "Set-Cookie",
                f"{ACCESS_COOKIE_NAME}={self._access_token()}; HttpOnly; Path=/; SameSite=Lax",
            )
            self.send_header("Cache-Control", "no-store")
            self.send_header("Location", location)
            self.end_headers()
            return True

        if self._is_authorized():
            return False

        self._send_auth_error(
            api_request,
            "OpenClaw Session Watcher access token required. Open /?access_token=... once to continue.",
        )
        return True

    def send_json(self, data: dict | list, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, path: Path):
        try:
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_error(404, "Not found")

    def send_file(self, path: Path, content_type: str, cache_control: str = "public, max-age=86400"):
        """Send a static file with appropriate headers."""
        try:
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", cache_control)
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_error(404, "Not found")

    def _sse_write(self, event: str, data: dict | None = None, retry_ms: int | None = None):
        chunks = []
        if retry_ms is not None:
            chunks.append(f"retry: {int(retry_ms)}\n")
        if event:
            chunks.append(f"event: {event}\n")
        if data is not None:
            payload = json.dumps(data, ensure_ascii=False)
            chunks.append(f"data: {payload}\n")
        chunks.append("\n")
        self.wfile.write("".join(chunks).encode("utf-8"))
        self.wfile.flush()

    def _stream_session_events(self, session_id: str):
        jsonl_path = find_session_jsonl_path(session_id)
        if not jsonl_path:
            self.send_json({"error": "session not found"}, 404)
            return

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        poll_interval_s = 0.25
        heartbeat_interval_s = 12.0
        next_heartbeat = time.monotonic() + heartbeat_interval_s

        seq = 0
        prev_state = session_file_state(jsonl_path)
        ready_payload = {
            "session_id": session_id,
            "hint_new_messages": False,
            "seq": seq,
            "last_mtime_ns": prev_state["last_mtime_ns"] if prev_state else None,
            "last_size": prev_state["last_size"] if prev_state else None,
        }

        try:
            self._sse_write("ready", ready_payload, retry_ms=1000)

            while True:
                now = time.monotonic()
                current_state = session_file_state(jsonl_path)

                # Only send changed event if file actually grew (new content)
                grew = False
                if prev_state and current_state:
                    grew = current_state["last_size"] > prev_state["last_size"]
                elif current_state and not prev_state:
                    grew = True  # File appeared
                
                if grew:
                    seq += 1
                    changed_payload = {
                        "session_id": session_id,
                        "hint_new_messages": True,
                        "seq": seq,
                        "last_mtime_ns": current_state["last_mtime_ns"] if current_state else None,
                        "last_size": current_state["last_size"] if current_state else None,
                    }
                    self._sse_write("changed", changed_payload)
                
                prev_state = current_state

                if now >= next_heartbeat:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    next_heartbeat = now + heartbeat_interval_s

                time.sleep(poll_interval_s)

        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            return
        except Exception:
            return

    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"

        if self._maybe_handle_access_gate(parsed, path):
            return

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

        elif path.startswith("/api/sessions/") and path.endswith("/events"):
            parts = path.split("/")
            # /api/sessions/<id>/events
            if len(parts) == 5:
                session_id = parts[3]
                self._stream_session_events(session_id)
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

        elif path == "/api/config/gateway":
            config = load_gateway_config()
            # Never expose the gateway auth token to browser clients.
            safe_config = {k: v for k, v in config.items() if k != "token"}
            self.send_json(safe_config)

        elif path == "/app.ico" or path == "/favicon.ico":
            self.send_file(
                Path(__file__).parent / "app.ico",
                "image/x-icon",
                cache_control="no-store, no-cache, must-revalidate",
            )

        elif path.startswith("/doc/"):
            # Serve screenshots/logo assets used by the UI and About dialog.
            base_dir = (Path(__file__).parent / "doc").resolve()
            requested = (Path(__file__).parent / path.lstrip("/")).resolve()
            try:
                requested.relative_to(base_dir)
            except ValueError:
                self.send_error(403, "Forbidden")
                return

            content_type, _ = mimetypes.guess_type(requested.name)
            self.send_file(requested, content_type or "application/octet-stream")

        else:
            self.send_error(404, "Not found")
    
    def do_POST(self):
        """Handle POST requests."""
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"
        
        if not self._is_authorized():
            self._send_auth_error(api_request=True, message="Unauthorized")
            return
        
        if path == "/api/chat/send":
            try:
                content_length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(content_length).decode('utf-8')
                data = json.loads(body)
                
                session_key = data.get("sessionKey")
                message = data.get("message")
                idempotency_key = str(data.get("idempotencyKey") or "").strip() or None
                
                if not session_key or not message:
                    self.send_json({"ok": False, "error": "Missing sessionKey or message"}, 400)
                    return
                
                # Get gateway client from server
                gateway_client = getattr(self.server, "gateway_client", None)
                if not gateway_client:
                    self.send_json({"ok": False, "error": "Gateway not connected"}, 503)
                    return

                # Best effort: lazily reconnect if the socket dropped after startup.
                if not gateway_client.connected and not gateway_client.connect():
                    self.send_json({"ok": False, "error": "Gateway not connected"}, 503)
                    return

                # Best-effort duplicate protection for accidental double submits.
                # If same session+message arrives within 2s, return cached ACK.
                dedupe_window_s = 2.0
                dedupe_key = hashlib.sha256(f"{session_key}\n{message}".encode("utf-8")).hexdigest()
                now_ts = time.monotonic()
                with self.server.chat_send_lock:
                    recent = self.server.chat_send_recent
                    # prune old cache entries
                    for k, v in list(recent.items()):
                        if now_ts - float(v.get("ts", 0.0)) > 15.0:
                            recent.pop(k, None)
                    cached = recent.get(dedupe_key)
                    if cached and (now_ts - float(cached.get("ts", 0.0)) <= dedupe_window_s):
                        cached_result = dict(cached.get("result", {}))
                        if cached_result.get("ok"):
                            cached_result["deduped"] = True
                            self.send_json(cached_result)
                            return
                
                # Send message via gateway
                response = gateway_client.send_chat(
                    session_key,
                    message,
                    idempotency_key=idempotency_key,
                )
                
                if response.get("ok"):
                    result = {
                        "ok": True,
                        "runId": response.get("payload", {}).get("runId"),
                        "status": response.get("payload", {}).get("status"),
                    }
                    with self.server.chat_send_lock:
                        self.server.chat_send_recent[dedupe_key] = {
                            "ts": now_ts,
                            "result": result,
                        }
                    self.send_json(result)
                else:
                    err_msg = str(response.get("error", "Unknown error"))
                    err_status = 503 if "not connected" in err_msg.lower() else 500
                    self.send_json({
                        "ok": False,
                        "error": err_msg
                    }, err_status)
                    
            except json.JSONDecodeError:
                self.send_json({"ok": False, "error": "Invalid JSON"}, 400)
            except Exception as e:
                self.send_json({"ok": False, "error": str(e)}, 500)
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
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"
        if self._maybe_handle_access_gate(parsed, path):
            return
        self.send_response(204)
        self.send_header("Allow", "GET, OPTIONS")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()


class SessionwatcherHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="OpenClaw Session Watcher server")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--bind", default=DEFAULT_BIND)
    parser.add_argument("--access-token", default=DEFAULT_ACCESS_TOKEN)
    args = parser.parse_args()

    access_token = str(args.access_token or "").strip()
    assert_public_bind_allowed(args.bind, access_token)

    server = SessionwatcherHTTPServer((args.bind, args.port), Handler)
    server.access_token = access_token
    server.chat_send_lock = threading.Lock()
    server.chat_send_recent = {}
    
    # Initialize Gateway WebSocket client
    gateway_config = load_gateway_config()
    if websocket is None:
        print("websocket-client not installed: chat features disabled")
        server.gateway_client = None
    elif gateway_config.get("available"):
        gateway_client = GatewayClient(
            host=gateway_config["bind"],
            port=gateway_config["port"],
            token=gateway_config["token"]
        )
        server.gateway_client = gateway_client
        
        # Start connection in background
        print(f"Connecting to OpenClaw Gateway at {gateway_config['bind']}:{gateway_config['port']}...")
        if gateway_client.connect():
            print("Gateway connection established")
        else:
            print("Gateway connection failed (chat features disabled)")
    else:
        print(f"Gateway not available: {gateway_config.get('error', 'unknown')} (chat features disabled)")
        server.gateway_client = None
    
    url = f"http://{args.bind}:{args.port}"
    print(f"OpenClaw Session Watcher running → {url}")
    print(f"OpenClaw dir: {OPENCLAW_DIR}")
    if access_token:
        print("Access protection: enabled")
        print(f"Bootstrap login: {url}/?{ACCESS_QUERY_PARAM}=<token>")
    if args.bind == "0.0.0.0":
        try:
            local_ip = socket.gethostbyname(socket.gethostname())
            print(f"LAN access: http://{local_ip}:{args.port}")
        except Exception:
            pass
    print("Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
        if hasattr(server, 'gateway_client') and server.gateway_client:
            server.gateway_client.disconnect()
        print("Stopped.")

if __name__ == "__main__":
    main()
