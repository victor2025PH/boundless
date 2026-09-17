"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { hostTag } from "@/lib/compute-host-tag";

// /admin/compute 看板本体（实施70 2026-08-27）：读 /api/admin/compute-status（5s 轮询）。
// 鉴权与 PaymentSettingsClient 同款：cookie 直通，未登录落口令闸门（key 只存内存 state）。
// 渲染防御式：快照由集群 pusher 拼装，任何字段缺失都按「未知」画灰，绝不白屏。

interface ChainNode {
  vendor?: string;
  model?: string;
  ok?: boolean;
  up?: boolean;
  latency_ms?: number;
  balance?: string;
  endpoint?: string;
  loaded?: boolean;
  note?: string;
  role?: string;
  rank?: number;
}
// 出话链：pusher v3 起带 mode/lock/order/roles（顺序与厂商从实例 overlay 现读）；
// v2 老快照只有 primary/pool/local —— 两种形状都要能画（官网与 pusher 不同步发版）。
interface ChainBlock {
  primary?: ChainNode;
  cloud?: ChainNode;
  pool?: ChainNode;
  local?: ChainNode;
  mode?: string;
  mode_label?: string;
  lock?: string | null;
  order?: string[];
  roles?: Record<string, string>;
  primary_text?: string;
  chain_text?: string;
  source?: string;
}
interface HostRow {
  host?: string;
  ip?: string;
  total_gb?: number;
  used_gb?: number | null;
  reachable?: boolean;
  kind?: string;
  note?: string;
  kv_cache_pct?: number | null;
  models?: Array<{ name?: string; vram_gb?: number | null }>;
}
interface FunctionRow {
  name?: string;
  backend?: string;
  cloud?: boolean;
  detail?: string;
  note?: string;
}
interface Snapshot {
  ts?: number;
  src?: string;
  chain?: ChainBlock;
  functions?: FunctionRow[];
  engine?: {
    up?: boolean;
    degraded?: boolean;
    runtime?: Record<string, unknown>;
    usage?: Record<string, unknown>;
  };
  hosts?: HostRow[];
  comfy?: { ok?: boolean; vram_free_gb?: number; vram_total_gb?: number; ip?: string };
  hub?: { ok?: boolean; mode?: string; held?: string[]; parked?: string[]; services_up?: number; services_total?: number };
  media?: {
    asr?: { ok?: boolean; asr_loaded?: boolean; ser_loaded?: boolean; endpoint?: string };
    tts104?: { ok?: boolean; models_loaded?: boolean; endpoint?: string };
    tts140?: { ok?: boolean; models_loaded?: boolean };
  };
}
interface StatusResp {
  ok?: boolean;
  latest?: Snapshot | null;
  age_sec?: number | null;
  events?: Array<{ ts: number; kind: string; text: string }>;
}

const card = "rounded-2xl border border-white/10 bg-white/[0.03] p-5";
const inputCls =
  "mt-1.5 w-full rounded-xl border border-white/10 bg-black/40 px-4 py-2.5 text-sm text-white placeholder-slate-600 outline-none transition focus:border-cyan-400/50";

function fmtTs(ts?: number): string {
  if (!ts) return "—";
  try {
    return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
  } catch {
    return String(ts);
  }
}
function gb(n?: number | null): string {
  return typeof n === "number" && isFinite(n) ? `${n.toFixed(1)}G` : "—";
}

// 三档卡的顺序与标题：v3 快照按 chain.order/roles；v2 老快照回落 08-27 的固定三档。
function chainTiers(chain?: ChainBlock): Array<{ key: string; title: string; sub: string; node?: ChainNode; okKey: "ok" | "up" }> {
  if (!chain) return [];
  const cloud = chain.cloud || chain.primary;
  const nodes: Record<string, ChainNode | undefined> = { cloud, pool: chain.pool, local: chain.local };
  const subs: Record<string, string> = { cloud: "cloud", pool: "key pool", local: "LAN vLLM" };
  const order = Array.isArray(chain.order) && chain.order.length ? chain.order : ["cloud", "pool", "local"];
  const legacy: Record<string, string> = { cloud: "① 主胎 · 云端", pool: "② 备胎 · key 池", local: "③ 三线 · 局域网本地" };
  return order
    .filter((k) => k in nodes)
    .map((k) => ({
      key: k,
      title: (chain.roles && chain.roles[k]) || legacy[k] || k,
      sub: subs[k] || k,
      node: nodes[k],
      okKey: k === "local" ? ("up" as const) : ("ok" as const),
    }));
}

