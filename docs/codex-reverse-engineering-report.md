# Codex 客户端指纹逆向研究报告

> 基于 openai/codex 仓库源码逆向分析，涵盖 User-Agent 构造、HTTP 请求头体系、Responses API 请求/响应结构、鉴权流程。
>
> 分析日期：2026-06-09
>
> 适用场景：编写 Codex 兼容客户端、API 中转网关、协议转换代理、风控绕过等。

---

## 一、数据来源

以下源码文件来自 openai/codex 仓库（Rust，仓库名 codex-rs）：

| 文件路径 | 核心内容 |
|----------|---------|
| `codex-rs/login/src/auth/default_client.rs` | UA 构造、originator 单例、全局客户端状态 |
| `codex-rs/core/src/model_provider_info.rs` | 请求 builder、HTTP header 注入、provider 配置模型 |
| `codex-rs/core/src/client.rs` | Responses API 请求构造、session/thread header 管理、WebSocket/SSE 分流 |
| `codex-rs/terminal-detection/src/lib.rs` | 终端环境检测、UA token 生成、tmux/zellij 多路复用感知 |
| `codex-rs/codex-api/src/requests/headers.rs` | session_id/thread_id header 构造（含中划线变体） |
| `codex-rs/backend-client/src/client.rs` | ChatGPT 后端 API 客户端、鉴权 header 管理 |
| `codex-rs/core/src/api_bridge.rs` | API 错误分类、响应头解析（request-id、cf-ray、鉴权错误等） |
| `codex-rs/cloud-tasks/src/util.rs` | AuthManager 加载、ChatGPT header 构建 |

---

## 二、User-Agent 构造逻辑

### 2.1 模板

```
{originator}/{version} ({os_type} {os_version}; {architecture}) {terminal_ua} [(suffix)]
```

来源：`default_client.rs:133-157`，函数 `get_codex_user_agent()`。代码逐行构造：

```rust
pub fn get_codex_user_agent() -> String {
    let build_version = env!("CARGO_PKG_VERSION");
    let os_info = os_info::get();
    let originator = originator();
    let prefix = format!(
        "{}/{build_version} ({} {}; {}) {}",
        originator.value.as_str(),
        os_info.os_type(),
        os_info.version(),
        os_info.architecture().unwrap_or("unknown"),
        user_agent()
    );
    let suffix = USER_AGENT_SUFFIX.lock().ok()
        .and_then(|guard| guard.clone())
        .as_deref()
        .map(str::trim)
        .filter(|v| !v.is_empty())
        .map_or_else(String::new, |v| format!(" ({v})"));
    sanitize_user_agent(format!("{prefix}{suffix}"), &prefix)
}
```

整个字符串经过 `sanitize_user_agent()` 处理，将非法 header 字符替换为 `_`，解析失败则回退到无 suffix 的 prefix。

### 2.2 七个字段的取值

#### originator

客户端类型标识，默认 `codex_cli_rs`。

读取优先级：
1. 环境变量 `CODEX_INTERNAL_ORIGINATOR_OVERRIDE`
2. `set_default_originator()` 调用（仅可设一次，之后返回 `AlreadyInitialized` 错误）
3. 默认值 `codex_cli_rs`

已知 originator 完整列表及使用方：

| originator | 使用方 |
|-----------|-------|
| `codex_cli_rs` | Codex CLI（Rust），默认值 |
| `codex-tui` | Codex CLI TUI 模式 |
| `codex_vscode` | VS Code 扩展 |
| `codex_app_server` | App Server 子进程 |
| `codex_atlas` | 桌面端 Atlas shell |
| `codex_chatgpt_desktop` | 桌面端 ChatGPT shell |
| `Codex App/{version}` | 桌面 App 独立模式下 |

First-party 判定（`is_first_party_originator()`）：
- 等于 `codex_cli_rs` 或 `codex-tui` 或 `codex_vscode`
- 或以 `Codex ` 开头

#### version

来源：`env!("CARGO_PKG_VERSION")`，即 Cargo.toml 中的版本号。

实际部署中会随每次 release 更新。建议伪装时与真实 Codex 发布版本保持同步。

#### os_type

来源：`os_info::get().os_type()`

取值：`Linux`、`Mac OS`、`Darwin`、`Windows`

#### os_version

来源：`os_info::get().version()`

示例：`6.8.0`（Linux 内核）、`24.1.0`（macOS）、`10.0`（Windows）

#### architecture

来源：`os_info::get().architecture()`

