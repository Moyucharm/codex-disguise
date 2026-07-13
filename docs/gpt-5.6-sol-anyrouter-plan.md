# gpt-5.6-sol AnyRouter 兼容实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 让网关对 `gpt-5.6-sol` 的处理回到官方 Codex 可证明的裸模型名路径，并用可验证的探测结果判断 AnyRouter 是否支持 GPT-5.6 Codex Lite 协议。

**架构：** 不再把 `gpt-5.6-sol` 改写为 `openai/gpt-5.6-sol`。网关只做两类明确行为：保留官方模型 slug 和透传/补齐有源码依据的 Codex 请求形态；对 AnyRouter 的能力缺口通过探测脚本和文档明确记录，而不是用未列出的模型名绕行。

**技术栈：** Python、FastAPI、httpx、unittest、OpenAI Codex Responses API、AnyRouter `/v1/responses` 与 `/v1/models`。

---

## 已验证事实

- AnyRouter `GET https://anyrouter.top/v1/models` 只暴露 `gpt-5.6-sol`，没有 `openai/gpt-5.6-sol`。
- AnyRouter `GET https://anyrouter.top/v1/models?client_version=0.144.1` 同样只返回 OpenAI 兼容 `{data}` 形态，`gpt-5.6-sol` 只有 `id`，没有 `use_responses_lite`、`tool_mode`、`multi_agent_version`、`minimal_client_version` 等 Codex 模型元数据。
- AnyRouter `https://anyrouter.top/backend-api/codex/models` 返回 HTML challenge，不是可用 JSON 模型目录。
- 官方 Codex 模型目录中 `gpt-5.6-sol` 的 slug 是裸 `gpt-5.6-sol`，且包含 `tool_mode: "code_mode_only"`、`multi_agent_version: "v2"`、`use_responses_lite: true`、`minimal_client_version: "0.144.0"`。
- 官方 Codex `build_responses_request` 使用 `model_info.slug.clone()` 填充 request `model` 字段。公开源码证据不支持把 `gpt-5.6-sol` 改写为 `openai/gpt-5.6-sol`。
- `openai/gpt-5.6-sol` 在 AnyRouter 上不再触发 `invalid codex request`，只能证明它走到了不同路由或 fallback 分支；它不在 AnyRouter 模型列表中，也不是官方 Codex 请求形态。
- 官方 Codex 对 `use_responses_lite=true` 的请求形态是：把工具 schema 放进 `input[0]` 的 `type: "additional_tools"` developer item，清空顶层 `tools`，设置 `parallel_tool_calls=false`，并发送 Responses Lite 标记。
- 官方 Codex HTTP 请求会发送 `x-openai-internal-codex-responses-lite: true`。WebSocket 请求会在 `client_metadata` 中放入 `ws_request_header_x_openai_internal_codex_responses_lite: "true"`。
- 官方 GPT-5.6 Sol Lite 工具集合至少包括 `exec`、`wait`、`request_user_input`、`collaboration` namespace。`collaboration` namespace 下包含 multi-agent v2 的 `spawn_agent`、`send_message`、`followup_task`、`wait_agent`、`interrupt_agent`、`list_agents`。
- 公开 issue 显示 Azure/custom provider 对 GPT-5.6 的失败常见根因是 Codex Lite header、`additional_tools` 和 `collaboration` namespace 不兼容；这支持“第三方 Responses provider 未完整支持 OpenAI-hosted Codex backend 协议”的判断。

## 已排除假设

- **排除：** 将 AnyRouter 上游请求模型名从 `gpt-5.6-sol` 改写成 `openai/gpt-5.6-sol`。
- **原因：** 该模型名不在 AnyRouter `/v1/models`，也没有官方 Codex 源码依据；它只是绕过了某个 AnyRouter 校验分支，不能作为真实 Codex 伪装修复。
- **影响：** 当前工作树中与 `_body_for_upstream_channel()`、`openai/gpt-5.6-sol` 相关的代码和测试需要撤销或重写。

## 根因判断

当前证据更符合以下判断：AnyRouter 公开模型目录暴露了裸 `gpt-5.6-sol`，但它的 Responses/Codex 校验或上游渠道对请求 shape 非常敏感。手写或不完整 Codex Lite schema 会返回 `400 invalid_responses_request` / `invalid codex request`；使用 Codex CLI 0.144.2 capture 得到的精确 `additional_tools` schema 后，请求可越过 invalid-codex 校验，但当前落入 `500 get_channel_failed` / 模型负载达到上限，尚未证明可用完成。

这不是模型名拼写问题。后续实现必须先保证网关发送的模型名和官方 Codex 一致，再用探测脚本证明 AnyRouter 是接受完整 Lite shape、接受非 Lite workaround，还是当前服务端不兼容。

