import { getOrder, markOrderCardPaid, notifyAdmins, notifyCustomerOfStatus, type OrderEntry } from "./order-store";

// 卡支付落账的单一策略源 —— webhook（实时）与 stripe-reconcile（每日巡检）共用，
// 保证两条对账通道的行为完全一致：先核金额、后落账、幂等去重、同一套通知。
// 为什么抽库：结算策略散在两处必然漂移（改一处漏一处），资金链路不允许。

export type SettleOutcome =
  | { result: "settled"; order: OrderEntry }
  | { result: "already"; order: OrderEntry }
  | { result: "amount_mismatch" }
  | { result: "order_not_found" };

const VIA_LABEL = { webhook: "Stripe webhook", reconcile: "对账巡检" } as const;

/** 把一个「已支付的 Checkout Session」结算到订单上（宁漏勿错：金额不符绝不自动落账）。
 *  幂等：订单已 paid/activated 时只补 session id、不重复通知。通知失败静默，不影响结果。 */
export async function settleCardPaidSession(args: {
  orderId: string;
  sessionId: string;
  /** Stripe amount_total（分）；null = 事件体未携带，跳过金额校验。 */
  amountTotal: number | null;
  via: keyof typeof VIA_LABEL;
}): Promise<SettleOutcome> {
  const { orderId, sessionId, amountTotal, via } = args;
  const sessionShort = sessionId.slice(0, 66);

  const order = await getOrder(orderId);
  if (!order) {
    await notifyAdmins(
      `⚠️ <b>Stripe 已付 session 找不到订单（${VIA_LABEL[via]}）</b>\n` +
        `订单号 <code>${orderId}</code> · session <code>${sessionShort}</code>\n请人工核对 Stripe 后台。`
    ).catch(() => {});
    return { result: "order_not_found" };
  }

  const expectedCents = Math.round(order.amount * 100);
  if (amountTotal != null && amountTotal !== expectedCents) {
    await notifyAdmins(
      `🚨 <b>Stripe 到账金额与订单不符，未自动落账（${VIA_LABEL[via]}）</b>\n` +
        `订单 <code>${order.id}</code> 挂牌 ${expectedCents} 分，session 实收 ${amountTotal} 分（<code>${sessionShort}</code>）。请人工复核。`
    ).catch(() => {});
    return { result: "amount_mismatch" };
  }

  const res = await markOrderCardPaid(orderId, sessionId, via === "webhook" ? "stripe_webhook" : "stripe_reconcile");
  if (!res) return { result: "order_not_found" };
  if (!res.changed) return { result: "already", order: res.order };

  // 与 USDT 到账同一套动作：私信客户 + 通知管理员
  await notifyCustomerOfStatus(res.order, "paid").catch(() => {});
  await notifyAdmins(
    `💳 <b>卡支付到账（${VIA_LABEL[via]}）</b>\n` +
      `<code>${res.order.id}</code> · ${res.order.plan} · $${res.order.amount}\n联系：${res.order.contact}\n等待履约机自动开通。` +
      (via === "reconcile" ? "\n⚠️ 该单由巡检补账 —— webhook 可能有漏投递，建议检查 Stripe 后台 webhook 状态。" : "")
  ).catch(() => {});
  return { result: "settled", order: res.order };
}
