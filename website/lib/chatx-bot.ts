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
 *     → 三按钮：下载桌面版（官网 /download/chatx?src=<src>&utm_*）/ 云端试用 / 交流群
 *     → chatx_download_click {src}                      （ChatxDownloadSection，前端埋点）
 *     → chatx_download_redirect {src}                   （/dl 分流入口服务端落账，无 JS 也算）
 *
 * 与主 bot 共用 token 以外的所有基础设施（tg-events / data-dir / site 常量）；token、webhook
 * secret、handle 用独立 env（CHATX_BOT_TOKEN / CHATX_WEBHOOK_SECRET / NEXT_PUBLIC_CHATX_BOT_HANDLE）。
 */
import { appendFile, mkdir, readFile } from "fs/promises";
import path from "path";
import type { BotLang } from "./bot-knowledge";
import { DATA_DIR } from "./data-dir";
import { trackTg } from "./tg-events";
import { CONTACT_URL, GROUP_URL, SITE_URL } from "./site";

export const CHATX_BOT_HANDLE = process.env.NEXT_PUBLIC_CHATX_BOT_HANDLE || "ctx2026_bot";
export const CHATX_BOT_URL = `https://t.me/${CHATX_BOT_HANDLE}`;

/** 云端试用入口：未配置时回落到官网智聊产品页（含来源 UTM）。 */
const CLOUD_TRIAL_URL = process.env.CHATX_CLOUD_TRIAL_URL || "";

const STARTS_LOG = process.env.CHATX_BOT_STARTS_LOG || path.join(DATA_DIR, "chatx_bot_starts.jsonl");

export type TgFrom = { id: number; username?: string; first_name?: string; language_code?: string };

type InlineBtn = { text: string; url: string } | { text: string; callback_data: string };

const API = (method: string) => `https://api.telegram.org/bot${process.env.CHATX_BOT_TOKEN}/${method}`;

export function chatxBotConfigured(): boolean {
  return Boolean(process.env.CHATX_BOT_TOKEN);
}

