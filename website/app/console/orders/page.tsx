// /console/orders：订单台账 —— 状态/关键词筛选 + 行内「归属客户 / 改绑 / 解绑」（viewer 隐藏）。
import Link from "next/link";
import { listOrders } from "@/lib/ledger";
import { getConsoleSessionUser } from "@/lib/console-auth";
import { roleAtLeast } from "@/lib/console-users";
import { getCustomerById } from "../data";
import { AssignCustomerControl } from "../ui";
import { ORDER_STATUS_LABEL, ORDER_STATUS_ORDER, PRODUCT_LABEL, lbl } from "../labels";
import { productDisplay, tierLabel } from "../sku-names";
import {
  Card,
  CustomerLink,
  DataTable,
  EmptyState,
  FilterSubmit,
  OrderStatusBadge,
  PageHeader,
  Pager,
  Td,
  TestBadge,
  TestFilterToggle,
  filterInputCls,
  fmtAmount,
  fmtRelative,
} from "../parts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const LIMIT = 50;
const STATUSES = ORDER_STATUS_ORDER;

export default function OrdersPage({
  searchParams,
}: {
  searchParams: { status?: string; q?: string; offset?: string; test?: string };
}) {
  const me = getConsoleSessionUser();
  if (!me) return null;
  const canWrite = roleAtLeast(me.role, "admin");
  const status = searchParams.status?.trim() || undefined;
  const q = searchParams.q?.trim() || undefined;
  const showTest = searchParams.test === "1";
  const offset = Math.max(0, Number(searchParams.offset) || 0);
  const { rows, total } = listOrders({ status, q, limit: LIMIT, offset, includeTest: showTest });
  // 当前筛选条件下的测试数据条数 = 含测试 total − 不含测试 total（limit:1 仅取计数）
  const testCount = showTest
    ? total - listOrders({ status, q, limit: 1 }).total
    : listOrders({ status, q, limit: 1, includeTest: true }).total - total;

  // 已归属行的客户显示名（仅查本页出现的 id，最多 50 次主键查询）
  const nameById = new Map<string, string | null>();
  for (const o of rows) {
    if (o.customer_id && !nameById.has(o.customer_id)) {
      nameById.set(o.customer_id, getCustomerById(o.customer_id)?.display_name ?? null);
    }
  }

  const filters = { status, q, test: showTest ? "1" : undefined };

  return (
    <div>
      <PageHeader
        title="订单台账"
        desc="查阅成交与充值订单，并把未归属的单挂到客户。改单退款仍走原支付链路。"
        techNote="订单从官网订单库同步进账本。智聊 Token 充值也在本页（充值签的是额度凭证，不是授权台账里的许可证）。"
      />

      <form method="GET" className="mb-4 flex flex-wrap items-center gap-2">
        <select name="status" defaultValue={status ?? ""} className={filterInputCls}>
          <option value="">全部状态</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {lbl(ORDER_STATUS_LABEL, s)}
            </option>
          ))}
        </select>
        <input
          type="search"
          name="q"
          defaultValue={q ?? ""}
          placeholder="搜索单号 / 联系方式 / 方案"
          className={`${filterInputCls} w-64`}
        />
        {showTest && <input type="hidden" name="test" value="1" />}
        <FilterSubmit />
        {(status || q) && (
          <Link href="/console/orders" className="text-xs text-slate-500 hover:text-slate-300">
            清除
          </Link>
        )}
        <TestFilterToggle
          basePath="/console/orders"
          params={{ status, q }}
          showTest={showTest}
          testCount={testCount}
          className="ml-auto"
        />
      </form>

      {rows.length === 0 ? (
        <EmptyState
          title={status || q ? "没有匹配的订单" : "订单台账还是空的"}
          hints={
            status || q
              ? ["调整筛选条件试试。"]
              : [
                  "尚无订单。新成交会自动入账；历史数据请联系技术负责人回填。",
                ]
          }
        />
      ) : (
        <Card className="p-0">
          <DataTable head={["来源单号", "关联客户", "产品", "档位", "金额", "状态", "联系方式", "创建时间"]}>
            {rows.map((o) => {
              const product = productDisplay(
                { product_id: o.product_id, sku_id: o.sku_id },
                o.product_id ? lbl(PRODUCT_LABEL, o.product_id) : "—"
              );
              const tier = tierLabel({ sku_id: o.sku_id, plan: o.plan, edition: o.edition, period: o.period });
              const created = fmtRelative(o.created_at);
              return (
              <tr key={o.id}>
                <Td>
                  <span className="font-mono text-xs text-slate-200">{o.source_key}</span>
                  {o.is_test === 1 && <TestBadge className="ml-2 align-middle" />}
                </Td>
                <Td>
                  {o.customer_id ? (
                    <span className="inline-flex items-center gap-1.5">
                      <CustomerLink customerId={o.customer_id} label={nameById.get(o.customer_id)} />
                      {canWrite && <AssignCustomerControl entity="order" entityKey={o.id} assigned />}
                    </span>
                  ) : canWrite ? (
                    <AssignCustomerControl entity="order" entityKey={o.id} />
                  ) : (
                    <span className="text-xs text-slate-400">未归属</span>
                  )}
                </Td>
                <Td className="text-xs text-slate-200" title={product.title}>
                  {product.text}
                </Td>
                <Td className="text-xs text-slate-300" title={tier.title}>
                  {tier.text}
                </Td>
                <Td className="text-xs tabular-nums text-slate-200">{fmtAmount(o.amount, o.pay_amount, o.currency)}</Td>
                <Td>
                  <OrderStatusBadge status={o.status} />
                </Td>
                <Td className="max-w-[160px] truncate text-xs text-slate-400">
                  <span title={o.contact ?? undefined}>{o.contact || "—"}</span>
                </Td>
                <Td>
                  <span title={created.title} className="text-xs text-slate-400">
                    {created.text}
                  </span>
                </Td>
              </tr>
              );
            })}
          </DataTable>
        </Card>
      )}

      <Pager basePath="/console/orders" params={filters} total={total} limit={LIMIT} offset={offset} />
    </div>
  );
}
