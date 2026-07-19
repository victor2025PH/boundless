// 集团账本（Group Ledger）— better-sqlite3 数据层。
//
// 定位：影子账本。现有 JSON 存储（order-store / lead-store）仍是业务主真相源，
// 账本只做双写镜像 + 幂等回填，供 /console 集团后台读取；切主留到以后阶段。
// 因此所有写入方（ledger-sync.ts 的钩子）必须 best-effort：本层可以 throw（例如
// 参数缺失），但调用方负责吞掉，绝不影响下单/留资主链路。
//
// ⚠️ DDL / upsert 语义双份维护：scripts/ledger-lib.mjs 有一份纯 JS 等价实现
// （CLI 回填/导入脚本用，不经 TS 编译）。修改本文件的表结构或 upsert 语义时，
// 必须同步修改 scripts/ledger-lib.mjs，两处 DDL 文本保持逐字一致！
//
// 设计约束（为将来平滑迁 PostgreSQL）：TEXT 主键、ISO8601 时间字符串、raw 存
// JSON 文本、不用 SQLite 特有类型；仅 identities.id 用 INTEGER AUTOINCREMENT。

import fs from "fs";
import path from "path";
import Database from "better-sqlite3";
import { DATA_DIR } from "./data-dir";
import { isValidId, newId } from "./ids";

export const LEDGER_SCHEMA_VERSION = 6;

// ── 表结构（schema v1）──────────────────────────────────────────────
// ⚠️ 与 scripts/ledger-lib.mjs 中的 DDL_V1 逐字一致，修改必须同步！
const DDL_V1 = `
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS customers (
  id TEXT PRIMARY KEY,
  display_name TEXT,
  primary_contact TEXT,
  tg_user_id TEXT,
  source TEXT,
  notes TEXT,
  created_at TEXT,
  updated_at TEXT
);
CREATE TABLE IF NOT EXISTS identities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id TEXT NOT NULL REFERENCES customers(id),
  kind TEXT NOT NULL CHECK(kind IN ('contact','tg','email','phone','fingerprint')),
  value TEXT NOT NULL,
  created_at TEXT,
  UNIQUE(kind, value)
);
CREATE TABLE IF NOT EXISTS leads (
  source_key TEXT PRIMARY KEY,
  customer_id TEXT,
  name TEXT,
  contact TEXT,
  interest TEXT,
  message TEXT,
  lang TEXT,
  source TEXT,
  utm TEXT,
  status TEXT,
  first_seen TEXT,
  last_seen TEXT,
  count INTEGER,
  raw TEXT,
  synced_at TEXT
);
CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY,
  source_key TEXT UNIQUE NOT NULL,
  customer_id TEXT,
  product_id TEXT,
  sku_id TEXT,
  plan TEXT,
  edition TEXT,
  period TEXT,
  amount REAL,
  pay_amount REAL,
  currency TEXT,
  status TEXT,
  contact TEXT,
  fingerprint TEXT,
  lang TEXT,
  created_at TEXT,
  paid_at TEXT,
  activated_at TEXT,
  notify_chat TEXT,
  code TEXT,
  raw TEXT,
  synced_at TEXT
);
CREATE TABLE IF NOT EXISTS licenses (
  id TEXT PRIMARY KEY,
  source_system TEXT NOT NULL,
  source_key TEXT NOT NULL,
  customer_id TEXT,
  product_id TEXT,
  sku_id TEXT,
  plan TEXT,
  edition TEXT,
  seats INTEGER,
  machine_fingerprint TEXT,
  issued_at TEXT,
  expires_at TEXT,
  status TEXT,
  raw TEXT,
  synced_at TEXT,
  UNIQUE(source_system, source_key)
);
CREATE TABLE IF NOT EXISTS audit (
  id TEXT PRIMARY KEY,
  ts TEXT,
  actor TEXT,
  action TEXT,
  entity TEXT,
  entity_id TEXT,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS idx_orders_customer ON orders(customer_id);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_leads_customer ON leads(customer_id);
CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
CREATE INDEX IF NOT EXISTS idx_licenses_customer ON licenses(customer_id);
CREATE INDEX IF NOT EXISTS idx_licenses_expires ON licenses(expires_at);
CREATE INDEX IF NOT EXISTS idx_identities_customer ON identities(customer_id);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit(entity, entity_id);
`;

// ── 表结构（schema v2：控制台实名账号 + 会话）───────────────────────
// ⚠️ 与 scripts/ledger-lib.mjs 中的 DDL_V2 逐字一致，修改必须同步！
const DDL_V2 = `
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  pw_salt TEXT NOT NULL,
  pw_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('master','admin','viewer')),
  display_name TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at TEXT,
  last_login TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id),
  created_at TEXT,
  last_seen TEXT,
  expires_at TEXT,
  revoked INTEGER NOT NULL DEFAULT 0,
  ip TEXT,
  ua TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
`;

// ── 表结构（schema v3：人设总线 Persona Bus）────────────────────────
// 人设注册表只存元数据与指纹（slots_detail JSON），资产本体（脸模/声纹/话术/知识库
// 文件）永不进集团库。purge 协议：console 发起 → persona_purges 逐引擎下发 →
// /api/sync/personas/purges 机器通道轮询+ack → 全部 ack 后 status=purged。
// ⚠️ 与 scripts/ledger-lib.mjs 中的 DDL_V3 逐字一致，修改必须同步！
const DDL_V3 = `
CREATE TABLE IF NOT EXISTS personas (
  id TEXT PRIMARY KEY,
  customer_id TEXT NULL REFERENCES customers(id),
  source_system TEXT NOT NULL,
  source_key TEXT NOT NULL,
  display_name TEXT,
  slot_face INTEGER NOT NULL DEFAULT 0,
  slot_voice INTEGER NOT NULL DEFAULT 0,
  slot_prompt INTEGER NOT NULL DEFAULT 0,
  slot_knowledge INTEGER NOT NULL DEFAULT 0,
  slots_detail TEXT,
  tags TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived','purge_pending','purged')),
  created_at TEXT,
  updated_at TEXT,
  synced_at TEXT,
  UNIQUE(source_system, source_key)
);
CREATE TABLE IF NOT EXISTS persona_grants (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  persona_id TEXT NOT NULL REFERENCES personas(id),
  product_id TEXT NOT NULL,
  scope TEXT,
  granted_by TEXT,
  granted_at TEXT,
  revoked_at TEXT NULL,
  UNIQUE(persona_id, product_id)
);
CREATE TABLE IF NOT EXISTS persona_purges (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  persona_id TEXT NOT NULL,
  requested_by TEXT,
  requested_at TEXT,
  target_system TEXT NOT NULL,
  acked_at TEXT NULL,
  ack_detail TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_personas_customer ON personas(customer_id);
CREATE INDEX IF NOT EXISTS idx_personas_status ON personas(status);
CREATE INDEX IF NOT EXISTS idx_persona_grants_persona ON persona_grants(persona_id);
CREATE INDEX IF NOT EXISTS idx_persona_purges_target ON persona_purges(target_system, acked_at);
`;

