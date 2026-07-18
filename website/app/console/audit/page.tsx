// /console/audit：写操作审计流水（只读）。支持 ?q=&action=；不含聊天内容。
import Link from "next/link";
import { ScrollText } from "lucide-react";
import { hasConsoleSession } from "@/lib/console-auth";
import { listAudit } from "../data";
import {
  Card,
  Code,
  DataTable,
  EmptyState,
  FilterSubmit,
  PageHeader,
  Td,
  filterInputCls,
  fmtDateTime,
} from "../parts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const LIMIT = 100;

const ACTION_HINTS = [
  "customer.create",
  "identity.attach",
  "order.assign",
  "license.assign",
  "lead.assign",
  "opportunity.mark",
] as const;

export default function AuditPage({
  searchParams,
}: {
  searchParams: { q?: string; action?: string };
}) {
  if (!hasConsoleSession()) return null;

  const q = searchParams.q?.trim() || undefined;
  const action = searchParams.action?.trim() || undefined;
  const { rows, total } = listAudit({ q, action, limit: LIMIT });

  return (
    <div>
      <PageHeader
        title="审计"
        desc={
          <>
            <ScrollText className="mr-1 inline h-3.5 w-3.5 align-text-bottom" />
            写操作审计流水（建档 / 挂身份 / 归属 / 商机跟进等）。
            <span className="font-medium text-amber-300/90"> 不含聊天内容</span>
            ——只记 actor / action / entity，detail 为结构化元数据。
          </>
        }
      />

      <Card className="mb-4 border-slate-800 bg-slate-900/40 !py-3">
        <p className="text-[11px] leading-relaxed text-slate-500">
          常用 action 示例：
          {ACTION_HINTS.map((a) => (
            <Link
              key={a}
              href={`/console/audit?action=${encodeURIComponent(a)}`}
              className="ml-2 font-mono text-amber-300/80 underline-offset-2 hover:underline"
            >
              {a}
            </Link>
          ))}
        </p>
      </Card>

      <form method="GET" className="mb-4 flex flex-wrap items-center gap-2">
        <input
          type="search"
          name="q"
          defaultValue={q ?? ""}
          placeholder="搜索 actor / action / entity / detail"
          className={`${filterInputCls} w-72`}
        />
        <input
          name="action"
          defaultValue={action ?? ""}
          placeholder="action 精确（如 customer.create）"
          className={`${filterInputCls} w-56 font-mono`}
        />
        <FilterSubmit />
        {(q || action) && (
          <Link href="/console/audit" className="text-xs text-slate-500 hover:text-slate-300">
            清除
          </Link>
        )}
      </form>

      {rows.length === 0 ? (
        <EmptyState
          title="暂无审计记录"
          hints={[
            <>在控制台做归属、挂身份、商机跟进等写操作后会出现流水。</>,
            <>
              也可用脚本头 <Code>x-console-key</Code> 调 API 写账本，同样会记 audit。
            </>,
          ]}
        />
      ) : (
        <>
          <DataTable head={["时间", "操作者", "动作", "实体", "详情"]}>
            {rows.map((a) => (
              <tr key={a.id} className="hover:bg-slate-800/40">
                <Td className="whitespace-nowrap font-mono text-xs text-slate-500">
                  {fmtDateTime(a.ts)}
                </Td>
                <Td>
                  <span className="rounded bg-slate-800 px-1.5 py-0.5 font-mono text-[10px] text-amber-300/80">
                    {a.actor ?? "system"}
                  </span>
                </Td>
                <Td className="text-xs font-medium text-slate-200">{a.action}</Td>
                <Td className="font-mono text-xs text-slate-400">
                  {a.entity ? (
                    a.entity === "customer" && a.entity_id ? (
                      <Link
                        href={`/console/customers/${a.entity_id}`}
                        className="text-amber-300 underline-offset-2 hover:underline"
                      >
                        {a.entity}:{a.entity_id.slice(0, 8)}…
                      </Link>
                    ) : (
                      `${a.entity}:${a.entity_id ?? ""}`
                    )
                  ) : (
                    "—"
                  )}
                </Td>
                <Td className="max-w-[420px] font-mono text-[10px] text-slate-600">
                  <span className="block truncate" title={a.detail ?? undefined}>
                    {a.detail || "—"}
                  </span>
                </Td>
              </tr>
            ))}
          </DataTable>
          <p className="mt-3 text-[11px] text-slate-500">
            显示最近 {rows.length} 条{total > rows.length ? `（共 ${total} 条匹配）` : ""} · 只读。
          </p>
        </>
      )}
    </div>
  );
}
