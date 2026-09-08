# -*- coding: utf-8 -*-
"""渠道接入教程（数据源）：抖音企业版（企业主体小程序 + 能力实验室）/ TikTok 官方通道 / 人民币付款说明。

**一份数据、两个出口**（2026-09-08 老板拍板：「在页面做出教程和链接，也加入小智的提问问答中」）：
- 页面 ``GET /help/onboarding/{slug}``（``src/web/routes/onboarding_guide_routes.py`` + ``help_onboarding.html``）
- 小智问答：``howto_pack._HOWTO`` 末尾 ``extend(howto_tuples())`` → 进 HelpKB（BM25 检索），答案带页面链接。

内容以 2026-09 核实的平台文档为准（见 docs/实施96 §2.1、指令_TK-1 §2）；平台政策变动频繁，
每步都附官方入口链接，页面顶部标注核实日期。纯数据 + 纯函数，零 IO。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

VERIFIED_ON = "2026-09-08"

# 每条 step：(zh_title, en_title, zh_detail, en_detail, link_label_zh, link_label_en, url)
GUIDES: Dict[str, Dict[str, Any]] = {
    "douyin": {
        "title": ("抖音企业版接入教程：企业主体小程序 + 能力实验室",
                  "Douyin (Enterprise) onboarding: business mini-program + Capability Lab"),
        "intro": (
            "抖音已于 2025 年回收了第三方「移动/网站应用」的私信接口，目前合规通道只剩「企业主体小程序 IM」。"
            "整条路径约 4–8 周，绝大部分是审核等待时间，所以**今天就开始申请**。智聊侧的官方通道代码已就位，"
            "凭证到手当天即可联调。",
            "Douyin withdrew third-party DM APIs for mobile/web apps in 2025; the only compliant channel left "
            "is the business mini-program IM. The whole path takes 4–8 weeks, mostly review time — start today. "
            "ChatX's official-channel code is ready; wiring takes minutes once credentials arrive.",
        ),
        "prereq": [
            ("企业营业执照（主体与抖音企业号一致）", "Business licence (same legal entity as the Douyin enterprise account)"),
            ("已认证的抖音企业号（蓝 V）及管理员手机", "A verified Douyin enterprise account (blue V) and the admin phone"),
            ("一个公网 HTTPS 域名给 Webhook（云实例或中继）", "A public HTTPS domain for the webhook (cloud instance or relay)"),
        ],
        "steps": [
            ("注册开放平台开发者，创建企业主体小程序并上线",
             "Register as an Open Platform developer, create a business mini-program and publish it",
             "developer.open-douyin.com → 控制台 → 创建应用 → 选「小程序」→ 主体认证选企业 → 提审上线并通过试运营期；"
             "小程序信用分需保持 ≥ 90。若已有企业小程序可直接复用。",
             "developer.open-douyin.com → Console → Create app → Mini-program → verify as an enterprise → submit, publish "
             "and pass the trial period; keep the credit score ≥ 90. An existing enterprise mini-program can be reused.",
             "抖音开放平台控制台", "Douyin Open Platform console", "https://developer.open-douyin.com/"),
            ("能力实验室申请四项能力",
             "Apply for four capabilities in the Capability Lab",
             "控制台 → 能力 → 能力实验室，依次申请：①小程序私信/群管理（收发私信）②主动授权私信组件 ③留资卡 "
             "im.message_card ④图片上传 tool.image.upload。条件：小程序已上线、信用分 ≥ 90、使用场景符合规范；"
             "审核约 5 个工作日。",
             "Console → Capabilities → Capability Lab: ① mini-program DM / group management ② proactive-DM authorisation "
             "component ③ lead card im.message_card ④ image upload tool.image.upload. Requires a published mini-program, "
             "credit score ≥ 90 and a compliant use case; review takes about 5 working days.",
             "小程序私信能力文档", "Mini-program DM capability docs",
             "https://developer.open-douyin.com/docs/resource/zh-CN/mini-app/open-capacity/operation/private-account/private-message"),
            ("绑定抖音企业号并授权私信能力",
             "Bind the Douyin enterprise account and authorise DM capability",
             "小程序控制台 → 关联设置 → 抖音号管理 → 绑定抖音企业机构账号（企业号或其员工号）→ 授权私信能力 "
             "im.direct_message.bind。注意：一个抖音号只能授权给一个应用，若之前授权过其它服务商需先解绑。",
             "Mini-program console → Linked settings → Douyin accounts → bind the enterprise account (or its staff account) "
             "→ grant im.direct_message.bind. One Douyin account can authorise only one app — unbind other vendors first.",
             "抖音企业号后台", "Douyin enterprise account console", "https://e.douyin.com/"),
            ("关闭「互动经营工作台」里的自动回复策略",
             "Turn off auto-reply policies in the Interactive Operations Workbench",
             "企业号后台的互动经营工作台若有生效的私信策略，开放平台接口发送会报 28003095。先停用这些策略，再让智聊接管。",
             "If the enterprise account's workbench still has live DM policies, API sends fail with 28003095. Disable them "
             "before ChatX takes over.",
             "互动经营工作台", "Interactive Operations Workbench", "https://e.douyin.com/"),
            ("配置 Webhook 并订阅事件",
             "Configure the webhook and subscribe to events",
             "控制台 → 设置 → 开发配置 → Webhooks → 填写 https://<你的域名>/webhook/douyin → 保存时平台推送 verify_webhook，"
             "智聊自动回显 challenge 完成验证 → 订阅 im_receive_msg、im_send_msg、im_enter_direct_msg（有群则加 "
             "im_group_receive_msg）。验签用 client_secret，无需再填其它密钥。",
             "Console → Settings → Dev config → Webhooks → enter https://<your-domain>/webhook/douyin → on save the "
             "platform sends verify_webhook and ChatX echoes the challenge → subscribe to im_receive_msg, im_send_msg, "
             "im_enter_direct_msg (plus im_group_receive_msg if you use groups). Signatures use client_secret.",
             "Webhook 文档", "Webhook docs",
             "https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/webhooks/summarize"),
            ("在智聊填入凭证并扫码授权",
             "Enter the credentials in ChatX and authorise by QR code",
             "在本页下方面板填 client_key / client_secret 并「保存凭证」（落 config.local.yaml，自动开启 douyin.enabled），"
             "先把回调地址所在域名填进控制台「授权回调域」，再点「用企业号抖音扫码授权」；成功后经营者 open_id 与 access_token / "
             "refresh_token 自动登记为「抖音 · 官方通道」账号，智聊自动续期（access 15 天、refresh 30 天、最多续 5 次，到期前在"
             "账号卡提醒重新授权）。保存凭证后需重启智聊一次让 webhook 挂载。可选：douyin.enter_greeting 设置客户进入私信页 30 秒内的自动问候。",
             "Use the panel below: enter client_key / client_secret and Save (written to config.local.yaml, enables douyin), "
             "add the callback domain to the console's authorised redirect domains, then click Authorise with the enterprise "
             "Douyin app. On success the operator open_id and access/refresh tokens are registered as a Douyin official-channel "
             "account and refreshed automatically (access 15 d, refresh 30 d, up to 5 renewals; the account card reminds you "
             "to re-authorise before expiry). Restart ChatX once after saving so the webhook is mounted. Optional: "
             "douyin.enter_greeting for the 30-second welcome when a customer opens the chat.",
             "接入向导", "Setup wizard", "/workspace/setup"),
            ("验收：从抖音端发一条私信",
             "Verify: send a DM from the Douyin app",
             "客户端发「你好」→ 收件箱出现抖音会话；回复时 composer 上方显示回复窗（24 小时内最多 6 条）；外链会被拦并提示改发"
             "留资卡；进入私信页会收到预置问候。",
             "Send “hi” from the app → the Douyin conversation appears in the inbox; when replying, the composer shows the "
             "reply window (max 6 messages in 24 h); external links are blocked with a hint to send a lead card instead; "
             "opening the chat triggers the preset greeting.",
             "聊天工作台", "Chat workspace", "/workspace"),
        ],
        "rules": [
            ("回复窗：客户最后一条消息后 24 小时内最多回 6 条；客户再发言即重置",
             "Reply window: at most 6 replies within 24 h of the customer's last message; resets when they write again"),
            ("进私事件：客户进入私信页后 30 秒内最多 3 条；每日最多响应 3 次进私",
             "Chat-open event: at most 3 messages within 30 s of the customer opening the chat; max 3 such events per day"),
            ("内容：文本 ≤ 1000 字、禁止外部链接；图片需图片上传能力；视频只能分享账号自己发布的作品",
             "Content: text ≤ 1000 chars, no external links; images need the upload capability; videos only your own posts"),
            ("主动私信：仅对通过小程序组件授权的用户，每日 1 次、每次 ≤ 3 条、每日 ≤ 5000 人",
             "Proactive DMs: only to users who authorised via the mini-program component; once a day, ≤ 3 messages, ≤ 5000 users/day"),
        ],
        "errors": [
            ("28003095", "互动经营工作台有生效策略", "Workbench policy still active", "停用工作台策略后重试", "Disable the workbench policy and retry"),
            ("28003081", "上条消息已超 24 小时", "Last message older than 24 h", "等客户再发言", "Wait for the customer to write again"),
            ("28003070", "超出频控（6 条 / 3 条）", "Rate limit hit (6 / 3 messages)", "等客户再发言或下一日", "Wait for the customer or the next day"),
            ("2190008", "access_token 过期", "access_token expired", "智聊自动刷新；若 refresh 也过期需重新扫码授权", "ChatX refreshes automatically; re-authorise if the refresh token also expired"),
            ("28029003", "发送方不是认证企业号/员工号", "Sender is not a verified enterprise/staff account", "用企业号或其员工号授权", "Authorise with the enterprise account or a staff account"),
            ("28001038", "内容不合法（超长 / 带 HTTP 链接）", "Invalid content (too long / contains a URL)", "删链接、拆短；智聊发送前已拦", "Remove links, shorten; ChatX blocks these before sending"),
        ],
        "links": [
            ("抖音开放平台", "Douyin Open Platform", "https://developer.open-douyin.com/"),
            ("发送私信消息接口", "Send DM API",
             "https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/search-management/private-message/send-msg"),
            ("接收私信消息事件", "Receive DM webhook",
             "https://developer.open-douyin.com/docs/resource/zh-CN/dop/develop/openapi/interaction-management/private-message/private-msg-webhook"),
            ("私信能力回收公告（为何必须走小程序）", "DM capability withdrawal notice (why the mini-program path)",
             "https://developer.open-douyin.com/announcement/300"),
        ],
        "eta": ("约 4–8 周（审核为主）", "About 4–8 weeks (mostly review time)"),
    },
    "tiktok": {
        "title": ("TikTok 官方通道接入教程：Business Messaging + Shop 客服",
                  "TikTok official channels: Business Messaging + Shop customer service"),
        "intro": (
            "TikTok 与抖音是两个平台，账号、接口、规则、法律都不通。TikTok 的私信 API（Business Messaging，Open Beta）"
            "只要 Business Account + 开发者权限，门槛低于抖音；但**按账号注册地**分区：欧洲经济区、瑞士、英国不可用，美国"
            "目前多数服务商也标不可用。店铺客服另走 TikTok Shop 开放平台。",
            "TikTok and Douyin are separate platforms with different accounts, APIs, rules and law. TikTok's DM API "
            "(Business Messaging, Open Beta) only needs a Business Account plus developer access — a lower bar than Douyin — "
            "but availability follows the account's registration region: EEA, Switzerland and the UK are excluded and most "
            "providers list the US as unavailable. Shop customer service uses the TikTok Shop Open Platform.",
        ),
        "prereq": [
            ("TikTok Business Account（个人/创作者号在设置里可切换）", "A TikTok Business Account (switch from personal/creator in settings)"),
            ("账号注册地在可用区（如东南亚、拉美、中东）", "Account registered in a supported region (e.g. SEA, LATAM, MENA)"),
            ("公网 HTTPS 域名给 Webhook", "A public HTTPS domain for the webhook"),
        ],
        "steps": [
            ("注册 TikTok for Business 开发者并申请 Business Messaging 权限",
             "Register as a TikTok for Business developer and request Business Messaging access",
             "business-api.tiktok.com → 注册开发者应用 → 申请 Business Messaging API（Open Beta，面向 APAC / 拉美 / 中东非 / 北美）。",
             "business-api.tiktok.com → register a developer app → request the Business Messaging API (Open Beta for APAC / LATAM / METAP / North America).",
             "Business Messaging API 文档", "Business Messaging API docs",
             "https://business-api.tiktok.com/portal/docs/business-messaging-api/v1.3"),
            ("在 Business Center 授权 Business Account 并订阅 Webhook",
             "Authorise the Business Account in Business Center and subscribe to webhooks",
             "授权后即可收发私信、配置 welcome message / suggested questions；规则：用户先发、48 小时内最多 10 条、"
             "文本 ≤ 6000 字、图片 ≤ 3MB（多地区禁媒体）、无按钮、无群发。",
             "After authorisation you can send/receive DMs and manage welcome message / suggested questions. Rules: "
             "user-initiated only, ≤ 10 messages within 48 h, text ≤ 6000 chars, images ≤ 3 MB (media disabled in many "
             "regions), no buttons, no broadcasts.",
             "TikTok Business Center", "TikTok Business Center", "https://business.tiktok.com/"),
            ("（卖家）Partner Center 申请店铺客服接口",
             "(Sellers) apply for the Shop customer-service API in Partner Center",
             "partner.tiktokshop.com → 创建应用，业务类目选 Customer Service → 申请 scope seller.customer_service → 店铺授权 "
             "→ 订阅 NEW_MESSAGE 等事件（HMAC-SHA256 验签、3 秒内回 200）。",
             "partner.tiktokshop.com → create an app in the Customer Service category → request scope seller.customer_service "
             "→ shop authorisation → subscribe to NEW_MESSAGE etc. (HMAC-SHA256 signature, respond 200 within 3 s).",
             "TikTok Shop Partner Center", "TikTok Shop Partner Center", "https://partner.tiktokshop.com/"),
            ("在智聊填入凭证并选择账号注册地",
             "Enter credentials in ChatX and select the account's registration region",
             "在本页下方面板：① 填开发者应用的 App ID / Secret 并保存；② 把回调地址填进开发者应用的 Redirect URL；③ 填账号注册地"
             "（ISO 两位，如 SG / MY / MX），点「用 TikTok Business Account 授权」，在 TikTok 页面把私信读取 / 发送 / 管理权限全部允许；"
             "成功后账号、令牌自动登记并自动续期，私信 Webhook 也会自动注册到 TikTok。注册地决定私信 API 是否可用、图片能否发送：不可用"
             "地区会直接标红并给出替代（Messaging Ads / 店铺客服 / WhatsApp 官方通道）。首次保存凭证后需重启智聊一次让 webhook 路由挂载。",
             "Use the panel below: ① enter the developer app's App ID / Secret and save; ② set the callback URL as the app's Redirect URL; "
             "③ enter the account's registration region (ISO-2, e.g. SG / MY / MX) and click “Authorise with TikTok Business Account”, "
             "allowing all messaging permissions (read / send / manage) on TikTok's page. On success the account and tokens are "
             "registered with auto-renewal and the DM webhook is registered automatically. The region decides whether the DM API and "
             "image sending are available; unsupported regions are flagged red with alternatives (Messaging Ads / Shop CS / WhatsApp). "
             "Restart ChatX once after the first credential save so the webhook route is mounted.",
             "接入向导", "Setup wizard", "/workspace/setup"),
        ],
        "rules": [
            ("私信 API 不可用地区：欧洲经济区、瑞士、英国（美国多数服务商标不可用）",
             "DM API unavailable: EEA, Switzerland, UK (most providers list the US as unavailable)"),
            ("48 小时内最多 10 条；只能回复先发消息的用户；无主动私信与群发",
             "≤ 10 messages within 48 h; reply only to users who message first; no proactive DMs or broadcasts"),
            ("端内：16 岁以下无私信、16–17 仅好友、非互关只能发 1 条 message request",
             "In-app: no DMs under 16, friends-only at 16–17, one message request to non-followers"),
        ],
        "errors": [],
        "links": [
            ("Business Messaging API", "Business Messaging API", "https://business-api.tiktok.com/portal/docs/business-messaging-api/v1.3"),
            ("TikTok Shop 开放平台", "TikTok Shop Open Platform", "https://partner.tiktokshop.com/"),
            ("TikTok for Business", "TikTok for Business", "https://business.tiktok.com/"),
        ],
        "eta": ("约 1–3 周（Business Account 切换即时，开发者权限按审核）", "About 1–3 weeks (account switch is instant; developer access depends on review)"),
    },
    "payment-cny": {
        "title": ("付款方式：美元 / USDT 充值与人民币通道进度",
                  "Payment: USD / USDT top-ups and the CNY channel status"),
        "intro": (
            "智聊按 Token 充值计费（1 USD = 1,500 Token，首充按档加赠）。当前官网支持美元与 USDT 结算；"
            "人民币收款主体（微信支付 / 支付宝）正在办理，上线后官网会出现人民币支付入口。企业客户可先走对公合同"
            "（年框 / 私有化部署，支持发票）。",
            "ChatX bills by Token top-ups (1 USD = 1,500 Tokens, first top-up bonus by tier). The website currently settles in "
            "USD and USDT; a CNY payment entity (WeChat Pay / Alipay) is being set up and the site will show a CNY option "
            "once live. Enterprise customers can start with a corporate contract (annual frame / private deployment, invoices supported).",
        ),
        "prereq": [],
        "steps": [
            ("现在就能用的付款方式", "Payment methods available now",
             "官网下单页选择美元或 USDT 充值（50 / 100 / 200 / 500 / 1000 / 5000 / 10000 USD 档，新人 6U 礼包）；到账后 Token 立即可用。",
             "Choose USD or USDT on the order page (50 / 100 / 200 / 500 / 1000 / 5000 / 10000 USD tiers, 6 USD newcomer pack); "
             "Tokens are available immediately after settlement.",
             "官网下单", "Order page", "https://bd2026.cc/order"),
            ("企业对公", "Corporate billing",
             "年框协议价（建议 ≥ 20,000 USD 起谈）、月结、对公转账、发票、专属 SLA；私有化部署一次性实施 + 年授权维保。请联系商务。",
             "Annual frame pricing (from ~20,000 USD), monthly settlement, bank transfer, invoices, dedicated SLA; private "
             "deployment is one-off implementation + annual licence. Contact sales.",
             "联系商务", "Contact sales", "https://bd2026.cc/"),
            ("人民币通道（办理中）", "CNY channel (in progress)",
             "微信支付与支付宝商户号申请中；上线后按当日汇率折算 Token 档位并支持电子发票。进度以官网公告为准。",
             "WeChat Pay and Alipay merchant accounts are being applied for; once live, Token tiers will be priced at the daily "
             "exchange rate with e-invoices. Follow the website announcements for progress.",
             "官网", "Website", "https://bd2026.cc/"),
        ],
        "rules": [],
        "errors": [],
        "links": [("官网下单", "Order page", "https://bd2026.cc/order")],
        "eta": ("人民币通道：以官网公告为准", "CNY channel: see website announcements"),
    },
}

SLUGS = tuple(GUIDES.keys())


def _pick(pair: Any, lang: str) -> str:
    if isinstance(pair, (list, tuple)) and len(pair) >= 2:
        return str(pair[1] if lang == "en" else pair[0])
    return str(pair or "")


def guide_for(slug: str, lang: str = "zh") -> Optional[Dict[str, Any]]:
    """按语言展平成模板可直接渲染的 dict；未知 slug → None。"""
    g = GUIDES.get(str(slug or "").strip().lower())
    if not g:
        return None
    en = str(lang or "zh").lower().startswith("en")
    lg = "en" if en else "zh"
    return {
        "slug": slug, "lang": lg, "verified_on": VERIFIED_ON,
        "title": _pick(g["title"], lg), "intro": _pick(g["intro"], lg),
        "eta": _pick(g.get("eta", ("", "")), lg),
        "prereq": [_pick(p, lg) for p in g.get("prereq", [])],
        "steps": [{"title": s[1] if en else s[0], "detail": s[3] if en else s[2],
                   "link_label": s[5] if en else s[4], "url": s[6]} for s in g.get("steps", [])],
        "rules": [_pick(r, lg) for r in g.get("rules", [])],
        "errors": [{"code": e[0], "meaning": e[2] if en else e[1], "fix": e[4] if en else e[3]}
                   for e in g.get("errors", [])],
        "links": [{"label": l[1] if en else l[0], "url": l[2]} for l in g.get("links", [])],
    }


def _summary(slug: str, lang: str) -> str:
    g = guide_for(slug, lang)
    if not g:
        return ""
    steps = " ".join(f"{i}. {s['title']}" for i, s in enumerate(g["steps"], 1))
    sep = " " if lang == "en" else ""
    return f"{g['intro']}{sep}{steps}"


def howto_tuples() -> List[tuple]:
    """→ howto_pack 七元组 (slug, title_zh, title_en, answer_zh, answer_en, keywords, path)。

    答案 = 教程 intro + 步骤标题串（完整步骤在页面），path 指向教程页——小智「带我去」直达。
    """
    kw = {
        "douyin": "抖音 企业号 小程序 能力实验室 私信 接入 申请 douyin mini-program capability lab webhook 蓝V",
        "tiktok": "tiktok 接入 申请 business messaging business account shop 客服 partner center 地区 region",
        "payment-cny": "付款 支付 人民币 微信支付 支付宝 充值 usdt 美元 发票 对公 price pay cny rmb payment invoice",
    }
    titles = {
        "douyin": ("怎么接入抖音 / 抖音企业版怎么申请（小程序与能力实验室）",
                   "How to connect Douyin / apply for the Douyin enterprise channel (mini-program + Capability Lab)"),
        "tiktok": ("怎么接入 TikTok（Business Messaging / 店铺客服）",
                   "How to connect TikTok (Business Messaging / Shop customer service)"),
        "payment-cny": ("怎么付款 / 支持人民币吗 / 微信支付宝能付吗",
                        "How do I pay / is CNY supported / WeChat Pay or Alipay"),
    }
    out: List[tuple] = []
    for slug in SLUGS:
        out.append((f"onboarding-{slug}", titles[slug][0], titles[slug][1],
                    _summary(slug, "zh") + f"。完整步骤与官方链接见教程页 /help/onboarding/{slug}。",
                    _summary(slug, "en") + f" Full steps and official links: /help/onboarding/{slug}.",
                    kw[slug], f"/help/onboarding/{slug}"))
    return out


__all__ = ["GUIDES", "SLUGS", "VERIFIED_ON", "guide_for", "howto_tuples"]
