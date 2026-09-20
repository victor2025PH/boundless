import type { LegalSection } from "@/components/LegalShell";
import { CONTACT_EMAIL, TELEGRAM_DISPLAY } from "@/lib/site";

export const LEGAL_UPDATED = "2026-06-13";

export const privacyTitle = { zh: "隐私政策", en: "Privacy Policy" };
export const termsTitle = { zh: "服务条款", en: "Terms of Service" };

export const privacySections: LegalSection[] = [
  {
    h: { zh: "我们收集的信息", en: "Information we collect" },
    p: {
      zh: [
        "咨询/留资信息：你主动提交的称呼、联系方式（Telegram / WhatsApp / 邮箱等）、咨询意向与备注。",
        "使用数据：匿名的页面浏览、按钮点击等统计事件，用于了解站点使用情况并改进产品。",
        "技术数据：访问时的 IP（截断保存）、浏览器 User-Agent、来源页等，用于安全与防滥用。",
        "Telegram 信息：当你通过 Telegram Mini App 访问时，我们会读取 Telegram 提供的基础身份（如用户 ID、用户名）以完成验证与服务。",
      ],
      en: [
        "Inquiry/lead data: the name, contact (Telegram / WhatsApp / email, etc.), interest and notes you choose to submit.",
        "Usage data: anonymous events such as page views and button clicks, used to understand usage and improve the product.",
        "Technical data: truncated IP, browser user-agent and referrer at access time, for security and abuse prevention.",
        "Telegram data: when you access via the Telegram Mini App, we read basic identity (e.g. user ID, username) provided by Telegram to complete verification and service.",
      ],
    },
  },
  {
    h: { zh: "Cookie 与本地存储", en: "Cookies & local storage" },
    p: {
      zh: [
        "我们使用浏览器本地存储记住你的语言偏好与 Cookie 同意状态。",
        "管理后台使用一个仅限服务端读取（httpOnly）的会话 Cookie 用于登录，不用于追踪访客。",
        "我们不使用第三方广告 Cookie。",
      ],
      en: [
        "We use browser local storage to remember your language preference and cookie-consent choice.",
        "The admin dashboard uses one httpOnly session cookie for login only; it is not used to track visitors.",
        "We do not use third-party advertising cookies.",
      ],
    },
  },
  {
    h: { zh: "我们如何使用信息", en: "How we use information" },
    p: {
      zh: [
        "回应你的咨询、提供报价与方案、交付与支持你购买的服务。",
        "运营与改进网站、衡量内容与活动效果。",
        "保障安全、防止欺诈与滥用，遵守适用的法律义务。",
      ],
      en: [
        "Respond to inquiries, provide quotes and proposals, and deliver and support the services you purchase.",
        "Operate and improve the site, and measure content and campaign performance.",
        "Ensure security, prevent fraud and abuse, and comply with applicable legal obligations.",
      ],
    },
  },
  {
    h: { zh: "第三方服务", en: "Third-party services" },
    p: {
      zh: [
        "Telegram：用于客服沟通、机器人与 Mini App。你与我们的对话受 Telegram 隐私政策约束。",
        "AI 模型服务商：站内 AI 问答会将你的提问内容发送给第三方大模型 API 以生成回复，请勿在对话中提交敏感个人信息。",
        "我们不会将你的联系方式出售给第三方。",
      ],
      en: [
        "Telegram: used for support chat, the bot and the Mini App. Your conversations with us are subject to Telegram's privacy policy.",
        "AI model provider: the on-site AI assistant sends your questions to a third-party large-language-model API to generate replies; please do not submit sensitive personal data in the chat.",
        "We do not sell your contact details to third parties.",
      ],
    },
  },
  {
    h: { zh: "数据留存与你的权利", en: "Retention & your rights" },
    p: {
      zh: [
        "我们仅在为上述目的所必需的期间内保留你的数据。",
        "你可以请求查询、更正或删除你的个人信息——通过下方联系方式联系我们即可。",
      ],
      en: [
        "We retain your data only for as long as necessary for the purposes above.",
        "You may request access to, correction of, or deletion of your personal data — contact us via the details below.",
      ],
    },
  },
  {
    h: { zh: "联系我们", en: "Contact us" },
    p: {
      zh: ["隐私相关问题请通过 Telegram 客服 @WJKJ2026 联系我们。"],
      en: ["For privacy questions, contact our Telegram support @WJKJ2026."],
    },
  },
];

