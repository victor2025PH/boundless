# Facebook 搜索 → 加好友 → 打招呼（方案 A2）

> 2026-04-23 首版。Owner: mobile-auto-0327 本机。
> 另一台电脑负责 Messenger 自动回复（方案 D），完成后会合并到 `check_inbox` 链路。

## 一、业务目标

把用户给定的"目标名字列表"或"群成员打招呼结果"转成**真实的好友关系 + 首次消息曝光**，
为后续引流（WhatsApp/Telegram/LINE）创造入口。

## 二、方案 A2（vs A1）

| 路径 | 步骤 | 风险 | 当前选择 |
|------|------|------|---------|
| **A1** | 加好友 → 切到 Messenger App → 搜名字 → 发消息 | 切 app 触发 MIUI XSpace "Select app" 弹窗；对刚加的人 Messenger 搜索命中率低 | fallback 可选 |
| **A2**（默认） | 搜索 → 进 profile → 加好友 → 停留 → **同 profile 页点 Message** → 对话页发消息 | 全程停在 `com.facebook.katana`，FB 真人路径，风控模型友好 | ✅ 默认 |

### A2 的关键节点

1. `search_people(name)` → 点击搜索结果进入 profile 页
2. `add_friend_with_note()`：看资料 2.5-4s + 滚动 1-2 次 + 总停留 8-15s → 点 "Add Friend" → 填验证语 → Send
3. **等待 `post_add_friend_wait_sec`**（默认 8-18s，模拟"看了资料再顺手打招呼"）
4. 二次风控检测（加好友后常弹 identity verification）
5. `_tap_profile_message_button()` → 打开内联对话
6. 若弹 "Send Message Request?" 确认框，先点 Send
7. `think_before_type_sec`（3-7s） → 输入 greeting（`get_greeting_message_with_id`） → 点 Send
8. 如果发送前弹确认框未处理，发送后再检测一次
9. BACK 一次回到 profile 页，保持下一轮起始点稳定

## 三、代码结构

### Automation 层
- `src/app_automation/facebook.py`
  - `send_greeting_after_add_friend(profile_name, greeting, ..., assume_on_profile=True)`
  - `add_friend_and_greet(profile_name, note, greeting, ...)` 组合封装
  - `_tap_profile_message_button()` / `_confirm_message_request_if_any()` 辅助
  - `_last_greet_skip_reason` 实例属性：归因标签

### Task 层
- `executor._execute_facebook`：新 task_type
  - `facebook_add_friend_and_greet`（单目标，带 gate）
  - `facebook_send_greeting`（独立打招呼，带 gate）
- `_run_facebook_campaign`：
  - `add_friends` step 默认走 `add_friend_and_greet`（`send_greeting_inline=True`）
  - 新增独立 `send_greeting` step

### Gate 层
- `fb_add_friend_gate.check_add_friend_gate`（兼管 `facebook_add_friend` + `facebook_add_friend_and_greet`）
- `fb_add_friend_gate.check_send_greeting_gate`（独立阀，防止绕过 add_friend cap 骚扰老朋友）

### 数据层
- `facebook_inbox_messages` 新增列：
  - `sent_at TEXT`（outgoing 专用时间，解决 `seen_at` 语义不清 + daily_cap 漏算）
  - `template_id TEXT`（模板 A/B 追踪，格式 `yaml:jp:3` / `fallback:ja:1` / `adhoc:default:0`）
  - 带 `|fallback` 后缀表示走 A1 降级路径发出

### 配置层
- `config/facebook_playbook.yaml` → `send_greeting:` 段（defaults + 4 phase）
  - `max_greetings_per_run`（单次上限）
  - `daily_cap_per_account`（24h 硬上限）
  - `inter_greeting_sec`（批量时两条间隔）
  - `post_add_friend_wait_sec`（加完好友后等多久点 Message）
  - `think_before_type_sec`（打开对话后打字前的思考停顿）
  - `enabled_probability`（A/B 抽样，1.0 必发）
  - `require_persona_template`（无本地化模板时是否跳过，防发错语种）
  - `allow_messenger_fallback`（A2 失败时是否降级 A1，默认 false）

