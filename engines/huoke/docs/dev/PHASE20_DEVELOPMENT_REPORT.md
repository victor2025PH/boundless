# Phase 20 系列开发报告 — Referral 闭环 A 侧基础设施

> 时段: 2026-04-25 单日
> 分支: `feat-a-reply-to-b` (PR #72)
> 测试基线: 315 passed (Phase 11 → 20.3)

---

## 1. 业务背景

LINE referral 是 FB 引流的"最后一公里": 用户在 Messenger 里同意加 LINE 之后,
我们派发 LINE ID, 跟踪用户是否真去加了, 形成可观测的转化漏斗:

```
greeting_replied  →  line_dispatch_planned  →  wa_referral_sent  →  wa_referral_replied
   (B 写)              (A 写, dispatcher)        (A 写, send)         (B 抓 / A 写, Phase 20.1 新增)
                                                                          ↓ 24-48h 未 reply
                                                                       referral_stale (Phase 20.2)
                                                                          ↓ 7d 仍未 reply
                                                                       referral_dead → Phase 14 回收
```

**Phase 20 之前**: `wa_referral_replied` 没有人写, 转化率永远是 0, 文案 A/B
无法收敛。

**Phase 20 目标**: 把"用户回复"信号引入闭环, 提供 SLA 死信回收 +
完整可观测性 (alert / latency / 历史 / per-region)。

---

## 2. 架构总览

```
┌─────────────────────────────────────────────────────────────────────┐
│                       Scheduled Jobs (cron)                         │
│  line_dispatch_30min  send_referral_30min  daily_summary_2355        │
│  alert_check_hourly  check_referral_replies_15m  mark_stale_daily   │
└────────────────────────────────┬────────────────────────────────────┘
                                  │
                                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                       Executor (src/host/executor.py)               │
│  _fb_line_dispatch_from_reply  _fb_send_referral_replies             │
│  _fb_daily_referral_summary    _fb_alert_check_hourly                │
│  _fb_check_referral_replies    _fb_mark_stale_referrals  ★Phase 20  │
└──────┬──────────────────┬──────────────────────────┬─────────────┬──┘
       │                  │                          │             │
       ▼                  ▼                          ▼             ▼
 ┌───────────┐     ┌──────────────┐       ┌──────────────┐  ┌───────────┐
 │ line_pool │     │  fb_store    │       │   FbAuto     │  │ lead_mesh │
 │ (LINE 池)  │     │ (events DB)  │       │ (B side UI)  │  │ canonical │
 └───────────┘     └──────────────┘       └──────────────┘  └───────────┘
                          │
                          ▼
                   ┌──────────────┐      Phase 20.1.9.1 ★
                   │ alert_history│      (跨进程持久化)
                   └──────────────┘
```

---

## 3. Phase 时间线 (单日 7 commit)

| commit | Phase | 主题 | 测试 |
|---|---|---|---|
| `1f5d94e` | 20.1 | referral_replies 调度 + 关键词字典 + B 接口契约 | 17 cases |
| `bd99b1e` | 20.1.7 | 自动 region 路由 + reply latency 指标 | +17 |
| `b8439a8` | 20.1.8 | peer→region 5min TTL 缓存 + replied_rate_low alert | +12 |
| `0f3d78e` | 20.1.9 | alert history 表 + per-region alerts + latency anomaly z-score | +17 |
| `2b83f0b` | 20.2 | SLA 死信回收 — referral_stale + 升级 referral_dead | +13 |
| `fc9add8` | 20.2.x | stale auto-revive + stale_rate_high alert + stale-leads endpoint | +12 |
| `9f2814b` | 20.3 | Mock B Messenger 联调脚手架 + e2e 完整闭环 | +12 |
| `f50d2c4` | 20.x | A→B handoff 信号 + 回应 PR #80 5 风险点 | — |

**累计**: 315 tests passed (Phase 11-20.3 全 phase 全绿)

---

## 4. 数据流细节

### 4.1 派发 → 发送 (Phase 11-12, 2026-04-25 早期已落地)

```python
# 触发: greeting_replied 事件 (B 写) 或 cron line_dispatch_30min
_fb_line_dispatch_from_reply(params={
    "hours_window": 6,
    "require_l2_verified": True,
    "persona_key": "jp_female_midlife",
    "min_score": 70,
})
# → 扫 fb_contact_events.greeting_replied
# → 过滤: l2_verified + persona 匹配 + score >= 70
# → line_pool.allocate() 轮循取 LINE 账号
# → 写 line_dispatch_planned 事件 + line_dispatch_log

# 触发: cron send_referral_replies_30min
_fb_send_referral_replies(params={
    "hours_window": 2,
    "dedupe_hours": 24,
    "strict_device_match": True,
})
# → 扫 line_dispatch_planned 事件
# → 调 facebook.send_message() 发 LINE ID 文案
# → 成功: 写 wa_referral_sent + dispatch_log status=sent
# → 失败: 写 dispatch_log status=failed, peer fail count++,
#   达阈值 (3 次同 err_code) → tag canonical referral_dead
```

### 4.2 Reply 检测 (Phase 20.1 新增)

```python
# 触发: cron check_referral_replies_15min (默认 disabled, 等 B 实装)
_fb_check_referral_replies(params={
    "hours_back": 48,
    "max_messages_per_peer": 5,
})
# 1. 拉 pending peers (sent 后 N 小时未 replied)
# 2. batch _resolve_peer_regions() — 5min TTL cache, peer→region map
# 3. 调 fb.check_messenger_inbox(referral_mode=True, peers_filter, ...)
#    ★ B 实装这个接口, 返 [{peer_name, last_inbound_text, conv_id}]
# 4. 对每条 conv: _match_referral_keyword(text, region) — yaml 字典 + region 优先
# 5. 命中 → record_contact_event(wa_referral_replied, meta={
#       platform="facebook",                       ★ TG R2 Q2 namespace
#       keyword_matched, raw_excerpt, sent_event_id,
#       latency_seconds, latency_min, region,
#    })
# 6. record_contact_event 末尾 hook 自动 _maybe_revive_stale_on_reply():
#    若 canonical 有 referral_stale tag → 移除 + 写 stale_revived_at meta
```

### 4.3 SLA 死信回收 (Phase 20.2 新增)

```python
# 触发: cron mark_stale_referrals_daily (默认 disabled, 04:30)
_fb_mark_stale_referrals(params={
    "stale_hours": 48,
    "escalate_to_dead_days": 7,
})
# 1. 拉 pending (lookback = max(stale_hours, esc*24*2)+1, 给 escalation buffer)
# 2. 按 sent age 分类:
#    - age >= 48h ∧ < 7d:  tag referral_stale + 写 stale event + meta
#    - age >= 7d:          额外 tag referral_dead + reason="stale_no_reply"
# 3. 已 stale 不重复 mark, 但仍可后续升级 dead
# 4. dry_run=True 仅统计不写入
```

### 4.4 Daily Summary (Phase 19 + 20 累积)

```python
# 触发: cron daily_referral_summary_2355 (23:55, enabled)
_fb_daily_referral_summary(params={"regions": ["jp", "it"]})
# 输出 logs/daily_summary_YYYYMMDD.json:
{
    "generated_at": "...",
    "funnel": {
        "planned": N, "sent": N, "replied": N, "stale": N,    ★ Phase 20.2
        "send_rate": 0.X, "conversion_rate": 0.X, "stale_rate": 0.X,
    },
    "by_region": {"jp": {...}, "it": {...}},                  Phase 19.3
    "top_5_accounts": [...],                                   Phase 13
    "reply_latency": {                                         ★ Phase 20.1.7.2
        "samples": N, "avg_min": X, "median_min": X,
        "p95_min": X, "max_min": X,
    },
    "latency_anomaly": {                                       ★ Phase 20.1.9.3
        "samples": 5, "avg_baseline": 8.3, "stdev": 1.2,
        "z": 3.4, "anomaly": True,
    },
    "trend": {"yesterday_date": "...", "planned_delta": ...}, Phase 19.1
    "trend_7d": {                                              Phase 19.x.2
        "samples": 7, "avg_planned": ..., "stdev_planned": ...,
        "z_planned": ..., "anomaly": True/False,               Phase 19.x.3.2
    },
    "alerts": [                                                Phase 19.2 → 20.x
        {"type": "send_rate_low", "severity": "warning", "message": "..."},
        {"type": "replied_rate_low", "severity": "warning", "region": "jp",
         "message": "[jp] reply_rate=8% < 20% (sent=12, replied=1)"},
        {"type": "stale_rate_high", "severity": "warning", ...},
        {"type": "latency_anomaly", "severity": "warning", ...},
    ],
}
# 同时写 fb_alert_history 表 (Phase 20.1.9.1)
# 触发 webhook (有 alerts 加 🚨, 有 OPENCLAW_DASHBOARD_BASE_URL 加详情链接)
```

---

## 5. Alert 规则总览 (Phase 19-20)

| Type | Severity | Cooldown | 触发条件 | 来源 |
|---|---|---|---|---|
| `send_rate_low` | warning | 24h | planned≥5 ∧ send_rate<30% | Phase 19.2 |
| `reject_rate_high` | warning | 24h | reject_total>=10 | Phase 19.2 |
| `no_dispatched` | critical | 4h | planned≥5 ∧ sent=0 | Phase 19.2 |
| `replied_rate_low` | warning | 24h | sent≥10 ∧ replied/sent<20% | ★ Phase 20.1.8.2 |
| `stale_rate_high` | warning | 24h | sent≥10 ∧ stale/sent>=50% | ★ Phase 20.2.x.2 |
| `latency_anomaly` | warning | 24h | 7d baseline avg latency \|z\|>2 | ★ Phase 20.1.9.3 |

**per-region 维度** (Phase 20.1.9.2): 上面规则对每个 region (jp/it) 独立触发,
state_key = `f"{type}:{region}"`, 不同 region 互不抑制。

---

## 6. API 端点清单 (Phase 20 新增)

| 路由 | 用途 | 来源 |
|---|---|---|
| `GET /line-pool/stats/referral-funnel` | 漏斗 4+1 维 (含 stale) | Phase 13 + 20.2 |
| `GET /line-pool/stats/account-ranking` | per-LINE 成功率排名 | Phase 13 |
| `GET /line-pool/stats/peer-name-rejects` | sanitize 拒绝计数 | Phase 17.1 |
| `GET /line-pool/stats/peer-name-rejects/history` | 跨进程 reject 历史 | Phase 18 |
| `GET /line-pool/stats/daily-summary?date=YYYYMMDD` | 缓存的每日摘要 JSON | ★ Phase 19.x.3.3 |
| `GET /line-pool/stats/alert-history` | 跨进程 alert 触发历史 | ★ Phase 20.1.9.1 |
| `GET /line-pool/stats/stale-leads` | 当前 referral_stale 列表 | ★ Phase 20.2.x.3 |

---

## 7. 测试金字塔 (315 tests)

```
                    ╱─────────────╲
                   ╱  e2e (12)    ╲          tests/test_phase20_3_e2e_full_loop.py
                  ╱─────────────────╲          (with FakeBMessenger mock)
                 ╱                   ╲
                ╱  Integration (51)   ╲       tests/test_phase11_/12_/13_/...
               ╱─────────────────────────╲     (db + executor + line_pool)
              ╱                           ╲
             ╱       Unit (252)            ╲   sanitize / cache / parse /
            ────────────────────────────────  匹配 / canonical helpers
```

### 主要测试模块

| 模块 | tests | 覆盖 |
|---|---|---|
| `test_phase11_line_pool` | 22 | LINE 池 CRUD + 轮循分配 |
| `test_phase12_*` (5 文件) | 55 | dispatcher + dead/cooldown + revive + audit |
| `test_phase13_funnel_ranking_decisions` | 14 | funnel + ranking + decision logging |
| `test_phase15_/16_/17_*` | 32 | peer_name sanitize + structural ListView |
| `test_phase17_1_yaml_blacklist_metrics` | 8 | yaml 热加载 + reject metrics |
| `test_phase18_persistence_summary` | 12 | persistence + daily_summary v1 |
| `test_phase19_*` (3 文件) | 36 | trend/alerts/region + 7d + alert state |
| `test_phase20_1_referral_replies` | 46 | referral 调度 + region 路由 + latency |
| `test_phase20_1_9_alert_history` | 17 | alert history 表 + per-region + anomaly |
| `test_phase20_2_stale_recycle` | 25 | mark stale + revive + endpoint |
| `test_phase20_3_e2e_full_loop` | 12 | e2e 完整闭环 (FakeBMessenger) |

---

## 8. 关键设计决策

### 8.1 关键词路由 — A 侧负责

B 只抓"对方说了什么", A 负责关键词识别 + 写 event。原因:
- yaml 字典在 A 侧热加载, B 不需重启
- 多语言 / region 选词逻辑 A 侧处理, B 侧专注 UI 抓取
- A 侧 `_match_referral_keyword(text, region)` 已有完整单测覆盖

### 8.2 自动 region 路由 (Phase 20.1.7.1)

per-peer canonical → region 三级 fallback:
1. `metadata.region` 直接字段
2. persona_key 前缀 (`jp_*` → `jp`)
3. dispatch_log JOIN line_account.region

5min TTL 缓存 (Phase 20.1.8.1) 减少重复 SQL: 30min cron × 50 peers
从 50 SQL/小时降到 10 SQL/小时。

### 8.3 事件常量 owner (PR #80 风险 1, 待 B 拍板)

`CONTACT_EVT_*` 严格按 INTEGRATION_CONTRACT 是 B owner。Phase 20.1 / 20.2
A 侧加了 2 个新常量 (`WA_REFERRAL_REPLIED` + `REFERRAL_STALE`), 已 ping B
在 PR #72 评论确认。

### 8.4 Cooldown state_key 含 region 后缀 (Phase 20.1.9.2)

避免 jp 和 it 的 `replied_rate_low` 共用一个 cooldown 互相抑制:
- state_key = `type` + (`:region` if region 非空)
- `replied_rate_low:jp` 与 `replied_rate_low:it` 独立 cooldown

### 8.5 Stale 自动复活 hook (Phase 20.2.x.1)

在 `record_contact_event` 末尾 gate by `event_type == wa_referral_replied`,
触发 `_maybe_revive_stale_on_reply(peer_name)` 移除 referral_stale tag。
其他高频事件 (greeting_sent / wa_referral_sent) 不触发, 性能 OK。

### 8.6 SLA lookback buffer (Phase 20.2)

`mark_stale_referrals` 的 lookback = `max(stale_hours, escalate_days*24*2)+1`,
即 escalation 阈值的 2 倍。防止 8 天前 sent 的 peer 漏网 (escalate_days=7
情形)。

---

## 9. 已知约束 / 待 B 拍板

PR #80 (A-main sibling review) + 本 commit 处置:

| # | 风险 | A 处置 |
|---|---|---|
| 1 | CONTACT_EVT_* 越界 | 2 个新常量待 B 接受/反对/重命名 |
| 2 | fb_store.py 注释矛盾 | ✅ 自修 (commit f50d2c4) |
| 3 | check_messenger_inbox 4 新参数命名 + auto_reply 互斥 | 待 B 拍板, A 30min 同步 spec |
| 4 | meta.platform 缺失 | ✅ 自修, 已加 platform="facebook" |
| 5 | TG journey_events.first_text_received 跨 repo 重叠 | 跨 repo 设计, 建议 BI 按 (canonical_id, platform) 去重 |

---

## 10. 联调步骤 (B 实装后)

```bash
# Step 1: B 实装 check_messenger_inbox(referral_mode=True, ...)
# Step 2: 跑 e2e 验证
pytest tests/test_phase20_3_e2e_full_loop.py -v
# 把 FakeBMessenger 替换为真 FacebookAutomation 后, 12 cases 全 pass

# Step 3: 启 cron (默认 disabled, 等联调 OK)
# config/scheduled_jobs.json:
#   "check_referral_replies_15min": "enabled": true
#   "mark_stale_referrals_daily":   "enabled": true

# Step 4: 重启 server
taskkill /F /PID <pid> && python server.py &

# Step 5: 观察
curl http://127.0.0.1:18080/line-pool/stats/referral-funnel?hours_window=72
# 期望: replied > 0, conversion_rate > 0
curl http://127.0.0.1:18080/line-pool/stats/alert-history?hours_window=168
# 期望: by_type 含历史 alert 分布
```

---

## 11. Phase 21+ 候选

A 侧已完成 referral 闭环基础设施。后续候选 (按价值排序):

| Phase | 主题 | 预估 |
|---|---|---|
| 21 | 文案 A/B 实验框架 — multi-template + reply_rate per template | 3-4h |
| 22 | 运营 Web Dashboard — 整合 stats endpoints | 2-3h |
| 20.4 | HTTP 端到端测试 — TestClient 跑 cron 集成 | 1.5h |
| 20.5 | per-region SLA 阈值 (jp 48h vs it 24h 等) | 1h |
| 20.6 | latency anomaly per-region z-score | 1.5h |

A 不主动启, 等用户 / B 拍板。

---

— A side, 2026-04-25
