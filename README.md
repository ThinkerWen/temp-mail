# Temp Mail

通过网页或统一 API 创建临时邮箱、查询邮箱和读取邮件。邮箱创建后固定绑定供应商，后续操作自动路由到原供应商。

项目采用 FastAPI + SQLite + 独立 Worker，提供持久化操作队列、幂等创建、凭据加密和定期收件同步。前端使用 HeroUI、React 和 TypeScript，依赖通过 pnpm 管理。

## 支持的供应商

| 供应商 ID | 能力 | 有效期 |
| --- | --- | --- |
| [`temp-mail-org`](docs/providers/temp-mail-org.md) | 创建、收件 | 本地访问上限 86400 秒，上游保留时间不保证 |
| [`tempmail-lol`](docs/providers/tempmail-lol.md) | 创建、收件 | 最多 3600 秒 |

两家均不支持本项目的发件、远端删除和附件下载。框架保留通用发件、删除接口，当前调用会返回能力错误。供应商故障不会触发已有邮箱迁移。

## Docker 部署

使用 Docker 和 Compose v2，在项目根目录执行：

```bash
docker compose build
docker compose run --rm --user "$(id -u):$(id -g)" init
mkdir -p data
docker compose up -d
```

`init` 仅用于首次创建配置；已有 `config.yaml` 时跳过，保留原加密密钥。Windows 可省略 `--user` 参数。默认同时启动 API 和 Worker，访问 `http://127.0.0.1:8000/`，使用配置中的 `app.api_token` 登录。交互 API 文档仍位于 `/docs`。

镜像构建时自动安装并构建前端，由 API 提供静态页面，无需在宿主机安装 Node.js 或单独部署前端容器。

容器数据库路径使用 `./data/temp-mail.db`，不要填写宿主机绝对路径。挂载、日志、升级和备份步骤见 [Docker 部署文档](docs/deployment.md)。

## 本地启动

后端需要 Python 3.12+ 和 uv；开发前端还需要 Node.js 22.13+（可使用 24 LTS）和 pnpm 11.0.9。在项目根目录安装后端依赖：

```bash
uv sync --dev
```

首次运行时执行下面的命令，从模板创建 `config.yaml` 并生成访问令牌和加密密钥。已有配置时命令会退出，避免覆盖密钥；使用已有数据库时，应恢复对应的原配置。

```bash
uv run python scripts/init_config.py
```

在项目根目录通过统一入口启动 API 和一个 Worker：

```bash
uv run python run.py
```

默认监听 `127.0.0.1:8000`，可通过 `--host` 和 `--port` 调整。开发时使用 `uv run python run.py --reload`，只对 API 启用热重载；修改 Worker 代码后需重启入口。按 Ctrl+C 会停止两个进程；任何一个进程意外退出，入口都会停止另一个并以非零状态退出。切换到统一入口前，先停止原先手动启动的 API 和 Worker，避免端口冲突或重复运行 Worker。

开发前端时，在第二个终端启动 Vite：

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm dev
```

访问 `http://127.0.0.1:5173/`，填入 `config.yaml` 中的 `app.api_token`。开发服务器将 API 请求转发到 `127.0.0.1:8000`；修改后端端口时需同步调整 Vite 的代理目标。访问 `http://127.0.0.1:8000/docs` 打开交互文档。统一入口仅启动后端，前端开发服务器由 pnpm 单独启动。

本地使用构建后的页面时，在 `frontend` 目录安装依赖并执行 `pnpm build`，然后在项目根目录执行 `uv run python run.py`，从 `http://127.0.0.1:8000/` 访问，无需启动 Vite。前端功能、配置与测试见 [前端说明](docs/frontend.md)。

需要独立管理进程时，仍可分别使用 `uv run uvicorn main:app --reload` 和 `uv run python -m app.worker`。只启动 API 时，创建操作会等待 Worker，收件也不会同步。调试时可以用 `uv run python -m app.worker --once` 执行一轮：最多处理一个队列操作和 20 个需要同步的邮箱。

## 创建与收件

网页支持选择供应商和有效期创建邮箱、查看操作状态、分页浏览邮箱及邮件、复制地址与读取纯文本正文。页面每 5 秒读取本地收件缓存；上游同步频率由 Worker 配置决定。登录令牌只保存在当前标签页的 `sessionStorage`，退出时清除。

业务接口使用 `Authorization: Bearer <app.api_token>` 鉴权。在交互文档的 Authorize 中填入令牌即可调用，也可以使用 [test_main.http](test_main.http) 中的请求示例。