取值：`x86_64`、`aarch64`、`arm64`。获取失败兜底 `unknown`。

#### terminal_ua

来源：`codex_terminal_detection::user_agent()`，详见 §2.3。

#### suffix（可选）

来源：全局 `USER_AGENT_SUFFIX` 静态变量。

- 主要用途：MCP 客户端区分；app-server 模式下填入 `{clientName}; {version}`
- 格式：` (suffix)`，自动包裹括号
- 空后缀不输出

### 2.3 终端检测逻辑

来源：`codex-rs/terminal-detection/src/lib.rs`，函数 `detect_terminal_info_from_env()`

13 级优先级链：

| 优先级 | 条件 | 输出 |
|--------|------|------|
| 1 | `TERM_PROGRAM=tmux` + tmux 激活 | 通过 `tmux display-message` 获取底层终端 |
| 2 | `TERM_PROGRAM` 存在 | `{program}/{version}`（如 `iTerm.app/3.6.6`） |
| 3 | `WEZTERM_VERSION` | WezTerm |
| 4 | `ITERM_SESSION_ID` | iTerm2 |
| 5 | `TERM_SESSION_ID` | Apple Terminal |
| 6 | `KITTY_WINDOW_ID` / `TERM` 含 `kitty` | Kitty |
| 7 | `ALACRITTY_SOCKET` | Alacritty |
| 8 | `KONSOLE_VERSION` | Konsole |
| 9 | `GNOME_TERMINAL_SCREEN` | GNOME Terminal |
| 10 | `VTE_VERSION` | VTE |
| 11 | `WT_SESSION` | Windows Terminal |
| 12 | `TERM` 环境变量 | TERM 值 |
| 13 | 全部未命中 | `unknown` |

**终端名枚举**：`AppleTerminal`、`Ghostty`、`Iterm2`、`WarpTerminal`、`VsCode`、`WezTerm`、`Kitty`、`Alacritty`、`Konsole`、`GnomeTerminal`、`Vte`、`WindowsTerminal`、`Dumb`、`Unknown`

**多路复用感知**：tmux 用户会通过 `tmux display-message #{client_termtype}` 和 `#{client_termname}` 还原底层终端，而非报告 `tmux`。zellij 类似。

### 2.4 UA 示例

```
codex_cli_rs/0.148.0 (Linux 6.8.0; x86_64) kitty
codex_cli_rs/0.148.0 (Linux 6.8.0; x86_64) xterm-256color
codex_cli_rs/0.148.0 (Mac OS 24.1.0; aarch64) iTerm.app/3.6.6
codex_cli_rs/0.148.0 (Windows 10.0; x86_64) WindowsTerminal
codex_vscode/0.148.0 (Linux 6.8.0; x86_64) vscode/1.107.1
Codex App/0.148.0 (Darwin 24.1.0; arm64) Apple_Terminal/455.1
codex_cli_rs/0.148.0 (Linux 6.8.0; x86_64) kitty (codex-app-server-tests; 0.1.0)
```

### 2.5 生成 UA 的参考实现

```python
import os, platform

def codex_user_agent(
    originator="codex_cli_rs",
    version="0.148.0",
    terminal="unknown",
    suffix=None
):
    os_type = platform.system()        # Linux / Darwin / Windows
    os_version = platform.release()    # 6.8.0 / 24.1.0
    arch = platform.machine().lower()  # x86_64 / aarch64
    base = f"{originator}/{version} ({os_type} {os_version}; {arch}) {terminal}"
    if suffix:
        base += f" ({suffix})"
    # 非法 header 字符归一化为 _
    return "".join(c if c.isascii() and (c.isalnum() or c in "-_ ./;()") else "_" for c in base)
```

---

## 三、HTTP 请求头体系

### 3.1 Provider 层

来源：`model_provider_info.rs`

每次请求必定携带，在 `HttpClient` builder 阶段注入：

| Header | 值 | 来源 |
|--------|-----|------|
| `originator` | `codex_cli_rs` | originator 单例 |
| `version` | `0.148.0` 等 | `CARGO_PKG_VERSION` |

条件携带（通过 `env_http_headers` 机制，环境变量存在且非空时发送）：

| Header | 环境变量 |
|--------|---------|
| `OpenAI-Organization` | `OPENAI_ORGANIZATION` |
| `OpenAI-Project` | `OPENAI_PROJECT` |

### 3.2 Responses API 请求层

来源：`client.rs`，函数 `build_responses_headers()` 及周边。

