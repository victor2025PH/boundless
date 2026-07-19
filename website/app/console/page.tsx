// /console 总览：账本统计卡 + 跨售商机 + 订单状态分布 + 到期预警 + 快捷入口 + 阶段路线。
import Link from "next/link";
import { AlertTriangle, ArrowRight, Inbox, KeyRound, ReceiptText, ScrollText, Sparkles, Users } from "lucide-react";
import { getStats } from "@/lib/ledger";
import { getOpportunityStats, listOpportunities, productLabel } from "@/lib/opportunities";
import { readIntroFunnel, readIntroExperiments } from "@/lib/intro-funnel";
import { readReconcileHealth } from "@/lib/payment-health";
import { getPaymentSettings } from "@/lib/payment-settings";
import { getConsoleSessionUser } from "@/lib/console-auth";
import { roleAtLeast } from "@/lib/console-users";
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
import { OpportunityActions, OpportunityLogBadge } from "./opportunities-ui";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const ROADMAP = [
  { phase: "P0/P1", label: "集团账本 + 授权台账（本台）", state: "done" },
  { phase: "P2", label: "控制台登录 / RBAC", state: "done" },
  { phase: "P3", label: "全域事件收集", state: "done" },
  { phase: "P4", label: "集团 KPI 看板", state: "done" },
  { phase: "P5", label: "人设总线 + 跨售商机", state: "done" },
  { phase: "下一阶段", label: ".117 真迁 / grant enforce 切强制 / 四视图接线试点", state: "next" },
] as const;

// 开场页 A/B 实验 → 人话（与 IntroCover 的 abVariant 实验 id 一一对应）
const INTRO_EXPERIMENT_LABELS: Record<string, { name: string; variants: Record<string, string> }> = {
  intro_auto_enter: { name: "自动进入（无操作 12s）", variants: { a: "对照·不自动", b: "自动进入" } },
  intro_btn_shape: { name: "进入按钮形状", variants: { a: "有机波浪（现行）", b: "标准胶囊（对照）" } },
};

const QUICK_LINKS = [
  { href: "/console/customers", label: "客户", desc: "客户主档 · 身份归并", Icon: Users },
  { href: "/console/opportunities", label: "商机", desc: "跨售信号 · 跟进", Icon: Sparkles },
  { href: "/console/orders", label: "订单", desc: "订单台账 · 归属客户", Icon: ReceiptText },
  { href: "/console/licenses", label: "授权", desc: "授权台账 · 到期预警", Icon: KeyRound },
  { href: "/console/leads", label: "留资", desc: "留资镜像 · 客户归并", Icon: Inbox },
  { href: "/console/audit", label: "审计", desc: "写操作流水 · 只读", Icon: ScrollText },
] as const;

