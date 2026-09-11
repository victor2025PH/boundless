// /console/kpi：事件流 · 全域遥测看板（2026-08 改版前叫「集团 KPI」——名不副实：
// 这里是事件吞吐/活跃度，不是钱；经营口径（本月成交/待办）已归总览）。
// 数据源：group-events.db（lib/events-db.ts，由 /api/collect 收集器写入）。
// 纯服务端渲染：堆叠条用 div 宽度百分比拼（不引图表库）；日期口径为事件 ts 的 UTC 日。
// 页脚折叠卡并入原 /console/health 的五机静态台账（该独立页已撤销并重定向到本页）。
import Link from "next/link";
import { Activity, Server } from "lucide-react";
import { hasConsoleSession } from "@/lib/console-auth";
import {
  EVENT_PRODUCT_IDS,
  countsByNameTop,
  countsByProductDay,
  recentEvents,
  totals,
} from "@/lib/events-db";
import {
  getMachineMesh,
  isPrivateLanIp,
  probeLocalEndpoints,
  type LocalProbeResult,
} from "@/lib/machine-mesh";
import { MACHINE_ROLE_LABEL } from "../labels";
import {
  Card,
  Code,
  DataTable,
  FilterSubmit,
  PageHeader,
  SectionTitle,
  Td,
  filterInputCls,
  fmtDateTime,
} from "../parts";

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

