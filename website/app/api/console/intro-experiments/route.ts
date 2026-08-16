// GET /api/console/intro-experiments?days=7 —— 开场页 A/B 实验读数（按会话连接曝光桶与进入行为）。
// 鉴权与 intro-funnel 同款：requireConsole（x-console-key 头或 console_session 会话）。
import { NextRequest, NextResponse } from "next/server";
import { requireConsole } from "@/lib/console-auth";
import { readIntroExperiments } from "@/lib/intro-funnel";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  if (!requireConsole(req)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  try {
    const days = Number(req.nextUrl.searchParams.get("days") ?? "7");
    const includeBots = req.nextUrl.searchParams.get("include_bots") === "1";
    return NextResponse.json({ ok: true, ...(await readIntroExperiments(days, { includeBots })) });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
