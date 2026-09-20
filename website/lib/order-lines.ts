// /order 面板的多产品线适配层（2026-07-24 P7；2026-08-21 充值唯一化改版）：把 ChatX /
// LingoX 的服务类 SKU（lib/chatx-pricing.ts 单一价格真相）适配成幻境 STUDIO
// 面板同构的 Tier 卡片，让下单面板一套交互多条产品线通吃。
//
// 设计约束：
//  - 价格绝不在本文件复写数字——一律从 chatx-pricing.ts 派生（改价只改那里）；
//  - key = plan id（recharge-200 / translate-workbench …）＝ POST /api/order 的 plan 参数，
//    经 lib/offer-map.ts::resolveOrderSku 映射全域 SKU → 引擎自动履约；
//  - 周期：季付 ×3、年付 ×10（送 2 个月）——现只剩 STUDIO 会员与翻译工作台适用；
//    充值档一律 oneTime，周期切换不影响其价格；
//  - 2026-08-21 充值唯一化：订阅三档（autochat-personal/pro/flagship）停售不再出卡，
//    「智聊 ChatX」产品线 Tab 整体由「充值」Tab 承接（tokens family 改名挂帅）；
//    历史深链经 LEGACY_PLAN_MAP 平移到就近充值档，绝不 404/落空。
import {
  BONUS_VALID_MONTHS,
  CHATX_FREE,
  LINGOX_WORKBENCH,
  NEWBIE_PACK,
  RECHARGE_TIERS,
  RECHARGE_TOKENS_PER_USD,
  SIGNUP_BONUS_TOKENS,
  VIP_REPEAT_BONUS_TIERS,
  rechargeBaseTokens,
  rechargeFirstTokens,
  rechargeUnitPrice,
  rechargeValidMonths,
  tokenRate,
} from "@/lib/chatx-pricing";
import type { Tier } from "@/lib/avatarhub-pricing";

/** 2026-08-21 起产品线只剩三条：STUDIO 会员 / 充值（=智聊唯一付费通道）/ 翻译工作台。
 *  "chatx" 订阅家族已整体停售移除——历史深链由 LEGACY_PLAN_MAP 平移进 tokens 家族。 */
export type OrderFamily = "avatarhub" | "tokens" | "lingox";

/** Tier 超集：oneTime=一次性买断（周期切换不变价）；perSeat=按坐席计价（出坐席步进器）。 */
export interface LineTier extends Tier {
  oneTime?: boolean;
  perSeat?: { min: number; max: number };
}

const fmtN = (n: number) => n.toLocaleString("en-US");