export const termsSections: LegalSection[] = [
  {
    h: { zh: "服务说明", en: "Service description" },
    p: {
      zh: [
        "无界科技 BOUNDLESS 提供 AI 技术服务，覆盖 AI 换脸、声音克隆、实时直播换脸换声、实时换语言互译，以及 AI 自动成交聊天等能力，并提供无界底座 BOUNDLESS Engine 私有化部署的选型、部署、定制与支持。",
        "具体交付内容、规格与时效以双方在下单沟通中确认的方案为准。",
      ],
      en: [
        "BOUNDLESS provides AI technical services covering AI face swap, voice cloning, real-time live face & voice swap, real-time language translation, and AI auto-closing chat, plus BOUNDLESS Engine private deployment — including selection, deployment, customization and support.",
        "The exact deliverables, specifications and timelines are those confirmed between both parties during the ordering conversation.",
      ],
    },
  },
  {
    h: { zh: "结算与付款", en: "Billing & payment" },
    p: {
      zh: [
        "服务以 USDT 结算（支持 TRC20 / ERC20）。具体金额、周期与里程碑在下单时确认。",
        "除另有书面约定外，已开始交付或已部署的定制服务不予退款。",
      ],
      en: [
        "Services are settled in USDT (TRC20 / ERC20 supported). Amounts, cycles and milestones are confirmed at ordering.",
        "Unless otherwise agreed in writing, customized services that have begun delivery or been deployed are non-refundable.",
      ],
    },
  },
  {
    h: { zh: "可接受使用", en: "Acceptable use" },
    p: {
      zh: [
        "你须确保对所提供的素材（人脸、声音、数据等）拥有合法授权，并对其使用承担责任。",
        "你不得将我们的服务用于违反所在地法律法规的用途，包括但不限于诈骗、冒充、侵犯他人肖像/声音/隐私权等。",
        "因你违规使用导致的一切后果由你自行承担。",
      ],
      en: [
        "You must ensure you hold lawful authorization for any material you provide (faces, voices, data, etc.) and are responsible for its use.",
        "You must not use our services for purposes that violate the laws of your jurisdiction, including but not limited to fraud, impersonation, or infringement of others' likeness/voice/privacy rights.",
        "You bear all consequences arising from your non-compliant use.",
      ],
    },
  },
  {
    h: { zh: "免责声明", en: "Disclaimer" },
    p: {
      zh: [
        "服务按“现状”提供。在适用法律允许的最大范围内，我们不对因使用或无法使用服务而产生的间接或后果性损失承担责任。",
        "示例数据、案例与 ROI 测算仅供参考，不构成对具体业务结果的承诺或保证。",
      ],
      en: [
        "Services are provided “as is”. To the maximum extent permitted by law, we are not liable for indirect or consequential losses arising from use or inability to use the services.",
        "Sample data, cases and ROI estimates are for reference only and do not constitute a promise or guarantee of specific business results.",
      ],
    },
  },
  {
    h: { zh: "条款变更与联系", en: "Changes & contact" },
    p: {
      zh: [
        "我们可能不时更新本条款，更新后在本页公布即生效。",
        "如有疑问，请通过 Telegram 客服 @WJKJ2026 联系我们。",
      ],
      en: [
        "We may update these terms from time to time; updates take effect once posted on this page.",
        "For questions, contact our Telegram support @WJKJ2026.",
      ],
    },
  },
];

// ── 危机干预协议（WP-4 合规模式配套模板页，2026-08-17）─────────────────────────
// 供部署智聊 ChatX 的运营方公示（加州 SB 243「危机协议公示」义务的模板落点）。
// 【运营方填写：…】占位处必须按属地实情替换后方可作为正式公示件；引擎侧
// compliance.crisis_protocol_url 配置本页 URL 后，披露语会自动带出本页链接。

export const crisisProtocolTitle = {
  zh: "危机干预协议（AI 陪伴服务）",
  en: "Crisis Intervention Protocol (AI Companion Service)",
};

