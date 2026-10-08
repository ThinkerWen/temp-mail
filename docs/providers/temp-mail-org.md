# temp-mail.org 接入说明

供应商 ID 为 `temp-mail-org`，支持创建邮箱和收件，不支持发件、远端删除、附件或 Webhook。接入使用网站内部接口，接口兼容性需要持续维护。

## 配置

在 `config.yaml` 的 `providers` 中配置：

```yaml
providers:
  temp-mail-org:
    enabled: true
    index_url: https://temp-mail.org/zh/
    base_url: https://web2.temp-mail.org
    timeout_seconds: 15
    impersonate: chrome110
    proxy: null
```

`index_url` 仅说明网站入口，`base_url` 是实际 API 地址。客户端使用 curl_cffi，默认 `impersonate: chrome110`。修改配置后重启 API 和 Worker。

`proxy` 是正式请求代理配置，支持 HTTP、HTTPS、SOCKS4、SOCKS4a、SOCKS5、SOCKS5h 和 URL 内认证。省略、`null` 或空白值保留 curl_cffi 的环境代理行为，不保证直连。显式代理失败返回 `PROVIDER_UNAVAILABLE`，不会自动切换直连。

## 上游协议

| 操作 | 请求 | 返回 |
| --- | --- | --- |
| 创建邮箱 | `POST /mailbox`，无请求体、无旧邮箱令牌 | `token`、`mailbox` |
| 收件列表 | `GET /messages`，使用邮箱 Bearer 令牌 | `mailbox`、`messages` 摘要数组 |
| 邮件详情 | `GET /messages/{id}`，使用同一令牌 | `_id`、`receivedAt`、`from`、`subject`、正文 |

列表中的 `_id` 用于获取详情。`receivedAt` 按 UNIX 秒转换为 UTC 时间，也兼容带时区的 ISO 字符串。正文优先使用非空白的 `bodyText`，否则把 `bodyHtml` 转换为纯文本。适配器校验邮箱地址和详情 ID，跳过已消失的邮件；每轮同步仍会获取列表中的详情，尚无增量游标。

网站的“删除邮箱”行为未确认会删除远端数据，因此适配器不声明删除能力，也不通过创建新邮箱替代删除。

## 凭据与有效期

每个邮箱独立返回令牌，无需共享上游 API Key 或 Cookie。业务层使用 Fernet 加密保存令牌，`upstream_id` 使用令牌的 SHA-256 摘要标识生命周期。令牌失效时保留原绑定，不创建新邮箱或切换供应商；摘要不能作为访问凭证。

本服务允许请求 60–86400 秒的有效期，该期限仅限制本地访问。上游未提供已验证的有效期，不能据此保证邮箱或邮件存活时长。本地到期会清理凭据和缓存，不代表远端数据已删除。

创建请求指定 `provider: "temp-mail-org"`、`required_capabilities: ["receive"]` 和 `ttl_seconds`。查询创建操作成功后，通过统一邮件 API 读取 Worker 同步的缓存。完整请求示例见 [test_main.http](../../test_main.http)。

## 错误与重试

| 上游情况 | 错误码 |
| --- | --- |
| `401` | `INVALID_CREDENTIAL` |
| `403` | `PROVIDER_ACCESS_DENIED` |
| `429` | `PROVIDER_RATE_LIMITED` |
| `404`、`410` | `MAILBOX_NOT_FOUND` 或 `MESSAGE_NOT_FOUND` |
| `408`、`5xx`、网络错误或超时 | `PROVIDER_UNAVAILABLE` |
| 意外状态或无效响应 | `PROVIDER_INVALID_RESPONSE` |
| 其他 `4xx` | `PROVIDER_REQUEST_REJECTED` |

创建没有已确认的上游幂等机制。结果无法确认时记录 `unknown`，不自动重放；客户端重试应复用原 `Idempotency-Key`。收件失败由 Worker 退避后继续使用原绑定同步，错误记录在 `last_sync_error_code`。发件和删除请求返回 `CAPABILITY_UNSUPPORTED`。

## 验证范围

已验证创建、空收件同步、进程重启后恢复原绑定和幂等请求复用；也已通过适配器读取外部真实来信及正文。真实来信经 Worker 入库、再由统一 API 读取的完整链路尚未实测。
