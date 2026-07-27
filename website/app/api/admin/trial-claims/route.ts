import { NextRequest, NextResponse } from "next/server";
import { requireAdmin } from "@/lib/admin-auth";
import { claimStats, fulfillClaim, listClaims, touchFulfiller } from "@/lib/trial-claim-store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * 厂商机（私钥本地）与试用台账之间的接口——与订单履约 `/api/admin/orders` +
 * `/api/admin/order-status` 完全同款的模型：
 *
 *   GET  /api/admin/trial-claims?status=pending      → 待签发的试用
 *   GET  /api/admin/trial-claims?needs_topup=1       → 已核销待签加量凭证的
 *   POST /api/admin/trial-claims {id, license?, topup_voucher?, topup_chars?, status?, reason?}
 *
 * 鉴权：`x-setup-key`（ADMIN_KEY / TELEGRAM_SETUP_KEY），与履约脚本既有口径一致。
 *
 * 为什么签发不在这里：Ed25519 私钥永不上 VPS（见 lib/trial-claim-store.ts 顶部说明）。
 * 本端点只做「取待办 / 回填结果」两件事。
 */

export async function GET(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const sp = req.nextUrl.searchParams;
  const needsTopup = sp.get("needs_topup") === "1";
  const statusRaw = String(sp.get("status") || "").trim();
  const status =
    statusRaw === "pending" || statusRaw === "issued" || statusRaw === "rejected"
      ? statusRaw
      : undefined;
  const limit = Number(sp.get("limit") || 100);
  // 取待办 = 心跳。厂商机没活干时也会来问，所以这是判断「签发链还活着吗」的信号；
  // 单独开 /heartbeat 反而要求脚本额外配合，且不如「还能取待办」贴近存活语义。
  await touchFulfiller();
  const claims = await listClaims({ status, needsTopup, limit });
  const s = await claimStats();
  return NextResponse.json({
    ok: true,
    // 线上格式统一 snake_case（与下面 claims 的字段口径一致）；claimStats 的
    // camelCase 只在 TS 内部用，别让两种命名混在同一个响应体里。
    stats: {
      total: s.total,
      pending: s.pending,
      issued: s.issued,
      rejected: s.rejected,
      bind_issued: s.bindIssued,
      bind_redeemed: s.bindRedeemed,
      topup_issued: s.topupIssued,
      fulfiller_last_seen: s.fulfillerLastSeen,
      oldest_pending_min: s.oldestPendingMin,
    },
    // 履约脚本只需要这几样：谁、哪台机、要什么。联系方式给出去便于人工核对异常件。
    claims: claims.map((c) => ({
      id: c.id,
      fingerprint: c.fingerprint,
      contact: c.contact,
      contact_kind: c.contactKind,
      source: c.source || "",
      product: c.product,
      created_at: c.createdAt,
      status: c.status,
      has_license: !!c.license,
      bind_code: c.bindCode || "",
      bind_redeemed_at: c.bindRedeemedAt || "",
      bind_chars: c.bindChars || 0,
      has_topup_voucher: !!c.topupVoucher,
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
  const statusRaw = String(data?.status || "").trim();
  const rec = await fulfillClaim({
    id,
    license: data?.license ? String(data.license) : undefined,
    topupVoucher: data?.topup_voucher ? String(data.topup_voucher) : undefined,
    topupChars: data?.topup_chars ? Number(data.topup_chars) : undefined,
    status:
      statusRaw === "issued" || statusRaw === "rejected" || statusRaw === "pending"
        ? statusRaw
        : undefined,
    reason: data?.reason ? String(data.reason) : undefined,
  });
  if (!rec) {
    return NextResponse.json({ ok: false, error: "not_found" }, { status: 404 });
  }
  return NextResponse.json({
    ok: true,
    id: rec.id,
    status: rec.status,
    has_license: !!rec.license,
    has_topup_voucher: !!rec.topupVoucher,
  });
}
