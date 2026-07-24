import json
import unittest

from fastapi.responses import JSONResponse

import main


class FakeRequest:
    headers: dict[str, str] = {}


def gateway_state() -> dict[str, object]:
    return {
        "installation_id": "install_123",
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

    def test_codex_wait_tool_matches_official_capture_fingerprint(self):
        wait = main.CODEX_WAIT_TOOL
        self.assertEqual(wait["type"], "function")
        self.assertEqual(wait["name"], "wait")
        self.assertFalse(wait["strict"])
        self.assertEqual(wait["parameters"]["required"], ["cell_id"])
        self.assertFalse(wait["parameters"]["additionalProperties"])
        self.assertEqual(
            sorted(wait["parameters"]["properties"]),
            ["cell_id", "max_tokens", "terminate", "yield_time_ms"],
        )
        self.assertEqual(len(wait["description"]), 769)
        self.assertIn("If the cell has already finished", wait["description"])

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
        self.assertEqual(
            shaped["input"][1],
            {
                "type": "message",
                "role": "system",
                "content": [{"type": "input_text", "text": "client instructions"}],
            },
        )
        self.assertEqual(
            shaped["input"][2],
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "ping"}],
            },
        )
        self.assertEqual(len(shaped["input"]), 3)
        self.assertEqual(shaped["client_metadata"]["existing"], "value")
        self.assertNotIn("session_id", shaped["client_metadata"])
        self.assertEqual(shaped["client_metadata"]["thread_id"], "thread_123")
        self.assertEqual(shaped["client_metadata"]["turn_id"], "turn_123")
        self.assertEqual(shaped["client_metadata"]["x-codex-installation-id"], "install_123")
        self.assertEqual(shaped["client_metadata"]["x-codex-window-id"], "thread_123:0")
        self.assertEqual(shaped["client_metadata"]["x-codex-turn-metadata"], turn_metadata)
        self.assertNotIn("session_id", json.loads(turn_metadata))
        self.assertIn("tools", body)
        self.assertEqual(body["instructions"], "client instructions")

    def test_lite_invalid_instructions_do_not_create_system_message(self):
        state = gateway_state()
        user_message = {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "ping"}],
        }

        for case_name, instructions, present in (
            ("missing", None, False),
            ("none", None, True),
            ("empty", "", True),
            ("non_string", 123, True),
        ):
            with self.subTest(case_name):
                body: dict[str, object] = {
                    "model": "gpt-5.6-sol",
                    "input": [user_message],
                }
                if present:
                    body["instructions"] = instructions

                shaped = main._body_for_upstream_channel(body, {"inject_wait_tool": 0}, state, None)

                self.assertNotIn("instructions", shaped)
                self.assertEqual(shaped["input"][0]["type"], "additional_tools")
                self.assertEqual(shaped["input"][1], user_message)
                self.assertEqual(len(shaped["input"]), 2)
                for item in shaped["input"]:
                    if isinstance(item, dict) and item.get("type") == "message":
                        self.assertNotEqual(item.get("role"), "system")

    def test_lite_preserves_existing_system_and_developer_messages(self):
        state = gateway_state()
        system_message = {
            "type": "message",
            "role": "system",
            "content": [{"type": "input_text", "text": "existing system"}],
        }
        developer_message = {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": "existing developer"}],
        }
        user_message = {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "ping"}],
        }
        body = {
            "model": "gpt-5.6-sol",
            "input": [system_message, developer_message, user_message],
            "instructions": "migrated instructions",
        }

        shaped = main._body_for_upstream_channel(body, {"inject_wait_tool": 0}, state, None)

        self.assertNotIn("instructions", shaped)
        self.assertEqual(shaped["input"][0]["type"], "additional_tools")
        self.assertEqual(
            shaped["input"][1],
            {
                "type": "message",
                "role": "system",
                "content": [{"type": "input_text", "text": "migrated instructions"}],
            },
        )
        self.assertEqual(shaped["input"][2], system_message)
        self.assertEqual(shaped["input"][3], developer_message)
        self.assertEqual(shaped["input"][4], user_message)
        self.assertEqual(body["instructions"], "migrated instructions")

    def test_lite_instructions_without_input_creates_system_message(self):
        state = gateway_state()
        body = {
            "model": "gpt-5.6-sol",
            "instructions": "only instructions",
        }

        shaped = main._body_for_upstream_channel(body, {"inject_wait_tool": 0}, state, None)

        self.assertNotIn("instructions", shaped)
        self.assertEqual(shaped["input"][0]["type"], "additional_tools")
        self.assertEqual(
            shaped["input"][1],
            {
                "type": "message",
                "role": "system",
                "content": [{"type": "input_text", "text": "only instructions"}],
            },
        )
        self.assertEqual(len(shaped["input"]), 2)
        self.assertEqual(body["instructions"], "only instructions")

    def test_lite_string_input_follows_migrated_system_message(self):
        state = gateway_state()
        body = {
            "model": "gpt-5.6-sol",
            "input": "ping",
            "instructions": "client instructions",
        }

        shaped = main._body_for_upstream_channel(body, {"inject_wait_tool": 0}, state, None)

        self.assertNotIn("instructions", shaped)
        self.assertEqual(shaped["input"][0]["type"], "additional_tools")
        self.assertEqual(
            shaped["input"][1],
            {
                "type": "message",
                "role": "system",
                "content": [{"type": "input_text", "text": "client instructions"}],
            },
        )
        self.assertEqual(
            shaped["input"][2],
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "ping"}],
            },
        )
        self.assertEqual(len(shaped["input"]), 3)
        self.assertEqual(body["instructions"], "client instructions")

    def test_lite_upstream_body_passthrough_client_session_id(self):
        state = gateway_state()
        body = {
            "model": "gpt-5.6-sol",
            "input": "ping",
            "client_metadata": {"session_id": "sess_client_owned"},
        }

        shaped = main._body_for_upstream_channel(body, {"inject_wait_tool": 0}, state, None)

        self.assertEqual(shaped["client_metadata"]["session_id"], "sess_client_owned")

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
        wait_tool = shaped["input"][0]["tools"][2]
        self.assertEqual(wait_tool, main.CODEX_WAIT_TOOL)
        self.assertEqual(len(wait_tool["description"]), 769)
        self.assertIsInstance(shaped["prompt_cache_key"], str)
        self.assertEqual(len(shaped["prompt_cache_key"]), 36)
        self.assertEqual(shaped["prompt_cache_key"].count("-"), 4)

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

    def test_lite_generates_uuid_prompt_cache_key_when_missing(self):
        state = gateway_state()
        body = {
            "model": "gpt-5.6-sol",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "ping"}],
                }
            ],
        }

        shaped = main._body_for_upstream_channel(body, {"inject_wait_tool": 0}, state, None)

        cache_key = shaped["prompt_cache_key"]
        self.assertIsInstance(cache_key, str)
        self.assertEqual(len(cache_key), 36)
        self.assertEqual(cache_key.count("-"), 4)
        self.assertNotIn("prompt_cache_key", body)

    def test_lite_blank_prompt_cache_key_is_replaced(self):
        state = gateway_state()
        body = {
            "model": "gpt-5.6-sol",
            "input": "ping",
            "prompt_cache_key": "   ",
        }

        shaped = main._body_for_upstream_channel(body, {"inject_wait_tool": 0}, state, None)

        self.assertNotEqual(shaped["prompt_cache_key"].strip(), "")
        self.assertEqual(len(shaped["prompt_cache_key"]), 36)

    def test_non_lite_model_does_not_generate_prompt_cache_key(self):
        body = {"model": "gpt-5.5", "input": "ping"}
        shaped = main._body_for_upstream_channel(body, {"inject_wait_tool": 1}, gateway_state(), None)
        self.assertIs(shaped, body)
        self.assertNotIn("prompt_cache_key", shaped)

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
        self.assertNotIn("session_id", lite_headers)
        self.assertNotIn("session-id", lite_headers)
        self.assertNotIn("session_id", non_lite_headers)
        self.assertNotIn("session-id", non_lite_headers)

        self.assertEqual(non_lite_headers["originator"], "codex_cli_rs")
        self.assertEqual(non_lite_headers["version"], "0.144.2")
        self.assertNotIn("x-openai-internal-codex-responses-lite", non_lite_headers)

    def test_codex_turn_metadata_omits_session_id(self):
        turn_metadata = json.loads(main._codex_turn_metadata(gateway_state(), "turn_123"))
        self.assertNotIn("session_id", turn_metadata)
        self.assertEqual(turn_metadata["thread_id"], "thread_123")
        self.assertEqual(turn_metadata["turn_id"], "turn_123")

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

    def test_sse_empty_completed_output_is_filled_from_stream_text_events(self):
        body = b"".join([
            b"event: response.output_text.delta\n",
            b'data: {"type":"response.output_text.delta","delta":"Yes, 1 + 2 = 3."}\n\n',
            b"event: response.output_text.done\n",
            b'data: {"type":"response.output_text.done","text":"Yes, 1 + 2 = 3."}\n\n',
            b"event: response.completed\n",
            b'data: {"type":"response.completed","response":{"id":"resp_empty","object":"response","status":"completed","output":[],"output_text":null,"model":"gpt-5.6-sol"}}\n\n',
        ])

        response_json = main._response_json_from_sse(body, "gpt-5.6-sol")

        self.assertEqual(response_json["id"], "resp_empty")
        self.assertEqual(response_json["status"], "completed")
        self.assertEqual(response_json["output_text"], "Yes, 1 + 2 = 3.")
        self.assertEqual(
            response_json["output"],
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Yes, 1 + 2 = 3."}],
                }
            ],
        )

    def test_sse_block_is_terminal_detects_completed_and_done(self):
        self.assertTrue(main._sse_block_is_terminal('event: response.completed\ndata: {"type":"response.completed"}'))
        self.assertTrue(main._sse_block_is_terminal('data: {"type":"response.failed","response":{"status":"failed"}}'))
        self.assertTrue(main._sse_block_is_terminal("data: [DONE]"))
        self.assertFalse(main._sse_block_is_terminal('event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"x"}'))


