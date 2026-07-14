# 引流加 LINE 触发器 + persona 分组 + 真人后台 — 设计文档

> 业务定位：日本 37-60 岁女性 **情感陪护聊天**，机器人陪聊到感情升温后转给真人客服做真实情感聊天。
>
> 决策日期：2026-04-26
>
> 关键参数（victor 拍板）：聊够 7 轮才发引流话术 + LINE，拒绝后冷却 7 天。

---

## 一、术语澄清

### 真人 ID 是什么

不是新发明的概念，就是项目已有的 `users.json` 里的 **username**。

举例：
```json
[
  {
    "username": "agent_zhang",
    "password": "<PBKDF2-SHA256>",
    "role": "customer_service",
    "display": "客服-张三"
  },
  {
    "username": "agent_li",
    "password": "<PBKDF2-SHA256>",
    "role": "customer_service",
    "display": "客服-李四"
  }
]
```

真人在 Web 后台登录后：
- 看到"待接管队列"
- 点"我接手"按钮 → 系统把当前登录的 username 写入 `lead_handoffs.receiver_account_key`（已有字段）
- 加 username 写入 `customer_handoffs.accepted_by_human`（L2 PG 字段）
- 真人发消息时，agent_mesh 的 from_agent = `human:<username>`（现有支持）

**结论**：直接用 `users.json` + `auth.py` 的现有认证系统，新增几个 `role="customer_service"` 账号即可。

---

## 二、双 handoff 系统并存策略（重要）

项目里**有两套 handoff 表**，各有各的用途：

| 表 | 路径 | 用途 | 状态 |
|---|---|---|---|
| `lead_handoffs` (SQLite) | 各 worker 本地 | 真人后台**实际用这个**（已有 UI） | ✅ 已部署 |
| `customer_handoffs` (PG) | 主电脑中央 | 跨 worker 数据汇总 / 漏斗分析 | 🚧 PR #88 待部署 |

**双写策略**（worker 触发 handoff 时）：

```
触发条件满足 (referral_gate 决策 hard_allow)
       ↓
1. lead_mesh.handoff.create_handoff(...)        ← 写 lead_handoffs
2. customer_sync_bridge.sync_handoff_to_line(...) ← 写 customer_handoffs (fire_and_forget)
       ↓
真人后台从 lead_handoffs 读队列（已有 UI）
真人接管 → 同时更新两边
       ↓
跨 worker 漏斗看板从 customer_handoffs 读（待 L3 dashboard）
```

**为什么不合并**：
- lead_handoffs 已有完整 UI、脱敏、journey、webhook，重写代价大
- customer_handoffs 是中央汇总价值（200 设备规模时跨 worker 看板必须中央化）
- 双写代价小（fire_and_forget，failure 不影响主流程）

**未来 v2**：lead_mesh 改成读 customer_handoffs，UI 不变，逐步切到单源。

---

## 三、关键词清单（客户主动要 LINE）

### 文件路径

新建 `config/referral_trigger_keywords.yaml`（与现有 `referral_reply_keywords.yaml` 区分语义）：

| 文件 | 语义 | 用途 |
|---|---|---|
| `referral_reply_keywords.yaml`（已有） | 客户**回应**引流话术（"加你了" / "ID 是 xxx"） | Phase 20 跟踪客户是否真去加 LINE |
| `referral_trigger_keywords.yaml`（新） | 客户**主动**索要 LINE 联系方式 | 新触发器命中即引（hard_allow） |

### 内容（基于日本 37-60 女性情感陪护场景）

