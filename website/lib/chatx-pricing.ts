// 智聊 ChatX / 通译 LingoX「按充值计费」单一真相（2026-08-21 充值唯一化改版）。
//
// 体系（2026-08-21 决议：订阅全线停售，只留「免费开始 + 按充值计费」两层）：
//   免费开始（非 SKU 档位）：下载即用 + 标准翻译永久免费不限量 + 每月 1,000 Token +
//   注册再送 10,000 体验 Token（1 个聊天账号防薅护栏是唯一上限）。
//   充值八档（唯一付费通道）：6U 新人包 + 50/100/200/500/1000/5000/10000 USD；
//   1 USD = 1,500 Token；首笔充值按到账金额向下取档一次性加赠 +5/10/20/30/35/40%
//   （每人一次，服务端判定，退款回收加赠）；新人包 6U = 18,000 Token（2 倍率，
//   注册 72 小时内，每账号一次，不占用首充资格）。
//   大额档服务权益：5000U=专属客户经理/发票合同/优先支持；10000U 另含团队用量
//   分账报表 + API 对接支持（权益写在 RechargeTier.perks，展示层零手写）。
//   企业两形态 lead-based 面议（不走自助结算，见 ENTERPRISE_TRACKS）：
//   企业合作年框（协议价/月结/发票/SLA）+ 企业级私有化部署（一次性实施 + 年授权维保，
//   本地模型数据不出网——承接旧旗舰版「可选私有化」叙事）。
//   标准翻译（内置引擎）永久免费不限量（公平使用 2,000,000 字符/日/授权）。
//   充值实付 Token 12 个月有效（≥500U 档 24 个月）；赠送部分 6 个月且先扣；
//   存量订阅（停售台账）履约期内扣减顺序仍为 先赠送、再订阅含量、后充值实付。
//
// 单源纪律：新体系价格/额度/费率**只改本文件**；
//   - lib/pricing.ts 的 tokenPackOffers / translateOffers 由本文件派生（schema.org 兼容形状）；
//   - lib/order-lines.ts 的下单档位卡由本文件派生；
//   - lib/content.ts 首页套餐区 / app/pricing 报价页 / lib/compare-content.ts 竞品对照全部派生取数；
//   - products/zhiliao/product.yaml + platform/licensing/sku_registry.json 与本文件同批修改。
// 停售台账：chatx-personal(39)/chatx-pro(99)/chatx-flagship(598) 订阅三档自 2026-08-21 停售
//   （存量按期履约到期转充值）——LEGACY_CHATX_PLANS 与 lib/pricing.ts legacy 数组仅供
//   台账/历史订单反查与门禁形状断言，不进任何页面展示或 JSON-LD。
// 更早停售：chatx-team-seat(49/坐席) 与 token-pack-s/m/l/xl 自 2026-08-20 停售，
//   chatx-entry(58)/chatx-team(198)/lingox-charpack(59)/lingox-team(99)/lingox-pro(198)
//   自 2026-08-19 停售——全部仅存 lib/pricing.ts legacy 数组 + offer-map 映射。

export type Lang = "zh" | "en";

/* ── Token 计价表（对外公示口径；引擎侧 token_ledger 以同表执行）───────────── */

export interface TokenRate {
  key: string;
  /** 动作名 */
  action: { zh: string; en: string };
  /** 每单位消耗 Token 数（0 = 永久免费） */
  tokens: number;
  /** 计量单位 */
  unit: { zh: string; en: string };
  note?: { zh: string; en: string };
}

export const TOKEN_RATES: TokenRate[] = [
  {
    key: "std_translate",
    action: { zh: "标准翻译（内置引擎）", en: "Standard translation (built-in engine)" },
    tokens: 0,
    unit: { zh: "不限字符", en: "unlimited chars" },
    note: { zh: "永久免费 · 公平使用 200 万字符/日", en: "Free forever · fair use 2M chars/day" },
  },
  {
    key: "ai_reply",
    action: { zh: "AI 智能回复（人设/记忆/情绪全链）", en: "AI reply (persona / memory / emotion)" },
    tokens: 10,
    unit: { zh: "条", en: "message" },
  },
  {
    key: "pro_translate",
    action: { zh: "专业翻译（术语锁定 + 翻译记忆）", en: "Pro translation (term-lock + memory)" },
    tokens: 10,
    unit: { zh: "千字符", en: "1k chars" },
  },
  {
    key: "deepl_translate",
    action: { zh: "认证翻译（DeepL 引擎）", en: "Certified translation (DeepL)" },
    tokens: 40,
    unit: { zh: "千字符", en: "1k chars" },
  },
  {
    key: "voice_clone",
    action: { zh: "克隆语音合成", en: "Cloned-voice synthesis" },
    tokens: 10,
    unit: { zh: "100 字符", en: "100 chars" },
    note: { zh: "比 ElevenLabs 同量便宜约一半", en: "~half the cost of ElevenLabs" },
  },
  {
    key: "ai_image",
    action: { zh: "AI 配图 / 人设自拍", en: "AI image / persona selfie" },
    tokens: 50,
    unit: { zh: "张", en: "image" },
  },
  {
    key: "asr",
    action: { zh: "语音转写（ASR）", en: "Speech-to-text (ASR)" },
    tokens: 5,
    unit: { zh: "分钟", en: "minute" },
  },
];

