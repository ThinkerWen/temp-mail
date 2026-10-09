# Docker 部署

使用 Docker 和 Compose v2，在项目根目录操作。部署包含一个 API 容器和一个 Worker 容器，共享 SQLite 数据及配置，默认访问地址为 `http://127.0.0.1:8000`。HeroUI 前端在镜像构建阶段通过 pnpm 构建，产物由 API 提供，不增加容器或端口；宿主机无需安装 Node.js 或 pnpm。

## 首次启动

先构建本地镜像，再生成配置。以下命令适用于 Linux 和 macOS：

```bash
docker compose build
mkdir -p config data
docker compose run --rm --user "$(id -u):$(id -g)" init
```

`init` 从镜像内的 `config.example.yaml` 生成宿主机 `config/config.yaml`，创建 API 令牌与 Fernet 密钥。已有配置时拒绝覆盖。`--user` 使生成文件归当前宿主用户所有；Windows 可使用 `docker compose run --rm init`，并预先在项目根目录创建 `config` 和 `data` 文件夹。

按需编辑 `config/config.yaml` 中的供应商配置，然后启动：

```bash
docker compose up -d
docker compose ps
```

打开 `http://127.0.0.1:8000/`，输入 `config/config.yaml` 中的 `app.api_token` 登录，随后可创建邮箱、查看收件及修改配置。页面中的刷新读取本地缓存，Worker 负责向供应商同步。

