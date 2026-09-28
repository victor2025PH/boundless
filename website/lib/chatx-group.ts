/**
 * ChatX bot 在群 / 频道里的行为（只对控制台里「已启用」的群生效）：
 *   · 社群：被 @ / 回复 / /ask 时 AI 简答；新成员欢迎与按钮验证；新成员发链接自动删；
 *     机器码 / 回执号自动删并引导私聊；管理员回复消息发 /ban /mute /unmute。
 *   · 客服群：工单话题里的消息转给用户（见 chatx-support）。
 *   · 频道 / 入群申请：自动批准，按邀请链接名（来源码）归因，并私聊发欢迎。
 */
import { detectKnowledgeLang, type BotLang } from "./bot-knowledge";
import { askDeepSeek } from "./deepseek";
import { chatxSystemHint, escHtml, normalizeSrc, sendText, tgCall, type InlineBtn, type TgFrom } from "./chatx-bot";
import { extractDiagCode, extractFingerprint, handleAgentMessage, type AgentMsg } from "./chatx-support";
import { notifyAdmins } from "./order-store";
import { BUILTIN_BOT_ID, currentBot, withBot } from "./tg-bot-context";
import { recordChatMembership, type DiscoveredChat, type HubChat } from "./tg-hub-store";
import { trackTg } from "./tg-events";

export type GroupMsg = AgentMsg & {
  chat: { id: number; type?: string; title?: string; username?: string; is_forum?: boolean };
  from?: TgFrom & { is_bot?: boolean };
  sender_chat?: { id: number };
  entities?: { type: string; offset: number; length: number }[];
  caption_entities?: { type: string; offset: number; length: number }[];
  reply_to_message?: { message_id: number; from?: { id: number; is_bot?: boolean; username?: string } };
  forward_from_chat?: unknown;
  forward_origin?: { type?: string };
  new_chat_members?: (TgFrom & { is_bot?: boolean })[];
};

const JOIN_WINDOW_MS = 24 * 3600_000;
const joinedAt = new Map<string, number>();
const warns = new Map<string, number>();
const lastGroupReply = new Map<string, number>();
const lastUserAsk = new Map<string, number>();

const GROUP_COOLDOWN_MS = 4000;
const USER_COOLDOWN_MS = 20_000;

const botId = () => currentBot()?.id ?? BUILTIN_BOT_ID;
const botUsername = () => currentBot()?.username ?? "";
const privateLink = (param: string) => `https://t.me/${botUsername()}?start=${param}`;
const nameOf = (u: TgFrom) => u.first_name || u.username || String(u.id);

function rememberJoin(chatId: number, uid: number) {
  joinedAt.set(`${chatId}:${uid}`, Date.now());
  if (joinedAt.size > 20000) joinedAt.delete(joinedAt.keys().next().value!);
}

function isNewcomer(chatId: number, uid: number): boolean {
  const t = joinedAt.get(`${chatId}:${uid}`);
  return t !== undefined && Date.now() - t < JOIN_WINDOW_MS;
}

async function isAdmin(chatId: number, msg: GroupMsg): Promise<boolean> {
  if (msg.sender_chat?.id === chatId) return true;
  if (!msg.from) return false;
  const r = await tgCall("getChatMember", { chat_id: chatId, user_id: msg.from.id });
  const st = (r?.result as { status?: string } | undefined)?.status;
  return st === "administrator" || st === "creator";
}

/** bot 进出群 / 频道：登记（默认未启用），新发现时提醒管理员去控制台启用。 */
export async function handleMyChatMember(chat: DiscoveredChat, status: string, by?: TgFrom) {
  const { isNew, chat: c } = await recordChatMembership(botId(), chat, status);
  await trackTg("chatx_hub_membership", { bot: botId(), chat: chat.id, status, type: chat.type });
  if (isNew && (status === "member" || status === "administrator")) {
    await notifyAdmins(
      `🔒 管理员 · 仅你可见\n🤖 bot @${escHtml(botUsername())} 被${by ? ` ${escHtml(nameOf(by))} ` : ""}加入 ${c.type === "channel" ? "频道" : "群"}「${escHtml(c.title)}」（${c.chatId}）。\n到控制台「Telegram 运营」里设置用途并启用后才会生效。`
    );
  }
}

// ── 新成员 ──────────────────────────────────────────────────────────

const FULL_PERMS = {
  can_send_messages: true,
  can_send_audios: true,
  can_send_documents: true,
  can_send_photos: true,
  can_send_videos: true,
  can_send_video_notes: true,
  can_send_voice_notes: true,
  can_send_polls: true,
  can_send_other_messages: true,
  can_add_web_page_previews: true,
};

