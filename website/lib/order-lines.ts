// /order 面板的多产品线适配层（2026-07-24 P7；2026-08-19 Token 定价改版）：把 ChatX /
// LingoX / Token 包的服务类 SKU（lib/chatx-pricing.ts 单一价格真相）适配成幻境 STUDIO
// 面板同构的 Tier 卡片，让下单面板一套交互四条产品线通吃。
//
// 设计约束：
//  - 价格绝不在本文件复写数字——一律从 chatx-pricing.ts 派生（改价只改那里）；
//  - key = plan id（autochat-personal / token-pack-m …）＝ POST /api/order 的 plan 参数，
//    经 lib/offer-map.ts::resolveOrderSku 映射全域 SKU → 引擎自动履约；
//  - 周期：季付 ×3、年付 ×10（送 2 个月，avatarhub-pricing.QUARTER_MONTHS / ANNUAL_MONTHS）；
//    一次性商品（Token 包）标 oneTime，周期切换不影响其价格；
//  - 团队版按坐席计价（perSeat）：面板出坐席步进器，应付 = 单价 × 坐席数 × 周期；
//  - 2026-08-19 停售的旧档（autochat-entry/team、translate-charpack/team/pro）不再出卡，
//    历史深链经 LEGACY_PLAN_MAP 平移到承接档，绝不 404/落空。
import {
  LINGOX_WORKBENCH,
  SIGNUP_BONUS_TOKENS,
  TOKEN_PACKS,
  TOKEN_PACK_VALID_MONTHS,
  chatxPlan,
  packUnitPrice,
  tokenRate,
} from "@/lib/chatx-pricing";
import type { Tier } from "@/lib/avatarhub-pricing";

export type OrderFamily = "avatarhub" | "chatx" | "tokens" | "lingox";

/** Tier 超集：oneTime=一次性买断（周期切换不变价）；perSeat=按坐席计价（出坐席步进器）。 */
export interface LineTier extends Tier {
  oneTime?: boolean;
  perSeat?: { min: number; max: number };
}

const fmtN = (n: number) => n.toLocaleString("en-US");

// ── 智聊 ChatX（Token 分层：个人 / 团队每坐席 / 旗舰；sku_registry zhiliao/chatx-*）──
// 免费版不出购买卡（0 元单污染台账）：面板 blurb 给「下载即用」入口；按量版 Flex 的
// 购买入口 = Token 包产品线（同一钱包），此处亦不出卡。
export const CHATX_TIERS: LineTier[] = (["autochat-personal", "autochat-team-seat", "autochat-flagship"] as const).map(
  (key) => {
    const p = chatxPlan(key);
    return {
      key: p.key,
      edition: p.edition === "trial" ? "standard" : p.edition,
      monthly: p.monthly,
      hot: p.hot,
      perSeat: p.perSeat,
      name: p.name,
      audience: p.audience,
      feats: p.feats,
    } satisfies LineTier;
  },
);

// ── Token 包（跨 ChatX/LingoX 通用耗材；一次性、12 个月有效；sku_registry zhiliao/token-pack-*）──
export const TOKEN_TIERS: LineTier[] = TOKEN_PACKS.map((p) => ({
  key: p.key,
  edition: "standard" as const,
  monthly: p.price,
  oneTime: true,
  hot: p.hot,
  name: { zh: `${p.name.zh} ${fmtN(p.tokens)}`, en: `${p.name.en} · ${fmtN(p.tokens)}` },
  audience: {
    zh: `$${packUnitPrice(p)} / 千 Token`,
    en: `$${packUnitPrice(p)} per 1k tokens`,
  },
  feats: {
    zh: [
      `${fmtN(p.tokens)} Token 一次到账`,
      `${TOKEN_PACK_VALID_MONTHS} 个月有效 · 先订阅含量后扣包`,
      "跨智聊 / 通译同一钱包",
      "会员中心粘贴凭证即到账",
    ],
    en: [
      `${fmtN(p.tokens)} tokens credited at once`,
      `Valid ${TOKEN_PACK_VALID_MONTHS} months · plan allowance spends first`,
      "One wallet across ChatX & LingoX",
      "Redeem instantly in the membership center",
    ],
  },
}));

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
        "标准翻译免费不限量（所有档位含）",
        `专业翻译按 Token：${tokenRate("pro_translate").tokens}/千字符 · DeepL 认证 ${tokenRate("deepl_translate").tokens}/千字符`,
      ],
      en: [
        "Multi-seat unified inbox · journey · funnel counter",
        "Term-lock glossary · translation memory",
        "Standard translation free & unlimited (every plan)",
        `Pro translation by tokens: ${tokenRate("pro_translate").tokens}/1k chars · DeepL ${tokenRate("deepl_translate").tokens}/1k`,
      ],
    },
  },
];

