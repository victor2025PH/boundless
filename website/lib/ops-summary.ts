import { readFile, readdir, stat } from "fs/promises";
import os from "os";
import path from "path";
import { DATA_DIR } from "./data-dir";
import { listOrders } from "./order-store";

// 运营速览聚合：订单漏斗 + 产品质量（升级成功率/错误簇）+ 支持负载（诊断包）。
// 数据全部就地读（订单库 / 遥测 jsonl / diag 登记），无新增采集，纯聚合视图。
// TG /ops 命令、周报、/api/admin/ops-summary 共用这一份。

const TELE_DIR = process.env.AH_INGEST_DATA || path.join(os.homedir(), "avatarhub-telemetry");
const DIAG_LOG = path.join(DATA_DIR, "diag", "uploads.jsonl");
// usdt-watch 每次成功拉链都会重写状态文件 → mtime 即「监听在岗」心跳（日志 no-op 时不落行，靠不住）
const WATCH_STATE = process.env.USDT_WATCH_STATE || path.join(os.homedir(), ".usdt-watch-state.json");
const DAY = 86400_000;

export interface OpsSummary {
  now: number;
  orders7: { created: number; paid: number; activated: number; cancelled: number; revenue: number; pendingBacklog: number; paidBacklog: number };
  orders30: { created: number; activated: number; revenue: number };
  updates14: { total: number; ok: number; pairs: Record<string, { n: number; ok: number }> };
  crashes7: { sig: string; service: string; exc: string; n: number }[];
  diag7: { n: number; latest: string[] };
  payWatch: { addrConfigured: boolean; logFresh: boolean };
}

async function readJsonlFile(file: string, max = 50000): Promise<Record<string, unknown>[]> {
  try {
    const raw = await readFile(file, "utf-8");
    const out: Record<string, unknown>[] = [];
    for (const l of raw.split("\n").filter(Boolean).slice(-max)) {
      try {
        out.push(JSON.parse(l));
      } catch {
        /* skip broken line */
      }
    }
    return out;
  } catch {
    return [];
  }
}

/** 近 N 天的遥测事件（ingest 按天滚动 events-YYYYMMDD.jsonl）。 */
async function readTeleEvents(days: number): Promise<Record<string, unknown>[]> {
  const out: Record<string, unknown>[] = [];
  try {
    const names = (await readdir(TELE_DIR)).filter((n) => /^events-\d{8}\.jsonl$/.test(n)).sort();
    for (const n of names.slice(-days)) {
      out.push(...(await readJsonlFile(path.join(TELE_DIR, n))));
    }
  } catch {
    /* telemetry dir absent → 空集 */
  }
  return out;
}

function evTs(e: Record<string, unknown>): number {
  const recv = Number(e._recv ?? 0);
  if (recv > 0) return recv * 1000;
  const t = Date.parse(String(e.ts ?? ""));
  return isNaN(t) ? 0 : t;
}

export async function buildOpsSummary(): Promise<OpsSummary> {
  const now = Date.now();
  const d7 = now - 7 * DAY;
  const d14 = now - 14 * DAY;
  const d30 = now - 30 * DAY;

  // ── 订单漏斗 ──
  const orders = await listOrders();
  const tIn = (s: string | undefined, since: number) => {
    const t = Date.parse(String(s ?? ""));
    return !isNaN(t) && t >= since;
  };
  // 到账/入账口径排除已取消单：测试单与退款单都会走 paid→cancelled，留着会虚增收入
  const created7 = orders.filter((o) => tIn(o.t, d7));
  const paid7 = orders.filter((o) => tIn(o.paid_at, d7) && o.status !== "cancelled");
  const act7 = orders.filter((o) => tIn(o.activated_at, d7) && o.status !== "cancelled");
  const cancelled7 = created7.filter((o) => o.status === "cancelled");
  // 收入口径：窗口内完成到账的订单金额（activated 必经 paid，按 paid_at 归窗，不重复计）
  const revenue = (list: typeof orders) => Math.round(list.reduce((s, o) => s + (o.pay_amount || 0), 0) * 100) / 100;
  const summary: OpsSummary = {
    now,
    orders7: {
      created: created7.length,
      paid: paid7.length,
      activated: act7.length,
      cancelled: cancelled7.length,
      revenue: revenue(paid7),
      pendingBacklog: orders.filter((o) => o.status === "pending").length,
      paidBacklog: orders.filter((o) => o.status === "paid").length,
    },
    orders30: {
      created: orders.filter((o) => tIn(o.t, d30)).length,
      activated: orders.filter((o) => tIn(o.activated_at, d30) && o.status !== "cancelled").length,
      revenue: revenue(orders.filter((o) => tIn(o.paid_at, d30) && o.status !== "cancelled")),
    },
    updates14: { total: 0, ok: 0, pairs: {} },
    crashes7: [],
    diag7: { n: 0, latest: [] },
    payWatch: { addrConfigured: !!(process.env.NEXT_PUBLIC_USDT_ADDR || "").trim(), logFresh: false },
  };

  // ── 升级回执（14 天）＋ 错误簇（7 天）──
  const events = await readTeleEvents(15);
  const sigAgg: Record<string, { service: string; exc: string; n: number }> = {};
  // 1.0.10 客户端有个重复上报 bug（report_error 内已 flush，外层又 flush 一次），
  // 同一台机同一次升级会落两条一样的事件 → 按 anon_id+sig+ts 去重，成功率才是真的。
  const seenUpdate = new Set<string>();
  for (const e of events) {
    const ts = evTs(e);
    const kind = String(e.kind ?? "");
    if (kind === "update" && ts >= d14) {
      const dk = `${e.anon_id}|${e.sig}|${e.ts}`;
      if (seenUpdate.has(dk)) continue;
      seenUpdate.add(dk);
      summary.updates14.total++;
      const ctx = String(e.context ?? "");
      const ok = /ok=True/i.test(ctx);
      if (ok) summary.updates14.ok++;
      const m = ctx.match(/([\d.]+)->([\d.]+)/);
      if (m) {
        const key = `${m[1]}→${m[2]}`;
        const p = (summary.updates14.pairs[key] ??= { n: 0, ok: 0 });
        p.n++;
        if (ok) p.ok++;
      }
    } else if ((kind === "crash" || kind === "error") && ts >= d7) {
      const sig = String(e.sig ?? "?");
      const a = (sigAgg[sig] ??= { service: String(e.service ?? "?"), exc: String(e.exc ?? ""), n: 0 });
      a.n++;
    }
  }
  summary.crashes7 = Object.entries(sigAgg)
    .map(([sig, a]) => ({ sig, ...a }))
    .sort((a, b) => b.n - a.n)
    .slice(0, 5);

  // ── 诊断包（7 天）──
  const diag = (await readJsonlFile(DIAG_LOG, 2000)).filter((r) => tIn(String(r.t ?? ""), d7));
  summary.diag7 = { n: diag.length, latest: diag.slice(-3).map((r) => String(r.code ?? "")).reverse() };

  // ── 链上核销监听健康：状态文件 10 分钟内更新过（cron 每 2 分钟一跑）= 在岗 ──
  try {
    const st = await stat(WATCH_STATE);
    summary.payWatch.logFresh = now - st.mtimeMs < 10 * 60_000;
  } catch {
    /* 没状态文件 = 从未成功跑过 */
  }
  return summary;
}

