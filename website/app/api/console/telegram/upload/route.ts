// /api/console/telegram/upload —— 定时帖配图上传（admin+）：POST 原始图片字节，返回 { photo: "media:<文件名>" }，排期时作为 photo 传回。
import { NextRequest, NextResponse } from "next/server";
import { getConsoleUser } from "@/lib/console-auth";
import { roleAtLeast } from "@/lib/console-users";
import { MEDIA_MAX_BYTES, MediaError, saveMedia } from "@/lib/tg-post-media";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  const user = getConsoleUser(req);
  if (!user) return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  if (!roleAtLeast(user.role, "admin")) return NextResponse.json({ error: "forbidden: admin role required" }, { status: 403 });
  if (Number(req.headers.get("content-length") || 0) > MEDIA_MAX_BYTES) return NextResponse.json({ error: "图片最大 5MB" }, { status: 413 });
  try {
    const photo = await saveMedia(Buffer.from(await req.arrayBuffer()));
    return NextResponse.json({ ok: true, photo });
  } catch (e) {
    if (e instanceof MediaError) return NextResponse.json({ error: e.message }, { status: 400 });
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