export function tokenRate(key: string): TokenRate {
  const r = TOKEN_RATES.find((x) => x.key === key);
  if (!r) throw new Error(`chatx-pricing: unknown token rate ${key}`);
  return r;
}

/** 免费标准翻译的公平使用上限（字符/日/授权）——营销文案与引擎限流同一数字。 */
export const FREE_TRANSLATE_FAIR_USE_CHARS_PER_DAY = 2_000_000;

/** 注册即送体验 Token（一次性）：与现行引擎试用发放 100 万字符**精确等值**
 *（100 万字符 × 专业翻译 10 Token/千字符 = 10,000 Token）——P2 Token 钱包上线时 1:1 并账，
 *  官网口径与引擎发放零冲突。试用口径全站唯一：不再有「7 天」「顾问试用码」两套旧说法。 */
export const SIGNUP_BONUS_TOKENS = 10_000;

/* ── 免费开始（非 SKU 档位：所有付费档的共同起点，不是「订阅第一档」）─────────── */

export const CHATX_FREE = {
  /** 每月免费 Token（自然月刷新，不结转） */
  tokensMonthly: 1_000,
  /** 唯一防滥用上限：1 个聊天账号 */
  accountCap: 1,
  signupBonusTokens: SIGNUP_BONUS_TOKENS,
} as const;

/* ── 停售订阅台账（2026-08-21 起不进任何页面/JSON-LD；勿删）────────────────────
 *  仅供：① 历史订单/授权反查（配合 lib/pricing.ts legacy 数组与 offer-map）；
 *  ② 门禁形状断言（scripts/check-content-integrity.mjs 抽 skuId/edition/monthly 三连、
 *     scripts/assert-order-lines.mjs 抽订阅价与注册表比对）——字段顺序勿动。 */

export interface ChatxPlan {
  /** /order?plan= 深链与 POST /api/order 的 plan 参数 */
  key: string;
  /** platform/licensing/sku_registry.json 关联键（null = 不产生订单，如免费版） */
  skuId: string | null;
  /** 引擎授权档（订单 edition 字段；chengjie 侧另有 plan 映射） */
  edition: "trial" | "standard" | "pro" | "enterprise";
  /** 月价 USD */
  monthly: number;
  custom?: boolean;
  hot?: boolean;
  name: { zh: string; en: string };
  audience: { zh: string; en: string };
  blurb: { zh: string; en: string };
  feats: { zh: string[]; en: string[] };
  /** 每月含 Token（当月有效不结转） */
  tokensMonthly: number;
}

