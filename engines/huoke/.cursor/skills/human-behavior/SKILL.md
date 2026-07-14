---
name: human-behavior
description: Simulate realistic human interaction patterns on mobile devices. Provides Bezier curve swiping, Gaussian typing rhythm, Poisson wait times, reading simulation, and session pacing. Use when implementing anti-detection behavior, human-like automation, or natural interaction patterns for any platform.
---

# Human Behavior Simulation Skill

## Module Location

`src/behavior/human_behavior.py` — `HumanBehavior` class

## Why This Matters

LinkedIn 检测数据 (2026):
- 28% 的账号限制源于 **非自然操作节奏**
- 34% 源于 **重复消息模式**
- 传统 `time.sleep(random.uniform(1,3))` 远远不够

## Architecture

```
HumanBehavior
├── 输入模拟
│   ├── type_text(d, text)         # 高斯分布逐字输入
│   ├── swipe_natural(d, ...)      # 贝塞尔曲线滑动
│   └── tap_natural(d, x, y)      # 带偏移的自然点击
├── 等待模拟
│   ├── wait_read(text_length)     # 模拟阅读时间
│   ├── wait_think()               # 模拟思考停顿
│   ├── wait_between_actions()     # 操作间等待
│   └── wait_poisson(mean_sec)     # 泊松分布等待
├── 会话节奏
│   ├── session_start()            # 记录会话开始
│   ├── should_rest()              # 是否该休息
│   ├── rest()                     # 执行休息周期
│   └── session_stats()            # 会话统计
└── 消息唯一化
    └── rewrite_message(template, context) → MessageRewriter
```

## Core Algorithms

### 贝塞尔曲线滑动

```
起点 P0 → 控制点 P1(随机偏移) → 控制点 P2(随机偏移) → 终点 P3
B(t) = (1-t)³P0 + 3(1-t)²tP1 + 3(1-t)t²P2 + t³P3,  t ∈ [0,1]

分为 N 个小步 (N=10~20)，每步间隔 10-30ms
模拟手指的自然弧线运动，而非直线
```

### 高斯打字

```
每个字符间隔: random.gauss(mu=120ms, sigma=40ms)
特殊规则:
- 空格后稍快 (mu=80ms)
- 标点后稍慢 (mu=200ms)
- 偶尔长停顿 (5% 概率, 500-1500ms) — 模拟思考
- 偶尔打错+退格 (2% 概率) — 最高级模拟
```

### 泊松等待

```
操作间等待: numpy.random.poisson(lam=mean_sec)
比均匀分布更自然 — 大部分等待较短，偶尔有长等待
```

### 阅读模拟

```
reading_time = text_length / reading_speed_cpm * 60
reading_speed_cpm = random.gauss(mu=250, sigma=50)  # 字/分钟
期间执行缓慢滚动，模拟视线移动
```

### 会话节奏

```
活跃期: random.gauss(mu=30min, sigma=8min)
休息期: random.gauss(mu=10min, sigma=4min)
活跃期内: 正常操作频率
休息期: 完全静默 (不操作设备)
```

## Platform Integration

每个平台的 Automation 类应通过 `HumanBehavior` 执行所有 UI 操作:

```python
# 替代直接调用
# 旧: d.click(x, y)
# 新: self.behavior.tap_natural(d, x, y)

# 旧: d(xxx).set_text(msg)
# 新: self.behavior.type_text(d, element, msg)

# 旧: time.sleep(random.uniform(1, 3))
# 新: self.behavior.wait_between_actions()
```

## Configuration

```yaml
human_behavior:
  typing:
    mean_interval_ms: 120
    sigma_ms: 40
    typo_probability: 0.02      # 打错概率
    think_pause_probability: 0.05
  swiping:
    bezier_steps: 15
    step_interval_ms: [10, 30]
    control_point_offset: [30, 80]  # 控制点偏移像素
  tapping:
    offset_pixels: [0, 5]       # 点击偏移
    pre_tap_delay_ms: [50, 200] # 点击前犹豫
  reading:
    speed_cpm_mean: 250
    speed_cpm_sigma: 50
    scroll_during_read: true
  session:
    active_mean_min: 30
    active_sigma_min: 8
    rest_mean_min: 10
    rest_sigma_min: 4
```

## ComplianceGuard 集成

`src/behavior/compliance_guard.py` — 滑动窗口限速器

```
ComplianceGuard
├── check_quota(platform, action) → bool   # 是否可执行
├── record_action(platform, action)        # 记录操作
├── get_remaining(platform, action) → int  # 剩余配额
├── get_cooldown(platform) → seconds       # 冷却倒计时
└── reset_daily()                          # 每日重置
```

存储: SQLite `data/compliance.db`

```sql
CREATE TABLE action_log (
    id INTEGER PRIMARY KEY,
    platform TEXT,          -- telegram/linkedin/whatsapp
    action TEXT,            -- send_message/search/connect/...
    account TEXT,           -- 哪个账号
    device_id TEXT,
    timestamp REAL,
    metadata TEXT            -- JSON 额外信息
);
```