## 文件结构

- 修改：`main.py`
  - 移除 AnyRouter 专属模型名改写逻辑。
  - 保留或调整 `gpt-5.6-sol` 的请求 shape 注入，但只能注入有官方源码依据的字段。
  - 如果实现 Lite shape，逻辑应集中在 `_ensure_codex_request_shape()` 或一个命名清晰的小函数中，不要散落到发送路径。
- 修改：`tests/test_codex_request_shape.py`
  - 删除断言 `openai/gpt-5.6-sol` 的测试。
  - 增加裸模型名保持不变的回归测试。
  - 增加 GPT-5.6 请求 shape 的单元测试。
- 修改：`scripts/probe_anyrouter_codex_shape.py`
  - 删除或不要新增 `with_anyrouter_provider_model()`。
  - 增加官方 Lite shape 探测变体。
  - 增加 AnyRouter `/v1/models` 能力探测输出，但不能打印 API key。
- 修改：`tests/test_probe_anyrouter_codex_shape.py`
  - 删除 `test_anyrouter_provider_model_keeps_codex_shape_but_changes_model`。
  - 增加模型列表解析和 Lite shape builder 的测试。
- 可选修改：`docs/codex-reverse-engineering-report.md`
  - 在已有逆向报告中补充 GPT-5.6 Sol 的 Lite 协议结论。
  - 只有在实现代码稳定后再更新，避免文档先行记录未验证行为。

---

## 任务 1：撤销错误的 AnyRouter 模型名前缀改写

**文件：**
- 修改：`main.py`
- 修改：`tests/test_codex_request_shape.py`
- 修改：`tests/test_probe_anyrouter_codex_shape.py`

- [x] **步骤 1：删除失败假设测试**

从 `tests/test_codex_request_shape.py` 删除这个测试：

```python
def test_gpt_56_sol_uses_anyrouter_provider_model_upstream_without_mutating_client_body(self):
    body = {
        "model": "gpt-5.6-sol",
        "input": "ping",
        "prompt_cache_key": "client-owned-cache-key",
    }
    channel = {"upstream_url": "https://anyrouter.top/v1"}

    upstream_body = main._body_for_upstream_channel(body, channel)

    self.assertEqual(upstream_body["model"], "openai/gpt-5.6-sol")
    self.assertEqual(upstream_body["prompt_cache_key"], "client-owned-cache-key")
    self.assertEqual(body["model"], "gpt-5.6-sol")
```

从 `tests/test_probe_anyrouter_codex_shape.py` 删除这个测试：

```python
def test_anyrouter_provider_model_keeps_codex_shape_but_changes_model(self):
    body = probe.with_anyrouter_provider_model(
        probe.with_reasoning_all_turns(
            probe.with_codex_body_min(probe.base_body("gpt-5.6-sol"))
        )
    )

    self.assertEqual(body["model"], "openai/gpt-5.6-sol")
    self.assertEqual(body["tool_choice"], "auto")
    self.assertEqual(body["reasoning"]["context"], "all_turns")
    self.assertIn("client_metadata", body)
```

- [x] **步骤 2：新增裸模型名保持不变测试**

在 `tests/test_codex_request_shape.py` 添加或保留这个语义的测试：

```python
def test_gpt_56_sol_keeps_bare_model_for_anyrouter_upstream(self):
    body = {
        "model": "gpt-5.6-sol",
        "input": "ping",
        "prompt_cache_key": "client-owned-cache-key",
    }
    channel = {"upstream_url": "https://anyrouter.top/v1"}

    upstream_body = main._body_for_upstream_channel(body, channel)

    self.assertIs(upstream_body, body)
    self.assertEqual(upstream_body["model"], "gpt-5.6-sol")
    self.assertEqual(upstream_body["prompt_cache_key"], "client-owned-cache-key")
```

- [ ] **步骤 3：运行单测确认当前失败**

运行：

```bash
.venv/bin/python -m unittest "tests.test_codex_request_shape" "tests.test_probe_anyrouter_codex_shape"
```

预期：至少 `test_gpt_56_sol_keeps_bare_model_for_anyrouter_upstream` 失败，因为当前 `main.py` 仍会把模型改成 `openai/gpt-5.6-sol`。

- [x] **步骤 4：删除生产代码中的模型名改写**

在 `main.py` 删除 `urlsplit` import：

```python
from urllib.parse import urlsplit
```

删除 `_body_for_upstream_channel()` 中的 AnyRouter 特判逻辑，把函数收敛成无改写函数：

```python
def _body_for_upstream_channel(body: dict[str, Any], channel: dict[str, Any]) -> dict[str, Any]:
    return body
```