// ── Token 充值（2026-08-21 起智聊唯一付费通道；sku_registry zhiliao/recharge-*）──
// 首充加赠只对首笔充值生效（每人一次，履约侧判定）；卡面同时标注首充/复充两个到账数；
// 大额档（≥5000U）把 perks 服务权益并进卡面清单（单源在 chatx-pricing.ts）。
export const TOKEN_TIERS: LineTier[] = [
  ...RECHARGE_TIERS.map((t) => ({
    key: t.key,
    edition: "standard" as const,
    monthly: t.price,
    oneTime: true,
    hot: t.hot,
    name: { zh: `充值 ${t.price}U`, en: `Top up ${t.price}U` },
    audience: {
      zh: t.firstBonusPct ? `首充 +${t.firstBonusPct}% · 每人一次` : `$${rechargeUnitPrice(t.price, rechargeBaseTokens(t))} / 千 Token`,
      en: t.firstBonusPct ? `First top-up +${t.firstBonusPct}% (once per person)` : `$${rechargeUnitPrice(t.price, rechargeBaseTokens(t))} per 1k tokens`,
    },
    feats: {
      zh: [
        `首充到账 ${fmtN(rechargeFirstTokens(t))} Token${t.firstBonusPct ? `（+${t.firstBonusPct}% 加赠）` : ""}`,
        `复充到账 ${fmtN(rechargeBaseTokens(t))} Token 起（VIP 累充最高再 +${VIP_REPEAT_BONUS_TIERS[VIP_REPEAT_BONUS_TIERS.length - 1].pct}%）`,
        `实付 Token ${rechargeValidMonths(t)} 个月有效 · 赠送 6 个月先扣`,
        "跨智聊 / 通译同一钱包 · 会员中心粘贴凭证即到账",
        ...(t.perks ? t.perks.zh : []),
      ],
      en: [
        `First top-up: ${fmtN(rechargeFirstTokens(t))} tokens${t.firstBonusPct ? ` (+${t.firstBonusPct}% bonus)` : ""}`,
        `Repeat top-up: from ${fmtN(rechargeBaseTokens(t))} tokens (VIP loyalty adds up to +${VIP_REPEAT_BONUS_TIERS[VIP_REPEAT_BONUS_TIERS.length - 1].pct}%)`,
        `Paid tokens valid ${rechargeValidMonths(t)} months · bonus 6 months, spends first`,
        "One wallet across ChatX & LingoX · redeem in membership center",
        ...(t.perks ? t.perks.en : []),
      ],
    },
  })),
  {
    key: NEWBIE_PACK.key,
    edition: "standard" as const,
    monthly: NEWBIE_PACK.price,
    oneTime: true,
    name: { zh: `${NEWBIE_PACK.name.zh} ${NEWBIE_PACK.price}U`, en: `${NEWBIE_PACK.name.en} ${NEWBIE_PACK.price}U` },
    audience: { zh: "注册 72 小时内 · 每账号一次", en: "Within 72h of signup · once per account" },
    feats: {
      zh: [
        `${fmtN(NEWBIE_PACK.tokens)} Token 一次到账（2 倍率，$0.33/千）`,
        "不占用首充加赠资格（大额首充另享 +5%~40%）",
        "注册 72 小时内可购 · 每账号仅一次（履约侧核验）",
        "跨智聊 / 通译同一钱包",
      ],
      en: [
        `${fmtN(NEWBIE_PACK.tokens)} tokens at double rate ($0.33/1k)`,
        "Does not consume your first-top-up bonus",
        "Within 72h of signup · once per account (verified at fulfillment)",
        "One wallet across ChatX & LingoX",
      ],
    },
  },
];

// ── 通译 LingoX（2026-08-19 翻译免费化：唯一订阅 = 翻译工作台每坐席）────────────
export const LINGOX_TIERS: LineTier[] = [
  {
    key: LINGOX_WORKBENCH.key,
    edition: LINGOX_WORKBENCH.edition,
    monthly: LINGOX_WORKBENCH.monthly,
    hot: true,
    perSeat: LINGOX_WORKBENCH.perSeat,
    name: LINGOX_WORKBENCH.name,
    audience: { zh: "纯翻译团队 · 按坐席", en: "Translation-only teams · per seat" },
    feats: {
      zh: [
        "多坐席统一收件箱 · 客户 journey · 漏斗计数",
        "术语锁定 · 翻译记忆",
        "标准翻译免费不限量（所有用户含）",
        `专业翻译按 Token：${tokenRate("pro_translate").tokens}/千字符 · DeepL 认证 ${tokenRate("deepl_translate").tokens}/千字符`,
      ],
      en: [
        "Multi-seat unified inbox · journey · funnel counter",
        "Term-lock glossary · translation memory",
        "Standard translation free & unlimited (for everyone)",
        `Pro translation by tokens: ${tokenRate("pro_translate").tokens}/1k chars · DeepL ${tokenRate("deepl_translate").tokens}/1k`,
      ],
    },
  },
];

