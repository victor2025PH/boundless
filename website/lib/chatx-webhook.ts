/**
 * ChatX 系 bot 的 update 分发（内置 @ctx2026_bot 与控制台里登记的 bot 共用）。
 * 调用方负责鉴权（secret_token）并用 withBot() 设好当前 bot。
 */
import { detectLang, type BotLang } from "./bot-knowledge";
import {
  answerCallback,
  handleCallback,
  handleContact,
  handleNonText,
  handleOther,
  handleStart,
  handleVoiceMessage,
  lastSrc,
  sendText,
  type NonTextKind,
  type TgFrom,
  type TgVoice,
} from "./chatx-bot";
import { optInAndSend, optOut } from "./chatx-push";
import { handleSupportCallback, handleSupportText, mirrorUserMessage, startBug, supportHuman, type RelayMsg } from "./chatx-support";
import { handleGroupMessage, handleJoinRequest, handleMyChatMember, handleVerifyCallback, type GroupMsg, type JoinRequest } from "./chatx-group";
import { BUILTIN_BOT_ID, currentBot } from "./tg-bot-context";
import { findChat, loadHub, recordChatMembership } from "./tg-hub-store";
import { createUpdateDedup } from "./tg-dedup";
import { trackTg } from "./tg-events";

type TgChat = { id: number; type?: string; title?: string; username?: string; is_forum?: boolean };

export type TgMessage = GroupMsg & {
  chat: TgChat;
  text?: string;
  contact?: { phone_number?: string; first_name?: string; user_id?: number };
  voice?: TgVoice & { file_id: string };
  video_note?: unknown;
  sticker?: unknown;
  location?: unknown;
  left_chat_member?: unknown;
};

export type TgUpdate = {
  update_id?: number;
  message?: TgMessage;
  edited_message?: TgMessage;
  channel_post?: { chat: TgChat };
  callback_query?: {
    id: string;
    data?: string;
    message?: { message_id?: number; chat: TgChat };
    from?: TgFrom;
  };
  my_chat_member?: { chat: TgChat; from?: TgFrom; new_chat_member?: { status?: string } };
  chat_join_request?: JoinRequest & { chat: TgChat };
};

const dedups = new Map<string, (id: unknown) => boolean>();

/** 每个 bot 的 update_id 各自递增，去重要分 bot。 */
export function isDuplicateUpdate(update: TgUpdate): boolean {
  const id = currentBot()?.id ?? BUILTIN_BOT_ID;
  let d = dedups.get(id);
  if (!d) {
    d = createUpdateDedup();
    dedups.set(id, d);
  }
  return d(update.update_id);
}

const NON_TEXT_KINDS = ["voice", "audio", "photo", "video", "video_note", "document", "sticker", "location"] as const satisfies readonly NonTextKind[];

function nonTextKind(msg: TgMessage): NonTextKind | null {
  if (msg.new_chat_members || msg.left_chat_member) return null;
  for (const k of NON_TEXT_KINDS) if (msg[k]) return k;
  return "other";
}

/** /stop 或「🔕 不再推送」：只关早报，对话与其他功能照常。 */
async function stopPush(chatId: number, from: TgFrom, lang: BotLang) {
  const src = await lastSrc(from.id);
  await optOut(from.id);
  await trackTg("chatx_bot_cmd", { cmd: "stop", src, uid: from.id });
  await sendText(
    chatId,
    lang === "zh"
      ? "🔕 已关闭每日早报，不再主动打扰。想恢复随时发 /news；有问题直接发消息给我，照常回你。"
      : "🔕 Daily digest turned off. Send /news any time to turn it back on; you can still message me whenever you like."
  );
}

const discovered = (c: TgChat) => ({ id: c.id, title: c.title, username: c.username, type: c.type ?? "group", is_forum: c.is_forum });

