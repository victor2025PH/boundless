# 双机协同对接契约

> 两台电脑，两个 Claude，一个 monorepo。
> **本文件是唯一的真实边界定义，任何跨边界修改必须先 PR 改本文件再 PR 改代码。**

## 零、边界声明 + 非本 repo 范围（防混淆）

> **⚠ 2026-04-25 拓扑澄清更新**: 本 repo (`mobile-auto0423`) 全 A 写, B 不在本 repo 写代码 (B 在 `telegram-mtproto-ai` 自己的 repo 工作). 后续章节里出现的 "机器 B" / "B 独占" / "B 写" / "A+B 共用" / "B 不得修改" 等表述都是历史措辞 — **实际全 A 写**, 模块内分工 (greeting / inbox / lead_mesh / referral / 等) 仍有效但都是 A 内部模块边界, 不是跨 owner 边界.
>
> 跨 repo 协同 (4 接触面: `chat_messages.yaml` 文案口径 / event aggregate / 真机设备池 / Coordinator service) 详见 [`CROSS_REPO_TOPOLOGY.md`](CROSS_REPO_TOPOLOGY.md). 跨 repo 设备锁 + 注册中心 spec 见 [`COORDINATOR_SPEC.md`](COORDINATOR_SPEC.md).

本 repo = **Facebook / Messenger 移动端自动化 bot**（greeting + messenger chat 全 A 模块）。下列内容**不属于本 repo**，去对应 repo 提需求：

| 内容 | 实际归属 |
|---|---|
| contacts / handoff 跨平台 Contact / HandoffToken 子系统 | `github.com/victor2025PH/telegram-mtproto-ai` |
| Telegram / LINE RPA runner | `telegram-mtproto-ai` |
| **Android Messenger RPA runner**（adb + UIAutomator 驱动手机 Messenger App） | `telegram-mtproto-ai/src/integrations/messenger_rpa/` |
| skill_manager / KB / trigger / 回复生成的主体 | `telegram-mtproto-ai` |

**"Messenger"歧义说明**：
- **本 repo 的 Messenger** = 通过 **FB App / Messenger App 的 UI 自动化**（`facebook.py::send_message` / `check_messenger_inbox` 等），走 mobile-auto0423 自己的 VLM Level 4 fallback 栈。
- **telegram-mtproto-ai 的 Messenger** = 一个**完全独立的** Android RPA runner，走 adb + UIAutomator + combined_vision。

两套实现**代码不共享、运行时互相独立**。只是通过 `contacts` 子系统在业务语义层衔接（Messenger→LINE 引流主线）。

---

## 一、协同模型

```
┌─────────────────────────────┐            ┌─────────────────────────────┐
│ 机器 A (victor2025PH)       │            │ 机器 B (另一台 Claude)       │
│ 负责: 加好友 + 打招呼       │  同 repo  │ 负责: Messenger 自动回复    │
│ 分支: main / feat-a-*        │  ←────→   │ 分支: feat-b-chat           │
└─────────────────────────────┘            └─────────────────────────────┘
            ↓                                       ↓
            └───── 共享 SQLite openclaw.db ────────┘
            └───── 共享 config/ 配置文件 ──────────┘
            └───── 共享 src/app_automation/ 核心 ──┘
            └───── 共享 docs/INTEGRATION_CONTRACT.md ┘
```

**两个项目在部署层独立运行**（各自启 server 就跑自己的 task_type），
**在代码层共享**（automation 层、数据库层、配置层、文案库、playbook）。

## 二、职责分区（硬边界）

### 机器 A 负责（greeting bot）

**业务关键词**：搜索名字 / 加好友 / 打招呼 / 群成员打招呼 / 点名添加

**独占写**（B 不得修改）：
- `src/app_automation/facebook.py` 中以下方法：
  - `add_friend` / `add_friend_with_note` / `add_friend_and_greet`
  - `send_greeting_after_add_friend`
  - `_tap_profile_message_button` / `_confirm_message_request_if_any` / `_set_greet_reason`
  - `extract_group_members` / `group_engage_session` / `search_people` / `search_and_collect_leads`
  - `browse_feed` / `browse_feed_by_interest`
  - `view_profile` / `read_profile_about`
