# TempMail.lol

## 能力

供应商 ID：`tempmail-lol`。支持创建邮箱和收件，不支持发件、远端删除、附件或 Webhook。收件具有消费式语义：上游读取后不保证再次返回同一邮件。

当前接入使用 v2 接口，协议参考站点 API 说明 `https://tempmail.lol/api` 与官方 SDK `https://github.com/tempmail-lol/api-javascript`。适配器使用的匿名创建及消费行为仍需随上游维护，不能据此承诺接口长期稳定。

## 配置差异

以下列出本供应商的默认地址和有效期，公共字段、代理配置与保存规则见 [配置说明](../configuration.md)。

```yaml
providers:
  tempmail-lol:
    enabled: true
    index_url: https://tempmail.lol/zh/
    base_url: https://api.tempmail.lol/v2
    max_ttl_seconds: 3600
```

客户端默认使用 `impersonate: chrome110`。调整本地有效期上限不会延长上游邮箱寿命。收件响应 `expired: true` 会记录同步错误并退避，不自动改写本地到期时间或清理缓存。

## 上游协议

| 操作 | 请求 | 返回 |
| --- | --- | --- |
| 创建邮箱 | `POST /inbox/create`，JSON 为 `{"domain": null, "captcha": null}` | `address`、`token` |
| 获取收件 | `GET /inbox?token=<邮箱令牌>` | `emails`、`expired`，包含邮件正文 |

`date` 按 UNIX 毫秒转换为 UTC，正文优先使用 `body`，必要时将 `html` 转为纯文本，并校验 `to` 与当前邮箱一致。缺少上游邮件 ID 时，以稳定批次 ID 和批次内序号去重。

每个邮箱令牌独立加密保存，`upstream_id` 是其 SHA-256 摘要。令牌位于查询参数中，日志不得记录完整上游请求 URL。创建与缓存查询示例见 [test_main.http](../../test_main.http)。

## 语义与错误

适配器声明 `destructive_receive: true`，Worker 按以下流程保护已取得的响应：

1. 优先处理已有 `message_batches`；存在暂存批次时不继续请求上游。
2. 没有批次时获取原始 JSON 响应，以 Fernet 加密后暂存 SQLite。
3. 解析批次，在同一事务内写入去重邮件、更新同步状态并删除批次。

解析或入库失败会保留批次供下轮重试，并阻止后续拉取。每个邮箱使用独立文件锁；共享数据库的进程必须共享锁目录，不同邮箱可并发收件。网络请求和解析不持有 SQLite 写事务。

暂存只保护已经落盘的响应。上游已消费但响应在网络中丢失，或首次暂存失败时，邮件仍可能无法恢复，不承诺无损或恰好一次接收。不要让浏览器或其他程序同时消费同一令牌；它们不受本服务的锁保护。统一 API 查询本地缓存不会消费邮件。

邮箱本地到期后，暂存批次保留但不再调度解析；确认清除失效邮箱时才删除批次和邮件缓存，不代表远端数据已删除。

| 上游情况 | 错误码 |
| --- | --- |
| 要求验证码 / 限流 / 已过期 | `CAPTCHA_REQUIRED` / `PROVIDER_RATE_LIMITED` / `MAILBOX_EXPIRED` |
| 拒绝凭据 / 拒绝访问 | `INVALID_CREDENTIAL` / `PROVIDER_ACCESS_DENIED` |
| `404` / `410` | `MAILBOX_NOT_FOUND` / `MAILBOX_EXPIRED` |
| 无效响应 | `PROVIDER_INVALID_RESPONSE` |
| 连接失败、超时或上游服务错误 | `PROVIDER_UNAVAILABLE` |
| 其他请求拒绝 | `PROVIDER_REQUEST_REJECTED` |

创建结果不确定时记为 `unknown`，不自动重放；令牌失效不替换邮箱或供应商。同步错误记入 `last_sync_error_code` 并退避处理。发件和远端删除返回 `CAPABILITY_UNSUPPORTED`。

## 验证范围

已验证统一 API 创建、Worker 处理空收件、缓存查询，以及限流退避后新进程恢复同步。离线测试覆盖解析、暂存与故障恢复；外部真实来信及其完整入库读取链路尚未实测。
