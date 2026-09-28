/**
 * 控制台「Telegram 运营」的服务端操作：登记 bot（getMe 校验 token）、设 webhook、验证群/频道权限、
 * 生成带来源码的邀请链接。token / secret 只在服务端，对外一律用 publicHub() 脱敏。
 */
import { CHATX_ALLOWED_UPDATES, setupChatxBot } from "./chatx-bot";
import { SITE_URL } from "./site";
import { builtinBot, BUILTIN_BOT_ID, withBot, type BotCtx } from "./tg-bot-context";
import {
  DEFAULT_FEATURES,
  HUB_ROLES,

  findChat,
  loadHub,
  maskToken,
  mutateHub,
  newBotId,
  newWebhookSecret,
  parseDuty,
  type HubChat,
  type HubChatRole,
  type HubConfig,
  type HubFeatures,
  type HubSupport,
} from "./tg-hub-store";

export const TOKEN_RE = /^\d{6,12}:[A-Za-z0-9_-]{30,}$/;
const SRC_RE = /^[A-Za-z0-9_-]{1,32}$/;

export class HubError extends Error {}

type TgRes<T = unknown> = { ok?: boolean; result?: T; description?: string };

async function tgRaw<T = unknown>(token: string, method: string, body: Record<string, unknown> = {}): Promise<TgRes<T>> {
  try {
    const r = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(10_000),
    });
    return (await r.json()) as TgRes<T>;
  } catch (e) {
    return { ok: false, description: String(e) };
  }
}

type Me = { id: number; username?: string; first_name?: string; can_join_groups?: boolean; can_read_all_group_messages?: boolean };

export type PublicBot = {
  id: string;
  name: string;
  username?: string;
  tokenMasked: string;
  enabled: boolean;
  builtin: boolean;
  webhookPath: string;
  webhookSetAt?: string;
  createdAt?: string;
};

export type PublicHub = { bots: PublicBot[]; chats: HubChat[]; invites: HubConfig["invites"]; support: HubSupport };

export type SetupStep = { key: string; label: string; done: boolean; hint: string; href: string };

/** 功能页顶部的配置清单：按推荐顺序列出每一步是否已完成、没完成时下一步做什么。 */
export function setupChecklist(hub: PublicHub, knownEnabled: number): SetupStep[] {
  const bots = hub.bots.filter((b) => b.enabled);
  const admin = (c: HubChat) => c.enabled && c.botStatus === "administrator";
  const support = hub.chats.filter((c) => c.role === "support");
  const supportOk = support.filter(admin);
  const outlets = hub.chats.filter((c) => c.role !== "support" && admin(c));
  const pendingNew = hub.chats.filter((c) => !c.enabled).length;
  return [
    { key: "bot", label: "至少一个可用的 bot", done: bots.length > 0, hint: "在「Bot」里填名称和 token 登记，然后点「设置 webhook」", href: "#bots" },
    {
      key: "support",
      label: "启用一个开了话题的客服群（bot 为管理员）",
      done: supportOk.some((c) => c.isForum),
      hint: supportOk.length
        ? "客服群要在 Telegram 群设置里打开「话题」，每个用户一个话题"
        : support.length
          ? "客服群已登记但未启用或 bot 不是管理员：先把 bot 设为管理员，再启用"
          : "建一个私有群并打开「话题」，把 bot 拉进去设为管理员；它会自动出现在「群与频道」，用途选「客服群」再启用",
      href: "#chats",
    },
    {
      key: "outlet",
      label: "启用一个社群或频道",
      done: outlets.length > 0,
      hint: pendingNew ? `有 ${pendingNew} 个新登记的群 / 频道还是停用状态，设好用途后启用` : "把 bot 拉进社群 / 频道并设为管理员，或在「群与频道」按 @用户名 手动添加",
      href: "#chats",
    },
    { key: "hours", label: "设置客服工作时间和超时提醒", done: hub.support.hours.trim() !== "", hint: "在「客服工作时间」里填上班时间、超时提醒和升级管理员的分钟数", href: "#support" },
    { key: "invite", label: "生成来源邀请链接", done: hub.invites.length > 0, hint: "给投放渠道各生成一个，链接名就是来源码，入群人数会按来源统计", href: "#invites" },
    { key: "known", label: "已知问题库有启用条目", done: knownEnabled > 0, hint: "看日报里「仍转人工」多的条目，改文案或补关键词", href: "#known" },
  ];
}

export function webhookPathFor(botId: string): string {
  return botId === BUILTIN_BOT_ID ? "/api/telegram/chatx/webhook" : `/api/telegram/bots/${botId}/webhook`;
}

