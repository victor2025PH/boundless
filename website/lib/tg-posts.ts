/**
 * 频道 / 社群定时发帖（基础版）：控制台排期 → 巡检到点由对应 bot 发出。
 * 单文件 JSON（DATA_DIR/tg_posts.json），原子写 + 串行化。巡检挂在 order-sla 的 10 分钟 cron 上，精度约 10 分钟。
 * 幂等：发送前先把状态改成 sending 落盘，进程中途崩溃也不会重发（宁漏不重）。
 */
import { mkdir, readFile, rename, writeFile } from "fs/promises";
import path from "path";
import { DATA_DIR } from "./data-dir";
import { SITE_URL } from "./site";
import { tgCall, tgUpload } from "./chatx-bot";
import { isMediaRef, pruneUnusedMedia, readMedia } from "./tg-post-media";
import { withBot } from "./tg-bot-context";
import { botCtxById, findChat, loadHub } from "./tg-hub-store";

const FILE = process.env.TG_POSTS_FILE || path.join(DATA_DIR, "tg_posts.json");
const KEEP = 500;

export type PostRepeat = "daily" | "weekly";
const REPEAT_MS: Record<PostRepeat, number> = { daily: 86400_000, weekly: 7 * 86400_000 };

export type PostStatus = "scheduled" | "sending" | "sent" | "failed" | "canceled";

export type ScheduledPost = {
  id: number;
  botId: string;
  chatId: string;
  text: string;
  button?: { text: string; url: string };
  /** 配图：https 图片地址，或控制台上传的 `media:<文件名>`；有图时内容作为图片说明，最多 1024 字 */
  photo?: string;
  /** 重复发：发出（或失败）后自动排下一期；取消待发的那一期就停止 */
  repeat?: PostRepeat;
  /** 重复系列：同一条重复帖各期共用首期的 id，用来按系列合计；首期本身不填 */
  seriesId?: number;
  sendAt: string;
  status: PostStatus;
  createdBy: string;
  createdAt: string;
  sentAt?: string;
  msgId?: number;
  error?: string;
  /** 按钮点击次数（https 按钮经 /r/p/<id> 跳转计数） */
  clicks?: number;
};

type Store = { seq: number; posts: ScheduledPost[] };

export class PostError extends Error {}

async function load(): Promise<Store> {
  try {
    const raw = JSON.parse(await readFile(FILE, "utf-8")) as Partial<Store>;
    return { seq: Number(raw.seq) || 0, posts: Array.isArray(raw.posts) ? raw.posts : [] };
  } catch {
    return { seq: 0, posts: [] };
  }
}

let chain: Promise<unknown> = Promise.resolve();
function mutate<T>(fn: (s: Store) => T): Promise<T> {
  const run = chain.then(async () => {
    const s = await load();
    const out = fn(s);
    if (s.posts.length > KEEP) s.posts = s.posts.slice(-KEEP);
    await mkdir(path.dirname(FILE), { recursive: true });
    const tmp = `${FILE}.${process.pid}.tmp`;
    await writeFile(tmp, JSON.stringify(s), "utf-8");
    await rename(tmp, FILE);
    return out;
  });
  chain = run.catch(() => undefined);
  return run;
}

/** https 按钮换成站内跳转链接计点击；tg:// 按钮原样发（没法经网页跳转）。 */
export function trackedButtonUrl(p: Pick<ScheduledPost, "id" | "button">): string | undefined {
  if (!p.button) return undefined;
  return p.button.url.startsWith("https://") ? `${SITE_URL}/r/p/${p.id}` : p.button.url;
}

/** 系列号：首期 id（非重复帖就是自己）。 */
export function seriesOf(p: Pick<ScheduledPost, "id" | "seriesId">): number {
  return p.seriesId ?? p.id;
}

/** /start 载荷里的帖子后缀：`<src>__p<帖子 id>`；bot 端 parseStartPayload 拆开。 */
export const START_POST_SUFFIX_RE = /__p(\d{1,9})$/;

/**
 * 用户点按钮后真正跳去的链接。t.me 深链把帖子号拼进 start=（`ad_x__p12`），
 * bot /start 时就知道是哪一期帖子带来的人，再由 uid 串到下载；其他 https 链接原样跳。
 */
export function postTargetUrl(p: Pick<ScheduledPost, "id" | "button">): string | undefined {
  const raw = p.button?.url;
  if (!raw) return undefined;
  try {
    const u = new URL(raw);
    if (u.protocol !== "https:" || u.hostname !== "t.me" || !/^\/[A-Za-z0-9_]{4,32}\/?$/.test(u.pathname)) return raw;
    const start = u.searchParams.get("start") ?? "";
    if (!/^[A-Za-z0-9_-]{0,48}$/.test(start) || START_POST_SUFFIX_RE.test(start)) return raw;
    u.searchParams.set("start", `${start}__p${p.id}`);
    return u.toString();
  } catch {
    return raw;
  }
}

