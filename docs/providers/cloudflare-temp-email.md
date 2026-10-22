# Cloudflare Temp Email

供应商 ID：`cloudflare-temp-email`。适配 [cloudflare_temp_email](https://github.com/dreamhunter2333/cloudflare_temp_email) 的 Worker API，支持创建邮箱、收件和正文读取。它是可自部署的项目，需要提供你的实例地址；GitHub 仓库地址不能作为 `base_url`。

## 配置

该供应商默认关闭。部署实例后，在供应商编辑弹窗填入 Worker API 地址并启用，或配置：

```yaml
providers:
  cloudflare-temp-email:
    enabled: true
    index_url: https://mail.example.com/
    base_url: https://your-worker.example.com
    domain: example.com
    admin_password: null
    site_password: null
    timeout_seconds: 15
    impersonate: chrome110
    max_ttl_seconds: 86400
    proxy: null
```

以上域名需要替换为真实部署值。`base_url` 是 Worker API 根地址，不包含 `/api`、`/admin` 或网页路由；`index_url` 是供用户打开的网页入口，仅用于展示。公共参数见 [配置说明](../configuration.md)。

| 专有字段 | 含义 |
| --- | --- |
| `domain` | 要使用的收信域名；留空由上游选择默认域名，须已在上游配置 |
| `admin_password` | 可选的上游管理员密码；填写后通过管理员接口创建地址 |
| `site_password` | 可选的上游站点访问密码，对应 `x-custom-auth`，与管理员密码不同 |

管理和站点密码在前端不回显，留空保留原值，可勾选清除；保存后对新任务热生效。它们不是 Cloudflare API Token，也不是本项目的 `app.api_token`。每个邮箱的 JWT 由创建接口返回并独立加密存入数据库，无需手动填写。

## 创建与收件

| 操作 | 请求 | 凭据 |
| --- | --- | --- |
| 匿名创建 | `POST /api/new_address` | 可选站点访问密码 |
| 管理员创建 | `POST /admin/new_address` | `x-admin-auth`，以及可选站点访问密码 |
| 分页读取邮件 | `GET /api/mails?limit=100&offset=0` | 邮箱 JWT，使用 `Authorization: Bearer ...` |
| 补取详情 | `GET /api/mail/{id}` | 邮箱 JWT；列表缺少 `raw` 时使用 |

未填写管理员密码时，上游必须允许匿名创建，且不能要求交互式 Turnstile 验证。若实例启用了这些限制，可在有管理员权限的前提下配置管理员密码。适配器不会在失败后自动切换创建方式，也不自动处理验证码。

管理员密码只发送给管理员创建接口；收件使用邮箱 JWT。站点密码按实例配置随请求发送，不跟随重定向转发凭据。服务使用配置的代理，失败时不会自动切换到直连。

管理员模式的新邮箱前缀随机采用单个名字、名字与姓氏、名字与单词或两个单词的组合；组合间随机使用点、下划线或不加分隔符，并附加固定四位随机尾缀，每位从小写字母 `a-z` 和数字 `0-9` 中选择，例如 `emma8k3x`、`oliver.hayes7a9b`、`mayariver4r8n`、`cedar_cloud6p2m`。匿名模式仍由上游生成，已有邮箱地址不变。

上游列表返回 `results` 和 `count`。`count` 只在第一页表示总数，后续页可能为 `0`；适配器分页读取并按数据库邮件 ID 去重。邮件从原始 RFC822/MIME 内容解析，解码主题、字符集及传输编码，优先纯文本，否则把 HTML 转为文本。收件归属以返回记录的 `address` 校验，支持 MIME 头中没有自身地址的密送邮件。

不加载 HTML 中的图片或脚本，不把附件正文当作主邮件内容。当前接入不提供发件、附件下载或远端删除，即使上游实例本身支持这些功能。

## 有效期与故障

上游创建接口未承诺固定过期时间，`max_ttl_seconds` 仅限制本服务的本地有效期。上游管理员清理地址、轮换 JWT 密钥或修改站点访问策略，可能提前影响访问。

邮箱到期后停止同步，已缓存邮件保留至手动清除。关闭供应商不会删除历史缓存；重新启用后，未到期邮箱继续按原绑定访问。

限流、鉴权失败、验证码要求、接口不可用和无效 MIME 会返回对应同步错误。创建结果不确定时保留 `unknown` 状态，避免超时后自动创建重复地址。已知的邮箱 JWT 失效不会自动重新创建邮箱。

## 验证依据

协议依据上游源码版本 `9fbe87ea8e7d2e437ca9e3e69cdd8901591ea6e6`，重点参考 `worker/src/mails_api/new_address.ts`、`worker/src/admin_api/address_api.ts`、`worker/src/mails_api/mails_crud.ts`、`worker/src/common.ts` 及上游的创建、收件 API 文档。

离线测试覆盖创建鉴权、分页、MIME 正文和错误处理。未提供实际部署地址与凭据，因此没有对用户实例做真实投递验证。
