// /console/orders：订单台账 —— 状态/关键词筛选 + 未归属行内「归属客户」操作（viewer 隐藏）。
import Link from "next/link";
import { listCustomers, listOrders } from "@/lib/ledger";
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
  OrderStatusBadge,
  PageHeader,
  Pager,
  Td,
  filterInputCls,
  fmtAmount,
  fmtDateTime,
} from "../parts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const LIMIT = 50;
const STATUSES = ["pending", "paid", "activated", "cancelled"] as const;
const STATUS_LABEL: Record<string, string> = {
  pending: "待支付",
  paid: "已支付",
  activated: "已开通",
  cancelled: "已取消",
};

export default function OrdersPage({
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
  const { rows, total } = listOrders({ status, q, limit: LIMIT, offset });

  // 归属下拉的候选客户（最多 500 位，超出后建议先用搜索建档再归属）
  const customerOptions: CustomerOption[] = listCustomers({ limit: 500 }).rows.map((c) => ({
    id: c.id,
    label: `${c.display_name || "（未命名）"}${c.primary_contact ? ` · ${c.primary_contact}` : ""}`,
  }));

  // 已归属行的客户显示名（仅查本页出现的 id，最多 50 次主键查询）
  const nameById = new Map<string, string | null>();
  for (const o of rows) {
    if (o.customer_id && !nameById.has(o.customer_id)) {
      nameById.set(o.customer_id, getCustomerById(o.customer_id)?.display_name ?? null);
    }
  }

  const filters = { status, q };

  return (
    <div>
      <PageHeader
        title="订单台账"
        desc="账本镜像自 order-store（JSON 主真相源，双写 + 回填）。此处只做客户归属与查阅；改单退款仍走原链路。"
      />

      <form method="GET" className="mb-4 flex flex-wrap items-center gap-2">
        <select name="status" defaultValue={status ?? ""} className={filterInputCls}>
          <option value="">全部状态</option>
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {STATUS_LABEL[s]}
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
        <FilterSubmit />
        {(status || q) && (
          <Link href="/console/orders" className="text-xs text-slate-500 hover:text-slate-300">
            清除
          </Link>
        )}
      </form>

      {rows.length === 0 ? (
        <EmptyState
          title={status || q ? "没有匹配的订单" : "订单台账还是空的"}
          hints={
            status || q
              ? ["调整筛选条件试试。"]
              : [
                  <span key="backfill">
                    先在服务器运行 <Code>node scripts/ledger-backfill.mjs</Code> 回填历史订单（幂等可重跑）；
                  </span>,
                  "新订单会经双写钩子自动入账。",
                ]
          }
        />
      ) : (
        <Card className="p-0">
          <DataTable head={["来源单号", "产品 / 方案", "金额", "状态", "联系方式", "创建时间", "关联客户"]}>
            {rows.map((o) => (
              <tr key={o.id} className="hover:bg-slate-800/40">
                <Td>
                  <span className="font-mono text-xs text-slate-200">{o.source_key}</span>
                </Td>
                <Td className="text-xs text-slate-300">
                  {[o.product_id, o.plan, o.edition, o.period].filter(Boolean).join(" / ") || "—"}
                </Td>
                <Td className="text-xs text-slate-200">{fmtAmount(o.amount, o.pay_amount, o.currency)}</Td>
                <Td>
                  <OrderStatusBadge status={o.status} />
                </Td>
                <Td className="max-w-[160px] truncate text-xs text-slate-400">
                  <span title={o.contact ?? undefined}>{o.contact || "—"}</span>
                </Td>
                <Td className="text-xs text-slate-500">{fmtDateTime(o.created_at)}</Td>
                <Td>
                  {o.customer_id ? (
                    <CustomerLink customerId={o.customer_id} label={nameById.get(o.customer_id)} />
                  ) : canWrite ? (
                    <AssignCustomerControl entity="order" entityKey={o.id} customers={customerOptions} />
                  ) : (
                    <span className="text-xs text-slate-600">未归属</span>
                  )}
                </Td>
              </tr>
            ))}
          </DataTable>
        </Card>
      )}

      <Pager basePath="/console/orders" params={filters} total={total} limit={LIMIT} offset={offset} />
    </div>
  );
}
