# GPT-5.6 Responses Lite 网关改造计划

## 目标

将网关对 `gpt-5.6-*` 系列模型统一切换为 Codex Responses Lite 上游请求形态，不做 AnyRouter 特例判断，不按渠道自动检测；同时保留普通下游客户端兼容性，并将官方 `wait` 工具注入做成渠道级开关，默认关闭。

## 已确认约束

- 仅对 `model.startswith("gpt-5.6-")` 的模型启用 Lite transform。
- 不对 `gpt-5.5`、`gpt-5.4`、`gpt-5.4-mini`、`gpt-5.2` 等非 Lite 模型启用。
- 不添加 `service_tier`，避免进入 fast/priority 层级导致额外消费。
- `text` 固定为 `{"verbosity":"medium"}`。
- `reasoning` 生产请求有下游值时透传；无下游值时补 `{"effort":"medium","context":"all_turns"}`。
- Channel test 固定使用 `{"effort":"medium","context":"all_turns"}`。
- 不再使用旧的 `{"effort":"medium","summary":"auto","context":"all_turns"}`，避免引入非 capture 形态字段。
- 不覆盖下游传入的工具列表；除 Lite 协议要求移除顶层 `tools` 外，语义上必须转移/追加到 `additional_tools`，不可丢弃。
- 官方 exact `wait` 工具注入是渠道维度选项，默认关闭；注入 schema 必须匹配 Codex CLI 0.144.2 capture。
- 不做 AnyRouter 渠道判断、不做 AnyRouter 特例适配。
- `prompt_cache_key`：下游有值时透传；Lite 上游请求缺失或为空时自动生成 UUID。
- 更新 Codex UA/version 到当前探测版本 `0.144.2`。

## 主要设计

### Lite 模型判断

新增：

- `_is_responses_lite_model(model: Any) -> bool`

规则：

- `isinstance(model, str) and model.startswith("gpt-5.6-")`

该判断用于 header/body upstream transform 和 channel test。

### Header Transform

对 Lite 模型使用 Codex CLI 0.144.2 Lite header 形态：

- `originator = "codex_exec"`
- `User-Agent = "codex_exec/0.144.2 (...)"`
- `x-codex-beta-features = "remote_compaction_v2"`
- `x-openai-internal-codex-responses-lite = "true"`
- 移除旧 `version` header
- 保留必要 session/thread/window/turn metadata header

非 Lite 模型继续使用普通 Codex header，但 `CODEX_VERSION` 更新为 `0.144.2`。

### Body Transform

对 Lite 模型在 upstream-only 阶段执行：

- 移除顶层 `instructions`
- 移除顶层 `tools`
- 移除 `max_output_tokens`
- 设置上游 `stream = true`
- 设置 `store = false`
- 设置 `tool_choice = "auto"`
- 设置 `parallel_tool_calls = false`
- 设置 `include = ["reasoning.encrypted_content"]`
- 设置 `text = {"verbosity":"medium"}`
- 不设置 `service_tier`
- `reasoning` 有下游值时原样透传；无下游值时补 `{"effort":"medium","context":"all_turns"}`
- 补齐 `client_metadata.session_id`
- 补齐 `client_metadata.thread_id`
- 补齐 `client_metadata.turn_id`
- 补齐 `client_metadata.x-codex-installation-id`
- 补齐 `client_metadata.x-codex-window-id`
- 补齐 `client_metadata.x-codex-turn-metadata`

### Tools 处理

Lite 上游不能保留顶层 `tools`，但不能丢弃下游工具语义。

实现策略：

- 读取下游顶层 `tools`
- 构造或复用 `input[0].type == "additional_tools"` developer item
- 将下游 `tools` 追加到 `additional_tools.tools`
- 如果渠道开启 `inject_wait_tool`，再追加官方 exact `wait` schema
- 避免重复插入同名 `wait`
- 不覆盖已有 `additional_tools.tools`

### 渠道开关

DB `channels` 表新增字段：

- `inject_wait_tool INTEGER NOT NULL DEFAULT 0`

管理 API 和管理台新增开关：

- 创建/编辑渠道时可设置
- 默认关闭
- 只控制是否追加官方 exact `wait` 工具
- 不控制 `gpt-5.6-*` Lite transform，Lite transform 自动启用

### Stream 兼容

上游 Lite 必须 `stream=true`，但不能强制下游也变 SSE。

实现策略：

- 下游 `stream=true`：继续透传上游 SSE
- 下游 `stream=false` 或未传：网关内部读取上游 SSE，聚合为普通 Responses JSON
- 如果先做阶段性实现，必须明确测试覆盖非流式路径，避免普通客户端破坏

## 实施步骤

- 更新模型与 UA 常量。
- 增加 Lite 模型判断函数。
- 增加 channel `inject_wait_tool` DB 字段、API 校验和管理台开关。
- 抽出 upstream-only Lite header transform。
- 抽出 upstream-only Lite body transform。
- 实现下游顶层 `tools` 到 `additional_tools.tools` 的转移追加。
- 实现官方 exact `wait` schema 注入，受渠道开关控制。
- 改造普通 JSON 请求路径，使 Lite 上游可 stream 聚合回 JSON。
- 改造 channel test，使 `gpt-5.6-*` 使用 Lite shape，测试 reasoning 使用 medium。
- 更新单元测试与文档。

## 测试计划

- Lite 模型判断覆盖 `gpt-5.6-sol`、`gpt-5.6-terra`、`gpt-5.6-luna`、未来 `gpt-5.6-*`。
- 非 Lite 模型不触发 Lite transform。
- Lite body 不包含 `instructions`、顶层 `tools`、`max_output_tokens`、`service_tier`。
- Lite body 包含 `text={"verbosity":"medium"}`。
- Lite body 有下游 `reasoning` 时透传。
- Lite body 无下游 `reasoning` 时补 `{"effort":"medium","context":"all_turns"}`。
- Lite body 有下游 `prompt_cache_key` 时透传；缺失或空值时生成 UUID。
- Channel test 对 Lite body 使用 `reasoning={"effort":"medium","context":"all_turns"}`。
- 下游顶层 `tools` 被追加进 `additional_tools.tools`，不丢弃。
- `inject_wait_tool=false` 时不注入 `wait`。
- `inject_wait_tool=true` 时追加官方 exact `wait`（description 长度 769，匹配 Codex CLI 0.144.2 capture）。
- 非流式下游请求不会被强制返回 SSE。
- 流式下游请求继续 SSE 透传。