export const crisisProtocolSections: LegalSection[] = [
  {
    h: { zh: "目的与适用范围", en: "Purpose & scope" },
    p: {
      zh: [
        "本协议说明由 AI 助理协助的聊天服务在识别到用户可能处于自我伤害或其他心理危机时的响应流程。",
        "本页为运营方公示模板：标注【运营方填写】的内容须由服务运营方按其所在法域与实际资源替换后生效。",
      ],
      en: [
        "This protocol describes how our AI-assisted chat service responds when a user may be at risk of self-harm or another mental-health crisis.",
        "This page is a deployer-facing template: items marked [to be completed by the operator] must be replaced by the service operator according to their jurisdiction and actual resources.",
      ],
    },
  },
  {
    h: { zh: "AI 参与披露", en: "AI involvement disclosure" },
    p: {
      zh: [
        "本服务的对话由 AI 助理协助生成，人工团队可随时接入。新会话首条回复会向用户作出披露。",
      ],
      en: [
        "Conversations in this service are assisted by an AI assistant; a human team can step in at any time. The first reply of a new conversation discloses this to the user.",
      ],
    },
  },
  {
    h: { zh: "危机信号的识别", en: "How crisis signals are detected" },
    p: {
      zh: [
        "系统对每条用户消息运行确定性的危机识别（含自我伤害相关表达的多语种检测），识别能力由常驻自动化测试守护（severe 级召回率要求 100%，惯用语不误报）。",
        "识别结果分级处理：severe（高危）触发完整干预流程；elevated（升高）触发预防性安全指令。",
      ],
      en: [
        "Every user message runs through deterministic crisis detection (multi-language coverage of self-harm expressions), guarded by permanent automated tests (100% recall required on severe cases, no false alarms on idioms).",
        "Signals are tiered: severe triggers the full intervention flow; elevated injects preventive safety instructions.",
      ],
    },
  },
  {
    h: { zh: "干预措施", en: "Intervention measures" },
    p: {
      zh: [
        "任何可能鼓励自我伤害的 AI 回复会在发出前被安全回复整段覆盖（出站硬红线，常驻测试钉住「终态输出 100% 不含鼓励自伤内容」）。",
        "识别为高危时，回复会附上危机求助资源（热线信息，见下节），并以共情、非评判的语气回应。",
        "【运营方填写：人工升级路径——何种情形下由人工团队接管、响应时限、值班安排。】",
      ],
      en: [
        "Any AI reply that could encourage self-harm is fully overridden by a safe reply before sending (a hard outbound red line, pinned by permanent tests: final output must contain zero self-harm-encouraging content).",
        "On severe signals, the reply includes crisis-support resources (hotlines, next section) and responds with an empathetic, non-judgmental tone.",
        "[To be completed by the operator: human escalation path — when the human team takes over, response SLA, on-call arrangements.]",
      ],
    },
  },
  {
    h: { zh: "危机转介资源", en: "Crisis referral resources" },
    p: {
      zh: [
        "【运营方填写：按服务用户所在地区列出危机热线名称与号码，例如所在国家/地区的自杀干预热线、心理援助热线；建议至少覆盖主要服务地区。】",
        "以上资源同时配置在系统内，高危对话中由系统自动提供给用户。",
      ],
      en: [
        "[To be completed by the operator: list crisis hotline names and numbers for the regions you serve — e.g. national suicide-prevention or mental-health support lines; cover at least your primary service regions.]",
        "The same resources are configured inside the system and are provided to users automatically during severe conversations.",
      ],
    },
  },
  {
    h: { zh: "记录与年度报告", en: "Records & annual reporting" },
    p: {
      zh: [
        "系统对危机处置持久计数（高危识别次数 / 安全覆盖次数 / 资源提供次数，按日累计、可导出），供运营方履行年度报告义务（如加州 SB 243 自 2027 年起的年报要求）。",
        "计数不含对话原文，仅为聚合数字。",
      ],
      en: [
        "The system keeps persistent counters of crisis handling (severe detections / safe-reply overrides / resource referrals, daily buckets, exportable) so operators can meet annual-reporting duties (e.g. California SB 243 reports starting 2027).",
        "Counters contain aggregate numbers only — never conversation content.",
      ],
    },
  },
  {
    h: { zh: "联系方式与法域说明", en: "Contact & jurisdiction note" },
    p: {
      /* 实施78 P0-6：这两行原是未填模板占位符（「【运营方填写…】」/「[To be completed
         by the operator…]」），而两个页面（/compliance/crisis-protocol 与 /en 侧）都已
         线上可访问 200——我们把合规当卖点，自己的合规页却写着「待填」，是可信度硬伤。
         现在先填本站真实联系人，同时保留该段的模板用途（客户自行部署时要换成自己的）。
         邮箱行按 lib/site.CONTACT_EMAIL 判空：没开通就不出现，绝不写一个会退信的地址。 */
      zh: [
        `本站服务的合规联系人：无界科技 BOUNDLESS（服务提供方），即时通讯 ${TELEGRAM_DISPLAY}` +
          (CONTACT_EMAIL ? `，邮箱 ${CONTACT_EMAIL}` : "") +
          "。",
        "若你把本工具部署给自己的客户使用，请把上一行替换为**你自己**的合规联系人——披露、公示与年报的法定义务主体是服务运营方（deployer），不是工具提供方。",
        "本模板不构成法律意见；运营方应根据其属地法规（如 EU AI Act 第 50 条、加州 SB 243、纽约 GBL §1700）对内容作最终审定。",
      ],
      en: [
        `Compliance contact for this site: BOUNDLESS (service provider), messaging ${TELEGRAM_DISPLAY}` +
          (CONTACT_EMAIL ? `, email ${CONTACT_EMAIL}` : "") +
          ".",
        "If you deploy this toolkit for your own customers, replace the line above with **your own** compliance contact — the legal duty for disclosure, public notice and annual reporting sits with the service operator (deployer), not the tool vendor.",
        "This template is not legal advice; operators should finalize the content per their local regulations (e.g. EU AI Act Art. 50, California SB 243, New York GBL §1700).",
      ],
    },
  },
];

