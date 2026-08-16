// POST /api/ops/alert —— 运维告警中继（实施26）。
//
// 用途：让持 EVENT_INGEST_KEY 的机器（五机 Windows cron 等）把运维告警经本端点转发到
// Telegram 管理员 chat，而**无需在各机分发 TELEGRAM_BOT_TOKEN**（token 只留 VPS，
// 密钥面最小化）。与 /api/collect 同一把机器密钥鉴权，人类口令体系（CONSOLE_KEY）完全独立。
//
// body: { text: string, source?: string }（text ≤ 1000 字，单条纯文本）。
// 收件人：getAdminChats()（env TELEGRAM_CHAT_ID ∪ admin_chats.json 绑定），与 health-watchdog 同源。
import { NextRequest, NextResponse } from "next/server";
import { getAdminChats } from "@/lib/admin-store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  const key = (process.env.EVENT_INGEST_KEY || "").trim();
  if (!key) {
    return NextResponse.json(
      { error: "not_configured", message: "服务端未配置 EVENT_INGEST_KEY，告警中继不可用。" },
      { status: 503 }
    );
  }
  const auth = req.headers.get("authorization") || "";
  const given = auth.startsWith("Bearer ") ? auth.slice(7).trim() : "";
  if (!given || given !== key) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid_json" }, { status: 400 });
  }
  const rawText = (body as { text?: unknown } | null)?.text;
  const source = String((body as { source?: unknown } | null)?.source ?? "").trim().slice(0, 60);
  const text = typeof rawText === "string" ? rawText.trim().slice(0, 1000) : "";
  if (!text) {
    return NextResponse.json({ error: "invalid_body", message: "需要 { text: 非空字符串 }" }, { status: 400 });
  }

  const token = (process.env.TELEGRAM_BOT_TOKEN || "").trim();
  if (!token) {
    // token 未配 → 明确告知（但不 500，调用方据 ok=false 落日志即可）
    return NextResponse.json({ ok: false, error: "no_bot_token", sent: 0 }, { status: 200 });
  }
  const chats = await getAdminChats();
  if (chats.length === 0) {
    return NextResponse.json({ ok: false, error: "no_recipients", sent: 0 }, { status: 200 });
  }

  const msg = source ? `[${source}] ${text}` : text;
  let sent = 0;
  for (const chat of chats) {
    try {
      const res = await fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ chat_id: chat, text: msg, disable_web_page_preview: true }),
      });
      const data = (await res.json()) as { ok?: boolean };
      if (data?.ok) sent++;
    } catch {
      /* 单个 chat 失败不影响其余 */
    }
  }
  return NextResponse.json({ ok: sent > 0, sent, recipients: chats.length });
}
