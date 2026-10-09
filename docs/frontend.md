# 网页前端

前端位于 `frontend/`，使用 HeroUI 3.2.6、React 19、TypeScript、Vite 8 和 Tailwind CSS 4。依赖通过 pnpm 管理，`packageManager` 固定为 `pnpm@11.0.9`，版本锁定在 `pnpm-lock.yaml`。

## 使用方式

Docker 部署后打开 `http://127.0.0.1:8000/`，输入 `config.yaml` 中的 `app.api_token` 登录。无需将配置文件复制到前端目录，也无需把令牌写入前端环境变量。

网页提供：

- 选择供应商及有效期创建临时邮箱，查看异步操作结果。
- 分页浏览邮箱，查看供应商、有效期与同步状态，复制邮箱地址。
- 分页查看收件列表，读取发件人、收件人、主题与纯文本正文。
- 自动刷新本地缓存，手动刷新并查看错误提示。
- 退出登录并清除当前标签页中的令牌。

当前两家供应商仅支持创建和收件，页面不提供发件、远端删除或附件功能。空列表表示尚无对应数据，不会填充演示邮箱或邮件。

## 本地开发

需要 Python 3.12+、uv、Node.js 22.13+（可使用 24 LTS）和 pnpm 11.0.9。先按 [README](../README.md#本地启动) 安装后端依赖并初始化 `config.yaml`，保留已有数据库对应的原加密密钥。

第一个终端在项目根目录启动 API 和 Worker：

```bash
uv run python run.py --reload
```

统一入口管理一个 API 和一个 Worker。`--reload` 只对 API 启用热重载，修改 Worker 代码后需重启入口；按 Ctrl+C 会停止两者。已有手动启动的 API 或 Worker 时，先将它们停止，再使用统一入口。

第二个终端启动前端：

```bash
cd frontend
pnpm install --frozen-lockfile
pnpm dev
```

打开 `http://127.0.0.1:5173/`。Vite 将 `/v1`、`/health`、`/docs` 与 `/openapi.json` 转发到 `http://127.0.0.1:8000`，浏览器只需要访问前端开发地址。修改后端监听端口时，需同步调整 Vite 的代理目标。

统一入口不会自动启动 pnpm。需要独立管理后端进程时，启动命令见 [README](../README.md#本地启动)；只启动 API 和前端时，创建操作会等待 Worker，收件缓存也不会自动从供应商更新。

## 构建与部署

在 `frontend` 目录执行：

```bash
pnpm install --frozen-lockfile
pnpm build
```

产物写入 `frontend/dist`。在项目根目录执行 `uv run python run.py`，访问 `http://127.0.0.1:8000/` 即可使用，无需额外启动 Vite；交互 API 文档仍位于 `/docs`。

Docker 已包含前端构建阶段，执行 `docker compose up -d --build` 即可一并发布前后端。前端与 API 共用 8000 端口，部署步骤见 [Docker 部署文档](deployment.md)。修改前端源文件后需重新构建产物或镜像。

## 数据与交互约定

登录成功后，API 令牌保存在当前标签页的 `sessionStorage`，刷新页面后可以继续使用，退出时清除。令牌随业务请求通过 `Authorization: Bearer` 发送；它是整个服务的访问口令，不是独立用户账户。前端静态产物不包含访问令牌、Fernet 密钥或供应商邮箱凭据。

邮箱与邮件列表每页 20 条，使用 API 分页。搜索按完整邮箱地址精确匹配；“使用中的邮箱”统计当前页。操作记录跟踪当前标签页提交的请求，也支持通过操作 ID 查询已有记录。邮件正文按纯文本展示，不执行邮件中的 HTML、脚本或加载远端邮件内容。

页面每 5 秒读取本地缓存。这个刷新周期与 `worker.sync_interval_seconds` 相互独立：Worker 按配置和错误退避访问供应商，API 返回当前已缓存内容。页面刷新成功只说明本地查询成功，应结合最后同步时间和同步错误判断上游收件情况。

创建邮箱是异步操作。提交后通过操作 ID 查询 `pending`、`running`、`succeeded`、`failed` 或 `unknown`，成功后加载真实邮箱。超时或结果为 `unknown` 时不会自动再次创建，避免重复调用供应商。提交请求遇到网络错误时会保留原参数与幂等键，刷新页面后仍可通过“继续上次创建”重试，退出登录会清除这份跟踪信息。具体状态含义见 [设计文档](design.md#6-异步写操作与幂等)。

## 检查与测试

在 `frontend` 目录执行：

```bash
pnpm check
pnpm build
pnpm exec playwright install chromium
pnpm test
```

`check` 执行 TypeScript 类型检查，`build` 验证生产构建，`test` 默认使用 Playwright Chromium 执行浏览器测试。也可使用已安装的 Chrome：`PLAYWRIGHT_CHANNEL=chrome pnpm test`（Linux/macOS）。测试使用模拟 API 响应，不创建真实供应商邮箱；`pnpm format:check` 检查前端代码格式。

后端回归测试在项目根目录执行：

```bash
uv run pytest
```
