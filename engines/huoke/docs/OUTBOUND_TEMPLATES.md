# Outbound 销售外联模板

> 给 victor 用来找第一批 B2B 早期客户.
> 5 个模板 × 不同 channel × 不同 ICP. 每个含个性化变量 `{{...}}`.

---

## 模板 1 — 菲律宾跨境电商卖家 (英文邮件)

**Subject**: 1 客服管 30 个 FB 账号, 不封号

```
Hi {{first_name}},

I noticed {{company}} has been growing on Lazada/Shopee in PH market.
A pain point I keep hearing from cross-border sellers like yours: customer
follow-up across multiple FB accounts is killing your team's productivity,
and switching SIMs/phones manually risks Meta's anti-spam.

We built OpenClaw — a phone-based RPA stack that lets 1 customer service rep
manage 30+ FB/Messenger/LINE accounts from a single dashboard. AI scores
customer "readiness" so reps focus on hot leads, and the system auto-handles
add_friend / greeting / inbox monitoring while keeping humans in the loop
for actual replies (we are NOT a fake-AI scam tool).

Recent internal test: 7 days, 4 transitions on 40 friend requests = 10% E2E
conversion rate. I'd love to show you the dashboard and discuss a free 3-month
PoC if your team has bandwidth.

15-min call this Tuesday or Thursday?

Best,
{{victor_name}}
{{contact_email}}

P.S. Full pitch deck (markdown): [PITCH.md link]
```

**发送场景**: 找 Lazada/Shopee PH 卖家的 LinkedIn marketing manager / customer
service manager. 用 Apollo / Hunter 找 email.

---

## 模板 2 — 日本 LINE 商家 (LinkedIn 私信, 日文)

**Subject**: 1 人で 30 LINE アカウントを管理する SCRM ツールのご提案

```
{{お名前}}様

突然のご連絡失礼いたします。
{{会社名}}様の LINE 公式アカウント運用拝見させていただきました。

中小規模で複数の LINE/Facebook/Messenger アカウントを並行運用される際、
担当者の方が一日中スマホを切り替えるオペレーションになっていませんか。

OpenClaw は 1 人の担当者が 30+ アカウントを一括管理できる RPA + AI ツール
です。AI が顧客の「興味度合い」を 0-1 スコアでリアルタイム判定し、担当者は
ホットなお客様に集中。AI が下書きを提案するので、最終送信は必ず担当者が
判断します(完全自動 bot は提供しません)。

弊社内テスト: 7 日間 / 40 友達リクエスト → 4 件成約 (CV 10%)。

3 ヶ月の無料 PoC をご提供しております。お時間ある際に 15 分ほど Zoom で
ダッシュボードのデモをお見せできれば幸いです。

何卒よろしくお願いいたします。

{{victor_name}}
{{contact_email}}
```

**发送场景**: 日本中小企业的 LINE 公式账号管理者。LinkedIn 搜
"LINE Marketing" / "SCRM" / "顧客管理" tags.

---

## 模板 3 — 跨境美妆/服装 (中文邮件)

**Subject**: 跨境美妆 SCRM: 30 设备客户跟进自动化, 月省 3 个客服

```
{{先生/女士}} 您好,

打扰了。我看到 {{品牌名}} 在东南亚/日韩市场做得不错, 想问下您的客服团队
当前是如何管理多账号客户跟进的?

我们做的 OpenClaw 是一套针对跨境品牌的客户跟进自动化平台:
- 1 客服 → 30+ FB/Messenger/LINE 账号同时跟
- AI 实时算客户"成熟度", Top 10 主动跟进面板
- AI 推荐回复话术, 客服一键复用 (人工最终决定发什么)
- 完整客服 KPI 看板 (响应时间 / 转化率 / 失败原因分析)

7 天内部测试 (3 设备, 日本市场): 40 加好友 → 4 成交 = 10% CV。

提供 3 个月免费 PoC, 客户自带账号. 您方便下周聊 15 分钟吗?

{{victor_name}}
{{contact_email}}
```

**发送场景**: 跨境品牌的运营总监 / CRM 主管. 钉钉群 / 跨境出海社区找到联系方式.

---

## 模板 4 — 已用其他 SCRM 客户 (英文 LinkedIn InMail)

**Subject**: Switching from {{Drift / Intercom / HubSpot}} to phone-based SCRM?

