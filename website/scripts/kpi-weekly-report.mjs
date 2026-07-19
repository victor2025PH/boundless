#!/usr/bin/env node
// 无界全矩阵 KPI 周报生成器（kpi-weekly-report）— P4 收口。
//
// 口径单一真相：platform/observability/KPI_DEFINITIONS.md（v1）。本脚本是该口径的
// 参考实现，任何数字与口径文件冲突时以口径文件为准并修本脚本（口径文件 §8）。
//
// 数据源（一律只读打开，不建表、不迁移、不写任何库）：
//   - 行为：group-events.db（env EVENTS_DB，缺省 DATA_DIR/group-events.db）
//   - 钱/授权：group-ledger.db（env LEDGER_DB，缺省 DATA_DIR/group-ledger.db）
//
// 用法：
//   node scripts/kpi-weekly-report.mjs                                  # 近 7 天（UTC 日含今天），md → stdout
//   node scripts/kpi-weekly-report.mjs --week last                      # 上一完整 ISO 周（周一 00:00Z 起 7 天）
//   node scripts/kpi-weekly-report.mjs --week 2026-W29                  # 指定 ISO 周
//   node scripts/kpi-weekly-report.mjs --days 14                        # 近 N 天
//   node scripts/kpi-weekly-report.mjs --since 2026-07-06 --until 2026-07-13   # 半开区间 [since, until)，UTC 日
//   node scripts/kpi-weekly-report.mjs --format json --out ../reports/kpi/2026-W29.json
//
// 容错：库不存在/为空 → 输出"暂无数据"骨架报告（含回填指引）并退出 0，cron 可常开。

import fs from "node:fs";
import path from "node:path";
import process from "node:process";
import Database from "better-sqlite3";
import { inferProductId, resolveDataDir, resolveLedgerDbPath } from "./ledger-lib.mjs";

const KPI_VERSION = "KPI_DEFINITIONS v1";
const DAY = 86400000;

// ── 七产品矩阵与口径常量（与 KPI_DEFINITIONS.md §4 一一对应，改口径先改那边）──
const PRODUCTS = [
  { id: "zhituo", label: "智拓" },
  { id: "zhiliao", label: "智聊" },
  { id: "tongyi", label: "通译" },
  { id: "tongchuan", label: "通传" },
  { id: "huansheng", label: "幻声" },
  { id: "huanying", label: "幻影" },
  { id: "huanyan", label: "幻颜" },
];
const PRODUCT_LABEL = Object.fromEntries(PRODUCTS.map((p) => [p.id, p.label]));

// 活跃判定事件集（任一即活跃，口径 §4）
const ACTIVITY_EVENTS = {
  zhituo: ["zhituo.friend.added"],
  zhiliao: ["zhiliao.session.ai_engaged"],
  tongyi: ["tongyi.translation.chars_metered"],
  tongchuan: ["tongchuan.session.started", "tongchuan.session.ended"],
  huansheng: ["huansheng.voice.clone_completed", "huansheng.tts.chars_metered"],
  huanying: ["huanying.live.started"],
  huanyan: ["huanyan.faceswap.completed"],
};

// 北极星指标（口径 §4）：kind=count 计条数；kind=sum 累加 props[prop]
const NORTH_STAR = {
  zhituo: { name: "周开口数", unit: "条", kind: "count", events: ["zhituo.prospect.replied"] },
  zhiliao: { name: "周 AI 承接会话数", unit: "条", kind: "count", events: ["zhiliao.session.ai_engaged"] },
  tongyi: { name: "周翻译字符数", unit: "字符", kind: "sum", events: ["tongyi.translation.chars_metered"], prop: "chars" },
  tongchuan: {
    name: "周同传分钟数",
    unit: "分钟",
    kind: "sum",
    events: ["tongchuan.session.ended"],
    prop: "audio_minutes",
    fallbackProp: "duration_s",
    fallbackScale: 1 / 60,
  },
  huansheng: { name: "周 TTS 合成字符数", unit: "字符", kind: "sum", events: ["huansheng.tts.chars_metered"], prop: "chars" },
  huanying: { name: "周开播场次", unit: "场", kind: "count", events: ["huanying.live.started"] },
  huanyan: { name: "周换脸完成任务数", unit: "个", kind: "count", events: ["huanyan.faceswap.completed"] },
};

const USAGE = `用法：node scripts/kpi-weekly-report.mjs [窗口] [--format md|json] [--out 文件]
窗口（三组互斥，缺省近 7 天 UTC 日含今天）：
  --week this|last|YYYY-Www   ISO 周（周一 00:00Z 起 7 天）
  --days N                    近 N 天（UTC 日含今天）
  --since YYYY-MM-DD [--until YYYY-MM-DD]   半开区间 [since, until)，until 缺省为明日 00:00Z
其他：
  --format md|json            输出格式，缺省 md
  --out <path>                写入文件，缺省 stdout
环境变量：EVENTS_DB / LEDGER_DB 覆盖库路径，缺省 DATA_DIR 下同名库。
`;

function die(msg) {
  process.stderr.write(`错误：${msg}\n\n${USAGE}`);
  process.exit(1);
}