function welcomeKeyboard(lang: BotLang, verifyUid?: number): InlineBtn[][] {
  const zh = lang === "zh";
  const rows: InlineBtn[][] = [];
  if (verifyUid) rows.push([{ text: zh ? "✅ 我是真人，点我验证" : "✅ I'm human — verify", callback_data: `gv:${verifyUid}` }]);
  rows.push([
    { text: zh ? "💬 私聊提问 / 报障" : "💬 Ask / report privately", url: privateLink("grp") },
    { text: zh ? "📥 下载 ChatX" : "📥 Get ChatX", url: privateLink("grp") },
  ]);
  return rows;
}

async function welcome(chat: HubChat, msg: GroupMsg) {
  const lang: BotLang = msg.from?.language_code?.startsWith("zh") === false ? "en" : "zh";
  for (const u of msg.new_chat_members ?? []) {
    if (u.is_bot) continue;
    rememberJoin(msg.chat.id, u.id);
    const verify = chat.features.verify;
    if (verify) await tgCall("restrictChatMember", { chat_id: msg.chat.id, user_id: u.id, permissions: { can_send_messages: false } });
    if (!chat.features.welcome && !verify) continue;
    const zh = lang === "zh";
    const text = zh
      ? `👋 欢迎 ${escHtml(nameOf(u))}！\n有问题在群里 @${escHtml(botUsername())} 就能问；报障、机器码、手机号请点下面按钮私聊发送，不要发在群里。${verify ? "\n\n先点「我是真人」完成验证才能发言。" : ""}`
      : `👋 Welcome ${escHtml(nameOf(u))}!\nMention @${escHtml(botUsername())} to ask anything. For bug reports, machine codes or phone numbers, tap below and message me privately — never post them here.${verify ? "\n\nTap “I'm human” to verify before chatting." : ""}`;
    const r = await tgCall("sendMessage", {
      chat_id: msg.chat.id,
      message_thread_id: msg.message_thread_id,
      text,
      parse_mode: "HTML",
      disable_web_page_preview: true,
      reply_markup: { inline_keyboard: welcomeKeyboard(lang, verify ? u.id : undefined) },
    });
    const mid = (r?.result as { message_id?: number } | undefined)?.message_id;
    if (mid) {
      const bot = currentBot();
      const t = setTimeout(() => {
        if (bot) void withBot(bot, () => tgCall("deleteMessage", { chat_id: msg.chat.id, message_id: mid }));
      }, 5 * 60_000);
      t.unref?.();
    }
    await trackTg("chatx_group_join", { bot: botId(), chat: msg.chat.id, uid: u.id, verify });
  }
}

/** 验证按钮：只有被验证的本人点才生效；恢复为群默认权限。 */
export async function handleVerifyCallback(cq: { id: string; data: string; from: TgFrom; chatId: number; messageId?: number }): Promise<boolean> {
  if (!cq.data.startsWith("gv:")) return false;
  const uid = Number(cq.data.slice(3));
  if (uid !== cq.from.id) {
    await tgCall("answerCallbackQuery", { callback_query_id: cq.id, text: "这个按钮不是给你的 / Not for you", show_alert: false });
    return true;
  }
  const chat = await tgCall("getChat", { chat_id: cq.chatId });
  const perms = (chat?.result as { permissions?: Record<string, boolean> } | undefined)?.permissions ?? FULL_PERMS;
  await tgCall("restrictChatMember", { chat_id: cq.chatId, user_id: uid, permissions: perms });
  await tgCall("answerCallbackQuery", { callback_query_id: cq.id, text: "✅ 验证通过 / Verified" });
  if (cq.messageId) await tgCall("deleteMessage", { chat_id: cq.chatId, message_id: cq.messageId });
  await trackTg("chatx_group_verify", { bot: botId(), chat: cq.chatId, uid });
  return true;
}

// ── 守护：隐私 / 反垃圾 ─────────────────────────────────────────────

const LINK_RE = /(https?:\/\/|t\.me\/|telegram\.me\/|www\.)\S+/i;

function hasLink(msg: GroupMsg, text: string): boolean {
  const ents = [...(msg.entities ?? []), ...(msg.caption_entities ?? [])];
  return LINK_RE.test(text) || ents.some((e) => e.type === "url" || e.type === "text_link" || e.type === "mention");
}

