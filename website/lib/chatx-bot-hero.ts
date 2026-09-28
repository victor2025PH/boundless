/**
 * @ChatX_bot 首条海报的文案与尺寸（bot 与 /chatx/bot-hero 渲染路由共用，不依赖 next/og，可在纯 node 测试里 import）。
 *
 * 尺寸取 4:3（960×720）而非 16:9：手机聊天窗按宽度铺满时 4:3 更高，同样的文案能放大约 1.3 倍；
 * 文案只留三层（标题 / 副标 / 两条卖点 + 一条 CTA），26px 级别的小字在 360px 宽的屏上根本看不清，平台列表等细节交给 caption。
 */
export const BOT_HERO_SIZE = { width: 960, height: 720 };

/**
 * 已接入平台的唯一来源（以 chatx-release-notes 与官网 platformsLive 为准）：caption / 能做什么 / AI 系统提示 /
 * BotFather 描述 / 海报 都从这里取，改一处全部同步。分「海外 / 国内」两组：手机上 10 个平台一行会折成三行，两组各一行才看得清。
 */
export const CHATX_PLATFORMS = {
  zh: {
    global: ["Telegram", "WhatsApp", "LINE", "Messenger", "Facebook"],
    cn: ["微信", "微信客服", "抖音", "QQ", "网页客服"],
  },
  en: {
    global: ["Telegram", "WhatsApp", "LINE", "Messenger", "Facebook"],
    cn: ["WeChat", "WeChat CS", "Douyin", "QQ", "Web chat"],
  },
} as const;

export const CHATX_PLATFORM_COUNT = CHATX_PLATFORMS.zh.global.length + CHATX_PLATFORMS.zh.cn.length;

/** 一行版：“Telegram · WhatsApp · … · 网页客服”（给 AI 提示、BotFather 描述等不分行的地方）。 */
export function platformsInline(lang: "zh" | "en"): string {
  const p = CHATX_PLATFORMS[lang];
  return [...p.global, ...p.cn].join(" · ");
}

export type BotHeroCopy = { kicker: string; title: string; titleAccent: string; points: string[]; foot: string };

export const BOT_HERO_COPY: Record<"zh" | "en", BotHeroCopy> = {
  zh: {
    kicker: "智聊 ChatX",
    title: "AI 全自动聊天",
    titleAccent: "客户消息自动回 · 自动成交",
    points: ["按你的话术 24 小时回复 · 外语互译", `支持 Telegram · WhatsApp · 微信 等 ${CHATX_PLATFORM_COUNT} 个平台`],
    foot: "👇 点下方按钮下载 Windows 版",
  },
  en: {
    kicker: "ChatX",
    title: "AI auto-chat",
    titleAccent: "replies & closes for you",
    points: ["24/7 in your voice · live translation", `Telegram · WhatsApp · WeChat + ${CHATX_PLATFORM_COUNT - 3} more platforms`],
    foot: "👇 Tap below to download for Windows",
  },
};

/** 文案/尺寸指纹：拼进海报 URL 的 ?v=，改文案后 Telegram 侧 URL 缓存与本地 file_id 缓存自动失效。 */
export const BOT_HERO_VERSION = (() => {
  const s = JSON.stringify({ c: BOT_HERO_COPY, s: BOT_HERO_SIZE, p: CHATX_PLATFORMS });
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return h.toString(36);
})();