创建邮箱：

```http
POST /v1/mailboxes
Authorization: Bearer <本地 API 令牌>
Idempotency-Key: mailbox-create-001
Content-Type: application/json

{
  "provider": "temp-mail-org",
  "required_capabilities": ["receive"],
  "ttl_seconds": 3600
}
```

1. 创建接口返回 `202` 和操作记录。用响应顶层的 `id` 查询 `GET /v1/operations/{id}`。
2. 等待操作变为 `succeeded`，从 `result` 取得 `mailbox_id` 和 `email`。
3. 向该地址发送邮件，保持 Worker 运行，使用 `GET /v1/mailboxes/{mailbox_id}/messages` 查询本地收件缓存。
4. 使用 `GET /v1/mailboxes/{mailbox_id}/messages/{message_id}` 读取正文。邮箱元数据中的 `last_synced_at`、`last_sync_error_code` 可用于判断同步情况。

每次新建邮箱使用新的 `Idempotency-Key`；重试同一请求复用原键。相同键配上不同内容返回 `409`。结果无法确认的操作标记为 `unknown`，不会自动重新执行。

`provider` 可以指定另一家供应商，也可以使用 `auto`。`auto` 按配置顺序选择满足能力和有效期要求的已启用供应商，模板默认优先选择 `temp-mail-org`。已创建邮箱按保存的绑定路由；可通过 `GET /v1/mailboxes?email=<完整地址>` 查找内部 ID。

TempMail.lol 的上游收件接口会消费邮件。Worker 先加密暂存响应，再写入本地缓存；同一邮箱令牌应由一个消费者读取。恢复边界见 [TempMail.lol 接入说明](docs/providers/tempmail-lol.md)。

## 配置

API 和 Worker 从当前工作目录读取同一份 `config.yaml`，不读取 `.env` 或 `TEMP_MAIL_*` 配置。相对数据库路径以配置文件所在目录为基准。完整模板见 [config.example.yaml](config.example.yaml)。

```yaml
app:
  db_path: ./data/temp-mail.db
  api_token: "填写至少24字符的令牌"
  encryption_key: "填写生成的Fernet密钥"
worker:
  sync_interval_seconds: 15
  operation_timeout_seconds: 300
  poll_seconds: 1
providers:
  temp-mail-org:
    enabled: true
    index_url: https://temp-mail.org/zh/
    base_url: https://web2.temp-mail.org
    timeout_seconds: 15
    impersonate: chrome110
    proxy: null
  tempmail-lol:
    enabled: true
    index_url: https://tempmail.lol/zh/
    base_url: https://api.tempmail.lol/v2
    timeout_seconds: 15
    impersonate: chrome110
    proxy: null
```

| 配置 | 说明 |
| --- | --- |
| `app.api_token` | 本服务 API 的访问口令；修改后客户端需同步更新 |
| `app.encryption_key` | 加密供应商邮箱凭据和待处理收件批次的 Fernet 密钥；已有数据库必须保留原密钥 |
| `providers.<id>.enabled` | 是否启用，默认 `true`；仅配置中列出的供应商会注册 |
| `providers.<id>.index_url` | 网站入口说明，不参与请求或路由 |
| `providers.<id>.base_url` | 供应商 API 基础地址 |
| `providers.<id>.proxy` | 可选的 HTTP、HTTPS 或 SOCKS 代理地址，支持 URL 内认证 |

`app` 和 `providers` 必填，`worker` 可省略。调整供应商顺序会改变新邮箱的自动选择顺序。代理为空或未配置时保留 HTTP 客户端的环境代理行为；显式代理失败不会自动改为直连。修改配置后重启 API 和 Worker。

备份数据库时应一并妥善备份加密密钥。不要将真实配置或运行数据库提交到版本库，也不要为已有数据库重新生成密钥，否则原凭据将无法解密。邮件缓存正文并非整体加密存储。

## 验证与设计

运行离线测试：

```bash
uv run pytest
```

前端检查在 `frontend` 目录执行：

```bash
pnpm check
pnpm build
pnpm test
```

首次运行浏览器测试前，执行 `pnpm exec playwright install chromium` 安装测试浏览器。

`GET /health/live` 和 `GET /health/ready` 不需要鉴权，其余业务接口需要 Bearer 令牌。供应商协议及验证范围见上方接入文档；架构、数据模型、操作状态机和扩展约定见 [设计文档](docs/design.md)。

当前尚未实现真实供应商发件、附件、Webhook、SSE、多租户、完整配额、数据库迁移及历史操作保留策略。
