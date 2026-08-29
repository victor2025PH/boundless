import { NextRequest, NextResponse } from "next/server";
import {
  ASR_CHAR_COST,
  ROUTE_BUDGET_MS,
  asrRelayEnabled,
  consumeQuota,
  extractDeviceToken,
  logGateway,
  proxyAsr,
  quotaSnapshot,
  verifyDeviceToken,
} from "@/lib/ai-gateway";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * OpenAI-compatible：POST /api/ai/v1/audio/transcriptions（multipart/form-data）
 * Authorization: Bearer cx.<payload>.<sig>
 *
 * 把 176 GPU ASR（OpenAI 形态 /v1/audio/transcriptions）经反向隧道暴露给持设备
 * 令牌的外网客户端——冻结安装包不带本地 ASR 模型，出了内网没有这条路就听不懂
 * 客户语音。请求体（multipart，含 boundary 的 Content-Type）原样透传，响应
 * （text 或 json，随客户端 response_format）原样返回。
 *
 * 额度：按次固定折算（ASR_CHAR_COST，默认 200 字符/次）——网关拿不到可靠的
 * 音频时长，按次计比按体积猜更可预算。错误语义与 chat 路由同口径。
 */
export async function POST(req: NextRequest) {
  const started = Date.now();
  try {
    if (!asrRelayEnabled()) {
      return NextResponse.json({ error: { message: "asr_unavailable" } }, { status: 503 });
    }
    const claims = verifyDeviceToken(extractDeviceToken(req.headers));
    if (!claims) {
      return NextResponse.json({ error: { message: "invalid_token" } }, { status: 401 });
    }

    const snap = await quotaSnapshot(claims);
    if (snap.busy) {
      void logGateway({ ev: "reject", mid: claims.mid, why: "global", op: "asr" });
      return NextResponse.json(
        { error: { message: "gateway_busy", type: "insufficient_quota" } },
        { status: 429 }
      );
    }
    if (snap.remaining <= 0) {
      void logGateway({ ev: "reject", mid: claims.mid, why: "machine", op: "asr" });
      return NextResponse.json(
        { error: { message: "quota_exceeded", type: "insufficient_quota" } },
        { status: 429 }
      );
    }

    const body = await req.arrayBuffer();
    if (!body || body.byteLength === 0) {
      return NextResponse.json({ error: { message: "bad_request" } }, { status: 400 });
    }
    if (body.byteLength > 25 * 1024 * 1024) {
      return NextResponse.json({ error: { message: "payload_too_large" } }, { status: 413 });
    }

    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), ROUTE_BUDGET_MS.asr);
    let upstream: Response;
    try {
      upstream = await proxyAsr(body, req.headers.get("content-type") || "", ac.signal);
    } catch {
      void logGateway({ ev: "asr_fail", mid: claims.mid, ms: Date.now() - started });
      return NextResponse.json({ error: { message: "upstream_error" } }, { status: 502 });
    } finally {
      clearTimeout(timer);
    }

    if (upstream.status === 401 || upstream.status === 403) {
      void logGateway({ ev: "asr_upstream_auth", mid: claims.mid, status: upstream.status });
      return NextResponse.json({ error: { message: "upstream_auth" } }, { status: 502 });
    }

    const out = await upstream.arrayBuffer();
    if (upstream.ok) {
      await consumeQuota(claims, ASR_CHAR_COST);
    }
    void logGateway({
      ev: "asr", mid: claims.mid, status: upstream.status,
      bytes: body.byteLength, ms: Date.now() - started,
    });
    return new NextResponse(out, {
      status: upstream.status,
      headers: {
        "Content-Type": upstream.headers.get("Content-Type") || "application/json",
      },
    });
  } catch {
    return NextResponse.json({ error: { message: "server_error" } }, { status: 500 });
  }
}
