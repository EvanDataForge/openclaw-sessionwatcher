import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SERVER_PY = REPO / "server.py"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def build_fixture_openclaw(root: Path):
    sessions_dir = root / "agents" / "main" / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)

    now_ms = int(time.time() * 1000)
    sessions = {
        "agent:main:sessions": {
            "sessionId": "sess-1",
            "updatedAt": now_ms,
            "lastChannel": "webchat",
            "model": "anthropic/claude-sonnet-4",
            "contextPct": 12,
            "origin": {"provider": "webchat", "label": "Direct"},
        }
    }
    (sessions_dir / "sessions.json").write_text(json.dumps(sessions), encoding="utf-8")

    entries = [
        {
            "id": "entry-1",
            "timestamp": "2026-03-07T12:00:00Z",
            "type": "message",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "Hello SessionWatcher"}],
                "usage": {"input": 0, "output": 0, "cost": {"total": 0}},
            },
        },
        {
            "id": "entry-2",
            "timestamp": "2026-03-07T12:00:05Z",
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Hello back"}],
                "model": "anthropic/claude-sonnet-4",
                "stopReason": "stop",
                "usage": {"input": 11, "output": 7, "cost": {"total": 1234}},
            },
        },
    ]
    with (sessions_dir / "sess-1.jsonl").open("w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry) + "\n")


class ServerProcessMixin:
    proc = None
    port = None
    tempdir = None

    @classmethod
    def start_server(cls, *, bind="127.0.0.1", token=None):
        cls.tempdir = tempfile.TemporaryDirectory()
        build_fixture_openclaw(Path(cls.tempdir.name))
        cls.port = free_port()

        env = os.environ.copy()
        env["OPENCLAW_DIR"] = cls.tempdir.name
        env["SESSIONWATCHER_PORT"] = str(cls.port)
        env["SESSIONWATCHER_BIND"] = bind
        if token is None:
            env.pop("SESSIONWATCHER_ACCESS_TOKEN", None)
        else:
            env["SESSIONWATCHER_ACCESS_TOKEN"] = token

        cls.proc = subprocess.Popen(
            [sys.executable, str(SERVER_PY)],
            cwd=str(REPO),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )

        deadline = time.time() + 8
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                output = cls.proc.stdout.read() if cls.proc.stdout else ""
                raise RuntimeError(f"Server exited early: {output}")
            try:
                conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=1)
                conn.request("GET", "/api/status")
                conn.getresponse().read()
                conn.close()
                return
            except Exception:
                time.sleep(0.1)

        raise RuntimeError("Server did not start in time")

    @classmethod
    def stop_server(cls):
        if cls.proc:
            cls.proc.terminate()
            try:
                cls.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.proc.kill()
                cls.proc.wait(timeout=5)
            if cls.proc.stdout:
                cls.proc.stdout.close()
            cls.proc = None
        if cls.tempdir:
            cls.tempdir.cleanup()
            cls.tempdir = None

    def request(self, path, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        headers_map = dict(resp.getheaders())
        status = resp.status
        conn.close()
        return status, headers_map, body

    def open_stream(self, path, headers=None, timeout=5):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        return conn, resp

    def read_sse_event(self, resp, timeout=3):
        deadline = time.time() + timeout
        lines = []

        while time.time() < deadline:
            try:
                raw = resp.fp.readline()
            except socket.timeout:
                continue

            if not raw:
                raise AssertionError("SSE stream closed before event was received")

            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                if lines:
                    break
                continue
            if line.startswith(":"):
                continue
            lines.append(line)

        if not lines:
            raise AssertionError("Timed out waiting for SSE event")

        event_name = "message"
        data_lines = []
        for line in lines:
            if line.startswith("event:"):
                event_name = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_lines.append(line.split(":", 1)[1].lstrip())

        payload = {}
        if data_lines:
            payload = json.loads("\n".join(data_lines))

        return {"event": event_name, "data": payload}


class TestLoopbackWithoutToken(ServerProcessMixin, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.start_server(bind="127.0.0.1", token=None)

    @classmethod
    def tearDownClass(cls):
        cls.stop_server()

    def test_loopback_status_is_open_by_default(self):
        status, headers, body = self.request("/api/status")
        self.assertEqual(status, 200)
        self.assertNotEqual(headers.get("Access-Control-Allow-Origin"), "*")
        data = json.loads(body)
        self.assertEqual(data["status"], "ok")

    def test_cron_session_uses_configured_job_name(self):
        root = Path(self.tempdir.name)
        sessions_file = root / "agents" / "main" / "sessions" / "sessions.json"
        sessions = json.loads(sessions_file.read_text(encoding="utf-8"))

        cron_id = "a037878e-74ff-47ca-b8b0-ef7162577a5c"
        cron_key = f"agent:main:cron:{cron_id}"
        sessions[cron_key] = {
            "sessionId": "sess-cron-1",
            "updatedAt": int(time.time() * 1000),
            "lastChannel": "cron",
            "model": "openai/gpt-5.3-codex",
            "contextPct": 0,
            "origin": {},
        }
        sessions_file.write_text(json.dumps(sessions), encoding="utf-8")

        cron_entries = [
            {
                "id": "cron-entry-1",
                "timestamp": "2026-03-07T12:10:00Z",
                "type": "message",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Start cron"}],
                    "usage": {"input": 0, "output": 0, "cost": {"total": 0}},
                },
            },
            {
                "id": "cron-entry-2",
                "timestamp": "2026-03-07T12:10:05Z",
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Cron done"}],
                    "model": "openai/gpt-5.3-codex",
                    "stopReason": "stop",
                    "usage": {"input": 2, "output": 4, "cost": {"total": 0}},
                },
            },
        ]
        cron_jsonl = root / "agents" / "main" / "sessions" / "sess-cron-1.jsonl"
        with cron_jsonl.open("w", encoding="utf-8") as handle:
            for entry in cron_entries:
                handle.write(json.dumps(entry) + "\n")

        cron_dir = root / "cron"
        cron_dir.mkdir(parents=True, exist_ok=True)
        cron_jobs = {
            "version": 1,
            "jobs": [
                {
                    "id": cron_id,
                    "name": "Morgencheck E-Mails 06:30",
                    "sessionKey": "agent:main:telegram:direct:6824095908",
                }
            ],
        }
        (cron_dir / "jobs.json").write_text(json.dumps(cron_jobs), encoding="utf-8")

        status, _headers, body = self.request("/api/sessions")
        self.assertEqual(status, 200)
        data = json.loads(body)

        cron_session = next((s for s in data["sessions"] if s["key"] == cron_key), None)
        self.assertIsNotNone(cron_session)
        self.assertEqual(cron_session["label"], "Morgencheck E-Mails 06:30")
        self.assertEqual(cron_session["label_sub"], cron_id)
        self.assertEqual(cron_session["session_key"], "agent:main:telegram:direct:6824095908")

    def test_delivery_mirror_is_not_used_as_session_model(self):
        root = Path(self.tempdir.name)
        sessions_dir = root / "agents" / "main" / "sessions"
        sessions_file = sessions_dir / "sessions.json"
        sessions = json.loads(sessions_file.read_text(encoding="utf-8"))

        sessions["agent:main:subagent:test"] = {
            "sessionId": "sess-subagent-1",
            "updatedAt": int(time.time() * 1000),
            "lastChannel": "subagent",
            "model": "delivery-mirror",
            "contextPct": 0,
            "origin": {"label": "Subagent test"},
        }
        sessions_file.write_text(json.dumps(sessions), encoding="utf-8")

        subagent_entries = [
            {
                "id": "subagent-entry-1",
                "timestamp": "2026-03-11T09:10:00Z",
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Mirrored delivery"}],
                    "model": "delivery-mirror",
                    "stopReason": "stop",
                    "usage": {"input": 1, "output": 1, "cost": {"total": 0}},
                },
            }
        ]
        with (sessions_dir / "sess-subagent-1.jsonl").open("w", encoding="utf-8") as handle:
            for entry in subagent_entries:
                handle.write(json.dumps(entry) + "\n")

        status, _headers, body = self.request("/api/sessions")
        self.assertEqual(status, 200)
        data = json.loads(body)

        subagent_session = next((s for s in data["sessions"] if s["session_id"] == "sess-subagent-1"), None)
        self.assertIsNotNone(subagent_session)
        self.assertEqual(subagent_session["model"], "—")

    def test_subagent_prefers_explicit_label_over_heartbeat_origin(self):
        root = Path(self.tempdir.name)
        sessions_dir = root / "agents" / "main" / "sessions"
        sessions_file = sessions_dir / "sessions.json"
        sessions = json.loads(sessions_file.read_text(encoding="utf-8"))

        sessions["agent:main:subagent:test-heartbeat"] = {
            "sessionId": "sess-subagent-heartbeat",
            "updatedAt": int(time.time() * 1000),
            "lastChannel": "webchat",
            "label": "bugfix-task-watchdog",
            "model": "gpt-5.4",
            "contextPct": 0,
            "origin": {
                "label": "heartbeat",
                "provider": "heartbeat",
                "from": "heartbeat",
                "to": "heartbeat",
            },
        }
        sessions_file.write_text(json.dumps(sessions), encoding="utf-8")

        subagent_entries = [
            {
                "id": "subagent-entry-1",
                "timestamp": "2026-03-11T10:52:34Z",
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Task complete"}],
                    "model": "gpt-5.4",
                    "stopReason": "stop",
                    "usage": {"input": 1, "output": 1, "cost": {"total": 0}},
                },
            }
        ]
        with (sessions_dir / "sess-subagent-heartbeat.jsonl").open("w", encoding="utf-8") as handle:
            for entry in subagent_entries:
                handle.write(json.dumps(entry) + "\n")

        status, _headers, body = self.request("/api/sessions")
        self.assertEqual(status, 200)
        data = json.loads(body)

        subagent_session = next((s for s in data["sessions"] if s["session_id"] == "sess-subagent-heartbeat"), None)
        self.assertIsNotNone(subagent_session)
        self.assertEqual(subagent_session["type"], "subagent")
        self.assertEqual(subagent_session["label"], "bugfix-task-watchdog")

    def test_telegram_group_uses_metadata_group_name_when_origin_is_stale(self):
        root = Path(self.tempdir.name)
        sessions_dir = root / "agents" / "main" / "sessions"
        sessions_file = sessions_dir / "sessions.json"
        sessions = json.loads(sessions_file.read_text(encoding="utf-8"))

        group_key = "agent:main:telegram:group:-1003714689801"
        group_session_id = "sess-tg-group-1"
        sessions[group_key] = {
            "sessionId": group_session_id,
            "updatedAt": int(time.time() * 1000),
            "lastChannel": "telegram",
            "model": "gpt-5.4",
            "contextPct": 0,
            "deliveryContext": {"channel": "telegram"},
            # Simulate stale origin metadata that no longer carries Telegram group labels.
            "origin": {
                "provider": "webchat",
                "surface": "webchat",
                "chatType": "direct",
            },
        }
        sessions_file.write_text(json.dumps(sessions), encoding="utf-8")

        group_entries = [
            {
                "id": "group-entry-1",
                "timestamp": "2026-03-10T20:27:15.691Z",
                "type": "message",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Conversation info (untrusted metadata):\n"
                                "```json\n"
                                "{\n"
                                '  "message_id": "90",\n'
                                '  "sender_id": "6824095908",\n'
                                '  "conversation_label": "Clawdine Sidechannel id:-1003714689801",\n'
                                '  "group_subject": "Clawdine Sidechannel",\n'
                                '  "is_group_chat": true\n'
                                "}\n"
                                "```\n\n"
                                "Test message"
                            ),
                        }
                    ],
                    "usage": {"input": 0, "output": 0, "cost": {"total": 0}},
                },
            }
        ]
        with (sessions_dir / f"{group_session_id}.jsonl").open("w", encoding="utf-8") as handle:
            for entry in group_entries:
                handle.write(json.dumps(entry) + "\n")

        status, _headers, body = self.request("/api/sessions")
        self.assertEqual(status, 200)
        data = json.loads(body)

        group_session = next((s for s in data["sessions"] if s["key"] == group_key), None)
        self.assertIsNotNone(group_session)
        self.assertEqual(group_session["type"], "group")
        self.assertEqual(group_session["label"], "Clawdine Sidechannel")

    def test_sessions_summary_msg_count_matches_detail_message_count(self):
        root = Path(self.tempdir.name)
        sessions_dir = root / "agents" / "main" / "sessions"

        entries = []
        for idx in range(76):
            entries.append(
                {
                    "id": f"user-{idx}",
                    "timestamp": f"2026-03-11T09:{idx // 60:02d}:{idx % 60:02d}Z",
                    "type": "message",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": f"Msg {idx}"}],
                        "usage": {"input": 0, "output": 0, "cost": {"total": 0}},
                    },
                }
            )

        for idx in range(136):
            entries.append(
                {
                    "id": f"evt-{idx}",
                    "timestamp": f"2026-03-11T10:{idx // 60:02d}:{idx % 60:02d}Z",
                    "type": "custom",
                    "customType": "test:event",
                    "data": {"index": idx},
                }
            )

        with (sessions_dir / "sess-1.jsonl").open("w", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry) + "\n")

        status_sessions, _headers_sessions, body_sessions = self.request("/api/sessions")
        self.assertEqual(status_sessions, 200)
        sessions_data = json.loads(body_sessions)
        listed = next((s for s in sessions_data["sessions"] if s["session_id"] == "sess-1"), None)
        self.assertIsNotNone(listed)

        status_messages, _headers_messages, body_messages = self.request("/api/sessions/sess-1/messages")
        self.assertEqual(status_messages, 200)
        messages_data = json.loads(body_messages)
        real_message_count = sum(1 for m in messages_data["messages"] if m["role"] != "event")

        self.assertEqual(listed["msg_count"], 76)
        self.assertEqual(listed["msg_count"], real_message_count)


