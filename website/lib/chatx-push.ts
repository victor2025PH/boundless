import { appendFile, mkdir, readFile } from "fs/promises";
import path from "path";
import type { BotLang } from "./bot-knowledge";
import { askDeepSeek } from "./deepseek";
import { DATA_DIR } from "./data-dir";
import { SITE_URL } from "./site";
import { trackTg } from "./tg-events";
import { CHATX_RELEASE_NOTES } from "./chatxReleaseNotes";
import { CHATX_TUTORIALS, CHATX_TUTORIALS_PATH } from "./chatx-tutorials";
import { listFeed } from "./feed-store";
import { allPrefs, setPrefs } from "./chatx-prefs";
import { pushOpener } from "./chatx-persona";
import { downloadLink, recentStarts, sendText, tutorialsLink, chatxBotConfigured } from "./chatx-bot";
import { TZ_MS } from "./chatx-report";

/**
 * @ChatX_bot 每日主动推送「小界早报」：AI 圈资讯 + ChatX 新鲜事 / 今日小技巧，并邀请用户直接回话。
 * 由 /api/admin/schedule/run 的 cron 驱动（与 24h 追发同一入口）。
 *
 * 内容只来自真实来源，不让 AI 编新闻：
 *   ① AI 资讯：CHATX_NEWS_FEEDS（逗号分隔的 RSS/Atom 地址）48h 内条目 → DeepSeek 只做「挑 3 条 + 各一句概括」，
 *      产出必须引用条目序号，对不上的丢弃；DeepSeek 不可用就直接列原标题 + 链接；没配 feed 就没有这一段。
 *   ② ChatX 新鲜事：最新一版更新日志（chatx-release-notes.json，按版本只推一次）> 最新视频动态（feed-store，只推一次）
 *      > 教程小技巧（13 集按天轮播，永远有内容）。
 * 受众：CHATX_PUSH_WINDOW_DAYS（默认 30）内 start 过、未 /stop、最近 24h 没 start（刚进来的人不叠消息）、当天没推过。
 * 频率：每天 1 条，CHATX_PUSH_HOUR（默认 10 点）起 3 小时窗口内分批发完（每次 cron 最多 CHATX_PUSH_BATCH 人），账本先占位；
 * 被用户拉黑（403）自动置 push=false 不再打扰。
 */

const PUSH_HOUR = Number(process.env.CHATX_PUSH_HOUR ?? 10);
const PUSH_WINDOW_H = 3;
const WINDOW_DAYS = Number(process.env.CHATX_PUSH_WINDOW_DAYS ?? 30);
const BATCH = Number(process.env.CHATX_PUSH_BATCH ?? 500);
const SEND_GAP_MS = 40;
const PUSH_LOG = process.env.CHATX_BOT_PUSH_LOG || path.join(DATA_DIR, "chatx_bot_push.jsonl");
const FEEDS = (process.env.CHATX_NEWS_FEEDS ?? "").split(",").map((s) => s.trim()).filter(Boolean);

export type NewsItem = { title: string; link: string; t: number; summary?: string };
export type ProductItem =
  | { kind: "release"; version: string; title: string; points: string[]; url: string }
  | { kind: "video"; id: string; title: string; desc: string; url: string }
  | { kind: "tip"; ep: string; title: string; desc: string; url: string };
export type PushContent = { day: string; news: NewsItem[]; product: ProductItem };

const localDay = (ms: number) => new Date(ms + TZ_MS).toISOString().slice(0, 10);

// ── 内容 ─────────────────────────────────────────────────────────────

