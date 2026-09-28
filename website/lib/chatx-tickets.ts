/**
 * ChatX 客服工单：用户在 bot 里转人工 / 报障 → 在客服群开一个话题（每个工单一个），
 * 用户和客服的消息经 bot 双向转发。单文件 JSON（DATA_DIR/chatx_tickets.json），量级 = 每天几十单。
 */
import { mkdir, readFile, rename, writeFile } from "fs/promises";
import path from "path";
import { DATA_DIR } from "./data-dir";

const FILE = process.env.CHATX_TICKETS_FILE || path.join(DATA_DIR, "chatx_tickets.json");
const KEEP = 2000;

export type TicketStatus = "open" | "claimed" | "resolved";

export type Ticket = {
  id: number;
  botId: string;
  uid: number;
  chatId: number;
  name: string;
  username?: string;
  src: string;
  lang: string;
  kind: string;
  status: TicketStatus;
  fp?: string;
  diag?: string;
  note?: string;
  supportBotId: string;
  supportChatId: string;
  topicId?: number;
  /** bot 在客服群里为该工单发出的消息 id（非话题群时靠「回复」对上工单） */
  msgIds: number[];
  agent?: string;
  createdAt: string;
  updatedAt: string;
  firstReplyAt?: string;
  resolvedAt?: string;
  rating?: "y" | "n";
  /** 已发过「超时未首次响应」提醒（幂等标记） */
  slaAlertAt?: string;
  /** 已升级通知管理员（幂等标记） */
  slaEscalatedAt?: string;
};

type Store = { seq: number; tickets: Ticket[] };

async function load(): Promise<Store> {
  try {
    const raw = JSON.parse(await readFile(FILE, "utf-8")) as Partial<Store>;
    return { seq: Number(raw.seq) || 0, tickets: Array.isArray(raw.tickets) ? raw.tickets : [] };
  } catch {
    return { seq: 0, tickets: [] };
  }
}

let chain: Promise<unknown> = Promise.resolve();

function mutate<T>(fn: (s: Store) => T): Promise<T> {
  const run = chain.then(async () => {
    const s = await load();
    const out = fn(s);
    if (s.tickets.length > KEEP) s.tickets = s.tickets.slice(-KEEP);
    await mkdir(path.dirname(FILE), { recursive: true });
    const tmp = `${FILE}.${process.pid}.tmp`;
    await writeFile(tmp, JSON.stringify(s), "utf-8");
    await rename(tmp, FILE);
    return out;
  });
  chain = run.catch(() => undefined);
  return run;
}

export async function listTickets(): Promise<Ticket[]> {
  return (await load()).tickets;
}

export async function getTicket(id: number): Promise<Ticket | undefined> {
  return (await load()).tickets.find((t) => t.id === id);
}

export async function activeTicketFor(botId: string, uid: number): Promise<Ticket | undefined> {
  const all = (await load()).tickets;
  for (let i = all.length - 1; i >= 0; i--) {
    const t = all[i];
    if (t.botId === botId && t.uid === uid && t.status !== "resolved") return t;
  }
  return undefined;
}

/** 客服群里的一条消息属于哪个工单：话题 id 优先，其次按「回复了 bot 为该工单发的哪条消息」。 */
export async function ticketForSupportMessage(supportBotId: string, supportChatId: string, topicId?: number, replyToId?: number): Promise<Ticket | undefined> {
  const all = (await load()).tickets;
  for (let i = all.length - 1; i >= 0; i--) {
    const t = all[i];
    if (t.supportBotId !== supportBotId || t.supportChatId !== supportChatId) continue;
    if (topicId && t.topicId === topicId) return t;
    if (replyToId && t.msgIds.includes(replyToId)) return t;
  }
  return undefined;
}

export async function createTicket(t: Omit<Ticket, "id" | "status" | "msgIds" | "createdAt" | "updatedAt">): Promise<Ticket> {
  return mutate((s) => {
    const now = new Date().toISOString();
    s.seq += 1;
    const ticket: Ticket = { ...t, id: s.seq, status: "open", msgIds: [], createdAt: now, updatedAt: now };
    s.tickets.push(ticket);
    return { ...ticket };
  });
}

export async function updateTicket(id: number, patch: (t: Ticket) => void): Promise<Ticket | undefined> {
  return mutate((s) => {
    const t = s.tickets.find((x) => x.id === id);
    if (!t) return undefined;
    patch(t);
    t.msgIds = t.msgIds.slice(-100);
    t.updatedAt = new Date().toISOString();
    return { ...t };
  });
}

