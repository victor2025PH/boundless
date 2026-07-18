// /console 总览：账本统计卡 + 跨售商机 + 订单状态分布 + 到期预警 + 快捷入口 + 阶段路线。
import Link from "next/link";
import { AlertTriangle, ArrowRight, Inbox, KeyRound, ReceiptText, Sparkles, Users } from "lucide-react";
import { getStats } from "@/lib/ledger";
import { getOpportunityStats, listOpportunities, productLabel } from "@/lib/opportunities";
import { hasConsoleSession } from "@/lib/console-auth";
import {
  Card,
  Code,
  CustomerLink,
  DataTable,
  OpportunityKindBadge,
  OrderStatusBadge,
  PageHeader,
  SectionTitle,
  Td,
} from "./parts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const ROADMAP = [
  { phase: "P0/P1", label: "集团账本 + 授权台账（本台）", state: "live" },
  { phase: "P2", label: "SSO 统一登录 / RBAC 权限", state: "next" },
  { phase: "P3", label: "产品双后台（AvatarHub / 成杰）接入", state: "plan" },
  { phase: "P4", label: "集团 KPI 看板", state: "plan" },
  { phase: "P5", label: "人设总线（跨产品客户画像）", state: "plan" },
] as const;

const QUICK_LINKS = [
  { href: "/console/customers", label: "客户", desc: "客户主档 · 身份归并", Icon: Users },
  { href: "/console/orders", label: "订单", desc: "订单台账 · 归属客户", Icon: ReceiptText },
  { href: "/console/licenses", label: "授权", desc: "授权台账 · 到期预警", Icon: KeyRound },
  { href: "/console/leads", label: "留资", desc: "留资镜像 · 客户归并", Icon: Inbox },
] as const;

