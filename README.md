# codex-disguise

`codex-disguise` 是一个面向 Codex Responses API 的轻量网关服务，提供 OpenAI 兼容的 `/v1/responses` 入口、Web 管理台、多渠道路由、失败自动切换、按模型路由以及渠道维度 HTTP/HTTPS 代理能力。

## 功能特性

- OpenAI 兼容入口：暴露 `/v1/responses` 与 `/v1/models`。
- 多渠道管理：支持新增、编辑、启用、禁用、删除渠道。
- 渠道路由：按优先级路由，同优先级随机打散，支持失败自动切换。
- 熔断冷却：连续失败达到阈值后自动进入冷却，冷却结束后恢复可用。
- 按模型路由：每个渠道可配置支持的模型列表。
- 渠道专属 Key：支持全局下游 Key，也支持渠道级下游 Key。
- 上游 Key 管理：每个渠道单独配置上游 API Key。
- 渠道 HTTP 代理：每个渠道可配置 `http://` 或 `https://` 代理地址。
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