/** 机器码 / 回执号 / 手机号出现在群里：删掉并引导私聊。返回 true = 已处理。 */
async function privacyGuard(msg: GroupMsg, text: string, lang: BotLang): Promise<boolean> {
  const phone = /(?<!\d)(\+?\d[\d -]{9,16}\d)(?!\d)/.test(text) && text.replace(/\D/g, "").length >= 11;
  const hit = extractFingerprint(text) || (await extractDiagCode(text)) || phone;
  if (!hit) return false;
  await tgCall("deleteMessage", { chat_id: msg.chat.id, message_id: msg.message_id });
  const zh = lang === "zh";
  await tgCall("sendMessage", {
    chat_id: msg.chat.id,
    message_thread_id: msg.message_thread_id,
    text: zh
      ? `🔒 ${escHtml(nameOf(msg.from!))}，你刚发的消息里有机器码 / 回执号 / 手机号，为保护隐私已删除。请点下面按钮私聊我发送，我帮你查。`
      : `🔒 ${escHtml(nameOf(msg.from!))}, your message contained a machine code / receipt / phone number, so I removed it for your privacy. Tap below and send it to me privately.`,
    parse_mode: "HTML",
    reply_markup: { inline_keyboard: [[{ text: zh ? "🩺 私聊报障" : "🩺 Report privately", url: privateLink("bug") }]] },
  });
  await trackTg("chatx_group_privacy", { bot: botId(), chat: msg.chat.id });
  return true;
}

/** 新成员（入群 24h 内）发链接 / 外部转发：删掉，第 3 次禁言 24h。 */
async function antispam(msg: GroupMsg, text: string): Promise<boolean> {
  const uid = msg.from!.id;
  if (!isNewcomer(msg.chat.id, uid)) return false;
  const forwarded = Boolean(msg.forward_from_chat) || msg.forward_origin?.type === "channel";
  if (!hasLink(msg, text) && !forwarded) return false;
  await tgCall("deleteMessage", { chat_id: msg.chat.id, message_id: msg.message_id });
  const key = `${msg.chat.id}:${uid}`;
  const n = (warns.get(key) ?? 0) + 1;
  warns.set(key, n);
  if (n >= 3) {
    await tgCall("restrictChatMember", { chat_id: msg.chat.id, user_id: uid, permissions: { can_send_messages: false }, until_date: Math.floor(Date.now() / 1000) + 86400 });
  } else if (n === 1) {
    await tgCall("sendMessage", {
      chat_id: msg.chat.id,
      message_thread_id: msg.message_thread_id,
      text: `⚠️ ${escHtml(nameOf(msg.from!))}：新成员入群 24 小时内不能发链接或转发，消息已删除。`,
      parse_mode: "HTML",
    });
  }
  await trackTg("chatx_group_spam", { bot: botId(), chat: msg.chat.id, uid, n });
  return true;
}

// ── 管理员命令（回复某条消息使用）──────────────────────────────────

async function moderation(msg: GroupMsg, text: string): Promise<boolean> {
  const m = text.match(/^\/(ban|mute|unmute)(?:@\w+)?(?:\s+(\d+))?/i);
  if (!m) return false;
  if (!(await isAdmin(msg.chat.id, msg))) return true;
  const target = msg.reply_to_message?.from;
  if (!target || target.is_bot) {
    await sendText(msg.chat.id, "用法：回复要处理的那条消息，再发 /ban、/mute [小时] 或 /unmute");
    return true;
  }
  const cmd = m[1].toLowerCase();
  if (cmd === "ban") {
    await tgCall("banChatMember", { chat_id: msg.chat.id, user_id: target.id });
    await tgCall("deleteMessage", { chat_id: msg.chat.id, message_id: msg.reply_to_message!.message_id });
  } else if (cmd === "mute") {
    const hours = Math.min(Math.max(Number(m[2]) || 24, 1), 720);
    await tgCall("restrictChatMember", { chat_id: msg.chat.id, user_id: target.id, permissions: { can_send_messages: false }, until_date: Math.floor(Date.now() / 1000) + hours * 3600 });
  } else {
    const chat = await tgCall("getChat", { chat_id: msg.chat.id });
    const perms = (chat?.result as { permissions?: Record<string, boolean> } | undefined)?.permissions ?? FULL_PERMS;
    await tgCall("restrictChatMember", { chat_id: msg.chat.id, user_id: target.id, permissions: perms });
  }
  await tgCall("deleteMessage", { chat_id: msg.chat.id, message_id: msg.message_id });
  await trackTg("chatx_group_mod", { bot: botId(), chat: msg.chat.id, cmd, target: target.id, by: msg.from?.id });
  return true;
}

// ── 群内 AI 简答 ───────────────────────────────────────────────────

function addressedText(msg: GroupMsg, text: string): string | null {
  const uname = botUsername().toLowerCase();
  const ask = text.match(/^\/ask(?:@\w+)?\s+([\s\S]+)/i);
  if (ask) return ask[1].trim();
  if (uname && text.toLowerCase().includes(`@${uname}`)) return text.replace(new RegExp(`@${uname}`, "ig"), "").trim();
  const r = msg.reply_to_message?.from;
  if (r?.is_bot && r.username?.toLowerCase() === uname) return text.trim();
  return null;
}