export default async function KpiPage({
  searchParams,
}: {
  searchParams: { product?: string; name?: string; probe?: string };
}) {
  if (!hasConsoleSession()) return null;

  // 最近事件筛选：product 按九枚举白名单收，name 精确匹配（lib 语义；
  // 从下方「Top 事件名」点击带入最方便）。命中筛选时放宽到 100 条。
  const productFilter = (EVENT_PRODUCT_IDS as readonly string[]).includes(searchParams.product ?? "")
    ? searchParams.product
    : undefined;
  const nameFilter = searchParams.name?.trim() || undefined;
  const filtered = !!(productFilter || nameFilter);

  // 五机台账折叠卡（原 /console/health 并入）：?probe=1 时才做 localhost 探测
  const mesh = getMachineMesh();
  const wantProbe = ["1", "true"].includes(searchParams.probe ?? "");
  const localProbe: LocalProbeResult[] | null = wantProbe ? await probeLocalEndpoints(mesh) : null;

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
  const recent = recentEvents({ productId: productFilter, name: nameFilter, limit: filtered ? 100 : 20 });

  const cards = [
    { label: "总事件数（全量）", value: t.total, sub: t.lastTs ? `最新事件 ${fmtDateTime(t.lastTs)}` : "尚无事件" },
    { label: "近 7 天", value: total7, sub: "按事件 ts 的 UTC 日统计" },
    { label: "近 30 天", value: total30, sub: `${activeProducts.length} 个产品活跃` },
    { label: "未注册事件", value: t.unregistered, sub: "带 _unregistered 标记，需回 registry 补注册" },
  ];

  return (
    <div className="space-y-5">
      <nav className="text-xs text-slate-500">
        <Link href="/console" className="text-crown-300 underline-offset-2 hover:underline">
          ← 返回总览
        </Link>
        <span className="mx-2 text-slate-700">/</span>
        <span>运营事件</span>
      </nav>

      <PageHeader
        title="运营事件"
        desc="各产品与官网的运营事件统一入库。这里看吞吐与活跃度；经营口径在总览。"
        techNote="上报链路：产品本地缓存 → 补传器 → 收集接口幂等入库。空库时请联系技术负责人接通上报密钥与补传器。"
      />

      {empty && (
        <div className="rounded-xl border-l-[3px] border-l-amber-400 border border-ink-700 bg-ink-900/60 p-4 text-xs leading-relaxed text-slate-300">
          <p className="mb-1.5 font-semibold text-amber-300">事件库还是空的</p>
          <p>尚无运营事件入库。请联系技术负责人接通上报密钥与补传器；接通后各产品会自动上报。</p>
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
                  <div className="h-3.5 flex-1 overflow-hidden rounded-sm bg-ink-700/60">
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
                <tr className="border-b border-ink-700 text-[11px] uppercase tracking-wider text-slate-500">
                  <th className="py-2 font-medium">产品</th>
                  <th className="py-2 text-right font-medium">近 7 天</th>
                  <th className="py-2 text-right font-medium">近 30 天</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-ink-700/70">
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
                  <Link
                    href={`/console/kpi?name=${encodeURIComponent(n.name)}`}
                    className="w-52 shrink-0 truncate font-mono text-xs text-slate-300 underline-offset-2 hover:text-amber-300 hover:underline"
                    title={`筛选最近事件：${n.name}`}
                  >
                    {n.name}
                  </Link>
                  <div className="h-2.5 flex-1 overflow-hidden rounded-sm bg-ink-700/60">
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
          <SectionTitle count={recent.length}>最近事件{filtered ? "（已筛选，最多 100 条）" : "（最近 20 条）"}</SectionTitle>
        </div>
        <form method="GET" className="flex flex-wrap items-center gap-2 px-5 pb-3">
          <select name="product" defaultValue={productFilter ?? ""} className={filterInputCls}>
            <option value="">全部产品</option>
            {EVENT_PRODUCT_IDS.map((pid) => (
              <option key={pid} value={pid}>
                {meta(pid).label} {pid}
              </option>
            ))}
          </select>
          <input
            name="name"
            defaultValue={nameFilter ?? ""}
            placeholder="事件名精确匹配（从上方 Top 点击带入）"
            className={`${filterInputCls} w-72 font-mono`}
          />
          <FilterSubmit />
          {filtered && (
            <Link href="/console/kpi" className="text-xs text-slate-500 hover:text-slate-300">
              清除
            </Link>
          )}
        </form>
        {recent.length === 0 ? (
          <p className="px-5 pb-5 text-xs text-slate-500">{filtered ? "没有匹配的事件。" : "还没有事件入库。"}</p>
        ) : (
          <DataTable head={["产品", "事件名", "发生时间（站点时区）", "来源", "标记"]}>
            {recent.map((e) => (
              <tr key={e.event_id} className="hover:bg-ink-700/40">
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

      {/* 五机台账（原 /console/health 独立页并入）：静态 CMDB，默认折叠不占版面；
          公网 VPS 探不到内网五机，真实探活走 tools/cluster_ping.ps1，此处不装监控 */}
      <details
        open={wantProbe || undefined}
        className="rounded-2xl border border-ink-700 bg-ink-900/60"
      >
        <summary className="flex cursor-pointer select-none flex-wrap items-center gap-2 px-5 py-4 text-sm font-semibold text-white [&::-webkit-details-marker]:hidden">
          <Server className="h-4 w-4 text-amber-400/80" />
          五机台账（静态 CMDB）
          <span className="text-[11px] font-normal text-slate-500">
            源 deploy/machines.json · 本页不做跨机探活，真实探活用 tools/cluster_ping.ps1
          </span>
        </summary>
        <div className="border-t border-ink-700 p-5 pt-4">
          <DataTable head={["机器", "角色", "IP", "主品牌", "主服务 / 端口", "产品", "可见性"]}>
            {mesh.map((m) => {
              const lanOnly = isPrivateLanIp(m.ip);
              const ports = m.services.length
                ? m.services.map((s) => (s.port != null ? `${s.name}:${s.port}` : s.name)).join(" · ")
                : "—";
              return (
                <tr key={m.id} className="hover:bg-ink-700/40">
                  <Td>
                    <p className="text-sm font-semibold text-white">{m.zh}</p>
                    <p className="font-mono text-[10px] text-slate-500">{m.id}</p>
                  </Td>
                  <Td className="text-xs text-slate-300">{MACHINE_ROLE_LABEL[m.role] ?? m.role}</Td>
                  <Td className="font-mono text-xs text-slate-200">{m.ip}</Td>
                  <Td className="font-mono text-xs text-amber-300/80">{m.primaryBrand || "—"}</Td>
                  <Td className="max-w-[280px] font-mono text-[11px] text-slate-400">
                    <span className="block truncate" title={ports}>
                      {ports}
                    </span>
                    {m.note && (
                      <span className="mt-0.5 block truncate text-[10px] text-slate-600" title={m.note}>
                        {m.note}
                      </span>
                    )}
                  </Td>
                  <Td className="text-xs text-slate-400">{m.products.join(" / ") || "—"}</Td>
                  <Td>
                    {lanOnly ? (
                      <span className="rounded-full border border-slate-600 bg-ink-700/80 px-2 py-0.5 text-[10px] font-medium text-slate-400">
                        仅内网可见
                      </span>
                    ) : (
                      <span className="rounded-full border border-emerald-500/30 bg-emerald-500/10 px-2 py-0.5 text-[10px] font-medium text-emerald-300">
                        可达探测
                      </span>
                    )}
                  </Td>
                </tr>
              );
            })}
          </DataTable>

          {localProbe ? (
            <div className="mt-4">
              <p className="mb-2 text-[11px] leading-relaxed text-slate-500">
                本机探测结果：仅请求 <Code>127.0.0.1</Code> 上的已知端口与本站健康端点，不代表五机真实状态。
                <Link href="/console/kpi" className="ml-2 text-amber-300 underline-offset-2 hover:underline">
                  清除探测
                </Link>
              </p>
              <DataTable head={["目标", "结果", "耗时", "详情"]}>
                {localProbe.map((p) => (
                  <tr key={p.target} className="hover:bg-ink-700/40">
                    <Td className="font-mono text-xs text-slate-300">{p.target}</Td>
                    <Td>
                      {p.ok ? (
                        <span className="text-xs font-medium text-emerald-300">ok</span>
                      ) : (
                        <span className="text-xs font-medium text-slate-500">unreachable</span>
                      )}
                    </Td>
                    <Td className="font-mono text-xs tabular-nums text-slate-400">{p.ms}ms</Td>
                    <Td className="font-mono text-[11px] text-slate-500">{p.detail}</Td>
                  </tr>
                ))}
              </DataTable>
            </div>
          ) : (
            <form method="GET" className="mt-3">
              <input type="hidden" name="probe" value="1" />
              <button
                type="submit"
                className="inline-flex items-center gap-1.5 rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-1.5 text-xs font-medium text-amber-300 hover:bg-amber-500/20"
              >
                <Activity className="h-3.5 w-3.5" />
                探测本机 localhost
              </button>
            </form>
          )}
        </div>
      </details>

      <p className="text-[11px] leading-relaxed text-slate-600">
        口径备注：日活跃度按事件 <Code>ts</Code> 的 UTC 日聚合（与 spool 日文件同口径）；表格时间列已折算站点时区。
        幂等以 <Code>event_id</Code> 为准，补传重发不会重复计数。台账 API：<Code>GET /api/console/health</Code>
        （可选 <Code>?probe=1</Code>）。
      </p>
    </div>
  );
}