// ── 参数解析 ────────────────────────────────────────────────────────
function parseArgs(argv) {
  const a = { format: "md", out: null, week: null, days: null, since: null, until: null };
  for (let i = 0; i < argv.length; i++) {
    const k = argv[i];
    const need = () => {
      const v = argv[++i];
      if (v === undefined) die(`${k} 缺参数值`);
      return v;
    };
    if (k === "--week") a.week = need();
    else if (k === "--days") a.days = need();
    else if (k === "--since") a.since = need();
    else if (k === "--until") a.until = need();
    else if (k === "--format") a.format = need();
    else if (k === "--out") a.out = need();
    else if (k === "--help" || k === "-h") {
      process.stdout.write(USAGE);
      process.exit(0);
    } else die(`未知参数 ${k}`);
  }
  if (a.format !== "md" && a.format !== "json") die("--format 只支持 md|json");
  const groups = [a.week !== null, a.days !== null, a.since !== null || a.until !== null].filter(Boolean);
  if (groups.length > 1) die("--week / --days / --since|--until 三组互斥，只能选一组");
  return a;
}

// ── 时间窗口（一律 UTC、半开区间 [since, until)，口径 §1）──────────
const iso = (ms) => new Date(ms).toISOString();

function utcDayStart(ms) {
  const d = new Date(ms);
  return Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
}

function parseDay(str, flag) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(str).trim());
  if (!m) die(`${flag} 需为 YYYY-MM-DD：${str}`);
  const ms = Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  const d = new Date(ms);
  if (d.getUTCMonth() !== Number(m[2]) - 1 || d.getUTCDate() !== Number(m[3])) die(`${flag} 不是合法日期：${str}`);
  return ms;
}

function isoWeekStartMs(year, week) {
  const jan4 = Date.UTC(year, 0, 4);
  const dow = (new Date(jan4).getUTCDay() + 6) % 7; // 0 = 周一
  return jan4 - dow * DAY + (week - 1) * 7 * DAY;
}

function isoWeekLabel(sinceMs) {
  const thu = sinceMs + 3 * DAY; // ISO 周归属看周四
  const y = new Date(thu).getUTCFullYear();
  const wk = Math.floor((thu - isoWeekStartMs(y, 1)) / (7 * DAY)) + 1;
  return `${y}-W${String(wk).padStart(2, "0")}`;
}

function resolveWindow(a, nowMs) {
  const todayStart = utcDayStart(nowMs);
  let since, until, label;
  if (a.since !== null || a.until !== null) {
    until = a.until !== null ? parseDay(a.until, "--until") : todayStart + DAY;
    since = a.since !== null ? parseDay(a.since, "--since") : until - 7 * DAY;
    if (since >= until) die("--since 必须早于 --until");
    label = "自定义窗口";
  } else if (a.days !== null) {
    const n = Math.trunc(Number(a.days));
    if (!Number.isFinite(n) || n < 1 || n > 366) die("--days 需为 1..366 的整数");
    until = todayStart + DAY;
    since = until - n * DAY;
    label = `近 ${n} 天`;
  } else if (a.week !== null) {
    const w = String(a.week).trim().toLowerCase();
    const curDow = (new Date(todayStart).getUTCDay() + 6) % 7;
    const curWeekStart = todayStart - curDow * DAY;
    if (w === "this") since = curWeekStart;
    else if (w === "last") since = curWeekStart - 7 * DAY;
    else {
      const m = /^(\d{4})-w(\d{1,2})$/.exec(w);
      if (!m) die("--week 需为 this|last|YYYY-Www（如 2026-W29）");
      const wk = Number(m[2]);
      if (wk < 1 || wk > 53) die(`--week 周号越界：${a.week}`);
      since = isoWeekStartMs(Number(m[1]), wk);
    }
    until = since + 7 * DAY;
    label = `ISO 周 ${isoWeekLabel(since)}`;
  } else {
    until = todayStart + DAY;
    since = until - 7 * DAY;
    label = "近 7 天（缺省）";
  }
  return { since, until, prevSince: since - (until - since), prevUntil: since, label };
}

// ── 只读开库 ────────────────────────────────────────────────────────
function resolveEventsDbPath() {
  return process.env.EVENTS_DB || path.join(resolveDataDir(), "group-events.db");
}

function openReadonly(file, requiredTables) {
  if (!fs.existsSync(file)) return { db: null, reason: "库文件不存在" };
  let db;
  try {
    db = new Database(file, { readonly: true, fileMustExist: true });
  } catch (e) {
    return { db: null, reason: `打开失败：${e.message}` };
  }
  try {
    const have = new Set(db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all().map((r) => r.name));
    const missing = requiredTables.filter((t) => !have.has(t));
    if (missing.length) {
      db.close();
      return { db: null, reason: `缺表 ${missing.join("/")}（库尚未初始化）` };
    }
  } catch (e) {
    try { db.close(); } catch { /* ignore */ }
    return { db: null, reason: `读取失败：${e.message}` };
  }
  return { db, reason: null };
}

// ── 事件侧聚合（单遍扫 [prevSince, until)，按 ts 分流本期/上期）─────
function parseProps(text) {
  try {
    const o = JSON.parse(text || "{}");
    return o && typeof o === "object" && !Array.isArray(o) ? o : {};
  } catch {
    return null; // 解析失败 → 计量异常
  }
}

const numOrNull = (v) => (typeof v === "number" && Number.isFinite(v) ? v : null);