export async function handleChatxUpdate(update: TgUpdate): Promise<void> {
  const botId = currentBot()?.id ?? BUILTIN_BOT_ID;

  if (update.my_chat_member) {
    const m = update.my_chat_member;
    await handleMyChatMember(discovered(m.chat), m.new_chat_member?.status ?? "member", m.from);
    return;
  }

  if (update.chat_join_request) {
    const r = update.chat_join_request;
    const hub = await loadHub();
    const c = findChat(hub, botId, r.chat.id);
    if (c?.enabled && c.features.joinApprove) await handleJoinRequest(c, r);
    return;
  }

  if (update.channel_post) {
    const hub = await loadHub();
    if (!findChat(hub, botId, update.channel_post.chat.id)) await recordChatMembership(botId, discovered(update.channel_post.chat), "administrator");
    return;
  }

  if (update.callback_query) {
    const cq = update.callback_query;
    const chat = cq.message?.chat;
    if (!chat || !cq.from || !cq.data) return;
    const from = cq.from;
    const lang = detectLang(from.language_code);
    if (await handleVerifyCallback({ id: cq.id, data: cq.data, from, chatId: chat.id, messageId: cq.message?.message_id })) return;
    if (await handleSupportCallback({ id: cq.id, data: cq.data, from, chatId: chat.id, chatType: chat.type }, lang)) return;
    if ((chat.type ?? "private") !== "private") {
      await answerCallback(cq.id);
      return;
    }
    if (cq.data === "cx_push_off") {
      await answerCallback(cq.id);
      await stopPush(chat.id, from, lang);
    } else if (cq.data === "cx_human") {
      await answerCallback(cq.id);
      await trackTg("chatx_bot_cta", { cta: "human", src: await lastSrc(from.id), uid: from.id });
      await supportHuman(chat.id, from, lang);
    } else {
      await handleCallback(chat.id, cq.id, cq.data, from, lang);
    }
    return;
  }

  const edited = !update.message && Boolean(update.edited_message);
  const msg = update.message ?? update.edited_message;
  if (!msg?.chat?.id) return;

  if ((msg.chat.type ?? "private") !== "private") {
    const hub = await loadHub();
    const c = findChat(hub, botId, msg.chat.id);
    if (c?.enabled) await handleGroupMessage(c, msg, edited);
    return;
  }
  if (edited || !msg.from) return;

  const lang = detectLang(msg.from.language_code);
  const from: TgFrom = msg.from;
  const chatId = msg.chat.id;
  const text = msg.text?.trim() ?? "";
  const isCommand = text.startsWith("/");

  if (!isCommand) {
    const mirrored = await mirrorUserMessage(from, msg as RelayMsg);
    if (mirrored === "claimed") return;
    if (mirrored === "open" && !text && !msg.voice) {
      await sendText(chatId, lang === "zh" ? "📎 已转给客服，稍等回复。" : "📎 Forwarded to support — they'll reply here.");
      return;
    }
  }

  if (!text) {
    if (msg.contact) await handleContact(chatId, from, msg.contact, lang);
    else if (msg.voice?.file_id) await handleVoiceMessage(chatId, from, msg.voice, lang);
    else {
      const kind = nonTextKind(msg);
      if (kind) await handleNonText(chatId, from, kind, lang);
    }
    return;
  }

  if (/^\/start(?:@\w+)?\s+bug\b/i.test(text) || /^\/(bug|report)\b/i.test(text)) {
    await startBug(chatId, from, lang);
  } else if (/^\/(human|support|kefu)\b/i.test(text)) {
    await supportHuman(chatId, from, lang);
  } else if (/^\/start\b/i.test(text)) {
    await handleStart(chatId, from, text, lang);
  } else if (/^\/(stop|unsubscribe)\b/i.test(text)) {
    await stopPush(chatId, from, lang);
  } else if (/^\/news\b/i.test(text)) {
    const src = await lastSrc(from.id);
    await trackTg("chatx_bot_cmd", { cmd: "news", src, uid: from.id });
    await optInAndSend(from.id, src, lang);
  } else if (!(await handleSupportText(chatId, from, text, lang))) {
    await handleOther(chatId, from, text, lang);
  }
}
