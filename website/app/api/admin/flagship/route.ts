import { NextRequest, NextResponse } from "next/server";
import { readFile, stat } from "fs/promises";
import path from "path";
import { requireAdmin } from "@/lib/admin-auth";
import { pinMessage } from "@/lib/tg-broadcast";
import { TELEGRAM_CHANNEL } from "@/lib/site";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// 旗舰帖发布（品牌片等特制内容）：与日更 feed 广播的区别——
// ① multipart 本地上传（URL 方式 Telegram 只拉 ≤20MB，品牌片 30-45MB 必须走上传，bot 上限 50MB）；
// ② 自定义 caption 与按钮（不套日更模板）；③ 可置顶。
// 视频文件需已 scp 到 /var/www/media/feed（与 feed 同通道）。

const MEDIA_ROOT = process.env.MEDIA_FEED_DIR || "/var/www/media/feed";
const MAX_MB = 49;

interface Btn {
  text: string;
  url: string;
}

export async function POST(req: NextRequest) {
  if (!requireAdmin(req)) return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "bad_json" }, { status: 400 });
  }

  // 只取文件名，锁死在 MEDIA_ROOT 内（admin 接口也不留路径穿越口子）
  const fname = path.basename(String(body.video || ""));
  if (!fname.endsWith(".mp4")) {
    return NextResponse.json({ ok: false, error: "video (.mp4 under /media/feed) required" }, { status: 400 });
  }
  const file = path.join(MEDIA_ROOT, fname);
  const caption = String(body.caption || "").slice(0, 1024);
  const chat = String(body.chat || `@${TELEGRAM_CHANNEL}`);
  const token = process.env.TELEGRAM_BOT_TOKEN;

  let sizeMB = 0;
  try {
    sizeMB = (await stat(file)).size / 1048576;
  } catch {
    return NextResponse.json({ ok: false, error: `file not found: ${fname}` }, { status: 404 });
  }

  if (body.dry) {
    return NextResponse.json({
      ok: true,
      dry: true,
      file: fname,
      sizeMB: Math.round(sizeMB * 10) / 10,
      withinLimit: sizeMB <= MAX_MB,
      hasToken: Boolean(token),
      chat,
      captionLen: caption.length,
    });
  }

  if (!token) return NextResponse.json({ ok: false, error: "no_bot_token" }, { status: 500 });
  if (sizeMB > MAX_MB) {
    return NextResponse.json({ ok: false, error: `file ${sizeMB.toFixed(1)}MB > ${MAX_MB}MB bot limit` }, { status: 400 });
  }
  if (!caption) return NextResponse.json({ ok: false, error: "caption required" }, { status: 400 });

  const rows = Array.isArray(body.buttons) ? (body.buttons as Btn[][]) : [];
  const keyboard = rows
    .map((row) =>
      (Array.isArray(row) ? row : [])
        .filter((b) => b && typeof b.text === "string" && /^https:\/\//.test(String(b.url)))
        .map((b) => ({ text: String(b.text).slice(0, 40), url: String(b.url) }))
    )
    .filter((row) => row.length > 0);

  const buf = await readFile(file);
  const form = new FormData();
  form.append("chat_id", chat);
  form.append("caption", caption);
  form.append("parse_mode", "HTML");
  form.append("supports_streaming", "true");
  if (keyboard.length > 0) form.append("reply_markup", JSON.stringify({ inline_keyboard: keyboard }));
  form.append("video", new Blob([new Uint8Array(buf)]), fname);

  let data: { ok?: boolean; description?: string; result?: { message_id?: number } };
  try {
    const res = await fetch(`https://api.telegram.org/bot${token}/sendVideo`, { method: "POST", body: form });
    data = await res.json();
  } catch (e) {
    return NextResponse.json({ ok: false, error: String(e) }, { status: 502 });
  }
  if (!data?.ok) return NextResponse.json({ ok: false, error: data?.description || "send failed" }, { status: 502 });

  const messageId = data.result?.message_id;
  let pinned = false;
  if (body.pin && messageId) pinned = await pinMessage(chat, messageId, true);

  return NextResponse.json({ ok: true, chat, messageId, pinned, sizeMB: Math.round(sizeMB * 10) / 10 });
}
