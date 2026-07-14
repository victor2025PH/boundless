# Phase 3 — 并发安全 + 观测性 + 基础设施

> 2026-04-23。机器 A (feat-a-phase3 分支) 做的本机可独立完成的 P0/P1 项。
> 不涉及对 B 地盘的任何修改 —— 等 B 的 Messenger 自动回复就位后,
> `greeting_replied` 事件会回写, `/facebook/greeting-reply-rate` 才有数据。

## 一、本轮解决的问题

| # | 问题 | 改动 |
|---|------|------|
| P3-1 | `daily_cap` 竞态 —— 两个 worker 同时过 gate 都放行导致超 cap 1~2 条 | 引入 `device_section_lock(did, section)` 串行化 "cap 检查 → UI 操作 → 入库" 整段 |
| P3-2 | `tasks.py` gate 调用 if-elif 膨胀,A/B 双方加 gate 会冲突 | 新 `gate_registry`,双方只需 `register_task_gate(...)` 注册自己的 gate |
| P3-3 | friend_requests + inbox 两表按对象组织,漏斗分析要 join,没有事件流水 | 新 `fb_contact_events` 统一流水表 + `/facebook/contact-events` + `/facebook/greeting-reply-rate` |
| P3-4 | `/facebook/funnel` 的 greeting 指标没有前端可视化 | Funnel modal 加入 greeting widget (总数/fallback率/rate/模板分布) |
| P3-5 | Phase 3 覆盖测试 | `tests/test_fb_phase3.py` 新增 20 个测试 |

## 二、P3-1 并发 Lock 细节

### 问题

```
worker A: check cap(n=7 < 8) → UI 发送 → 释放锁 → [executor] record_friend_request
worker B:                          check cap(n=7 < 8, A 的 record 还没到库) → 放行 → UI 发送
结果: UI 实际发 9 条, cap=8 超 1 条
```

### 方案

把"check cap"和"UI 操作"以及"入库"三步合并为一个锁内关键区。
原先 record 在 executor 层散装,现在下放到 automation 层的 `_record_friend_request_safely`,
保证入库也在锁内完成。

### 锁粒度

- key = `(device_id, section)`, section ∈ {`add_friend`, `send_greeting`}
- 同 device 同 section 串行; 跨 device 或跨 section 完全独立
- timeout=180s 兜底防死锁

### API

```python
from src.host.fb_concurrency import device_section_lock, device_lock_metrics

with device_section_lock(device_id, "send_greeting", timeout=180.0):
    # cap 检查 + 操作 + 入库
    ...

# 运维: 查锁指标
metrics = device_lock_metrics()  # {acquired_count, avg_wait_ms, timeouts, active_count, ...}
```

### 局限

- **单进程内**有效; 多进程部署需另加 SQLite advisory lock 或 Redis
- 持锁期间 UI 操作可能 30-60s, 但粒度够细不会卡全局

## 三、P3-2 Gate 注册表

### 使用

```python
# A 的 gate 已自动在 gate_registry.py 模块末尾注册好
# B 只需要在自己的 gate 模块末尾添加:

from src.host.gate_registry import register_task_gate, register_campaign_step_gate
from src.host.your_gate_module import check_reply_gate

register_task_gate("facebook_check_inbox", check_reply_gate)
register_campaign_step_gate("check_inbox", check_reply_gate)
```

然后 `tasks.py` 自动生效 —— B 不必改任何既有文件。

### 查看注册状态

```python
from src.host.gate_registry import registered_task_types, registered_campaign_steps
print(registered_task_types())  # ['facebook_add_friend', 'facebook_send_greeting', ...]
```

## 四、P3-3 接触事件表

### 表结构

`fb_contact_events(device_id, peer_name, event_type, template_id, preset_key, meta_json, at)`

### event_type 枚举