#### 身份相关

| Header | 携带条件 | 格式 |
|--------|---------|------|
| `User-Agent` | 始终 | 见 §2 |
| `Authorization` | 始终 | `Bearer {token}` |
| `Content-Type` | 始终 | `application/json` |
| `x-codex-installation-id` | 始终 | 固定 UUID（读自 `~/.codex/installation_id`） |
| `x-codex-window-id` | 始终 | `{thread_id}:{window_generation}` |

#### 会话/线程

| Header | 说明 |
|--------|------|
| `session_id` | 会话 UUID，`sess_` 前缀 |
| `session-id` | 中划线变体（兼容不支持 underscore 的系统） |
| `thread_id` | 线程 UUID，`thread_` 前缀 |
| `thread-id` | 中划线变体 |
| `x-client-request-id` | 等于 thread_id |

> `session-id` / `thread-id` 的中划线变体来自 commit `bd8fc9a`（2026-05-08），原因是部分中间件/代理会规范化或拒绝 underscore 头的名字。

#### 状态路由

| Header | 携带条件 | 说明 |
|--------|---------|------|
| `x-codex-turn-state` | 同 turn 内 | 服务器下发，原样回传，保证 sticky routing |
| `x-codex-turn-metadata` | 可选 | turn 元信息 |

#### Sub-agent / 特殊标记

| Header | 携带条件 | 值 |
|--------|---------|-----|
| `x-openai-subagent` | sub-agent 启动 | `review` 或自定义 task 名 |
| `x-codex-parent-thread-id` | sub-agent 启动 | 父线程 ID |
| `x-openai-memgen-request` | memory consolidation | `true` |

#### 特性开关

| Header | 携带条件 | 值 |
|--------|---------|-----|
| `OpenAI-Beta` | WebSocket 传输 | `responses_websockets=2026-02-06` |
| `x-responsesapi-include-timing-metrics` | 时间度量开启 | `true` |

#### 设备认证

| Header | 携带条件 |
|--------|---------|
| `x-oai-attestation` | attestation provider 已配置且启用 |

### 3.3 ChatGPT 后端 API 层

来源：`backend-client/src/client.rs:headers()`

使用 ChatGPT OAuth 模式时额外发送：

| Header | 携带条件 |
|--------|---------|
| `ChatGPT-Account-Id` | OAuth 认证且 token 可解码出账户 ID |
| `X-OpenAI-Fedramp` | FedRAMP 环境，固定值 `true` |

---

## 四、请求体结构

### 4.1 基础结构

```json
{
  "model": "gpt-5.5",
  "input": [
    {
      "role": "user",
      "content": [
        {"type": "input_text", "text": "hello"}
      ]
    }
  ],
  "instructions": "You are a helpful assistant.",
  "stream": true,
  "client_metadata": {
    "x-codex-installation-id": "...",
    "x-codex-window-id": "..."
  }
}
```

### 4.2 完整参数

| 字段 | 类型 | 说明 |
|------|------|------|
| `model` | string | 模型名 |
| `input` | array | ResponseInputItem 列表 |
| `instructions` | string | 系统指令 |
| `stream` | bool | 是否 SSE 流式 |
| `store` | bool | 持久化存储 |
| `previous_response_id` | string | 多轮对话上下文链 |
| `reasoning` | object | `{effort: "low"|"medium"|"high", summary: "auto"|"concise"|"detailed"|"none"}` |
| `text` | object | `{format: {type: "text"}, verbosity: "low"|"medium"|"high"}` |
| `tools` | array | 工具列表 |
| `tool_choice` | string | 工具选择策略 |
| `temperature` | number | 采样温度 |
| `top_p` | number | top-p |
| `frequency_penalty` | number | 频率惩罚 |
| `presence_penalty` | number | 存在惩罚 |
| `max_output_tokens` | int | 最大输出 token |
| `service_tier` | string | `auto` / `default` |
| `parallel_tool_calls` | bool | 并行工具调用 |
| `truncation` | string | 上下文截断策略 |
| `top_logprobs` | int | top logprobs 数量 |
| `client_metadata` | object | 客户端元数据 |
| `include` | array | 额外返回内容项，推理模型常用 `["reasoning.encrypted_content"]` |

> **重要**：部分中转服务（如 anyrouter）对推理模型（如 gpt-5.5）强制要求请求体携带 `include: ["reasoning.encrypted_content"]`，缺失时返回 `400 invalid_responses_request`（错误信息 `invalid codex request`）。这与 TLS 指纹、请求头无关，纯粹是请求体字段校验。对不校验该字段的模型注入此字段也无害。

