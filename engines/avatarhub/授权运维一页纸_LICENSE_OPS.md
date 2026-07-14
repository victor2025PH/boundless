# 授权运维一页纸（厂商侧 · LICENSE OPS）

> 对象：接手授权发放/续费/吊销/对账的运维与销售支持。
> 原则：**厂商持私钥签发，产品内置公钥验签**；客户侧无密钥、无法自造授权。
> 两条发码路：**在线激活服务**（客户输兑换码自助）/ **离线签发**（发 license.key 文件）。

## 0. 文件地图（都在 `secrets/`，仅厂商侧持有，勿进 git/交付包）

| 文件 | 作用 | 谁写 |
|---|---|---|
| `secrets/license_vendor_sk.pem` | **签发私钥（最高机密）** | `license_admin.py keygen` 一次性生成 |
| `license_pubkey.pem` / 代码内置 | 验签公钥（随产品交付） | keygen 同时产出 |
| `secrets/orders.json` | 兑换码台账（码/档位/座位/激活记录） | `license_server.py addcode` + 激活自动记 |
| `secrets/trials.json` | 一键试用台账（指纹→签发/到期，一机一次） | 试用端点自动记 |
| `secrets/telemetry.jsonl` | 客户端匿名健康回执 | 客户端自动上报 |
| `revocations.json` | 吊销名单 CRL（已签名，可公开分发） | `license_admin.py revoke` |

## 1. 首次开通（一次性）

```powershell
python license_admin.py keygen          # 生成 sk/pk；把公钥内置进产品或随包发 license_pubkey.pem
python license_server.py serve --host 0.0.0.0 --port 8770   # 起激活服务（可选，在线发码用）
```

客户端配置激活地址（任一）：环境变量 `AVATARHUB_ACTIVATION_URL=http://<你的服务>:8770`
或 `config.json` 里 `activation_url`。不配置=只支持离线导入 license.key。

## 2. `serve` 参数速查

| 参数 | 默认 | 说明 |
|---|---|---|
| `--host` / `--port` | 127.0.0.1 / 8770 | 对外发布建议挂反代 + HTTPS |
| `--sk` | secrets/license_vendor_sk.pem | 私钥路径 |
| `--orders` | secrets/orders.json | 兑换码台账 |
| `--trials` | secrets/trials.json | 试用台账 |
| `--trial-days` | 7（`AVATARHUB_TRIAL_UP_DAYS` 可调） | 一键试用天数/机；**0=关闭试用端点** |
| `--telemetry` | secrets/telemetry.jsonl | 回执落盘路径 |
| `--public-base` | http://127.0.0.1:\<port\> | 临期通知里「一键发码」链接的对外可点地址（挂反代时填公网域名） |
| `--qi-edition` / `--qi-days` | pro / 365 | 一键发码出的兑换码档位与有效天数 |
| `--no-notify-activate` | 默认开 | 关闭 P15 激活转化回推（quickissue 码被激活 / 试用转正 → webhook 通知销售） |
| `--health-th` | 80 | P15 客户健康告警阈值%：会话/组件成功率破线即推 webhook（每客户每日一次；0=关闭） |

端点：`POST /api/activate`（输码激活）· `POST /api/trial`（一键试用，一机一次）·
`POST /api/telemetry`（回执）· `GET /api/funnel`（试用转化漏斗 + 一键发码四级漏斗）· `GET /api/customers`（客户健康度行级视图）·
`GET /quickissue`（一键发码确认页，签名链接进入）· `POST /api/quickissue`（验签出码）· `GET /dashboard`（质量+漏斗+客户看板）。

## 3. 日常发码 / 离线签发

```powershell
# 在线：造码给客户（客户在产品「授权徽章 → 输码」自助激活；输码容错：大小写/空格/连字符都能对上）
python license_server.py addcode --edition pro --days 365 --seats 1 --licensee "某某传媒"
# 离线（无激活服务的内网客户）：客户在授权卡复制「本机指纹」发你 →
python license_admin.py issue --machine <指纹> --edition pro --days 365 --licensee "某某传媒"
#   → 把生成的 license.key 发客户，放产品根目录（或授权卡「导入授权」粘贴）即生效
```

档位：`trial / standard / pro`；`--days <=0` 永久；`--feature k=v` 可覆盖单项能力（如 `max_sessions=16`）。

## 4. 台账查询 / 对账

