import { NextRequest, NextResponse } from "next/server";
import {
  EMBED_CHAR_MIN_COST,
  ROUTE_BUDGET_MS,
  consumeQuota,
  embedRelayEnabled,
  estimateEmbedChars,
  extractDeviceToken,
  logGateway,
  proxyEmbeddings,
  quotaSnapshot,
  verifyDeviceToken,
} from "@/lib/ai-gateway";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * OpenAI-compatible：POST /api/ai/v1/embeddings
 * Authorization: Bearer cx.<payload>.<sig>
 *
 * 把 LAN Ollama 的 bge-m3 经反向隧道暴露给持设备令牌的外网客户端。
 *
 * 为什么必须有这条路由（2026-08-28 P0-1，B126 根因）：客户端
 * ``ai_client.embed()`` 在未配独立嵌入端点时**回落对话客户端**，而托管态对话
 * 客户端的 base_url 就是本网关 → 它一直在打 ``/api/ai/v1/embeddings``。该路由
 * 缺失时 Next.js 回的是 404 **HTML 页**：OpenAI SDK 解析 JSON 失败 → 连续 3 次
 * 触发客户端 120s 熔断 → 语义记忆召回全程降级为关键词，「客户明说过生日、AI
 * 还反问」就是这么来的。缺失的代价全部落在静默降级上，没有任何一端会报错。
 *
 * 额度：按 input 字符计，下限 EMBED_CHAR_MIN_COST（短句也占一轮 GPU）。
 * 错误语义与 chat / asr 路由同口径，且**永远回 JSON**——本路由存在的全部意义
 * 就是让客户端不再收到 HTML。
 */
export async function POST(req: NextRequest) {
  const started = Date.now();
  try {
    if (!embedRelayEnabled()) {
      return NextResponse.json({ error: { message: "embed_unavailable" } }, { status: 503 });
    }
    const claims = verifyDeviceToken(extractDeviceToken(req.headers));
    if (!claims) {
      return NextResponse.json({ error: { message: "invalid_token" } }, { status: 401 });
    }

    const snap = await quotaSnapshot(claims);
    if (snap.busy) {
      void logGateway({ ev: "reject", mid: claims.mid, why: "global", op: "embed" });
      return NextResponse.json(
        { error: { message: "gateway_busy", type: "insufficient_quota" } },
        { status: 429 }
      );
    }
    if (snap.remaining <= 0) {
      void logGateway({ ev: "reject", mid: claims.mid, why: "machine", op: "embed" });
      return NextResponse.json(
        { error: { message: "quota_exceeded", type: "insufficient_quota" } },
        { status: 429 }
      );
    }

    let body: Record<string, unknown>;
    try {
      body = (await req.json()) as Record<string, unknown>;
    } catch {
      return NextResponse.json({ error: { message: "bad_request" } }, { status: 400 });
    }
    const chars = estimateEmbedChars(body);
    if (chars <= 0) {
      return NextResponse.json({ error: { message: "bad_request" } }, { status: 400 });
    }
    // 单次批量上限：记忆召回一次最多几十条短文本，10 万字符已是数十倍余量；
    // 再大就是误用/攻击，早拒比让 GPU 空转一分钟好。
    if (chars > 100_000) {
      return NextResponse.json({ error: { message: "payload_too_large" } }, { status: 413 });
    }

    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), ROUTE_BUDGET_MS.embed);
    let upstream: Response;
    try {
      upstream = await proxyEmbeddings(body, ac.signal);
    } catch {
      void logGateway({ ev: "embed_fail", mid: claims.mid, ms: Date.now() - started });
      return NextResponse.json({ error: { message: "upstream_error" } }, { status: 502 });
    } finally {
      clearTimeout(timer);
    }

    const out = await upstream.arrayBuffer();
    if (upstream.ok) {
      await consumeQuota(claims, Math.max(chars, EMBED_CHAR_MIN_COST));
    }
    void logGateway({
      ev: "embed", mid: claims.mid, status: upstream.status,
      chars, ms: Date.now() - started,
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
