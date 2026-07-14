import { NextRequest, NextResponse } from "next/server";
import { requireAdmin } from "@/lib/admin-auth";
import { notifyAdmins } from "@/lib/order-store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// 发布机/运维脚本 → 管理员 Telegram 私信的通用通道（发布结果、素材告急等）。
export async function POST(req: NextRequest) {
  if (!requireAdmin(req)) return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  let text = "";
  try {
    text = String((await req.json())?.text || "").trim();
  } catch {
    /* fallthrough */
  }
  if (!text) return NextResponse.json({ ok: false, error: "text required" }, { status: 400 });
  await notifyAdmins(text.slice(0, 3800));
  return NextResponse.json({ ok: true });
}
