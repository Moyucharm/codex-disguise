import json
import tempfile
import unittest
from pathlib import Path

import httpx

from scripts import probe_anyrouter_codex_shape as probe


class AnyRouterProbeShapeTests(unittest.TestCase):
    def test_plain_responses_body_omits_codex_only_fields(self):
        body = probe.plain_responses_body("gpt-5.6-sol", "1 + 2 equals 3, right?")

        self.assertEqual(body, {"model": "gpt-5.6-sol", "input": "1 + 2 equals 3, right?", "max_output_tokens": 16})
        self.assertNotIn("include", body)
        self.assertNotIn("client_metadata", body)
        self.assertNotIn("instructions", body)
        self.assertNotIn("tools", body)

    def test_plain_headers_omit_codex_only_headers(self):
        headers = probe.plain_headers("sk-test")

        self.assertEqual(headers["Authorization"], "Bearer sk-test")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertNotIn("originator", headers)
        self.assertNotIn("version", headers)
        self.assertNotIn("x-codex-turn-metadata", headers)

    def test_gateway_baseline_matches_current_gateway_shape(self):
        body = probe.gateway_baseline_body("gpt-5.6-sol", "1 + 2 equals 3, right?")
        headers = probe.gateway_baseline_headers("sk-test")

        self.assertEqual(body["model"], "gpt-5.6-sol")
        self.assertEqual(body["input"][0], {"type": "additional_tools", "role": "developer", "tools": []})
        self.assertEqual(
            body["input"][1],
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "1 + 2 equals 3, right?"}],
            },
        )
        self.assertNotIn("instructions", body)
        self.assertNotIn("tools", body)
        self.assertNotIn("max_output_tokens", body)
        self.assertNotIn("service_tier", body)
        self.assertEqual(body["tool_choice"], "auto")
        self.assertFalse(body["parallel_tool_calls"])
        self.assertFalse(body["store"])
        self.assertTrue(body["stream"])
        self.assertEqual(body["text"], {"verbosity": "medium"})
        self.assertEqual(body["reasoning"]["context"], "all_turns")
        self.assertEqual(body["include"], ["reasoning.encrypted_content"])
        self.assertIn("client_metadata", body)
        self.assertIsInstance(body["prompt_cache_key"], str)
        self.assertEqual(len(body["prompt_cache_key"]), 36)
        self.assertEqual(body["prompt_cache_key"].count("-"), 4)
        self.assertEqual(headers["Authorization"], "Bearer sk-test")
        self.assertEqual(headers["originator"], "codex_exec")
        self.assertNotIn("version", headers)
        self.assertIn("x-codex-turn-metadata", headers)

    def test_gateway_variants_only_add_or_authorized_override_fields(self):
        baseline_body = probe.gateway_baseline_body("gpt-5.6-sol", "1 + 2 equals 3, right?")
        baseline_headers = probe.gateway_baseline_headers("sk-test")
        baseline_body_keys = set(baseline_body)
        baseline_header_keys = set(baseline_headers)
        baseline_metadata_keys = set(baseline_body["client_metadata"])

        for name, body_transform, headers_transform in probe.gateway_variants("0.144.0"):
            if name.startswith("captured-official-lite"):
                continue
            with self.subTest(name=name):
                body = body_transform(baseline_body)
                headers = headers_transform(baseline_headers)
                metadata = body["client_metadata"]

                self.assertGreaterEqual(set(body), baseline_body_keys)
                self.assertGreaterEqual(set(headers), baseline_header_keys)
                self.assertGreaterEqual(set(metadata), baseline_metadata_keys)
                self.assertEqual(body["input"], baseline_body["input"])
                self.assertNotIn("instructions", body)
                self.assertNotIn("tools", body)
                self.assertEqual(body["tool_choice"], baseline_body["tool_choice"])
                self.assertEqual(body["parallel_tool_calls"], baseline_body["parallel_tool_calls"])
                self.assertEqual(body["reasoning"], baseline_body["reasoning"])
                self.assertEqual(body["include"], baseline_body["include"])
                self.assertTrue(body["stream"])
                if "override-version" in name:
                    self.assertEqual(headers["version"], "0.144.0")
                    self.assertIn("/0.144.0 ", headers["User-Agent"])
                else:
                    self.assertNotIn("version", headers)
                    self.assertEqual(headers["User-Agent"], baseline_headers["User-Agent"])
                self.assertEqual(body["input"][0]["type"], "additional_tools")

    def test_captured_official_lite_variant_matches_cli_fingerprint_shape(self):
        baseline_body = probe.gateway_baseline_body("gpt-5.6-sol", "1 + 2 equals 3, right?")
        baseline_headers = probe.gateway_baseline_headers("sk-test")

        body = probe.with_captured_official_lite_body(baseline_body)
        headers = probe.with_captured_codex_exec_headers(baseline_headers, "0.144.2")

        self.assertNotIn("instructions", body)
        self.assertNotIn("tools", body)
        self.assertNotIn("max_output_tokens", body)
        self.assertNotIn("prompt_cache_key", body)
        self.assertEqual(body["input"][0]["type"], "additional_tools")
        self.assertEqual(
            [tool["name"] for tool in body["input"][0]["tools"]],
            ["exec", "wait", "request_user_input", "collaboration"],
        )
        self.assertTrue(body["stream"])
        self.assertEqual(body["include"], ["reasoning.encrypted_content"])
        self.assertEqual(body["reasoning"], {"effort": "xhigh", "context": "all_turns"})
        self.assertEqual(body["text"], {"verbosity": "low"})
        self.assertEqual(body["service_tier"], "priority")
        self.assertEqual(headers["originator"], "codex_exec")
        self.assertEqual(headers["x-openai-internal-codex-responses-lite"], "true")
        self.assertEqual(headers["x-codex-beta-features"], "remote_compaction_v2")
        self.assertNotIn("version", headers)
        self.assertNotIn("session_id", headers)
        self.assertNotIn("thread_id", headers)
        self.assertNotIn("x-codex-installation-id", headers)

    def test_captured_official_lite_can_use_loaded_additional_tools(self):
        baseline_body = probe.gateway_baseline_body("gpt-5.6-sol", "1 + 2 equals 3, right?")
        tools = [{"type": "function", "name": "exact_tool"}]

        body = probe.with_captured_official_lite_body(baseline_body, additional_tools=tools)

        self.assertEqual(body["input"][0]["tools"], tools)
        self.assertIsNot(body["input"][0]["tools"], tools)

    def test_load_latest_captured_additional_tools(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "tools.jsonl"
            path.write_text(
                "\n".join([
                    json.dumps({"additional_tools": [{"name": "old", "type": "function"}]}),
                    json.dumps({"additional_tools": [{"name": "new", "type": "function"}]}),
                ]),
                encoding="utf-8",
            )

            tools = probe.load_latest_captured_additional_tools(path)

        self.assertEqual(tools, [{"name": "new", "type": "function"}])

    def test_additional_tools_subset_selects_ordered_deep_copy(self):
        tools = [
            {"name": "exec", "type": "custom", "nested": {"value": 1}},
            {"name": "wait", "type": "function"},
            {"name": "request_user_input", "type": "function"},
        ]

        subset = probe.additional_tools_subset(tools, ["wait", "exec"])

        self.assertEqual([tool["name"] for tool in subset], ["wait", "exec"])
        self.assertIsNot(subset[1], tools[0])
        subset[1]["nested"]["value"] = 2
        self.assertEqual(tools[0]["nested"]["value"], 1)

    def test_gateway_tool_subset_variants_use_requested_tool_names(self):
        baseline_body = probe.gateway_baseline_body("gpt-5.6-sol", "1 + 2 equals 3, right?")
        tools = [
            {"name": "exec", "type": "custom"},
            {"name": "wait", "type": "function"},
            {"name": "request_user_input", "type": "function"},
            {"name": "collaboration", "type": "namespace", "tools": []},
        ]
        variants = {
            name: body_transform
            for name, body_transform, _headers_transform in probe.gateway_variants("0.144.2", tools)
        }

        expected = {
            "captured-official-lite-tools-wait-only": ["wait"],
            "captured-official-lite-tools-request-user-input-only": ["request_user_input"],
            "captured-official-lite-tools-exec-only": ["exec"],
            "captured-official-lite-tools-exec-wait": ["exec", "wait"],
            "captured-official-lite-tools-exec-wait-request-user-input": ["exec", "wait", "request_user_input"],
            "captured-official-lite-tools-exec-wait-collaboration": ["exec", "wait", "collaboration"],
            "captured-official-lite-tools-full": ["exec", "wait", "request_user_input", "collaboration"],
        }
        for name, tool_names in expected.items():
            with self.subTest(name=name):
                body = variants[name](baseline_body)
                self.assertEqual(
                    [tool["name"] for tool in body["input"][0]["tools"]],
                    tool_names,
                )
                self.assertEqual(body["input"][0]["type"], "additional_tools")
                self.assertTrue(body["stream"])
                self.assertEqual(body["prompt_cache_key"], "123e4567-e89b-42d3-a456-426614174000")

    def test_captured_official_lite_can_include_probe_cache_key(self):
        baseline_body = probe.gateway_baseline_body("gpt-5.6-sol", "1 + 2 equals 3, right?")

        body = probe.with_captured_official_lite_body(baseline_body, include_prompt_cache_key=True)

        self.assertEqual(body["prompt_cache_key"], "probe-gpt-5.6-sol-codex-lite")

    def test_uuid_prompt_cache_key_matches_captured_shape(self):
        baseline_body = probe.gateway_baseline_body("gpt-5.6-sol", "1 + 2 equals 3, right?")
        body = probe.with_uuid_prompt_cache_key(
            probe.with_captured_official_lite_body(baseline_body)
        )

        shape = probe.string_shape(body["prompt_cache_key"])

        self.assertEqual(shape["length"], 36)
        self.assertEqual(shape["dash_count"], 4)
        self.assertEqual(shape["colon_count"], 0)
        self.assertTrue(shape["has_digit"])
        self.assertFalse(shape["has_uppercase"])

    def test_captured_official_lite_ablations_change_only_target_fields(self):
        baseline_body = probe.gateway_baseline_body("gpt-5.6-sol", "1 + 2 equals 3, right?")
        body = probe.with_captured_official_lite_body(baseline_body, include_prompt_cache_key=True)

        no_tier = probe.without_service_tier(body)
        low_reasoning = probe.with_reasoning_effort(body, "low")

        self.assertNotIn("service_tier", no_tier)
        for key, value in body.items():
            if key == "service_tier":
                continue
            self.assertEqual(no_tier[key], value)
        self.assertEqual(low_reasoning["reasoning"], {"effort": "low", "context": "all_turns"})
        for key, value in body.items():
            if key == "reasoning":
                continue
            self.assertEqual(low_reasoning[key], value)

    def test_gateway_plus_full_metadata_adds_expected_non_tool_fields(self):
        baseline_body = probe.gateway_baseline_body("gpt-5.6-sol", "1 + 2 equals 3, right?")
        baseline_headers = probe.gateway_baseline_headers("sk-test")

        body, headers = probe.with_full_non_output_affecting_metadata(baseline_body, baseline_headers)

        self.assertEqual(headers["x-openai-internal-codex-responses-lite"], "true")
        metadata = body["client_metadata"]
        self.assertEqual(metadata["ws_request_header_x_openai_internal_codex_responses_lite"], "true")
        self.assertEqual(metadata["session_id"], "sess_probe_anyrouter")
        self.assertEqual(metadata["thread_id"], "thread_probe_anyrouter")
        self.assertEqual(metadata["turn_id"], "turn_probe_anyrouter")
        self.assertIn("x-codex-turn-metadata", metadata)
        self.assertEqual(body["input"], baseline_body["input"])
        self.assertNotIn("tools", body)
        self.assertEqual(body["input"][0]["type"], "additional_tools")

    def test_codex_client_version_override_updates_version_and_user_agent_only(self):
        baseline_headers = probe.gateway_baseline_headers("sk-test")

        headers = probe.with_codex_client_version(baseline_headers, "0.144.0")

        self.assertEqual(headers["version"], "0.144.0")
        self.assertIn("/0.144.0 ", headers["User-Agent"])
        for key, value in baseline_headers.items():
            if key in {"version", "User-Agent"}:
                continue
            self.assertEqual(headers[key], value)

    def test_stream_true_override_changes_only_stream(self):
        baseline_body = probe.gateway_baseline_body("gpt-5.6-sol", "1 + 2 equals 3, right?")

        body = probe.with_stream_true(baseline_body)

        self.assertTrue(body["stream"])
        for key, value in baseline_body.items():
            if key == "stream":
                continue
            self.assertEqual(body[key], value)

    def test_real_lite_additional_tools_uses_codex_responses_lite_contract(self):
        body = probe.with_real_lite_additional_tools(
            probe.with_lite_metadata(
                probe.with_reasoning_all_turns(
                    probe.with_codex_body_min(probe.base_body("gpt-5.6-sol", "1 + 2 equals 3, right?"))
                )
            )
        )

        self.assertNotIn("instructions", body)
        self.assertNotIn("tools", body)
        self.assertEqual(body["tool_choice"], "auto")
        self.assertFalse(body["parallel_tool_calls"])
        self.assertEqual(body["reasoning"]["context"], "all_turns")
        self.assertEqual(
            body["client_metadata"]["ws_request_header_x_openai_internal_codex_responses_lite"],
            "true",
        )

        additional_tools = body["input"][0]
        self.assertEqual(additional_tools["type"], "additional_tools")
        self.assertEqual(additional_tools["role"], "developer")
        tools = additional_tools["tools"]
        self.assertEqual([tool["name"] for tool in tools[:2]], ["exec", "wait"])

        exec_tool = tools[0]
        self.assertEqual(exec_tool["type"], "custom")
        self.assertEqual(exec_tool["format"]["type"], "grammar")
        self.assertEqual(exec_tool["format"]["syntax"], "lark")
        self.assertIn("PRAGMA_LINE", exec_tool["format"]["definition"])

        wait_tool = tools[1]
        self.assertEqual(wait_tool["type"], "function")
        self.assertFalse(wait_tool["strict"])
        self.assertEqual(wait_tool["parameters"]["required"], ["cell_id"])
        self.assertFalse(wait_tool["parameters"]["additionalProperties"])

    def test_full_lite_additional_tools_matches_official_tool_names(self):
        body = probe.with_full_lite_additional_tools(
            probe.with_lite_metadata(
                probe.with_reasoning_all_turns(
                    probe.with_codex_body_min(probe.base_body("gpt-5.6-sol", "1 + 2 equals 3, right?"))
                )
            )
        )

        self.assertEqual(body["model"], "gpt-5.6-sol")
        self.assertEqual(body["input"][0]["type"], "additional_tools")
        self.assertEqual(body["input"][0]["role"], "developer")
        self.assertNotIn("tools", body)
        self.assertEqual(body["tool_choice"], "auto")
        self.assertFalse(body["parallel_tool_calls"])

        tools = body["input"][0]["tools"]
        self.assertEqual(
            [tool["name"] for tool in tools],
            ["exec", "wait", "request_user_input", "collaboration"],
        )
        self.assertEqual(tools[0]["type"], "custom")
        self.assertEqual(tools[1]["type"], "function")
        self.assertEqual(tools[2]["type"], "function")
        self.assertEqual(tools[3]["type"], "namespace")
        self.assertEqual(
            [tool["name"] for tool in tools[3]["tools"]],
            [
                "followup_task",
                "interrupt_agent",
                "list_agents",
                "send_message",
                "spawn_agent",
                "wait_agent",
            ],
        )
        collaboration_tools = {tool["name"]: tool for tool in tools[3]["tools"]}
        self.assertEqual(
            collaboration_tools["send_message"]["parameters"]["required"],
            ["target", "message"],
        )
        self.assertEqual(
            sorted(collaboration_tools["send_message"]["parameters"]["properties"]),
            ["message", "target"],
        )
        self.assertEqual(
            sorted(collaboration_tools["list_agents"]["parameters"]["properties"]),
            ["path_prefix"],
        )
        self.assertNotIn("required", collaboration_tools["list_agents"]["parameters"])

    def test_streaming_real_lite_shape_matches_codex_normal_turn_transport(self):
        body = probe.with_streaming(
            probe.with_real_lite_additional_tools(
                probe.with_lite_metadata(
                    probe.with_reasoning_all_turns(
                        probe.with_codex_body_min(probe.base_body("gpt-5.6-sol", "1 + 2 equals 3, right?"))
                    )
                )
            )
        )

        self.assertTrue(body["stream"])
        self.assertEqual(body["input"][0]["tools"][0]["name"], "exec")
        self.assertEqual(body["input"][0]["tools"][1]["name"], "wait")

    def test_codex_56_runtime_fields_match_latest_model_metadata(self):
        body = probe.with_codex_56_runtime_fields(
            probe.with_real_lite_additional_tools(
                probe.with_lite_metadata(
                    probe.with_reasoning_all_turns(
                        probe.with_codex_body_min(probe.base_body("gpt-5.6-sol", "1 + 2 equals 3, right?"))
                    )
                )
            )
        )

        self.assertEqual(body["reasoning"], {"effort": "low", "context": "all_turns"})
        self.assertEqual(body["text"], {"verbosity": "low"})
        metadata = body["client_metadata"]
        self.assertEqual(metadata["session_id"], "sess_probe_anyrouter")
        self.assertEqual(metadata["thread_id"], "thread_probe_anyrouter")
        self.assertIn("turn_id", metadata)
        self.assertIn("x-codex-turn-metadata", metadata)
        turn_metadata = probe.json.loads(metadata["x-codex-turn-metadata"])
        self.assertEqual(turn_metadata["request_kind"], "turn")
        self.assertEqual(turn_metadata["session_id"], "sess_probe_anyrouter")
        self.assertEqual(turn_metadata["thread_id"], "thread_probe_anyrouter")

    def test_codex_144_terminal_user_agent_keeps_version_header_clean(self):
        headers = probe.with_codex_144_terminal_user_agent({})

        self.assertEqual(headers["version"], "0.144.0")
        self.assertEqual(
            headers["User-Agent"],
            "codex_cli_rs/0.144.0 (Windows 10; x86_64) unknown",
        )

    def test_summarize_models_extracts_gpt_56_sol_without_provider_prefix(self):
        payload = {
            "data": [
                {"id": "gpt-5.5", "object": "model"},
                {"id": "gpt-5.6-sol", "object": "model"},
            ],
            "success": True,
        }

        summary = probe.summarize_models_payload(payload)

        self.assertEqual(summary["model_count"], 2)
        self.assertEqual(summary["gpt_56_sol"], {"id": "gpt-5.6-sol"})
        self.assertFalse(summary["has_openai_prefixed_gpt_56_sol"])

    def test_summarize_response_classifies_capacity_limit(self):
        response = httpx.Response(
            500,
            json={
                "error": {
                    "message": "当前模型 gpt-5.6-sol 负载已经达到上限，请稍后重试",
                    "code": "get_channel_failed",
                    "type": "new_api_error",
                }
            },
        )

        summary = probe.summarize_response(response)

        self.assertFalse(summary["invalid_codex"])
        self.assertTrue(summary["capacity_limited"])
        self.assertEqual(summary["code"], "get_channel_failed")


if __name__ == "__main__":
    unittest.main()