export const LEGACY_CHATX_PLANS: ChatxPlan[] = [
  {
    key: "autochat-free",
    skuId: "chatx-free",
    edition: "trial",
    monthly: 0,
    name: { zh: "免费开始", en: "Free start" },
    audience: { zh: "下载即用 · 翻译永久免费", en: "Free forever · translation included" },
    blurb: { zh: "全功能开放 + 每月 1,000 Token", en: "All features + 1,000 tokens/mo" },
    feats: { zh: [], en: [] },
    tokensMonthly: 1_000,
  },
  {
    key: "autochat-personal",
    skuId: "chatx-personal",
    edition: "standard",
    monthly: 39,
    name: { zh: "基础版（停售）", en: "Basic (legacy)" },
    audience: { zh: "停售 2026-08-21", en: "Discontinued 2026-08-21" },
    blurb: { zh: "原每月 60,000 Token", en: "Was 60,000 tokens/mo" },
    feats: { zh: [], en: [] },
    tokensMonthly: 60_000,
  },
  {
    key: "autochat-pro",
    skuId: "chatx-pro",
    edition: "pro",
    monthly: 99,
    name: { zh: "专业版（停售）", en: "Pro (legacy)" },
    audience: { zh: "停售 2026-08-21", en: "Discontinued 2026-08-21" },
    blurb: { zh: "原每月 200,000 Token", en: "Was 200,000 tokens/mo" },
    feats: { zh: [], en: [] },
    tokensMonthly: 200_000,
  },
  {
    key: "autochat-flagship",
    skuId: "chatx-flagship",
    edition: "enterprise",
    monthly: 598,
    name: { zh: "旗舰版（停售）", en: "Max (legacy)" },
    audience: { zh: "停售 2026-08-21 · 私有化叙事由企业级部署承接", en: "Discontinued 2026-08-21" },
    blurb: { zh: "原每月 1,500,000 Token + 本地模型不限", en: "Was 1.5M tokens/mo + unlimited local models" },
    feats: { zh: [], en: [] },
    tokensMonthly: 1_500_000,
  },
];

/* ── Token 充值（2026-08-21 起唯一付费通道）──────────────────────────────────
 *  1 USD = 1,500 Token（基准 $0.667/千）；首笔充值按到账金额**向下取档**一次性加赠，
 *  每人一次（账号 + 支付指纹 + 设备指纹三合一，服务端判定，退款回收加赠）；
 *  复充按 **VIP 累充等级**加赠（实施50 P2 落地，取代「复充回归基准价」）：
 *  累计已付充值达门槛后，之后每笔复充自动 +3%/+5%/+8%（VIP_REPEAT_BONUS_TIERS，
 *  与引擎 chatx_fulfillment.VIP_REPEAT_BONUS_TIERS 跨仓门禁钉死）。
 *  有效期：实付 Token 12 个月（≥500U 档 24 个月）；赠送 Token 6 个月且先扣。 */

export const RECHARGE_TOKENS_PER_USD = 1_500;

/** VIP 累充等级：累计已付充值（USD）≥ fromUsd → 复充加赠 pct%。
 *  首充不参与（首充走一次性 +5%~40% 阶梯）；档位判定在履约侧按订单台账现算。 */
export const VIP_REPEAT_BONUS_TIERS: ReadonlyArray<{ fromUsd: number; pct: number }> = [
  { fromUsd: 500, pct: 3 },
  { fromUsd: 2000, pct: 5 },
  { fromUsd: 10000, pct: 8 },
];

export interface RechargeTier {
  /** /order?plan= 键 = registry sku_id（recharge-*） */
  key: string;
  skuId: string;
  /** 充值金额 USD（同时是挂牌价） */
  price: number;
  /** 首充一次性加赠百分比（复充为 0） */
  firstBonusPct: number;
  hot?: boolean;
  name: { zh: string; en: string };
  /** 大额档（≥5000U）附带的服务权益；展示层由此派生，勿另写清单。 */
  perks?: { zh: string[]; en: string[] };
}

export const RECHARGE_TIERS: RechargeTier[] = [
  { key: "recharge-50", skuId: "recharge-50", price: 50, firstBonusPct: 0, name: { zh: "50U 起充档", en: "50U starter" } },
  { key: "recharge-100", skuId: "recharge-100", price: 100, firstBonusPct: 5, name: { zh: "100U 档", en: "100U" } },
  { key: "recharge-200", skuId: "recharge-200", price: 200, firstBonusPct: 10, hot: true, name: { zh: "200U 档", en: "200U" } },
  { key: "recharge-500", skuId: "recharge-500", price: 500, firstBonusPct: 20, name: { zh: "500U 档", en: "500U" } },
  { key: "recharge-1000", skuId: "recharge-1000", price: 1000, firstBonusPct: 30, name: { zh: "1000U 档", en: "1000U" } },
  {
    key: "recharge-5000", skuId: "recharge-5000", price: 5000, firstBonusPct: 35,
    name: { zh: "5000U 工作室档", en: "5000U studio" },
    perks: {
      zh: ["专属客户经理", "发票 / 合同 / 对公", "优先支持"],
      en: ["Dedicated account manager", "Invoice / contract / corporate billing", "Priority support"],
    },
  },
  {
    key: "recharge-10000", skuId: "recharge-10000", price: 10000, firstBonusPct: 40,
    name: { zh: "10000U 旗舰档", en: "10000U flagship" },
    perks: {
      zh: ["5000U 档权益全含", "团队用量分账报表", "API 对接支持"],
      en: ["Everything in 5000U", "Team usage split reports", "API integration support"],
    },
  },
];

