// ── 竞品对比页内容（WS-1 2026-08-17）────────────────────────────────────────
// 措辞纪律：我方列只写已交付实况；竞品列只写「其官网公开资料的一般性理解」并
// 全文挂免责（对比页最容易翻车的就是替竞品下结论——具体功能/价格一律指回其官网）。
// 打法对位：respond.io=企业级全渠道客服 SaaS → 打「私有化+数据主权+拟人化」；
// SaleSmartly=跨境社媒聚合客服 SaaS → 打「AI 主动成交+人设深度+本地部署」。

export interface CompareCell {
  zh: string;
  en: string;
}

export interface CompareRow {
  dim: CompareCell;
  us: CompareCell;
  them: CompareCell;
}

/** GEO FAQ（实施77 渠道四 2026-08-27）：问题=买家真实会问 AI 的问法，
 *  答案=自包含可引用（AI 引擎按段落抽取，答案必须脱离上下文也成立）。 */
export interface CompareFaq {
  q: CompareCell;
  a: CompareCell;
}

export interface CompareSpec {
  slug: string;
  /** 对方产品显示名（不译） */
  name: string;
  tagline: CompareCell;
  /** GEO 答案胶囊：开篇 40-60 词直答「两者区别/该选谁」，供 AI 引擎整段引用。 */
  answer: CompareCell;
  intro: { zh: string[]; en: string[] };
  rows: CompareRow[];
  faq: CompareFaq[];
  disclaimer: CompareCell;
}

/** FAQPage JSON-LD（schema.org），服务端页面注入；lang 随路由固定。 */
export function buildFaqJsonLd(faq: CompareFaq[], lang: "zh" | "en"): object {
  return {
    "@context": "https://schema.org",
    "@type": "FAQPage",
    mainEntity: faq.map((f) => ({
      "@type": "Question",
      name: f.q[lang],
      acceptedAnswer: { "@type": "Answer", text: f.a[lang] },
    })),
  };
}

const US_DEPLOY: CompareCell = {
  zh: "私有化部署：跑在你自己的服务器/桌面机上，聊天记录、客户资料、AI 记忆全部留在本地，不经第三方云。",
  en: "Self-hosted: runs on your own server or desktop; chat history, customer data and AI memory stay on your machines — no third-party cloud in the loop.",
};

const US_AI: CompareCell = {
  zh: "AI 定位是「拟人化关系运营」：人设档案、长期记忆、情绪感知、主动关怀问候、克隆声语音回复、图文一致性守卫——目标是像一个真的运营在聊，而不只是工单机器人。",
  en: "The AI is built for human-like relationship operations: persona profiles, long-term memory, emotion awareness, proactive check-ins, cloned-voice replies and consistency guards — it chats like a real operator, not a ticket bot.",
};

const US_COMPLIANCE: CompareCell = {
  zh: "合规做成产品开关：AI 披露语（9 语种）、诚实身份模式、危机识别→干预→热线转介闭环 + 年报转介计数导出（对应 EU AI Act 第 50 条 / 加州 SB 243 / 纽约 §1700），默认关、按属地开启。",
  en: "Compliance ships as product switches: AI disclosure (9 languages), honest-identity mode, and a crisis detect→intervene→hotline-referral loop with exportable annual counters (EU AI Act Art. 50 / CA SB 243 / NY §1700) — off by default, enabled per jurisdiction.",
};

// 2026-08-19 Token 定价改版：口径与 lib/chatx-pricing.ts 同步（改价两处一起改）。
const US_PRICING: CompareCell = {
  zh: "免费开始（全功能 + 标准翻译不限量），充多少用多少、不订阅：充值 50U 起、1U = 1,500 Token，首充按档加赠最高 +40%，新人 6U 大礼包 18,000 Token 双倍到账。AI 用量按 Token 透明计价（无月费、无按月活联系人 MAU 加价），支持 USDT / 银行卡；企业年框与私有化部署面议。",
  en: "Start free (all features + unlimited standard translation), then top up as you go — no subscription: from 50U at 1U = 1,500 tokens, first top-up earns up to +40%, newcomer 6U pack lands 18,000 tokens at double rate. AI usage meters in transparent tokens (no monthly fee, no per-MAU surcharges), USDT & cards accepted; enterprise frames and private deployment by quote.",
};

const THEM_SAAS_DATA: CompareCell = {
  zh: "云端 SaaS，数据托管于服务商侧（数据驻留与安全条款以其官网说明为准）。",
  en: "Cloud SaaS; data is hosted on the vendor side (see their website for data-residency and security terms).",
};

