// GET /api/console/funnel —— 激活漏斗（7/30 天双窗口聚合直出）。
// 数据口径与诚实声明见 lib/activation-funnel.ts 模块头。
import { NextRequest, NextResponse } from "next/server";
import { requireConsole } from "@/lib/console-auth";
import { activationFunnel } from "@/lib/activation-funnel";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  if (!requireConsole(req)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  try {
    const [w7, w30] = await Promise.all([activationFunnel(7), activationFunnel(30)]);
    return NextResponse.json({ ok: true, windows: [w7, w30] });
  } catch (e) {
    return NextResponse.json(
      { ok: false, error: String((e as Error)?.message || e).slice(0, 200) },
      { status: 500 }
    );
  }
}
