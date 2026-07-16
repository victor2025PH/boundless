export const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL || "https://bd2026.cc";

// 人工客服。注意：@handle 是 Telegram 平台实体，更名需在 Telegram 客户端/BotFather 同步操作，
// 这里仅做引用集中化，可用环境变量覆盖。
export const TELEGRAM_HANDLE = process.env.NEXT_PUBLIC_TELEGRAM_HANDLE || "WJKJ2026";
export const TELEGRAM_DISPLAY = `@${TELEGRAM_HANDLE}`;
export const CONTACT_URL = `https://t.me/${TELEGRAM_HANDLE}`;

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
