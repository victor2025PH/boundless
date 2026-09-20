import { NextRequest, NextResponse } from "next/server";
import {
  ROUTE_BUDGET_MS,
  TTS_CHAR_MIN_COST,
  consumeQuota,
  extractDeviceToken,
  logGateway,
  proxyTts,
  quotaSnapshot,
  ttsRelayEnabled,
  verifyDeviceToken,
} from "@/lib/ai-gateway";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * POST /api/ai/hub/v1/tts/{clone|instruct|register_spk}
 * 鉴权：X-AH-Svc: cx.<payload>.<sig>（avatar_voice 客户端原生头）或 Authorization Bearer。
 *
 * 把 AvatarHub CosyVoice3(7852) 的合成契约原样暴露给持设备令牌的外网客户端：
 * 路径与 7852 逐字对齐（客户端 base_urls 指到 /api/ai/hub 即可，零改动），请求体
 * JSON 原样转发到 VPS localhost 的反向隧道端口（117 主 / 140 备，失败冷却切换），
 * 集群内部 X-AH-Svc 令牌由网关代注——真令牌永不下发客户端。
 *
 * 额度：clone/instruct 按合成文本字符计（有单次下限——短句也占一轮 GPU 串行合成），
 * register_spk（预热）免计费但仍要有效令牌。错误语义与 chat 路由同口径：
 *   401 invalid_token / 429 quota_exceeded|gateway_busy / 502 upstream_auth|upstream_error
 */

const OPS: Record<string, string> = {
  clone: "/v1/tts/clone",
  instruct: "/v1/tts/instruct",
  register_spk: "/v1/tts/register_spk",
};

export async function POST(
  req: NextRequest,
  { params }: { params: { op: string } }
) {
  const started = Date.now();
  try {
    const path = OPS[String(params?.op || "")];
    if (!path) {
      return NextResponse.json({ error: { message: "unknown_op" } }, { status: 404 });
    }
    if (!ttsRelayEnabled()) {
      return NextResponse.json({ error: { message: "tts_unavailable" } }, { status: 503 });
    }
    const claims = verifyDeviceToken(extractDeviceToken(req.headers));
    if (!claims) {
      return NextResponse.json({ error: { message: "invalid_token" } }, { status: 401 });
    }

    const raw = await req.text();
    if (!raw) {
      return NextResponse.json({ error: { message: "bad_request" } }, { status: 400 });
    }
    let textLen = 0;
    try {
      const j = JSON.parse(raw) as { text?: unknown };
      textLen = typeof j?.text === "string" ? j.text.length : 0;
    } catch {
      return NextResponse.json({ error: { message: "bad_request" } }, { status: 400 });
    }
    const billable = params.op !== "register_spk";
    const cost = billable ? Math.max(textLen, TTS_CHAR_MIN_COST) : 0;

    if (billable) {
      const snap = await quotaSnapshot(claims);
      if (snap.busy) {
        void logGateway({ ev: "reject", mid: claims.mid, why: "global", op: `tts_${params.op}` });
        return NextResponse.json(
          { error: { message: "gateway_busy", type: "insufficient_quota" } },
          { status: 429 }
        );
      }
      if (snap.remaining <= 0) {
        void logGateway({ ev: "reject", mid: claims.mid, why: "machine", op: `tts_${params.op}` });
        return NextResponse.json(
          { error: { message: "quota_exceeded", type: "insufficient_quota" } },
          { status: 429 }
        );
      }
    }

    // 合成冷载可到几十秒；客户端 synth_timeout 75s，网关放 90s（客户端先放弃）。
    // 单台中继另有 TTS_ATTEMPT_MS 上限——病态节点不再吃掉整个预算把备机饿死。
    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), ROUTE_BUDGET_MS.tts);
    let upstream: Response;
    try {
      upstream = await proxyTts(path, raw, ac.signal);
    } catch {
      void logGateway({ ev: "tts_fail", mid: claims.mid, op: params.op, ms: Date.now() - started });
      return NextResponse.json({ error: { message: "upstream_error" } }, { status: 502 });
    } finally {
      clearTimeout(timer);
    }

    // 中继侧 401/403 = 网关代注的集群令牌有问题，绝不能回 401 让客户端误判自己令牌失效
    if (upstream.status === 401 || upstream.status === 403) {
      void logGateway({ ev: "tts_upstream_auth", mid: claims.mid, status: upstream.status });
      return NextResponse.json({ error: { message: "upstream_auth" } }, { status: 502 });
    }

    const body = await upstream.arrayBuffer();
    if (upstream.ok && cost > 0) {
      await consumeQuota(claims, cost);
    }
    void logGateway({
      ev: "tts", mid: claims.mid, op: params.op, status: upstream.status,
      chars: textLen, ms: Date.now() - started,
    });
    return new NextResponse(body, {
      status: upstream.status,
      headers: {
        "Content-Type": upstream.headers.get("Content-Type") || "application/json",
      },
    });
  } catch {
    return NextResponse.json({ error: { message: "server_error" } }, { status: 500 });
  }
}