export function publicHub(hub: HubConfig): PublicHub {
  const bots: PublicBot[] = [];
  const b0 = builtinBot();
  if (b0) bots.push({ id: b0.id, name: "ChatX 推广 bot（内置）", username: b0.username, tokenMasked: maskToken(b0.token), enabled: true, builtin: true, webhookPath: webhookPathFor(b0.id) });
  for (const b of hub.bots) {
    bots.push({ id: b.id, name: b.name, username: b.username, tokenMasked: maskToken(b.token), enabled: b.enabled, builtin: false, webhookPath: webhookPathFor(b.id), webhookSetAt: b.webhookSetAt, createdAt: b.createdAt });
  }
  return { bots, chats: hub.chats, invites: hub.invites, support: hub.support };
}

async function ctxOrThrow(botId: string, hub?: HubConfig): Promise<BotCtx> {
  if (botId === BUILTIN_BOT_ID) {
    const b = builtinBot();
    if (!b) throw new HubError("内置 bot 未配置 CHATX_BOT_TOKEN");
    return b;
  }
  const h = hub ?? (await loadHub());
  const b = h.bots.find((x) => x.id === botId);
  if (!b) throw new HubError(`bot 不存在：${botId}`);
  return { id: b.id, token: b.token, username: b.username };
}

export async function addBot(input: { name: string; token: string }): Promise<PublicBot> {
  const name = input.name.trim().slice(0, 60);
  const token = input.token.trim();
  if (!name) throw new HubError("请填写名称");
  if (!TOKEN_RE.test(token)) throw new HubError("token 格式不对（应为 BotFather 给的 123456789:AA… 格式）");
  const me = await tgRaw<Me>(token, "getMe");
  if (!me.ok || !me.result) throw new HubError(`token 无效：${me.description ?? "getMe 失败"}`);
  const botNumId = token.split(":")[0];
  const b0 = builtinBot();
  if (b0 && b0.token.split(":")[0] === botNumId) throw new HubError("这是内置 ChatX bot，无需重复登记");
  return mutateHub((hub) => {
    if (hub.bots.some((b) => b.token.split(":")[0] === botNumId)) throw new HubError(`@${me.result!.username} 已登记过`);
    const bot = { id: newBotId(), name, username: me.result!.username, token, secret: newWebhookSecret(), enabled: true, createdAt: new Date().toISOString() };
    hub.bots.push(bot);
    return publicHub({ ...hub, bots: [bot] }).bots.find((x) => x.id === bot.id)!;
  });
}

export async function updateBot(id: string, patch: { name?: string; enabled?: boolean; token?: string }) {
  if (id === BUILTIN_BOT_ID) throw new HubError("内置 bot 的 token 在服务器 .env.local 里管理，不能在这里改");
  let username: string | undefined;
  if (patch.token !== undefined) {
    const token = patch.token.trim();
    if (!TOKEN_RE.test(token)) throw new HubError("token 格式不对");
    const me = await tgRaw<Me>(token, "getMe");
    if (!me.ok || !me.result) throw new HubError(`token 无效：${me.description ?? "getMe 失败"}`);
    username = me.result.username;
  }
  return mutateHub((hub) => {
    const b = hub.bots.find((x) => x.id === id);
    if (!b) throw new HubError(`bot 不存在：${id}`);
    if (patch.token !== undefined) {
      if (b.token.split(":")[0] !== patch.token.trim().split(":")[0]) throw new HubError("新 token 属于另一个 bot；换 bot 请新增一条");
      b.token = patch.token.trim();
      b.username = username ?? b.username;
      b.webhookSetAt = undefined;
    }
    if (patch.name !== undefined && patch.name.trim()) b.name = patch.name.trim().slice(0, 60);
    if (patch.enabled !== undefined) b.enabled = patch.enabled;
    return { id: b.id, enabled: b.enabled, tokenChanged: patch.token !== undefined };
  });
}

const SITE = SITE_URL.endsWith("/") ? SITE_URL.slice(0, -1) : SITE_URL;

/** 设 webhook（带该 bot 的 secret 与扩展后的 update 类型）+ 命令菜单；不丢积压消息。 */
export async function applyWebhook(botId: string) {
  const hub = await loadHub();
  const ctx = await ctxOrThrow(botId, hub);
  const b = hub.bots.find((x) => x.id === botId);
  const res = await withBot(ctx, () =>
    setupChatxBot(botId === BUILTIN_BOT_ID ? { skipProfile: true } : { webhookUrl: SITE + webhookPathFor(botId), secret: b!.secret, skipProfile: true })
  );
  if (res.ok && b) {
    await mutateHub((h) => {
      const x = h.bots.find((y) => y.id === botId);
      if (x) x.webhookSetAt = new Date().toISOString();
    });
  }
  return { ok: res.ok, steps: res.steps, allowedUpdates: CHATX_ALLOWED_UPDATES };
}

