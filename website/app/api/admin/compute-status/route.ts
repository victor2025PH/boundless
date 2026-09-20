import { NextRequest, NextResponse } from "next/server";
import { requireAdmin } from "@/lib/admin-auth";
import {
  ingestSnapshot,
  statusView,
  type ComputeSnapshot,
} from "@/lib/compute-status-store";

// 算力调度实时看板 API（实施70 2026-08-27）：
// POST = 117 集群 pusher 上报快照（x-compute-key 专用密钥；泄露面有界——只允许伪造
//        算力快照，读不了任何业务数据）；管理员 cookie 亦可（演练用）。
// GET  = /admin/compute 页面读最新快照 + 调动事件（requireAdmin：cookie / x-setup-key /
//        ?key=；x-compute-key 同样放行，供集群侧自检闭环）。
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function reportKeyOk(req: NextRequest): boolean {
  const expect = (process.env.COMPUTE_REPORT_KEY || "").trim();
  if (!expect) return false;
  return (req.headers.get("x-compute-key") || "").trim() === expect;
}

export async function POST(req: NextRequest) {
  if (!reportKeyOk(req) && !requireAdmin(req)) {
    return NextResponse.json({ ok: false }, { status: 401 });
  }
  let body: unknown = null;
  try {
    body = await req.json();
  } catch {
    body = null;
  }
  const snap = body as ComputeSnapshot | null;
  if (!snap || typeof snap !== "object" || typeof snap.ts !== "number") {
    return NextResponse.json({ ok: false, error: "bad snapshot" }, { status: 400 });
  }
  try {
    if (JSON.stringify(snap).length > 256 * 1024) {
      return NextResponse.json({ ok: false, error: "too large" }, { status: 413 });
    }
  } catch {
    return NextResponse.json({ ok: false, error: "bad snapshot" }, { status: 400 });
  }
  ingestSnapshot(snap);
  return NextResponse.json({ ok: true });
}

export async function GET(req: NextRequest) {
  if (!requireAdmin(req) && !reportKeyOk(req)) {
    return NextResponse.json({ ok: false }, { status: 401 });
  }
  return NextResponse.json({ ok: true, ...statusView() });
}
