// 智聊 ChatX / 通译 LingoX「Token 分层报价」单一真相（2026-08-19 定价决议）。
//
// 体系（ChatGPT 式分层 + Token 耗材双轮）：
//   免费版 0 / 个人版 39 / 按量版 Flex（0 月费纯钱包）/ 团队版 49/坐席（≥2 席）/ 旗舰 598；
//   统一耗材货币 Token（跨 ChatX/LingoX 同一钱包）；标准翻译（内置引擎）永久免费不限量
//   （公平使用 2,000,000 字符/日/授权）；专业翻译（术语锁定/翻译记忆/认证引擎/多模态）耗 Token。
//   年付 = 月价 × 10（送 2 个月）——全线唯一公式，废除旧「85 折折合月价」双口径。
//   订阅含量当月有效不结转；Token 包 12 个月有效；扣减顺序 = 先订阅含量后 Token 包。
//
// 单源纪律：新体系价格/额度/费率**只改本文件**；
//   - lib/pricing.ts 的 autochatOffers / tokenPackOffers / translateOffers 由本文件派生（schema.org 兼容形状）；
//   - lib/order-lines.ts 的下单档位卡由本文件派生；
//   - lib/content.ts 首页套餐区 / app/pricing 报价页 / lib/compare-content.ts 竞品对照全部派生取数；
//   - products/zhiliao/product.yaml + products/tongyi/product.yaml（sku_registry 源）与本文件同批修改。
// 旧 chatx-entry(58)/chatx-team(198)/lingox-charpack(59)/lingox-team(99)/lingox-pro(198)
// 自 2026-08-19 停售，仅存 lib/pricing.ts legacy 数组供台账/历史订单反查。
import { ANNUAL_MONTHS, QUARTER_MONTHS, type Period } from "./avatarhub-pricing";

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

/* ── 档位（Free / Personal / Flex / Team / Flagship）───────────────────────── */

export interface ChatxPlan {
  /** /order?plan= 深链与 POST /api/order 的 plan 参数 */
  key: string;
  /** platform/licensing/sku_registry.json 关联键（null = 不产生订单，如免费版） */
  skuId: string | null;
  /** 引擎授权档（订单 edition 字段；chengjie 侧另有 plan 映射） */
  edition: "trial" | "standard" | "pro" | "enterprise";
  /** 月价 USD；perSeat 档 = 每坐席月价；custom 档无挂牌价 */
  monthly: number;
  /** 按坐席计价（团队版）：应付 = monthly × seats */
  perSeat?: { min: number; max: number };
  /** 按量版：无月费，用量走 Token 钱包（购买入口 = Token 包） */
  wallet?: boolean;
  custom?: boolean;
  hot?: boolean;
  name: { zh: string; en: string };
  audience: { zh: string; en: string };
  blurb: { zh: string; en: string };
  feats: { zh: string[]; en: string[] };
  /** 每月含 Token（perSeat 档 = 每坐席入池；0 = 无含量） */
  tokensMonthly: number;
  /** 聊天账号数（perSeat 档 = 每坐席，池共享） */
  accounts: number;
  platforms: "one" | "all";
  seats: number | "per-seat" | "custom";
}