/** 产品线元信息：Tab 文案 + 面板首段（幻境 STUDIO 的「不按字符计费」话术只对本机算力
 *  产品成立，充值档按 Token 计量——文案必须随产品线切换，防承诺错位）。
 *  2026-08-21 首屏 Hero 化：blurb 压缩为一句话副标题（首屏可读完），完整条款下沉到
 *  rules 字段（首屏「详细规则」折叠区渲染）——信息没删，只是不再一次性砸在脸上。 */
export interface FamilyMeta {
  key: OrderFamily;
  tab: { zh: string; en: string };
  /** 一句话副标题（Hero 区，≤80 字） */
  blurb: { zh: string; en: string };
  /** 完整条款（Hero「详细规则」折叠区；原 blurb 长文下沉于此，勿删信息） */
  rules: { zh: string; en: string };
}

export const FAMILIES: FamilyMeta[] = [
  {
    key: "avatarhub",
    tab: { zh: "幻境 STUDIO ", en: "STUDIO" },
    blurb: {
      zh: "引擎跑在你自己的设备上——不按字符、张数、时长计费；免费版下载即可换脸，付费档解锁作图、直播换脸、变声与克隆音同传。",
      en: "The engine runs on your own hardware — no per-character or per-minute metering. The Free plan does face swap; paid tiers unlock image gen, live swap, voice changer and interpreting.",
    },
    rules: {
      zh: "我们不卖算力，引擎全部在你本机 / 内网运行，用量不限；免费版输出带合规水印。设备自备（下方有最低配置与配件清单），我们协助部署，远程代部署可预约；到账后按机器指纹签发 Ed25519 授权，客户端一键激活，产出默认带 C2PA 内容凭证。会员周期支持月付 / 季付 / 年付，私有化与企业定制走旗舰版咨询客服。",
      en: "We don't sell compute — the engine runs entirely on your hardware with unlimited usage; Free-plan output is watermarked. Bring your own device (minimum specs and accessories below), we help you deploy, and remote installation can be booked. After payment an Ed25519 license is issued against your machine fingerprint for one-click activation; outputs carry C2PA credentials. Monthly / quarterly / annual billing; private deployment and enterprise customization go through the Flagship track.",
    },
  },
  {
    // 2026-08-21 充值唯一化：本 Tab 即智聊 ChatX 的唯一付费入口（family key 保持 "tokens"
    // ——它是 /order 深链参数与埋点维度，改键会断历史深链与漏斗数据）。
    key: "tokens",
    tab: { zh: "智聊 ChatX · 充值", en: "ChatX · Top up" },
    blurb: {
      zh: `免费开始，要 AI 用量就充值：${RECHARGE_TIERS[0].price}U 起、1U = ${fmtN(RECHARGE_TOKENS_PER_USD)} Token，Token 跨智聊 / 通译一个钱包。`,
      en: `Start free, top up for AI usage: from ${RECHARGE_TIERS[0].price}U at 1U = ${fmtN(RECHARGE_TOKENS_PER_USD)} tokens — one wallet across ChatX & LingoX.`,
    },
    rules: {
      zh: "首笔充值按到账金额向下取档一次性加赠 +5%~40%（每人一次，履约时核验，退款回收加赠）；新人 6U 大礼包 18,000 Token 双倍到账（注册 72 小时内、每账号一次、不占首充资格）。实付 Token 12 个月有效（500U 及以上档 24 个月），赠送部分 6 个月且先扣；复充按 VIP 累充等级自动加赠（累计 ≥500U 复充 +3%、≥2000U +5%、≥10000U +8%，与首充加赠不叠加）。5000U 起含专属客户经理与发票合同；年框合作 / 私有化部署请联系商务。",
      en: "Your first top-up earns a once-per-person bonus of +5%–40% by tier (verified at fulfillment; refunds claw the bonus back). Newcomer pack: 6U for 18,000 tokens at double rate — within 72h of signup, once per account, without consuming the first-top-up bonus. Paid tokens are valid 12 months (24 for 500U+); bonus tokens last 6 months and spend first. Repeat top-ups earn automatic VIP loyalty bonuses (lifetime ≥500U → +3%, ≥2000U → +5%, ≥10000U → +8%; doesn't stack with the first-top-up bonus). From 5000U you get a dedicated account manager and invoicing; annual frames and private deployment are quoted by sales.",
    },
  },
  {
    // 2026-08-04 通译并入智聊；2026-08-19 翻译免费化：标准翻译不要钱了，本 tab 只卖
    // 翻译工作台（纯翻译团队坐席订阅）。family key 与 translate-* 深链保持不变——
    // 它们是 /order?plan= 参数与埋点维度，改键会断历史深链与漏斗数据。
    key: "lingox",
    tab: { zh: "智聊 · 翻译", en: "ChatX · Translate" },
    blurb: {
      zh: "标准翻译永久免费、不限字符，下载智聊 ChatX 即用；本页只卖翻译工作台（纯翻译团队按坐席）。",
      en: "Standard translation is free forever with unlimited characters — just download ChatX. This tab sells one thing: the per-seat Translation Workbench for translation-only teams.",
    },
    rules: {
      zh: "标准翻译由内置引擎提供，永久免费、不限字符（公平使用 200 万字符/日/授权）。专业翻译（术语锁定 / 翻译记忆 / DeepL 认证 / 图片语音多模态）按 Token 计量，见「充值」Tab。原字符包 / 团队 / 专业订阅已停售：存量订阅服务到期，字符包未用完的字符按 150 万字符 = 60,000 Token 免费换发（只多不少）。",
      en: "Standard translation ships with the built-in engine — free forever, unlimited characters (fair use 2M chars/day per license). Pro translation (term-lock, memory, certified DeepL, multimodal) meters in tokens — see the Top up tab. Legacy char packs and subscriptions are discontinued: active plans run to term, and unused char-pack balances convert to 60,000 tokens per 1.5M chars, always in your favor.",
    },
  },
];

