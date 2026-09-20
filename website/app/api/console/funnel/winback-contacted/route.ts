// POST /api/console/funnel/winback-contacted —— 标记/取消挽回名单某机器「已联系」。
// body: { fingerprint, note?, undo? }。运营外呼闭环，避免重复打扰；只 admin+ 可写。
import { NextRequest, NextResponse } from "next/server";
import { getConsoleUser, requireRole } from "@/lib/console-auth";
import { markContacted, unmarkContacted } from "@/lib/winback-store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function POST(req: NextRequest) {
  if (!requireRole(req, "admin")) {
    return NextResponse.json({ error: "forbidden" }, { status: 403 });
  }
  let body: unknown;
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "invalid_json" }, { status: 400 });
  }
  const fp = String((body as { fingerprint?: unknown })?.fingerprint || "").trim();
  const undo = (body as { undo?: unknown })?.undo === true;
  const note = String((body as { note?: unknown })?.note || "").trim() || undefined;
  if (!fp) return NextResponse.json({ ok: false, error: "bad_fingerprint" }, { status: 400 });

  if (undo) {
    await unmarkContacted(fp);
    return NextResponse.json({ ok: true, contacted: false });
  }
  const by = getConsoleUser(req)?.username || "console";
  const r = await markContacted(fp, by, note);
  if (!r.ok) return NextResponse.json({ ok: false, error: r.error }, { status: 400 });
  return NextResponse.json({ ok: true, contacted: true });
}
