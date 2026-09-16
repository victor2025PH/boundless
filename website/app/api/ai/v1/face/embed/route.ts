import { NextRequest, NextResponse } from "next/server";
import {
  FACE_CHAR_COST,
  ROUTE_BUDGET_MS,
  consumeQuota,
  extractDeviceToken,
  faceRelayEnabled,
  logGateway,
  proxyFace,
  quotaSnapshot,
  verifyDeviceToken,
} from "@/lib/ai-gateway";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * POST /api/ai/v1/face/embed（JSON：{image_base64, max_faces?, min_det_score?}）
 * Authorization: Bearer cx.<payload>.<sig>
 *
 * #333 视觉身份层：把 176 CPU 人脸嵌入边车（scripts/face176，/v1/face/embed）经反向隧道
 * 暴露给持设备令牌的外网坐席机——客户发的图里是谁（人设 / 客户本人 / 关系人 / 未知）
 * 的向量在这里算，判定与记忆在客户端（src/companion/visual_identity · visual_memory）。
 * 服务无状态不落图；网关不解析图片内容，请求体原样透传，响应原样返回。
 *
 * 额度：按次小额固定折算（FACE_CHAR_COST，默认 50 字符/次）。错误语义与 asr 路由同口径。
 */
export async function POST(req: NextRequest) {
  const started = Date.now();
  try {
    if (!faceRelayEnabled()) {
      return NextResponse.json({ error: { message: "face_unavailable" } }, { status: 503 });
    }
    const claims = verifyDeviceToken(extractDeviceToken(req.headers));
    if (!claims) {
      return NextResponse.json({ error: { message: "invalid_token" } }, { status: 401 });
    }

    const snap = await quotaSnapshot(claims);
    if (snap.busy) {
      void logGateway({ ev: "reject", mid: claims.mid, why: "global", op: "face" });
      return NextResponse.json(
        { error: { message: "gateway_busy", type: "insufficient_quota" } },
        { status: 429 }
      );
    }
    if (snap.remaining <= 0) {
      void logGateway({ ev: "reject", mid: claims.mid, why: "machine", op: "face" });
      return NextResponse.json(
        { error: { message: "quota_exceeded", type: "insufficient_quota" } },
        { status: 429 }
      );
    }

    const raw = await req.text();
    if (!raw) {
      return NextResponse.json({ error: { message: "bad_request" } }, { status: 400 });
    }
    // 8MB 图 ≈ 10.7MB base64 + 少量字段；与边车 MAX_B64 对齐
    if (raw.length > 12 * 1024 * 1024) {
      return NextResponse.json({ error: { message: "payload_too_large" } }, { status: 413 });
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(raw);
    } catch {
      return NextResponse.json({ error: { message: "bad_request" } }, { status: 400 });
    }
    if (!parsed || typeof parsed !== "object" ||
        typeof (parsed as { image_base64?: unknown }).image_base64 !== "string") {
      return NextResponse.json({ error: { message: "bad_request" } }, { status: 400 });
    }

    const ac = new AbortController();
    const timer = setTimeout(() => ac.abort(), ROUTE_BUDGET_MS.face);
    let upstream: Response;
    try {
      upstream = await proxyFace(raw, ac.signal);
    } catch {
      void logGateway({ ev: "face_fail", mid: claims.mid, ms: Date.now() - started });
      return NextResponse.json({ error: { message: "upstream_error" } }, { status: 502 });
    } finally {
      clearTimeout(timer);
    }

    const out = await upstream.arrayBuffer();
    if (upstream.ok) {
      await consumeQuota(claims, FACE_CHAR_COST);
    }
    void logGateway({
      ev: "face", mid: claims.mid, status: upstream.status,
      bytes: raw.length, ms: Date.now() - started,
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