const TIERS_BY_FAMILY: Record<Exclude<OrderFamily, "avatarhub">, LineTier[]> = {
  tokens: TOKEN_TIERS,
  lingox: LINGOX_TIERS,
};

export function familyTiers(family: OrderFamily, avatarhubTiers: Tier[]): LineTier[] {
  return family === "avatarhub" ? avatarhubTiers : TIERS_BY_FAMILY[family];
}

/** 停售档深链平移表：老链接 / 老收藏 / 历史聊天里的下单链一律落到承接档。
 *  注意：resolvePlanAlias 是**单跳**查表，链式条目必须拍平（entry→personal→recharge 要直接写 recharge）。
 *  2026-08-19 批：translate-team/pro→翻译工作台。
 *  2026-08-20 批：旧 Token 包→就近充值档。
 *  2026-08-21 批（充值唯一化）：订阅三档 + 更早订阅档全部按月价就近平移到充值档——
 *  entry(58)/personal(39)/team-seat(49)→50U；pro(99)/team(198 旧链已拍平)/flex→100U；
 *  裸 "team"→200U；flagship(598)→500U；free（免费营销键）→新人 6U 包（新客最优转化钩）。 */
export const LEGACY_PLAN_MAP: Record<string, string> = {
  "autochat-entry": "recharge-50",
  "autochat-personal": "recharge-50",
  "autochat-team-seat": "recharge-50",
  "autochat-pro": "recharge-100",
  "autochat-team": "recharge-100",
  "autochat-flex": "recharge-100",
  "autochat-flagship": "recharge-500",
  "autochat-free": "recharge-newbie-6",
  "team": "recharge-200",
  "translate-charpack": "recharge-50",
  "translate-team": LINGOX_WORKBENCH.key,
  "translate-pro": LINGOX_WORKBENCH.key,
  // 2026-08-20 停售的旧 Token 包 → 就近充值档。
  "token-pack-s": "recharge-50",
  "token-pack-m": "recharge-50",
  "token-pack-l": "recharge-200",
  "token-pack-xl": "recharge-500",
};

