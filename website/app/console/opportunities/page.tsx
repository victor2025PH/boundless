// /console/opportunities：跨售商机独立页 —— 复用 lib/opportunities + opportunities-ui。
// 筛选 kind / 跟进 status；空态引导补人设与账本。admin+ 可跟进。
import Link from "next/link";
import { Sparkles } from "lucide-react";
import {
  OPPORTUNITY_KINDS,
  OPPORTUNITY_LOG_STATUSES,
  getOpportunityStats,
  isOpportunityKind,
  isOpportunityLogStatus,
  listOpportunities,
  productLabel,
  type OpportunityLogStatus,
} from "@/lib/opportunities";
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
  Td,
  filterInputCls,
} from "../parts";
import { OpportunityActions, OpportunityLogBadge } from "../opportunities-ui";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const KIND_OPTIONS = [
  { value: "persona_cross_sell", label: "人设跨售" },
  { value: "product_gap_cross_sell", label: "互补缺口" },
  { value: "expiring_renewal", label: "续费在即" },
] as const;

const STATUS_OPTIONS = [
  { value: "open", label: "待跟进（含未标记）" },
  { value: "contacted", label: "已联系" },
  { value: "won", label: "已赢单" },
  { value: "dismissed", label: "已忽略" },
] as const;

export default function OpportunitiesPage({
  searchParams,
}: {
  searchParams: { kind?: string; status?: string };
}) {
  const me = getConsoleSessionUser();
  if (!me) return null;
  const canWrite = roleAtLeast(me.role, "admin");

  const kindRaw = searchParams.kind?.trim() || undefined;
  const kind = kindRaw && isOpportunityKind(kindRaw) ? kindRaw : undefined;
  const statusRaw = searchParams.status?.trim() || undefined;
  const status =
    statusRaw && isOpportunityLogStatus(statusRaw) ? (statusRaw as OpportunityLogStatus) : undefined;

  // 赢单/忽略默认隐藏，筛这两类时必须 includeClosed
  const includeClosed = status === "won" || status === "dismissed";
  const stats = getOpportunityStats();
  let rows = listOpportunities({ kind, limit: 500, includeClosed });

  if (status === "open") {
    rows = rows.filter((o) => !o.log || o.log.status === "open");
  } else if (status === "contacted" || status === "won" || status === "dismissed") {
    rows = rows.filter((o) => o.log?.status === status);
  }

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
        <FilterSubmit />
        {(kind || status) && (
          <Link href="/console/opportunities" className="text-xs text-slate-500 hover:text-slate-300">
            清除
          </Link>
        )}
      </form>

      {rows.length === 0 ? (
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
        <DataTable head={["类型", "客户", "从 → 到", "理由", "信号值", "跟进"]}>
          {rows.map((o) => (
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
              <Td className="max-w-[360px] text-xs text-slate-400">
                <span className="block truncate" title={o.reason}>
                  {o.reason}
                </span>
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
      )}

      <p className="mt-3 text-[11px] leading-relaxed text-slate-500">
        共 {rows.length} 条 · 类型枚举 {OPPORTUNITY_KINDS.join(" / ")} · 跟进状态{" "}
        {OPPORTUNITY_LOG_STATUSES.join(" / ")}。证据字段不含联系方式与聊天内容。
      </p>
    </div>
  );
}