- `src/host/fb_add_friend_gate.py`（全文件）
- `src/host/fb_account_phase.py`（全文件）
- `src/host/fb_playbook.py` 中 `send_greeting` / `add_friend` / `extract_members` / `group_engage` 段
- `config/facebook_playbook.yaml` 中 `send_greeting:` / `add_friend:` / `extract_members:` / `group_engage:` 段
- `src/host/routers/facebook.py` 中 `friend_growth` / `group_hunter` / `name_hunter` / `warmup` / `full_funnel` 预设
- `src/host/static/js/facebook-ops.js` 中 `fbOpenNameHunterInput` 相关代码
- task_type 命名空间：
  - `facebook_add_friend`
  - `facebook_add_friend_and_greet`
  - `facebook_send_greeting`
  - `facebook_extract_members`
  - `facebook_browse_groups`
  - `facebook_group_engage`
  - `facebook_browse_feed`
  - `facebook_browse_feed_by_interest`
  - `facebook_search_leads`
  - `facebook_join_group`
  - `facebook_profile_hunt`

**数据库写入**：
- `facebook_friend_requests` 表 —— A 独占写
- `facebook_groups` 表 —— A 独占写
- `facebook_inbox_messages` 表 —— 写 `direction='outgoing' + ai_decision='greeting'` 专属
  - `peer_type='friend_request'` 表示对方尚未接受好友请求（消息请求文件夹）
  - `template_id` 格式 `<src>:<cc_or_lang>:<idx>`（如 `yaml:jp:3`），带 `|fallback` 后缀表示走 A1 降级路径

### 机器 B 负责（messenger chat bot）

**业务关键词**：Messenger 收件箱 / 自动回复 / 消息请求 / 引流话术切换 / 好友请求审核

**独占写**（A 不得修改）：
- `src/app_automation/facebook.py` 中以下方法：
  - `check_messenger_inbox` / `check_message_requests` / `check_friend_requests_inbox`
  - `_ai_reply_and_send` / `_extract_latest_incoming_message`
  - `_open_message_requests_fallback` / `_list_friend_requests`
  - `send_message`（Messenger 发消息主路径 —— A 的 A2 降级会 *只读调用* 这个方法，但不改其实现）
- `src/ai/auto_reply.py`（如有，全文件）
- `src/ai/chat_brain.py`（如有，全文件）
- `src/host/fb_playbook.py` 中 `check_inbox` 段
- `config/facebook_playbook.yaml` 中 `check_inbox:` 段
- `src/host/routers/facebook.py` 中 `inbox_pro` 预设
- task_type 命名空间：
  - `facebook_check_inbox`
  - `facebook_check_message_requests`
  - `facebook_check_friend_requests`
  - `facebook_send_message`
  - 任何新建的 `facebook_*_reply` / `facebook_*_chat` task_type

**数据库写入**：
- `facebook_inbox_messages` 表 —— 写 `direction='incoming'` 所有行
- `facebook_inbox_messages` 表 —— 写 `direction='outgoing' + ai_decision IN ('reply', 'wa_referral')`
- 允许回写 A 写入的 greeting 行的 `replied_at` 字段（当对方回复了 greeting 时）

### 共享区（双方只读 + 协商修改）

双方都能读，但修改必须先在 PR 里 @对方 review：

- `src/host/fb_store.py` — 数据层函数（`record_friend_request` / `record_inbox_message` / `count_*` / `get_funnel_metrics`）
- `src/host/database.py` — schema 迁移（两边 ALTER TABLE 必须合并到同一个 migration 列表，不能互相覆盖）
- `src/app_automation/fb_content_assets.py` — 文案加载器（`get_verification_note` / `get_greeting_message` / `get_comment_pool` / `get_referral_snippet`）
- `config/chat_messages.yaml` — 文案模板池（多语种开场白、评论、引流话术）
- `config/fb_target_personas.yaml` — 客群定义
- `src/host/executor.py` — task_type 分发器（各自加自己的 case 分支，不碰对方的）
- `src/host/routers/tasks.py` — POST /tasks 的 gate 检查（各自加自己的 gate 调用）
- `src/host/schemas.py` — task_type 枚举
- `src/host/task_labels_zh.py` — 中文标签

