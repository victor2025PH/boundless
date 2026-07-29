import { NextRequest, NextResponse } from "next/server";
import { clientIp } from "@/lib/client-ip";
import { getClaimByFingerprint, normalizeFingerprint as claimFp } from "@/lib/trial-claim-store";
import { verifyDeviceToken } from "@/lib/ai-gateway";
import { assignCred, poolEnabled } from "@/lib/tg-cred-pool";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * POST /api/pool/telegram-cred —— 公网 Telegram 凭据派发（用户只登录、不填 ID/Hash）。
 *
 * 鉴权：Bearer 设备令牌（cx.…，与 AI 网关同一枚）。令牌本就 claim-gated 才发得出，
 * 这里再加一道台账校验（双保险，且令牌缺失时也能靠指纹+台账放行——升级用户可能
 * 先有 claim 后有令牌）。
 * body: { fingerprint }
 * → { ok, api_id, api_hash, name } | { ok:false, error }
 *
 * 池未配（POOL_TG_CREDS 空）→ 503 pool_disabled：客户端据此回落「自备凭据」旧流程，
 * 不报错、不阻断（暗态部署零副作用）。
 */

const WINDOW_MS = 10 * 60 * 1000;
const MAX_PER_IP = 40;
const MAX_PER_FP = 15;
const hits = new Map<string, number[]>();

function limited(key: string, max: number): boolean {
  const now = Date.now();
  const arr = (hits.get(key) || []).filter((t) => now - t < WINDOW_MS);
  arr.push(now);
  hits.set(key, arr);
  if (hits.size > 8000) {
    for (const [k, v] of hits) {
      if (!v.length || now - v[v.length - 1]! > WINDOW_MS) hits.delete(k);
    }
  }
  return arr.length > max;
}

function bearer(req: NextRequest): string {
  const h = req.headers.get("authorization") || "";
  const m = /^Bearer\s+(.+)$/i.exec(h.trim());
  return m ? m[1].trim() : "";
}

export async function POST(req: NextRequest) {
  try {
    if (!poolEnabled()) {
      return NextResponse.json({ ok: false, error: "pool_disabled" }, { status: 503 });
    }
    const data = await req.json().catch(() => ({}));
    const ip = clientIp(req);
    if (limited(`ip:${ip}`, MAX_PER_IP)) {
      return NextResponse.json({ ok: false, error: "rate_limited" }, { status: 429 });
    }

    // 指纹来源双通道：优先令牌里的 mid（可信），回落 body.fingerprint（需台账背书）
    let mid = "";
    const claims = verifyDeviceToken(bearer(req));
    if (claims) {
      mid = claims.mid;
    } else {
      const fp = claimFp(String(data?.fingerprint || ""));
      if (!fp) return NextResponse.json({ ok: false, error: "bad_fingerprint" }, { status: 400 });
      const claim = await getClaimByFingerprint(fp);
      if (!claim || claim.status === "rejected") {
        return NextResponse.json({ ok: false, error: "no_claim" }, { status: 403 });
      }
      mid = fp;
    }
    if (limited(`fp:${mid}`, MAX_PER_FP)) {
      return NextResponse.json({ ok: false, error: "rate_limited" }, { status: 429 });
    }

    const res = assignCred(mid);
    if (!res.ok) {
      const status = res.error === "pool_full" ? 503 : res.error === "bad_fingerprint" ? 400 : 503;
      return NextResponse.json({ ok: false, error: res.error }, { status });
    }
    return NextResponse.json({
      ok: true,
      api_id: res.api_id,
      api_hash: res.api_hash,
      name: res.name,
      reused: res.reused,
    });
  } catch {
    return NextResponse.json({ ok: false, error: "server_error" }, { status: 500 });
  }
}