/** 新人首充大礼包：6U = 18,000 Token（2 倍率）；注册 72 小时内、每账号一次；
 *  刻意**不占用**首充加赠资格——新人包是首笔小额转化钩，加赠阶梯是首笔大额抬单器。 */
export const NEWBIE_PACK = {
  key: "recharge-newbie-6",
  skuId: "recharge-newbie-6",
  price: 6,
  tokens: 18_000,
  windowHours: 72,
  name: { zh: "新人首充大礼包", en: "Newcomer pack" },
} as const;

/** 实付 Token 有效期（月）；≥LARGE_FROM 的大额档放宽（否则大额加赠是画饼）。 */
export const RECHARGE_VALID_MONTHS = 12;
export const RECHARGE_VALID_MONTHS_LARGE = 24;
export const RECHARGE_LARGE_FROM_USD = 500;
/** 赠送 Token 有效期（月）；扣费顺序=先赠送、（存量订阅含量）、后充值实付。 */
export const BONUS_VALID_MONTHS = 6;

export function rechargeTier(key: string): RechargeTier {
  const t = RECHARGE_TIERS.find((x) => x.key === key);
  if (!t) throw new Error(`chatx-pricing: unknown recharge tier ${key}`);
  return t;
}

/** 基础到账（不含加赠）。 */
export function rechargeBaseTokens(t: RechargeTier): number {
  return t.price * RECHARGE_TOKENS_PER_USD;
}

/** 首充到账（含一次性加赠）。 */
export function rechargeFirstTokens(t: RechargeTier): number {
  return Math.round(rechargeBaseTokens(t) * (1 + t.firstBonusPct / 100));
}

/** 每千 Token 单价（USD，两位小数）。 */
export function rechargeUnitPrice(priceUsd: number, tokens: number): number {
  return Math.round((priceUsd / tokens) * 1000 * 100) / 100;
}

/** 该档有效期（月）。 */
export function rechargeValidMonths(t: RechargeTier): number {
  return t.price >= RECHARGE_LARGE_FROM_USD ? RECHARGE_VALID_MONTHS_LARGE : RECHARGE_VALID_MONTHS;
}

/** 下一个加赠档（UI「再充 X 升到 +Y%」提示）；已到顶返回 null。 */
export function nextRechargeTier(t: RechargeTier): RechargeTier | null {
  const idx = RECHARGE_TIERS.findIndex((x) => x.key === t.key);
  for (let i = idx + 1; i < RECHARGE_TIERS.length; i++) {
    if (RECHARGE_TIERS[i].firstBonusPct > t.firstBonusPct) return RECHARGE_TIERS[i];
  }
  return null;
}

/* ── 企业两形态（lead-based 面议：不产生自助订单，CTA=联系商务）───────────────
 *  「企业合作」= 还是用云端服务只是量大（年框协议价，充值体系的顶端延伸）；
 *  「企业级部署」= 整套引擎部署进客户内网（一次性实施 + 年授权维保），
 *  承接旧旗舰版「可选私有化 + 本地模型不限量」叙事。 */

export interface EnterpriseTrack {
  key: "enterprise-coop" | "private-deploy";
  /** sku_registry.json 台账键（lead 成交后人工履约挂账用，不进自助结算） */
  skuId: string;
  name: { zh: string; en: string };
  tagline: { zh: string; en: string };
  points: { zh: string[]; en: string[] };
}

export const ENTERPRISE_TRACKS: EnterpriseTrack[] = [
  {
    key: "enterprise-coop",
    skuId: "chatx-enterprise",
    name: { zh: "企业合作 · 年框", en: "Enterprise partnership" },
    tagline: { zh: "年用量超过 10000U？一次谈清协议价", en: "Beyond 10000U a year? Lock an annual frame deal" },
    points: {
      zh: ["年框协议价（建议 ≥20,000U 起谈）", "月结 · 对公 · 发票", "专属 SLA 与客户成功", "多席位与子账号管理"],
      en: ["Annual frame pricing (from ~20,000U)", "Monthly settlement · corporate billing · invoices", "Dedicated SLA & customer success", "Multi-seat & sub-account management"],
    },
  },
  {
    key: "private-deploy",
    skuId: "chatx-private-deploy",
    name: { zh: "企业级私有化部署", en: "Private deployment" },
    tagline: { zh: "整套引擎部署进你的内网，数据不出网", en: "The full engine inside your own network" },
    points: {
      zh: ["部署在你自己的服务器 / 内网 GPU", "本地模型 Token 不限量", "一次性实施 + 年授权维保", "实施培训 · 升级通道 · SLA"],
      en: ["Runs on your servers / on-prem GPUs", "Unlimited local-model tokens", "One-time setup + annual license & care", "Onboarding training · upgrade channel · SLA"],
    },
  },
];