交互 API 文档保留在 `http://127.0.0.1:8000/docs`，在 Authorize 中填写同一令牌即可调用。请求示例见 [README](../README.md#创建与收件)，前端行为见 [前端说明](frontend.md)。

### 从已有配置升级

旧版 Docker 挂载项目根目录的 `config.yaml`。为支持网页原子保存，现在挂载整个 `config` 目录。已有根目录配置和数据库时跳过 `init`，先停止服务，将原配置复制到新位置，再启动：

```bash
docker compose stop api worker
mkdir -p config data
cp -p -n config.yaml config/config.yaml
docker compose up -d --build
```

`cp -p -n` 保留文件权限且不覆盖已有目标；如果 `config/config.yaml` 已存在，确认它是需要使用的配置后再启动。Windows 请手动复制原文件。Docker 此后读取和保存 `config/config.yaml`，本地启动仍默认读取根目录 `config.yaml`，两者不会自动同步。原 `./data` 目录无需移动，默认 `app.db_path: ./data/temp-mail.db` 也无需修改。

不要为已有数据库重新生成加密密钥，否则原邮箱凭据和待处理收件批次无法解密。已经使用 `config/config.yaml` 的部署升级时直接运行 `docker compose up -d --build`。

## 服务与持久化

| 服务 | 用途 | 默认启动 |
| --- | --- | --- |
| `api` | 提供网页、静态资源和统一 HTTP API，容器内监听 `0.0.0.0:8000` | 是 |
| `worker` | 执行创建操作、收件同步和到期状态更新 | 是，等待 API 健康后启动 |
| `init` | 一次性初始化配置，属于 `tools` profile | 否，显式 `run init` 时执行 |

三者使用同一镜像 `temp-mail:local`。API 和 Worker 命令分别为 `python -m app.server --host 0.0.0.0 --port 8000` 与 `python -m app.worker`，工作目录为 `/app`。

镜像使用 `frontend/pnpm-lock.yaml` 锁定依赖，将前端产物放在 `/app/frontend/dist`。浏览器通过同源的 `/v1` 与 `/health` 请求 API；`/docs` 和 `/openapi.json` 保留原功能。前端构建不读取 `config.yaml`，API 令牌、加密密钥及供应商凭据不写入静态文件。

| 宿主路径 | 容器路径 | 用途 |
| --- | --- | --- |
| `./config` | `/app/config` | API 可读写、Worker 只读；包含 `config.yaml` 及保存所需的锁文件和临时文件 |
| `./data` | `/app/config/data` | API 和 Worker 均可读写；默认相对数据库路径对应的数据库、SQLite WAL 及邮箱锁目录 |
| `./data` | `/app/data` | 同一数据目录的兼容挂载，保留旧配置的 `/app/data/...` 绝对路径 |

API 和 Worker 均通过 `TEMP_MAIL_CONFIG=/app/config/config.yaml` 选择配置文件。整个目录挂载允许 API 先写临时文件再原子替换配置；不要改回单文件挂载，否则无法保证网页保存。Worker 的配置目录只读，但嵌套的数据挂载保持可写。普通运行服务不会挂载整个项目目录。

`init` 只将项目目录挂载到 `/workspace`，运行 `python scripts/init_config.py --output /workspace/config/config.yaml --template /app/config.example.yaml`。普通 `up` 不运行初始化服务。

数据库路径按容器文件系统解释，建议保留：

```yaml
app:
  db_path: ./data/temp-mail.db
```

该相对路径以配置文件所在目录 `/app/config` 为基准，因此解析为 `/app/config/data/temp-mail.db`，实际仍保存在宿主机 `./data/temp-mail.db`。也可使用 `/app/data/` 下的绝对路径。宿主机上的 `/Users/...` 或 `/home/...` 路径在容器中并不对应挂载数据，不能直接沿用。容器默认以 root 运行，可以读取初始化时权限为 `0600` 的配置文件。网页原子保存时保留原文件的用户和组，并将配置权限设为 `0600`；无法保留归属或写入文件时会报告保存失败。

当前按一个 Worker 进程部署，通过两个独立线程池并发创建邮箱和收件。可在网页“系统配置”或 YAML 中调整：

```yaml
worker:
  create_concurrency: 2
  receive_concurrency: 4
```

两项均为 1–32 的整数，省略时分别使用 2 和 4。创建，以及未来支持的发送、删除共用操作池；收件池独立，不会因某个创建请求较慢而停下所有收件。并发数是单进程的上限，继续使用一个 Worker 容器即可；API 健康不代表 Worker 正常消费队列或已成功同步供应商。

同步同一个邮箱时使用独立文件锁，不同邮箱可以同时获取邮件，网络请求和响应解析不持有 SQLite 写事务。邮箱锁目录位于数据库旁，例如 `data/temp-mail.db.mailbox-locks/`，需允许 Worker 写入；现有 `./data` 卷已满足要求。共享数据库的进程也必须共享该锁目录，运行时不要删除锁文件。进程退出后会释放锁，文件本身保留。

常驻 Worker 在任务完成后继续补位，不受每轮一个创建或 20 个收件的批量限制。`python -m app.worker --once` 则只执行有限一批任务：操作和收件各不超过对应并发数，等待它们完成后退出，不保证排空队列。

## 状态、日志与配置更新

后端统一使用 Loguru，以 `INFO` 级别输出到标准错误，由 Docker 收集。日志包含时间、级别、进程 ID 和代码位置，覆盖 API、Uvicorn 访问与运行日志、Worker、配置初始化脚本以及标准库日志和 Python 警告。应用不创建日志文件，日志保留与轮转由 Docker 日志驱动管理。

API 专用入口 `app.server` 将 `app/logging.py` 中的 `UVICORN_LOG_CONFIG` 字典传给 Uvicorn，日志配置随 Python 代码内置，无需单独的配置文件或挂载。该入口也覆盖热重载父进程、服务子进程和访问日志。异常日志关闭局部变量诊断和扩展回溯，不应额外记录令牌、解密凭据或邮件正文。

```bash
docker compose ps
docker compose logs --tail=100 api worker
docker compose logs -f worker
```

操作持续 `pending` 时先检查 Worker 日志。收件状态查看邮箱的 `last_synced_at` 和 `last_sync_error_code`；API 的 `/health/ready` 主要检查本地服务及数据库就绪状态。

在网页的“供应商”页面切换卡片右上角开关，会立即将启用状态写入宿主机 `config/config.yaml`；其他供应商参数在“编辑配置”弹窗中修改并保存。“系统配置”页面也可修改并保存系统参数，或者直接编辑这个文件。

供应商启用状态、顺序、连接参数、最长本地有效期、Worker 周期、创建和收件并发数以及 API 令牌支持热更新，无需重启容器。API 在保存成功后立即应用新值，其他 API 进程在收到请求时检查文件变化；Worker 调度器最多每秒检查一次，即使还有网络请求在执行也能应用配置。新任务使用新配置，在途请求继续使用原配置完成。两个服务必须使用同一配置文件，默认目录挂载已保证这一点。

提高并发数后，下一次调度会按新上限补充任务；降低并发数不会中断在途请求，待执行中的任务数量降到新上限以下再继续分配。例如收件从 4 调为 2 时，原有 4 个任务继续完成，之后最多同时同步 2 个邮箱。

数据库路径与加密密钥保持进程启动时的值，变化后才需要同时重启两个服务。配置响应的 `restart_required_fields` 和前端提示会列出这些具体字段：

```bash
docker compose restart api worker
```

配置变更不需要重新构建镜像。API 令牌更新在保存成功后立即生效，前端返回登录页面并提示使用新令牌；留空保留原令牌和代理，清除代理需明确选择清除。Fernet 密钥不允许通过网页修改，离线轮换也不会自动重新加密已有凭据。数据库路径变更不会迁移数据，须自行安排数据移动，并保证新路径仍位于持久化挂载内。

目录或文件没有写权限时，网页会报告保存失败，运行配置保持不变。外部修改导致文件无效或不可读时，API 和 Worker 继续使用上一次有效配置并记录脱敏警告，配置查询报告读取失败；修复文件后会再次尝试加载。不要仅为保存配置而重新生成密钥。

临时停止与恢复：

```bash
docker compose stop api worker
docker compose up -d
```

停止并移除容器及 Compose 网络：

```bash
docker compose down
```

`down` 不会删除绑定挂载的 `config` 和 `data`，下次 `up` 继续使用原数据。

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
cp -p config/config.yaml "$backup_dir/config.yaml"
cp -a data "$backup_dir/data"
docker compose up -d
```

复制完整 `data` 目录可保留数据库及可能存在的 WAL 文件；配置中的原加密密钥必须与数据库一起保存。备份包含访问令牌、密钥和邮件数据，应保留其访问权限。

恢复时停止两个服务，将同一份备份中的 `config.yaml` 恢复到项目的 `config/config.yaml`，替换整个 `data` 目录，再执行 `docker compose up -d`。不要混用不同备份的数据库和密钥。配置目录内的锁文件和临时文件无需备份。

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