### 4.3 input 消息格式

```json
{
  "role": "user",
  "content": [
    {"type": "input_text", "text": "..."},
    {"type": "input_image", "image_url": "..."}
  ]
}
```

支持的 role：`user`、`assistant`、`developer`、`system`

支持的 content 类型：`input_text`、`input_image`、`output_text`、`refusal`

### 4.4 client_metadata

来源：`client.rs:build_ws_client_metadata()` 及测试中的断言检查。

Codex 发送的 `client_metadata` 包含：

| 字段 | 说明 |
|------|------|
| `x-codex-installation-id` | 安装 UUID（设备标识） |
| `x-codex-window-id` | `{thread_id}:{generation}` 格式 |
| `x-openai-subagent` | sub-agent 标识（可选） |
| `x-codex-parent-thread-id` | 父线程 ID（可选） |
| `x-codex-turn-metadata` | turn 元数据（可选） |

`client_metadata` 同时出现在 HTTP headers 和请求 body 中（header 为 `x-codex-installation-id` 等独立 header，body 中为 `client_metadata` 对象）。

---

## 五、鉴权流程

### 5.1 API Key 模式

```http
Authorization: Bearer {openai_api_key}
```

- Key 来源：环境变量 `OPENAI_API_KEY` 或 `CODEX_API_KEY`
- 直接请求 `/v1/responses`

### 5.2 ChatGPT OAuth 模式

```http
Authorization: Bearer {chatgpt_access_token}
ChatGPT-Account-Id: {account_id}
```

**流程**：
1. OAuth PKCE：浏览器登录 → callback → 获取 access_token + refresh_token
2. Token 缓存于 `~/.codex/auth.json`（明文 JSON）
3. 自动刷新：token 到期前 5 分钟自动刷新
4. 请求 `https://chatgpt.com/backend-api/codex/responses`

**auth.json 结构**：
```json
{
  "access_token": "...",
  "refresh_token": "...",
  "account": {
    "id": "...",
    "email": "..."
  }
}
```

### 5.3 Credential Store 配置

通过 `cli_auth_credentials_store` 控制：
- `file` — `{CODEX_HOME}/auth.json`
- `keyring` — 系统凭据存储
- `auto` — 优先 keyring，不可用时 fallback file

### 5.4 请求 URL 路径

- API Key 模式：`{base_url}/v1/responses`
- ChatGPT OAuth 模式：`{chatgpt_base_url}/backend-api/codex/responses`

其中 `chatgpt_base_url` 默认为 `https://chatgpt.com`，但可通过配置覆盖（如指向自定义代理）。

---

## 六、SSE 流式响应协议

### 6.1 完整事件流

```
event: response.created
data: {"type":"response.created","response":{"id":"resp_...","status":"in_progress",...}}

event: response.in_progress
data: {"type":"response.in_progress","response":{...}}

event: response.metadata
data: {"type":"response.metadata","metadata":{}}

event: response.output_item.added
data: {"type":"response.output_item.added","item":{"type":"reasoning",...},"output_index":0}

event: response.output_item.done
data: {"type":"response.output_item.done","item":{...},"output_index":0}

event: response.output_item.added
data: {"type":"response.output_item.added","item":{"type":"message",...},"output_index":1}

event: response.content_part.added
data: {"type":"response.content_part.added","part":{"type":"output_text","text":""}}

event: response.output_text.delta
data: {"type":"response.output_text.delta","delta":"Hello","item_id":"msg_..."}

event: response.output_text.done
data: {"type":"response.output_text.done","text":"Hello world"}

event: response.content_part.done
data: {"type":"response.content_part.done","part":{"type":"output_text",...}}

event: response.output_item.done
event: response.completed
data: {"type":"response.completed","response":{"status":"completed","usage":{...}}}
```

### 6.2 事件类型

| 事件 | 说明 |
|------|------|
| `response.created` | 响应创建，含 response.id |
| `response.in_progress` | 进行中 |
| `response.metadata` | 元数据更新 |
| `response.output_item.added` | 新增推理/消息块 |
| `response.content_part.added` | 新增内容块 |
| `response.output_text.delta` | 增量文本 |
| `response.output_text.done` | 文本块完结，含完整 text |
| `response.content_part.done` | 内容块完结 |
| `response.output_item.done` | 推理/消息块完结 |
| `response.completed` | 响应完结，含完整 response + usage |
| `response.error` | 错误事件 |

