// /console/leads：留资只读镜像 + 客户归并（viewer 隐藏）。日常跟进（改状态/回访）仍在 /admin。
import Link from "next/link";
import { listCustomers, listLeads } from "@/lib/ledger";
import { getConsoleSessionUser } from "@/lib/console-auth";
import { roleAtLeast } from "@/lib/console-users";
import { getCustomerById } from "../data";
import { AssignCustomerControl, type CustomerOption } from "../ui";
import {
  Card,
  Code,
  CustomerLink,
  DataTable,
  EmptyState,
  FilterSubmit,
  LeadStatusBadge,
  PageHeader,
  Pager,
  Td,
  filterInputCls,
  fmtDateTime,
} from "../parts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const LIMIT = 50;
const STATUSES = [
  { value: "new", label: "新留资" },
  { value: "contacted", label: "已联系" },
  { value: "won", label: "已成交" },
  { value: "lost", label: "已流失" },
] as const;

export default function LeadsPage({
  searchParams,
}: {
  searchParams: { status?: string; q?: string; offset?: string };
}) {
  const me = getConsoleSessionUser();
  if (!me) return null;
  const canWrite = roleAtLeast(me.role, "admin");
  const status = searchParams.status?.trim() || undefined;
  const q = searchParams.q?.trim() || undefined;
  const offset = Math.max(0, Number(searchParams.offset) || 0);
  const { rows, total } = listLeads({ status, q, limit: LIMIT, offset });

  const customerOptions: CustomerOption[] = listCustomers({ limit: 500 }).rows.map((c) => ({
    id: c.id,
    label: `${c.display_name || "（未命名）"}${c.primary_contact ? ` · ${c.primary_contact}` : ""}`,
  }));

  const nameById = new Map<string, string | null>();
  for (const l of rows) {
    if (l.customer_id && !nameById.has(l.customer_id)) {
      nameById.set(l.customer_id, getCustomerById(l.customer_id)?.display_name ?? null);
    }
  }

  const filters = { status, q };

  return (
    <div>
      <PageHeader
        title="留资"
        desc={
          <>
            日常跟进仍在{" "}
            <a href="/admin" className="text-amber-300 underline-offset-2 hover:underline">
              /admin
            </a>
            ，本页只做客户归并：把留资挂到客户主档，让客户 360 看到完整旅程。账本为只读镜像，状态以 /admin 为准。
          </>
        }
      />

      <form method="GET" className="mb-4 flex flex-wrap items-center gap-2">
        <select name="status" defaultValue={status ?? ""} className={filterInputCls}>
          <option value="">全部状态</option>
          {STATUSES.map((s) => (
            <option key={s.value} value={s.value}>
              {s.label}
            </option>
          ))}
        </select>
        <input
          type="search"
          name="q"
          defaultValue={q ?? ""}
          placeholder="搜索来源键 / 称呼 / 联系方式"
          className={`${filterInputCls} w-64`}
        />
        <FilterSubmit />
        {(status || q) && (
          <Link href="/console/leads" className="text-xs text-slate-500 hover:text-slate-300">
            清除
          </Link>
        )}
      </form>

      {rows.length === 0 ? (
        <EmptyState
          title={status || q ? "没有匹配的留资" : "留资镜像还是空的"}
          hints={
            status || q
              ? ["调整筛选条件试试。"]
              : [
                  <span key="backfill">
                    先在服务器运行 <Code>node scripts/ledger-backfill.mjs</Code> 回填历史留资（与订单同一脚本）；
                  </span>,
                  "新留资会经双写钩子自动入账。",
                ]
          }
        />
      ) : (
        <Card className="p-0">
          <DataTable head={["来源键", "称呼", "联系方式", "意向", "来源", "状态", "次数", "最近活跃", "关联客户"]}>
            {rows.map((l) => (
              <tr key={l.source_key} className="hover:bg-slate-800/40">
                <Td>
                  <span className="font-mono text-xs text-slate-200">{l.source_key}</span>
                </Td>
                <Td className="text-xs text-slate-300">{l.name || "—"}</Td>
                <Td className="max-w-[150px] truncate text-xs text-slate-300">
                  <span title={l.contact ?? undefined}>{l.contact || "—"}</span>
                </Td>
                <Td className="max-w-[180px] truncate text-xs text-slate-400">
                  <span title={l.interest ?? undefined}>{l.interest || "—"}</span>
                </Td>
                <Td className="text-xs text-slate-400">{l.source || "—"}</Td>
                <Td>
                  <LeadStatusBadge status={l.status} />
                </Td>
                <Td className="text-xs tabular-nums text-slate-400">{l.count ?? "—"}</Td>
                <Td className="text-xs text-slate-500">{fmtDateTime(l.last_seen)}</Td>
                <Td>
                  {l.customer_id ? (
                    <CustomerLink customerId={l.customer_id} label={nameById.get(l.customer_id)} />
                  ) : canWrite ? (
                    <AssignCustomerControl entity="lead" entityKey={l.source_key} customers={customerOptions} />
                  ) : (
                    <span className="text-xs text-slate-600">未归属</span>
                  )}
                </Td>
              </tr>
            ))}
          </DataTable>
        </Card>
      )}

      <Pager basePath="/console/leads" params={filters} total={total} limit={LIMIT} offset={offset} />
    </div>
  );
}