export type PostSeriesSummary = {
  seriesId: number;
  repeat: PostRepeat;
  botId: string;
  chatId: string;
  text: string;
  /** 期数（含待发那一期） */
  issues: number;
  sent: number;
  failed: number;
  /** 还有待发的一期 = 系列在跑 */
  active: boolean;
  firstAt: string;
  lastAt: string;
  clicks: number;
};

/** 每日 / 每周重复帖按系列合计（只含重复帖）；点击取各期 clicks 之和，进 bot / 下载人数由 chatx-report.buildPostFunnel(events, seriesKey) 给。 */
export function summarizeSeries(posts: ScheduledPost[]): PostSeriesSummary[] {
  const m = new Map<number, PostSeriesSummary>();
  for (const p of posts) {
    if (!p.repeat) continue;
    const id = seriesOf(p);
    const at = p.sentAt ?? p.sendAt;
    let s = m.get(id);
    if (!s) m.set(id, (s = { seriesId: id, repeat: p.repeat, botId: p.botId, chatId: p.chatId, text: p.text, issues: 0, sent: 0, failed: 0, active: false, firstAt: at, lastAt: at, clicks: 0 }));
    s.issues++;
    if (p.status === "sent") s.sent++;
    if (p.status === "failed") s.failed++;
    if (p.status === "scheduled" || p.status === "sending") s.active = true;
    if (at < s.firstAt) s.firstAt = at;
    if (at > s.lastAt) s.lastAt = at;
    s.clicks += p.clicks ?? 0;
  }
  return [...m.values()].sort((a, b) => (b.lastAt < a.lastAt ? -1 : 1));
}

/** 帖子 id → 系列 id 的查表函数（给 buildPostFunnel 用；不认识的 id 原样返回）。 */
export function seriesKeyFn(posts: ScheduledPost[]): (post: number) => number {
  const m = new Map<number, number>();
  for (const p of posts) m.set(p.id, seriesOf(p));
  return (post) => m.get(post) ?? post;
}

/** 按钮链接里的来源码：t.me 深链的 start= 或网页链接的 src=。 */
export function buttonSrc(url: string): string | undefined {
  try {
    const u = new URL(url);
    const v = u.searchParams.get("start") ?? u.searchParams.get("src") ?? "";
    return /^[A-Za-z0-9_-]{1,48}$/.test(v) ? v : undefined;
  } catch {
    return undefined;
  }
}

/** 记一次按钮点击，返回帖子（没有按钮 / 不存在返回 null）。 */
export function recordPostClick(id: number): Promise<ScheduledPost | null> {
  return mutate((s) => {
    const p = s.posts.find((x) => x.id === id);
    if (!p?.button) return null;
    p.clicks = (p.clicks ?? 0) + 1;
    return { ...p };
  });
}

export async function listPosts(): Promise<ScheduledPost[]> {
  return (await load()).posts;
}

export async function schedulePost(input: { botId: string; chatId: string; text: string; sendAt?: string; buttonText?: string; buttonUrl?: string; photo?: string; repeat?: string; createdBy: string }, now = Date.now()): Promise<ScheduledPost> {
  const text = input.text.trim();
  if (!text) throw new PostError("请填写内容");
  if (text.length > 4000) throw new PostError("内容最多 4000 字");
  const photo = (input.photo ?? "").trim() || undefined;
  if (photo) {
    if (!isMediaRef(photo) && (!/^https:\/\/\S+$/.test(photo) || photo.length > 500)) throw new PostError("图片地址要以 https:// 开头，或直接上传图片");
    if (text.length > 1024) throw new PostError("带图时内容最多 1024 字（Telegram 图片说明上限）");
  }
  const rp = (input.repeat ?? "").trim();
  if (rp && rp !== "daily" && rp !== "weekly") throw new PostError("重复只能是每天或每周");
  const repeat = rp ? (rp as PostRepeat) : undefined;
  const hub = await loadHub();
  const chat = findChat(hub, input.botId, input.chatId);
  if (!chat) throw new PostError("没有这个群 / 频道");
  if (chat.role === "support") throw new PostError("客服群不能定时发帖");
  if (!chat.enabled) throw new PostError("这个群 / 频道已停用，先启用");
  if (chat.botStatus !== "administrator") throw new PostError("bot 还不是这个群 / 频道的管理员");
  let sendAt = now;
  if (input.sendAt) {
    sendAt = Date.parse(input.sendAt);
    if (!Number.isFinite(sendAt)) throw new PostError("发送时间格式不对");
    if (sendAt > now + 60 * 86400_000) throw new PostError("最多提前 60 天排期");
    if (sendAt < now - 60_000) throw new PostError("发送时间已经过了");
  }
  let button: ScheduledPost["button"];
  const bt = (input.buttonText ?? "").trim();
  const bu = (input.buttonUrl ?? "").trim();
  if (bt || bu) {
    if (!bt || !bu) throw new PostError("按钮文字和链接要一起填");
    if (bt.length > 40) throw new PostError("按钮文字最多 40 字");
    if (!/^(https:\/\/|tg:\/\/)/.test(bu) || bu.length > 500) throw new PostError("按钮链接要以 https:// 或 tg:// 开头");
    button = { text: bt, url: bu };
  }
  return mutate((s) => {
    s.seq += 1;
    const p: ScheduledPost = { id: s.seq, botId: chat.botId, chatId: chat.chatId, text, button, photo, repeat, sendAt: new Date(sendAt).toISOString(), status: "scheduled", createdBy: input.createdBy, createdAt: new Date(now).toISOString() };
    s.posts.push(p);
    return { ...p };
  });
}

