import argparse
import copy
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main  # noqa: E402
from scripts.capture_codex_fingerprint import tool_schema_fingerprint  # noqa: E402


ANYROUTER_URL = "https://anyrouter.top/v1/responses"
DEFAULT_CAPTURED_ADDITIONAL_TOOLS_PATH = ROOT / "data" / "codex-capture-additional-tools.jsonl"
INVALID_CODEX_MARKERS = (
    "invalid codex request",
    "invalid_responses_request",
    "invalid responses request",
    "invalid_prompt",
)
CAPACITY_LIMIT_MARKERS = (
    "负载已经达到上限",
    "capacity",
    "overloaded",
    "rate limit",
)


class FakeRequest:
    headers: dict[str, str] = {}


def load_dotenv_key(name: str) -> str | None:
    path = ROOT / ".env"
    if not path.exists():
        return None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() != name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        return value.strip() or None
    return None


def load_api_key() -> str:
    value = os.environ.get("ANY_API_KEY") or load_dotenv_key("ANY_API_KEY")
    if not value:
        raise SystemExit("ANY_API_KEY is not set in the environment or .env")
    return value


def state() -> dict[str, Any]:
    return {
        "installation_id": "d818825a-972a-5a7a-901e-fe4ce7912668",
        "session_id": "sess_probe_anyrouter",
        "thread_id": "thread_probe_anyrouter",
        "window_generation": 0,
    }


def anyrouter_channel(api_key: str) -> dict[str, Any]:
    return {
        "id": "probe",
        "name": "anyrouter-probe",
        "upstream_api_key": api_key,
        "upstream_url": "https://anyrouter.top/v1",
    }


def base_body(model: str, input_text: str, max_output_tokens: int = 16) -> dict[str, Any]:
    gateway_state = state()
    body: dict[str, Any] = {"model": model, "input": input_text, "max_output_tokens": max_output_tokens}
    body = main._ensure_input_array(body)
    body = main._ensure_client_metadata(FakeRequest(), body, gateway_state)
    body = main._ensure_responses_include(body)
    return body


def base_headers(api_key: str) -> dict[str, str]:
    gateway_state = state()
    return main._codex_headers(FakeRequest(), gateway_state, anyrouter_channel(api_key))


def plain_responses_body(model: str, input_text: str, max_output_tokens: int = 16) -> dict[str, Any]:
    return {"model": model, "input": input_text, "max_output_tokens": max_output_tokens}


def plain_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": main._format_bearer_token(api_key),
        "Content-Type": "application/json",
    }


def gateway_baseline_body(model: str, input_text: str, max_output_tokens: int = 16) -> dict[str, Any]:
    gateway_state = state()
    body: dict[str, Any] = {"model": model, "input": input_text, "max_output_tokens": max_output_tokens}
    body = main._ensure_input_array(body)
    body = main._ensure_client_metadata(FakeRequest(), body, gateway_state)
    body = main._ensure_responses_include(body)
    body = main._ensure_codex_request_shape(body, gateway_state)
    return main._body_for_upstream_channel(body, anyrouter_channel("sk-probe-placeholder"), gateway_state, gateway_turn_metadata())


def gateway_baseline_headers(api_key: str) -> dict[str, str]:
    gateway_state = state()
    return main._codex_headers(FakeRequest(), gateway_state, anyrouter_channel(api_key), "gpt-5.6-sol", gateway_turn_metadata())


def _add_header(headers: dict[str, str], name: str, value: str) -> dict[str, str]:
    headers = dict(headers)
    existing = headers.get(name)
    if existing is not None and existing != value:
        raise ValueError(f"header {name} already exists with a different value")
    headers[name] = value
    return headers


def _client_metadata(body: dict[str, Any]) -> dict[str, Any]:
    metadata = body.get("client_metadata")
    if metadata is None:
        return {}
    if not isinstance(metadata, dict):
        raise ValueError("client_metadata must be an object")
    return dict(metadata)


