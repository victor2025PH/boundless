import { NextRequest, NextResponse } from "next/server";
import { guideTrialIngress } from "@/lib/guide-trial-ingress";
import { guideTrialTarget, postGuideTrial } from "@/lib/guide-trial-notify";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * POST /api/guide-trial
 * body: `{ telegram, feature: "matrixx" }`
 *
 * MatrixX 在第一次确认送出消息后调用。请求里没有引擎密钥。
 * 本进程用 GUIDE_TRIAL_SECRET 转给向导场；没配密钥就拒绝，不转发。
 * 引擎仍要求这个用户名已经先私聊过向导，对不上就不记。
 */

const WINDOW_MS = 60 * 1000;
const MAX_PER_NAME = 4;
const MAX_PER_IP = 30;
const hits = new Map<string, number[]>();

function limited(key: string, max: number): boolean {
  const now = Date.now();
  const arr = (hits.get(key) || []).filter((t) => now - t < WINDOW_MS);
  arr.push(now);
  hits.set(key, arr);
  if (hits.size > 5000) {
    for (const [k, v] of hits) {
      if (!v.length || now - v[v.length - 1] > WINDOW_MS) hits.delete(k);
    }
  }
  return arr.length > max;
}

function clientIp(req: NextRequest): string {
  const fwd = req.headers.get("x-forwarded-for") || "";
  return fwd.split(",")[0].trim() || "local";
}

export async function POST(req: NextRequest) {
  try {
    const data = await req.json().catch(() => null);
    const body = guideTrialIngress(data);
    if (!body) {
      return NextResponse.json({ ok: false, error: "bad_request" }, { status: 400 });
    }
    if (!guideTrialTarget()) {
      return NextResponse.json({ ok: false, reason: "not_configured" });
    }
    if (limited(`ip:${clientIp(req)}`, MAX_PER_IP) || limited(`name:${body.telegram.toLowerCase()}`, MAX_PER_NAME)) {
      return NextResponse.json({ ok: false, error: "rate_limited" }, { status: 429 });
    }
    await postGuideTrial(body);
    return NextResponse.json({ ok: true });
  } catch {
    return NextResponse.json({ ok: false, error: "server_error" }, { status: 500 });
  }
}