```yaml
# 客户主动要 LINE 的关键词触发字典
# 命中即 hard_allow（无视 7 轮限制）
# 业务: 日本情感陪护, 主市场 ja, 兜底 en

ja:
  # 直接索要联系方式
  - LINE教えて      # "教我 LINE"
  - LINEを教え      # 同上
  - ライン教え       # 同上
  - LINE交換       # "换 LINE"
  - ラインこうかん   # 同上
  - ID教え         # "告诉我 ID"
  - LINEのID       # "LINE 的 ID"
  - 連絡先教え      # "告诉我联系方式"
  - 連絡先交換      # "交换联系方式"
  # 表达想私聊
  - もっと話したい   # "想多聊聊"
  - 個別で話        # "私下聊"
  - プライベートで   # "私下"
  - 二人で話        # "两个人聊"
  - 直接連絡        # "直接联系"
  # 加好友/朋友
  - 友達になり      # "想成为朋友"
  - 友だち追加      # "加好友"
  - 仲良く          # "亲近一些"

en:  # 兜底, 极少日本客户用英文但偶尔遇到
  - line id
  - your line
  - line account
  - can we chat privately
  - add me on line
  - private message

# 反向词典: 命中即 NOT 引流 (客户拒绝)
rejection_ja:
  - LINEはやらない   # "不用 LINE"
  - LINE使わない    # 同上
  - 教えたくない    # "不想告诉"
  - 結構です        # "不用了"
  - いりません      # "不需要"
  - やめて          # "停止"
  - しつこい        # "烦人"
```

### 命中规则

1. 入站消息 → 全文小写 + 简易日文规范化（半全角统一）
2. 命中 `ja` 或 `en` 任一关键词 → `trigger_signal=true`
3. 命中 `rejection_ja` → `trigger_signal=rejected`，记录到客户 ai_profile，**触发 7 天冷却**

---

## 四、Persona 分组引流策略

### 现有 persona 复用 + 扩展

项目已有 `config/fb_target_personas.yaml` 里的 **jp_female_midlife**（37-60 日本女性）就是我们的核心目标。其它 persona（italy_lifestyle / brazil_beauty 等）保持现有引流逻辑不变。

### 新建分组策略文件

新建 `config/referral_strategies.yaml`：

```yaml
# 按 persona 区分引流策略
# 默认值会被具体 persona 覆盖

default:
  min_turns: 7                # 至少聊够 7 轮才允许引流
  rejection_cooldown_days: 7  # 客户拒绝后冷却天数
  max_referrals_per_peer: 3   # 同一客户最多发 3 次引流话术
  rejection_max_count: 1      # 拒绝 1 次就触发冷却
  channel_priority: [line, whatsapp, telegram]
  trigger_keywords_lang: en

jp_female_midlife:
  # 日本 37-60 女性情感陪护 — 主市场
  min_turns: 7                # 业务定 7 轮
  rejection_cooldown_days: 7  # 业务定 7 天
  max_referrals_per_peer: 3
  rejection_max_count: 1
  channel_priority: [line]    # 只引 LINE, 不引 WA/Telegram
  trigger_keywords_lang: ja
  # 引流话术风格: 温柔陪护型, 不商业
  referral_tone: companion    # vs business
  # 触发条件强化 (相比 default)
  require_emotion_score: 0.5  # L3 情感分超 0.5 才允许 (聊得有温度)
  # 触发时机
  prefer_triggered_by: [keyword, emotion]  # 不靠 buying intent

# 其它 persona (italy_lifestyle / brazil_beauty / arabic_business / global_hustle / india_tech) 走 default
```

### 引流话术按 persona 分组

新建 `config/referral_snippets.yaml`：

```yaml
default:
  channel_line:
    - "Let's continue chatting on LINE: <ID>"

jp_female_midlife:
  channel_line:
    # 温柔陪护型, 多种变体随机挑
    - "もしよければ、LINEでもお話しませんか？私のID: <ID>"
    - "LINEの方がお話しやすいかも。私のIDです: <ID>"
    - "もっとゆっくりお話したくて、LINEを交換できたら嬉しいです。<ID>"
    - "ここだとちょっと不便で、LINEに移りませんか？ID: <ID>"
```

---

## 五、触发器流程（接到现有 referral_gate 上）

### 现状 (referral_gate.py)

3 层决策：
- `hard_block`: 无 contact / cooldown 未过 → refer=False
- `hard_allow`: intent=referral_ask / buying → refer=True (覆盖 cooldown)
- `soft_score`: 综合分 ≥ threshold → refer=True

### 改造后 (referral_gate.py 扩展，不重写)

