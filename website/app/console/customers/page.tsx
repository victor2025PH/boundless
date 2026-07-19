// /console/customers：客户列表 + 搜索（GET 表单）+ 新建客户（客户端表单，viewer 隐藏）。
import Link from "next/link";
import { listCustomers } from "@/lib/ledger";
import { getConsoleSessionUser } from "@/lib/console-auth";
import { roleAtLeast } from "@/lib/console-users";
import { NewCustomerForm } from "../ui";
import {
  Card,
  DataTable,
  EmptyState,
  FilterSubmit,
  PageHeader,
  Pager,
  ShortId,
  Td,
  TestBadge,
  TestFilterToggle,
  filterInputCls,
  fmtDateTime,
} from "../parts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const LIMIT = 50;

export default function CustomersPage({ searchParams }: { searchParams: { q?: string; offset?: string; test?: string } }) {
  const me = getConsoleSessionUser();
  if (!me) return null;
  const canWrite = roleAtLeast(me.role, "admin");
  const q = searchParams.q?.trim() || undefined;
  const showTest = searchParams.test === "1";
  const offset = Math.max(0, Number(searchParams.offset) || 0);
  const { rows, total } = listCustomers({ q, limit: LIMIT, offset, includeTest: showTest });
  // 当前筛选条件下的测试数据条数 = 含测试 total − 不含测试 total（limit:1 仅取计数）
  const testCount = showTest
    ? total - listCustomers({ q, limit: 1 }).total
    : listCustomers({ q, limit: 1, includeTest: true }).total - total;

  return (
    <div>
      <PageHeader
        title="客户"
        desc="集团客户主档：跨产品身份归并的锚点。订单/授权/留资归属到客户后，在客户 360 汇成一张脸。"
        actions={canWrite ? <NewCustomerForm /> : undefined}
      />

      <form method="GET" className="mb-4 flex flex-wrap items-center gap-2">
        <input
          type="search"
          name="q"
          defaultValue={q ?? ""}
          placeholder="搜索显示名 / 联系方式 / TG ID"
          className={`${filterInputCls} w-64`}
        />
        {showTest && <input type="hidden" name="test" value="1" />}
        <FilterSubmit />
        {q && (
          <Link href="/console/customers" className="text-xs text-slate-500 hover:text-slate-300">
            清除
          </Link>
        )}
        <TestFilterToggle
          basePath="/console/customers"
          params={{ q }}
          showTest={showTest}
          testCount={testCount}
          className="ml-auto"
        />
      </form>

      {rows.length === 0 ? (
        <EmptyState
          title={q ? `没有匹配「${q}」的客户` : "还没有客户档案"}
          hints={
            q
              ? ["换个关键词，或清除搜索条件。"]
              : [
                  "点击右上角「新建客户」手工建档；",
                  "或到订单 / 留资页，对未归属的行点「归属客户」时快捷新建；",
                  "订单回填后带 contact / fingerprint 的会自动匹配已有客户身份。",
                ]
          }
        />
      ) : (
        <Card className="p-0">
          <DataTable head={["客户", "主联系方式", "TG", "来源", "备注", "创建时间", ""]}>
            {rows.map((c) => (
              <tr key={c.id} className="hover:bg-slate-800/40">
                <Td>
                  <Link href={`/console/customers/${c.id}`} className="font-medium text-amber-300 hover:underline">
                    {c.display_name || "（未命名）"}
                  </Link>
                  {c.is_test === 1 && <TestBadge className="ml-2 align-middle" />}
                  <div className="mt-0.5">
                    <ShortId id={c.id} />
                  </div>
                </Td>
                <Td className="text-slate-300">{c.primary_contact || "—"}</Td>
                <Td className="font-mono text-xs text-slate-400">{c.tg_user_id || "—"}</Td>
                <Td className="text-xs text-slate-400">{c.source || "—"}</Td>
                <Td className="max-w-[220px] truncate text-xs text-slate-400" >
                  <span title={c.notes ?? undefined}>{c.notes || "—"}</span>
                </Td>
                <Td className="text-xs text-slate-500">{fmtDateTime(c.created_at)}</Td>
                <Td>
                  <Link
                    href={`/console/customers/${c.id}`}
                    className="rounded-lg border border-slate-700 px-2.5 py-1 text-xs text-slate-300 hover:border-amber-500/60 hover:text-amber-300"
                  >
                    客户 360 →
                  </Link>
                </Td>
              </tr>
            ))}
          </DataTable>
        </Card>
      )}

      <Pager
        basePath="/console/customers"
        params={{ q, test: showTest ? "1" : undefined }}
        total={total}
        limit={LIMIT}
        offset={offset}
      />
    </div>
  );
}
