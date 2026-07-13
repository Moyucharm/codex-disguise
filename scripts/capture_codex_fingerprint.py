import argparse
import base64
import copy
import hashlib
import json
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit


SENSITIVE_HEADERS = {
    "authorization",
    "cookie",
    "set-cookie",
    "chatgpt-account-id",
    "openai-organization",
    "openai-project",
}

HEADER_VALUE_ALLOWLIST = {
    "accept",
    "content-type",
    "openai-beta",
    "originator",
    "user-agent",
    "version",
    "x-codex-beta-features",
    "x-openai-internal-codex-responses-lite",
}

_CODEX_MODEL_CATALOG: dict[str, Any] | None = None


def codex_model_catalog() -> dict[str, Any]:
    global _CODEX_MODEL_CATALOG
    if _CODEX_MODEL_CATALOG is not None:
        return _CODEX_MODEL_CATALOG

    try:
        output = subprocess.check_output(["codex", "debug", "models", "--bundled"], text=True, timeout=10)
        payload = json.loads(output)
        models = payload.get("models") if isinstance(payload, dict) else None
        if isinstance(models, list):
            selected = [model for model in models if isinstance(model, dict) and model.get("slug") == "gpt-5.6-sol"]
            if selected:
                _CODEX_MODEL_CATALOG = {"models": selected}
                return _CODEX_MODEL_CATALOG
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        pass

    _CODEX_MODEL_CATALOG = fallback_codex_model_catalog()
    return _CODEX_MODEL_CATALOG


def fallback_codex_model_catalog() -> dict[str, Any]:
    return {
        "models": [
            {
                "slug": "gpt-5.6-sol",
                "display_name": "GPT-5.6-Sol",
                "description": "Latest frontier agentic coding model.",
                "default_reasoning_level": "low",
                "tool_mode": "code_mode_only",
                "multi_agent_version": "v2",
                "use_responses_lite": True,
                "supported_reasoning_levels": [
                    {"effort": "low", "description": "Fast responses with lighter reasoning"},
                    {"effort": "medium", "description": "Balances speed and reasoning depth for everyday tasks"},
                    {"effort": "high", "description": "Greater reasoning depth for complex problems"},
                    {"effort": "xhigh", "description": "Extra high reasoning depth for complex problems"},
                    {"effort": "max", "description": "Maximum reasoning depth for the hardest problems"},
                    {"effort": "ultra", "description": "Maximum reasoning with automatic task delegation"},
                ],
                "shell_type": "shell_command",
                "visibility": "list",
                "supported_in_api": True,
                "priority": 1,
                "additional_speed_tiers": ["fast"],
                "service_tiers": [
                    {"id": "priority", "name": "Fast", "description": "1.5x speed, increased usage"}
                ],
                "availability_nux": None,
                "upgrade": None,
                "base_instructions": "You are Codex, an agent based on GPT-5.",
            }
        ]
    }


def safe_headers(headers: Any) -> dict[str, Any]:
    header_map = {name.lower(): value for name, value in headers.items()}
    return {
        "keys": sorted(name for name in header_map if name not in SENSITIVE_HEADERS),
        "values": {
            name: header_map[name]
            for name in sorted(HEADER_VALUE_ALLOWLIST)
            if name in header_map
        },
        "has_authorization": "authorization" in header_map,
        "has_chatgpt_account_id": "chatgpt-account-id" in header_map,
    }


def _safe_json_loads(data: bytes) -> Any:
    if not data:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _input_fingerprint(input_value: Any) -> dict[str, Any]:
    if not isinstance(input_value, list):
        return {"kind": type(input_value).__name__}

    item_types = []
    roles = []
    content_types = []
    additional_tool_names = []
    additional_tool_fingerprints = []
    for item in input_value:
        if not isinstance(item, dict):
            item_types.append(type(item).__name__)
            continue
        item_types.append(str(item.get("type")))
        if item.get("role") is not None:
            roles.append(str(item.get("role")))
        if item.get("type") == "additional_tools":
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
        content = item.get("content")
        if isinstance(content, list):
            content_types.extend(
                str(part.get("type"))
                for part in content
                if isinstance(part, dict) and part.get("type") is not None
            )

    return {
        "kind": "list",
        "count": len(input_value),
        "item_types": item_types,
        "roles": roles,
        "content_types": content_types,
        "additional_tool_names": additional_tool_names,
        "additional_tool_fingerprints": additional_tool_fingerprints,
    }


