# B Messenger Bot 运维指南

> **对象**: 接手 B 自动回复机器人运维的工程师 / 未来 B 会话进入 ops 模式 / 故障第一响应人
> **范围**: messenger bot 相关的运维、观测、故障响应。A 独占区 (INTEGRATION_CONTRACT §二) 不在本文件; 找 A 运维 doc。
> **不在范围**: 业务决策 / 文案调整 / A/B 实验配置 (走 product owner)。

---

## 一、启停

### 1.1 本地开发

```bash
python src/main.py serve --host 0.0.0.0 --port 8000
```

入口文件 `src/main.py`; API app 在 `src/host/api.py`。启动时会:
1. 读 `config/devices.yaml` 连接设备
2. 调用 `src.host.database.init_db()` 自动跑 schema migration
3. 启动 HealthMonitor 后台线程 (可设 `OPENCLAW_DISABLE_DEVICE_HEALTH_MONITOR=1` 跳过, 仅用于测试)
4. 启动 job scheduler

### 1.2 Docker

```bash
docker build -t openclaw:latest .
docker run -p 8000:8000 \
  -v ./logs:/app/logs \
  -v ./data:/app/data \
  -v ./config:/app/config \
  -e ADB_SERVER_HOST=host.docker.internal \
  openclaw:latest
```

- Dockerfile 已包含 android-tools-adb
- `ADB_SERVER_HOST=host.docker.internal` 让容器连宿主机 adb server
- CMD = `python src/main.py serve --host 0.0.0.0 --port 8000`

### 1.3 停止

- 本地: `Ctrl-C` 或 `kill <pid>` (FastAPI 会 graceful shutdown, scheduler 和 worker pool 清理)
- Docker: `docker stop <container>`
- **不要** `kill -9` — 会跳过 `shutdown_pool()` / `stop_monitor()`, DB 可能 WAL 未 checkpoint

---

## 二、Health & Observability

### 2.1 全局 health endpoints (`src/host/routers/monitoring.py`)

| Endpoint | 用途 |
|---|---|
| `GET /health` | 综合健康 — capabilities + build_id (env `OPENCLAW_BUILD_ID`) + watchdog 状态 |
| `GET /health/alerts` | 活跃告警列表 (auth required) |
| `GET /health-report` | 详细健康报告 (auth required) |
| `GET /metrics` | Prometheus 格式, 对接 Grafana |
| `GET /observability/metrics` | Detailed observability (auth required) |
| `GET /devices/health-summary` | 全设备健康聚合 |
| `GET /devices/{device_id}/health-score` | 单设备评分 |
| `GET /devices/health-trends?hours=24` | 时间序列 |
| `GET /watchdog/health` | Watchdog 状态 |

### 2.2 B 专属 Facebook endpoints (`src/host/routers/facebook.py`, prefix `/facebook`)

**观测/漏斗**:

| Endpoint | 用途 |
|---|---|
| `GET /facebook/funnel[?group_by=preset_key]` | 转化漏斗 (加好友 → 接受 → 打招呼 → 回复) |
| `GET /facebook/contact-events` | `fb_contact_events` 查询 (add_friend_accepted / greeting_replied / message_received / wa_referral_sent) |
| `GET /facebook/greeting-reply-rate` | 按 template_id 聚合招呼回复率 (Phase 5 weekly A/B) |
| `GET /facebook/qualified-leads` | 合格 lead 列表 |
| `GET /facebook/campaign-runs` | 营销活动运行历史 |
| `GET /facebook/insights`, `GET /facebook/insights/stats` | 漏斗分析 + 统计 |
| `GET /facebook/l1-rule-analytics` | L1 规则命中分析 (A 的 persona gate 维护) |
| `GET /facebook/content-exposure/top-interests` | 兴趣曝光 |
| `POST /facebook/daily-brief/generate`, `GET /facebook/daily-brief/latest` | 日报 |
| `GET /facebook/dashboard/ops` | Ops 面板数据 |

**Phase + playbook 状态** (读-only, A 独占写):