export const CHATX_PLANS: ChatxPlan[] = [
  {
    key: "autochat-free",
    skuId: "chatx-free",
    edition: "trial",
    monthly: 0,
    name: { zh: "免费版 Free", en: "Free" },
    audience: { zh: "下载即用 · 翻译永久免费", en: "Free forever · translation included" },
    blurb: { zh: "标准翻译不限量 + 每月 1,000 Token", en: "Unlimited standard translation + 1,000 tokens/mo" },
    feats: {
      zh: ["标准翻译免费 · 不限字符", "1 个聊天账号 · 1 个平台", "每月 1,000 Token（约 100 条 AI 回复）", "注册再送 10,000 体验 Token", "统一收件箱 · 人审工作台"],
      en: ["Unlimited standard translation", "1 chat account · 1 platform", "1,000 tokens/mo (~100 AI replies)", "10,000 bonus tokens on signup", "Unified inbox · review workspace"],
    },
    tokensMonthly: 1_000,
    accounts: 1,
    platforms: "one",
    seats: 1,
  },
  {
    key: "autochat-personal",
    skuId: "chatx-personal",
    edition: "standard",
    monthly: 39,
    name: { zh: "个人版 Personal", en: "Personal" },
    audience: { zh: "单人创业者 / 跨境个体", en: "Solo founders & sellers" },
    blurb: { zh: "3 账号全平台 + 每月 30,000 Token", en: "3 accounts, all platforms + 30,000 tokens/mo" },
    feats: {
      zh: ["3 个聊天账号 · 全平台", "每月 30,000 Token（约 100 条 AI 回复/天）", "3 个 AI 人设 · 1 个克隆音色", "人设相册 · 主动跟进", "标准翻译免费 · 不限字符"],
      en: ["3 chat accounts · all platforms", "30,000 tokens/mo (~100 AI replies/day)", "3 AI personas · 1 cloned voice", "Persona albums · proactive follow-up", "Unlimited standard translation"],
    },
    tokensMonthly: 30_000,
    accounts: 3,
    platforms: "all",
    seats: 1,
  },
  {
    key: "autochat-flex",
    skuId: null, // 无订阅 SKU：开通 = 免费版 + 购买任意 Token 包（token-pack-*）
    edition: "standard",
    monthly: 0,
    wallet: true,
    name: { zh: "按量版 Flex", en: "Flex (pay-as-you-go)" },
    audience: { zh: "用量波动大 · 用多少付多少", en: "Spiky usage · pay for what you use" },
    blurb: { zh: "0 月费 · 全功能 · 纯 Token 钱包扣费", en: "No monthly fee · full features · wallet only" },
    feats: {
      zh: ["0 月费 · 预充 Token 包即用", "个人版全部功能", "3 个聊天账号 · 全平台", "Token 12 个月有效", "标准翻译免费 · 不限字符"],
      en: ["No monthly fee — top up a token pack", "Everything in Personal", "3 chat accounts · all platforms", "Tokens valid 12 months", "Unlimited standard translation"],
    },
    tokensMonthly: 0,
    accounts: 3,
    platforms: "all",
    seats: 1,
  },
  {
    key: "autochat-team-seat",
    skuId: "chatx-team-seat",
    edition: "pro",
    monthly: 49,
    perSeat: { min: 2, max: 50 },
    hot: true,
    name: { zh: "团队版 Team", en: "Team" },
    audience: { zh: "跨境销售 / 客服团队", en: "Cross-border sales & support teams" },
    blurb: { zh: "每坐席 49/月（≥2 席）· 每席 5 账号 + 50,000 Token 入池", en: "$49/seat/mo (2+ seats) · 5 accounts + 50k tokens per seat, pooled" },
    feats: {
      zh: ["每坐席 5 个聊天账号（池共享）", "每坐席 50,000 Token / 月（入池共享）", "10 个 AI 人设 · 每席 1 克隆音色", "权限 / 审计 / 团队看板 · 周报", "优先支持"],
      en: ["5 chat accounts per seat (pooled)", "50,000 tokens/seat/mo (pooled)", "10 AI personas · 1 cloned voice per seat", "Roles / audit / team dashboards", "Priority support"],
    },
    tokensMonthly: 50_000,
    accounts: 5,
    platforms: "all",
    seats: "per-seat",
  },
  {
    key: "autochat-flagship",
    skuId: "chatx-flagship",
    edition: "enterprise",
    monthly: 598,
    name: { zh: "旗舰 · 私有化", en: "Flagship · Private" },
    audience: { zh: "企业级 · 数据不出网", en: "Enterprise · on-prem data" },
    blurb: { zh: "50 账号 · 本地模型 Token 不限 · 可私有化", en: "50 accounts · unlimited local-model tokens · private deploy" },
    feats: {
      zh: ["50 个聊天账号 · 坐席不限", "本地模型 Token 不限量（云模型按量）", "人工接管 · 数据看板 · API", "可选私有化部署 · 数据不出网", "专属对接 · SLA"],
      en: ["50 chat accounts · unlimited seats", "Unlimited local-model tokens (cloud metered)", "Human takeover · dashboards · API", "Optional private deployment", "Dedicated support · SLA"],
    },
    tokensMonthly: 0,
    accounts: 50,
    platforms: "all",
    seats: "custom",
  },
];

export function chatxPlan(key: string): ChatxPlan {
  const p = CHATX_PLANS.find((x) => x.key === key);
  if (!p) throw new Error(`chatx-pricing: unknown plan ${key}`);
  return p;
}

/** 挂牌付费订阅档（进 JSON-LD / PriceOffer 派生；免费/按量档不进）。 */
export const CHATX_PAID_PLANS = CHATX_PLANS.filter((p) => !p.custom && !p.wallet && p.monthly > 0);

/* ── Token 包（一次性 · 12 个月有效 · 跨 ChatX/LingoX 通用）────────────────── */

export interface TokenPack {
  /** /order?plan= 键 = registry sku_id（token-pack-*） */
  key: string;
  skuId: string;
  price: number;
  tokens: number;
  hot?: boolean;
  name: { zh: string; en: string };
}

