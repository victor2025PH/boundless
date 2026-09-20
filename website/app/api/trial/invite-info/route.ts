import { NextRequest, NextResponse } from "next/server";
import { getClaim, normalizeFingerprint } from "@/lib/trial-claim-store";
import {
  getOrCreateInviteCode,
  inviteStats,
  REFERRAL_INVITEE_CHARS,
  REFERRAL_INVITER_CHARS,
  REFERRAL_QUALIFY_CHARS,
} from "@/lib/referral-store";
import { SITE_URL } from "@/lib/site";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * GET /api/trial/invite-info?claim_id=<id>&fingerprint=<fp>
 * —— 客户端会员页「邀请好友送字符」卡片的数据源。
 *
 * 返回 `{ ok, code, share_url, invitee_bonus, inviter_bonus, qualify_chars, stats }`。
 *
 * 访问控制与 claim-status 同款：claim_id（随机不可枚举）+ fingerprint 交叉校验。
 * 邀请码幂等（一 claim 一码，首次调用生成）；分享链接落到下载页带 ?ref= 归因。
 */
export async function GET(req: NextRequest) {
  try {
    const id = String(req.nextUrl.searchParams.get("id") || req.nextUrl.searchParams.get("claim_id") || "").trim();
    if (!id) {
      return NextResponse.json({ ok: false, error: "id_required" }, { status: 400 });
    }
    const claim = await getClaim(id);
    if (!claim) {
      return NextResponse.json({ ok: false, error: "not_found" }, { status: 404 });
    }
    const fp = normalizeFingerprint(req.nextUrl.searchParams.get("fingerprint"));
    if (!fp || fp !== claim.fingerprint) {
      // 交叉校验不过：不透露这条 claim 是否存在，统一按 404（与 claim-status 同口径）
      return NextResponse.json({ ok: false, error: "not_found" }, { status: 404 });
    }
    const code = await getOrCreateInviteCode(claim.id);
    const stats = await inviteStats(claim.id);
    return NextResponse.json({
      ok: true,
      code,
      share_url: `${SITE_URL}/download/chatx?ref=${encodeURIComponent(code)}`,
      invitee_bonus: REFERRAL_INVITEE_CHARS,
      inviter_bonus: REFERRAL_INVITER_CHARS,
      qualify_chars: REFERRAL_QUALIFY_CHARS,
      stats,
    });
  } catch {
    return NextResponse.json({ ok: false, error: "server_error" }, { status: 500 });
  }
}
