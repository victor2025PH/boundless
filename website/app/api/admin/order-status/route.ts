import { NextRequest, NextResponse } from "next/server";
import { requireAdmin } from "@/lib/admin-auth";
import { ORDER_STATUSES, notifyCustomerOfStatus, setOrderStatus, type OrderStatus } from "@/lib/order-store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// 管理员改订单状态。GET 供 Telegram 通知卡一键点击（query key 走 requireAdmin 的 legacy 通道），
// POST 供后台/脚本调用。
async function handle(req: NextRequest, id: string, status: string, code?: string) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  if (!id || !ORDER_STATUSES.includes(status as OrderStatus)) {
    return NextResponse.json({ ok: false, error: "bad_request" }, { status: 400 });
  }
  const o = await setOrderStatus(id, status as OrderStatus, code);
  if (!o) return NextResponse.json({ ok: false, error: "not_found" }, { status: 404 });
  // 到账 / 开通即自动私信已绑定 TG 的客户（未绑定则静默；网络失败不影响改状态）
  if (status === "paid" || status === "activated") {
    await notifyCustomerOfStatus(o, status).catch(() => {});
  }
  return { order: o };
}

export async function GET(req: NextRequest) {
  const id = req.nextUrl.searchParams.get("id") ?? "";
  const status = req.nextUrl.searchParams.get("status") ?? "";
  const r = await handle(req, id, status);
  if (r instanceof NextResponse) return r;
  // TG 里点开是浏览器页面，给人读的确认文本
  const zhStatus: Record<string, string> = {
    pending: "待付款",
    paid: "已到账",
    activated: "已开通",
    cancelled: "已取消",
  };
  return new NextResponse(
    `✅ 订单 ${r.order.id} 已更新为「${zhStatus[r.order.status] ?? r.order.status}」\n套餐：${r.order.plan} · 应付 ${r.order.pay_amount} USDT\n联系：${r.order.contact}`,
    { headers: { "Content-Type": "text/plain; charset=utf-8" } }
  );
}

export async function POST(req: NextRequest) {
  const data = await req.json().catch(() => ({}));
  // code：履约兑换码（本地签发机开通订单时一并回填，客户在状态页自取）
  const r = await handle(req, String(data?.id ?? ""), String(data?.status ?? ""), data?.code ? String(data.code) : undefined);
  if (r instanceof NextResponse) return r;
  return NextResponse.json({ ok: true, order: r.order });
}
