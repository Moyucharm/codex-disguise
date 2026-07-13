import json
import unittest

from fastapi.responses import JSONResponse

import main


class FakeRequest:
    headers: dict[str, str] = {}


def gateway_state() -> dict[str, object]:
    return {
        "installation_id": "install_123",
        "session_id": "sess_123",
        "thread_id": "thread_123",
        "window_generation": 0,
        "created_at": "2026-01-01T00:00:00Z",
    }


class CodexRequestShapeTests(unittest.TestCase):
    def test_lite_model_detection_is_limited_to_gpt_56_prefix(self):
        for model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6-future"):
            with self.subTest(model=model):
                self.assertTrue(main._is_responses_lite_model(model))

        for model in ("gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.2", None):
            with self.subTest(model=model):
                self.assertFalse(main._is_responses_lite_model(model))

    def test_non_lite_model_keeps_original_upstream_body(self):
        body = {"model": "gpt-5.5", "input": "ping"}
        channel = {"upstream_url": "https://new.sharedchat.cc/codex/v1"}

        upstream_body = main._body_for_upstream_channel(body, channel, gateway_state(), None)

        self.assertIs(upstream_body, body)

    def test_lite_upstream_body_uses_codex_responses_lite_shape(self):
        state = gateway_state()
        turn_metadata = main._codex_turn_metadata(state, "turn_123")
        body = {
            "model": "gpt-5.6-sol",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "ping"}],
                }
            ],
            "instructions": "client instructions",
            "tools": [{"type": "function", "name": "client_tool"}],
            "include": ["other.include"],
            "client_metadata": {"existing": "value"},
            "max_output_tokens": 1024,
            "service_tier": "priority",
            "reasoning": {"effort": "high", "context": "all_turns"},
            "prompt_cache_key": "client-owned-cache-key",
            "stream": False,
        }

        shaped = main._body_for_upstream_channel(body, {"inject_wait_tool": 0}, state, turn_metadata)

        self.assertEqual(shaped["model"], "gpt-5.6-sol")
        self.assertNotIn("instructions", shaped)
        self.assertNotIn("tools", shaped)
        self.assertNotIn("max_output_tokens", shaped)
        self.assertNotIn("service_tier", shaped)
        self.assertTrue(shaped["stream"])
        self.assertFalse(shaped["store"])
        self.assertEqual(shaped["tool_choice"], "auto")
        self.assertFalse(shaped["parallel_tool_calls"])
        self.assertEqual(shaped["include"], ["reasoning.encrypted_content"])
        self.assertEqual(shaped["text"], {"verbosity": "medium"})
        self.assertEqual(shaped["reasoning"], {"effort": "high", "context": "all_turns"})
        self.assertEqual(shaped["prompt_cache_key"], "client-owned-cache-key")
        self.assertEqual(shaped["input"][0]["type"], "additional_tools")
        self.assertEqual([tool["name"] for tool in shaped["input"][0]["tools"]], ["client_tool"])
        self.assertEqual(shaped["input"][1]["type"], "message")
        self.assertEqual(shaped["client_metadata"]["existing"], "value")
        self.assertEqual(shaped["client_metadata"]["session_id"], "sess_123")
        self.assertEqual(shaped["client_metadata"]["thread_id"], "thread_123")
        self.assertEqual(shaped["client_metadata"]["turn_id"], "turn_123")
        self.assertEqual(shaped["client_metadata"]["x-codex-installation-id"], "install_123")
        self.assertEqual(shaped["client_metadata"]["x-codex-window-id"], "thread_123:0")
        self.assertEqual(shaped["client_metadata"]["x-codex-turn-metadata"], turn_metadata)
        self.assertIn("tools", body)

    def test_lite_upstream_body_defaults_reasoning_and_reuses_additional_tools(self):
        state = gateway_state()
        body = {
            "model": "gpt-5.6-terra",
            "input": [
                {"type": "additional_tools", "role": "developer", "tools": [{"type": "function", "name": "existing_tool"}]},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "ping"}]},
            ],
            "tools": [{"type": "function", "name": "client_tool"}],
        }

        shaped = main._body_for_upstream_channel(body, {"inject_wait_tool": 1}, state, main._codex_turn_metadata(state, "turn_abc"))

        self.assertEqual(shaped["reasoning"], {"effort": "medium", "context": "all_turns"})
        self.assertEqual(
            [tool["name"] for tool in shaped["input"][0]["tools"]],
            ["existing_tool", "client_tool", "wait"],
        )
        self.assertEqual(shaped["input"][1]["type"], "message")

    def test_lite_wait_injection_does_not_duplicate_existing_wait_tool(self):
        state = gateway_state()
        body = {
            "model": "gpt-5.6-luna",
            "input": [
                {"type": "additional_tools", "tools": [{"type": "function", "name": "wait"}]},
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "ping"}]},
            ],
        }

        shaped = main._body_for_upstream_channel(body, {"inject_wait_tool": 1}, state, None)

        self.assertEqual([tool["name"] for tool in shaped["input"][0]["tools"]], ["wait"])

    def test_codex_headers_switch_to_lite_shape_only_for_lite_models(self):
        state = gateway_state()
        channel = {"upstream_api_key": "sk-test"}
        turn_metadata = main._codex_turn_metadata(state, "turn_123")

        lite_headers = main._codex_headers(FakeRequest(), state, channel, "gpt-5.6-sol", turn_metadata)
        non_lite_headers = main._codex_headers(FakeRequest(), state, channel, "gpt-5.5", turn_metadata)

        self.assertEqual(lite_headers["Authorization"], "Bearer sk-test")
        self.assertEqual(lite_headers["originator"], "codex_exec")
        self.assertIn("codex_exec/0.144.2", lite_headers["User-Agent"])
        self.assertEqual(lite_headers["x-codex-beta-features"], "remote_compaction_v2")
        self.assertEqual(lite_headers["x-openai-internal-codex-responses-lite"], "true")
        self.assertNotIn("version", lite_headers)
        self.assertEqual(lite_headers["x-codex-turn-metadata"], turn_metadata)

        self.assertEqual(non_lite_headers["originator"], "codex_cli_rs")
        self.assertEqual(non_lite_headers["version"], "0.144.2")
        self.assertNotIn("x-openai-internal-codex-responses-lite", non_lite_headers)

    def test_sse_output_text_events_can_be_aggregated_to_response_json(self):
        body = b"".join([
            b"event: response.output_text.delta\n",
            b'data: {"type":"response.output_text.delta","delta":"he"}\n\n',
            b"event: response.output_text.delta\n",
            b'data: {"type":"response.output_text.delta","delta":"llo"}\n\n',
            b"event: response.completed\n",
            b'data: {"type":"response.completed","response":{"id":"resp_1","object":"response","status":"completed","output_text":"hello"}}\n\n',
        ])

        response_json = main._response_json_from_sse(body, "gpt-5.6-sol")

        self.assertEqual(response_json["id"], "resp_1")
        self.assertEqual(response_json["status"], "completed")
        self.assertEqual(response_json["output_text"], "hello")


class PostResponsesShapeTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_responses_keeps_non_streaming_lite_downstream_response_as_json(self):
        state = gateway_state()
        captured: dict[str, object] = {}

        async def fake_open_stream(request, body):
            captured["body"] = body
            return object()

        async def fake_json_from_stream_result(result, model):
            captured["stream_result"] = result
            captured["model"] = model
            return JSONResponse({"ok": True})

        original_get_gateway_state = main._get_gateway_state
        original_open_stream = main._open_stream_with_failover
        original_json_from_stream = main._json_response_from_stream_result
        main._get_gateway_state = lambda: state
        main._open_stream_with_failover = fake_open_stream
        main._json_response_from_stream_result = fake_json_from_stream_result
        try:
            response = await main._post_responses(FakeRequest(), {"model": "gpt-5.6-sol", "input": "ping"})
        finally:
            main._get_gateway_state = original_get_gateway_state
            main._open_stream_with_failover = original_open_stream
            main._json_response_from_stream_result = original_json_from_stream

        self.assertIsInstance(response, JSONResponse)
        self.assertEqual(json.loads(response.body), {"ok": True})
        body = captured["body"]
        self.assertNotEqual(body.get("stream"), True)
        self.assertEqual(captured["model"], "gpt-5.6-sol")


if __name__ == "__main__":
    unittest.main()