function mkEventsAgg() {
  return {
    total: 0,
    byProduct: {},
    sources: {},
    unregistered: 0,
    renewedEvents: [], // 每条 platform.license.renewed 的 props.license_id（无则 null）
    anonymousActive: 0,
    meteringAnomalies: 0,
    top: new Map(),
    pp: Object.fromEntries(PRODUCTS.map((p) => [p.id, { activeDays: new Set(), subjects: new Set(), northStar: 0 }])),
  };
}

function collectEvents(db, win) {
  const cur = mkEventsAgg();
  const prev = mkEventsAgg();
  const sinceIso = iso(win.since);
  const stmt = db.prepare(
    "SELECT ts, product_id, name, workspace_id, customer_id, props, unregistered, source FROM events WHERE ts >= ? AND ts < ?"
  );
  for (const r of stmt.iterate(iso(win.prevSince), iso(win.until))) {
    const agg = r.ts >= sinceIso ? cur : prev;
    agg.total++;
    agg.byProduct[r.product_id] = (agg.byProduct[r.product_id] || 0) + 1;
    const src = r.source || "unknown";
    agg.sources[src] = (agg.sources[src] || 0) + 1;
    if (r.unregistered) agg.unregistered++;
    const tk = `${r.name}\u0000${r.product_id}`;
    agg.top.set(tk, (agg.top.get(tk) || 0) + 1);
    if (r.name === "platform.license.renewed") {
      const p = parseProps(r.props);
      agg.renewedEvents.push(p && typeof p.license_id === "string" ? p.license_id : null);
    }
    const pp = agg.pp[r.product_id];
    if (!pp) continue; // website/platform 不参与活跃/北极星口径
    if ((ACTIVITY_EVENTS[r.product_id] || []).includes(r.name)) {
      pp.activeDays.add(r.ts.slice(0, 10));
      const subj = r.workspace_id || r.customer_id;
      if (subj) pp.subjects.add(subj);
      else agg.anonymousActive++; // 匿名活跃：计事件与活跃日，不计主体/留存（口径 §1）
    }
    const ns = NORTH_STAR[r.product_id];
    if (ns && ns.events.includes(r.name)) {
      if (ns.kind === "count") pp.northStar++;
      else {
        const p = parseProps(r.props);
        const v = numOrNull(p ? p[ns.prop] : null);
        if (v !== null && v >= 0) pp.northStar += v;
        else if (ns.fallbackProp) {
          const f = numOrNull(p ? p[ns.fallbackProp] : null);
          if (f !== null && f >= 0) pp.northStar += f * ns.fallbackScale;
          else agg.meteringAnomalies++;
        } else agg.meteringAnomalies++;
      }
    }
  }
  const t = db
    .prepare(
      "SELECT COUNT(*) AS total, COALESCE(SUM(unregistered),0) AS unreg, MIN(ts) AS first_ts, MAX(ts) AS last_ts FROM events"
    )
    .get();
  return { cur, prev, dbTotals: { total: t.total, unregistered: t.unreg, first_ts: t.first_ts, last_ts: t.last_ts } };
}

// 测试数据过滤片段（实施22）：表有 is_test 列时排除 is_test=1；无列（未迁移旧库/只读快照）
// 返回空串不误伤。KPI 只算真实经营数据。
function testExclClause(db, table) {
  try {
    const has = db.prepare(`PRAGMA table_info(${table})`).all().some((c) => c.name === "is_test");
    return has ? " AND COALESCE(is_test,0) = 0" : "";
  } catch {
    return "";
  }
}

