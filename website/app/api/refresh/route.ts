import { NextRequest, NextResponse } from "next/server";
import { listOrders } from "@/lib/order-store";
import { isRevokedPayload, readCrlDoc } from "@/lib/license-crl";
import { clientIp, rateOk } from "@/lib/rate-limit";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// 授权在线刷新（《授权在线刷新_对接说明_20260714.md》的官网对端，2026-08-05 落地）：
// 客户端按【机器指纹】POST 来查「当前应得授权」——本端点【绝不签发】，只把厂商机
// （Ed25519 私钥留在本地）经 fulfill_orders.py 早已签好、回填在订单 code 里的授权原样取回。
// 私钥永不上服务器，服务器被攻破也伪造不了授权。
//
// 「最优」口径与客户端单调闸门配套（对接说明 §3）：跳过已吊销（CRL 命中）的授权；
// 取到期最晚（expires=0 永久视为最大）；到期相同再取档位最高。客户端拿到后仍会
// 内置公钥复验签名+校验指纹+「只升不降」，所以此端点最坏结果是「没更新」，绝不能改差。

type LicPayload = { machine?: string; edition?: string; expires?: number; [k: string]: unknown };
type LicDoc = { payload?: LicPayload; sig?: string };

const RANK: Record<string, number> = { trial: 0, standard: 1, pro: 2, enterprise: 2 };

function decodeLicense(code: string): LicDoc | null {
  try {
    const doc = JSON.parse(Buffer.from(code, "base64").toString("utf-8"));
    return doc && typeof doc === "object" && doc.payload && doc.sig ? (doc as LicDoc) : null;
  } catch {
    return null;
  }
}

function effExpiry(e: unknown): number {
  const n = Number(e || 0);
  return n === 0 ? Number.POSITIVE_INFINITY : n;
}

export async function POST(req: NextRequest) {
  if (!rateOk("refresh:" + clientIp(req))) {
    return NextResponse.json({ ok: false, error: "请求过于频繁，请稍后再试。" }, { status: 429 });
  }
  let body: Record<string, unknown> = {};
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "缺少 fingerprint。" }, { status: 400 });
  }
  const fp = String(body?.fingerprint || "").trim().slice(0, 128);
  if (!fp) {
    return NextResponse.json({ ok: false, error: "缺少 fingerprint。" }, { status: 400 });
  }

  const [orders, crl] = await Promise.all([listOrders("activated"), readCrlDoc()]);
  let best: { doc: LicDoc; code: string } | null = null;
  for (const o of orders) {
    if (!o.code) continue;
    const doc = decodeLicense(o.code);
    if (!doc) continue;
    // payload.machine 是签名内的绑定指纹（权威）；"*" 站点授权也认。订单登记指纹只是辅助线索。
    const m = String(doc.payload?.machine || "");
    if (m !== fp && m !== "*") continue;
    if (isRevokedPayload(doc.payload as Record<string, unknown>, crl)) continue;
    if (!best) {
      best = { doc, code: o.id };
      continue;
    }
    const a = doc.payload as LicPayload, b = best.doc.payload as LicPayload;
    if (
      effExpiry(a.expires) > effExpiry(b.expires) ||
      (effExpiry(a.expires) === effExpiry(b.expires) &&
        (RANK[String(a.edition)] ?? -1) > (RANK[String(b.edition)] ?? -1))
    ) {
      best = { doc, code: o.id };
    }
  }
  if (!best) {
    return NextResponse.json(
      { ok: false, error: "该机器无有效授权记录（未激活或已被移除）。" },
      { status: 404 },
    );
  }
  return NextResponse.json({ ok: true, license: best.doc, code: best.code });
}
