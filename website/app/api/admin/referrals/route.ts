import { NextRequest, NextResponse } from "next/server";
import { requireAdmin } from "@/lib/admin-auth";
import { addExtraVoucher } from "@/lib/trial-claim-store";
import {
  listDueRewards,
  listReferrals,
  markRewarded,
  referralAggregate,
} from "@/lib/referral-store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * 厂商机（私钥本地）与邀请台账之间的接口——与 `/api/admin/trial-claims` 完全同款模型：
 *
 *   GET  /api/admin/referrals?due=1        → 待发奖清单（见面礼 / 邀请人奖励）
 *   GET  /api/admin/referrals?status=...   → 台账列表（排障用）
 *   POST /api/admin/referrals {id, invitee_voucher?, invitee_ref?, invitee_chars?,
 *                              inviter_voucher?, inviter_ref?, inviter_chars?}
 *        → 凭证挂到双方 claim.extraVouchers（ref 幂等）+ 标记 rewarded
 *
 * 鉴权：`x-setup-key`。签发永远不在这里——本端点只做「取待办 / 回填结果」。
 */

export async function GET(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const sp = req.nextUrl.searchParams;
  const limit = Math.max(1, Math.min(500, Number(sp.get("limit") || 100)));
  const stats = await referralAggregate();
  if (sp.get("due") === "1") {
    const due = await listDueRewards(limit);
    return NextResponse.json({ ok: true, stats, due });
  }
  const statusRaw = String(sp.get("status") || "").trim();
  const status = (["registered", "qualified", "flagged", "rejected"] as const).find(
    (s) => s === statusRaw
  );
  const rows = await listReferrals({ status, limit });
  return NextResponse.json({
    ok: true,
    stats,
    referrals: rows.map((r) => ({
      id: r.id,
      code: r.code,
      inviter_claim_id: r.inviterClaimId,
      invitee_claim_id: r.inviteeClaimId,
      status: r.status,
      created_at: r.createdAt,
      qualified_at: r.qualifiedAt || "",
      flag_reason: r.flagReason || "",
      invitee_rewarded_at: r.inviteeRewardedAt || "",
      inviter_rewarded_at: r.inviterRewardedAt || "",
    })),
  });
}

export async function POST(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const data = await req.json().catch(() => ({}));
  const id = String(data?.id || "").trim();
  if (!id) {
    return NextResponse.json({ ok: false, error: "id_required" }, { status: 400 });
  }
  // 先把凭证挂到对应 claim（ref 幂等），全部成功才标 rewarded——顺序错了会出现
  // 「标了 rewarded 但凭证没挂上」的黑洞（fulfiller 不再重试，用户永远拿不到）。
  const rows = await listReferrals({ limit: 500 });
  const rec = rows.find((r) => r.id === id);
  if (!rec) {
    return NextResponse.json({ ok: false, error: "not_found" }, { status: 404 });
  }
  let inviteeOk = false;
  let inviterOk = false;
  if (data?.invitee_voucher) {
    const attached = await addExtraVoucher(rec.inviteeClaimId, {
      ref: String(data?.invitee_ref || `refwel-${id}`),
      voucher: String(data.invitee_voucher),
      chars: Number(data?.invitee_chars) || 0,
      note: "referral-welcome",
    });
    inviteeOk = !!attached;
  }
  if (data?.inviter_voucher) {
    const attached = await addExtraVoucher(rec.inviterClaimId, {
      ref: String(data?.inviter_ref || `refearn-${id}`),
      voucher: String(data.inviter_voucher),
      chars: Number(data?.inviter_chars) || 0,
      note: "referral-reward",
    });
    inviterOk = !!attached;
  }
  const updated = await markRewarded(id, {
    invitee: inviteeOk,
    inviteeChars: Number(data?.invitee_chars) || 0,
    inviter: inviterOk,
    inviterChars: Number(data?.inviter_chars) || 0,
  });
  return NextResponse.json({
    ok: true,
    id,
    invitee_rewarded: !!updated?.inviteeRewardedAt,
    inviter_rewarded: !!updated?.inviterRewardedAt,
  });
}
