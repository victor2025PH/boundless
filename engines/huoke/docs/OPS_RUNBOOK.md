# OPS RUNBOOK — mobile-auto0423 运营操作手册

> 用途: Phase 1-13 已建好的功能"跑起来"的实操指南。
> 目标读者: victor 自己, 以及未来扩团队时的运营。
> 不是设计文档 (那看 INTEGRATION_CONTRACT.md / SYSTEM_ARCHITECTURE.md);
> 这是"日常做什么"的 checklist。

---

## 0. 一次性安装步骤 (新机器才需要)

### 0.1 PG 用户 lc_messages 修复 (Chinese Windows 必做)

中文 Windows 的 PG 默认错误消息是 GBK 编码, psycopg2 解码会崩。需要 superuser 一次性运行:

```bash
PGPASSWORD=<superuser_pw> psql -h 127.0.0.1 -U postgres -d openclaw \
  -c "ALTER ROLE openclaw_app SET lc_messages='C';"
```

验证:

```bash
PGPASSWORD=<openclaw_app_pw> psql -h 127.0.0.1 -U openclaw_app -d openclaw \
  -c "SHOW lc_messages;"   # 应输出 C
```

不做的后果: dashboard 查询时随机出现 503 "central store unavailable: 'utf-8' codec can't decode byte 0xd6"。

### 0.2 .env 必填字段

```
OPENCLAW_PG_HOST=127.0.0.1
OPENCLAW_PG_PORT=5432
OPENCLAW_PG_DB=openclaw
OPENCLAW_PG_USER=openclaw_app
OPENCLAW_PG_PASSWORD=<填上>
# OPENCLAW_API_KEY=<可选, 设了就启用 API key 鉴权>
# OPENCLAW_NOTIFY_WEBHOOK=<可选, 钉钉/feishu/slack URL>
# OPENCLAW_NOTIFY_TYPE=generic | dingtalk | feishu | slack
```

### 0.3 数据库迁移

```bash
# 应用所有 migrations/ 下的 SQL 到 openclaw + openclaw_test
PGPASSWORD=$OPENCLAW_PG_PASSWORD psql -h 127.0.0.1 -U openclaw_app -d openclaw \
  -f migrations/001_central_customer_schema.sql
# ...每个 002_phase7_ab_views.sql / 003_phase7_trgm_search.sql 同样
```

---

## 1. 日常启动 (每天 1 次, 早上)

### 1.1 启动主控

```bash
# 方式 A: service_wrapper (推荐, 自动拉起 + 自动更新)
python service_wrapper.py

# 方式 B: 直接 server.py
python server.py
```

健康检查:

```bash
curl -s http://127.0.0.1:8000/health | jq .status   # 应返 "ok"
```

### 1.2 30 phones 上线确认

```bash
adb devices                # 预期 30 行 device
```

dashboard 看: <http://127.0.0.1:8000/dashboard> 设备总数与上面一致。

### 1.3 启动批量任务

(根据具体 SOP 略, 这里是真业务跑起来的入口)

---

## 2. 每天监控 (每 2 小时刷一次 dashboard)

### 2.1 看 L3 dashboard <http://127.0.0.1:8000/static/l2-dashboard.html>

**6 个 stat card (上方)**:
- 客户总数 / in_messenger / in_line / accepted / 已转化 / 流失
- 旁边 sparkline 是 7 天趋势

**关键看板**:
- 📥 待人工接管队列: 看 ⏰ 红色超时 pill 数, 应该 ≤ 2; > 5 触发 webhook 报警
- 🔥 高优先级 Top 10 / 😣 高 frustration Top 10: 主动跟进
- 📊 引流决策分析: refer 率应在 15-25% 绿色健康带, 偏离即调阈值
- 🏆 SLA 看板: 客服响应时间应 < 15 min 绿色

### 2.2 异常红线 (出现立刻处置)

| 现象 | 含义 | 处置 |
|---|---|---|
| dashboard 多个面板"加载失败 HTTP 503" | central_store 出问题 | 重启 server.py |
| 待接管队列 ≥ 5 超时 | 客服没盯盘 | 手动接管 / 加客服 |
| refer 率长期 > 30% | 引流过激 | 调高 `early_refer_readiness` 或 `min_emotion_score` |
| refer 率长期 < 5% | 引流过保守 | 调低 `delay_refer_readiness` |
| push 失败率 > 30% | 网络 / coordinator 问题 | 看 `push_metrics` + 检查 PG / 网络 |
| 拉黑率 > 5% (从 fb_store 看) | bot 太机械 / 引流太早 | 优化 chat_brain / 调 referral_gate |

---

## 3. 调阈值 (referral_strategies.yaml hot reload, 不用重启)

```yaml
# config/referral_strategies.yaml
jp_female_midlife:
  min_turns: 7              # 最少几轮才能引流
  refer_cooldown_hours: 1   # 同客户引流冷却
  rejection_cooldown_days: 7
  min_emotion_score: 0.5    # 情感分门槛
  max_frustration: 0.5      # frustration > 此值不引
  early_refer_readiness: 0.8  # readiness ≥ 此值 + turns ≥ early_refer_min_turns → 早引流
  early_refer_min_turns: 5
  delay_refer_readiness: 0.3  # readiness ≤ 此值 + turns ≤ delay_refer_max_turns → 延后
  delay_refer_max_turns: 10
```