export type WebhookInfo = { url?: string; pending: number; allowedUpdates?: string[]; lastError?: string; lastErrorAt?: string; me?: Me };

export async function webhookInfo(botId: string): Promise<WebhookInfo> {
  const ctx = await ctxOrThrow(botId);
  const [w, me] = await Promise.all([
    tgRaw<{ url?: string; pending_update_count?: number; allowed_updates?: string[]; last_error_message?: string; last_error_date?: number }>(ctx.token, "getWebhookInfo"),
    tgRaw<Me>(ctx.token, "getMe"),
  ]);
  if (!w.ok || !w.result) throw new HubError(w.description ?? "getWebhookInfo 失败");
  const r = w.result;
  return {
    url: r.url,
    pending: r.pending_update_count ?? 0,
    allowedUpdates: r.allowed_updates,
    lastError: r.last_error_message,
    lastErrorAt: r.last_error_date ? new Date(r.last_error_date * 1000).toISOString() : undefined,
    me: me.result,
  };
}

type ChatInfo = { id: number; title?: string; username?: string; type: string; is_forum?: boolean };
type Member = { status: string; can_delete_messages?: boolean; can_restrict_members?: boolean; can_manage_topics?: boolean; can_invite_users?: boolean; can_post_messages?: boolean };

export type ChatCheck = { chat: HubChat; missing: string[] };

function missingPerms(role: HubChatRole, type: string, m: Member, forum?: boolean): string[] {
  const miss: string[] = [];
  if (m.status !== "administrator" && m.status !== "creator") return ["bot 不是管理员"];
  if (type === "channel") {
    if (!m.can_invite_users) miss.push("邀请用户（生成邀请链接 / 批准申请）");
    return miss;
  }
  if (!m.can_delete_messages) miss.push("删除消息");
  if (!m.can_restrict_members) miss.push("封禁 / 限制成员");
  if (!m.can_invite_users) miss.push("邀请用户");
  if (role === "support" && forum && !m.can_manage_topics) miss.push("管理话题");
  if (role === "support" && !forum) miss.push("客服群建议开启「话题」功能（每个工单一个话题）");
  return miss;
}

/** 按 chat id 或 @username 登记 / 刷新一个群或频道；会校验 bot 是否是管理员及所需权限。 */
export async function verifyChat(botId: string, chatRef: string, opts?: { role?: HubChatRole; enable?: boolean }): Promise<ChatCheck> {
  const ctx = await ctxOrThrow(botId);
  const ref = chatRef.trim();
  if (!/^(-?\d{5,20}|@[A-Za-z0-9_]{4,32})$/.test(ref)) throw new HubError("请填群 / 频道的数字 id（如 -1001234567890）或 @公开用户名");
  const [c, me] = await Promise.all([tgRaw<ChatInfo>(ctx.token, "getChat", { chat_id: ref }), tgRaw<Me>(ctx.token, "getMe")]);
  if (!c.ok || !c.result) throw new HubError(`找不到这个群 / 频道（先把 bot 拉进去）：${c.description ?? ""}`);
  if (!me.result) throw new HubError("getMe 失败");
  const info = c.result;
  if (info.type === "private") throw new HubError("这是私聊，不是群或频道");
  const m = await tgRaw<Member>(ctx.token, "getChatMember", { chat_id: info.id, user_id: me.result.id });
  const member = m.result ?? { status: "left" };
  const role = opts?.role;
  const chat = await mutateHub((hub) => {
    const now = new Date().toISOString();
    let x = findChat(hub, botId, info.id);
    if (!x) {
      x = {
        chatId: String(info.id),
        botId,
        title: info.title || String(info.id),
        type: info.type,
        role: info.type === "channel" ? "channel" : info.is_forum ? "support" : "community",
        enabled: false,
        features: { ...DEFAULT_FEATURES },
        discoveredAt: now,
        updatedAt: now,
      };
      hub.chats.push(x);
    }
    x.title = info.title || x.title;
    x.username = info.username;
    x.type = info.type;
    x.isForum = info.is_forum;
    x.botStatus = member.status;
    if (role) x.role = role;
    if (opts?.enable !== undefined) x.enabled = opts.enable;
    x.updatedAt = now;
    return { ...x };
  });
  return { chat, missing: missingPerms(chat.role, chat.type, member, chat.isForum) };
}

export function parseLangs(v: string): string[] {
  const out = v.split(/[\s,，、]+/).map((x) => x.trim().toLowerCase()).filter(Boolean);
  for (const l of out) if (!/^[a-z]{2}$/.test(l)) throw new HubError(`语言代码应为两位字母（如 zh, en），收到：${l}`);
  return Array.from(new Set(out));
}

