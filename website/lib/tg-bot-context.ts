/**
 * 当前处理中的 Telegram bot 身份（token / id / 用户名）。
 * 内置 bot（env CHATX_BOT_TOKEN）是默认值；控制台登记的其它 bot 在各自 webhook 里用 withBot 包住整个处理流程，
 * chatx-bot 的所有 Bot API 调用都走当前身份，一套业务逻辑可以服务多个 bot。
 */
import { AsyncLocalStorage } from "async_hooks";

export type BotCtx = { id: string; token: string; username?: string };

export const BUILTIN_BOT_ID = "chatx";

const als = new AsyncLocalStorage<BotCtx>();

export function builtinBot(): BotCtx | null {
  const token = process.env.CHATX_BOT_TOKEN;
  return token ? { id: BUILTIN_BOT_ID, token, username: process.env.NEXT_PUBLIC_CHATX_BOT_HANDLE || "ctx2026_bot" } : null;
}

export function currentBot(): BotCtx | null {
  return als.getStore() ?? builtinBot();
}

export function withBot<T>(bot: BotCtx, fn: () => Promise<T>): Promise<T> {
  return als.run(bot, fn);
}
