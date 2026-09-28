import { NextRequest, NextResponse } from "next/server";
import { requireAdmin } from "@/lib/admin-auth";
import { buildDailyReport, readEvents, recentDays } from "@/lib/chatx-report";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * ChatX 广告日报：GET /api/admin/chatx-daily?days=7（1–60）
 * 日 × 来源的进人 / 落地 / 点击 / 下载人、对话数、互动时长、来源 IP、推送回话 + 投放诊断。
 * 口径与聚合逻辑全部在 lib/chatx-report.ts（管理员每日 Telegram 日报同一份数据）。
 */
export async function GET(req: NextRequest) {
  if (!process.env.TELEGRAM_SETUP_KEY && !process.env.ADMIN_KEY) {
    return NextResponse.json({ ok: false, error: "not_configured" }, { status: 503 });
  }
  if (!requireAdmin(req)) return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });

  const n = Number(req.nextUrl.searchParams.get("days") ?? "7");
  const days = Number.isFinite(n) && n >= 1 ? Math.min(Math.floor(n), 60) : 7;
  const keys = recentDays(days);
  // 多读 2 天：跨天会话 / 推送次日回话要看得到前后文
  const since = Date.now() - (days + 2) * 86_400_000;
  const events = await readEvents(undefined, since);
  const report = buildDailyReport(events, keys);
  return NextResponse.json({ ok: true, ...report }, { headers: { "Cache-Control": "no-store" } });
}
