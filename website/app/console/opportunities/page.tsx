// /console/opportunities：跨售商机独立页 —— 复用 lib/opportunities + ui.tsx 商机组件。
// 筛选 kind / 跟进 status / 关键词 q + 分页；空态引导补人设与账本。admin+ 可跟进，
// 已关闭（won/dismissed）行可「重开」。
import Link from "next/link";
import { Sparkles } from "lucide-react";
import {
  OPPORTUNITY_KINDS,
  OPPORTUNITY_LOG_STATUSES,
  getOpportunityStats,
  isOpportunityKind,
  isOpportunityLogStatus,
  listOpportunities,
  type OpportunityLogStatus,
} from "@/lib/opportunities";
import { OPP_KIND_LABEL, OPP_STATUS_LABEL, PRODUCT_LABEL, lbl, opportunityPriority } from "../labels";
import { getConsoleSessionUser } from "@/lib/console-auth";
import { roleAtLeast } from "@/lib/console-users";
import {
  Card,
  CustomerLink,
  DataTable,
  EmptyState,
  FilterSubmit,
  OpportunityKindBadge,
  PageHeader,
  Pager,
  Td,
  filterInputCls,
} from "../parts";
import { OpportunityActions, OpportunityLogBadge } from "../ui";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const LIMIT = 50;

const KIND_OPTIONS = [
  { value: "persona_cross_sell", label: OPP_KIND_LABEL.persona_cross_sell },
  { value: "product_gap_cross_sell", label: OPP_KIND_LABEL.product_gap_cross_sell },
  { value: "expiring_renewal", label: OPP_KIND_LABEL.expiring_renewal },
] as const;

const STATUS_OPTIONS = [
  { value: "open", label: `${OPP_STATUS_LABEL.open}（含未标记）` },
  { value: "contacted", label: OPP_STATUS_LABEL.contacted },
  { value: "won", label: OPP_STATUS_LABEL.won },
  { value: "dismissed", label: OPP_STATUS_LABEL.dismissed },
] as const;

