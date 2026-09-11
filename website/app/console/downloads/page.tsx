// /console/downloads：安装包下载台账（2026-09-10）。
// 回答「有多少人在下载、从哪里下、有多少不是我们自己人」——此前只能翻 nginx 14 天日志。
// 数据源 downloads.jsonl（lib/download-ledger.ts，/dl 单点写入）；归属地 lib/ip-geo.ts 惰性补齐；
// 「已装机」= 该 IP 出现过桌面端 beacon（client-logs.jsonl），能把「下载」和「真装了」接上。
import Link from "next/link";
import { Download, ShieldBan } from "lucide-react";
import { hasConsoleSession } from "@/lib/console-auth";
import { readLedger, type LedgerRow } from "@/lib/download-ledger";
import { geoLabel, geoLookup } from "@/lib/ip-geo";
import { Card, Code, DataTable, EmptyState, PageHeader, SectionTitle, Td, fmtDateTime } from "../parts";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const DAYS_OPTIONS = [1, 7, 30, 90];
const PRODUCT_LABEL: Record<string, string> = {
  chatx: "智聊 ChatX",
  avatarhub: "幻境 STUDIO",
  matrixx: "智控 MatrixX",
};

const KIND_STYLE: Record<LedgerRow["kind"], string> = {
  human: "bg-emerald-500/15 text-emerald-300 border-emerald-500/30",
  office: "bg-sky-500/15 text-sky-300 border-sky-500/30",
  bot: "bg-slate-500/15 text-slate-400 border-slate-500/30",
  blocked: "bg-rose-500/15 text-rose-300 border-rose-500/30",
};
const KIND_LABEL: Record<LedgerRow["kind"], string> = {
  human: "外部",
  office: "办公室",
  bot: "爬虫",
  blocked: "已拦",
};
const FLAG_LABEL: Record<string, string> = {
  sweep: "横扫多产品",
  ratelimit: "限流",
  bot: "爬虫UA",
  noua: "空UA",
  range: "续传",
  office: "办公室",
};
const VIA_LABEL: Record<string, string> = { r2: "云端镜像", local: "本站", blocked: "拦截" };

function shortUa(ua: string): string {
  if (!ua) return "—";
  if (/electron-builder/i.test(ua)) return "自动更新器";
  if (/^curl\//i.test(ua)) return ua.slice(0, 12);
  const m = ua.match(/(Chrome|Firefox|Edg|Safari)\/([\d.]+)/);
  const os = /Windows/.test(ua) ? "Win" : /Mac OS/.test(ua) ? "Mac" : /Android/.test(ua) ? "Android" : /iPhone|iPad/.test(ua) ? "iOS" : /Linux/.test(ua) ? "Linux" : "";
  if (m) return `${os} ${m[1]} ${m[2].split(".")[0]}`.trim();
  return ua.slice(0, 28);
}

function mk(base: Record<string, string | undefined>, over: Record<string, string | undefined>) {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries({ ...base, ...over })) if (v) sp.set(k, v);
  const qs = sp.toString();
  return qs ? `/console/downloads?${qs}` : "/console/downloads";
}

