# Docker 部署

使用 Docker 和 Compose v2，在项目根目录操作。部署包含一个 API 容器和一个 Worker 容器，共享 SQLite 数据及配置，默认访问地址为 `http://127.0.0.1:8000`。HeroUI 前端在镜像构建阶段通过 pnpm 构建，产物由 API 提供，不增加容器或端口；宿主机无需安装 Node.js 或 pnpm。

## 首次启动

先构建本地镜像，再生成配置。以下命令适用于 Linux 和 macOS：

```bash
docker compose build
docker compose run --rm --user "$(id -u):$(id -g)" init
mkdir -p data
```

`init` 从镜像内的 `config.example.yaml` 生成 `config.yaml`，创建 API 令牌与 Fernet 密钥。已有配置时拒绝覆盖。`--user` 使生成文件归当前宿主用户所有；Windows 可使用 `docker compose run --rm init`，并在项目根目录创建 `data` 文件夹。

按需编辑 `config.yaml` 中的供应商配置，然后启动：

```bash
docker compose up -d
docker compose ps
```

打开 `http://127.0.0.1:8000/`，输入 `config.yaml` 中的 `app.api_token` 登录，随后可创建邮箱与查看收件。页面中的刷新读取本地缓存，Worker 负责向供应商同步。

交互 API 文档保留在 `http://127.0.0.1:8000/docs`，在 Authorize 中填写同一令牌即可调用。请求示例见 [README](../README.md#创建与收件)，前端行为见 [前端说明](frontend.md)。

已有配置和数据库时跳过 `init`，确认容器内数据库路径正确后直接执行：

```bash
docker compose up -d --build
```

不要为已有数据库重新生成加密密钥，否则原邮箱凭据和待处理收件批次无法解密。

## 服务与持久化

| 服务 | 用途 | 默认启动 |
| --- | --- | --- |
| `api` | 提供网页、静态资源和统一 HTTP API，容器内监听 `0.0.0.0:8000` | 是 |
| `worker` | 执行创建操作、收件同步和到期清理 | 是，等待 API 健康后启动 |
| `init` | 一次性初始化配置，属于 `tools` profile | 否，显式 `run init` 时执行 |

三者使用同一镜像 `temp-mail:local`。API 和 Worker 命令分别为 `python -m uvicorn main:app --host 0.0.0.0 --port 8000` 与 `python -m app.worker`，工作目录为 `/app`。

镜像使用 `frontend/pnpm-lock.yaml` 锁定依赖，将前端产物放在 `/app/frontend/dist`。浏览器通过同源的 `/v1` 与 `/health` 请求 API；`/docs` 和 `/openapi.json` 保留原功能。前端构建不读取 `config.yaml`，API 令牌、加密密钥及供应商凭据不写入静态文件。

| 宿主路径 | 容器路径 | 用途 |
| --- | --- | --- |
| `./config.yaml` | `/app/config.yaml`，只读 | API 和 Worker 共用配置 |
| `./data` | `/app/data` | 数据库、SQLite WAL 及其他运行数据 |

`init` 只将项目目录挂载到 `/workspace`，运行 `python scripts/init_config.py --output /workspace/config.yaml --template /app/config.example.yaml`。普通 `up` 不运行初始化服务。

数据库路径按容器文件系统解释，建议保留：

```yaml
app:
  db_path: ./data/temp-mail.db
```

也可使用 `/app/data/` 下的绝对路径。宿主机上的 `/Users/...` 或 `/home/...` 路径在容器中并不对应挂载数据，不能直接沿用。容器默认以 root 运行，可以读取初始化时权限为 `0600` 的配置文件。

当前按一个 Worker 部署，不建议增加 Worker 副本。消费式收件会在获取期间持有 SQLite 写事务；API 健康也不代表 Worker 正常消费队列或已成功同步供应商。

## 状态、日志与配置更新

```bash
docker compose ps
docker compose logs --tail=100 api worker
docker compose logs -f worker
```

操作持续 `pending` 时先检查 Worker 日志。收件状态查看邮箱的 `last_synced_at` 和 `last_sync_error_code`；API 的 `/health/ready` 主要检查本地服务及数据库就绪状态。

修改 `config.yaml` 后重建两个容器，使配置重新加载：

```bash
docker compose up -d --force-recreate api worker
```

临时停止与恢复：

```bash
docker compose stop api worker
docker compose up -d
```

停止并移除容器及 Compose 网络：

```bash
docker compose down
```

`down` 不会删除绑定挂载的 `config.yaml` 和 `data`，下次 `up` 继续使用原数据。

## 更新镜像

更新项目源码后重新构建并启动，前端改动也通过同一流程发布：

```bash
docker compose build
docker compose up -d
docker compose ps
docker compose logs --tail=100 api worker
```

配置和数据库保留在宿主机。涉及数据结构变更时，先按下一节备份，并遵循对应版本的迁移说明；当前项目尚无通用数据库迁移工具。

## 备份与恢复

先停止 API 和 Worker，避免备份期间有写入，然后将配置和整个数据目录一起复制。以下为 Linux/macOS 示例：

```bash
docker compose stop api worker
backup_dir="backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup_dir"
cp -p config.yaml "$backup_dir/config.yaml"
cp -a data "$backup_dir/data"
docker compose up -d
```

复制完整 `data` 目录可保留数据库及可能存在的 WAL 文件；配置中的原加密密钥必须与数据库一起保存。备份包含访问令牌、密钥和邮件数据，应保留其访问权限。

恢复时停止两个服务，用同一份备份中的 `config.yaml` 和整个 `data` 目录替换当前版本，再执行 `docker compose up -d`。不要混用不同备份的数据库和密钥。

## 宿主机网络

默认端口映射为 `127.0.0.1:8000:8000`，只允许从宿主机本地访问。需要通过其他地址访问时，可调整 Compose 的端口绑定。

若供应商的 `proxy` 指向宿主机服务，容器中的 `127.0.0.1` 表示容器自身。Docker Desktop 环境可将地址配置为 `host.docker.internal`，例如 `http://host.docker.internal:7890`，端口应替换为实际服务端口。

Linux 环境需要访问宿主机服务时，可自行添加 `compose.override.yaml`：

```yaml
services:
  api:
    extra_hosts:
      - "host.docker.internal:host-gateway"
  worker:
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

该映射是可选配置，默认部署不会添加。宿主服务还需监听容器能够访问的地址；仅监听宿主机 `127.0.0.1` 的服务未必可通过该映射访问。