// ── 账本侧聚合 ──────────────────────────────────────────────────────
function collectLedger(db, win) {
  const S = iso(win.since);
  const U = iso(win.until);
  const XO = testExclClause(db, "orders");
  const XL = testExclClause(db, "leads");
  const XC = testExclClause(db, "licenses");
  const PS = iso(win.prevSince);
  const PU = iso(win.prevUntil);

  // 线索：first_seen 归属窗口，interest → inferProductId（口径 §1/§2.2）
  function leadsAgg(s, u) {
    const rows = db
      .prepare(`SELECT interest, COUNT(*) AS c FROM leads WHERE first_seen >= ? AND first_seen < ?${XL} GROUP BY interest`)
      .all(s, u);
    const byProduct = {};
    let total = 0;
    let unmapped = 0;
    for (const r of rows) {
      total += r.c;
      const pid = inferProductId(r.interest, null);
      if (pid && PRODUCT_LABEL[pid]) byProduct[pid] = (byProduct[pid] || 0) + r.c;
      else unmapped += r.c;
    }
    return { total, byProduct, unmapped };
  }

  // 订单/支付：按阶段时间戳列归属（口径 §2.3/§2.4）
  function ordersAgg(col, s, u) {
    const rows = db
      .prepare(
        `SELECT COALESCE(product_id, '') AS pid, COUNT(*) AS c FROM orders WHERE ${col} >= ? AND ${col} < ?${XO} GROUP BY pid`
      )
      .all(s, u);
    const byProduct = {};
    let total = 0;
    for (const r of rows) {
      total += r.c;
      byProduct[r.pid] = (byProduct[r.pid] || 0) + r.c;
    }
    return { total, byProduct };
  }

  function paidAmounts(s, u) {
    const rows = db
      .prepare(
        `SELECT COALESCE(product_id, '') AS pid, COALESCE(currency, '(未知币种)') AS ccy,
                COALESCE(SUM(pay_amount), 0) AS amt
         FROM orders WHERE paid_at >= ? AND paid_at < ?${XO} GROUP BY pid, ccy`
      )
      .all(s, u);
    const total = {};
    const byProduct = {};
    for (const r of rows) {
      total[r.ccy] = (total[r.ccy] || 0) + r.amt;
      if (!byProduct[r.pid]) byProduct[r.pid] = {};
      byProduct[r.pid][r.ccy] = (byProduct[r.pid][r.ccy] || 0) + r.amt;
    }
    return { total, byProduct };
  }

  // 激活合并去重（口径 §3）：订单先计；同 customer_id 同 UTC 日的授权判为同一次交付
  function activatedAgg(s, u) {
    const ords = db
      .prepare(
        `SELECT COALESCE(product_id, '') AS pid, customer_id, substr(activated_at, 1, 10) AS day FROM orders WHERE activated_at >= ? AND activated_at < ?${XO}`
      )
      .all(s, u);
    const lics = db
      .prepare(
        `SELECT COALESCE(product_id, '') AS pid, customer_id, substr(issued_at, 1, 10) AS day FROM licenses WHERE issued_at >= ? AND issued_at < ?${XC}`
      )
      .all(s, u);
    const orderKey = new Set(ords.filter((o) => o.customer_id).map((o) => `${o.customer_id}|${o.day}`));
    const byProduct = {};
    let total = 0;
    let merged = 0;
    for (const o of ords) {
      total++;
      byProduct[o.pid] = (byProduct[o.pid] || 0) + 1;
    }
    for (const l of lics) {
      if (l.customer_id && orderKey.has(`${l.customer_id}|${l.day}`)) {
        merged++;
        continue;
      }
      total++;
      byProduct[l.pid] = (byProduct[l.pid] || 0) + 1;
    }
    return { total, merged, byProduct };
  }

  // 授权：本期新签 + 30 天内到期（以窗口末端 until 为"现在"，可复现，口径 §7 呼应）
  function licensesNew(s, u) {
    const rows = db
      .prepare(
        `SELECT COALESCE(product_id, '') AS pid, COUNT(*) AS c FROM licenses WHERE issued_at >= ? AND issued_at < ?${XC} GROUP BY pid`
      )
      .all(s, u);
    const byProduct = {};
    let total = 0;
    for (const r of rows) {
      total += r.c;
      byProduct[r.pid] = (byProduct[r.pid] || 0) + r.c;
    }
    return { total, byProduct };
  }

  const horizon = iso(win.until + 30 * DAY);
  const expRows = db
    .prepare(
      `SELECT COALESCE(product_id, '') AS pid, COUNT(*) AS c FROM licenses
       WHERE expires_at IS NOT NULL AND expires_at > ? AND expires_at <= ?
         AND (status IS NULL OR status NOT IN ('revoked','expired'))${XC}
       GROUP BY pid`
    )
    .all(U, horizon);
  const expiring = { as_of: U, horizon, total: 0, byProduct: {} };
  for (const r of expRows) {
    expiring.total += r.c;
    expiring.byProduct[r.pid] = (expiring.byProduct[r.pid] || 0) + r.c;
  }

  // 归属缺失（本期，口径 §7）
  const gapCount = (sql, ...p) => db.prepare(sql).get(...p).c;
  const gaps = {
    orders_no_product: gapCount(
      `SELECT COUNT(*) AS c FROM orders WHERE created_at >= ? AND created_at < ?${XO} AND (product_id IS NULL OR product_id = '')`,
      S, U
    ),
    orders_no_customer: gapCount(
      `SELECT COUNT(*) AS c FROM orders WHERE created_at >= ? AND created_at < ?${XO} AND (customer_id IS NULL OR customer_id = '')`,
      S, U
    ),
    licenses_no_customer: gapCount(
      `SELECT COUNT(*) AS c FROM licenses WHERE issued_at >= ? AND issued_at < ?${XC} AND (customer_id IS NULL OR customer_id = '')`,
      S, U
    ),
  };

  const licProductStmt = db.prepare("SELECT product_id FROM licenses WHERE id = ?");

  return {
    leads: { cur: leadsAgg(S, U), prev: leadsAgg(PS, PU) },
    orders: { cur: ordersAgg("created_at", S, U), prev: ordersAgg("created_at", PS, PU) },
    paid: { cur: ordersAgg("paid_at", S, U), prev: ordersAgg("paid_at", PS, PU) },
    paidAmounts: { cur: paidAmounts(S, U), prev: paidAmounts(PS, PU) },
    activated: { cur: activatedAgg(S, U), prev: activatedAgg(PS, PU) },
    licensesNew: { cur: licensesNew(S, U), prev: licensesNew(PS, PU) },
    expiring,
    gaps,
    licenseProductOf: (id) => {
      if (!id) return null;
      const row = licProductStmt.get(id);
      return row ? row.product_id : null;
    },
  };
}

// ── 指标组装 ────────────────────────────────────────────────────────
function cmpN(cur, prev) {
  return { cur, prev, pct: prev > 0 ? Math.round(((cur - prev) / prev) * 1000) / 10 : null };
}

function retentionOf(prevSet, curSet) {
  const prevActive = prevSet.size;
  let retained = 0;
  for (const s of prevSet) if (curSet.has(s)) retained++;
  return { prev_active: prevActive, retained, rate: prevActive > 0 ? Math.round((retained / prevActive) * 1000) / 1000 : null };
}