如果没有后续 per-channel body transform 需求，也可以删除 `_body_for_upstream_channel()` 并把三处调用改回直接传 `body` / `test_body`。更小改动是保留函数但让它直接返回原 body。

- [x] **步骤 5：运行单测确认通过**

运行：

```bash
.venv/bin/python -m unittest "tests.test_codex_request_shape" "tests.test_probe_anyrouter_codex_shape"
```

预期：测试通过；不再有任何 `openai/gpt-5.6-sol` 断言。

---

## 任务 2：补齐 GPT-5.6 Sol 官方 Lite shape 的探测构造

**文件：**
- 修改：`scripts/probe_anyrouter_codex_shape.py`
- 修改：`tests/test_probe_anyrouter_codex_shape.py`

- [x] **步骤 1：为完整 Lite 工具集合编写失败测试**

在 `tests/test_probe_anyrouter_codex_shape.py` 添加：

```python
def test_full_lite_additional_tools_matches_official_tool_names(self):
    body = probe.with_full_lite_additional_tools(
        probe.with_lite_metadata(
            probe.with_reasoning_all_turns(
                probe.with_codex_body_min(probe.base_body("gpt-5.6-sol"))
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
```

- [ ] **步骤 2：运行测试确认失败**

运行：

```bash
.venv/bin/python -m unittest "tests.test_probe_anyrouter_codex_shape.AnyRouterProbeShapeTests.test_full_lite_additional_tools_matches_official_tool_names"
```

预期：FAIL，报错 `AttributeError: module 'scripts.probe_anyrouter_codex_shape' has no attribute 'with_full_lite_additional_tools'`。

- [x] **步骤 3：实现 `request_user_input` 探测工具 schema**

在 `scripts/probe_anyrouter_codex_shape.py` 添加：

```python
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
                }
            },
            "required": ["questions"],
            "additionalProperties": False,
        },
    }
```

- [x] **步骤 4：实现 `collaboration` namespace 探测工具 schema**

在 `scripts/probe_anyrouter_codex_shape.py` 添加最小 namespace schema。字段描述不需要完全复刻官方长文，但类型、名称和 namespace 结构必须保真：

```python
def collaboration_namespace_tool() -> dict[str, Any]:
    def function_tool(name: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
        return {
            "type": "function",
            "name": name,
            "description": f"Multi-agent v2 {name} tool.",
            "strict": False,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        }

    return {
        "type": "namespace",
        "name": "collaboration",
        "description": "Tools for spawning and managing sub-agents.",
        "tools": [
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
                "send_message",
                {"agent": {"type": "string"}, "message": {"type": "string"}},
                ["agent", "message"],
            ),
            function_tool(
                "followup_task",
                {"agent": {"type": "string"}, "message": {"type": "string"}},
                ["agent", "message"],
            ),
            function_tool(
                "wait_agent",
                {"agent": {"type": "string"}, "timeout_ms": {"type": "number"}},
                ["agent"],
            ),
            function_tool(
                "interrupt_agent",
                {"agent": {"type": "string"}},
                ["agent"],
            ),
            function_tool(
                "list_agents",
                {},
                [],
            ),
        ],
    }
```

- [x] **步骤 5：实现 `with_full_lite_additional_tools()`**

在 `scripts/probe_anyrouter_codex_shape.py` 添加：

```python
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
```

- [x] **步骤 6：运行探测脚本单测确认通过**

运行：

```bash
.venv/bin/python -m unittest "tests.test_probe_anyrouter_codex_shape"
```

预期：PASS。

---

## 任务 3：增加 AnyRouter 模型列表能力探测

**文件：**
- 修改：`scripts/probe_anyrouter_codex_shape.py`
- 修改：`tests/test_probe_anyrouter_codex_shape.py`

- [x] **步骤 1：为模型列表解析写失败测试**

在 `tests/test_probe_anyrouter_codex_shape.py` 添加：

```python
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
```

- [x] **步骤 2：实现模型列表解析函数**

在 `scripts/probe_anyrouter_codex_shape.py` 添加：

```python
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
                for key in ("id", "slug", "use_responses_lite", "tool_mode", "multi_agent_version", "minimal_client_version")
                if key in model
            }
        if model_id == "openai/gpt-5.6-sol":
            has_openai_prefixed = True

    return {
        "model_count": len(models),
        "gpt_56_sol": gpt_56_sol,
        "has_openai_prefixed_gpt_56_sol": has_openai_prefixed,
    }
```

- [x] **步骤 3：增加 CLI 选项但默认不额外请求**

在 `main_cli()` 里添加参数：

```python
parser.add_argument("--models", action="store_true")
```

在读取 `api_key` 后，如果 `args.models` 为真，执行只读模型列表请求并打印摘要：

```python
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
```

