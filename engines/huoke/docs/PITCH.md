# OpenClaw — 跨境社媒客服自动化平台

> Phone-based RPA + AI 决策栈，让 1 个客服管 30 个 Facebook/Messenger/LINE 账号

---

## 一句话定位

**给跨境电商 / 海外品牌的客户增长团队一套 SaaS 工具**——用真机集群跑 FB/Messenger/LINE 客户跟进自动化，AI 帮客服判断"什么时候发什么消息给谁"，**人工最终把关**。

---

## 解决什么问题

跨境电商企业卖到日本/东南亚，客户跟进的痛：

| 痛点 | 现状 | OpenClaw 后 |
|---|---|---|
| 1 客服只能管 2-3 个账号 | 手机切来切去, 易封号 | 1 个 dashboard 管 30+ 账号 |
| 客户分散在 FB/Messenger/LINE | 切不同 App 漏消息 | 统一收件箱 + 待人工接管队列 |
| 不知道哪个客户该主动跟进 | 凭感觉, 漏单 | AI 算 readiness 0-1, Top 10 主动出击面板 |
| 多账号不一致回复 | 客服个人发挥, 转化率波动 | A/B 实验 + 推荐回复模板, 数据驱动 |
| 加好友/打招呼时机踩反风控 | 凭运气, 经常封号 | 多层 gate (phase/cooldown/keyword/emotion) 自动反风控 |
| 老板不知道客服效率 | Excel 月报手工拼 | SLA 实时看板, 接管时间/转化率/客服 KPI |

---

## 核心能力 (13 个 Phase 累积)

### 1. 集群调度
- 1 主控 + N worker, 每 worker 接 N 部手机
- 跨设备并发任务派发 (实测 50 并发 P95 < 3 秒)
- 自动设备故障切换 + 状态回报

### 2. 智能决策栈
- **referral_gate 4 层**: hard_block (拒绝词/冷却) → hard_allow (主动要) → 多维 LLM 判定 → soft 综合打分
- **emotion 多维**: trust / interest / frustration / topic_match (qwen2.5 LLM)
- **A/B 实验自动化**: variants 流量分配 + winner graduate 自动启新实验

### 3. 客户全生命周期画像
- L1 (worker SQLite) + L2 (中央 PG) 双写架构, 抗断网
- canonical_id (UUIDv5 worker 端预算) 跨设备客户 dedup
- 关键事件全 append-only: friend_request / greeting / message / referral / handoff / convert
- 自定义标签 + 客户视图保存

### 4. 运营看板 (B2B 客户日常)
- 6 stat cards + sparklines (客户分桶 / 7 天趋势)
- 待人工接管队列 (⏰ 红色超时 SLA breach)
- Top 高优先级 / 高 frustration 主动出击面板
- 引流决策分析 4 块 (level 分布 / 评分对比 / 30 天时序 / Top reasons)
- SLA 看板 (按客服 / 按 A/B variant)
- 任务失败原因分析 (新, 帮 admin 调试)
- 客户旅程时间线 (modal 内 SVG)
- LLM 客户洞察 (urgent / concerns / readiness / 推荐回复模板)

### 5. 客服质检 + 数据驱动
- chat_review CLI: 5 min/天过 20 条对话, 标 good/bad
- weekly_report: 7 天漏斗趋势 (ASCII sparkline) + 启发式调参建议
- daily_snapshot: 24 列指标 append-only CSV

### 6. 故障运维
- e2e_smoke: 29 stages 端到端验证, 每次重启必跑
- slo_check: 7 项红线检查, cron 每 5 min, 红线 webhook 推钉钉/飞书/Slack
- 设备网络/SIM 诊断: 自动定位 captive portal / 飞行模式 / VPN

---

## 技术差异化

### vs Drift / Intercom (B2C SaaS)
- 它们只接 web/email, OpenClaw **直接驱动手机** (FB/Messenger/LINE 等无 API 平台)
- 它们 LLM 是 chat widget, OpenClaw **决策栈可解释** (raw_readiness vs consensus 共识降级, reasons 数组)

### vs HubSpot Service Hub
- 它们贵 $45/seat ×多 seat, OpenClaw 按 **device 数定价** $99/device/月
- 它们 30 用户起跑, OpenClaw 1 个客服管 30 设备

### vs 国内竞品 (微信群控)
- 它们只懂微信, OpenClaw **跨平台 + 反风控决策栈**
- 它们没 LLM 洞察, OpenClaw **emotion 4 维 + readiness consensus**

---

## 部署模式

| 模式 | 适用 | 价格 (示例) |
|---|---|---|
| 私有部署 (自有服务器) | 100+ device 大客户 | 一次性 $5K + 每年维护 $2K |
| 私有云 (托管在客户 VPC) | 30-100 device 中型 | $99/device/月 起 |
| SaaS (multi-tenant, 计划中) | 5-30 device 小型 | $79/device/月 起 |

---

## 早期客户画像

✅ **菲律宾跨境电商卖家** — Lazada/Shopee 的 FB 客户跟进
✅ **日本本地中小商家** — LINE 业务客户增长
✅ **跨境美妆/服装** — 多账号 Messenger DM 自动化
❌ **C 端社交诈骗 / 情感欺诈** — 不卖, 用户协议拒绝

---

## 实测案例 (W175 cluster, 7 天)

- 3 设备 × 7 天
- 加好友请求发出: **40**
- 打招呼消息发出: **33**
- 客户回复: **153** (回复率 79%)
- 出站消息: **150** (含多轮聊天)
- 引流到 LINE: **2**
- 真人接管: **4**
- 转化: **4**
- **整体转化率: 4/40 = 10%** (跨境电商 SCRM 行业中上水平)

详见 [CASE_STUDY.md](CASE_STUDY.md)。

---

## 试用流程

1. NDA + 试用合同签订 (3 个月免费)
2. 客户提供 1 台主控服务器 + N 个 worker + N 个手机 (有自己合法获取的 FB/Messenger/LINE 账号)
3. 我们派工程师 1 天上门 / 远程部署 (跑 e2e_smoke 全绿)
4. 客户运维 + 我们每周 review 数据
5. 3 个月后转付费或不付费 (无义务)

---

## 接洽

- Email: contact@openclaw.example
- Demo: https://demo.openclaw.example (限时申请)
- GitHub (源代码): private
- 详细技术文档: [INSTALL.md](INSTALL.md), [OPS_RUNBOOK.md](OPS_RUNBOOK.md)

---

## 用户协议要点 (B2B 必读)

1. 客户**自己负责**: 账号合法获取 (不买黑卡), 操作合规 (符合目标地区法律 / 平台 ToS)
2. AI 生成内容**人工最终把关**: 所有自动发出的消息可在 dashboard 查到完整 trace
3. 客户**承担风控风险**: 因违反平台 ToS 导致账号封禁与我方无关
4. 数据**完全隔离** (private deploy / 多租户): 我方不访问客户业务数据
5. 不卖给 C 端用户做"假身份社交诈骗"等灰产场景
