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

export interface CompareSpec {
  slug: string;
  /** 对方产品显示名（不译） */
  name: string;
  tagline: CompareCell;
  intro: { zh: string[]; en: string[] };
  rows: CompareRow[];
  disclaimer: CompareCell;
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

const US_PRICING: CompareCell = {
  zh: "USD 订阅三档：Entry $58 / Team $198 / Flagship $598 每月，支持 USDT 结算；私有化部署不按月活联系人（MAU）加价。",
  en: "Three USD plans: Entry $58 / Team $198 / Flagship $598 per month, USDT accepted; self-hosted — no per-MAU surcharges.",
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
    disclaimer: {
      zh: "对比基于我方产品实况与对方官网公开资料的一般性理解（更新于 2026-08），可能滞后或有出入；对方具体功能与价格一律以其官网最新信息为准。SaleSmartly 为其所有者商标。",
      en: "This comparison reflects our product as shipped and a general reading of the other party's public website (as of 2026-08); it may lag or differ. Always verify features and pricing on their official site. SaleSmartly is a trademark of its owner.",
    },
  },
};