## 三、数据库表 —— 字段语义权威定义

### `facebook_inbox_messages`

| 字段 | 写入方 | 语义 |
|------|--------|------|
| `device_id` | 双方 | 设备 ID |
| `peer_name` | 双方 | 对方姓名 |
| `peer_type` | 双方 | `friend`（已是好友） / `friend_request`（刚发好友请求未接受） / `stranger`（message request 里的陌生人） / `group` |
| `direction` | 双方 | `incoming` / `outgoing` |
| `message_text` | 双方 | 消息正文 |
| `ai_decision` | 双方 | `greeting`（A 写：加完好友打招呼） / `reply`（B 写：自动回复） / `wa_referral`（B 写：引流到 WhatsApp/LINE） / `''`（incoming 默认） |
| `ai_reply_text` | 双方 | 出站方向时与 message_text 相同 |
| `language_detected` | B | B 做语言检测时填 |
| `seen_at` | 双方 | 创建时间（incoming = 看到时间；outgoing 也会填但语义弱） |
| `sent_at` | 双方 | **outgoing 专用发送时间** —— `count_outgoing_messages_since` 优先读这个字段 |
| `replied_at` | B | 当 B 回复了一条 incoming 时，同时把被回复的 incoming 行的 `replied_at` 设为回复时间；**也可以回写 A 写入的 greeting 行，标记"对方回复了我们的打招呼"** |
| `template_id` | 双方 | A 写打招呼模板 ID；B 写 reply 模板 ID（如有） |
| `lead_id` | B | 当识别到与 leads.db 的 lead 对应时填 |
| `preset_key` | 双方 | 当前运行的预设 key |

### `facebook_friend_requests`

A 独占写。B 只读。

### `facebook_groups`

A 独占写。B 只读。

### `fb_risk_events`

双方都写（检测到风控时上报）。

## 四、task_type 命名空间

双方在 `src/host/schemas.py::TaskType` 枚举里**追加**自己的 task_type，不修改对方的。

**命名约定**：
- A 的任务：`facebook_<动作>` 不包含 `inbox` / `message` / `reply` / `chat` 关键词
- B 的任务：`facebook_<动作>` 包含 `inbox` / `message` / `reply` / `chat` / `followup` 关键词

**争议处理**：若无法确定归属，写在本文件 `§六 遗留问题` 先挂起，下次碰头再决。

## 五、playbook 配置

`config/facebook_playbook.yaml` 顶层 `defaults:` / `phases:` 结构是**硬约定**。双方在对应的段内增加字段不冲突：

```yaml
defaults:
  add_friend:       # A 维护
  send_greeting:    # A 维护
  extract_members:  # A 维护
  group_engage:     # A 维护
  browse_feed:      # A 维护
  check_inbox:      # B 维护  ← 对方加字段只改这段
  risk:             # 共享 — 改动需 PR review

phases:
  cold_start: {…}   # 双方都要在各自段里设置该 phase 的保守值
  growth: {…}
  mature: {…}
  cooldown: {…}     # 冷却期双方都必须归零
```

`src/host/fb_playbook.py::_PHASE_AWARE_SECTIONS` 也是共享，增加段时双方协商。

## 六、Git workflow

```
main                 ← 稳定分支，双方都能 merge（review 后）
 ├── feat-a-*        ← A 的 feature branch
 └── feat-b-*        ← B 的 feature branch
```

**铁律**：
1. **不直接 push main**，永远走 PR
2. 每个 PR 必须跑完 `pytest tests/ -x` 后再 merge（GitHub Actions 如果接好自动跑）
3. 涉及 "共享区" 文件的 PR 必须 @对方 review
4. 合并策略用 **squash merge** 保持 main 线性
5. 两方 baseline 分歧时，以 main 为准，各自 rebase 自己的分支