async function groupAnswer(msg: GroupMsg, q: string) {
  const now = Date.now();
  const gk = String(msg.chat.id);
  const uk = `${msg.chat.id}:${msg.from!.id}`;
  if (now - (lastGroupReply.get(gk) ?? 0) < GROUP_COOLDOWN_MS || now - (lastUserAsk.get(uk) ?? 0) < USER_COOLDOWN_MS) return;
  lastGroupReply.set(gk, now);
  lastUserAsk.set(uk, now);
  const lang = detectKnowledgeLang(q);
  const rule =
    lang === "zh"
      ? "你在 Telegram 公开群里回答。回答控制在 3 句以内；不要索要或复述机器码、手机号、账号等隐私；需要个人排查时请对方点私聊按钮。"
      : "You are answering in a public Telegram group. Keep it within 3 sentences; never ask for or repeat machine codes, phone numbers or account details; for personal troubleshooting ask them to message you privately.";
  const ans = await askDeepSeek(q, lang, [], 12000, `${chatxSystemHint("tg_group", lang)}\n\n${rule}`);
  const text = ans ?? (lang === "zh" ? "这个问题我私聊帮你看更方便，点下面按钮找我 👇" : "Easier to sort out in private — tap below 👇");
  await tgCall("sendMessage", {
    chat_id: msg.chat.id,
    message_thread_id: msg.message_thread_id,
    reply_parameters: { message_id: msg.message_id, allow_sending_without_reply: true },
    text: text.slice(0, 3500),
    disable_web_page_preview: true,
    reply_markup: { inline_keyboard: [[{ text: lang === "zh" ? "💬 私聊继续" : "💬 Continue privately", url: privateLink("grp") }]] },
  });
  await trackTg("chatx_group_ask", { bot: botId(), chat: msg.chat.id, uid: msg.from!.id, ai: Boolean(ans) });
}

/** 已启用群里的一条消息。chat 为控制台里的配置。 */
export async function handleGroupMessage(chat: HubChat, msg: GroupMsg, edited = false) {
  if (chat.role === "support") {
    if (!edited) await handleAgentMessage(chat.chatId, msg);
    return;
  }
  if (msg.new_chat_members?.length && !edited) {
    await welcome(chat, msg);
    return;
  }
  if (!msg.from || msg.from.is_bot) return;
  const text = msg.text ?? msg.caption ?? "";
  const lang: BotLang = detectKnowledgeLang(text) === "zh" || msg.from.language_code?.startsWith("zh") ? "zh" : "en";
  if (await moderation(msg, text)) return;
  const admin = msg.sender_chat?.id === msg.chat.id;
  if (chat.features.privacyGuard && text && (await privacyGuard(msg, text, lang))) return;
  if (chat.features.antispam && !admin && (await antispam(msg, text))) return;
  if (edited || !chat.features.ai || !text) return;
  const q = addressedText(msg, text);
  if (q) await groupAnswer(msg, q);
}

// ── 入群 / 频道申请 ────────────────────────────────────────────────

export type JoinRequest = {
  chat: { id: number; title?: string; type?: string };
  from: TgFrom;
  user_chat_id?: number;
  invite_link?: { name?: string; invite_link?: string };
};

/** 自动批准，并按邀请链接名归因（邀请链接名填来源码，如 ad_biz_voice01）。 */
export async function handleJoinRequest(chat: HubChat, req: JoinRequest) {
  const src = req.invite_link?.name ? normalizeSrc(req.invite_link.name) : "join";
  await tgCall("approveChatJoinRequest", { chat_id: req.chat.id, user_id: req.from.id });
  const zh = req.from.language_code?.startsWith("zh") !== false;
  await sendText(
    req.user_chat_id ?? req.from.id,
    zh
      ? `👋 已通过你加入「${escHtml(chat.title)}」的申请！\nChatX 是 Telegram 的 AI 翻译 + 自动回复助手，点下面按钮免费体验，有问题直接在这里问我。`
      : `👋 You've been approved to join “${escHtml(chat.title)}”!\nChatX is an AI translation + auto-reply assistant for Telegram — tap below to try it free, and ask me anything right here.`,
    [[{ text: zh ? "🚀 免费体验 ChatX" : "🚀 Try ChatX free", url: privateLink(src) }]]
  );
  await trackTg("chatx_join_request", { bot: botId(), chat: req.chat.id, uid: req.from.id, src });
}