```powershell
python license_server.py listcodes            # 兑换码：档位/座位消耗/被授权方/停用标记
python license_server.py listtrials           # 试用：每机一行（签发/到期/试用中|已到期）
python license_server.py stats --weeks 8      # 漏斗一屏 + 按签发周转化时序（判断转化在变好还是变坏）
python license_server.py expiring --window 48 # 即将到期的试签（销售跟进名单，只读）
python license_server.py customers --limit 50 # 客户健康度行级视图（回执按 anon_id 聚合，售后先看这行）
```

同数据的 HTTP 版：`GET /api/funnel`（含 weekly 时序，脚本对接）；`GET /api/customers`（客户行级 JSON）；
`GET /dashboard`（漏斗卡+时序小图+客户健康度表，点行下钻失败组件）。
转正口径=试用指纹在试签签发之后出现过兑换码激活（台账即真相，无客户端埋点）。

**临期主动通知**：`serve` 运行期间每小时自动扫描试用台账，剩 48h 内到期且未通知过的试签
→ 走 alerts webhook 推送「试用临期·建议跟进转化」（厂商机配 `AVATARHUB_ALERT_WEBHOOK`
或 `secrets/alert_webhooks.txt`；不配置则仅记控制台）。每机只提醒一次（标记进 trials.json）。

**一键发码（P14 · 通知里的转化动作）**：临期通知文案自动附 `/quickissue?fp=…&exp=…&sig=…`
签名链接（HMAC 由私钥派生，7 天时效，改参即拒）。销售点开 → 确认页「一键出码」→ 出
`--qi-edition/--qi-days` 规格的正式兑换码（自动复制，发客户输码即转正）。同指纹幂等：
上一张码没被激活就复用不重发；台账记 `via=quickissue` 可对账。链接地址用 `--public-base` 配置。

**发码→激活闭环（P15）**：客户激活 quickissue 码 / 试用客户用码转正的那一刻，服务自动
回推 webhook「闭环达成/试用转正」——销售不用轮询台账（`--no-notify-activate` 可关）。
转化衰减看 `stats` 或看板的**一键发码四级漏斗**：临期通知 → 链接点开 → 出码 → 激活
（点开数=确认页首开指纹数，记在 orders.json 的 `qi_opened`）。

**客户健康告警（P15）**：`serve` 每小时聚合一次回执，会话/组件成功率低于 `--health-th`%
且近 7 天活跃、回执≥3 份的客户 → 推「客户健康度破线·建议回访」（每客户每日至多一次，
标记在 `secrets/health_notify.json`）。看板 ⚠ 行与告警口径同源。

## 5. 吊销（退款 / 密钥泄露 / 违约）

```powershell
python license_admin.py revoke --lic-id <序列号> --reason "退款收回"   # 精确（推荐，listcodes 可查）
python license_admin.py revoke --machine <指纹> --reason "..."         # 按机器（同机重签会一起死，配 --issued 精确）
python license_admin.py list-revoked                                    # 核对名单
python license_admin.py unrevoke --lic-id <序列号>                      # 误吊回滚
```

**分发**（吊销要到达客户机才生效，两选一）：
- 在线：客户产品自动从激活服务 `GET /api/revocations` 拉取（有缓存+防回滚）；
- 离线：把签名后的 `revocations.json` 发给客户放产品根目录。
名单缺失/被篡改=视为无吊销（fail-safe），所以**吊销后要确认送达**（授权卡「吊销名单」行有条数+更新时间）。

## 6. 审计线（谁在何时激活/试用/还原）

客户机 `logs/alerts.jsonl` 里 `source=avatar_hub/授权` 的事件（激活/试用升级/还原/自动还原，含失败）：

```powershell
Get-Content logs\alerts.jsonl | ConvertFrom-Json | ? source -like "*授权*" | ft ts,title,detail
```

厂商侧对账用 `secrets/orders.json` 的 `activations[]`（指纹/时间/序列号）与 `trials.json`。

## 7. 客户侧行为速查（支持答疑用）

| 现象 | 机制 |
|---|---|
| 不放 license.key 也能用 | 软降级 `trial` 档（限时+受限），`AVATARHUB_LICENSE_ENFORCE=1` 才真拦 |
| 到期后没立刻锁 | 正式授权有宽限期（默认 7 天，`LICENSE_GRACE_DAYS` 可调）；**试用签到点即收，无宽限** |
| 试用到期自动变回原授权 | P11 软着陆：试用升级前自动备份正式授权，到期自动还原 |
| 换机不能用 | 授权绑机器指纹；客户发新指纹，重签或 addcode 新座位 |
| 输码大小写/空格错也激活成功 | 服务端归一化容错（精确匹配优先，自定义码不受影响） |
| 「联系厂商」按钮内容 | 产品「🎨 品牌白标 → 联系方式」配置（brand.json.contact），随白标交付设置 |