### 6.3 输出类型

| 类型 | 内容载体 |
|------|---------|
| `reasoning` | `content[]`（明文）和 `encrypted_content`（加密） |
| `message` | `content[].text`，role 为 `assistant`，phase 为 `final_answer` |

### 6.4 completed 响应元数据

```json
{
  "id": "resp_...",
  "object": "response",
  "status": "completed",
  "model": "gpt-5.5",
  "output": [...],
  "usage": {
    "input_tokens": 310,
    "input_tokens_details": {"cached_tokens": 0},
    "output_tokens": 34,
    "output_tokens_details": {"reasoning_tokens": 9},
    "total_tokens": 344
  },
  "previous_response_id": null,
  "service_tier": "auto"
}
```

---

## 七、错误响应结构

来源：`api_bridge.rs`，返回的 JSON error 格式：

```json
{
  "error": {
    "message": "...",
    "type": "invalid_request_error",
    "param": null,
    "code": "..."
  }
}
```

### 错误分类

| HTTP 状态码 | 业务 code | 说明 |
|------------|----------|------|
| 400 | `invalid_responses_request`** | 请求体不符合上游对 Responses API 的校验 |
| 401 | — | 鉴权失败或 token 过期 |
| 403 | `codex_access_restricted`* | 客户端版本/身份被拒 |
| 429 | `usage_limit_reached` | 速率限制 |
| 429 | `usage_not_included` | 用量未包含在订阅中 |
| 500 | — | 服务内部错误 |

> *`codex_access_restricted` 是已知的错误代码，某些服务端使用它来强制要求客户端携带特定标识。绕过该检测的关键在于请求体中提供 `client_metadata` 字段。

> **`invalid_responses_request`（错误信息常显示为 "invalid codex request"）是部分中转服务（如 anyrouter）对推理类模型的强校验。实测结论：**对 gpt-5.5 这类推理模型，请求体必须包含 `"include": ["reasoning.encrypted_content"]`，否则一律返回 400。** 此校验与 TLS 指纹、User-Agent、originator、session/thread header 均无关——实测仅凭最简请求体加上该 `include` 字段即可成功调用，无需任何 codex 伪装 header。对不做此校验的模型（如 gpt-5.4/gpt-5.3）注入该字段无副作用。详见 §4.2 的 `include` 字段说明。

### 响应头追踪

| 响应头 | 说明 |
|--------|------|
| `x-request-id` | 上游请求 ID |
| `x-oai-request-id` | OpenAI 内部 ID |
| `cf-ray` | Cloudflare Ray ID |
| `x-codex-active-limit` | 活跃速率限制标识 |
| `x-openai-authorization-error` | 鉴权错误详情 |

---

## 八、完整请求示例

### 8.1 最小请求

```http
POST /v1/responses HTTP/2
Host: chatgpt.com
content-type: application/json
authorization: Bearer {key_or_token}
user-agent: codex_cli_rs/0.148.0 (Linux 6.8.0; x86_64) unknown
originator: codex_cli_rs
version: 0.148.0

{
  "model": "gpt-5.5",
  "instructions": "You are a helpful assistant.",
  "input": [
    {"role": "user", "content": [{"type": "input_text", "text": "hello"}]}
  ],
  "client_metadata": {
    "x-codex-installation-id": "d818825a-972a-5a7a-901e-fe4ce7912668",
    "x-codex-window-id": "thread_001:0"
  }
}
```

### 8.2 带完整 session/thread 头的请求

```http
POST /v1/responses HTTP/2
Host: chatgpt.com
content-type: application/json
authorization: Bearer {token}
user-agent: codex_cli_rs/0.148.0 (Linux 6.8.0; x86_64) kitty
originator: codex_cli_rs
version: 0.148.0
session-id: sess_dd3e8a7c
thread-id: thread_dd3e8a7c
x-client-request-id: thread_dd3e8a7c
x-codex-installation-id: d818825a-972a-5a7a-901e-fe4ce7912668
x-codex-window-id: thread_dd3e8a7c:0

{...同上 body...}
```

### 8.3 响应（非流式）

```json
{
  "id": "resp_...",
  "object": "response",
  "status": "completed",
  "model": "gpt-5.5",
  "output": [
    {
      "type": "message",
      "status": "completed",
      "content": [{"type": "output_text", "text": "Hi!"}],
      "role": "assistant"
    }
  ],
  "usage": {
    "input_tokens": 310,
    "output_tokens": 34,
    "output_tokens_details": {"reasoning_tokens": 9},
    "total_tokens": 344
  }
}
```

