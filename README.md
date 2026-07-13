# codex-disguise

`codex-disguise` 是一个面向 Codex Responses API 的轻量网关服务，提供 OpenAI 兼容的 `/v1/responses` 入口、Web 管理台、多渠道路由、失败自动切换、按模型路由以及渠道维度 HTTP/HTTPS 代理能力。

## 功能特性

- OpenAI 兼容入口：暴露 `/v1/responses` 与 `/v1/models`。
- 多渠道管理：支持新增、编辑、启用、禁用、删除渠道。
- 渠道路由：按优先级路由，同优先级随机打散，支持失败自动切换。
- 熔断冷却：连续失败达到阈值后自动进入冷却，冷却结束后恢复可用。
- 熔断阈值：支持在管理台手动设置连续错误上限，未设置时默认使用 `3`。
- 按模型路由：每个渠道可配置支持的模型列表。
- 渠道级手动模型：支持在渠道配置中添加自定义模型名，并通过 `/v1/models` 暴露。
- 渠道专属 Key：支持全局下游 Key，也支持渠道级下游 Key。
- 上游 Key 管理：每个渠道单独配置上游 API Key。
- 渠道 HTTP 代理：每个渠道可配置 `http://` 或 `https://` 代理地址。
- GPT-5.6 Lite：`gpt-5.6-*` 自动使用 Codex Responses Lite 上游请求形态，非流式客户端仍返回普通 JSON。
- Lite wait 工具：每个渠道可单独开启官方 `wait` 工具注入，默认关闭。
- 管理台：访问 `/management` 管理渠道、测试渠道、查看统计和网关状态。
- 持久化：使用 SQLite，默认数据目录为 `data/`。

## 环境变量

服务启动需要以下配置。可以复制 `.env.example` 为 `.env` 后修改：

```bash
ADMIN_TOKEN=change-me-admin-token
CLIENT_API_KEY=change-me-client-api-key
```

- `ADMIN_TOKEN`：登录 `/management` 与调用 `/admin/*` 接口使用。
- `CLIENT_API_KEY`：客户端调用 `/v1/*` 接口使用的全局 Key。

## 本地运行

准备 Python 环境并安装依赖：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

编辑 `.env` 后启动：

```bash
./run.sh
```

服务默认监听：

```text
http://localhost:8002
```

公网部署时不要直接暴露明文 HTTP 端口。建议放在 HTTPS 反向代理后面，
并让应用端口只监听本机或内网地址。

## Docker 部署

本仓库提供 `docker-compose.yml`，默认使用远程镜像：

```text
ghcr.io/moyucharm/codex-disguise:latest
```

部署前准备 `.env`：

```bash
cp .env.example .env
```

编辑 `.env` 后启动：

```bash
docker compose up -d
```

默认映射端口：

```text
宿主机 127.0.0.1:8002 -> 容器 8002
```

如果需要公网访问，请使用 Nginx、Caddy 等 HTTPS 反向代理转发到
`127.0.0.1:8002`。不要直接将 `8002:8002` 暴露到公网，否则管理 token
和客户端 API key 会在 HTTP 明文链路上传输。

数据持久化使用 bind mount：

```text
./data:/app/data
```

如果 GHCR 镜像不是公开包，部署机器需要先登录：

```bash
docker login ghcr.io
```

## 管理台

访问：

```text
http://localhost:8002/management
```

使用 `.env` 中的 `ADMIN_TOKEN` 登录。

管理台支持：

- 查看服务概览与 24 小时渠道统计。
- 新增、编辑、启用、禁用、删除渠道。
- 配置渠道上游地址、上游 API Key、下游 API Key、支持模型和 HTTP 代理。
- 配置渠道是否为 GPT-5.6 Lite 上游请求注入官方 `wait` 工具。
- 在渠道配置中添加自定义模型名，自定义模型只会路由到显式选择它的渠道。
- 设置连续错误上限；留空或恢复默认时使用 `3`，达到上限后渠道进入冷却。
- 设置渠道冷却时间；默认 `5` 分钟，可调整为 `5～180` 分钟，保存于 `data/config.json`。
- 查看渠道当前冷却时的上游错误原因；该原因仅保存在进程内存中，用于临时排查。
- 查看渠道近 75 分钟健康度，以 5 分钟为一格显示成功、失败、混合或无调用；该健康度仅保存在进程内存中。
- 测试单个渠道可用性。
- 重置渠道运行状态或网关指纹。