function Dot({ on }: { on: boolean | null }) {
  const cls = on === null ? "bg-slate-500" : on ? "bg-emerald-400" : "bg-red-500";
  return <span className={`inline-block h-2.5 w-2.5 rounded-full ${cls}`} />;
}

function ChainCard({ title, sub, node, okKey }: { title: string; sub: string; node?: ChainNode; okKey: "ok" | "up" }) {
  const on = node ? (typeof node[okKey] === "boolean" ? (node[okKey] as boolean) : null) : null;
  return (
    <div className={`${card} flex-1 min-w-[220px]`}>
      <div className="flex items-center gap-2">
        <Dot on={on} />
        <span className="text-sm font-semibold text-white">{title}</span>
        <span className="ml-auto text-xs text-slate-500">{sub}</span>
      </div>
      <div className="mt-3 space-y-1 text-xs text-slate-300">
        <div>节点：<span className="text-slate-100">{node?.vendor || node?.endpoint || "—"}</span></div>
        <div>模型：<span className="text-slate-100">{node?.model || "—"}</span></div>
        {typeof node?.latency_ms === "number" && <div>探活延迟：{Math.round(node.latency_ms)} ms</div>}
        {node?.balance && <div>余额：<span className="text-emerald-300">{node.balance}</span></div>}
        {typeof node?.loaded === "boolean" && (
          <div>模型驻留：{node.loaded ? "已常驻（vLLM）" : "目录未挂载"}</div>
        )}
        {node?.note && <div className="text-slate-500">{node.note}</div>}
      </div>
    </div>
  );
}

function VramBar({ used, total }: { used?: number; total?: number }) {
  const u = typeof used === "number" ? used : 0;
  const t = typeof total === "number" && total > 0 ? total : 0;
  const pct = t > 0 ? Math.min(100, Math.round((u / t) * 100)) : 0;
  const color = pct >= 90 ? "bg-red-500" : pct >= 75 ? "bg-amber-400" : "bg-emerald-400";
  return (
    <div className="flex items-center gap-2">
      <div className="h-2 w-28 overflow-hidden rounded-full bg-white/10">
        <div className={`h-full ${color}`} style={{ width: `${pct}%` }} />
      </div>
      <span className="text-xs tabular-nums text-slate-300">
        {gb(used)}/{gb(total)}（{t > 0 ? `${pct}%` : "—"}）
      </span>
    </div>
  );
}

