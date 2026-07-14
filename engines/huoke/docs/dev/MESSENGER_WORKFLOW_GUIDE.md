# Messenger 工作流部署指南

> 从 `git clone` 到"看到 Lead Mesh Dashboard 出数据"的 step-by-step 操作手册。
> 适用范围: B 机 Messenger 聊天机器人(含 A 机 add_friend / greeting 协作)。

## 一、前置要求

| 项 | 说明 |
|---|---|
| Python | 3.12+ |
| 仓库 | `git clone https://github.com/victor2025PH/mobile-auto0423.git` |
| main 分支状态 | **需含合并后的 B 所有 PR (#1-#11) + A 的 Phase 3/4/5/5.5/6** (否则部分功能 graceful skip) |
| ADB 设备 | 真机 smoke 需要;数据层 smoke 不需要 |
| Facebook 账号 | 真机 smoke 需要,已登录 FB + Messenger, phase=growth |
| LINE / WhatsApp ID | 引流话术目标,填到 `config/facebook_playbook.yaml` 或 task params |

## 二、快速验证 (无真机, 5 分钟)

跑数据层 smoke 确认代码契约闭环:

```bash
pip install -r requirements.txt
python scripts/messenger_workflow_smoke.py
```

期望输出(main 全合并状态):
```
[PASS] 01_setup_tmp_db
[PASS] 02_seed_leads_A      (A 模拟: 3 leads)
[PASS] 03_a_send_friend_requests
[PASS] 04_b_lookup_lead_score   (P1 fuzzy 命中)
[PASS] 05_peers_accept_friend   (含 add_friend_accepted event)
[PASS] 06_a_send_greetings
[PASS] 07_peers_reply_incoming
[PASS] 08_b_mark_greeting_replied   (P0 F1 同步 greeting_replied event)
[PASS] 09_b_chat_memory
[PASS] 10_b_intent_classify
[PASS] 11_b_referral_gate
[PASS] 12_b_wa_referral_sent
[PASS] 13_verify_funnel_metrics   (stage_wa_referrals=2 等)
[PASS] 14_verify_contact_events_total   (add_friend_accepted=3 / greeting_replied=3 / ...)
[PASS] 15_teardown

PASS: 14   SKIP: 0   FAIL: 0
```

PR 还没全合入时会有 SKIP,这是预期的(不 block 合入)。有 FAIL 则表示有分支
合入后出现了回归, 请查日志。

## 三、配置清单 (生产环境)

### 3.1 `config/facebook_playbook.yaml::check_inbox`

B 维护段。推荐值:

```yaml
defaults:
  check_inbox:
    max_conversations: 15         # 主 inbox 单次最多处理条数
    max_requests: 20              # Message Requests 单次最多条数
    auto_reply_stranger: true     # P6: 陌生人自动回复 (默认开)

phases:
  cold_start:
    check_inbox:
      max_conversations: 5        # 新号: 被动收
      max_requests: 5
      auto_reply_stranger: false  # 冷启关闭避免反垃圾
  growth:
    check_inbox:
      max_conversations: 10
      max_requests: 10
      auto_reply_stranger: true
  mature:
    check_inbox:
      max_conversations: 20
      max_requests: 20
  cooldown:
    check_inbox:
      max_conversations: 5        # 冷却期最小量
      max_requests: 3
      auto_reply_stranger: false
```

### 3.2 `config/fb_target_personas.yaml`

A/B 共用, 本项目主要 persona:
- `jp_female_midlife` (日本女性中年, lang=ja, LINE 优先)
- `it_male_midlife` (意大利男性中年, lang=it, WhatsApp 优先)
- `us_female_midlife` (美国女性中年, lang=en, WhatsApp 优先)

`target_language` 字段决定 B 的 chat_intent/chat_memory 的语种偏好。

### 3.3 `config/chat_messages.yaml`

A 维护。模板 `template_id` 格式 `<src>:<cc_or_lang>:<idx>` 是公开契约(见
`INTEGRATION_CONTRACT §三`)。B 的 `chat_memory.greeting_template_ids` 依赖
这个格式。

### 3.4 引流渠道 (`referral_contact` 参数)

通过 task params 或 playbook preset 传, 格式:
- 单渠道: `"line:abc123"` 或 `"wa:+8190..."`
- 多渠道: JSON `{"line":"abc","whatsapp":"+81..."}`,`fb_referral_contact.pick_referral_for_persona` 会按 persona 选最合适的

## 四、单设备真机 smoke 流程

### Step 1 — 启动 server

```bash
python server.py
```

默认监听 `:8000`。Dashboard: http://localhost:8000/

### Step 2 — 设备就绪

- ADB 连上一台 Android 手机
- FB 已登录, Messenger 已登录
- 在 Dashboard 添加 device_id + alias

### Step 3 — 配 phase=growth

```bash
curl -X POST http://localhost:8000/facebook/account-phase \
  -d '{"device_id":"<你的did>","phase":"growth"}'
```

### Step 4 — 触发 A 的获客 + 加好友

通过 Dashboard 创建任务, 或直接 API:

```bash
# 群成员打招呼 → 打分 → 加好友 → 打招呼, 一步到位
curl -X POST http://localhost:8000/tasks -d '{
  "device_id": "<did>",
  "task_type": "facebook_extract_members",
  "params": {
    "preset_key": "jp_growth",
    "persona_key": "jp_female_midlife",
    "max_members": 3
  }
}'
```

### Step 5 — 等对方接受 (~数小时 - 1 天)

真人接受好友请求。B 端调度器会自动跑 `check_friend_requests_inbox` 把
accepted 的转成 add_friend_accepted contact_event。

### Step 6 — A 触发打招呼

accepted 后触发:
```bash
curl -X POST http://localhost:8000/tasks -d '{
  "device_id": "<did>",
  "task_type": "facebook_send_greeting",
  "params": {"persona_key": "jp_female_midlife"}
}'
```

### Step 7 — 等对方回复

对方自然回复后, B 端的 `facebook_check_inbox` (建议 cron 每 30 分钟跑一次)
自动触发:
- `_open_and_read_conversation` 读 incoming → `greeting_replied` event
- `_ai_reply_and_send` 走 P3 记忆 + P4 意图 + P5 gate 生成回复
- 若 intent ∈ {buying, referral_ask} → `wa_referral_sent` event

### Step 8 — 看 Dashboard

- http://localhost:8000/facebook/funnel — 漏斗 6 阶段
- http://localhost:8000/facebook/greeting-reply-rate — A 的模板 A/B
- http://localhost:8000/lead-mesh/dashboard — A Phase 5 Lead 档案 + Journey

## 五、任务调度 (生产)

推荐 cron(A 的 job_scheduler 支持):
```yaml
# config/scheduler.yaml (示意)
jobs:
  - name: check_inbox_hourly
    task_type: facebook_check_inbox
    cron: "0 * * * *"
    params: {auto_reply: true, max_conversations: 10}

  - name: check_friend_requests_daily
    task_type: facebook_check_friend_requests
    cron: "30 9 * * *"
    params: {min_mutual_friends: 1, min_lead_score: 0}
    # min_lead_score=0 先禁用, 累积 2 周数据再开 (A→B Q9)

  - name: check_message_requests_2h
    task_type: facebook_check_message_requests
    cron: "0 */2 * * *"
    params: {auto_reply: true, max_requests: 10}
```

## 六、故障排查

### 问题: B 发消息后 greeting_replied 没出现在 Dashboard

检查:
1. A 的 Phase 5 (`fb_contact_events` 表) 已 merge? 未 merge 则 greeting_replied 不写
2. P0 的 F1 (`mark_greeting_replied_back` 内部同步) 已 merge?
3. template_id 格式正确? `<src>:<cc_or_lang>:<idx>`, 带 `|fallback` 会被去后缀
4. 窗口内? 默认 7 天内的 greeting 才 mark

### 问题: 陌生人 auto_reply 触发引流被 FB 标 spam

检查:
1. 陌生人场景 gate 的保守配置是否激活? P6 的 stranger cfg: `min_turns=5`,
   `score_threshold=4`, `cooldown=6h`
2. 是否首轮就引流? intent=opening 不应触发 wa_referral
3. 文案是否触发违禁词? 查 `fb_risk_events WHERE kind='content_blocked'`

### 问题: 撞 FB 风控 (登录 challenge / checkpoint)

B 的 `check_messenger_inbox` 入口拿 `device_section_lock("messenger_active")`,
撞到时 raise MessengerError(risk_detected) 被 catch → 写 `fb_risk_events` +
`stats["risk_detected"]`。

处理:
1. 设置 phase=cooldown 暂停该账号 48h
2. 换 session / 验证码 → 重启账号
3. 查 `fb_risk_events` 最近 24h 分布找模式

### 问题: 抢输入框 (A 的 send_greeting fallback 和 B 的 check_inbox 同时跑)

已通过 `device_section_lock("messenger_active")` 串行化 (F3 + A 端 f940be5)。
若仍发生, 查日志 "lock timeout" / `stats["lock_timeout"]=True`。

## 七、关键日志 + DB 表

| 表 | 字段 | 用途 |
|---|---|---|
| `facebook_friend_requests` | status / sent_at / accepted_at | A 独占写, 漏斗 stage_friend_* 源 |
| `facebook_inbox_messages` | direction / ai_decision / replied_at / template_id | 双方共写, 记忆 + 漏斗源 |
| `fb_contact_events` | event_type / meta_json | A Phase 5 新表, Lead Mesh 消费 |
| `fb_risk_events` | kind / raw_message | 双方共写, phase 升降依据 |
| `leads` | score / normalized_name | A 打分, B 的 gate 消费 |

日志关键字:
- `[ai_reply]` — B 的回复生成
- `[check_messenger_inbox]` — B 的主 inbox
- `[send_greeting]` — A 的打招呼
- `[fb_lock]` — device_section_lock 争用
- `[contact_event]` — Phase 5 事件写入
- `[P7 greeting_replied]` — P7 触发点

## 八、分阶段运维

| phase | B 的行为 | 典型日流量 |
|---|---|---|
| cold_start | auto_reply 关, 只读消息入库 | 5 inbox / 0 reply |
| growth | auto_reply 开, gate 默认 | 10-15 inbox / 5-10 reply |
| mature | 放量 + stranger auto_reply | 20 inbox / 10-15 reply / 1-3 wa_referral |
| cooldown | 全关 + 48h 暂停 | 0 |

phase 升级规则(A 的 `fb_account_phase`):
- cold_start → growth: 48h 无 risk_event + ≥5 接受好友
- growth → mature: 2 周无 risk_event + accept_rate > 30%
- 任何 phase → cooldown: 24h 内 ≥2 risk_event 自动降级

## 九、下一步

- 跑一次 smoke dryrun 确认 12+ PASS
- 连一台真机跑完 Step 1-8 的完整循环
- 观察 Dashboard 48h
- 根据实际数据调 `config/facebook_playbook.yaml` 里 `min_lead_score` / 各
  phase 的流量上限
