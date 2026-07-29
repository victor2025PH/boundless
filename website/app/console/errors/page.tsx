// /console/errors：客户端错误回传看板（公网测试者的远程可观测）。
// 数据源 = DATA_DIR/client-logs.jsonl（桌面 beacon 回传，见 lib/telemetry_beacon.py）。
// 只读聚合：版本分布 / 错误 Top / 崩溃 / 最近事件。默认近 24h，?h=6 可调窗口。
import { AlertTriangle, Bug, MonitorSmartphone, Skull } from "lucide-react";
import { hasConsoleSession } from "@/lib/console-auth";
import { summarizeClientLogs } from "@/lib/client-logs";
import { Card, DataTable, EmptyState, PageHeader, SectionTitle, Td, fmtDateTime } from "../parts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

function Stat({ label, value, tone }: { label: string; value: number | string; tone?: "amber" | "red" }) {
  const c = tone === "red" ? "text-rose-400" : tone === "amber" ? "text-amber-300" : "text-white";
  return (
    <div className="rounded-xl border border-slate-800 bg-slate-900/40 px-4 py-3">
      <div className={`text-xl font-bold ${c}`}>{value}</div>
      <div className="mt-0.5 text-[11px] text-slate-500">{label}</div>
    </div>
  );
}

export default async function ErrorsPage({ searchParams }: { searchParams: { h?: string } }) {
  if (!hasConsoleSession()) return null;
  const h = Math.min(168, Math.max(1, Number(searchParams.h) || 24));
  const s = await summarizeClientLogs(h);

  return (
    <div className="space-y-5">
      <PageHeader
        title="客户端错误"
        desc={
          <>
            <Bug className="mr-1 inline h-3.5 w-3.5 align-text-bottom" />
            公网/内网测试者的桌面端 <span className="font-medium text-amber-300/90">ERROR 摘要 + 崩溃</span>
            自动回传归集（消毒后：无聊天内容、无密钥）。近 {h} 小时；
            <a className="ml-1 text-amber-300 hover:underline" href="?h=6">6h</a> ·
            <a className="ml-1 text-amber-300 hover:underline" href="?h=24">24h</a> ·
            <a className="ml-1 text-amber-300 hover:underline" href="?h=72">72h</a>
          </>
        }
      />

      {!s.present || s.total_events === 0 ? (
        <EmptyState
          title="窗口内还没有回传"
          hints={[
            <>桌面端 0.2.6+ 装好并联网后，ERROR 级日志与崩溃会自动回传到这里。</>,
            <>没有回传通常是好事（没人报错）；也可能是还没人在该窗口内启动。</>,
          ]}
        />
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Stat label="事件总数" value={s.total_events} />
            <Stat label="涉及机器数" value={s.machines} />
            <Stat label="错误类型" value={s.top_errors.length} tone={s.top_errors.length ? "amber" : undefined} />
            <Stat label="崩溃/异常退出" value={s.crashes.length} tone={s.crashes.length ? "red" : undefined} />
          </div>

          <Card>
            <SectionTitle count={s.by_version.length}>
              <MonitorSmartphone className="mr-1 inline h-4 w-4 align-text-bottom" />版本分布
            </SectionTitle>
            <DataTable head={["版本", "机器数", "事件数"]}>
              {s.by_version.map((v) => (
                <tr key={v.ver} className="hover:bg-slate-800/40">
                  <Td><span className="font-mono text-xs">{v.ver || "?"}</span></Td>
                  <Td>{v.machines}</Td>
                  <Td>{v.events}</Td>
                </tr>
              ))}
            </DataTable>
          </Card>

          {s.crashes.length > 0 && (
            <Card className="border-rose-500/30">
              <SectionTitle count={s.crashes.length}>
                <Skull className="mr-1 inline h-4 w-4 align-text-bottom text-rose-400" />崩溃 / 异常退出（哨兵）
              </SectionTitle>
              <DataTable head={["时间", "机器", "版本", "详情"]}>
                {s.crashes.map((c, i) => (
                  <tr key={i} className="hover:bg-slate-800/40">
                    <Td className="whitespace-nowrap text-xs text-slate-400">{fmtDateTime(c.t)}</Td>
                    <Td><span className="font-mono text-[11px]">{c.fp || "?"}</span></Td>
                    <Td><span className="font-mono text-[11px]">{c.ver}</span></Td>
                    <Td><span className="text-xs text-slate-300">{c.msg}</span></Td>
                  </tr>
                ))}
              </DataTable>
            </Card>
          )}

          <Card>
            <SectionTitle count={s.top_errors.length}>
              <AlertTriangle className="mr-1 inline h-4 w-4 align-text-bottom text-amber-400" />错误 Top（合并计数）
            </SectionTitle>
            {s.top_errors.length === 0 ? (
              <p className="px-1 py-3 text-xs text-slate-500">窗口内只有启动心跳，无 ERROR/WARNING。</p>
            ) : (
              <DataTable head={["次数", "机器", "来源", "消息", "最近"]}>
                {s.top_errors.map((e, i) => (
                  <tr key={i} className="hover:bg-slate-800/40">
                    <Td><span className="font-semibold text-amber-300">{e.count}</span></Td>
                    <Td>{e.machines}</Td>
                    <Td><span className="font-mono text-[11px] text-slate-400">{e.logger}</span></Td>
                    <Td><span className="text-xs text-slate-300">{e.msg}</span></Td>
                    <Td className="whitespace-nowrap text-[11px] text-slate-500">{fmtDateTime(e.last)}</Td>
                  </tr>
                ))}
              </DataTable>
            )}
          </Card>
        </>
      )}
    </div>
  );
}
