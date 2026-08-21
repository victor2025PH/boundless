import { NextRequest, NextResponse } from "next/server";
import { notifyAdmins } from "@/lib/order-store";
import { requireAdmin } from "@/lib/admin-auth";
import {
  DiagRequest,
  loadDiagRequests as loadReqs,
  saveDiagRequests as saveReqs,
} from "@/lib/diag-requests";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// 远程诊断拉取（2026-08-21，配套 engines hosted_gateway.check_remote_diag）：
// 内测实况是用户不会走「设置→一键上传→回读短码」三步，客服催一上午拿不到包。
// 本路由只补「远程按下那颗按钮」：客服按机器指纹登记取包请求，客户端启动后
// ~2 分钟与每小时刷新轮各轻询一次，看到请求就地打包直传 /api/diag-upload
// （包内容与用户手点完全同一实现、同一套打码规则），然后回执销记。
// 实施51 提速：/api/ai/v1/quota 额度轮询捎带 diag_requested 位（lib/diag-requests
// 单源 hasPendingDiag），工作台开着时客户端 ~1 分钟内就会看到请求。
//
// 动作面：
//   POST {action:"create", fp, note?}   requireAdmin —— 登记取包请求
//   POST {action:"ack", fp, request_id, code}  客户端回执（无鉴权：需同时命中
//        fp+request_id 才能销记，伪造成本高且最坏效果=客服再点一次 create）
//   GET  ?fp=XXXX                        客户端轻询（无鉴权：只回有/无+请求号）
//   GET  ?list=1                         requireAdmin —— 看全部请求状态
// 存储：DATA_DIR/diag/requests.json（lib/diag-requests 单源；7 天过期）。

function newRequestId(): string {
  return `dr-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;
}

export async function POST(req: NextRequest) {
  let body: Record<string, unknown> = {};
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "bad_body" }, { status: 400 });
  }
  const action = String(body.action || "");
  const fp = String(body.fp || "").trim().slice(0, 64);

  if (action === "create") {
    if (!requireAdmin(req)) {
      return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
    }
    if (!fp) {
      return NextResponse.json({ ok: false, error: "fp_required" }, { status: 400 });
    }
    const reqs = await loadReqs();
    // 同指纹已有 pending → 幂等复用，避免客服连点造成客户端多次上传
    const existing = reqs.find((r) => r.fp === fp && r.status === "pending");
    if (existing) {
      return NextResponse.json({ ok: true, request_id: existing.request_id, reused: true });
    }
    const rec: DiagRequest = {
      request_id: newRequestId(),
      fp,
      note: String(body.note || "").slice(0, 200),
      t: new Date().toISOString(),
      status: "pending",
    };
    reqs.push(rec);
    await saveReqs(reqs);
    return NextResponse.json({ ok: true, request_id: rec.request_id });
  }

  if (action === "ack") {
    const requestId = String(body.request_id || "");
    const rec = (await loadReqs()).find(
      (r) => r.request_id === requestId && r.fp === fp,
    );
    if (!rec) {
      return NextResponse.json({ ok: false, error: "not_found" }, { status: 404 });
    }
    const reqs = await loadReqs();
    const target = reqs.find((r) => r.request_id === requestId)!;
    target.status = "done";
    target.code = String(body.code || "").slice(0, 12);
    target.done_t = new Date().toISOString();
    await saveReqs(reqs);
    // 包本体已由 /api/diag-upload 推进客服 TG；这里补一条「哪个请求兑现了」的对账通知
    await notifyAdmins(
      `🧲 远程诊断请求已兑现：码 <code>${target.code}</code>（指纹 ${fp.slice(0, 12)}…）`,
    ).catch(() => {});
    return NextResponse.json({ ok: true });
  }

  return NextResponse.json({ ok: false, error: "bad_action" }, { status: 400 });
}

export async function GET(req: NextRequest) {
  if (req.nextUrl.searchParams.get("list")) {
    if (!requireAdmin(req)) {
      return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
    }
    return NextResponse.json({ ok: true, requests: await loadReqs() });
  }
  const fp = (req.nextUrl.searchParams.get("fp") || "").trim().slice(0, 64);
  if (!fp) {
    return NextResponse.json({ ok: false, error: "fp_required" }, { status: 400 });
  }
  const rec = (await loadReqs()).find((r) => r.fp === fp && r.status === "pending");
  if (!rec) {
    return NextResponse.json({ ok: true, requested: false });
  }
  return NextResponse.json({ ok: true, requested: true, request_id: rec.request_id });
}
