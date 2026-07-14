# 客户 Onboarding Checklist — 第 1 个真实客户来了, 第 1 周做什么

> 给签了 PoC 合同的早期客户做交付前检查 + 落地引导.
> 时间预算: 1 工程日 (远程协助) + 客户运营 1 周.

---

## Day 0 (合同签订日, 签约后 24h 内)

### 文档移交
- [ ] 给客户发 docs/INSTALL.md (部署文档)
- [ ] 给客户发 docs/OPS_RUNBOOK.md (运维异常处置)
- [ ] 给客户发 docs/PITCH.md + CASE_STUDY.md (产品理念 + 内测数据)
- [ ] NDA + PoC 合同副本归档 (你方 + 客户方)
- [ ] 创建客户专属 Slack/Telegram 群 / 微信群 (PoC 期间日常沟通)

### 客户需要准备的 (PoC 期间客户负责)
- [ ] **5+ 部 Android 11+ 手机** (推荐 Xiaomi / Samsung 中端机)
- [ ] **5+ 个合法获取的 FB/Messenger/LINE 账号** (客户自有, 经过冷启动养号 ≥ 7 天)
- [ ] **5+ 张 SIM 卡 + 数据流量** (推荐目标地区本地运营商)
- [ ] **1 台 Windows 11 / Ubuntu 22 服务器** (主控, 8 核 16GB SSD 50GB+)
- [ ] **稳定 WiFi (必须)** (设备避免依赖蜂窝)
- [ ] **VPN 配置** (V2RayNG + 目标地区节点; 我们提供推荐供应商)
- [ ] **预留 1 名运营/客服员工** PoC 期间专门负责使用 dashboard

### 项目预启动会 (Zoom 30 min)
- [ ] 客户业务介绍: 当前流程 / 痛点 / 期望结果
- [ ] 我方产品 demo (15 min, dashboard 走一遍)
- [ ] PoC 时间线 + 里程碑确认
- [ ] PoC 成功标准: 30/60/90 天 CVR / 接管时间 / 客服效率
- [ ] 数据隐私 + 退出条款再次 walk through

---

## Day 1 (上门或远程, 5h)

### Step 1: 服务器环境 (1.5h)
- [ ] PostgreSQL 16 安装 (`docs/INSTALL.md §1.1`)
- [ ] **PG `lc_messages` 永久 fix** (Chinese Windows 必做)
  ```bash
  PGPASSWORD=$SU psql -h 127.0.0.1 -U postgres -d openclaw \
    -c "ALTER ROLE openclaw_app SET lc_messages='C';"
  ```
- [ ] 创建 openclaw + openclaw_test database
- [ ] 应用 migrations 001-003
- [ ] .env 配置 (DB 凭证 / OPENCLAW_PORT=8000 / 可选 webhook)
- [ ] Python 3.13 + requirements.txt
- [ ] `python server.py` 启动 + 验证 /health

### Step 2: e2e_smoke 全绿 (30 min)
- [ ] 跑 `python scripts/e2e_smoke.py --base http://127.0.0.1:8000`
- [ ] 必须 **29 stages 全 passed**, 失败任何一项停下排查
- [ ] 失败常见: PG `lc_messages` 没设 / 端口被占 / migrations 没全应用

### Step 3: Worker 节点部署 (1h)
- [ ] Worker 装 Python 3.13 + ADB platform-tools
- [ ] 运行 OTA 升级脚本 `python scripts/ota_pull.py --coord http://<COORD>:8000`
- [ ] 配 cluster.yaml (role: worker, coordinator_url, host_id)
- [ ] **`run_server.bat` 必须含 `set OPENCLAW_PORT=8000`** (新代码默认 18080 会被防火墙拦)
- [ ] schtasks /Create 自启 worker
- [ ] 验证: 主控 `curl /cluster/devices` 看到 worker host

### Step 4: Android 设备激活 (1.5h, 5 设备并行)
- [ ] 设备 USB debugging on
- [ ] `adb devices` 全部 device (非 unauthorized)
- [ ] 装 ADBKeyboard / 跑 `scripts/unify_ime.py` 统一输入法 (中日文支持)
- [ ] MIUI 弹窗禁用 `scripts/disable_miui_security_popups.py`
- [ ] 装 V2RayNG + 导入客户 VPN 配置
- [ ] V2RayNG **手动**点连接, 验证 `adb -s <D> shell curl https://ipinfo.io/json`
      country 是目标地区
- [ ] 装/确认 FB Katana + Messenger, 客户登录自己的账号 (运营手动)
- [ ] 设备运行 24h+ 浏览 feed (cold_start 养号阶段)

### Step 5: 客服 dashboard 培训 (30 min)
- [ ] 给客户运营员工开账号 (admin/agent role)
- [ ] dashboard 13 个面板 walkthrough
- [ ] chat_review CLI 演示 (5 min/天)
- [ ] weekly_report 自动出周报介绍

---

## Day 2-7 (客户运营, 我方周一/周五各 30 min check)

### Week 1 客户每天做
- [ ] 早 9 点登 dashboard 看 ⏰ 红色超时 / refer rate 健康度
- [ ] 上午 / 下午 各 1 次 chat_review CLI (5 min × 2)
- [ ] 下午 / 晚上 各 1 次 dashboard 看"📥 待人工接管队列"
- [ ] 见客户主动来消息 → "🙋 我接手" 接管 → 切手机回真消息
- [ ] 周日 30 min 看 weekly_report, 给我方反馈

