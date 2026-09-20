import { NextRequest, NextResponse } from "next/server";
import { requireAdmin } from "@/lib/admin-auth";
import { addExtraVoucher } from "@/lib/trial-claim-store";
import {
  listBonusDue,
  listDueRewards,
  listReferrals,
  markBonusRewarded,
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
    // bonus_due（里程碑/首充返利，实施50 P1）与 due 同响应下发：老履约脚本不认识
    // 该字段会自然忽略（前向兼容）；新脚本对旧官网 .get 不到也安全退化。
    const [due, bonusDue] = await Promise.all([listDueRewards(limit), listBonusDue(limit)]);
    return NextResponse.json({ ok: true, stats, due, bonus_due: bonusDue });
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

  // ── 加码发奖回填（里程碑/首充返利；实施50 P1）：{bonus:[{key,claim_id,ref,voucher,chars,note}]}
  // 先挂凭证再标记（与下方 rewarded 同一顺序纪律——反过来会出现「标了但凭证没挂上」黑洞）。
  if (Array.isArray(data?.bonus)) {
    const results: Array<{ key: string; attached: boolean; marked: boolean }> = [];
    for (const b of data.bonus.slice(0, 50)) {
      const key = String(b?.key || "").trim();
      const claimId = String(b?.claim_id || "").trim();
      const voucher = String(b?.voucher || "");
      const chars = Number(b?.chars) || 0;
      if (!key || !claimId || !voucher || chars <= 0) {
        results.push({ key, attached: false, marked: false });
        continue;
      }
      const attached = await addExtraVoucher(claimId, {
        ref: String(b?.ref || key),
        voucher,
        chars,
        note: String(b?.note || "referral-bonus"),
      });
      const marked = attached
        ? await markBonusRewarded(key, chars, String(b?.note || ""))
        : false;
      results.push({ key, attached: !!attached, marked });
    }
    return NextResponse.json({ ok: true, bonus: results });
  }

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