def _description_shape(value: Any) -> dict[str, Any]:
    if not isinstance(value, str):
        return {"present": False}
    return {
        "present": True,
        "length": len(value),
        "sha256_12": hashlib.sha256(value.encode("utf-8")).hexdigest()[:12],
    }


def _schema_hash(value: Any) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:12]


def _parameters_fingerprint(parameters: Any) -> dict[str, Any]:
    if not isinstance(parameters, dict):
        return {"present": False}
    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        properties = {}
    return {
        "present": True,
        "keys": sorted(parameters.keys()),
        "required": parameters.get("required") if isinstance(parameters.get("required"), list) else [],
        "additional_properties": parameters.get("additionalProperties"),
        "property_keys": sorted(properties.keys()),
        "property_types": {
            key: value.get("type")
            for key, value in sorted(properties.items())
            if isinstance(value, dict)
        },
    }


def tool_schema_fingerprint(tool: dict[str, Any]) -> dict[str, Any]:
    format_value = tool.get("format") if isinstance(tool.get("format"), dict) else {}
    namespace_tools = tool.get("tools") if isinstance(tool.get("tools"), list) else []
    return {
        "name": tool.get("name"),
        "type": tool.get("type"),
        "keys": sorted(tool.keys()),
        "strict": tool.get("strict"),
        "description_shape": _description_shape(tool.get("description")),
        "schema_hash": _schema_hash(tool),
        "format_type": format_value.get("type"),
        "format_syntax": format_value.get("syntax"),
        "format_definition_shape": _description_shape(format_value.get("definition")),
        "parameters": _parameters_fingerprint(tool.get("parameters")),
        "namespace_tool_names": [
            child.get("name")
            for child in namespace_tools
            if isinstance(child, dict) and child.get("name") is not None
        ],
        "namespace_tool_fingerprints": [
            {
                "name": child.get("name"),
                "type": child.get("type"),
                "keys": sorted(child.keys()),
                "strict": child.get("strict"),
                "parameters": _parameters_fingerprint(child.get("parameters")),
                "schema_hash": _schema_hash(child),
            }
            for child in namespace_tools
            if isinstance(child, dict)
        ],
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


def body_fingerprint(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"json_type": type(payload).__name__}

    metadata = payload.get("client_metadata")
    tools = payload.get("tools")
    reasoning = payload.get("reasoning")
    text = payload.get("text")
    return {
        "json_type": "object",
        "body_keys": sorted(payload.keys()),
        "model": payload.get("model"),
        "stream": payload.get("stream"),
        "store": payload.get("store"),
        "tool_choice": payload.get("tool_choice"),
        "parallel_tool_calls": payload.get("parallel_tool_calls"),
        "include": payload.get("include") if isinstance(payload.get("include"), list) else None,
        "reasoning": reasoning if isinstance(reasoning, dict) else None,
        "text_keys": sorted(text.keys()) if isinstance(text, dict) else [],
        "client_metadata_keys": sorted(metadata.keys()) if isinstance(metadata, dict) else [],
        "tools_count": len(tools) if isinstance(tools, list) else None,
        "tool_names": [
            str(tool.get("name"))
            for tool in tools
            if isinstance(tool, dict) and tool.get("name") is not None
        ] if isinstance(tools, list) else [],
        "prompt_cache_key_shape": string_shape(payload.get("prompt_cache_key")),
        "input": _input_fingerprint(payload.get("input")),
    }


def additional_tools_schema(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    input_value = payload.get("input")
    if not isinstance(input_value, list):
        return []
    for item in input_value:
        if not isinstance(item, dict) or item.get("type") != "additional_tools":
            continue
        tools = item.get("tools")
        if isinstance(tools, list):
            return [copy.deepcopy(tool) for tool in tools if isinstance(tool, dict)]
    return []


def additional_tools_capture_event(method: str, target: str, body: bytes) -> dict[str, Any] | None:
    payload = _safe_json_loads(body)
    tools = additional_tools_schema(payload)
    if not tools:
        return None
    parsed_url = urlsplit(target)
    return {
        "captured_at_unix_ms": int(time.time() * 1000),
        "method": method,
        "path": parsed_url.path,
        "additional_tools": tools,
    }


def request_fingerprint(method: str, target: str, headers: Any, body: bytes) -> dict[str, Any]:
    parsed_url = urlsplit(target)
    payload = _safe_json_loads(body)
    return {
        "captured_at_unix_ms": int(time.time() * 1000),
        "method": method,
        "path": parsed_url.path,
        "query_keys": sorted(parse_qs(parsed_url.query).keys()),
        "headers": safe_headers(headers),
        "body": body_fingerprint(payload),
    }


def websocket_accept_key(key: str) -> str:
    digest = hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def recv_websocket_frame(sock: socket.socket) -> tuple[int, bytes] | None:
    header = recv_exact(sock, 2)
    if len(header) < 2:
        return None
    opcode = header[0] & 0x0F
    masked = bool(header[1] & 0x80)
    length = header[1] & 0x7F
    if length == 126:
        length = int.from_bytes(recv_exact(sock, 2), "big")
    elif length == 127:
        length = int.from_bytes(recv_exact(sock, 8), "big")
    mask = recv_exact(sock, 4) if masked else b""
    payload = recv_exact(sock, length)
    if masked and len(mask) == 4:
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return opcode, payload


def make_handler(events: list[dict[str, Any]], output_path: str | None, additional_tools_output_path: str | None):
    lock = threading.Lock()

    def record(event: dict[str, Any]) -> None:
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with lock:
            events.append(event)
            if output_path:
                with open(output_path, "a", encoding="utf-8") as output_file:
                    output_file.write(line + "\n")
        print(line, flush=True)

    def record_additional_tools(event: dict[str, Any]) -> None:
        if not additional_tools_output_path:
            return
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with lock:
            with open(additional_tools_output_path, "a", encoding="utf-8") as output_file:
                output_file.write(line + "\n")

    class CaptureHandler(BaseHTTPRequestHandler):
        server_version = "codex-fingerprint-capture/1.0"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def do_GET(self) -> None:
            if self.headers.get("Upgrade", "").lower() == "websocket":
                self._handle_websocket()
                return
            self._record_http_request(b"")
            if self.path.startswith("/v1/models"):
                catalog = codex_model_catalog()
                self._send_json(200, {**catalog, "object": "list", "data": [{"id": "gpt-5.6-sol", "object": "model"}]})
            elif self.path.startswith("/backend-api/codex/models") or self.path.startswith("/backend-api/models"):
                self._send_json(200, codex_model_catalog())
            else:
                self._send_json(404, {"error": {"message": "captured", "code": "captured"}})

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            self._record_http_request(body)
            self._send_json(418, {"error": {"message": "captured", "code": "captured"}})

        def _record_http_request(self, body: bytes) -> None:
            record({"transport": "http", **request_fingerprint(self.command, self.path, self.headers, body)})
            event = additional_tools_capture_event(self.command, self.path, body)
            if event is not None:
                record_additional_tools({"transport": "http", **event})

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle_websocket(self) -> None:
            self._record_http_request(b"")
            key = self.headers.get("Sec-WebSocket-Key")
            if not key:
                self.send_error(400)
                return
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", websocket_accept_key(key))
            self.end_headers()
            self.connection.settimeout(5.0)
            for index in range(8):
                try:
                    frame = recv_websocket_frame(self.connection)
                except (OSError, TimeoutError):
                    break
                if frame is None:
                    break
                opcode, payload = frame
                if opcode == 0x8:
                    break
                if opcode in {0x1, 0x2}:
                    event = additional_tools_capture_event("WEBSOCKET", self.path, payload)
                    if event is not None:
                        record_additional_tools({"transport": "websocket-frame", "frame_index": index, **event})
                    record({
                        "transport": "websocket-frame",
                        "frame_index": index,
                        "opcode": opcode,
                        "body": body_fingerprint(_safe_json_loads(payload)),
                    })

    return CaptureHandler


def run_server(
    host: str,
    port: int,
    output_path: str | None,
    additional_tools_output_path: str | None,
    timeout: float | None,
    command: list[str],
) -> int:
    events: list[dict[str, Any]] = []
    server = ThreadingHTTPServer((host, port), make_handler(events, output_path, additional_tools_output_path))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    print(json.dumps({"event": "listening", "url": f"http://{host}:{port}"}), flush=True)

    return_code = 0
    try:
        if command:
            process = subprocess.run(command, check=False)
            return_code = process.returncode
        else:
            deadline = time.time() + timeout if timeout is not None else None
            while deadline is None or time.time() < deadline:
                time.sleep(0.2)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)

    print(json.dumps({"event": "stopped", "captured_events": len(events), "command_returncode": return_code}), flush=True)
    return return_code


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--out")
    parser.add_argument("--out-additional-tools")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    raise SystemExit(run_server(args.host, args.port, args.out, args.out_additional_tools, args.timeout, command))


if __name__ == "__main__":
    main()