def _add_client_metadata(body: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(body)
    metadata = _client_metadata(body)
    for key, value in values.items():
        existing = metadata.get(key)
        if existing is None:
            metadata[key] = value
    body["client_metadata"] = metadata
    return body


def gateway_turn_id() -> str:
    return "turn_probe_anyrouter"


def gateway_turn_metadata() -> str:
    gateway_state = state()
    return json.dumps({
        "session_id": gateway_state["session_id"],
        "thread_id": gateway_state["thread_id"],
        "thread_source": "user",
        "turn_id": gateway_turn_id(),
        "workspaces": {},
        "sandbox": "seccomp",
        "turn_started_at_unix_ms": int(time.time() * 1000),
        "request_kind": "turn",
        "window_id": f"{gateway_state['thread_id']}:{gateway_state['window_generation']}",
    }, separators=(",", ":"))


def with_lite_header(headers: dict[str, str]) -> dict[str, str]:
    return _add_header(headers, "x-openai-internal-codex-responses-lite", "true")


def _codex_user_agent_for_version(user_agent: str, version: str) -> str:
    prefix = f"{main.ORIGINATOR}/"
    if not user_agent.startswith(prefix):
        return f"{main.ORIGINATOR}/{version} (Windows 10; x86_64) unknown"

    remainder = user_agent[len(prefix):]
    suffix_start = remainder.find(" ")
    if suffix_start == -1:
        return f"{prefix}{version}"
    return f"{prefix}{version}{remainder[suffix_start:]}"


def with_codex_client_version(headers: dict[str, str], version: str) -> dict[str, str]:
    headers = dict(headers)
    headers["version"] = version
    headers["User-Agent"] = _codex_user_agent_for_version(headers.get("User-Agent", ""), version)
    return headers


def with_stream_true(body: dict[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(body)
    body["stream"] = True
    return body


def with_lite_client_marker(body: dict[str, Any]) -> dict[str, Any]:
    return _add_client_metadata(body, {"ws_request_header_x_openai_internal_codex_responses_lite": "true"})


def with_body_session_thread(body: dict[str, Any]) -> dict[str, Any]:
    gateway_state = state()
    return _add_client_metadata(body, {
        "session_id": gateway_state["session_id"],
        "thread_id": gateway_state["thread_id"],
    })


def with_body_turn_id(body: dict[str, Any]) -> dict[str, Any]:
    return _add_client_metadata(body, {"turn_id": gateway_turn_id()})


def with_body_turn_metadata(body: dict[str, Any]) -> dict[str, Any]:
    return _add_client_metadata(body, {"x-codex-turn-metadata": gateway_turn_metadata()})


def with_body_session_turn_metadata(body: dict[str, Any]) -> dict[str, Any]:
    body = with_body_session_thread(body)
    body = with_body_turn_id(body)
    return with_body_turn_metadata(body)


def with_full_non_output_affecting_metadata(
    body: dict[str, Any],
    headers: dict[str, str],
) -> tuple[dict[str, Any], dict[str, str]]:
    body = with_lite_client_marker(body)
    body = with_body_session_turn_metadata(body)
    headers = with_lite_header(headers)
    return body, headers


def codex_exec_user_agent(version: str) -> str:
    return f"codex_exec/{version} (Debian 13.0.0; x86_64) tmux/3.5a (codex_exec; {version})"


def with_captured_codex_exec_headers(headers: dict[str, str], version: str) -> dict[str, str]:
    headers = dict(headers)
    headers["originator"] = "codex_exec"
    headers["User-Agent"] = codex_exec_user_agent(version)
    headers["x-codex-beta-features"] = "remote_compaction_v2"
    headers["x-openai-internal-codex-responses-lite"] = "true"
    for name in ("version", "session_id", "thread_id", "x-codex-installation-id"):
        headers.pop(name, None)
    return headers


def load_latest_captured_additional_tools(path: str | Path) -> list[dict[str, Any]] | None:
    source = Path(path)
    if not source.exists():
        return None

    latest: list[dict[str, Any]] | None = None
    for line in source.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        tools = event.get("additional_tools") if isinstance(event, dict) else None
        if isinstance(tools, list) and all(isinstance(tool, dict) for tool in tools):
            latest = copy.deepcopy(tools)
    return latest


def default_additional_tools() -> list[dict[str, Any]]:
    return [
        code_mode_exec_tool(),
        code_mode_wait_tool(),
        request_user_input_tool(),
        collaboration_namespace_tool(),
    ]


def additional_tools_subset(
    additional_tools: list[dict[str, Any]] | None,
    names: list[str],
) -> list[dict[str, Any]]:
    tools = additional_tools if additional_tools is not None else default_additional_tools()
    by_name = {
        tool.get("name"): tool
        for tool in tools
        if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    }
    missing = [name for name in names if name not in by_name]
    if missing:
        raise ValueError(f"captured additional tools missing: {', '.join(missing)}")
    return [copy.deepcopy(by_name[name]) for name in names]


def captured_additional_tools_item(additional_tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "type": "additional_tools",
        "role": "developer",
        "tools": copy.deepcopy(additional_tools) if additional_tools is not None else default_additional_tools(),
    }


def with_captured_official_lite_body(
    body: dict[str, Any],
    include_prompt_cache_key: bool = False,
    additional_tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    body = copy.deepcopy(body)
    input_items = list(body.get("input") or [])
    tools_item = captured_additional_tools_item(additional_tools)
    if input_items and isinstance(input_items[0], dict) and input_items[0].get("type") == "additional_tools":
        input_items[0] = {**input_items[0], "role": "developer", "tools": tools_item["tools"]}
    else:
        input_items.insert(0, tools_item)

    body["input"] = input_items
    body.pop("instructions", None)
    body.pop("tools", None)
    body.pop("max_output_tokens", None)
    body["stream"] = True
    body["store"] = False
    body["tool_choice"] = "auto"
    body["parallel_tool_calls"] = False
    body["include"] = [main.REQUIRED_RESPONSES_INCLUDE]
    body["reasoning"] = {"effort": "xhigh", "context": "all_turns"}
    body["text"] = {"verbosity": "low"}
    body["service_tier"] = "priority"
    if include_prompt_cache_key:
        body["prompt_cache_key"] = "probe-gpt-5.6-sol-codex-lite"
    else:
        body.pop("prompt_cache_key", None)
    return with_body_session_turn_metadata(body)


def with_uuid_prompt_cache_key(body: dict[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(body)
    body["prompt_cache_key"] = "123e4567-e89b-42d3-a456-426614174000"
    return body


def without_service_tier(body: dict[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(body)
    body.pop("service_tier", None)
    return body


def with_reasoning_effort(body: dict[str, Any], effort: str) -> dict[str, Any]:
    body = copy.deepcopy(body)
    reasoning = dict(body.get("reasoning") or {})
    reasoning["effort"] = effort
    reasoning.setdefault("context", "all_turns")
    body["reasoning"] = reasoning
    return body


def with_codex_body_min(body: dict[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(body)
    body.setdefault("instructions", "")
    body.setdefault("tools", [])
    body.setdefault("tool_choice", "auto")
    body.setdefault("parallel_tool_calls", False)
    body.setdefault("store", False)
    body.setdefault("stream", False)
    return body


def with_reasoning(body: dict[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(body)
    body.setdefault("reasoning", {"effort": "medium", "summary": "auto"})
    return body


def with_reasoning_all_turns(body: dict[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(body)
    reasoning = dict(body.get("reasoning") or {})
    reasoning.setdefault("effort", "medium")
    reasoning.setdefault("summary", "auto")
    reasoning["context"] = "all_turns"
    body["reasoning"] = reasoning
    return body


def with_lite_metadata(body: dict[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(body)
    metadata = dict(body.get("client_metadata") or {})
    metadata["ws_request_header_x_openai_internal_codex_responses_lite"] = "true"
    body["client_metadata"] = metadata
    return body


def with_empty_additional_tools(body: dict[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(body)
    input_items = list(body.get("input") or [])
    input_items.insert(0, {"type": "additional_tools", "role": "developer", "tools": []})
    body["input"] = input_items
    body["instructions"] = ""
    body["tools"] = []
    body["tool_choice"] = "auto"
    body["parallel_tool_calls"] = False
    return body


def code_mode_exec_tool() -> dict[str, Any]:
    return {
        "type": "custom",
        "name": "exec",
        "description": (
            "Run JavaScript code to orchestrate/compose tool calls\n"
            "- Evaluates the provided JavaScript code in a fresh V8 isolate as an async module.\n"
            "- All nested tools are available on the global `tools` object.\n"
            "- Runs raw JavaScript -- no Node, no file system, no network access, no console.\n"
            "- Accepts raw JavaScript source text, not JSON, quoted strings, or markdown code fences.\n"
            "- You may optionally start the tool input with a first-line pragma like "
            "`// @exec: {\"yield_time_ms\": 10000, \"max_output_tokens\": 1000}`."
        ),
        "format": {
            "type": "grammar",
            "syntax": "lark",
            "definition": r"""
start: pragma_source | plain_source
pragma_source: PRAGMA_LINE NEWLINE SOURCE
plain_source: SOURCE

PRAGMA_LINE: /[ \t]*\/\/ @exec:[^\r\n]*/
NEWLINE: /\r?\n/
SOURCE: /[\s\S]+/
""",
        },
    }


def code_mode_wait_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "wait",
        "description": (
            "Waits on a yielded `exec` cell and returns new output or completion.\n"
            "- Use `wait` only after `exec` returns `Script running with cell ID ...`.\n"
            "- `cell_id` identifies the running `exec` cell to resume.\n"
            "- `yield_time_ms` controls how long to wait for more output before yielding again. "
            "Defaults to 10000 ms.\n"
            "- `max_tokens` limits how much new output this wait call returns. Defaults to 10000 tokens.\n"
            "- `terminate: true` stops the running cell; false or omitted waits for output."
        ),
        "strict": False,
        "parameters": {
            "type": "object",
            "properties": {
                "cell_id": {
                    "type": "string",
                    "description": "Identifier of the running exec cell.",
                },
                "yield_time_ms": {
                    "type": "number",
                    "description": "Wait before yielding more output. Defaults to 10000 ms.",
                },
                "max_tokens": {
                    "type": "number",
                    "description": "Output token budget for this wait call. Defaults to 10000 tokens.",
                },
                "terminate": {
                    "type": "boolean",
                    "description": "True stops the running exec cell; false or omitted waits for output.",
                },
            },
            "required": ["cell_id"],
            "additionalProperties": False,
        },
    }


def request_user_input_tool() -> dict[str, Any]:
    return {
        "type": "function",
        "name": "request_user_input",
        "description": "Request user input for one to three short questions and wait for the response. This tool is only available in Plan mode.",
        "strict": False,
        "parameters": {
            "type": "object",
            "properties": {
                "questions": {
                    "type": "array",
                    "description": "Questions to show the user. Prefer 1 and do not exceed 3",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string", "description": "Stable identifier for mapping answers (snake_case)."},
                            "header": {"type": "string", "description": "Short header label shown in the UI (12 or fewer chars)."},
                            "question": {"type": "string", "description": "Single-sentence prompt shown to the user."},
                            "options": {
                                "type": "array",
                                "description": "Provide 2-3 mutually exclusive choices. Put the recommended option first and suffix its label with \"(Recommended)\". Do not include an \"Other\" option in this list; the client will add a free-form \"Other\" option automatically.",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string", "description": "User-facing label (1-5 words)."},
                                        "description": {"type": "string", "description": "One short sentence explaining impact/tradeoff if selected."},
                                    },
                                    "required": ["label", "description"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["id", "header", "question", "options"],
                        "additionalProperties": False,
                    },
                },
                "autoResolutionMs": {
                    "type": "number",
                    "description": "Optional timeout in milliseconds before the client auto-resolves the prompt.",
                }
            },
            "required": ["questions"],
            "additionalProperties": False,
        },
    }


def collaboration_namespace_tool() -> dict[str, Any]:
    def function_tool(name: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        parameters: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "additionalProperties": False,
        }
        if required:
            parameters["required"] = required
        return {
            "type": "function",
            "name": name,
            "description": f"Multi-agent v2 {name} tool.",
            "strict": False,
            "parameters": parameters,
        }

    return {
        "type": "namespace",
        "name": "collaboration",
        "description": "Tools for spawning and managing sub-agents.",
        "tools": [
            function_tool(
                "followup_task",
                {"target": {"type": "string"}, "message": {"type": "string"}},
                ["target", "message"],
            ),
            function_tool(
                "interrupt_agent",
                {"target": {"type": "string"}},
                ["target"],
            ),
            function_tool(
                "list_agents",
                {"path_prefix": {"type": "string"}},
                [],
            ),
            function_tool(
                "send_message",
                {"target": {"type": "string"}, "message": {"type": "string"}},
                ["target", "message"],
            ),
            function_tool(
                "spawn_agent",
                {
                    "task_name": {"type": "string"},
                    "message": {"type": "string"},
                    "fork_turns": {"type": "string"},
                },
                ["task_name", "message"],
            ),
            function_tool(
                "wait_agent",
                {"timeout_ms": {"type": "number"}},
                [],
            ),
        ],
    }


def with_real_lite_additional_tools(body: dict[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(body)
    input_items = list(body.get("input") or [])
    input_items.insert(
        0,
        {
            "type": "additional_tools",
            "role": "developer",
            "tools": [code_mode_exec_tool(), code_mode_wait_tool()],
        },
    )
    body["input"] = input_items
    body.pop("instructions", None)
    body.pop("tools", None)
    body["tool_choice"] = "auto"
    body["parallel_tool_calls"] = False
    return body


def with_full_lite_additional_tools(body: dict[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(body)
    input_items = list(body.get("input") or [])
    input_items.insert(
        0,
        {
            "type": "additional_tools",
            "role": "developer",
            "tools": [
                code_mode_exec_tool(),
                code_mode_wait_tool(),
                request_user_input_tool(),
                collaboration_namespace_tool(),
            ],
        },
    )
    body["input"] = input_items
    body.pop("instructions", None)
    body.pop("tools", None)
    body["tool_choice"] = "auto"
    body["parallel_tool_calls"] = False
    return body


def with_streaming(body: dict[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(body)
    body["stream"] = True
    return body


def with_codex_56_runtime_fields(body: dict[str, Any]) -> dict[str, Any]:
    body = copy.deepcopy(body)
    gateway_state = state()
    turn_id = "turn_probe_anyrouter"
    window_id = f"{gateway_state['thread_id']}:{gateway_state['window_generation']}"
    turn_metadata = {
        "installation_id": gateway_state["installation_id"],
        "session_id": gateway_state["session_id"],
        "thread_id": gateway_state["thread_id"],
        "turn_id": turn_id,
        "window_id": window_id,
        "request_kind": "turn",
        "thread_source": "user",
        "sandbox": "seccomp",
        "workspaces": {},
        "turn_started_at_unix_ms": int(time.time() * 1000),
    }

    metadata = dict(body.get("client_metadata") or {})
    metadata.setdefault("x-codex-installation-id", gateway_state["installation_id"])
    metadata["session_id"] = gateway_state["session_id"]
    metadata["thread_id"] = gateway_state["thread_id"]
    metadata["turn_id"] = turn_id
    metadata["x-codex-window-id"] = window_id
    metadata["x-codex-turn-metadata"] = json.dumps(turn_metadata, separators=(",", ":"))
    body["client_metadata"] = metadata
    body["reasoning"] = {"effort": "low", "context": "all_turns"}
    body["text"] = {"verbosity": "low"}
    return body


def with_codex_version(headers: dict[str, str], version: str) -> dict[str, str]:
    headers = dict(headers)
    headers["version"] = version
    headers["User-Agent"] = f"{main.ORIGINATOR}/{version} (Windows 10; x86_64)"
    return headers


def with_codex_144_terminal_user_agent(headers: dict[str, str]) -> dict[str, str]:
    headers = dict(headers)
    headers["version"] = "0.144.0"
    headers["User-Agent"] = f"{main.ORIGINATOR}/0.144.0 (Windows 10; x86_64) unknown"
    return headers


def summarize_response(response: httpx.Response) -> dict[str, Any]:
    text = response.text.strip()
    parsed: Any = None
    try:
        parsed = response.json()
    except json.JSONDecodeError:
        pass

    error = parsed.get("error") if isinstance(parsed, dict) else None
    if isinstance(error, dict):
        message = str(error.get("message") or error.get("error") or "")
        code = error.get("code")
        error_type = error.get("type") or (error.get("metadata") or {}).get("type")
    elif isinstance(parsed, dict) and "error" in parsed:
        message = str(parsed.get("error") or "")
        code = parsed.get("code")
        error_type = parsed.get("type")
    else:
        message = text[:300]
        code = None
        error_type = None

    lowered = message.lower()
    invalid_codex = any(marker in lowered for marker in INVALID_CODEX_MARKERS)
    capacity_limited = code == "get_channel_failed" or any(marker in lowered for marker in CAPACITY_LIMIT_MARKERS)
    return {
        "status": response.status_code,
        "invalid_codex": invalid_codex,
        "capacity_limited": capacity_limited,
        "code": code,
        "type": error_type,
        "message": message[:500],
        "request_id": response.headers.get("x-request-id") or response.headers.get("x-anyrouter-trace-id"),
    }


def summarize_models_payload(payload: Any) -> dict[str, Any]:
    models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        return {
            "model_count": None,
            "gpt_56_sol": None,
            "has_openai_prefixed_gpt_56_sol": False,
        }

    gpt_56_sol = None
    has_openai_prefixed = False
    for model in models:
        if not isinstance(model, dict):
            continue
        model_id = model.get("id") or model.get("slug")
        if model_id == "gpt-5.6-sol":
            gpt_56_sol = {
                key: model[key]
                for key in (
                    "id",
                    "slug",
                    "use_responses_lite",
                    "tool_mode",
                    "multi_agent_version",
                    "minimal_client_version",
                )
                if key in model
            }
        if model_id == "openai/gpt-5.6-sol":
            has_openai_prefixed = True

    return {
        "model_count": len(models),
        "gpt_56_sol": gpt_56_sol,
        "has_openai_prefixed_gpt_56_sol": has_openai_prefixed,
    }


def run_variant(
    client: httpx.Client,
    name: str,
    body: dict[str, Any],
    headers: dict[str, str],
) -> dict[str, Any]:
    started_at = time.perf_counter()
    try:
        response = client.post(ANYROUTER_URL, headers=headers, json=body)
        result = summarize_response(response)
    except httpx.HTTPError as exc:
        result = {
            "status": None,
            "invalid_codex": False,
            "capacity_limited": False,
            "code": "transport_error",
            "type": exc.__class__.__name__,
            "message": str(exc),
            "request_id": None,
        }
    result["variant"] = name
    result["elapsed_ms"] = int((time.perf_counter() - started_at) * 1000)
    return result


def request_fingerprint(body: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    metadata = body.get("client_metadata")
    input_items = body.get("input") if isinstance(body.get("input"), list) else []
    additional_tool_names: list[str] = []
    additional_tool_fingerprints: list[dict[str, Any]] = []
    for item in input_items:
        if not isinstance(item, dict) or item.get("type") != "additional_tools":
            continue
        tools = item.get("tools")
        if isinstance(tools, list):
            additional_tool_names.extend(
                str(tool.get("name"))
                for tool in tools
                if isinstance(tool, dict) and tool.get("name") is not None
            )
            additional_tool_fingerprints.extend(
                tool_schema_fingerprint(tool)
                for tool in tools
                if isinstance(tool, dict)
            )
    return {
        "body_keys": sorted(body.keys()),
        "header_keys": sorted(name for name in headers.keys() if name.lower() != "authorization"),
        "client_metadata_keys": sorted(metadata.keys()) if isinstance(metadata, dict) else [],
        "input_item_types": [str(item.get("type")) for item in input_items if isinstance(item, dict)],
        "additional_tool_names": additional_tool_names,
        "additional_tool_fingerprints": additional_tool_fingerprints,
        "stream": body.get("stream"),
        "include": body.get("include") if isinstance(body.get("include"), list) else None,
        "reasoning": body.get("reasoning") if isinstance(body.get("reasoning"), dict) else None,
        "text_keys": sorted(body["text"].keys()) if isinstance(body.get("text"), dict) else [],
        "version": headers.get("version"),
        "user_agent": headers.get("User-Agent"),
        "originator": headers.get("originator"),
        "has_lite_header": headers.get("x-openai-internal-codex-responses-lite") == "true",
        "has_tools": "tools" in body,
        "has_additional_tools": bool(additional_tool_names),
        "prompt_cache_key_shape": string_shape(body.get("prompt_cache_key")),
    }


def string_shape(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        return {"present": False}
    return {
        "present": True,
        "length": len(value),
        "sha256_12": hashlib.sha256(value.encode("utf-8")).hexdigest()[:12],
        "colon_count": value.count(":"),
        "dash_count": value.count("-"),
        "underscore_count": value.count("_"),
        "dot_count": value.count("."),
        "slash_count": value.count("/"),
        "first_segment_length": len(value.split(":", 1)[0]),
        "last_segment_length": len(value.rsplit(":", 1)[-1]),
        "has_uppercase": any(character.isupper() for character in value),
        "has_lowercase": any(character.islower() for character in value),
        "has_digit": any(character.isdigit() for character in value),
    }


def _identity_body(body: dict[str, Any]) -> dict[str, Any]:
    return body


def _identity_headers(headers: dict[str, str]) -> dict[str, str]:
    return headers


GatewayVariant = tuple[str, Callable[[dict[str, Any]], dict[str, Any]], Callable[[dict[str, str]], dict[str, str]]]


def _compose_body_transforms(*transforms: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def composed(body: dict[str, Any]) -> dict[str, Any]:
        for transform in transforms:
            body = transform(body)
        return body

    return composed


def _compose_header_transforms(*transforms: Callable[[dict[str, str]], dict[str, str]]) -> Callable[[dict[str, str]], dict[str, str]]:
    def composed(headers: dict[str, str]) -> dict[str, str]:
        for transform in transforms:
            headers = transform(headers)
        return headers

    return composed


def gateway_variants(
    codex_version: str = "0.144.2",
    additional_tools: list[dict[str, Any]] | None = None,
) -> list[GatewayVariant]:
    version_header = lambda h: with_codex_client_version(h, codex_version)
    captured_headers = lambda h: with_captured_codex_exec_headers(h, codex_version)
    captured_lite_body = lambda b: with_captured_official_lite_body(
        b,
        include_prompt_cache_key=False,
        additional_tools=additional_tools,
    )
    captured_lite_body_with_cache = lambda b: with_captured_official_lite_body(
        b,
        include_prompt_cache_key=True,
        additional_tools=additional_tools,
    )
    captured_lite_body_with_uuid_cache = lambda b: with_uuid_prompt_cache_key(captured_lite_body(b))
    captured_lite_body_with_tool_subset = lambda names: (
        lambda b: with_uuid_prompt_cache_key(
            with_captured_official_lite_body(
                b,
                include_prompt_cache_key=False,
                additional_tools=additional_tools_subset(additional_tools, names),
            )
        )
    )
    captured_lite_body_with_uuid_cache_no_tier = lambda b: without_service_tier(captured_lite_body_with_uuid_cache(b))
    captured_lite_body_with_uuid_cache_low_reasoning = lambda b: with_reasoning_effort(captured_lite_body_with_uuid_cache(b), "low")
    captured_lite_body_with_uuid_cache_low_reasoning_no_tier = lambda b: without_service_tier(captured_lite_body_with_uuid_cache_low_reasoning(b))
    captured_lite_body_with_cache_no_tier = lambda b: without_service_tier(captured_lite_body_with_cache(b))
    captured_lite_body_with_cache_low_reasoning = lambda b: with_reasoning_effort(captured_lite_body_with_cache(b), "low")
    captured_lite_body_with_cache_low_reasoning_no_tier = lambda b: without_service_tier(captured_lite_body_with_cache_low_reasoning(b))
    full_metadata_body = lambda b: with_body_session_turn_metadata(with_lite_client_marker(b))
    full_metadata_headers = with_lite_header
    return [
        ("gateway-baseline", _identity_body, _identity_headers),
        ("gateway-plus-lite-header", _identity_body, with_lite_header),
        ("gateway-plus-lite-client-marker", with_lite_client_marker, _identity_headers),
        ("gateway-plus-lite-both", with_lite_client_marker, with_lite_header),
        ("gateway-plus-body-session-thread", with_body_session_thread, _identity_headers),
        ("gateway-plus-body-turn-id", with_body_turn_id, _identity_headers),
        ("gateway-plus-body-turn-metadata", with_body_turn_metadata, _identity_headers),
        ("gateway-plus-body-session-turn-metadata", with_body_session_turn_metadata, _identity_headers),
        (
            "gateway-plus-full-non-output-affecting-metadata",
            full_metadata_body,
            full_metadata_headers,
        ),
        ("gateway-override-version", _identity_body, version_header),
        ("gateway-override-stream-true", with_stream_true, _identity_headers),
        ("gateway-override-version-stream-true", with_stream_true, version_header),
        (
            "gateway-override-version-plus-lite-both",
            with_lite_client_marker,
            _compose_header_transforms(version_header, with_lite_header),
        ),
        (
            "gateway-override-stream-true-plus-lite-both",
            _compose_body_transforms(with_stream_true, with_lite_client_marker),
            with_lite_header,
        ),
        (
            "gateway-override-version-stream-true-plus-lite-both",
            _compose_body_transforms(with_stream_true, with_lite_client_marker),
            _compose_header_transforms(version_header, with_lite_header),
        ),
        (
            "gateway-override-version-stream-true-plus-full-metadata",
            _compose_body_transforms(with_stream_true, full_metadata_body),
            _compose_header_transforms(version_header, full_metadata_headers),
        ),
        (
            "captured-official-lite-no-cache-key",
            captured_lite_body,
            captured_headers,
        ),
        (
            "captured-official-lite-with-cache-key",
            captured_lite_body_with_cache,
            captured_headers,
        ),
        (
            "captured-official-lite-with-uuid-cache-key",
            captured_lite_body_with_uuid_cache,
            captured_headers,
        ),
        (
            "captured-official-lite-with-uuid-cache-key-no-service-tier",
            captured_lite_body_with_uuid_cache_no_tier,
            captured_headers,
        ),
        (
            "captured-official-lite-with-uuid-cache-key-low-reasoning",
            captured_lite_body_with_uuid_cache_low_reasoning,
            captured_headers,
        ),
        (
            "captured-official-lite-with-uuid-cache-key-low-reasoning-no-service-tier",
            captured_lite_body_with_uuid_cache_low_reasoning_no_tier,
            captured_headers,
        ),
        (
            "captured-official-lite-with-cache-key-no-service-tier",
            captured_lite_body_with_cache_no_tier,
            captured_headers,
        ),
        (
            "captured-official-lite-with-cache-key-low-reasoning",
            captured_lite_body_with_cache_low_reasoning,
            captured_headers,
        ),
        (
            "captured-official-lite-with-cache-key-low-reasoning-no-service-tier",
            captured_lite_body_with_cache_low_reasoning_no_tier,
            captured_headers,
        ),
        (
            "captured-official-lite-tools-wait-only",
            captured_lite_body_with_tool_subset(["wait"]),
            captured_headers,
        ),
        (
            "captured-official-lite-tools-request-user-input-only",
            captured_lite_body_with_tool_subset(["request_user_input"]),
            captured_headers,
        ),
        (
            "captured-official-lite-tools-exec-only",
            captured_lite_body_with_tool_subset(["exec"]),
            captured_headers,
        ),
        (
            "captured-official-lite-tools-exec-wait",
            captured_lite_body_with_tool_subset(["exec", "wait"]),
            captured_headers,
        ),
        (
            "captured-official-lite-tools-exec-wait-request-user-input",
            captured_lite_body_with_tool_subset(["exec", "wait", "request_user_input"]),
            captured_headers,
        ),
        (
            "captured-official-lite-tools-exec-wait-collaboration",
            captured_lite_body_with_tool_subset(["exec", "wait", "collaboration"]),
            captured_headers,
        ),
        (
            "captured-official-lite-tools-full",
            captured_lite_body_with_tool_subset(["exec", "wait", "request_user_input", "collaboration"]),
            captured_headers,
        ),
    ]


def variants(input_text: str) -> list[tuple[str, str, Any, bool, bool, str | None]]:
    return [
        ("baseline-55-control", "gpt-5.5", lambda b: b, False, False, None),
        ("plain-56-no-codex", "gpt-5.6-sol", lambda b: plain_responses_body("gpt-5.6-sol", input_text), False, False, "plain"),
        ("baseline-56-current", "gpt-5.6-sol", lambda b: b, False, False, None),
        ("baseline-56-version-144", "gpt-5.6-sol", lambda b: b, False, False, "0.144.0"),
        ("codex-body-min", "gpt-5.6-sol", with_codex_body_min, False, False, None),
        ("codex-body-min-version-144", "gpt-5.6-sol", with_codex_body_min, False, False, "0.144.0"),
        ("codex-reasoning", "gpt-5.6-sol", lambda b: with_reasoning(with_codex_body_min(b)), False, False, None),
        ("codex-reasoning-all-turns", "gpt-5.6-sol", lambda b: with_reasoning_all_turns(with_codex_body_min(b)), False, False, None),
        ("codex-reasoning-all-turns-version-144", "gpt-5.6-sol", lambda b: with_reasoning_all_turns(with_codex_body_min(b)), False, False, "0.144.0"),
        ("lite-header-only", "gpt-5.6-sol", lambda b: with_reasoning_all_turns(with_codex_body_min(b)), True, False, None),
        ("lite-client-metadata-only", "gpt-5.6-sol", lambda b: with_lite_metadata(with_reasoning_all_turns(with_codex_body_min(b))), False, False, None),
        ("lite-both-markers", "gpt-5.6-sol", lambda b: with_lite_metadata(with_reasoning_all_turns(with_codex_body_min(b))), True, False, None),
        ("lite-both-markers-version-144", "gpt-5.6-sol", lambda b: with_lite_metadata(with_reasoning_all_turns(with_codex_body_min(b))), True, False, "0.144.0"),
        ("lite-additional-tools-empty", "gpt-5.6-sol", lambda b: with_empty_additional_tools(with_lite_metadata(with_reasoning_all_turns(with_codex_body_min(b)))), True, True, None),
        ("lite-additional-tools-empty-version-144", "gpt-5.6-sol", lambda b: with_empty_additional_tools(with_lite_metadata(with_reasoning_all_turns(with_codex_body_min(b)))), True, True, "0.144.0"),
        ("lite-additional-tools-real-min", "gpt-5.6-sol", lambda b: with_real_lite_additional_tools(with_lite_metadata(with_reasoning_all_turns(with_codex_body_min(b)))), True, True, None),
        ("lite-additional-tools-real-min-version-144", "gpt-5.6-sol", lambda b: with_real_lite_additional_tools(with_lite_metadata(with_reasoning_all_turns(with_codex_body_min(b)))), True, True, "0.144.0"),
        ("lite-additional-tools-full-version-144", "gpt-5.6-sol", lambda b: with_full_lite_additional_tools(with_lite_metadata(with_reasoning_all_turns(with_codex_body_min(b)))), True, True, "0.144.0"),
        ("lite-additional-tools-real-min-stream", "gpt-5.6-sol", lambda b: with_streaming(with_real_lite_additional_tools(with_lite_metadata(with_reasoning_all_turns(with_codex_body_min(b))))), True, True, None),
        ("lite-additional-tools-real-min-stream-version-144", "gpt-5.6-sol", lambda b: with_streaming(with_real_lite_additional_tools(with_lite_metadata(with_reasoning_all_turns(with_codex_body_min(b))))), True, True, "0.144.0"),
        ("lite-additional-tools-real-min-runtime", "gpt-5.6-sol", lambda b: with_codex_56_runtime_fields(with_real_lite_additional_tools(with_lite_metadata(with_reasoning_all_turns(with_codex_body_min(b))))), True, True, None),
        ("lite-additional-tools-real-min-runtime-version-144", "gpt-5.6-sol", lambda b: with_codex_56_runtime_fields(with_real_lite_additional_tools(with_lite_metadata(with_reasoning_all_turns(with_codex_body_min(b))))), True, True, "0.144.0"),
        ("lite-additional-tools-real-min-runtime-version-144-terminal-ua", "gpt-5.6-sol", lambda b: with_codex_56_runtime_fields(with_real_lite_additional_tools(with_lite_metadata(with_reasoning_all_turns(with_codex_body_min(b))))), True, True, "0.144.0-terminal-ua"),
    ]


def main_cli() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--include-additional-tools-empty", action="store_true")
    parser.add_argument("--include-tool-variants", action="store_true")
    parser.add_argument("--input-text")
    parser.add_argument("--probe-codex-version", default="0.144.2")
    parser.add_argument("--variant-prefix", action="append", default=[])
    parser.add_argument("--captured-additional-tools-jsonl", default=str(DEFAULT_CAPTURED_ADDITIONAL_TOOLS_PATH))
    parser.add_argument("--request-timeout", type=float, default=90.0)
    parser.add_argument("--models", action="store_true")
    args = parser.parse_args()

    api_key = load_api_key()
    if args.models:
        headers = plain_headers(api_key)
        with httpx.Client(timeout=httpx.Timeout(20.0, connect=10.0)) as client:
            response = client.get("https://anyrouter.top/v1/models", headers=headers)
        payload = response.json()
        print(json.dumps({
            "variant": "models",
            "status": response.status_code,
            **summarize_models_payload(payload),
        }, ensure_ascii=False), flush=True)
        return

    if args.input_text is None:
        raise SystemExit("--input-text is required for live response probes")

    timeout = httpx.Timeout(args.request_timeout, connect=10.0)
    results = []
    additional_tools = load_latest_captured_additional_tools(args.captured_additional_tools_jsonl)
    with httpx.Client(timeout=timeout) as client:
        if args.include_tool_variants:
            headers = base_headers(api_key)
            for name, model, transform, use_lite_header, is_additional_tools, codex_version in variants(args.input_text):
                if args.variant_prefix and not any(name.startswith(prefix) for prefix in args.variant_prefix):
                    continue
                if is_additional_tools and not args.include_additional_tools_empty:
                    continue
                body = transform(base_body(model, args.input_text))
                variant_headers = dict(headers)
                if codex_version is not None:
                    if codex_version == "plain":
                        variant_headers = plain_headers(api_key)
                    elif codex_version == "0.144.0-terminal-ua":
                        variant_headers = with_codex_144_terminal_user_agent(variant_headers)
                    else:
                        variant_headers = with_codex_version(variant_headers, codex_version)
                if use_lite_header:
                    variant_headers["x-openai-internal-codex-responses-lite"] = "true"
                result = run_variant(client, name, body, variant_headers)
                result["fingerprint"] = request_fingerprint(body, variant_headers)
                results.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
        else:
            baseline_body = gateway_baseline_body("gpt-5.6-sol", args.input_text)
            baseline_headers = gateway_baseline_headers(api_key)
            for name, body_transform, headers_transform in gateway_variants(args.probe_codex_version, additional_tools):
                if args.variant_prefix and not any(name.startswith(prefix) for prefix in args.variant_prefix):
                    continue
                body = body_transform(baseline_body)
                variant_headers = headers_transform(baseline_headers)
                result = run_variant(client, name, body, variant_headers)
                result["fingerprint"] = request_fingerprint(body, variant_headers)
                results.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)

    non_invalid = [item for item in results if item["status"] is not None and not item["invalid_codex"]]
    usable = [
        item
        for item in non_invalid
        if isinstance(item.get("status"), int)
        and 200 <= item["status"] < 300
        and not item.get("capacity_limited")
    ]
    if non_invalid:
        print("FIRST_NON_INVALID_CODEX=" + non_invalid[0]["variant"], flush=True)
    else:
        print("FIRST_NON_INVALID_CODEX=none", flush=True)
    if usable:
        print("FIRST_USABLE_RESPONSE=" + usable[0]["variant"], flush=True)
    else:
        print("FIRST_USABLE_RESPONSE=none", flush=True)


if __name__ == "__main__":
    main_cli()
