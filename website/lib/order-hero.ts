// /order 首屏「主张区」文案与数据单源（2026-08-21 下单页首屏 Hero 化改版）。
//
// 设计约束：
//  - 三条产品线各一组 { 徽章 / 两行主标题 / 轮换卖点 / CountUp 数据卡 }，随 Tab 切换整组淡切；
//  - 数字**零手写**：全部派生自 lib/chatx-pricing.ts / lib/avatarhub-pricing.ts（改价只改那里）；
//  - 主标题走「先破后立」：第一行宣言（实色），第二行解法（text-gradient 渐变，日间有覆盖表）；
//  - 「≈5 分钟到账」是运营口径（与 CheckoutModal「客服 ≈5 分钟内为你开通」同话术单源常量）。
import {
  NEWBIE_PACK,
  RECHARGE_TIERS,
  RECHARGE_TOKENS_PER_USD,
  RECHARGE_VALID_MONTHS,
  LINGOX_WORKBENCH,
  tokenRate,
} from "@/lib/chatx-pricing";
import { STUDIO_PAID_FROM } from "@/lib/avatarhub-pricing";
import type { OrderFamily } from "@/lib/order-lines";

/** 到账开通时效（分钟，运营口径）：Hero 数据卡与信任卡共用，别再散写「5 分钟」。 */
export const ACTIVATION_MINUTES = 5;

export interface OrderHeroStat {
  /** CountUp 数值（纯数字字符串） */
  value: string;
  /** 数值后缀（% / 分钟…），双语 */
  suffix?: { zh: string; en: string };
  label: { zh: string; en: string };
  /** 千分位分组（1,500 / 18,000 这类 Token 数） */
  grouping?: boolean;
  /** "activation" = 到账时长卡：渲染层用 /api/order/stats 近 60 天实测 p50 替换
   *  value（样本不足 / 接口失败回落本常量值），见 components/HeroStatCards.tsx。 */
  dynamic?: "activation";
}

export interface OrderHeroCopy {
  badge: { zh: string; en: string };
  /** 主标题第一行（实色，宣言） */
  titleTop: { zh: string; en: string };
  /** 主标题第二行（渐变，解法） */
  titleAccent: { zh: string; en: string };
  /** 轮换卖点（2.2s 一换，与首页 Hero 同节奏） */
  rotating: { zh: string; en: string }[];
  stats: OrderHeroStat[];
}

const fmtN = (n: number) => n.toLocaleString("en-US");
const MAX_FIRST_BONUS_PCT = RECHARGE_TIERS[RECHARGE_TIERS.length - 1].firstBonusPct;