/** 极简 RSS 2.0 / Atom 解析（无依赖）：只取 title / link / 时间。 */
export function parseFeed(xml: string): NewsItem[] {
  const out: NewsItem[] = [];
  const clean = (s: string) =>
    s
      .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1")
      .replace(/<[^>]+>/g, "")
      .replace(/&amp;/g, "&")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&quot;/g, '"')
      .replace(/&#39;|&apos;/g, "'")
      .trim();
  const pick = (block: string, tag: string) => {
    const m = block.match(new RegExp(`<${tag}(?:\\s[^>]*)?>([\\s\\S]*?)</${tag}>`, "i"));
    return m ? clean(m[1]) : "";
  };
  const blocks = xml.match(/<(?:item|entry)\b[\s\S]*?<\/(?:item|entry)>/gi) ?? [];
  for (const b of blocks) {
    const title = pick(b, "title");
    let link = pick(b, "link");
    if (!link) {
      const m = b.match(/<link\b[^>]*href=["']([^"']+)["']/i);
      link = m ? m[1] : "";
    }
    const when = pick(b, "pubDate") || pick(b, "published") || pick(b, "updated") || pick(b, "dc:date");
    const t = Date.parse(when);
    if (!title || !/^https?:\/\//.test(link)) continue;
    out.push({ title: title.slice(0, 140), link, t: Number.isFinite(t) ? t : 0 });
  }
  return out;
}

async function fetchNews(now: number, feeds = FEEDS): Promise<NewsItem[]> {
  if (!feeds.length) return [];
  const since = now - 48 * 3600_000;
  const all: NewsItem[] = [];
  await Promise.all(
    feeds.map(async (url) => {
      try {
        const ctl = new AbortController();
        const timer = setTimeout(() => ctl.abort(), 8000);
        const res = await fetch(url, { signal: ctl.signal, headers: { "user-agent": "ChatXBot/1.0 (+https://bd2026.cc)" } });
        clearTimeout(timer);
        if (!res.ok) return;
        for (const it of parseFeed(await res.text())) if (it.t >= since) all.push(it);
      } catch {
        /* 单个 feed 失败不影响其他 */
      }
    })
  );
  const seen = new Set<string>();
  return all
    .sort((a, b) => b.t - a.t)
    .filter((x) => (seen.has(x.link) ? false : (seen.add(x.link), true)))
    .slice(0, 12);
}

/** DeepSeek 只挑条 + 概括，行格式 `序号|概括`；序号对不上的丢弃，保证每句都对应一条真实新闻。 */
export async function summarizeNews(items: NewsItem[], lang: BotLang): Promise<NewsItem[]> {
  if (!items.length) return [];
  const list = items.map((x, i) => `${i + 1}. ${x.title}`).join("\n");
  const q =
    lang === "zh"
      ? `下面是过去两天的 AI 新闻标题。挑出最多 3 条与「AI 自动回复 / 智能客服 / 销售转化 / 大模型进展」最相关的，每条用一句不超过 40 字的中文概括它对做生意的人意味着什么。只能根据标题本身，不得补充标题里没有的事实。每行输出格式严格为「序号|概括」，不要其他文字。\n\n${list}`
      : `Below are AI news headlines from the last two days. Pick at most 3 most relevant to "AI auto-reply / customer service / sales / LLM progress" and write one sentence (≤25 words) on what each means for a business owner. Use only the headline itself, add no facts. Output strictly one line per item as "index|summary", nothing else.\n\n${list}`;
  const raw = await askDeepSeek(q, lang, [], 12000, "You are a concise news editor. Never invent facts.").catch(() => null);
  const picked: NewsItem[] = [];
  if (raw) {
    for (const line of raw.split("\n")) {
      const m = line.match(/^\s*(\d+)\s*[|｜.、:：]\s*(.+?)\s*$/);
      if (!m) continue;
      const it = items[Number(m[1]) - 1];
      if (!it || picked.includes(it)) continue;
      picked.push({ ...it, summary: m[2].slice(0, 120) });
      if (picked.length >= 3) break;
    }
  }
  return picked.length ? picked : items.slice(0, 3);
}

/** 往日（不含当天）已推过的产品位 key：同一天里中英文、分批都拿同一条。 */
async function pushedProductKeys(day: string): Promise<Set<string>> {
  const out = new Set<string>();
  try {
    for (const line of (await readFile(PUSH_LOG, "utf8")).split("\n")) {
      if (!line.trim()) continue;
      try {
        const r = JSON.parse(line) as { product?: string; day?: string };
        if (r.product && r.day !== day) out.add(r.product);
      } catch {
        /* skip */
      }
    }
  } catch {
    /* first run */
  }
  return out;
}

export const productKey = (p: ProductItem) => (p.kind === "release" ? `release:${p.version}` : p.kind === "video" ? `video:${p.id}` : `tip:${p.ep}`);

/** 新鲜事优先级：没推过的最新版本 > 没推过的最新视频 > 按天轮播的教程小技巧。 */
export async function pickProduct(day: string, lang: BotLang, already: Set<string>): Promise<ProductItem> {
  const rel = CHATX_RELEASE_NOTES[0];
  if (rel && !already.has(`release:${rel.version}`)) {
    const base = lang === "zh" ? "/download/chatx/releases" : "/en/download/chatx/releases";
    const cut = (s: string, n: number) => (s.length > n ? s.slice(0, n - 1).replace(/[；;，,、\s]+$/, "") + "…" : s);
    return {
      kind: "release",
      version: rel.version,
      title: cut(rel.title[lang].split(/[；;]/)[0], 60),
      points: rel.highlights[lang].slice(0, 2).map((s) => cut(s, 80)),
      url: `${SITE_URL}${base}?utm_source=telegram&utm_medium=chatx_push&utm_campaign=release-${rel.version}`,
    };
  }
  const vids = await listFeed(1).catch(() => []);
  const v = vids[0];
  if (v && !already.has(`video:${v.id}`)) {
    return {
      kind: "video",
      id: v.id,
      title: v.title[lang],
      desc: v.desc[lang],
      url: `${SITE_URL}${lang === "zh" ? "" : "/en"}/videos?utm_source=telegram&utm_medium=chatx_push&utm_campaign=video-${v.id}`,
    };
  }
  const eps = CHATX_TUTORIALS;
  const idx = Math.floor(Date.parse(day) / 86_400_000) % eps.length;
  const e = eps[idx];
  const tut = new URL(lang === "zh" ? CHATX_TUTORIALS_PATH : `/en${CHATX_TUTORIALS_PATH}`, SITE_URL);
  tut.searchParams.set("ep", e.id);
  tut.searchParams.set("utm_source", "telegram");
  tut.searchParams.set("utm_medium", "chatx_push");
  tut.searchParams.set("utm_campaign", `tip-${e.id.toLowerCase()}`);
  return { kind: "tip", ep: e.id, title: e.title[lang], desc: e.desc[lang], url: tut.toString() };
}

const contentCache = new Map<string, Promise<PushContent>>();

/** 当天内容按语言各算一次（进程内缓存），所有人拿同一份。 */
export function dailyContent(day: string, lang: BotLang, now = Date.now()): Promise<PushContent> {
  const k = `${day}:${lang}`;
  let p = contentCache.get(k);
  if (!p) {
    p = (async () => {
      const [raw, already] = await Promise.all([fetchNews(now), pushedProductKeys(day)]);
      const news = await summarizeNews(raw, lang);
      const product = await pickProduct(day, lang, already);
      return { day, news, product };
    })();
    contentCache.set(k, p);
    p.catch(() => contentCache.delete(k));
  }
  return p;
}

// ── 文案 ─────────────────────────────────────────────────────────────

const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

/** opener 由人设决定（小界 / 恋爱陪聊各自的一句开场），缺省小界。 */
export function formatPush(c: PushContent, lang: BotLang, opener?: string): string {
  const zh = lang === "zh";
  const md = c.day.slice(5).replace("-", "/");
  const lines: string[] = [zh ? `🗞 <b>小界早报 · ${md}</b>` : `🗞 <b>ChatX Daily · ${md}</b>`];
  if (opener) lines.push(esc(opener));
  if (c.news.length) {
    lines.push("", zh ? "🤖 <b>AI 圈今天</b>" : "🤖 <b>AI today</b>");
    for (const n of c.news) lines.push(`• <a href="${n.link}">${esc(n.title)}</a>${n.summary ? `\n  ${esc(n.summary)}` : ""}`);
  }
  const p = c.product;
  lines.push("");
  if (p.kind === "release") {
    lines.push(zh ? `✨ <b>ChatX 新版 ${p.version}</b>` : `✨ <b>ChatX ${p.version} is out</b>`, esc(p.title));
    for (const pt of p.points) lines.push(`• ${esc(pt)}`);
    lines.push(`<a href="${p.url}">${zh ? "查看全部更新 →" : "Full release notes →"}</a>`);
  } else if (p.kind === "video") {
    lines.push(zh ? "🎬 <b>ChatX 新动态</b>" : "🎬 <b>New from ChatX</b>", `<a href="${p.url}">${esc(p.title)}</a>`, esc(p.desc));
  } else {
    lines.push(zh ? `💡 <b>今日小技巧 · ${esc(p.title)}</b>` : `💡 <b>Tip of the day · ${esc(p.title)}</b>`, esc(p.desc), `<a href="${p.url}">${zh ? "看这一集 →" : "Watch this episode →"}</a>`);
  }
  lines.push(
    "",
    zh ? "想聊聊哪一条，或者手头有客户消息不知道怎么回？直接发给我 👇" : "Want to talk about any of these, or stuck on a customer message? Just send it to me 👇",
    zh ? "<i>不想收早报发 /stop</i>" : "<i>Send /stop to unsubscribe</i>"
  );
  return lines.join("\n");
}

export function pushKeyboard(src: string, lang: BotLang, uid: number) {
  const zh = lang === "zh";
  return [
    [
      { text: zh ? "📖 看教程" : "📖 Tutorials", url: tutorialsLink(src, lang, "chatx_push") },
      { text: zh ? "📥 下载桌面版" : "📥 Download", url: downloadLink(src, lang, "chatx_push", uid) },
    ],
    [{ text: zh ? "🔕 不再推送" : "🔕 Unsubscribe", callback_data: "cx_push_off" }],
  ];
}

// ── 发送 ─────────────────────────────────────────────────────────────

async function pushedToday(day: string): Promise<Set<number>> {
  const out = new Set<number>();
  try {
    for (const line of (await readFile(PUSH_LOG, "utf8")).split("\n")) {
      if (!line.trim()) continue;
      try {
        const r = JSON.parse(line) as { day?: string; uid?: number };
        if (r.day === day && typeof r.uid === "number") out.add(r.uid);
      } catch {
        /* skip */
      }
    }
  } catch {
    /* first run */
  }
  return out;
}

async function ledger(rec: Record<string, unknown>) {
  await mkdir(path.dirname(PUSH_LOG), { recursive: true });
  await appendFile(PUSH_LOG, JSON.stringify(rec) + "\n");
}

const BLOCKED_RE = /blocked by the user|user is deactivated|chat not found|bot can't initiate/i;

/** 给一个人发早报（每日批量与 /news 即时共用）。返回是否成功；被拉黑时自动退订。 */
export async function sendPushTo(uid: number, src: string, lang: BotLang, day: string, opener?: string, via: "daily" | "news" = "daily"): Promise<boolean> {
  const c = await dailyContent(day, lang);
  const key = productKey(c.product);
  try {
    await ledger({ t: new Date().toISOString(), day, uid, src, product: key, via });
  } catch {
    return false; // 账本写不进就不发，宁漏不重
  }
  const r = await sendText(uid, formatPush(c, lang, opener), pushKeyboard(src, lang, uid));
  const ok = Boolean(r?.ok);
  if (!ok && BLOCKED_RE.test(String(r?.description ?? ""))) await setPrefs(uid, { push: false }).catch(() => {});
  await trackTg("chatx_bot_push", { src, uid, ok, kind: c.product.kind, news: c.news.length, via, lang });
  return ok;
}

let running = false;

export async function runDailyPush(now = Date.now(), force = false): Promise<{ day: string; due: number; sent: number; skipped: number; reason?: string }> {
  const day = localDay(now);
  const zero = { day, due: 0, sent: 0, skipped: 0 };
  if (!chatxBotConfigured()) return { ...zero, reason: "not_configured" };
  const hour = new Date(now + TZ_MS).getUTCHours();
  if (!force && (hour < PUSH_HOUR || hour >= PUSH_HOUR + PUSH_WINDOW_H)) return { ...zero, reason: "not_hour" };
  if (running) return { ...zero, reason: "running" };
  running = true;
  try {
    const [starts, prefs, done] = await Promise.all([recentStarts(now - WINDOW_DAYS * 86_400_000), allPrefs(), pushedToday(day)]);
    const due: { uid: number; src: string; lang: BotLang; opener?: string }[] = [];
    let skipped = 0;
    for (const [uid, s] of starts) {
      if (done.has(uid)) continue;
      const p = prefs.get(uid);
      if (p?.push === false || now - s.lastT < 24 * 3600_000) {
        skipped++;
        continue;
      }
      const lang: BotLang = s.lang === "en" ? "en" : "zh";
      due.push({ uid, src: s.src, lang, opener: pushOpener(p?.persona, lang) });
    }
    let sent = 0;
    for (const u of due.slice(0, BATCH)) {
      if (await sendPushTo(u.uid, u.src, u.lang, day, u.opener)) sent++;
      await new Promise((r) => setTimeout(r, SEND_GAP_MS));
    }
    return { day, due: due.length, sent, skipped };
  } finally {
    running = false;
  }
}

/** /stop：退订早报（对话与其他功能不受影响）。 */
export async function optOut(uid: number) {
  await setPrefs(uid, { push: false });
}

/** /news：重新订阅并立刻发今天这份。 */
export async function optInAndSend(uid: number, src: string, lang: BotLang, now = Date.now()): Promise<boolean> {
  const p = await setPrefs(uid, { push: true });
  return sendPushTo(uid, src, lang, localDay(now), pushOpener(p.persona, lang), "news");
}