class PostResponsesShapeTests(unittest.IsolatedAsyncioTestCase):
    async def test_read_sse_until_terminal_stops_before_eof(self):
        class FakeResponse:
            def __init__(self, chunks: list[bytes]):
                self._chunks = chunks

            async def aiter_bytes(self):
                for chunk in self._chunks:
                    yield chunk

        prefix = (
            b"event: response.output_text.delta\n"
            b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
        )
        completed = (
            b"event: response.completed\n"
            b'data: {"type":"response.completed","response":{"id":"resp_early","status":"completed","output":[],"output_text":null}}\n\n'
        )
        hang_tail = b": keep-alive after completed\n\n" + (b"x" * 1024)
        expected = prefix + completed
        response = FakeResponse([prefix + completed, hang_tail])

        body = await main._read_sse_until_terminal(response)

        self.assertEqual(body, expected)
        self.assertNotIn(b"keep-alive after completed", body)
        parsed = main._response_json_from_sse(body, "gpt-5.6-sol")
        self.assertEqual(parsed["id"], "resp_early")
        self.assertEqual(parsed["output_text"], "hi")

    async def test_read_sse_until_terminal_truncates_same_chunk_trailer(self):
        class FakeResponse:
            def __init__(self, chunks: list[bytes]):
                self._chunks = chunks

            async def aiter_bytes(self):
                for chunk in self._chunks:
                    yield chunk

        completed = (
            b"event: response.output_text.delta\n"
            b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n'
            b"event: response.completed\n"
            b'data: {"type":"response.completed","response":{"id":"resp_same","status":"completed","output":[],"output_text":null}}\n\n'
        )
        hang_tail = b": keep-alive after completed\n\n" + (b"noise" * 64)
        response = FakeResponse([completed + hang_tail])

        body = await main._read_sse_until_terminal(response)

        self.assertEqual(body, completed)
        self.assertNotIn(b"keep-alive after completed", body)
        self.assertNotIn(b"noise", body)
        parsed = main._response_json_from_sse(body, "gpt-5.6-sol")
        self.assertEqual(parsed["id"], "resp_same")
        self.assertEqual(parsed["output_text"], "hi")

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