| Endpoint | 用途 |
|---|---|
| `GET /facebook/phase` | 全设备 phase 总览 |
| `GET /facebook/phase/{device_id}` | 单设备 phase |
| `GET /facebook/playbook` | 当前 playbook 配置快照 |
| `POST /facebook/playbook/reload` | 热重载 `config/facebook_playbook.yaml` (热修复后要跑) |
| `GET /facebook/target-personas`, `POST /facebook/target-personas/reload` | persona 配置 |

**风控 + 选择器**:

| Endpoint | 用途 |
|---|---|
| `GET /facebook/risk/status` | 当前各设备风控状态 (core on-call 面板) |
| `GET /facebook/risk/history/{device_id}` | 该设备风控事件历史 (fb_risk_events) |
| `POST /facebook/risk/clear/{device_id}` | **⚠ 清风控** — 谨慎, 会解除 cooldown 允许设备继续跑 |
| `POST /facebook/risk/reload` | 重载 `_RISK_KIND_RULES` 分类规则 |
| `POST /facebook/risk/inject` | 测试专用, 注入假风控事件验证响应链 |
| `GET /facebook/selectors/health` | UI 选择器健康 (FB UI 改版时红) |

**VLM + persona 分类**:

| Endpoint | 用途 |
|---|---|
| `GET /facebook/vlm/health` | Ollama VLM (persona 分类) 服务健康 |
| `GET /facebook/vlm/level4/status` | **Level 4 UI fallback** 运行时状态 (provider / P5b swap / 失败 counter / last_error / budget) — 详见 §12.5 |
| `POST /facebook/vlm/warmup` | 预热 (首次 VLM 调用慢) |
| `POST /facebook/classify/single` | 手工单次 L1+L2 分类 |

---

## 三、B 任务类型 + Cooldown

`src/host/executor.py` 里 `TASK_COOLDOWN_SECONDS`:

| task_type | cooldown | 用途 |
|---|---|---|
| `facebook_check_inbox` | **15 min** | 收件箱自动回复 (B 核心) |
| `facebook_check_message_requests` | **10 min** | 消息请求 (陌生人) 回复 |
| `facebook_check_friend_requests` | **10 min** | 好友请求审核 + accept |
| `facebook_send_message` | — | Messenger 发消息 (A 的 A2 降级也调这个, 但 A 不改 B 的实现) |

### 触发 task

```bash
POST /tasks
Content-Type: application/json
Authorization: Bearer <token>

{
  "task_type": "facebook_check_inbox",
  "device_id": "devA",
  "params": { /* 具体字段见 src/host/schemas.py::TaskCreate */ }
}
```

查询状态: `GET /tasks/{task_id}` → `TaskStatus` enum (pending/running/completed/failed)。

定时任务 (scheduler): `src/host/job_scheduler.py`, config 在 `config/scheduled_jobs.json`。

---

## 四、日志

- **主日志**: `logs/openclaw.log` (轮转策略见 `src/utils/log_config.py`)
- **截图**: `logs/screenshots/` (风控触发或 UI 异常时 dump)

### 常见查询

```bash
# 最近 200 行
tail -n 200 logs/openclaw.log

# B bot 专属
grep -E '\[check_messenger_inbox\]|\[check_message_requests\]|\[check_friend_requests_inbox\]' logs/openclaw.log

# 发送错误 (MessengerError 7 档)
grep -E 'MessengerError\(code=' logs/openclaw.log

# 风控事件
grep -E '\[risk_detected\]|fb_risk_events|content_blocked' logs/openclaw.log

# Lock timeout (A+B 共用 messenger_active section)
grep -E 'device_section_lock timeout|LockTimeoutError' logs/openclaw.log

# Phase 切换
grep -E 'fb_account_phase|set_phase' logs/openclaw.log
```

---

## 五、故障 playbook — MessengerError 7 档

详细分流矩阵见 `docs/INTEGRATION_CONTRACT.md §7.6`。A 的 A2 降级路径也按此 catch。

