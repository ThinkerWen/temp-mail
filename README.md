<p align="center">
  <img src="docs/assets/logo.svg" width="112" height="112" alt="Temp Mail 图标" />
</p>

<h1 align="center">Temp Mail</h1>

<p align="center"><strong>给收件箱，加一些隐私。</strong></p>

<p align="center">一个可自部署的临时邮箱聚合服务，通过网页和统一 API 管理多个供应商的邮箱。</p>

---

## 功能

- **统一收件**：创建临时邮箱、复制地址、查看邮件与操作记录。
- **数据看板**：查看邮箱统计、收件趋势和最近活动。
- **在线配置**：管理供应商与系统参数，支持配置热更新。
- **简洁界面**：支持深色模式、中英文和移动端。
- **保留历史**：邮箱到期后停止收发，历史邮件保留至手动清除。

## 已接入平台

| 平台 | 网站 | 支持能力 | 接入文档 |
| --- | --- | --- | --- |
| [temp-mail.org](https://temp-mail.org/) | [temp-mail.org](https://temp-mail.org/) | 创建、收件 | [接入说明](docs/providers/temp-mail-org.md) |
| [TempMail.lol](https://tempmail.lol/) | [tempmail.lol](https://tempmail.lol/) | 创建、收件 | [接入说明](docs/providers/tempmail-lol.md) |

目前暂不支持发件和附件下载。

## Docker 部署

首次部署，在项目根目录执行：

```bash
docker compose build
mkdir -p config data
docker compose run --rm --user "$(id -u):$(id -g)" init
docker compose up -d
```

打开 `http://127.0.0.1:8000/`，使用 `config/config.yaml` 中的 `app.api_token` 登录。

以上命令适用于 Linux / macOS。Windows、已有配置迁移、备份与升级见 [部署指南](docs/deployment.md)。已有数据库请保留原配置及加密密钥。

## 本地启动

需要 Python 3.12+、uv、Node.js 22.13+ 和 pnpm 11。

```bash
uv sync --dev
uv run python scripts/init_config.py
pnpm --dir frontend install --frozen-lockfile
pnpm --dir frontend build
uv run python run.py
```

打开 `http://127.0.0.1:8000/`，使用根目录 `config.yaml` 中的 `app.api_token` 登录。初始化命令仅用于首次运行，已有配置时跳过。

`run.py` 同时启动 API 和 Worker，按 Ctrl+C 结束。开发时使用 `uv run python run.py --reload`，另开终端运行 `pnpm --dir frontend dev`；详见 [前端开发](docs/frontend.md#开发与构建)。

## 文档

| 文档 | 内容 |
| --- | --- |
| [部署指南](docs/deployment.md) | Docker、持久化、日志与备份 |
| [配置说明](docs/configuration.md) | YAML 参数、供应商配置与热更新 |
| [使用与前端开发](docs/frontend.md) | 页面功能、本地开发与构建 |
| [API 使用](docs/api.md) | 鉴权、创建邮箱、收件与接口索引 |
| [架构设计](docs/design.md) | 数据模型、任务调度与供应商扩展 |

配置模板见 [config.example.yaml](config.example.yaml)，请求示例见 [test_main.http](test_main.http)。服务启动后可访问 `/docs` 查看交互 API 文档。

## 开发检查

```bash
uv run pytest
pnpm --dir frontend build
pnpm --dir frontend exec playwright install chromium
pnpm --dir frontend test
```

后端使用 FastAPI、SQLite 和 Loguru；前端使用 React、TypeScript 和 HeroUI。测试使用本地模拟供应商，无需创建真实邮箱。
