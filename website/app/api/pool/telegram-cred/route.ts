import { NextRequest, NextResponse } from "next/server";
import { clientIp } from "@/lib/client-ip";
import { getClaimByFingerprint, normalizeFingerprint as claimFp } from "@/lib/trial-claim-store";
import { logGateway, verifyDeviceToken } from "@/lib/ai-gateway";
import { assignCred, poolEnabled, reportInvalid } from "@/lib/tg-cred-pool";
import { notifyAdmins } from "@/lib/order-store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * POST /api/pool/telegram-cred —— 公网 Telegram 凭据派发（用户只登录、不填 ID/Hash）。
 *
 * 鉴权：Bearer 设备令牌（cx.…，与 AI 网关同一枚）。令牌本就 claim-gated 才发得出，
 * 这里再加一道台账校验（双保险，且令牌缺失时也能靠指纹+台账放行——升级用户可能
 * 先有 claim 后有令牌）。
 * body: { fingerprint, invalid_api_id?, tg_direct? }
 * → { ok, api_id, api_hash, name, reused, proxy?, swapped? } | { ok:false, error }
 *
 * ``invalid_api_id``（2026-08-10 API_ID_INVALID 事故闭环）：客户端扫码撞
 * API_ID_INVALID 时把废组举报上来 → 校验举报者确实粘定在该组（防恶意逐组打烊）
 * → 删粘定、本次分配排除该组 → 同组被 ≥N 台不同机器举报即自动隔离停发 + 管理员
 * TG 告警。举报者当场拿到新组，无需重启（客户端配合 hosted_gateway 换发重试）。
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

    // 无感换发：先消化举报（校验粘定归属），再做排除该组的重新分配
    const badId = String((data as { invalid_api_id?: unknown })?.invalid_api_id || "").trim();
    let swapped = false;
    if (badId) {
      const rep = reportInvalid(mid, badId);
      if (rep.accepted) {
        swapped = true;
        void logGateway({ ev: "cred_report", mid, api_id: badId, distinct: rep.distinct, ip });
        if (rep.quarantined) {
          void logGateway({ ev: "cred_quarantine", api_id: badId, distinct: rep.distinct });
          // 隔离是「池在烂」的高置信信号，必须有人立刻知道（探针日巡检是兜底，这里是实时）
          void notifyAdmins(
            `🚨 Telegram 凭据池自动隔离：api_id=${badId} 在 24h 内被 ${rep.distinct} 台不同机器` +
            `举报 API_ID_INVALID，已停止派发。请跑 tg_cred_probe 核实并更换该组（误报可 unquarantine 恢复）。`
          ).catch(() => {});
        }
      }
    }

    // P2-⑨ 智能派发：客户端自报直连可达性（tg_direct）——直连不通的机器软偏好
    // 带出口的组、直连通畅的省着出口容量。缺省/非布尔 = 不偏好（旧客户端零影响）。
    const td = (data as { tg_direct?: unknown })?.tg_direct;
    const preferProxy = td === false ? true : td === true ? false : undefined;

    const res = assignCred(mid, {
      ...(swapped ? { excludeApiId: badId } : {}),
      ...(preferProxy !== undefined ? { preferProxy } : {}),
    });
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
      ...(res.proxy ? { proxy: res.proxy } : {}),
      ...(swapped ? { swapped: true } : {}),
    });
  } catch {
    return NextResponse.json({ ok: false, error: "server_error" }, { status: 500 });
  }
}
