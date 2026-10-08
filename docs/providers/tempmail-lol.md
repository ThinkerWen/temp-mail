# TempMail.lol 接入说明

供应商 ID 为 `tempmail-lol`，使用 v2 API，支持创建和收件，不支持发件、远端删除、附件或 Webhook。免费邮箱有效期最多 3600 秒，收件具有消费式语义：上游读取后不保证再次返回同一邮件。

## 配置

在 `config.yaml` 的 `providers` 中配置：

```yaml
providers:
  tempmail-lol:
    enabled: true
    index_url: https://tempmail.lol/zh/
    base_url: https://api.tempmail.lol/v2
    timeout_seconds: 15
    impersonate: chrome110
    proxy: null
```

`index_url` 仅说明网站入口，`base_url` 是实际 API 地址。客户端使用 curl_cffi，默认 `impersonate: chrome110`。修改配置后重启 API 和 Worker。

`proxy` 是正式请求代理配置，支持 HTTP、HTTPS、SOCKS4、SOCKS4a、SOCKS5、SOCKS5h 和 URL 内认证。省略、`null` 或空白值保留 curl_cffi 的环境代理行为，不保证直连。显式代理失败返回 `PROVIDER_UNAVAILABLE`，不会自动切换直连。

## 上游协议

协议参考官方 API 说明 `https://tempmail.lol/api` 和官方 SDK `https://github.com/tempmail-lol/api-javascript`。

| 操作 | 请求 | 返回 |
| --- | --- | --- |
| 创建邮箱 | `POST /inbox/create`，JSON 为 `{"domain": null, "captcha": null}` | `address`、`token` |
| 获取收件 | `GET /inbox?token=<邮箱令牌>` | `emails`、`expired` |

一次响应包含正文。`date` 按 UNIX 毫秒转换为 UTC 时间，优先使用 `body`，必要时将 `html` 转为纯文本，并校验 `to` 与当前邮箱一致。缺少上游邮件 ID 时，以稳定的批次 ID 和批次内序号去重。

每个邮箱令牌独立加密保存，`upstream_id` 使用令牌的 SHA-256 摘要标识生命周期。上游令牌位于查询参数中，日志不得记录完整请求 URL。令牌失效不替换邮箱，也不切换供应商。

创建请求指定 `provider: "tempmail-lol"`、`required_capabilities: ["receive"]`，有效期为 60–3600 秒。通过统一 API 查询缓存不会消费或删除本地邮件。完整请求示例见 [test_main.http](../../test_main.http)。

## 消费式收件

适配器声明 `destructive_receive: true`。Worker 将获取与解析分开处理：

1. 优先读取邮箱已有的 `message_batches`，有批次时不继续拉取上游。
2. 没有批次时请求上游，将原始 JSON 响应用 Fernet 加密并暂存 SQLite。
3. 解析暂存内容，在同一事务内写入去重后的 `messages`、更新同步状态并删除批次。

解析或入库失败时保留批次，下一轮重试原内容；解析异常会阻塞后续拉取，修复后才能继续。邮箱到期时清理批次、凭据和邮件缓存，不代表远端数据已删除。

获取期间持有 SQLite 写事务，避免共享同一数据库的并发消费者重复获取。其他写入可能等待至上游请求结束，受 `timeout_seconds` 约束，当前按单 Worker 部署。

暂存只能保护已落盘的响应。上游已消费但网络响应丢失，或响应到达后首次暂存失败，邮件仍可能无法恢复；不承诺无损或恰好一次接收。不要让浏览器或其他程序同时消费同一令牌，它们不受本服务的数据库锁保护。

## 错误与验证范围

验证码要求返回 `CAPTCHA_REQUIRED`，限流返回 `PROVIDER_RATE_LIMITED`，过期返回 `MAILBOX_EXPIRED`，无效响应返回 `PROVIDER_INVALID_RESPONSE`。连接失败、超时及上游服务错误返回 `PROVIDER_UNAVAILABLE`。创建结果不确定时记录 `unknown`，不自动重放；同步错误记录在 `last_sync_error_code`，按退避策略处理。发件和删除返回 `CAPABILITY_UNSUPPORTED`。

已验证统一 API 创建、Worker 处理空收件响应、缓存查询，以及限流退避后新进程恢复同步。离线测试覆盖响应解析、暂存和故障恢复；外部真实来信及其完整入库读取链路尚未实测。
