export const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL || "https://bd2026.cc";

// 人工客服。注意：@handle 是 Telegram 平台实体，更名需在 Telegram 客户端/BotFather 同步操作，
// 这里仅做引用集中化，可用环境变量覆盖。
export const TELEGRAM_HANDLE = process.env.NEXT_PUBLIC_TELEGRAM_HANDLE || "WJKJ2026";
export const TELEGRAM_DISPLAY = `@${TELEGRAM_HANDLE}`;
export const CONTACT_URL = `https://t.me/${TELEGRAM_HANDLE}`;

/* 对外邮箱（实施78 P0-6 / D9）。空值 = 全站不渲染任何邮箱：西方 B2B 买家与 EU AI Act
 * 第 50 条都需要一个可写信的 provider 身份，但发布一个不存在的地址会退信，比没有更伤信任。
 * 消费方一律用 `CONTACT_EMAIL ? … : …` 判空，别硬编。
 *
 * 2026-08-28 当天两步走：先用老板的个人邮箱上线解除阻塞，同日 Cloudflare Email Routing
 * 建好 `hello@bd2026.cc`（转发到同一信箱）后随即切换到域名地址。为什么值得多走这一步：
 * ① G2 / Capterra 这类目录站常拒收个人邮箱域作为厂商联系方式；② 个人地址一旦被爬虫
 * 收录就再也撤不回来，而转发地址随时可换；③ `hello@自有域名` 对国际买家的可信度与
 * 个人 Gmail 不是一个量级。
 * 以后要换只改这一行（或用 NEXT_PUBLIC_CONTACT_EMAIL 覆盖），全部消费面自动跟随。
 * 收信链路实测通过：MX 三条已全球生效、SMTP RCPT 返回 250、真实投递已收到。 */
export const CONTACT_EMAIL =
  process.env.NEXT_PUBLIC_CONTACT_EMAIL || "hello@bd2026.cc";
export const CONTACT_EMAIL_URL = CONTACT_EMAIL ? `mailto:${CONTACT_EMAIL}` : "";

// 自助 Bot + Mini App
export const BOT_HANDLE = process.env.NEXT_PUBLIC_BOT_HANDLE || "tgzkw_bot";
export const BOT_URL = `https://t.me/${BOT_HANDLE}`;
// Mini App 深链（群/频道内用 url 按钮打开；如已在 BotFather 配置 Main Mini App 则直开小程序）
export const MINIAPP_URL = `https://t.me/${BOT_HANDLE}?startapp=autochat`;

// Telegram 频道（案例/动态沉淀）
export const TELEGRAM_CHANNEL = process.env.NEXT_PUBLIC_TELEGRAM_CHANNEL || "hykj7";
export const CHANNEL_URL = `https://t.me/${TELEGRAM_CHANNEL}`;

// Telegram 讨论组 / 群（互动 + 裂变拉新）
export const TELEGRAM_GROUP = process.env.NEXT_PUBLIC_TELEGRAM_GROUP || "hykjz";
export const GROUP_URL = `https://t.me/${TELEGRAM_GROUP}`;

/** 官网 UTM 深链：Telegram 出站按钮统一走这里，让「频道/群/机器人 → 官网 → 留资」可归因。
 *  medium 区分入口（channel/group/bot），campaign 区分具体帖子或菜单位。 */
export function siteUtmLink(medium: string, campaign = "", path = "/"): string {
  const u = new URL(path, SITE_URL);
  u.searchParams.set("utm_source", "telegram");
  u.searchParams.set("utm_medium", medium);
  if (campaign) u.searchParams.set("utm_campaign", campaign);
  return u.toString();
}

/** 小程序 UTM 深链：startapp 只允许 [A-Za-z0-9_-]、≤64 字符，格式 "<入口>__<campaign>"。
 *  小程序端按 "__" 拆分：前段路由视图，后段作为 campaign 归因。 */
export function miniappUtmLink(campaign = "", entry = "autochat"): string {
  const cmp = campaign.replace(/[^A-Za-z0-9_-]/g, "").slice(0, 40);
  const sp = cmp ? `${entry}__${cmp}` : entry;
  return `https://t.me/${BOT_HANDLE}?startapp=${sp.slice(0, 64)}`;
}

/** 中英双路由路径：zh → `/brand`，en → `/en/brand`；hash 原样保留。 */
export function localePath(lang: "zh" | "en", path: string): string {
  if (!path || path.startsWith("http")) return path;
  if (path.startsWith("#")) return path;
  const hashIdx = path.indexOf("#");
  const hash = hashIdx >= 0 ? path.slice(hashIdx) : "";
  let bare = hashIdx >= 0 ? path.slice(0, hashIdx) : path;
  if (!bare) bare = "/";

  let out: string;
  if (lang === "zh") {
    out = bare.startsWith("/en/") ? bare.slice(3) || "/" : bare === "/en" ? "/" : bare;
  } else if (bare === "/" || bare === "") {
    out = "/en";
  } else if (bare.startsWith("/en/") || bare === "/en") {
    out = bare;
  } else {
    out = `/en${bare.startsWith("/") ? bare : `/${bare}`}`;
  }
  return out + hash;
}
