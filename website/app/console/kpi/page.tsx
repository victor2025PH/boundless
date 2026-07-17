// /console/kpi：集团 KPI · 全域事件流看板。
// 数据源：group-events.db（lib/events-db.ts，由 /api/collect 收集器写入）。
// 纯服务端渲染：堆叠条用 div 宽度百分比拼（不引图表库）；日期口径为事件 ts 的 UTC 日。
import Link from "next/link";
import { hasConsoleSession } from "@/lib/console-auth";
import {
  countsByNameTop,
  countsByProductDay,
  recentEvents,
  totals,
} from "@/lib/events-db";
import { Card, Code, DataTable, PageHeader, SectionTitle, Td, fmtDateTime } from "../parts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

// 产品配色 / 中文名（九枚举，与 EVENT_CONTRACT.md product_id 一致）
const PRODUCT_META: Record<string, { label: string; bar: string; badge: string }> = {
  zhituo: { label: "智拓", bar: "bg-sky-400", badge: "bg-sky-500/15 text-sky-300 border-sky-500/30" },
  zhiliao: { label: "智聊", bar: "bg-emerald-400", badge: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30" },
  tongyi: { label: "通译", bar: "bg-teal-400", badge: "bg-teal-500/15 text-teal-300 border-teal-500/30" },
  tongchuan: { label: "通传", bar: "bg-cyan-400", badge: "bg-cyan-500/15 text-cyan-300 border-cyan-500/30" },
  huansheng: { label: "幻声", bar: "bg-violet-400", badge: "bg-violet-500/15 text-violet-300 border-violet-500/30" },
  huanying: { label: "幻影", bar: "bg-fuchsia-400", badge: "bg-fuchsia-500/15 text-fuchsia-300 border-fuchsia-500/30" },
  huanyan: { label: "幻颜", bar: "bg-rose-400", badge: "bg-rose-500/15 text-rose-300 border-rose-500/30" },
  website: { label: "官网", bar: "bg-amber-400", badge: "bg-amber-500/15 text-amber-300 border-amber-500/30" },
  platform: { label: "平台", bar: "bg-slate-400", badge: "bg-slate-500/15 text-slate-300 border-slate-500/30" },
};
const FALLBACK_META = { label: "?", bar: "bg-slate-600", badge: "bg-slate-500/15 text-slate-400 border-slate-500/30" };
const meta = (pid: string) => PRODUCT_META[pid] ?? FALLBACK_META;

function ProductBadge({ productId }: { productId: string }) {
  const m = meta(productId);
  return (
    <span className={`inline-block rounded-full border px-2 py-0.5 text-[11px] font-medium ${m.badge}`}>
      {m.label} <span className="font-mono opacity-70">{productId}</span>
    </span>
  );
}

/** 近 N 个 UTC 日的日期串列表（YYYY-MM-DD，旧 → 新，含今天）。 */
function lastUtcDays(n: number): string[] {
  const now = new Date();
  const todayStart = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  const out: string[] = [];
  for (let i = n - 1; i >= 0; i--) out.push(new Date(todayStart - i * 86400000).toISOString().slice(0, 10));
  return out;
}

export default function KpiPage() {
  if (!hasConsoleSession()) return null;

  const t = totals();
  const empty = t.total === 0;
  const rows30 = countsByProductDay(30);
  const days7 = new Set(lastUtcDays(7));

  // (日, 产品) → 数量；同时聚出 7/30 天的产品小计
  const byDay = new Map<string, Map<string, number>>();
  const product30 = new Map<string, number>();
  const product7 = new Map<string, number>();
  let total30 = 0;
  let total7 = 0;
  for (const r of rows30) {
    let m = byDay.get(r.day);
    if (!m) byDay.set(r.day, (m = new Map()));
    m.set(r.product_id, r.count);
    product30.set(r.product_id, (product30.get(r.product_id) ?? 0) + r.count);
    total30 += r.count;
    if (days7.has(r.day)) {
      product7.set(r.product_id, (product7.get(r.product_id) ?? 0) + r.count);
      total7 += r.count;
    }
  }
  const dayList = lastUtcDays(30).map((day) => {
    const m = byDay.get(day);
    const segments = m
      ? Array.from(m.entries())
          .sort((a, b) => a[0].localeCompare(b[0]))
          .map(([pid, count]) => ({ pid, count }))
      : [];
    return { day, total: segments.reduce((s, x) => s + x.count, 0), segments };
  });
  const maxDayTotal = Math.max(1, ...dayList.map((d) => d.total));
  const activeProducts = Array.from(product30.keys()).sort();
  const topNames = countsByNameTop(30, 10);
  const maxNameCount = Math.max(1, ...topNames.map((n) => n.count));
  const recent = recentEvents({ limit: 20 });

  const cards = [
    { label: "总事件数（全量）", value: t.total, sub: t.lastTs ? `最新事件 ${fmtDateTime(t.lastTs)}` : "尚无事件" },
    { label: "近 7 天", value: total7, sub: "按事件 ts 的 UTC 日统计" },
    { label: "近 30 天", value: total30, sub: `${activeProducts.length} 个产品活跃` },
    { label: "未注册事件", value: t.unregistered, sub: "带 _unregistered 标记，需回 registry 补注册" },
  ];

  return (
    <div className="space-y-5">
      <nav className="text-xs text-slate-500">
        <Link href="/console" className="text-amber-300/90 underline-offset-2 hover:underline">
          ← 返回总览
        </Link>
        <span className="mx-2 text-slate-700">/</span>
        <span>集团 KPI</span>
      </nav>

      <PageHeader
        title="集团 KPI · 全域事件流"
        desc="七产品 + 官网 + 平台层的运营事件统一入库（group-events.db，独立于账本）。上报链路：产品 spool → uploader 补传 → /api/collect 幂等入库。"
      />

      {empty && (
        <div className="rounded-xl border border-amber-500/30 bg-amber-500/5 p-4 text-xs leading-relaxed text-amber-200/90">
          <p className="mb-1.5 font-semibold text-amber-300">事件库还是空的，三步接通上报链路：</p>
          <ol className="list-decimal space-y-1.5 pl-5">
            <li>
              在官网部署环境设置 <Code>EVENT_INGEST_KEY</Code>（机器上报密钥，独立于 CONSOLE_KEY；见{" "}
              <Code>website/.env.example</Code>），未配置时 /api/collect 返回 503；
            </li>
            <li>
              在产品机器跑补传器（建议 cron 每 5 分钟）：
              <pre className="mt-1.5 overflow-x-auto rounded-lg bg-slate-950 p-2.5 font-mono text-[11px] leading-relaxed text-slate-300">
                {"python platform/observability/uploader.py --endpoint https://bd2026.cc/api/collect --key <EVENT_INGEST_KEY>"}
              </pre>
              spool 目录、断点游标等参数见 <Code>uploader.py --help</Code> 与 EVENT_CONTRACT.md 传输层一节；
            </li>
            <li>
              或用 curl 手工验证收集器：
              <pre className="mt-1.5 overflow-x-auto rounded-lg bg-slate-950 p-2.5 font-mono text-[11px] leading-relaxed text-slate-300">
                {`curl -X POST https://bd2026.cc/api/collect \\
  -H "Authorization: Bearer <EVENT_INGEST_KEY>" \\
  -H "Content-Type: application/json" -H "X-Event-Source: manual-test" \\
  -d '{"events":[{"event_id":"evt_01KXS8BM00008J4CT4ANK7F24S","ts":"2026-07-18T04:00:00.123Z","product_id":"website","name":"website.lead.submitted","props":{"lead_id":"ld_demo"}}]}'`}
              </pre>
            </li>
          </ol>
        </div>
      )}

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        {cards.map((c) => (
          <Card key={c.label}>
            <p className="text-xs text-slate-500">{c.label}</p>
            <p className="mt-1 text-3xl font-bold tabular-nums text-white">{c.value}</p>
            <p className="mt-1.5 text-[11px] text-slate-500">{c.sub}</p>
          </Card>
        ))}
      </div>

      <Card>
        <SectionTitle count={total30}>近 30 天日活跃度（按产品堆叠 · UTC 日）</SectionTitle>
        {total30 === 0 ? (
          <p className="text-xs text-slate-500">近 30 天没有事件。</p>
        ) : (
          <>
            <div className="mb-3 flex flex-wrap gap-x-4 gap-y-1.5 text-[11px] text-slate-400">
              {activeProducts.map((pid) => (
                <span key={pid} className="inline-flex items-center gap-1.5">
                  <span className={`h-2.5 w-2.5 rounded-sm ${meta(pid).bar}`} />
                  {meta(pid).label} <span className="font-mono text-slate-500">{pid}</span>
                </span>
              ))}
            </div>
            <div className="space-y-1">
              {dayList.map((d) => (
                <div key={d.day} className="flex items-center gap-2.5">
                  <span className="w-20 shrink-0 font-mono text-[11px] text-slate-500">{d.day.slice(5)}</span>
                  <div className="h-3.5 flex-1 overflow-hidden rounded-sm bg-slate-800/60">
                    {d.total > 0 && (
                      <div className="flex h-full" style={{ width: `${(d.total / maxDayTotal) * 100}%` }}>
                        {d.segments.map((s) => (
                          <div
                            key={s.pid}
                            className={meta(s.pid).bar}
                            style={{ width: `${(s.count / d.total) * 100}%` }}
                            title={`${d.day} ${meta(s.pid).label} ${s.pid}: ${s.count}`}
                          />
                        ))}
                      </div>
                    )}
                  </div>
                  <span className="w-14 shrink-0 text-right font-mono text-[11px] tabular-nums text-slate-400">
                    {d.total || "·"}
                  </span>
                </div>
              ))}
            </div>
          </>
        )}
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <SectionTitle>产品分布（近 7 / 30 天）</SectionTitle>
          {activeProducts.length === 0 ? (
            <p className="text-xs text-slate-500">近 30 天没有事件。</p>
          ) : (
            <table className="w-full text-left text-sm">
              <thead>
                <tr className="border-b border-slate-800 text-[11px] uppercase tracking-wider text-slate-500">
                  <th className="py-2 font-medium">产品</th>
                  <th className="py-2 text-right font-medium">近 7 天</th>
                  <th className="py-2 text-right font-medium">近 30 天</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800/70">
                {activeProducts
                  .slice()
                  .sort((a, b) => (product30.get(b) ?? 0) - (product30.get(a) ?? 0))
                  .map((pid) => (
                    <tr key={pid}>
                      <td className="py-2">
                        <ProductBadge productId={pid} />
                      </td>
                      <td className="py-2 text-right font-mono text-xs tabular-nums text-slate-300">
                        {product7.get(pid) ?? 0}
                      </td>
                      <td className="py-2 text-right font-mono text-xs tabular-nums text-slate-200">
                        {product30.get(pid) ?? 0}
                      </td>
                    </tr>
                  ))}
              </tbody>
            </table>
          )}
        </Card>

        <Card>
          <SectionTitle>Top 事件名（近 30 天）</SectionTitle>
          {topNames.length === 0 ? (
            <p className="text-xs text-slate-500">近 30 天没有事件。</p>
          ) : (
            <ul className="space-y-2">
              {topNames.map((n) => (
                <li key={n.name} className="flex items-center gap-2.5">
                  <span className="w-52 shrink-0 truncate font-mono text-xs text-slate-300" title={n.name}>
                    {n.name}
                  </span>
                  <div className="h-2.5 flex-1 overflow-hidden rounded-sm bg-slate-800/60">
                    <div className={`h-full ${meta(n.product_id).bar}`} style={{ width: `${(n.count / maxNameCount) * 100}%` }} />
                  </div>
                  <span className="w-12 shrink-0 text-right font-mono text-[11px] tabular-nums text-slate-400">{n.count}</span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      <Card className="p-0">
        <div className="px-5 pt-5">
          <SectionTitle count={recent.length}>最近 20 条事件</SectionTitle>
        </div>
        {recent.length === 0 ? (
          <p className="px-5 pb-5 text-xs text-slate-500">还没有事件入库。</p>
        ) : (
          <DataTable head={["产品", "事件名", "发生时间（站点时区）", "来源", "标记"]}>
            {recent.map((e) => (
              <tr key={e.event_id} className="hover:bg-slate-800/40">
                <Td>
                  <ProductBadge productId={e.product_id} />
                </Td>
                <Td>
                  <span className="font-mono text-xs text-slate-200" title={e.event_id}>
                    {e.name}
                  </span>
                </Td>
                <Td className="text-xs text-slate-400">{fmtDateTime(e.ts)}</Td>
                <Td className="text-xs text-slate-500">{e.source || "—"}</Td>
                <Td>
                  {e.unregistered ? (
                    <span className="rounded-full border border-rose-500/30 bg-rose-500/15 px-2 py-0.5 text-[11px] font-medium text-rose-300">
                      未注册
                    </span>
                  ) : (
                    <span className="text-xs text-slate-600">—</span>
                  )}
                </Td>
              </tr>
            ))}
          </DataTable>
        )}
      </Card>

      <p className="text-[11px] leading-relaxed text-slate-600">
        口径备注：日活跃度按事件 <Code>ts</Code> 的 UTC 日聚合（与 spool 日文件同口径）；表格时间列已折算站点时区。
        幂等以 <Code>event_id</Code> 为准，补传重发不会重复计数。
      </p>
    </div>
  );
}