// ── 表结构（schema v4：跨售商机跟进 opportunities_log）──────────────
// 商机本体仍由 lib/opportunities.ts 规则引擎从账本只读推导（不落库）；本表只落
// 销售跟进动作：opp_key 为可再生的商机指纹（kind|customerId|toProduct，续费类
// 再拼 license id，见 lib/opportunities.ts::oppKey），note 只存运营备注。
// ⚠️ 与 scripts/ledger-lib.mjs 中的 DDL_V4 逐字一致，修改必须同步！
const DDL_V4 = `
CREATE TABLE IF NOT EXISTS opportunities_log (
  id TEXT PRIMARY KEY,
  opp_key TEXT UNIQUE NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('persona_cross_sell','product_gap_cross_sell','expiring_renewal')),
  customer_id TEXT NOT NULL REFERENCES customers(id),
  to_product TEXT,
  status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','contacted','won','dismissed')),
  note TEXT,
  acted_by TEXT, acted_at TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_opplog_customer ON opportunities_log(customer_id);
CREATE INDEX IF NOT EXISTS idx_opplog_status ON opportunities_log(status);
`;

// ── 表结构（schema v5：渠道账号台账 channel_accounts）───────────────
// 多平台对外账号登记：哪个号（label/handle）- 哪个平台 - 挂哪个实例 - 什么用途 -
// 谁保管（渠道账号架构 2026-07 纪律三条之 3）。session_ref 只存登录态文件/凭据
// 位置的纯文本备注（如 sessions/639952947442.session），绝不存任何密钥或凭据本体。
// CRUD 在 lib/channels.ts（同 v3 personas / v4 opportunities 的分层惯例）。
// ⚠️ 与 scripts/ledger-lib.mjs 中的 DDL_V5 逐字一致，修改必须同步！
const DDL_V5 = `
CREATE TABLE IF NOT EXISTS channel_accounts (
  id TEXT PRIMARY KEY,
  platform TEXT NOT NULL CHECK(platform IN ('telegram','whatsapp','messenger','line','web','other')),
  label TEXT NOT NULL,
  handle TEXT,
  instance TEXT NOT NULL DEFAULT 'none' CHECK(instance IN ('zhiliao','tongyi','avatarhub','huoke','website','none')),
  purpose TEXT NOT NULL DEFAULT '其他' CHECK(purpose IN ('总机接待','交付服务','测试','投放专号','其他')),
  holder TEXT,
  status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused','revoked','pending')),
  session_ref TEXT,
  notes TEXT,
  created_at TEXT,
  updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_channel_accounts_platform ON channel_accounts(platform);
CREATE INDEX IF NOT EXISTS idx_channel_accounts_status ON channel_accounts(status);
`;

// ── 行类型（console 直接消费）───────────────────────────────────────
export interface CustomerRow {
  id: string;
  display_name: string | null;
  primary_contact: string | null;
  tg_user_id: string | null;
  source: string | null;
  notes: string | null;
  created_at: string | null;
  updated_at: string | null;
  /** 1 = 测试/演练数据（e2e/smoke 等，schema v6）：KPI/商机默认排除，console 显示徽章。 */
  is_test: number;
}

export const IDENTITY_KINDS = ["contact", "tg", "email", "phone", "fingerprint"] as const;
export type IdentityKind = (typeof IDENTITY_KINDS)[number];

export interface IdentityRow {
  id: number;
  customer_id: string;
  kind: IdentityKind;
  value: string;
  created_at: string | null;
}

export interface LeadRow {
  source_key: string;
  customer_id: string | null;
  name: string | null;
  contact: string | null;
  interest: string | null;
  message: string | null;
  lang: string | null;
  source: string | null;
  utm: string | null;
  status: string | null;
  first_seen: string | null;
  last_seen: string | null;
  count: number | null;
  raw: string | null;
  synced_at: string | null;
  is_test?: number | null;
}

export interface OrderRow {
  id: string;
  source_key: string;
  customer_id: string | null;
  product_id: string | null;
  sku_id: string | null;
  plan: string | null;
  edition: string | null;
  period: string | null;
  amount: number | null;
  pay_amount: number | null;
  currency: string | null;
  status: string | null;
  contact: string | null;
  fingerprint: string | null;
  lang: string | null;
  created_at: string | null;
  paid_at: string | null;
  activated_at: string | null;
  notify_chat: string | null;
  code: string | null;
  raw: string | null;
  synced_at: string | null;
  is_test?: number | null;
}

export interface LicenseRow {
  id: string;
  source_system: string;
  source_key: string;
  customer_id: string | null;
  product_id: string | null;
  sku_id: string | null;
  plan: string | null;
  edition: string | null;
  seats: number | null;
  machine_fingerprint: string | null;
  issued_at: string | null;
  expires_at: string | null;
  status: string | null;
  raw: string | null;
  synced_at: string | null;
  is_test?: number | null;
}

export interface AuditRow {
  id: string;
  ts: string | null;
  actor: string | null;
  action: string | null;
  entity: string | null;
  entity_id: string | null;
  detail: string | null;
}

/** upsert 入参：自然键必填，其余可缺（缺 → NULL）。 */
export type LeadRowInput = Partial<Omit<LeadRow, "source_key" | "synced_at">> & { source_key: string };
export type OrderRowInput = Partial<Omit<OrderRow, "source_key" | "synced_at">> & { source_key: string };
export type LicenseRowInput = Partial<Omit<LicenseRow, "source_system" | "source_key" | "synced_at">> & {
  source_system: string;
  source_key: string;
};