**日常循环**：
```bash
git fetch origin
git checkout main && git pull
git checkout -b feat-a-optimize-gate
# ... 改代码、pytest ...
git push -u origin feat-a-optimize-gate
# GitHub 网页上开 PR → review → merge
```

## 七、Phase 3 新增接口（A 已实施，待 B 对接）

### 7.1 统一接触事件表 `fb_contact_events`

**双方协同的核心表**。A 已在 schema 迁移里建好,自动写入 `add_friend_*` / `greeting_*` 事件。

**B 需要做的回写**:

| 场景 | 写入 event_type | 用 template_id | meta_json |
|------|----------------|---------------|-----------|
| 对方接受好友请求 | `add_friend_accepted` | 留空或传原值 | `{"accepted_at": iso}` |
| 对方拒绝 | `add_friend_rejected` | 留空 | `{"rejected_at": iso}` |
| **对方回复了我们的 greeting** | `greeting_replied` | **必须传原 greeting outgoing 行的 template_id** | `{"reply_ms_after": int, "reply_text": str[:50]}` |
| 对方主动发 DM | `message_received` | 留空 | `{"decision": "reply"\|"wa_referral"\|"skip"}` |
| 引流话术发出 | `wa_referral_sent` | 可选 | `{"channel": "line"\|"whatsapp"\|...}` |

**最关键的是 `greeting_replied`** —— 它让 A 的 `/facebook/greeting-reply-rate` 能按模板算真实 A/B
回复率,否则 reply_rate 永远是 0。

B 的实现建议(在 `check_messenger_inbox` / `_ai_reply_and_send` 内):
```python
# 检测到对方回复后,查该 peer 最近 7 天的 greeting outgoing 行
from src.host.fb_store import record_contact_event, CONTACT_EVT_GREETING_REPLIED
from src.host.database import _connect
with _connect() as conn:
    row = conn.execute(
        "SELECT template_id, sent_at FROM facebook_inbox_messages"
        " WHERE device_id=? AND peer_name=? AND direction='outgoing'"
        " AND ai_decision='greeting' AND sent_at > datetime('now', '-7 days')"
        " ORDER BY sent_at DESC LIMIT 1",
        (device_id, peer_name)).fetchone()
if row:
    tpl_id, sent_at = row
    # 回写事件
    record_contact_event(
        device_id, peer_name, CONTACT_EVT_GREETING_REPLIED,
        template_id=(tpl_id or "").split("|")[0],  # 去掉 |fallback 后缀
        meta={"reply_to_sent_at": sent_at, "reply_text": msg[:50]})
```

### 7.2 Gate 注册表

B 不再需要改 `routers/tasks.py` 加 gate 的 if-elif。
在自己的 gate 模块末尾调 `register_task_gate()` 即可:

```python
# src/host/your_gate_module.py (B 新建)
def check_my_task_gate(device_id, params): ...

# 模块末尾自动注册:
from src.host.gate_registry import register_task_gate, register_campaign_step_gate
register_task_gate("facebook_check_inbox", check_my_task_gate)
register_campaign_step_gate("check_inbox", check_my_task_gate)
```

### 7.3 并发 Lock（B 写入事件表也建议用）

`fb_contact_events` 表的 INSERT 是原子的不需要锁。
但 B 如果要做"同 peer 24h 内只接触 N 次"的配额 gate(需要 read + write 两步), 应该用:

```python
from src.host.fb_concurrency import device_section_lock

with device_section_lock(device_id, "check_inbox", timeout=120.0):
    # 查 count + 决定是否跳过 + 写入事件
    ...
```

section name 自选,但不要用 A 已占用的 `add_friend` / `send_greeting`。

## 七点五、多渠道引流接口（A 机 Phase 4 新建,2026-04-23）

### 7.5.1 `src/app_automation/referral_channels.py`

A 机建立了统一的多渠道引流抽象层。**B 的 `_ai_reply_and_send` 可选迁移**到
新接口,零破坏现有流程(旧的 `parse_referral_channels` / `pick_referral_for_persona`
仍然保留)。