/* ── 展示派生（营销卡 / 报价页 / JSON-LD 全部由此取数，零手写数字）─────────── */

/** 首页产品卡报价行（content.ts Solution.pricing 同构：plan/price/detail/order）。 */
export function chatxPlanRows(lang: Lang): { plan: string; price: string; detail: string; order?: string }[] {
  const zh = lang === "zh";
  const hot = RECHARGE_TIERS.find((t) => t.hot) ?? RECHARGE_TIERS[0];
  const maxPct = RECHARGE_TIERS[RECHARGE_TIERS.length - 1].firstBonusPct;
  return [
    {
      plan: zh ? "免费开始" : "Free start",
      price: zh ? "0" : "0",
      detail: zh
        ? `下载即用 · 标准翻译免费不限量 · 每月 ${CHATX_FREE.tokensMonthly.toLocaleString("en-US")} Token，注册再送 ${SIGNUP_BONUS_TOKENS.toLocaleString("en-US")}`
        : `Download & go · unlimited standard translation · ${CHATX_FREE.tokensMonthly.toLocaleString("en-US")} tokens/mo + ${SIGNUP_BONUS_TOKENS.toLocaleString("en-US")} on signup`,
    },
    {
      plan: zh ? "新人 6U 大礼包" : "Newcomer 6U pack",
      price: String(NEWBIE_PACK.price),
      detail: zh
        ? `${NEWBIE_PACK.tokens.toLocaleString("en-US")} Token 双倍到账 · 注册 ${NEWBIE_PACK.windowHours} 小时内 · 每账号一次`
        : `${NEWBIE_PACK.tokens.toLocaleString("en-US")} tokens at double rate · within ${NEWBIE_PACK.windowHours}h of signup`,
      order: NEWBIE_PACK.key,
    },
    {
      plan: zh ? "Token 充值" : "Top-up",
      price: zh ? `${RECHARGE_TIERS[0].price}U 起` : `from ${RECHARGE_TIERS[0].price}U`,
      detail: zh
        ? `1U = ${RECHARGE_TOKENS_PER_USD.toLocaleString("en-US")} Token · 首充最高 +${maxPct}% · 充多少用多少不订阅`
        : `1U = ${RECHARGE_TOKENS_PER_USD.toLocaleString("en-US")} tokens · first top-up up to +${maxPct}% · no subscription`,
      order: hot.key,
    },
    {
      plan: zh ? "企业合作 / 私有化部署" : "Enterprise / private deploy",
      price: zh ? "面议" : "Custom",
      detail: zh
        ? "年框协议价 · 月结发票 · 或整套部署进你的内网（数据不出网）"
        : "Annual frame pricing · invoices · or the full engine deployed in your network",
    },
  ];
}

/** 翻译卡报价行（免费主张 + 工作台 + Token 计价）。 */
export function translateRows(lang: Lang): { plan: string; price: string; detail: string; order?: string }[] {
  const zh = lang === "zh";
  const pro = tokenRate("pro_translate");
  return [
    {
      plan: zh ? "标准翻译" : "Standard",
      price: zh ? "永久免费" : "Free forever",
      detail: zh
        ? "内置引擎 · 不限字符（公平使用 200 万字符/日）· 所有用户含"
        : "Built-in engine · unlimited chars (fair use 2M/day) · included for everyone",
      order: "autochat-free",
    },
    {
      plan: zh ? "专业翻译" : "Pro translate",
      price: zh ? `${pro.tokens} Token / 千字符` : `${pro.tokens} tokens / 1k chars`,
      detail: zh
        ? "术语锁定 + 翻译记忆 + 多模态（图/语音）· 认证引擎（DeepL）40 Token/千字符"
        : "Term-lock + memory + multimodal · certified DeepL engine at 40 tokens/1k chars",
      order: RECHARGE_TIERS[1].key,
    },
    {
      plan: zh ? "翻译工作台" : "Workbench",
      price: zh ? `${LINGOX_WORKBENCH.monthly} / 月 / 坐席` : `${LINGOX_WORKBENCH.monthly}/mo/seat`,
      detail: zh
        ? "纯翻译团队：多坐席收件箱 + 客户 journey + 漏斗计数"
        : "Translation-only teams: shared inbox + journey + funnel counter",
      order: LINGOX_WORKBENCH.key,
    },
  ];
}