function day(ms: number): string {
  return new Date(ms + 8 * 3600_000).toISOString().slice(5, 10);
}

// 崩溃签名常含 <string>:<module> 这类原始尖括号——不转义的话 Telegram 按 HTML 解析
// 直接拒收整条消息（parse error），/ops 与周报会一起静默失联。
function esc(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** TG（HTML）摘要文本；周报与 /ops 共用。 */
export function formatOpsSummary(s: OpsSummary): string {
  const o = s.orders7;
  const conv = (a: number, b: number) => (b > 0 ? `${Math.round((a / b) * 100)}%` : "—");
  const lines = [
    `💼 <b>运营速览</b>（近 7 天 · ${day(s.now - 7 * DAY)} ~ ${day(s.now)}）`,
    ``,
    `🧾 订单：新建 ${o.created} · 到账 ${o.paid} · 开通 ${o.activated} · 取消 ${o.cancelled}`,
    `💰 入账：${o.revenue} USDT（近 30 天 ${s.orders30.revenue} USDT · 开通 ${s.orders30.activated} 单）`,
    `🔁 转化：下单→到账 ${conv(o.paid, o.created)}｜积压：待付 ${o.pendingBacklog} · 已付待开通 ${o.paidBacklog}${o.paidBacklog > 0 ? " ⚠" : ""}`,
    `⛓ 自动核销：${s.payWatch.addrConfigured ? (s.payWatch.logFresh ? "在岗 ✅" : "已配置，cron 日志不新鲜 ⚠") : "未配置收款地址（NEXT_PUBLIC_USDT_ADDR）⚠ 到账仍需手点"}`,
  ];
  const u = s.updates14;
  if (u.total > 0) {
    const rate = Math.round((u.ok / u.total) * 100);
    const pairs = Object.entries(u.pairs)
      .map(([k, v]) => `${k} ${v.ok}/${v.n}`)
      .join("｜");
    lines.push(``, `⬆️ 客户端升级（14 天）：${u.total} 次 · 成功率 ${rate}%${rate < 90 ? " ⚠" : ""}${pairs ? `（${pairs}）` : ""}`);
  } else {
    lines.push(``, `⬆️ 客户端升级（14 天）：暂无回执`);
  }
  if (s.crashes7.length > 0) {
    lines.push(``, `🧨 错误簇 Top（7 天）`);
    for (const c of s.crashes7) {
      const label = c.sig.length > 60 ? c.sig.slice(0, 57) + "…" : c.sig;
      lines.push(`· [${esc(c.service)}] ${esc(c.exc || "error")} ×${c.n}\n  <code>${esc(label)}</code>`);
    }
  } else {
    lines.push(``, `🧨 错误簇（7 天）：无上报 ✅`);
  }
  lines.push(
    ``,
    `🧰 诊断包（7 天）：${s.diag7.n} 个${s.diag7.latest.length ? `（最新 ${s.diag7.latest.join(" ")}，回复 /diag 码 取包）` : ""}`
  );
  return lines.join("\n");
}