### 8.4 响应（SSE 流式）

```
event: response.created
data: {"type":"response.created","response":{"id":"resp_...","status":"in_progress",...}}

event: response.output_item.added
data: {"type":"response.output_item.added","item":{"type":"message",...},"output_index":0}

event: response.content_part.added
data: {"type":"response.content_part.added","part":{"type":"output_text","text":""}}

event: response.output_text.delta
data: {"type":"response.output_text.delta","delta":"Hi","item_id":"msg_..."}

event: response.output_text.delta
data: {"type":"response.output_text.delta","delta":"!","item_id":"msg_..."}

event: response.output_text.done
data: {"type":"response.output_text.done","text":"Hi!"}

event: response.completed
data: {"type":"response.completed","response":{"status":"completed","usage":{...}}}
```

---

## 九、伪装要点总结

### 9.1 关键伪装维度

| 维度 | 必要程度 | 说明 |
|------|---------|------|
| `Authorization: Bearer` | 必须 | 鉴权令牌 |
| `Content-Type: application/json` | 必须 | 标准 HTTP |
| `include: ["reasoning.encrypted_content"]`（请求体） | **关键** | 调用推理模型（如 gpt-5.5）时部分中转必须携带，缺失直接返回 `400 invalid_responses_request`。实测 anyrouter 上加此字段即可成功，连 codex headers 都不需要 |
| `client_metadata`（请求体） | 中 | 部分服务端会检测，但非 anyrouter gpt-5.5 的关键因素 |
| `User-Agent` | 中 | 符合 §2 格式可降低异常标记概率 |
| `originator` | 中 | 固定为已知有效值 |
| `version` | 中 | 与真实 Codex 版本对齐 |
| `session-id` / `thread-id` | 低 | 多轮对话可能需要，单轮可省略 |
| `x-codex-installation-id` | 低 | 可作为 header 发送，也可仅放在 body 的 `client_metadata` 中 |
| `x-codex-window-id` | 低 | `{thread_id}:0` 格式 |
| `OpenAI-Beta` | 低 | 仅 WebSocket 模式需要 |
| `x-openai-subagent` 等 | 低 | 非 sub-agent 场景可省略 |
| TLS 指纹 | 低 | Rust reqwest 的 TLS 指纹不同于 Python/curl |

### 9.2 伪装等级

**最低伪装（针对纯 API Key 校验）**：
```http
POST /v1/responses
Authorization: Bearer {key}
Content-Type: application/json
```

**基础伪装（绕过简单 client 检查）**：
在最低伪装基础上，请求体加 `client_metadata`：
```json
{"client_metadata": {"x-codex-installation-id": "any-non-empty-value"}}
```

**推理模型伪装（关键，实测于 anyrouter gpt-5.5）**：
调用 gpt-5.5 等推理模型时，请求体必须携带 `include`，否则返回 `400 invalid_responses_request`：
```json
{
  "model": "gpt-5.5",
  "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]}],
  "stream": true,
  "include": ["reasoning.encrypted_content"]
}
```
> 实测结论：在 anyrouter 上，仅添加 `include` 字段即可成功调用 gpt-5.5（流式），无需 codex headers、无需 TLS 指纹对齐。对不校验该字段的模型（如 gpt-5.4）注入此字段无害。早期"gpt-5.5 需要 Rust TLS 指纹"的推测已被推翻——真正原因是缺少 `include`。

**完整伪装（模拟真实 Codex CLI）**：
- 添加 `originator: codex_cli_rs`
- 添加完整 `User-Agent`（含系统信息 + 终端标识）
- 添加 `version` header
- 添加 session/thread header 对
- 请求体包含完整的 `client_metadata`

**深度伪装（绕过高级检测）**：
- 以上全部
- TLS 指纹对齐（使用 Rust reqwest 或 curl-impersonate）
- HTTP/2 优先
- 请求频率和行为模式符合真人操作

### 9.3 client_metadata 策略建议

- 使用**确定性 UUID**（如 `uuid5(NAMESPACE_DNS, "identifier")`），保证重启后不变
- 至少包含 `x-codex-installation-id`
- 建议同时包含 `x-codex-window-id`（`{thread_id}:0` 格式）
- 避免使用明显异常的值如 `"test"`、空字符串等