/** 充值档展示行（首页/下单面板紧凑位）。 */
export function rechargeRows(lang: Lang): { plan: string; price: string; detail: string; order: string }[] {
  const zh = lang === "zh";
  return RECHARGE_TIERS.map((t) => {
    const first = rechargeFirstTokens(t);
    return {
      plan: t.name[lang],
      price: String(t.price),
      detail: zh
        ? `首充到账 ${first.toLocaleString("en-US")} Token${t.firstBonusPct ? `（+${t.firstBonusPct}%）` : ""} · 复充 ${rechargeBaseTokens(t).toLocaleString("en-US")} · ${rechargeValidMonths(t)} 个月有效`
        : `First top-up ${first.toLocaleString("en-US")} tokens${t.firstBonusPct ? ` (+${t.firstBonusPct}%)` : ""} · repeat ${rechargeBaseTokens(t).toLocaleString("en-US")} · valid ${rechargeValidMonths(t)} months`,
      order: t.key,
    };
  });
}

/** 首页套餐区卡片（components/Plans.tsx 消费；2026-08-21 起为充值卡形状，无月/年切换）。 */
export interface PlanCardItem {
  name: string;
  /** 主价格文本（"0" / "6" / "200" / "面议"） */
  price: string;
  /** 价格单位行（"USD · 一次性" / "免费" / "联系商务"…） */
  unit: string;
  desc: string;
  features: string[];
  highlight?: boolean;
  /** /order 深链 plan；免费开始走 href 直达下载页；两者皆无 = 联系商务 */
  plan?: string;
  href?: string;
}