修改文件保存后 30 worker 自动 reload (Phase-11 hot reload)。

**调参建议**:
- 拉黑率高 → `min_emotion_score 0.5 → 0.6`, `max_frustration 0.5 → 0.4`
- 引流率太低 → `min_turns 7 → 5`, `early_refer_readiness 0.8 → 0.7`
- 看完决策 aggregate 看板的 Top 触发原因, 把高频 hard_block 原因拿出来分析

---

## 4. 每天落 daily snapshot (cron)

```bash
# 凌晨 3 点跑 (Linux cron)
0 3 * * * cd /path/to/repo && python scripts/daily_snapshot.py >> logs/snapshot.log 2>&1

# Windows Task Scheduler
schtasks /Create /SC DAILY /ST 03:00 /TN OpenClawSnapshot \
  /TR "python D:\workspace\mobile-auto0423\scripts\daily_snapshot.py"
```

跑完看: `reports/daily_snapshot.csv` (每天 1 行)

1 周后用 Excel 打开看趋势, 找漏斗最差的环节定向优化。

---

## 5. e2e smoke test (每次重启后跑一次)

```bash
python scripts/e2e_smoke.py --base http://127.0.0.1:18080
```

**预期**: 12 stages × 29 assertions 全过, 0 failed. 失败任何一项不要继续跑真实流量, 先修。

## 5.1 SLO 红线检查 (cron 每 5 min)

```bash
python scripts/slo_check.py --webhook  # 红线 + 黄线时推 webhook
python scripts/slo_check.py --json     # 机器可读
```

7 个红线检查: health / central_store / handoff_breach / push_fail_rate /
refer_rate / daily_friend_request / sla_response

cron 配置 (Linux):
```bash
*/5 * * * * cd /path && python scripts/slo_check.py --webhook >> logs/slo.log
```

Windows Task Scheduler:
```powershell
schtasks /Create /SC MINUTE /MO 5 /TN OpenClawSLO ^
  /TR "python D:\workspace\mobile-auto0423\scripts\slo_check.py --webhook"
```

## 5.2 Chat review (每天 5 min)

```bash
python scripts/chat_review.py --base http://127.0.0.1:18080 --limit 20
```

交互快捷键 `g`/`b`/`s`/`n`/`p`/`q`. 输出 `reports/chat_review_YYYYMMDD.csv`.

每天目标 review **20 条**, 标 bad 的客户 1 周后聚类找规律 → 优化 chat_brain
prompt / 调阈值。

## 5.3 周报 (每周日)

```bash
python scripts/weekly_report.py             # 自动出 reports/weekly_YYYYMMDD.md
python scripts/weekly_report.py --print     # 终端预览
```

含 7 天漏斗趋势 (ASCII sparkline) + 客户分桶 + 决策 SLA + **启发式调参建议**。

---

## 6. 异常处置常见问题 (FAQ)

### Q1: 30 phones 启动时输入法 / MIUI 弹窗
- 跑 `python scripts/disable_miui_security_popups.py` (一次性禁用)
- 跑 `python scripts/unify_ime.py` (统一 ADBKeyboard, 让中文输入)

### Q2: PG 503 "central store unavailable"
1. `curl /health` 看是不是其他面板也挂
2. 是 → 重启 `server.py` (singleton 已坏, 重启重建 pool)
3. 不是 → 看具体 503 详情, 可能是 lc_messages 没设 (回 0.1)

### Q3: A/B 实验为什么没自动 graduate?
- 需要实验跑 ≥ 7 天 + winner 已 graduated + 距上次 graduate ≥ 7 天
- 看 dashboard "🧪 当前 A/B 实验" 面板, 应有 winner 标识
- admin 可手动点 "🎓 graduate 当前 · 启新实验" 按钮 (绕开自动判定)

### Q4: webhook 通知不收到
- 检查 `OPENCLAW_NOTIFY_WEBHOOK` env 是否设
- 检查 `OPENCLAW_NOTIFY_TYPE` (generic / slack / dingtalk / feishu)
- 5 min 冷却内同类事件不重发, 等等再看

### Q5: 决策看板 refer 率持续异常
- 先确认有真实 referral_decision 事件 (drill 模态点击具体 reason)
- 看 raw_readiness vs 共识 readiness 是否经常不一致 (LLM 不稳定)
- 不一致频繁 → 可能 LLM 服务不稳, 检查 ollama / Anthropic
- 一致但比例异常 → 调阈值 (回 §3)

---

## 7. 每周复盘 (周日)

1. 拉 `reports/daily_snapshot.csv` 7 行数据看趋势
2. dashboard "📊 引流决策分析" 看 30 天 refer 率时序
3. dashboard "🏆 SLA 看板" 看每个客服响应/转化时间
4. 决定下周优化方向 (调阈值 / 加客服 / 改话术 / 扩 phone 数)

**不要在没真实数据时凭直觉调代码**。

---

## 文档版本

- v1.0 (2026-04-26): Phase-13 完成后初版, 落地"运营起跑"模式