**核心概念**:
* `ReferralChannel` — 每个渠道一个子类
  - `LineChannel` / `WhatsAppChannel` / `TelegramChannel` / `MessengerChannel` / `InstagramChannel`
  - 每类自带 `validate_account` / `build_deep_link` / `detect_intent` / `format_snippet` / `mask`
* `REFERRAL_REGISTRY` — 中央注册表;第三方渠道(如 KakaoTalk) 运行时注册
* `pick_channel_smart()` — 三段式智能选渠道:
  1. **意图感知**: 对方消息里问"LINE 有吗?" → 直接回 LINE(不管 persona 优先级)
  2. **persona 优先级**: 无意图时按 `referral_priority` 排序
  3. **兜底**: 任意可用渠道

**B 迁移建议**(可选, 不迁移不破坏):

```python
# 旧代码 (fb_store 的 _ai_reply_and_send 里, B 的 P0 分支保留即可):
from src.host.fb_referral_contact import (
    parse_referral_channels, pick_referral_for_persona)
_rch_map = parse_referral_channels(referral_contact)
_r_val, _r_channel = pick_referral_for_persona(_rch_map, persona_key)
# 仍然工作, 但不做意图识别

# 新推荐 (B 未来迁移):
from src.app_automation.referral_channels import pick_channel_smart
from src.host.fb_referral_contact import parse_referral_channels
channel_obj, value = pick_channel_smart(
    incoming_text=incoming_text,
    persona_key=persona_key,
    available_accounts=parse_referral_channels(referral_contact),
)
if channel_obj and value:
    snippet = channel_obj.format_snippet(value, persona_key=persona_key, name=peer_name)
    # deep link 自动按渠道策略拼接(WA/TG 有,LINE 故意无)
```

### 7.5.2 LINE 特化

**重要**: LINE 渠道刻意不发 `line.me/...` deep link。
- FB 风控对 line.me 屏蔽严, 多次触发会把账号判为 spam
- 改用**纯文本 @ID**: `LINE: @myid` + 提示对方搜索
- 如果运营要用 QR 落地页, 直接在账号 value 里填 `https://xxx.example.com/mypage.png`,
  `validate_account` 会识别 URL 形式并原样保留

### 7.5.3 数据层扩展

`fb_contact_events` 事件枚举**保持不变**(无破坏性迁移), 新增 `meta_json` 字段约定:

| event_type | meta 字段 |
|-----------|-----------|
| `wa_referral_sent` (legacy,保留) | `{"channel": "whatsapp", "account_masked": "+8*******"}` |
| `wa_referral_sent` | B 也可以用这个 event_type 记其他渠道(通过 meta.channel 区分) |

**新建议**: B 用 `ReferralChannel.event_meta(value)` 直接返回 meta dict:

```python
from src.app_automation.referral_channels import pick_channel_smart
from src.host.fb_store import record_contact_event, CONTACT_EVT_WA_REFERRAL_SENT

channel_obj, value = pick_channel_smart(...)
if channel_obj:
    # 发消息...
    record_contact_event(
        device_id, peer_name, CONTACT_EVT_WA_REFERRAL_SENT,
        meta=channel_obj.event_meta(value),  # 自动含 channel + masked
    )
```

这样 A 的 `/facebook/contact-events?event_type=wa_referral_sent` 能按
`meta.channel` 聚合出 LINE / WA / TG 各自的转化数据。

### 7.5.4 新渠道扩展流程

双方任意一方想加新渠道(如 KakaoTalk / Viber / WeChat):

1. 在 `src/app_automation/referral_channels.py` 新建 `KakaoChannel(ReferralChannel)` 子类
2. 在 `REFERRAL_REGISTRY["kakao"] = KakaoChannel()` 注册
3. 在 `config/fb_target_personas.yaml` 的某 persona 的 `referral_priority` 里可选加 `kakao`
4. 在 `config/chat_messages.yaml` 的 `countries[cc].referral_kakao` 加本地化文案
5. 不改 B 的 `_ai_reply_and_send` 也不改 A 的 greeting 代码 —— 注册表自动生效

**决策权**: 添加新渠道**A 主导** (因为属于共享基础设施),
B 给出"需要这个渠道"的需求在遗留问题里列, A 实现后 B 直接 import 用。

