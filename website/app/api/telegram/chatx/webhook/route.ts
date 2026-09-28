import { NextRequest, NextResponse } from "next/server";
import { chatxBotConfigured } from "@/lib/chatx-bot";
import { handleChatxUpdate, isDuplicateUpdate, type TgUpdate } from "@/lib/chatx-webhook";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** 内置 @ctx2026_bot 的 webhook（token = CHATX_BOT_TOKEN）。控制台登记的其它 bot 走 /api/telegram/bots/[botId]/webhook。 */
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
  if (isDuplicateUpdate(update)) return NextResponse.json({ ok: true });

  try {
    await handleChatxUpdate(update);
  } catch {
    /* never fail webhook — TG will retry */
  }
  return NextResponse.json({ ok: true });
}
