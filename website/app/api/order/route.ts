import { NextRequest, NextResponse } from "next/server";
import { appendLead, upsertLead, type LeadRecord } from "@/lib/lead-store";
import { createOrder, getOrder, notifyAdminsOfOrder } from "@/lib/order-store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function clean(v: unknown, max: number) {
  return String(v ?? "").trim().slice(0, max);
}

/** 公开订单进度查询：GET /api/order?id=AH-…（只回状态与非敏感字段，不回联系方式/指纹）。 */
export async function GET(req: NextRequest) {
  const id = clean(req.nextUrl.searchParams.get("id"), 40);
  if (!/^AH-\d{8}-[A-Z0-9]{4,10}$/i.test(id)) {
    return NextResponse.json({ ok: false, error: "bad_id" }, { status: 400 });
  }
  const o = await getOrder(id);
  if (!o) return NextResponse.json({ ok: false, error: "not_found" }, { status: 404 });
  return NextResponse.json({
    ok: true,
    id: o.id,
    status: o.status,
    plan: o.plan,
    period: o.period,
    pay_amount: o.pay_amount,
    t: o.t,
    paid_at: o.paid_at ?? null,
    activated_at: o.activated_at ?? null,
    // 开通后把兑换码给到客户自取（单号即领取凭证：随机段仅出现在客户下单回执里）
    code: o.status === "activated" ? o.code ?? null : null,
  });
}

export async function POST(req: NextRequest) {
  try {
    const data = await req.json();
    if (clean(data?.hp, 50)) return NextResponse.json({ ok: true }); // honeypot

    const contact = clean(data?.contact, 200);
    if (!contact || contact.length < 4) {
      return NextResponse.json({ ok: false, error: "contact_required" }, { status: 400 });
    }

    const order = await createOrder({
      plan: clean(data?.plan, 40),
      edition: clean(data?.edition, 20),
      period: clean(data?.period, 10),
      amount: Math.max(0, Number(data?.amount) || 0),
      contact,
      fingerprint: clean(data?.fingerprint, 128),
      lang: clean(data?.lang, 8),
      ip: clean(req.headers.get("x-forwarded-for")?.split(",")[0] || req.headers.get("x-real-ip"), 60),
      ua: clean(req.headers.get("user-agent"), 250),
    });

    // 进 CRM 留痕（后台 /admin 可见、可跟进）；TG 通知走专用订单卡（带一键改状态按钮），不重复 ping。
    const rec: LeadRecord = {
      t: order.t,
      name: "",
      contact,
      interest: `订单 ${order.plan}/${order.period}`,
      message: `[${order.id}] ${order.plan} (${order.edition}) ${order.period} 应付 ${order.pay_amount} USDT${order.fingerprint ? ` 指纹:${order.fingerprint}` : ""}`,
      lang: order.lang,
      source: "order",
      path: "/order",
      ip: order.ip,
      ua: order.ua,
    };
    await upsertLead(rec);
    await appendLead(rec);
    await notifyAdminsOfOrder(order);

    return NextResponse.json({ ok: true, order_id: order.id, pay_amount: order.pay_amount });
  } catch {
    return NextResponse.json({ ok: false, error: "server_error" }, { status: 500 });
  }
}