export const compareSpecs: Record<string, CompareSpec> = {
  "respond-io": {
    slug: "respond-io",
    name: "respond.io",
    tagline: {
      zh: "智聊 ChatX vs respond.io：私有化拟人运营 vs 企业级全渠道客服 SaaS",
      en: "ChatX vs respond.io: self-hosted human-like operations vs enterprise omnichannel SaaS",
    },
    answer: {
      zh: "一句话结论：respond.io 是企业级云端全渠道客服 SaaS，以官方商业 API 触达与团队协作见长，订阅计费；智聊 ChatX 是可私有化部署的 AI 拟人关系运营系统——数据留在自己机器、AI 以稳定人设主动经营客户，免费开始、按 Token 充值不订阅。要标准化客服流程选前者，要数据主权与 AI 替人聊单选后者。",
      en: "In one sentence: respond.io is an enterprise cloud omnichannel support SaaS, strong at official business-API messaging and team workflows, billed by subscription; ChatX is a self-hostable AI relationship-operations system — data stays on your machines, the AI proactively nurtures customers under a stable persona, free to start with pay-as-you-go tokens. Pick the former for standardized support workflows, the latter for data sovereignty and AI that actually sells.",
    },
    intro: {
      zh: [
        "respond.io 是成熟的企业级全渠道客户对话管理 SaaS，强在官方商业 API 的合规触达与团队协作流程。",
        "智聊 ChatX 解决的是另一类问题：当你的业务要求数据不出自己机器、要求 AI 像真人一样长期经营关系（而不只是接工单）时，SaaS 客服台的形态本身就不够用了。",
      ],
      en: [
        "respond.io is a mature enterprise omnichannel conversation SaaS, strong at official business-API messaging and team workflows.",
        "ChatX solves a different problem: when your business requires data to stay on your own machines and an AI that nurtures relationships like a real person — not just answers tickets — the SaaS help-desk form factor itself falls short.",
      ],
    },
    rows: [
      {
        dim: { zh: "产品定位", en: "Positioning" },
        us: {
          zh: "多平台 AI 拟人化关系运营系统：成交与陪伴双场景，AI 主动经营客户关系。",
          en: "Multi-platform AI relationship-operations system: sales and companionship scenarios, with the AI proactively nurturing customers.",
        },
        them: {
          zh: "企业级全渠道客户对话管理平台（客服/营销消息一体化，以官网为准）。",
          en: "Enterprise omnichannel customer-conversation management platform (support/marketing messaging; see their site).",
        },
      },
      {
        dim: { zh: "部署与数据主权", en: "Deployment & data ownership" },
        us: US_DEPLOY,
        them: THEM_SAAS_DATA,
      },
      {
        dim: { zh: "渠道接入形态", en: "Channel connectivity" },
        us: {
          zh: "Telegram 协议号 + WhatsApp / LINE / Messenger / Instagram / Zalo 等多通道，协议、官方 API、真机自动化三种形态按渠道择优。",
          en: "Telegram protocol accounts plus WhatsApp / LINE / Messenger / Instagram / Zalo, mixing protocol, official-API and real-device automation per channel.",
        },
        them: {
          zh: "以官方商业 API 渠道为主（如 WhatsApp Business API 等，以官网渠道清单为准）。",
          en: "Primarily official business-API channels (e.g. WhatsApp Business API; see their channel list).",
        },
      },
      {
        dim: { zh: "AI 能力", en: "AI capabilities" },
        us: US_AI,
        them: {
          zh: "以客服自动化 / 工作流机器人 / AI 辅助回复为主（以其官网最新功能为准）。",
          en: "Centered on support automation, workflow bots and AI-assisted replies (see their site for the latest).",
        },
      },
      {
        dim: { zh: "合规工具", en: "Compliance tooling" },
        us: US_COMPLIANCE,
        them: {
          zh: "面向商业消息合规（渠道政策/送达合规）方向，具体以其官网合规说明为准。",
          en: "Oriented to business-messaging compliance (channel policy / deliverability); see their compliance docs.",
        },
      },
      {
        dim: { zh: "计费模式", en: "Pricing model" },
        us: US_PRICING,
        them: {
          zh: "订阅制，常见按席位与月活联系人（MAU）阶梯计费（以官网价目为准）。",
          en: "Subscription, commonly tiered by seats and monthly active contacts (see their pricing page).",
        },
      },
      {
        dim: { zh: "适合谁", en: "Best fit" },
        us: {
          zh: "跨境私域运营、陪伴/情感运营工作室、对数据主权有硬要求的团队。",
          en: "Cross-border private-domain teams, companionship studios, and teams with hard data-sovereignty requirements.",
        },
        them: {
          zh: "需要官方 API 合规触达与标准客服协作流程的中大型客服团队。",
          en: "Mid-to-large support teams needing official-API messaging and standardized workflows.",
        },
      },
    ],
    faq: [
      {
        q: {
          zh: "respond.io 和智聊 ChatX 最大的区别是什么？",
          en: "What is the biggest difference between respond.io and ChatX?",
        },
        a: {
          zh: "形态不同：respond.io 是云端 SaaS 客服平台，数据托管在服务商侧；智聊 ChatX 可私有化部署，聊天记录、客户资料与 AI 记忆留在你自己的机器上，且 AI 定位是拟人化关系运营（人设、长期记忆、主动跟进、克隆声语音），不只是应答工单。",
          en: "Form factor: respond.io is a cloud SaaS platform with vendor-hosted data; ChatX can be self-hosted so chats, customer data and AI memory stay on your own machines, and its AI is built for human-like relationship operations (personas, long-term memory, proactive follow-ups, cloned-voice replies) rather than ticket answering.",
        },
      },
      {
        q: {
          zh: "有没有支持中文、可私有化部署的 respond.io 替代品？",
          en: "Is there a self-hostable respond.io alternative with first-class Chinese support?",
        },
        a: {
          zh: "智聊 ChatX 是中文团队开发的私有化替代选项：Telegram / WhatsApp / LINE / Messenger 等多平台统一收件箱，内建拟人互译与 AI 主动跟进，免费开始（标准翻译不限量），按 Token 充值、无订阅。",
          en: "ChatX is a self-hostable alternative built by a Chinese-speaking team: a unified inbox for Telegram / WhatsApp / LINE / Messenger with built-in human-like translation and proactive AI follow-ups; free to start (unlimited standard translation) with pay-as-you-go tokens, no subscription.",
        },
      },
      {
        q: {
          zh: "按席位/月活联系人订阅，和按 Token 充值，哪个更划算？",
          en: "Which costs less: seat/MAC subscriptions or pay-as-you-go tokens?",
        },
        a: {
          zh: "取决于用量形态：联系人多而 AI 用量低时，按席位与月活联系人（MAC）阶梯的订阅费会随联系人数增长；按 Token 充值只为真实 AI 用量付费（智聊 1U = 1,500 Token，充值 12 个月有效），起步期与波动期成本更可控。请以双方价目页按自己的量实算。",
          en: "It depends on your usage shape: with many contacts but light AI usage, seat/monthly-active-contact subscription tiers grow with your contact count; pay-as-you-go tokens only charge for actual AI usage (ChatX: 1U = 1,500 tokens, valid 12 months). Model both with your own numbers from the two pricing pages.",
        },
      },
      {
        q: {
          zh: "哪个更适合 Telegram 多账号运营？",
          en: "Which is better for multi-account Telegram operations?",
        },
        a: {
          zh: "智聊原生支持 Telegram 协议号多账号聚合（配频控与风控护栏），同一工作台管理多个 TG 账号的会话；respond.io 以官方商业 API 渠道为主，Telegram 支持形态以其官网为准。",
          en: "ChatX natively aggregates multiple Telegram protocol accounts (with rate-control and risk guardrails) in one workspace; respond.io centers on official business-API channels — check their site for current Telegram support.",
        },
      },
      {
        q: {
          zh: "智聊的数据能完全不出自己的服务器吗？",
          en: "Can ChatX keep all data on my own servers?",
        },
        a: {
          zh: "可以。私有化部署下聊天记录、客户资料、AI 记忆全部在本地，翻译可用本地引擎、模型可跑本地 GPU；不想自维护的团队也可选云端托管形态（一客户一实例、独立子域）。",
          en: "Yes. Self-hosted deployments keep chats, customer data and AI memory local; translation can run on local MT engines and models on local GPUs. Teams that prefer zero-ops can use the hosted option (one isolated instance per customer, own subdomain).",
        },
      },
    ],
    disclaimer: {
      zh: "对比基于我方产品实况与对方官网公开资料的一般性理解（更新于 2026-08），可能滞后或有出入；对方具体功能与价格一律以其官网最新信息为准。respond.io 为其所有者商标。",
      en: "This comparison reflects our product as shipped and a general reading of the other party's public website (as of 2026-08); it may lag or differ. Always verify features and pricing on their official site. respond.io is a trademark of its owner.",
    },
  },

  salesmartly: {
    slug: "salesmartly",
    name: "SaleSmartly",
    tagline: {
      zh: "智聊 ChatX vs SaleSmartly：AI 主动成交 vs 跨境社媒聚合客服",
      en: "ChatX vs SaleSmartly: proactive AI closing vs cross-border social-inbox SaaS",
    },
    answer: {
      zh: "一句话结论：SaleSmartly 是云端跨境社媒聚合客服 SaaS（多渠道收件 + 翻译 + 流程自动化）；智聊 ChatX 把聚合收件箱当地基，核心是 AI 以稳定人设主动经营每个客户——记住对方、主动问候、克隆声说话、推进成交，且可私有化部署数据不出机。要快速聚合客服消息选前者，要 AI 真正替人聊单成交、数据留在自己手里选后者。",
      en: "In one sentence: SaleSmartly is a cloud social-inbox SaaS for cross-border teams (multi-channel inbox + translation + flow automation); ChatX treats the unified inbox as the foundation — its core is an AI that runs each relationship under a stable persona (remembers people, checks in proactively, speaks with a cloned voice, advances deals) and can be fully self-hosted. Pick the former to quickly unify support messages, the latter for AI that genuinely chats to close with data kept in-house.",
    },
    intro: {
      zh: [
        "SaleSmartly 是常见的跨境社媒聚合客服 SaaS：把多平台消息收进一个云端工作台，配自动翻译与流程自动化。",
        "智聊 ChatX 的出发点不同：聚合收件只是地基，核心是让 AI 以稳定人设长期经营每个客户——主动问候、记住对方、用克隆声说话、在合适的时机推进成交，而且全部跑在你自己的机器上。",
      ],
      en: [
        "SaleSmartly is a typical cross-border social-inbox SaaS: multi-platform messages in one cloud workspace, with auto-translation and flow automation.",
        "ChatX starts elsewhere: the unified inbox is just the foundation — the core is an AI that runs each relationship under a stable persona, proactively checks in, remembers people, speaks with a cloned voice and advances deals at the right moment, all on your own machines.",
      ],
    },
    rows: [
      {
        dim: { zh: "产品定位", en: "Positioning" },
        us: {
          zh: "AI 拟人化关系运营系统：AI 是「会主动经营的数字员工」，不是被动应答的客服插件。",
          en: "AI relationship-operations system: the AI is a proactive digital operator, not a passive support widget.",
        },
        them: {
          zh: "跨境社媒聚合客服平台（多渠道收件 + 翻译 + 自动化流程，以官网为准）。",
          en: "Cross-border social-inbox platform (multi-channel inbox + translation + flow automation; see their site).",
        },
      },
      {
        dim: { zh: "部署与数据主权", en: "Deployment & data ownership" },
        us: US_DEPLOY,
        them: THEM_SAAS_DATA,
      },
      {
        dim: { zh: "AI 能力", en: "AI capabilities" },
        us: US_AI,
        them: {
          zh: "以关键词/流程自动化与 AI 辅助客服回复为主（以其官网最新功能为准）。",
          en: "Centered on keyword/flow automation and AI-assisted support replies (see their site for the latest).",
        },
      },
      {
        dim: { zh: "翻译", en: "Translation" },
        us: {
          zh: "双向实时翻译内建（入站看原文+译文、出站按客户语言自动翻译），支持本地翻译引擎——译文也不必出机房。",
          en: "Built-in two-way live translation (inbound shown with translation, outbound auto-translated to the customer's language), with local MT engines — even translations can stay on-prem.",
        },
        them: {
          zh: "聚合客服场景的多语言自动翻译（以官网为准）。",
          en: "Multi-language auto-translation for the support inbox (see their site).",
        },
      },
      {
        dim: { zh: "合规工具", en: "Compliance tooling" },
        us: US_COMPLIANCE,
        them: {
          zh: "以其官网合规与隐私说明为准。",
          en: "See their compliance and privacy documentation.",
        },
      },
      {
        dim: { zh: "计费模式", en: "Pricing model" },
        us: US_PRICING,
        them: {
          zh: "订阅制 SaaS（以官网价目为准）。",
          en: "Subscription SaaS (see their pricing page).",
        },
      },
      {
        dim: { zh: "适合谁", en: "Best fit" },
        us: {
          zh: "要 AI 真正替人聊天成交、要数据留在自己手里的私域/陪伴运营团队。",
          en: "Private-domain and companionship teams that want the AI to genuinely chat and close, with data kept in-house.",
        },
        them: {
          zh: "需要把多平台客服消息快速聚合到一个云端工作台的起步团队。",
          en: "Teams that primarily need multi-platform support messages unified in one cloud workspace.",
        },
      },
    ],
    faq: [
      {
        q: {
          zh: "SaleSmartly 和智聊 ChatX 有什么区别？",
          en: "How is ChatX different from SaleSmartly?",
        },
        a: {
          zh: "两者都做多渠道消息聚合，但定位不同：SaleSmartly 是云端聚合客服 SaaS（收件 + 翻译 + 流程自动化）；智聊在聚合之上做 AI 拟人经营——人设档案、长期记忆、主动关怀、克隆声语音、图文一致性守卫，并支持私有化部署（数据不出机）。",
          en: "Both unify multi-channel messages, but the positioning differs: SaleSmartly is a cloud inbox SaaS (inbox + translation + flow automation); ChatX builds human-like AI operations on top — persona profiles, long-term memory, proactive care, cloned-voice replies, consistency guards — and can be self-hosted so data never leaves your machines.",
        },
      },
      {
        q: {
          zh: "有本地部署（数据不出机）的 SaleSmartly 替代品吗？",
          en: "Is there a self-hosted SaleSmartly alternative?",
        },
        a: {
          zh: "智聊 ChatX 提供私有化部署（跑在你自己的服务器/桌面机，聊天与客户数据全留本地）与云端托管（一客户一实例）两种形态；免费开始，标准翻译不限量，AI 用量按 Token 充值。",
          en: "ChatX offers both self-hosted deployment (runs on your own server or desktop; chats and customer data stay local) and a hosted option (one isolated instance per customer). Free to start, unlimited standard translation, AI usage on pay-as-you-go tokens.",
        },
      },
      {
        q: {
          zh: "两者的翻译能力有什么差异？",
          en: "How does translation compare?",
        },
        a: {
          zh: "智聊内建双向实时翻译：入站显示原文加译文、出站自动译成客户语言，主打「拟人不像机翻」，标准翻译永久免费不限量（公平使用 200 万字符/日），还可切本地翻译引擎让译文也不出机房；SaleSmartly 的翻译能力与计费以其官网为准。",
          en: "ChatX ships two-way live translation: inbound shows original plus translation, outbound auto-translates into the customer's language, tuned to read human rather than machine-translated; standard translation is free and unlimited (fair use 2M chars/day), with optional local MT engines so even translations stay on-prem. See SaleSmartly's site for its translation scope and pricing.",
        },
      },
      {
        q: {
          zh: "AI 能主动跟进客户吗，还是只能被动回复？",
          en: "Can the AI follow up proactively, or only reply?",
        },
        a: {
          zh: "智聊的 AI 会按关系阶段主动经营：沉默客户问候、纪念日关怀、按客户活跃时间择时触达，全部带频控、退避与打扰保护护栏；一般聚合客服工具以被动应答与关键词流程自动化为主（以各家官网为准）。",
          en: "ChatX's AI nurtures proactively by relationship stage: re-engaging silent customers, milestone check-ins, timing outreach to each customer's active hours — all under rate-control, backoff and anti-nuisance guardrails. Typical inbox SaaS focuses on reactive replies and keyword flows (verify per vendor).",
        },
      },
      {
        q: {
          zh: "价格模式有什么差异？",
          en: "How does pricing differ?",
        },
        a: {
          zh: "智聊免费开始（全功能 + 标准翻译不限量 + 每月 1,000 Token），付费只有按量充值一种：1U = 1,500 Token、首充最高 +40%、新人 6U 大礼包 18,000 Token，无订阅、无席位费；SaleSmartly 为订阅制 SaaS，价格以其官网为准。",
          en: "ChatX starts free (all features + unlimited standard translation + 1,000 tokens/month) and charges only via top-ups: 1U = 1,500 tokens, first top-up bonus up to +40%, newcomer 6U pack lands 18,000 tokens — no subscription, no seat fees. SaleSmartly is subscription SaaS; see its pricing page.",
        },
      },
    ],
    disclaimer: {
      zh: "对比基于我方产品实况与对方官网公开资料的一般性理解（更新于 2026-08），可能滞后或有出入；对方具体功能与价格一律以其官网最新信息为准。SaleSmartly 为其所有者商标。",
      en: "This comparison reflects our product as shipped and a general reading of the other party's public website (as of 2026-08); it may lag or differ. Always verify features and pricing on their official site. SaleSmartly is a trademark of its owner.",
    },
  },

  sleekflow: {
    slug: "sleekflow",
    name: "SleekFlow",
    tagline: {
      zh: "智聊 ChatX vs SleekFlow：私域 AI 拟人成交 vs 社交电商客服营销 SaaS",
      en: "ChatX vs SleekFlow: private-domain AI closing vs social-commerce messaging SaaS",
    },
    answer: {
      zh: "一句话结论：SleekFlow 是面向社交电商的云端客服/营销 SaaS，强在 Shopify 等电商生态集成与购物流程自动化；智聊 ChatX 是可私有化部署的 AI 拟人运营系统，强在 Telegram 协议号多账号、拟人互译与 AI 主动经营关系。做店铺购物流程自动化选前者，做私域关系与聊天成交、要数据主权选后者。",
      en: "In one sentence: SleekFlow is a cloud messaging SaaS for social commerce, strong at Shopify-style e-commerce integrations and shopping-flow automation; ChatX is a self-hostable AI relationship-operations system, strong at multi-account Telegram, human-like translation and proactive AI nurturing. Pick the former for storefront flow automation, the latter for private-domain relationships, chat-based closing and data sovereignty.",
    },
    intro: {
      zh: [
        "SleekFlow 是常见的社交电商向全渠道消息 SaaS：把 WhatsApp、Instagram 等渠道消息聚合，配电商目录、支付链接与流程自动化（以其官网为准）。",
        "智聊 ChatX 面向的是另一类经营：没有「购物车」的生意——私域成交、服务型销售、陪伴运营。这里的关键不是把商品卡推进聊天，而是让 AI 像真人一样把关系聊深、把单聊成，并且数据留在自己机器上。",
      ],
      en: [
        "SleekFlow is a typical omnichannel messaging SaaS for social commerce: WhatsApp, Instagram and other channels unified, with catalogs, payment links and flow automation (see their site).",
        "ChatX targets a different kind of business — ones without a shopping cart: private-domain sales, service selling, companionship operations. The job is not pushing product cards into chat, but an AI that deepens relationships and closes like a real person, with data on your own machines.",
      ],
    },
    rows: [
      {
        dim: { zh: "产品定位", en: "Positioning" },
        us: {
          zh: "AI 拟人化关系运营系统：AI 是会主动经营的数字员工，覆盖成交与陪伴双场景。",
          en: "AI relationship-operations system: a proactive digital operator covering both sales and companionship scenarios.",
        },
        them: {
          zh: "社交电商向全渠道客服/营销 SaaS（电商集成 + 流程自动化，以官网为准）。",
          en: "Omnichannel messaging SaaS for social commerce (e-commerce integrations + flow automation; see their site).",
        },
      },
      {
        dim: { zh: "部署与数据主权", en: "Deployment & data ownership" },
        us: US_DEPLOY,
        them: THEM_SAAS_DATA,
      },
      {
        dim: { zh: "渠道接入形态", en: "Channel connectivity" },
        us: {
          zh: "Telegram 协议号多账号 + WhatsApp / LINE / Messenger / Instagram / Zalo 等，协议、官方 API、真机自动化按渠道择优；适合以 TG/私域为主战场的团队。",
          en: "Multi-account Telegram protocol plus WhatsApp / LINE / Messenger / Instagram / Zalo, mixing protocol, official-API and real-device automation per channel — built for TG/private-domain-first teams.",
        },
        them: {
          zh: "以官方商业 API 渠道与社媒渠道为主（以官网渠道清单为准）。",
          en: "Primarily official business-API and social channels (see their channel list).",
        },
      },
      {
        dim: { zh: "AI 能力", en: "AI capabilities" },
        us: US_AI,
        them: {
          zh: "以电商场景的流程自动化与 AI 辅助回复/导购为主（以其官网最新功能为准）。",
          en: "Centered on commerce flow automation and AI-assisted replies/shopping guidance (see their site for the latest).",
        },
      },
      {
        dim: { zh: "合规工具", en: "Compliance tooling" },
        us: US_COMPLIANCE,
        them: {
          zh: "以其官网合规与隐私说明为准。",
          en: "See their compliance and privacy documentation.",
        },
      },
      {
        dim: { zh: "计费模式", en: "Pricing model" },
        us: US_PRICING,
        them: {
          zh: "订阅制 SaaS（常见按席位/联系人阶梯，以官网价目为准）。",
          en: "Subscription SaaS (commonly tiered by seats/contacts; see their pricing page).",
        },
      },
      {
        dim: { zh: "适合谁", en: "Best fit" },
        us: {
          zh: "私域成交/服务型销售/陪伴运营团队，重 Telegram 与数据主权的跨境团队。",
          en: "Private-domain sales, service selling and companionship teams — especially Telegram-heavy, data-sovereignty-minded cross-border operators.",
        },
        them: {
          zh: "以 Shopify 等店铺为中心、需要把渠道消息接进购物流程的电商团队。",
          en: "Storefront-centric e-commerce teams wiring channel messages into shopping flows.",
        },
      },
    ],
    faq: [
      {
        q: {
          zh: "SleekFlow 和智聊 ChatX 分别适合什么团队？",
          en: "Which teams suit SleekFlow vs ChatX?",
        },
        a: {
          zh: "有在线店铺、要把消息接进购物流程（目录/支付链接/弃购挽回）的团队更贴合 SleekFlow 这类电商向 SaaS；没有购物车、靠聊天建立信任再成交的私域/服务型团队更贴合智聊——AI 人设经营、拟人互译、主动跟进是为这类成交设计的。",
          en: "Storefront teams wiring messages into shopping flows (catalogs, payment links, cart recovery) fit commerce SaaS like SleekFlow; cart-less businesses that close through trust-building conversations fit ChatX — persona-driven AI, human-like translation and proactive follow-ups are built for that motion.",
        },
      },
      {
        q: {
          zh: "智聊支持 Shopify 集成吗？",
          en: "Does ChatX integrate with Shopify?",
        },
        a: {
          zh: "智聊不做店铺购物流程集成（那是电商向 SaaS 的主场）；它的下单转化走对话内链接与自有收款（USDT/银行卡），面向私域与服务型成交。若你的核心诉求是 Shopify 订单自动化，电商向工具更合适。",
          en: "ChatX doesn't do storefront flow integrations (that's commerce SaaS territory); conversions run through in-chat links and your own checkout (USDT/cards), aimed at private-domain and service selling. If Shopify order automation is the core need, a commerce-focused tool fits better.",
        },
      },
      {
        q: {
          zh: "为什么说数据主权对私域团队重要？",
          en: "Why does data sovereignty matter for private-domain teams?",
        },
        a: {
          zh: "私域团队的核心资产就是客户关系数据：聊天记录、客户画像、AI 记忆。云 SaaS 形态下这些托管在服务商侧，停服、封号或出海合规变化都可能让资产失联；私有化部署把它们留在自己机器上，这是智聊的默认形态。",
          en: "A private-domain team's core asset is relationship data: chat history, customer profiles, AI memory. In cloud SaaS those live on the vendor's side — service changes, account bans or compliance shifts can cut you off. Self-hosting keeps them on your machines, which is ChatX's default form.",
        },
      },
      {
        q: {
          zh: "两者的 AI 有什么本质区别？",
          en: "What is the essential AI difference?",
        },
        a: {
          zh: "智聊的 AI 是「数字员工」：稳定人设、长期记忆、情绪感知、主动关怀、克隆声语音，目标是长期经营关系并推进成交；电商向 SaaS 的 AI 多为导购/客服辅助（以各家官网为准）。",
          en: "ChatX's AI is a digital operator: stable persona, long-term memory, emotion awareness, proactive care, cloned-voice speech — built to run relationships and advance deals over time. Commerce SaaS AI is mostly shopping/support assistance (verify per vendor).",
        },
      },
      {
        q: {
          zh: "从 SleekFlow 迁移到智聊难吗？",
          en: "Is migrating from SleekFlow to ChatX hard?",
        },
        a: {
          zh: "渠道账号（WhatsApp/Telegram 等）本就属于你，重新接入即可；智聊免费开始（全功能 + 标准翻译不限量），可先并行试用再决定切换，不需要一次性迁移。",
          en: "Your channel accounts (WhatsApp/Telegram etc.) already belong to you — just reconnect them. ChatX is free to start (all features + unlimited standard translation), so you can run it in parallel before deciding, no big-bang migration needed.",
        },
      },
    ],
    disclaimer: {
      zh: "对比基于我方产品实况与对方官网公开资料的一般性理解（更新于 2026-08），可能滞后或有出入；对方具体功能与价格一律以其官网最新信息为准。SleekFlow 为其所有者商标。",
      en: "This comparison reflects our product as shipped and a general reading of the other party's public website (as of 2026-08); it may lag or differ. Always verify features and pricing on their official site. SleekFlow is a trademark of its owner.",
    },
  },

  wati: {
    slug: "wati",
    name: "Wati",
    tagline: {
      zh: "智聊 ChatX vs Wati：多渠道 AI 拟人经营 vs WhatsApp 单渠道客服工具",
      en: "ChatX vs Wati: multi-channel human-like AI vs WhatsApp-first support tooling",
    },
    answer: {
      zh: "一句话结论：Wati 是 WhatsApp 官方 API 单渠道客服/营销工具，适合以 WhatsApp 为唯一主渠道、要模板群发与目录的小团队；智聊 ChatX 覆盖 Telegram / WhatsApp / LINE / Messenger 等多渠道，核心差异是 AI 拟人化深度（人设/记忆/主动跟进/克隆声）与私有化数据主权。只做 WhatsApp 官方触达选前者，多渠道 AI 经营选后者。",
      en: "In one sentence: Wati is a WhatsApp-first support/marketing tool on the official API — a fit for small teams whose only main channel is WhatsApp and who need template broadcasts and catalogs; ChatX covers Telegram / WhatsApp / LINE / Messenger and differentiates on human-like AI depth (personas, memory, proactive follow-ups, cloned voice) plus self-hosted data sovereignty. Pick Wati for official WhatsApp-only messaging, ChatX for multi-channel AI operations.",
    },
    intro: {
      zh: [
        "Wati 是常见的 WhatsApp Business API 工具：共享收件箱、模板消息群发、商品目录与流程机器人（以其官网为准），入门价格对小团队友好。",
        "智聊 ChatX 解决的是「客户不止在 WhatsApp」的现实：跨境私域的客户散在 Telegram、LINE、Messenger、Instagram——多渠道聚合只是起点，AI 用稳定人设把每个渠道的客户当同一个人长期经营，才是成交率的来源。",
      ],
      en: [
        "Wati is a typical WhatsApp Business API tool: shared inbox, template broadcasts, catalogs and flow bots (see their site), with SMB-friendly entry pricing.",
        "ChatX addresses the reality that customers are not only on WhatsApp: cross-border private-domain customers live across Telegram, LINE, Messenger and Instagram. Aggregation is just the start — an AI that treats each customer as one person across channels, under one stable persona, is where conversion comes from.",
      ],
    },
    rows: [
      {
        dim: { zh: "产品定位", en: "Positioning" },
        us: {
          zh: "多平台 AI 拟人化关系运营系统（成交 + 陪伴双场景）。",
          en: "Multi-platform AI relationship-operations system (sales + companionship).",
        },
        them: {
          zh: "WhatsApp 官方 API 客服/营销工具（模板群发 + 目录 + 流程机器人，以官网为准）。",
          en: "WhatsApp-official-API support/marketing tool (template broadcasts + catalogs + flow bots; see their site).",
        },
      },
      {
        dim: { zh: "渠道覆盖", en: "Channel coverage" },
        us: {
          zh: "Telegram 协议号多账号 + WhatsApp / LINE / Messenger / Instagram / Zalo 等多渠道一个工作台。",
          en: "Multi-account Telegram protocol plus WhatsApp / LINE / Messenger / Instagram / Zalo in one workspace.",
        },
        them: {
          zh: "以 WhatsApp 单渠道为主（以官网渠道说明为准）。",
          en: "Primarily WhatsApp as the single channel (see their site).",
        },
      },
      {
        dim: { zh: "部署与数据主权", en: "Deployment & data ownership" },
        us: US_DEPLOY,
        them: THEM_SAAS_DATA,
      },
      {
        dim: { zh: "AI 能力", en: "AI capabilities" },
        us: US_AI,
        them: {
          zh: "以模板消息、关键词/流程机器人与 AI 辅助回复为主（以其官网最新功能为准）。",
          en: "Centered on template messaging, keyword/flow bots and AI-assisted replies (see their site for the latest).",
        },
      },
      {
        dim: { zh: "翻译", en: "Translation" },
        us: {
          zh: "内建双向实时拟人互译（入站原文+译文、出站自动按客户语言发出），标准翻译免费不限量。",
          en: "Built-in two-way human-like translation (inbound original + translation, outbound auto-translated per customer), standard tier free and unlimited.",
        },
        them: {
          zh: "以其官网多语言能力说明为准。",
          en: "See their site for multilingual capabilities.",
        },
      },
      {
        dim: { zh: "计费模式", en: "Pricing model" },
        us: US_PRICING,
        them: {
          zh: "订阅制（常见按席位起步 + WhatsApp 会话费另计，以官网价目为准）。",
          en: "Subscription (commonly per-seat entry + WhatsApp conversation fees billed separately; see their pricing page).",
        },
      },
      {
        dim: { zh: "适合谁", en: "Best fit" },
        us: {
          zh: "客户分布在多平台、要 AI 深度经营与数据主权的跨境私域团队。",
          en: "Cross-border private-domain teams with customers across platforms, wanting deep AI operations and data sovereignty.",
        },
        them: {
          zh: "以 WhatsApp 为唯一主渠道、以模板群发与目录为核心诉求的小团队。",
          en: "Small teams whose single main channel is WhatsApp, centered on broadcasts and catalogs.",
        },
      },
    ],
    faq: [
      {
        q: {
          zh: "Wati 和智聊 ChatX 怎么选？",
          en: "How do I choose between Wati and ChatX?",
        },
        a: {
          zh: "先看渠道：客户只在 WhatsApp、诉求是官方 API 模板群发与目录，Wati 这类单渠道工具够用；客户散在 Telegram / LINE / Messenger 等多平台、要 AI 像真人一样长期经营并推进成交，选智聊 ChatX。",
          en: "Start from channels: if customers are only on WhatsApp and you mainly need official-API broadcasts and catalogs, a WhatsApp-first tool like Wati suffices; if customers span Telegram / LINE / Messenger and you want an AI that nurtures and closes like a person, choose ChatX.",
        },
      },
      {
        q: {
          zh: "智聊支持 WhatsApp 吗，是什么形态？",
          en: "Does ChatX support WhatsApp, and in what form?",
        },
        a: {
          zh: "支持。WhatsApp 会话进统一收件箱，享受与其他渠道一致的 AI 拟稿、拟人互译、语音与人审链路；接入形态按部署场景择优（协议/网页会话等），以产品文档为准。",
          en: "Yes. WhatsApp conversations land in the unified inbox with the same AI drafting, human-like translation, voice and human-review pipeline as other channels; the connection form is chosen per deployment scenario (protocol/web session), see product docs.",
        },
      },
      {
        q: {
          zh: "WhatsApp 会话费是怎么回事，智聊怎么计费？",
          en: "What about WhatsApp conversation fees — how does ChatX bill?",
        },
        a: {
          zh: "官方 Business API 渠道由 Meta 按会话向使用方收费（各家工具通常转嫁该费用，以其价目为准）；智聊自身不收席位费与订阅费，AI 用量按 Token 充值（1U = 1,500 Token），标准翻译免费不限量。",
          en: "On the official Business API, Meta charges per conversation (tools typically pass this through; check each pricing page). ChatX itself has no seat or subscription fees — AI usage is pay-as-you-go tokens (1U = 1,500 tokens), standard translation free and unlimited.",
        },
      },
      {
        q: {
          zh: "有支持 Telegram 的 Wati 替代品吗？",
          en: "Is there a Wati alternative that supports Telegram?",
        },
        a: {
          zh: "智聊 ChatX 原生支持 Telegram 协议号多账号聚合（配频控与风控护栏），并同时覆盖 WhatsApp / LINE / Messenger 等渠道——适合从 WhatsApp 单渠道扩展到多渠道的团队。",
          en: "ChatX natively aggregates multi-account Telegram (with rate-control and risk guardrails) alongside WhatsApp / LINE / Messenger — a fit for teams expanding beyond a single WhatsApp channel.",
        },
      },
      {
        q: {
          zh: "小团队预算有限，智聊起步成本是多少？",
          en: "What does ChatX cost to start for a small team?",
        },
        a: {
          zh: "0 元起步：下载即用，全功能开放，标准翻译不限量，每月送 1,000 Token，注册再送 10,000 体验 Token；用顺后按需充值（新人 6U 大礼包 = 18,000 Token 双倍到账），无订阅无席位费。",
          en: "Zero to start: download and go with all features, unlimited standard translation, 1,000 tokens monthly plus 10,000 bonus tokens on signup; top up as needed later (newcomer 6U pack = 18,000 tokens at double rate), no subscription, no seat fees.",
        },
      },
    ],
    disclaimer: {
      zh: "对比基于我方产品实况与对方官网公开资料的一般性理解（更新于 2026-08），可能滞后或有出入；对方具体功能与价格一律以其官网最新信息为准。Wati 为其所有者商标。",
      en: "This comparison reflects our product as shipped and a general reading of the other party's public website (as of 2026-08); it may lag or differ. Always verify features and pricing on their official site. Wati is a trademark of its owner.",
    },
  },
};

