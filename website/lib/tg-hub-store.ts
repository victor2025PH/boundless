/**
 * Telegram 运营中心配置：控制台登记的 bot、bot 所在的群 / 频道、频道邀请链接、客服工作时间。
 * 单文件 JSON（DATA_DIR/tg_hub.json，权限 600）：bot token 只存这里，接口对外一律打码。
 * 内置 bot（env CHATX_BOT_TOKEN）不入库，id 固定为 BUILTIN_BOT_ID。
 */
import crypto from "crypto";
import { mkdir, readFile, rename, writeFile } from "fs/promises";
import path from "path";
import { DATA_DIR } from "./data-dir";
import { BUILTIN_BOT_ID, builtinBot, type BotCtx } from "./tg-bot-context";

const HUB_FILE = process.env.TG_HUB_FILE || path.join(DATA_DIR, "tg_hub.json");

export type HubBot = {
  id: string;
  name: string;
  username?: string;
  token: string;
  secret: string;
  enabled: boolean;
  createdAt: string;
  webhookSetAt?: string;
};

export const HUB_ROLES = ["community", "support", "channel"] as const;
export type HubChatRole = (typeof HUB_ROLES)[number];

export type HubFeatures = {
  /** 群内被 @ / 回复 / 命令时 AI 作答 */
  ai: boolean;
  /** 没 @ 时也分析群消息：产品相关提问主动答，闲聊不插话（bot 需为管理员或关闭隐私模式才收得到） */
  autoReply: boolean;
  /** 新成员欢迎卡 */
  welcome: boolean;
  /** 新成员点按钮验证后才能发言 */
  verify: boolean;
  /** 新成员 24 小时内发链接 / 转发频道消息自动删除 */
  antispam: boolean;
  /** 群里出现机器码 / 回执号自动删除并引导私聊 */
  privacyGuard: boolean;
  /** 频道 / 群的入群申请自动批准并私聊欢迎 */
  joinApprove: boolean;
};

export const DEFAULT_FEATURES: HubFeatures = {
  ai: true,
  autoReply: true,
  welcome: true,
  verify: false,
  antispam: true,
  privacyGuard: true,
  joinApprove: true,
};

export type HubChat = {
  chatId: string;
  botId: string;
  title: string;
  username?: string;
  type: string;
  isForum?: boolean;
  role: HubChatRole;
  enabled: boolean;
  /** bot 在该群的身份：member / administrator / left / kicked */
  botStatus?: string;
  /** 客服群接待的用户语言（如 ["zh"]）；空 = 全部语言 */
  langs?: string[];
  features: HubFeatures;
  discoveredAt: string;
  updatedAt: string;
};

export type HubInvite = { chatId: string; botId: string; src: string; link: string; createdAt: string };

export type HubSupport = { hours: string; tzOffset: number; /** 待接工单多少分钟没人回复就在客服群提醒 */ slaMin: number; /** 等待满多少分钟仍没人回复再通知管理员（0 = 不升级） */ escalateMin: number;
  /** 值班表，每行「[星期] [时段] @客服…」，如 "1-5 09:00-18:00 @alice"；超时提醒 / 升级时 @ 当班的人 */
  duty: string;
  /** 已知问题给出后多少分钟没点按钮就追问一次（0 = 不追问） */
  followupMin: number;
};

export type HubConfig = { bots: HubBot[]; chats: HubChat[]; invites: HubInvite[]; support: HubSupport };

export const DEFAULT_SUPPORT: HubSupport = { hours: "09:00-22:00", tzOffset: 8, slaMin: 15, escalateMin: 45, duty: "", followupMin: 30 };

function emptyHub(): HubConfig {
  return { bots: [], chats: [], invites: [], support: { ...DEFAULT_SUPPORT } };
}

export async function loadHub(): Promise<HubConfig> {
  try {
    const raw = JSON.parse(await readFile(HUB_FILE, "utf-8")) as Partial<HubConfig>;
    return {
      bots: Array.isArray(raw.bots) ? raw.bots : [],
      chats: Array.isArray(raw.chats) ? raw.chats.map((c) => ({ ...c, features: { ...DEFAULT_FEATURES, ...c.features } })) : [],
      invites: Array.isArray(raw.invites) ? raw.invites : [],
      support: { ...DEFAULT_SUPPORT, ...raw.support },
    };
  } catch {
    return emptyHub();
  }
}

let chain: Promise<unknown> = Promise.resolve();

/** 串行读改写，防并发 webhook 互相覆盖。 */
export function mutateHub<T>(fn: (hub: HubConfig) => T | Promise<T>): Promise<T> {
  const run = chain.then(async () => {
    const hub = await loadHub();
    const out = await fn(hub);
    await mkdir(path.dirname(HUB_FILE), { recursive: true });
    const tmp = `${HUB_FILE}.${process.pid}.tmp`;
    await writeFile(tmp, JSON.stringify(hub, null, 1), { encoding: "utf-8", mode: 0o600 });
    await rename(tmp, HUB_FILE);
    return out;
  });
  chain = run.catch(() => undefined);
  return run;
}

export const chatKey = (botId: string, chatId: string | number) => `${botId}:${chatId}`;

export function newBotId(): string {
  return "b" + crypto.randomBytes(4).toString("hex");
}

export function newWebhookSecret(): string {
  return crypto.randomBytes(24).toString("hex");
}

export function maskToken(token: string): string {
  const [id] = token.split(":");
  return `${id}:••••${token.slice(-4)}`;
}

