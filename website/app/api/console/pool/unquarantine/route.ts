// POST /api/console/pool/unquarantine —— 解除凭据组隔离（运营核实误报后用）。
// body: { api_id }。隔离本身由客户端举报聚类自动触发（tg-cred-pool.reportInvalid），
// 解除是人的决定：先跑 tg_cred_probe 核实真伪，误报才解除；真废组直接换池别解除。
import { NextRequest, NextResponse } from "next/server";
import { requireConsole } from "@/lib/console-auth";
import { unquarantine } from "@/lib/tg-cred-pool";
import { logGateway } from "@/lib/ai-gateway";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  if (!requireConsole(req)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "invalid_json" }, { status: 400 });
  }
  const apiId = String((body as { api_id?: unknown })?.api_id || "").trim();
  if (!/^\d{4,}$/.test(apiId)) {
    return NextResponse.json({ ok: false, error: "bad_api_id" }, { status: 400 });
  }
  const removed = unquarantine(apiId);
  if (removed) void logGateway({ ev: "cred_unquarantine", api_id: apiId });
  return NextResponse.json({ ok: true, removed });
}