```
Hi {{first_name}},

Saw {{company}} uses {{competitor_tool}} for customer engagement. Quick
question: how do you handle multi-account FB/Messenger/LINE customer
follow-up? Most web-chat SaaS tools (Drift, Intercom) don't drive native
mobile apps.

We built OpenClaw — phone-based RPA + AI decisioning. Real Android phones,
real FB accounts, AI-augmented (not replaced) human reps.

Differentiation:
- vs Drift/Intercom: drives Android phones (FB/LINE/Messenger), not just web
- vs HubSpot Service: $99/device/mo (not per-seat), 1 rep manages 30+ accounts
- vs Chinese 微信群控: cross-platform + LLM decision stack with full traceability

Open to a quick demo? I'd love your honest feedback even if we're not a fit.

Best,
{{victor_name}}
```

**发送场景**: 已 invest 客户增长工具的中型客户. LinkedIn 看 Sales Navigator
"Marketing technology" filters.

---

## 模板 5 — 跨境 SaaS Slack 群 / Telegram 群冷启动

**Subject (Slack/TG post)**: [open] PoC partner wanted — phone-based FB/LINE customer follow-up

```
Hey 👋

Building OpenClaw — RPA + AI tool for managing 30+ FB/Messenger/LINE
accounts from a single dashboard. Hit a milestone last week: real e2e
conversion rate of 10% on 40 friend requests in 7 days (Japan market).

Looking for 1-2 cross-border e-com sellers (PH/JP/KR/East Asia) for
3-month free PoC. You bring:
- Your own legit FB/Messenger/LINE accounts (we don't sell black-market accts)
- 5+ Android devices on stable WiFi/SIM
- Willingness to share data for case study (NDA mutual)

You get:
- Full private deployment of OpenClaw stack (not SaaS)
- Hands-on engineering support during PoC
- $0 license fee for 3 months

DM if interested. Happy to share the pitch deck and tech architecture in
private. Thanks!
```

**发送场景**: 跨境出海 Slack / Telegram 群 (eg "出海派"/"BG 创业群"/"DTC 论坛").

---

## 发送节奏 + 跟进策略

### Week 1: 冷邮件批量发 (50 封)
- 模板 1 → 30 封 (PH 跨境电商)
- 模板 2 → 10 封 (日本 LINE)
- 模板 3 → 10 封 (中文跨境品牌)
- 跟进: 7 天后没回的发模板 4 (差异化)

### Week 2-3: LinkedIn outreach (50 InMail)
- 模板 4 给已用 SCRM 工具的客户

### Week 1+: 社区/群发布 (3-5 个群)
- 模板 5

### 期望转化漏斗
- 100 outbound → 10% 回复 = 10 dialogue
- 10 dialogue → 30% demo = 3 demo
- 3 demo → 30% PoC sign = 1 PoC client
- 1 PoC client × 3 months → 1 paying customer

---

## ICP (Ideal Customer Profile)

✅ **优先**:
- 跨境 e-com 卖家 (年 $500K-5M GMV) - PH / JP / 东南亚
- 日本中小 LINE 公式账号 (粉丝 1K-50K)
- DTC 美妆/服装 (亚太市场)

❌ **不要**:
- 灰产 (情感诈骗 / 投资骗局 / 虚拟币推销)
- B2C 直接消费者
- 大企业 (>$50M, 销售周期太长)

---

## 跟进话术 (客户回复后)

### 客户问 "How does it differ from {{competitor}}?"
> "{{Competitor}} drives web-only chat. We drive Android phones — your team's
> real FB/LINE/Messenger accounts. Show you the dashboard differs in 5 min."

### 客户问 "Can it auto-reply?"
> "It can, but we configure for human-in-the-loop by default. AI drafts replies,
> reps approve/edit/send. We don't recommend full-auto bot to chat with
> end customers — it gets you account banned and creates trust issues."

### 客户问 "How much?"
> "$99/device/month after 3-month free PoC. You bring devices/accounts, we
> handle deployment + ongoing engineering support."

### 客户问 "What if it doesn't work?"
> "PoC = 0 risk. We deploy, you test 90 days. If you don't see CVR uplift, walk
> away. If you do, sign year contract."

---

## 用法

1. 用 victor 的真实 email 替换 `{{victor_name}}` / `{{contact_email}}`
2. 每封邮件 personalize 至少 2 个字段 (公司名 + 1 个具体业务细节)
3. 不要批量复制粘贴 — 每封单独发, 间隔 5-10 min
4. 跟进规则: 7 天没回, 发 1 次跟进 (模板 4); 14 天没回, 关闭
