import { NextRequest, NextResponse } from "next/server";
import {
  ROUTE_BUDGET_MS,
  VISION_CHAR_COST,
  CHATX_CHAR_MIN_COST,
  consumeQuota,
  estimateRequestChars,
  extractUsage,
  gatewayEnabled,
  isVisionModel,
  isChatxModel,
  logGateway,
  noteChatLatency,
  proxyChatCompletionsEx,
  proxyChatx,
  proxyVision,
  quotaSnapshot,
  sanitizePurpose,
  shouldAlertVisionOverflow,
  verifyDeviceToken,
  visionContextOverflow,
  visionRelayEnabled,
  chatxRelayEnabled,
  chatxBusy,
  type ChatUpstreamChoice,
  type VisionContextOverflow,
} from "@/lib/ai-gateway";
import { getAdminChats } from "@/lib/admin-store";
import { card } from "@/lib/tg-card";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/**
 * 识图中继回「上下文超限」400 → 推管理员 TG（#213）。否则 prompt 又长了 / 中继 Modelfile
 * 被重拉丢了 num_ctx，只会在客户机日志里烂掉，没人知道。节流见 shouldAlertVisionOverflow。
 */
async function alertVisionOverflow(ovf: VisionContextOverflow, mid: string): Promise<void> {
  try {
    if (!shouldAlertVisionOverflow()) return;
    const token = (process.env.TELEGRAM_BOT_TOKEN || "").trim();
    if (!token) return;
    const chats = await getAdminChats();
    if (!chats.length) return;
    const msg = card({
      sev: "warn",
      cat: "算力",
      title: "识图网关：请求超出中继上下文窗口，识图 400",
      body: [
        `请求 ${ovf.n_prompt_tokens ?? "?"} tokens > 中继 n_ctx ${ovf.n_ctx ?? "?"}（网关期望 ${ovf.expected_ctx}）`,
        ovf.n_ctx !== null && ovf.n_ctx < ovf.expected_ctx
          ? "中继模型的 num_ctx 低于期望：多半是 qwen3-vl:8b-instruct 被重拉、Modelfile 的 PARAMETER num_ctx 丢了（176/140 各 ollama show 核对）"
          : "中继窗口已是期望值仍超限：识图 prompt 变长了，收 prompt 或抬 VISION_NUM_CTX + Modelfile",
        `触发机器指纹：${mid}；30 分钟内只推一条`,
      ].join("\n"),
      source: "官网 AI 网关 /api/ai/v1",
      rawTag: "vision_ctx_overflow",
    });
    await Promise.allSettled(
      chats.map((chat) =>
        fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ chat_id: chat, text: msg, disable_web_page_preview: true }),
        })
      )
    );
  } catch {
    /* 告警失败不影响主链路 */
  }
}

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
    const chatx = isChatxModel(body.model);
    if (vision && !visionRelayEnabled()) {
      return NextResponse.json({ error: { message: "vision_unavailable" } }, { status: 503 });
    }
    if (chatx && !chatxRelayEnabled()) {
      return NextResponse.json({ error: { message: "chatx_unavailable" } }, { status: 503 });
    }
    if (chatx && chatxBusy()) {
      return NextResponse.json({ error: { message: "chatx_busy" } }, { status: 503 });
    }
    // 识图按固定成本计额；ChatX 27B 有最低计额（短句也占一整轮 GPU）。
    const inChars = vision
      ? VISION_CHAR_COST
      : chatx
        ? Math.max(estimateRequestChars(body), CHATX_CHAR_MIN_COST)
        : estimateRequestChars(body);
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
    // 识图冷载/大图更慢，给更宽超时。chat 预算 = ROUTE_BUDGET_MS.chat（55s）：客户端读超时
    // 必须 ≥ 它（1.0.79 起默认 60s，Q-14 #262），否则客户端先断 → nginx 499、客户端沉默。
    const timer = setTimeout(() => ac.abort(), vision ? ROUTE_BUDGET_MS.vision : ROUTE_BUDGET_MS.chat);
    let upstream: Response;
    let choice: ChatUpstreamChoice | null = null;
    const reqModel = String(body.model || "");
    try {
      if (vision) {
        upstream = await proxyVision(body, ac.signal);
      } else if (chatx) {
        upstream = await proxyChatx(body, ac.signal);
      } else {
        const r = await proxyChatCompletionsEx(body, ac.signal);
        upstream = r.res;
        choice = r.choice;
      }
    } catch (e) {
      const why = e instanceof Error ? e.message : "";
      if (chatx && why === "chatx_busy") {
        return NextResponse.json({ error: { message: "chatx_busy" } }, { status: 503 });
      }
      const ms = Date.now() - started;
      // 超时 / 连接失败也进慢模型观测：整包吃满 55s 预算就是「慢」的最强证据
      if (!vision) noteChatLatency(choice?.model || reqModel, ms);
      void logGateway({
        ev: vision ? "vision_fail" : (chatx ? "chatx_fail" : "upstream_fail"), mid: claims.mid, ms,
        ...(vision ? {} : { model: choice?.model || reqModel, key: choice?.key || "primary", prompt_chars: inChars }),
      });
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
    // B5（2026-09-11）：真 token 用量（含 DeepSeek 缓存命中/未命中、思维链 tokens）
    // 与客户端申报用途一并落流水——成本从「按字符估」变成可对账，缓存命中率可见。
    let usage = extractUsage(null);
    try {
      const j = JSON.parse(text);
      const c = j?.choices?.[0]?.message?.content;
      if (typeof c === "string") outChars = c.length;
      usage = extractUsage(j);
    } catch {
      /* ignore */
    }
    const purpose = sanitizePurpose(req.headers.get("x-chatx-purpose"));

    if (vision) {
      const ovf = visionContextOverflow(upstream.status, text);
      if (ovf) {
        void logGateway({
          ev: "vision_ctx_overflow", mid: claims.mid,
          n_prompt_tokens: ovf.n_prompt_tokens, n_ctx: ovf.n_ctx, expected_ctx: ovf.expected_ctx,
        });
        void alertVisionOverflow(ovf, claims.mid);
      }
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
    // Q-14 #262：ok 事件补 model / key=primary|backup / prompt_chars / ok，供
    // scripts/ai-gateway-report.mjs 算近 24h 按模型 p50 / p95（499 率另取 nginx access.log）。
    const totalMs = Date.now() - started;
    if (!vision) noteChatLatency(choice?.model || reqModel, totalMs);
    void logGateway({
      ev: "chat", mid: claims.mid, status: upstream.status, ok: upstream.ok ? 1 : 0,
      in: inChars, out: outChars, ms: totalMs,
      ...(vision
        ? { vision: 1 }
        : { model: choice?.model || reqModel, key: choice?.key || "primary", prompt_chars: inChars }),
      // 真 usage（上游未回 usage 时全 0，与「无字段」可区分：pt/ct 同为 0 且 ok=1 即上游没给）
      pt: usage.pt, ct: usage.ct, cache_hit: usage.cache_hit, cache_miss: usage.cache_miss,
      ...(usage.reasoning ? { reasoning: usage.reasoning } : {}),
      ...(purpose ? { purpose } : {}),
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