/** 产品线元信息：Tab 文案 + 面板首段（幻境 STUDIO 的「不按字符计费」话术只对本机算力
 *  产品成立，ChatX/Token 按 Token 计量——文案必须随产品线切换，防承诺错位）。 */
export interface FamilyMeta {
  key: OrderFamily;
  tab: { zh: string; en: string };
  blurb: { zh: string; en: string };
}

export const FAMILIES: FamilyMeta[] = [
  {
    key: "avatarhub",
    tab: { zh: "幻境 STUDIO ", en: "STUDIO" },
    blurb: {
      zh: "幻境 STUDIO ：引擎跑在你自己的设备上——我们不卖算力，所以不按字符、张数、时长计费，用量不限。免费版即可换脸（输出带水印），付费档解锁作图、直播换脸、变声与同传。设备自备（下方有配置与配件清单），我们协助部署；到账后按机器指纹签发授权，客户端一键激活。",
      en: "STUDIO: the engine runs on your own hardware — we don't sell compute, so there's no per-character or per-minute metering. The Free plan does face swap (watermarked); paid tiers unlock image gen, live swap, voice changer and interpreting. Bring your device (specs below), we help you deploy; licenses are issued against your machine fingerprint after payment.",
    },
  },
  {
    key: "chatx",
    tab: { zh: "智聊 ChatX", en: "ChatX" },
    blurb: {
      zh: "AI 客服成交引擎：多平台账号统一接管，AI 自动回复、主动跟进、引导成交，人工可随时接管。标准翻译永久免费不限量；AI 动作按 Token 计量（订阅含每月 Token，超出买 Token 包）。免费版下载即用，无需下单；到账自动开通，粘贴授权码即激活。",
      en: "AI closing engine: unify accounts across platforms with AI auto-reply, proactive follow-up and guided closing — humans can take over anytime. Standard translation is free & unlimited; AI actions meter in tokens (plans include monthly tokens, top up with packs). The Free plan needs no order — just download. Paid plans auto-activate after payment.",
    },
  },
  {
    key: "tokens",
    tab: { zh: "Token 包", en: "Token packs" },
    blurb: {
      zh: "跨智聊 / 通译通用的 AI 耗材钱包：AI 回复、专业翻译、克隆语音、AI 配图按公示费率扣 Token；订阅含量先扣，Token 包 12 个月有效。按量版（0 月费）用户预充任意包即可开用；到账自动发放兑换凭证，会员中心粘贴即入账。",
      en: "One AI wallet across ChatX & LingoX: AI replies, pro translation, cloned voice and AI images meter at published token rates. Plan allowances spend first; packs stay valid 12 months. Pay-as-you-go users just top up any pack — vouchers issue automatically after payment and redeem in the membership center.",
    },
  },
  {
    // 2026-08-04 通译并入智聊；2026-08-19 翻译免费化：标准翻译不要钱了，本 tab 只卖
    // 翻译工作台（纯翻译团队坐席订阅）。family key 与 translate-* 深链保持不变——
    // 它们是 /order?plan= 参数与埋点维度，改键会断历史深链与漏斗数据。
    key: "lingox",
    tab: { zh: "智聊 · 翻译", en: "ChatX · Translate" },
    blurb: {
      zh: "标准翻译已永久免费、不限字符（内置引擎，公平使用 200 万字符/日）——下载智聊 ChatX 即用，无需购买。本页只卖两样：翻译工作台（纯翻译团队的坐席订阅）与专业翻译所需的 Token（术语锁定 / 翻译记忆 / DeepL 认证 / 图片语音多模态，见 Token 包）。原字符包 / 团队 / 专业订阅已停售，存量按公告换发升级。",
      en: "Standard translation is now free forever with unlimited characters (built-in engine, fair use 2M chars/day) — just download ChatX. This tab sells two things only: the Translation Workbench (per-seat plan for translation-only teams) and tokens for pro translation (term-lock, memory, certified DeepL, multimodal — see Token packs). Legacy char packs and subscriptions are discontinued; existing customers get an upgrade conversion.",
    },
  },
];

