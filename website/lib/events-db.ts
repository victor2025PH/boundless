// 集团事件库（Group Events）— better-sqlite3 数据层。
//
// 定位：全域运营事件流的集团侧存储（platform/observability/EVENT_CONTRACT.md
// 传输层落地）。事件量远大于账本量，因此独立成库 group-events.db（env EVENTS_DB
// 可覆盖），不进 group-ledger.db —— 账本备份保持轻小。
// 写入方：/api/collect 收集器（uploader.py 批量补传）；消费方：/console/kpi 看板。
//
// 幂等（契约 §6）：event_id 为主键 + INSERT OR IGNORE，重放/补传同一事件只入库
// 一次，重复计入 ignoredDuplicates。信封校验以 EVENT_CONTRACT.md §2/§3 为唯一
// 真相：event_id 正则 / ts ISO8601 UTC 毫秒 / product_id 九枚举 / name 三段式且
// namespace === product_id / props 扁平标量对象且序列化 ≤ 8KB。
//
// 连接模式参照 lib/ledger.ts（WAL / busy_timeout 5000 / globalThis 单例 / meta
// 版本迁移），但完全独立实现——不与账本共享连接、DDL 或任何代码。

import fs from "fs";
import path from "path";
import Database from "better-sqlite3";
import { DATA_DIR } from "./data-dir";

export const EVENTS_SCHEMA_VERSION = 1;

// ── 契约常量（与 EVENT_CONTRACT.md / events_registry.json 一致，改动即破坏契约）──
export const EVENT_PRODUCT_IDS = [
  "zhituo",
  "zhiliao",
  "tongyi",
  "tongchuan",
  "huansheng",
  "huanying",
  "huanyan",
  "website",
  "platform",
] as const;
export type EventProductId = (typeof EVENT_PRODUCT_IDS)[number];
const PRODUCT_ID_SET: ReadonlySet<string> = new Set(EVENT_PRODUCT_IDS);

export const EVENT_ID_RE = /^evt_[0-9A-HJKMNP-TV-Z]{26}$/;
export const EVENT_NAME_RE = /^[a-z0-9_]+\.[a-z0-9_]+\.[a-z0-9_]+$/;
// ts：ISO8601 UTC 毫秒精度、Z 结尾（契约 §2，与 emitter.py 的 _TS_RE 同款）
const TS_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/;
export const PROPS_MAX_BYTES = 8 * 1024;

// ── 表结构（schema v1）──────────────────────────────────────────────
const DDL_V1 = `
CREATE TABLE IF NOT EXISTS events (
  event_id TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  product_id TEXT NOT NULL,
  name TEXT NOT NULL,
  workspace_id TEXT,
  customer_id TEXT,
  actor TEXT,
  props TEXT,
  unregistered INTEGER NOT NULL DEFAULT 0,
  received_at TEXT NOT NULL,
  source TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_product_ts ON events(product_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_name_ts ON events(name, ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
`;

// ── 行类型 ──────────────────────────────────────────────────────────
export interface EventRow {
  event_id: string;
  ts: string;
  product_id: string;
  name: string;
  workspace_id: string | null;
  customer_id: string | null;
  actor: string | null;
  /** 扁平 props 的 JSON 文本（入库时重新序列化）。 */
  props: string;
  /** 1 = 信封带 _unregistered: true（未注册事件，治理点名用）。 */
  unregistered: number;
  received_at: string;
  source: string | null;
}

// ── 连接单例（globalThis 缓存，防 Next dev 热重载重复打开）──────────
type GlobalWithEvents = typeof globalThis & { __groupEventsConns?: Map<string, Database.Database> };

/** 事件库文件路径：env EVENTS_DB 可覆盖，默认 DATA_DIR/group-events.db。 */
export function resolveEventsDbPath(): string {
  return process.env.EVENTS_DB || path.join(DATA_DIR, "group-events.db");
}

/** 打开（或复用）事件库连接：WAL、busy_timeout 5000，首次打开自动建表+迁移。 */
export function getEventsDb(dbPath?: string): Database.Database {
  const file = path.resolve(dbPath || resolveEventsDbPath());
  const g = globalThis as GlobalWithEvents;
  if (!g.__groupEventsConns) g.__groupEventsConns = new Map();
  const cached = g.__groupEventsConns.get(file);
  if (cached && cached.open) return cached;
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const db = new Database(file);
  db.pragma("journal_mode = WAL");
  db.pragma("busy_timeout = 5000");
  db.pragma("synchronous = NORMAL");
  migrate(db);
  g.__groupEventsConns.set(file, db);
  return db;
}