export default function ConsoleOverviewPage() {
  if (!hasConsoleSession()) return null;
  const stats = getStats();
  const oppStats = getOpportunityStats();
  const topOpportunities = listOpportunities({ limit: 5 });
  const empty = stats.orders === 0 && stats.leads === 0 && stats.licenses === 0 && stats.customers === 0;

  const totals = [
    { label: "客户", value: stats.customers, sub: `${stats.identities} 条身份标识`, href: "/console/customers" },
    { label: "订单", value: stats.orders, sub: statusSub(stats.ordersByStatus), href: "/console/orders" },
    { label: "授权", value: stats.licenses, sub: `${stats.licensesExpiringIn30d} 条 30 天内到期`, href: "/console/licenses" },
    { label: "留资", value: stats.leads, sub: `${stats.audit} 条审计流水`, href: "/console/leads" },
  ];

  return (
    <div className="space-y-5">
      <PageHeader
        title="总览"
        desc={`集团账本（customers / orders / licenses / leads）实时统计 · 生成于 ${stats.generatedAt.slice(0, 19).replace("T", " ")} UTC`}
      />

      {empty && (
        <div className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-4 text-xs leading-relaxed text-amber-200/90">
          <p className="mb-1.5 font-semibold text-amber-300">账本还是空的，三步接入数据：</p>
          <ol className="list-decimal space-y-1 pl-5">
            <li>
              在服务器运行 <Code>node scripts/ledger-backfill.mjs</Code> 回填历史订单与留资（幂等，可重复执行）；
            </li>
            <li>
              用 <Code>node scripts/ledger-import-licenses.mjs &lt;导出json&gt;</Code> 导入 AvatarHub / 成杰授权台账；
            </li>
            <li>新订单/新留资已由双写钩子自动入账，无需人工操作。</li>
          </ol>
        </div>
      )}

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {totals.map((t) => (
          <Link key={t.label} href={t.href} className="group">
            <Card className="transition hover:border-amber-500/40">
              <p className="text-xs text-slate-500">{t.label}</p>
              <p className="mt-1 text-3xl font-bold tabular-nums text-white group-hover:text-amber-300">{t.value}</p>
              <p className="mt-1.5 text-[11px] text-slate-500">{t.sub}</p>
            </Card>
          </Link>
        ))}
      </div>

      <Card className={oppStats.total > 0 ? "border-violet-500/40 bg-gradient-to-br from-violet-500/10 to-slate-900/60" : ""}>
        <SectionTitle count={oppStats.total}>跨售商机（人设总线 P5 · 只读试运行）</SectionTitle>
        <div className="mb-4 grid grid-cols-3 gap-3">
          {(
            [
              { kind: "persona_cross_sell", desc: "人设槽位能支撑、未授权未购的产品" },
              { kind: "product_gap_cross_sell", desc: "买了 A 未买同系互补品 B" },
              { kind: "expiring_renewal", desc: "30 天内到期授权 → 续费" },
            ] as const
          ).map((k) => (
            <div key={k.kind} className="rounded-xl border border-slate-800 bg-slate-950/50 p-3">
              <OpportunityKindBadge kind={k.kind} />
              <p className="mt-2 text-2xl font-bold tabular-nums text-white">{oppStats.byKind[k.kind]}</p>
              <p className="mt-1 text-[11px] leading-relaxed text-slate-500">{k.desc}</p>
            </div>
          ))}
        </div>
        {topOpportunities.length === 0 ? (
          <p className="flex items-center gap-1.5 text-xs text-slate-500">
            <Sparkles className="h-3.5 w-3.5" />
            暂无商机信号 —— 人设归属客户、订单/授权入账后自动出现。
          </p>
        ) : (
          <>
            <p className="mb-2 text-[11px] text-slate-500">Top {topOpportunities.length} 商机（按信号值排序，点客户进 360 跟进）：</p>
            <DataTable head={["类型", "客户", "从 → 到", "理由", "信号值"]}>
              {topOpportunities.map((o, i) => (
                <tr key={`${o.kind}-${o.customerId}-${o.toProduct}-${i}`} className="hover:bg-slate-800/40">
                  <Td>
                    <OpportunityKindBadge kind={o.kind} />
                  </Td>
                  <Td>
                    <CustomerLink customerId={o.customerId} label={o.customerName} />
                  </Td>
                  <Td className="text-xs text-slate-300">
                    <span className="font-mono">{productLabel(o.fromProduct)}</span>
                    <span className="mx-1.5 text-slate-600">→</span>
                    <span className="font-mono text-amber-300">{productLabel(o.toProduct)}</span>
                  </Td>
                  <Td className="max-w-[320px] text-xs text-slate-400">
                    <span className="block truncate" title={o.reason}>{o.reason}</span>
                  </Td>
                  <Td className="text-xs font-semibold tabular-nums text-slate-200">{o.signalValue}</Td>
                </tr>
              ))}
            </DataTable>
          </>
        )}
        <p className="mt-3 text-[11px] leading-relaxed text-slate-500">
          口径：商机由账本（personas / orders / licenses）只读推导，不落库；「标记已跟进」待 opportunities_log 表（下阶段）。
        </p>
      </Card>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-1">
          <SectionTitle>订单状态分布</SectionTitle>
          {Object.keys(stats.ordersByStatus).length ? (
            <ul className="space-y-2.5">
              {Object.entries(stats.ordersByStatus)
                .sort((a, b) => b[1] - a[1])
                .map(([status, count]) => (
                  <li key={status} className="flex items-center justify-between gap-2">
                    <OrderStatusBadge status={status === "(null)" ? null : status} />
                    <span className="text-sm font-semibold tabular-nums text-slate-200">{count}</span>
                  </li>
                ))}
            </ul>
          ) : (
            <p className="text-xs text-slate-500">暂无订单数据。</p>
          )}
        </Card>

        <Card
          className={
            stats.licensesExpiringIn30d > 0
              ? "border-amber-500/50 bg-gradient-to-br from-amber-500/10 to-slate-900/60"
              : ""
          }
        >
          <SectionTitle>到期预警</SectionTitle>
          <div className="flex items-start gap-3">
            <AlertTriangle
              className={`mt-0.5 h-8 w-8 shrink-0 ${stats.licensesExpiringIn30d > 0 ? "text-amber-400" : "text-slate-600"}`}
            />
            <div>
              <p className="text-3xl font-bold tabular-nums text-white">
                {stats.licensesExpiringIn30d}
                <span className="ml-1.5 text-xs font-normal text-slate-400">条授权 30 天内到期</span>
              </p>
              <Link
                href="/console/licenses?expiring_days=30"
                className="mt-2 inline-flex items-center gap-1 text-xs font-medium text-amber-300 underline-offset-2 hover:underline"
              >
                查看到期清单 <ArrowRight className="h-3.5 w-3.5" />
              </Link>
            </div>
          </div>
          <p className="mt-3 text-[11px] leading-relaxed text-slate-500">
            续费触达是集团现金流第一优先级：到期前 30 天进入跟进队列，逐条归属客户后在客户 360 里跟进。
          </p>
        </Card>

        <Card>
          <SectionTitle>阶段路线</SectionTitle>
          <ul className="space-y-2.5">
            {ROADMAP.map((r) => (
              <li key={r.phase} className="flex items-center gap-2.5 text-xs">
                <span
                  className={`h-2 w-2 shrink-0 rounded-full ${
                    r.state === "live" ? "bg-emerald-400" : r.state === "next" ? "bg-amber-400" : "bg-slate-600"
                  }`}
                />
                <span className="w-12 shrink-0 font-mono font-semibold text-slate-400">{r.phase}</span>
                <span className={r.state === "live" ? "text-slate-200" : "text-slate-400"}>{r.label}</span>
                {r.state === "live" && (
                  <span className="rounded-full bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-medium text-emerald-300">
                    已上线
                  </span>
                )}
              </li>
            ))}
          </ul>
        </Card>
      </div>

      <div>
        <SectionTitle>快捷入口</SectionTitle>
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          {QUICK_LINKS.map(({ href, label, desc, Icon }) => (
            <Link key={href} href={href}>
              <Card className="flex items-center gap-3 transition hover:border-amber-500/40">
                <Icon className="h-6 w-6 shrink-0 text-amber-400/80" />
                <div>
                  <p className="text-sm font-semibold text-white">{label}</p>
                  <p className="text-[11px] text-slate-500">{desc}</p>
                </div>
              </Card>
            </Link>
          ))}
        </div>
      </div>
    </div>
  );
}

function statusSub(byStatus: Record<string, number>): string {
  const paid = (byStatus.paid ?? 0) + (byStatus.activated ?? 0);
  const pending = byStatus.pending ?? 0;
  return `已成交 ${paid} · 待支付 ${pending}`;
}