function otherBucket(agg) {
  // "(未归属/其他)" = 总数 − 七产品之和（含 product_id 为空/website 等非引擎归属）
  let seven = 0;
  for (const p of PRODUCTS) seven += agg.byProduct[p.id] || 0;
  return agg.total - seven;
}

function round2(v) {
  return Math.round(v * 100) / 100;
}

function buildReport(win, args, sides) {
  const { events, ledger } = sides;
  const ev = events.data; // null 当不可用
  const lg = ledger.data;

  const notices = [];
  if (!events.available) {
    notices.push(
      `事件库不可用（${events.path}：${events.reason}）——行为类指标（活跃/留存/北极星/Top 事件/续费）暂无数据。` +
        `回填：确认 EVENTS_DB / DATA_DIR 指向正确；在各产品机器跑 platform/observability/uploader.py 补传 spool` +
        `（EVENT_CONTRACT §10.4），事件按 event_id 幂等、重放安全。`
    );
  }
  if (!ledger.available) {
    notices.push(
      `账本库不可用（${ledger.path}：${ledger.reason}）——漏斗钱侧（线索/订单/支付/激活/授权）暂无数据。` +
        `回填：cd website && node scripts/ledger-backfill.mjs（JSON 主真相源 → 账本镜像）；` +
        `授权用 node scripts/ledger-import-licenses.mjs 导入。`
    );
  }

  // 续费（事件侧，口径 §2.7）：总数 = 事件条数；分产品经账本 licenses.id → product_id
  let renewed = null;
  let renewedByProduct = null;
  let renewedUnattributed = 0;
  if (ev) {
    renewed = cmpN(ev.cur.renewedEvents.length, ev.prev.renewedEvents.length);
    renewedByProduct = { cur: {}, prev: {} };
    for (const [key, list] of [["cur", ev.cur.renewedEvents], ["prev", ev.prev.renewedEvents]]) {
      for (const licId of list) {
        const pid = lg ? lg.licenseProductOf(licId) : null;
        if (pid && PRODUCT_LABEL[pid]) renewedByProduct[key][pid] = (renewedByProduct[key][pid] || 0) + 1;
        else if (key === "cur") renewedUnattributed++;
      }
    }
  }

  // 全域活跃主体（七产品 union）与留存（口径 §4/§5）
  let activeSubjects = null;
  let retention = null;
  if (ev) {
    const unionOf = (agg) => {
      const u = new Set();
      for (const p of PRODUCTS) for (const s of agg.pp[p.id].subjects) u.add(s);
      return u;
    };
    const curU = unionOf(ev.cur);
    const prevU = unionOf(ev.prev);
    activeSubjects = cmpN(curU.size, prevU.size);
    retention = retentionOf(prevU, curU);
  }

  // 全域漏斗（口径 §2）
  const funnel = {
    exposure_note: "v1 暂缺：registry 无 pageview 类事件，漏斗自线索起算（KPI_DEFINITIONS §2.1）",
    leads: lg ? cmpN(lg.leads.cur.total, lg.leads.prev.total) : null,
    orders: lg ? cmpN(lg.orders.cur.total, lg.orders.prev.total) : null,
    paid: lg ? cmpN(lg.paid.cur.total, lg.paid.prev.total) : null,
    paid_amounts: lg
      ? {
          cur: Object.fromEntries(Object.entries(lg.paidAmounts.cur.total).map(([k, v]) => [k, round2(v)])),
          prev: Object.fromEntries(Object.entries(lg.paidAmounts.prev.total).map(([k, v]) => [k, round2(v)])),
        }
      : null,
    activated: lg ? cmpN(lg.activated.cur.total, lg.activated.prev.total) : null,
    activated_merged_licenses: lg ? lg.activated.cur.merged : null,
    active_subjects: activeSubjects,
    retention,
    renewed,
  };

  // 分产品（口径 §4）
  const products = PRODUCTS.map((p) => {
    const id = p.id;
    const fl = lg
      ? {
          leads: cmpN(lg.leads.cur.byProduct[id] || 0, lg.leads.prev.byProduct[id] || 0),
          orders: cmpN(lg.orders.cur.byProduct[id] || 0, lg.orders.prev.byProduct[id] || 0),
          paid: cmpN(lg.paid.cur.byProduct[id] || 0, lg.paid.prev.byProduct[id] || 0),
          paid_amounts_cur: Object.fromEntries(
            Object.entries(lg.paidAmounts.cur.byProduct[id] || {}).map(([k, v]) => [k, round2(v)])
          ),
          activated: cmpN(lg.activated.cur.byProduct[id] || 0, lg.activated.prev.byProduct[id] || 0),
        }
      : null;
    const renewedCmp = renewedByProduct
      ? cmpN(renewedByProduct.cur[id] || 0, renewedByProduct.prev[id] || 0)
      : null;
    let activity = null;
    let northStar = null;
    if (ev) {
      const c = ev.cur.pp[id];
      const pv = ev.prev.pp[id];
      activity = {
        events: cmpN(ev.cur.byProduct[id] || 0, ev.prev.byProduct[id] || 0),
        active_days: cmpN(c.activeDays.size, pv.activeDays.size),
        active_subjects: cmpN(c.subjects.size, pv.subjects.size),
        retention: retentionOf(pv.subjects, c.subjects),
      };
      const ns = NORTH_STAR[id];
      northStar = { name: ns.name, unit: ns.unit, ...cmpN(round2(c.northStar), round2(pv.northStar)) };
    }
    return { id, label: p.label, funnel: fl, renewed: renewedCmp, activity, north_star: northStar };
  });

  // "(未归属/其他)" 桶（本期，口径 §1：全域总数按行计，分产品按归属计）
  const unattributed = {
    leads: lg ? lg.leads.cur.unmapped : null,
    orders: lg ? otherBucket(lg.orders.cur) : null,
    paid: lg ? otherBucket(lg.paid.cur) : null,
    activated: lg ? otherBucket(lg.activated.cur) : null,
    renewed: ev ? renewedUnattributed : null,
  };

  // 授权与到期
  const licenses = lg
    ? {
        new: {
          ...cmpN(lg.licensesNew.cur.total, lg.licensesNew.prev.total),
          by_product: lg.licensesNew.cur.byProduct,
        },
        expiring_30d: {
          as_of: lg.expiring.as_of,
          horizon: lg.expiring.horizon,
          count: lg.expiring.total,
          by_product: lg.expiring.byProduct,
        },
      }
    : null;

  // Top 事件（本期）
  const topEvents = ev
    ? [...ev.cur.top.entries()]
        .map(([k, count]) => {
          const [name, product_id] = k.split("\u0000");
          return { name, product_id, count };
        })
        .sort((a, b) => b.count - a.count || (a.name < b.name ? -1 : 1))
        .slice(0, 10)
    : null;

  // 数据健康（口径 §7）
  const zeroEventProducts = [];
  if (ev) {
    for (const p of PRODUCTS) {
      if ((ev.cur.byProduct[p.id] || 0) === 0) {
        zeroEventProducts.push({
          product_id: p.id,
          label: p.label,
          level: "alert",
          message: `本期 0 事件——埋点未上报或无业务，需人工排查（uploader 断传 vs 真无业务）`,
        });
      }
    }
    for (const pid of ["website", "platform"]) {
      if ((ev.cur.byProduct[pid] || 0) === 0) {
        zeroEventProducts.push({
          product_id: pid,
          label: pid,
          level: "info",
          message: "本期 0 事件（漏斗钱侧走账本不受影响，但交叉校验缺失）",
        });
      }
    }
  }
  const health = {
    events_window: ev ? cmpN(ev.cur.total, ev.prev.total) : null,
    db_totals: ev ? events.data.dbTotalsOut : null,
    sources: ev
      ? Object.entries(ev.cur.sources)
          .map(([source, count]) => ({ source, count }))
          .sort((a, b) => b.count - a.count)
      : null,
    unregistered_window: ev ? ev.cur.unregistered : null,
    anonymous_active_events: ev ? ev.cur.anonymousActive : null,
    metering_anomalies: ev ? ev.cur.meteringAnomalies : null,
    ledger_gaps: lg ? { ...lg.gaps, leads_unmapped_interest: lg.leads.cur.unmapped } : null,
    zero_event_products: zeroEventProducts,
  };

  return {
    meta: {
      title: "无界全矩阵 KPI 周报",
      generated_at: new Date().toISOString(),
      kpi_version: KPI_VERSION,
      window: { label: win.label, since: iso(win.since), until: iso(win.until) },
      prev_window: { since: iso(win.prevSince), until: iso(win.prevUntil) },
      format: args.format,
      readonly: true,
    },
    sources: {
      events: { path: events.path, available: events.available, reason: events.reason },
      ledger: { path: ledger.path, available: ledger.available, reason: ledger.reason },
    },
    notices,
    funnel,
    products,
    unattributed,
    licenses,
    top_events: topEvents,
    health,
  };
}

