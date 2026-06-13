import json
import os
import platform
import random
import secrets
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, NamedTuple

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
DB_PATH = DATA_DIR / "gateway.db"
LEGACY_STATE_PATH = DATA_DIR / "state.json"
ENV_PATH = APP_DIR / ".env"
MANAGEMENT_HTML_PATH = APP_DIR / "management.html"

DEFAULT_UPSTREAM_URL = "https://new.sharedchat.cc/codex/v1"
TIMEOUT = httpx.Timeout(300.0, connect=10.0)
CHANNEL_TEST_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

ORIGINATOR = "codex_cli_rs"
CODEX_VERSION = "0.139.0"
TERMINAL_UA = ""
FAILURE_THRESHOLD = 3
COOLDOWN_SECONDS = 300
STATS_WINDOW_SECONDS = 24 * 60 * 60
DB_BUSY_TIMEOUT_MS = 5000

TRACE_RESPONSE_HEADERS = (
    "x-request-id",
    "x-oai-request-id",
    "cf-ray",
    "x-codex-active-limit",
    "x-openai-authorization-error",
)

PASSTHROUGH_REQUEST_HEADERS = (
    "x-oai-attestation",
    "x-openai-subagent",
    "x-codex-parent-thread-id",
    "x-codex-turn-metadata",
    "x-codex-turn-state",
)

METADATA_HEADER_KEYS = (
    "x-openai-subagent",
    "x-codex-parent-thread-id",
    "x-codex-turn-metadata",
)

REQUIRED_RESPONSES_INCLUDE = "reasoning.encrypted_content"

RETRYABLE_STATUS_CODES = {401, 403, 408, 409, 429, 500, 502, 503, 504}

MODELS = [
    {"id": "gpt-5.5", "object": "model", "created": 1700000000, "owned_by": "openai"},
    {"id": "gpt-5.4", "object": "model", "created": 1700000000, "owned_by": "openai"},
    {"id": "gpt-5.3-codex", "object": "model", "created": 1700000000, "owned_by": "openai"},
]


