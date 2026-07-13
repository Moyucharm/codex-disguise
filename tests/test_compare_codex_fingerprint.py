import unittest

from scripts import compare_codex_fingerprint as compare


class CompareCodexFingerprintTests(unittest.TestCase):
    def test_compare_reports_added_removed_and_changed_fields(self):
        captured = {
            "headers": {
                "keys": ["content-type", "x-openai-internal-codex-responses-lite"],
                "values": {"content-type": "application/json", "originator": "codex_exec"},
            },
            "body": {
                "body_keys": ["input", "model", "text"],
                "model": "gpt-5.6-sol",
                "stream": True,
                "include": ["reasoning.encrypted_content"],
                "reasoning": {"effort": "xhigh", "context": "all_turns"},
                "text_keys": ["verbosity"],
                "client_metadata_keys": ["session_id"],
                "tool_names": [],
                "tools_count": None,
                "input": {
                    "item_types": ["additional_tools", "message"],
                    "roles": ["developer", "user"],
                    "additional_tool_names": ["exec"],
                },
            },
        }
        gateway = {
            "headers": {
                "keys": ["content-type"],
                "values": {"content-type": "application/json", "originator": "codex_cli_rs"},
            },
            "body": {
                "body_keys": ["input", "instructions", "model", "tools"],
                "model": "gpt-5.6-sol",
                "stream": False,
                "include": ["reasoning.encrypted_content"],
                "reasoning": {"effort": "medium", "summary": "auto", "context": "all_turns"},
                "text_keys": [],
                "client_metadata_keys": [],
                "tool_names": [],
                "tools_count": 0,
                "input": {"item_types": ["message"], "roles": ["user"], "additional_tool_names": []},
            },
        }

        result = compare.compare(captured, gateway)

        self.assertEqual(result["header_keys"]["only_in_capture"], ["x-openai-internal-codex-responses-lite"])
        self.assertEqual(result["header_values"]["originator"]["capture"], "codex_exec")
        self.assertEqual(result["body_keys"]["only_in_capture"], ["text"])
        self.assertEqual(result["body_keys"]["only_in_gateway"], ["instructions", "tools"])
        self.assertTrue(result["body_values"]["stream"]["capture"])
        self.assertEqual(result["additional_tool_names"]["only_in_capture"], ["exec"])
        self.assertEqual(result["tools_count"], {"capture": None, "gateway": 0})


if __name__ == "__main__":
    unittest.main()
