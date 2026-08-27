import { NextRequest, NextResponse } from "next/server";
import {
  VISION_CHAR_COST,
  consumeQuota,
  estimateRequestChars,
  gatewayEnabled,
  isVisionModel,
  logGateway,
  proxyChatCompletions,
  proxyVision,
  quotaSnapshot,
  verifyDeviceToken,
  visionRelayEnabled,
} from "@/lib/ai-gateway";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * OpenAI-compatible：POST /api/ai/v1/chat/completions
 * Authorization: Bearer cx.<payload>.<sig>
 *
 * 验设备令牌 → 双层额度（单机日额度 / 全局日预算）→ 代理到厂商（Key 只在服务端，
 * model/max_tokens 服务端钳制）→ 只对成功响应计费。
 * 错误语义（客户端据此分级展示）：
 *   401 invalid_token（客户端应换新令牌）
 *   429 quota_exceeded（单机额度，明天恢复）/ gateway_busy（全局预算，稍后再试）
 *   502 upstream_auth（厂商 Key 问题——绝不伪装成客户端 401）/ upstream_error
 */

function bearer(req: NextRequest): string {
  const h = req.headers.get("authorization") || req.headers.get("Authorization") || "";
  const m = /^Bearer\s+(.+)$/i.exec(h.trim());
  return m ? m[1].trim() : "";
}

export async function POST(req: NextRequest) {
  const started = Date.now();
  try {
    if (!gatewayEnabled()) {
      return NextResponse.json({ error: { message: "gateway_disabled" } }, { status: 503 });
    }
    const token = bearer(req);
    const claims = verifyDeviceToken(token);
    if (!claims) {
      return NextResponse.json({ error: { message: "invalid_token" } }, { status: 401 });
    }

    const body = (await req.json().catch(() => null)) as Record<string, unknown> | null;
    if (!body || typeof body !== "object") {
      return NextResponse.json({ error: { message: "bad_request" } }, { status: 400 });
    }

    // 识图模型 → 路由到我们自己的 GPU VLM 中继（隧道），而非 DeepSeek（不做视觉）。
    const vision = isVisionModel(body.model);
    if (vision && !visionRelayEnabled()) {
      return NextResponse.json({ error: { message: "vision_unavailable" } }, { status: 503 });
    }
    // 识图按固定成本计额（图片 token 远超字符估算）；文本按 in+out 实计。
    const inChars = vision ? VISION_CHAR_COST : estimateRequestChars(body);
    const snap = await quotaSnapshot(claims);
    if (snap.busy) {
      void logGateway({ ev: "reject", mid: claims.mid, why: "global" });
      return NextResponse.json(
        { error: { message: "gateway_busy", type: "insufficient_quota" } },
        { status: 429 }
      );
    }
    if (snap.remaining <= 0) {
      void logGateway({ ev: "reject", mid: claims.mid, why: "machine" });
      return NextResponse.json(
        { error: { message: "quota_exceeded", type: "insufficient_quota" } },
        { status: 429 }
      );
    }

    const ac = new AbortController();
    // 识图冷载/大图更慢，给更宽超时
    const timer = setTimeout(() => ac.abort(), vision ? 120000 : 55000);
    let upstream: Response;
    try {
      upstream = vision ? await proxyVision(body, ac.signal) : await proxyChatCompletions(body, ac.signal);
    } catch {
      void logGateway({ ev: vision ? "vision_fail" : "upstream_fail", mid: claims.mid, ms: Date.now() - started });
      return NextResponse.json({ error: { message: "upstream_error" } }, { status: 502 });
    } finally {
      clearTimeout(timer);
    }

    // 厂商侧鉴权失败 = 我们的 Key 出问题，绝不能回 401 让客户端误判自己令牌失效
    if (upstream.status === 401 || upstream.status === 403) {
      void logGateway({ ev: "upstream_auth", mid: claims.mid, status: upstream.status });
      return NextResponse.json({ error: { message: "upstream_auth" } }, { status: 502 });
    }

    const text = await upstream.text();
    let outChars = 0;
    try {
      const j = JSON.parse(text);
      const c = j?.choices?.[0]?.message?.content;
      if (typeof c === "string") outChars = c.length;
    } catch {
      /* ignore */
    }

    // 只对成功响应计费（上游 5xx/超时不该消耗用户额度）
    let remaining = snap.remaining;
    if (upstream.ok) {
      const billed = await consumeQuota(claims, Math.max(inChars + outChars, 1));
      remaining = billed.remaining;
    }
    // 识图调用带独立标（2026-08-23）：此前 vision 也记 ev:"chat"，排障只能靠
    // in===VISION_CHAR_COST 猜——「把人看成猫」事故的服务端流水就因此被误读成
    // 「零识图调用」。加 vision:1 字段（不改 ev，老消费方零影响）。
    void logGateway({
      ev: "chat", mid: claims.mid, status: upstream.status,
      in: inChars, out: outChars, ms: Date.now() - started,
      ...(vision ? { vision: 1 } : {}),
    });

    return new NextResponse(text, {
      status: upstream.status,
      headers: {
        "Content-Type": upstream.headers.get("Content-Type") || "application/json",
        "X-ChatX-Quota-Remaining": String(remaining),
      },
    });
  } catch {
    return NextResponse.json({ error: { message: "server_error" } }, { status: 500 });
  }
}
