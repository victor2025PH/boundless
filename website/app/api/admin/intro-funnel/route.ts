// GET /api/admin/intro-funnel —— 开场页（IntroCover）转化漏斗统计（/admin 增长分析 tab 消费）。
// 聚合口径统一走 lib/intro-funnel.ts（与 /api/console/intro-funnel、/console 总览卡片同源），
// 避免两套实现各自演化后数字对不上；本路由只负责 /admin 系的鉴权与参数解析。
import { NextRequest, NextResponse } from "next/server";
import { requireAdmin } from "@/lib/admin-auth";
import { readIntroFunnel } from "@/lib/intro-funnel";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(req: NextRequest) {
  // 与 /api/admin/stats 相同的双重守卫：口令未配置视为后台未开通，503 而非放行
  if (!process.env.TELEGRAM_SETUP_KEY && !process.env.ADMIN_KEY) {
    return NextResponse.json({ ok: false, error: "not_configured" }, { status: 503 });
  }
  if (!requireAdmin(req)) {
    return NextResponse.json({ ok: false, error: "unauthorized" }, { status: 401 });
  }
  const url = new URL(req.url);
  const days = Number(url.searchParams.get("days") ?? "7");
  // include_bots=1：带回自动化流量（默认排除 HeadlessChrome/爬虫，防污染实验读数）
  const includeBots = url.searchParams.get("include_bots") === "1";
  // /admin 前端期望裸对象（无 ok 包装），保持既有消费契约
  return NextResponse.json(await readIntroFunnel(days, { includeBots }));
}