export default function OpportunitiesPage({
  searchParams,
}: {
  searchParams: { kind?: string; status?: string; q?: string; offset?: string };
}) {
  const me = getConsoleSessionUser();
  if (!me) return null;
  const canWrite = roleAtLeast(me.role, "admin");

  const kindRaw = searchParams.kind?.trim() || undefined;
  const kind = kindRaw && isOpportunityKind(kindRaw) ? kindRaw : undefined;
  const statusRaw = searchParams.status?.trim() || undefined;
  const status =
    statusRaw && isOpportunityLogStatus(statusRaw) ? (statusRaw as OpportunityLogStatus) : undefined;
  const q = searchParams.q?.trim() || undefined;
  const offset = Math.max(0, Number(searchParams.offset) || 0);

  // 赢单/忽略默认隐藏，筛这两类时必须 includeClosed
  const includeClosed = status === "won" || status === "dismissed";
  const stats = getOpportunityStats();
  let rows = listOpportunities({ kind, limit: 500, includeClosed });

  if (status === "open") {
    rows = rows.filter((o) => !o.log || o.log.status === "open");
  } else if (status === "contacted" || status === "won" || status === "dismissed") {
    rows = rows.filter((o) => o.log?.status === status);
  }
  if (q) {
    // 商机是内存推导（无表可查），关键词在输出侧匹配：客户名 / 理由 / 起止产品
    const needle = q.toLowerCase();
    rows = rows.filter((o) =>
      [o.customerName ?? "", o.reason, o.fromProduct, o.toProduct, lbl(PRODUCT_LABEL, o.fromProduct), lbl(PRODUCT_LABEL, o.toProduct)]
        .join("\n")
        .toLowerCase()
        .includes(needle)
    );
  }
  const total = rows.length;
  const pageRows = rows.slice(offset, offset + LIMIT);

  return (
    <div>
      <PageHeader
        title="商机"
        desc={
          <>
            跨售商机由账本（人设 / 订单 / 授权）只读推导；跟进落 opportunities_log。
            口径与总览一致：「赢单/忽略」默认隐藏，可用状态筛选带出。
          </>
        }
      />

      <div className="mb-4 grid grid-cols-3 gap-3">
        {OPPORTUNITY_KINDS.map((k) => {
          const opt = KIND_OPTIONS.find((o) => o.value === k);
          return (
            <Card key={k} className="!p-3">
              <OpportunityKindBadge kind={k} />
              <p className="mt-2 text-2xl font-bold tabular-nums text-white">{stats.byKind[k]}</p>
              <p className="mt-1 text-[11px] text-slate-500">{opt?.label ?? k}</p>
            </Card>
          );
        })}
      </div>

      <form method="GET" className="mb-4 flex flex-wrap items-center gap-2">
        <select name="kind" defaultValue={kind ?? ""} className={filterInputCls}>
          <option value="">全部类型</option>
          {KIND_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <select name="status" defaultValue={status ?? ""} className={filterInputCls}>
          <option value="">进行中（默认）</option>
          {STATUS_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
        <input
          type="search"
          name="q"
          defaultValue={q ?? ""}
          placeholder="搜索客户 / 理由 / 产品"
          className={`${filterInputCls} w-56`}
        />
        <FilterSubmit />
        {(kind || status || q) && (
          <Link href="/console/opportunities" className="text-xs text-slate-500 hover:text-slate-300">
            清除
          </Link>
        )}
      </form>

      {pageRows.length === 0 ? (
        <EmptyState
          title="暂无商机信号"
          hints={[
            <>
              <Sparkles className="mr-1 inline h-3.5 w-3.5" />
              人设归属客户、订单/授权入账后，跨售与续费信号会自动出现。
            </>,
            <>
              先去{" "}
              <Link href="/console/personas" className="text-amber-300 underline-offset-2 hover:underline">
                人设
              </Link>{" "}
              /{" "}
              <Link href="/console/licenses" className="text-amber-300 underline-offset-2 hover:underline">
                授权
              </Link>{" "}
              补数据；或换筛选条件。
            </>,
          ]}
        />
      ) : (
        <DataTable head={["类型", "客户", "从 → 到", "理由", "优先级", "跟进"]}>
          {pageRows.map((o) => {
            const pri = opportunityPriority(o.signalValue);
            return (
            <tr key={o.oppKey}>
              <Td>
                <OpportunityKindBadge kind={o.kind} />
              </Td>
              <Td>
                <CustomerLink customerId={o.customerId} label={o.customerName} />
              </Td>
              <Td className="text-xs text-slate-300">
                <span>{lbl(PRODUCT_LABEL, o.fromProduct)}</span>
                <span className="mx-1.5 text-slate-500">→</span>
                <span className="text-crown-300">{lbl(PRODUCT_LABEL, o.toProduct)}</span>
              </Td>
              <Td className="max-w-[360px] text-xs text-slate-400">
                <span className="block truncate" title={o.reason}>
                  {o.reason}
                </span>
              </Td>
              <Td>
                <span title={`信号值 ${o.signalValue}`} className={`text-xs font-semibold ${pri.tone === "danger" ? "text-rose-300" : pri.tone === "warning" ? "text-amber-300" : "text-slate-300"}`}>
                  {pri.label}
                </span>
              </Td>
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
            );
          })}
        </DataTable>
      )}

      <Pager
        basePath="/console/opportunities"
        params={{ kind, status, q }}
        total={total}
        limit={LIMIT}
        offset={offset}
      />

      <p className="mt-3 text-[11px] leading-relaxed text-slate-500">
        共 {total} 条 · 类型枚举 {OPPORTUNITY_KINDS.join(" / ")} · 跟进状态{" "}
        {OPPORTUNITY_LOG_STATUSES.join(" / ")}。证据字段不含联系方式与聊天内容。
      </p>
    </div>
  );
}