export async function updateChat(botId: string, chatId: string, patch: { role?: string; enabled?: boolean; features?: Partial<HubFeatures>; title?: string; langs?: string }) {
  const langs = patch.langs === undefined ? undefined : parseLangs(patch.langs);
  if (patch.role !== undefined && !(HUB_ROLES as readonly string[]).includes(patch.role)) throw new HubError(`未知用途：${patch.role}`);
  return mutateHub((hub) => {
    const c = findChat(hub, botId, chatId);
    if (!c) throw new HubError("群 / 频道不存在");
    if (patch.role !== undefined) c.role = patch.role as HubChatRole;
    if (patch.enabled !== undefined) c.enabled = patch.enabled;
    if (patch.features) {
      for (const k of Object.keys(DEFAULT_FEATURES) as (keyof HubFeatures)[]) {
        const v = patch.features[k];
        if (typeof v === "boolean") c.features[k] = v;
      }
    }
    if (patch.title?.trim()) c.title = patch.title.trim().slice(0, 120);
    if (langs !== undefined) c.langs = langs.length ? langs : undefined;
    c.updatedAt = new Date().toISOString();
    return { ...c };
  });
}

/** 生成「需申请」的邀请链接，链接名 = 来源码：用户申请后 bot 自动批准并按来源码归因。 */
export async function createInvite(botId: string, chatId: string, src: string) {
  if (!SRC_RE.test(src)) throw new HubError("来源码只能用字母、数字、下划线、横线（≤32 位），如 ad_biz_voice01");
  const hub = await loadHub();
  const ctx = await ctxOrThrow(botId, hub);
  if (!findChat(hub, botId, chatId)) throw new HubError("请先登记这个群 / 频道");
  const r = await tgRaw<{ invite_link: string }>(ctx.token, "createChatInviteLink", { chat_id: chatId, name: src, creates_join_request: true });
  if (!r.ok || !r.result) throw new HubError(`生成失败（bot 需要「邀请用户」权限）：${r.description ?? ""}`);
  const inv = { chatId, botId, src, link: r.result.invite_link, createdAt: new Date().toISOString() };
  await mutateHub((h) => {
    h.invites.push(inv);
    if (h.invites.length > 500) h.invites = h.invites.slice(-500);
  });
  return inv;
}

export async function setSupport(s: { hours?: string; tzOffset?: number; slaMin?: number; escalateMin?: number; duty?: string; followupMin?: number }) {
  if (s.hours !== undefined && !/^\d{1,2}:\d{2}\s*-\s*\d{1,2}:\d{2}$/.test(s.hours.trim())) throw new HubError("工作时间格式：09:00-22:00");
  if (s.tzOffset !== undefined && !(Number.isFinite(s.tzOffset) && s.tzOffset >= -12 && s.tzOffset <= 14)) throw new HubError("时区偏移应在 -12 到 14 之间");
  if (s.slaMin !== undefined && !(Number.isInteger(s.slaMin) && s.slaMin >= 1 && s.slaMin <= 1440)) throw new HubError("超时提醒应为 1–1440 分钟");
  if (s.escalateMin !== undefined && !(Number.isInteger(s.escalateMin) && s.escalateMin >= 0 && s.escalateMin <= 2880)) throw new HubError("升级管理员应为 0–2880 分钟");
  if (s.followupMin !== undefined && !(Number.isInteger(s.followupMin) && s.followupMin >= 0 && s.followupMin <= 720)) throw new HubError("追问时间应为 0–720 分钟");
  const duty = s.duty === undefined ? undefined : s.duty.replace(/\r/g, "").trim();
  if (duty !== undefined) {
    if (duty.length > 1000) throw new HubError("值班表最多 1000 字");
    const bad = parseDuty(duty).bad;
    if (bad.length) throw new HubError(`值班表这行看不懂：${bad[0]}（格式：1-5 09:00-18:00 @用户名）`);
  }
  return mutateHub((hub) => {
    if (duty !== undefined) hub.support.duty = duty;
    if (s.followupMin !== undefined) hub.support.followupMin = s.followupMin;
    if (s.slaMin !== undefined) hub.support.slaMin = s.slaMin;
    if (s.escalateMin !== undefined) hub.support.escalateMin = s.escalateMin;
    if (hub.support.escalateMin && hub.support.escalateMin <= hub.support.slaMin) throw new HubError("升级管理员的时间要大于超时提醒时间（或填 0 不升级）");
    if (s.hours !== undefined) hub.support.hours = s.hours.trim();
    if (s.tzOffset !== undefined) hub.support.tzOffset = s.tzOffset;
    return { ...hub.support };
  });
}

