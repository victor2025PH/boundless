import { NextResponse } from "next/server";
import { getPublicPaymentSettings } from "@/lib/payment-settings";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** 公开的可用支付方式（非机密子集）：结算弹窗据此决定展示 USDT / 银行卡入口。 */
export async function GET() {
  const pub = await getPublicPaymentSettings();
  return NextResponse.json({ ok: true, ...pub });
}
