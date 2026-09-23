import { NextRequest, NextResponse } from "next/server";
import { detectLang } from "@/lib/bot-knowledge";
import { chatxBotConfigured, handleCallback, handleOther, handleStart, type TgFrom } from "@/lib/chatx-bot";
import { createUpdateDedup } from "@/lib/tg-dedup";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** @ChatX_bot 的 webhook：只处理私聊（广告落地），群/频道消息一律忽略。 */

type TgUpdate = {
  update_id?: number;
  message?: {
    chat: { id: number; type?: string };
    text?: string;
    from?: { id: number; language_code?: string; username?: string; first_name?: string };
  };
  callback_query?: {
    id: string;
    data?: string;
    message?: { chat: { id: number } };
    from?: { id: number; language_code?: string; username?: string; first_name?: string };
  };
};

const isDuplicateUpdate = createUpdateDedup();

export async function POST(req: NextRequest) {
  if (!chatxBotConfigured()) return NextResponse.json({ ok: true });

  const secret = process.env.CHATX_WEBHOOK_SECRET;
  if (secret && req.headers.get("x-telegram-bot-api-secret-token") !== secret) {
    return NextResponse.json({ ok: false }, { status: 403 });
  }

  let update: TgUpdate;
  try {
    update = await req.json();
  } catch {
    return NextResponse.json({ ok: true });
  }
  if (isDuplicateUpdate(update.update_id)) return NextResponse.json({ ok: true });

  try {
    if (update.callback_query) {
      const cq = update.callback_query;
      const chatId = cq.message?.chat.id;
      if (chatId && cq.from && cq.data) {
        const from: TgFrom = cq.from;
        await handleCallback(chatId, cq.id, cq.data, from, detectLang(cq.from.language_code));
      }
      return NextResponse.json({ ok: true });
    }

    const msg = update.message;
    if (!msg?.text || !msg.chat?.id || !msg.from) return NextResponse.json({ ok: true });
    if ((msg.chat.type ?? "private") !== "private") return NextResponse.json({ ok: true });

    const text = msg.text.trim();
    const lang = detectLang(msg.from.language_code);
    const from: TgFrom = msg.from;
    if (/^\/start\b/i.test(text)) {
      await handleStart(msg.chat.id, from, text, lang);
    } else {
      await handleOther(msg.chat.id, from, text, lang);
    }
  } catch {
    /* never fail webhook — TG will retry */
  }
  return NextResponse.json({ ok: true });
}
