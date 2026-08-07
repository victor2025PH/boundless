// /console 总览（经营驾驶舱）：今日待办条 + 账本统计卡 + 本月成交 + 跨售商机 +
// 订单状态分布 + 到期预警 + 支付对账健康 + 快捷入口。
// 2026-08 改版：删掉「阶段路线」（内容随实施推进已过时，项目进度归 docs/）与
// 「开场页漏斗 / A/B 实验」（实验已定稿，营销分析属 /admin 职责）两张卡。
import Link from "next/link";
import {
  AlertTriangle,
  ArrowRight,
  BadgeDollarSign,
  CheckCircle2,
  Flame,
  Gift,
  Inbox,
  KeyRound,
  ListTodo,
  ReceiptText,
  ScrollText,
  Sparkles,
  Users,
} from "lucide-react";
import { getStats } from "@/lib/ledger";
import { getOpportunityStats, listOpportunities, productLabel } from "@/lib/opportunities";
import { getPurgeQueueStats } from "@/lib/personas";
import { claimStats } from "@/lib/trial-claim-store";
import { getStripeSetupStatus, readReconcileHealth } from "@/lib/payment-health";
import { getPaymentSettings } from "@/lib/payment-settings";
import { getConsoleSessionUser } from "@/lib/console-auth";
import { roleAtLeast } from "@/lib/console-users";
import { countUnassignedPaidOrders, revenueSnapshot, type RevenueLine } from "./data";
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
  fmtDateTime,
} from "./parts";
import { OpportunityActions, OpportunityLogBadge } from "./ui";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const QUICK_LINKS = [
  { href: "/console/customers", label: "客户", desc: "客户主档 · 身份归并", Icon: Users },
  { href: "/console/opportunities", label: "商机", desc: "跨售信号 · 跟进", Icon: Sparkles },
  { href: "/console/orders", label: "订单", desc: "订单台账 · 归属客户", Icon: ReceiptText },
  { href: "/console/licenses", label: "授权", desc: "授权台账 · 到期预警", Icon: KeyRound },
  { href: "/console/leads", label: "留资", desc: "留资镜像 · 客户归并", Icon: Inbox },
  { href: "/console/audit", label: "审计", desc: "写操作流水 · 只读", Icon: ScrollText },
] as const;

/** 今日待办条目：count > 0 才渲染（全零时整条显示健康空态）。 */
interface TodoChip {
  key: string;
  label: string;
  count: number;
  href: string;
  Icon: typeof ListTodo;
  /** rose = 需要立刻处理；amber = 尽快处理。 */
  tone: "amber" | "rose";
}