// ── Markdown 渲染 ───────────────────────────────────────────────────
const NA = "暂无数据";

function fmtNum(v) {
  if (v === null || v === undefined) return "—";
  return Number.isInteger(v) ? String(v) : String(round2(v));
}

function fmtPct(c) {
  if (!c) return "—";
  if (c.pct === null) return c.prev === 0 && c.cur > 0 ? "新增（上期 0）" : "—";
  return `${c.pct > 0 ? "+" : ""}${c.pct.toFixed(1)}%`;
}

function fmtAmounts(m) {
  const ks = Object.keys(m || {});
  if (!ks.length) return "0";
  return ks
    .sort()
    .map((k) => `${fmtNum(m[k])} ${k}`)
    .join(" / ");
}

function fmtRetention(r) {
  if (!r) return NA;
  if (r.rate === null) return "—（上期无活跃主体）";
  return `${(r.rate * 100).toFixed(1)}%（上期 ${r.prev_active} 个主体中 ${r.retained} 个本期仍活跃）`;
}

function cmpRow(label, c) {
  if (!c) return `| ${label} | ${NA} | ${NA} | — |`;
  return `| ${label} | ${fmtNum(c.cur)} | ${fmtNum(c.prev)} | ${fmtPct(c)} |`;
}

function byProductLine(map) {
  const parts = [];
  for (const p of PRODUCTS) if (map[p.id]) parts.push(`${p.label} ${p.id} ${map[p.id]}`);
  let seven = 0;
  for (const p of PRODUCTS) seven += map[p.id] || 0;
  const other = Object.values(map).reduce((a, b) => a + b, 0) - seven;
  if (other > 0) parts.push(`(未归属/其他) ${other}`);
  return parts.length ? parts.join("、") : "无";
}

