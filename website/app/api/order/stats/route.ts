import { NextResponse } from "next/server";
import { listOrders } from "@/lib/order-store";

// 公开只读聚合：近 60 天已开通订单的「付款 → 开通」中位时长（分钟）。
// 消费方 = 首屏数据卡（components/HeroStatCards.tsx）——把「≈5 分钟到账」的运营
// 口径换成实测值。隐私面：只回一个中位数与样本量，绝不回单号 / 联系方式 / 金额。
// 守卫：剔除 >24h 的单（自动履约上线前的人工隔夜单会污染口径）；样本 <8 回 null
//（渲染层回落常量）；进程内 10 分钟缓存防刷。
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const WINDOW_DAYS = 60;
const MIN_SAMPLES = 8;
const MAX_MINUTES = 24 * 60;

let cache: { t: number; body: { ok: true; activation_p50_min: number | null; n: number } } | null = null;

export async function GET() {
  if (cache && Date.now() - cache.t < 600_000) {
    return NextResponse.json(cache.body);
  }
  try {
    const orders = await listOrders("activated");
    const cutoff = Date.now() - WINDOW_DAYS * 86_400_000;
    const mins = orders
      .filter((o) => o.paid_at && o.activated_at && new Date(o.activated_at).getTime() >= cutoff)
      .map((o) => (new Date(o.activated_at!).getTime() - new Date(o.paid_at!).getTime()) / 60_000)
      .filter((m) => m >= 0 && m <= MAX_MINUTES)
      .sort((a, b) => a - b);
    const n = mins.length;
    const p50 = n >= MIN_SAMPLES ? Math.round(mins[Math.floor(n / 2)] * 10) / 10 : null;
    const body = { ok: true as const, activation_p50_min: p50, n };
    cache = { t: Date.now(), body };
    return NextResponse.json(body);
  } catch {
    return NextResponse.json({ ok: false }, { status: 500 });
  }
}
