import { NextRequest, NextResponse } from "next/server";
import { setupChatxBot } from "@/lib/chatx-bot";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** One-time / maintenance for @ChatX_bot: register webhook + commands + identity.
 *  Protected by TELEGRAM_SETUP_KEY (header x-setup-key), same as the main bot's /api/telegram/setup.
 *  Body (JSON, optional): { "webhook": false } to refresh identity only without re-pointing the webhook. */
export async function POST(req: NextRequest) {
  const key = process.env.TELEGRAM_SETUP_KEY;
  if (!key) return NextResponse.json({ ok: false, error: "SETUP_KEY not configured" }, { status: 503 });
  if (req.headers.get("x-setup-key") !== key) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  let body: { webhook?: boolean } = {};
  try {
    body = await req.json();
  } catch {
    /* empty body ok */
  }
  const bot = await setupChatxBot({ skipWebhook: body.webhook === false });
  return NextResponse.json({ ok: bot.ok, bot });
}
