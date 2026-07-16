# 官网运营后台运维一页纸（CONSOLE OPS）

> 对象：接手「客户 / 机器名单 / 授权发放 / 兑换码 / 订单 / 销售 / 到期 / 客服账号」统一管理的运维与销售。
> 位置：**官网 bd2026.cc**（Next.js 仓库 `C:\web117`，服务器 `/home/ubuntu/yuntech`，PM2 名 `yuntech`）。
> 底座：**签发私钥永不上服务器**——官网做控制面 + 客户登记 + 客户激活；本机签发机 `sign_worker.py`
> 轮询官网签发队列、就地用私钥签发再回填。服务器被攻破也伪造不了授权。

## 0. 组成

**官网侧（`C:\web117`，随官网部署）**
| 文件 | 作用 |
|---|---|
| `app/console/page.tsx` + `layout.tsx` | 运营后台 SPA（登录 + 概览/客户/机器/授权/兑换码/订单/销售/客服账号/审计） |
| `lib/console-auth.ts` | 客服多账号（scrypt 口令 + HMAC 无状态会话 + 角色 + 审计） |
| `lib/console-data.ts` / `console-issue.ts` | 客户/机器/授权/码/签发队列/CRL 数据层（JSON 文件存储，落 `~/hualing-leads`） |
| `app/api/console/*` | 后台 API（会话鉴权）：overview/customers/machines/licenses/codes/orders/sales/agents/audit + `sign/pull`·`sign/complete`（签发机桥） |
| `app/api/register` | 产品联网注册（机器名单数据源，公开） |
| `app/api/activate`（已扩展） | 客户端在线激活：订单号（AH-）原样取回；兑换码（AVH-）走签发桥 |
| `app/api/revocations` | 分发签名吊销名单 |

**本机侧（`D:\projects\模仿音色`，持私钥）**
| 文件 | 作用 |
|---|---|
| `sign_worker.py` | 签发机：轮询官网签发队列 → 本地私钥签 → 回填（路线A/B/CRL 都靠它） |
| `register_sign_worker_task.ps1` / `sign_worker_watch.bat` | 把签发机注册成自愈常驻计划任务（install/status/stop/remove） |
| `admin_client.py` | 产品端联网注册模块（心跳式；已接入 `avatar_hub.py` + `faceswap_api.py`，默认上报 bd2026.cc） |
| `register_machine_task.ps1` / `machine_register.bat` | 给不跑 Hub/换脸的机器（STT/口型分机等）一键自登记（每 3h 上报） |
| `license.py` / `license_admin.py` | 客户端验签 / 私钥与签发原语（worker 复用） |

## 1. 首次开通（一次性）

**官网服务器**（`C:\web117` 部署到 bd2026.cc）：设环境变量（`prod.env.local`）
```
CONSOLE_ADMIN_USER=admin          # 首个管理员用户名（首启无账号时自动建）
CONSOLE_ADMIN_PASS=<强密码>        # 首个管理员密码（建完后可删此两行）
CONSOLE_SECRET=<随机长串>          # 会话签名密钥（不设则复用 ADMIN_KEY）
# ADMIN_KEY 已有（客服/签发机/脚本共用的运维密钥）
```
部署：`cd C:\web117; ./scripts/deploy.ps1`（Posh-SSH，密钥自动）。浏览器开 **https://bd2026.cc/console** 登录。

**本机签发机（一键常驻，推荐）**：
```powershell
# 注册成自愈计划任务：立即启动 + 每 2 分钟检查自愈(崩溃/杀进程自动重启) + 单实例 + 随可用即启
powershell -ExecutionPolicy Bypass -File register_sign_worker_task.ps1
powershell -ExecutionPolicy Bypass -File register_sign_worker_task.ps1 -Action status   # 看是否在跑
powershell -ExecutionPolicy Bypass -File register_sign_worker_task.ps1 -Action remove   # 卸载
# 手动前台跑（调试用）：python sign_worker.py --watch 20
```
签发机复用 `secrets/deploy/`（站点地址 + ADMIN_KEY）与 `secrets/license_vendor_sk.pem`（私钥）。
免管理员即可注册；`--watch 20` 每 20s 轮询一轮，签发体感接近即时。日志 `logs\sign_worker.log`。
> 免管理员的任务在**用户登录后**随可用即启（重启后自动续跑）；若要"关机重启后未登录也自动跑"，
> 用管理员 PowerShell 跑同一脚本可再加开机触发/服务化。

**机器名单怎么来（让机器自动出现）**：
- 跑 **Hub(`avatar_hub.py`)** 或 **换脸(`faceswap_api.py`)** 的机器：已内置 `admin_client.install`，启动即登记 + 每 6h 心跳保鲜，自动出现在「机器」页，无需操作。
- **不跑上述服务的机器**（如 STT 分机 140 / 口型冷备 198）：在该机项目目录跑一次
  `powershell -ExecutionPolicy Bypass -File register_machine_task.ps1`（每 3h 自动上报本机信息）；
  或只临时登记一次：`register_machine_task.ps1 -Action now`（等价 `python admin_client.py`）。
- 关闭上报：`AVATARHUB_ADMIN_REGISTER=0`。
- 机器登记后,若其指纹已有归属某客户的授权,会**自动归属到该客户**(免手动分配)。

**签发机健康可见**：后台「概览」页顶部实时显示**签发机在线/离线 + 最后活跃时间**(用签发机每 20s 的轮询当心跳)。
离线且有待签发时红色告警——授权卡在「签发中」时先看这里,离线就去签发机启动 `register_sign_worker_task.ps1`。