// ── 产品映射 ────────────────────────────────────────────────────────
export const PRODUCT_IDS = [
  "zhituo", // 智拓 ReachX
  "zhiliao", // 智聊 ChatX
  "tongyi", // 通译 LingoX
  "tongchuan", // 通传 VoxX
  "huansheng", // 幻声 VoiceX
  "huanying", // 幻影 LiveX
  "huanyan", // 幻颜 FaceX
  "website", // 官网服务类
] as const;
export type ProductId = (typeof PRODUCT_IDS)[number];

/** 已知 SKU / plan 标识 → 产品的精确映射（brand.ts skuIds、pricing.ts offer id）。 */
const PRODUCT_EXACT: Record<string, ProductId> = {
  voice: "huansheng",
  faceswap: "huanyan",
  "digital-human": "huanying",
  "video-dubbing": "huanying",
  translate: "tongyi",
};

/** plan/edition → product_id 的宽松推断（纯函数）。
 *  映射不了（如 AvatarHub 会员档 trial/starter/standard/pro/flagship 属整机引擎，
 *  跨幻声/幻颜/幻影多产品）一律返回 null，宁缺毋错。
 *  ⚠️ 与 scripts/ledger-lib.mjs 的 inferProductId 逻辑一致，修改必须同步！ */
export function inferProductId(plan?: string | null, edition?: string | null): ProductId | null {
  const p = (plan ?? "").trim().toLowerCase();
  if (p && PRODUCT_EXACT[p]) return PRODUCT_EXACT[p];
  const hay = `${p} ${(edition ?? "").trim().toLowerCase()}`;
  if (!hay.trim()) return null;
  const rules: [RegExp, ProductId][] = [
    [/zhituo|reachx|growthreach|智拓/, "zhituo"],
    [/zhiliao|chatx|autochat|chathub|智聊/, "zhiliao"],
    [/tongyi|lingox|livelingo|translate|通译/, "tongyi"],
    [/tongchuan|voxx|interpret|通传/, "tongchuan"],
    [/huansheng|voicex|voice-?clone|幻声/, "huansheng"],
    [/huanying|livex|livemorph|digital-?human|video-?dubbing|幻影/, "huanying"],
    [/huanyan|facex|face-?swap|幻颜/, "huanyan"],
    [/website|官网/, "website"],
  ];
  for (const [re, id] of rules) if (re.test(hay)) return id;
  return null;
}

// ── 连接单例（globalThis 缓存，防 Next dev 热重载重复打开）──────────
type GlobalWithLedger = typeof globalThis & { __groupLedgerConns?: Map<string, Database.Database> };

/** 账本 DB 文件路径：env LEDGER_DB 可覆盖，默认 DATA_DIR/group-ledger.db。 */
export function resolveLedgerDbPath(): string {
  return process.env.LEDGER_DB || path.join(DATA_DIR, "group-ledger.db");
}

/** 打开（或复用）账本连接：WAL、busy_timeout 5000，首次打开自动建表+迁移。 */
export function getLedgerDb(dbPath?: string): Database.Database {
  const file = path.resolve(dbPath || resolveLedgerDbPath());
  const g = globalThis as GlobalWithLedger;
  if (!g.__groupLedgerConns) g.__groupLedgerConns = new Map();
  const cached = g.__groupLedgerConns.get(file);
  if (cached && cached.open) return cached;
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const db = new Database(file);
  db.pragma("journal_mode = WAL");
  db.pragma("busy_timeout = 5000");
  db.pragma("synchronous = NORMAL");
  db.pragma("foreign_keys = ON");
  migrate(db);
  g.__groupLedgerConns.set(file, db);
  return db;
}

// v5 → v6：为 customers/orders/leads/licenses 补 is_test 列（测试/演练数据标记）。
// 用函数式迁移而非裸 ALTER：生产库可能已被 scripts/ledger-mark-testdata.mjs 抢先加过该列，
// 故先探 PRAGMA 存在性再 ALTER，重复运行幂等（裸 ALTER 会因 duplicate column 报错）。
// ⚠️ 与 scripts/ledger-lib.mjs 的 migrateV6 逐字一致，修改必须同步！
function migrateV6(d: Database.Database) {
  const hasCol = (t: string, c: string) =>
    (d.prepare(`PRAGMA table_info(${t})`).all() as { name: string }[]).some((x) => x.name === c);
  for (const t of ["customers", "orders", "leads", "licenses"]) {
    if (!hasCol(t, "is_test")) d.exec(`ALTER TABLE ${t} ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0`);
  }
  d.exec("CREATE INDEX IF NOT EXISTS idx_orders_is_test ON orders(is_test)");
}