/** bot id → 调用身份；停用或不存在返回 null。 */
export async function botCtxById(id: string, hub?: HubConfig): Promise<BotCtx | null> {
  if (id === BUILTIN_BOT_ID) return builtinBot();
  const h = hub ?? (await loadHub());
  const b = h.bots.find((x) => x.id === id && x.enabled);
  return b ? { id: b.id, token: b.token, username: b.username } : null;
}

export function findChat(hub: HubConfig, botId: string, chatId: string | number): HubChat | undefined {
  return hub.chats.find((c) => c.botId === botId && c.chatId === String(chatId));
}

/**
 * 客服群选择（多客服群分流）：只看已启用且 bot 为管理员的客服群；
 * 优先级：明确接待该语言 > 不限语言 > 语言不符；同级里同一个 bot 优先，再选当前未结工单最少的群。
 */
export function pickSupportChat(hub: HubConfig, userBotId: string, lang?: string, openLoad: Record<string, number> = {}): HubChat | undefined {
  const ok = hub.chats.filter((c) => c.role === "support" && c.enabled && c.botStatus === "administrator");
  const langRank = (c: HubChat) => (!c.langs?.length ? 1 : lang && c.langs.includes(lang) ? 2 : 0);
  const score = (c: HubChat): number[] => [langRank(c), c.botId === userBotId ? 1 : 0, -(openLoad[chatKey(c.botId, c.chatId)] ?? 0)];
  const cmp = (a: number[], b: number[]) => {
    for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return a[i] - b[i];
    return 0;
  };
  let best: HubChat | undefined;
  for (const c of ok) if (!best || cmp(score(c), score(best)) > 0) best = c;
  return best;
}

export type DiscoveredChat = { id: number; title?: string; username?: string; type?: string; is_forum?: boolean };

/** bot 被拉进 / 移出某个群或频道：登记为待启用（默认关闭，需在控制台启用）。返回是否为新发现。 */
export async function recordChatMembership(botId: string, chat: DiscoveredChat, botStatus: string): Promise<{ isNew: boolean; chat: HubChat }> {
  return mutateHub((hub) => {
    const now = new Date().toISOString();
    let c = findChat(hub, botId, chat.id);
    const isNew = !c;
    if (!c) {
      c = {
        chatId: String(chat.id),
        botId,
        title: chat.title || String(chat.id),
        username: chat.username,
        type: chat.type || "group",
        isForum: chat.is_forum,
        role: chat.type === "channel" ? "channel" : chat.is_forum ? "support" : "community",
        enabled: false,
        botStatus,
        features: { ...DEFAULT_FEATURES },
        discoveredAt: now,
        updatedAt: now,
      };
      hub.chats.push(c);
    } else {
      c.title = chat.title || c.title;
      c.username = chat.username ?? c.username;
      c.type = chat.type || c.type;
      if (chat.is_forum !== undefined) c.isForum = chat.is_forum;
      c.botStatus = botStatus;
      c.updatedAt = now;
    }
    return { isNew, chat: { ...c } };
  });
}

export type DutyRule = { days: number[]; from?: number; to?: number; users: string[] };

/** 解析值班表：星期 1–7（1 = 周一），可写 "1-5"、"6,7"、"1-5,7"，省略 = 每天；时段可省略 = 全天。返回规则和格式不对的行。 */
export function parseDuty(text: string): { rules: DutyRule[]; bad: string[] } {
  const rules: DutyRule[] = [];
  const bad: string[] = [];
  for (const raw of text.split("\n")) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const m = line.match(/^(?:([1-7](?:-[1-7])?(?:,[1-7](?:-[1-7])?)*)\s+)?(?:(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s+)?((?:@[A-Za-z0-9_]{4,32}\s*)+)$/);
    if (!m) {
      bad.push(line);
      continue;
    }
    const days = new Set<number>();
    for (const part of (m[1] ?? "1-7").split(",")) {
      const [a, b] = part.split("-").map(Number);
      for (let d = a; d <= (b ?? a); d++) days.add(d);
    }
    const rule: DutyRule = { days: [...days].sort(), users: m[6].trim().split(/\s+/).map((u) => u.slice(1)) };
    if (m[2]) {
      rule.from = Number(m[2]) * 60 + Number(m[3]);
      rule.to = Number(m[4]) * 60 + Number(m[5]);
    }
    rules.push(rule);
  }
  return { rules, bad };
}

/** 当前当班的客服用户名（不带 @，去重）；没配值班表返回空。 */
export function dutyNow(s: Pick<HubSupport, "duty" | "tzOffset">, now = Date.now()): string[] {
  const d = new Date(now + s.tzOffset * 3600_000);
  const day = d.getUTCDay() || 7;
  const cur = d.getUTCHours() * 60 + d.getUTCMinutes();
  const out = new Set<string>();
  for (const r of parseDuty(s.duty ?? "").rules) {
    if (!r.days.includes(day)) continue;
    if (r.from !== undefined && r.to !== undefined && r.from !== r.to && !(r.from < r.to ? cur >= r.from && cur < r.to : cur >= r.from || cur < r.to)) continue;
    for (const u of r.users) out.add(u);
  }
  return [...out];
}

/** 工作时间判断：hours 形如 "09:00-22:00"（可跨零点，如 "20:00-02:00"）；格式不对按全天在线。 */
export function inSupportHours(s: Pick<HubSupport, "hours" | "tzOffset">, now = Date.now()): boolean {
  const m = s.hours.match(/^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$/);
  if (!m) return true;
  const start = Number(m[1]) * 60 + Number(m[2]);
  const end = Number(m[3]) * 60 + Number(m[4]);
  const d = new Date(now + s.tzOffset * 3600_000);
  const cur = d.getUTCHours() * 60 + d.getUTCMinutes();
  if (start === end) return true;
  return start < end ? cur >= start && cur < end : cur >= start || cur < end;
}
