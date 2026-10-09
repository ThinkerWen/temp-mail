# temp-mail.org

## 能力

供应商 ID：`temp-mail-org`。支持创建邮箱和收件，不支持发件、远端删除、附件或 Webhook。

当前适配器使用网站内部 Web 接口，并非有稳定兼容性承诺的官方开放 API。网站的“删除邮箱”行为尚未确认会删除远端数据，因此本接入不声明删除能力。

## 配置差异

以下列出本供应商的默认地址和有效期，公共字段、代理配置与保存规则见 [配置说明](../configuration.md)。

```yaml
providers:
  temp-mail-org:
    enabled: true
    index_url: https://temp-mail.org/zh/
    base_url: https://web2.temp-mail.org
    max_ttl_seconds: 86400
```

客户端默认使用 `impersonate: chrome110`。上游未提供已验证的邮箱有效期，本地配置不保证上游邮箱或邮件的存活时长。

## 上游协议

| 操作 | 请求 | 返回 |
| --- | --- | --- |
| 创建邮箱 | `POST /mailbox`，无请求体、无旧邮箱令牌 | `token`、`mailbox` |
| 收件列表 | `GET /messages`，携带邮箱 Bearer 令牌 | `mailbox`、`messages` 摘要数组 |
| 邮件详情 | `GET /messages/{id}`，携带同一令牌 | `_id`、`receivedAt`、`from`、`subject`、正文 |

列表中的 `_id` 用于获取详情，每轮同步仍读取列表内的邮件详情，尚无增量游标。`receivedAt` 按 UNIX 秒转换为 UTC，也兼容带时区的 ISO 字符串。正文优先使用非空白 `bodyText`，否则将 `bodyHtml` 转为纯文本；适配器校验邮箱地址与详情 ID，跳过已消失的邮件。

每个邮箱返回独立令牌，无需共享 API Key 或 Cookie。令牌由业务层加密保存，`upstream_id` 是其 SHA-256 摘要，摘要不能用于访问邮箱。创建与缓存查询示例见 [test_main.http](../../test_main.http)。

## 语义与错误

令牌失效后保留原供应商绑定，不创建替代邮箱。创建没有已确认的上游幂等机制：结果无法确认时记为 `unknown`，不自动重放；客户端重试应复用原 `Idempotency-Key`。

| 上游情况 | 错误码 |
| --- | --- |
| `401` / `403` / `429` | `INVALID_CREDENTIAL` / `PROVIDER_ACCESS_DENIED` / `PROVIDER_RATE_LIMITED` |
| `404`、`410` | `MAILBOX_NOT_FOUND` 或 `MESSAGE_NOT_FOUND` |
| `408`、`5xx`、网络错误或超时 | `PROVIDER_UNAVAILABLE` |
| 意外状态或无效响应 | `PROVIDER_INVALID_RESPONSE` |
| 其他 `4xx` | `PROVIDER_REQUEST_REJECTED` |

收件失败记入 `last_sync_error_code`，Worker 退避后继续同步原邮箱。发件和远端删除返回 `CAPABILITY_UNSUPPORTED`；本地清除不代表远端数据已删除。

## 验证范围

已验证创建、空收件同步、进程重启后恢复原绑定与幂等请求复用；适配器也已读取外部真实来信及正文。真实来信经 Worker 入库、再由统一 API 读取的完整链路尚未实测。
