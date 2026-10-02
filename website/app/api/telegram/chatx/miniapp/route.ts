import { NextRequest, NextResponse } from "next/server";
import { verifyInitData } from "@/lib/verify-initdata";
import { lastSrc } from "@/lib/chatx-bot";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * ChatX 小程序回源：小程序从菜单键/无参链接打开时 URL 上没有 src，
 * 用 Telegram initData 校验身份后，查该用户最近一次 /start 的来源码补回归因。
 */
export async function POST(req: NextRequest) {
  const token = process.env.CHATX_BOT_TOKEN;
  if (!token) return NextResponse.json({ ok: false, error: "not_configured" }, { status: 503 });

  let initData = "";
  try {
    const body = (await req.json()) as { initData?: unknown };
    initData = typeof body?.initData === "string" ? body.initData : "";
  } catch {
    return NextResponse.json({ ok: false, error: "bad_request" }, { status: 400 });
  }

  const v = verifyInitData(initData, token);
  if (!v.ok || !v.userId) return NextResponse.json({ ok: false, error: "invalid_initdata" }, { status: 401 });

  const src = await lastSrc(v.userId);
  return NextResponse.json({ ok: true, src });
}