不要打印请求 headers，不要打印 `ANY_API_KEY`。

- [x] **步骤 4：运行单测确认通过**

运行：

```bash
.venv/bin/python -m unittest "tests.test_probe_anyrouter_codex_shape"
```

预期：PASS。

---

## 任务 4：用探测矩阵区分 AnyRouter 的三种可能行为

**文件：**
- 修改：`scripts/probe_anyrouter_codex_shape.py`

- [x] **步骤 1：更新探测变体**

在 `variants()` 中保留裸模型名，新增完整 Lite 变体，不新增 `openai/gpt-5.6-sol`：

```python
(
    "lite-additional-tools-full-version-144",
    "gpt-5.6-sol",
    lambda b: with_full_lite_additional_tools(
        with_lite_metadata(
            with_reasoning_all_turns(with_codex_body_min(b))
        )
    ),
    True,
    True,
    "0.144.0",
),
```

如果保留已有 `lite-additional-tools-real-min-*` 变体，名称要清楚表示它只包含 `exec` 和 `wait`，不是完整官方工具集合。

- [x] **步骤 2：运行真实探测**

运行：

```bash
.venv/bin/python scripts/probe_anyrouter_codex_shape.py --include-additional-tools-empty
```

预期：输出 JSON lines，不包含 API key。关注字段：`variant`、`status`、`invalid_codex`、`code`、`type`、`message`、`request_id`。

- [x] **步骤 3：根据结果分类**

按以下规则判断：

```text
如果 full Lite 变体返回 2xx 或进入 5xx get_channel_failed：AnyRouter 接受官方 Lite shape，生产代码可考虑补齐 Lite shape。
如果 full Lite 变体仍返回 400 invalid codex request：AnyRouter 当前不接受裸 gpt-5.6-sol 的官方 Lite shape，需要上游修复或另选非 Lite workaround。
如果关闭 Lite、移除 collaboration 的变体成功：可选择实现一个明确命名的 AnyRouter GPT-5.6 non-Lite adapter，但它不是官方 Codex 伪装，需要用户批准。
```

- [x] **步骤 4：把探测结果写回文档**

将真实探测输出摘要追加到 `docs/gpt-5.6-sol-anyrouter-plan.md` 的“探测结果记录”章节，只记录非敏感字段：

```markdown
## 探测结果记录

- `baseline-55-control`: status=..., code=..., invalid_codex=...
- `baseline-56-current`: status=..., code=..., invalid_codex=...
- `lite-additional-tools-full-version-144`: status=..., code=..., invalid_codex=...
- 结论：...
```

不要记录 Authorization header、API key、完整请求体中的任何敏感内容。

---

## 任务 5：生产请求 shape 的取舍点

**文件：**
- 修改：`main.py`
- 修改：`tests/test_codex_request_shape.py`

只有在任务 4 的真实探测证明某个 shape 被 AnyRouter 接受后，才执行本任务。不要提前实现。

### 方案 A：官方 Lite shape

适用条件：完整 Lite 变体不再返回 `invalid codex request`。

- [ ] **步骤 1：编写生产 shape 测试**

在 `tests/test_codex_request_shape.py` 添加：

```python
def test_gpt_56_sol_lite_shape_uses_bare_model_and_additional_tools(self):
    state = {
        "installation_id": "install_123",
        "session_id": "sess_123",
        "thread_id": "thread_123",
        "window_generation": 0,
    }
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

    shaped = main._ensure_codex_request_shape(body, state)

    self.assertEqual(shaped["model"], "gpt-5.6-sol")
    self.assertEqual(shaped["input"][0]["type"], "additional_tools")
    self.assertEqual(shaped["input"][0]["role"], "developer")
    self.assertNotIn("tools", shaped)
    self.assertEqual(shaped["tool_choice"], "auto")
    self.assertFalse(shaped["parallel_tool_calls"])
    self.assertEqual(shaped["reasoning"]["context"], "all_turns")
```

- [ ] **步骤 2：实现生产 shape**

将 `main._ensure_codex_request_shape()` 从当前最小字段注入改为官方 Lite shape：

```python
def _ensure_codex_request_shape(body: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    body = dict(body)
    if body.get("model") != "gpt-5.6-sol":
        return body

    body["model"] = "gpt-5.6-sol"
    body["input"] = _prepend_codex_lite_additional_tools(body.get("input") or [])
    body.pop("instructions", None)
    body.pop("tools", None)
    body.setdefault("tool_choice", "auto")
    body["parallel_tool_calls"] = False
    body.setdefault("store", False)
    body.setdefault("stream", False)
    body.setdefault("reasoning", {"effort": "medium", "summary": "auto", "context": "all_turns"})
    return body
```