class GatewayError(Exception):
    def __init__(
        self,
        message: str,
        status_code: int = 400,
        code: str | None = None,
        error_type: str = "invalid_request_error",
        param: str | None = None,
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code
        self.error_type = error_type
        self.param = param


class UpstreamResult(NamedTuple):
    response: httpx.Response
    channel: dict[str, Any]
    failover_count: int


class StreamResult(NamedTuple):
    stream_context: Any
    response: httpx.Response
    channel: dict[str, Any]
    failover_count: int
    started_at: float


class SecretValue(NamedTuple):
    value: str
    source: str


def _now_ts() -> float:
    return time.time()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ts_to_iso(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _new_prefixed_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _sanitize_header_value(value: Any) -> str:
    text = str(value)
    return "".join(char if char.isascii() and 32 <= ord(char) < 127 else "_" for char in text)


def _sanitize_user_agent(value: str) -> str:
    return "".join(
        char if char.isascii() and (char.isalnum() or char in "-_ ./;()") else "_"
        for char in value
    )


def _codex_user_agent() -> str:
    return f"{ORIGINATOR}/{CODEX_VERSION} (Windows 10; x86_64)"


def _connect_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=DB_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
    return conn


def _read_legacy_state() -> dict[str, Any]:
    if not LEGACY_STATE_PATH.exists():
        return {}
    try:
        data = json.loads(LEGACY_STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _new_gateway_state() -> dict[str, Any]:
    return {
        "installation_id": str(uuid.uuid4()),
        "session_id": _new_prefixed_id("sess"),
        "thread_id": _new_prefixed_id("thread"),
        "window_generation": 0,
        "created_at": _utc_now(),
    }


def _state_from_rows(rows: list[sqlite3.Row]) -> dict[str, Any]:
    raw = {row["key"]: row["value"] for row in rows}
    return {
        "installation_id": raw["installation_id"],
        "session_id": raw["session_id"],
        "thread_id": raw["thread_id"],
        "window_generation": int(raw.get("window_generation") or 0),
        "created_at": raw["created_at"],
    }


def _get_gateway_state(conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    owns_conn = conn is None
    if conn is None:
        conn = _connect_db()
    try:
        rows = conn.execute("SELECT key, value FROM gateway_state").fetchall()
        state = _state_from_rows(rows)
    finally:
        if owns_conn:
            conn.close()
    return state


def _upsert_gateway_state(conn: sqlite3.Connection, state: dict[str, Any]) -> None:
    now = _utc_now()
    for key, value in state.items():
        conn.execute(
            """
            INSERT INTO gateway_state(key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (key, str(value), now),
        )


def _ensure_gateway_state(conn: sqlite3.Connection) -> None:
    existing_rows = conn.execute("SELECT key, value FROM gateway_state").fetchall()
    existing = {row["key"]: row["value"] for row in existing_rows}
    legacy = _read_legacy_state()
    defaults = _new_gateway_state()

    state = {
        "installation_id": existing.get("installation_id") or legacy.get("installation_id") or defaults["installation_id"],
        "session_id": existing.get("session_id") or legacy.get("session_id") or defaults["session_id"],
        "thread_id": existing.get("thread_id") or legacy.get("thread_id") or defaults["thread_id"],
        "window_generation": existing.get("window_generation") or legacy.get("window_generation") or 0,
        "created_at": existing.get("created_at") or legacy.get("created_at") or defaults["created_at"],
    }
    _upsert_gateway_state(conn, state)


def _reset_gateway_state() -> dict[str, Any]:
    with _connect_db() as conn:
        state = _new_gateway_state()
        _upsert_gateway_state(conn, state)
        conn.commit()
        return _get_gateway_state(conn)


def _load_dotenv(path: Path = ENV_PATH) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _ensure_secret(name: str, dotenv: dict[str, str]) -> SecretValue:
    value = os.environ.get(name)
    if value and value.strip():
        return SecretValue(value.strip(), "env")

    value = dotenv.get(name)
    if value and value.strip():
        return SecretValue(value.strip(), "dotenv")

    raise RuntimeError(f"{name} must be set in the environment or {ENV_PATH}")


def _ensure_admin_token(dotenv: dict[str, str]) -> SecretValue:
    return _ensure_secret("ADMIN_TOKEN", dotenv)


def _ensure_client_api_key(dotenv: dict[str, str]) -> SecretValue:
    return _ensure_secret("CLIENT_API_KEY", dotenv)


def _init_db() -> None:
    with _connect_db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS gateway_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS channels (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                upstream_url TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                upstream_api_key TEXT,
                downstream_api_key TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS channel_runtime (
                channel_id TEXT PRIMARY KEY,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                cooldown_until REAL,
                last_success_at REAL,
                last_failure_at REAL,
                updated_at REAL NOT NULL,
                FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS channel_events (
                id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                success INTEGER NOT NULL,
                status_code INTEGER,
                error_code TEXT,
                latency_ms INTEGER NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY(channel_id) REFERENCES channels(id) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_channel_events_channel_created
            ON channel_events(channel_id, created_at);
            """
        )
        channel_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(channels)").fetchall()
        }
        if "upstream_api_key" not in channel_columns:
            conn.execute("ALTER TABLE channels ADD COLUMN upstream_api_key TEXT")
            conn.execute("UPDATE channels SET upstream_api_key = downstream_api_key WHERE downstream_api_key IS NOT NULL")
            conn.execute("UPDATE channels SET downstream_api_key = NULL WHERE downstream_api_key IS NOT NULL")
        if "supported_models" not in channel_columns:
            conn.execute("ALTER TABLE channels ADD COLUMN supported_models TEXT")
        if "proxy_url" not in channel_columns:
            conn.execute("ALTER TABLE channels ADD COLUMN proxy_url TEXT")
        _ensure_gateway_state(conn)
        channel_count = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        if channel_count == 0:
            channel_id = _new_prefixed_id("ch")
            now = _utc_now()
            conn.execute(
                """
                INSERT INTO channels(id, name, upstream_url, priority, enabled, upstream_api_key, downstream_api_key, supported_models, proxy_url, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (channel_id, "default", DEFAULT_UPSTREAM_URL, 0, 1, None, None, None, None, now, now),
            )
            conn.execute(
                """
                INSERT INTO channel_runtime(channel_id, consecutive_failures, cooldown_until, updated_at)
                VALUES (?, 0, NULL, ?)
                """,
                (channel_id, _now_ts()),
            )
        conn.commit()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _init_db()
    dotenv = _load_dotenv()
    admin_token = _ensure_admin_token(dotenv)
    client_api_key = _ensure_client_api_key(dotenv)
    app.state.admin_token = admin_token.value
    app.state.admin_token_source = admin_token.source
    app.state.client_api_key = client_api_key.value
    app.state.client_api_key_source = client_api_key.source
    app.state.http_client = httpx.AsyncClient(timeout=TIMEOUT)
    app.state.proxy_clients = {}
    try:
        yield
    finally:
        await app.state.http_client.aclose()
        for proxy_client in app.state.proxy_clients.values():
            await proxy_client.aclose()


app = FastAPI(
    title="codex-disguise",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.exception_handler(GatewayError)
async def gateway_error_handler(_: Request, exc: GatewayError) -> JSONResponse:
    return _gateway_error_response(exc)


def _window_id(state: dict[str, Any]) -> str:
    return f"{state['thread_id']}:{state['window_generation']}"


def _public_state(state: dict[str, Any]) -> dict[str, Any]:
    return {
        "installation_id": state["installation_id"],
        "session_id": state["session_id"],
        "thread_id": state["thread_id"],
        "window_generation": state["window_generation"],
        "window_id": _window_id(state),
        "created_at": state["created_at"],
    }


def _trace_headers(response: httpx.Response) -> dict[str, str]:
    return {
        name: value
        for name in TRACE_RESPONSE_HEADERS
        if (value := response.headers.get(name)) is not None
    }


def _channel_response_headers(channel: dict[str, Any], failover_count: int) -> dict[str, str]:
    return {
        "x-codex-disguise-channel-id": _sanitize_header_value(channel["id"]),
        "x-codex-disguise-channel-name": _sanitize_header_value(channel["name"]),
        "x-codex-disguise-failover-count": str(failover_count),
    }


def _merge_headers(*headers: dict[str, str] | None) -> dict[str, str]:
    merged: dict[str, str] = {}
    for header_set in headers:
        if header_set:
            merged.update(header_set)
    return merged


def _error_response(
    message: str,
    status_code: int,
    code: str | None = None,
    error_type: str = "invalid_request_error",
    param: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        headers=headers,
        content={
            "error": {
                "message": message,
                "type": error_type,
                "param": param,
                "code": code,
            }
        },
    )


def _config_payload(request: Request, include_private: bool) -> dict[str, Any]:
    with _connect_db() as conn:
        state = _get_gateway_state(conn) if include_private else None
        channel_count = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        enabled_channel_count = conn.execute("SELECT COUNT(*) FROM channels WHERE enabled = 1").fetchone()[0]

    payload = {
        "originator": ORIGINATOR,
        "version": CODEX_VERSION,
        "user_agent": _codex_user_agent(),
        "authorization": "client_api_key_or_channel_downstream_api_key",
        "failure_threshold": FAILURE_THRESHOLD,
        "cooldown_seconds": COOLDOWN_SECONDS,
        "channel_count": channel_count,
        "enabled_channel_count": enabled_channel_count,
    }
    if include_private:
        assert state is not None
        payload.update(
            {
                "database_path": str(DB_PATH),
                "admin_token_source": request.app.state.admin_token_source,
                "client_api_key_source": request.app.state.client_api_key_source,
                "state": _public_state(state),
            }
        )
    return payload


def _gateway_error_response(error: GatewayError) -> JSONResponse:
    return _error_response(
        error.message,
        status_code=error.status_code,
        code=error.code,
        error_type=error.error_type,
        param=error.param,
    )


async def _read_json_body(request: Request) -> dict[str, Any]:
    raw_body = await request.body()
    if not raw_body:
        return {}
    try:
        body = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise GatewayError("Request body is not valid JSON", 400, "invalid_json") from exc
    if not isinstance(body, dict):
        raise GatewayError("Request body must be a JSON object", 400, "invalid_request_body")
    return body


def _require_admin(request: Request) -> None:
    expected = f"Bearer {request.app.state.admin_token}"
    authorization = request.headers.get("Authorization", "")
    if not secrets.compare_digest(authorization, expected):
        raise GatewayError(
            "Missing or invalid admin token",
            status_code=401,
            code="invalid_admin_token",
            error_type="authentication_error",
        )


def _format_bearer_token(value: str) -> str:
    token = value.strip()
    if token.startswith("Bearer "):
        return token
    return f"Bearer {token}"


def _request_bearer_token(request: Request) -> str:
    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer "):
        token = authorization.removeprefix("Bearer ").strip()
        if token:
            return token

    raise GatewayError(
        "Missing Authorization bearer token",
        status_code=401,
        code="missing_api_key",
        error_type="authentication_error",
    )


def _channel_accepts_client_key(channel: dict[str, Any], token: str) -> bool:
    channel_key = channel.get("downstream_api_key")
    if isinstance(channel_key, str) and channel_key.strip():
        return secrets.compare_digest(token, channel_key.strip())
    return False


def _upstream_authorization_header(channel: dict[str, Any]) -> str:
    channel_key = channel.get("upstream_api_key")
    if isinstance(channel_key, str) and channel_key.strip():
        return _format_bearer_token(channel_key)

    raise GatewayError(
        "Missing channel upstream_api_key",
        status_code=401,
        code="missing_upstream_api_key",
        error_type="authentication_error",
    )


def _upstream_responses_url(channel: dict[str, Any]) -> str:
    upstream_url = str(channel["upstream_url"]).rstrip("/")
    if upstream_url.endswith("/responses"):
        return upstream_url
    return f"{upstream_url}/responses"


def _client_for_channel(request: Request, channel: dict[str, Any]) -> httpx.AsyncClient:
    """返回用于该渠道的 HTTP 客户端。

    无 proxy_url 时复用全局客户端；配置了代理时按代理地址懒加载并缓存
    一个绑定该代理的客户端（httpx 仅支持创建客户端时绑定代理，不支持按请求设置，
    且 proxy= 参数需 httpx>=0.26）。
    事件循环为单线程，check-and-set 之间没有 await，无并发竞争。
    """
    proxy = channel.get("proxy_url")
    if not isinstance(proxy, str) or not proxy.strip():
        return request.app.state.http_client

    proxy = proxy.strip()
    clients: dict[str, httpx.AsyncClient] = request.app.state.proxy_clients
    client = clients.get(proxy)
    if client is None:
        client = httpx.AsyncClient(timeout=TIMEOUT, proxy=proxy)
        clients[proxy] = client
    return client


def _codex_headers(request: Request, state: dict[str, Any], channel: dict[str, Any]) -> dict[str, str]:
    turn_metadata = json.dumps({
        "session_id": state["session_id"],
        "thread_id": state["thread_id"],
        "thread_source": "user",
        "turn_id": _new_prefixed_id("turn"),
        "workspaces": {},
        "sandbox": "seccomp",
        "turn_started_at_unix_ms": int(_now_ts() * 1000),
        "request_kind": "turn",
        "window_id": _window_id(state),
    })

    headers = {
        "Content-Type": "application/json",
        "Authorization": _upstream_authorization_header(channel),
        "User-Agent": _codex_user_agent(),
        "accept": "text/event-stream",
        "originator": ORIGINATOR,
        "version": CODEX_VERSION,
        "x-codex-beta-features": "terminal_resize_reflow",
        "x-codex-installation-id": state["installation_id"],
        "x-codex-window-id": _window_id(state),
        "x-codex-turn-metadata": turn_metadata,
        "session-id": state["session_id"],
        "session_id": state["session_id"],
        "thread-id": state["thread_id"],
        "thread_id": state["thread_id"],
        "x-client-request-id": state["thread_id"],
    }

    for name in PASSTHROUGH_REQUEST_HEADERS:
        value = request.headers.get(name)
        if value:
            headers[name] = value

    return headers


def _ensure_client_metadata(
    request: Request,
    body: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    body = dict(body)
    metadata = body.get("client_metadata")

    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        raise GatewayError(
            "client_metadata must be a JSON object",
            status_code=400,
            code="invalid_client_metadata",
            param="client_metadata",
        )

    metadata = dict(metadata)
    metadata.setdefault("x-codex-installation-id", state["installation_id"])
    metadata.setdefault("x-codex-window-id", _window_id(state))

    for name in METADATA_HEADER_KEYS:
        value = request.headers.get(name)
        if value:
            metadata.setdefault(name, value)

    body["client_metadata"] = metadata
    return body


def _ensure_input_array(body: dict[str, Any]) -> dict[str, Any]:
    """将字符串格式的 input 转换为数组格式。

    anyrouter 等第三方代理要求 input 必须是数组格式，
    而非简单的字符串。缺失数组格式会返回 invalid_codex_request。
    """
    body = dict(body)
    input_value = body.get("input")

    if isinstance(input_value, str):
        body["input"] = [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": input_value}],
            }
        ]

    return body


def _ensure_responses_include(body: dict[str, Any]) -> dict[str, Any]:
    """确保请求体携带 reasoning.encrypted_content。

    部分上游（如 anyrouter 的 gpt-5.5 推理模型）会校验 include 字段，
    缺失时返回 400 invalid_codex_request。对不校验的模型注入此字段无害。
    """
    body = dict(body)
    include = body.get("include")
    if not isinstance(include, list):
        include = []
    else:
        include = list(include)
    if REQUIRED_RESPONSES_INCLUDE not in include:
        include.append(REQUIRED_RESPONSES_INCLUDE)
    body["include"] = include
    return body


def _status_is_success(status_code: int) -> bool:
    return 200 <= status_code < 400


def _status_should_failover(status_code: int) -> bool:
    return status_code in RETRYABLE_STATUS_CODES or status_code >= 500


def _record_channel_result(
    channel_id: str,
    success: bool,
    status_code: int | None,
    error_code: str | None,
    latency_ms: int,
    affects_runtime: bool,
) -> None:
    now = _now_ts()
    with _connect_db() as conn:
        conn.execute(
            """
            INSERT INTO channel_events(id, channel_id, success, status_code, error_code, latency_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (_new_prefixed_id("evt"), channel_id, 1 if success else 0, status_code, error_code, latency_ms, now),
        )

        if success:
            conn.execute(
                """
                INSERT INTO channel_runtime(channel_id, consecutive_failures, cooldown_until, last_success_at, updated_at)
                VALUES (?, 0, NULL, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    consecutive_failures = 0,
                    cooldown_until = NULL,
                    last_success_at = excluded.last_success_at,
                    updated_at = excluded.updated_at
                """,
                (channel_id, now, now),
            )
        elif affects_runtime:
            row = conn.execute(
                "SELECT consecutive_failures, cooldown_until FROM channel_runtime WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()
            previous_failures = int(row["consecutive_failures"] if row else 0)
            if row and row["cooldown_until"] is not None and float(row["cooldown_until"]) <= now:
                previous_failures = 0
            failures = previous_failures + 1
            cooldown_until = now + COOLDOWN_SECONDS if failures >= FAILURE_THRESHOLD else None
            conn.execute(
                """
                INSERT INTO channel_runtime(channel_id, consecutive_failures, cooldown_until, last_failure_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(channel_id) DO UPDATE SET
                    consecutive_failures = excluded.consecutive_failures,
                    cooldown_until = excluded.cooldown_until,
                    last_failure_at = excluded.last_failure_at,
                    updated_at = excluded.updated_at
                """,
                (channel_id, failures, cooldown_until, now, now),
            )
        conn.commit()


def _select_channels(request: Request, model: str | None = None) -> list[dict[str, Any]]:
    client_token = _request_bearer_token(request)
    with _connect_db() as conn:
        now = _now_ts()
        rows = conn.execute(
            """
            SELECT
                c.id, c.name, c.upstream_url, c.priority, c.enabled, c.upstream_api_key, c.downstream_api_key, c.supported_models,
                c.proxy_url, c.created_at, c.updated_at,
                COALESCE(r.consecutive_failures, 0) AS consecutive_failures,
                r.cooldown_until, r.last_success_at, r.last_failure_at
            FROM channels c
            LEFT JOIN channel_runtime r ON r.channel_id = c.id
            WHERE c.enabled = 1 AND (r.cooldown_until IS NULL OR r.cooldown_until <= ?)
            ORDER BY c.priority DESC, c.created_at ASC
            """,
            (now,),
        ).fetchall()

    groups: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        channel = dict(row)
        if (
            not secrets.compare_digest(client_token, request.app.state.client_api_key)
            and not _channel_accepts_client_key(channel, client_token)
        ):
            continue
        if model:
            raw = channel.get("supported_models")
            if raw:
                try:
                    allowed_models = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    continue
                if model not in allowed_models:
                    continue
        groups.setdefault(int(channel["priority"]), []).append(channel)

    if rows and not groups:
        raise GatewayError(
            "Invalid API key",
            status_code=401,
            code="invalid_api_key",
            error_type="authentication_error",
        )

    selected: list[dict[str, Any]] = []
    for priority in sorted(groups.keys(), reverse=True):
        group = groups[priority]
        random.shuffle(group)
        selected.extend(group)
    return selected


def _stats_for_channel(conn: sqlite3.Connection, channel_id: str) -> dict[str, Any]:
    return _stats_for_channels(conn, [channel_id]).get(channel_id, _empty_channel_stats())


def _empty_channel_stats() -> dict[str, Any]:
    return {
        "success_24h": 0,
        "failure_24h": 0,
        "total_24h": 0,
        "success_rate_24h": 0.0,
        "failure_rate_24h": 0.0,
    }


def _stats_for_channels(conn: sqlite3.Connection, channel_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not channel_ids:
        return {}
    cutoff = _now_ts() - STATS_WINDOW_SECONDS
    placeholders = ", ".join("?" for _ in channel_ids)
    rows = conn.execute(
        f"""
        SELECT
            channel_id,
            COUNT(*) AS total,
            COALESCE(SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END), 0) AS success_count,
            COALESCE(SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END), 0) AS failure_count
        FROM channel_events
        WHERE channel_id IN ({placeholders}) AND created_at >= ?
        GROUP BY channel_id
        """,
        [*channel_ids, cutoff],
    ).fetchall()
    stats = {channel_id: _empty_channel_stats() for channel_id in channel_ids}
    for row in rows:
        total = int(row["total"] or 0)
        success_count = int(row["success_count"] or 0)
        failure_count = int(row["failure_count"] or 0)
        success_rate = round((success_count / total) * 100, 2) if total else 0.0
        failure_rate = round((failure_count / total) * 100, 2) if total else 0.0
        stats[row["channel_id"]] = {
            "success_24h": success_count,
            "failure_24h": failure_count,
            "total_24h": total,
            "success_rate_24h": success_rate,
            "failure_rate_24h": failure_rate,
        }
    return stats


def _public_channel(
    row: sqlite3.Row | dict[str, Any],
    conn: sqlite3.Connection | None = None,
    stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    channel = dict(row)
    cooldown_until = channel.get("cooldown_until")
    last_success_at = channel.get("last_success_at")
    last_failure_at = channel.get("last_failure_at")
    now = _now_ts()
    active_cooldown = cooldown_until is not None and float(cooldown_until) > now
    consecutive_failures = int(channel.get("consecutive_failures") or 0) if active_cooldown or cooldown_until is None else 0
    raw_models = channel.get("supported_models")
    try:
        parsed_models = json.loads(raw_models) if raw_models else None
    except (json.JSONDecodeError, TypeError):
        parsed_models = None
    result = {
        "id": channel["id"],
        "name": channel["name"],
        "upstream_url": channel["upstream_url"],
        "priority": int(channel["priority"]),
        "enabled": bool(channel["enabled"]),
        "has_upstream_api_key": bool(channel.get("upstream_api_key")),
        "has_downstream_api_key": bool(channel.get("downstream_api_key")),
        "supported_models": parsed_models,
        "proxy_url": channel.get("proxy_url"),
        "consecutive_failures": consecutive_failures,
        "cooldown_until": _ts_to_iso(float(cooldown_until)) if active_cooldown else None,
        "last_success_at": _ts_to_iso(float(last_success_at)) if last_success_at is not None else None,
        "last_failure_at": _ts_to_iso(float(last_failure_at)) if last_failure_at is not None else None,
        "created_at": channel.get("created_at"),
        "updated_at": channel.get("updated_at"),
    }
    if stats is not None:
        result.update(stats)
    elif conn is not None:
        result.update(_stats_for_channel(conn, channel["id"]))
    return result


def _get_channel_row(conn: sqlite3.Connection, channel_id: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT
            c.id, c.name, c.upstream_url, c.priority, c.enabled, c.upstream_api_key, c.downstream_api_key, c.supported_models, c.proxy_url,
            c.created_at, c.updated_at,
            COALESCE(r.consecutive_failures, 0) AS consecutive_failures,
            r.cooldown_until, r.last_success_at, r.last_failure_at
        FROM channels c
        LEFT JOIN channel_runtime r ON r.channel_id = c.id
        WHERE c.id = ?
        """,
        (channel_id,),
    ).fetchone()
    if row is None:
        raise GatewayError("Channel not found", 404, "channel_not_found")
    return row


def _list_channel_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            c.id, c.name, c.upstream_url, c.priority, c.enabled, c.upstream_api_key, c.downstream_api_key, c.supported_models, c.proxy_url,
            c.created_at, c.updated_at,
            COALESCE(r.consecutive_failures, 0) AS consecutive_failures,
            r.cooldown_until, r.last_success_at, r.last_failure_at
        FROM channels c
        LEFT JOIN channel_runtime r ON r.channel_id = c.id
        ORDER BY c.priority DESC, c.created_at ASC
        """
    ).fetchall()


def _validate_channel_payload(body: dict[str, Any], partial: bool) -> dict[str, Any]:
    allowed = {"name", "upstream_url", "priority", "enabled", "upstream_api_key", "downstream_api_key", "supported_models", "proxy_url"}
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise GatewayError(f"Unknown channel fields: {', '.join(unknown)}", 400, "unknown_channel_fields")

    values: dict[str, Any] = {}
    if not partial or "name" in body:
        name = str(body.get("name", "")).strip()
        if not name:
            raise GatewayError("Channel name is required", 400, "invalid_channel_name", param="name")
        values["name"] = name

    if not partial or "upstream_url" in body:
        upstream_url = str(body.get("upstream_url", "")).strip()
        if not upstream_url.startswith(("http://", "https://")):
            raise GatewayError(
                "Channel upstream_url must start with http:// or https://",
                400,
                "invalid_upstream_url",
                param="upstream_url",
            )
        values["upstream_url"] = upstream_url

    if "priority" in body:
        try:
            values["priority"] = int(body["priority"])
        except (TypeError, ValueError) as exc:
            raise GatewayError("Channel priority must be an integer", 400, "invalid_priority", param="priority") from exc
    elif not partial:
        values["priority"] = 0

    if "enabled" in body:
        if not isinstance(body["enabled"], bool):
            raise GatewayError("Channel enabled must be a boolean", 400, "invalid_enabled", param="enabled")
        values["enabled"] = 1 if body["enabled"] else 0
    elif not partial:
        values["enabled"] = 1

    if "upstream_api_key" in body:
        key = body["upstream_api_key"]
        values["upstream_api_key"] = str(key).strip() if key is not None and str(key).strip() else None
    elif not partial:
        values["upstream_api_key"] = None

    if "downstream_api_key" in body:
        key = body["downstream_api_key"]
        values["downstream_api_key"] = str(key).strip() if key is not None and str(key).strip() else None
    elif not partial:
        values["downstream_api_key"] = None

    if "supported_models" in body:
        raw = body["supported_models"]
        if raw is None:
            values["supported_models"] = None
        elif isinstance(raw, list):
            if not raw:
                raise GatewayError("supported_models must not be empty", 400, "invalid_supported_models", param="supported_models")
            models = []
            for item in raw:
                if not isinstance(item, str) or not item.strip():
                    raise GatewayError("Each model in supported_models must be a non-empty string", 400, "invalid_supported_models", param="supported_models")
                models.append(item.strip())
            values["supported_models"] = json.dumps(models)
        else:
            raise GatewayError("supported_models must be an array or null", 400, "invalid_supported_models", param="supported_models")
    elif not partial:
        values["supported_models"] = None

    if "proxy_url" in body:
        raw = body["proxy_url"]
        if raw is None or not str(raw).strip():
            values["proxy_url"] = None
        else:
            proxy_url = str(raw).strip()
            if not proxy_url.startswith(("http://", "https://")):
                raise GatewayError(
                    "Channel proxy_url must start with http:// or https://",
                    400,
                    "invalid_proxy_url",
                    param="proxy_url",
                )
            values["proxy_url"] = proxy_url
    elif not partial:
        values["proxy_url"] = None

    return values


def _json_response_from_upstream(
    response: httpx.Response,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    headers = _merge_headers(_trace_headers(response), extra_headers)
    if not response.content:
        return Response(status_code=response.status_code, headers=headers)

    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type.lower():
        try:
            content = response.json()
        except json.JSONDecodeError:
            return _error_response("Upstream returned invalid JSON", 502, "upstream_invalid_json", headers=headers)
        return JSONResponse(content=content, status_code=response.status_code, headers=headers)

    text = response.text.strip()
    if response.status_code >= 400:
        return _error_response(
            text or f"Upstream request failed with status {response.status_code}",
            status_code=response.status_code,
            code="upstream_error",
            headers=headers,
        )

    return _error_response(
        "Upstream returned a non-JSON response",
        status_code=502,
        code="upstream_invalid_response",
        headers=headers,
    )


def _upstream_error_from_bytes(
    status_code: int,
    response_headers: httpx.Headers,
    body: bytes,
    extra_headers: dict[str, str] | None = None,
) -> JSONResponse:
    headers = _merge_headers(
        {name: value for name in TRACE_RESPONSE_HEADERS if (value := response_headers.get(name)) is not None},
        extra_headers,
    )
    if body:
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            message = body.decode("utf-8", errors="replace").strip()
            return _error_response(
                message or f"Upstream stream failed with status {status_code}",
                status_code=status_code,
                code="upstream_error",
                headers=headers,
            )
        return JSONResponse(content=parsed, status_code=status_code, headers=headers)

    return _error_response(
        f"Upstream stream failed with status {status_code}",
        status_code=status_code,
        code="upstream_error",
        headers=headers,
    )


async def _post_upstream_with_failover(request: Request, body: dict[str, Any]) -> UpstreamResult:
    channels = _select_channels(request, body.get("model"))
    if not channels:
        raise GatewayError("No available channels", 503, "no_available_channels")

    state = _get_gateway_state()
    last_response: httpx.Response | None = None
    last_channel: dict[str, Any] | None = None
    last_error_message = "All channels failed"
    missing_key_error: GatewayError | None = None
    failed_attempts = 0

    for channel in channels:
        try:
            headers = _codex_headers(request, state, channel)
        except GatewayError as exc:
            if exc.code == "missing_upstream_api_key":
                missing_key_error = exc
                continue
            raise
        client = _client_for_channel(request, channel)
        started_at = time.perf_counter()
        try:
            response = await client.post(_upstream_responses_url(channel), headers=headers, json=body)
        except httpx.TimeoutException:
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            _record_channel_result(channel["id"], False, None, "upstream_timeout", latency_ms, True)
            last_error_message = "Upstream request timed out"
            failed_attempts += 1
            continue
        except httpx.RequestError as exc:
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            _record_channel_result(channel["id"], False, None, "upstream_connection_error", latency_ms, True)
            last_error_message = str(exc)
            failed_attempts += 1
            continue

        latency_ms = int((time.perf_counter() - started_at) * 1000)
        success = _status_is_success(response.status_code)
        should_failover = _status_should_failover(response.status_code)
        _record_channel_result(
            channel["id"],
            success,
            response.status_code,
            None if success else "upstream_error",
            latency_ms,
            should_failover,
        )

        if success or not should_failover:
            return UpstreamResult(response, channel, failed_attempts)

        last_response = response
        last_channel = channel
        failed_attempts += 1

    if last_response is not None and last_channel is not None:
        return UpstreamResult(last_response, last_channel, max(failed_attempts - 1, 0))
    if missing_key_error is not None:
        raise missing_key_error
    raise GatewayError(last_error_message, 502, "all_channels_failed")


async def _open_upstream_stream(
    client: httpx.AsyncClient,
    channel: dict[str, Any],
    headers: dict[str, str],
    body: dict[str, Any],
) -> tuple[Any, httpx.Response]:
    stream_context = client.stream("POST", _upstream_responses_url(channel), headers=headers, json=body)
    response = await stream_context.__aenter__()
    return stream_context, response


async def _close_upstream_stream(stream_context: Any) -> None:
    await stream_context.__aexit__(None, None, None)


async def _open_stream_with_failover(request: Request, body: dict[str, Any]) -> StreamResult | JSONResponse:
    try:
        channels = _select_channels(request, body.get("model"))
    except GatewayError as exc:
        return _gateway_error_response(exc)
    if not channels:
        return _error_response("No available channels", 503, "no_available_channels")

    state = _get_gateway_state()
    last_error_response: JSONResponse | None = None
    last_error_message = "All channels failed"
    missing_key_error: GatewayError | None = None
    failed_attempts = 0

    for channel in channels:
        try:
            headers = _codex_headers(request, state, channel)
        except GatewayError as exc:
            if exc.code == "missing_upstream_api_key":
                missing_key_error = exc
                continue
            raise
        client = _client_for_channel(request, channel)
        started_at = time.perf_counter()
        try:
            stream_context, response = await _open_upstream_stream(client, channel, headers, body)
        except httpx.TimeoutException:
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            _record_channel_result(channel["id"], False, None, "upstream_timeout", latency_ms, True)
            last_error_message = "Upstream request timed out"
            failed_attempts += 1
            continue
        except httpx.RequestError as exc:
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            _record_channel_result(channel["id"], False, None, "upstream_connection_error", latency_ms, True)
            last_error_message = str(exc)
            failed_attempts += 1
            continue

        if _status_is_success(response.status_code):
            return StreamResult(stream_context, response, channel, failed_attempts, started_at)

        try:
            body_bytes = await response.aread()
        finally:
            await _close_upstream_stream(stream_context)

        latency_ms = int((time.perf_counter() - started_at) * 1000)
        should_failover = _status_should_failover(response.status_code)
        _record_channel_result(
            channel["id"],
            False,
            response.status_code,
            "upstream_error",
            latency_ms,
            should_failover,
        )
        last_error_response = _upstream_error_from_bytes(
            response.status_code,
            response.headers,
            body_bytes,
            _channel_response_headers(channel, failed_attempts),
        )
        if not should_failover:
            return last_error_response
        failed_attempts += 1

    if last_error_response is not None:
        return last_error_response
    if missing_key_error is not None:
        return _gateway_error_response(missing_key_error)
    return _error_response(last_error_message, 502, "all_channels_failed")


async def _raw_sse_bytes(result: StreamResult) -> AsyncIterator[bytes]:
    success = False
    try:
        async for chunk in result.response.aiter_bytes():
            yield chunk
        success = True
    except Exception:
        latency_ms = int((time.perf_counter() - result.started_at) * 1000)
        _record_channel_result(result.channel["id"], False, result.response.status_code, "stream_error", latency_ms, True)
        raise
    finally:
        if success:
            latency_ms = int((time.perf_counter() - result.started_at) * 1000)
            _record_channel_result(result.channel["id"], True, result.response.status_code, None, latency_ms, False)
        await _close_upstream_stream(result.stream_context)


def _content_value_has_text(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, list):
        return any(_content_value_has_text(item) for item in value)
    if isinstance(value, dict):
        for key in ("text", "content", "output_text", "message"):
            if _content_value_has_text(value.get(key)):
                return True
    return False


def _test_response_has_content(data: Any) -> bool:
    if not isinstance(data, dict) or not data:
        return False

    for key in ("output_text", "content", "text", "message"):
        if _content_value_has_text(data.get(key)):
            return True

    output = data.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            if _content_value_has_text(item.get("content")):
                return True
            if _content_value_has_text(item.get("text")):
                return True

    choices = data.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if isinstance(message, dict) and _content_value_has_text(message.get("content")):
                return True
            if _content_value_has_text(choice.get("text")):
                return True

    return False


async def _post_responses(request: Request, body: dict[str, Any]) -> Response:
    state = _get_gateway_state()
    body = _ensure_input_array(body)
    body = _ensure_client_metadata(request, body, state)
    body = _ensure_responses_include(body)

    if body.get("stream") is True:
        stream_result = await _open_stream_with_failover(request, body)
        if isinstance(stream_result, JSONResponse):
            return stream_result
        return StreamingResponse(
            _raw_sse_bytes(stream_result),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                **_trace_headers(stream_result.response),
                **_channel_response_headers(stream_result.channel, stream_result.failover_count),
            },
        )

    upstream = await _post_upstream_with_failover(request, body)
    return _json_response_from_upstream(
        upstream.response,
        _channel_response_headers(upstream.channel, upstream.failover_count),
    )


@app.get("/health")
def health():
    with _connect_db() as conn:
        conn.execute("SELECT 1").fetchone()
    return {"status": "ok"}


@app.get("/management", include_in_schema=False)
def management_page():
    return FileResponse(
        MANAGEMENT_HTML_PATH,
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/v1/config")
def config(request: Request):
    return _config_payload(request, include_private=False)


@app.get("/v1/models")
def list_models():
    return {"object": "list", "data": MODELS}


@app.post("/v1/responses")
async def proxy_responses(request: Request):
    try:
        body = await _read_json_body(request)
        return await _post_responses(request, body)
    except GatewayError as exc:
        return _gateway_error_response(exc)


@app.post("/v1/chat/completions")
def chat_completions(_: Request):
    return _error_response(
        "The /v1/chat/completions endpoint is not supported. Use /v1/responses instead.",
        status_code=404,
        code="endpoint_not_supported",
        error_type="invalid_request_error",
    )


@app.post("/admin/fingerprint/reset")
def reset_fingerprint(request: Request):
    _require_admin(request)
    state = _reset_gateway_state()
    return {"state": _public_state(state)}


@app.get("/admin/session")
def admin_session(request: Request):
    _require_admin(request)
    return {
        "authenticated": True,
        "admin_token_source": request.app.state.admin_token_source,
        "client_api_key_source": request.app.state.client_api_key_source,
    }


@app.get("/admin/config")
def admin_config(request: Request):
    _require_admin(request)
    return _config_payload(request, include_private=True)


@app.get("/admin/channels")
def list_channels(request: Request):
    _require_admin(request)
    with _connect_db() as conn:
        rows = _list_channel_rows(conn)
        stats = _stats_for_channels(conn, [row["id"] for row in rows])
        return {"object": "list", "data": [_public_channel(row, stats=stats.get(row["id"])) for row in rows]}


@app.post("/admin/channels")
async def create_channel(request: Request):
    _require_admin(request)
    body = await _read_json_body(request)
    values = _validate_channel_payload(body, partial=False)
    channel_id = _new_prefixed_id("ch")
    now = _utc_now()
    with _connect_db() as conn:
        conn.execute(
            """
            INSERT INTO channels(id, name, upstream_url, priority, enabled, upstream_api_key, downstream_api_key, supported_models, proxy_url, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                channel_id,
                values["name"],
                values["upstream_url"],
                values["priority"],
                values["enabled"],
                values["upstream_api_key"],
                values["downstream_api_key"],
                values["supported_models"],
                values["proxy_url"],
                now,
                now,
            ),
        )
        conn.execute(
            """
            INSERT INTO channel_runtime(channel_id, consecutive_failures, cooldown_until, updated_at)
            VALUES (?, 0, NULL, ?)
            """,
            (channel_id, _now_ts()),
        )
        conn.commit()
        row = _get_channel_row(conn, channel_id)
        return JSONResponse(content=_public_channel(row, conn), status_code=201)


@app.get("/admin/channels/{channel_id}")
def get_channel(request: Request, channel_id: str):
    _require_admin(request)
    with _connect_db() as conn:
        row = _get_channel_row(conn, channel_id)
        return _public_channel(row, conn)


@app.patch("/admin/channels/{channel_id}")
async def update_channel(request: Request, channel_id: str):
    _require_admin(request)
    body = await _read_json_body(request)
    values = _validate_channel_payload(body, partial=True)
    if not values:
        with _connect_db() as conn:
            row = _get_channel_row(conn, channel_id)
            return _public_channel(row, conn)

    assignments = [f"{field} = ?" for field in values]
    parameters = list(values.values())
    assignments.append("updated_at = ?")
    parameters.append(_utc_now())
    parameters.append(channel_id)

    with _connect_db() as conn:
        _get_channel_row(conn, channel_id)
        conn.execute(
            f"UPDATE channels SET {', '.join(assignments)} WHERE id = ?",
            parameters,
        )
        conn.commit()
        row = _get_channel_row(conn, channel_id)
        return _public_channel(row, conn)


@app.delete("/admin/channels/{channel_id}")
def delete_channel(request: Request, channel_id: str):
    _require_admin(request)
    with _connect_db() as conn:
        _get_channel_row(conn, channel_id)
        conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        conn.commit()
    return {"deleted": True, "id": channel_id}


@app.post("/admin/channels/{channel_id}/enable")
def enable_channel(request: Request, channel_id: str):
    _require_admin(request)
    with _connect_db() as conn:
        _get_channel_row(conn, channel_id)
        conn.execute("UPDATE channels SET enabled = 1, updated_at = ? WHERE id = ?", (_utc_now(), channel_id))
        conn.commit()
        return _public_channel(_get_channel_row(conn, channel_id), conn)


@app.post("/admin/channels/{channel_id}/disable")
def disable_channel(request: Request, channel_id: str):
    _require_admin(request)
    with _connect_db() as conn:
        _get_channel_row(conn, channel_id)
        conn.execute("UPDATE channels SET enabled = 0, updated_at = ? WHERE id = ?", (_utc_now(), channel_id))
        conn.commit()
        return _public_channel(_get_channel_row(conn, channel_id), conn)


@app.post("/admin/channels/{channel_id}/reset-runtime")
def reset_channel_runtime(request: Request, channel_id: str):
    _require_admin(request)
    now = _now_ts()
    with _connect_db() as conn:
        _get_channel_row(conn, channel_id)
        conn.execute(
            """
            INSERT INTO channel_runtime(channel_id, consecutive_failures, cooldown_until, updated_at)
            VALUES (?, 0, NULL, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                consecutive_failures = 0,
                cooldown_until = NULL,
                updated_at = excluded.updated_at
            """,
            (channel_id, now),
        )
        conn.commit()
        return _public_channel(_get_channel_row(conn, channel_id), conn)


@app.post("/admin/channels/{channel_id}/test")
async def test_channel(request: Request, channel_id: str):
    _require_admin(request)
    body = await _read_json_body(request)
    model = str(body.get("model") or "gpt-5.4").strip() or "gpt-5.4"
    with _connect_db() as conn:
        channel = dict(_get_channel_row(conn, channel_id))

    result: dict[str, Any] = {
        "success": False,
        "channel_id": channel_id,
        "model": model,
        "latency_ms": 0,
        "status_code": None,
        "message": "测试失败",
    }

    started_at = time.perf_counter()
    try:
        headers = _codex_headers(request, _get_gateway_state(), channel)
        test_body = {"model": model, "input": "ping", "max_output_tokens": 16}
        test_body = _ensure_input_array(test_body)
        response = await _client_for_channel(request, channel).post(
            _upstream_responses_url(channel),
            headers=headers,
            json=test_body,
            timeout=CHANNEL_TEST_TIMEOUT,
        )
        result["latency_ms"] = int((time.perf_counter() - started_at) * 1000)
        result["status_code"] = response.status_code
        if not _status_is_success(response.status_code):
            message = response.text.strip() or f"上游返回 HTTP {response.status_code}"
            result["message"] = message[:500]
            return result
        if not response.content:
            result["message"] = "上游返回空响应"
            return result
        try:
            data = response.json()
        except json.JSONDecodeError:
            result["message"] = "上游返回非 JSON 响应"
            return result
        if not _test_response_has_content(data):
            result["message"] = "上游返回空内容"
            return result
        result["success"] = True
        result["message"] = "测试成功"
        return result
    except httpx.TimeoutException:
        result["latency_ms"] = int((time.perf_counter() - started_at) * 1000)
        result["message"] = "测试超时（60 秒）"
        return result
    except httpx.RequestError as exc:
        result["latency_ms"] = int((time.perf_counter() - started_at) * 1000)
        result["message"] = str(exc)
        return result
    except GatewayError as exc:
        result["latency_ms"] = int((time.perf_counter() - started_at) * 1000)
        result["message"] = exc.message
        return result


@app.get("/admin/channels/{channel_id}/stats")
def channel_stats(request: Request, channel_id: str):
    _require_admin(request)
    with _connect_db() as conn:
        row = _get_channel_row(conn, channel_id)
        return _public_channel(row, conn)


@app.get("/admin/channel-stats")
def all_channel_stats(request: Request):
    _require_admin(request)
    with _connect_db() as conn:
        rows = _list_channel_rows(conn)
        stats = _stats_for_channels(conn, [row["id"] for row in rows])
        return {"object": "list", "data": [_public_channel(row, stats=stats.get(row["id"])) for row in rows]}
