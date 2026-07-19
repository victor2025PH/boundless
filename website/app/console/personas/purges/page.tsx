// /console/personas/purges：人设清除队列监控（P5 运营收尾）。
// 全局视角盯 purge 协议执行：persona_purges 指令级队列 —— 待回执积压、滞留告警
//（>24h 琥珀 / >72h 玫红）、逐引擎积压与最早滞留、回执时延。只读视图；发起清除
// 仍在人设详情页危险区（admin+），引擎回执走 /api/sync/personas/purges 机器通道。
import Link from "next/link";
import { AlertTriangle, ArrowLeft, CheckCircle2, Hourglass, Timer } from "lucide-react";
import { getConsoleSessionUser } from "@/lib/console-auth";
import { PRODUCT_ENGINE_MAP, getPurgeQueueStats, listPurgeQueue } from "@/lib/personas";
import {
  Card,
  Code,
  DataTable,
  EmptyState,
  FilterSubmit,
  PageHeader,
  Pager,
  PersonaSlotCells,
  PersonaStatusBadge,
  SectionTitle,
  SystemBadge,
  Td,
  filterInputCls,
  fmtDateTime,
} from "../../parts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const LIMIT = 50;
const STATE_CHOICES = [
  { value: "", label: "全部指令" },
  { value: "pending", label: "待回执 pending" },
  { value: "acked", label: "已回执 acked" },
] as const;

/** ISO 起点 → 到 toMs 的小时数；不可解析返回 null。 */
function hoursBetween(fromIso: string | null, toMs: number): number | null {
  if (!fromIso) return null;
  const t = Date.parse(fromIso);
  if (!Number.isFinite(t)) return null;
  return (toMs - t) / 3600_000;
}