export default async function ConsoleOverviewPage() {
  const me = getConsoleSessionUser();
  if (!me) return null;
  const canWrite = roleAtLeast(me.role, "admin");
  const stats = getStats();
  const oppStats = getOpportunityStats();
  const topOpportunities = listOpportunities({ limit: 5 });
  const revenue = revenueSnapshot();
  const reconcile = await readReconcileHealth(7);
  const paySettings = await getPaymentSettings();
  const stripeSetup = await getStripeSetupStatus({ lastAgoMin: reconcile.lastAgoMin });
  const payLights = [
    { label: "银行卡通道", on: paySettings.card.enabled, note: paySettings.card.enabled ? "已启用" : "未启用" },
    { label: "Stripe Secret", on: !!process.env.STRIPE_SECRET_KEY, note: "STRIPE_SECRET_KEY" },
    { label: "Webhook 对账", on: !!process.env.STRIPE_WEBHOOK_SECRET, note: "STRIPE_WEBHOOK_SECRET" },
  ];
  const empty = stats.orders === 0 && stats.leads === 0 && stats.licenses === 0 && stats.customers === 0;

  // ── 今日待办：跨子系统聚合「现在该有人处理」的计数（全部只读、单点失败不拖垮整条）──
  let purgeOver72h = 0;
  try {
    purgeOver72h = getPurgeQueueStats().pendingOver72h;
  } catch {
    /* personas 表异常按 0 处理 */
  }
  let trialPending = 0;
  let trialGiftWaiting = 0;
  try {
    const t = await claimStats();
    trialPending = t.pending;
    trialGiftWaiting = Math.max(0, t.bindRedeemed - t.topupIssued);
  } catch {
    /* 试用台账缺失按 0 处理 */
  }
  const reconcileAnomalies = reconcile.totals.amount_mismatch + reconcile.totals.order_not_found;
  const todoChips: TodoChip[] = ([
    {
      key: "unassigned",
      label: "成交订单未归属",
      count: countUnassignedPaidOrders(),
      href: "/console/orders?status=paid",
      Icon: ReceiptText,
      tone: "amber",
    },
    {
      key: "expiring",
      label: "授权 30 天内到期",
      count: stats.licensesExpiringIn30d,
      href: "/console/licenses?expiring_days=30",
      Icon: KeyRound,
      tone: "amber",
    },
    {
      key: "purge",
      label: "清除指令滞留超 72h",
      count: purgeOver72h,
      href: "/console/personas/purges?state=pending",
      Icon: Flame,
      tone: "rose",
    },
    {
      key: "trial",
      label: "试用待签发",
      count: trialPending,
      href: "/console/trial",
      Icon: Gift,
      tone: "amber",
    },
    {
      key: "gift",
      label: "赠量待厂商机签发",
      count: trialGiftWaiting,
      href: "/console/trial",
      Icon: Gift,
      tone: "amber",
    },
    {
      key: "reconcile",
      label: "支付对账异常",
      count: reconcileAnomalies,
      href: "#reconcile",
      Icon: AlertTriangle,
      tone: "rose",
    },
  ] satisfies TodoChip[]).filter((c) => c.count > 0);

  // headline 数字已由 getStats 排除测试数据；testCount>0 时副文案追加「+N 测试」提示存在感。
  const totals = [
    { label: "客户", value: stats.customers, sub: `${stats.identities} 条身份标识`, testCount: stats.test.customers, href: "/console/customers" },
    { label: "订单", value: stats.orders, sub: statusSub(stats.ordersByStatus), testCount: stats.test.orders, href: "/console/orders" },
    { label: "授权", value: stats.licenses, sub: `${stats.licensesExpiringIn30d} 条 30 天内到期`, testCount: stats.test.licenses, href: "/console/licenses" },
    { label: "留资", value: stats.leads, sub: `${stats.audit} 条审计流水`, testCount: stats.test.leads, href: "/console/leads" },
  ];

  return (
    <div className="space-y-5">
      <PageHeader
        title="总览"
        desc={`集团账本（customers / orders / licenses / leads）实时统计 · 生成于 ${fmtDateTime(stats.generatedAt)}（站点时区）`}
      />

      {empty && (
        <div className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-4 text-xs leading-relaxed text-amber-200/90">
          <p className="mb-1.5 font-semibold text-amber-300">账本还是空的，三步接入数据：</p>
          <ol className="list-decimal space-y-1 pl-5">
            <li>
              在服务器运行 <Code>node scripts/ledger-backfill.mjs</Code> 回填历史订单与留资（幂等，可重复执行）；
            </li>
            <li>
              用 <Code>node scripts/ledger-import-licenses.mjs &lt;导出json&gt;</Code> 导入幻境 STUDIO / 成杰授权台账；
            </li>
            <li>新订单/新留资已由双写钩子自动入账，无需人工操作。</li>
          </ol>
        </div>
      )}

      {/* 今日待办：打开控制台第一眼先看「现在该干什么」，而不是逐页翻 */}
      <Card className="!py-3">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <span className="inline-flex shrink-0 items-center gap-1.5 text-sm font-semibold text-white">
            <ListTodo className="h-4 w-4 text-amber-400" />
            今日待办
          </span>
          {todoChips.length === 0 ? (
            <span className="inline-flex items-center gap-1.5 text-xs text-slate-500">
              <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" />
              暂无积压：成交已归属 · 无 30 天内到期 · 清除/试用/对账队列全清。
            </span>
          ) : (
            todoChips.map(({ key, label, count, href, Icon, tone }) => (
              <Link
                key={key}
                href={href}
                className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-medium transition ${
                  tone === "rose"
                    ? "border-rose-500/40 bg-rose-500/10 text-rose-300 hover:bg-rose-500/20"
                    : "border-amber-500/40 bg-amber-500/10 text-amber-300 hover:bg-amber-500/20"
                }`}
              >
                <Icon className="h-3.5 w-3.5" />
                {label}
                <b className="tabular-nums">{count}</b>
                <ArrowRight className="h-3 w-3 opacity-70" />
              </Link>
            ))
          )}
        </div>
      </Card>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {totals.map((t) => (
          <Link key={t.label} href={t.href} className="group">
            <Card className="transition hover:border-amber-500/40">
              <p className="text-xs text-slate-500">{t.label}</p>
              <p className="mt-1 text-3xl font-bold tabular-nums text-white group-hover:text-amber-300">{t.value}</p>
              <p className="mt-1.5 text-[11px] text-slate-500">
                {t.sub}
                {t.testCount > 0 && (
                  <span
                    className="ml-1 text-slate-600"
                    title={`另有 ${t.testCount} 条测试/演练数据，未计入主数（列表页 ?test=1 可见）`}
                  >
                    （+{t.testCount} 测试）
                  </span>
                )}
              </p>
            </Card>
          </Link>
        ))}
      </div>

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <SectionTitle>
              <BadgeDollarSign className="mr-0.5 inline h-4 w-4 align-text-bottom text-amber-400" />
              本月成交（账本口径）
            </SectionTitle>
            <span className="text-[11px] text-slate-500">
              paid / activated 订单按产品 × 币种聚合 · 记账时点 = 支付时间 · 不折算汇率
            </span>
          </div>
          {revenue.current.length === 0 ? (
            <p className="text-xs text-slate-500">
              本月还没有成交订单。上月成交 {sumLines(revenue.previous)} 单
              {revenue.previous.length > 0 ? `（${linesBrief(revenue.previous)}）` : ""}。
            </p>
          ) : (
            <>
              <div className="mb-3 flex flex-wrap items-baseline gap-x-5 gap-y-1">
                {totalsByCurrency(revenue.current).map(([cur, amt]) => (
                  <span key={cur} className="text-2xl font-bold tabular-nums text-white">
                    {amt.toLocaleString()} <span className="text-sm font-normal text-slate-400">{cur || "(未标币种)"}</span>
                  </span>
                ))}
                <span className="text-xs text-slate-500">共 {sumLines(revenue.current)} 单</span>
              </div>
              <DataTable head={["产品", "币种", "成交单数", "金额"]}>
                {revenue.current.map((l) => (
                  <tr key={`${l.product_id}|${l.currency}`} className="hover:bg-ink-700/40">
                    <Td className="text-xs text-slate-200">
                      <span className="font-mono">{l.product_id ? productLabel(l.product_id) : "（未标产品）"}</span>
                    </Td>
                    <Td className="text-xs text-slate-400">{l.currency || "—"}</Td>
                    <Td className="text-xs tabular-nums text-slate-300">{l.orders}</Td>
                    <Td className="text-xs font-semibold tabular-nums text-slate-100">{l.amount.toLocaleString()}</Td>
                  </tr>
                ))}
              </DataTable>
              <p className="mt-2 text-[11px] text-slate-500">
                上月：{revenue.previous.length ? linesBrief(revenue.previous) : "无成交"} · 未标产品的订单可在订单台账修正 plan/SKU 后自动归类。
              </p>
            </>
          )}
        </Card>

        <Card
          className={
            stats.licensesExpiringIn30d > 0
              ? "border-amber-500/50 bg-gradient-to-br from-amber-500/10 to-ink-900/60"
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
            续费触达是集团现金流第一优先级：到期前 30 天进入跟进队列，逐条归属客户后在客户 360 里跟进
            （商机卡的「续费在即」与此同源）。
          </p>
        </Card>
      </div>

      <Card className={oppStats.total > 0 ? "border-violet-500/40 bg-gradient-to-br from-violet-500/10 to-ink-900/60" : ""}>
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <SectionTitle count={oppStats.total}>跨售商机（人设总线 P5）</SectionTitle>
          <Link
            href="/console/opportunities"
            className="inline-flex items-center gap-1 text-xs font-medium text-amber-300 underline-offset-2 hover:underline"
          >
            全部商机 <ArrowRight className="h-3.5 w-3.5" />
          </Link>
        </div>
        <div className="mb-4 grid grid-cols-3 gap-3">
          {(
            [
              { kind: "persona_cross_sell", desc: "人设槽位能支撑、未授权未购的产品" },
              { kind: "product_gap_cross_sell", desc: "买了 A 未买同系互补品 B" },
              { kind: "expiring_renewal", desc: "30 天内到期授权 → 续费" },
            ] as const
          ).map((k) => (
            <div key={k.kind} className="rounded-xl border border-ink-700 bg-ink-950/50 p-3">
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
            <DataTable head={["类型", "客户", "从 → 到", "理由", "信号值", "跟进"]}>
              {topOpportunities.map((o) => (
                <tr key={o.oppKey} className="hover:bg-ink-700/40">
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
                  <Td>
                    <span className="inline-flex items-center gap-1.5">
                      <OpportunityLogBadge log={o.log} />
                      {canWrite && (
                        <OpportunityActions
                          oppKey={o.oppKey}
                          kind={o.kind}
                          customerId={o.customerId}
                          toProduct={o.toProduct}
                          log={o.log}
                        />
                      )}
                    </span>
                  </Td>
                </tr>
              ))}
            </DataTable>
          </>
        )}
        <p className="mt-3 text-[11px] leading-relaxed text-slate-500">
          口径：商机由账本（personas / orders / licenses）只读推导；跟进动作落 opportunities_log（schema v4）——
          「跟进」保留在列并降权 −20，「赢单/忽略」默认从清单隐藏（API ?include_closed=1 可带出）。
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
          id="reconcile"
          className={`lg:col-span-2 ${reconcile.totals.amount_mismatch + reconcile.totals.order_not_found > 0 ? "border-rose-500/40" : ""}`}
        >
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <SectionTitle>支付对账健康（近 {reconcile.days} 天）</SectionTitle>
            <span className="text-[11px] text-slate-500">
              双通道：webhook 实时到账 · 每日 04:10 巡检兜底（<Code>stripe-reconcile</Code>）
            </span>
          </div>
          <div className="grid gap-4 lg:grid-cols-3">
            <div className="space-y-2">
              {payLights.map((l) => (
                <div key={l.label} className="flex items-center justify-between text-xs">
                  <span className="flex items-center gap-2 text-slate-400">
                    <span className={`h-2 w-2 rounded-full ${l.on ? "bg-emerald-400" : "bg-slate-600"}`} />
                    {l.label}
                  </span>
                  <span className={l.on ? "text-emerald-300" : "text-slate-500"}>{l.on ? "✓" : l.note}</span>
                </div>
              ))}
              <p className="pt-1 text-[11px] leading-relaxed text-slate-500">
                三灯全绿 = 卡支付双重对账在岗；配置指引见 /admin/payment。
                {stripeSetup.ready
                  ? ` 就绪 ${stripeSetup.done}/${stripeSetup.total}。`
                  : ` 就绪 ${stripeSetup.done}/${stripeSetup.total}（差 ${stripeSetup.total - stripeSetup.done} 步）。`}
              </p>
              {!stripeSetup.ready && (
                <ol className="mt-2 list-decimal space-y-1.5 pl-4 text-[11px] leading-relaxed text-amber-200/85">
                  {stripeSetup.steps
                    .filter((s) => !s.ok && s.id !== "cron_fresh")
                    .map((s) => (
                      <li key={s.id}>
                        <span className="font-medium text-amber-100">{s.label}</span> — {s.hint}
                      </li>
                    ))}
                </ol>
              )}
            </div>
            <div className="lg:col-span-2">
              {reconcile.runs === 0 ? (
                <p className="text-xs text-slate-500">
                  暂无巡检记录 —— cron 每日 04:10 首跑后此处出现趋势（未配置 Stripe 时巡检自动空转，无害）。
                </p>
              ) : (
                <div className="space-y-2 text-xs">
                  <div className="flex flex-wrap items-center gap-x-5 gap-y-1">
                    <span className="text-slate-400">
                      运行 <b className="tabular-nums text-slate-200">{reconcile.runs}</b> 次
                    </span>
                    <span className="text-slate-400">
                      最近一次{" "}
                      <b className="tabular-nums text-slate-200">
                        {reconcile.lastAgoMin != null && reconcile.lastAgoMin < 90
                          ? `${reconcile.lastAgoMin} 分钟前`
                          : reconcile.lastRun?.t.slice(0, 16).replace("T", " ")}
                      </b>
                      {reconcile.lastAgoMin != null && reconcile.lastAgoMin > 26 * 60 && (
                        <span className="ml-1.5 text-amber-300">⚠ 超 26h 未跑，检查 cron</span>
                      )}
                    </span>
                  </div>
                  <div className="flex flex-wrap gap-2">
                    {(
                      [
                        ["巡检补账", reconcile.totals.settled, reconcile.totals.settled > 0 ? "text-amber-300" : "text-slate-200"],
                        ["webhook 已处理", reconcile.totals.already, "text-emerald-300"],
                        ["金额不符", reconcile.totals.amount_mismatch, reconcile.totals.amount_mismatch > 0 ? "text-rose-300" : "text-slate-200"],
                        ["孤儿 session", reconcile.totals.order_not_found, reconcile.totals.order_not_found > 0 ? "text-rose-300" : "text-slate-200"],
                      ] as const
                    ).map(([label, n, cls]) => (
                      <span key={label} className="rounded-lg border border-ink-700 bg-ink-950/50 px-2.5 py-1">
                        <span className="text-slate-500">{label}</span>{" "}
                        <b className={`tabular-nums ${cls}`}>{n}</b>
                      </span>
                    ))}
                  </div>
                  {reconcile.totals.settled > 0 && (
                    <p className="leading-relaxed text-amber-200/80">
                      ⚠ 巡检补过账说明 webhook 有漏投递：最近补账 {reconcile.recovered.slice(-5).join("、")}
                      ，建议检查 Stripe 后台 webhook 投递状态。
                    </p>
                  )}
                </div>
              )}
            </div>
          </div>
        </Card>
      </div>

      <div>
        <SectionTitle>快捷入口</SectionTitle>
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-3">
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

function sumLines(lines: RevenueLine[]): number {
  return lines.reduce((s, l) => s + l.orders, 0);
}

/** 币种 → 金额合计（金额降序）。 */
function totalsByCurrency(lines: RevenueLine[]): [string, number][] {
  const m = new Map<string, number>();
  for (const l of lines) m.set(l.currency ?? "", (m.get(l.currency ?? "") ?? 0) + l.amount);
  return [...m.entries()].sort((a, b) => b[1] - a[1]).map(([c, a]) => [c, Math.round(a * 100) / 100]);
}

/** 上月一行话摘要：「金额 币种（按币种合计）」。 */
function linesBrief(lines: RevenueLine[]): string {
  return totalsByCurrency(lines)
    .map(([cur, amt]) => `${amt.toLocaleString()} ${cur || "(未标币种)"}`)
    .join(" + ");
}
