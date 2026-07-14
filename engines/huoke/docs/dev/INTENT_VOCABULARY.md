# Intent 分类体系词表（2026-04-23）

> 本项目有 **两套独立的 intent 分类体系**,长期保持分开,不合并。本文档解释
> 两者的边界和使用场景,避免未来开发者把它们串用。
>
> 起因: B 机 PR #8 Q8 — "chat_intent (8 类) vs intent_classifier (9 类) 分开
> 还是合并?" A 机答复: 分开。本文档正式固化这个决策。

## 一、两套体系定位

| 维度 | `src/ai/chat_intent.py` (B 维护) | `src/ai/intent_classifier.py` (A 维护) |
|------|-----------------------------------|------------------------------------------|
| **语义焦点** | 单轮对话的**即时决策**（当前消息要怎么回） | lead 在**漏斗中的归类**（对方处于销售流程哪一阶段） |
| **时间尺度** | 瞬时（本轮 incoming 消息触发） | 累积（可跨多轮, 经常 10+ 轮对话后定型） |
| **典型调用方** | `_ai_reply_and_send`(B)、`referral_gate`(B) | TikTok lead scoring, CRM 同步, 漏斗分析 |
| **LLM 预算** | rule-first, ~60% 零 LLM; 模糊意图才调 | 原设计偏 LLM-heavy, 有缓存 |
| **输出对象** | `ChatIntentResult(intent, confidence, source)` | `ClassificationResult(intent, confidence, next_action, ...)` |
| **枚举数** | 8 类 | 9 类 |

## 二、词表对照

### 2.1 B 的 chat_intent (Messenger 对话状态机)

```python
# src/ai/chat_intent.py
INTENTS = (
    "opening",      # 开场破冰(peer 首次发言)
    "smalltalk",    # 日常闲聊,无明显意图
    "interest",     # 表现兴趣(问产品/服务细节)
    "objection",    # 反对/疑虑(价格/怀疑/犹豫)
    "buying",       # 强购买信号(报价/下单)
    "referral_ask", # 对方主动要联系方式(LINE/WA/TG)
    "closing",      # 对话收尾(告别)
    "cold",         # 冷场(单字/表情/无内容)
)
```

下游决策示例（B 的 `referral_gate`）:
- `intent=referral_ask` → **hard_allow** 引流 (对方明确要, 必须回)
- `intent=buying` → hard_allow 引流 (强信号)
- `intent=cold` → skip 或发短句
- `intent=objection` → LLM prompt 加"温和回应消除疑虑"
- `intent=closing` → 发简短告别

### 2.2 A 的 Intent (TikTok lead 漏斗归类)

```python
# src/ai/intent_classifier.py
class Intent(str, Enum):
    INTERESTED   = "interested"     # 表现兴趣 (泛)
    QUESTION     = "question"       # 问问题
    POSITIVE     = "positive"       # 正向回应
    NEGATIVE     = "negative"       # 负面回应
    SPAM         = "spam"           # 垃圾信息
    NEUTRAL      = "neutral"        # 中性
    MEETING      = "meeting"        # 要求见面/通话
    REFERRAL     = "referral"       # 推荐给他人
    UNSUBSCRIBE  = "unsubscribe"    # 要求退出
```

下游决策示例（A 的 `NEXT_ACTION_MAP`）:
- `Intent.MEETING` → `schedule_meeting`
- `Intent.REFERRAL` → `contact_referral`
- `Intent.UNSUBSCRIBE` → `blacklist`
- `Intent.NEGATIVE` → `respect_and_pause`
- `Intent.SPAM` → `ignore`

## 三、关键差异点

### 3.1 `referral` 语义**完全相反**

| 字段 | B 的 `chat_intent="referral_ask"` | A 的 `Intent.REFERRAL` |
|------|------------------------------------|------------------------|
| 方向 | **对方向我们要**联系方式 | **对方推荐其他人**给我们 |
| 我们动作 | **发出**我们的 LINE/WA ID | **问候**被推荐的人 |
| 业务意图 | 引流即将成功的信号 | 二次获客线索 |

