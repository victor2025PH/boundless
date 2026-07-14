# 给另一台电脑 Claude 的启动指令

> 这份文档是给**另一台电脑（机器 B）上的 Claude**读的。
> 机器 A 的 Claude（负责加好友+打招呼）已经把 baseline 推到 GitHub。
> 你（机器 B 的 Claude）读完这份就知道自己的边界了。

---

## 第 1 步：拉代码

在你电脑的工作目录里跑：

```bash
git clone https://github.com/victor2025PH/mobile-auto0423.git
cd mobile-auto0423
```

**重要**：全局 git 身份最好先和机器 A 区分一下：

```bash
git config user.name "其他用户名"
git config user.email "其他邮箱"
```

## 第 2 步：拉依赖

```bash
pip install -r requirements.txt
# 如果要跑 e2e/playwright 测试，再装:
pip install -r requirements-e2e.txt
```

需要的运行时：
- Python 3.13+
- `data/` 和 `logs/` 会在首次运行时自动创建（已被 .gitignore 忽略）
- `apk_repo/` 不在 git 里，如果需要 TikTok 相关 APK 另外要 (问机器 A)

## 第 3 步：读懂契约 —— 最重要

**必读文件**：`docs/INTEGRATION_CONTRACT.md`

这份契约定义了你（机器 B）和机器 A 的硬边界。核心要点：

### 你负责的（独占写）

| 文件 / 模块 | 内容 |
|------------|------|
| `src/app_automation/facebook.py` 里的 **Messenger 相关方法** | `check_messenger_inbox` / `check_message_requests` / `check_friend_requests_inbox` / `_ai_reply_and_send` / `_extract_latest_incoming_message` / `send_message` 等 |
| `src/ai/auto_reply.py` / `src/ai/chat_brain.py` | 新建或扩展 LLM 回复逻辑 |
| `src/host/fb_playbook.py` 里 `check_inbox:` 段 | |
| `config/facebook_playbook.yaml` 的 `check_inbox:` 段 | |
| `src/host/routers/facebook.py` 的 `inbox_pro` 预设 | |
| 新 task_type：`facebook_check_*` / `facebook_*_reply` / `facebook_*_chat` | 在 `schemas.py::TaskType` 追加 |

### 机器 A 负责的（你不要改）

- `add_friend` / `add_friend_with_note` / `add_friend_and_greet` / `send_greeting_after_add_friend`
- `extract_group_members` / `group_engage_session` / `search_people` / `browse_feed`
- `src/host/fb_add_friend_gate.py` / `src/host/fb_account_phase.py`
- `config/facebook_playbook.yaml` 的 `add_friend:` / `send_greeting:` / `extract_members:` / `group_engage:` 段
- task_type：`facebook_add_friend*` / `facebook_send_greeting` / `facebook_extract_members` 等

### 共享区（改动要 PR @ 对方 review）

- `src/host/fb_store.py`（数据层函数）
- `src/host/database.py`（schema 迁移）
- `src/app_automation/fb_content_assets.py`（文案加载器）
- `config/chat_messages.yaml` / `config/fb_target_personas.yaml`
- `src/host/executor.py`（只在自己的 task_type case 分支内改）
- `src/host/routers/tasks.py`（只加自己的 gate 调用）

## 第 4 步：你的起点 —— 已有的骨架

机器 A 在 `src/app_automation/facebook.py` 里已经有你的 Messenger 方法的骨架和部分实现，具体：

- `check_messenger_inbox` (行 ~2280-2380) — 已实现大部分，含 `_ai_reply_and_send` 闭环
- `check_message_requests` — 骨架已建，细节可能需要完善
- `check_friend_requests_inbox` — 骨架已建
- `_ai_reply_and_send` (行 ~2638-2768) — 已含 LLM 生成 + 引流判断逻辑，**你可以直接扩展**
- `send_message` (行 ~787-818) — 走 Messenger App 路径（机器 A 的 A2 降级会 *只读调用* 这个，**不要改它的签名**）

**优先级建议**（你的下一阶段）：

### P0 — 完善 `_ai_reply_and_send`
目前已有基础：
- 读取最新 incoming 消息
- LLM 生成回复
- 判断是否要引流（`ai_decision='reply'` 或 `'wa_referral'`）
- 调 `smart_tap("Send message button")` 发送
- 调 `record_inbox_message` 入库

