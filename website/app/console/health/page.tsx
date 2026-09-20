// /console/health：独立页已撤销（2026-08 改版）——静态五机台账并入 /console/kpi
// 页脚折叠卡（「伪监控感」问题：公网 VPS 探不到内网五机，独立页名不副实）。
// 保留路由做重定向，旧书签/旧链接不断；?probe=1 透传（到 kpi 页自动展开台账并探测）。
import { redirect } from "next/navigation";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export default function HealthPage({ searchParams }: { searchParams: { probe?: string } }) {
  const probe = ["1", "true"].includes(searchParams.probe ?? "");
  redirect(probe ? "/console/kpi?probe=1" : "/console/kpi");
}