**签发机离线自动告警(主动推送)**：由**服务器 cron 每 2 分钟**巡检 `/api/admin/signworker-check?key=…`
(另 `/api/admin/order-sla` 每 10 分钟也兜底巡检一次),心跳陈旧即推送管理员、恢复即通知。
默认无心跳 **>150 秒判离线**(`SIGN_WORKER_OFFLINE_SECS` 秒 / `SIGN_WORKER_OFFLINE_MIN` 分 可调)——
掉线约 **2.5–4.5 分钟内**告警;仍离线每 6h 再提醒一次。管道:默认复用官网 Telegram 管理员通知;
另可设 `SIGN_ALERT_WEBHOOK`(钉钉/企业微信群机器人 URL)同时推送。
演练:`GET /api/admin/signworker-check?key=…&dry=1&simulate=1`(key 鉴权,dry 不真发)或后台会话
`GET /api/console/sign/health?simulate=1` 预览告警文案。

**每日健康日报(含签发机)**：搭车 `/api/admin/order-sla` cron,每天 9 点后推一次摘要(站点+签发机+DeepSeek+TG)。
预览:后台会话 `GET /api/console/digest/preview`(只读不发)。`SIGN_ALERT_WEBHOOK`/Telegram 同上。

## 2. 客服日常（全在 /console 点鼠标）

| 想做什么 | 在哪 |
|---|---|
| 建客户、填联系方式/公司/标签、（管理员）分配归属客服 | **客户** → 新建 |
| **路线A**：按指纹（或 `*` 站点授权）签发 → 状态转「有效」后下载 `license.key` / 复制授权码发客户 | **授权** → 签发授权 |
| **路线B**：批量生成兑换码（档位/天数/座位）→ 复制发客户，客户端输码自助激活 | **兑换码** → 生成 |
| 看谁装了机器、GPU/版本/档位/在线状态、归属客户 | **机器**（产品联网自动建档） |
| 订单标记已付 / 一键按指纹开通 / 标记开通 | **订单** |
| 营收（累计/近30天/按套餐）、30 天内到期授权 | **销售** |
| 吊销/恢复授权（签发机重签 CRL 下发） | **授权** → 吊销 |
| 新建/停用客服账号、设角色、改密 | **客服账号**（仅管理员） |
| 谁在何时改了什么 | **审计日志** |

角色：`admin`（全权+管账号）/ `agent`（客服，管客户/授权/码/订单）/ `viewer`（只读）。

## 3. 授权是怎么签出来的（异步桥，理解一次即可）

1. 客服点「签发授权」或客户输兑换码 → 官网在授权库建一条 `status=签发中` 记录 + 入**签发队列**。
2. 本机 `sign_worker.py` 轮询到 → 用本地私钥签出授权 → 回填官网（状态转「有效」，写入 `doc`）。
3. 路线A：客服在「授权」页看到「有效」即可下载 key / 复制授权码。
   路线B：客户端「在线激活」首次返回「正在开通，请稍后重试」，签好后再点即得（与订单开通同体验）。

> 所以「签发中」停留过久＝检查签发机是否在跑（`sign_worker.py`）。这是私钥不上服务器的必要代价，
> 把 worker 设 `--watch 20` 或计划任务高频跑即接近即时。

## 4. 让产品连到官网后台

客户端默认激活地址就是 `https://bd2026.cc`（`license.py._DEFAULT_ACTIVATION_URL`），开箱即用：
- 在线激活（路线B）/一键试用 → `POST /api/activate`、`/api/trial`；
- 联网注册（机器名单）→ `POST /api/register`（`admin_client.py` 已接入 Hub 启动，默认走激活地址）；
- 在线拉吊销名单 → `GET /api/revocations`。

白标/私有部署可用 `AVATARHUB_ACTIVATION_URL` / `AVATARHUB_ADMIN_URL` 覆盖；`AVATARHUB_ADMIN_REGISTER=0` 关注册上报。

## 5. 安全与鉴权

| 关注点 | 现状 |
|---|---|
| 客服口令 | scrypt（N=16384）不可逆；账号存 `~/hualing-leads/console-users.json` |
| 会话 | HttpOnly + SameSite=Strict 的 HMAC 签名 token（PM2 重启/多实例不掉登录）；12h 过期；篡改角色即失效 |
| 登录限流 | 单 IP 10 分钟 8 次失败即 429 |
| 后台 API | 一律要会话（`X-Robots-Tag: noindex`；`/console` 不入索引） |
| 签发机桥 | `sign/pull`·`sign/complete` 用 `ADMIN_KEY`（`x-setup-key`，与 fulfill_orders 同款），仅本机签发机持有 |
| 私钥 | 只在本机 `secrets/license_vendor_sk.pem`；官网从不接触；库里只存已签 `doc` |
| 与老 /admin | 完全并存：老 `/admin`（增长/内容/CRM/兑换折扣码）不受影响；`/console` 专管授权与客户经营 |

## 6. 数据 / 备份

后台数据都在服务器 `~/hualing-leads/`（与现有 leads/orders 同目录）：`console-users.json`、
`console-customers.json`、`console-machines.json`、`console-licenses.json`、`console-codes.json`、
`console-signq.json`、`console-revocations.json`、`console-audit.jsonl`。沿用现成的
`scripts/pull_leads_to_117.ps1` 异地备份即可一并覆盖（同目录）。

## 7. 自测

- 官网侧口令/会话逻辑：Node 跑过 round-trip（scrypt 校验、会话验签、防提权篡改、canonical 键序与 Python 一致）8/8。
- 签发机产物：临时密钥验证 `sign_worker` 签出的授权/CRL 能被产品内置公钥验签、篡改即失败 5/5。
- 上线前建议在 `C:\web117` 跑 `npm install && npm run build` 做一次 Next.js 类型/构建检查（本机无 npm，未代跑）。