| code | 第一响应 | 根因排查 |
|---|---|---|
| `messenger_unavailable` | 跳该 peer + 设备级标记 `messenger_not_ready` (30 min 过期) | Messenger app 卡死 / cold start 失败; `adb shell am force-stop com.facebook.orca` 后重跑 |
| `xspace_blocked` | Retry 1 次; 仍失败降级 FB 主 app 个人页 DM | MIUI/HyperOS XSpace 双开选择框; **不** cooldown |
| `risk_detected` | `fb_account_phase.set_phase(did, 'cooldown')` 30 min 硬停 | `GET /facebook/risk/status` 看详情, `GET /facebook/risk/history/{did}` 看历史 |
| `search_ui_missing` | 等 8s retry 1 次; 仍失败降级 FB 主 app | UI 改版可能 → 查 `GET /facebook/selectors/health` |
| `recipient_not_found` | Retry 2 次 × 5-15s; 跳过该 peer | peer 刚 accept 但 Messenger 未索引, `journey=referral_blocked{reason=peer_not_in_messenger}` |
| `send_button_missing` | `record_risk_event(content_blocked, text_hash)` + 降级 FB 主 app | 文案可能违禁 (被键盘遮住 / Send 灰出) |
| `send_blocked_by_content` (PR #1) | 同上 + 用更短模板重试一次 | FB 主动拒发, `hint=text_hash=...` 去重防重触 |
| `send_fail` | Cooldown 3 min + retry 1 次 | 保底, 未分类 |

### 快速处理脚本

```bash
# 设备风控状态
curl -H "Authorization: Bearer $TOKEN" http://localhost:8000/facebook/risk/status | jq

# 清某设备风控 (谨慎!)
curl -X POST -H "Authorization: Bearer $TOKEN" http://localhost:8000/facebook/risk/clear/devA

# 重载风控规则 (config/fb_risk_rules.yaml 改动后)
curl -X POST -H "Authorization: Bearer $TOKEN" http://localhost:8000/facebook/risk/reload
```

---

## 六、Phase 管理 (read-only for B)

Phases (A 独占 `src/host/fb_account_phase.py` 写入):

| Phase | 含义 | B 侧行为 |
|---|---|---|
| `cold_start` | 新设备热身, 保守低频 | check_inbox 频率降低 |
| `growth` | 活跃产出 | 正常频率 |
| `mature` | 稳定长期运营 | 正常频率 |
| `cooldown` | 风控触发, 30 min 硬停 | **B 所有 task_type 拒绝执行**, 返回 skip |

### 查看

```bash
# 全设备 phase 总览
curl http://localhost:8000/facebook/phase | jq

# 单设备
curl http://localhost:8000/facebook/phase/devA | jq
```

### 热重载 playbook

改 `config/facebook_playbook.yaml` 后:

```bash
curl -X POST http://localhost:8000/facebook/playbook/reload
```

不需要重启 service。`defaults.check_inbox:` 段 B 维护, 其他段 A 维护 (INTEGRATION_CONTRACT §五)。

---

## 七、DB migration

SQLite (WAL mode), 迁移逻辑在 `src/host/database.py::init_db()`。

- App 启动时自动调 `init_db()`
- 手工 (部署前 smoke / 修复场景):
  ```bash
  python -c "from src.host.database import init_db; init_db()"
  ```
- 备份: `stop app + cp data/openclaw.db data/openclaw.db.bak` (必须 stop, 否则 WAL + -shm 不完整)

### B 写入的表 (INTEGRATION_CONTRACT §三)

- `facebook_inbox_messages` — B 写 `direction='incoming'` 全部 + `direction='outgoing' + ai_decision IN ('reply', 'wa_referral')`; 允许回写 A 写入的 greeting 行 `replied_at` 字段
- `fb_contact_events` (Phase 5) — B 在 PR #10 实施 3 个触发点 (message_received / wa_referral_sent / add_friend_accepted)
- `fb_risk_events` — 双方都写 (检测到风控上报)

---

## 八、On-call 第一响应清单

### 页面卡 / 无回复

1. `GET /health` 综合绿? 失败 → `GET /health/alerts` 看告警
2. 最近 10 min error: `tail -n 500 logs/openclaw.log | grep -iE 'ERROR|Exception|MessengerError'`
3. `GET /facebook/risk/status` 风控触发设备?
4. `GET /facebook/funnel` 漏斗断在哪层 (加好友 / 接受 / 打招呼 / 回复)?

### 具体故障

| 症状 | 排查 |
|---|---|
| 全设备停跑 | `GET /watchdog/health` 看 Watchdog 状态; `GET /devices/health-summary` 看设备连接 |
| 单设备跑不起 | `GET /facebook/phase/{did}` 是否 cooldown; `GET /facebook/risk/history/{did}` |
| Messenger 回复停 | `grep 'check_messenger_inbox\|MessengerError' logs/openclaw.log \| tail -50`; 比对 §五 7 档分流 |
| 招呼回复率异常 | `GET /facebook/greeting-reply-rate?hours=24` 和历史比 |
| Lock timeout 频繁 | `grep 'device_section_lock timeout' logs/openclaw.log`; `messenger_active` section 抢占说明 A/B 调度撞车 |

### 紧急升级

1. 单设备救急: `POST /facebook/risk/clear/{did}` + 观察
2. 全面停 B 任务: 注释 scheduled_jobs.json 里 B task 或改 cooldown = 86400
3. 代码 hot-fix: 改 + `reload playbook` (若仅配置) 或 重启 service (若 code)
4. 最极端: `docker stop` + 修 + `docker run`

---

## 九、跨 bot (A ↔ B) 协同契约要点

完整版见 `docs/INTEGRATION_CONTRACT.md`, on-call 只需记:

- **A 独占**: `fb_account_phase` / `fb_add_friend_gate` / greeting 相关方法 — **B 绝不改**, 改这些找 A
- **B 独占**: `check_*_inbox` / `_ai_reply_and_send` / `send_message` / stranger reply — A 只读调用
- **共享**: `fb_store.py` CRUD / `database.py` migrations / `fb_concurrency.py` / `config/chat_messages.yaml` / `fb_content_assets.py` — 改必 @对方 review
- **Lock section**: `messenger_active` A+B 共用, 抢占要长等 (see §7.7)
- **数据库字段语义**: `facebook_inbox_messages` ai_decision ∈ `{greeting|reply|wa_referral|''}`, direction + peer_type 组合见 §三

---

## 十、已知非 block issues (2026-04-24)

来自 `docs/B_PRODUCTION_READINESS.md §四`:

1. **PR #10 fb_contact_events 代码误合到 `feat-b-followup-a-review` 分支** (merge sha `09ccc79c`), 非 main。补救: A review PR #9 时看到 combined diff 一并 re-approve, 带 #10 一起进 main。
2. **2 pre-existing pytest failures** (非 B 引入): `test_api.py::test_create_task` / `test_behavior.py::test_cleanup_old`。不 block 合并, 建议后续由 A 侧排查。
3. **栈上层 PR 无 CI checks** — 由 `on.pull_request.branches:[main]` 决定, 非 bug。合 main 前 retarget 到 main 后 CI 触发。

---

## 十一、相关文档

- 契约权威: `docs/INTEGRATION_CONTRACT.md`
- B 角色定义: `docs/FOR_MESSENGER_BOT_CLAUDE.md`
- 当前状态: `docs/B_PRODUCTION_READINESS.md`
- A↔B round 3 review: `docs/A_TO_B_ROUND3_REVIEW_RESULTS.md`
- B 崩溃恢复脚本 (技术视角): `docs/B_RESUME_2026-04-23-EVENING.md`
- 架构总览: `docs/ARCHITECTURE_OVERVIEW.md` (如果存在)

---

## 十二、VLM (Level 4 UI Fallback) 运维

**2026-04-24 新增** — Messenger 2026 Compose UI 下 `smart_tap` + multi-locale selector + coordinate 三级 fallback 仍可能 miss (搜索栏、搜索结果、Compose-rendered Send 按钮偶有 SDUI 渲染, 不在 AccessibilityNode tree)。第 4 级使用 VisionFallback (`src/ai/vision_fallback.py`) 用多模态 LLM 识别屏幕上目标元素坐标。

### 12.1 什么时候会启用 VLM

自动触发 — caller 无感知。流程:

```
smart_tap → multi-locale selector → coordinate → VLM vision
     ↑ L1             ↑ L2                ↑ L3        ↑ L4 (本节)
```

已接入 VLM 的 send_message 子步骤:
- `_enter_messenger_search` (打开搜索框)
- `_tap_first_search_result` (选中搜索结果第一条; **2026-04-24 新接入**)
- `_tap_messenger_send` (点 Send 按钮)

前 3 级 miss 时自动 fall through 到 L4, 成功无日志中断, 命中时 `log.info("[_xxx_] VLM hit @ (x, y)")`。

### 12.2 Provider 选择

按优先级 (`get_free_vision_client()`):

| Provider | 限额 | 适用场景 | 配置 |
|---|---|---|---|
| **Gemini 2.5 Flash** | 1500 req/day 免费 | 默认 — 识别准确率高 | `export GEMINI_API_KEY=...` |
| **Ollama (llava/moondream/minicpm/bakllava/qwen2.5vl)** | 本地无限 | Gemini 不可用 / 隐私敏感 | `ollama pull llava:7b` |
| (无) | — | 禁用 L4 (前 3 级失败直接抛) | 两者均不设 |

### 12.3 配置

**Gemini (推荐)**:
```bash
export GEMINI_API_KEY=AIzaSy...      # 从 https://aistudio.google.com/apikey 申请
```
首次 `_get_vision_fallback()` 调用会 lazy init, 日志:
```
[vision] VisionFallback ready (Level 4 UI fallback, 免费 provider)
```

**Ollama 本地 (备用 / fallback 的 fallback)**:
```bash
# 装 Ollama: https://ollama.com/download
ollama pull llava:7b                 # 或 moondream (体积小), qwen2.5vl:7b (更新)
# Ollama 默认监听 localhost:11434, 无需其他配置
```
无 GEMINI_API_KEY 时 `get_free_vision_client()` 自动 probe Ollama。**两者都有时默认 Gemini**; Gemini 连续失败时自动 swap (见 §12.4)。

### 12.4 Gemini → Ollama 运行时 swap (P5b, 2026-04-24)

Peak-hour Gemini 经常返 503 "high demand"。若连续 3 次 VLM HTTP 失败 (code 非 None 或 timeout) 且当前 provider 是 Gemini 且 Ollama 本地可用 → 自动 swap `_vision_fallback_instance` 为 Ollama client。

**观察日志**:
```
[vision] VLM HTTP failure #3 (code=503, body=The service is currently unavailable...)
[vision] Gemini 连续 3 次 HTTP 失败 (last code=503), 切 Ollama (model=llava:7b)
```

**Swap 单向** — 一次 swap 后不再 flip-flop 回 Gemini (避免来回抖)。想恢复 Gemini 需重启 bot 进程 (`_vlm_provider_swapped` reset 到 False)。

### 12.5 运行时状态查询

**Python REPL / 调试**:
```python
from src.app_automation import facebook as fb
print({
    "instance":  fb._vision_fallback_instance,          # None = 未 init
    "swapped":   fb._vlm_provider_swapped,              # True = 已 swap 到 Ollama
    "failures":  fb._vlm_consecutive_failures,          # 连续 HTTP 失败 count
    "provider":  getattr(
        fb._vision_fallback_instance and fb._vision_fallback_instance._client.config,
        "provider", None),
})
# VisionFallback 本身的 budget + cache
if fb._vision_fallback_instance:
    print(fb._vision_fallback_instance.stats())
    # → {"hourly_used": 7, "hourly_budget": 20, "budget_remaining": 13, "cache_size": 4}
```

**LLMClient last error (P5c)**:
```python
c = fb._vision_fallback_instance._client
c.last_error_code    # 最后一次 HTTP 状态码 (None = 上次是 success or non-HTTP 层错)
c.last_error_body    # 最后一次 error 响应 body[:500] 或 "timeout"
```

**HTTP 查询 (无 Python REPL 的场景, P15 新增)**:
```bash
# 等价于上面所有字段, 直接 curl / jq 可读
curl -s http://localhost:8000/facebook/vlm/level4/status | jq
# {
#   "provider": "gemini",
#   "vision_model": "gemini-2.5-flash",
#   "swapped": false,              # true = P5b 已切 Ollama
#   "consecutive_failures": 0,      # 达 3 触发 swap
#   "swap_events_total": 0,         # P16 累计 swap 次数 (单向, swapped=true 后不再增)
#   "last_error_code": null,        # 503/429/...
#   "last_error_body": "",          # 或 "timeout"
#   "budget": {"hourly_used": 7, "hourly_budget": 20, "budget_remaining": 13, "cache_size": 4},
#   "init_attempted": true
# }
```

**Prometheus 抓取 (P16 新增)**:

`GET /observability/prometheus` 追加了 Level 4 VLM 指标 (自动追加，无需额外配置)。示例输出:
```
openclaw_vlm_level4_swapped 0
openclaw_vlm_level4_consecutive_failures 0
openclaw_vlm_level4_swap_events_total 0
openclaw_vlm_level4_ready 1
openclaw_vlm_level4_init_attempted 1
openclaw_vlm_level4_budget_used 7
openclaw_vlm_level4_budget_hourly 20
openclaw_vlm_level4_budget_remaining 13
openclaw_vlm_level4_cache_size 4
openclaw_vlm_level4_last_error_code 0
openclaw_vlm_level4_provider_info{provider="gemini",vision_model="gemini-2.5-flash"} 1
```

**Latency histogram (P18)** — find_element 每次 call 落入 cumulative bucket:
```
openclaw_vlm_level4_call_duration_seconds_bucket{le="0.5"} 0
openclaw_vlm_level4_call_duration_seconds_bucket{le="1.0"} 0
openclaw_vlm_level4_call_duration_seconds_bucket{le="2.0"} 0
openclaw_vlm_level4_call_duration_seconds_bucket{le="5.0"} 12
openclaw_vlm_level4_call_duration_seconds_bucket{le="10.0"} 25      # HIT 集中这 ~8s
openclaw_vlm_level4_call_duration_seconds_bucket{le="20.0"} 28
openclaw_vlm_level4_call_duration_seconds_bucket{le="30.0"} 30
openclaw_vlm_level4_call_duration_seconds_bucket{le="60.0"} 32      # MISS-retry ~32s
openclaw_vlm_level4_call_duration_seconds_bucket{le="+Inf"} 32
openclaw_vlm_level4_call_duration_seconds_sum 294.350
openclaw_vlm_level4_call_duration_seconds_count 32
```

Grafana 公式:
- P95: `histogram_quantile(0.95, rate(openclaw_vlm_level4_call_duration_seconds_bucket[5m]))`
- 平均: `rate(..._sum[5m]) / rate(..._count[5m])`
- "MISS-with-retry" 比例: `1 - (rate(..._bucket{le="10.0"}[5m]) / rate(..._count[5m]))`

推荐 Grafana alert rules:
| rule | 语义 |
|---|---|
| `vlm_level4_consecutive_failures >= 2 for 5m` | 即将 swap 预警 |
| `increase(vlm_level4_swap_events_total[1h]) > 0` | 1h 内触发过 swap (Gemini 挂) |
| `vlm_level4_budget_remaining < 3` | 小时 VLM 预算快耗尽 |
| `vlm_level4_last_error_code == 429 for 2m` | Gemini rate limit 持续 |
| `vlm_level4_last_error_code >= 500 for 2m` | provider 5xx 持续 |
| `vlm_level4_ready == 0 and vlm_level4_init_attempted == 1` | 无 VLM provider 可用 (Gemini key 未设 + Ollama 没起) |
| `histogram_quantile(0.95, ..._duration_seconds_bucket) > 15 for 10m` | P95 latency 异常 (可能全员 MISS-with-retry, provider 深度降级) |

按 `provider_info` label 分面板, 可分别看 Gemini / Ollama 时段的命中/失败分布。

### 12.6 日志诊断 checklist

| 症状 | 根因 | 处理 |
|---|---|---|
| 未见 `[vision] VisionFallback ready` | 无 provider — `GEMINI_API_KEY` 未设 + Ollama 未起 | 按 §12.3 二选一 |
| `VisionFallback budget exhausted (20/20)` | 每小时 20 次调用上限用完 | 调 `VisionConfig(hourly_budget=...)` 或 1h 等待 |
| `VLM out-of-bounds coords (x, y)` | VLM 返超屏坐标 (e.g. 720x1600 上返 (980, 2020)) | 自动 reject + retry, 无需人工; 参见 PR #57 |
| `VLM click (x, y) 后 EditText 未出现, invalidate cache` | post-verify 失败, 5min TTL 内不复发坏坐标 | 正常 self-healing, 无需人工 |
| `[vision] VLM HTTP failure #N (code=503)` 反复 | Gemini peak-hour 过载 | 等 §12.4 自动 swap 或手动起 Ollama |
| Level 4 抛 `xxx_missing 4 级 fallback 都失败` hint | 4 级全 miss, Messenger UI 大改版 / provider 挂 | 跑 `scripts/messenger_vlm_prompt_eval.py` 验 prompt 是否还准 |

### 12.7 Prompt 质量回归 (eval 工具)

`scripts/messenger_vlm_prompt_eval.py` — offline 用真机截图 + ground truth bbox 跑 VLM, 算命中率。**改 prompt / 切 Gemini 版本 / 换 provider 前后都跑一遍**。

```bash
# 基础回归 (default dataset)
python scripts/messenger_vlm_prompt_eval.py

# JSON 输出供 CI / 对比
python scripts/messenger_vlm_prompt_eval.py --json > eval_before.json
# ... 改 prompt ...
python scripts/messenger_vlm_prompt_eval.py --json > eval_after.json
diff <(jq -S . eval_before.json) <(jq -S . eval_after.json)
```

**退出码**: 0 = 全 HIT / SKIP, 1 = 有 WRONG/MISS/ERROR, 2 = 依赖错误 (no dataset / no VLM provider)。

**数据集**: `scripts/vlm_eval_dataset/cases.yaml` (metadata 入库) + `screenshots/` (真机截图 gitignored, 本地 populate)。增 case 只需:
1. 截屏存 `scripts/vlm_eval_dataset/screenshots/<name>.png`
2. cases.yaml 加一条 `{ screenshot, target, context, ground_truth_bbox: [x1,y1,x2,y2] }`
3. 跑 eval 看 hit rate

当前 14 cases 覆盖 inbox search bar / conversation row / 搜索结果 / composer / send button / note popup / DM screen 等核心 Messenger UI。

### 12.8 Provider 不可用 playbook

```bash
# 症状: Level 4 也 miss, recipient_not_found 频发

# 1. 确认 provider 状态
python -c "from src.ai.llm_client import get_free_vision_client as g; \
           c = g(); print('provider:', c and c.config.provider)"

# 2a. Gemini 路: 验 API key
curl -H "x-goog-api-key: $GEMINI_API_KEY" \
    "https://generativelanguage.googleapis.com/v1beta/models" | head

# 2b. Ollama 路: 验服务 + model
curl -s http://localhost:11434/api/tags | jq '.models[].name'

# 3. 两个都不可用时: 准备 Ollama
ollama pull llava:7b     # 或 moondream:latest (~1.6GB, 更轻)

# 4. 进程内 reset swap 状态 (重启 bot 更简单)
python -c "from src.app_automation import facebook as fb; \
           fb._vlm_provider_swapped=False; fb._vision_fallback_instance=None; \
           fb._vision_fallback_init_attempted=False"
```

---

## 十三、历史变更

- 2026-04-24 首版 — B Claude 在 autonomous loop Iter 2 写, 关闭 `B_PRODUCTION_READINESS §四.3` flag 的 ops runbook gap
- 2026-04-24 (P4, 本次) — 加 §十二 VLM Level 4 fallback 运维章节: provider 选择 / Gemini→Ollama swap / 状态查询 / 日志诊断 / eval 工具