export async function cancelPost(id: number): Promise<ScheduledPost> {
  const r = await mutate((s) => {
    const p = s.posts.find((x) => x.id === id);
    if (!p || p.status !== "scheduled") return null;
    p.status = "canceled";
    return { ...p };
  });
  if (!r) throw new PostError("只能取消还没发出的帖子");
  return r;
}

/** 清理上传但已没有任何帖子（不论状态）引用的配图；挂在巡检上。待发 / 重复系列下一期都在帖子表里，不会被清。 */
export async function prunePostMedia(now = Date.now()): Promise<{ scanned: number; removed: number; kept: number }> {
  const refs = (await load()).posts.map((p) => p.photo).filter((v): v is string => !!v && isMediaRef(v));
  const r = await pruneUnusedMedia(refs, now);
  return { scanned: r.scanned, removed: r.removed.length, kept: r.kept };
}

export async function runScheduledPosts(now = Date.now()): Promise<{ due: number; sent: number; failed: number }> {
  const due = await mutate((s) => {
    const out: ScheduledPost[] = [];
    for (const p of s.posts) {
      if (p.status === "scheduled" && Date.parse(p.sendAt) <= now) {
        p.status = "sending";
        out.push({ ...p });
      }
    }
    return out;
  });
  let sent = 0;
  let failed = 0;
  for (const p of due) {
    let msgId: number | undefined;
    let error: string | undefined;
    const bot = await botCtxById(p.botId);
    if (!bot) error = "bot 不可用";
    else {
      const url = trackedButtonUrl(p);
      const reply_markup = p.button && url ? { inline_keyboard: [[{ text: p.button.text, url }]] } : undefined;
      const media = p.photo && isMediaRef(p.photo) ? await readMedia(p.photo) : null;
      if (p.photo && isMediaRef(p.photo) && !media) error = "配图文件找不到了";
      else {
        const r = await withBot(bot, () =>
          media
            ? tgUpload("sendPhoto", { chat_id: p.chatId, caption: p.text, reply_markup }, { field: "photo", name: media.name, data: media.data, type: media.type })
            : p.photo
              ? tgCall("sendPhoto", { chat_id: p.chatId, photo: p.photo, caption: p.text, reply_markup })
              : tgCall("sendMessage", { chat_id: p.chatId, text: p.text, reply_markup })
        );
        msgId = (r?.result as { message_id?: number } | undefined)?.message_id;
        if (!r?.ok) error = String(r?.description ?? "发送失败").slice(0, 200);
      }
    }
    await mutate((s) => {
      const x = s.posts.find((y) => y.id === p.id);
      if (!x) return;
      x.status = error ? "failed" : "sent";
      x.sentAt = new Date(now).toISOString();
      if (msgId) x.msgId = msgId;
      if (error) x.error = error;
      if (x.repeat) {
        const step = REPEAT_MS[x.repeat];
        let next = Date.parse(x.sendAt) + step;
        while (next <= now) next += step;
        s.seq += 1;
        s.posts.push({ id: s.seq, botId: x.botId, chatId: x.chatId, text: x.text, button: x.button, photo: x.photo, repeat: x.repeat, seriesId: seriesOf(x), sendAt: new Date(next).toISOString(), status: "scheduled", createdBy: x.createdBy, createdAt: new Date(now).toISOString() });
      }
    });
    if (error) failed++;
    else sent++;
  }
  return { due: due.length, sent, failed };
}
