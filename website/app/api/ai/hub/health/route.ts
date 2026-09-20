import { NextResponse } from "next/server";
import { ttsRelayEnabled, ttsRelayHealth } from "@/lib/ai-gateway";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * GET /api/ai/hub/health —— 克隆语音中继健康（与 7852 服务契约同形：{ok, models_loaded}）。
 *
 * 客户端 AvatarVoiceClient 把本网关当作 base_urls 里的一个端点，用它自带的
 * `{base}/health` 预检决定端点次序；这里透传真实中继健康（15s 缓存）——
 * 隧道断 / 7852 半死时如实报不健康，客户端才会正确回落（预渲染命中/文字）。
 * 无鉴权（与 service_auth 的 /health 常开语义一致；不含任何敏感信息）。
 */
export async function GET() {
  if (!ttsRelayEnabled()) {
    return NextResponse.json({ ok: false, models_loaded: false, reason: "relay_off" });
  }
  const h = await ttsRelayHealth();
  return NextResponse.json({ ok: h.ok, models_loaded: h.models_loaded });
}
