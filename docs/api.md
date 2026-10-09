# API 使用

服务启动后访问 `/docs` 查看交互文档，完整参数与响应结构以 `/openapi.json` 为准。可直接执行的请求示例见 [test_main.http](../test_main.http)。

## 鉴权

业务请求携带配置中的 `app.api_token`：

```http
Authorization: Bearer <API_TOKEN>
```

`GET /health/live` 和 `GET /health/ready` 无需鉴权。访问令牌是服务级口令，同时拥有邮箱与配置管理权限，不是独立用户账户。

## 创建与收件

### 创建邮箱

```http
POST /v1/mailboxes
Authorization: Bearer <API_TOKEN>
Idempotency-Key: mailbox-create-001
Content-Type: application/json

{
  "provider": "auto",
  "required_capabilities": ["receive"],
  "ttl_seconds": 3600
}
```

`provider` 可指定 `temp-mail-org`、`tempmail-lol` 或 `auto`。自动选择按配置顺序匹配能力和有效期，已有邮箱始终使用创建时绑定的供应商。

### 等待结果

创建请求返回 `202` 和操作记录。将顶层 `id` 用于 `GET /v1/operations/{id}`，等待 `status` 变为 `succeeded`，再从 `result` 取得 `mailbox_id` 和 `email`。

| 状态 | 含义 |
| --- | --- |
| `pending` | 已入队，等待 Worker |
| `running` | 正在执行 |
| `succeeded` | 确定成功，读取 `result` |
| `failed` | 确定失败，检查 `error_code` |
| `unknown` | 上游结果无法确认，不自动重放 |

每次新建邮箱使用新的 `Idempotency-Key`；重试同一请求复用原键。相同键配上不同内容返回 `409`。键长为 1–200 字符，不能只有空白。

### 读取邮件

向新地址发信并保持 Worker 运行，然后查询：

```http
GET /v1/mailboxes/{mailbox_id}/messages
Authorization: Bearer <API_TOKEN>
```

从列表取得邮件 ID，再调用 `GET /v1/mailboxes/{mailbox_id}/messages/{message_id}` 读取纯文本正文。查询只读取本地缓存，不直接请求供应商；通过邮箱元数据的 `last_synced_at` 与 `last_sync_error_code` 判断同步情况。

邮箱到期后停止收发，详情和历史邮件仍可读取。已删除邮箱返回 `410`，确认清除本地数据后返回 `404`。

## 接口索引

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| GET | `/v1/capabilities` | 已启用供应商的能力 |
| GET | `/v1/dashboard` | 全量统计、7 天趋势及最近 3 条操作 |
| GET / PUT | `/v1/config` | 读取脱敏配置、保存配置 |
| POST | `/v1/mailboxes` | 异步创建邮箱 |
| GET | `/v1/mailboxes` | 邮箱列表，可按完整 `email` 精确过滤 |
| GET | `/v1/mailboxes/{id}` | 邮箱详情 |
| GET | `/v1/mailboxes/{id}/messages` | 邮件摘要列表 |
| GET | `/v1/mailboxes/{id}/messages/{message_id}` | 邮件正文 |
| GET | `/v1/operations` | 持久化操作历史 |
| GET | `/v1/operations/{id}` | 操作详情 |
| GET | `/v1/mailboxes/cleanup-preview` | 获取待清除数量与确认版本 |
| POST | `/v1/mailboxes/cleanup` | 确认后清除失效邮箱及邮件 |
| POST | `/v1/mailboxes/{id}/messages` | 异步发件，当前供应商不支持 |
| DELETE | `/v1/mailboxes/{id}` | 异步远端删除，当前供应商不支持 |

本地清除与远端删除是不同操作。清除请求使用预览返回的 `cutoff`、`revision`，并将 `count` 作为 `expected_count` 提交。确认期间候选集合变化时返回 `409 / CLEANUP_CHANGED`，需重新预览；清除保留操作历史。

## 分页与统计

列表使用 `limit` 和 `offset`，默认分别为 `50` 和 `0`，`limit` 范围 1–100。响应包含 `items`、`limit`、`offset`；邮箱和操作列表还包含 `total`，邮件列表包含 `last_synced_at`。

操作历史按创建时间及 ID 倒序排列。首页最近操作按更新时间及 ID 倒序取 3 条。两者均从数据库读取，不依赖浏览器会话。

看板统计不受列表分页影响。邮件新增数按缓存入库时间计算，7 天趋势采用 UTC 日期；清除数据会减少对应统计，操作历史继续保留。统计口径详见 [架构设计](design.md)。

## 错误处理

同步请求错误使用统一结构：

```json
{"error": {"code": "UNAUTHORIZED", "message": "A valid bearer token is required"}}
```

| HTTP 状态 | 常见原因 |
| --- | --- |
| `401` | 缺失、无效或已更新的令牌 |
| `404` | 邮箱、邮件或操作不存在 |
| `409` | 幂等键冲突、配置版本冲突或清除集合变化 |
| `410` | 对到期邮箱执行收发，或访问已删除邮箱 |
| `422` | 参数、能力或有效期不符合要求 |
| `503` | 供应商或存储暂不可用 |

HTTP `202` 只表示请求已入队。异步失败通过操作的 `status` 和 `error_code` 表达，不能仅凭创建请求的 HTTP 状态判断成功。

配置保存及热更新规则见 [配置说明](configuration.md)，供应商错误与恢复边界见 [temp-mail.org](providers/temp-mail-org.md) 和 [TempMail.lol](providers/tempmail-lol.md) 接入说明。
