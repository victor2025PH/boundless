// /console/licenses：授权台账 —— source_system / 状态 / 到期窗口筛选 + 归属客户（viewer 隐藏）。
import Link from "next/link";
import { listCustomers, listLicenses } from "@/lib/ledger";
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
  ExpiryCell,
  FilterSubmit,
  PageHeader,
  Pager,
  SystemBadge,
  Td,
  filterInputCls,
  fmtDateTime,
} from "../parts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const LIMIT = 50;
const SYSTEMS = ["avatarhub", "chengjie"] as const;
const EXPIRING_CHOICES = [
  { value: "", label: "全部到期时间" },
  { value: "30", label: "30 天内到期" },
  { value: "60", label: "60 天内到期" },
  { value: "90", label: "90 天内到期" },
] as const;

export default function LicensesPage({
  searchParams,
}: {
  searchParams: { source_system?: string; status?: string; expiring_days?: string; offset?: string };
}) {
  const me = getConsoleSessionUser();
  if (!me) return null;
  const canWrite = roleAtLeast(me.role, "admin");
  const sourceSystem = searchParams.source_system?.trim() || undefined;
  const status = searchParams.status?.trim() || undefined;
  const expiringRaw = searchParams.expiring_days?.trim() || undefined;
  const expiringDays = expiringRaw ? Number(expiringRaw) : undefined;
  const offset = Math.max(0, Number(searchParams.offset) || 0);

  const { rows, total } = listLicenses({
    sourceSystem,
    status,
    expiringInDays: Number.isFinite(expiringDays) ? expiringDays : undefined,
    limit: LIMIT,
    offset,
  });

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

  const filters = { source_system: sourceSystem, status, expiring_days: expiringRaw };
  const hasFilter = !!(sourceSystem || status || expiringRaw);

  return (
    <div>
      <PageHeader
        title="授权台账"
        desc={
          <>
            数据来自 <Code>tools/license_ledger</Code> 导出 →{" "}
            <Code>scripts/ledger-import-licenses.mjs</Code> 导入（幂等，可重复执行）。到期不足 30 天的行会高亮，
            是续费触达的第一队列。
          </>
        }
      />

      <form method="GET" className="mb-4 flex flex-wrap items-center gap-2">
        <select name="source_system" defaultValue={sourceSystem ?? ""} className={filterInputCls}>
          <option value="">全部系统</option>
          {SYSTEMS.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <input
          name="status"
          defaultValue={status ?? ""}
          placeholder="状态（如 active / expired）"
          className={`${filterInputCls} w-44`}
        />
        <select name="expiring_days" defaultValue={expiringRaw ?? ""} className={filterInputCls}>
          {EXPIRING_CHOICES.map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
        </select>
        <FilterSubmit />
        {hasFilter && (
          <Link href="/console/licenses" className="text-xs text-slate-500 hover:text-slate-300">
            清除
          </Link>
        )}
      </form>

      {rows.length === 0 ? (
        <EmptyState
          title={hasFilter ? "没有匹配的授权" : "授权台账还是空的"}
          hints={
            hasFilter
              ? ["调整筛选条件试试。"]
              : [
                  <span key="export">
                    先在产品侧用 <Code>tools/license_ledger</Code> 生成归一化导出 JSON；
                  </span>,
                  <span key="import">
                    再运行 <Code>node scripts/ledger-import-licenses.mjs &lt;导出json&gt;</Code> 导入本账本。
                  </span>,
                ]
          }
        />
      ) : (
        <Card className="p-0">
          <DataTable head={["系统", "授权号", "产品 / 方案", "席位", "到期时间", "状态", "同步于", "关联客户"]}>
            {rows.map((l) => (
              <tr key={l.id} className="hover:bg-slate-800/40">
                <Td>
                  <SystemBadge system={l.source_system} />
                </Td>
                <Td>
                  <span className="font-mono text-xs text-slate-200" title={l.id}>
                    {l.source_key}
                  </span>
                </Td>
                <Td className="text-xs text-slate-300">
                  {[l.product_id, l.plan, l.edition].filter(Boolean).join(" / ") || "—"}
                </Td>
                <Td className="text-xs text-slate-400">{l.seats ?? "—"}</Td>
                <Td className="text-xs">
                  <ExpiryCell expiresAt={l.expires_at} />
                </Td>
                <Td className="text-xs text-slate-400">{l.status || "—"}</Td>
                <Td className="text-xs text-slate-600">{fmtDateTime(l.synced_at)}</Td>
                <Td>
                  {l.customer_id ? (
                    <CustomerLink customerId={l.customer_id} label={nameById.get(l.customer_id)} />
                  ) : canWrite ? (
                    <AssignCustomerControl entity="license" entityKey={l.id} customers={customerOptions} />
                  ) : (
                    <span className="text-xs text-slate-600">未归属</span>
                  )}
                </Td>
              </tr>
            ))}
          </DataTable>
        </Card>
      )}

      <Pager basePath="/console/licenses" params={filters} total={total} limit={LIMIT} offset={offset} />
    </div>
  );
}
