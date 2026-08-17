import { NextResponse } from "next/server";
import { referralAggregate } from "@/lib/referral-store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * GET /api/trial/referral-stats —— 邀请裂变全局聚合（纯计数，零 PII，公开可读）。
 *
 * 消费方：厂商 ops 看板「邀请裂变」卡（经实例后端 TTL 代理）。数字都是全站聚合
 * 计数（发码数/归因数/达标数/发奖数），不含任何联系方式、指纹或凭证。
 */
export async function GET() {
  try {
    const agg = await referralAggregate();
    return NextResponse.json({ ok: true, ...agg });
  } catch {
    return NextResponse.json({ ok: false, error: "server_error" }, { status: 500 });
  }
}
