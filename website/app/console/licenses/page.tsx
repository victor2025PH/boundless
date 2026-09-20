// /console/licenses：授权台账 —— 产品许可证（订阅 / 试用 / 手工签发）。
// Token 充值走订单台账（充值签的是额度凭证，不是本页这类许可证）。
import Link from "next/link";
import { listLicenses } from "@/lib/ledger";
import { getConsoleSessionUser } from "@/lib/console-auth";
import { roleAtLeast } from "@/lib/console-users";
import { getCustomerById } from "../data";
import { AssignCustomerControl } from "../ui";
import {
  LEDGER_SOURCE_SYSTEMS,
  LICENSE_STATUS_LABEL,
  LICENSE_STATUS_ORDER,
  SYSTEM_LABEL,
  lbl,
  seatsLabel,
  toOptions,
} from "../labels";
import { productDisplay, tierLabel } from "../sku-names";
import {
  Card,
  CustomerLink,
  DataTable,
  EmptyState,
  ExpiryCell,
  FilterSubmit,
  FingerprintCell,
  LicenseStatusBadge,
  PageHeader,
  Pager,
  QuotaCell,
  Td,
  TestBadge,
  TestFilterToggle,
  filterInputCls,
  fmtRelative,
} from "../parts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const LIMIT = 50;
const STATUS_CHOICES = [
  { value: "", label: "全部状态" },
  ...toOptions(LICENSE_STATUS_LABEL, LICENSE_STATUS_ORDER),
];
const EXPIRING_CHOICES = [
  { value: "", label: "全部到期时间" },
  { value: "30", label: "30 天内到期" },
  { value: "60", label: "60 天内到期" },
  { value: "90", label: "90 天内到期" },
] as const;

