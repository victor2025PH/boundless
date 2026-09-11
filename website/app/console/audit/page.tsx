// /console/audit：写操作审计流水（只读）。支持 ?q=&action=&days= + 分页；不含聊天内容。
import Link from "next/link";
import { ScrollText } from "lucide-react";
import { hasConsoleSession } from "@/lib/console-auth";
import { listAudit } from "../data";
import { AUDIT_ACTION_LABEL, AUDIT_ACTION_QUICK, AUDIT_ENTITY_LABEL, lbl } from "../labels";
import {
  Card,
  DataTable,
  EmptyState,
  FilterSubmit,
  PageHeader,
  Pager,
  Td,
  filterInputCls,
  fmtRelative,
} from "../parts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const LIMIT = 100;

const ACTION_HINTS = AUDIT_ACTION_QUICK;

const DAYS_CHOICES = [
  { value: "", label: "全部时间" },
  { value: "1", label: "近 24 小时" },
  { value: "7", label: "近 7 天" },
  { value: "30", label: "近 30 天" },
] as const;

export default function AuditPage({
  searchParams,
}: {
  searchParams: { q?: string; action?: string; days?: string; offset?: string };
}) {
  if (!hasConsoleSession()) return null;

  const q = searchParams.q?.trim() || undefined;
  const action = searchParams.action?.trim() || undefined;
  const daysRaw = searchParams.days?.trim() || undefined;
  const days = daysRaw ? Number(daysRaw) : undefined;
  const offset = Math.max(0, Number(searchParams.offset) || 0);
  const { rows, total } = listAudit({ q, action, days, limit: LIMIT, offset });

  return (
    <div>
      <PageHeader
        title="审计"
        desc={
          <>
            <ScrollText className="mr-1 inline h-3.5 w-3.5 align-text-bottom" />
            写操作流水（建档 / 挂身份 / 归属 / 商机跟进等）。
            <span className="font-medium text-amber-300/90"> 不含聊天内容</span>
          </>
        }
        techNote="每条记录含操作者、动作、对象和结构化详情，不含客户聊天原文。"
      />

      <Card className="mb-4 border-ink-700 bg-ink-900/40 !py-3">
        <p className="text-[11px] leading-relaxed text-slate-500">
          常用动作：
          {ACTION_HINTS.map((a) => (
            <Link
              key={a}
              href={`/console/audit?action=${encodeURIComponent(a)}`}
              className="ml-2 text-crown-300 underline-offset-2 hover:underline"
            >
              {lbl(AUDIT_ACTION_LABEL, a)}
            </Link>
          ))}
        </p>
      </Card>

      <form method="GET" className="mb-4 flex flex-wrap items-center gap-2">
        <input
          type="search"
          name="q"
          defaultValue={q ?? ""}
          placeholder="搜索操作者 / 动作 / 对象 / 详情"
          className={`${filterInputCls} w-72`}
        />
        <input
          name="action"
          defaultValue={action ?? ""}
          placeholder="动作精确值（如 customer.create）"
          className={`${filterInputCls} w-56 font-mono`}
        />
        <select name="days" defaultValue={daysRaw ?? ""} className={filterInputCls}>
          {DAYS_CHOICES.map((c) => (
            <option key={c.value} value={c.value}>
              {c.label}
            </option>
          ))}
        </select>
        <FilterSubmit />
        {(q || action || daysRaw) && (
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
              脚本巡检写账本同样会记流水。
            </>,
          ]}
        />
      ) : (
        <>
          <DataTable head={["时间", "操作者", "动作", "对象", "详情"]}>
            {rows.map((a) => {
              const when = fmtRelative(a.ts);
              const entityLabel = lbl(AUDIT_ENTITY_LABEL, a.entity);
              return (
              <tr key={a.id}>
                <Td>
                  <span title={when.title} className="whitespace-nowrap text-xs text-slate-400">
                    {when.text}
                  </span>
                </Td>
                <Td>
                  <span className="rounded bg-ink-700 px-1.5 py-0.5 text-[11px] text-slate-300">
                    {a.actor === "system" || !a.actor ? "系统" : a.actor}
                  </span>
                </Td>
                <Td className="text-xs font-medium text-slate-200" title={a.action ?? undefined}>
                  {lbl(AUDIT_ACTION_LABEL, a.action)}
                </Td>
                <Td className="text-xs text-slate-400">
                  {a.entity ? (
                    a.entity === "customer" && a.entity_id ? (
                      <Link
                        href={`/console/customers/${a.entity_id}`}
                        className="text-crown-300 underline-offset-2 hover:underline"
                      >
                        {entityLabel} {a.entity_id.slice(0, 8)}…
                      </Link>
                    ) : (
                      `${entityLabel}${a.entity_id ? ` ${a.entity_id.slice(0, 8)}…` : ""}`
                    )
                  ) : (
                    "—"
                  )}
                </Td>
                <Td className="max-w-[420px] text-[11px] text-slate-400">
                  <span className="block truncate" title={a.detail ?? undefined}>
                    {a.detail || "—"}
                  </span>
                </Td>
              </tr>
              );
            })}
          </DataTable>
          <Pager
            basePath="/console/audit"
            params={{ q, action, days: daysRaw }}
            total={total}
            limit={LIMIT}
            offset={offset}
          />
          <p className="mt-3 text-[11px] text-slate-500">共 {total} 条匹配 · 只读。</p>
        </>
      )}
    </div>
  );
}
