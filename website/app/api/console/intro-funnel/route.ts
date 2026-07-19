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
    // include_bots=1：带回自动化流量（默认排除 HeadlessChrome/爬虫，防污染实验读数）
    const includeBots = req.nextUrl.searchParams.get("include_bots") === "1";
    return NextResponse.json({ ok: true, funnel: await readIntroFunnel(days, { includeBots }) });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}