```
入站消息进来
       ↓
[Layer 1: 关键词命中]
   ↓ 命中 trigger_keywords 任一 → 直接 hard_allow
   ↓ 命中 rejection_keywords → 设 7 天冷却 + hard_block
       ↓
[Layer 2: 持久 hard_block 检查]
   ↓ peer 在冷却中 (rejection_cooldown_days) → block
   ↓ 该 peer max_referrals_per_peer 已达 → block
   ↓ total_turns < min_turns (7) → block
       ↓
[Layer 3: 本地 LLM 情感评分] (新建 chat_emotion_scorer.py)
   ↓ 输入: 最近 5 条聊天 + persona
   ↓ 输出: trust / interest / frustration / topic_match (0-1 各 score)
   ↓ 综合分 = trust × 0.4 + interest × 0.3 + (1-frustration) × 0.2 + topic_match × 0.1
   ↓ 综合分 < persona.require_emotion_score (jp 默认 0.5) → block
       ↓
[Layer 4: 最终 hard_allow]
   ↓ 调 line_pool.allocate() 拿 LINE ID
   ↓ 用 referral_snippets[persona].channel_line 随机挑话术
   ↓ 发出引流消息
   ↓ 同时双写 lead_handoffs + customer_handoffs (fire_and_forget)
```

### 关键参数表（jp_female_midlife）

| 参数 | 值 | 含义 |
|---|---|---|
| min_turns | 7 | 至少 7 轮才允许引流（victor 拍板） |
| rejection_cooldown_days | 7 | 拒绝后冷却 7 天（victor 拍板） |
| rejection_max_count | 1 | 拒绝 1 次即冷却 |
| max_referrals_per_peer | 3 | 同客户最多发 3 次引流话术 |
| require_emotion_score | 0.5 | L3 情感分超 0.5 才引（聊得有温度） |
| channel_priority | [line] | 只引 LINE |

---

## 六、L3 情感评分（新建）

新建 `src/ai/chat_emotion_scorer.py`：

### Prompt 模板

```
你是一个情感分析专家。给定客户最近 5 条聊天，评估这 4 个维度（每项 0-1 分）：

trust（信任度）：客户主动分享个人信息？回复速度快？情感表达开放？
interest（兴趣度）：会问问题？回复长度？主动延展话题？
frustration（不耐烦）：负面情绪？敷衍？句子越来越短？
topic_match（话题匹配度）：是否在情感陪护话题（家庭/感情/兴趣/生活），还是抱怨工作/产品询价等无关话题

输出严格 JSON: {"trust": 0.0-1.0, "interest": 0.0-1.0, "frustration": 0.0-1.0, "topic_match": 0.0-1.0, "rationale": "<一句话>"}

聊天历史:
<最近 5 条 incoming + outgoing>

persona: jp_female_midlife (日本 37-60 女性, 情感陪护场景)
```

### 调用频率（victor 拍板：每条入站消息都调）

每次 messenger inbound → 调 L3 评分 → 存到客户 ai_profile.emotion_scores

成本估算（200 设备 × 平均 50 条/天/设备 入站 = 10000 次/天）：
- 用本地 ollama qwen2.5（已部署）→ **零边际成本**，只占算力
- 主电脑 GPU 算力够：qwen2.5:latest 推理 ~500ms/次，每秒 2 次，每天 86400 次理论上限
- 实际 10000 次/天 远低于上限 ✅

### 缓存策略

每条入站都调 → 但相同上下文不重复调：
- 缓存 key = hash(最近 5 条聊天)
- TTL 10 分钟（足够覆盖单次会话）
- LRU 1000 条

---

## 七、真人后台扩展（不重写，扩 lead_mesh）

### 现有 lead-mesh-ui.js 已有的页面

- `lmOpenHandoffInbox(receiverKey)` → 接收方工作台 SPA
- 显示 pending/acknowledged/completed/rejected 4 tabs
- 30 秒自动刷新
- 点 handoff 卡片展开 conversation_snapshot（已脱敏）

### 需要扩展的（最小改动）

