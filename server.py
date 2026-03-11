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

# ── JSON helpers ─────────────────────────────────────────────────────────────

_TRAILING_COMMA_RE = re.compile(r',\s*([}\]])')

def json_loads_lenient(text: str):
    """Parse JSON that may contain trailing commas (e.g. JSON5/JSONC style).
    Strips trailing commas before handing off to the standard parser."""
    cleaned = _TRAILING_COMMA_RE.sub(r'\1', text)
    return json.loads(cleaned)

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
_GENERIC_ORIGIN_LABELS = {
    "direct",
    "direct/webchat",
    "direct / webchat",
    "webchat",
    "telegram",
    "group",
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


def _looks_like_session_key(value: str) -> bool:
    s = str(value or "").strip()
    return s.startswith("agent:") and ":" in s


def _is_generic_origin_label(value: str) -> bool:
    s = str(value or "").strip().casefold()
    if not s:
        return True
    if s in _GENERIC_ORIGIN_LABELS:
        return True
    return s.startswith("dm ")


def _extract_group_name_from_conversation_label(value: str) -> str:
    """Extract display name from labels like 'Clawdine Sidechannel id:-100...'."""
    raw = str(value or "").strip()
    if not raw:
        return ""

    for sep in (" id:", " (id:", " [id:"):
        idx = raw.casefold().find(sep)
        if idx > 0:
            return raw[:idx].strip()

    return raw


def _content_text_blocks(content) -> list[str]:
    """Extract plain text blocks from OpenClaw message content payload."""
    out: list[str] = []

    if isinstance(content, str):
        txt = content.strip()
        return [txt] if txt else []

    if not isinstance(content, list):
        return out

    for block in content:
        if not isinstance(block, dict):
            continue
        if str(block.get("type", "")).strip() != "text":
            continue
        txt = str(block.get("text", "")).strip()
        if txt:
            out.append(txt)

    return out


def infer_telegram_group_label_from_entries(entries: list[dict]) -> str:
    """Best-effort group name extraction from untrusted Telegram metadata."""
    if not entries:
        return ""

    for entry in reversed(entries):
        if not isinstance(entry, dict) or entry.get("type") != "message":
            continue

        msg = entry.get("message")
        if not isinstance(msg, dict):
            continue

        for text in _content_text_blocks(msg.get("content", [])):
            blocks = parse_untrusted_metadata_blocks(text)
            if not blocks:
                continue

            for block in reversed(blocks):
                if not isinstance(block, dict):
                    continue

                group_subject = str(block.get("group_subject", "")).strip()
                if group_subject:
                    return group_subject

                conversation_label = _extract_group_name_from_conversation_label(
                    block.get("conversation_label", "")
                )
                if conversation_label:
                    return conversation_label

    return ""


def infer_telegram_group_label_from_paths(jsonl_paths: list[Path]) -> str:
    """Find Telegram group label from recent session history JSONL aliases."""
    if not jsonl_paths:
        return ""

    # Fast path: scan recent tails first.
    for path in reversed(jsonl_paths):
        label = infer_telegram_group_label_from_entries(tail_jsonl(path, 250))
        if label:
            return label

    # Fallback: scan full files if recent tail did not contain metadata.
    for path in reversed(jsonl_paths):
        label = infer_telegram_group_label_from_entries(read_jsonl_full(path))
        if label:
            return label

    return ""

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

_NON_MODEL_VALUES = {
    "-",
    "—",
    "unknown",
    "delivery-mirror",
}


def is_display_model(raw: str) -> bool:
    value = str(raw or "").strip()
    if not value:
        return False
    return value.casefold() not in _NON_MODEL_VALUES


def friendly_model(raw: str) -> str:
    if not is_display_model(raw):
        return "—"
    raw = str(raw).strip()
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
        data = json_loads_lenient(openclaw_path.read_text(encoding="utf-8"))
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
        data = json_loads_lenient(openclaw_path.read_text(encoding="utf-8"))
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
        data = json_loads_lenient(openclaw_path.read_text(encoding="utf-8"))
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

def create_gateway_client_from_runtime() -> "GatewayClient | None":
    """Create a GatewayClient from current runtime config when possible."""
    if websocket is None:
        return None

    gateway_config = load_gateway_config()
    if not gateway_config.get("available"):
        return None

    return GatewayClient(
        host=gateway_config["bind"],
        port=gateway_config["port"],
        token=gateway_config["token"],
    )

def ensure_server_gateway_client(
    server_obj,
    *,
    max_attempts: int = 3,
    wait_per_attempt_s: float = 1.5,
    retry_delay_s: float = 2.0,
):
    """Return a connected gateway client, recreating it if needed."""
    lock = getattr(server_obj, "gateway_client_lock", None)
    if lock is None:
        lock = threading.Lock()
        server_obj.gateway_client_lock = lock

    with lock:
        client = getattr(server_obj, "gateway_client", None)
        if client is None:
            client = create_gateway_client_from_runtime()
            server_obj.gateway_client = client

        if client and client.ensure_connected(
            max_attempts=max_attempts,
            wait_per_attempt_s=wait_per_attempt_s,
            retry_delay_s=retry_delay_s,
        ):
            return client

        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass

        fresh_client = create_gateway_client_from_runtime()
        server_obj.gateway_client = fresh_client
        if fresh_client and fresh_client.ensure_connected(
            max_attempts=max_attempts,
            wait_per_attempt_s=wait_per_attempt_s,
            retry_delay_s=retry_delay_s,
        ):
            return fresh_client

        return None


def rebuild_server_gateway_client(server_obj):
    """Disconnect and replace the shared gateway client instance."""
    lock = getattr(server_obj, "gateway_client_lock", None)
    if lock is None:
        lock = threading.Lock()
        server_obj.gateway_client_lock = lock

    with lock:
        client = getattr(server_obj, "gateway_client", None)
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass

        fresh_client = create_gateway_client_from_runtime()
        server_obj.gateway_client = fresh_client
        return fresh_client


def gateway_response_error(response: dict | None) -> str:
    """Return a normalized error string from a gateway response dict."""
    if not isinstance(response, dict):
        return str(response or "").strip()
    return str(response.get("error", "") or "").strip()


def gateway_chat_unavailable_reason() -> str:
    """Return a human-readable reason when chat transport cannot run at all."""
    if websocket is None:
        return (
            "SessionWatcher chat backend unavailable: websocket-client is not "
            "installed in the server Python runtime"
        )

    gateway_config = load_gateway_config()
    if not gateway_config.get("available"):
        return f"Gateway unavailable: {gateway_config.get('error', 'unknown error')}"

    return ""


def is_retryable_gateway_response(response: dict | None) -> bool:
    """Detect transport-level gateway failures that merit reconnect+retry."""
    if not isinstance(response, dict):
        return False
    if response.get("ok"):
        return False

    err = gateway_response_error(response).casefold()
    if not err:
        return False

    retryable_snippets = (
        "not connected",
        "transport unavailable",
        "gateway timeout",
        "timed out",
        "connection reset",
        "connection refused",
        "broken pipe",
        "socket",
        "closed",
        "handshake",
        "network is unreachable",
        "temporarily unavailable",
        "refused",
        "reset by peer",
    )
    return any(snippet in err for snippet in retryable_snippets)


def send_chat_with_recovery(
    server_obj,
    session_key: str,
    message: str,
    *,
    timeout_ms: int = 180000,
    idempotency_key: str | None = None,
    connection_attempts: int = 2,
    connection_wait_s: float = 0.8,
    connection_retry_delay_s: float = 1.0,
    send_attempts: int = 3,
    send_retry_delays_s: tuple[float, ...] = (0.75, 1.5),
) -> dict:
    """Send chat with bounded reconnect/rebuild retries before failing."""
    preflight_error = gateway_chat_unavailable_reason()
    if preflight_error:
        return {"ok": False, "error": preflight_error}

    stable_idempotency_key = str(idempotency_key or "").strip() or str(uuid.uuid4())
    attempts = max(1, int(send_attempts or 1))
    retry_delays = tuple(max(0.0, float(delay)) for delay in (send_retry_delays_s or ()))
    last_response: dict = {"ok": False, "error": "Gateway not connected"}

    for attempt in range(1, attempts + 1):
        client = ensure_server_gateway_client(
            server_obj,
            max_attempts=connection_attempts,
            wait_per_attempt_s=connection_wait_s,
            retry_delay_s=connection_retry_delay_s,
        )
        if client is None:
            last_response = {"ok": False, "error": "Gateway not connected"}
        else:
            response = client.send_chat(
                session_key,
                message,
                timeout_ms=timeout_ms,
                idempotency_key=stable_idempotency_key,
            )
            last_response = response if isinstance(response, dict) else {
                "ok": False,
                "error": str(response),
            }
            if last_response.get("ok"):
                return last_response
            if not is_retryable_gateway_response(last_response):
                return last_response

        if attempt >= attempts:
            break

        delay_s = retry_delays[min(attempt - 1, len(retry_delays) - 1)] if retry_delays else 0.0
        print(
            f"Gateway send attempt {attempt}/{attempts} failed "
            f"({gateway_response_error(last_response) or 'unknown error'}); rebuilding client"
        )
        rebuild_server_gateway_client(server_obj)
        if delay_s > 0:
            time.sleep(delay_s)

    return last_response

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

def load_session_messages_from_paths(jsonl_paths: list[Path]) -> list[dict]:
    """Read and merge all JSONL aliases for one logical session."""
    merged_entries: list[dict] = []
    for jsonl_path in jsonl_paths:
        merged_entries.extend(read_jsonl_full(jsonl_path))
    return parse_messages(_merge_session_entries(merged_entries))

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

def _short_text(value, limit: int = 140) -> str:
    """Return compact, single-line text for event previews."""
    if value is None:
        s = "null"
    elif isinstance(value, (str, int, float, bool)):
        s = str(value)
    elif isinstance(value, dict):
        keys = [str(k) for k in value.keys()]
        if keys:
            shown = ", ".join(keys[:4])
            s = "{" + shown + (", ..." if len(keys) > 4 else "") + "}"
        else:
            s = "{}"
    elif isinstance(value, list):
        s = f"[{len(value)} items]"
    else:
        s = str(value)

    s = " ".join(s.split())
    return s[:limit] + ("…" if len(s) > limit else "")

def _message_content_preview(content) -> str:
    """Extract a concise message preview from OpenClaw message content."""
    if isinstance(content, str):
        return _short_text(content, 160)
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = str(block.get("type", ""))
            if btype == "text":
                txt = str(block.get("text", "")).strip()
                if txt:
                    parts.append(txt)
            elif btype == "thinking":
                txt = str(block.get("thinking", "")).strip()
                if txt:
                    parts.append(f"[thinking] {txt}")
            elif btype == "toolCall":
                name = str(block.get("name", "?")).strip() or "?"
                parts.append(f"[toolCall] {name}")
            elif btype == "toolResult":
                name = str(block.get("toolName", "?")).strip() or "?"
                parts.append(f"[toolResult] {name}")
            if len(parts) >= 2:
                break
        return _short_text(" | ".join(parts), 160)
    return _short_text(content, 160)

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

        if etype == "session":
            version = entry.get("version")
            cwd = str(entry.get("cwd", "")).strip()
            cwd_label = Path(cwd).name if cwd else ""
            parts = ["session started"]
            if version not in (None, ""):
                parts.append(f"v{version}")
            if cwd_label:
                parts.append(f"cwd:{cwd_label}")
            msgs.append({
                "id":         entry.get("id", ""),
                "role":       "event",
                "event_type": "session",
                "text":       " · ".join(parts),
                "ts_iso":     entry.get("timestamp", ""),
                "ts_fmt":     fmt_iso(entry.get("timestamp", "")),
                "raw_json":   json.dumps(entry, ensure_ascii=False),
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
                continue

            if custom_type == "model-snapshot":
                provider = str((data or {}).get("provider", "")).strip()
                model_id = str((data or {}).get("modelId", "")).strip() or str((data or {}).get("model", "")).strip()
                model_api = str((data or {}).get("modelApi", "")).strip()
                model_name = model_id or "?"
                model_with_provider = f"{provider}/{model_name}" if provider else model_name
                suffix = f" ({model_api})" if model_api else ""
                msgs.append({
                    "id":         entry.get("id", ""),
                    "role":       "event",
                    "event_type": "model",
                    "text":       f"model snapshot → {model_with_provider}{suffix}",
                    "ts_iso":     entry.get("timestamp", ""),
                    "ts_fmt":     fmt_iso(entry.get("timestamp", "")),
                    "raw_json":   json.dumps(entry, ensure_ascii=False),
                })
                continue

            # Fallback for all other custom records.
            preview = _short_text(data, 140)
            custom_label = custom_type or "unknown"
            text = f"custom:{custom_label}" + (f" · {preview}" if preview else "")
            msgs.append({
                "id":         entry.get("id", ""),
                "role":       "event",
                "event_type": "custom",
                "text":       text,
                "ts_iso":     entry.get("timestamp", ""),
                "ts_fmt":     fmt_iso(entry.get("timestamp", "")),
                "raw_json":   json.dumps(entry, ensure_ascii=False),
            })
            continue

        if etype != "message":
            preview = _short_text({
                "type": etype,
                "id": entry.get("id", ""),
                "parentId": entry.get("parentId", ""),
            }, 120)
            msgs.append({
                "id":         entry.get("id", ""),
                "role":       "event",
                "event_type": "meta",
                "text":       f"entry:{etype or 'unknown'} · {preview}",
                "ts_iso":     entry.get("timestamp", ""),
                "ts_fmt":     fmt_iso(entry.get("timestamp", "")),
                "raw_json":   json.dumps(entry, ensure_ascii=False),
            })
            continue
        msg = entry.get("message", {})
        role = msg.get("role", "")
        if role not in ("user", "assistant", "toolResult"):
            msgs.append({
                "id":         entry.get("id", ""),
                "role":       "event",
                "event_type": "meta",
                "text":       f"message role:{role or 'unknown'} · {_message_content_preview(msg.get('content', ''))}",
                "ts_iso":     entry.get("timestamp", ""),
                "ts_fmt":     fmt_iso(entry.get("timestamp", "")),
                "raw_json":   json.dumps(entry, ensure_ascii=False),
            })
            continue

        raw_json = json.dumps(entry, ensure_ascii=False)
        provenance = msg.get("provenance", {}) if isinstance(msg.get("provenance", {}), dict) else {}
        provenance_kind = str(provenance.get("kind", "") or "").strip()
        provenance_source_session_key = str(provenance.get("sourceSessionKey", "") or "").strip()
        provenance_source_channel = str(provenance.get("sourceChannel", "") or "").strip()
        provenance_source_tool = str(provenance.get("sourceTool", "") or "").strip()

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
                "provenance_kind": provenance_kind,
                "provenance_source_session_key": provenance_source_session_key,
                "provenance_source_channel": provenance_source_channel,
                "provenance_source_tool": provenance_source_tool,
                "error_message": "",
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
        raw_error_message = msg.get("errorMessage", msg.get("error_message", ""))
        if isinstance(raw_error_message, str):
            error_message = raw_error_message.strip()
        elif raw_error_message is None:
            error_message = ""
        else:
            error_message = str(raw_error_message)
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
            "provenance_kind": provenance_kind,
            "provenance_source_session_key": provenance_source_session_key,
            "provenance_source_channel": provenance_source_channel,
            "provenance_source_tool": provenance_source_tool,
            "blocks":       blocks,
            "ts_iso":       entry.get("timestamp", ""),
            "ts_fmt":       fmt_iso(entry.get("timestamp", "")),
            "model":        friendly_model(msg.get("model", "")),
            "stop_reason":  msg.get("stopReason", msg.get("stop_reason", "")),
            "error_message": error_message,
            "input_tok":    usage.get("input", 0),
            "output_tok":   usage.get("output", 0),
            "cost":         round(cost, 6),
            "raw_json":     raw_json,
            "tool_name":    "",
            "is_error":     False,
            "total_chars":  0,
        })
    return _dedupe_retry_user_messages(msgs)

def _dedupe_paths(paths: list[Path | None]) -> list[Path]:
    """Return unique paths while preserving order."""
    out: list[Path] = []
    seen: set[str] = set()
    for p in paths:
        if not p:
            continue
        try:
            key = str(p.resolve())
        except Exception:
            key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out

def _resolve_declared_session_file(agent_dir: Path, declared: str) -> Path | None:
    """Resolve sessionFile value from sessions.json to an absolute path."""
    raw = str(declared or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if path.is_absolute():
        return path
    return agent_dir / "sessions" / path

def _entry_merge_key(entry: dict) -> str:
    """Build stable merge key for deduplicating entries across alias files."""
    entry_id = str(entry.get("id", "") or "").strip()
    if entry_id:
        return f"id:{entry_id}"
    # Fallback key when id is absent in malformed/legacy rows.
    return json.dumps(
        {
            "type": entry.get("type", ""),
            "timestamp": entry.get("timestamp", ""),
            "parentId": entry.get("parentId", ""),
            "message": entry.get("message", {}),
        },
        ensure_ascii=False,
        sort_keys=True,
    )

def _merge_session_entries(entries: list[dict]) -> list[dict]:
    """Merge and dedupe entries from multiple JSONL aliases by id/timestamp."""
    if not entries:
        return []

    def sort_key(item: tuple[int, dict]) -> tuple[int, int, int]:
        idx, entry = item
        ts = _parse_iso_ms(str(entry.get("timestamp", "") or ""))
        if ts:
            return (0, ts, idx)
        return (1, idx, idx)

    merged: list[dict] = []
    merged_index: dict[str, int] = {}

    for _, entry in sorted(enumerate(entries), key=sort_key):
        key = _entry_merge_key(entry)
        existing_idx = merged_index.get(key)
        if existing_idx is None:
            merged_index[key] = len(merged)
            merged.append(entry)
            continue

        # Keep the richer entry in case one alias contains additional fields.
        existing = merged[existing_idx]
        try:
            existing_size = len(json.dumps(existing, ensure_ascii=False, sort_keys=True))
            incoming_size = len(json.dumps(entry, ensure_ascii=False, sort_keys=True))
        except Exception:
            existing_size = 0
            incoming_size = 0
        if incoming_size > existing_size:
            merged[existing_idx] = entry

    return merged

def resolve_session_jsonl_paths(session_id: str) -> list[Path]:
    """Resolve all JSONL files that can belong to one logical session.

    A session can temporarily span aliases when sessions.json drifts
    (sessionId points to new file while sessionFile still points to old file).
    """
    sid = str(session_id or "").strip()
    if not sid:
        return []

    paths: list[Path | None] = []

    for agent_dir in sorted(AGENTS_DIR.iterdir()):
        if not agent_dir.is_dir():
            continue

        sess_dir = agent_dir / "sessions"
        direct = sess_dir / f"{sid}.jsonl"
        if direct.exists():
            paths.append(direct)

        store = load_sessions_store(agent_dir)
        for _, val in store.items():
            if not isinstance(val, dict):
                continue

            store_sid = str(val.get("sessionId", "") or "").strip()
            declared = _resolve_declared_session_file(agent_dir, str(val.get("sessionFile", "") or ""))
            declared_stem = declared.stem if declared else ""

            if store_sid != sid and declared_stem != sid:
                continue

            sid_path = sess_dir / f"{store_sid}.jsonl" if store_sid else None
            if sid_path and sid_path.exists():
                paths.append(sid_path)
            if declared and declared.exists():
                paths.append(declared)

    return _dedupe_paths(paths)

def resolve_session_jsonl_path(session_id: str) -> Path | None:
    """Resolve a primary JSONL file for a logical session."""
    paths = resolve_session_jsonl_paths(session_id)
    return paths[0] if paths else None

def resolve_session_jsonl_paths_for_entry(session_id: str, entry_id: str = "") -> list[Path]:
    """Resolve candidate JSONL paths, prioritizing files containing entry_id."""
    paths = resolve_session_jsonl_paths(session_id)
    eid = str(entry_id or "").strip()
    if not eid or not paths:
        return paths

    containing: list[Path] = []
    others: list[Path] = []

    for path in paths:
        found = False
        for entry in tail_jsonl(path, 400):
            if str(entry.get("id", "") or "") == eid:
                found = True
                break
        if not found:
            for entry in read_jsonl_full(path):
                if str(entry.get("id", "") or "") == eid:
                    found = True
                    break
        if found:
            containing.append(path)
        else:
            others.append(path)

    if containing:
        return _dedupe_paths(containing + others)

    # Slow fallback: entry may only exist in a stale alias not linked anymore.
    for agent_dir in sorted(AGENTS_DIR.iterdir()):
        if not agent_dir.is_dir():
            continue
        for candidate in sorted((agent_dir / "sessions").glob("*.jsonl")):
            if candidate in paths:
                continue
            try:
                for entry in tail_jsonl(candidate, 300):
                    if str(entry.get("id", "") or "") == eid:
                        paths.append(candidate)
                        raise StopIteration
            except StopIteration:
                continue

    return _dedupe_paths(paths)

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

            # Prefer session identity from sessionId; sessionFile is only a path hint.
            session_file_declared = str(val.get("sessionFile", "") or "").strip()
            session_id = str(val.get("sessionId", "") or "").strip()
            if not session_id and session_file_declared:
                session_id = Path(session_file_declared).stem

            jsonl_paths = resolve_session_jsonl_paths(session_id)
            declared_path = _resolve_declared_session_file(agent_dir, session_file_declared)
            if declared_path and declared_path.exists():
                jsonl_paths = _dedupe_paths(jsonl_paths + [declared_path])

            has_file = bool(jsonl_paths)

            # Count messages & get stats
            msg_count   = 0
            last_model  = ""
            total_input = 0
            total_output= 0
            last_ts_iso = ""
            last_stop_reason = ""

            if has_file:
                msgs = load_session_messages_from_paths(jsonl_paths)
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
            explicit_label = str(val.get("label") or "").strip()
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
                delivery_ctx = val.get("deliveryContext") or {}
                group_subject = ""
                if isinstance(delivery_ctx, dict):
                    group_subject = str(
                        delivery_ctx.get("groupSubject") or delivery_ctx.get("group_subject") or ""
                    ).strip()

                origin_group_subject = str(
                    origin.get("groupSubject") or origin.get("group_subject") or ""
                ).strip()

                origin_group_label = ""
                if origin_label and not _looks_like_session_key(origin_label) and not _is_generic_origin_label(origin_label):
                    origin_group_label = _extract_group_name_from_conversation_label(origin_label)

                history_group_label = ""
                if not (group_subject or origin_group_subject or origin_group_label) and has_file:
                    history_group_label = infer_telegram_group_label_from_paths(jsonl_paths)

                label = group_subject or origin_group_subject or origin_group_label or history_group_label or key
            elif stype == "telegram":
                origin_dm_label = ""
                if origin_label and not _looks_like_session_key(origin_label) and not _is_generic_origin_label(origin_label):
                    origin_dm_label = origin_label
                label = origin_dm_label or (f"DM {parts[-1]}" if parts else key)
            elif stype == "cron":
                label = cron_name_map.get(cron_id, "") or origin_label or key
            elif stype == "subagent":
                label = explicit_label or origin_label or (parts[-1] if parts else key)
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
    paths = resolve_session_jsonl_paths(session_id)
    if not paths:
        return []

    merged_entries: list[dict] = []
    for jsonl_path in paths:
        merged_entries.extend(read_jsonl_full(jsonl_path))
    return parse_messages(_merge_session_entries(merged_entries))

def find_session_jsonl_paths(session_id: str) -> list[Path]:
    """Resolve all JSONL aliases for a session_id."""
    return resolve_session_jsonl_paths(session_id)

def find_session_jsonl_path(session_id: str) -> Path | None:
    """Resolve a session JSONL path by session_id across all agents."""
    return resolve_session_jsonl_path(session_id)

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

def session_paths_state(paths: list[Path]) -> dict | None:
    """Aggregate file state for multiple JSONL alias paths."""
    if not paths:
        return None

    states = [session_file_state(path) for path in paths]
    states = [state for state in states if state]
    if not states:
        return None

    return {
        "last_mtime_ns": max(int(s["last_mtime_ns"]) for s in states),
        "last_size": sum(int(s["last_size"]) for s in states),
        "path_count": len(states),
    }

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
        
    def connect(self, wait_s: float = 5.0):
        """Start WebSocket connection in background thread and wait briefly."""
        if self.connected:
            return True

        if not (self.thread and self.thread.is_alive()):
            self._stop_event.clear()
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

        wait_s = max(0.2, float(wait_s or 0.0))
        poll_steps = max(1, int(wait_s / 0.1))
        # Wait for websocket hello-ok handshake.
        for _ in range(poll_steps):
            if self.connected:
                return True
            time.sleep(0.1)
        return bool(self.connected)

    def ensure_connected(
        self,
        max_attempts: int = 3,
        wait_per_attempt_s: float = 1.5,
        retry_delay_s: float = 2.0,
    ) -> bool:
        """Try to establish gateway connection with bounded retries."""
        attempts = max(1, int(max_attempts or 1))
        wait_per_attempt_s = max(0.2, float(wait_per_attempt_s or 0.0))
        retry_delay_s = max(0.0, float(retry_delay_s or 0.0))

        for attempt in range(1, attempts + 1):
            if self.connected:
                return True

            if self.connect(wait_s=wait_per_attempt_s):
                return True

            if attempt < attempts and retry_delay_s > 0:
                print(
                    f"Gateway reconnect attempt {attempt}/{attempts} failed; "
                    f"retrying in {retry_delay_s:.1f}s"
                )
                time.sleep(retry_delay_s)

        return bool(self.connected)
    
    def disconnect(self):
        """Stop WebSocket connection."""
        self._stop_event.set()
        if self.ws:
            self.ws.close()
        if self.thread:
            self.thread.join(timeout=2)
        self.connected = False
        self.nonce = None
    
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
        self.nonce = None
        print(f"Gateway WebSocket error: {error}")
    
    def _on_close(self, ws, close_status_code, close_msg):
        """WebSocket closed."""
        self.connected = False
        self.nonce = None
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
                    "version": "1.3.0",
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
        if not self.connected and not self.ensure_connected(
            max_attempts=2,
            wait_per_attempt_s=1.2,
            retry_delay_s=1.5,
        ):
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
                self.connected = False
                return {"ok": False, "error": "Gateway timeout"}
        except Exception as e:
            self.connected = False
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
        session_paths = find_session_jsonl_paths(session_id)
        if not session_paths:
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
        prev_state = session_paths_state(session_paths)
        ready_payload = {
            "session_id": session_id,
            "hint_new_messages": False,
            "seq": seq,
            "last_mtime_ns": prev_state["last_mtime_ns"] if prev_state else None,
            "last_size": prev_state["last_size"] if prev_state else None,
            "path_count": prev_state["path_count"] if prev_state else 0,
        }

        try:
            self._sse_write("ready", ready_payload, retry_ms=1000)

            while True:
                now = time.monotonic()
                refreshed_paths = find_session_jsonl_paths(session_id)
                if refreshed_paths:
                    session_paths = refreshed_paths
                current_state = session_paths_state(session_paths)

                # Send updates when alias set or size changes; hint when bytes grew.
                grew = False
                changed = False
                if prev_state and current_state:
                    grew = current_state["last_size"] > prev_state["last_size"]
                    changed = (
                        current_state["last_size"] != prev_state["last_size"]
                        or current_state["last_mtime_ns"] != prev_state["last_mtime_ns"]
                        or current_state.get("path_count", 0) != prev_state.get("path_count", 0)
                    )
                elif current_state and not prev_state:
                    grew = True  # File appeared
                    changed = True
                elif prev_state and not current_state:
                    changed = True
                
                if changed:
                    seq += 1
                    changed_payload = {
                        "session_id": session_id,
                        "hint_new_messages": grew,
                        "seq": seq,
                        "last_mtime_ns": current_state["last_mtime_ns"] if current_state else None,
                        "last_size": current_state["last_size"] if current_state else None,
                        "path_count": current_state["path_count"] if current_state else 0,
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
                full = self._load_entry_full(session_id, entry_id)
                if not full.get("ok"):
                    self.send_json({"error": full.get("error", "entry not found")}, 404)
                else:
                    self.send_json({"text": full.get("text", "")})
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

                request_idempotency_key = idempotency_key or str(uuid.uuid4())

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
                
                response = send_chat_with_recovery(
                    self.server,
                    session_key,
                    message,
                    idempotency_key=request_idempotency_key,
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
                    err_msg = gateway_response_error(response) or "Unknown error"
                    err_status = 503 if is_retryable_gateway_response(response) else 500
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
        session_paths = resolve_session_jsonl_paths_for_entry(session_id, entry_id)
        if not session_paths:
            return {
                "ok": False,
                "error": f"session not found for id '{session_id}'",
            }

        for jsonl_path in session_paths:
            for entry in read_jsonl_full(jsonl_path):
                if str(entry.get("id", "") or "") != str(entry_id):
                    continue
                msg = entry.get("message", {})
                content = msg.get("content", "")
                if isinstance(content, str):
                    return {"ok": True, "text": content}
                if isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict):
                            t = block.get("text") or block.get("thinking", "")
                            if t:
                                parts.append(t)
                    return {"ok": True, "text": "\n".join(parts)}
                return {"ok": True, "text": str(content)}

        searched = ", ".join(str(p.name) for p in session_paths[:4])
        if len(session_paths) > 4:
            searched += ", ..."
        return {
            "ok": False,
            "error": f"entry '{entry_id}' not found (searched {len(session_paths)} file(s): {searched})",
        }

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
    server.gateway_client_lock = threading.Lock()
    
    # Initialize Gateway WebSocket client
    if websocket is None:
        print("websocket-client not installed: chat features disabled")
        server.gateway_client = None
    else:
        gateway_config = load_gateway_config()
        gateway_client = create_gateway_client_from_runtime()
        server.gateway_client = gateway_client
        if gateway_client is None:
            print(f"Gateway not available: {gateway_config.get('error', 'unknown')} (chat features disabled)")
        else:
            print(f"Connecting to OpenClaw Gateway at {gateway_config['bind']}:{gateway_config['port']}...")
            if gateway_client.connect():
                print("Gateway connection established")
            else:
                print("Gateway connection failed at startup; will retry on demand")
    
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
