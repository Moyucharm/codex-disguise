import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import capture_codex_fingerprint as capture
from scripts import probe_anyrouter_codex_shape as probe


def load_latest_responses_event(path: str) -> dict[str, Any]:
    latest: dict[str, Any] | None = None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if event.get("transport") == "http" and event.get("method") == "POST" and event.get("path") == "/v1/responses":
            latest = event
    if latest is None:
        raise SystemExit(f"No /v1/responses event found in {path}")
    return latest


def gateway_fingerprint(input_text: str, codex_version: str) -> dict[str, Any]:
    body = probe.gateway_baseline_body("gpt-5.6-sol", input_text)
    headers = probe.gateway_baseline_headers("sk-capture")
    headers["User-Agent"] = probe.codex_exec_user_agent(codex_version)
    return {
        "headers": capture.safe_headers(headers),
        "body": capture.body_fingerprint(body),
    }


def list_diff(left: list[Any] | None, right: list[Any] | None) -> dict[str, list[Any]]:
    left_values = left or []
    right_values = right or []
    return {
        "only_in_capture": [item for item in left_values if item not in right_values],
        "only_in_gateway": [item for item in right_values if item not in left_values],
    }


def compare(capture_event: dict[str, Any], gateway_event: dict[str, Any]) -> dict[str, Any]:
    captured_headers = capture_event.get("headers") or {}
    captured_body = capture_event.get("body") or {}
    gateway_headers = gateway_event.get("headers") or {}
    gateway_body = gateway_event.get("body") or {}

    captured_header_values = captured_headers.get("values") or {}
    gateway_header_values = gateway_headers.get("values") or {}
    common_header_value_keys = sorted(set(captured_header_values) & set(gateway_header_values))
    changed_header_values = {
        key: {"capture": captured_header_values.get(key), "gateway": gateway_header_values.get(key)}
        for key in common_header_value_keys
        if captured_header_values.get(key) != gateway_header_values.get(key)
    }

    scalar_body_keys = ["model", "stream", "store", "tool_choice", "parallel_tool_calls", "include", "reasoning"]
    changed_body_values = {
        key: {"capture": captured_body.get(key), "gateway": gateway_body.get(key)}
        for key in scalar_body_keys
        if captured_body.get(key) != gateway_body.get(key)
    }

    return {
        "header_keys": list_diff(captured_headers.get("keys"), gateway_headers.get("keys")),
        "header_values": changed_header_values,
        "body_keys": list_diff(captured_body.get("body_keys"), gateway_body.get("body_keys")),
        "body_values": changed_body_values,
        "text_keys": list_diff(captured_body.get("text_keys"), gateway_body.get("text_keys")),
        "client_metadata_keys": list_diff(captured_body.get("client_metadata_keys"), gateway_body.get("client_metadata_keys")),
        "tool_names": list_diff(captured_body.get("tool_names"), gateway_body.get("tool_names")),
        "input_item_types": list_diff(
            (captured_body.get("input") or {}).get("item_types"),
            (gateway_body.get("input") or {}).get("item_types"),
        ),
        "input_roles": list_diff(
            (captured_body.get("input") or {}).get("roles"),
            (gateway_body.get("input") or {}).get("roles"),
        ),
        "additional_tool_names": list_diff(
            (captured_body.get("input") or {}).get("additional_tool_names"),
            (gateway_body.get("input") or {}).get("additional_tool_names"),
        ),
        "additional_tool_fingerprints": {
            "capture": (captured_body.get("input") or {}).get("additional_tool_fingerprints") or [],
            "gateway": (gateway_body.get("input") or {}).get("additional_tool_fingerprints") or [],
        },
        "prompt_cache_key_shape": {
            "capture": captured_body.get("prompt_cache_key_shape"),
            "gateway": gateway_body.get("prompt_cache_key_shape"),
        },
        "tools_count": {"capture": captured_body.get("tools_count"), "gateway": gateway_body.get("tools_count")},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("jsonl_path")
    parser.add_argument("--input-text", required=True)
    parser.add_argument("--codex-version", default="0.144.2")
    args = parser.parse_args()

    captured = load_latest_responses_event(args.jsonl_path)
    gateway = gateway_fingerprint(args.input_text, args.codex_version)
    print(json.dumps(compare(captured, gateway), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
