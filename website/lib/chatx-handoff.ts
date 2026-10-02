import { SITE_URL } from "./site";

/**
 * 移动端 → 电脑的「带走链接」：下载页 URL 带上广告来源码 src（和 bot 用户 id tg，若有），
 * 用户在电脑上打开时 getSrc()/getTgUid() 仍能落盘归因，否则手机端 Telegram 广告流量到电脑就断了来源。
 */
export function chatxHandoffUrl(lang: "zh" | "en", src: string, tg = ""): string {
  const u = new URL(`${lang === "zh" ? "" : "/en"}/download/chatx`, SITE_URL);
  if (src) u.searchParams.set("src", src);
  if (tg) u.searchParams.set("tg", tg);
  return u.toString();
}

/** 小程序内的外跳链接（下载页 / 教程）：与 bot 深链同一套 utm 约定，medium=chatx_miniapp，src / tg 原样带走。 */
export function chatxMiniAppOutLink(kind: "download" | "tutorials", lang: "zh" | "en", src: string, tg = ""): string {
  const base = kind === "download" ? chatxHandoffUrl(lang, src, tg) : new URL(`${lang === "zh" ? "" : "/en"}/chatx/tutorials`, SITE_URL).toString();
  const u = new URL(base);
  u.searchParams.set("utm_source", "telegram");
  u.searchParams.set("utm_medium", "chatx_miniapp");
  u.searchParams.set("utm_campaign", src || "organic");
  if (src) u.searchParams.set("src", src);
  return u.toString();
}

/** Telegram 官方分享深链（用户可选「收藏消息」，桌面端同步）。 */
export function telegramShareHref(url: string, text: string): string {
  return `https://t.me/share/url?url=${encodeURIComponent(url)}&text=${encodeURIComponent(text)}`;
}

export function handoffShareText(lang: "zh" | "en"): string {
  return lang === "zh" ? "智聊 ChatX 下载（在电脑上打开）" : "ChatX download — open on your computer";
}