## API 简表

公开接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `GET` | `/v1/config` | 公开网关配置摘要，不包含密钥或本地路径 |
| `GET` | `/v1/models` | 模型列表 |
| `POST` | `/v1/responses` | OpenAI 兼容 Responses 入口 |

管理接口需要 `Authorization: Bearer <ADMIN_TOKEN>`：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/admin/session` | 验证管理会话 |
| `GET` | `/admin/config` | 获取完整管理配置摘要 |
| `PATCH` | `/admin/config` | 更新管理配置，支持 `failure_threshold`、`cooldown_minutes` |
| `GET` | `/admin/channels` | 获取渠道列表 |
| `POST` | `/admin/channels` | 创建渠道 |
| `GET` | `/admin/channels/{channel_id}` | 获取渠道详情 |
| `PATCH` | `/admin/channels/{channel_id}` | 更新渠道 |
| `DELETE` | `/admin/channels/{channel_id}` | 删除渠道 |
| `POST` | `/admin/channels/{channel_id}/enable` | 启用渠道 |
| `POST` | `/admin/channels/{channel_id}/disable` | 禁用渠道 |
| `POST` | `/admin/channels/{channel_id}/test` | 测试渠道 |
| `POST` | `/admin/channels/{channel_id}/reset-runtime` | 重置渠道运行状态 |
| `GET` | `/admin/channels/{channel_id}/stats` | 获取渠道统计 |
| `POST` | `/admin/fingerprint/reset` | 重置网关指纹 |

注意：`/v1/chat/completions` 不支持，请使用 `/v1/responses`。

## 模型与熔断设置

- `/v1/models` 返回内置模型与所有渠道 `supported_models` 中声明的自定义模型，重复模型名会自动去重。
- 渠道必须显式选择至少一个支持模型，`supported_models` 不再使用 `null` 表示“支持全部模型”。
- 自定义模型只会路由到显式声明该模型的渠道；从所有渠道移除某个自定义模型后，该模型会从 `/v1/models` 消失。
- `gpt-5.6-*` 模型会自动启用 Codex Responses Lite 上游转换，不需要按渠道选择，也不会影响 `gpt-5.5`、`gpt-5.4`、`gpt-5.4-mini`、`gpt-5.2` 等非 Lite 模型。
- Lite 转换会将下游顶层 `tools` 转移到 `additional_tools.tools`，不会主动生成 `prompt_cache_key`，也不会设置 `service_tier`。
- 渠道的 `inject_wait_tool` 只控制是否追加官方 `wait` 工具，默认关闭；如果已有同名 `wait` 工具则不会重复追加。
- `failure_threshold` 表示渠道进入冷却前允许的连续错误次数，必须是不小于 `1` 的整数。
- `PATCH /admin/config` 传入 `{"failure_threshold": null}` 可恢复默认值 `3`。
- `cooldown_minutes` 表示渠道进入冷却后的持续时间，必须为 `5～180` 分钟的整数，默认值为 `5`。
- `PATCH /admin/config` 传入 `{"cooldown_minutes": null}` 可恢复默认值 `5` 分钟。
- 全局 Key 不能调用禁用渠道，并会跳过冷却中的渠道；匹配的专属下游 Key 忽略渠道启用状态和冷却状态，仍然调用该渠道。
- `data/config.json` 示例：`{"cooldown_minutes": 5}`。

## 渠道代理

每个渠道可配置 `proxy_url`，仅支持以下前缀：

```text
http://
https://
```

留空表示直连。代理配置只影响该渠道调用上游时使用的 HTTP 客户端，不影响其他渠道。

## 自动构建镜像

GitHub Actions 工作流位于：

```text
.github/workflows/docker-publish.yml
```

触发条件：

- push 到 `main` 分支。
- push `v*` 标签。
- 手动触发 `workflow_dispatch`。

镜像会推送到：

```text
ghcr.io/moyucharm/codex-disguise
```

默认分支会发布 `latest` 标签，同时会生成短 SHA 标签。