// ── 合规能力页（P2 销售件，2026-08-18）：三部已生效法规 → 产品开关与证据面 ──
// 内容与 docs/智聊合规能力说明_EUAIAct50_SB243_NY1700_2026-08.md 同源收敛（销售
// 发这页链接替代 md 附件）；只写已实装能力，法规义务表述保持「运营方=deployer
// 承担、我们提供工具」的责任划分口径。

export const complianceCapabilityTitle = {
  zh: "合规能力（EU AI Act · 加州 SB 243 · 纽约 GBL §1700）",
  en: "Compliance capabilities (EU AI Act · CA SB 243 · NY GBL §1700)",
};

export const complianceCapabilitySections: LegalSection[] = [
  {
    h: { zh: "一句话", en: "In one sentence" },
    p: {
      zh: [
        "针对已生效的三部 AI 对话法规，智聊把「披露、诚实身份、危机干预、年报计数」做成了产品开关与可导出的证据面——合规不再是上线阻碍，而是您对客户的采购优势。",
        "本页不构成法律意见；披露等义务的责任主体是服务运营方（deployer），我们提供工具与证据能力。",
      ],
      en: [
        "For three AI-conversation regulations already in force, ChatX ships disclosure, honest identity, crisis intervention and annual-report counting as product switches plus exportable evidence — compliance becomes a procurement advantage instead of a launch blocker.",
        "This page is not legal advice; disclosure obligations rest with the service operator (deployer). We provide the tooling and the evidence surfaces.",
      ],
    },
  },
  {
    h: { zh: "法规风险地图", en: "The regulatory map" },
    p: {
      zh: [
        "EU AI Act 第 50 条（2026-08-02 起适用）：与 AI 交互须在首次交互时披露；违反最高可罚 1500 万欧元或全球营业额 3%。",
        "加州 SB 243（2026-01-01 生效）：陪伴型聊天机器人须披露 AI 身份、公示危机干预协议，并自 2027 年起按年报告危机转介数据；赋予用户私诉权。",
        "纽约 GBL §1700 系列：AI 伴侣服务的披露与安全义务。",
      ],
      en: [
        "EU AI Act Art. 50 (applies from 2026-08-02): users must be informed at first interaction that they are interacting with AI; fines up to €15M or 3% of global turnover.",
        "California SB 243 (effective 2026-01-01): companion chatbots must disclose AI identity, publish a crisis-intervention protocol, report crisis-referral numbers annually from 2027, and users get a private right of action.",
        "New York GBL §1700 family: disclosure and safety duties for AI companion services.",
      ],
    },
  },
  {
    h: { zh: "能力一：系统级披露语", en: "Capability 1: system-level disclosure" },
    p: {
      zh: [
        "开关开启后，每个会话的首条 AI 出站消息前置披露语（客服团队公告口吻，非法务腔），九种语言按客户语言自动选取，运营方可整条覆写。",
        "覆盖全部 AI 出站形态：三条回复链路与主动触达（首触即披露）；每会话只发一次，标记持久落盘，重启不重发；克隆语音不念披露语，由首条文本补上。",
      ],
      en: [
        "When enabled, the first AI outbound message of every conversation is prefixed with a disclosure line (support-team tone, not legalese), auto-selected across nine languages by the customer's language, fully overridable by the operator.",
        "It covers every AI outbound path: all three reply pipelines plus proactive outreach (disclosure at first touch); sent once per conversation with a durable mark that survives restarts; cloned voice never speaks the line — the first text reply carries it.",
      ],
    },
  },
  {
    h: { zh: "能力二：诚实身份模式", en: "Capability 2: honest-identity mode" },
    p: {
      zh: [
        "开启后，用户直接问「你是 AI 吗」会得到如实回答：身份否认类硬约束被跳过、守卫层放行如实自认，人设的其余性格与边界完全不变。",
        "披露与诚实身份是两个独立开关——多数 B 端客户开披露、按需开诚实身份（法规要求「不误导」，不要求每句自认）。",
      ],
      en: [
        "When enabled, a direct \"are you an AI?\" gets an honest answer: identity-denial constraints are skipped and the outbound guard allows truthful self-identification, while every other persona trait stays intact.",
        "Disclosure and honest identity are independent switches — most B2B deployments enable disclosure and opt into honest identity as needed (the law requires non-deception, not per-message confession).",
      ],
    },
  },
  {
    h: { zh: "能力三：危机识别→干预→资源闭环", en: "Capability 3: crisis detection → intervention → resources" },
    p: {
      zh: [
        "内置危机识别（严重程度分级、惯用语防误报）、安全回复覆盖（触红线的输出被整段替换）、热线资源保障（严重会话确保出现一次转介资源），主动消息在危机窗口内自动抑制。",
        "整条链路由常驻自动化评测门禁守护（识别召回、响应覆盖、资源保障、主动抑制四层），每次代码变更自动回归——不是一次性验收，是持续在测的安全能力。",
      ],
      en: [
        "Built-in crisis detection (severity tiers, idiom-safe), safe-reply override (red-line outputs are fully replaced), hotline-resource assurance (severe conversations always surface referral resources once), and proactive messages are suppressed inside crisis windows.",
        "The whole chain is guarded by always-on automated evaluation gates (detection recall, response override, resource assurance, proactive suppression) that re-run on every code change — a continuously tested safety capability, not a one-off audit.",
      ],
    },
  },
  {
    h: { zh: "能力四：年报证据面", en: "Capability 4: annual-report evidence" },
    p: {
      zh: [
        "危机转介计数持久落盘（按日、按转介类型），管理端只读接口一键导出——SB 243 自 2027 年起的年报数字有据可查。",
        "披露执行情况同样可查：已披露会话计数与合规开关现状经同一接口回显，排障与合同附件采数一个入口。",
      ],
      en: [
        "Crisis-referral counts persist to disk (daily buckets, by referral kind) with a read-only admin endpoint for export — the numbers SB 243 requires annually from 2027 are always at hand.",
        "Disclosure execution is equally auditable: disclosed-conversation counts and the live switch states are returned by the same endpoint — one surface for troubleshooting and contract-exhibit data.",
      ],
    },
  },
  {
    h: { zh: "能力五：危机协议公示页模板", en: "Capability 5: crisis-protocol template page" },
    p: {
      zh: [
        "我们提供双语《危机干预协议》公示页模板（见本站 /compliance/crisis-protocol），运营方替换【运营方填写】占位后即可作为 SB 243 要求的公示页使用；产品内会员页可直接配置指向运营方自己的公示地址。",
      ],
      en: [
        "A bilingual crisis-intervention protocol template is provided (see /compliance/crisis-protocol on this site); operators replace the [to be completed] placeholders and publish it to satisfy SB 243's publication duty. The in-product membership page links to the operator's own URL.",
      ],
    },
  },
  {
    h: { zh: "责任划分与默认状态", en: "Responsibility split & defaults" },
    p: {
      zh: [
        "披露、公示与年报的法定义务主体是服务运营方（deployer）；我们作为工具提供方交付开关、多语文案、计数与导出能力，并在文档中写明部署与验证步骤。",
        "全部合规开关出厂默认关闭：不改变既有部署的任何行为，由运营方按属地法规显式开启（配置组 compliance.*，开启即时生效）。",
      ],
      en: [
        "The legal duties of disclosure, publication and annual reporting rest with the service operator (deployer); as the tool vendor we ship the switches, multilingual copy, counting and export, with documented deployment and verification steps.",
        "All compliance switches default to OFF: existing deployments are untouched until the operator explicitly enables them per local regulation (config group compliance.*, effective immediately).",
      ],
    },
  },
  {
    h: { zh: "验证方式（给采购与法务）", en: "How to verify (for procurement & legal)" },
    p: {
      zh: [
        "开启披露开关后：任意新会话首条 AI 回复带披露语且同会话不再重复；管理接口回显开关状态、已披露会话数与转介计数。",
        "评测门禁清单、工程实现映射表与责任划分详见《智聊合规能力说明》文档，可作为销售合同引用件；需要演示或试用可从下载页开始。",
      ],
      en: [
        "With disclosure on: the first AI reply of any new conversation carries the line exactly once per conversation; the admin endpoint echoes switch states, disclosed-conversation counts and referral counts.",
        "The evaluation-gate inventory, engineering mapping and responsibility split are detailed in the ChatX Compliance Capability document, referenceable in sales contracts; start from the download page for a demo or trial.",
      ],
    },
  },
];