/** 待接且超过 slaMin 分钟还没有客服首次回复。 */
export function isOverdue(t: Ticket, slaMin: number, now = Date.now()): boolean {
  return t.status === "open" && !t.firstReplyAt && now - Date.parse(t.createdAt) > slaMin * 60000;
}

/** 工单搜索：#id / 用户名 / 昵称 / uid / 来源码 / 机器码 / 回执号 / 类型 / 客服 / 备注，多个词为「且」。 */
export function searchTickets(tickets: Ticket[], q: string, opts: { status?: string; overdueMin?: number; limit?: number; now?: number } = {}): Ticket[] {
  const terms = q.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const out: Ticket[] = [];
  for (let i = tickets.length - 1; i >= 0 && out.length < (opts.limit ?? 100); i--) {
    const t = tickets[i];
    if (opts.status && t.status !== opts.status) continue;
    if (opts.overdueMin !== undefined && !isOverdue(t, opts.overdueMin, opts.now)) continue;
    const hay = [`#${t.id}`, String(t.id), String(t.uid), t.name, t.username ? `@${t.username}` : "", t.src, t.fp, t.diag, t.kind, t.agent, t.note, t.lang]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    if (terms.every((w) => hay.includes(w))) out.push(t);
  }
  return out;
}

export type SupportDayStats = { created: number; replied: number; medianFirstReplyMin: number | null; overdue: number; resolved: number; good: number; bad: number };

/** 某一天（[fromMs, toMs)）新建工单的客服表现；overdue = 首次回复晚于 slaMin，或到 toMs 仍未回复且已超 slaMin。 */
export function supportDayStats(tickets: Ticket[], fromMs: number, toMs: number, slaMin: number): SupportDayStats {
  const rows = tickets.filter((t) => {
    const c = Date.parse(t.createdAt);
    return c >= fromMs && c < toMs;
  });
  const waits = rows
    .filter((t) => t.firstReplyAt)
    .map((t) => (Date.parse(t.firstReplyAt!) - Date.parse(t.createdAt)) / 60000)
    .sort((a, b) => a - b);
  const overdue = rows.filter((t) => {
    const c = Date.parse(t.createdAt);
    const end = t.firstReplyAt ? Date.parse(t.firstReplyAt) : t.status === "resolved" && t.resolvedAt ? Date.parse(t.resolvedAt) : Math.max(toMs, Date.now());
    return end - c > slaMin * 60000;
  }).length;
  return {
    created: rows.length,
    replied: waits.length,
    medianFirstReplyMin: waits.length ? Math.round(waits[Math.floor(waits.length / 2)] * 10) / 10 : null,
    overdue,
    resolved: rows.filter((t) => t.status === "resolved").length,
    good: rows.filter((t) => t.rating === "y").length,
    bad: rows.filter((t) => t.rating === "n").length,
  };
}

export function formatSupportDayLine(s: SupportDayStats, slaMin: number): string | null {
  if (!s.created) return null;
  const rate = Math.round((s.overdue / s.created) * 100);
  return `🎫 客服：新工单 ${s.created} · 已回复 ${s.replied}${s.medianFirstReplyMin === null ? "" : `（首次响应中位 ${s.medianFirstReplyMin} 分钟）`} · 超 ${slaMin} 分钟 ${s.overdue}（${rate}%）· 已结案 ${s.resolved}${s.good || s.bad ? ` · 👍${s.good}/👎${s.bad}` : ""}`;
}

export type TicketStats = { total: number; open: number; claimed: number; resolved: number; medianFirstReplyMin: number | null; good: number; bad: number };

export function ticketStats(tickets: Ticket[], sinceMs = 0): TicketStats {
  const rows = tickets.filter((t) => Date.parse(t.createdAt) >= sinceMs);
  const waits = rows
    .filter((t) => t.firstReplyAt)
    .map((t) => (Date.parse(t.firstReplyAt!) - Date.parse(t.createdAt)) / 60000)
    .sort((a, b) => a - b);
  return {
    total: rows.length,
    open: rows.filter((t) => t.status === "open").length,
    claimed: rows.filter((t) => t.status === "claimed").length,
    resolved: rows.filter((t) => t.status === "resolved").length,
    medianFirstReplyMin: waits.length ? Math.round(waits[Math.floor(waits.length / 2)] * 10) / 10 : null,
    good: rows.filter((t) => t.rating === "y").length,
    bad: rows.filter((t) => t.rating === "n").length,
  };
}
