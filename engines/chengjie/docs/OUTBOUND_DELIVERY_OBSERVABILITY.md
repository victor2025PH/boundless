# 出站投递观测（发出去了吗）

一句话：**按平台看「未送达率」与「拒因分布」**，读出走 ops-overview 的「📤 出站投递」卡、
`/api/workspace/metrics.outbound_delivery`、Prometheus `outbound_delivery_*`。
不健康时经 HealthWatchdog 发 `outbound_delivery_alert`（订阅别名 `outbound_delivery`）。

## 为什么有这张卡

worker 的发送失败此前只落 `app.log`。`{"delivered": false, "error": "forbidden"}`
被各调用方自己处理掉，没有任何地方回答「这个号这小时发出去几条、被拒几条、为什么」。

Discord 接入把这个洞照亮了——Bot **不能主动私信陌生人**，403 是**日常业务信号**
（社群覆盖不够）而不是故障。但这不是 Discord 独有的：WhatsApp 会话过期、Telegram
`PEER_ID_INVALID`、Messenger 24h 窗口关闭，每个平台都有自己的「正常的失败」，
过去全都同样不可见。

## 口径（四个刻意的选择）

1. **成功也计**。只报失败数的看板是骗人的：40 次失败，分母是 40 还是 40000，是
   「链路断了」和「正常摩擦」两种处境。卡上给的是**率**，不是裸计数。
2. **护栏拦截计入「未送达」**。Kill-Switch / 反封号闸门拦下的那条话，客户同样没收到。
3. **拒因取族名，不取原文**。`kill_switch:telegram:acct-7` → `kill_switch`；异常
   消息先按关键词归进小枚举，都不中才退到 `exc_<类名>`。
4. **入队 ≠ 已送达**（`queued` 单列）。RPA 适配器返回 `delivered=True, queued=True`
   只表示交给了队列；手机关机数小时时若按 sent 记，看板会显示 100% 成功。`queued`
   **不进 attempts**；真正成败由 RPA 物理出口稍后记账。「queued 涨而 attempts 不动」
   ＝队列卡住——看门狗会单独报 `stuck_queue`。

## 覆盖面（四条栈都已接）

| 记录点 | 覆盖 |
|---|---|
| `AccountOrchestrator.send` / `send_media`（外层 `claim_send`） | 编排器托管的协议号与官方 API 号 |
| `channel_adapters.send_via_adapters` 适配器回落（外层 `claim_send`） | 编排器不拥有该账号时 |
| Telegram A 线 `client/sender.py` / `voice_sender.py`（`only_if_outermost`） | Pyrogram 直发缝 |
| LINE/WA/Messenger RPA 物理出口 + 官方 webhook 叶子 HTTP（`meter_send` / `record_ok_send`） | `_pace_and_send`、WA `_send_text_coord_fallback`、Messenger `_send_reply_with_retry`、`fb_send_message` / `wa_send_text` / `line_push` / … |

**去重＝最外层占坑**：路由层进入真正的 worker 调用前 `with claim_send():`，内层叶子带
`only_if_outermost=True` 自动让位。选「外层赢」——外层是一条逻辑消息一次记录（内层有重试），
且外层看到的正是调用方看到的结果。

**仍不计**：Node 微服务（messenger-web / whatsapp-baileys）进程内重试——独立进程写不到
本计数器；它们的失败经 HTTP 返回编排器时已被第 1 条记下。

## 告警（不是裸失败率）

`outbound_delivery_health.diagnose` 把原因分成三类，**按平台分开判**：

| kind | 含义 | 该做什么 |
|---|---|---|
| `infra` | 超时/网络/鉴权/no_worker/未知异常——有人能修 | 查链路 / 重登 / 看 worker |
| `blocked` | Kill-Switch / send_gate 激增 | 去看开关，不是去看网络 |
| `stuck_queue` | queued 涨、同平台 attempts 不动 | 查手机 / ADB / RPA runner |

**刻意不告警**：`forbidden` / `window_expired` / `peer_invalid` / `media_too_large` 等
业务性拒收——把它们和网络故障混在同一个阈值里，运维一周内就会学会无视告警。

配置：`health_watchdog.outbound_delivery_remind`；Webhook 订阅别名 `outbound_delivery`
（受众目录归 business——老板听得懂「消息发不出去」）。

## 读法

| 现象 | 含义 | 该做什么 |
|---|---|---|
| `discord: forbidden` 占多数 | Bot 不能主动私信陌生人 | **获客问题**：入口改成先进服务器 |
| `whatsapp: session_unhealthy` 升高 | Baileys 会话掉了 | ops「平台会话健康」点重新登录 |
| `peer_invalid` 升高 | 名单里大量注销号 | 清名单 |
| `queued` 涨、attempts 不动 | RPA 队列只进不出 | 查手机/ADB |
| `blocked: kill_switch` 满屏 | 急停开着 | 预期行为，确认是不是忘了关 |
| `exc_*` 出现新面孔 | 没归类过的异常 | 看要不要进 `_EXC_PATTERNS` |

## 门禁

- `tests/test_outbound_delivery_stats.py`：分类器、queued、占坑去重、`meter_send`、编排器/适配器接线
- `tests/test_outbound_delivery_health.py`：infra / blocked / stuck_queue 判据与业务原因豁免