**1. lead_handoffs 表加 3 列**（database.py:490-509 已有 lead_handoffs，加列即可）：
```sql
ALTER TABLE lead_handoffs ADD COLUMN assigned_to_username TEXT;        -- 当前接管的 username
ALTER TABLE lead_handoffs ADD COLUMN customer_service_replies_json TEXT DEFAULT '[]';
ALTER TABLE lead_handoffs ADD COLUMN internal_notes_json TEXT DEFAULT '[]';
```

**2. lead_mesh.py router 加 4 个端点**：
```
POST /lead-mesh/handoffs/{id}/assign     # 真人按"我接手"
POST /lead-mesh/handoffs/{id}/reply      # 真人发消息（→ agent_mesh → worker → 物理手机）
POST /lead-mesh/handoffs/{id}/note       # 真人加内部备注
POST /lead-mesh/handoffs/{id}/outcome    # 真人标记结果（成交/流失/待跟进）
```

**3. lead-mesh-ui.js 加 UI**（在现有工作台基础上）：
- 客户卡片右侧加"我接手"按钮
- 接手后展开"回复输入框" + "内部备注"
- 底部加"标记结果"下拉（成交/流失/待跟进 + 备注）

**4. users.json 加客服账号**（运营操作，不在 PR 范围）：
```json
{"username": "agent_001", "password": "...", "role": "customer_service", "display": "客服 001"}
```

### 移动端

`src/host/static/css/dashboard.css` 已有 `@media 480px / 768px / 900px` 完全 responsive，**不用改**。

---

## 八、PR 拆分（按这个文档实施）

**当前 PR-3** (失败重发) — 与本文档无关，先做完

**PR-4** (引流触发器扩展) — 实施本文档
- referral_trigger_keywords.yaml
- referral_strategies.yaml
- referral_snippets.yaml
- 改造 referral_gate.py（不重写，加 keyword + cooldown + persona 分组）
- 接入 _ai_reply_and_send 路径

**PR-5** (L3 情感评分)
- chat_emotion_scorer.py
- 接到 referral_gate 的 soft_score 路径

**PR-6** (真人后台扩展)
- lead_handoffs 加 3 列
- 4 个新 router 端点
- lead-mesh-ui.js 扩展 UI

**PR-7** (运营动作 — victor 操作)
- users.json 加客服账号
- 文档：客服培训 / SLA 标准

---

## 九、关键风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| L3 LLM 返回不是合法 JSON | 评分失败 | try/except + 失败 fallback 为中性分（0.5） |
| ollama qwen2.5 离线 | 评分全失败 | LLM Router (PR #87) 自动 fallback 到 deepseek 远程 API |
| 双 handoff 数据漂移 | lead_handoffs vs customer_handoffs 不一致 | fire_and_forget 失败队列兜底（drain），数据校对 job 周对账 |
| 真人接管后机器人还在自动回复 | 客户体验混乱 | 接管事件触发 worker 把该客户加入 "ai_paused" 名单 |
| 客户被多次引流 | 客户烦 | max_referrals_per_peer=3 + rejection_cooldown_days=7 双重保护 |
| persona 误判（非日本人误归 jp_female_midlife） | 引流策略错配 | 现有 fb_target_personas L1 规则 + L2 VLM 双重判定 |

---

## 十、跑通后的验收标准

部署后可观测的指标（暴露到 /cluster/stats）：

```
referral_trigger_total                  -- 触发器命中次数
referral_trigger_by_layer{layer=keyword|cooldown|turns|emotion}
referral_referral_sent_total            -- 真发出引流话术
referral_rejection_total                -- 客户拒绝次数
referral_handoff_dual_write_total       -- 双写成功数
referral_handoff_dual_write_drift       -- 双写不一致数

emotion_scorer_calls_total
emotion_scorer_cache_hits_total
emotion_scorer_avg_latency_ms

handoff_pending_count                   -- 待接管队列长度
handoff_assigned_count                  -- 已接管在跟进
handoff_avg_assign_latency_seconds      -- 平均接管延迟
handoff_outcome_total{outcome=converted|lost|待跟进}
```

转化率 = `outcome_converted / handoff_total` 是终极业务 KPI。