/* ── /compare 枢纽页：「跨境电商 AI 客服工具怎么选（2026）」（实施77 渠道四）────
 *  GEO 结构：答案胶囊 + 五维选型框架表 + 工具速览表（链向各对比页）+ FAQ。
 *  措辞纪律同上：他家只写公开资料一般性理解，结论交给「按维度自查」。 */

export interface CompareHubTool {
  name: string;
  /** 中文页显示名（仅我方需要：竞品名一律拉丁字形不译）。实施78 P0-2：英文页不出中文字形。 */
  nameZh?: string;
  /** 一句话定位（公开资料口径） */
  positioning: CompareCell;
  /** 适合谁 */
  bestFor: CompareCell;
  /** 站内对比页（我方无） */
  compareHref?: string;
}

export const compareHub = {
  title: {
    zh: "跨境电商 AI 客服工具怎么选（2026 实战指南）",
    en: "How to choose an AI customer-chat tool for cross-border sales (2026 guide)",
  },
  answer: {
    zh: "选跨境 AI 客服/聊天工具，先回答三个问题：① 数据放哪——云 SaaS 托管还是自己机器（聊天记录与客户资料是核心资产）；② AI 的深度——只会被动答工单，还是能以稳定人设主动经营客户、推进成交；③ 计费方式——按席位/月活联系人订阅，还是用多少付多少。本指南给出五维选型框架，并逐一对比智聊 ChatX、respond.io、SaleSmartly、SleekFlow、Wati。",
    en: "To pick an AI customer-chat tool for cross-border sales, answer three questions first: (1) where the data lives — vendor cloud or your own machines (chats and customer profiles are core assets); (2) AI depth — reactive ticket answering vs proactively nurturing customers under a stable persona; (3) billing — seat/MAC subscriptions vs pay-as-you-go. This guide provides a five-dimension framework and compares ChatX, respond.io, SaleSmartly, SleekFlow and Wati.",
  },
  intro: {
    zh: [
      "2026 年的变化是明确的：聊天渠道从「客服成本中心」变成「营收主阵地」——东南亚多数零售商已有可观比例的销售直接发生在对话里，AI 则把「谁来聊」从人力问题变成了工具问题。",
      "工具没有绝对的好坏，只有与你业务形态的匹配度。下面的五个维度按重要性排序，每个维度都给出自查方法；文末是五款常见工具的速览与逐一对比入口。",
    ],
    en: [
      "The 2026 shift is clear: chat has moved from a support cost center to a revenue channel — a large share of cross-border sales now happens inside conversations, and AI turns 'who does the chatting' from a headcount problem into a tooling choice.",
      "There is no absolute best tool, only fit. The five dimensions below are ordered by importance, each with a self-check; a quick overview of five common tools with per-tool comparison links follows.",
    ],
  },
  framework: [
    {
      dim: { zh: "① 数据主权", en: "1. Data sovereignty" },
      check: {
        zh: "问清楚：聊天记录、客户画像、AI 记忆存在谁的机器上？服务商停服/封号/涨价时，这些资产能不能完整带走？私有化部署（数据不出机）是唯一的结构性答案，云 SaaS 则看导出能力与条款。",
        en: "Ask: whose machines hold the chats, customer profiles and AI memory? If the vendor shuts down, bans or reprices, can you take these assets with you? Self-hosting is the only structural answer; for cloud SaaS, scrutinize export capabilities and terms.",
      },
    },
    {
      dim: { zh: "② 渠道形态", en: "2. Channel form" },
      check: {
        zh: "官方商业 API（合规稳、按会话收费、以 WhatsApp 为代表）与协议/真机形态（覆盖 Telegram 个人号等私域场景）是两条路线。按你的客户实际在哪、获客怎么来选——不要按工具反推业务。",
        en: "Official business APIs (compliant, per-conversation fees, WhatsApp-style) and protocol/real-device forms (covering Telegram personal accounts and private-domain scenarios) are two routes. Choose by where your customers actually are — never bend the business to fit the tool.",
      },
    },
    {
      dim: { zh: "③ AI 深度", en: "3. AI depth" },
      check: {
        zh: "分三档自查：能答（FAQ/工单）→ 能翻（双向翻译像不像真人）→ 能经营（人设一致、记住客户、主动跟进、语音/图片表达）。第三档才能把 AI 从省成本变成产营收。",
        en: "Three tiers: answers (FAQ/tickets) → translates (does two-way translation read human?) → operates (consistent persona, remembers customers, proactive follow-ups, voice/image expression). Only the third tier turns AI from cost-saving into revenue-making.",
      },
    },
    {
      dim: { zh: "④ 翻译质量", en: "4. Translation quality" },
      check: {
        zh: "跨语言成交的关键不是「有翻译」而是「不像机翻」：客户能一眼看出的机翻腔会直接杀死信任。测法：拿你行业的真实话术做双向盲测，再看是否支持术语锁定与本地引擎。",
        en: "For cross-language closing the bar is not 'has translation' but 'doesn't read machine-translated' — obvious MT kills trust instantly. Test with real scripts from your niche in both directions, and check term-locking and local-engine options.",
      },
    },
    {
      dim: { zh: "⑤ 总拥有成本", en: "5. Total cost of ownership" },
      check: {
        zh: "把三块加起来算：订阅费（席位 × 月活联系人阶梯）+ 渠道费（如 WhatsApp 会话费）+ AI 用量费。低频起步选按量计费；规模稳定后拿 12 个月总账对比，不要只看月费标价。",
        en: "Sum three parts: subscription (seats × MAC tiers) + channel fees (e.g. WhatsApp conversations) + AI usage. Start pay-as-you-go at low volume; at steady scale compare 12-month totals, not sticker prices.",
      },
    },
  ] as { dim: CompareCell; check: CompareCell }[],
  tools: [
    {
      name: "ChatX",
      nameZh: "智聊 ChatX",
      positioning: {
        zh: "私有化 AI 拟人运营系统：多平台统一收件箱 + 拟人互译内建 + AI 主动经营成交；免费开始、按 Token 充值。",
        en: "Self-hostable AI relationship-operations system: unified multi-platform inbox + built-in human-like translation + proactive AI closing; free start, pay-as-you-go tokens.",
      },
      bestFor: {
        zh: "私域/成交型跨境团队、Telegram 多账号运营、数据主权硬要求",
        en: "Private-domain cross-border teams, multi-account Telegram, hard data-sovereignty needs",
      },
    },
    {
      name: "respond.io",
      positioning: {
        zh: "企业级云端全渠道客服 SaaS：官方 API 触达 + 团队协作流程（以官网为准）。",
        en: "Enterprise cloud omnichannel SaaS: official-API messaging + team workflows (see their site).",
      },
      bestFor: {
        zh: "中大型标准化客服团队",
        en: "Mid-to-large standardized support teams",
      },
      compareHref: "/compare/respond-io",
    },
    {
      name: "SaleSmartly",
      positioning: {
        zh: "跨境社媒聚合客服 SaaS：多渠道收件 + 翻译 + 流程自动化（以官网为准）。",
        en: "Cross-border social-inbox SaaS: multi-channel inbox + translation + flow automation (see their site).",
      },
      bestFor: {
        zh: "需要快速聚合客服消息的起步团队",
        en: "Teams that need messages unified quickly",
      },
      compareHref: "/compare/salesmartly",
    },
    {
      name: "SleekFlow",
      positioning: {
        zh: "社交电商向客服/营销 SaaS：Shopify 等电商集成 + 购物流程自动化（以官网为准）。",
        en: "Social-commerce messaging SaaS: Shopify-style integrations + shopping-flow automation (see their site).",
      },
      bestFor: {
        zh: "店铺型电商团队",
        en: "Storefront-centric e-commerce teams",
      },
      compareHref: "/compare/sleekflow",
    },
    {
      name: "Wati",
      positioning: {
        zh: "WhatsApp 官方 API 单渠道客服/营销工具：模板群发 + 目录（以官网为准）。",
        en: "WhatsApp-first official-API tool: template broadcasts + catalogs (see their site).",
      },
      bestFor: {
        zh: "WhatsApp 为唯一主渠道的小团队",
        en: "Small WhatsApp-only teams",
      },
      compareHref: "/compare/wati",
    },
  ] as CompareHubTool[],
  faq: [
    {
      q: {
        zh: "2026 年跨境电商 AI 客服工具怎么选？",
        en: "How should I choose an AI customer-chat tool for cross-border sales in 2026?",
      },
      a: {
        zh: "按五个维度依次自查：数据主权（数据放谁机器）、渠道形态（官方 API 还是协议/真机）、AI 深度（能答/能翻/能经营三档）、翻译质量（像不像真人）、总拥有成本（订阅+渠道费+AI 用量 12 个月总账）。前两个维度是结构性选择，选错后期迁移成本最高。",
        en: "Self-check five dimensions in order: data sovereignty (whose machines), channel form (official API vs protocol/real-device), AI depth (answers / translates / operates), translation quality (does it read human), and 12-month total cost (subscription + channel fees + AI usage). The first two are structural — wrong picks cost the most to migrate later.",
      },
    },
    {
      q: {
        zh: "有免费的多平台 AI 客服工具吗？",
        en: "Is there a free multi-platform AI customer-chat tool?",
      },
      a: {
        zh: "智聊 ChatX 提供永久免费档：下载即用、全功能开放、标准翻译不限量（公平使用 200 万字符/日）、每月 1,000 Token、注册再送 10,000 体验 Token，唯一限制是 1 个聊天账号。其他工具的免费档以各家官网为准。",
        en: "ChatX has a free-forever tier: download and go, all features, unlimited standard translation (fair use 2M chars/day), 1,000 tokens monthly plus 10,000 bonus on signup — the only cap is one chat account. For other tools, check each vendor's site.",
      },
    },
    {
      q: {
        zh: "AI 能自动用客户的母语回复吗？",
        en: "Can AI reply automatically in the customer's native language?",
      },
      a: {
        zh: "可以。以智聊为例：入站消息显示原文+译文，出站回复自动翻译成客户语言，AI 拟稿本身也按客户语言生成，配术语锁定与翻译记忆——目标是让客户以为在和母语者聊天。",
        en: "Yes. In ChatX, inbound messages show original plus translation, outbound replies auto-translate into the customer's language, and AI drafts are generated in that language with term-locking and translation memory — the goal is for customers to feel they're chatting with a native speaker.",
      },
    },
    {
      q: {
        zh: "Telegram 个人号可以多账号统一管理吗？",
        en: "Can multiple Telegram personal accounts be managed in one place?",
      },
      a: {
        zh: "可以，但要选协议号形态的工具：智聊原生支持多个 Telegram 协议号聚合到一个工作台（带频控与风控护栏）；以官方商业 API 为主的客服 SaaS 通常聚焦 WhatsApp 等渠道，TG 个人号支持以各家官网为准。",
        en: "Yes, with protocol-based tools: ChatX natively aggregates multiple Telegram protocol accounts into one workspace (with rate-control and risk guardrails). Official-API-centric SaaS usually focuses on WhatsApp-style channels — check each vendor for TG personal-account support.",
      },
    },
    {
      q: {
        zh: "云端 SaaS 和私有化部署怎么选？",
        en: "Cloud SaaS or self-hosted — how to decide?",
      },
      a: {
        zh: "看两点：数据资产敏感度与运维意愿。要零维护快速起步选云端（智聊也提供云端托管：一客户一实例、独立子域）；客户数据是核心资产、或有合规/防封号考量的团队选私有化——数据不出机，服务商变化不影响资产。",
        en: "Two factors: data sensitivity and ops appetite. For zero-ops quick starts choose cloud (ChatX also offers hosting: one isolated instance per customer, own subdomain); teams whose customer data is a core asset — or with compliance/ban-risk concerns — should self-host, keeping assets independent of any vendor.",
      },
    },
    {
      q: {
        zh: "怎么评估 AI 客服工具的实际效果？",
        en: "How do I evaluate an AI chat tool's real effect?",
      },
      a: {
        zh: "用自己的真实会话测三件事：① 拿 20 条历史客户消息看 AI 拟稿采用率；② 双向翻译盲测给懂该语言的人挑毛病；③ 试用期盯「首响时长、回复率、成交转化」三个数字的前后对比——工具好不好，两周数据比任何评测文都可信。",
        en: "Test with your own real conversations: (1) feed 20 historical customer messages and measure AI-draft adoption; (2) blind-test two-way translation with a native reader; (3) during trial, compare first-response time, reply rate and conversion before vs after. Two weeks of your own data beats any review article.",
      },
    },
  ] as CompareFaq[],
  disclaimer: {
    zh: "本指南中他方工具的描述基于其官网公开资料的一般性理解（更新于 2026-08），可能滞后或有出入，具体功能与价格以各官网最新信息为准；respond.io、SaleSmartly、SleekFlow、Wati 均为其各自所有者商标。我方（智聊 ChatX）描述为已交付实况。",
    en: "Descriptions of third-party tools reflect a general reading of their public websites (as of 2026-08) and may lag or differ — verify features and pricing on each official site. respond.io, SaleSmartly, SleekFlow and Wati are trademarks of their respective owners. ChatX descriptions reflect the product as shipped.",
  },
} as const;