export async function tgCall(method: string, body: Record<string, unknown>) {
  if (!process.env.CHATX_BOT_TOKEN) return null;
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

export async function sendText(chatId: number | string, text: string, keyboard?: InlineBtn[][]) {
  return tgCall("sendMessage", {
    chat_id: chatId,
    text,
    parse_mode: "HTML",
    disable_web_page_preview: true,
    reply_markup: keyboard ? { inline_keyboard: keyboard } : undefined,
  });
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

/** 从 "/start ad_biz_a01" 或 "/start@ChatX_bot ad_biz_a01" 抽出来源码。 */
export function parseStartSrc(text: string): string {
  const m = text.trim().match(/^\/start(?:@\w+)?(?:\s+(\S+))?/i);
  return normalizeSrc(m?.[1]);
}

interface StartRec {
  t: string;
  uid: number;
  src: string;
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
export async function recordStart(from: TgFrom, src: string, lang: BotLang): Promise<{ first: boolean }> {
  const first = !(await seenBefore(from.id));
  const rec: StartRec = { t: new Date().toISOString(), uid: from.id, src, username: from.username, lang };
  try {
    await mkdir(path.dirname(STARTS_LOG), { recursive: true });
    await appendFile(STARTS_LOG, JSON.stringify(rec) + "\n");
  } catch {
    /* never fail bot flow over bookkeeping */
  }
  await trackTg("chatx_bot_start", { src, uid: from.id, first, lang });
  return { first };
}

// ── 深链 ──────────────────────────────────────────────────────────────

/** 官网下载页深链：utm_* 给会话归因（attribution.ts），src 给下载点击/分流入口直接落账。 */
export function downloadLink(src: string, lang: BotLang): string {
  const u = new URL(lang === "zh" ? "/download/chatx" : "/en/download/chatx", SITE_URL);
  u.searchParams.set("utm_source", "telegram");
  u.searchParams.set("utm_medium", "chatx_bot");
  u.searchParams.set("utm_campaign", src);
  u.searchParams.set("src", src);
  return u.toString();
}

export function cloudTrialLink(src: string, lang: BotLang): string {
  const base = CLOUD_TRIAL_URL || new URL(lang === "zh" ? "/growth" : "/en/growth", SITE_URL).toString();
  try {
    const u = new URL(base);
    u.searchParams.set("utm_source", "telegram");
    u.searchParams.set("utm_medium", "chatx_bot");
    u.searchParams.set("utm_campaign", src);
    u.searchParams.set("src", src);
    return u.toString();
  } catch {
    return base;
  }
}

// ── 文案 / 键盘 ──────────────────────────────────────────────────────

export function mainKeyboard(src: string, lang: BotLang): InlineBtn[][] {
  const zh = lang === "zh";
  return [
    [{ text: zh ? "📥 下载桌面版（Windows · 免费）" : "📥 Download desktop (Windows · free)", url: downloadLink(src, lang) }],
    [{ text: zh ? "☁️ 云端试用（免安装）" : "☁️ Cloud trial (no install)", url: cloudTrialLink(src, lang) }],
    [{ text: zh ? "💬 加入交流群" : "💬 Join the community", url: GROUP_URL }],
    [
      { text: zh ? "❓ 能做什么" : "❓ What it does", callback_data: "cx_features" },
      { text: zh ? "👤 人工客服" : "👤 Human support", url: CONTACT_URL },
    ],
  ];
}

export function welcomeText(lang: BotLang, first: boolean): string {
  if (lang === "zh") {
    return (
      (first ? "👋 欢迎来到 <b>智聊 ChatX</b>！\n\n" : "👋 欢迎回来！\n\n") +
      "把 Telegram / WhatsApp / LINE / Messenger 的私信收进<b>一个收件箱</b>，AI 自动拟稿、自动回复、实时互译，" +
      "24 小时不漏一条客户消息。\n\n" +
      "✅ 免费下载试用 · 无需 API Key · 数据保存在本机\n\n" +
      "👇 选一个开始："
    );
  }
  return (
    (first ? "👋 Welcome to <b>ChatX</b>!\n\n" : "👋 Welcome back!\n\n") +
    "One inbox for Telegram / WhatsApp / LINE / Messenger DMs, with AI drafting, auto-reply and live translation — " +
    "never miss a customer message.\n\n" +
    "✅ Free to download · no API key · data stays on your PC\n\n" +
    "👇 Pick one to get started:"
  );
}

export function featuresText(lang: BotLang): string {
  if (lang === "zh") {
    return (
      "<b>智聊 ChatX 能做什么</b>\n\n" +
      "📨 <b>统一收件箱</b>：多账号、多平台私信一屏处理\n" +
      "🤖 <b>AI 回复</b>：仅拟稿 / 人审放行 / 全自动 三档可切\n" +
      "🌐 <b>实时互译</b>：中英日韩等多语，收发双向翻译\n" +
      "📚 <b>知识库 + 人设</b>：按你的产品资料和话术风格回答\n" +
      "🎙 <b>语音消息</b>：语音转写、克隆音色回复\n" +
      "🔒 <b>本地部署</b>：数据留在自己电脑，免显卡\n\n" +
      "适合：Telegram 引流/私域团队、跨境电商客服、社群运营、海外招聘。"
    );
  }
  return (
    "<b>What ChatX does</b>\n\n" +
    "📨 <b>Unified inbox</b>: multi-account, multi-platform DMs on one screen\n" +
    "🤖 <b>AI replies</b>: draft-only / human-approved / fully automatic\n" +
    "🌐 <b>Live translation</b>: two-way, many languages\n" +
    "📚 <b>Knowledge base + persona</b>: answers in your voice, from your docs\n" +
    "🎙 <b>Voice</b>: transcription and cloned-voice replies\n" +
    "🔒 <b>Local-first</b>: data stays on your PC, no GPU\n\n" +
    "For Telegram growth teams, cross-border e-commerce support, community managers and overseas recruiters."
  );
}

// ── 处理器 ────────────────────────────────────────────────────────────

/** 私聊 /start（含广告深链）：记录来源 → 欢迎语 + 三按钮。 */
export async function handleStart(chatId: number, from: TgFrom, text: string, lang: BotLang) {
  const src = parseStartSrc(text);
  const { first } = await recordStart(from, src, lang);
  await sendText(chatId, welcomeText(lang, first), mainKeyboard(src, lang));
}

/** 其他命令 / 自由文本：一律回菜单（广告落地 bot 不做开放问答，避免审核风险与额度消耗）。 */
export async function handleOther(chatId: number, from: TgFrom, text: string, lang: BotLang) {
  const src = await lastSrc(from.id);
  if (/^\/(download|dl)\b/i.test(text)) {
    await trackTg("chatx_bot_cmd", { cmd: "download", src });
    await sendText(
      chatId,
      lang === "zh" ? "📥 点下面按钮下载 Windows 安装包（约 450 MB，装完即用）：" : "📥 Tap below to download the Windows installer (~450 MB):",
      [[{ text: lang === "zh" ? "📥 下载桌面版" : "📥 Download desktop", url: downloadLink(src, lang) }]]
    );
    return;
  }
  if (/^\/(features|help)\b/i.test(text)) {
    await trackTg("chatx_bot_cmd", { cmd: "features", src });
    await sendText(chatId, featuresText(lang), mainKeyboard(src, lang));
    return;
  }
  await sendText(chatId, welcomeText(lang, false), mainKeyboard(src, lang));
}

export async function handleCallback(chatId: number, cqId: string, data: string, from: TgFrom, lang: BotLang) {
  await answerCallback(cqId);
  const src = await lastSrc(from.id);
  if (data === "cx_features") {
    await trackTg("chatx_bot_cmd", { cmd: "features", src });
    await sendText(chatId, featuresText(lang), mainKeyboard(src, lang));
  }
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

export async function setupChatxBot(opts?: { skipWebhook?: boolean }) {
  if (!process.env.CHATX_BOT_TOKEN) return { ok: false, error: "no token" };
  const token = process.env.CHATX_BOT_TOKEN;
  const secret = process.env.CHATX_WEBHOOK_SECRET || "chatx-wh-" + token.slice(-8);
  const webhookUrl = `${SITE_URL}/api/telegram/chatx/webhook`;

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
      allowed_updates: ["message", "callback_query"],
      drop_pending_updates: true,
    });
  }

  await run("setMyCommands", "setMyCommands", {
    commands: [
      { command: "start", description: "开始 / Start" },
      { command: "download", description: "下载桌面版 / Download" },
      { command: "features", description: "能做什么 / Features" },
    ],
  });

  await run("setMyName(zh)", "setMyName", { name: "智聊 ChatX" });
  await run("setMyName(en)", "setMyName", { name: "ChatX", language_code: "en" });
  await run("setMyShortDescription(zh)", "setMyShortDescription", {
    short_description: "多平台 AI 聊天工作台：统一收件箱 · AI 自动回复 · 实时互译。免费下载，无需 API Key。",
  });
  await run("setMyShortDescription(en)", "setMyShortDescription", {
    short_description: "Omni-channel AI chat workspace: unified inbox, AI auto-reply, live translation. Free download.",
    language_code: "en",
  });
  await run("setMyDescription(zh)", "setMyDescription", {
    description:
      "智聊 ChatX —— 把 Telegram / WhatsApp / LINE / Messenger 的私信收进一个收件箱，AI 自动拟稿、自动回复、实时互译。\n\n" +
      "✅ 免费下载试用 · 无需 API Key · 数据保存在本机\n\n点 /start 获取下载链接。",
  });
  await run("setMyDescription(en)", "setMyDescription", {
    description:
      "ChatX — one inbox for Telegram / WhatsApp / LINE / Messenger DMs with AI drafting, auto-reply and live translation.\n\n" +
      "✅ Free download · no API key · data stays on your PC\n\nTap /start to get the download link.",
    language_code: "en",
  });

  const me = await tgCall("getMe", {});
  return { ok: steps.every((s) => s.ok), secret, webhookUrl: opts?.skipWebhook ? "skipped" : webhookUrl, steps, me: me?.result };
}