`_prepend_codex_lite_additional_tools()` 必须避免重复插入：如果 `input[0].type == "additional_tools"`，直接返回原 input list 的浅拷贝。

- [ ] **步骤 3：补 Lite header**

如果生产请求由网关生成 Lite shape，应在 `_codex_headers()` 或发送前 header transform 中对 `gpt-5.6-sol` 增加：

```python
headers["x-openai-internal-codex-responses-lite"] = "true"
```

如果下游客户端已经传入该 header，应保留下游值，不要覆盖。

### 方案 B：非 Lite AnyRouter adapter

适用条件：官方 Lite shape 仍失败，但关闭 Lite / 移除 `collaboration` 的裸模型名变体成功。该方案不是官方 Codex 伪装，执行前必须让用户明确批准。

- [ ] **步骤 1：向用户确认 adapter 语义**

确认内容必须包含：

```text
AnyRouter 当前不接受官方 gpt-5.6-sol Lite shape。可实现一个 AnyRouter 专用 non-Lite adapter：保持 model=gpt-5.6-sol，但关闭 Lite header，使用顶层 tools 或移除 collaboration namespace。该方案是兼容 workaround，不是官方 Codex 真实请求形态。是否继续？
```

- [ ] **步骤 2：得到用户明确确认后再写测试和代码**

没有确认时，不执行本方案。

---

## 验证命令

实现阶段每个任务后运行对应最小测试，最终运行：

```bash
.venv/bin/python -m unittest "tests.test_codex_request_shape" "tests.test_probe_anyrouter_codex_shape"
```

需要真实 AnyRouter 探测时运行：

```bash
.venv/bin/python scripts/probe_anyrouter_codex_shape.py --models
.venv/bin/python scripts/probe_anyrouter_codex_shape.py --include-additional-tools-empty
```

真实探测必须遵守：不打印 API key，不打印 Authorization header，不把 `.env` 内容写入日志。

## 探测结果记录

当前已知记录：

- `/v1/models`: `status=200`，`model_count=16`，命中 `{"id": "gpt-5.6-sol"}`，未命中 `openai/gpt-5.6-sol`。
- `/v1/models?client_version=0.144.1`: `status=200`，`model_count=16`，命中 `{"id": "gpt-5.6-sol"}`，未返回 Codex GPT-5.6 元数据。
- `/backend-api/codex/models?client_version=0.144.1`: `status=200`，`content-type=text/html`，不是 JSON 模型目录。
- 裸 `gpt-5.6-sol` 多个不完整 Codex body 变体返回 `400 invalid_responses_request` / `invalid codex request`。
- `openai/gpt-5.6-sol` 曾返回 `500 get_channel_failed`，但该结果已被排除为不可靠路由绕行，不能作为修复依据。

2026-07-13 真实探测记录：

- `/v1/models`: `status=200`，`model_count=16`，命中 `{"id": "gpt-5.6-sol"}`，`has_openai_prefixed_gpt_56_sol=false`。
- `baseline-55-control`: `status=500`，`code=get_channel_failed`，`invalid_codex=false`，消息为模型负载达到上限。
- `plain-56-no-codex`: `status=400`，`code=invalid_responses_request`，`invalid_codex=true`。
- `baseline-56-current`: `status=400`，`code=invalid_responses_request`，`invalid_codex=true`。
- `baseline-56-version-144`: `status=400`，`code=invalid_responses_request`，`invalid_codex=true`。
- `codex-body-min`: `status=400`，`code=invalid_responses_request`，`invalid_codex=true`。
- `codex-body-min-version-144`: `status=400`，`code=invalid_responses_request`，`invalid_codex=true`。
- `codex-reasoning`: `status=400`，`code=invalid_responses_request`，`invalid_codex=true`。
- `codex-reasoning-all-turns`: `status=400`，`code=invalid_responses_request`，`invalid_codex=true`。
- `codex-reasoning-all-turns-version-144`: `status=400`，`code=invalid_responses_request`，`invalid_codex=true`。
- `lite-header-only`: `status=400`，`code=invalid_responses_request`，`invalid_codex=true`。
- `lite-client-metadata-only`: `status=400`，`code=invalid_responses_request`，`invalid_codex=true`。
- `lite-both-markers`: `status=400`，`code=invalid_responses_request`，`invalid_codex=true`。
- `lite-both-markers-version-144`: `status=400`，`code=invalid_responses_request`，`invalid_codex=true`。
- `lite-additional-tools-empty`: `status=400`，`code=invalid_responses_request`，`invalid_codex=true`。
- `lite-additional-tools-empty-version-144`: `status=400`，`code=invalid_responses_request`，`invalid_codex=true`。
- `lite-additional-tools-real-min`: `status=400`，`code=invalid_responses_request`，`invalid_codex=true`。
- `lite-additional-tools-real-min-version-144`: `status=400`，`code=invalid_responses_request`，`invalid_codex=true`。
- `lite-additional-tools-full-version-144`: `status=400`，`code=invalid_responses_request`，`invalid_codex=true`。
- `lite-additional-tools-real-min-stream`: `status=400`，`code=invalid_responses_request`，`invalid_codex=true`。
- `lite-additional-tools-real-min-stream-version-144`: `status=400`，`code=invalid_responses_request`，`invalid_codex=true`。
- `lite-additional-tools-real-min-runtime`: `status=400`，`code=invalid_responses_request`，`invalid_codex=true`。
- `lite-additional-tools-real-min-runtime-version-144`: `status=400`，`code=invalid_responses_request`，`invalid_codex=true`。
- `lite-additional-tools-real-min-runtime-version-144-terminal-ua`: `status=400`，`code=invalid_responses_request`，`invalid_codex=true`。
- 结论：手写/不完整 Lite 工具 schema 不足以通过 AnyRouter 的裸 `gpt-5.6-sol` 校验；该结论已被后续 Codex CLI 0.144.2 精确 capture 复测细化。

