import { NextRequest, NextResponse } from "next/server";
import { requireAdmin } from "@/lib/admin-auth";
import { listFeed, markFeedBroadcast, removeFeedVideo, upsertFeedVideo, type FeedVideo } from "@/lib/feed-store";
import { broadcastVideoToChannel } from "@/lib/tg-broadcast";
import { SITE_URL } from "@/lib/site";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// 每日视频动态上架接口（发布机调用）：
// POST 元数据 → 幂等入库 → 未广播过则自动发 Telegram 频道（带播放视频）。
// 视频文件本体走 scp 到 /var/www/media/feed（nginx 直出），不经过本接口。

function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export async function GET(req: NextRequest) {
  if (!requireAdmin(req)) return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  return NextResponse.json({ ok: true, videos: await listFeed(200) });
}

export async function POST(req: NextRequest) {
  if (!requireAdmin(req)) return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  let body: Record<string, unknown>;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "bad_json" }, { status: 400 });
  }

  const id = String(body.id || "").trim();
  const src = String(body.src || "").trim();
  const titleZh = String(body.title_zh || "").trim();
  if (!id || !src || !titleZh) {
    return NextResponse.json({ ok: false, error: "id/src/title_zh required" }, { status: 400 });
  }
  if (!/^\/(media|videos|showcase)\//.test(src) && !/^https?:\/\//.test(src)) {
    return NextResponse.json({ ok: false, error: "src must be a site path or https url" }, { status: 400 });
  }

  const video: FeedVideo = {
    id,
    date: String(body.date || new Date().toISOString()),
    title: { zh: titleZh, en: String(body.title_en || titleZh) },
    desc: { zh: String(body.desc_zh || ""), en: String(body.desc_en || body.desc_zh || "") },
    src,
    poster: body.poster ? String(body.poster) : undefined,
    youtube: body.youtube ? String(body.youtube) : undefined,
    ai: body.ai === false ? false : true,
  };
  const { created, video: saved } = await upsertFeedVideo(video);

  // 频道广播（幂等：已有回执不重发；broadcast=false 可跳过）
  let tg: { ok: boolean; error?: string; messageId?: number } | null = null;
  if (body.broadcast !== false && !saved.tg?.message_id) {
    const abs = /^https?:\/\//.test(src) ? src : `${SITE_URL}${src}`;
    const caption =
      `🎬 <b>${esc(saved.title.zh)}</b>\n${esc(saved.desc.zh)}` +
      (saved.ai !== false ? "\n\n<i>AI 概念演示 · 实际效果以引擎实测为准</i>" : "") +
      (saved.youtube ? `\n▶️ YouTube: https://youtu.be/${esc(saved.youtube)}` : "");
    tg = await broadcastVideoToChannel({ videoUrl: abs, caption, campaign: `feed-${id}` });
    if (tg.ok && tg.messageId) await markFeedBroadcast(id, tg.messageId);
  }

  return NextResponse.json({ ok: true, created, id, broadcast: tg });
}

export async function DELETE(req: NextRequest) {
  if (!requireAdmin(req)) return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  const id = req.nextUrl.searchParams.get("id") || "";
  if (!id) return NextResponse.json({ ok: false, error: "id required" }, { status: 400 });
  return NextResponse.json({ ok: await removeFeedVideo(id), id });
}
