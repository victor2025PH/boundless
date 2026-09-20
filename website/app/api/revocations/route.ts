import { NextRequest, NextResponse } from "next/server";
import { requireAdmin } from "@/lib/admin-auth";
import { readCrlDoc, writeCrlDoc } from "@/lib/license-crl";
import { clientIp, rateOk } from "@/lib/rate-limit";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// 吊销名单分发（CRL，2026-08-05 落地）：
// GET 公开——产品周期拉取（客户端用内置公钥验签 + updated 单调防回滚，伪造/回灌旧单都无效）；
// 名单缺失时回「空名单结构」而非 404：客户端 fail-safe 视为无吊销，能力探测也能确认端点存在。
// POST 仅厂商（ADMIN_KEY / setup key）——厂商机 license_admin.py revoke 改完名单后
// 用 push_revocations.py 推最新已签名单上来。

export async function GET() {
  const doc = await readCrlDoc();
  return NextResponse.json(doc ?? { payload: { v: 1, updated: 0, revoked: [] }, sig: "" });
}

export async function POST(req: NextRequest) {
  if (!rateOk("crlpush:" + clientIp(req), 10)) {
    return NextResponse.json({ ok: false, error: "too many" }, { status: 429 });
  }
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  let doc: unknown = null;
  try {
    doc = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "bad json" }, { status: 400 });
  }
  const d = doc as { payload?: { updated?: number; revoked?: unknown[] }; sig?: string };
  if (!d || typeof d !== "object" || !d.payload || typeof d.sig !== "string"
      || !Array.isArray(d.payload.revoked)) {
    return NextResponse.json(
      { ok: false, error: "结构不完整（需 {payload:{updated,revoked[]},sig:<hex>}）。" },
      { status: 400 },
    );
  }
  await writeCrlDoc(d);
  return NextResponse.json({
    ok: true,
    updated: Number(d.payload.updated || 0),
    count: d.payload.revoked.length,
  });
}