### 7.5.5 测试

覆盖 47 个 test case: `tests/test_referral_channels.py`。

---

## 七点六、MessengerError 分流契约（2026-04-23 B PR #1 + A review 确认）

**source**: `src/app_automation/facebook.py::MessengerError` (7 档 code)

A 机的 Messenger 降级路径 (`send_greeting_after_add_friend` 的 A2 fallback)
catch 下列 code 后按此分流:

| code | A 机分流动作 | 归因副作用 |
|------|-------------|------------|
| `risk_detected` | `fb_account_phase.set_phase(did, "cooldown")` — **设备级** | journey `risk_detected` + 30 min 硬停 |
| `xspace_blocked` | 只 log warning, retry 1 次; 仍失败 → 降级 FB 主 app 个人页 DM | **不** cooldown (系统弹窗, 不是 FB 风控) |
| `recipient_not_found` | 重试 2 次 × 间隔 5-15s; 仍失败 → 跳该 peer | journey `referral_blocked{reason=peer_not_in_messenger}` |
| `search_ui_missing` | 等 8s retry 1 次; 仍失败 → 降级 FB 主 app | 可能是 cold start |
| `send_button_missing` | 记 `fb_risk_events{kind=content_blocked, text_hash=...}` → 降级主 app | 文案可能违禁 |
| `send_blocked_by_content` *(建议 B 后续加一档)* | 同上 + 用更短版本重试一次 | FB 主动拒发 |
| `messenger_unavailable` | 跳该 peer + **device-level 标记** `messenger_not_ready` (临时过期 30 min) | 调度器应避开 |
| `send_fail` | cooldown 3 min + retry 1 次 | 保底 |

**不改码契约**: 新增 code 要双方同意, 加到本表。删 code 不允许。

---

## 七点七、共享 `device_section_lock` section 命名约定（2026-04-23）

`src/host/fb_concurrency.device_section_lock(device_id, section)` 的
section 字符串是**全局键名空间**, 双方共用:

| section | 持有者 | 含义 |
|---------|--------|------|
| `add_friend` | A 独占 | 加好友 UI 操作 |
| `send_greeting` | A 独占 | profile 页 Message 内嵌对话 |
| `messenger_active` | **A+B 共用** | Messenger App 前台占用 (避免 A 的 fallback 和 B 的 check_message_requests 抢输入框) |
| `chatting_<canonical_id>` | 未来扩展 | 跨 agent 持有同一 lead 的对话权 |

**铁律**: B 的 `check_message_requests` / `check_messenger_inbox` 进入 Messenger
App 操作时必须先拿 `messenger_active` 锁; A 的 `send_message` fallback 路径同样。

---

## 七点七之二、真机设备独占声明（2026-04-25 B 在 HANDOFF doc 建议）

- `mobile-auto0423` 独占 historical Redmi 集群的全部 adb serial (实际 2026-04-25 在线 2 台 device + 2 台 unauthorized 待授权)
- `telegram-mtproto-ai` 独占其 `config/config.yaml::messenger_rpa.accounts.*` 里声明的 serial (历史 `bg_phone_{1,2}`, victor 已搬本机 USB)
- 两方 serial 清单**禁止交集**; victor2025PH 新增设备时在其中一方注册不两注册
- **物理隔离** (A/B 各跑各的 device pool) 是 4 设备规模的首选 (见 [`COORDINATOR_SPEC.md §13`](COORDINATOR_SPEC.md))
- 当扩到云手机 / 跨 repo 借用场景时, 走 Coordinator distributed lock (`messenger_active` 等 section)

设备分配落地: `D:\workspace\coord-board\device_assignment.yaml` (sibling 重启后写, 见 `COORDINATOR_SPEC.md §13.2`)

---

## 七点七之三、跨 repo BI 去重契约（2026-04-25 B 在 HANDOFF doc §三-2 提议）

跨 repo 转化漏斗 BI aggregate 用**双键 + 比例计算**, 不要单计或单键去重:

