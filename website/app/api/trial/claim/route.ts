import { NextRequest, NextResponse } from "next/server";
import { createClaim, issueBindCode } from "@/lib/trial-claim-store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * POST /api/trial/claim —— 客户端注册领 7 天试用。
 *
 * body: `{ fingerprint, contact, source?, product? }`
 * 返回: `{ ok, claim_id, bind_code, status, deduped }`
 *
 * 安全模型（与 /api/activate 一致）：本端点**绝不签发**授权。它只把「谁、哪台机器、
 * 要一份试用」写进台账（pending）；Ed25519 私钥留在厂商机，由履约脚本轮询
 * `/api/admin/trial-claims?status=pending` 本地签好后回填。服务器被攻破也伪造不出授权。
 *
 * 防滥用两道：
 *  ① **按机器指纹幂等**（store 层）——同一台机器永远只有一条 claim。这是唯一真闸门：
 *    客户端那份本地体验档删掉即重置，只有台账拦得住。
 *  ② IP + 指纹双轴滑动窗限流——挡住「脚本批量伪造指纹刷 claim」。
 */

const WINDOW_MS = 10 * 60 * 1000;
const MAX_PER_IP = 12;         // 同一出口 IP 下多台机器装机是正常的（网吧/公司），给宽一点
const MAX_PER_FP = 5;          // 同一台机器十分钟内不该反复来
const hits = new Map<string, number[]>();

function clientIp(req: NextRequest): string {
  const xff = req.headers.get("x-forwarded-for");
  if (xff) return xff.split(",")[0].trim();
  return req.headers.get("x-real-ip") || "unknown";
}

function limited(key: string, max: number): boolean {
  const now = Date.now();
  const arr = (hits.get(key) || []).filter((t) => now - t < WINDOW_MS);
  arr.push(now);
  hits.set(key, arr);
  // 顺手回收：内存表不清会随 IP 数无界增长
  if (hits.size > 5000) {
    for (const [k, v] of hits) {
      if (!v.length || now - v[v.length - 1] > WINDOW_MS) hits.delete(k);
    }
  }
  return arr.length > max;
}

export async function POST(req: NextRequest) {
  try {
    const data = await req.json().catch(() => ({}));
    // 蜜罐（与 /api/order 同款）：真客户端不会带这个字段
    if (String(data?.hp || "").trim()) {
      return NextResponse.json({ ok: true, claim_id: "", status: "pending" });
    }

    const ip = clientIp(req);
    if (limited(`ip:${ip}`, MAX_PER_IP)) {
      return NextResponse.json({ ok: false, error: "rate_limited" },
        { status: 429, headers: { "Retry-After": "600" } });
    }
    const fpRaw = String(data?.fingerprint || "");
    if (limited(`fp:${fpRaw.toUpperCase()}`, MAX_PER_FP)) {
      return NextResponse.json({ ok: false, error: "rate_limited" },
        { status: 429, headers: { "Retry-After": "600" } });
    }

    const res = await createClaim({
      fingerprint: fpRaw,
      contact: String(data?.contact || ""),
      source: data?.source ? String(data.source) : undefined,
      product: data?.product ? String(data.product) : undefined,
    });
    if (!res.ok) {
      return NextResponse.json({ ok: false, error: res.reason }, { status: 400 });
    }

    // 顺手把绑定码一起发出去：领试用与「加客服送额度」是同一次交互里的两步，
    // 分两个请求只会让客户端多一次往返、多一处失败点。
    const withCode = (await issueBindCode(res.claim.id)) || res.claim;

    return NextResponse.json({
      ok: true,
      claim_id: withCode.id,
      bind_code: withCode.bindCode || "",
      status: withCode.status,
      deduped: res.deduped,
      // 已经签好了就直接给（重装/换目录后再来 claim 的常见路径，免得再轮询一轮）
      license: withCode.license || undefined,
    });
  } catch {
    return NextResponse.json({ ok: false, error: "server_error" }, { status: 500 });
  }
}
