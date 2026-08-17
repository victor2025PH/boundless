import { NextRequest, NextResponse } from "next/server";
import { getConsoleUser, requireRole } from "@/lib/console-auth";
import { approveReferral } from "@/lib/referral-store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * POST /api/console/referral-approve {id} —— 客服人审：放行一条被标 flagged 的邀请。
 *
 * flagged 来自防刷启发（同邀请人名下多个被邀请人共用出口 IP 等）；启发式必然有
 * 误伤（宿舍/公司同网段装机是真实场景），所以出口是人审而不是直接拒绝。
 * 放行后回到正常状态机：被邀请人消耗已达标则直接 qualified，等厂商机下一轮发奖。
 *
 * 鉴权：console `admin` 角色（与 trial-redeem 同口径）。
 */
export async function POST(req: NextRequest) {
  if (!requireRole(req, "admin")) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const user = getConsoleUser(req);
  const data = await req.json().catch(() => ({}));
  const id = String(data?.id || "").trim();
  if (!id) {
    return NextResponse.json({ ok: false, error: "id_required" }, { status: 400 });
  }
  const rec = await approveReferral(id, user ? `console:${user.username}` : "console");
  if (!rec) {
    return NextResponse.json({ ok: false, error: "not_found" }, { status: 404 });
  }
  return NextResponse.json({ ok: true, id: rec.id, status: rec.status });
}
