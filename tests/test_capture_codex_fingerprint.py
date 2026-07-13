import unittest

from scripts import capture_codex_fingerprint as capture


class CaptureCodexFingerprintTests(unittest.TestCase):
    def test_safe_headers_redacts_sensitive_values(self):
        headers = {
            "Authorization": "Bearer secret",
            "ChatGPT-Account-Id": "acct_secret",
            "User-Agent": "codex_cli_rs/0.144.2 (Linux 6.1; x86_64) unknown",
            "version": "0.144.2",
            "x-openai-internal-codex-responses-lite": "true",
        }

        safe = capture.safe_headers(headers)

        self.assertTrue(safe["has_authorization"])
        self.assertTrue(safe["has_chatgpt_account_id"])
        self.assertNotIn("authorization", safe["keys"])
        self.assertNotIn("chatgpt-account-id", safe["keys"])
        self.assertNotIn("authorization", safe["values"])
        self.assertNotIn("chatgpt-account-id", safe["values"])
        self.assertEqual(safe["values"]["version"], "0.144.2")
        self.assertEqual(safe["values"]["x-openai-internal-codex-responses-lite"], "true")

    def test_body_fingerprint_omits_prompt_text_and_keeps_shape(self):
        payload = {
            "model": "gpt-5.6-sol",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "1 + 2 equals 3, right?"}],
                }
            ],
            "stream": True,
            "prompt_cache_key": "prefix:abc-123_DEF",
            "client_metadata": {"session_id": "sess_123"},
            "tools": [],
        }

        fingerprint = capture.body_fingerprint(payload)

        self.assertEqual(fingerprint["model"], "gpt-5.6-sol")
        self.assertTrue(fingerprint["stream"])
        self.assertEqual(fingerprint["client_metadata_keys"], ["session_id"])
        self.assertEqual(fingerprint["tools_count"], 0)
        self.assertEqual(fingerprint["prompt_cache_key_shape"]["length"], 18)
        self.assertEqual(fingerprint["prompt_cache_key_shape"]["colon_count"], 1)
        self.assertNotIn("prefix:abc", str(fingerprint))
        self.assertEqual(fingerprint["input"]["item_types"], ["message"])
        self.assertEqual(fingerprint["input"]["content_types"], ["input_text"])
        self.assertNotIn("1 + 2", str(fingerprint))

    def test_additional_tools_schema_extracts_only_tool_definitions(self):
        payload = {
            "input": [
                {
                    "type": "additional_tools",
                    "role": "developer",
                    "tools": [{"type": "function", "name": "wait", "description": "safe"}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "secret prompt"}],
                },
            ]
        }

        schema = capture.additional_tools_schema(payload)

        self.assertEqual(schema, [{"type": "function", "name": "wait", "description": "safe"}])
        self.assertNotIn("secret prompt", str(schema))

    def test_additional_tools_capture_event_omits_headers_and_prompt(self):
        body = b'{"input":[{"type":"additional_tools","tools":[{"name":"exec","type":"custom"}]},{"type":"message","content":[{"type":"input_text","text":"secret prompt"}]}]}'

        event = capture.additional_tools_capture_event("POST", "/v1/responses", body)

        self.assertEqual(event["path"], "/v1/responses")
        self.assertEqual(event["additional_tools"], [{"name": "exec", "type": "custom"}])
        self.assertNotIn("secret prompt", str(event))
        self.assertNotIn("headers", event)

    def test_websocket_accept_key_matches_rfc_example(self):
        self.assertEqual(
            capture.websocket_accept_key("dGhlIHNhbXBsZSBub25jZQ=="),
            "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=",
        )


if __name__ == "__main__":
    unittest.main()
