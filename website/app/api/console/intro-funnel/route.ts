// GET /api/console/intro-funnel?days=7 —— 开场页转化漏斗（events.jsonl 只读聚合）。
// 鉴权与 /api/console/stats 同款：requireConsole（x-console-key 头或 console_session 会话）。
import { NextRequest, NextResponse } from "next/server";
import { requireConsole } from "@/lib/console-auth";
import { readIntroFunnel } from "@/lib/intro-funnel";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  if (!requireConsole(req)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  try {
    const days = Number(req.nextUrl.searchParams.get("days") ?? "7");
    return NextResponse.json({ ok: true, funnel: await readIntroFunnel(days) });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