| 事件 | 写入方 | 触发时机 |
|------|--------|----------|
| `add_friend_sent` | A | add_friend_with_note 成功 |
| `add_friend_risk` | A | add_friend 失败(风控 / UI 问题) |
| `add_friend_accepted` | B | Messenger/好友请求通过时回写 |
| `add_friend_rejected` | B | 对方拒绝时回写 |
| `greeting_sent` | A | send_greeting_after_add_friend 成功(A2 路径) |
| `greeting_fallback` | A | 走了 Messenger App fallback(A1 降级) |
| `greeting_replied` | **B** | 对方回复我们的 greeting 时回写 |
| `message_received` | B | 对方主动 DM 时 |
| `wa_referral_sent` | B | 引流话术发出时 |

### B 如何回写 greeting_replied

```python
from src.host.fb_store import record_contact_event, CONTACT_EVT_GREETING_REPLIED

# B 在 check_messenger_inbox 检测到对方回复了我们的 greeting 时:
record_contact_event(
    device_id, peer_name, CONTACT_EVT_GREETING_REPLIED,
    template_id=original_greeting_template_id,  # 从 facebook_inbox_messages 的原 outgoing 行查得
    meta={"reply_ms_after": reply_delay_ms, "reply_text": reply[:50]},
)
```

这让 A 的 `/facebook/greeting-reply-rate` 能真正按模板算回复率,闭合 A/B 实验闭环。

### API

- `GET /facebook/contact-events?device_id=X&peer_name=Y` — 某人完整接触流水
- `GET /facebook/contact-events?event_type=greeting_sent&hours=24` — 某类事件近期计数
- `GET /facebook/greeting-reply-rate?hours=168` — 模板分组的 reply_rate

## 五、P3-4 前端 Funnel widget

`fbOpenFunnelModal` 改为两列布局:
- 左列: 传统漏斗(群→提取→加友→通过→DM→WA)
- 右列: greeting 专项
  - 总数 (蓝色块)
  - Fallback 路径数 + 占比 (>10% 黄色, >30% 红色)
  - 加友后打招呼率
  - 模板命中 Top 5 (水平柱状)

右下角提示: 回复率 A/B 需要机器 B 的 Messenger 自动回复合入后才能看到。

## 六、遗留 / 下一阶段

### 仍依赖 B 的 P0
- `greeting_replied` 事件回写 —— B 的 `check_messenger_inbox` 扫近 7 天的 greeting outgoing 行,
  发现新 incoming 就写 `fb_contact_events(event_type=greeting_replied)`
- `add_friend_accepted / rejected` 事件 —— B 的 `check_friend_requests_inbox` 扫通过/拒绝后回写

### 本机可继续的 P1
- **接触配额建模**: 基于 `fb_contact_events` 算 "同一 peer 24h 被接触总次数", 超阈值警告/拒绝
- **fallback 开关前端 UI**: 在 persona 卡片或 playbook 编辑器加 `allow_messenger_fallback` toggle
- **Lock 超时监控**: 把 `device_lock_metrics()` 挂到 /health 暴露
- **多进程锁扩展**: 如果部署改成 gunicorn 多 worker, 需把 `threading.Lock` 换成 SQLite advisory lock
- **真机 smoke**: 跑 name_hunter 预设验证 greeting A2 完整链路 (我没真机做不了,等你下指令)

## 七、变更文件清单

| 文件 | 类型 | 行 |
|------|------|----|
| `src/host/fb_concurrency.py` | 新建 | 140 |
| `src/host/gate_registry.py` | 新建 | 160 |
| `src/host/database.py` | 改 | 迁移 +15 |
| `src/host/fb_store.py` | 改 | +170 (新 API + 事件表 CRUD) |
| `src/app_automation/facebook.py` | 改 | 锁 + `_record_friend_request_safely` + 事件写入 hook |
| `src/host/executor.py` | 改 | 移除重复 record, 传 source/preset_key |
| `src/host/routers/facebook.py` | 改 | +/contact-events + /greeting-reply-rate + funnel 补字段 |
| `src/host/routers/tasks.py` | 改 | 用 gate_registry 替换散装 if-elif |
| `src/host/static/js/facebook-ops.js` | 改 | Funnel modal greeting widget |
| `tests/test_fb_phase3.py` | 新建 | 20 测试 |
| `docs/FB_PHASE3_PLAN.md` | 新建 | 本文档 |