⚠️ **绝对不要混用这两个字段**。看字符串不一样 (`referral_ask` vs `referral`),
就是防混用的。

### 3.2 `buying` vs `INTERESTED`

- B 的 `buying` = "强购买信号" (有报价/数量/下单动作)
- A 的 `Intent.INTERESTED` = "对产品表示兴趣" (问产品特性/适用场景)

`buying` 是 `INTERESTED` 的**更进一步**。如果要同时做两层归类:
- B 的单轮判: `buying`
- A 的漏斗累积判: `INTERESTED` (或更高级的 `MEETING`)

### 3.3 `cold` vs `NEUTRAL`

- B 的 `cold` = 对方只回复 "嗯" / 😊 / "." 等 (对话濒临死亡)
- A 的 `Intent.NEUTRAL` = 对方有意义地回复但无强烈倾向 (常规闲聊)

B 的 `cold` **更消极**, 需要 `_ai_reply_and_send` 考虑要不要干脆 skip。

## 四、约定: 未来如何演进

### 4.1 新增 intent 的流程

- B 在 `chat_intent.INTENTS` 添新值: 不影响 A, 自行决定
- A 在 `Intent` Enum 添新值: 不影响 B, 自行决定
- **如果一个场景 A+B 都需要表达同一概念**: 在本文档 §五 双词表对照里加一行,
  但**仍用各自的字符串**,不强制命名一致

### 4.2 不要在 API/事件数据里混写

- 写 `fb_contact_events.meta_json` / `lead_journey.data_json` 等 JSON 字段时:
  - B 自己的场景用 `"chat_intent": "referral_ask"` (key 带前缀 chat_)
  - A 自己的场景用 `"lead_intent": "referral"` (key 带前缀 lead_)
  - 不写 `"intent": ...` 没有前缀的 key

### 4.3 禁止跨模块 import

- B 不要 `from src.ai.intent_classifier import Intent`
- A 不要 `from src.ai.chat_intent import INTENTS`
- 如有真需要(如 CRM 要用 B 的对话状态), 应写**转换函数**而不是直接 import, 避免耦合

## 五、双词表对照(参考, 不用于代码)

| 业务场景 | B (chat_intent) | A (intent_classifier.Intent) | 注释 |
|----------|-----------------|------------------------------|------|
| 对方问产品细节 | `interest` | `INTERESTED` / `QUESTION` | B 细一级,A 分两类 |
| 对方报价下单 | `buying` | `INTERESTED` (或自定义新值) | B 专有, A 可以 `INTERESTED` 兜底 |
| 对方问 LINE/WA | `referral_ask` | — | A 无对应; 引流属 B 的单轮决策范畴 |
| 对方推荐其他人给我们 | — | `REFERRAL` | B 无对应; 二次获客属 A 的漏斗范畴 |
| 对方要见面/通话 | `buying` (含 meeting 暗示) | `MEETING` | B 合并,A 分独立 |
| 对方疑虑反驳 | `objection` | `NEGATIVE` (若很强烈) | B 更细, A 更粗 |
| 对方说再见 | `closing` | `NEUTRAL` | B 专有, A 无动作 |
| 对方冷场 | `cold` | `NEUTRAL` | B 专有, A 无动作 |
| 对方闲聊 | `smalltalk` | `NEUTRAL` | 大致对应 |
| 首次开场 | `opening` | `NEUTRAL` | B 特化首轮 |
| 垃圾信息 | — | `SPAM` | A 专有(B 场景一般已被 referral_gate 挡住) |
| 退订 | — | `UNSUBSCRIBE` | A 专有 |
| 正向回应 | `smalltalk` 或 `interest` | `POSITIVE` | A 独立,B 按程度散到多类 |

## 六、变更历史

- 2026-04-23 首版 — A 机起草, 回应 B PR #8 Q8 "分开还是合并?"
- 源 PR: `feat-a-phase5-mesh` 后续 post-review commit
