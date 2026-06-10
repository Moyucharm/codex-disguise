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
ADMIN_TOKEN_PATH = DATA_DIR / "admin_token.txt"
MANAGEMENT_HTML_PATH = APP_DIR / "management.html"

DEFAULT_UPSTREAM_URL = "https://new.sharedchat.cc/codex/v1/responses"
TIMEOUT = httpx.Timeout(300.0, connect=10.0)

ORIGINATOR = "codex_cli_rs"
CODEX_VERSION = "0.148.0"
TERMINAL_UA = "unknown"
FAILURE_THRESHOLD = 3
COOLDOWN_SECONDS = 300
STATS_WINDOW_SECONDS = 24 * 60 * 60

TRACE_RESPONSE_HEADERS = (
    "x-request-id",
    "x-oai-request-id",
    "cf-ray",
    "x-codex-active-limit",
    "x-openai-authorization-error",
)

PASSTHROUGH_REQUEST_HEADERS = (
    "OpenAI-Organization",
    "OpenAI-Project",
    "ChatGPT-Account-Id",
    "X-OpenAI-Fedramp",
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

RETRYABLE_STATUS_CODES = {401, 403, 408, 409, 429, 500, 502, 503, 504}

MODELS = [
    {"id": "gpt-5.5-codex", "object": "model", "created": 1700000000, "owned_by": "openai"},
    {"id": "gpt-5.5", "object": "model", "created": 1700000000, "owned_by": "openai"},
    {"id": "gpt-5.4-codex", "object": "model", "created": 1700000000, "owned_by": "openai"},
    {"id": "gpt-5.3-codex", "object": "model", "created": 1700000000, "owned_by": "openai"},
    {"id": "gpt-5.2-codex", "object": "model", "created": 1700000000, "owned_by": "openai"},
    {"id": "gpt-5.1-codex", "object": "model", "created": 1700000000, "owned_by": "openai"},
    {"id": "gpt-5-codex", "object": "model", "created": 1700000000, "owned_by": "openai"},
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
    os_type = platform.system() or "unknown"
    os_version = platform.release() or "unknown"
    arch = (platform.machine() or "unknown").lower()
    return _sanitize_user_agent(
        f"{ORIGINATOR}/{CODEX_VERSION} ({os_type} {os_version}; {arch}) {TERMINAL_UA}"
    )


def _connect_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
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


def _ensure_admin_token() -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if ADMIN_TOKEN_PATH.exists():
        token = ADMIN_TOKEN_PATH.read_text(encoding="utf-8").strip()
        if token:
            return token

    token = secrets.token_urlsafe(32)
    ADMIN_TOKEN_PATH.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(ADMIN_TOKEN_PATH, 0o600)
    except OSError:
        pass
    return token


def _init_db() -> None:
    with _connect_db() as conn:
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
        _ensure_gateway_state(conn)
        channel_count = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        if channel_count == 0:
            channel_id = _new_prefixed_id("ch")
            now = _utc_now()
            conn.execute(
                """
                INSERT INTO channels(id, name, upstream_url, priority, enabled, downstream_api_key, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (channel_id, "default", DEFAULT_UPSTREAM_URL, 0, 1, None, now, now),
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
    app.state.admin_token = _ensure_admin_token()
    app.state.http_client = httpx.AsyncClient(timeout=TIMEOUT)
    try:
        yield
    finally:
        await app.state.http_client.aclose()


app = FastAPI(title="codex2api", lifespan=lifespan)


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
        "x-codex2api-channel-id": _sanitize_header_value(channel["id"]),
        "x-codex2api-channel-name": _sanitize_header_value(channel["name"]),
        "x-codex2api-failover-count": str(failover_count),
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


def _authorization_header(request: Request, channel: dict[str, Any]) -> str:
    channel_key = channel.get("downstream_api_key")
    if isinstance(channel_key, str) and channel_key.strip():
        return _format_bearer_token(channel_key)

    authorization = request.headers.get("Authorization", "")
    if authorization.startswith("Bearer ") and authorization.removeprefix("Bearer ").strip():
        return authorization

    raise GatewayError(
        "Missing Authorization bearer token and channel downstream_api_key",
        status_code=401,
        code="missing_api_key",
        error_type="authentication_error",
    )


def _codex_headers(request: Request, state: dict[str, Any], channel: dict[str, Any]) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": _authorization_header(request, channel),
        "User-Agent": _codex_user_agent(),
        "originator": ORIGINATOR,
        "version": CODEX_VERSION,
        "x-codex-installation-id": state["installation_id"],
        "x-codex-window-id": _window_id(state),
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
                "SELECT consecutive_failures FROM channel_runtime WHERE channel_id = ?",
                (channel_id,),
            ).fetchone()
            failures = int(row["consecutive_failures"] if row else 0) + 1
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


def _refresh_expired_cooldowns(conn: sqlite3.Connection) -> None:
    now = _now_ts()
    conn.execute(
        """
        UPDATE channel_runtime
        SET consecutive_failures = 0, cooldown_until = NULL, updated_at = ?
        WHERE cooldown_until IS NOT NULL AND cooldown_until <= ?
        """,
        (now, now),
    )


def _select_channels() -> list[dict[str, Any]]:
    with _connect_db() as conn:
        _refresh_expired_cooldowns(conn)
        conn.commit()
        now = _now_ts()
        rows = conn.execute(
            """
            SELECT
                c.id, c.name, c.upstream_url, c.priority, c.enabled, c.downstream_api_key,
                c.created_at, c.updated_at,
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
        groups.setdefault(int(channel["priority"]), []).append(channel)

    selected: list[dict[str, Any]] = []
    for priority in sorted(groups.keys(), reverse=True):
        group = groups[priority]
        random.shuffle(group)
        selected.extend(group)
    return selected


def _stats_for_channel(conn: sqlite3.Connection, channel_id: str) -> dict[str, Any]:
    cutoff = _now_ts() - STATS_WINDOW_SECONDS
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            COALESCE(SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END), 0) AS success_count,
            COALESCE(SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END), 0) AS failure_count
        FROM channel_events
        WHERE channel_id = ? AND created_at >= ?
        """,
        (channel_id, cutoff),
    ).fetchone()
    total = int(row["total"] or 0)
    success_count = int(row["success_count"] or 0)
    failure_count = int(row["failure_count"] or 0)
    success_rate = round((success_count / total) * 100, 2) if total else 0.0
    failure_rate = round((failure_count / total) * 100, 2) if total else 0.0
    return {
        "success_24h": success_count,
        "failure_24h": failure_count,
        "total_24h": total,
        "success_rate_24h": success_rate,
        "failure_rate_24h": failure_rate,
    }


def _public_channel(row: sqlite3.Row | dict[str, Any], conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    channel = dict(row)
    cooldown_until = channel.get("cooldown_until")
    last_success_at = channel.get("last_success_at")
    last_failure_at = channel.get("last_failure_at")
    result = {
        "id": channel["id"],
        "name": channel["name"],
        "upstream_url": channel["upstream_url"],
        "priority": int(channel["priority"]),
        "enabled": bool(channel["enabled"]),
        "has_downstream_api_key": bool(channel.get("downstream_api_key")),
        "consecutive_failures": int(channel.get("consecutive_failures") or 0),
        "cooldown_until": _ts_to_iso(float(cooldown_until)) if cooldown_until is not None else None,
        "last_success_at": _ts_to_iso(float(last_success_at)) if last_success_at is not None else None,
        "last_failure_at": _ts_to_iso(float(last_failure_at)) if last_failure_at is not None else None,
        "created_at": channel.get("created_at"),
        "updated_at": channel.get("updated_at"),
    }
    if conn is not None:
        result.update(_stats_for_channel(conn, channel["id"]))
    return result


def _get_channel_row(conn: sqlite3.Connection, channel_id: str) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT
            c.id, c.name, c.upstream_url, c.priority, c.enabled, c.downstream_api_key,
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
    _refresh_expired_cooldowns(conn)
    conn.commit()
    return conn.execute(
        """
        SELECT
            c.id, c.name, c.upstream_url, c.priority, c.enabled, c.downstream_api_key,
            c.created_at, c.updated_at,
            COALESCE(r.consecutive_failures, 0) AS consecutive_failures,
            r.cooldown_until, r.last_success_at, r.last_failure_at
        FROM channels c
        LEFT JOIN channel_runtime r ON r.channel_id = c.id
        ORDER BY c.priority DESC, c.created_at ASC
        """
    ).fetchall()


def _validate_channel_payload(body: dict[str, Any], partial: bool) -> dict[str, Any]:
    allowed = {"name", "upstream_url", "priority", "enabled", "downstream_api_key"}
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

    if "downstream_api_key" in body:
        key = body["downstream_api_key"]
        values["downstream_api_key"] = str(key).strip() if key is not None and str(key).strip() else None
    elif not partial:
        values["downstream_api_key"] = None

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
    channels = _select_channels()
    if not channels:
        raise GatewayError("No available channels", 503, "no_available_channels")

    state = _get_gateway_state()
    client: httpx.AsyncClient = request.app.state.http_client
    last_response: httpx.Response | None = None
    last_channel: dict[str, Any] | None = None
    last_error_message = "All channels failed"
    missing_key_error: GatewayError | None = None
    failed_attempts = 0

    for channel in channels:
        try:
            headers = _codex_headers(request, state, channel)
        except GatewayError as exc:
            if exc.code == "missing_api_key":
                missing_key_error = exc
                continue
            raise
        started_at = time.perf_counter()
        try:
            response = await client.post(channel["upstream_url"], headers=headers, json=body)
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
    stream_context = client.stream("POST", channel["upstream_url"], headers=headers, json=body)
    response = await stream_context.__aenter__()
    return stream_context, response


async def _close_upstream_stream(stream_context: Any) -> None:
    await stream_context.__aexit__(None, None, None)


async def _open_stream_with_failover(request: Request, body: dict[str, Any]) -> StreamResult | JSONResponse:
    channels = _select_channels()
    if not channels:
        return _error_response("No available channels", 503, "no_available_channels")

    state = _get_gateway_state()
    client: httpx.AsyncClient = request.app.state.http_client
    last_error_response: JSONResponse | None = None
    last_error_message = "All channels failed"
    missing_key_error: GatewayError | None = None
    failed_attempts = 0

    for channel in channels:
        try:
            headers = _codex_headers(request, state, channel)
        except GatewayError as exc:
            if exc.code == "missing_api_key":
                missing_key_error = exc
                continue
            raise
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


def _message_content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for part in content:
            if isinstance(part, dict):
                part_type = part.get("type")
                if part_type in {"text", "input_text", "output_text"}:
                    texts.append(str(part.get("text", "")))
        return "\n".join(text for text in texts if text)
    return json.dumps(content, ensure_ascii=False)


def _chat_content_to_response_parts(content: Any, role: str) -> list[dict[str, Any]]:
    text_type = "output_text" if role == "assistant" else "input_text"

    if content is None:
        return [{"type": text_type, "text": ""}]
    if isinstance(content, str):
        return [{"type": text_type, "text": content}]
    if not isinstance(content, list):
        return [{"type": text_type, "text": json.dumps(content, ensure_ascii=False)}]

    parts: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, dict):
            continue

        part_type = part.get("type")
        if part_type in {"text", "input_text", "output_text"}:
            parts.append({"type": text_type, "text": str(part.get("text", ""))})
        elif part_type == "image_url" and role == "user":
            image_url = part.get("image_url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if image_url:
                parts.append({"type": "input_image", "image_url": image_url})
        elif part_type == "input_image" and role == "user" and part.get("image_url"):
            parts.append({"type": "input_image", "image_url": part["image_url"]})

    if not parts:
        parts.append({"type": text_type, "text": ""})
    return parts


def _chat_tool_to_response_tool(tool: Any) -> Any:
    if not isinstance(tool, dict) or tool.get("type") != "function":
        return tool

    function = tool.get("function")
    if not isinstance(function, dict):
        raise GatewayError("Function tool must include a function object", 400, "invalid_tool", param="tools")

    name = function.get("name")
    if not isinstance(name, str) or not name.strip():
        raise GatewayError("Function tool name is required", 400, "invalid_tool_name", param="tools.function.name")

    response_tool: dict[str, Any] = {"type": "function", "name": name}
    for source_key, target_key in (
        ("description", "description"),
        ("parameters", "parameters"),
        ("strict", "strict"),
    ):
        if source_key in function:
            response_tool[target_key] = function[source_key]
    return response_tool


def _chat_tools_to_response_tools(tools: Any) -> list[Any]:
    if not isinstance(tools, list):
        raise GatewayError("tools must be an array", 400, "invalid_tools", param="tools")
    return [_chat_tool_to_response_tool(tool) for tool in tools]


def _chat_tool_choice_to_response_tool_choice(tool_choice: Any) -> Any:
    if not isinstance(tool_choice, dict) or tool_choice.get("type") != "function":
        return tool_choice

    function = tool_choice.get("function")
    name = function.get("name") if isinstance(function, dict) else None
    if not isinstance(name, str) or not name.strip():
        raise GatewayError(
            "Function tool_choice name is required",
            400,
            "invalid_tool_choice",
            param="tool_choice.function.name",
        )
    return {"type": "function", "name": name}


def _chat_tool_calls_to_response_items(tool_calls: Any) -> list[dict[str, Any]]:
    if not isinstance(tool_calls, list):
        raise GatewayError("tool_calls must be an array", 400, "invalid_tool_calls", param="messages.tool_calls")

    items: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict) or tool_call.get("type") != "function":
            raise GatewayError("Only function tool calls are supported", 400, "unsupported_tool_call")

        function = tool_call.get("function")
        if not isinstance(function, dict):
            raise GatewayError("Function tool call must include a function object", 400, "invalid_tool_call")

        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            raise GatewayError("Function tool call name is required", 400, "invalid_tool_call_name")

        arguments = function.get("arguments", "")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)

        call_id = tool_call.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            raise GatewayError("Tool call id is required", 400, "invalid_tool_call_id")

        items.append(
            {
                "type": "function_call",
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
            }
        )
    return items


def _chat_response_format_to_text(response_format: Any) -> dict[str, Any]:
    if not isinstance(response_format, dict):
        raise GatewayError("response_format must be an object", 400, "invalid_response_format", param="response_format")

    format_type = response_format.get("type")
    if format_type == "json_schema" and isinstance(response_format.get("json_schema"), dict):
        return {"format": {"type": "json_schema", **response_format["json_schema"]}}
    return {"format": dict(response_format)}


def _chat_to_responses_body(chat_body: dict[str, Any]) -> dict[str, Any]:
    messages = chat_body.get("messages")
    if not isinstance(messages, list):
        raise GatewayError("messages must be an array", 400, "invalid_messages", param="messages")

    instructions: list[str] = []
    input_items: list[dict[str, Any]] = []

    for message in messages:
        if not isinstance(message, dict):
            raise GatewayError("Each message must be an object", 400, "invalid_messages", param="messages")

        role = message.get("role")
        content = message.get("content")
        if role in {"system", "developer"}:
            text = _message_content_to_text(content)
            if text:
                instructions.append(text)
        elif role in {"user", "assistant"}:
            tool_calls = message.get("tool_calls") if role == "assistant" else None
            if tool_calls is not None and _message_content_to_text(content):
                input_items.append(
                    {
                        "role": role,
                        "content": _chat_content_to_response_parts(content, role),
                    }
                )
            elif tool_calls is None:
                input_items.append(
                    {
                        "role": role,
                        "content": _chat_content_to_response_parts(content, role),
                    }
                )

            if tool_calls is not None:
                input_items.extend(_chat_tool_calls_to_response_items(tool_calls))
        elif role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id.strip():
                raise GatewayError("Tool message tool_call_id is required", 400, "invalid_tool_call_id")
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": _message_content_to_text(content),
                }
            )
        else:
            raise GatewayError("Unsupported message role", 400, "unsupported_message_role", param="messages.role")

    body: dict[str, Any] = {
        "model": chat_body.get("model", "gpt-5.5"),
        "input": input_items,
        "stream": bool(chat_body.get("stream", False)),
    }

    if instructions:
        body["instructions"] = "\n\n".join(instructions)

    passthrough_fields = (
        "temperature",
        "top_p",
        "frequency_penalty",
        "presence_penalty",
        "parallel_tool_calls",
        "reasoning",
        "text",
        "store",
        "previous_response_id",
        "service_tier",
        "truncation",
        "top_logprobs",
        "client_metadata",
    )
    for field in passthrough_fields:
        if field in chat_body:
            body[field] = chat_body[field]

    if "tools" in chat_body:
        body["tools"] = _chat_tools_to_response_tools(chat_body["tools"])
    if "tool_choice" in chat_body:
        body["tool_choice"] = _chat_tool_choice_to_response_tool_choice(chat_body["tool_choice"])
    if "response_format" in chat_body:
        text_options = dict(body.get("text") or {})
        text_options.update(_chat_response_format_to_text(chat_body["response_format"]))
        body["text"] = text_options

    if "max_completion_tokens" in chat_body:
        body["max_output_tokens"] = chat_body["max_completion_tokens"]
    elif "max_tokens" in chat_body:
        body["max_output_tokens"] = chat_body["max_tokens"]

    return body


def _extract_response_text(response_body: dict[str, Any]) -> str:
    texts: list[str] = []

    for item in response_body.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []) or []:
            if isinstance(content, dict) and content.get("type") in {"output_text", "text"}:
                texts.append(str(content.get("text", "")))

    if texts:
        return "".join(texts)

    output_text = response_body.get("output_text")
    if isinstance(output_text, str):
        return output_text

    return ""


def _extract_response_tool_calls(response_body: dict[str, Any]) -> list[dict[str, Any]]:
    tool_calls: list[dict[str, Any]] = []
    for item in response_body.get("output", []) or []:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            continue

        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        arguments = item.get("arguments", "")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False)
        call_id = item.get("call_id") or item.get("id") or _new_prefixed_id("call")
        tool_calls.append(
            {
                "id": str(call_id),
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": arguments,
                },
            }
        )
    return tool_calls


def _responses_to_chat_completion(response_body: dict[str, Any], model: str) -> dict[str, Any]:
    response_id = response_body.get("id") or f"chatcmpl-{uuid.uuid4().hex}"
    status = response_body.get("status")
    tool_calls = _extract_response_tool_calls(response_body)
    finish_reason = "tool_calls" if tool_calls else "stop" if status in {None, "completed"} else status
    message = {
        "role": "assistant",
        "content": _extract_response_text(response_body),
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
        if not message["content"]:
            message["content"] = None

    return {
        "id": response_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": response_body.get("model") or model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": response_body.get("usage"),
    }


def _chat_stream_chunk(
    stream_id: str,
    created: int,
    model: str,
    delta: dict[str, Any],
    finish_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "id": stream_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason,
            }
        ],
    }


def _chat_tool_call_delta(index: int, item: dict[str, Any], arguments: str | None = None) -> dict[str, Any]:
    function: dict[str, Any] = {}
    if "name" in item:
        function["name"] = str(item.get("name") or "")
    if arguments is not None:
        function["arguments"] = arguments

    delta: dict[str, Any] = {
        "index": index,
        "type": "function",
        "function": function,
    }
    call_id = item.get("call_id") or item.get("id")
    if call_id:
        delta["id"] = str(call_id)
    return {"tool_calls": [delta]}


async def _chat_completion_sse(result: StreamResult, model: str) -> AsyncIterator[str]:
    stream_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    saw_error = False
    saw_tool_call = False
    success = False

    try:
        first_chunk = _chat_stream_chunk(stream_id, created, model, {"role": "assistant"})
        yield f"data: {json.dumps(first_chunk, ensure_ascii=False)}\n\n"

        tool_call_indexes: dict[str, int] = {}
        tool_call_argument_keys: set[str] = set()
        data_lines: list[str] = []
        async for line in result.response.aiter_lines():
            if line == "":
                if data_lines:
                    raw_data = "\n".join(data_lines)
                    data_lines = []
                    if raw_data == "[DONE]":
                        break
                    try:
                        event = json.loads(raw_data)
                    except json.JSONDecodeError:
                        continue

                    if event.get("type") == "response.output_text.delta":
                        delta = event.get("delta") or ""
                        if delta:
                            chunk = _chat_stream_chunk(stream_id, created, model, {"content": delta})
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    elif event.get("type") == "response.output_item.added":
                        item = event.get("item")
                        if isinstance(item, dict) and item.get("type") == "function_call":
                            saw_tool_call = True
                            item_key = str(item.get("id") or item.get("call_id") or event.get("output_index"))
                            index = tool_call_indexes.setdefault(item_key, len(tool_call_indexes))
                            chunk = _chat_stream_chunk(stream_id, created, model, _chat_tool_call_delta(index, item, ""))
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    elif event.get("type") == "response.function_call_arguments.delta":
                        saw_tool_call = True
                        item_key = str(event.get("item_id") or event.get("call_id") or event.get("output_index"))
                        index = tool_call_indexes.setdefault(item_key, len(tool_call_indexes))
                        item = {"call_id": event["call_id"]} if event.get("call_id") else {}
                        delta = event.get("delta") or ""
                        if delta:
                            tool_call_argument_keys.add(item_key)
                            chunk = _chat_stream_chunk(stream_id, created, model, _chat_tool_call_delta(index, item, delta))
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    elif event.get("type") == "response.function_call_arguments.done":
                        saw_tool_call = True
                        item_key = str(event.get("item_id") or event.get("call_id") or event.get("output_index"))
                        index = tool_call_indexes.setdefault(item_key, len(tool_call_indexes))
                        arguments = event.get("arguments")
                        if arguments and item_key not in tool_call_argument_keys:
                            if not isinstance(arguments, str):
                                arguments = json.dumps(arguments, ensure_ascii=False)
                            item = {"call_id": event["call_id"]} if event.get("call_id") else {}
                            chunk = _chat_stream_chunk(stream_id, created, model, _chat_tool_call_delta(index, item, arguments))
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    elif event.get("type") == "response.output_item.done":
                        item = event.get("item")
                        if isinstance(item, dict) and item.get("type") == "function_call":
                            saw_tool_call = True
                            item_key = str(item.get("id") or item.get("call_id") or event.get("output_index"))
                            index = tool_call_indexes.setdefault(item_key, len(tool_call_indexes))
                            arguments = item.get("arguments")
                            if arguments and item_key not in tool_call_argument_keys:
                                if not isinstance(arguments, str):
                                    arguments = json.dumps(arguments, ensure_ascii=False)
                                chunk = _chat_stream_chunk(stream_id, created, model, _chat_tool_call_delta(index, item, arguments))
                                yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                    elif event.get("type") == "response.error":
                        saw_error = True
                        error = event.get("error") or {}
                        message = error.get("message") if isinstance(error, dict) else None
                        if message:
                            chunk = _chat_stream_chunk(stream_id, created, model, {"content": message})
                            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
                continue

            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())

        final_chunk = _chat_stream_chunk(stream_id, created, model, {}, "tool_calls" if saw_tool_call else "stop")
        yield f"data: {json.dumps(final_chunk, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
        success = not saw_error
    except Exception:
        latency_ms = int((time.perf_counter() - result.started_at) * 1000)
        _record_channel_result(result.channel["id"], False, result.response.status_code, "stream_error", latency_ms, True)
        raise
    finally:
        if success:
            latency_ms = int((time.perf_counter() - result.started_at) * 1000)
            _record_channel_result(result.channel["id"], True, result.response.status_code, None, latency_ms, False)
        elif saw_error:
            latency_ms = int((time.perf_counter() - result.started_at) * 1000)
            _record_channel_result(result.channel["id"], False, result.response.status_code, "response_error", latency_ms, True)
        await _close_upstream_stream(result.stream_context)


async def _post_responses(request: Request, body: dict[str, Any]) -> Response:
    state = _get_gateway_state()
    body = _ensure_client_metadata(request, body, state)

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
async def health():
    with _connect_db() as conn:
        channel_count = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        enabled_channel_count = conn.execute("SELECT COUNT(*) FROM channels WHERE enabled = 1").fetchone()[0]
    return {
        "status": "ok",
        "database_path": str(DB_PATH),
        "admin_token_path": str(ADMIN_TOKEN_PATH),
        "channel_count": channel_count,
        "enabled_channel_count": enabled_channel_count,
    }


@app.get("/management.html", include_in_schema=False)
async def management_html():
    return FileResponse(MANAGEMENT_HTML_PATH)


@app.get("/", include_in_schema=False)
async def management_root():
    return FileResponse(MANAGEMENT_HTML_PATH)


@app.get("/v1/config")
async def config():
    with _connect_db() as conn:
        state = _get_gateway_state(conn)
        channel_count = conn.execute("SELECT COUNT(*) FROM channels").fetchone()[0]
        enabled_channel_count = conn.execute("SELECT COUNT(*) FROM channels WHERE enabled = 1").fetchone()[0]
    return {
        "database_path": str(DB_PATH),
        "admin_token_path": str(ADMIN_TOKEN_PATH),
        "originator": ORIGINATOR,
        "version": CODEX_VERSION,
        "user_agent": _codex_user_agent(),
        "authorization": "channel_downstream_api_key_or_request_header",
        "failure_threshold": FAILURE_THRESHOLD,
        "cooldown_seconds": COOLDOWN_SECONDS,
        "channel_count": channel_count,
        "enabled_channel_count": enabled_channel_count,
        "state": _public_state(state),
    }


@app.get("/v1/models")
async def list_models():
    return {"object": "list", "data": MODELS}


@app.post("/v1/responses")
async def proxy_responses(request: Request):
    try:
        body = await _read_json_body(request)
        return await _post_responses(request, body)
    except GatewayError as exc:
        return _gateway_error_response(exc)


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    try:
        chat_body = await _read_json_body(request)
        responses_body = _chat_to_responses_body(chat_body)
        responses_body = _ensure_client_metadata(request, responses_body, _get_gateway_state())

        if responses_body.get("stream") is True:
            stream_result = await _open_stream_with_failover(request, responses_body)
            if isinstance(stream_result, JSONResponse):
                return stream_result
            return StreamingResponse(
                _chat_completion_sse(stream_result, responses_body["model"]),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                    **_trace_headers(stream_result.response),
                    **_channel_response_headers(stream_result.channel, stream_result.failover_count),
                },
            )

        upstream = await _post_upstream_with_failover(request, responses_body)
        channel_headers = _channel_response_headers(upstream.channel, upstream.failover_count)
        if upstream.response.status_code >= 400:
            return _json_response_from_upstream(upstream.response, channel_headers)
        if not upstream.response.content:
            return _error_response(
                "Upstream returned an empty response",
                502,
                "upstream_empty_response",
                headers=channel_headers,
            )
        if "application/json" not in upstream.response.headers.get("content-type", "").lower():
            return _json_response_from_upstream(upstream.response, channel_headers)

        try:
            responses_json = upstream.response.json()
        except json.JSONDecodeError:
            return _error_response(
                "Upstream returned invalid JSON",
                502,
                "upstream_invalid_json",
                headers=_merge_headers(_trace_headers(upstream.response), channel_headers),
            )
        if not isinstance(responses_json, dict):
            return _error_response(
                "Upstream JSON response must be an object",
                502,
                "upstream_invalid_response",
                headers=channel_headers,
            )
        return JSONResponse(
            content=_responses_to_chat_completion(responses_json, responses_body["model"]),
            headers=_merge_headers(_trace_headers(upstream.response), channel_headers),
        )
    except GatewayError as exc:
        return _gateway_error_response(exc)


@app.post("/admin/fingerprint/reset")
async def reset_fingerprint(request: Request):
    _require_admin(request)
    state = _reset_gateway_state()
    return {"state": _public_state(state)}


@app.get("/admin/channels")
async def list_channels(request: Request):
    _require_admin(request)
    with _connect_db() as conn:
        rows = _list_channel_rows(conn)
        return {"object": "list", "data": [_public_channel(row, conn) for row in rows]}


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
            INSERT INTO channels(id, name, upstream_url, priority, enabled, downstream_api_key, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                channel_id,
                values["name"],
                values["upstream_url"],
                values["priority"],
                values["enabled"],
                values["downstream_api_key"],
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
async def get_channel(request: Request, channel_id: str):
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
async def delete_channel(request: Request, channel_id: str):
    _require_admin(request)
    with _connect_db() as conn:
        _get_channel_row(conn, channel_id)
        conn.execute("DELETE FROM channels WHERE id = ?", (channel_id,))
        conn.commit()
    return {"deleted": True, "id": channel_id}


@app.post("/admin/channels/{channel_id}/enable")
async def enable_channel(request: Request, channel_id: str):
    _require_admin(request)
    with _connect_db() as conn:
        _get_channel_row(conn, channel_id)
        conn.execute("UPDATE channels SET enabled = 1, updated_at = ? WHERE id = ?", (_utc_now(), channel_id))
        conn.commit()
        return _public_channel(_get_channel_row(conn, channel_id), conn)


@app.post("/admin/channels/{channel_id}/disable")
async def disable_channel(request: Request, channel_id: str):
    _require_admin(request)
    with _connect_db() as conn:
        _get_channel_row(conn, channel_id)
        conn.execute("UPDATE channels SET enabled = 0, updated_at = ? WHERE id = ?", (_utc_now(), channel_id))
        conn.commit()
        return _public_channel(_get_channel_row(conn, channel_id), conn)


@app.post("/admin/channels/{channel_id}/reset-runtime")
async def reset_channel_runtime(request: Request, channel_id: str):
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


@app.get("/admin/channels/{channel_id}/stats")
async def channel_stats(request: Request, channel_id: str):
    _require_admin(request)
    with _connect_db() as conn:
        row = _get_channel_row(conn, channel_id)
        return _public_channel(row, conn)


@app.get("/admin/channel-stats")
async def all_channel_stats(request: Request):
    _require_admin(request)
    with _connect_db() as conn:
        rows = _list_channel_rows(conn)
        return {"object": "list", "data": [_public_channel(row, conn) for row in rows]}
