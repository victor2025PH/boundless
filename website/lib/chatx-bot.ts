/**
 * @ChatX_bot —— 智聊 ChatX 的 Telegram 广告落地机器人。
 *
 * 为什么不用主 bot（@tgzkw_bot）：Telegram Ads（TON 账户）的广告目标只能是 t.me 内部链接，
 * 官网下载页不能直接当广告落地；需要一个「广告内容 = 目标机器人内容」的专属 bot 承接，
 * 且 /start 的 payload 就是广告来源码，用来把「哪条广告 → 多少人 start → 多少人下载」串起来。
 *
 * 归因链路（全部落 events.jsonl，与 /api/track 同格式，admin 统计直接可读）：
 *   广告按钮 t.me/ctx2026_bot?start=<src>
 *     → chatx_bot_start  {src, uid, first}              （本文件）
 *     → 键盘：教程 / 能做什么 / 频道 / 官网 / 交流群 / 人工客服 / 最下一行下载桌面版（官网 /download/chatx?src=<src>&utm_*）；小程序走菜单键
 *     → 自由文本：同官网 AI 客服机制（askDeepSeek + 知识库兜底），bot 本身就是 ChatX 自动回复的演示
 *     → chatx_download_click {src}                      （ChatxDownloadSection，前端埋点）
 *     → chatx_download_redirect {src}                   （/dl 分流入口服务端落账，无 JS 也算）
 *
 * 与主 bot 共用 token 以外的所有基础设施（tg-events / data-dir / site 常量）；token、webhook
 * secret、handle 用独立 env（CHATX_BOT_TOKEN / CHATX_WEBHOOK_SECRET / NEXT_PUBLIC_CHATX_BOT_HANDLE）。
 */
import { appendFile, mkdir, readFile, rename, writeFile } from "fs/promises";
import path from "path";
import { detectKnowledgeLang, matchFreeText, type BotLang } from "./bot-knowledge";
import { splitChatLinks } from "./chat-links";
import { askDeepSeek, type ChatTurn } from "./deepseek";
import { dailyGuard, logChat } from "./chat-log";
import { DATA_DIR } from "./data-dir";
import { appendLead, notifyAdminsOfLead, upsertLead } from "./lead-store";
import { notifyAdmins } from "./order-store";
import { downloadedUids } from "./chatx-dl-ledger";
import { getPrefs, setPrefs, type ChatxPersona, type ChatxPrefs } from "./chatx-prefs";
import { DEFAULT_PERSONA, PERSONAS, parsePersona, personaLabel, personaMenuText, personaSwitchedText, personaSystemHint } from "./chatx-persona";
import {
  VOICE_IN_MAX_BYTES,
  VOICE_IN_MAX_SEC,
  asksForVoice,
  scrubVoiceDenial,
  synthesizeVoice,
  takeVoiceSlot,
  transcribeVoice,
  ttsText,
  voiceInEnabled,
  voiceOutEnabled,
  voiceReplyHint,
  wantsVoiceReply,
  wavToMp3,
} from "./chatx-voice";
import { decodeWavMono, encodeWavMono } from "./chatx-voice-ref";
import { CLONE_IN_MAX_BYTES, CLONE_IN_MAX_SEC, type CloneResult, cloneArmed, cloneRejectText, enrollUserVoice, removeUserVoiceFile } from "./chatx-voice-clone";
import { DEFAULT_VOICE, MY_VOICE, PERSONA_DEFAULT_VOICE, VOICEPACK, availableVoices, hasMyVoice, parseVoiceChoice, resolveVoiceChoice, voiceLabel, voicepackPath } from "./chatx-voicepack";
import { trackTg } from "./tg-events";
import { BUILTIN_BOT_ID, currentBot } from "./tg-bot-context";
import { CHANNEL_URL, CONTACT_URL, GROUP_URL, SITE_URL, siteUtmLink } from "./site";
import { CHATX_TUTORIALS_PATH, CHATX_TUTORIAL_COUNT, CHATX_TUTORIAL_TOTAL_SEC } from "./chatx-tutorials";
import { BOT_HERO_VERSION, CHATX_PLATFORMS, CHATX_PLATFORM_COUNT, platformsInline } from "./chatx-bot-hero";

export const CHATX_BOT_HANDLE = process.env.NEXT_PUBLIC_CHATX_BOT_HANDLE || "ctx2026_bot";
export const CHATX_BOT_URL = `https://t.me/${CHATX_BOT_HANDLE}`;

const STARTS_LOG = process.env.CHATX_BOT_STARTS_LOG || path.join(DATA_DIR, "chatx_bot_starts.jsonl");
/** 追发提醒账本：每个 uid 一生只发一次（先记后发，乐观认领）。 */
const REMIND_LOG = process.env.CHATX_BOT_REMIND_LOG || path.join(DATA_DIR, "chatx_bot_reminds.jsonl");
/** AI 会话上下文快照（只存未过期的最近几轮，pm2 重启后续上；对话全文本来就在 chats.jsonl） */
const CTX_SNAPSHOT = process.env.CHATX_BOT_CTX_FILE || path.join(DATA_DIR, "chatx_bot_ctx.json");
/** /start 后多少小时追发一次；0 = 关闭。只追 72h 内的 start，避免首次启用时群发陈年老用户。 */
const REMIND_AFTER_MS = Number(process.env.CHATX_BOT_REMIND_HOURS ?? 24) * 3600_000;
const REMIND_MAX_AGE_MS = 72 * 3600_000;

/** 首条欢迎图。默认用 /chatx/bot-hero 动态海报（按语言；?v= 是文案指纹，改文案即换 URL）；配了 CHATX_BOT_HERO_IMAGE 则中英共用该图。
 *  首次按 URL 发（Telegram 要回源拉图 + 我们现场渲染，慢），成功后记下 file_id，之后同一张图直接按 file_id 发，秒出。 */
function heroImageUrl(lang: BotLang): string {
  return process.env.CHATX_BOT_HERO_IMAGE || `${SITE_URL}/chatx/bot-hero?lang=${lang}&v=${BOT_HERO_VERSION}`;
}

const HERO_CACHE = process.env.CHATX_BOT_HERO_CACHE || path.join(DATA_DIR, "chatx_bot_hero_fileid.json");
let heroFileIds: Map<string, string> | null = null;

async function loadHeroFileIds(): Promise<Map<string, string>> {
  if (heroFileIds) return heroFileIds;
  const m = new Map<string, string>();
  try {
    const raw = JSON.parse(await readFile(HERO_CACHE, "utf-8")) as Record<string, unknown>;
    for (const [k, v] of Object.entries(raw)) if (typeof v === "string" && v) m.set(k, v);
  } catch {
    /* 无缓存 */
  }
  heroFileIds = m;
  return m;
}

async function saveHeroFileIds(m: Map<string, string>) {
  try {
    await mkdir(path.dirname(HERO_CACHE), { recursive: true });
    await writeFile(HERO_CACHE, JSON.stringify(Object.fromEntries(m)), "utf-8");
  } catch {
    /* 只是加速缓存，写失败无妨 */
  }
}

/** sendPhoto 返回的 message.photo 是多尺寸数组，最后一个最大；file_id 可跨会话复用。 */
function largestPhotoFileId(result: unknown): string | null {
  if (!result || typeof result !== "object") return null;
  const photo = (result as { photo?: unknown }).photo;
  if (!Array.isArray(photo) || photo.length === 0) return null;
  const last = photo[photo.length - 1] as { file_id?: unknown };
  return typeof last.file_id === "string" ? last.file_id : null;
}

export type TgFrom = { id: number; username?: string; first_name?: string; language_code?: string };

export type InlineBtn =
  | { text: string; url: string }
  | { text: string; callback_data: string }
  | { text: string; web_app: { url: string } };

const API = (method: string) => `https://api.telegram.org/bot${currentBot()?.token}/${method}`;
const FILE_API = (filePath: string) => `https://api.telegram.org/file/bot${currentBot()?.token}/${filePath}`;

export function chatxBotConfigured(): boolean {
  return Boolean(currentBot());
}

export const escHtml = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

export async function tgCall(method: string, body: Record<string, unknown>) {
  if (!currentBot()) return null;
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), 8000);
  try {
    const res = await fetch(API(method), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: ac.signal,
    });
    return (await res.json()) as { ok?: boolean; description?: string; result?: unknown } | null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

