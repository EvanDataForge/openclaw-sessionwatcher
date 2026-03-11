import unittest
from unittest import mock
from types import SimpleNamespace

import server


class GatewayClientReconnectTests(unittest.TestCase):
    def test_ensure_connected_retries_until_success(self):
        client = server.GatewayClient(host="127.0.0.1", port=18789, token="x")

        attempts = {"count": 0}

        def fake_connect(*, wait_s=5.0):
            attempts["count"] += 1
            if attempts["count"] >= 2:
                client.connected = True
            return client.connected

        client.connect = fake_connect  # type: ignore[method-assign]

        with mock.patch("server.time.sleep") as sleep_mock:
            ok = client.ensure_connected(max_attempts=3, wait_per_attempt_s=0.2, retry_delay_s=0.7)

        self.assertTrue(ok)
        self.assertEqual(attempts["count"], 2)
        sleep_mock.assert_called_once_with(0.7)

    def test_ensure_connected_stops_after_max_attempts(self):
        client = server.GatewayClient(host="127.0.0.1", port=18789, token="x")

        attempts = {"count": 0}

        def fake_connect(*, wait_s=5.0):
            attempts["count"] += 1
            client.connected = False
            return False

        client.connect = fake_connect  # type: ignore[method-assign]

        with mock.patch("server.time.sleep") as sleep_mock:
            ok = client.ensure_connected(max_attempts=3, wait_per_attempt_s=0.2, retry_delay_s=0.4)

        self.assertFalse(ok)
        self.assertEqual(attempts["count"], 3)
        # Between 3 attempts there are exactly 2 retry delays.
        self.assertEqual(sleep_mock.call_count, 2)


class ServerGatewayClientRecoveryTests(unittest.TestCase):
    def test_server_gateway_client_is_created_lazily(self):
        created = []

        class FakeClient:
            def __init__(self):
                self.connected = False

            def ensure_connected(self, **kwargs):
                self.connected = True
                return True

            def disconnect(self):
                return None

        def fake_create():
            client = FakeClient()
            created.append(client)
            return client

        server_obj = SimpleNamespace(gateway_client=None)

        with mock.patch("server.create_gateway_client_from_runtime", side_effect=fake_create):
            client = server.ensure_server_gateway_client(server_obj, max_attempts=1, wait_per_attempt_s=0.2, retry_delay_s=0.0)

        self.assertIs(client, created[0])
        self.assertIs(server_obj.gateway_client, created[0])
        self.assertTrue(created[0].connected)

    def test_server_gateway_client_recreates_stale_client(self):
        class StaleClient:
            def __init__(self):
                self.connected = False
                self.disconnected = False

            def ensure_connected(self, **kwargs):
                return False

            def disconnect(self):
                self.disconnected = True

        class FreshClient:
            def __init__(self):
                self.connected = False

            def ensure_connected(self, **kwargs):
                self.connected = True
                return True

            def disconnect(self):
                return None

        stale = StaleClient()
        fresh = FreshClient()
        server_obj = SimpleNamespace(gateway_client=stale)

        with mock.patch("server.create_gateway_client_from_runtime", return_value=fresh) as create_mock:
            client = server.ensure_server_gateway_client(server_obj, max_attempts=1, wait_per_attempt_s=0.2, retry_delay_s=0.0)

        self.assertIs(client, fresh)
        self.assertTrue(stale.disconnected)
        self.assertIs(server_obj.gateway_client, fresh)
        self.assertTrue(fresh.connected)
        create_mock.assert_called_once()


class GatewaySendRecoveryTests(unittest.TestCase):
    def test_send_chat_with_recovery_reports_missing_websocket_dependency(self):
        server_obj = SimpleNamespace(gateway_client=None)

        with mock.patch("server.websocket", None):
            with mock.patch("server.create_gateway_client_from_runtime") as create_mock:
                response = server.send_chat_with_recovery(
                    server_obj,
                    "agent:main:sessions",
                    "Hello",
                    idempotency_key="idem-0",
                )

        self.assertFalse(response.get("ok"))
        self.assertIn("websocket-client", str(response.get("error", "")))
        create_mock.assert_not_called()

    def test_send_chat_with_recovery_rebuilds_client_after_transport_error(self):
        class StaleClient:
            def __init__(self):
                self.connected = True
                self.disconnected = False
                self.sent_idempotency_keys = []

            def ensure_connected(self, **kwargs):
                return True

            def send_chat(self, session_key, message, timeout_ms=180000, idempotency_key=None):
                self.sent_idempotency_keys.append(idempotency_key)
                return {"ok": False, "error": "Connection reset by peer"}

            def disconnect(self):
                self.disconnected = True

        class FreshClient:
            def __init__(self):
                self.connected = False
                self.sent_idempotency_keys = []

            def ensure_connected(self, **kwargs):
                self.connected = True
                return True

            def send_chat(self, session_key, message, timeout_ms=180000, idempotency_key=None):
                self.sent_idempotency_keys.append(idempotency_key)
                return {"ok": True, "payload": {"runId": "run-1", "status": "accepted"}}

            def disconnect(self):
                return None

        stale = StaleClient()
        fresh = FreshClient()
        server_obj = SimpleNamespace(gateway_client=stale)

        with mock.patch("server.create_gateway_client_from_runtime", return_value=fresh) as create_mock:
            with mock.patch("server.time.sleep") as sleep_mock:
                response = server.send_chat_with_recovery(
                    server_obj,
                    "agent:main:sessions",
                    "Hello",
                    idempotency_key="idem-1",
                    send_attempts=2,
                    send_retry_delays_s=(0.25,),
                )

        self.assertTrue(response.get("ok"))
        self.assertTrue(stale.disconnected)
        self.assertIs(server_obj.gateway_client, fresh)
        self.assertEqual(stale.sent_idempotency_keys, ["idem-1"])
        self.assertEqual(fresh.sent_idempotency_keys, ["idem-1"])
        sleep_mock.assert_called_once_with(0.25)
        create_mock.assert_called_once()

    def test_send_chat_with_recovery_does_not_retry_non_transport_errors(self):
        class Client:
            def __init__(self):
                self.connected = False
                self.send_calls = 0

            def ensure_connected(self, **kwargs):
                self.connected = True
                return True

            def send_chat(self, session_key, message, timeout_ms=180000, idempotency_key=None):
                self.send_calls += 1
                return {"ok": False, "error": "invalid session key"}

            def disconnect(self):
                return None

        client = Client()
        server_obj = SimpleNamespace(gateway_client=client)

        with mock.patch("server.create_gateway_client_from_runtime") as create_mock:
            with mock.patch("server.time.sleep") as sleep_mock:
                response = server.send_chat_with_recovery(
                    server_obj,
                    "agent:main:sessions",
                    "Hello",
                    idempotency_key="idem-2",
                    send_attempts=3,
                    send_retry_delays_s=(0.25, 0.5),
                )

        self.assertFalse(response.get("ok"))
        self.assertEqual(response.get("error"), "invalid session key")
        self.assertEqual(client.send_calls, 1)
        sleep_mock.assert_not_called()
        create_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
