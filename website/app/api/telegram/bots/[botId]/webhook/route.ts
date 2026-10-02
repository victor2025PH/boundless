import { timingSafeEqual } from "crypto";
import { NextRequest, NextResponse } from "next/server";
import { handleChatxUpdate, isDuplicateUpdate, type TgUpdate } from "@/lib/chatx-webhook";
import { withBot } from "@/lib/tg-bot-context";
import { loadHub } from "@/lib/tg-hub-store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function sameSecret(a: string | null, b: string): boolean {
  if (!a || !b) return false;
  const x = Buffer.from(a);
  const y = Buffer.from(b);
  return x.length === y.length && timingSafeEqual(x, y);
}

/** 控制台「Telegram 运营」里登记的 bot：每个 bot 独立 secret，停用即不处理。 */
export async function POST(req: NextRequest, ctx: { params: { botId: string } }) {
  const { botId } = ctx.params;
  const hub = await loadHub();
  const bot = hub.bots.find((b) => b.id === botId);
  if (!bot || !sameSecret(req.headers.get("x-telegram-bot-api-secret-token"), bot.secret)) {
    return NextResponse.json({ ok: false }, { status: 403 });
  }
  if (!bot.enabled) return NextResponse.json({ ok: true });

  let update: TgUpdate;
  try {
    update = await req.json();
  } catch {
    return NextResponse.json({ ok: true });
  }
  try {
    await withBot({ id: bot.id, token: bot.token, username: bot.username }, async () => {
      if (!isDuplicateUpdate(update)) await handleChatxUpdate(update);
    });
  } catch {
    /* never fail webhook — TG will retry */
  }
  return NextResponse.json({ ok: true });
}