function migrate(db: Database.Database) {
  db.exec("CREATE TABLE IF NOT EXISTS meta (\n  key TEXT PRIMARY KEY,\n  value TEXT\n);");
  const row = db.prepare("SELECT value FROM meta WHERE key = 'schema_version'").get() as
    | { value: string }
    | undefined;
  const current = row ? Number(row.value) || 0 : 0;
  const migrations: ((db: Database.Database) => void)[] = [
    (d) => d.exec(DDL_V1), // v0 → v1：全量建表（IF NOT EXISTS，幂等）
  ];
  if (current >= migrations.length) return;
  const run = db.transaction(() => {
    for (let i = current; i < migrations.length; i++) migrations[i](db);
    db.prepare(
      "INSERT INTO meta (key, value) VALUES ('schema_version', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    ).run(String(migrations.length));
  });
  run.immediate();
}

// ── 信封校验 ────────────────────────────────────────────────────────
type ValidatedEnvelope = {
  event_id: string;
  ts: string;
  product_id: string;
  name: string;
  workspace_id: string | null;
  customer_id: string | null;
  actor: string | null;
  props: string;
  unregistered: number;
};

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** 校验一条上报信封；合法返回入库行（不含 received_at/source），非法返回错误原因字符串。
 *  信封外的未知字段按契约 §2 容忍不报错（只取已知列）。 */
function validateEnvelope(raw: unknown): ValidatedEnvelope | string {
  if (!isPlainObject(raw)) return `信封必须是 JSON 对象，得到 ${raw === null ? "null" : typeof raw}`;
  const eid = raw.event_id;
  if (typeof eid !== "string" || !EVENT_ID_RE.test(eid)) {
    return `event_id 不符合 ${EVENT_ID_RE.source}: ${JSON.stringify(eid ?? null)}`;
  }
  const ts = raw.ts;
  if (typeof ts !== "string" || !TS_RE.test(ts) || !Number.isFinite(Date.parse(ts))) {
    return `ts 不是 ISO8601 UTC 毫秒格式（如 2026-07-18T04:00:00.123Z）: ${JSON.stringify(ts ?? null)}`;
  }
  const pid = raw.product_id;
  if (typeof pid !== "string" || !PRODUCT_ID_SET.has(pid)) {
    return `product_id 不在枚举内: ${JSON.stringify(pid ?? null)}`;
  }
  const name = raw.name;
  if (typeof name !== "string" || !EVENT_NAME_RE.test(name)) {
    return `name 不符合三段式 ${EVENT_NAME_RE.source}: ${JSON.stringify(name ?? null)}`;
  }
  if (name.split(".", 1)[0] !== pid) {
    return `namespace 必须等于 product_id: ${JSON.stringify(name)} vs ${JSON.stringify(pid)}`;
  }
  for (const key of ["workspace_id", "customer_id", "actor"] as const) {
    const v = raw[key];
    if (v !== undefined && v !== null && typeof v !== "string") {
      return `${key} 必须是字符串: ${JSON.stringify(v)}`;
    }
  }
  // props：缺省空对象；必须是扁平对象、值只允许标量（契约 §2），序列化后 ≤ 8KB
  const props = raw.props ?? {};
  if (!isPlainObject(props)) return `props 必须是 JSON 对象: ${JSON.stringify(props)}`;
  for (const [k, v] of Object.entries(props)) {
    if (v === null) continue;
    const t = typeof v;
    if (t !== "string" && t !== "number" && t !== "boolean") {
      return `props[${JSON.stringify(k)}] 必须是标量（string/number/boolean/null），得到 ${Array.isArray(v) ? "array" : t}`;
    }
    if (t === "number" && !Number.isFinite(v)) return `props[${JSON.stringify(k)}] 数值非有限`;
  }
  const propsJson = JSON.stringify(props);
  if (Buffer.byteLength(propsJson, "utf8") > PROPS_MAX_BYTES) {
    return `props 序列化后超过 ${PROPS_MAX_BYTES} 字节`;
  }
  return {
    event_id: eid,
    ts,
    product_id: pid,
    name,
    workspace_id: typeof raw.workspace_id === "string" ? raw.workspace_id : null,
    customer_id: typeof raw.customer_id === "string" ? raw.customer_id : null,
    actor: typeof raw.actor === "string" ? raw.actor : null,
    props: propsJson,
    unregistered: raw._unregistered === true ? 1 : 0,
  };
}

// ── 批量写入（单事务 + INSERT OR IGNORE 幂等）───────────────────────
export interface InsertRejection {
  index: number;
  reason: string;
}

export interface InsertEventsResult {
  /** 新入库条数。 */
  accepted: number;
  /** event_id 已存在（含批内重复）被幂等忽略的条数。 */
  ignoredDuplicates: number;
  /** 信封校验失败的条目（index 为批内下标）。 */
  rejected: InsertRejection[];
}

/** 批量入库：整批单事务；逐条校验，非法进 rejected，合法 INSERT OR IGNORE。 */
export function insertEvents(
  envelopes: unknown[],
  source: string,
  db: Database.Database = getEventsDb()
): InsertEventsResult {
  const receivedAt = new Date().toISOString();
  const src = source && source.trim() ? source.trim().slice(0, 120) : "unknown";
  const stmt = db.prepare(
    `INSERT OR IGNORE INTO events
       (event_id, ts, product_id, name, workspace_id, customer_id, actor, props, unregistered, received_at, source)
     VALUES (@event_id, @ts, @product_id, @name, @workspace_id, @customer_id, @actor, @props, @unregistered, @received_at, @source)`
  );
  const tx = db.transaction((): InsertEventsResult => {
    const result: InsertEventsResult = { accepted: 0, ignoredDuplicates: 0, rejected: [] };
    for (let i = 0; i < envelopes.length; i++) {
      const v = validateEnvelope(envelopes[i]);
      if (typeof v === "string") {
        result.rejected.push({ index: i, reason: v });
        continue;
      }
      const info = stmt.run({ ...v, received_at: receivedAt, source: src });
      if (info.changes > 0) result.accepted++;
      else result.ignoredDuplicates++;
    }
    return result;
  });
  return tx();
}

// ── 查询（/console/kpi 消费）────────────────────────────────────────
/** 近 N 天窗口起点：(N-1) 天前的 UTC 零点 ISO 串（含今天共 N 个 UTC 日）。
 *  库内 ts 格式统一（入库校验保证），字符串比较即时间比较。 */
function sinceIso(days: number): string {
  const d = Math.min(Math.max(1, Math.trunc(days)), 3650);
  const now = new Date();
  const todayStart = Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate());
  return new Date(todayStart - (d - 1) * 86400000).toISOString();
}