function renderMd(r) {
  const L = [];
  L.push(`# ${r.meta.title}`);
  L.push("");
  L.push(
    `> 窗口：${r.meta.window.since.slice(0, 10)} ~ ${r.meta.window.until.slice(0, 10)}（UTC，半开区间 [since, until)，${r.meta.window.label}）` +
      ` · 上期：${r.meta.prev_window.since.slice(0, 10)} ~ ${r.meta.prev_window.until.slice(0, 10)}`
  );
  L.push(`> 口径：${r.meta.kpi_version}（platform/observability/KPI_DEFINITIONS.md）· 生成：${r.meta.generated_at} · 只读聚合`);
  L.push(
    `> 数据源：events=${r.sources.events.path}（${r.sources.events.available ? "可用" : `不可用：${r.sources.events.reason}`}）` +
      ` · ledger=${r.sources.ledger.path}（${r.sources.ledger.available ? "可用" : `不可用：${r.sources.ledger.reason}`}）`
  );
  if (r.notices.length) {
    L.push("");
    for (const n of r.notices) L.push(`> ⚠ ${n}`);
  }

  L.push("");
  L.push("## ① 全域漏斗");
  L.push("");
  L.push("| 阶段 | 本期 | 上期 | 环比 |");
  L.push("|---|---:|---:|---:|");
  L.push(`| 曝光 | — | — | — |`);
  L.push(cmpRow("线索（leads.first_seen）", r.funnel.leads));
  L.push(cmpRow("订单（orders.created_at）", r.funnel.orders));
  L.push(cmpRow("支付（orders.paid_at）", r.funnel.paid));
  if (r.funnel.paid_amounts) {
    L.push(`| 支付金额（按币种） | ${fmtAmounts(r.funnel.paid_amounts.cur)} | ${fmtAmounts(r.funnel.paid_amounts.prev)} | — |`);
  } else {
    L.push(`| 支付金额（按币种） | ${NA} | ${NA} | — |`);
  }
  L.push(cmpRow("激活（订单+授权合并去重）", r.funnel.activated));
  L.push(cmpRow("活跃主体（七产品 union）", r.funnel.active_subjects));
  L.push(cmpRow("续费（platform.license.renewed）", r.funnel.renewed));
  L.push("");
  L.push(`- 曝光口径：${r.funnel.exposure_note}`);
  L.push(`- 全域留存（次周仍活跃，KPI_DEFINITIONS §5）：${fmtRetention(r.funnel.retention)}`);
  if (r.funnel.activated_merged_licenses !== null && r.funnel.activated_merged_licenses !== undefined) {
    L.push(`- 激活合并：本期 ${r.funnel.activated_merged_licenses} 条授权与订单判为同一次交付，已去重（KPI_DEFINITIONS §3）`);
  }

  L.push("");
  L.push("## ② 分产品");
  for (const p of r.products) {
    L.push("");
    L.push(`### ${p.label} ${p.id}`);
    L.push("");
    L.push("| 指标 | 本期 | 上期 | 环比 |");
    L.push("|---|---:|---:|---:|");
    L.push(cmpRow("线索", p.funnel ? p.funnel.leads : null));
    L.push(cmpRow("订单", p.funnel ? p.funnel.orders : null));
    L.push(cmpRow("支付", p.funnel ? p.funnel.paid : null));
    L.push(cmpRow("激活", p.funnel ? p.funnel.activated : null));
    L.push(cmpRow("续费", p.renewed));
    L.push(cmpRow("事件量", p.activity ? p.activity.events : null));
    L.push(cmpRow("活跃日", p.activity ? p.activity.active_days : null));
    L.push(cmpRow("活跃主体", p.activity ? p.activity.active_subjects : null));
    L.push(
      p.north_star
        ? cmpRow(`北极星·${p.north_star.name}（${p.north_star.unit}）`, p.north_star)
        : `| 北极星 | ${NA} | ${NA} | — |`
    );
    const extras = [];
    if (p.funnel && Object.keys(p.funnel.paid_amounts_cur).length) {
      extras.push(`本期支付金额：${fmtAmounts(p.funnel.paid_amounts_cur)}`);
    }
    extras.push(`留存：${p.activity ? fmtRetention(p.activity.retention) : NA}`);
    L.push("");
    for (const e of extras) L.push(`- ${e}`);
  }
  const un = r.unattributed;
  const unParts = [];
  if (un.leads) unParts.push(`线索 ${un.leads}`);
  if (un.orders) unParts.push(`订单 ${un.orders}`);
  if (un.paid) unParts.push(`支付 ${un.paid}`);
  if (un.activated) unParts.push(`激活 ${un.activated}`);
  if (un.renewed) unParts.push(`续费 ${un.renewed}`);
  if (unParts.length) {
    L.push("");
    L.push(`> (未归属/其他) 桶（本期，不计入上述七产品分区，已计入全域总数）：${unParts.join("、")}`);
  }

  L.push("");
  L.push("## ③ 授权与到期");
  L.push("");
  if (r.licenses) {
    L.push(
      `- 本期新签授权：${r.licenses.new.cur}（上期 ${r.licenses.new.prev}，环比 ${fmtPct(r.licenses.new)}）；按产品：${byProductLine(r.licenses.new.by_product)}`
    );
    L.push(
      `- 30 天内到期（截至 ${r.licenses.expiring_30d.horizon.slice(0, 10)}，以窗口末端为基准，不含 revoked/expired）：` +
        `${r.licenses.expiring_30d.count}；按产品：${byProductLine(r.licenses.expiring_30d.by_product)}`
    );
  } else {
    L.push(`- ${NA}（账本库不可用，见页首提示）`);
  }

  L.push("");
  L.push("## ④ Top 事件（本期）");
  L.push("");
  if (r.top_events && r.top_events.length) {
    L.push("| # | 事件 | 产品 | 条数 |");
    L.push("|---:|---|---|---:|");
    r.top_events.forEach((e, i) => L.push(`| ${i + 1} | ${e.name} | ${e.product_id} | ${e.count} |`));
  } else if (r.top_events) {
    L.push(`- 本期无事件`);
  } else {
    L.push(`- ${NA}（事件库不可用，见页首提示）`);
  }

  L.push("");
  L.push("## ⑤ 数据健康");
  L.push("");
  const h = r.health;
  if (h.events_window) {
    L.push(`- 事件量：本期 ${h.events_window.cur}（上期 ${h.events_window.prev}，环比 ${fmtPct(h.events_window)}）`);
    L.push(
      `- 库累计：${h.db_totals.total} 条（未注册 ${h.db_totals.unregistered}），首条 ${h.db_totals.first_ts ?? "—"}，末条 ${h.db_totals.last_ts ?? "—"}`
    );
    L.push(
      `- 上报源（X-Event-Source，本期）：${h.sources.length ? h.sources.map((s) => `${s.source}=${s.count}`).join("、") : "无"}`
    );
    L.push(`- 未注册事件（本期，治理点名）：${h.unregistered_window}`);
    L.push(`- 匿名活跃事件（缺 workspace_id/customer_id，不计主体与留存）：${h.anonymous_active_events}`);
    L.push(`- 计量异常（props 解析失败/负值，按 0 计）：${h.metering_anomalies}`);
  } else {
    L.push(`- 事件侧：${NA}（事件库不可用）`);
  }
  if (h.ledger_gaps) {
    L.push(
      `- 归属缺失（本期）：订单缺产品 ${h.ledger_gaps.orders_no_product}、订单缺客户 ${h.ledger_gaps.orders_no_customer}、` +
        `授权缺客户 ${h.ledger_gaps.licenses_no_customer}、线索意向未映射 ${h.ledger_gaps.leads_unmapped_interest}`
    );
  } else {
    L.push(`- 账本侧：${NA}（账本库不可用）`);
  }
  if (h.zero_event_products.length) {
    for (const z of h.zero_event_products) {
      L.push(`- ${z.level === "alert" ? "⚠ 缺口告警" : "ℹ 提示"}［${z.label} ${z.product_id}］${z.message}`);
    }
  } else if (r.sources.events.available) {
    L.push("- 缺口告警：无（九个 product_id 本期均有事件）");
  }

  L.push("");
  return L.join("\n");
}