2026-07-13 Codex CLI 0.144.2 脱敏 capture 记录：

- 本机 `codex` 已更新到 `codex-cli 0.144.2`；新版 bundled catalog 中 `gpt-5.6-sol` 的 `use_responses_lite=true`、`tool_mode=code_mode_only`、`multi_agent_version=v2`。
- 使用本地 capture provider 捕获新版 CLI 对 `gpt-5.6-sol` 的 `/v1/responses` 请求，未记录 Authorization、API key、完整 prompt 或完整 body。
- 新版 CLI 真实请求 header 包含 `x-openai-internal-codex-responses-lite=true`，`originator=codex_exec`，`User-Agent=codex_exec/0.144.2 (...)`，`x-codex-beta-features=remote_compaction_v2`。
- 新版 CLI 真实请求 body keys 为 `client_metadata`、`include`、`input`、`model`、`parallel_tool_calls`、`prompt_cache_key`、`reasoning`、`service_tier`、`store`、`stream`、`text`、`tool_choice`。
- 新版 CLI 真实请求不含顶层 `tools`，不含 `instructions`，不含 `max_output_tokens`；工具 schema 位于 `input[0]` 的 `additional_tools` developer item。
- 新版 CLI `additional_tools` 工具名为 `exec`、`wait`、`request_user_input`、`collaboration`；这与此前手写 Lite 变体相似，但新版 CLI 还同时替换了顶层 body 结构。
- 新版 CLI `reasoning={"effort":"xhigh","context":"all_turns"}`，`text` 含 `verbosity`，`include=["reasoning.encrypted_content"]`，`stream=true`，`store=false`，`parallel_tool_calls=false`，`tool_choice=auto`。
- 与当前网关 baseline 的关键差异：当前网关含顶层 `tools=[]`、`instructions=""`、`max_output_tokens`，且缺 `text`、`service_tier`、`prompt_cache_key`、`additional_tools`、Lite header、完整 body client metadata。
- 修正结论：此前 `lite-additional-tools-full-version-144` 失败不能代表新版 CLI 0.144.2 的真实官方 Lite shape 失败；下一步需要用 capture 得到的真实 Lite 替换形态重新探测 AnyRouter。

2026-07-13 官方 Lite 替换形态 AnyRouter 探测记录：

- 新增独立探测器变体 `captured-official-lite-no-cache-key` 和 `captured-official-lite-with-cache-key`，按 Codex CLI 0.144.2 capture 结果替换 body/header；该实验移除顶层 `tools`、`instructions`、`max_output_tokens`，插入 `input[0].type=additional_tools`，并设置 Lite header、`stream=true`、`reasoning={"effort":"xhigh","context":"all_turns"}`、`text.verbosity=low`、`service_tier=priority`。
- `captured-official-lite-no-cache-key`: `status=400`，`code=invalid_responses_request`，`invalid_codex=true`，仍为 `invalid codex request`。
- `captured-official-lite-with-cache-key`: `status=400`，`type=invalid_request_error`，`code=null`，`invalid_codex=false`，消息为 `bad response status code 400`。
- 复测结果稳定：有无 `prompt_cache_key` 是当前两个官方 Lite 替换变体之间唯一差异；带 `prompt_cache_key` 可以越过 AnyRouter 的 `invalid codex request` 校验，但仍未成功完成上游请求。
- 语义说明：`FIRST_NON_INVALID_CODEX=captured-official-lite-with-cache-key` 只表示未命中 invalid-codex 校验，不表示请求成功。
- 当前新结论：AnyRouter 可能接受更接近 Codex CLI 0.144.2 的 Lite 请求外形，但后续 400 仍需继续定位，重点消融 `prompt_cache_key` 形态、`service_tier`、`reasoning.effort`、`originator/User-Agent` 与官方工具 schema 精确度。