| 事件 | 表 (owner) | 语义 |
|---|---|---|
| `wa_referral_replied` (`meta.platform="facebook"`) | A 的 `fb_contact_events` | "意向表达": peer 在 FB 回复 referral 关键词 (OK / 加 LINE / 友達追加) |
| `first_text_received` | B 的 `journey_events` | "成交触达": peer 真到 LINE/TG 主动发首条 |

**BI dashboard 跨表 aggregate 规则**:

```
转化率 = COUNT(first_text_received WHERE peer_canonical_id IN sent_referrals)
       / COUNT(wa_referral_replied)
```

- **不要**单计任一事件作转化数
- **不要**按 `peer_canonical_id` 单键去重把两个事件合并
- 跨表 join 用 `(peer_canonical_id, source_platform)` **双键**, `source_platform` 取自各表的 `meta.platform` / `platform_tag`
- 两者比例 = "回了 OK 但没真到 LINE" 的流失率, 比 raw funnel 更有业务价值

B 的 `journey_events` 已有 `first_text_received / handoff_accepted / handoff_issued`, 加 `platform_tag` 字段对齐 A 的 `meta.platform` 枚举 (`facebook` / `line` / `telegram` / `messenger_rpa`).

---

## 七点八、greeting 回复归因双写（2026-04-23 A 机 PR #6 review 补充）

为让 Phase 5 的 `/facebook/greeting-reply-rate` 能按 template_id 算 A/B 回复率,
B 在 `mark_greeting_replied_back` 成功后需要同步写 `fb_contact_events`:

```python
record_contact_event(
    device_id, peer_name, CONTACT_EVT_GREETING_REPLIED,
    template_id=greeting_row.template_id.split("|")[0],  # 去 |fallback 后缀
    preset_key=greeting_row.preset_key or "",
    meta={"via": "mark_greeting_replied_back", "window_days": 7})
```

详见 `docs/A_TO_B_REPLY_REVIEW.md` Q1。

---

## 八、遗留问题 / 待协商

> 双方遇到不确定归属的事情先写在这里，不要直接动代码。

- [ ] 接触配额: 同一 peer 24h 内被接触总次数(add+greet+referral 合计)超阈值时, A 和 B 都应该主动跳过 —— 具体阈值由谁定?(建议 3 次)
- [ ] `ai_cost_events` 表对齐: B 跑 LLM 回复的成本统计格式请和现有 TikTok / A 的 LLM 调用一致
- [ ] Phase-aware 的"过度接触"冷却: 单一 peer 被接触 3+ 次后进入私人 cooldown, 是否要落到 playbook?
- [ ] B 完成 Messenger 自动回复后, 真机 smoke 测试由谁跑? 建议 victor2025PH 协调(两台电脑各一个设备)

## 九、历史变更

- 2026-04-23 首版 — 由 A 起草（v1.1.0 + Phase 1/2 实施完成后）
- 2026-04-23 Phase 3 更新 — A 追加: fb_contact_events / gate_registry / device_section_lock /
  /facebook/contact-events / /facebook/greeting-reply-rate / Funnel greeting widget
- 2026-04-23 Phase 4+5 — A 追加: 多渠道 ReferralChannel (§7.5) + Lead Mesh (§7+) —
  Dossier / Handoff / Agent Mesh / Webhook
- 2026-04-23 B PR #1-#7 — B 追加: `mark_greeting_replied_back` + lang_detect +
  MessengerError + chat_memory + chat_intent + referral_gate + stranger auto-reply
- 2026-04-23 A review 回复 — A 新增契约: MessengerError 分流矩阵 (§7.6) +
  device_section_lock section 命名 (§7.7) + greeting 归因双写 (§7.8)
- 2026-04-25 拓扑澄清 — victor2025PH 澄清 B 不在本 repo 写代码, 历史 "双 owner 同 repo"
  叙述全错. §零 加边界声明. 跨 repo 协同 4 接触面移到 `CROSS_REPO_TOPOLOGY.md` (PR #81).
  跨 repo Coordinator service 实施 spec 见 `COORDINATOR_SPEC.md` (PR #82).
  PR #79 / #80 (基于错误前提) 已 close.
