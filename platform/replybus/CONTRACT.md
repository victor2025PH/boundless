# platform/replybus · 决策回执总线契约（执行层 ⇄ 承接大脑，同步问答）

> 版本：v1（融合期新增） · 2026-07-19
> 归属：`platform/replybus/` · 配套：`client.py`（stdlib 瘦客户端，可降级）、`reply_schema.json`（decide 请求/响应信封）
> 定位：产业链去重后，**TG-AI智控王 = TG 采集触达执行层**（拥有并操作 Telegram `.session`、负责真正发消息），**chengjie = 承接大脑**（决定怎么回）。二者之间的接缝：智控王收到一条入站私信 → 问 chengjie 『这条怎么回』→ chengjie 返回**决策** → 智控王在**自己的 session 上**执行发送。本契约就是这条"同步决策回执"通道。

---

## 0. 为什么是新的一条契约（不是并进 leadbus / enable）

| 契约 | 语义 | 方向与时效 | 为何盖不住本接缝 |
|---|---|---|---|
| `leadbus` | 线索**单向投递**（fire-and-queue，可落 outbox 补投） | 获客 → 承接，异步，晚到无妨 | 回执决策是**同步问答**：对方正在等回复，过时决策没有意义，不可排队补投 |
| `enable` | 赋能**能力调用**（TTS/翻译/渲染，给定输入出产物） | 承接 → 引擎能力面 | 方向相反：本接缝是**执行层问大脑要决策**，而非承接调能力出产物 |
| `replybus`（本契约） | **决策回执**：一问一答，答案是"怎么回"的指令 | 执行层 → 承接大脑，同步、短时效 | —— |

> 判断口诀：**"这条数据是丢过去慢慢处理的（leadbus），还是要个产物（enable），还是现在就要一个'怎么办'的答复（replybus）？"**

## 1. 能力与归属

| 能力 | 谁做 | 怎么触达 |
|---|---|---|
| **回复决策**（入站私信 → send/draft/silent/handoff 决策） | chengjie（承接大脑：人设/话术/意向判断都在它那） | `POST /api/replybus/decide` |
| **执行发送**（把决策变成真实 TG 消息） | **TG-AI智控王**（`.session` 的唯一持有者与操作者） | 智控王本机执行，**不经任何 HTTP** |
| **本地兜底**（总线不可达时的回复生成） | 智控王本地 `ai_auto_chat` | 智控王本机，`action="fallback"` 时触发 |
| **状态（决策口可达性）** | chengjie | `GET /api/replybus/status` |

> 设计含义：chengjie 对智控王是**增强项而非必需项**——大脑离线时，智控王靠本地
> ai_auto_chat 照常收发（总线可选）。这与 enable"赋能面全离线仍能发纯文本"同构。

## 2. 契约（stdlib 瘦客户端签名）

`platform/replybus/client.py`（纯 stdlib，零第三方依赖，零反向依赖）：

```python
ReplyBusClient(bus_url=None, timeout=6.0)   # bus_url 缺省读环境变量 BOUNDLESS_BUS_URL（与 leadbus 同源，指向 chengjie）
  .decide(message)          -> dict      # POST /api/replybus/decide → 决策；任何失败收敛 action="fallback"（永不抛）
  .status()                 -> dict      # GET  /api/replybus/status（不可达则 available=False）
  .available()              -> bool      # 承接中台决策口是否就绪
  .envelope_error(message)  -> str|None  # 消息信封校验（静态方法）：platform+external_id+text 必填
```

- **决策端点**：`POST {BOUNDLESS_BUS_URL}/api/replybus/decide`，body `{"message": <信封>}`；由 chengjie 承接侧实现。
- **刻意没有 outbox**（与 leadbus 的关键差别）：决策是同步时效数据，错过就走本地兜底，补投一条昨天的"怎么回"只会造成事故。
- **不做决策实现**：客户端不含任何话术/意向逻辑——那些只在 chengjie 侧发生；也不含发送逻辑——那只在智控王侧发生。

## 3. 消息信封与决策（详见 reply_schema.json）

请求 `message`（智控王 → chengjie）：

```jsonc
{
  "platform": "telegram",            // 必填：来源平台
  "account": "acct_pool_007",        // 执行账号引用（池化编号，非凭据；.session 永不出智控王本机）
  "external_id": "tg:987654321",     // 必填：对方在平台内的唯一 ID
  "text": "在吗？想了解一下代发",     // 必填：入站原文（隐私红线见 §6）
  "msg_id": "m_10086",               // 平台消息 ID（chengjie 可据此幂等：同一 msg_id 重复问询返回同一决策）
  "session_id": "s_tg_987654321",    // 可选：会话线索串联
  "context_hint": { "lang": "zh", "funnel_stage": "new" }   // 可选：轻量上下文提示
}
```

响应（决策，chengjie → 智控王）：

