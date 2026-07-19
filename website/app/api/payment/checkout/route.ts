import { NextRequest, NextResponse } from "next/server";
import { getOrder } from "@/lib/order-store";
import { getPaymentSettings, stripeSecret } from "@/lib/payment-settings";
import { SITE_URL } from "@/lib/site";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** 银行卡结算：为已创建的订单生成 Stripe Checkout Session（REST 直连，零 SDK 依赖）。
 *  卡未启用 / 服务器没配 STRIPE_SECRET_KEY / 金额为 0 → 回 not_configured，
 *  前端优雅回落 USDT/客服，绝不出现坏流程。对账靠 client_reference_id=订单号。 */
export async function POST(req: NextRequest) {
  try {
    const data = await req.json().catch(() => ({}));
    const orderId = String(data?.order_id ?? "").trim().slice(0, 40);
    if (!/^AH-\d{8}-[A-Z0-9]{4,10}$/i.test(orderId)) {
      return NextResponse.json({ ok: false, error: "bad_id" }, { status: 400 });
    }
    const order = await getOrder(orderId);
    if (!order) return NextResponse.json({ ok: false, error: "not_found" }, { status: 404 });

    const settings = await getPaymentSettings();
    const secret = stripeSecret();
    if (!settings.card.enabled || !secret || !(order.amount > 0)) {
      return NextResponse.json({ ok: false, error: "not_configured" });
    }

    const successUrl = settings.card.successUrl || `${SITE_URL}/order?check=${encodeURIComponent(order.id)}`;
    const cancelUrl = settings.card.cancelUrl || `${SITE_URL}/order`;
    const params = new URLSearchParams({
      mode: "payment",
      success_url: successUrl,
      cancel_url: cancelUrl,
      client_reference_id: order.id,
      "line_items[0][price_data][currency]": (settings.card.currency || "USD").toLowerCase(),
      "line_items[0][price_data][product_data][name]": order.plan || "subscription",
      "line_items[0][price_data][unit_amount]": String(Math.round(order.amount * 100)),
      "line_items[0][quantity]": "1",
    });

    const resp = await fetch("https://api.stripe.com/v1/checkout/sessions", {
      method: "POST",
      headers: {
        Authorization: `Bearer ${secret}`,
        "Content-Type": "application/x-www-form-urlencoded",
      },
      body: params.toString(),
    });
    const session = await resp.json().catch(() => null);
    if (!resp.ok || !session?.url) {
      const detail = String(session?.error?.message || `http_${resp.status}`).slice(0, 200);
      return NextResponse.json({ ok: false, error: "stripe_error", detail });
    }
    return NextResponse.json({ ok: true, url: session.url });
  } catch {
    return NextResponse.json({ ok: false, error: "server_error" }, { status: 500 });
  }
}