const TIERS_BY_FAMILY: Record<Exclude<OrderFamily, "avatarhub">, LineTier[]> = {
  chatx: CHATX_TIERS,
  tokens: TOKEN_TIERS,
  lingox: LINGOX_TIERS,
};

export function familyTiers(family: OrderFamily, avatarhubTiers: Tier[]): LineTier[] {
  return family === "avatarhub" ? avatarhubTiers : TIERS_BY_FAMILY[family];
}

/** 2026-08-19 停售档深链平移表：老链接 / 老收藏 / 历史聊天里的下单链一律落到承接档。
 *  entry→个人版；team→团队版每坐席；charpack→Token 标准包（60,000 Token 恰是换发等值）；
 *  translate-team/pro→翻译工作台。 */
export const LEGACY_PLAN_MAP: Record<string, string> = {
  "autochat-entry": "autochat-personal",
  "autochat-team": "autochat-team-seat",
  "translate-charpack": "token-pack-m",
  "translate-team": LINGOX_WORKBENCH.key,
  "translate-pro": LINGOX_WORKBENCH.key,
  // 非购买档的营销键（免费版/按量版）：落到最合理的购买入口——免费版落个人版卡
  //（chatx tab 顶部有「免费版无需下单」提示条），按量版落 Token 标准包。
  "autochat-free": "autochat-personal",
  "autochat-flex": "token-pack-m",
  // 历史 compare 页曾用的裸 "team" 深链（原本就落不到任何档）：平移到团队版。
  "team": "autochat-team-seat",
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
  if (CHATX_TIERS.some((t) => t.key === p)) return "chatx";
  if (TOKEN_TIERS.some((t) => t.key === p)) return "tokens";
  if (LINGOX_TIERS.some((t) => t.key === p)) return "lingox";
  return undefined;
}

/** 每条产品线的默认选中档（hot 优先，无 hot 取首个）。 */
export function familyDefaultTier(family: OrderFamily, avatarhubTiers: Tier[]): string {
  const tiers = familyTiers(family, avatarhubTiers);
  return (tiers.find((t) => t.hot) ?? tiers[0]).key;
}

/** 免费版指引（chatx 产品线面板下的「不用买」提示）。 */
export const CHATX_FREE_HINT = {
  zh: `免费版无需下单：下载智聊 ChatX 即用——标准翻译免费不限量 + 每月 ${fmtN(chatxPlan("autochat-free").tokensMonthly)} Token，注册再送 ${fmtN(SIGNUP_BONUS_TOKENS)} 体验 Token。`,
  en: `The Free plan needs no order: download ChatX and go — unlimited standard translation + ${fmtN(chatxPlan("autochat-free").tokensMonthly)} tokens/mo, plus ${fmtN(SIGNUP_BONUS_TOKENS)} bonus tokens on signup.`,
};