export const TOKEN_PACKS: TokenPack[] = [
  { key: "token-pack-s", skuId: "token-pack-s", price: 9.9, tokens: 10_000, name: { zh: "体验包", en: "Starter pack" } },
  { key: "token-pack-m", skuId: "token-pack-m", price: 49, tokens: 60_000, hot: true, name: { zh: "标准包", en: "Standard pack" } },
  { key: "token-pack-l", skuId: "token-pack-l", price: 199, tokens: 300_000, name: { zh: "专业包", en: "Pro pack" } },
  { key: "token-pack-xl", skuId: "token-pack-xl", price: 499, tokens: 1_000_000, name: { zh: "团队包", en: "Team pack" } },
];

/** Token 包有效期（月）；订阅含量当月有效。 */
export const TOKEN_PACK_VALID_MONTHS = 12;

/** 每千 Token 单价（USD，保留两位）——包越大越便宜的展示口径。 */
export function packUnitPrice(p: TokenPack): number {
  return Math.round((p.price / p.tokens) * 1000 * 100) / 100;
}

/** 相对体验包的赠幅百分比（体验包为基准 0%）。 */
export function packBonusPct(p: TokenPack): number {
  const base = TOKEN_PACKS[0];
  const baseTokensAtPrice = (p.price / base.price) * base.tokens;
  return Math.round((p.tokens / baseTokensAtPrice - 1) * 100);
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
    zh: "纯翻译团队按坐席订阅：多坐席统一收件箱 · 客户 journey · 漏斗计数（AI 成交能力见智聊档位）",
    en: "Per-seat plan for translation-only teams: shared inbox, customer journey, funnel counter",
  },
};

/* ── 周期换算（与 avatarhub-pricing 同一常量：季 ×3 / 年 ×10 送 2 个月）────── */

export function planPrice(monthly: number, period: Period): number {
  if (period === "monthly") return monthly;
  if (period === "quarterly") return monthly * QUARTER_MONTHS;
  return monthly * ANNUAL_MONTHS;
}

/** 年付总价（营销位「年付 XXX · 省 2 个月」用）。 */
export function annualTotal(monthly: number): number {
  return monthly * ANNUAL_MONTHS;
}

/* ── 展示派生（营销卡 / 报价页 / JSON-LD 全部由此取数，零手写数字）─────────── */

export function planPriceLabel(p: ChatxPlan, lang: Lang): string {
  if (p.custom) return lang === "zh" ? "咨询报价" : "Quote";
  if (p.wallet) return lang === "zh" ? "0 月费" : "$0/mo";
  if (p.monthly === 0) return lang === "zh" ? "免费" : "Free";
  const seat = p.perSeat ? (lang === "zh" ? " / 坐席" : "/seat") : "";
  return lang === "zh" ? `${p.monthly} / 月${seat}` : `${p.monthly}/mo${seat}`;
}

