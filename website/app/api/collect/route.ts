// POST /api/collect —— 集团事件收集器（EVENT_CONTRACT.md 传输层）。
//
// 上报方：platform/observability/uploader.py（spool 补传）或任意持
// EVENT_INGEST_KEY 的机器上报端。body: { events: [信封, ...] }，单批 ≤ 500 条。
// 鉴权：Authorization: Bearer <EVENT_INGEST_KEY>（机器对机器密钥，与 /console
// 人类口令体系完全独立）。幂等：event_id 主键 INSERT OR IGNORE，重发同批不重计。
import { NextRequest, NextResponse } from "next/server";
import { insertEvents } from "@/lib/events-db";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const MAX_BATCH = 500;

export async function POST(req: NextRequest) {
  const key = (process.env.EVENT_INGEST_KEY || "").trim();
  if (!key) {
    return NextResponse.json(
      {
        error: "collector_not_configured",
        message: "服务端未配置 EVENT_INGEST_KEY，收集器不可用。请在部署环境设置该密钥后重试（见 website/.env.example 与 platform/observability/EVENT_CONTRACT.md 传输层一节）。",
      },
      { status: 503 }
    );
  }
  const auth = req.headers.get("authorization") || "";
  const given = auth.startsWith("Bearer ") ? auth.slice(7).trim() : "";
  if (!given || given !== key) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }

  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid_json", message: "请求体必须是 JSON 对象" }, { status: 400 });
  }
  const events = (body as { events?: unknown } | null)?.events;
  if (!Array.isArray(events)) {
    return NextResponse.json(
      { error: "invalid_body", message: "请求体必须形如 { events: [信封, ...] }" },
      { status: 400 }
    );
  }
  if (events.length === 0) {
    return NextResponse.json({ ok: true, accepted: 0, ignoredDuplicates: 0, rejected: [] });
  }
  if (events.length > MAX_BATCH) {
    return NextResponse.json(
      { error: "batch_too_large", message: `单批最多 ${MAX_BATCH} 条，收到 ${events.length} 条，请拆批重发` },
      { status: 413 }
    );
  }

  const source = (req.headers.get("x-event-source") || "unknown").trim().slice(0, 120) || "unknown";
  try {
    const result = insertEvents(events, source);
    return NextResponse.json({ ok: true, ...result });
  } catch (e) {
    return NextResponse.json({ error: "internal", message: String(e) }, { status: 500 });
  }
}

export async function GET() {
  return NextResponse.json(
    { error: "method_not_allowed", message: "收集器只接受 POST { events: [...] }" },
    { status: 405, headers: { Allow: "POST" } }
  );
}