export default function LicensesPage({
  searchParams,
}: {
  searchParams: { source_system?: string; status?: string; expiring_days?: string; offset?: string; test?: string };
}) {
  const me = getConsoleSessionUser();
  if (!me) return null;
  const canWrite = roleAtLeast(me.role, "admin");
  const sourceSystem = searchParams.source_system?.trim() || undefined;
  const status = searchParams.status?.trim() || undefined;
  const expiringRaw = searchParams.expiring_days?.trim() || undefined;
  const expiringDays = expiringRaw ? Number(expiringRaw) : undefined;
  const showTest = searchParams.test === "1";
  const offset = Math.max(0, Number(searchParams.offset) || 0);

  const baseFilter = {
    sourceSystem,
    status,
    expiringInDays: Number.isFinite(expiringDays) ? expiringDays : undefined,
  };
  const { rows, total } = listLicenses({ ...baseFilter, limit: LIMIT, offset, includeTest: showTest });
  const testCount = showTest
    ? total - listLicenses({ ...baseFilter, limit: 1 }).total
    : listLicenses({ ...baseFilter, limit: 1, includeTest: true }).total - total;

  const nameById = new Map<string, string | null>();
  for (const l of rows) {
    if (l.customer_id && !nameById.has(l.customer_id)) {
      nameById.set(l.customer_id, getCustomerById(l.customer_id)?.display_name ?? null);
    }
  }

  const filters = { source_system: sourceSystem, status, expiring_days: expiringRaw, test: showTest ? "1" : undefined };
  const hasFilter = !!(sourceSystem || status || expiringRaw);

  return (
    <div>
      <PageHeader
        title="授权台账"
        desc="产品许可证（试用 / 订阅 / 手工签发）。2026-08-21 起智聊只卖 Token 充值，充值记录在订单台账，不在本页。"
        techNote="授权由各产品引擎每日同步进账本。到期不足 30 天会高亮。席位 0（成杰引擎）表示不限。额度列读签发时写入的 included_chars / included_tokens_monthly，台账暂无消耗水位。"
      />

      <form method="GET" className="mb-4 flex flex-wrap items-center gap-2">
        <select name="source_system" defaultValue={sourceSystem ?? ""} className={filterInputCls}>
          <option value="">全部引擎</option>
          {LEDGER_SOURCE_SYSTEMS.map((s) => (
            <option key={s} value={s}>
              {lbl(SYSTEM_LABEL, s)}
            </option>
          ))}
        </select>
        <select name="status" defaultValue={status ?? ""} className={filterInputCls}>
          {STATUS_CHOICES.map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
        </select>
        <select name="expiring_days" defaultValue={expiringRaw ?? ""} className={filterInputCls}>
          {EXPIRING_CHOICES.map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
        </select>
        {showTest && <input type="hidden" name="test" value="1" />}
        <FilterSubmit />
        {hasFilter && (
          <Link href="/console/licenses" className="text-xs text-slate-400 hover:text-slate-200">
            清除
          </Link>
        )}
        <TestFilterToggle
          basePath="/console/licenses"
          params={{ source_system: sourceSystem, status, expiring_days: expiringRaw }}
          showTest={showTest}
          testCount={testCount}
          className="ml-auto"
        />
      </form>

      {rows.length === 0 ? (
        <EmptyState
          title={hasFilter ? "没有匹配的授权" : "授权台账还是空的"}
          hints={
            hasFilter
              ? ["调整筛选条件试试。"]
              : ["授权由各产品引擎自动同步。尚无记录时请联系技术负责人确认履约端是否在岗。"]
          }
        />
      ) : (
        <Card className="p-0">
          <DataTable
            head={["产品", "关联客户", "授权号", "档位", "席位", "绑定设备", "额度", "到期", "状态", "最近同步"]}
          >
            {rows.map((l) => {
              const product = productDisplay(
                { product_id: l.product_id, sku_id: l.sku_id },
                lbl(SYSTEM_LABEL, l.source_system)
              );
              const tier = tierLabel({ sku_id: l.sku_id, plan: l.plan, edition: l.edition });
              const synced = fmtRelative(l.synced_at);
              return (
                <tr key={l.id}>
                  <Td>
                    <span title={product.title} className="text-xs text-slate-200">
                      {product.text}
                    </span>
                    {l.is_test === 1 && <TestBadge className="ml-2 align-middle" />}
                  </Td>
                  <Td>
                    {l.customer_id ? (
                      <span className="inline-flex items-center gap-1.5">
                        <CustomerLink customerId={l.customer_id} label={nameById.get(l.customer_id)} />
                        {canWrite && <AssignCustomerControl entity="license" entityKey={l.id} assigned />}
                      </span>
                    ) : canWrite ? (
                      <AssignCustomerControl entity="license" entityKey={l.id} />
                    ) : (
                      <span className="text-xs text-slate-400">未归属</span>
                    )}
                  </Td>
                  <Td>
                    <span className="font-mono text-xs text-slate-200" title={l.id}>
                      {l.source_key}
                    </span>
                  </Td>
                  <Td>
                    <span title={tier.title} className="text-xs text-slate-300">
                      {tier.text}
                    </span>
                  </Td>
                  <Td className="text-xs tabular-nums text-slate-400">
                    {seatsLabel(l.seats, l.source_system)}
                  </Td>
                  <Td>
                    <FingerprintCell fingerprint={l.machine_fingerprint} />
                  </Td>
                  <Td>
                    <QuotaCell raw={l.raw} />
                  </Td>
                  <Td className="text-xs">
                    <ExpiryCell expiresAt={l.expires_at} />
                  </Td>
                  <Td>
                    <LicenseStatusBadge status={l.status} />
                  </Td>
                  <Td>
                    <span title={synced.title} className="text-xs text-slate-400">
                      {synced.text}
                    </span>
                  </Td>
                </tr>
              );
            })}
          </DataTable>
        </Card>
      )}

      <Pager basePath="/console/licenses" params={filters} total={total} limit={LIMIT} offset={offset} />
    </div>
  );
}
