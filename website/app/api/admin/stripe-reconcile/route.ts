import { NextRequest, NextResponse } from "next/server";
import { requireAdmin } from "@/lib/admin-auth";
import { stripeSecret } from "@/lib/payment-settings";
import { settleCardPaidSession } from "@/lib/stripe-settle";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Stripe 对账巡检 —— 卡支付的第二重对账（webhook 之外的兜底）：
 *  拉取最近 N 小时「已支付」的 Checkout Session，与订单库逐单比对，
 *  webhook 停摆/漏投递期间的到账单在这里自动补账（幂等，同一策略源 lib/stripe-settle）。
 *
 *  谁调用：服务器 cron 每日 04:10 本机 curl（与 order-sla 同一模式，见 _setup_cron.sh）；
 *  也可随时手动触发。未配置 STRIPE_SECRET_KEY 时返回 not_configured（卡通道未启用，无害）。
 *
 *  参数：hours 回看窗口（默认 25 略盖过 24h cron 周期，上限 168=7 天）。
 *  静默原则：全部对得上不打扰任何人；只有补账/金额不符/孤儿 session 才通知管理员
 *  （通知在 settleCardPaidSession 内统一发）。 */

// 测试缝：冒烟用本地 mock 服务替身；生产恒为官方地址（env 未设时）
const API_BASE = () => (process.env.STRIPE_API_BASE || "https://api.stripe.com").replace(/\/$/, "");
const PAGE_LIMIT = 100;
const MAX_PAGES = 5; // 单次最多 500 个 session：日常一天的量级足够，防异常数据拖死巡检

interface StripeSession {
  id?: string;
  client_reference_id?: string | null;
  payment_status?: string;
  amount_total?: number | null;
}

export async function GET(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const secret = stripeSecret();
  if (!secret) {
    return NextResponse.json({ ok: false, error: "not_configured" });
  }

  const hoursRaw = Number(req.nextUrl.searchParams.get("hours") ?? "25");
  const hours = Number.isFinite(hoursRaw) && hoursRaw > 0 ? Math.min(Math.floor(hoursRaw), 168) : 25;
  const createdGte = Math.floor(Date.now() / 1000) - hours * 3600;

  let sessionsChecked = 0;
  let paidSessions = 0;
  const counts = { settled: 0, already: 0, amount_mismatch: 0, order_not_found: 0, foreign: 0 };
  const recovered: string[] = [];

  let startingAfter = "";
  for (let page = 0; page < MAX_PAGES; page++) {
    const qs = new URLSearchParams({ limit: String(PAGE_LIMIT), "created[gte]": String(createdGte) });
    if (startingAfter) qs.set("starting_after", startingAfter);
    let resp: Response;
    try {
      resp = await fetch(`${API_BASE()}/v1/checkout/sessions?${qs}`, {
        headers: { Authorization: `Bearer ${secret}` },
      });
    } catch {
      return NextResponse.json({ ok: false, error: "stripe_unreachable" }, { status: 502 });
    }
    const body = (await resp.json().catch(() => null)) as { data?: StripeSession[]; has_more?: boolean } | null;
    if (!resp.ok || !Array.isArray(body?.data)) {
      return NextResponse.json(
        { ok: false, error: "stripe_error", status: resp.status },
        { status: 502 }
      );
    }

    for (const s of body.data) {
      sessionsChecked++;
      if (s.payment_status !== "paid") continue;
      paidSessions++;
      const orderId = String(s.client_reference_id ?? "").trim();
      // 无单号或非本站单号格式：他处创建的 session（如 Dashboard 手动收款），不属于订单对账范围
      if (!/^AH-\d{8}-[A-Z0-9]{4,10}$/i.test(orderId)) {
        counts.foreign++;
        continue;
      }
      const outcome = await settleCardPaidSession({
        orderId,
        sessionId: String(s.id ?? ""),
        amountTotal: typeof s.amount_total === "number" ? s.amount_total : null,
        via: "reconcile",
      });
      counts[outcome.result]++;
      if (outcome.result === "settled") recovered.push(orderId);
    }

    if (!body.has_more || body.data.length === 0) break;
    startingAfter = String(body.data[body.data.length - 1]?.id ?? "");
    if (!startingAfter) break;
  }

  return NextResponse.json({
    ok: true,
    windowHours: hours,
    sessionsChecked,
    paidSessions,
    // settled = 巡检补上的漏单（webhook 没接住的），already = webhook 已处理过的正常单
    ...counts,
    recovered,
  });
}
