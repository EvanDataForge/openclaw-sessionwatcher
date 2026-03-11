import unittest

import server


class ParseMessagesErrorFallbackTests(unittest.TestCase):
    def test_user_inter_session_provenance_is_exposed(self):
        entries = [
            {
                "type": "message",
                "id": "prov-1",
                "timestamp": "2026-03-11T10:52:34.493Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "internal child result"}],
                    "provenance": {
                        "kind": "inter_session",
                        "sourceSessionKey": "agent:main:subagent:abc",
                        "sourceChannel": "webchat",
                        "sourceTool": "subagent_announce",
                    },
                    "usage": {
                        "input": 0,
                        "output": 0,
                        "cost": {"total": 0},
                    },
                },
            }
        ]

        parsed = server.parse_messages(entries)

        self.assertEqual(len(parsed), 1)
        msg = parsed[0]
        self.assertEqual(msg["role"], "user")
        self.assertEqual(msg["provenance_kind"], "inter_session")
        self.assertEqual(msg["provenance_source_session_key"], "agent:main:subagent:abc")
        self.assertEqual(msg["provenance_source_channel"], "webchat")
        self.assertEqual(msg["provenance_source_tool"], "subagent_announce")

    def test_assistant_error_message_is_preserved_when_content_is_empty(self):
        error_text = (
            'Codex error: {"type":"error","error":{"type":"server_error",'
            '"code":"server_error","message":"An error occurred"}}'
        )
        entries = [
            {
                "type": "message",
                "id": "075a2ff5",
                "timestamp": "2026-03-10T14:00:23.983Z",
                "message": {
                    "role": "assistant",
                    "content": [],
                    "stopReason": "error",
                    "errorMessage": error_text,
                    "usage": {
                        "input": 0,
                        "output": 0,
                        "cost": {"total": 0},
                    },
                },
            }
        ]

        parsed = server.parse_messages(entries)

        self.assertEqual(len(parsed), 1)
        msg = parsed[0]
        self.assertEqual(msg["role"], "assistant")
        self.assertEqual(msg["text"], "")
        self.assertEqual(msg["stop_reason"], "error")
        self.assertEqual(msg["error_message"], error_text)

    def test_model_snapshot_custom_is_mapped_to_model_event(self):
        entries = [
            {
                "type": "custom",
                "customType": "model-snapshot",
                "id": "snap-1",
                "timestamp": "2026-03-10T16:35:46.189Z",
                "data": {
                    "provider": "nvidia",
                    "modelApi": "openai-completions",
                    "modelId": "z-ai/glm4.7",
                },
            }
        ]

        parsed = server.parse_messages(entries)

        self.assertEqual(len(parsed), 1)
        msg = parsed[0]
        self.assertEqual(msg["role"], "event")
        self.assertEqual(msg["event_type"], "model")
        self.assertIn("model snapshot", msg["text"])
        self.assertIn("nvidia/z-ai/glm4.7", msg["text"])

    def test_delivery_mirror_is_not_exposed_as_model(self):
        entries = [
            {
                "type": "message",
                "id": "assist-1",
                "timestamp": "2026-03-11T09:10:00Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Mirrored delivery"}],
                    "model": "delivery-mirror",
                    "stopReason": "stop",
                    "usage": {"input": 3, "output": 4, "cost": {"total": 0}},
                },
            }
        ]

        parsed = server.parse_messages(entries)

        self.assertEqual(len(parsed), 1)
        msg = parsed[0]
        self.assertEqual(msg["role"], "assistant")
        self.assertEqual(msg["model"], "—")

    def test_friendly_model_rejects_delivery_mirror_marker(self):
        self.assertEqual(server.friendly_model("delivery-mirror"), "—")
        self.assertEqual(server.friendly_model(" anthropic/claude-sonnet-4 "), "claude-sonnet-4")

    def test_unknown_custom_is_mapped_to_custom_event(self):
        entries = [
            {
                "type": "custom",
                "customType": "my:custom:event",
                "id": "cust-1",
                "timestamp": "2026-03-10T16:35:46.189Z",
                "data": {"foo": "bar", "answer": 42},
            }
        ]

        parsed = server.parse_messages(entries)

        self.assertEqual(len(parsed), 1)
        msg = parsed[0]
        self.assertEqual(msg["role"], "event")
        self.assertEqual(msg["event_type"], "custom")
        self.assertIn("custom:my:custom:event", msg["text"])

    def test_session_entry_is_rendered_as_event(self):
        entries = [
            {
                "type": "session",
                "version": 3,
                "id": "sess-1",
                "timestamp": "2026-03-10T16:13:52.550Z",
                "cwd": "/Users/openclaw/.openclaw/workspace",
            }
        ]

        parsed = server.parse_messages(entries)

        self.assertEqual(len(parsed), 1)
        msg = parsed[0]
        self.assertEqual(msg["role"], "event")
        self.assertEqual(msg["event_type"], "session")
        self.assertIn("session started", msg["text"])

    def test_unknown_non_message_type_falls_back_to_meta_event(self):
        entries = [
            {
                "type": "mystery-type",
                "id": "myst-1",
                "parentId": "x",
                "timestamp": "2026-03-10T16:13:52.550Z",
            }
        ]

        parsed = server.parse_messages(entries)

        self.assertEqual(len(parsed), 1)
        msg = parsed[0]
        self.assertEqual(msg["role"], "event")
        self.assertEqual(msg["event_type"], "meta")
        self.assertIn("entry:mystery-type", msg["text"])


if __name__ == "__main__":
    unittest.main()