export default function ComputeStatusClient() {
  const [key, setKey] = useState("");
  const [gate, setGate] = useState<"checking" | "locked" | "open">("checking");
  const [data, setData] = useState<StatusResp | null>(null);
  const [msg, setMsg] = useState("");
  const keyRef = useRef("");

  const load = useCallback(async (k: string, silent = false) => {
    try {
      const url = `/api/admin/compute-status${k ? `?key=${encodeURIComponent(k)}` : ""}`;
      const r = await fetch(url, { cache: "no-store" });
      if (r.status === 401) {
        setGate("locked");
        if (!silent) setMsg("口令无效或会话已过期");
        return;
      }
      const j = (await r.json().catch(() => null)) as StatusResp | null;
      if (r.ok && j?.ok) {
        setData(j);
        setGate("open");
        keyRef.current = k;
        setMsg("");
      } else if (!silent) {
        setMsg("加载失败，请稍后重试");
      }
    } catch {
      if (!silent) setMsg("网络错误，请稍后重试");
    }
  }, []);

  // 首次不带 key 试探（cookie 直通）；开门后 5s 轮询。
  useEffect(() => {
    void load("", true).then(() => {
      setGate((g) => (g === "checking" ? "locked" : g));
    });
  }, [load]);
  useEffect(() => {
    if (gate !== "open") return;
    const t = setInterval(() => void load(keyRef.current, true), 5000);
    return () => clearInterval(t);
  }, [gate, load]);

  if (gate !== "open") {
    return (
      <main className="mx-auto flex min-h-screen max-w-md flex-col justify-center px-6 py-16">
        <h1 className="text-lg font-bold text-white">算力调度实时看板</h1>
        <p className="mt-2 text-sm text-slate-400">
          请先在 <a className="text-cyan-300 underline" href="/admin">/admin</a> 登录，或输入管理口令。
        </p>
        <input
          className={inputCls}
          type="password"
          placeholder="管理口令（ADMIN_KEY）"
          value={key}
          onChange={(e) => setKey(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") void load(key.trim()); }}
        />
        <button
          className="mt-3 rounded-xl bg-cyan-500/90 px-4 py-2.5 text-sm font-semibold text-black transition hover:bg-cyan-400"
          onClick={() => void load(key.trim())}
        >
          进入看板
        </button>
        {msg && <p className="mt-3 text-sm text-red-400">{msg}</p>}
        {gate === "checking" && <p className="mt-3 text-sm text-slate-500">正在检查会话…</p>}
      </main>
    );
  }

  const s = data?.latest || null;
  const age = data?.age_sec;
  const fresh = typeof age === "number" ? age : null;
  const freshCls =
    fresh === null ? "bg-slate-600/40 text-slate-300"
    : fresh <= 25 ? "bg-emerald-500/15 text-emerald-300"
    : fresh <= 60 ? "bg-amber-500/15 text-amber-300"
    : "bg-red-500/15 text-red-300";
  const freshText =
    fresh === null ? "尚无快照（等待集群上报）"
    : fresh <= 25 ? `实时 · ${fresh}s 前`
    : fresh <= 60 ? `延迟 · ${fresh}s 前`
    : `⚠ 上报中断 · 最后快照 ${fresh}s 前`;
  const usage = (s?.engine?.usage || {}) as Record<string, unknown>;

  return (
    <main className="mx-auto max-w-6xl px-4 py-8 text-white">
      <div className="flex flex-wrap items-center gap-3">
        <h1 className="text-xl font-bold">⚡ 算力调度实时看板</h1>
        <span className={`rounded-full px-3 py-1 text-xs font-semibold ${freshCls}`}>{freshText}</span>
        <span className="ml-auto text-xs text-slate-500">
          快照时间 {fmtTs(s?.ts)} · 5s 自动刷新 · <a className="underline" href="/admin">返回后台</a>
        </span>
      </div>

      {/* 出话链三档：顺序/标题来自快照 chain.order/roles（ai.primary 现值），不写死厂商 */}
      <h2 className="mt-6 text-sm font-semibold text-slate-400">
        主对话链（按序降级）
        {s?.chain?.mode && (
          <span className="ml-2 rounded-full bg-cyan-500/15 px-2 py-0.5 text-[11px] font-semibold text-cyan-300">
            档位 {s.chain.mode}{s.chain.mode_label ? ` · ${s.chain.mode_label}` : ""}
            {s.chain.lock ? ` · 🔒 锁 ${s.chain.lock}` : " · 未设锁"}
          </span>
        )}
      </h2>
      {s?.chain?.chain_text && (
        <div className="mt-1 text-xs text-slate-400">
          降级链：<span className="text-slate-200">{s.chain.chain_text}</span>
          {s.chain.source === "engine_summary" && <span className="ml-2 text-slate-600">（引擎单一口径）</span>}
        </div>
      )}
      <div className="mt-2 flex flex-wrap gap-3">
        {chainTiers(s?.chain).map((t) => (
          <ChainCard key={t.key} title={t.title} sub={t.sub} node={t.node} okKey={t.okKey} />
        ))}
        {!chainTiers(s?.chain).length && <p className="text-xs text-slate-500">暂无出话链数据</p>}
      </div>
      {Object.keys(usage).length > 0 && (
        <div className="mt-2 text-xs text-slate-400">
          出话分布（引擎口径）：
          <span className="text-slate-200"> {JSON.stringify(usage)}</span>
        </div>
      )}

      {/* 功能 × 算力落点真值表：哪些功能已在云端、哪些没对齐、为什么（实施70 §能力对齐） */}
      {!!(s?.functions || []).length && (
        <div className={`${card} mt-6`}>
          <h2 className="text-sm font-semibold text-slate-300">软件功能 × 算力落点（为什么还没全在云端，逐项说明）</h2>
          <div className="mt-3 overflow-x-auto">
            <table className="w-full text-left text-xs">
              <thead>
                <tr className="text-slate-500">
                  <th className="pb-2 pr-3 font-medium">功能</th>
                  <th className="pb-2 pr-3 font-medium">当前落点</th>
                  <th className="pb-2 pr-3 font-medium">明细/用量</th>
                  <th className="pb-2 font-medium">说明（不切/待切原因）</th>
                </tr>
              </thead>
              <tbody className="align-top">
                {(s?.functions || []).map((f, i) => (
                  <tr key={i} className="border-t border-white/5">
                    <td className="py-2 pr-3 font-semibold text-slate-100">{f.name}</td>
                    <td className="py-2 pr-3">
                      <span className={`rounded-md px-2 py-0.5 font-semibold ${f.cloud ? "bg-cyan-500/15 text-cyan-300" : "bg-white/5 text-slate-300"}`}>
                        {f.backend || "—"}
                      </span>
                    </td>
                    <td className="py-2 pr-3 text-slate-300">{f.detail || "—"}</td>
                    <td className="py-2 text-slate-400">{f.note || ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      <div className="mt-6 grid gap-3 md:grid-cols-2">
        {/* GPU 主机水位 */}
        <div className={card}>
          <h2 className="text-sm font-semibold text-slate-300">局域网 GPU 显存水位</h2>
          <div className="mt-3 space-y-3">
            {(s?.hosts || []).map((h, i) => (
              <div key={i}>
                <div className="flex items-center gap-2 text-xs">
                  <Dot on={typeof h.reachable === "boolean" ? h.reachable : null} />
                  <span className="font-semibold text-slate-100">{h.host || h.ip || `host${i}`}</span>
                  <span className="text-slate-500">{h.ip}</span>
                </div>
                {/* vLLM 主机预留式占显存、不按模型报字节：used_gb 为空时画 note（常驻模型 + KV cache 水位），不画 0% 假空卡 */}
                {typeof h.used_gb === "number" ? (
                  <div className="mt-1"><VramBar used={h.used_gb} total={h.total_gb} /></div>
                ) : h.reachable ? (
                  <div className="mt-1 text-xs text-slate-300">{h.note || "vLLM 常驻"} · 卡容量 {gb(h.total_gb)}</div>
                ) : null}
                {!!(h.models || []).length && (
                  <div className="mt-1 flex flex-wrap gap-1.5">
                    {(h.models || []).map((m, j) => (
                      <span key={j} className="rounded-md bg-white/5 px-2 py-0.5 text-[11px] text-slate-300">
                        {m.name}{typeof m.vram_gb === "number" ? ` · ${gb(m.vram_gb)}` : ""}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ))}
            {!(s?.hosts || []).length && <p className="text-xs text-slate-500">暂无主机数据</p>}
          </div>
        </div>

        <div className="space-y-3">
          {/* 出图卡 */}
          <div className={card}>
            <div className="flex items-center gap-2 text-sm font-semibold text-slate-300">
              <Dot on={typeof s?.comfy?.ok === "boolean" ? s.comfy.ok : null} />
              出图（ComfyUI @ {s?.comfy?.ip || "176:8188"}）
            </div>
            <div className="mt-2 text-xs text-slate-300">
              空闲显存 <span className={`font-semibold ${typeof s?.comfy?.vram_free_gb === "number" && s.comfy.vram_free_gb < 14 ? "text-amber-300" : "text-emerald-300"}`}>{gb(s?.comfy?.vram_free_gb)}</span>
              {" / "}{gb(s?.comfy?.vram_total_gb)}
              {typeof s?.comfy?.vram_free_gb === "number" && s.comfy.vram_free_gb < 14 && (
                <span className="ml-2 text-amber-300">（低于 14G：生成前需腾挪，更慢）</span>
              )}
            </div>
          </div>

          {/* hub 泊车 */}
          <div className={card}>
            <div className="flex items-center gap-2 text-sm font-semibold text-slate-300">
              <Dot on={typeof s?.hub?.ok === "boolean" ? s.hub.ok : null} />
              集群 hub（176:9000）
              <span className="ml-auto text-xs text-slate-500">
                服务在线 {s?.hub?.services_up ?? "—"}/{s?.hub?.services_total ?? "—"}
              </span>
            </div>
            {!!(s?.hub?.parked || []).length && (
              <div className="mt-2 flex flex-wrap gap-1.5">
                <span className="text-[11px] text-slate-500">已泊车:</span>
                {(s?.hub?.parked || []).map((x) => (
                  <span key={x} className="rounded-md bg-amber-500/10 px-2 py-0.5 text-[11px] text-amber-300">{x}</span>
                ))}
              </div>
            )}
            {!!(s?.hub?.held || []).length && (
              <div className="mt-1.5 flex flex-wrap gap-1.5">
                <span className="text-[11px] text-slate-500">挂起:</span>
                {(s?.hub?.held || []).map((x) => (
                  <span key={x} className="rounded-md bg-white/5 px-2 py-0.5 text-[11px] text-slate-400">{x}</span>
                ))}
              </div>
            )}
          </div>

          {/* 媒体服务 + 引擎 */}
          <div className={card}>
            <h2 className="text-sm font-semibold text-slate-300">引擎与媒体服务</h2>
            <div className="mt-2 flex flex-wrap gap-2 text-xs">
              <span className="flex items-center gap-1.5 rounded-md bg-white/5 px-2 py-1">
                <Dot on={typeof s?.engine?.up === "boolean" ? s.engine.up : null} /> 智聊引擎 18799
                {s?.engine?.degraded && <span className="text-amber-300">（降级中）</span>}
              </span>
              <span className="flex items-center gap-1.5 rounded-md bg-white/5 px-2 py-1">
                <Dot on={typeof s?.media?.asr?.ok === "boolean" ? s.media.asr.ok : null} /> ASR/SER {hostTag(s?.media?.asr?.endpoint, "@176")}
              </span>
              <span className="flex items-center gap-1.5 rounded-md bg-white/5 px-2 py-1">
                <Dot on={(s?.media?.tts104?.ok ?? s?.media?.tts140?.ok) ?? null} /> 克隆TTS {hostTag(s?.media?.tts104?.endpoint, "@104")}
              </span>
            </div>
          </div>
        </div>
      </div>

      {/* 调动事件流 */}
      <div className={`${card} mt-6`}>
        <h2 className="text-sm font-semibold text-slate-300">算力调动事件（可用性翻转留痕）</h2>
        <div className="mt-2 max-h-64 space-y-1 overflow-y-auto">
          {(data?.events || []).map((e, i) => (
            <div key={i} className="flex gap-3 text-xs">
              <span className="shrink-0 tabular-nums text-slate-500">{fmtTs(e.ts)}</span>
              <span className={/恢复/.test(e.text) ? "text-emerald-300" : "text-red-300"}>{e.text}</span>
            </div>
          ))}
          {!(data?.events || []).length && (
            <p className="text-xs text-slate-500">暂无事件——自看板上线以来三档链与关键服务未发生可用性翻转。</p>
          )}
        </div>
      </div>

      <p className="mt-4 text-[11px] leading-relaxed text-slate-600">
        数据由集群侧（117）每 ~10 秒推送一次；「上报中断」通常意味着 117 推送任务或公网出口异常，
        而非集群本身故障。三档链按上方「①→②→③」顺序降级（顺序由实例 ai.primary 档位决定：本地主链档先走局域网 vLLM、失败回落云端；云端主链档先走云端、再 key 池、再本地），全灭才回占位应答。
      </p>
    </main>
  );
}