export function chatxPlanCardItems(lang: Lang): PlanCardItem[] {
  const zh = lang === "zh";
  const fmtN = (n: number) => n.toLocaleString("en-US");
  const hot = RECHARGE_TIERS.find((t) => t.hot) ?? RECHARGE_TIERS[2];
  const big = rechargeTier("recharge-1000");
  const aiReply = tokenRate("ai_reply").tokens;
  return [
    {
      name: zh ? "免费开始" : "Free start",
      price: "0",
      unit: zh ? "永久免费" : "free forever",
      desc: zh ? "下载即用 · 翻译永久免费" : "Download & go · translation included",
      features: zh
        ? [
            "全功能开放 · 与付费用户同款能力",
            "标准翻译免费 · 不限字符",
            `每月 ${fmtN(CHATX_FREE.tokensMonthly)} Token（约 ${fmtN(CHATX_FREE.tokensMonthly / aiReply)} 条 AI 回复）`,
            `注册再送 ${fmtN(SIGNUP_BONUS_TOKENS)} 体验 Token`,
            "1 个聊天账号（唯一的防滥用上限）",
          ]
        : [
            "All features unlocked — same as paying users",
            "Unlimited standard translation",
            `${fmtN(CHATX_FREE.tokensMonthly)} tokens/mo (~${fmtN(CHATX_FREE.tokensMonthly / aiReply)} AI replies)`,
            `${fmtN(SIGNUP_BONUS_TOKENS)} bonus tokens on signup`,
            "1 chat account (the only anti-abuse cap)",
          ],
      href: zh ? "/download/chatx" : "/en/download/chatx",
    },
    {
      name: zh ? "新人 6U 大礼包" : "Newcomer 6U pack",
      price: String(NEWBIE_PACK.price),
      unit: zh ? "USD · 一次性" : "USD one-time",
      desc: zh ? `注册 ${NEWBIE_PACK.windowHours} 小时内 · 每账号一次` : `Within ${NEWBIE_PACK.windowHours}h of signup · once`,
      features: zh
        ? [
            `${fmtN(NEWBIE_PACK.tokens)} Token 双倍到账（$0.33/千）`,
            `约 ${fmtN(NEWBIE_PACK.tokens / aiReply)} 条 AI 回复的量`,
            "不占用首充加赠资格",
            "跨智聊 / 通译同一钱包",
          ]
        : [
            `${fmtN(NEWBIE_PACK.tokens)} tokens at double rate ($0.33/1k)`,
            `~${fmtN(NEWBIE_PACK.tokens / aiReply)} AI replies' worth`,
            "Doesn't consume your first-top-up bonus",
            "One wallet across ChatX & LingoX",
          ],
      plan: NEWBIE_PACK.key,
    },
    {
      name: zh ? `充值 ${hot.price}U` : `Top up ${hot.price}U`,
      price: String(hot.price),
      unit: zh ? "USD · 一次性" : "USD one-time",
      desc: zh ? "个人 / 小团队主力档" : "The solo & small-team workhorse",
      features: zh
        ? [
            `首充到账 ${fmtN(rechargeFirstTokens(hot))} Token（+${hot.firstBonusPct}%）`,
            `复充 ${fmtN(rechargeBaseTokens(hot))} · 1U = ${fmtN(RECHARGE_TOKENS_PER_USD)}`,
            `实付 ${rechargeValidMonths(hot)} 个月有效`,
            "用尽自动降级免费引擎，永不断线",
          ]
        : [
            `First top-up ${fmtN(rechargeFirstTokens(hot))} tokens (+${hot.firstBonusPct}%)`,
            `Repeat ${fmtN(rechargeBaseTokens(hot))} · 1U = ${fmtN(RECHARGE_TOKENS_PER_USD)}`,
            `Paid tokens valid ${rechargeValidMonths(hot)} months`,
            "Graceful fallback to free engines — never offline",
          ],
      highlight: true,
      plan: hot.key,
    },
    {
      name: zh ? `充值 ${big.price}U` : `Top up ${big.price}U`,
      price: String(big.price),
      unit: zh ? "USD · 一次性" : "USD one-time",
      desc: zh ? "重度团队 · 越充越省" : "Heavy teams · bigger is cheaper",
      features: zh
        ? [
            `首充到账 ${fmtN(rechargeFirstTokens(big))} Token（+${big.firstBonusPct}%）`,
            `≈ $${rechargeUnitPrice(big.price, rechargeFirstTokens(big))}/千 Token`,
            `实付 ${rechargeValidMonths(big)} 个月有效`,
            `更高档：5000U +35% · 10000U +40%`,
          ]
        : [
            `First top-up ${fmtN(rechargeFirstTokens(big))} tokens (+${big.firstBonusPct}%)`,
            `≈ $${rechargeUnitPrice(big.price, rechargeFirstTokens(big))}/1k tokens`,
            `Paid tokens valid ${rechargeValidMonths(big)} months`,
            "Higher tiers: 5000U +35% · 10000U +40%",
          ],
      plan: big.key,
    },
    {
      name: zh ? "企业 · 合作与部署" : "Enterprise",
      price: zh ? "面议" : "Custom",
      unit: zh ? "联系商务" : "contact sales",
      desc: zh ? "年框协议 / 私有化部署" : "Annual frame / private deployment",
      features: zh
        ? ["年框协议价 · 月结 · 发票", "企业级私有化部署（数据不出网）", "本地模型 Token 不限量", "专属 SLA 与客户成功"]
        : ["Annual frame pricing · invoices", "Private deployment (data stays on-prem)", "Unlimited local-model tokens", "Dedicated SLA & customer success"],
    },
  ];
}

/** JSON-LD offers（schema.org Offer 源数据；lib/pricing.ts toSchemaOffer 消费）。
 *  2026-08-21 起只出一次性充值商品——停售订阅不进结构化数据。 */
export function chatxSchemaOffers(): {
  id: string;
  skuId?: string;
  name: string;
  price: string;
  currency: "USD";
  unit: "month" | "one-time";
  description: string;
}[] {
  const recharges = RECHARGE_TIERS.map((t) => ({
    id: t.key,
    skuId: t.skuId,
    name: `Token top-up ${t.price}U`,
    price: String(t.price),
    currency: "USD" as const,
    unit: "one-time" as const,
    description: `One-time top-up; ${rechargeBaseTokens(t).toLocaleString("en-US")} tokens (first top-up ${rechargeFirstTokens(t).toLocaleString("en-US")} with +${t.firstBonusPct}% once-per-person bonus), valid ${rechargeValidMonths(t)} months, shared across ChatX & LingoX.`,
  }));
  const newbie = {
    id: NEWBIE_PACK.key,
    skuId: NEWBIE_PACK.skuId,
    name: "Newcomer token pack 6U",
    price: String(NEWBIE_PACK.price),
    currency: "USD" as const,
    unit: "one-time" as const,
    description: `One-time; ${NEWBIE_PACK.tokens.toLocaleString("en-US")} tokens at double rate, within ${NEWBIE_PACK.windowHours}h of signup, once per account.`,
  };
  return [...recharges, newbie];
}