export default async function DownloadsPage({
  searchParams,
}: {
  searchParams: { days?: string; all?: string; product?: string };
}) {
  if (!hasConsoleSession()) return null;
  const days = DAYS_OPTIONS.includes(Number(searchParams.days)) ? Number(searchParams.days) : 7;
  const includeNoise = ["1", "true"].includes(searchParams.all ?? "");
  const product = searchParams.product && PRODUCT_LABEL[searchParams.product] ? searchParams.product : undefined;
  const params = { days: String(days), all: includeNoise ? "1" : undefined, product };

  const s = await readLedger({ days, includeNoise, product, limit: 300 });
  const geo = await geoLookup(s.rows.map((r) => r.ip));
  const maxDay = Math.max(1, ...s.by_day.map((d) => d.human + d.noise));

  const pill = (active: boolean) =>
    `rounded-full border px-2.5 py-1 text-xs ${
      active ? "border-crown-500/60 bg-crown-500/15 text-crown-300" : "border-slate-700 text-slate-400 hover:text-slate-200"
    }`;

  return (
    <div className="space-y-5">
      <PageHeader
        title="安装包下载"
        desc={
          <>
            <Download className="mr-1 inline h-3.5 w-3.5 align-text-bottom" />
            所有安装包下载（/dl 分流 + /downloads 直链）单点记账：IP · 归属地 · 产品/版本/渠道 · 走向。
            <span className="ml-1 text-slate-400">外部</span> = 非办公室出口、非爬虫的真实来访；
            <span className="ml-1 text-slate-400">已装机</span> = 该 IP 之后有桌面端开机信标。
            同 IP 同文件 30 分钟内只记一次（续传/差量不重复计）。
          </>
        }
        actions={
          <div className="flex flex-wrap items-center gap-1.5">
            {DAYS_OPTIONS.map((d) => (
              <Link key={d} href={mk(params, { days: String(d) })} className={pill(d === days)}>
                {d === 1 ? "今天" : `近 ${d} 天`}
              </Link>
            ))}
            <span className="mx-1 h-4 w-px bg-ink-700" />
            <Link href={mk(params, { all: includeNoise ? undefined : "1" })} className={pill(includeNoise)}>
              {includeNoise ? "隐藏爬虫/办公室" : "显示爬虫/办公室"}
            </Link>
          </div>
        }
      />

      {!s.log_present ? (
        <EmptyState
          title="台账还没有记录"
          hints={[
            <>台账文件 <Code>downloads.jsonl</Code> 尚不存在——本次部署后第一次安装包 GET 才会写入。</>,
            <>历史 14 天的 nginx 明细见 2026-09-10 分析画布；此后一律看这里。</>,
          ]}
        />
      ) : (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Card>
              <div className="text-[11px] uppercase tracking-wider text-slate-500">外部下载</div>
              <div className="mt-1 text-2xl font-bold tabular-nums text-white">{s.human}</div>
              <div className="mt-0.5 text-[11px] text-slate-500">
                {s.human_ips} 个 IP · 其中 {s.human_ips_installed} 个之后装机启动
              </div>
            </Card>
            <Card>
              <div className="text-[11px] uppercase tracking-wider text-slate-500">办公室 / 发版 / 坐席更新</div>
              <div className="mt-1 text-2xl font-bold tabular-nums text-sky-300">{s.office}</div>
              <div className="mt-0.5 text-[11px] text-slate-500">DL_TRUSTED_IPS 出口，不计入外部</div>
            </Card>
            <Card>
              <div className="text-[11px] uppercase tracking-wider text-slate-500">爬虫 / 扫描器（已拦）</div>
              <div className="mt-1 text-2xl font-bold tabular-nums text-slate-300">{s.bot}</div>
              <div className="mt-0.5 text-[11px] text-slate-500">UA 名单命中，403</div>
            </Card>
            <Card>
              <div className="text-[11px] uppercase tracking-wider text-slate-500">限流拦截</div>
              <div className={`mt-1 text-2xl font-bold tabular-nums ${s.blocked ? "text-rose-300" : "text-white"}`}>{s.blocked}</div>
              <div className="mt-0.5 text-[11px] text-slate-500">同 IP 一小时内不同安装包 &gt; 上限，429</div>
            </Card>
          </div>

          <div className="grid gap-4 lg:grid-cols-3">
            <Card>
              <SectionTitle>外部下载 · 按产品</SectionTitle>
              {s.by_product.length === 0 ? (
                <div className="text-[12px] text-slate-500">窗口内无外部安装包下载。</div>
              ) : (
                <div className="space-y-2">
                  {s.by_product.map((p) => (
                    <div key={p.product}>
                      <div className="flex items-baseline justify-between text-[12px]">
                        <Link href={mk(params, { product: product === p.product ? undefined : p.product })} className="text-slate-300 hover:text-crown-300">
                          {PRODUCT_LABEL[p.product] || p.product}
                        </Link>
                        <span className="tabular-nums text-white">{p.human}</span>
                      </div>
                      <div className="mt-1 h-1.5 rounded bg-ink-900">
                        <div className="h-1.5 rounded bg-emerald-400/70" style={{ width: `${Math.max(3, Math.round((p.human / Math.max(1, s.by_product[0].human)) * 100))}%` }} />
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </Card>
            <Card className="lg:col-span-2">
              <SectionTitle>每日走势（绿 = 外部，灰 = 办公室/爬虫/拦截）</SectionTitle>
              <div className="flex h-24 items-end gap-[3px]">
                {s.by_day.map((d) => (
                  <div key={d.day} className="flex flex-1 flex-col justify-end" title={`${d.day}：外部 ${d.human} · 其他 ${d.noise}`}>
                    <div className="w-full rounded-t bg-slate-600/60" style={{ height: `${Math.round((d.noise / maxDay) * 96)}px` }} />
                    <div className="w-full bg-emerald-400/70" style={{ height: `${Math.round((d.human / maxDay) * 96)}px` }} />
                  </div>
                ))}
              </div>
              <div className="mt-1 flex justify-between text-[10px] text-slate-600">
                <span>{s.by_day[0]?.day}</span>
                <span>{s.by_day[s.by_day.length - 1]?.day}</span>
              </div>
            </Card>
          </div>

          <Card>
            <SectionTitle count={s.rows.length}>
              明细{includeNoise ? "（含爬虫/办公室）" : "（仅外部）"}
              {product && <span className="ml-1 text-slate-500">· {PRODUCT_LABEL[product]}</span>}
            </SectionTitle>
            {s.rows.length === 0 ? (
              <div className="text-[12px] text-slate-500">窗口内没有符合条件的记录。</div>
            ) : (
              <DataTable head={["时间", "IP · 归属地", "产品 / 版本", "渠道", "走向", "客户端", "标记", "装机"]}>
                {s.rows.map((r, i) => (
                  <tr key={`${r.t}-${r.ip}-${i}`} className="border-t border-ink-800">
                    <Td><span className="whitespace-nowrap tabular-nums text-slate-300">{fmtDateTime(r.t)}</span></Td>
                    <Td>
                      <div className="font-mono text-[12px] text-white">{r.ip}</div>
                      <div className="text-[11px] text-slate-500">{geoLabel(geo[r.ip])}</div>
                    </Td>
                    <Td>
                      <span className="text-slate-200">{PRODUCT_LABEL[r.product] || r.product}</span>
                      {r.version && <span className="ml-1 font-mono text-[11px] text-slate-400">{r.version}</span>}
                      {!/\.(exe|msi|dmg|zip|7z)$/i.test(r.path) && (
                        <span className="ml-1 text-[10px] text-slate-600">{r.path.split("/").pop()}</span>
                      )}
                    </Td>
                    <Td><span className={`text-[11px] ${r.channel === "internal" ? "text-amber-300" : "text-slate-400"}`}>{r.channel}</span></Td>
                    <Td>
                      <span className={`inline-block rounded-full border px-2 py-0.5 text-[11px] font-medium ${KIND_STYLE[r.kind]}`}>
                        {KIND_LABEL[r.kind]}
                      </span>
                      <span className="ml-1 text-[11px] text-slate-500">{VIA_LABEL[r.via] || r.via}{r.reason ? ` · ${r.reason}` : ""}</span>
                    </Td>
                    <Td><span className="text-[12px] text-slate-300" title={r.ua}>{shortUa(r.ua)}</span></Td>
                    <Td>
                      <div className="flex flex-wrap gap-1">
                        {r.flags.filter((f) => f !== "office" && f !== "range").map((f) => (
                          <span key={f} className={`rounded px-1.5 py-0.5 text-[10px] ${f === "sweep" || f === "ratelimit" ? "bg-rose-500/15 text-rose-300" : "bg-ink-800 text-slate-400"}`}>
                            {FLAG_LABEL[f] || f}
                          </span>
                        ))}
                      </div>
                    </Td>
                    <Td>
                      {r.fps.length ? (
                        <span className="font-mono text-[11px] text-emerald-300" title={r.fps.join(", ")}>
                          {r.fps.length === 1 ? r.fps[0].slice(0, 9) : `${r.fps.length} 台`}
                        </span>
                      ) : (
                        <span className="text-slate-600">—</span>
                      )}
                    </Td>
                  </tr>
                ))}
              </DataTable>
            )}
          </Card>

          <p className="text-[11px] leading-relaxed text-slate-600">
            <ShieldBan className="mr-1 inline h-3.5 w-3.5 align-text-bottom" />
            防护现状：爬虫 UA 403 · 同 IP 一小时不同安装包 ≤ {process.env.DL_RATE_MAX_FILES_PER_HOUR || 8} 个 · 横扫 ≥3 产品打标 ·
            /dl 与 /downloads 不入索引。看不到的部分：直接转发 <Code>dl.bd2026.cc</Code>（R2）链接的下载不经本站——
            要堵这一段需在 Cloudflare 侧给该域加 WAF（UA/速率/Referer）规则。
          </p>
        </>
      )}
    </div>
  );
}