/** 带文件的 Bot API 调用（sendVoice 等）：multipart，字段为对象时 JSON 化。 */
export async function tgUpload(method: string, fields: Record<string, unknown>, file: { field: string; name: string; data: Buffer; type: string }) {
  if (!currentBot()) return null;
  const form = new FormData();
  for (const [k, v] of Object.entries(fields)) {
    if (v === undefined || v === null) continue;
    form.append(k, typeof v === "object" ? JSON.stringify(v) : String(v));
  }
  const bytes = new Uint8Array(file.data.byteLength);
  bytes.set(file.data);
  form.append(file.field, new Blob([bytes], { type: file.type }), file.name);
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), 20000);
  try {
    const res = await fetch(API(method), { method: "POST", body: form, signal: ac.signal });
    return (await res.json()) as { ok?: boolean; description?: string; result?: unknown } | null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

export async function sendVoice(chatId: number | string, mp3: Buffer, durationSec?: number) {
  return tgUpload("sendVoice", { chat_id: chatId, duration: durationSec ? Math.round(durationSec) : undefined }, { field: "voice", name: "reply.mp3", data: mp3, type: "audio/mpeg" });
}

/** getFile → 下载文件字节（Telegram 语音 ≤ 1 分钟通常几十 KB）。 */
export async function downloadTgFile(fileId: string, maxBytes: number): Promise<ArrayBuffer | null> {
  const meta = await tgCall("getFile", { file_id: fileId });
  const r = meta?.result as { file_path?: string; file_size?: number } | undefined;
  if (!meta?.ok || !r?.file_path) return null;
  if (typeof r.file_size === "number" && r.file_size > maxBytes) return null;
  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), 10000);
  try {
    const res = await fetch(FILE_API(r.file_path), { signal: ac.signal });
    if (!res.ok) return null;
    const buf = await res.arrayBuffer();
    return buf.byteLength > maxBytes ? null : buf;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

export async function sendText(chatId: number | string, text: string, keyboard?: InlineBtn[][], opts?: { plain?: boolean }) {
  return tgCall("sendMessage", {
    chat_id: chatId,
    text,
    parse_mode: opts?.plain ? undefined : "HTML",
    disable_web_page_preview: true,
    reply_markup: keyboard ? { inline_keyboard: keyboard } : undefined,
  });
}

/** 图文首条：优先用缓存的 file_id（秒发），没有/失效再按 URL 发并记下 file_id；sendPhoto 全失败（图不可达/被拒）时回落纯文字，保证 /start 永远有回应。 */
export async function sendPhotoOrText(chatId: number | string, photo: string, caption: string, keyboard?: InlineBtn[][]) {
  const base = { chat_id: chatId, caption, parse_mode: "HTML", reply_markup: keyboard ? { inline_keyboard: keyboard } : undefined };
  const ids = await loadHeroFileIds();
  const botId = currentBot()?.id ?? BUILTIN_BOT_ID;
  const cacheKey = botId === BUILTIN_BOT_ID ? photo : `${botId}|${photo}`;
  const cached = ids.get(cacheKey);
  if (cached) {
    const hit = await tgCall("sendPhoto", { ...base, photo: cached });
    if (hit?.ok) return hit;
    ids.delete(cacheKey);
    void saveHeroFileIds(ids);
  }
  const res = await tgCall("sendPhoto", { ...base, photo });
  if (res?.ok) {
    const fid = largestPhotoFileId(res.result);
    if (fid && ids.get(cacheKey) !== fid) {
      ids.set(cacheKey, fid);
      void saveHeroFileIds(ids);
    }
    return res;
  }
  return sendText(chatId, caption, keyboard);
}

export async function answerCallback(id: string, text?: string) {
  return tgCall("answerCallbackQuery", { callback_query_id: id, text });
}

// ── 来源码 ────────────────────────────────────────────────────────────

/** Telegram start payload 只允许 [A-Za-z0-9_-]，≤64 字符；这里再收紧到 48 防日志膨胀。 */
const SRC_RE = /^[A-Za-z0-9_-]{1,48}$/;
export const DEFAULT_SRC = "organic";

/** 规范化广告来源码：非法/缺省 → "organic"。 */
export function normalizeSrc(raw: string | undefined | null): string {
  const s = (raw ?? "").trim();
  return SRC_RE.test(s) ? s : DEFAULT_SRC;
}

/** 定时帖按钮经 /r/p/<id> 跳转时在 start 载荷后拼的帖子号：`ad_x__p12`（与 tg-posts.ts 的 START_POST_SUFFIX_RE 一致）。 */
const START_POST_RE = /__p(\d{1,9})$/;

/** 从 "/start ad_biz_a01" / "/start@ChatX_bot ad_biz_a01__p12" 抽出来源码和带人来的帖子号。 */
export function parseStartPayload(text: string): { src: string; post?: number } {
  const m = text.trim().match(/^\/start(?:@\w+)?(?:\s+(\S+))?/i);
  const raw = m?.[1] ?? "";
  const pm = START_POST_RE.exec(raw);
  if (!pm) return { src: normalizeSrc(raw) };
  return { src: normalizeSrc(raw.slice(0, pm.index)), post: Number(pm[1]) };
}

/** 从 "/start ad_biz_a01" 或 "/start@ChatX_bot ad_biz_a01" 抽出来源码。 */
export function parseStartSrc(text: string): string {
  return parseStartPayload(text).src;
}

interface StartRec {
  t: string;
  uid: number;
  src: string;
  post?: number;
  username?: string;
  lang: BotLang;
}

/** 该用户是否已 start 过（首触判定；文件缺失 = 首触）。 */
async function seenBefore(uid: number): Promise<boolean> {
  try {
    const raw = await readFile(STARTS_LOG, "utf-8");
    const needle = `"uid":${uid},`;
    return raw.includes(needle);
  } catch {
    return false;
  }
}

/** 记录一次 /start：私有账本（含 uid，用于首触/去重）+ 公共漏斗事件（admin 统计口径）。 */
export async function recordStart(from: TgFrom, src: string, lang: BotLang, post?: number): Promise<{ first: boolean }> {
  const first = !(await seenBefore(from.id));
  const rec: StartRec = { t: new Date().toISOString(), uid: from.id, src, ...(post ? { post } : {}), username: from.username, lang };
  try {
    await mkdir(path.dirname(STARTS_LOG), { recursive: true });
    await appendFile(STARTS_LOG, JSON.stringify(rec) + "\n");
  } catch {
    /* never fail bot flow over bookkeeping */
  }
  await trackTg("chatx_bot_start", { src, uid: from.id, first, lang, ...(post ? { post } : {}) });
  return { first };
}

// ── 深链 ──────────────────────────────────────────────────────────────

/** 官网下载页深链：utm_* 给会话归因（attribution.ts），src 给下载点击/分流入口直接落账。
 *  medium 默认 chatx_bot；追发提醒用 chatx_bot_remind，src 不变（功劳仍归广告位），会话口径可单独看提醒带来的下载。
 *  uid（Telegram 用户 id）作 `tg=` 带到下载页 → /dl，安装包请求就能落到人：追发只发没下过的，admin 数下载人数。 */
export function downloadLink(src: string, lang: BotLang, medium = "chatx_bot", uid?: number): string {
  const u = new URL(lang === "zh" ? "/download/chatx" : "/en/download/chatx", SITE_URL);
  u.searchParams.set("utm_source", "telegram");
  u.searchParams.set("utm_medium", medium);
  u.searchParams.set("utm_campaign", src);
  u.searchParams.set("src", src);
  if (uid && Number.isInteger(uid) && uid > 0) u.searchParams.set("tg", String(uid));
  return u.toString();
}

export function tutorialsLink(src: string, lang: BotLang, medium = "chatx_bot"): string {
  const u = new URL(lang === "zh" ? CHATX_TUTORIALS_PATH : `/en${CHATX_TUTORIALS_PATH}`, SITE_URL);
  u.searchParams.set("utm_source", "telegram");
  u.searchParams.set("utm_medium", medium);
  u.searchParams.set("utm_campaign", src);
  u.searchParams.set("src", src);
  return u.toString();
}

/** ChatX 小程序（/app/chatx）：菜单键用无 src 的地址（静态），键盘按钮带 src；页内再用 initData 反查兜底。 */
export const CHATX_MINIAPP_PATH = "/app/chatx";
export function miniAppLink(src?: string): string {
  const u = new URL(CHATX_MINIAPP_PATH, SITE_URL);
  if (src) u.searchParams.set("src", src);
  return u.toString();
}

// ── 文案 / 键盘 ──────────────────────────────────────────────────────

/** 官网首页深链（utm_campaign = 广告来源码，会话归因走 attribution.ts）。 */
export function siteLink(src: string, lang: BotLang): string {
  return siteUtmLink("chatx_bot", src, lang === "zh" ? "/" : "/en");
}

/** 下载按钮放最后一行（紧贴输入框、拇指最顺手），小程序不占键盘位，只留菜单键。
 *  「人工客服」是回调而非外链：点了才能通知管理员去迎，用户随后仍拿到直达链接。 */
export function mainKeyboard(src: string, lang: BotLang, uid?: number): InlineBtn[][] {
  const zh = lang === "zh";
  const mins = Math.round(CHATX_TUTORIAL_TOTAL_SEC / 60);
  return [
    [
      { text: zh ? `📖 ${mins} 分钟学会` : `📖 Learn in ${mins} min`, url: tutorialsLink(src, lang) },
      { text: zh ? "❓ 能做什么" : "❓ What it does", callback_data: "cx_features" },
    ],
    [
      { text: zh ? "👤 人工客服" : "👤 Human support", callback_data: "cx_human" },
      { text: zh ? "🩺 报障 / 求助" : "🩺 Report a problem", callback_data: "cxb:start" },
    ],
    [
      { text: zh ? "📢 频道" : "📢 Channel", url: CHANNEL_URL },
      { text: zh ? "👥 交流群" : "👥 Community", url: GROUP_URL },
      { text: zh ? "🏠 官网" : "🏠 Website", url: siteLink(src, lang) },
    ],
    [{ text: zh ? "📥 下载桌面版（Windows · 免费）" : "📥 Download desktop (Windows · free)", url: downloadLink(src, lang, "chatx_bot", uid) }],
  ];
}

/** 支持平台两行（海外 / 国内）：手机上各占一行。
 *  caption 海外行不列 Facebook：Messenger 已代表 FB 私信，5 个英文名在 360px 屏会折成两行；完整列表仍在「能做什么」/ AI 提示 / bot 描述里。 */
const CAPTION_GLOBAL_OMIT: ReadonlySet<string> = new Set(["Facebook"]);

function platformLines(lang: BotLang): string {
  const p = CHATX_PLATFORMS[lang];
  const global = p.global.filter((x) => !CAPTION_GLOBAL_OMIT.has(x)).join(" · ");
  return lang === "zh"
    ? `🌍 <b>海外：</b>${global}\n🇨🇳 <b>国内：</b>${p.cn.join(" · ")}`
    : `🌍 <b>Global:</b> ${global}\n🇨🇳 <b>China:</b> ${p.cn.join(" · ")}`;
}

/**
 * 「可在这里直接与我聊天」重点块：用 Telegram 原生 <blockquote>（左侧色条 + 浅底，宽度自适应），
 * 不用 ━ 字符线——全角线在 360px 手机上超过 13 个就折行。老客户端不认 blockquote 会退化为普通文字，不报错。
 */
function chatHereBlock(lang: BotLang): string {
  return lang === "zh"
    ? "<blockquote>💬 <b>可在这里直接与我聊天</b>\n随便发一句，看看 AI 怎么回你</blockquote>"
    : "<blockquote>💬 <b>You can chat with me right here</b>\nSend anything and see how the AI replies</blockquote>";
}

/** 首条 caption：标题 → 三行卖点 → 支持平台两行 → blockquote 重点块 → 按钮引导（含手机上下载的预期管理）。≤1024 字符。 */
export function welcomeText(lang: BotLang, first: boolean): string {
  if (lang === "zh") {
    return (
      (first ? "👋 欢迎来到 <b>智聊 ChatX · AI 全自动聊天</b>" : "👋 <b>欢迎回来</b> · 智聊 ChatX · AI 全自动聊天") +
      "\n\n" +
      "🤖 客户发消息，<b>AI 按你的话术自动回复</b>\n" +
      "📈 24 小时自动跟进 · 自动成交\n" +
      "🗣 外语客户自动互译\n\n" +
      `${platformLines("zh")}\n\n` +
      `${chatHereBlock("zh")}\n\n` +
      "👇 或点下面的按钮（手机上点下载可发到电脑）"
    );
  }
  return (
    (first ? "👋 Welcome to <b>ChatX · AI auto-chat</b>" : "👋 <b>Welcome back</b> · ChatX · AI auto-chat") +
    "\n\n" +
    "🤖 Customers message you, <b>AI replies in your voice</b>\n" +
    "📈 Follows up and closes 24/7\n" +
    "🗣 Live translation for foreign-language leads\n\n" +
    `${platformLines("en")}\n\n` +
    `${chatHereBlock("en")}\n\n` +
    "👇 Or tap a button below (on mobile, Download lets you send it to your PC)"
  );
}

export function featuresText(lang: BotLang): string {
  if (lang === "zh") {
    return (
      "<b>智聊 ChatX · AI 全自动聊天能做什么</b>\n\n" +
      "🤖 <b>AI 自动回复</b>：客户消息秒回，全自动 / 人审放行 / 仅拟稿 三档可切\n" +
      "📚 <b>你的话术 + 人设</b>：喂进产品资料，AI 按你的风格聊、按你的流程推进成交\n" +
      "🗣 <b>外语自动互译</b>：客户说什么语言都能聊，收发双向翻译\n" +
      `📨 <b>多平台多账号</b>：${platformsInline("zh")} 共 ${CHATX_PLATFORM_COUNT} 个平台一屏管\n` +
      "🎙 <b>语音消息</b>：语音转写、克隆音色回复\n" +
      "🔒 <b>本地运行</b>：数据留在自己电脑，免显卡\n\n" +
      "适合：Telegram 引流/私域团队、跨境电商客服、社群运营、海外招聘。\n\n" +
      "👉 你现在跟我聊的，就是 ChatX 的 AI 自动回复。" +
      (voiceInEnabled() ? "发语音我也听得懂；" : "") +
      (voiceOutEnabled() ? "/voice 开语音回复，" : "") +
      "/persona 体验恋爱陪聊 / 销售跟进人设" +
      (voiceOutEnabled() ? "，/voices 换声音或克隆你自己的声音。" : "。")
    );
  }
  return (
    "<b>What ChatX AI auto-chat does</b>\n\n" +
    "🤖 <b>AI auto-reply</b>: instant answers to customers — fully automatic / human-approved / draft-only\n" +
    "📚 <b>Your script + persona</b>: feed it your product docs; it chats in your voice and follows your sales flow\n" +
    "🗣 <b>Live translation</b>: talk to customers in any language, two-way\n" +
    `📨 <b>Multi-platform, multi-account</b>: ${platformsInline("en")} — ${CHATX_PLATFORM_COUNT} platforms on one screen\n` +
    "🎙 <b>Voice</b>: transcription and cloned-voice replies\n" +
    "🔒 <b>Local-first</b>: data stays on your PC, no GPU\n\n" +
    "For Telegram growth teams, cross-border e-commerce support, community managers and overseas recruiters.\n\n" +
    "👉 You're chatting with ChatX's AI auto-reply right now. " +
    (voiceInEnabled() ? "Voice messages work too; " : "") +
    (voiceOutEnabled() ? "/voice for spoken replies, " : "") +
    "/persona to try the companion / sales follow-up personas" +
    (voiceOutEnabled() ? ", /voices to change the voice or clone your own." : ".")
  );
}

export function remindText(lang: BotLang): string {
  const mins = Math.round(CHATX_TUTORIAL_TOTAL_SEC / 60);
  if (lang === "zh") {
    return (
      "👋 昨天看过智聊 ChatX（AI 全自动聊天），装上了吗？\n\n" +
      "还没装：点下面下载 Windows 安装包，免费开始、无需绑卡，装好客户消息就有 AI 自动回。\n" +
      `已经装好：看 ${mins} 分钟教程，从接入渠道到 AI 自动回复一步到位。\n\n` +
      "这是唯一一次提醒，不会再打扰。"
    );
  }
  return (
    "👋 You checked out ChatX (AI auto-chat) yesterday — did you get it installed?\n\n" +
    "Not yet: tap below to download the Windows installer. Free to start, no card — AI answers your customers as soon as it's set up.\n" +
    `Already installed: the ${mins}-minute tutorials take you from connecting channels to AI auto-reply.\n\n` +
    "This is the only reminder we’ll send."
  );
}

export function remindKeyboard(src: string, lang: BotLang, uid?: number): InlineBtn[][] {
  const zh = lang === "zh";
  return [
    [{ text: zh ? "📥 下载桌面版（Windows · 免费）" : "📥 Download desktop (Windows · free)", url: downloadLink(src, lang, "chatx_bot_remind", uid) }],
    [
      { text: zh ? "📖 看教程" : "📖 Tutorials", url: tutorialsLink(src, lang) },
      { text: zh ? "👤 人工客服" : "👤 Human support", callback_data: "cx_human" },
    ],
  ];
}

// ── 追发提醒 ──────────────────────────────────────────────────────────

async function readJsonl<T>(file: string): Promise<T[]> {
  try {
    const raw = await readFile(file, "utf-8");
    const out: T[] = [];
    for (const line of raw.split("\n")) {
      if (!line.trim()) continue;
      try {
        out.push(JSON.parse(line) as T);
      } catch {
        /* skip bad line */
      }
    }
    return out;
  } catch {
    return [];
  }
}

let reminding = false;

/**
 * /start 后 24h 追发一次（由 /api/admin/schedule/run 的外部 cron 驱动）。
 * 已经请求过安装包的人（chatx-dl-ledger，下载链 tg=uid 回执）不发；绕开 bot 链接自己下的没有回执，
 * 所以文案仍覆盖「没装 / 装好了」两种人；每人一生只发一次，重复 /start（已主动回来）的不发，超过 72h 的不补发。
 */
export async function runStartReminders(now = Date.now()): Promise<{ due: number; sent: number; skipped: number }> {
  if (reminding || REMIND_AFTER_MS <= 0 || !chatxBotConfigured()) return { due: 0, sent: 0, skipped: 0 };
  reminding = true;
  try {
    const starts = await readJsonl<StartRec>(STARTS_LOG);
    const reminded = new Set((await readJsonl<{ uid: number }>(REMIND_LOG)).map((r) => r.uid));
    const downloaded = await downloadedUids();
    const byUid = new Map<number, { first: StartRec; count: number }>();
    for (const s of starts) {
      if (typeof s.uid !== "number") continue;
      const cur = byUid.get(s.uid);
      if (cur) cur.count++;
      else byUid.set(s.uid, { first: s, count: 1 });
    }
    const due: StartRec[] = [];
    let skipped = 0;
    for (const [uid, { first, count }] of byUid) {
      if (reminded.has(uid) || count > 1) continue;
      const age = now - Date.parse(first.t);
      if (!Number.isFinite(age) || age < REMIND_AFTER_MS || age >= REMIND_MAX_AGE_MS) continue;
      if (downloaded.has(uid)) {
        skipped++;
        continue;
      }
      due.push(first);
    }
    let sent = 0;
    for (const s of due) {
      try {
        await mkdir(path.dirname(REMIND_LOG), { recursive: true });
        await appendFile(REMIND_LOG, JSON.stringify({ t: new Date(now).toISOString(), uid: s.uid, src: s.src }) + "\n");
      } catch {
        continue; // 账本写不进就不发，宁漏不重
      }
      const lang: BotLang = s.lang === "en" ? "en" : "zh";
      const r = await sendText(s.uid, remindText(lang), remindKeyboard(s.src, lang, s.uid));
      const ok = Boolean(r?.ok);
      if (ok) sent++;
      await trackTg("chatx_bot_remind", { src: s.src, uid: s.uid, ok, lang });
    }
    return { due: due.length, sent, skipped };
  } finally {
    reminding = false;
  }
}

// ── 处理器 ────────────────────────────────────────────────────────────

/** 私聊 /start（含广告深链）：记录来源 → 欢迎语 + 三按钮。 */
export async function handleStart(chatId: number, from: TgFrom, text: string, lang: BotLang) {
  const { src, post } = parseStartPayload(text);
  const { first } = await recordStart(from, src, lang, post);
  await sendPhotoOrText(chatId, heroImageUrl(lang), welcomeText(lang, first), mainKeyboard(src, lang, from.id));
  // 左下角菜单键按会话覆盖成带 src 的小程序链接，菜单打开的小程序也能归因到这条广告
  void tgCall("setChatMenuButton", {
    chat_id: chatId,
    menu_button: { type: "web_app", text: lang === "zh" ? "智聊小程序" : "ChatX app", web_app: { url: miniAppLink(src) } },
  }).catch(() => null);
}

// ── AI 自动回复（与官网 AI 客服 / 主 bot 同一套 askDeepSeek + 知识库，叠一层 ChatX 人设）──

/**
 * 每会话保留最近几轮上下文（30 分钟）。内存为主，写入时防抖落一份快照，
 * 进程重启（部署 / pm2 reload）后首次读取时回载，用户聊到一半不会被“失忆”。
 */
const HISTORY_TURNS = 6;
const HISTORY_TTL_MS = 30 * 60_000;
const HISTORY_MAX_CHATS = 5000;
const CTX_FLUSH_MS = 2000;
const history = new Map<number, { at: number; turns: ChatTurn[] }>();
let ctxLoaded: Promise<void> | null = null;
let ctxFlushTimer: ReturnType<typeof setTimeout> | null = null;

function isTurn(v: unknown): v is ChatTurn {
  if (typeof v !== "object" || v === null) return false;
  const t = v as Partial<ChatTurn>;
  return (t.role === "user" || t.role === "assistant") && typeof t.content === "string";
}

function loadCtxSnapshot(): Promise<void> {
  if (!ctxLoaded) {
    ctxLoaded = readFile(CTX_SNAPSHOT, "utf-8")
      .then((raw) => {
        const now = Date.now();
        const data = JSON.parse(raw) as Record<string, { at?: number; turns?: unknown[] }>;
        for (const [k, v] of Object.entries(data)) {
          const id = Number(k);
          if (!Number.isFinite(id) || typeof v?.at !== "number" || now - v.at > HISTORY_TTL_MS) continue;
          const turns = Array.isArray(v.turns) ? v.turns.filter(isTurn).slice(-HISTORY_TURNS) : [];
          if (turns.length && !history.has(id)) history.set(id, { at: v.at, turns });
        }
      })
      .catch(() => undefined);
  }
  return ctxLoaded;
}

function scheduleCtxFlush() {
  if (ctxFlushTimer) return;
  ctxFlushTimer = setTimeout(() => {
    ctxFlushTimer = null;
    const now = Date.now();
    const out: Record<string, { at: number; turns: ChatTurn[] }> = {};
    for (const [id, h] of history) {
      if (now - h.at > HISTORY_TTL_MS) history.delete(id);
      else out[String(id)] = h;
    }
    const tmp = `${CTX_SNAPSHOT}.${process.pid}.tmp`;
    void mkdir(path.dirname(CTX_SNAPSHOT), { recursive: true })
      .then(() => writeFile(tmp, JSON.stringify(out), "utf-8"))
      .then(() => rename(tmp, CTX_SNAPSHOT))
      .catch(() => undefined);
  }, CTX_FLUSH_MS);
  ctxFlushTimer.unref?.();
}

export async function recallHistory(chatId: number): Promise<ChatTurn[]> {
  await loadCtxSnapshot();
  const h = history.get(chatId);
  if (!h || Date.now() - h.at > HISTORY_TTL_MS) return [];
  return h.turns;
}

async function rememberTurn(chatId: number, q: string, a: string) {
  const turns = [...(await recallHistory(chatId)), { role: "user" as const, content: q }, { role: "assistant" as const, content: a }];
  if (history.size >= HISTORY_MAX_CHATS && !history.has(chatId)) {
    const oldest = history.keys().next().value;
    if (oldest !== undefined) history.delete(oldest);
  }
  history.set(chatId, { at: Date.now(), turns: turns.slice(-HISTORY_TURNS) });
  scheduleCtxFlush();
}

/** 刷屏限流：同一会话短时间内连发只答一条（与主 bot 同参数）。 */
const lastReply = new Map<number, number>();
const COOLDOWN_MS = 800;
function rateLimited(chatId: number) {
  const now = Date.now();
  if (now - (lastReply.get(chatId) ?? 0) < COOLDOWN_MS) return true;
  lastReply.set(chatId, now);
  return false;
}

/**
 * 叠在官网通用人设上的 ChatX 专属指令：这个 bot 本身就是「AI 全自动聊天」的演示，答案要短、能落到下载。
 * persona 非默认时再叠一层人设指令（见 chatx-persona.ts），产品知识与链接不变。
 */
export function chatxSystemHint(src: string, lang: BotLang, where: "bot" | "miniapp" = "bot", persona: ChatxPersona = DEFAULT_PERSONA, voice?: "on" | "off"): string {
  const layers = [chatxSceneHint(src, lang, where), personaSystemHint(persona, lang), voice ? voiceReplyHint(lang, voice === "on") : ""];
  return layers.filter(Boolean).join("\n\n");
}

function chatxSceneHint(src: string, lang: BotLang, where: "bot" | "miniapp"): string {
  const medium = where === "miniapp" ? "chatx_miniapp" : "chatx_bot";
  const dl = downloadLink(src, lang, medium);
  const tut = tutorialsLink(src, lang, medium);
  if (lang === "zh") {
    const scene = where === "miniapp" ? "「智聊 ChatX」的 Telegram 小程序里" : "「智聊 ChatX」的 Telegram 官方 bot 里";
    return (
      `【当前场景】你此刻在${scene}与潜在用户聊天，而你自己就是 ChatX「AI 全自动聊天」的现场演示。\n` +
      `- 以 ChatX 为中心回答：客户消息 AI 按用户自己的话术/人设自动回复、自动跟进成交（全自动/人审放行/仅拟稿三档），外语自动互译，已接入 ${platformsInline("zh")} 共 ${CHATX_PLATFORM_COUNT} 个平台、多账号一屏管，Windows 本地运行、数据留本机、免费下载、无需 API Key。\n` +
      "- 不要用「统一收件箱 / 全渠道」这类术语，用大白话说「AI 帮你自动回客户、自动成交」。\n" +
      "- 不要编造云端网页版、macOS 版、不存在的价格或功能；不确定就建议点「人工客服」。\n" +
      `- 下载安装链接：${dl}\n- 视频教程：${tut}\n` +
      "- 回答简短（不超过 3 句 / 120 字），纯文本、不用 Markdown 和标题；当用户表现出兴趣时自然引向下载试用。"
    );
  }
  const scene = where === "miniapp" ? "the Telegram Mini App of ChatX" : "the official Telegram bot of ChatX";
  return (
    `[Context] You are chatting inside ${scene}, and you yourself are the live demo of ChatX's "AI auto-chat".\n` +
    `- Centre answers on ChatX: AI replies to the user's customers in their own voice/persona, follows up and closes automatically (fully automatic / human-approved / draft-only), live translation, ${CHATX_PLATFORM_COUNT} platforms connected (${platformsInline("en")}) with multi-account on one screen, runs locally on Windows, data stays on the PC, free download, no API key.\n` +
    "- Avoid jargon like \"unified inbox / omni-channel\"; say plainly \"AI answers your customers and closes for you\".\n" +
    "- Never invent a web/cloud version, a macOS build, or prices/features you are not sure about; suggest human support instead.\n" +
    `- Download link: ${dl}\n- Video tutorials: ${tut}\n` +
    "- Keep it short (max 3 sentences / ~60 words), plain text, no Markdown or headings; when the user shows interest, naturally point them to the free download."
  );
}

/** AI 回复后的紧凑键盘：教程 + 人工，下载放最后一行（与首条键盘一致，小程序只走菜单键）。 */
export function replyKeyboard(src: string, lang: BotLang, uid?: number): InlineBtn[][] {
  const zh = lang === "zh";
  return [
    [
      { text: zh ? "📖 视频教程" : "📖 Tutorials", url: tutorialsLink(src, lang) },
      { text: zh ? "👤 人工客服" : "👤 Human support", callback_data: "cx_human" },
    ],
    [{ text: zh ? "📥 免费下载桌面版" : "📥 Free desktop download", url: downloadLink(src, lang, "chatx_bot", uid) }],
  ];
}

/** 人工客服回调后的键盘：直达人工 + 一键留资（客服主动找他）。 */
export function humanKeyboard(lang: BotLang): InlineBtn[][] {
  const zh = lang === "zh";
  return [
    [{ text: zh ? "👤 打开人工客服" : "👤 Open human support", url: CONTACT_URL }],
    [{ text: zh ? "🙋 让客服联系我" : "🙋 Have support contact me", callback_data: "cx_lead" }],
  ];
}

/** Telegram 的 typing 状态只持续 5 秒，AI 最长等 12 秒：每 4.5 秒续一次，直到 promise 结束。 */
const TYPING_KEEPALIVE_MS = 4500;
async function withTyping<T>(chatId: number, work: () => Promise<T>, action: "typing" | "record_voice" = "typing"): Promise<T> {
  await tgCall("sendChatAction", { chat_id: chatId, action });
  const timer = setInterval(() => void tgCall("sendChatAction", { chat_id: chatId, action }), TYPING_KEEPALIVE_MS);
  timer.unref?.();
  try {
    return await work();
  } finally {
    clearInterval(timer);
  }
}

/**
 * AI 回答里的裸链接不直接发（带 utm 的下载链 100+ 字符，手机上一块乱码）：正文去链接，
 * 下载 / 教程 / 人工已由 replyKeyboard 承接，其余外链单独成一行按钮。与小程序 AiChat 共用 splitChatLinks。
 */
export function aiReplyPayload(answer: string, src: string, lang: BotLang, uid?: number): { text: string; keyboard: InlineBtn[][] } {
  const { text, urls } = splitChatLinks(answer);
  const keyboard = replyKeyboard(src, lang, uid);
  const known = /\/download\/chatx|\/tutorials|\/chatx\/tutorials/;
  for (const u of urls) {
    if (known.test(u) || u.startsWith(CONTACT_URL) || u.startsWith(CHATX_BOT_URL)) continue;
    let host = u;
    try {
      host = new URL(u).hostname.replace(/^www\./, "");
    } catch {
      continue;
    }
    keyboard.push([{ text: `🔗 ${host}`, url: u }]);
  }
  return { text: text || (lang === "zh" ? "👇 点下面的按钮：" : "👇 Tap a button below:"), keyboard };
}

/** AI 不可用（额度到顶 / 超时 / 熔断）且知识库没命中时的 ChatX 专属提示：说清是系统忙，给出可点的下一步，不走官网通用兜底。 */
export function aiBusyText(lang: BotLang): string {
  return lang === "zh"
    ? "😅 AI 这会儿有点忙，没接上。你可以先点下面的按钮看教程或下载，或直接转人工客服；稍后再发一句我就能答。"
    : "😅 The AI is busy right now. Tap a button below for tutorials or the download, or reach human support; try me again in a moment.";
}

/**
 * 文字答完后补一条语音（同一段话朗读）：先发文字保证秒回和按钮，再 record_voice + 合成 + sendVoice；
 * 任何一步失败只是没有语音。返回是否发出，供埋点。
 */
async function voiceFollowUp(chatId: number, uid: number, src: string, answer: string, lang: BotLang, viaVoice: boolean, prefs: ChatxPrefs, force = false): Promise<void> {
  if (!force && !wantsVoiceReply(prefs, viaVoice)) return;
  const persona = prefs.persona ?? DEFAULT_PERSONA;
  const spoken = ttsText(answer);
  const deny = takeVoiceSlot(uid, spoken.length);
  if (deny) {
    if (deny !== "disabled") await trackTg("chatx_bot_voice_out", { src, uid, ok: false, why: deny, persona });
    return;
  }
  const synth = await withTyping(chatId, () => synthesizeVoice(spoken, lang, persona, prefs), "record_voice");
  const sent = synth ? await sendVoice(chatId, synth.mp3, synth.durationSec) : null;
  await trackTg("chatx_bot_voice_out", {
    src,
    uid,
    ok: Boolean(sent?.ok),
    why: !synth ? "tts_failed" : sent?.ok ? undefined : "send_failed",
    chars: spoken.length,
    sec: synth ? Math.round(synth.durationSec) : undefined,
    ms: synth?.ms,
    ref: synth?.ref,
    voice: synth?.voice,
    persona,
  });
}

/** 自由文本 → AI 自动回复：同官网 /api/chat 链路（DeepSeek + 知识库 → 关键词知识库 → ChatX 专属忙提示），共用每日额度闸。 */
export async function handleFreeText(chatId: number, from: TgFrom, text: string, lang: BotLang, opts?: { heard?: string }) {
  if (rateLimited(chatId)) return;
  const src = await lastSrc(from.id);
  const klang = detectKnowledgeLang(text);
  const viaVoice = Boolean(opts?.heard);
  const quote = opts?.heard ? `🎙 ${opts.heard}\n\n` : "";
  const prefs = await getPrefs(from.id);
  const persona = prefs.persona ?? DEFAULT_PERSONA;

  const canVoice = voiceOutEnabled();
  const forceVoice = canVoice && asksForVoice(text);
  const willVoice = canVoice && (forceVoice || wantsVoiceReply(prefs, viaVoice));
  const voiceMode = canVoice ? (willVoice ? "on" : "off") : undefined;

  const guard = dailyGuard();
  const raw = guard.allowed
    ? await withTyping(chatId, async () => {
        const past = await recallHistory(chatId);
        const hist = willVoice ? past.map((t) => (t.role === "assistant" ? { ...t, content: scrubVoiceDenial(t.content) } : t)) : past;
        return askDeepSeek(text, klang, hist, 12000, chatxSystemHint(src, klang, "bot", persona, voiceMode));
      })
    : null;
  const llm = raw && willVoice ? scrubVoiceDenial(raw) : raw;
  if (llm) {
    const { text: body, keyboard } = aiReplyPayload(llm, src, klang, from.id);
    await rememberTurn(chatId, text, llm);
    await sendText(chatId, quote + body, keyboard, { plain: true });
    void logChat({ q: text, a: llm, lang: klang, source: "chatx_bot_ai" });
    await trackTg("chatx_bot_ai", { src, uid: from.id, mode: "ai", via: viaVoice ? "voice" : undefined, persona });
    await voiceFollowUp(chatId, from.id, src, llm, klang, viaVoice, prefs, forceVoice);
    return;
  }

  const kb = matchFreeText(text, klang);
  const reply = kb ?? aiBusyText(klang);
  await sendText(chatId, (quote ? `<i>${escHtml(quote.trim())}</i>\n\n` : "") + reply, replyKeyboard(src, klang, from.id));
  void logChat({ q: text, a: reply, lang: klang, source: guard.allowed ? "chatx_bot_kb" : "chatx_bot_capped" });
  await trackTg("chatx_bot_ai", { src, uid: from.id, mode: kb ? "kb" : "unavailable", via: viaVoice ? "voice" : undefined, persona });
}

export type TgVoice = { file_id: string; duration?: number; mime_type?: string; file_size?: number };

/**
 * 用户发语音：ASR 转写后按文字同一链路回答（并回语音）。没配 ASR 中继 → 走原「只看文字」提示；
 * 太长 / 下载失败 / 没听清 → 各自一句话提示，绝不静默。
 */
export async function handleVoiceMessage(chatId: number, from: TgFrom, voice: TgVoice, lang: BotLang) {
  const prefs = await getPrefs(from.id);
  if (voiceOutEnabled() && cloneArmed(prefs.cloneArmedAt)) {
    await handleCloneSample(chatId, from, voice, lang, prefs);
    return;
  }
  if (!voiceInEnabled()) {
    await handleNonText(chatId, from, "voice", lang);
    return;
  }
  const src = await lastSrc(from.id);
  const zh = lang === "zh";
  const sec = typeof voice.duration === "number" ? voice.duration : 0;
  if (sec > VOICE_IN_MAX_SEC || (typeof voice.file_size === "number" && voice.file_size > VOICE_IN_MAX_BYTES)) {
    await sendText(chatId, zh ? `🎙 这条语音有点长，我一次只听 ${VOICE_IN_MAX_SEC} 秒以内的——拆短一点再发，或直接打字。` : `🎙 That voice message is a bit long — I can take up to ${VOICE_IN_MAX_SEC}s at a time. Send a shorter one or just type.`);
    await trackTg("chatx_bot_voice_in", { src, uid: from.id, ok: false, why: "too_long", sec });
    return;
  }
  const started = Date.now();
  const heard = await withTyping(chatId, async () => {
    const audio = await downloadTgFile(voice.file_id, VOICE_IN_MAX_BYTES);
    return audio ? transcribeVoice(audio, voice.mime_type || "audio/ogg") : null;
  });
  await trackTg("chatx_bot_voice_in", { src, uid: from.id, ok: Boolean(heard), why: heard ? undefined : "asr_failed", sec, chars: heard?.length ?? 0, ms: Date.now() - started });
  if (!heard) {
    await sendText(chatId, zh ? "🎙 没听清这条语音，再说一次或直接打字问我。" : "🎙 I couldn't make that out — try again or just type your question.");
    return;
  }
  await handleFreeText(chatId, from, heard, lang, { heard });
}

/** /voice：语音回复 开/关（显式设置，覆盖人设默认）。 */
export async function handleVoiceToggle(chatId: number, from: TgFrom, arg: string, lang: BotLang) {
  const src = await lastSrc(from.id);
  const zh = lang === "zh";
  if (!voiceOutEnabled()) {
    await sendText(chatId, zh ? "🔇 这里暂时没开语音回复，装好 ChatX 桌面版就能用语音给客户回话。" : "🔇 Voice replies aren't available here yet; the ChatX desktop app can answer your customers by voice.");
    return;
  }
  const prefs = await getPrefs(from.id);
  const cur = wantsVoiceReply(prefs, false);
  const next = /^(on|开|开启|1)$/i.test(arg) ? true : /^(off|关|关闭|0)$/i.test(arg) ? false : !cur;
  await setPrefs(from.id, { voice: next });
  await trackTg("chatx_bot_cmd", { cmd: "voice", src, uid: from.id, on: next });
  await sendText(
    chatId,
    next
      ? zh ? "🔊 语音回复已开：之后我每句回答都会附一条语音。发 /voice 可关。" : "🔊 Voice replies on: I'll attach a voice note to each answer. Send /voice to turn off."
      : zh ? "🔇 语音回复已关：只回文字（你发语音我照样听得懂）。发 /voice 可再开。" : "🔇 Voice replies off: text only (I still understand your voice messages). Send /voice to turn back on."
  );
}

export type NonTextKind = "voice" | "audio" | "photo" | "video" | "video_note" | "document" | "sticker" | "location" | "other";

/** 语音 / 图片 / 贴纸等非文字消息：不能静默，告诉用户目前只看文字（语音转写是 ChatX 桌面版能力，bot 后续再接）。 */
export async function handleNonText(chatId: number, from: TgFrom, kind: NonTextKind, lang: BotLang) {
  if (rateLimited(chatId)) return;
  const src = await lastSrc(from.id);
  const zh = lang === "zh";
  const what: Record<NonTextKind, [string, string]> = {
    voice: ["语音", "voice message"],
    audio: ["音频", "audio"],
    photo: ["图片", "photo"],
    video: ["视频", "video"],
    video_note: ["视频消息", "video note"],
    document: ["文件", "file"],
    sticker: ["贴纸", "sticker"],
    location: ["位置", "location"],
    other: ["这类消息", "this kind of message"],
  };
  const [zhName, enName] = what[kind];
  const media = kind === "photo" || kind === "document" || kind === "video";
  let text: string;
  if (kind === "voice" || kind === "audio" || kind === "video_note") {
    text = zh
      ? `🎙 收到你的${zhName}，语音识别这会儿没接上，先打字问我吧，我照样马上回你。`
      : `🎙 Got your ${enName} — voice recognition isn't available right now, so please type your question and I'll answer right away.`;
  } else if (media) {
    text = zh
      ? `🖼 收到你的${zhName}。如果是遇到问题 / 报错，点「🩺 报障 / 求助」，按提示发机器码或诊断回执号，客服能直接定位；也可以点「👤 人工客服」把截图发给客服。`
      : `🖼 Got your ${enName}. If something isn't working, tap “🩺 Report a problem” and send your machine code or diagnostic receipt so support can pinpoint it — or tap “👤 Human support” to share the screenshot with our team.`;
  } else {
    text = zh ? `🙌 收到你的${zhName}！有问题直接打字或发语音问我。` : `🙌 Got your ${enName}! Type or send a voice message with your question.`;
  }
  const kb = replyKeyboard(src, lang, from.id);
  await sendText(chatId, text, media ? [[{ text: zh ? "🩺 报障 / 求助" : "🩺 Report a problem", callback_data: "cxb:start" }], ...kb] : kb);
  await trackTg("chatx_bot_nontext", { src, uid: from.id, kind });
}

/** 用户直接发了名片（手机号）：视为留资，写进统一线索库并通知管理员，带 src 归因。 */
export async function handleContact(chatId: number, from: TgFrom, contact: { phone_number?: string; first_name?: string }, lang: BotLang) {
  const src = await lastSrc(from.id);
  const zh = lang === "zh";
  const phone = (contact.phone_number ?? "").trim();
  const handle = from.username ? `@${from.username}` : `tg:${chatId}`;
  const rec = {
    t: new Date().toISOString(),
    name: contact.first_name || from.first_name || "",
    contact: phone ? `${phone} (${handle})` : handle,
    interest: zh ? "ChatX bot 发送名片" : "ChatX bot shared contact",
    message: "",
    lang,
    source: "chatx_bot",
    utm: `telegram/chatx_bot/${src}`,
    verified: "verified",
    tg_user_id: String(from.id),
  };
  try {
    const { entry, isNew } = await upsertLead(rec);
    await appendLead(rec);
    if (isNew) await notifyAdminsOfLead(entry);
  } catch {
    /* best-effort */
  }
  await sendText(
    chatId,
    zh
      ? "✅ 收到！人工客服会尽快通过 Telegram 联系你。等不及的话，可以先在这里问我，或点下面直接找人工。"
      : "✅ Got it! Our team will reach you on Telegram shortly. Meanwhile ask me anything here, or tap below for human support.",
    replyKeyboard(src, lang, from.id)
  );
  await trackTg("chatx_bot_lead", { src, uid: from.id, via: "contact" });
}

/** 用户在 bot 里的可读标识（管理员通知用）。 */
function whoLabel(from: TgFrom): string {
  if (from.username) return `@${from.username}`;
  return from.first_name ? `${from.first_name}（id ${from.id}）` : `id ${from.id}`;
}

/** 一键留资「让客服联系我」：同名片留资走同一条线索链（upsert + 通知管理员），用户无需打字。 */
export async function handleQuickLead(chatId: number, from: TgFrom, src: string, lang: BotLang) {
  const zh = lang === "zh";
  const rec = {
    t: new Date().toISOString(),
    name: from.first_name || "",
    contact: from.username ? `@${from.username}` : `tg:${chatId}`,
    interest: zh ? "ChatX bot 让客服联系我" : "ChatX bot: contact me",
    message: "",
    lang,
    source: "chatx_bot",
    utm: `telegram/chatx_bot/${src}`,
    verified: "verified",
    tg_user_id: String(from.id),
  };
  try {
    const { entry, isNew } = await upsertLead(rec);
    await appendLead(rec);
    if (isNew) await notifyAdminsOfLead(entry);
  } catch {
    /* best-effort */
  }
  await sendText(
    chatId,
    zh
      ? "✅ 已记下！人工客服会尽快通过 Telegram 联系你（工作时间约 5 分钟内）。等的时候也可以先在这里问我。"
      : "✅ Noted! Our team will reach you on Telegram shortly (~5 min during work hours). Feel free to ask me anything meanwhile.",
    replyKeyboard(src, lang, from.id)
  );
  await trackTg("chatx_bot_lead", { src, uid: from.id, via: "quick" });
}

/** 人工客服：给用户直达链接 + 一键留资，同时通知管理员去迎（主 bot 发，管理员都绑在主 bot 上）。 */
export async function handleHuman(chatId: number, from: TgFrom, src: string, lang: BotLang) {
  const zh = lang === "zh";
  await sendText(
    chatId,
    zh
      ? "👤 已为你转人工客服：点下面按钮直达，人工已收到提醒。\n不想切屏的话，点「让客服联系我」，客服会主动来找你；急事也可以先在这里问我。"
      : "👤 Connecting you to human support — tap below; our team has been notified.\nOr tap “Have support contact me” and we’ll reach out to you; you can also keep asking me here.",
    humanKeyboard(lang)
  );
  await notifyAdmins(
    `🔒 管理员 · 仅你可见\n🙋 <b>ChatX bot 用户请求人工客服</b>：${whoLabel(from)}（来源 ${src}）\n请到 ${CONTACT_URL} 主动迎接（用户已拿到人工入口）。`
  );
  await trackTg("chatx_bot_human", { src, uid: from.id, user: from.username ?? String(from.id) });
}

/** 其他命令：下载 / 教程 / 功能；非命令文本交给 AI 自动回复。 */
export async function handleOther(chatId: number, from: TgFrom, text: string, lang: BotLang) {
  const src = await lastSrc(from.id);
  if (/^\/(download|dl)\b/i.test(text)) {
    await trackTg("chatx_bot_cmd", { cmd: "download", src, uid: from.id });
    await sendText(
      chatId,
      lang === "zh" ? "📥 点下面按钮下载 Windows 安装包（约 450 MB，装完即用）：" : "📥 Tap below to download the Windows installer (~450 MB):",
      [[{ text: lang === "zh" ? "📥 下载桌面版" : "📥 Download desktop", url: downloadLink(src, lang, "chatx_bot", from.id) }]]
    );
    return;
  }
  if (/^\/tutorials?\b/i.test(text)) {
    await trackTg("chatx_bot_cmd", { cmd: "tutorials", src, uid: from.id });
    await sendText(
      chatId,
      lang === "zh"
        ? `📖 ${CHATX_TUTORIAL_COUNT} 集短视频、约 ${Math.round(CHATX_TUTORIAL_TOTAL_SEC / 60)} 分钟：从安装到接入渠道、AI 自动回复，装好就会用。`
        : `📖 ${CHATX_TUTORIAL_COUNT} short videos, ~${Math.round(CHATX_TUTORIAL_TOTAL_SEC / 60)} min: install, connect channels, AI auto-reply.`,
      [[{ text: lang === "zh" ? "📖 看教程" : "📖 Watch tutorials", url: tutorialsLink(src, lang) }]]
    );
    return;
  }
  if (/^\/(features|help)\b/i.test(text)) {
    await trackTg("chatx_bot_cmd", { cmd: "features", src, uid: from.id });
    await sendText(chatId, featuresText(lang), mainKeyboard(src, lang, from.id));
    return;
  }
  if (/^\/(support|human|cs)\b/i.test(text)) {
    await handleHuman(chatId, from, src, lang);
    return;
  }
  const voicesCmd = text.match(/^\/(?:voices|timbre|sound)(?:@\w+)?\s*(\S*)/i);
  if (voicesCmd) {
    await handleVoices(chatId, from, lang, voicesCmd[1]);
    return;
  }
  if (/^\/(?:clone|myvoice)\b/i.test(text)) {
    await handleCloneStart(chatId, from, lang);
    return;
  }
  const voiceCmd = text.match(/^\/voice(?:@\w+)?\s*(\S*)/i);
  if (voiceCmd) {
    await handleVoiceToggle(chatId, from, voiceCmd[1], lang);
    return;
  }
  const personaCmd = text.match(/^\/(?:persona|role|mode)(?:@\w+)?\s*(\S*)/i);
  if (personaCmd) {
    await handlePersona(chatId, from, personaCmd[1], lang);
    return;
  }
  if (/^\//.test(text)) {
    await sendText(chatId, welcomeText(lang, false), mainKeyboard(src, lang, from.id));
    return;
  }
  await handleFreeText(chatId, from, text, lang);
}

export async function handleCallback(chatId: number, cqId: string, data: string, from: TgFrom, lang: BotLang) {
  await answerCallback(cqId);
  const src = await lastSrc(from.id);
  if (data === "cx_features") {
    await trackTg("chatx_bot_cmd", { cmd: "features", src, uid: from.id });
    await sendText(chatId, featuresText(lang), mainKeyboard(src, lang, from.id));
  } else if (data === "cx_human") {
    await handleHuman(chatId, from, src, lang);
  } else if (data === "cx_lead") {
    await handleQuickLead(chatId, from, src, lang);
  } else if (data.startsWith("cx_persona:")) {
    await handlePersona(chatId, from, data.slice("cx_persona:".length), lang);
  } else if (data === "cx_voices" || data.startsWith("cx_voices:")) {
    await handleVoices(chatId, from, lang, data.split(":")[1]);
  } else if (data.startsWith("cx_vset:")) {
    const [, p, c] = data.split(":");
    const persona = parsePersona(p);
    if (persona) await handleVoicePick(chatId, from, persona, c ?? "", lang);
  } else if (data === "cx_vclone") {
    await handleCloneStart(chatId, from, lang);
  } else if (data === "cx_vconsent") {
    await handleCloneConsent(chatId, from, lang);
  } else if (data === "cx_vdel") {
    await handleVoiceDelete(chatId, from, lang);
  }
}

/** /persona 选择键盘：默认人设独占首行，演示人设同一行；当前的打勾。 */
export function personaKeyboard(cur: ChatxPersona, lang: BotLang): InlineBtn[][] {
  const btn = (p: ChatxPersona) => ({ text: `${cur === p ? "✅ " : ""}${personaLabel(p, lang)}`, callback_data: `cx_persona:${p}` });
  const [def, ...demos] = PERSONAS;
  return [[btn(def)], demos.map(btn)];
}

/**
 * /persona：不带参数 → 当前人设 + 选择键盘；带参数 / 按钮 → 切换并用新人设的语气确认。
 * 不动用户显式设过的 /voice；没设过的话恢爱陈聊默认带语音（wantsVoiceReply）。
 */
export async function handlePersona(chatId: number, from: TgFrom, arg: string, lang: BotLang) {
  const src = await lastSrc(from.id);
  const prefs = await getPrefs(from.id);
  const cur = prefs.persona ?? DEFAULT_PERSONA;
  const next = parsePersona(arg);
  if (!next) {
    await trackTg("chatx_bot_cmd", { cmd: "persona", src, uid: from.id, persona: cur });
    await sendText(chatId, personaMenuText(cur, lang), personaKeyboard(cur, lang));
    return;
  }
  const saved = next === cur ? prefs : await setPrefs(from.id, { persona: next });
  await trackTg("chatx_bot_cmd", { cmd: "persona", src, uid: from.id, persona: next, from: cur });
  await sendText(chatId, personaSwitchedText(next, lang, voiceOutEnabled() && wantsVoiceReply(saved, false)), replyKeyboard(src, lang, from.id));
}

// ── 音色：按人设选库音色 / 克隆自己的声音 ─────────────────────────────

const VOICE_PREVIEW_SEC = 6;

/** /voices 键盘：目标人设一行 → 库音色两列 → 我的声音 / 克隆 → 默认 / 删除。 */
export function voicesKeyboard(target: ChatxPersona, prefs: ChatxPrefs, avail: Set<string>, lang: BotLang): InlineBtn[][] {
  const zh = lang === "zh";
  const cur = resolveVoiceChoice(prefs, target).choice;
  const rec = PERSONA_DEFAULT_VOICE[target];
  const mark = (c: string) => (cur === c ? "✅ " : c === rec ? "⭐ " : "");
  const rows: InlineBtn[][] = [PERSONAS.map((p) => ({ text: `${p === target ? "▸ " : ""}${personaZhShort(p, lang)}`, callback_data: `cx_voices:${p}` }))];
  const lib = VOICEPACK.filter((v) => avail.has(v.spk));
  for (let i = 0; i < lib.length; i += 2) {
    rows.push(lib.slice(i, i + 2).map((v) => ({ text: mark(v.spk) + voiceLabel(v.spk, lang), callback_data: `cx_vset:${target}:${v.spk}` })));
  }
  rows.push(
    hasMyVoice(prefs)
      ? [
          { text: mark(MY_VOICE) + voiceLabel(MY_VOICE, lang), callback_data: `cx_vset:${target}:${MY_VOICE}` },
          { text: zh ? "🔁 重新克隆" : "🔁 Re-clone", callback_data: "cx_vclone" },
        ]
      : [{ text: zh ? "🧬 克隆我自己的声音" : "🧬 Clone my own voice", callback_data: "cx_vclone" }]
  );
  const last: InlineBtn[] = [{ text: mark(DEFAULT_VOICE) + (zh ? "↩️ 恢复默认" : "↩️ Default"), callback_data: `cx_vset:${target}:${DEFAULT_VOICE}` }];
  if (hasMyVoice(prefs)) last.push({ text: zh ? "🗑 删除我的声音" : "🗑 Delete my voice", callback_data: "cx_vdel" });
  rows.push(last);
  return rows;
}

function personaZhShort(p: ChatxPersona, lang: BotLang): string {
  return personaLabel(p, lang).replace(/ · .*$/, "");
}

export function voicesMenuText(target: ChatxPersona, prefs: ChatxPrefs, lang: BotLang): string {
  const zh = lang === "zh";
  const { choice, explicit } = resolveVoiceChoice(prefs, target);
  const cur = `<b>${escHtml(explicit ? voiceLabel(choice, lang) : zh ? "默认" : "Default")}</b>`;
  return zh
    ? `🎙 <b>${personaLabel(target, "zh")}</b> 当前声音：${cur}\n\n` +
        "每个人设可以配不同的声音：下面是 ChatX 真人音色库（棚录真人授权音色，⭐ 为该人设推荐），点一下就换，并先放一段原声给你听；" +
        "也可以<b>克隆你自己的声音</b>，让 AI 用你的声音回话——这正是 ChatX 给客户回语音的能力。\n" +
        "<i>第一行切换要设置的人设；语音回复开关用 /voice。</i>"
    : `🎙 Voice for <b>${personaLabel(target, "en")}</b>: ${cur}\n\n` +
        "Each persona can use a different voice: pick one from the ChatX studio voice library (⭐ = recommended; you'll hear the original sample first), " +
        "or <b>clone your own voice</b> so the AI answers in it — exactly what ChatX does for your customers.\n" +
        "<i>The first row picks which persona to configure; /voice toggles voice replies.</i>";
}

export async function handleVoices(chatId: number, from: TgFrom, lang: BotLang, targetArg?: string) {
  const src = await lastSrc(from.id);
  const prefs = await getPrefs(from.id);
  const target = parsePersona(targetArg) ?? prefs.persona ?? DEFAULT_PERSONA;
  if (!voiceOutEnabled()) {
    await sendText(chatId, lang === "zh" ? "🔇 这里暂时没开语音回复，装好 ChatX 桌面版就能用克隆音给客户回话。" : "🔇 Voice replies aren't available here yet; the ChatX desktop app can answer your customers in a cloned voice.");
    return;
  }
  await trackTg("chatx_bot_cmd", { cmd: "voices", src, uid: from.id, persona: target });
  await sendText(chatId, voicesMenuText(target, prefs, lang), voicesKeyboard(target, prefs, await availableVoices(), lang));
}

/** 库音色试听：直接放参考原声前几秒（真人录音、零 GPU 开销）。 */
async function sendLibraryPreview(chatId: number, spk: string): Promise<boolean> {
  try {
    const d = decodeWavMono(await readFile(voicepackPath(spk)));
    if (!d) return false;
    const wav = encodeWavMono(d.samples.subarray(0, Math.floor(VOICE_PREVIEW_SEC * d.sampleRate)), d.sampleRate);
    const mp3 = await wavToMp3(wav);
    if (!mp3) return false;
    const sent = await sendVoice(chatId, mp3.mp3, mp3.durationSec);
    return Boolean(sent?.ok);
  } catch {
    return false;
  }
}

/** 用某人设当前声音合成一句示范（占语音配额）；成功返回 true。 */
async function sendVoiceSample(chatId: number, uid: number, lang: BotLang, persona: ChatxPersona, prefs: ChatxPrefs): Promise<boolean> {
  const line = lang === "zh" ? "你好，我是小界。现在听到的，就是 ChatX 用你的声音克隆出来的效果。" : "Hi, I'm Xiaojie. What you're hearing is ChatX speaking in your cloned voice.";
  if (takeVoiceSlot(uid, line.length)) return false;
  const synth = await withTyping(chatId, () => synthesizeVoice(line, lang, persona, prefs), "record_voice");
  const sent = synth ? await sendVoice(chatId, synth.mp3, synth.durationSec) : null;
  return Boolean(sent?.ok);
}

export async function handleVoicePick(chatId: number, from: TgFrom, persona: ChatxPersona, choiceArg: string, lang: BotLang) {
  const zh = lang === "zh";
  const src = await lastSrc(from.id);
  const choice = parseVoiceChoice(choiceArg);
  if (!choice) return;
  const prefs = await getPrefs(from.id);
  if (choice === MY_VOICE && !hasMyVoice(prefs)) {
    await handleCloneStart(chatId, from, lang);
    return;
  }
  const saved = await setPrefs(from.id, { voices: { ...prefs.voices, [persona]: choice } });
  const avail = await availableVoices();
  await sendText(
    chatId,
    zh
      ? `✅ 「${personaLabel(persona, "zh")}」的声音已换成 <b>${escHtml(voiceLabel(choice, lang))}</b>。` + (wantsVoiceReply(saved, false) ? "" : "\n<i>语音回复目前是关的，发 /voice 打开后就能听到。</i>")
      : `✅ Voice for “${personaLabel(persona, "en")}” set to <b>${escHtml(voiceLabel(choice, lang))}</b>.` + (wantsVoiceReply(saved, false) ? "" : "\n<i>Voice replies are off — send /voice to hear it.</i>"),
    voicesKeyboard(persona, saved, avail, lang)
  );
  let preview = false;
  if (choice === MY_VOICE) preview = await sendVoiceSample(chatId, from.id, lang, persona, saved);
  else if (choice !== DEFAULT_VOICE) preview = await sendLibraryPreview(chatId, choice);
  await trackTg("chatx_bot_voice_pick", { src, uid: from.id, persona, voice: choice, preview });
}

/** 克隆入口：先讲规则 + 本人授权（owner_consent），点同意才开始收样本。 */
export async function handleCloneStart(chatId: number, from: TgFrom, lang: BotLang) {
  const zh = lang === "zh";
  const src = await lastSrc(from.id);
  if (!voiceOutEnabled()) {
    await handleVoices(chatId, from, lang);
    return;
  }
  await trackTg("chatx_bot_voice_clone", { src, uid: from.id, step: "start" });
  await sendText(
    chatId,
    zh
      ? "🧬 <b>克隆你自己的声音</b>（和 ChatX 桌面版同一套规则）\n\n" +
          "• 录一条 <b>8–15 秒</b>的语音（3 秒以下不收，最多用前 20 秒）\n" +
          "• 安静环境、单人、连续说话，别离麦太近（破音会被拒）\n" +
          "• 只能克隆<b>你本人</b>的声音；样本只存在我们服务器上用于给你合成语音，随时可在 /voices 里删除\n\n" +
          "确认是你本人的声音并同意克隆，点下面按钮后再录。"
      : "🧬 <b>Clone your own voice</b> (same rules as the ChatX desktop app)\n\n" +
          "• Record <b>8–15 seconds</b> of speech (under 3s is rejected; only the first 20s are used)\n" +
          "• Quiet room, one speaker, continuous speech, not too close to the mic\n" +
          "• Only <b>your own</b> voice; the sample stays on our server only to synthesize replies for you and can be deleted any time in /voices\n\n" +
          "Tap below to confirm it's your own voice and you consent, then record.",
    [[{ text: zh ? "✅ 是我本人的声音，同意克隆" : "✅ It's my own voice — I consent", callback_data: "cx_vconsent" }]]
  );
}

export async function handleCloneConsent(chatId: number, from: TgFrom, lang: BotLang) {
  const src = await lastSrc(from.id);
  await setPrefs(from.id, { cloneArmedAt: new Date().toISOString() });
  await trackTg("chatx_bot_voice_clone", { src, uid: from.id, step: "consent" });
  await sendText(
    chatId,
    lang === "zh"
      ? "🎙 好，现在按住麦克风录一条 8–15 秒的语音发给我（10 分钟内有效）。不知道说什么？就读：\n<i>「你好，我是 ChatX 的用户，这是我的声音样本，用来体验 AI 用我的声音自动回复客户。」</i>"
      : "🎙 Great — hold the mic and send me an 8–15 second voice message (valid for 10 minutes). Not sure what to say? Read:\n<i>“Hi, I'm a ChatX user and this is my voice sample for trying AI replies in my own voice.”</i>"
  );
}

/** 已授权窗口内收到的语音 → 体检 + 登记；红灯拒收并保持窗口，成功后让用户明确选择用在哪个人设。 */
export async function handleCloneSample(chatId: number, from: TgFrom, voice: TgVoice, lang: BotLang, prefs: ChatxPrefs) {
  const zh = lang === "zh";
  const src = await lastSrc(from.id);
  const sec = typeof voice.duration === "number" ? voice.duration : 0;
  if (sec > CLONE_IN_MAX_SEC || (typeof voice.file_size === "number" && voice.file_size > CLONE_IN_MAX_BYTES)) {
    await sendText(chatId, zh ? "🎙 这条太长了，8–15 秒就够，重录一条短的发我。" : "🎙 That's too long — 8–15 seconds is plenty. Please send a shorter one.");
    await trackTg("chatx_bot_voice_clone", { src, uid: from.id, step: "sample", ok: false, why: "too_long", sec });
    return;
  }
  const started = Date.now();
  const res = await withTyping(chatId, async () => {
    const audio = await downloadTgFile(voice.file_id, CLONE_IN_MAX_BYTES);
    return audio ? enrollUserVoice(from.id, Buffer.from(audio), prefs.cloneArmedAt ?? new Date().toISOString()) : null;
  });
  if (!res || !res.ok) {
    const rej: Extract<CloneResult, { ok: false }> = res && !res.ok ? res : { ok: false, code: "not_decodable" };
    await sendText(chatId, cloneRejectText(rej, lang));
    await trackTg("chatx_bot_voice_clone", { src, uid: from.id, step: "sample", ok: false, why: res ? rej.code : "download_failed", issue: rej.health?.issues[0], grade: rej.health?.grade, sec, ms: Date.now() - started });
    return;
  }
  const old = prefs.myVoice?.path;
  const saved = await setPrefs(from.id, { myVoice: res.voice, cloneArmedAt: undefined });
  if (old && old !== res.voice.path) await removeUserVoiceFile(old);
  await trackTg("chatx_bot_voice_clone", { src, uid: from.id, step: "sample", ok: true, grade: res.voice.grade, score: res.voice.score, sec: res.voice.sec, ms: Date.now() - started });
  const h = res.health;
  const verdict = h.grade === "green" ? (zh ? "🟢 质量良好" : "🟢 Good quality") : zh ? "🟡 可用" : "🟡 Usable";
  const persona = prefs.persona ?? DEFAULT_PERSONA;
  await sendText(
    chatId,
    zh
      ? `${verdict}，已克隆你的声音（${res.voice.sec}s，${h.score} 分）。${h.grade === "yellow" ? `\n<i>${escHtml(h.hints.join("；"))}</i>` : ""}\n\n要把它用在哪个人设上？（不选就不会替换现有声音）`
      : `${verdict} — your voice is cloned (${res.voice.sec}s, score ${h.score}).\n\nWhich persona should use it? (Nothing changes until you choose.)`,
    [
      PERSONAS.map((p) => ({ text: personaZhShort(p, lang), callback_data: `cx_vset:${p}:${MY_VOICE}` })),
      [{ text: zh ? "先不用" : "Not now", callback_data: `cx_voices:${persona}` }],
    ]
  );
  await sendVoiceSample(chatId, from.id, lang, persona, { ...saved, voices: { ...saved.voices, [persona]: MY_VOICE } });
}

export async function handleVoiceDelete(chatId: number, from: TgFrom, lang: BotLang) {
  const src = await lastSrc(from.id);
  const prefs = await getPrefs(from.id);
  const voices = { ...prefs.voices };
  for (const p of PERSONAS) if (voices[p] === MY_VOICE) delete voices[p];
  await removeUserVoiceFile(prefs.myVoice?.path);
  const saved = await setPrefs(from.id, { myVoice: undefined, cloneArmedAt: undefined, voices });
  await trackTg("chatx_bot_voice_clone", { src, uid: from.id, step: "delete" });
  const target = prefs.persona ?? DEFAULT_PERSONA;
  await sendText(
    chatId,
    lang === "zh" ? "🗑 已删除你的声音样本，用到它的人设已恢复默认声音。" : "🗑 Your voice sample is deleted; personas using it are back to the default voice.",
    voicesKeyboard(target, saved, await availableVoices(), lang)
  );
}

/** 窗口内 start 过的用户（每人取最近一次），给每日推送选受众；lastT = 最近一次 start 时间。 */
export async function recentStarts(sinceMs: number): Promise<Map<number, StartRec & { lastT: number }>> {
  const out = new Map<number, StartRec & { lastT: number }>();
  for (const s of await readJsonl<StartRec>(STARTS_LOG)) {
    if (typeof s.uid !== "number") continue;
    const t = Date.parse(s.t);
    if (!Number.isFinite(t) || t < sinceMs) continue;
    const cur = out.get(s.uid);
    if (!cur || t > cur.lastT) out.set(s.uid, { ...s, src: normalizeSrc(s.src), lastT: t });
  }
  return out;
}

/** 用户最近一次 /start 的来源码（用于非 start 消息的按钮深链保持同一归因）。 */
export async function lastSrc(uid: number): Promise<string> {
  try {
    const raw = await readFile(STARTS_LOG, "utf-8");
    const lines = raw.split("\n");
    for (let i = lines.length - 1; i >= 0; i--) {
      const l = lines[i];
      if (!l || !l.includes(`"uid":${uid},`)) continue;
      const rec = JSON.parse(l) as StartRec;
      return normalizeSrc(rec.src);
    }
  } catch {
    /* fall through */
  }
  return DEFAULT_SRC;
}

// ── 安装 / 身份 ───────────────────────────────────────────────────────

export const CHATX_ALLOWED_UPDATES = ["message", "edited_message", "callback_query", "channel_post", "my_chat_member", "chat_join_request"];

/** 配置当前 bot（默认内置 bot；控制台登记的 bot 在 withBot 里调用并传入自己的 webhook 地址与 secret）。 */
export async function setupChatxBot(opts?: { skipWebhook?: boolean; webhookUrl?: string; secret?: string; skipProfile?: boolean }) {
  const bot = currentBot();
  if (!bot) return { ok: false, error: "no token" };
  const token = bot.token;
  const secret = opts?.secret || process.env.CHATX_WEBHOOK_SECRET || "chatx-wh-" + token.slice(-8);
  const webhookUrl = opts?.webhookUrl || `${SITE_URL}/api/telegram/chatx/webhook`;

  const steps: { step: string; ok: boolean; error?: string }[] = [];
  const run = async (step: string, method: string, body: Record<string, unknown>) => {
    const res = await tgCall(method, body);
    const desc = String(res?.description || "");
    const ok = Boolean(res?.ok) || /is not modified/i.test(desc);
    steps.push({ step, ok, error: ok ? undefined : desc || "failed" });
    return res;
  };

  if (!opts?.skipWebhook) {
    await run("setWebhook", "setWebhook", {
      url: webhookUrl,
      secret_token: secret,
      allowed_updates: CHATX_ALLOWED_UPDATES,
      drop_pending_updates: false,
    });
  }

  await run("setMyCommands", "setMyCommands", {
    commands: [
      { command: "start", description: "开始 / Start" },
      { command: "download", description: "下载桌面版 / Download" },
      { command: "features", description: "能做什么 / Features" },
      { command: "tutorials", description: "视频教程 / Tutorials" },
      { command: "support", description: "人工客服 / Human support" },
      { command: "bug", description: "报障：发机器码 / 回执号定位问题 / Report a problem" },
      { command: "news", description: "今日早报（AI 资讯 + ChatX 新鲜事）/ Daily digest" },
      { command: "voice", description: "语音回复 开/关 / Voice replies on/off" },
      { command: "persona", description: "切换人设：小界 / 恋爱陪聊 / 销售跟进 / Switch persona" },
      { command: "voices", description: "换声音 / 克隆我的声音 / Voices & clone my voice" },
      { command: "stop", description: "关闭每日早报 / Unsubscribe" },
    ],
  });

  if (opts?.skipProfile) return { ok: steps.every((x) => x.ok), secretSet: Boolean(secret), webhookUrl: opts?.skipWebhook ? "skipped" : webhookUrl, steps };
  await run("setMyName(zh)", "setMyName", { name: "智聊 ChatX · AI 全自动聊天" });
  await run("setMyName(en)", "setMyName", { name: "ChatX · AI auto-chat", language_code: "en" });
  await run("setMyShortDescription(zh)", "setMyShortDescription", {
    short_description: "AI 全自动聊天：客户消息 AI 按你的话术自动回复、自动成交，外语自动互译。免费下载，无需 API Key。",
  });
  await run("setMyShortDescription(en)", "setMyShortDescription", {
    short_description: "AI auto-chat: replies to your customers in your voice, follows up and closes 24/7, live translation. Free download.",
    language_code: "en",
  });
  await run("setMyDescription(zh)", "setMyDescription", {
    description:
      "智聊 ChatX —— AI 全自动聊天。客户发来消息，AI 按你的话术自动回复、自动跟进、自动成交，外语客户自动互译。\n\n" +
      `📱 已支持 ${CHATX_PLATFORM_COUNT} 个平台：${platformsInline("zh")}\n\n直接发消息体验 AI 自动回复，或点 /start 获取下载链接。`,
  });
  await run("setMyDescription(en)", "setMyDescription", {
    description:
      "ChatX — AI auto-chat. When customers message you, AI replies in your voice, follows up and closes 24/7, with live translation.\n\n" +
      `📱 ${CHATX_PLATFORM_COUNT} platforms supported: ${platformsInline("en")}\n\nSend any message to try the AI, or tap /start for the download link.`,
    language_code: "en",
  });
  await run("setChatMenuButton", "setChatMenuButton", {
    menu_button: { type: "web_app", text: "智聊小程序", web_app: { url: miniAppLink() } },
  });

  const me = await tgCall("getMe", {});
  return { ok: steps.every((s) => s.ok), secretSet: Boolean(secret), webhookUrl: opts?.skipWebhook ? "skipped" : webhookUrl, steps, me: me?.result };
}