2026-07-13 官方 Lite 替换形态消融记录：

- 仅复测 `captured-official-lite-with-cache-key*` 系列，避免完整矩阵噪声。
- `captured-official-lite-with-cache-key`: `status=400`，`type=invalid_request_error`，`code=null`，`invalid_codex=false`，消息为 `bad response status code 400`。
- `captured-official-lite-with-cache-key-no-service-tier`: `status=400`，`type=invalid_request_error`，`code=null`，`invalid_codex=false`，消息为 `bad response status code 400`。
- `captured-official-lite-with-cache-key-low-reasoning`: `status=400`，`type=invalid_request_error`，`code=null`，`invalid_codex=false`，消息为 `bad response status code 400`。
- `captured-official-lite-with-cache-key-low-reasoning-no-service-tier`: `status=400`，`type=invalid_request_error`，`code=null`，`invalid_codex=false`，消息为 `bad response status code 400`。
- 消融结论：`service_tier` 和 `reasoning.effort` 不是 `invalid codex request` 之后 400 的直接触发因素；`prompt_cache_key` 的存在是目前已知越过 AnyRouter Codex 校验的关键差异。
- 下一步：记录 Codex CLI 真实 `prompt_cache_key` 的非敏感 shape（例如是否存在、长度、前缀、分隔符特征），用相同 shape 重新探测，避免把任意字符串误判为官方行为。

2026-07-13 Codex CLI `prompt_cache_key` 脱敏 shape 记录：

- 新版 CLI 0.144.2 capture 中 `prompt_cache_key` 存在，长度为 36，包含 4 个 dash，无冒号、下划线、点或斜杠；字符类别为小写字母和数字，形态接近 UUID。
- capture 记录只保存 `length`、分隔符计数、字符类别和短 hash，不记录真实 `prompt_cache_key` 原文。
- 下一轮探测新增 UUID-shaped probe key，用于区分“任意字符串 key”与“官方 UUID-like key 形态”。

2026-07-13 UUID-shaped `prompt_cache_key` 探测记录：

- `captured-official-lite-with-uuid-cache-key`: `status=400`，`type=invalid_request_error`，`code=null`，`invalid_codex=false`，消息为 `bad response status code 400`。
- `captured-official-lite-with-uuid-cache-key-no-service-tier`: `status=400`，`type=invalid_request_error`，`code=null`，`invalid_codex=false`，消息为 `bad response status code 400`。
- `captured-official-lite-with-uuid-cache-key-low-reasoning`: `status=400`，`type=invalid_request_error`，`code=null`，`invalid_codex=false`，消息为 `bad response status code 400`。
- `captured-official-lite-with-uuid-cache-key-low-reasoning-no-service-tier`: `status=400`，`type=invalid_request_error`，`code=null`，`invalid_codex=false`，消息为 `bad response status code 400`。
- UUID-shaped key 使用长度 36、4 个 dash、包含小写 hex 字母和数字的 probe 值；结果与任意字符串 key 一致，说明 key 形态不是后续 400 的唯一原因。
- 当前剩余重点：官方 CLI 的 `additional_tools` schema 可能和手写 schema 不一致，需要记录脱敏工具 schema 指纹（字段名、类型、required、hash），而不是记录完整工具描述或完整请求体。

2026-07-13 官方 `additional_tools` 精确 schema 复测记录：

- 新增安全 capture 导出 `--out-additional-tools`，只写出 `input[0].type=additional_tools` 的工具 schema，不写 Authorization、prompt、完整请求 body 或 client metadata；导出文件位于 ignored 的 `data/codex-capture-additional-tools.jsonl`。
- 探测器新增 `--captured-additional-tools-jsonl`，默认读取 `data/codex-capture-additional-tools.jsonl`；文件缺失时回退到手写结构。使用该文件时，`exec`、`wait`、`request_user_input`、`collaboration` 及 namespace 子工具 hash 与 Codex CLI 0.144.2 capture 完全一致。
- `collaboration` 手写结构已修正：官方子工具顺序为 `followup_task`、`interrupt_agent`、`list_agents`、`send_message`、`spawn_agent`、`wait_agent`；消息目标字段为 `target`，不是 `agent`；`list_agents` 有可选 `path_prefix`；`wait_agent` 只有可选 `timeout_ms`。
- 精确 schema 复测 `captured-official-lite-with-uuid-cache-key*`：全部返回 `status=500`、`code=get_channel_failed`、`invalid_codex=false`、`capacity_limited=true`，消息为 `当前模型 gpt-5.6-sol 负载已经达到上限，请稍后重试`。
- 对照复测 `gateway-baseline`、`captured-official-lite-no-cache-key`、`captured-official-lite-with-cache-key*`：当前窗口内也返回同类 `500 get_channel_failed` 或超时；这说明此时 AnyRouter 的模型渠道容量状态会掩盖协议层结果。
- 当前结论：精确官方 `additional_tools` schema 至少能让 Lite 请求不再命中 `invalid codex request`，但还没有 2xx 或 SSE 成功样本，不能把 Lite 替换形态同步到网关。下一步需要在容量恢复后重复同一精确 schema 探测，并把 `FIRST_USABLE_RESPONSE` 作为是否可用的判据。