### 预设层
- `friend_growth`：群提取 + 加好友 + 打招呼（内联）
- `name_hunter`：点名添加（需要 `add_friend_targets` 输入）— 2026-04-23 新增
- `full_funnel`：全链路（add_friends 默认已带打招呼）

## 四、调用示例

### 最小 REST 调用

```bash
# 单目标：加好友+打招呼
curl -XPOST http://localhost:8000/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "type": "facebook_add_friend_and_greet",
    "device_id": "R9...AD",
    "params": {
      "target": "山田花子",
      "persona_key": "jp_female_midlife"
    }
  }'

# 批量（预设）：点名添加（会先过 gate）
curl -XPOST http://localhost:8000/facebook/device/R9...AD/launch \
  -H 'Content-Type: application/json' \
  -d '{
    "preset_key": "name_hunter",
    "persona_key": "jp_female_midlife",
    "add_friend_targets": ["山田花子", "佐藤美咲", "鈴木由美"]
  }'
```

### 归因标签

`send_greeting_after_add_friend` 失败时会设 `_last_greet_skip_reason`：

| Reason | 含义 | 后续动作 |
|--------|------|---------|
| `phase_blocked` | phase=cold_start/cooldown，禁止打招呼 | 无，phase 变后自动恢复 |
| `prob_gate` | `enabled_probability` 抽样未命中 | 无 |
| `cap_hit` | 24h rolling 已达 daily_cap | 24h 后自然恢复 |
| `template_empty` | 无 persona 文案，`require_persona_template=true` | 运营补齐 `chat_messages.yaml` |
| `search_miss` | `search_people` 结果为空 | 名字写错 / FB 封号 |
| `first_tap_miss` | 找不到可点击的搜索结果 | 检查 selector |
| `no_message_button` | profile 页无 Message 按钮 | 可开 `allow_messenger_fallback` 降级 |
| `no_message_button_fallback_miss` | fallback 仍失败 | 对方隐私设置不允许陌生人 DM |
| `ok_via_fallback` | A1 降级路径成功 | 监控 fallback 占比，过高考虑关 A1 |
| `risk_before_msg` | 风控对话框 | 设备转 cooldown |
| `input_miss` | 对话页无输入框 | UI 变更，需更新 selector |
| `send_miss` | Send 按钮未命中 | 同上 |
| `ok` | 成功 | — |

## 五、漏斗指标

`GET /facebook/funnel` 响应中新增字段：
- `stage_greetings_sent`：主动打招呼总数（ai_decision=greeting）
- `stage_greetings_fallback`：其中走 A1 降级的数量
- `rate_greet_after_add`：greetings_sent / friend_request_sent（覆盖率）
- `greeting_template_distribution`：`[[template_id, count], ...]` 前 5（A/B 参考）

## 六、Playbook phase 分档

| Phase | max/run | daily_cap | prob | 备注 |
|-------|---------|-----------|------|------|
| cold_start | 0 | 0 | 0.0 | 全禁 |
| growth | 2 | 4 | 0.8 | 保守，20% 抽样不发 |
| mature | 3 | 8 | 1.0 | 正常 |
| cooldown | 0 | 0 | 0.0 | 全禁 |

## 七、下一阶段计划

- [ ] 合并另一台电脑的 Messenger 自动回复（方案 D）
- [ ] `sync_sent_requests`：定时扫好友请求状态，回写 `inbox.status=delivered/rejected/replied`
- [ ] 接触事件建模（`fb_contact_events` 表）：统一 `add_friend + greeting + reply_received`，修正 daily_cap 策略
- [ ] 前端漏斗面板渲染 `stage_greetings_*` / `greeting_template_distribution`
- [ ] 真机 smoke 测试 `name_hunter` 预设