### Week 1 我方监控
- [ ] **每天**: SLO red line check (slo_check 自动 5 min cron + webhook)
- [ ] **周二**: 看客户 daily_snapshot CSV, 24 列指标看趋势
- [ ] **周五**: 30 min Zoom check-in (客户体验 / 数据 / 问题)

### Week 1 关键指标 baseline
- [ ] 加好友通过率 (friend_request_sent / accepted)
- [ ] 第一条消息回复率 (message_received / greeting_sent)
- [ ] 多轮聊天率 (≥ 5 轮的 customer 占比)
- [ ] AI 决策准确率 (chat_review 标 good / total reviewed)
- [ ] 客服平均响应时间 (handoff_breach_30min count)

**第 1 周不调任何参数, 只采基线**.

---

## Day 8-30 (Week 2-4, 数据驱动调参)

### Week 2: 看 baseline 找瓶颈
基于 Week 1 数据找漏斗最差的 1 环:
- 加好友通过率 < 50% → FB 反风控触发, 检查 phase 配置 / IP 质量
- 回复率 < 30% → greeting 话术差, 改 chat_brain prompt
- 多轮率 < 50% → bot 第 2 轮就破功, review chat_review CSV 标 bad 的对话
- 客服响应慢 → 加客服 / 调 SLA 阈值

### Week 3-4: 定向优化
**只动 1 个 yaml 阈值或 1 段 prompt** (hot reload 不重启).
观察 1 周看是否真改善.

---

## 30 / 60 / 90 天里程碑

### Day 30 review
- [ ] 1 个月真实数据: 加好友 N / 引流 N / 转化 N / 拉黑 N
- [ ] 客户体验调研 (5 个问题)
- [ ] 调整: 设备数 / phase / 阈值
- [ ] **决定: 继续 PoC / 加大预算 / 终止**

### Day 60 review
- [ ] 整体 CVR 趋势 (周环比)
- [ ] 客服效率: 1 客服管多少账号
- [ ] LLM 调用成本 vs 客户预估付费
- [ ] 准备 case study (匿名版)

### Day 90 (PoC 结束)
- [ ] 完整 90 天数据报告 + ROI 计算
- [ ] **决定: 转付费 / 续 PoC / 不签**
- [ ] 不签也要拿客户 honest feedback (产品改进金矿)
- [ ] 签的话, 转 standard 计价 ($99/device/month)

---

## 风险 + 应急

### 客户账号被 FB 封了
- 立即停所有自动化任务 (停 worker)
- 手机端检查具体封禁原因 (review / 客户挑战)
- 帮客户走 FB appeal 流程
- 24h 内全员养号 phase 重启
- **如客户多账号同时封, 暂停 PoC 1 周排查**

### 客户数据保护
- 客户数据完全在客户自己服务器 (private deploy)
- 我方 ssh 进客户服务器需要客户**明确授权 + 录屏**
- 不下载客户客户名单到本地

### 客户中途想退出
- 任何时间客户 30 天通知期可以退出
- 不退还 PoC 期间已发生的部署 / engineering 成本
- 帮客户备份 + 删除所有 OpenClaw 数据 + 断开远程访问

---

## 联系点 + 升级路径

| 场景 | 联系人 | 响应时间 |
|---|---|---|
| dashboard 503 / 关键 bug | victor + on-call eng | 30 min |
| 数据 anomaly (CVR 突降) | victor | 4h |
| 客户 FB 账号封禁 | victor + 我方专家 | 1 个工作日 |
| 一般使用问题 | 客户群 / 文档 FAQ | 24h |

---

## PoC 成功 = 转付费的判断标准

✅ **签付费**:
- 整体 CVR ≥ 8% (本案例 baseline 10%)
- 客户主观打分 ≥ 7/10
- 客服效率比 PoC 前 ≥ 2x
- 设备 / 账号 90 天 0 封禁
- 客户主动续约 (不催)

❌ **不签**:
- CVR ≤ 5% 持续 30 天
- 多次 FB 账号封禁 (产品风险评估失败)
- 客户客服员工普遍说"用不动"
- 我方支持成本超过订阅费 (单位经济不合)

---

## 我方准备的 PoC 资源

- 30 天: victor 70% 时间投入 (技术 + 销售)
- 60 天: 50% 时间 (主要客户支持)
- 90 天: 30% 时间 (review + 转化)

> 1 个 PoC 客户对你的真实成本 ≈ 30 工时 + LLM API 费 (~$200/月). 如果转付费 $99 × 5 device × 12 月 = $5,940 ARR. 投入产出 1:5+. 找 3-5 个 PoC 即可有 first 1-2 paying.

---

## 相关文档

- docs/INSTALL.md - 部署 SOP
- docs/OPS_RUNBOOK.md - 运维异常处置
- docs/PITCH.md - 产品介绍
- docs/CASE_STUDY.md - 内测案例数据
- docs/MULTI_TENANT_DESIGN.md - 多租户设计 (第 2 客户来时实施)
- docs/OUTBOUND_TEMPLATES.md - 销售模板