export default async function ConsoleOverviewPage() {
  const me = getConsoleSessionUser();
  if (!me) return null;
  const canWrite = roleAtLeast(me.role, "admin");
  const stats = getStats();
  const oppStats = getOpportunityStats();
  const topOpportunities = listOpportunities({ limit: 5 });
  const introFunnel = await readIntroFunnel(7);
  const introExperiments = await readIntroExperiments(7);
  const reconcile = await readReconcileHealth(7);
  const paySettings = await getPaymentSettings();
  const payLights = [
    { label: "银行卡通道", on: paySettings.card.enabled, note: paySettings.card.enabled ? "已启用" : "未启用" },
    { label: "Stripe Secret", on: !!process.env.STRIPE_SECRET_KEY, note: "STRIPE_SECRET_KEY" },
    { label: "Webhook 对账", on: !!process.env.STRIPE_WEBHOOK_SECRET, note: "STRIPE_WEBHOOK_SECRET" },
  ];
  const empty = stats.orders === 0 && stats.leads === 0 && stats.licenses === 0 && stats.customers === 0;

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

      <Card className={oppStats.total > 0 ? "border-violet-500/40 bg-gradient-to-br from-violet-500/10 to-slate-900/60" : ""}>
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
            <DataTable head={["类型", "客户", "从 → 到", "理由", "信号值", "跟进"]}>
              {topOpportunities.map((o) => (
                <tr key={o.oppKey} className="hover:bg-slate-800/40">
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
                    r.state === "done" ? "bg-emerald-400" : r.state === "next" ? "bg-amber-400" : "bg-slate-600"
                  }`}
                />
                <span className="w-14 shrink-0 font-mono font-semibold text-slate-400">{r.phase}</span>
                <span className={r.state === "done" ? "text-slate-200" : "text-slate-400"}>{r.label}</span>
                {r.state === "done" && (
                  <span className="rounded-full bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-medium text-emerald-300">
                    已交付
                  </span>
                )}
                {r.state === "next" && (
                  <span className="rounded-full bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-amber-300">
                    进行中
                  </span>
                )}
              </li>
            ))}
          </ul>
        </Card>
      </div>

      <Card>
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <SectionTitle>开场页漏斗（近 {introFunnel.days} 天）</SectionTitle>
          <span className="text-[11px] text-slate-500">口径：按会话（sid）去重 · 已滤自动化 UA · 来源 events.jsonl</span>
        </div>
        {introFunnel.sessions.shown === 0 ? (
          <p className="text-xs text-slate-500">
            暂无开场页事件 —— 首页开场（IntroCover）被访问后自动上报 intro_shown / intro_first_gesture /
            intro_sound_on / intro_enter 四事件。
          </p>
        ) : (
          <div className="grid gap-4 lg:grid-cols-3">
            <div className="lg:col-span-2">
              {(
                [
                  { label: "开场展示", value: introFunnel.sessions.shown, rate: 1 },
                  { label: "首次交互", value: introFunnel.sessions.gesture, rate: introFunnel.rates.gestureRate },
                  { label: "开启音效", value: introFunnel.sessions.soundOn, rate: introFunnel.rates.soundRate },
                  { label: "进入正文", value: introFunnel.sessions.enter, rate: introFunnel.rates.enterRate },
                ] as const
              ).map((s) => (
                <div key={s.label} className="mb-2.5 last:mb-0">
                  <div className="mb-1 flex items-center justify-between text-xs">
                    <span className="text-slate-400">{s.label}</span>
                    <span className="tabular-nums text-slate-200">
                      {s.value}
                      <span className="ml-1.5 text-[11px] text-slate-500">{Math.round(s.rate * 100)}%</span>
                    </span>
                  </div>
                  <div className="h-1.5 overflow-hidden rounded-full bg-slate-800">
                    <div
                      className="h-full rounded-full bg-gradient-to-r from-cyan-400/80 to-violet-400/80"
                      style={{ width: `${Math.max(2, Math.round(s.rate * 100))}%` }}
                    />
                  </div>
                </div>
              ))}
            </div>
            <div className="space-y-3 text-xs">
              <div>
                <p className="mb-1.5 text-slate-500">进入方式</p>
                <ul className="space-y-1">
                  {(
                    [
                      ["点击按钮", introFunnel.enterByMethod.click],
                      ["滚动", introFunnel.enterByMethod.scroll],
                      ["触屏上滑", introFunnel.enterByMethod.touch],
                      ["键盘", introFunnel.enterByMethod.key],
                      ["自动进入（AB·B桶）", introFunnel.enterByMethod.auto],
                    ] as const
                  ).map(([label, n]) => (
                    <li key={label} className="flex items-center justify-between">
                      <span className="text-slate-400">{label}</span>
                      <span className="tabular-nums text-slate-200">{n}</span>
                    </li>
                  ))}
                </ul>
              </div>
              <div>
                <p className="mb-1 text-slate-500">展示 → 进入停留</p>
                <p className="text-slate-200">
                  中位 <span className="font-semibold tabular-nums">{(introFunnel.dwellMs.median / 1000).toFixed(1)}s</span>
                  <span className="mx-1.5 text-slate-600">·</span>
                  p90 <span className="font-semibold tabular-nums">{(introFunnel.dwellMs.p90 / 1000).toFixed(1)}s</span>
                </p>
              </div>
              <p className="leading-relaxed text-slate-500">
                进入率长期低于 60% 或 p90 停留超 20s 时，考虑缩短开场或加自动进入。API：
                <Code>/api/console/intro-funnel?days=7</Code>
              </p>
            </div>
          </div>
        )}

        {/* A/B 实验读数：按会话把曝光桶与进入行为连起来，决策直接看每桶进入率与停留 */}
        {introExperiments.experiments.length > 0 && (
          <div className="mt-4 border-t border-slate-800 pt-3">
            <p className="mb-2 text-[11px] text-slate-500">
              A/B 实验读数（近 {introExperiments.days} 天 · 会话级连接）：进入率差 ≥5 个百分点且各桶展示 ≥50 才有决策意义
            </p>
            <div className="grid gap-2 text-xs lg:grid-cols-2">
              {introExperiments.experiments.map((exp) => {
                const meta = INTRO_EXPERIMENT_LABELS[exp.experiment];
                return (
                  <div key={exp.experiment} className="rounded-xl border border-slate-800 bg-slate-950/40 p-3">
                    <p className="mb-1.5 font-medium text-slate-300">{meta?.name ?? exp.experiment}</p>
                    <ul className="space-y-1">
                      {Object.entries(exp.variants).map(([variant, v]) => (
                        <li key={variant} className="flex items-center justify-between gap-2">
                          <span className="text-slate-400">
                            {variant.toUpperCase()} · {meta?.variants[variant] ?? variant}
                          </span>
                          <span className="shrink-0 tabular-nums text-slate-200">
                            {v.shown} 展示 · 进入 {Math.round(v.enterRate * 100)}%
                            <span className="ml-1.5 text-[11px] text-slate-500">
                              停留中位 {(v.dwellMs.median / 1000).toFixed(1)}s
                            </span>
                          </span>
                        </li>
                      ))}
                    </ul>
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </Card>

      <Card className={reconcile.totals.amount_mismatch + reconcile.totals.order_not_found > 0 ? "border-rose-500/40" : ""}>
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
            </p>
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
                    <span key={label} className="rounded-lg border border-slate-800 bg-slate-950/50 px-2.5 py-1">
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
