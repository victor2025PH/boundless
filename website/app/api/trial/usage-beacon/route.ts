import { NextRequest, NextResponse } from "next/server";
import { getClaim, normalizeFingerprint, recordUsage } from "@/lib/trial-claim-store";
import { noteInviteeUsage } from "@/lib/referral-store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * POST /api/trial/usage-beacon —— 客户端节流上报本机累计消耗字符（水位，单调只增）。
 *
 * body: `{ claim_id, fingerprint, used_chars }` → `{ ok }`
 *
 * 它是邀请裂变「达标门」的唯一数据源：被邀请人真实消耗 ≥ 阈值才给邀请人发奖。
 * 客户端已按 ≥1000 字符增量 + ≥10 分钟节流；这里再做轻量滑动窗限流兜底。
 * 访问控制与 claim-status 同款（claim_id + fingerprint 交叉校验）；水位只增不减，
 * 谎报大数只会提前触发一次达标判定——阈值本身就是防刷参数，无需更重的防护。
 */

const WINDOW_MS = 60 * 1000;
const MAX_PER_CLAIM = 6;
const hits = new Map<string, number[]>();

function limited(key: string): boolean {
  const now = Date.now();
  const arr = (hits.get(key) || []).filter((t) => now - t < WINDOW_MS);
  arr.push(now);
  hits.set(key, arr);
  if (hits.size > 5000) {
    for (const [k, v] of hits) {
      if (!v.length || now - v[v.length - 1] > WINDOW_MS) hits.delete(k);
    }
  }
  return arr.length > MAX_PER_CLAIM;
}

export async function POST(req: NextRequest) {
  try {
    const data = await req.json().catch(() => ({}));
    const id = String(data?.claim_id || "").trim();
    const used = Math.max(0, Math.round(Number(data?.used_chars) || 0));
    if (!id || used <= 0) {
      return NextResponse.json({ ok: false, error: "bad_request" }, { status: 400 });
    }
    if (limited(id)) {
      return NextResponse.json({ ok: false, error: "rate_limited" }, { status: 429 });
    }
    const claim = await getClaim(id);
    if (!claim) {
      return NextResponse.json({ ok: false, error: "not_found" }, { status: 404 });
    }
    const fp = normalizeFingerprint(data?.fingerprint);
    if (!fp || fp !== claim.fingerprint) {
      return NextResponse.json({ ok: false, error: "not_found" }, { status: 404 });
    }
    await recordUsage(id, used);
    // 达标推进（该机器是某条邀请的被邀请人时才有效果；其余零副作用）
    await noteInviteeUsage(id, used);
    return NextResponse.json({ ok: true });
  } catch {
    return NextResponse.json({ ok: false, error: "server_error" }, { status: 500 });
  }
}