/** 深链 plan 参数解析（含停售档平移）：返回归一化后的 plan key。 */
export function resolvePlanAlias(plan: string): string {
  const p = String(plan || "").trim().toLowerCase();
  return LEGACY_PLAN_MAP[p] ?? p;
}

/** 深链 plan 参数 → 所属产品线（找不到 = undefined，面板保持默认）。 */
export function familyOfPlan(plan: string, avatarhubTiers: Tier[]): OrderFamily | undefined {
  const p = resolvePlanAlias(plan);
  if (!p) return undefined;
  if (avatarhubTiers.some((t) => t.key === p)) return "avatarhub";
  if (TOKEN_TIERS.some((t) => t.key === p)) return "tokens";
  if (LINGOX_TIERS.some((t) => t.key === p)) return "lingox";
  return undefined;
}

/** 每条产品线的默认选中档（hot 优先，无 hot 取首个）。 */
export function familyDefaultTier(family: OrderFamily, avatarhubTiers: Tier[]): string {
  const tiers = familyTiers(family, avatarhubTiers);
  return (tiers.find((t) => t.hot) ?? tiers[0]).key;
}

/* ── 充值档到账明细（2026-08-21 首屏改版 C1：「所付即所得」）──────────────────
 *  结算条 / 确认弹窗 / 吸底条三个消费面共用同一份数字——用户点「下单」前必须
 *  看得到「这单到账多少 Token、多久有效、约等于多少条 AI 回复」。 */

export interface RechargeCredit {
  /** 首充口径到账（含一次性加赠） */
  first: number;
  /** 复充口径到账（基础额；VIP 累充另加，履约侧现算） */
  repeat: number;
  /** 首充加赠百分比（新人包 = 0，双倍率已含在 first 里） */
  bonusPct: number;
  /** 实付 Token 有效期（月）；新人包按赠送口径 6 个月 */
  months: number;
  newbie: boolean;
  /** ≈ AI 回复条数（首充口径） */
  repliesFirst: number;
  /** ≈ AI 回复条数（复充口径） */
  repliesRepeat: number;
}

/** 按 plan key 取充值到账明细；非充值档（STUDIO 会员 / 工作台）返回 null。 */
export function rechargeCreditOf(planKey: string): RechargeCredit | null {
  const perReply = tokenRate("ai_reply").tokens;
  if (planKey === NEWBIE_PACK.key) {
    const replies = Math.round(NEWBIE_PACK.tokens / perReply);
    return {
      first: NEWBIE_PACK.tokens,
      repeat: NEWBIE_PACK.tokens,
      bonusPct: 0,
      months: BONUS_VALID_MONTHS,
      newbie: true,
      repliesFirst: replies,
      repliesRepeat: replies,
    };
  }
  const t = RECHARGE_TIERS.find((x) => x.key === planKey);
  if (!t) return null;
  const first = rechargeFirstTokens(t);
  const repeat = rechargeBaseTokens(t);
  return {
    first,
    repeat,
    bonusPct: t.firstBonusPct,
    months: rechargeValidMonths(t),
    newbie: false,
    repliesFirst: Math.round(first / perReply),
    repliesRepeat: Math.round(repeat / perReply),
  };
}

/** 免费开始指引（tokens 产品线面板下的「不用买也能用」提示）。 */
export const CHATX_FREE_HINT = {
  zh: `免费开始无需下单：下载智聊 ChatX 即用——标准翻译免费不限量 + 每月 ${fmtN(CHATX_FREE.tokensMonthly)} Token，注册再送 ${fmtN(SIGNUP_BONUS_TOKENS)} 体验 Token。`,
  en: `Starting free needs no order: download ChatX and go — unlimited standard translation + ${fmtN(CHATX_FREE.tokensMonthly)} tokens/mo, plus ${fmtN(SIGNUP_BONUS_TOKENS)} bonus tokens on signup.`,
};