/** 首页产品卡报价行（content.ts Solution.pricing 同构：plan/price/detail/order）。 */
export function chatxPlanRows(lang: Lang): { plan: string; price: string; detail: string; order?: string }[] {
  return CHATX_PLANS.map((p) => ({
    plan: lang === "zh" ? p.name.zh.split(" ")[0] : p.name.en.split(" (")[0],
    price: planPriceLabel(p, lang),
    detail: p.blurb[lang],
    order: p.key,
  }));
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
        ? "内置引擎 · 不限字符（公平使用 200 万字符/日）· 所有档位含"
        : "Built-in engine · unlimited chars (fair use 2M/day) · in every plan",
      order: "autochat-free",
    },
    {
      plan: zh ? "专业翻译" : "Pro translate",
      price: zh ? `${pro.tokens} Token / 千字符` : `${pro.tokens} tokens / 1k chars`,
      detail: zh
        ? "术语锁定 + 翻译记忆 + 多模态（图/语音）· 认证引擎（DeepL）40 Token/千字符"
        : "Term-lock + memory + multimodal · certified DeepL engine at 40 tokens/1k chars",
      order: "token-pack-m",
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

/** Token 包展示行。 */
export function tokenPackRows(lang: Lang): { plan: string; price: string; detail: string; order: string }[] {
  const zh = lang === "zh";
  return TOKEN_PACKS.map((p) => ({
    plan: p.name[lang],
    price: String(p.price),
    detail: zh
      ? `${p.tokens.toLocaleString("en-US")} Token · $${packUnitPrice(p)}/千 · ${TOKEN_PACK_VALID_MONTHS} 个月有效`
      : `${p.tokens.toLocaleString("en-US")} tokens · $${packUnitPrice(p)}/1k · valid ${TOKEN_PACK_VALID_MONTHS} months`,
    order: p.key,
  }));
}

/** 首页套餐区卡片（components/Plans.tsx 消费；年付显示**年付总价**，废除折合月价）。 */
export interface PlanCardItem {
  name: string;
  priceMonthly: string;
  /** 年付总价（×10；月价 0 → "0"） */
  priceYearly: string;
  /** perSeat 档的单位后缀（"/ 坐席"）；普通档为空串 */
  seatSuffix: string;
  desc: string;
  features: string[];
  highlight?: boolean;
  /** /order 深链 plan；免费版走 href 直达下载页 */
  plan?: string;
  href?: string;
}

export function chatxPlanCardItems(lang: Lang): PlanCardItem[] {
  const zh = lang === "zh";
  return CHATX_PLANS.map((p) => {
    const item: PlanCardItem = {
      name: zh ? p.name.zh.split(" ")[0] : p.name.en.split(" (")[0],
      priceMonthly: p.wallet ? "0" : String(p.monthly),
      priceYearly: p.wallet ? "0" : String(annualTotal(p.monthly)),
      seatSuffix: p.perSeat ? (zh ? " / 坐席" : " /seat") : "",
      desc: p.audience[lang],
      features: p.feats[lang],
      highlight: !!p.hot,
      plan: p.key === "autochat-free" ? undefined : p.key,
      href: p.key === "autochat-free" ? (zh ? "/download/chatx" : "/en/download/chatx") : undefined,
    };
    return item;
  });
}

/** JSON-LD offers（schema.org Offer 源数据；lib/pricing.ts toSchemaOffer 消费）。 */
export function chatxSchemaOffers(): {
  id: string;
  skuId?: string;
  name: string;
  price: string;
  currency: "USD";
  unit: "month" | "one-time";
  description: string;
}[] {
  const subs = CHATX_PAID_PLANS.map((p) => ({
    id: p.key,
    skuId: p.skuId ?? undefined,
    name: `ChatX ${p.name.en.split(" (")[0]}`,
    price: String(p.monthly),
    currency: "USD" as const,
    unit: "month" as const,
    description: `Per month${p.perSeat ? " per seat" : ""}; ${p.blurb.en}.`,
  }));
  const packs = TOKEN_PACKS.map((p) => ({
    id: p.key,
    skuId: p.skuId,
    name: `Token ${p.name.en}`,
    price: String(p.price),
    currency: "USD" as const,
    unit: "one-time" as const,
    description: `One-time; ${p.tokens.toLocaleString("en-US")} tokens, valid ${TOKEN_PACK_VALID_MONTHS} months, shared across ChatX & LingoX.`,
  }));
  return [...subs, ...packs];
}

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

/** 超量部分按最优 Token 包组合估算月补充成本（简化：用标准包单价做边际价）。 */
export function estimateTopUpCost(tokensShort: number): number {
  if (tokensShort <= 0) return 0;
  const marginal = packUnitPrice(TOKEN_PACKS[1]); // 标准包 $/千
  return Math.round(((tokensShort / 1000) * marginal + Number.EPSILON) * 10) / 10;
}

export interface PlanCost {
  planKey: string;
  seats: number;
  base: number;
  topUp: number;
  total: number;
  fits: boolean;
}

/** 各档位承接该用量的月成本对比（计算器输出；cheapest 用 total 升序 + fits 优先）。 */
export function comparePlanCosts(tokensPerMonth: number, seats: number): PlanCost[] {
  const s = Math.max(1, Math.round(seats));
  const out: PlanCost[] = [];
  for (const p of CHATX_PLANS) {
    if (p.custom) continue;
    if (p.key === "autochat-flagship") {
      out.push({ planKey: p.key, seats: s, base: p.monthly, topUp: 0, total: p.monthly, fits: true });
      continue;
    }
    const planSeats = p.perSeat ? Math.max(p.perSeat.min, s) : 1;
    const fits = p.perSeat ? true : s <= 1;
    const included = p.perSeat ? p.tokensMonthly * planSeats : p.tokensMonthly;
    const base = p.perSeat ? p.monthly * planSeats : p.monthly;
    const topUp = estimateTopUpCost(tokensPerMonth - included);
    out.push({ planKey: p.key, seats: planSeats, base, topUp, total: Math.round((base + topUp) * 10) / 10, fits });
  }
  return out;
}

/** 推荐档位：能承接（fits）里月总成本最低者；并列取靠后档（功能更全）。 */
export function recommendPlan(tokensPerMonth: number, seats: number): PlanCost {
  const all = comparePlanCosts(tokensPerMonth, seats).filter((c) => c.fits);
  let best = all[0];
  for (const c of all) if (c.total <= best.total) best = c;
  return best;
}
