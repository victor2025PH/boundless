// /api/console/opportunities —— 跨售商机清单 + 跟进标记（schema v4）。
// GET ?kind=&customerId=&limit=&include_closed=1：三类规则见 lib/opportunities.ts，
//   商机本体只读推导，每行附 oppKey 指纹与跟进状态 log（won/dismissed 默认隐藏，
//   include_closed=1 带出且沉底）；viewer+ 可读。
// POST { opp_key, kind, customer_id, to_product?, status, note? }：标记跟进 →
//   markOpportunity UPSERT opportunities_log + audit（opportunity.mark），admin+，
//   actor=console:<username>。note 只存运营备注，不存客户聊天原文。
import { NextRequest, NextResponse } from "next/server";
import { getConsoleUser, requireConsole } from "@/lib/console-auth";
import { roleAtLeast } from "@/lib/console-users";
import {
  OPPORTUNITY_KINDS,
  OPPORTUNITY_LOG_STATUSES,
  getOpportunityStats,
  isOpportunityKind,
  isOpportunityLogStatus,
  listOpportunities,
  markOpportunity,
} from "@/lib/opportunities";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  if (!requireConsole(req)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  try {
    const sp = req.nextUrl.searchParams;
    const kind = sp.get("kind") ?? undefined;
    if (kind && !isOpportunityKind(kind)) {
      return NextResponse.json(
        { error: `unknown kind: ${kind} (expect ${OPPORTUNITY_KINDS.join("|")})` },
        { status: 400 }
      );
    }
    const includeClosed = ["1", "true"].includes(sp.get("include_closed") ?? "");
    const rows = listOpportunities({
      kind,
      // 与 console 其他路由的 snake_case 习惯兼容：customerId / customer_id 都收
      customerId: sp.get("customerId") ?? sp.get("customer_id") ?? undefined,
      limit: sp.get("limit") ? Number(sp.get("limit")) : undefined,
      includeClosed,
    });
    return NextResponse.json({ ok: true, rows, total: rows.length, stats: getOpportunityStats() });
  } catch (e) {
    return NextResponse.json({ error: String(e) }, { status: 500 });
  }
}

export async function POST(req: NextRequest) {
  const user = getConsoleUser(req);
  if (!user) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  if (!roleAtLeast(user.role, "admin")) {
    return NextResponse.json({ error: "forbidden: admin role required" }, { status: 403 });
  }
  let body: Record<string, unknown> = {};
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "invalid json body" }, { status: 400 });
  }
  const oppKey = String(body.opp_key ?? "").trim();
  const kind = String(body.kind ?? "").trim();
  const customerId = String(body.customer_id ?? "").trim();
  const status = String(body.status ?? "").trim();
  if (!oppKey || !kind || !customerId || !status) {
    return NextResponse.json({ error: "opp_key, kind, customer_id, status required" }, { status: 400 });
  }
  if (!isOpportunityKind(kind)) {
    return NextResponse.json(
      { error: `unknown kind: ${kind} (expect ${OPPORTUNITY_KINDS.join("|")})` },
      { status: 400 }
    );
  }
  if (!isOpportunityLogStatus(status)) {
    return NextResponse.json(
      { error: `unknown status: ${status} (expect ${OPPORTUNITY_LOG_STATUSES.join("|")})` },
      { status: 400 }
    );
  }
  try {
    const result = markOpportunity(
      {
        oppKey,
        kind,
        customerId,
        toProduct: body.to_product != null ? String(body.to_product) : null,
        status,
        // note 缺省 = 保留已有备注；显式传空串/null = 清空
        note: body.note === undefined ? undefined : body.note == null ? null : String(body.note),
      },
      `console:${user.username}`
    );
    return NextResponse.json({ ok: true, log: result });
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    if (msg.includes("customer not found")) {
      return NextResponse.json({ error: msg }, { status: 404 });
    }
    if (e instanceof TypeError) {
      return NextResponse.json({ error: msg }, { status: 400 });
    }
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
