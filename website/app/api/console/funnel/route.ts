// GET /api/console/funnel —— 激活漏斗（7/30 天双窗口聚合直出）。
// 数据口径与诚实声明见 lib/activation-funnel.ts 模块头。
import { NextRequest, NextResponse } from "next/server";
import { requireConsole } from "@/lib/console-auth";
import { activationFunnel } from "@/lib/activation-funnel";
import { trialPaidFunnel } from "@/lib/trial-paid-funnel";
import { newbieFunnel } from "@/lib/newbie-funnel";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  if (!requireConsole(req)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  try {
    const [w7, w30, p7, p30, n7, n30] = await Promise.all([
      activationFunnel(7), activationFunnel(30),
      trialPaidFunnel(7), trialPaidFunnel(30),
      newbieFunnel(7), newbieFunnel(30),
    ]);
    return NextResponse.json({ ok: true, windows: [w7, w30],
                               trial_paid: [p7, p30], newbie: [n7, n30] });
  } catch (e) {
    return NextResponse.json(
      { ok: false, error: String((e as Error)?.message || e).slice(0, 200) },
      { status: 500 }
    );
  }
}