export const ORDER_HERO: Record<OrderFamily, OrderHeroCopy> = {
  tokens: {
    badge: {
      zh: "一次性充值 · 不订阅 · USDT / 银行卡结算",
      en: "One-time top-ups · no subscription · USDT / card",
    },
    titleTop: { zh: "告别订阅时代", en: "Subscriptions are over" },
    titleAccent: { zh: "充多少，用多少", en: "Pay for what you use" },
    rotating: [
      {
        zh: `首充一次性加赠最高 +${MAX_FIRST_BONUS_PCT}%`,
        en: `First top-up bonus up to +${MAX_FIRST_BONUS_PCT}%`,
      },
      {
        zh: `新人 ${NEWBIE_PACK.price}U = ${fmtN(NEWBIE_PACK.tokens)} Token 双倍到账`,
        en: `Newcomers: ${NEWBIE_PACK.price}U = ${fmtN(NEWBIE_PACK.tokens)} tokens at double rate`,
      },
      { zh: "标准翻译永久免费 · 不限字符", en: "Standard translation free forever" },
      {
        zh: `到账自动开通 ≈${ACTIVATION_MINUTES} 分钟`,
        en: `Auto-activation in ~${ACTIVATION_MINUTES} minutes`,
      },
      { zh: "用尽自动降级 · 永不断线", en: "Graceful fallback — never offline" },
    ],
    stats: [
      {
        value: String(RECHARGE_TOKENS_PER_USD),
        grouping: true,
        label: { zh: "1U 到账 Token", en: "tokens per 1U" },
      },
      {
        value: String(MAX_FIRST_BONUS_PCT),
        suffix: { zh: "%", en: "%" },
        label: { zh: "首充加赠上限", en: "max first top-up bonus" },
      },
      {
        value: String(NEWBIE_PACK.tokens),
        grouping: true,
        label: {
          zh: `新人 ${NEWBIE_PACK.price}U 包到账`,
          en: `tokens in the ${NEWBIE_PACK.price}U newcomer pack`,
        },
      },
      {
        value: String(ACTIVATION_MINUTES),
        suffix: { zh: " 分钟", en: " min" },
        label: { zh: "到账自动开通", en: "auto-activation after payment" },
        dynamic: "activation",
      },
    ],
  },

  avatarhub: {
    badge: {
      zh: "本地部署 · 数据不出机房 · 机器指纹授权",
      en: "Local deployment · data stays on-prem · fingerprint licensing",
    },
    titleTop: { zh: "算力在你手里", en: "Your hardware, your rules" },
    titleAccent: { zh: "用量永不设限", en: "Usage is never metered" },
    rotating: [
      { zh: "免费版下载即换脸", en: "The Free plan does face swap out of the box" },
      { zh: "不按字符 / 张数 / 时长计费", en: "No per-character, per-image or per-minute fees" },
      { zh: "Ed25519 机器指纹授权 · 不怕丢单", en: "Ed25519 licenses bound to your machine" },
      { zh: "输出带 C2PA 可验真凭证", en: "Outputs carry C2PA credentials" },
    ],
    stats: [
      { value: "0", label: { zh: "免费版月费 USD", en: "USD/mo on the Free plan" } },
      {
        value: String(STUDIO_PAID_FROM),
        label: { zh: "USD/月 · 付费档起", en: "USD/mo · paid tiers from" },
      },
      {
        value: "100",
        suffix: { zh: "%", en: "%" },
        label: { zh: "本机推理 · 素材不上传", en: "on-device inference, zero upload" },
      },
      {
        value: String(ACTIVATION_MINUTES),
        suffix: { zh: " 分钟", en: " min" },
        label: { zh: "到账自动签发授权", en: "license auto-issued after payment" },
        dynamic: "activation",
      },
    ],
  },

  lingox: {
    badge: {
      zh: "标准翻译永久免费 · 不限字符",
      en: "Standard translation free forever · unlimited characters",
    },
    titleTop: { zh: "翻译，不要钱了", en: "Translation is now free" },
    titleAccent: { zh: "团队才付坐席费", en: "Teams pay per seat" },
    rotating: [
      { zh: "0 字符费 · 内置引擎永久免费", en: "Zero per-character fees, forever" },
      { zh: "公平使用 200 万字符 / 日", en: "Fair use: 2M characters per day" },
      {
        zh: `翻译工作台 ${LINGOX_WORKBENCH.monthly} USD/坐席/月`,
        en: `Workbench at ${LINGOX_WORKBENCH.monthly} USD/seat/mo`,
      },
      {
        zh: `专业翻译 ${tokenRate("pro_translate").tokens} Token/千字符`,
        en: `Pro translation: ${tokenRate("pro_translate").tokens} tokens per 1k chars`,
      },
    ],
    stats: [
      { value: "0", label: { zh: "标准翻译字符费", en: "per-char fee on standard" } },
      {
        value: String(LINGOX_WORKBENCH.monthly),
        label: { zh: "USD/坐席/月 · 工作台", en: "USD/seat/mo · Workbench" },
      },
      {
        value: String(tokenRate("pro_translate").tokens),
        label: { zh: "Token/千字符 · 专业翻译", en: "tokens/1k chars · pro translate" },
      },
      {
        value: String(tokenRate("deepl_translate").tokens),
        label: { zh: "Token/千字符 · DeepL 认证", en: "tokens/1k chars · certified DeepL" },
      },
    ],
  },
};