export interface ProductDayCount {
  /** UTC 日期 YYYY-MM-DD（取自事件 ts）。 */
  day: string;
  product_id: string;
  count: number;
}

/** 近 N 天（UTC 日，含今天）按 (日, 产品) 的事件量。 */
export function countsByProductDay(days: number, db: Database.Database = getEventsDb()): ProductDayCount[] {
  return db
    .prepare(
      `SELECT substr(ts, 1, 10) AS day, product_id, COUNT(*) AS count
       FROM events WHERE ts >= ?
       GROUP BY day, product_id
       ORDER BY day ASC, product_id ASC`
    )
    .all(sinceIso(days)) as ProductDayCount[];
}

export interface NameCount {
  name: string;
  product_id: string;
  count: number;
}

/** 近 N 天 Top 事件名（按量降序）。 */
export function countsByNameTop(days: number, limit = 10, db: Database.Database = getEventsDb()): NameCount[] {
  const n = Math.min(Math.max(1, Math.trunc(limit)), 100);
  return db
    .prepare(
      `SELECT name, product_id, COUNT(*) AS count
       FROM events WHERE ts >= ?
       GROUP BY name, product_id
       ORDER BY count DESC, name ASC
       LIMIT ?`
    )
    .all(sinceIso(days), n) as NameCount[];
}

export interface RecentEventsFilter {
  productId?: string;
  name?: string;
  limit?: number;
}

/** 最近事件（ts 降序），可按产品/事件名过滤，limit 默认 20、上限 200。 */
export function recentEvents(filter: RecentEventsFilter = {}, db: Database.Database = getEventsDb()): EventRow[] {
  const where: string[] = [];
  const params: Record<string, unknown> = {};
  if (filter.productId && filter.productId.trim()) {
    where.push("product_id = @productId");
    params.productId = filter.productId.trim();
  }
  if (filter.name && filter.name.trim()) {
    where.push("name = @name");
    params.name = filter.name.trim();
  }
  const cond = where.length ? ` WHERE ${where.join(" AND ")}` : "";
  params.limit = Math.min(Math.max(1, Math.trunc(filter.limit ?? 20)), 200);
  return db
    .prepare(`SELECT * FROM events${cond} ORDER BY ts DESC, event_id DESC LIMIT @limit`)
    .all(params) as EventRow[];
}

export interface EventsTotals {
  total: number;
  /** 未注册事件条数（unregistered = 1）。 */
  unregistered: number;
  byProduct: Record<string, number>;
  firstTs: string | null;
  lastTs: string | null;
}

/** 全库统计：总量、未注册量、按产品分布、首末事件时间。 */
export function totals(db: Database.Database = getEventsDb()): EventsTotals {
  const agg = db
    .prepare(
      "SELECT COUNT(*) AS total, COALESCE(SUM(unregistered), 0) AS unregistered, MIN(ts) AS firstTs, MAX(ts) AS lastTs FROM events"
    )
    .get() as { total: number; unregistered: number; firstTs: string | null; lastTs: string | null };
  const byProduct: Record<string, number> = {};
  for (const r of db
    .prepare("SELECT product_id, COUNT(*) AS c FROM events GROUP BY product_id")
    .all() as { product_id: string; c: number }[]) {
    byProduct[r.product_id] = r.c;
  }
  return {
    total: agg.total,
    unregistered: agg.unregistered,
    byProduct,
    firstTs: agg.firstTs,
    lastTs: agg.lastTs,
  };
}
