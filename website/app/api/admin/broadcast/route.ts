import { NextRequest, NextResponse } from "next/server";
import path from "path";
import { access } from "fs/promises";
import { broadcastMessage, broadcastPhoto, type BroadcastTarget } from "@/lib/tg-broadcast";
import { requireAdmin } from "@/lib/admin-auth";
import { recordPublish } from "@/lib/publish-log";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const authorized = requireAdmin;

export async function POST(req: NextRequest) {
  if (!process.env.TELEGRAM_SETUP_KEY) {
    return NextResponse.json({ ok: false, error: "not_configured" }, { status: 503 });
  }
  if (!authorized(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  if (!process.env.TELEGRAM_BOT_TOKEN) {
    return NextResponse.json({ ok: false, error: "no_bot_token" }, { status: 503 });
  }

  const body = await req.json().catch(() => null);
  const text = String(body?.text ?? "").trim();
  const target = (String(body?.target ?? "channel") as BroadcastTarget);
  const withButton = body?.withButton !== false;
  if (!text) {
    return NextResponse.json({ ok: false, error: "text_required" }, { status: 400 });
  }

  // campaign 默认按天区分（broadcast-0707）；主题帖可自定义（如 ko-voice-launch）做精准归因。
  const day = new Date().toISOString().slice(5, 10).replace("-", "");
  const campaign =
    String(body?.campaign ?? "").replace(/[^A-Za-z0-9_-]/g, "").slice(0, 40) || `broadcast-${day}`;
  // 官网按钮可深链到特定落地页；仅接受站内路径。
  const rawPath = String(body?.sitePath ?? "");
  const sitePath = rawPath.startsWith("/") ? rawPath.slice(0, 100) : undefined;
  const siteLabel = body?.siteLabel ? String(body.siteLabel).slice(0, 32) : undefined;

  /* 图文帖：photo 仅接受站内 public 相对路径（/brand/...），本地 multipart 上传（比 URL 拉取稳） */
  const rawPhoto = String(body?.photo ?? "");
  let photoFile: string | null = null;
  if (rawPhoto.startsWith("/") && !rawPhoto.includes("..")) {
    const abs = path.join(process.cwd(), "public", rawPhoto);
    try {
      await access(abs);
      photoFile = abs;
    } catch {
      return NextResponse.json({ ok: false, error: "photo_not_found" }, { status: 400 });
    }
  }

  const { ok, results } = photoFile
    ? await broadcastPhoto({ photo: photoFile, caption: text, target, withButton, campaign })
    : await broadcastMessage({ text, target, withButton, campaign, sitePath, siteLabel });
  if (ok) await recordPublish({ kind: "broadcast", target, summary: text, campaign });
  return NextResponse.json({ ok, results });
}