/* ── 通译 LingoX（翻译免费化后的两个新形状）──────────────────────────────── */

/** 翻译工作台（纯翻译团队订阅，每坐席/月）：多坐席收件箱 / journey / 漏斗。 */
export const LINGOX_WORKBENCH = {
  key: "translate-workbench",
  skuId: "lingox-workbench",
  edition: "standard" as const,
  monthly: 29,
  perSeat: { min: 1, max: 50 },
  name: { zh: "翻译工作台", en: "Translation Workbench" },
  blurb: {
    zh: "纯翻译团队按坐席订阅：多坐席统一收件箱 · 客户 journey · 漏斗计数（AI 成交能力见智聊充值档）",
    en: "Per-seat plan for translation-only teams: shared inbox, customer journey, funnel counter",
  },
};

/* ── 用量计算器（/pricing 页交互模型；纯函数便于门禁）──────────────────────── */

export interface UsageInput {
  /** 日均 AI 回复条数 */
  repliesPerDay: number;
  /** 月均专业翻译千字符数 */
  proTranslateKCharsPerMonth: number;
  /** 日均克隆语音条数（按均 40 字符/条折算） */
  voiceMsgsPerDay: number;
  /** 月均 AI 配图张数 */
  imagesPerMonth: number;
}

export const AVG_VOICE_CHARS = 40;

/** 月度 Token 用量估算（计费口径与 TOKEN_RATES 完全一致）。 */
export function estimateMonthlyTokens(u: UsageInput): number {
  const replies = Math.max(0, u.repliesPerDay) * 30 * tokenRate("ai_reply").tokens;
  const pro = Math.max(0, u.proTranslateKCharsPerMonth) * tokenRate("pro_translate").tokens;
  const voice = Math.max(0, u.voiceMsgsPerDay) * 30 * (AVG_VOICE_CHARS / 100) * tokenRate("voice_clone").tokens;
  const images = Math.max(0, u.imagesPerMonth) * tokenRate("ai_image").tokens;
  return Math.round(replies + pro + voice + images);
}

/** 充值基准单价（USD/千 Token）。 */
export function rechargeMarginalPerK(): number {
  return Math.round((1000 / RECHARGE_TOKENS_PER_USD) * 100) / 100;
}

/** 按基准价折算的月成本（USD/月）。 */
export function rechargeOnlyMonthlyCost(tokensPerMonth: number): number {
  if (tokensPerMonth <= 0) return 0;
  return Math.round(((tokensPerMonth / 1000) * rechargeMarginalPerK() + Number.EPSILON) * 10) / 10;
}

/** 某档位按当前月用量的续航（月）：first=首充口径 / repeat=复充口径。 */
export interface RechargeAdvice {
  tier: RechargeTier;
  monthsFirst: number;
  monthsRepeat: number;
}

function monthsOf(tokens: number, tokensPerMonth: number): number {
  if (tokensPerMonth <= 0) return Infinity;
  return Math.round((tokens / tokensPerMonth) * 10) / 10;
}

export function rechargeCoverage(t: RechargeTier, tokensPerMonth: number): RechargeAdvice {
  return {
    tier: t,
    monthsFirst: monthsOf(rechargeFirstTokens(t), tokensPerMonth),
    monthsRepeat: monthsOf(rechargeBaseTokens(t), tokensPerMonth),
  };
}

/** 推荐充值档：能撑满 ≥1 个月（复充口径，保守）的最小档；用量再大取最大档。
 *  用量为 0 → 最小档（免得推荐空转）。 */
export function recommendRechargeTier(tokensPerMonth: number): RechargeAdvice {
  if (tokensPerMonth <= 0) return rechargeCoverage(RECHARGE_TIERS[0], tokensPerMonth);
  for (const t of RECHARGE_TIERS) {
    if (rechargeBaseTokens(t) >= tokensPerMonth) return rechargeCoverage(t, tokensPerMonth);
  }
  return rechargeCoverage(RECHARGE_TIERS[RECHARGE_TIERS.length - 1], tokensPerMonth);
}