// ── 主流程 ──────────────────────────────────────────────────────────
function main() {
  const args = parseArgs(process.argv.slice(2));
  const win = resolveWindow(args, Date.now());

  const eventsPath = path.resolve(resolveEventsDbPath());
  const ledgerPath = path.resolve(resolveLedgerDbPath());

  // 事件侧
  const eOpen = openReadonly(eventsPath, ["events"]);
  let events = { path: eventsPath, available: false, reason: eOpen.reason, data: null };
  if (eOpen.db) {
    try {
      const total = eOpen.db.prepare("SELECT COUNT(*) AS c FROM events").get().c;
      if (total === 0) {
        events.reason = "events 表为空（尚无任何事件入库）";
      } else {
        const collected = collectEvents(eOpen.db, win);
        collected.dbTotalsOut = collected.dbTotals;
        events = { path: eventsPath, available: true, reason: null, data: collected };
      }
    } catch (e) {
      events.reason = `查询失败：${e.message}`;
    } finally {
      try { eOpen.db.close(); } catch { /* ignore */ }
    }
  }

  // 账本侧
  const lOpen = openReadonly(ledgerPath, ["orders", "leads", "licenses"]);
  let ledger = { path: ledgerPath, available: false, reason: lOpen.reason, data: null };
  if (lOpen.db) {
    try {
      const c = (t) => lOpen.db.prepare(`SELECT COUNT(*) AS c FROM ${t}`).get().c;
      if (c("orders") + c("leads") + c("licenses") === 0) {
        ledger.reason = "orders/leads/licenses 均为空（账本尚未回填）";
      } else {
        ledger = { path: ledgerPath, available: true, reason: null, data: collectLedger(lOpen.db, win) };
      }
    } catch (e) {
      ledger.reason = `查询失败：${e.message}`;
    }
    // 注意：licenseProductOf 是懒查询，构建报告期间连接须保持打开，报告完成后统一关闭
  }

  const report = buildReport(win, args, { events, ledger });
  if (lOpen.db) {
    try { lOpen.db.close(); } catch { /* ignore */ }
  }

  const text = args.format === "json" ? JSON.stringify(report, null, 2) + "\n" : renderMd(report);
  if (args.out) {
    const out = path.resolve(args.out);
    fs.mkdirSync(path.dirname(out), { recursive: true });
    fs.writeFileSync(out, text, "utf8");
    process.stderr.write(`已写入 ${out}\n`);
  } else {
    process.stdout.write(text);
  }
}

main();
