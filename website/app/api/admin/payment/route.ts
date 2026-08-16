import { NextRequest, NextResponse } from "next/server";
import { requireAdmin } from "@/lib/admin-auth";
import {
  getPaymentSettings,
  savePaymentSettings,
  type PaymentSettingsPatch,
} from "@/lib/payment-settings";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function clean(v: unknown, max: number) {
  return String(v ?? "").trim().slice(0, max);
}

/** 回跳 URL 只收 http(s)；其余（含空串）一律落成空 → 结算时用站内兜底地址。 */
function cleanUrl(v: unknown) {
  const s = clean(v, 300);
  return /^https?:\/\//i.test(s) ? s : "";
}

/** 支付渠道设置（管理员）。GET 读全量 + Stripe Secret/Webhook 配置状态；POST 保存补丁。
 *  Secret Key 与 Webhook Secret 永远只在服务器环境变量 —— 这里既不收也不回。 */
export async function GET(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const settings = await getPaymentSettings();
  return NextResponse.json({
    ok: true,
    settings,
    cardSecretConfigured: !!process.env.STRIPE_SECRET_KEY,
    // webhook 未配置时对账只剩「回跳」弱通道：后台设置页据此显示黄色提醒
    webhookConfigured: !!process.env.STRIPE_WEBHOOK_SECRET,
  });
}

export async function POST(req: NextRequest) {
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  let data;
  try {
    data = await req.json();
  } catch {
    return NextResponse.json({ ok: false, error: "bad_json" }, { status: 400 });
  }

  // 白名单式取字段：只认识的键才进补丁，其余一概丢弃；字符串统一 clamp。
  const patch: PaymentSettingsPatch = {};
  if (data?.usdt && typeof data.usdt === "object") {
    patch.usdt = {};
    if ("enabled" in data.usdt) patch.usdt.enabled = !!data.usdt.enabled;
    if ("address" in data.usdt) patch.usdt.address = clean(data.usdt.address, 120);
  }
  if (data?.card && typeof data.card === "object") {
    patch.card = { provider: "stripe" };
    if ("enabled" in data.card) patch.card.enabled = !!data.card.enabled;
    if ("publishableKey" in data.card) patch.card.publishableKey = clean(data.card.publishableKey, 200);
    if ("currency" in data.card) {
      patch.card.currency = clean(data.card.currency, 10).replace(/[^A-Za-z]/g, "").toUpperCase() || "USD";
    }
    if ("successUrl" in data.card) patch.card.successUrl = cleanUrl(data.card.successUrl);
    if ("cancelUrl" in data.card) patch.card.cancelUrl = cleanUrl(data.card.cancelUrl);
  }
  if (!patch.usdt && !patch.card) {
    return NextResponse.json({ ok: false, error: "empty_patch" }, { status: 400 });
  }

  const settings = await savePaymentSettings(patch);
  return NextResponse.json({ ok: true, settings, cardSecretConfigured: !!process.env.STRIPE_SECRET_KEY });
}
