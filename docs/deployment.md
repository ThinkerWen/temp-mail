# Docker 部署

需要 Docker 和 Compose v2。部署直接拉取 Docker Hub 发布的镜像，包含网页、API 和 Worker，宿主机无需安装 Python、Node.js 或下载项目源码。API 和 Worker 分别运行，共享配置与 SQLite 数据。源码开发见 [前端开发与构建](frontend.md#开发与构建)。

镜像地址：[`designerwang/temp-mail`](https://hub.docker.com/r/designerwang/temp-mail)，默认标签为 `latest`。

## 首次部署

创建一个部署目录，在该目录下载 Compose 文件。以下命令适用于 Linux/macOS：

```bash
mkdir -p temp-mail
cd temp-mail
curl -fsSL https://raw.githubusercontent.com/ThinkerWen/temp-mail/main/compose.yaml -o compose.yaml
mkdir -p config data
docker compose --profile tools pull
docker compose run --rm --user "$(id -u):$(id -g)" init
```

`--profile tools pull` 同时准备运行服务与初始化工具所用的镜像。`init` 根据镜像内模板生成 `config/config.yaml`，创建 API 令牌与 Fernet 密钥，拒绝覆盖已有文件。`--user` 使文件归当前宿主用户所有。Windows 可手动下载 Compose 文件、创建 `config` 和 `data` 目录，拉取镜像后执行 `docker compose run --rm init`。

按需编辑供应商配置，然后启动：

```bash
docker compose up -d
docker compose ps
```

打开 `http://127.0.0.1:8000/`，使用 `config/config.yaml` 中的 `app.api_token` 登录。API 文档位于 `/docs`。Worker 等待 API 健康后启动，负责执行创建操作与收件同步；网页刷新读取本地缓存。

默认端口绑定为 `127.0.0.1:8000:8000`，仅宿主机可访问。需要其他访问地址时，调整 Compose 端口绑定。

Compose 顶部的 `x-image` 统一指定三个服务使用的镜像，默认使用 `latest`。需要固定版本时，将这一处的 `latest` 改为 Docker Hub 上已发布的版本标签；通过 Git 标签发布的镜像使用同名标签。API、Worker 与初始化工具应使用同一版本。

## 已有配置迁移

先将部署目录的 Compose 文件更新为仓库中的 [compose.yaml](../compose.yaml)，保留自己的端口、挂载等配置。新版通过 `x-image` 统一使用 Docker Hub 镜像。

若旧版直接挂载根目录的 `config.yaml`，需迁移为整个配置目录挂载，以支持网页原子保存。已有配置和数据库时跳过 `init`：

```bash
docker compose stop api worker
mkdir -p config data
cp -p -n config.yaml config/config.yaml
docker compose --profile tools pull
docker compose up -d
```

`cp -p -n` 保留权限且不覆盖已有目标；目标文件已存在时，先确认它是要使用的配置。Windows 可手动复制。原 `./data` 无需移动，默认 `app.db_path: ./data/temp-mail.db` 无需修改。

Docker 此后使用 `config/config.yaml`，源码运行默认使用根目录 `config.yaml`，两者不会自动同步。不要为已有数据库重新生成加密密钥，否则原邮箱凭据和暂存收件批次无法解密。已经使用新目录布局的部署直接按“更新与重启”操作。

## 挂载与权限

| 宿主路径 | 容器路径 | 用途 |
| --- | --- | --- |
| `./config` | `/app/config` | API 可读写，Worker 只读；保存配置、锁文件和临时文件 |
| `./data` | `/app/config/data` | API、Worker 均可读写；默认数据库路径 |
| `./data` | `/app/data` | 兼容旧配置中的 `/app/data/...` 绝对路径 |

两个服务都使用 `TEMP_MAIL_CONFIG=/app/config/config.yaml`。不要改回单文件挂载：保存时需在同目录写入临时文件，再原子替换原文件。Worker 配置目录只读，其嵌套的数据挂载仍可写。`init` 仅显式执行时运行，普通 `up` 不会初始化配置。

建议保留默认数据库路径：

```yaml
app:
  db_path: ./data/temp-mail.db
```

相对路径以配置文件所在目录为基准，上述配置解析为 `/app/config/data/temp-mail.db`，对应宿主机 `./data/temp-mail.db`。也可使用 `/app/data/` 下的绝对路径；宿主机 `/Users/...` 或 `/home/...` 路径不能直接用于容器。

容器默认以 root 运行，能读取初始化生成的 `0600` 配置。网页保存保留原文件用户和组，并设置权限为 `0600`；无法保留归属或写入时保存失败。若自行指定容器用户，须保证配置目录可写、配置文件可读写、数据目录可读写。

保持一个 Worker 容器，通过独立的创建与收件并发配置调节吞吐，见 [配置说明](configuration.md)。邮箱锁位于数据库旁的 `temp-mail.db.mailbox-locks/`；共享数据库的进程也必须共享该目录，运行时不要删除锁文件。

## 更新与重启

网页“供应商”和“系统配置”的保存会写入宿主机 `config/config.yaml`。大多数配置支持热更新；字段含义、生效时机与保存规则见 [配置说明](configuration.md)。

数据库路径或加密密钥变化后，须同时重启两个服务：

```bash
docker compose restart api worker
```

更改数据库路径不会迁移数据，新的路径仍须位于持久化挂载内。更换密钥不会重新加密已有凭据，网页也不提供密钥编辑。

升级时拉取已发布镜像并重新创建服务，网页会随镜像一起更新。使用固定版本时，先修改 `x-image` 的标签：

```bash
docker compose --profile tools pull
docker compose up -d
docker compose ps
docker compose logs --tail=100 api worker
```

涉及数据结构变更时先备份，并遵循对应版本的迁移说明；项目尚无通用数据库迁移工具。临时停止可用 `docker compose stop api worker`，恢复用 `docker compose up -d`。`docker compose down` 移除容器与网络，但不会删除宿主机的 `config`、`data`。

## 备份与恢复

先停止两个服务，确保备份期间没有写入，再复制配置和完整数据目录：

```bash
docker compose stop api worker
backup_dir="backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup_dir"
cp -p config/config.yaml "$backup_dir/config.yaml"
cp -a data "$backup_dir/data"
docker compose up -d
```

完整 `data` 包含数据库及可能存在的 WAL 文件。配置中的原加密密钥必须与数据库一起保存；备份含访问令牌、密钥与邮件数据，应保留访问权限。配置目录的锁文件和临时文件无需备份。

恢复时停止两个服务，将同一备份的 `config.yaml` 放回 `config/config.yaml`，替换整个 `data` 目录，再运行 `docker compose up -d`。不要混用不同备份的数据库和密钥。

## 排错

```bash
docker compose ps
docker compose logs --tail=100 api worker
docker compose logs -f worker
```

后端日志由 Loguru 输出到标准错误，由 Docker 日志驱动收集，应用不创建日志文件。日志轮转按宿主机的 Docker 日志配置执行。

| 现象 | 检查项 |
| --- | --- |
| 创建操作一直等待处理 | Worker 是否运行及其日志；API 健康不代表 Worker 正常消费队列 |
| 邮件未更新 | 邮箱的 `last_synced_at`、`last_sync_error_code` 与 Worker 日志；`/health/ready` 只检查本地就绪状态 |
| 网页保存配置失败 | API 是否挂载整个可写配置目录，目录与文件权限是否允许保存 |
| 手动修改后配置未生效 | 文件是否有效、两个服务是否读取同一路径；无效配置会保留上次有效运行值，修复后重新加载 |
| 容器无法连接宿主机代理 | 容器内 `127.0.0.1` 指向容器自身，参见下方网络配置 |

Docker Desktop 可将宿主机代理地址写为 `http://host.docker.internal:7890`，端口按实际服务调整。Linux 可在 `compose.override.yaml` 中添加：

```yaml
services:
  api:
    extra_hosts:
      - "host.docker.internal:host-gateway"
  worker:
    extra_hosts:
      - "host.docker.internal:host-gateway"
```

此映射为可选配置；宿主服务还需监听容器可访问的地址，仅监听宿主机 `127.0.0.1` 未必可达。