/** 小时数 → "X 分钟" / "X 小时" / "X 天 Y 小时"。 */
function fmtDuration(hours: number): string {
  if (hours < 0) return "—";
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))} 分钟`;
  if (hours < 48) return `${Math.round(hours * 10) / 10} 小时`;
  return `${Math.floor(hours / 24)} 天 ${Math.round(hours % 24)} 小时`;
}

/** 待回执等待时长（滞留分级上色：≥72h 玫红 / ≥24h 琥珀 / 其余石板）。 */
function PendingAge({ requestedAt, now }: { requestedAt: string | null; now: number }) {
  const h = hoursBetween(requestedAt, now);
  if (h == null) return <span className="text-amber-300">待回执</span>;
  const cls = h >= 72 ? "font-semibold text-rose-300" : h >= 24 ? "font-medium text-amber-300" : "text-slate-300";
  return (
    <span className={cls}>
      待回执 · 已等 {fmtDuration(h)}
      {h >= 72 && "（滞留）"}
    </span>
  );
}

export default function PurgeQueuePage({
  searchParams,
}: {
  searchParams: { target?: string; state?: string; q?: string; offset?: string };
}) {
  const me = getConsoleSessionUser();
  if (!me) return null;
  const target = searchParams.target?.trim() || undefined;
  const state = searchParams.state?.trim() || undefined;
  const q = searchParams.q?.trim() || undefined;
  const offset = Math.max(0, Number(searchParams.offset) || 0);
  const now = Date.now();

  const stats = getPurgeQueueStats();
  const { rows, total } = listPurgeQueue({ target, state, q, limit: LIMIT, offset });

  // 引擎筛选项：已知承载引擎 ∪ 队列里实际出现过的目标系统
  const engineChoices = [
    ...new Set([...Object.values(PRODUCT_ENGINE_MAP), ...stats.byTarget.map((t) => t.target_system)]),
  ].sort();

  const hasFilter = !!(target || state || q);
  const oldestPendingHours = hoursBetween(stats.oldestPendingAt, now);

  const cards = [
    {
      label: "待回执指令",
      value: stats.pendingDirectives,
      sub: stats.pendingDirectives
        ? `最早一条已等 ${oldestPendingHours != null ? fmtDuration(oldestPendingHours) : "—"}`
        : "队列无积压",
      Icon: Hourglass,
      tone: stats.pendingDirectives ? "text-amber-400" : "text-slate-600",
      cls: "",
    },
    {
      label: "滞留告警（>24h 未回执）",
      value: stats.pendingOver24h,
      sub: `其中超 72h ${stats.pendingOver72h} 条`,
      Icon: AlertTriangle,
      tone: stats.pendingOver72h ? "text-rose-400" : stats.pendingOver24h ? "text-amber-400" : "text-slate-600",
      cls: stats.pendingOver72h
        ? "border-rose-500/50 bg-gradient-to-br from-rose-500/10 to-slate-900/60"
        : stats.pendingOver24h
          ? "border-amber-500/50 bg-gradient-to-br from-amber-500/10 to-slate-900/60"
          : "",
    },
    {
      label: "已回执（累计）",
      value: stats.ackedDirectives,
      sub: `近 7 天回执 ${stats.ackedLast7d} 条`,
      Icon: CheckCircle2,
      tone: "text-emerald-400",
      cls: "",
    },
    {
      label: "平均回执时延",
      value: stats.avgAckHours != null ? `${stats.avgAckHours}h` : "—",
      sub: `清除中人设 ${stats.personasPurgePending} · 已清除 ${stats.personasPurged}`,
      Icon: Timer,
      tone: "text-sky-400",
      cls: "",
    },
  ] as const;

  return (
    <div className="space-y-5">
      <nav className="text-xs text-slate-500">
        <Link
          href="/console/personas"
          className="inline-flex items-center gap-1 text-amber-300/90 underline-offset-2 hover:underline"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          返回人设列表
        </Link>
        <span className="mx-2 text-slate-700">/</span>
        <span>清除队列</span>
      </nav>

      <PageHeader
        title="人设清除队列"
        desc={
          <>
            purge 协议的指令级监控：console 发起全域清除后，每个承载引擎各领一条指令，引擎轮询{" "}
            <Code>GET /api/sync/personas/purges?system=&lt;引擎&gt;</Code>{" "}
            并在本地删除资产后回执。滞留超 24h 说明引擎侧清除执行器（cron）可能没跑，超 72h 需人工介入。
          </>
        }
      />

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {cards.map((c) => (
          <Card key={c.label} className={c.cls}>
            <div className="flex items-start justify-between gap-2">
              <p className="text-xs text-slate-500">{c.label}</p>
              <c.Icon className={`h-4 w-4 shrink-0 ${c.tone}`} />
            </div>
            <p className="mt-1 text-3xl font-bold tabular-nums text-white">{c.value}</p>
            <p className="mt-1.5 text-[11px] text-slate-500">{c.sub}</p>
          </Card>
        ))}
      </div>

      <Card>
        <SectionTitle>逐引擎积压</SectionTitle>
        {stats.byTarget.length === 0 ? (
          <p className="text-xs text-slate-500">还没有下发过清除指令。</p>
        ) : (
          <table className="w-full text-left text-sm">
            <thead>
              <tr className="border-b border-slate-800 text-[11px] uppercase tracking-wider text-slate-500">
                <th className="py-2 font-medium">目标引擎</th>
                <th className="py-2 text-right font-medium">待回执</th>
                <th className="py-2 text-right font-medium">已回执</th>
                <th className="py-2 pl-6 font-medium">最早滞留</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/70">
              {stats.byTarget.map((t) => {
                const h = hoursBetween(t.oldest_pending_at, now);
                return (
                  <tr key={t.target_system}>
                    <td className="py-2">
                      <Link href={`/console/personas/purges?target=${encodeURIComponent(t.target_system)}&state=pending`}>
                        <SystemBadge system={t.target_system} />
                      </Link>
                    </td>
                    <td
                      className={`py-2 text-right font-mono text-xs tabular-nums ${
                        t.pending ? "font-semibold text-amber-300" : "text-slate-500"
                      }`}
                    >
                      {t.pending}
                    </td>
                    <td className="py-2 text-right font-mono text-xs tabular-nums text-slate-300">{t.acked}</td>
                    <td className="py-2 pl-6 text-xs">
                      {t.oldest_pending_at ? (
                        <span className={h != null && h >= 72 ? "text-rose-300" : h != null && h >= 24 ? "text-amber-300" : "text-slate-400"}>
                          {fmtDateTime(t.oldest_pending_at)}
                          {h != null && <span className="ml-1.5 text-slate-500">（已等 {fmtDuration(h)}）</span>}
                        </span>
                      ) : (
                        <span className="text-slate-600">无积压</span>
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Card>

      <form method="GET" className="flex flex-wrap items-center gap-2">
        <select name="target" defaultValue={target ?? ""} className={filterInputCls}>
          <option value="">全部引擎</option>
          {engineChoices.map((e) => (
            <option key={e} value={e}>
              {e}
            </option>
          ))}
        </select>
        <select name="state" defaultValue={state ?? ""} className={filterInputCls}>
          {STATE_CHOICES.map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
        </select>
        <input
          type="search"
          name="q"
          defaultValue={q ?? ""}
          placeholder="搜索来源键 / 显示名 / 人设 ID"
          className={`${filterInputCls} w-64`}
        />
        <FilterSubmit />
        {hasFilter && (
          <Link href="/console/personas/purges" className="text-xs text-slate-500 hover:text-slate-300">
            清除
          </Link>
        )}
      </form>

      {rows.length === 0 ? (
        <EmptyState
          title={hasFilter ? "没有匹配的清除指令" : "清除队列是空的"}
          hints={
            hasFilter
              ? ["调整引擎 / 回执状态 / 搜索条件试试。"]
              : [
                  "还没有发起过全域清除 —— 在人设详情页「危险区」发起（admin+）。",
                  <span key="proto">
                    发起后每个承载引擎各领一条指令；引擎轮询{" "}
                    <Code>GET /api/sync/personas/purges?system=&lt;自己&gt;</Code>{" "}
                    删除本地资产后回执，全部回执人设自动置「已清除」。
                  </span>,
                ]
          }
        />
      ) : (
        <Card className="p-0">
          <DataTable head={["指令", "人设", "目标引擎", "待删槽位", "下发", "回执", "回执详情"]}>
            {rows.map((r) => (
              <tr key={r.purge_id} className="hover:bg-slate-800/40">
                <Td className="font-mono text-xs text-slate-500">#{r.purge_id}</Td>
                <Td>
                  <div className="flex items-center gap-2">
                    <Link
                      href={`/console/personas/${r.persona_id}`}
                      className="font-medium text-amber-300 hover:underline"
                    >
                      {r.display_name || "（未命名）"}
                    </Link>
                    <PersonaStatusBadge status={r.persona_status} />
                  </div>
                  <div className="mt-0.5 font-mono text-[11px] text-slate-500" title={r.persona_id}>
                    {r.source_key}
                  </div>
                </Td>
                <Td>
                  <SystemBadge system={r.target_system} />
                </Td>
                <Td>
                  <PersonaSlotCells
                    face={!!r.slot_face}
                    voice={!!r.slot_voice}
                    prompt={!!r.slot_prompt}
                    knowledge={!!r.slot_knowledge}
                  />
                </Td>
                <Td className="text-xs">
                  <span className="text-slate-400">{fmtDateTime(r.requested_at)}</span>
                  <div className="mt-0.5 font-mono text-[11px] text-slate-600">{r.requested_by || "—"}</div>
                </Td>
                <Td className="text-xs">
                  {r.acked_at ? (
                    <>
                      <span className="font-medium text-emerald-300">已回执 · {fmtDateTime(r.acked_at)}</span>
                      {(() => {
                        const took =
                          r.requested_at != null ? hoursBetween(r.requested_at, Date.parse(r.acked_at)) : null;
                        return took != null && took >= 0 ? (
                          <div className="mt-0.5 text-[11px] text-slate-500">耗时 {fmtDuration(took)}</div>
                        ) : null;
                      })()}
                    </>
                  ) : (
                    <PendingAge requestedAt={r.requested_at} now={now} />
                  )}
                </Td>
                <Td className="max-w-[240px] truncate font-mono text-[11px] text-slate-500">
                  <span title={r.ack_detail ?? undefined}>{r.ack_detail || "—"}</span>
                </Td>
              </tr>
            ))}
          </DataTable>
        </Card>
      )}

      <Pager
        basePath="/console/personas/purges"
        params={{ target, state, q }}
        total={total}
        limit={LIMIT}
        offset={offset}
      />

      <p className="text-[11px] leading-relaxed text-slate-600">
        口径备注：一行 = 对一个引擎的一条删除指令（同一人设发往 N 个引擎即 N 行）；排序为待回执在前（等得最久的最靠上）、
        已回执按回执时间倒序。回执幂等，重复 ack 保留首次回执时间；本页只读——发起清除在人设详情页，回执只认引擎机器通道。
      </p>
    </div>
  );
}