class TestProtectedMode(ServerProcessMixin, unittest.TestCase):
    TOKEN = "test-sessionwatcher-token"

    @classmethod
    def setUpClass(cls):
        cls.start_server(bind="127.0.0.1", token=cls.TOKEN)

    @classmethod
    def tearDownClass(cls):
        cls.stop_server()

    def _bootstrap_cookie(self):
        status, headers, _body = self.request(f"/?access_token={self.TOKEN}")
        self.assertEqual(status, 302)
        cookie_header = headers.get("Set-Cookie", "")
        self.assertIn("sessionwatcher_access=" + self.TOKEN, cookie_header)
        return cookie_header.split(";", 1)[0]

    def test_root_requires_token_before_cookie_bootstrap(self):
        status, headers, body = self.request("/")
        self.assertEqual(status, 401)
        self.assertIn("OpenClaw Session Watcher access required", body)
        self.assertNotIn("Set-Cookie", headers)

    def test_api_requires_cookie_when_token_enabled(self):
        status, headers, body = self.request("/api/status")
        self.assertEqual(status, 401)
        self.assertNotEqual(headers.get("Access-Control-Allow-Origin"), "*")
        data = json.loads(body)
        self.assertTrue(data["auth_required"])
        self.assertIn("access_token", data["bootstrap"])

    def test_sse_requires_cookie_when_token_enabled(self):
        status, headers, body = self.request("/api/sessions/sess-1/events")
        self.assertEqual(status, 401)
        self.assertNotEqual(headers.get("Access-Control-Allow-Origin"), "*")
        data = json.loads(body)
        self.assertTrue(data["auth_required"])
        self.assertIn("access_token", data["bootstrap"])

    def test_invalid_bootstrap_token_is_rejected(self):
        status, _headers, body = self.request("/?access_token=wrong-token")
        self.assertEqual(status, 401)
        self.assertIn("Invalid OpenClaw Session Watcher access token", body)

    def test_bootstrap_sets_cookie_and_allows_followup_requests(self):
        status, headers, _body = self.request(f"/?access_token={self.TOKEN}")
        self.assertEqual(status, 302)
        self.assertEqual(headers.get("Location"), "/")
        cookie_header = headers.get("Set-Cookie", "")
        self.assertIn("sessionwatcher_access=" + self.TOKEN, cookie_header)
        cookie = cookie_header.split(";", 1)[0]

        status2, _headers2, body2 = self.request("/api/status", headers={"Cookie": cookie})
        self.assertEqual(status2, 200)
        self.assertEqual(json.loads(body2)["status"], "ok")

        status3, _headers3, body3 = self.request("/", headers={"Cookie": cookie})
        self.assertEqual(status3, 200)
        self.assertIn("<!DOCTYPE html>", body3[:100])

        status4, headers4, body4 = self.request("/api/sessions", headers={"Cookie": cookie})
        self.assertEqual(status4, 200)
        self.assertNotEqual(headers4.get("Access-Control-Allow-Origin"), "*")
        self.assertEqual(json.loads(body4)["count"], 1)

    def test_sse_stream_opens_with_cookie_and_emits_ready(self):
        cookie = self._bootstrap_cookie()
        conn, resp = self.open_stream(
            "/api/sessions/sess-1/events",
            headers={"Cookie": cookie},
            timeout=5,
        )
        try:
            self.assertEqual(resp.status, 200)
            content_type = resp.getheader("Content-Type", "")
            self.assertTrue(content_type.startswith("text/event-stream"))

            event = self.read_sse_event(resp, timeout=3)
            self.assertEqual(event["event"], "ready")
            self.assertEqual(event["data"].get("session_id"), "sess-1")
        finally:
            conn.close()

    def test_sse_emits_changed_after_jsonl_append(self):
        cookie = self._bootstrap_cookie()
        conn, resp = self.open_stream(
            "/api/sessions/sess-1/events",
            headers={"Cookie": cookie},
            timeout=5,
        )

        try:
            ready = self.read_sse_event(resp, timeout=3)
            self.assertEqual(ready["event"], "ready")

            jsonl = Path(self.tempdir.name) / "agents" / "main" / "sessions" / "sess-1.jsonl"
            new_entry = {
                "id": "entry-3",
                "timestamp": "2026-03-07T12:00:09Z",
                "type": "message",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "stream update"}],
                    "model": "anthropic/claude-sonnet-4",
                    "stopReason": "stop",
                    "usage": {"input": 3, "output": 2, "cost": {"total": 42}},
                },
            }
            with jsonl.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(new_entry) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

            changed = None
            deadline = time.time() + 3
            while time.time() < deadline:
                event = self.read_sse_event(resp, timeout=1.2)
                if event["event"] == "changed":
                    changed = event
                    break

            self.assertIsNotNone(changed)
            self.assertEqual(changed["data"].get("session_id"), "sess-1")
            self.assertTrue(changed["data"].get("hint_new_messages"))
        finally:
            conn.close()


class TestPublicBindPolicy(unittest.TestCase):
    def test_public_bind_without_token_fails_closed(self):
        tempdir = tempfile.TemporaryDirectory()
        try:
            build_fixture_openclaw(Path(tempdir.name))
            env = os.environ.copy()
            env["OPENCLAW_DIR"] = tempdir.name
            env["SESSIONWATCHER_PORT"] = str(free_port())
            env["SESSIONWATCHER_BIND"] = "0.0.0.0"
            env.pop("SESSIONWATCHER_ACCESS_TOKEN", None)

            proc = subprocess.Popen(
                [sys.executable, str(SERVER_PY)],
                cwd=str(REPO),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )
            output, _ = proc.communicate(timeout=5)
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("Refusing to bind OpenClaw Session Watcher to public host", output)
        finally:
            tempdir.cleanup()


if __name__ == "__main__":
    unittest.main()