function migrate(db: Database.Database) {
  db.exec("CREATE TABLE IF NOT EXISTS meta (\n  key TEXT PRIMARY KEY,\n  value TEXT\n);");
  const row = db.prepare("SELECT value FROM meta WHERE key = 'schema_version'").get() as
    | { value: string }
    | undefined;
  const current = row ? Number(row.value) || 0 : 0;
  const migrations: ((db: Database.Database) => void)[] = [
    (d) => d.exec(DDL_V1), // v0 → v1：全量建表（IF NOT EXISTS，幂等）
    (d) => d.exec(DDL_V2), // v1 → v2：控制台实名账号 users + 会话 sessions
    (d) => d.exec(DDL_V3), // v2 → v3：人设总线 personas / persona_grants / persona_purges
    (d) => d.exec(DDL_V4), // v3 → v4：跨售商机跟进 opportunities_log
    (d) => d.exec(DDL_V5), // v4 → v5：渠道账号台账 channel_accounts
    migrateV6, // v5 → v6：is_test 标记列（测试/演练数据，KPI/商机默认排除）
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

// ── 取值规整（undefined/空串 → NULL；better-sqlite3 不接受 undefined）─
function s(v: unknown): string | null {
  if (v === undefined || v === null) return null;
  const t = String(v).trim();
  return t === "" ? null : t;
}
function num(v: unknown): number | null {
  if (v === undefined || v === null || v === "") return null;
  const x = typeof v === "number" ? v : Number(v);
  return Number.isFinite(x) ? x : null;
}
function int(v: unknown): number | null {
  const x = num(v);
  return x === null ? null : Math.trunc(x);
}
const nowIso = () => new Date().toISOString();

/** 身份值归一化：contact/email 小写去空白、phone 去分隔符——存取两侧都走这里，保证匹配一致。 */
export function normIdentityValue(kind: IdentityKind, value: string): string {
  const v = value.trim();
  if (kind === "contact" || kind === "email") return v.toLowerCase().replace(/\s+/g, "");
  if (kind === "phone") return v.replace(/[\s\-()]/g, "");
  return v;
}

// ── 测试/演练数据判定（schema v6 is_test 的唯一口径）────────────────
// 保守词边界匹配：e2e / test / drill / smoke 必须前后都是非字母数字（"contest"、
// "latest"、"testuser" 不命中，宁可漏标不误标——误标会把真实数据从 KPI 滤掉）；
// @internal 只认结尾（e2e 脚本约定 contact 形如 e2e-notify@internal）。
// ⚠️ 与 scripts/ledger-lib.mjs 的 isTestSignal 逐字一致，修改必须同步！
const TEST_SIGNAL_RE = /(^|[^a-z0-9])(e2e|test|drill|smoke)([^a-z0-9]|$)|@internal\s*$/i;

/** 任一入参命中测试信号即 true（null/undefined 跳过）。 */
export function isTestSignal(...vals: (string | null | undefined)[]): boolean {
  return vals.some((v) => v !== null && v !== undefined && TEST_SIGNAL_RE.test(String(v)));
}

/** 联系方式强信号分类（与 scripts/ledger-lib.mjs 的 classifyStrongContact 同规则，修改必须同步）：
 *  仅 @handle / t.me/handle / email / phone 判为可自动建档的强信号；自由文本不建档。
 *  返回 { kind, value（已归一）, display }，用于实时归并的 create-on-miss。 */
export function classifyStrongContact(
  text: string | null | undefined
): { kind: IdentityKind; value: string; display: string } | null {
  const t = String(text ?? "").trim();
  if (!t) return null;
  if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(t)) return { kind: "email", value: t.toLowerCase(), display: t.toLowerCase() };
  const h =
    t.match(/^@([A-Za-z0-9_]{3,32})$/) ||
    t.match(/^(?:https?:\/\/)?(?:www\.)?t(?:elegram)?\.me\/@?([A-Za-z0-9_]{3,32})\/?$/i);
  if (h) return { kind: "contact", value: h[1].toLowerCase(), display: `@${h[1].toLowerCase()}` };
  const d = t.replace(/[\s\-()]/g, "");
  if (/^\+?\d{7,15}$/.test(d)) return { kind: "phone", value: d, display: t };
  return null;
}

// ── 幂等 upsert（自然键；已有 customer_id 关联绝不被覆盖）───────────
export interface UpsertResult {
  id: string;
  inserted: boolean;
}

/** 订单 upsert，自然键 source_key（旧订单号 AH-…）。
 *  更新时：customer_id 保留已有值；product_id/sku_id 传 NULL 不清空已有值（console 可能手工修正过）；
 *  其余镜像字段以来件为准全量覆盖（JSON 主真相源）。 */
export function upsertOrderRow(row: OrderRowInput, db: Database.Database = getLedgerDb()): UpsertResult {
  const sourceKey = s(row.source_key);
  if (!sourceKey) throw new TypeError("upsertOrderRow: source_key required");
  const p = {
    source_key: sourceKey,
    customer_id: s(row.customer_id),
    product_id: s(row.product_id),
    sku_id: s(row.sku_id),
    plan: s(row.plan),
    edition: s(row.edition),
    period: s(row.period),
    amount: num(row.amount),
    pay_amount: num(row.pay_amount),
    currency: s(row.currency),
    status: s(row.status),
    contact: s(row.contact),
    fingerprint: s(row.fingerprint),
    lang: s(row.lang),
    created_at: s(row.created_at),
    paid_at: s(row.paid_at),
    activated_at: s(row.activated_at),
    notify_chat: s(row.notify_chat),
    code: s(row.code),
    raw: s(row.raw),
    synced_at: nowIso(),
  };
  const tx = db.transaction((): UpsertResult => {
    const existing = db.prepare("SELECT id FROM orders WHERE source_key = ?").get(sourceKey) as
      | { id: string }
      | undefined;
    if (!existing) {
      const id = row.id && isValidId(row.id, "ord") ? row.id : newId("ord");
      db.prepare(
        `INSERT INTO orders (id, source_key, customer_id, product_id, sku_id, plan, edition, period, amount, pay_amount, currency, status, contact, fingerprint, lang, created_at, paid_at, activated_at, notify_chat, code, raw, synced_at)
         VALUES (@id, @source_key, @customer_id, @product_id, @sku_id, @plan, @edition, @period, @amount, @pay_amount, @currency, @status, @contact, @fingerprint, @lang, @created_at, @paid_at, @activated_at, @notify_chat, @code, @raw, @synced_at)`
      ).run({ ...p, id });
      return { id, inserted: true };
    }
    db.prepare(
      `UPDATE orders SET
         customer_id = COALESCE(customer_id, @customer_id),
         product_id = COALESCE(@product_id, product_id),
         sku_id = COALESCE(@sku_id, sku_id),
         plan = @plan, edition = @edition, period = @period,
         amount = @amount, pay_amount = @pay_amount, currency = @currency,
         status = @status, contact = @contact, fingerprint = @fingerprint, lang = @lang,
         created_at = @created_at, paid_at = @paid_at, activated_at = @activated_at,
         notify_chat = @notify_chat, code = @code, raw = @raw, synced_at = @synced_at
       WHERE source_key = @source_key`
    ).run(p);
    return { id: existing.id, inserted: false };
  });
  return tx();
}

/** 留资 upsert，自然键 source_key（tg:xxx / c:xxx）。语义同订单：customer_id 不被覆盖。 */
export function upsertLeadRow(row: LeadRowInput, db: Database.Database = getLedgerDb()): UpsertResult {
  const sourceKey = s(row.source_key);
  if (!sourceKey) throw new TypeError("upsertLeadRow: source_key required");
  const p = {
    source_key: sourceKey,
    customer_id: s(row.customer_id),
    name: s(row.name),
    contact: s(row.contact),
    interest: s(row.interest),
    message: s(row.message),
    lang: s(row.lang),
    source: s(row.source),
    utm: s(row.utm),
    status: s(row.status),
    first_seen: s(row.first_seen),
    last_seen: s(row.last_seen),
    count: int(row.count),
    raw: s(row.raw),
    synced_at: nowIso(),
  };
  const tx = db.transaction((): UpsertResult => {
    const existing = db.prepare("SELECT source_key FROM leads WHERE source_key = ?").get(sourceKey) as
      | { source_key: string }
      | undefined;
    if (!existing) {
      db.prepare(
        `INSERT INTO leads (source_key, customer_id, name, contact, interest, message, lang, source, utm, status, first_seen, last_seen, count, raw, synced_at)
         VALUES (@source_key, @customer_id, @name, @contact, @interest, @message, @lang, @source, @utm, @status, @first_seen, @last_seen, @count, @raw, @synced_at)`
      ).run(p);
      return { id: sourceKey, inserted: true };
    }
    db.prepare(
      `UPDATE leads SET
         customer_id = COALESCE(customer_id, @customer_id),
         name = @name, contact = @contact, interest = @interest, message = @message,
         lang = @lang, source = @source, utm = @utm, status = @status,
         first_seen = @first_seen, last_seen = @last_seen, count = @count,
         raw = @raw, synced_at = @synced_at
       WHERE source_key = @source_key`
    ).run(p);
    return { id: sourceKey, inserted: false };
  });
  return tx();
}

/** 授权 upsert，自然键 (source_system, source_key)。语义同上。 */
export function upsertLicenseRow(row: LicenseRowInput, db: Database.Database = getLedgerDb()): UpsertResult {
  const sourceSystem = s(row.source_system);
  const sourceKey = s(row.source_key);
  if (!sourceSystem || !sourceKey) throw new TypeError("upsertLicenseRow: source_system + source_key required");
  const p = {
    source_system: sourceSystem,
    source_key: sourceKey,
    customer_id: s(row.customer_id),
    product_id: s(row.product_id),
    sku_id: s(row.sku_id),
    plan: s(row.plan),
    edition: s(row.edition),
    seats: int(row.seats),
    machine_fingerprint: s(row.machine_fingerprint),
    issued_at: s(row.issued_at),
    expires_at: s(row.expires_at),
    status: s(row.status),
    raw: s(row.raw),
    synced_at: nowIso(),
  };
  const tx = db.transaction((): UpsertResult => {
    const existing = db
      .prepare("SELECT id FROM licenses WHERE source_system = ? AND source_key = ?")
      .get(sourceSystem, sourceKey) as { id: string } | undefined;
    if (!existing) {
      const id = row.id && isValidId(row.id, "lic") ? row.id : newId("lic");
      db.prepare(
        `INSERT INTO licenses (id, source_system, source_key, customer_id, product_id, sku_id, plan, edition, seats, machine_fingerprint, issued_at, expires_at, status, raw, synced_at)
         VALUES (@id, @source_system, @source_key, @customer_id, @product_id, @sku_id, @plan, @edition, @seats, @machine_fingerprint, @issued_at, @expires_at, @status, @raw, @synced_at)`
      ).run({ ...p, id });
      return { id, inserted: true };
    }
    db.prepare(
      `UPDATE licenses SET
         customer_id = COALESCE(customer_id, @customer_id),
         product_id = COALESCE(@product_id, product_id),
         sku_id = COALESCE(@sku_id, sku_id),
         plan = @plan, edition = @edition, seats = @seats,
         machine_fingerprint = @machine_fingerprint,
         issued_at = @issued_at, expires_at = @expires_at, status = @status,
         raw = @raw, synced_at = @synced_at
       WHERE source_system = @source_system AND source_key = @source_key`
    ).run(p);
    return { id: existing.id, inserted: false };
  });
  return tx();
}

// ── 客户 / 身份 / 归属 ──────────────────────────────────────────────
export interface CreateCustomerInput {
  display_name?: string | null;
  primary_contact?: string | null;
  tg_user_id?: string | null;
  source?: string | null;
  notes?: string | null;
}

export function createCustomer(
  input: CreateCustomerInput = {},
  db: Database.Database = getLedgerDb(),
  actor = "system"
): CustomerRow {
  const id = newId("cust");
  const t = nowIso();
  const rowValues = {
    id,
    display_name: s(input.display_name),
    primary_contact: s(input.primary_contact),
    tg_user_id: s(input.tg_user_id),
    source: s(input.source),
    notes: s(input.notes),
    created_at: t,
    updated_at: t,
  };
  const tx = db.transaction(() => {
    db.prepare(
      `INSERT INTO customers (id, display_name, primary_contact, tg_user_id, source, notes, created_at, updated_at)
       VALUES (@id, @display_name, @primary_contact, @tg_user_id, @source, @notes, @created_at, @updated_at)`
    ).run(rowValues);
    writeAudit({ actor, action: "customer.create", entity: "customer", entity_id: id, detail: rowValues }, db);
  });
  tx();
  return rowValues as CustomerRow;
}

export interface AttachIdentityResult {
  ok: boolean;
  existed: boolean;
  /** 冲突时：该身份已属于的另一位客户。 */
  conflictCustomerId?: string;
}

/** 给客户挂身份标识（幂等）。同 (kind,value) 已属于其他客户时不抢占，返回冲突信息。 */
export function attachIdentity(
  customerId: string,
  kind: IdentityKind,
  value: string,
  db: Database.Database = getLedgerDb(),
  actor = "system"
): AttachIdentityResult {
  if (!IDENTITY_KINDS.includes(kind)) throw new TypeError(`attachIdentity: bad kind ${kind}`);
  const v = normIdentityValue(kind, value);
  if (!v) throw new TypeError("attachIdentity: empty value");
  const tx = db.transaction((): AttachIdentityResult => {
    const existing = db
      .prepare("SELECT customer_id FROM identities WHERE kind = ? AND value = ?")
      .get(kind, v) as { customer_id: string } | undefined;
    if (existing) {
      if (existing.customer_id === customerId) return { ok: true, existed: true };
      return { ok: false, existed: true, conflictCustomerId: existing.customer_id };
    }
    db.prepare("INSERT INTO identities (customer_id, kind, value, created_at) VALUES (?, ?, ?, ?)").run(
      customerId,
      kind,
      v,
      nowIso()
    );
    writeAudit(
      { actor, action: "identity.attach", entity: "customer", entity_id: customerId, detail: { kind, value: v } },
      db
    );
    return { ok: true, existed: false };
  });
  return tx();
}

/** 按身份精确匹配已有客户：命中返回 customer_id，否则 null。不自动建客户。 */
export function linkCustomer(kind: IdentityKind, value: string, db: Database.Database = getLedgerDb()): string | null {
  const v = normIdentityValue(kind, value ?? "");
  if (!v) return null;
  const row = db.prepare("SELECT customer_id FROM identities WHERE kind = ? AND value = ?").get(kind, v) as
    | { customer_id: string }
    | undefined;
  return row?.customer_id ?? null;
}

export type LedgerEntity = "order" | "lead" | "license";

/** 手工把订单/留资/授权归属到客户（写 audit）。orders/licenses 的 key 可传账本 id 或 source_key；
 *  leads 传 source_key。客户不存在或行不存在返回 false。 */
export function assignCustomer(
  entity: LedgerEntity,
  entityKey: string,
  customerId: string,
  db: Database.Database = getLedgerDb(),
  actor = "admin"
): boolean {
  const key = entityKey.trim();
  if (!key) return false;
  const tx = db.transaction((): boolean => {
    const cust = db.prepare("SELECT id FROM customers WHERE id = ?").get(customerId) as { id: string } | undefined;
    if (!cust) return false;
    let changes = 0;
    if (entity === "order") {
      changes = db.prepare("UPDATE orders SET customer_id = ? WHERE id = ? OR source_key = ?").run(customerId, key, key).changes;
    } else if (entity === "lead") {
      changes = db.prepare("UPDATE leads SET customer_id = ? WHERE source_key = ?").run(customerId, key).changes;
    } else if (entity === "license") {
      changes = db.prepare("UPDATE licenses SET customer_id = ? WHERE id = ? OR source_key = ?").run(customerId, key, key).changes;
    }
    if (!changes) return false;
    writeAudit(
      { actor, action: "assign_customer", entity, entity_id: key, detail: { customer_id: customerId, rows: changes } },
      db
    );
    return true;
  });
  return tx();
}

/** 订单自动归属：按 fingerprint / contact 身份精确匹配已有客户（不自动建客户）。
 *  订单尚未归属且命中时写回 customer_id 并记 audit（actor=system）。返回 customer_id 或 null。 */
export function ensureCustomerForOrder(orderKey: string, db: Database.Database = getLedgerDb()): string | null {
  const o = db
    .prepare("SELECT id, source_key, customer_id, contact, fingerprint, notify_chat FROM orders WHERE id = ? OR source_key = ?")
    .get(orderKey, orderKey) as
    | Pick<OrderRow, "id" | "source_key" | "customer_id" | "contact" | "fingerprint" | "notify_chat">
    | undefined;
  if (!o) return null;
  if (o.customer_id) return o.customer_id;
  const chat = String(o.notify_chat ?? "").trim();
  const tgId = /^\d+$/.test(chat) ? chat : null; // 客户本人深链绑定的私聊 chat_id = 其 user id
  // 先按身份精确匹配已有客户（含 fingerprint / tg id / 联系方式）
  const candidates: [IdentityKind, string | null][] = [
    ["fingerprint", o.fingerprint],
    ["tg", tgId],
    ["contact", o.contact],
    ["email", o.contact],
    ["phone", o.contact],
  ];
  for (const [kind, value] of candidates) {
    if (!value) continue;
    const cid = linkCustomer(kind, value, db);
    if (cid) {
      db.prepare("UPDATE orders SET customer_id = ? WHERE id = ? AND customer_id IS NULL").run(cid, o.id);
      writeAudit(
        { actor: "system", action: "auto_link", entity: "order", entity_id: o.source_key, detail: { customer_id: cid, via: kind } },
        db
      );
      return cid;
    }
  }
  // 未命中：强信号（@handle / email / phone / tg id）自动建档（实时归档，实施22）。
  // 弱信号/自由文本/仅 fingerprint 不建档，避免热路径产生垃圾客户。
  const strong = classifyStrongContact(o.contact);
  if (!strong && !tgId) return null;
  const cid = createCustomerForContact(
    { display: strong?.display ?? `tg:${tgId}`, primaryContact: o.contact, tgUserId: tgId },
    db
  );
  if (strong) attachIdentity(cid, strong.kind, strong.value, db);
  if (o.contact && (!strong || normIdentityValue("contact", o.contact) !== strong.value))
    attachIdentity(cid, "contact", o.contact, db);
  if (tgId) attachIdentity(cid, "tg", tgId, db);
  db.prepare("UPDATE orders SET customer_id = ? WHERE id = ? AND customer_id IS NULL").run(cid, o.id);
  writeAudit(
    { actor: "system", action: "auto_create", entity: "order", entity_id: o.source_key, detail: { customer_id: cid, via: strong?.kind ?? "tg" } },
    db
  );
  return cid;
}

/** 留资自动归属：按 tg / contact 身份精确匹配已有客户；未命中且有强信号则自动建档。语义同订单。 */
export function ensureCustomerForLead(sourceKey: string, db: Database.Database = getLedgerDb()): string | null {
  const l = db
    .prepare("SELECT source_key, customer_id, name, contact, raw FROM leads WHERE source_key = ?")
    .get(sourceKey) as Pick<LeadRow, "source_key" | "customer_id" | "name" | "contact" | "raw"> | undefined;
  if (!l) return null;
  if (l.customer_id) return l.customer_id;
  let tgUserId: string | null = null;
  if (l.source_key.startsWith("tg:")) tgUserId = l.source_key.slice(3);
  else if (l.source_key.startsWith("c:")) {
    const c = classifyStrongContact(l.source_key.slice(2));
    if (c?.kind === "contact") tgUserId = null; // c: 前缀是联系方式，非 tg id
  }
  let rawTgId: string | null = null;
  try {
    const raw = l.raw ? (JSON.parse(l.raw) as { tg_user_id?: unknown }) : null;
    if (raw?.tg_user_id != null && /^\d+$/.test(String(raw.tg_user_id))) rawTgId = String(raw.tg_user_id);
  } catch {
    /* raw 非 JSON → 忽略 */
  }
  const tgId = tgUserId ?? rawTgId;
  const candidates: [IdentityKind, string | null][] = [
    ["tg", tgId],
    ["contact", l.contact],
    ["email", l.contact],
    ["phone", l.contact],
  ];
  for (const [kind, value] of candidates) {
    if (!value) continue;
    const cid = linkCustomer(kind, value, db);
    if (cid) {
      db.prepare("UPDATE leads SET customer_id = ? WHERE source_key = ? AND customer_id IS NULL").run(cid, l.source_key);
      writeAudit(
        { actor: "system", action: "auto_link", entity: "lead", entity_id: l.source_key, detail: { customer_id: cid, via: kind } },
        db
      );
      return cid;
    }
  }
  // 未命中：强信号或 tg id 自动建档（实时归档，实施22）
  const strong = classifyStrongContact(l.contact);
  if (!strong && !tgId) return null;
  const cid = createCustomerForContact(
    { display: s(l.name) ?? strong?.display ?? (tgId ? `tg:${tgId}` : null), primaryContact: l.contact, tgUserId: tgId },
    db
  );
  if (tgId) attachIdentity(cid, "tg", tgId, db);
  if (strong) attachIdentity(cid, strong.kind, strong.value, db);
  if (l.contact && (!strong || normIdentityValue("contact", l.contact) !== strong.value))
    attachIdentity(cid, "contact", l.contact, db);
  db.prepare("UPDATE leads SET customer_id = ? WHERE source_key = ? AND customer_id IS NULL").run(cid, l.source_key);
  writeAudit(
    { actor: "system", action: "auto_create", entity: "lead", entity_id: l.source_key, detail: { customer_id: cid, via: strong?.kind ?? "tg" } },
    db
  );
  return cid;
}

/** create-on-miss 小工具：建客户主档（实时归档钩子用）。identities 由调用方随后挂。 */
function createCustomerForContact(
  input: { display: string | null; primaryContact: string | null; tgUserId: string | null },
  db: Database.Database
): string {
  const cust = createCustomer(
    {
      display_name: input.display,
      primary_contact: input.primaryContact,
      tg_user_id: input.tgUserId,
      source: "auto:order-lead",
      notes: "下单/留资实时自动建档（强信号）",
    },
    db,
    "system"
  );
  return cust.id;
}

// ── 审计 ────────────────────────────────────────────────────────────
export interface WriteAuditInput {
  actor?: string | null;
  action: string;
  entity?: string | null;
  entity_id?: string | null;
  detail?: unknown;
}

export function writeAudit(a: WriteAuditInput, db: Database.Database = getLedgerDb()): AuditRow {
  const row: AuditRow = {
    id: newId("aud"),
    ts: nowIso(),
    actor: s(a.actor) ?? "system",
    action: a.action,
    entity: s(a.entity),
    entity_id: s(a.entity_id),
    detail: a.detail === undefined ? null : typeof a.detail === "string" ? a.detail : JSON.stringify(a.detail),
  };
  db.prepare(
    "INSERT INTO audit (id, ts, actor, action, entity, entity_id, detail) VALUES (@id, @ts, @actor, @action, @entity, @entity_id, @detail)"
  ).run(row);
  return row;
}

// ── 查询（简单过滤 + 分页；console 消费）────────────────────────────
export interface ListOptions {
  limit?: number;
  offset?: number;
  /** 默认 false：列表排除测试/演练数据（is_test=1）。true 时一并带出（配合 UI「显示测试」开关）。 */
  includeTest?: boolean;
}

// 表是否有 is_test 列（按连接缓存；迁移前旧库/无该表时安全返回 false）。
const _isTestColCache = new WeakMap<Database.Database, Map<string, boolean>>();
function tableHasIsTest(db: Database.Database, table: string): boolean {
  let m = _isTestColCache.get(db);
  if (!m) _isTestColCache.set(db, (m = new Map()));
  const hit = m.get(table);
  if (hit !== undefined) return hit;
  let has = false;
  try {
    has = (db.prepare(`PRAGMA table_info(${table})`).all() as { name: string }[]).some((c) => c.name === "is_test");
  } catch {
    has = false;
  }
  m.set(table, has);
  return has;
}
export interface ListResult<T> {
  rows: T[];
  total: number;
  limit: number;
  offset: number;
}

function page(opts: ListOptions): { limit: number; offset: number } {
  const limit = Math.min(Math.max(1, Math.trunc(opts.limit ?? 100)), 500);
  const offset = Math.max(0, Math.trunc(opts.offset ?? 0));
  return { limit, offset };
}

function runList<T>(
  db: Database.Database,
  table: string,
  where: string[],
  params: Record<string, unknown>,
  orderBy: string,
  opts: ListOptions
): ListResult<T> {
  // 默认排除测试数据（实施23）：表有 is_test 列且未显式 includeTest 时加过滤。
  if (!opts.includeTest && tableHasIsTest(db, table)) where = [...where, "COALESCE(is_test,0) = 0"];
  const cond = where.length ? ` WHERE ${where.join(" AND ")}` : "";
  const { limit, offset } = page(opts);
  const total = (db.prepare(`SELECT COUNT(*) AS c FROM ${table}${cond}`).get(params) as { c: number }).c;
  const rows = db
    .prepare(`SELECT * FROM ${table}${cond} ORDER BY ${orderBy} LIMIT @limit OFFSET @offset`)
    .all({ ...params, limit, offset }) as T[];
  return { rows, total, limit, offset };
}

export interface OrderFilter extends ListOptions {
  status?: string;
  customerId?: string;
  productId?: string;
  /** 模糊匹配 source_key / contact / plan。 */
  q?: string;
}

export function listOrders(filter: OrderFilter = {}, db: Database.Database = getLedgerDb()): ListResult<OrderRow> {
  const where: string[] = [];
  const params: Record<string, unknown> = {};
  if (s(filter.status)) { where.push("status = @status"); params.status = s(filter.status); }
  if (s(filter.customerId)) { where.push("customer_id = @customerId"); params.customerId = s(filter.customerId); }
  if (s(filter.productId)) { where.push("product_id = @productId"); params.productId = s(filter.productId); }
  if (s(filter.q)) {
    where.push("(source_key LIKE @q OR contact LIKE @q OR plan LIKE @q)");
    params.q = `%${s(filter.q)}%`;
  }
  return runList<OrderRow>(db, "orders", where, params, "COALESCE(created_at, '') DESC, source_key DESC", filter);
}

export interface LeadFilter extends ListOptions {
  status?: string;
  customerId?: string;
  /** 模糊匹配 source_key / name / contact。 */
  q?: string;
}

export function listLeads(filter: LeadFilter = {}, db: Database.Database = getLedgerDb()): ListResult<LeadRow> {
  const where: string[] = [];
  const params: Record<string, unknown> = {};
  if (s(filter.status)) { where.push("status = @status"); params.status = s(filter.status); }
  if (s(filter.customerId)) { where.push("customer_id = @customerId"); params.customerId = s(filter.customerId); }
  if (s(filter.q)) {
    where.push("(source_key LIKE @q OR name LIKE @q OR contact LIKE @q)");
    params.q = `%${s(filter.q)}%`;
  }
  return runList<LeadRow>(db, "leads", where, params, "COALESCE(last_seen, '') DESC, source_key DESC", filter);
}

export interface LicenseFilter extends ListOptions {
  status?: string;
  customerId?: string;
  sourceSystem?: string;
  productId?: string;
  /** 只看 N 天内到期（expires_at ∈ (now, now+N]）。 */
  expiringInDays?: number;
}

export function listLicenses(filter: LicenseFilter = {}, db: Database.Database = getLedgerDb()): ListResult<LicenseRow> {
  const where: string[] = [];
  const params: Record<string, unknown> = {};
  if (s(filter.status)) { where.push("status = @status"); params.status = s(filter.status); }
  if (s(filter.customerId)) { where.push("customer_id = @customerId"); params.customerId = s(filter.customerId); }
  if (s(filter.sourceSystem)) { where.push("source_system = @sourceSystem"); params.sourceSystem = s(filter.sourceSystem); }
  if (s(filter.productId)) { where.push("product_id = @productId"); params.productId = s(filter.productId); }
  if (filter.expiringInDays !== undefined && Number.isFinite(filter.expiringInDays)) {
    where.push("expires_at IS NOT NULL AND expires_at > @now AND expires_at <= @until");
    params.now = nowIso();
    params.until = new Date(Date.now() + filter.expiringInDays * 86400000).toISOString();
  }
  return runList<LicenseRow>(
    db,
    "licenses",
    where,
    params,
    "CASE WHEN expires_at IS NULL THEN 1 ELSE 0 END, expires_at ASC",
    filter
  );
}

export interface CustomerFilter extends ListOptions {
  /** 模糊匹配 display_name / primary_contact / tg_user_id。 */
  q?: string;
}

export function listCustomers(filter: CustomerFilter = {}, db: Database.Database = getLedgerDb()): ListResult<CustomerRow> {
  const where: string[] = [];
  const params: Record<string, unknown> = {};
  if (s(filter.q)) {
    where.push("(display_name LIKE @q OR primary_contact LIKE @q OR tg_user_id LIKE @q)");
    params.q = `%${s(filter.q)}%`;
  }
  return runList<CustomerRow>(db, "customers", where, params, "COALESCE(created_at, '') DESC, id DESC", filter);
}

// ── 统计 ────────────────────────────────────────────────────────────
export interface LedgerStats {
  customers: number;
  identities: number;
  leads: number;
  orders: number;
  licenses: number;
  audit: number;
  ordersByStatus: Record<string, number>;
  /** 30 天内到期（未 revoked/expired）的授权数。 */
  licensesExpiringIn30d: number;
  /** 被标记为测试/演练的数据量（headline 计数已排除这些；供 UI 显示「+N 测试」）。 */
  test: { customers: number; leads: number; orders: number; licenses: number };
  generatedAt: string;
}

export function getStats(db: Database.Database = getLedgerDb()): LedgerStats {
  // headline 计数默认排除测试数据（is_test=1）；无该列的旧库退化为全量（excl 为空串）。
  const excl = (table: string) => (tableHasIsTest(db, table) ? " WHERE COALESCE(is_test,0) = 0" : "");
  const count = (table: string) =>
    (db.prepare(`SELECT COUNT(*) AS c FROM ${table}${excl(table)}`).get() as { c: number }).c;
  const testCount = (table: string) =>
    tableHasIsTest(db, table)
      ? (db.prepare(`SELECT COUNT(*) AS c FROM ${table} WHERE is_test = 1`).get() as { c: number }).c
      : 0;
  const byStatus: Record<string, number> = {};
  const ordExcl = tableHasIsTest(db, "orders") ? " WHERE COALESCE(is_test,0) = 0" : "";
  for (const r of db
    .prepare(`SELECT COALESCE(status, '(null)') AS status, COUNT(*) AS c FROM orders${ordExcl} GROUP BY status`)
    .all() as { status: string; c: number }[]) {
    byStatus[r.status] = r.c;
  }
  const now = nowIso();
  const until = new Date(Date.now() + 30 * 86400000).toISOString();
  const licExcl = tableHasIsTest(db, "licenses") ? " AND COALESCE(is_test,0) = 0" : "";
  const expiring = (
    db
      .prepare(
        `SELECT COUNT(*) AS c FROM licenses
         WHERE expires_at IS NOT NULL AND expires_at > ? AND expires_at <= ?
           AND (status IS NULL OR status NOT IN ('revoked','expired'))${licExcl}`
      )
      .get(now, until) as { c: number }
  ).c;
  return {
    customers: count("customers"),
    identities: count("identities"),
    leads: count("leads"),
    orders: count("orders"),
    licenses: count("licenses"),
    audit: count("audit"),
    ordersByStatus: byStatus,
    licensesExpiringIn30d: expiring,
    test: {
      customers: testCount("customers"),
      leads: testCount("leads"),
      orders: testCount("orders"),
      licenses: testCount("licenses"),
    },
    generatedAt: now,
  };
}