```jsonc
{
  "available": true,
  "action": "send",                  // send=发文本 / draft=生成草稿待人工确认 / silent=不回 / handoff=转人工
  "text": "可以的，方便说下您主要发什么品类吗？",   // action=send/draft 时的回复文本
  "media": null,                     // 可选：随发媒体引用（URL/资源 ID）
  "persona": "sales_amy",            // 可选：本次决策采用的人设
  "delay_ms": 1200,                  // 可选：建议拟人化延迟（由智控王执行时落实）
  "reason": "new_lead_greeting"      // 可选：决策理由短码（可观测用，不含原文）
}
```

客户端本地合成（服务端永不返回）：`{"available": false, "action": "fallback", ...}`。

## 4. 可降级说明（大脑离线时执行层如何优雅退化）

`decide()` **绝不抛异常**。所有失败路径统一收敛为 `action="fallback"`：

| 情形 | decide 返回 | 智控王行为 |
|---|---|---|
| `BOUNDLESS_BUS_URL` 未配置（单机模式） | `{"available":false,"action":"fallback","mode":"standalone"}` | 本地 ai_auto_chat 兜底 |
| 连接失败 / 超时 / HTTP 5xx / JSON 解析失败 | `{"available":false,"action":"fallback","error":...}` | 本地 ai_auto_chat 兜底 |
| 总线可达但决策非法（缺 action / 未知动作） | `{"available":false,"action":"fallback","error":...}` | 本地兜底（不执行可疑决策） |
| 信封非法（调用方 bug） | `{"available":false,"action":"fallback","rejected":true,"error":...}` | 本地兜底，顺带修信封 |

这保证：**智控王脱离总线能独立跑（总线可选），接上总线自动升级为"大脑决策"，一条私信都不冷场。**

## 5. 防双发 / session 归属（本契约头等大事）

**执行权归智控王**——这是铁律，不是建议：

1. **`.session` 归属**：Telegram `.session` 由智控王持有并独占操作；chengjie **不持有、不触碰**任何获客会话的 session 凭据。**承接用号与获客用号分池**：chengjie 自有坐席号只用于承接侧自己的会话，绝不用于回复经 replybus 问询的获客会话。
2. **chengjie 只返回决策，绝不代发**：`/api/replybus/decide` 是纯函数式问答——收信封、回决策，服务端处理中不产生任何对该会话的发送副作用。决策响应里**不存在也永远不得加入**"chengjie 已发送 / sent / delivered"语义字段；若未来加入，即视为契约破坏。
3. **两条生成路径互斥**：客户端保证——只有 `available=True` 且 `action ∈ {send,draft,silent,handoff}` 的决策才交给执行；其余一律 `fallback` 走本地 ai_auto_chat。同一条入站消息要么用总线决策、要么用本地兜底，绝无两者都发。
4. **迟到决策天然无害**：问询超时后客户端已 fallback，chengjie 即便迟到算出决策，它不持有该 session，物理上无从发送——防双发不依赖时序运气，而依赖"执行单点"。
5. **重复问询幂等**（chengjie 侧待接线要求）：同一 `msg_id` 重复问询应返回同一决策，防止调用方重试导致语义分叉。

## 6. 隐私边界（红线，与 observability EVENT_CONTRACT 配合）

- `message.text`（入站私信原文）是**点对点业务载荷**，只在"智控王 ↔ chengjie"两端之间流转：只作本次请求载荷，**不落任何日志/事件/集团数仓**。`client.py` 不打印原文（自测的 mock 服务器也静音访问日志）；chengjie 侧同样不得把原文写进日志或转发进 observability spool。
- 可观测走**另一条管道**：调用方就近另发 observability 计数事件（如 `zhituo.reply.decided`，维度只有 action/persona/延迟等，**不含原文**）。本客户端刻意只做问询、不发遥测（单一职责，与 leadbus 同构）。
- `account` 是池化账号引用（编号），不是凭据；`.session` 文件与登录态永不出智控王本机。

## 7. 依赖方向

`智控王（执行）→ platform/replybus(契约+client) →(HTTP)→ chengjie /api/replybus/decide（大脑）`。
platform 不 import 任何产品/引擎，仅通过 HTTP 契约交互；话术/人设/意向模型留在 chengjie，
`.session` 与发送执行留在智控王。

## 8. 落地状态与下一步

- 本层交付：`CONTRACT.md`（本文件）+ `client.py`（瘦客户端 + `--selftest` 全通过）+ `reply_schema.json`。
- 待接线（下一阶段）：
  1. chengjie 侧实现 `POST /api/replybus/decide` + `GET /api/replybus/status`（按 `msg_id` 幂等；接人设/话术/意向管线；恪守 §5 不代发、§6 原文不落日志）；
  2. 智控王在"收到入站私信"处调 `ReplyBusClient().decide(...)`，按 action 分派执行（send 落实 `delay_ms` 拟人节律；draft 进人工确认队列；handoff 挂转人工），`fallback` 走本地 ai_auto_chat，并**另发** observability 计数事件（不含原文）；
  3. 承接用号与获客用号分池的账号台账落到 `platform/identity`（防双发的组织侧保险）。
