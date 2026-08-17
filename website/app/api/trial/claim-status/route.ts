import { NextRequest, NextResponse } from "next/server";
import { getClaim, normalizeFingerprint } from "@/lib/trial-claim-store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * GET /api/trial/claim-status?id=<claim_id>[&fingerprint=<fp>] —— 客户端轮询取结果。
 *
 * 返回 `{ ok, status, license?, topup_voucher?, bind_code, bind_redeemed }`。
 *
 * 访问控制：**必须带 claim_id**（16 字节随机，不可枚举）。可选再带 fingerprint 交叉校验——
 * 客户端本来就有，多一层几乎零成本；claim_id 万一从日志/截图泄露，别人也换不出授权，
 * 因为授权本身还绑着机器指纹。
 *
 * 刻意不提供「按指纹查」：指纹是可推测的确定性派生值（同机每次都一样），
 * 拿它当查询凭据等于把授权发放做成了「知道机器码就能领」。
 */
export async function GET(req: NextRequest) {
  try {
    const id = String(req.nextUrl.searchParams.get("id") || "").trim();
    if (!id) {
      return NextResponse.json({ ok: false, error: "id_required" }, { status: 400 });
    }
    const claim = await getClaim(id);
    if (!claim) {
      return NextResponse.json({ ok: false, error: "not_found" }, { status: 404 });
    }
    const fpParam = req.nextUrl.searchParams.get("fingerprint");
    if (fpParam) {
      const fp = normalizeFingerprint(fpParam);
      if (!fp || fp !== claim.fingerprint) {
        // 交叉校验不过：不透露这条 claim 是否存在，统一按 404
        return NextResponse.json({ ok: false, error: "not_found" }, { status: 404 });
      }
    }
    return NextResponse.json({
      ok: true,
      status: claim.status,
      license: claim.license || undefined,
      issued_at: claim.issuedAt || undefined,
      rejected_reason: claim.rejectedReason || undefined,
      bind_code: claim.bindCode || "",
      bind_redeemed: !!claim.bindRedeemedAt,
      topup_voucher: claim.topupVoucher || undefined,
      topup_chars: claim.bindChars || undefined,
      // 追加凭证（邀请见面礼/邀请人奖励等）：客户端按 ref 幂等入账，重复下发无害。
      extra_vouchers: (claim.extraVouchers || []).map((v) => ({
        ref: v.ref,
        voucher: v.voucher,
        chars: v.chars,
        note: v.note || "",
      })),
    });
  } catch {
    return NextResponse.json({ ok: false, error: "server_error" }, { status: 500 });
  }
}