你要补的：
- 多语言意图识别（日语 / 意语 / 英语）—— 对接已有的 `src/ai/` LLM 客户端
- 把 `replied_at` 回写到**被回复的 incoming 行**（现在没做）
- **跨 bot 契约**：扫 `peer_type='friend_request'` 且 `ai_decision='greeting'` 且 `sent_at` 在最近 7 天内的行 → 如果该 peer_name 最近有新 incoming → 把 greeting 行的 `replied_at` 设上。这让机器 A 能算 `reply_rate_by_template`（A/B 模板效果）。

### P1 — `check_friend_requests_inbox` 自动通过
基于 `min_mutual_friends` / `lead_score` 决定自动接受。契合机器 A 的 `fb_profile_classifier` 评分。

### P2 — `send_message` 的 XSpace 兜底细化
机器 A 在 Phase 2 审查里提到 "send_message 切 Messenger 触发 XSpace 弹窗没有独立 reason 标签"，建议你在 send_message 里加：
- `MessengerError(code="xspace_blocked")` / `MessengerError(code="search_miss")`
- 机器 A 的 A2 降级路径会 catch 这些异常做细分归因

## 第 5 步：工作流程

```bash
# 每次开始新工作前
git fetch origin
git checkout main && git pull
git checkout -b feat-b-<短描述>

# 开发 + 跑测试
python -m pytest tests/ -x -q --ignore=tests/e2e -k "not real"

# 提交
git add <你改的文件>
git commit -m "your message"
git push -u origin feat-b-<短描述>

# 到 GitHub 开 PR，指定 @victor2025PH review 共享区修改
```

## 第 6 步：遇到不确定归属的事

**不要动代码**，先在 `docs/INTEGRATION_CONTRACT.md` 的 "七、遗留问题 / 待协商" 段落加一条，开 PR，等机器 A 响应。

## 第 7 步：常用调试命令

```bash
# 跑全部单元测试
python -m pytest tests/ -x -q --ignore=tests/e2e -k "not real"

# 只跑你的模块相关测试（如果有）
python -m pytest tests/test_fb_*.py -v

# 启动 server（dev 模式）
python server.py

# 看 Facebook 漏斗指标
curl http://localhost:8000/facebook/funnel
# 其中 stage_greetings_sent / stage_inbox_incoming / stage_outgoing_replies / stage_wa_referrals 是你和 A 的联合数据
```

## 第 8 步：数据库字段速查

最关键的表是 `facebook_inbox_messages`，字段语义在 `docs/INTEGRATION_CONTRACT.md §三`，这里不重复。

**你写 incoming 时的典型 payload**：
```python
from src.host.fb_store import record_inbox_message

record_inbox_message(
    device_id=did,
    peer_name="某用户",
    peer_type="friend",  # 或 "stranger" / "friend_request"
    message_text="对方发的原文",
    direction="incoming",
    language_detected="ja",    # 你做的语言检测
    preset_key=preset_key or "",
)
```

**你写 outgoing 回复时的典型 payload**：
```python
record_inbox_message(
    device_id=did,
    peer_name="某用户",
    peer_type="friend",
    message_text=reply_text,
    direction="outgoing",
    ai_decision="reply",   # 或 "wa_referral"
    ai_reply_text=reply_text,
    language_detected="ja",
    preset_key=preset_key or "",
    template_id="",        # 如果你也做模板池 A/B，按 "<src>:<key>:<idx>" 格式填
)
```

## 第 9 步：问机器 A 的方式

目前两个 Claude 之间**没有直接通信通道**。通信只能通过：
1. **Git commit message** — 在 commit 里写 `@A:` 开头的消息
2. **PR description** — 在 PR 里 @victor2025PH，他会在两台电脑之间转达
3. **`docs/INTEGRATION_CONTRACT.md` 的遗留问题段** — 作为官方看板

**不要**试图"通过文件系统找到对方的项目" —— 两台电脑的本地 checkout 互相看不见，唯一媒介是 GitHub。

---

## TL;DR

1. `git clone https://github.com/victor2025PH/mobile-auto0423.git`
2. 读 `docs/INTEGRATION_CONTRACT.md`（硬边界）
3. 自己负责 Messenger 收件箱 / 自动回复 / 引流切换 相关的一切
4. 不要改 add_friend / greeting / extract_members 相关代码
5. 改共享区必须开 PR @ 对方
6. 每次 commit 前跑 `pytest tests/ -x -q --ignore=tests/e2e -k "not real"`
