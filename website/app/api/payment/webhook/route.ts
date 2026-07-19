import { createHmac, timingSafeEqual } from "crypto";
import { NextRequest, NextResponse } from "next/server";
import { getOrder, markOrderCardPaid, notifyAdmins, notifyCustomerOfStatus } from "@/lib/order-store";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Stripe Webhook —— 卡支付对账的权威通道（补齐「回跳 + client_reference_id」的弱对账）：
 *  客户付完款即便不回跳官网，checkout.session.completed 也会推到这里，订单照样自动到账。
 *
 *  安全模型（零 SDK，自校验签名）：
 *  - 只信 Stripe-Signature：HMAC-SHA256(secret, "{t}.{rawBody}")，timingSafeEqual 防时序侧信道；
 *  - 5 分钟时间戳容差防重放；STRIPE_WEBHOOK_SECRET 未配置时 503（端点未启用）；
 *  - 事件体里只取 client_reference_id / amount_total 做对账，金额不匹配绝不落账（宁漏勿错，
 *    告警管理员人工核）；处理幂等——Stripe 会重试投递，重复事件只回 200 不重复通知。
 *
 *  Stripe 后台配置：Developers → Webhooks → Add endpoint
 *    URL   https://<域名>/api/payment/webhook
 *    事件  checkout.session.completed / checkout.session.async_payment_succeeded
 *          / checkout.session.async_payment_failed
 *    签名密钥（whsec_…）写进服务器 .env.local 的 STRIPE_WEBHOOK_SECRET 并重启。 */

const TOLERANCE_SEC = 300;

/** 解析并校验 Stripe-Signature。通过返回 null，失败返回错误码字符串（给 400 detail）。 */
function verifySignature(rawBody: string, header: string | null, secret: string): string | null {
  if (!header) return "missing_signature";
  let t = "";
  const v1s: string[] = [];
  for (const part of header.split(",")) {
    const [k, v] = part.split("=", 2);
    if (k?.trim() === "t") t = (v ?? "").trim();
    else if (k?.trim() === "v1" && v) v1s.push(v.trim());
  }
  if (!t || v1s.length === 0) return "malformed_signature";
  const ts = Number(t);
  if (!Number.isFinite(ts) || Math.abs(Date.now() / 1000 - ts) > TOLERANCE_SEC) return "timestamp_out_of_tolerance";
  const expected = createHmac("sha256", secret).update(`${t}.${rawBody}`, "utf8").digest("hex");
  const expBuf = Buffer.from(expected, "utf8");
  for (const v1 of v1s) {
    const got = Buffer.from(v1, "utf8");
    if (got.length === expBuf.length && timingSafeEqual(got, expBuf)) return null;
  }
  return "signature_mismatch";
}

export async function POST(req: NextRequest) {
  const secret = (process.env.STRIPE_WEBHOOK_SECRET || "").trim();
  if (!secret) {
    return NextResponse.json({ ok: false, error: "not_configured" }, { status: 503 });
  }

  // 必须用原始字节验签：任何 JSON 反序列化/再序列化都会破坏签名
  const rawBody = await req.text();
  const sigErr = verifySignature(rawBody, req.headers.get("stripe-signature"), secret);
  if (sigErr) {
    return NextResponse.json({ ok: false, error: sigErr }, { status: 400 });
  }

  let event: {
    type?: string;
    data?: {
      object?: {
        id?: string;
        client_reference_id?: string | null;
        payment_status?: string;
        amount_total?: number | null;
      };
    };
  };
  try {
    event = JSON.parse(rawBody);
  } catch {
    return NextResponse.json({ ok: false, error: "bad_json" }, { status: 400 });
  }

  const type = String(event?.type ?? "");
  const session = event?.data?.object ?? {};
  const orderId = String(session.client_reference_id ?? "").trim();

  // 到账事件：completed（同步支付 payment_status=paid）或异步支付成功
  const isPaidEvent =
    (type === "checkout.session.completed" && session.payment_status === "paid") ||
    type === "checkout.session.async_payment_succeeded";
  // 异步支付失败：不改单（订单留在 pending，客户仍可走 USDT/客服），只提醒管理员跟进
  const isFailEvent = type === "checkout.session.async_payment_failed";

  if (!isPaidEvent && !isFailEvent) {
    // 其余事件类型（expired 等）确认收到即可，避免 Stripe 反复重试
    return NextResponse.json({ ok: true, ignored: type || "unknown" });
  }
  if (!orderId) {
    // 不是本站下的单（无 client_reference_id）——确认收到，不处理
    return NextResponse.json({ ok: true, ignored: "no_client_reference_id" });
  }

  if (isFailEvent) {
    await notifyAdmins(
      `⚠️ <b>卡支付（异步）失败</b>\n订单 <code>${orderId}</code> · Stripe session <code>${String(session.id ?? "").slice(0, 66)}</code>\n订单保持待付款，客户可改走 USDT 或联系客服。`
    ).catch(() => {});
    return NextResponse.json({ ok: true, noted: "async_payment_failed" });
  }

  // 先取单、先核金额，全对得上才落账（宁漏勿错：漏了有管理员告警兜底，错了要退款）
  const order = await getOrder(orderId);
  if (!order) {
    // 签名合法但单号找不到：可能是测试模式事件或数据被清，告警人工核
    await notifyAdmins(
      `⚠️ <b>Stripe 到账事件找不到订单</b>\n<code>${orderId}</code> · session <code>${String(session.id ?? "").slice(0, 66)}</code>\n请人工核对 Stripe 后台。`
    ).catch(() => {});
    return NextResponse.json({ ok: true, noted: "order_not_found" });
  }
  const expectedCents = Math.round(order.amount * 100);
  const gotCents = typeof session.amount_total === "number" ? session.amount_total : null;
  if (gotCents != null && gotCents !== expectedCents) {
    await notifyAdmins(
      `🚨 <b>Stripe 到账金额与订单不符，未自动落账，请人工复核</b>\n订单 <code>${order.id}</code> 挂牌 ${expectedCents} 分，session 实收 ${gotCents} 分（<code>${String(session.id ?? "").slice(0, 66)}</code>）。`
    ).catch(() => {});
    return NextResponse.json({ ok: true, noted: "amount_mismatch" });
  }

  const res = await markOrderCardPaid(orderId, String(session.id ?? ""));
  if (!res) {
    return NextResponse.json({ ok: true, noted: "order_not_found" });
  }

  if (res.changed) {
    // 与 USDT 链路同一套到账动作：私信客户 + 通知管理员（失败静默，不影响 200 应答）
    await notifyCustomerOfStatus(res.order, "paid").catch(() => {});
    await notifyAdmins(
      `💳 <b>卡支付到账（Stripe webhook）</b>\n<code>${res.order.id}</code> · ${res.order.plan} · $${res.order.amount}\n联系：${res.order.contact}\n等待履约机自动开通。`
    ).catch(() => {});
  }
  return NextResponse.json({ ok: true, order: res.order.id, changed: res.changed });
}
