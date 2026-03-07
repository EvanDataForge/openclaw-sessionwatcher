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
        self.assertIn("SessionWatcher access required", body)
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
        self.assertIn("Invalid SessionWatcher access token", body)

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
            self.assertIn("Refusing to bind SessionWatcher to public host", output)
        finally:
            tempdir.cleanup()


if __name__ == "__main__":
    unittest.main()
