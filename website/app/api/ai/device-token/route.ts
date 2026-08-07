import { NextRequest, NextResponse } from "next/server";
import { clientIp } from "@/lib/client-ip";
import { getClaimByFingerprint, normalizeFingerprint as claimFp } from "@/lib/trial-claim-store";
import {
  gatewayEnabled,
  logGateway,
  mintDeviceToken,
  normalizeInstanceId,
  publicGatewayBase,
  publicModel,
  quotaSnapshot,
  TOKEN_TTL_SEC,
} from "@/lib/ai-gateway";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * POST /api/ai/device-token
 * body: { fingerprint, product?, instance_id?, source? }
 * → { ok, token, base_url, model, expires_in, exp, quota, instance_id? }
 *
 * 资格闸：指纹必须已在试用台账（/api/trial/claim 留过联系方式）——
 * 防脚本刷随机指纹白嫖，同时每个 AI 试用用户都是可触达线索。
 * AI_GATEWAY_REQUIRE_CLAIM=0 可临时关闸（运维逃生口，默认开）。
 * 真云 Key 永不下发。
 *
 * ``instance_id``（可选）：托管多租户同机时写入令牌 claims.iid，额度按实例隔离；
 * 桌面壳不传则保持机器指纹旧口径。旧客户端忽略本字段回传。
 */

const WINDOW_MS = 10 * 60 * 1000;
const MAX_PER_IP = 30;
const MAX_PER_FP = 10;
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

export async function POST(req: NextRequest) {
  try {
    if (!gatewayEnabled()) {
      return NextResponse.json({ ok: false, error: "gateway_disabled" }, { status: 503 });
    }
    const data = await req.json().catch(() => ({}));
    if (String(data?.hp || "").trim()) {
      return NextResponse.json({ ok: true, token: "", base_url: "" });
    }
    const ip = clientIp(req);
    if (limited(`ip:${ip}`, MAX_PER_IP)) {
      return NextResponse.json({ ok: false, error: "rate_limited" }, { status: 429 });
    }
    // 用台账的严格指纹格式（XXXX-XXXX-XXXX-XXXX）——闸门与去重键同一口径
    const fp = claimFp(String(data?.fingerprint || ""));
    if (!fp) {
      return NextResponse.json({ ok: false, error: "bad_fingerprint" }, { status: 400 });
    }
    if (limited(`fp:${fp}`, MAX_PER_FP)) {
      return NextResponse.json({ ok: false, error: "rate_limited" }, { status: 429 });
    }

    const requireClaim = process.env.AI_GATEWAY_REQUIRE_CLAIM !== "0";
    if (requireClaim) {
      const claim = await getClaimByFingerprint(fp);
      if (!claim) {
        return NextResponse.json({ ok: false, error: "no_claim" }, { status: 403 });
      }
      if (claim.status === "rejected") {
        return NextResponse.json({ ok: false, error: "claim_rejected" }, { status: 403 });
      }
    }

    const iid = normalizeInstanceId(String(data?.instance_id || ""));
    const { token, claims } = mintDeviceToken(fp, Math.floor(Date.now() / 1000), iid || undefined);
    const origin = process.env.NEXT_PUBLIC_SITE_URL || new URL(req.url).origin;
    const quota = await quotaSnapshot(claims);
    void logGateway({ ev: "mint", mid: fp, ip, iid: iid || undefined });

    return NextResponse.json({
      ok: true,
      token,
      base_url: publicGatewayBase(origin),
      model: publicModel(),
      expires_in: TOKEN_TTL_SEC,
      exp: claims.exp,
      quota,
      ...(iid ? { instance_id: iid } : {}),
    });
  } catch {
    return NextResponse.json({ ok: false, error: "server_error" }, { status: 500 });
  }
}