2026-07-13 官方 exact schema 工具子集消融记录：

- 新增独立探测器变体 `captured-official-lite-tools-exec-only`、`captured-official-lite-tools-exec-wait`、`captured-official-lite-tools-exec-wait-request-user-input`、`captured-official-lite-tools-exec-wait-collaboration`、`captured-official-lite-tools-full`。
- 所有子集变体共用同一个官方 Lite 外壳：`x-openai-internal-codex-responses-lite=true`、`originator=codex_exec`、Codex CLI 0.144.2 UA、`stream=true`、`reasoning={"effort":"xhigh","context":"all_turns"}`、`text.verbosity=low`、`service_tier=priority`、UUID-shaped `prompt_cache_key`。
- 唯一变量是 `input[0].tools`：从 `data/codex-capture-additional-tools.jsonl` 读取官方 exact schema 后按工具名选取子集。
- `captured-official-lite-tools-exec-only`: `status=500`，`code=get_channel_failed`，`invalid_codex=false`，`capacity_limited=true`。
- `captured-official-lite-tools-exec-wait`: `status=500`，`code=get_channel_failed`，`invalid_codex=false`，`capacity_limited=true`。
- `captured-official-lite-tools-exec-wait-request-user-input`: `status=500`，`code=get_channel_failed`，`invalid_codex=false`，`capacity_limited=true`。
- `captured-official-lite-tools-exec-wait-collaboration`: `status=500`，`code=get_channel_failed`，`invalid_codex=false`，`capacity_limited=true`。
- `captured-official-lite-tools-full`: `status=500`，`code=get_channel_failed`，`invalid_codex=false`，`capacity_limited=true`。
- 结论：AnyRouter 的 invalid-codex 校验不要求完整四工具集合；在当前已测集合里，官方 exact `exec` 单工具是最小可过协议校验的工具集。仍未获得 2xx/SSE 成功样本，端到端可用性要等容量恢复后再测。

2026-07-13 低影响官方单工具探测记录：

- 新增 `captured-official-lite-tools-wait-only` 和 `captured-official-lite-tools-request-user-input-only`，仍使用官方 exact schema 与相同 Lite 外壳，只改变 `input[0].tools`。
- `captured-official-lite-tools-wait-only`: `status=500`，`code=get_channel_failed`，`invalid_codex=false`，`capacity_limited=true`，工具集仅包含 `wait`。
- `captured-official-lite-tools-request-user-input-only`: `status=500`，`code=get_channel_failed`，`invalid_codex=false`，`capacity_limited=true`，工具集仅包含 `request_user_input`。
- 结论：`wait-only` 与 `request_user_input-only` 均能越过 invalid-codex 校验。若目标是尽量降低对普通对话的行为影响，`wait-only` 是当前最低影响候选，因为它需要已有 `exec` cell id 才有自然用途；`request_user_input-only` 可能诱导模型发起结构化提问，影响略高。

## 不做事项

- 不提交 git commit，除非用户后续明确要求。
- 不把 `gpt-5.6-sol` 改写为 `openai/gpt-5.6-sol`。
- 不生成 `prompt_cache_key`；只能透传下游已有值。
- 不打印 `.env`、`ANY_API_KEY` 或任何 Authorization header。
- 不对 `/tmp` 或其他外部目录做文件操作。
- 不把 AnyRouter workaround 描述成官方 Codex 请求形态。

## 自检

- 规格覆盖度：计划覆盖了模型名回滚、官方 Lite shape 探测、AnyRouter 模型列表能力探测、生产 shape 决策和验证命令。
- 占位符扫描：没有需要实现者自行猜测的“待定”实现；所有涉及新增函数的步骤都给出函数签名和代码骨架。
- 类型一致性：计划中统一使用 `dict[str, Any]`、`gpt-5.6-sol`、`additional_tools`、`with_full_lite_additional_tools()`、`summarize_models_payload()`。
- 范围控制：当前计划不实现 non-Lite adapter，除非后续真实探测和用户确认同时满足。
