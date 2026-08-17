// 集团账本 CLI 共享库（纯 JS，供 scripts/ledger-backfill.mjs、ledger-import-licenses.mjs、
// ledger-link-customers.mjs 直接 node 运行）。
//
// ⚠️ 本文件是 website/lib/ledger.ts 的纯 JS 等价实现（DDL / upsert 语义 / ID 规范 /
// inferProductId 与 TS 版一一对应）。修改任何一侧的表结构、upsert 语义或映射逻辑时，
// 必须同步修改另一侧，DDL 文本保持逐字一致！

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { randomBytes } from "node:crypto";
import Database from "better-sqlite3";

export const LEDGER_SCHEMA_VERSION = 6;

// ── ID 规范（与 lib/ids.ts 一致）───────────────────────────────────
const ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
export const ID_REGEX = /^[a-z]{2,5}_[0-9A-HJKMNP-TV-Z]{26}$/;

function encodeTime(ms) {
  let t = Math.floor(ms);
  if (!Number.isFinite(t) || t < 0) t = 0;
  if (t > 2 ** 48 - 1) t = 2 ** 48 - 1;
  const out = new Array(10);
  for (let i = 9; i >= 0; i--) {
    out[i] = ALPHABET[t % 32];
    t = Math.floor(t / 32);
  }
  return out.join("");
}

function encodeRandom(bytes) {
  let out = "";
  let acc = 0;
  let bits = 0;
  for (const b of bytes) {
    acc = (acc << 8) | b;
    bits += 8;
    while (bits >= 5) {
      out += ALPHABET[(acc >>> (bits - 5)) & 31];
      bits -= 5;
    }
    acc &= (1 << bits) - 1;
  }
  return out;
}

export function ulid(time = Date.now()) {
  return encodeTime(time) + encodeRandom(randomBytes(10));
}

export function newId(prefix) {
  if (!/^[a-z]{2,5}$/.test(prefix)) throw new TypeError(`invalid id prefix: ${prefix}`);
  return `${prefix}_${ulid()}`;
}

export function isValidId(id, prefix) {
  if (typeof id !== "string" || !ID_REGEX.test(id)) return false;
  return prefix ? id.startsWith(prefix + "_") : true;
}

// ── 数据目录 / DB 路径（与 lib/data-dir.ts 的解析规则一致）─────────
export function resolveDataDir() {
  if (process.env.LEADS_DIR) return process.env.LEADS_DIR;
  const legacy = path.join(os.homedir(), "yuntech-leads");
  const primary = path.join(os.homedir(), "hualing-leads");
  try {
    if (!fs.existsSync(primary) && fs.existsSync(legacy)) return legacy;
  } catch {
    /* ignore */
  }
  return primary;
}

export function resolveLedgerDbPath() {
  return process.env.LEDGER_DB || path.join(resolveDataDir(), "group-ledger.db");
}

// ── 表结构（schema v1）──────────────────────────────────────────────
// ⚠️ 与 website/lib/ledger.ts 中的 DDL_V1 逐字一致，修改必须同步！
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
// ⚠️ 与 website/lib/ledger.ts 中的 DDL_V2 逐字一致，修改必须同步！
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
// ⚠️ 与 website/lib/ledger.ts 中的 DDL_V3 逐字一致，修改必须同步！
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
// ⚠️ 与 website/lib/ledger.ts 中的 DDL_V4 逐字一致，修改必须同步！
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
// ⚠️ 与 website/lib/ledger.ts 中的 DDL_V5 逐字一致，修改必须同步！
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

/** 打开账本 DB（WAL / busy_timeout 5000 / 自动建表迁移），与 lib/ledger.ts::getLedgerDb 等价。 */
export function openLedgerDb(dbPath) {
  const file = path.resolve(dbPath || resolveLedgerDbPath());
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const db = new Database(file);
  db.pragma("journal_mode = WAL");
  db.pragma("busy_timeout = 5000");
  db.pragma("synchronous = NORMAL");
  db.pragma("foreign_keys = ON");
  migrate(db);
  return db;
}

// ⚠️ 与 website/lib/ledger.ts 的 migrateV6 逐字一致（is_test 标记列，条件式 ALTER 幂等）。
function migrateV6(d) {
  const hasCol = (t, c) => d.prepare(`PRAGMA table_info(${t})`).all().some((x) => x.name === c);
  for (const t of ["customers", "orders", "leads", "licenses"]) {
    if (!hasCol(t, "is_test")) d.exec(`ALTER TABLE ${t} ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0`);
  }
  d.exec("CREATE INDEX IF NOT EXISTS idx_orders_is_test ON orders(is_test)");
}

function migrate(db) {
  db.exec("CREATE TABLE IF NOT EXISTS meta (\n  key TEXT PRIMARY KEY,\n  value TEXT\n);");
  const row = db.prepare("SELECT value FROM meta WHERE key = 'schema_version'").get();
  const current = row ? Number(row.value) || 0 : 0;
  const migrations = [(d) => d.exec(DDL_V1), (d) => d.exec(DDL_V2), (d) => d.exec(DDL_V3), (d) => d.exec(DDL_V4), (d) => d.exec(DDL_V5), migrateV6];
  if (current >= migrations.length) return;
  const run = db.transaction(() => {
    for (let i = current; i < migrations.length; i++) migrations[i](db);
    db.prepare(
      "INSERT INTO meta (key, value) VALUES ('schema_version', ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    ).run(String(migrations.length));
  });
  run.immediate();
}

// ── 取值规整（与 ledger.ts 一致）───────────────────────────────────
export function s(v) {
  if (v === undefined || v === null) return null;
  const t = String(v).trim();
  return t === "" ? null : t;
}
export function num(v) {
  if (v === undefined || v === null || v === "") return null;
  const x = typeof v === "number" ? v : Number(v);
  return Number.isFinite(x) ? x : null;
}
export function int(v) {
  const x = num(v);
  return x === null ? null : Math.trunc(x);
}
const nowIso = () => new Date().toISOString();

// ── 测试/演练数据判定（schema v6 is_test 的唯一口径）────────────────
// 保守词边界匹配（宁可漏标不误标——误标会把真实数据从 KPI 滤掉）：
//   - test / drill / smoke 前后都必须是非字母数字（"contest"/"latest"/"drilling"/
//     "smoked" 不命中；"my-test" / "drill-run" 命中）；
//   - e2e 只要求前边界（"E2ENOTIFYFP01" 这类 e2e 前缀指纹命中；正常英文词不会
//     以 e2e 开头，误伤面为零）；
//   - @internal 只认结尾（e2e 脚本约定 contact 形如 e2e-notify@internal）。
// ⚠️ 与 lib/ledger.ts 的 isTestSignal 逐字一致，修改必须同步！
const TEST_SIGNAL_RE = /(^|[^a-z0-9])e2e|(^|[^a-z0-9])(test|drill|smoke)([^a-z0-9]|$)|@internal\s*$/i;

/** 任一入参命中测试信号即 true（null/undefined 跳过）。 */
export function isTestSignal(...vals) {
  return vals.some((v) => v !== null && v !== undefined && TEST_SIGNAL_RE.test(String(v)));
}

// ── 联系方式分类（客户归并的公共规则）───────────────────────────────
// 仅显式 @xxx / t.me/xxx 形态判为 handle，email/phone 精确形态判定，其余为自由
// 文本（不作跨行归并键）。ledger-link-customers.mjs 与 ensureCustomerFor* 的
// create-on-miss 共用本函数。
// ⚠️ 与 lib/ledger.ts 的 classifyStrongContact 同规则（TS 侧只取强信号三态），修改必须同步！
export function classifyContact(text) {
  const t = String(text ?? "").trim();
  if (!t) return null;
  if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(t)) return { type: "email", norm: t.toLowerCase() };
  const handle =
    t.match(/^@([A-Za-z0-9_]{3,32})$/) ||
    t.match(/^(?:https?:\/\/)?(?:www\.)?t(?:elegram)?\.me\/@?([A-Za-z0-9_]{3,32})\/?$/i);
  if (handle) return { type: "handle", norm: handle[1].toLowerCase() };
  const digits = t.replace(/[\s\-()]/g, "");
  if (/^\+?\d{7,15}$/.test(digits)) return { type: "phone", norm: digits };
  return { type: "text", norm: t.toLowerCase().replace(/\s+/g, "") };
}

/** classifyContact 的强信号视图：handle/email/phone → { kind, value, display }；
 *  自由文本/空 → null（不建档）。与 lib/ledger.ts::classifyStrongContact 语义一致。 */
export function classifyStrongContact(text) {
  const c = classifyContact(text);
  if (!c || c.type === "text") return null;
  if (c.type === "email") return { kind: "email", value: c.norm, display: c.norm };
  if (c.type === "handle") return { kind: "contact", value: c.norm, display: `@${c.norm}` };
  return { kind: "phone", value: c.norm, display: String(text).trim() };
}

// ── 产品映射（与 ledger.ts::inferProductId 一致，修改必须同步！）──
const PRODUCT_EXACT = {
  voice: "huansheng",
  faceswap: "huanyan",
  "digital-human": "huanying",
  "video-dubbing": "huanying",
  translate: "tongyi",
};

export function inferProductId(plan, edition) {
  const p = (plan ?? "").trim().toLowerCase();
  if (p && PRODUCT_EXACT[p]) return PRODUCT_EXACT[p];
  const hay = `${p} ${(edition ?? "").trim().toLowerCase()}`;
  if (!hay.trim()) return null;
  const rules = [
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

// ── 订单 plan → 全域 SKU 静态映射（sku_registry.json 关联键）────────
// ⚠️ 条目与 lib/offer-map.ts 的 ORDER_SKU_MAP 文本一致（本文件不经 TS 编译、无法
// import 那份），修改必须两处同步！
const ORDER_SKU_MAP = {
  // 通译 LingoX（pricing.ts translateOffers → registry tongyi/lingox-*）
  "translate-charpack": { skuId: "lingox-charpack", productId: "tongyi" },
  "translate-team": { skuId: "lingox-team", productId: "tongyi" },
  "translate-pro": { skuId: "lingox-pro", productId: "tongyi" },
  // 智聊 ChatX（pricing.ts autochatOffers → registry zhiliao/chatx-*）
  "autochat-team": { skuId: "chatx-team", productId: "zhiliao" },
  "autochat-flagship": { skuId: "chatx-flagship", productId: "zhiliao" },
  // 实时部署（pricing.ts realtimeOffers）：basic → 幻颜实时换脸部署；
  // creator → 幻影创作者全能部署（registry 2026-07 新增 livex-creator-deploy）
  "realtime-basic": { skuId: "facex-live-deploy", productId: "huanyan" },
  "realtime-creator": { skuId: "livex-creator-deploy", productId: "huanying" },
};

/** 下单参数 → 全域 SKU（宽松容错：大小写 / 首尾空白；未命中一律 null，宁缺毋错）。
 *  ⚠️ 与 lib/offer-map.ts 的 resolveOrderSku 逻辑一致，修改必须同步！ */
export function resolveOrderSku(plan, edition, period) {
  void edition;
  void period;
  const key = String(plan ?? "").trim().toLowerCase();
  const hit = key && Object.prototype.hasOwnProperty.call(ORDER_SKU_MAP, key) ? ORDER_SKU_MAP[key] : undefined;
  return hit ? { skuId: hit.skuId, productId: hit.productId } : { skuId: null, productId: null };
}

// ── 幂等 upsert（自然键；customer_id 不被覆盖 —— 与 ledger.ts 语义一致）──
export function upsertOrderRow(row, db) {
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
    is_test: row.is_test ? 1 : 0,
    synced_at: nowIso(),
  };
  const tx = db.transaction(() => {
    const existing = db.prepare("SELECT id FROM orders WHERE source_key = ?").get(sourceKey);
    if (!existing) {
      const id = row.id && isValidId(row.id, "ord") ? row.id : newId("ord");
      db.prepare(
        `INSERT INTO orders (id, source_key, customer_id, product_id, sku_id, plan, edition, period, amount, pay_amount, currency, status, contact, fingerprint, lang, created_at, paid_at, activated_at, notify_chat, code, raw, is_test, synced_at)
         VALUES (@id, @source_key, @customer_id, @product_id, @sku_id, @plan, @edition, @period, @amount, @pay_amount, @currency, @status, @contact, @fingerprint, @lang, @created_at, @paid_at, @activated_at, @notify_chat, @code, @raw, @is_test, @synced_at)`
      ).run({ ...p, id });
      return { id, inserted: true };
    }
    // is_test 只升不降（MAX）：回扫打过的测试标不被后续 JSON 镜像双写抹掉
    db.prepare(
      `UPDATE orders SET
         customer_id = COALESCE(customer_id, @customer_id),
         product_id = COALESCE(@product_id, product_id),
         sku_id = COALESCE(@sku_id, sku_id),
         plan = @plan, edition = @edition, period = @period,
         amount = @amount, pay_amount = @pay_amount, currency = @currency,
         status = @status, contact = @contact, fingerprint = @fingerprint, lang = @lang,
         created_at = @created_at, paid_at = @paid_at, activated_at = @activated_at,
         notify_chat = @notify_chat, code = @code, raw = @raw,
         is_test = MAX(is_test, @is_test), synced_at = @synced_at
       WHERE source_key = @source_key`
    ).run(p);
    return { id: existing.id, inserted: false };
  });
  return tx();
}

export function upsertLeadRow(row, db) {
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
    is_test: row.is_test ? 1 : 0,
    synced_at: nowIso(),
  };
  const tx = db.transaction(() => {
    const existing = db.prepare("SELECT source_key FROM leads WHERE source_key = ?").get(sourceKey);
    if (!existing) {
      db.prepare(
        `INSERT INTO leads (source_key, customer_id, name, contact, interest, message, lang, source, utm, status, first_seen, last_seen, count, raw, is_test, synced_at)
         VALUES (@source_key, @customer_id, @name, @contact, @interest, @message, @lang, @source, @utm, @status, @first_seen, @last_seen, @count, @raw, @is_test, @synced_at)`
      ).run(p);
      return { id: sourceKey, inserted: true };
    }
    // is_test 只升不降（MAX）：语义同订单
    db.prepare(
      `UPDATE leads SET
         customer_id = COALESCE(customer_id, @customer_id),
         name = @name, contact = @contact, interest = @interest, message = @message,
         lang = @lang, source = @source, utm = @utm, status = @status,
         first_seen = @first_seen, last_seen = @last_seen, count = @count,
         raw = @raw, is_test = MAX(is_test, @is_test), synced_at = @synced_at
       WHERE source_key = @source_key`
    ).run(p);
    return { id: sourceKey, inserted: false };
  });
  return tx();
}

export function upsertLicenseRow(row, db) {
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
    is_test: row.is_test ? 1 : 0,
    synced_at: nowIso(),
  };
  const tx = db.transaction(() => {
    const existing = db
      .prepare("SELECT id FROM licenses WHERE source_system = ? AND source_key = ?")
      .get(sourceSystem, sourceKey);
    if (!existing) {
      const id = row.id && isValidId(row.id, "lic") ? row.id : newId("lic");
      db.prepare(
        `INSERT INTO licenses (id, source_system, source_key, customer_id, product_id, sku_id, plan, edition, seats, machine_fingerprint, issued_at, expires_at, status, raw, is_test, synced_at)
         VALUES (@id, @source_system, @source_key, @customer_id, @product_id, @sku_id, @plan, @edition, @seats, @machine_fingerprint, @issued_at, @expires_at, @status, @raw, @is_test, @synced_at)`
      ).run({ ...p, id });
      return { id, inserted: true };
    }
    // is_test 只升不降（MAX）：语义同订单
    db.prepare(
      `UPDATE licenses SET
         customer_id = COALESCE(customer_id, @customer_id),
         product_id = COALESCE(@product_id, product_id),
         sku_id = COALESCE(@sku_id, sku_id),
         plan = @plan, edition = @edition, seats = @seats,
         machine_fingerprint = @machine_fingerprint,
         issued_at = @issued_at, expires_at = @expires_at, status = @status,
         raw = @raw, is_test = MAX(is_test, @is_test), synced_at = @synced_at
       WHERE source_system = @source_system AND source_key = @source_key`
    ).run(p);
    return { id: existing.id, inserted: false };
  });
  return tx();
}

// ── 身份匹配 / 自动归属 / 审计（与 ledger.ts 语义一致）──────────────
export const IDENTITY_KINDS = ["contact", "tg", "email", "phone", "fingerprint"];

export function normIdentityValue(kind, value) {
  const v = String(value ?? "").trim();
  if (kind === "contact" || kind === "email") return v.toLowerCase().replace(/\s+/g, "");
  if (kind === "phone") return v.replace(/[\s\-()]/g, "");
  return v;
}

export function linkCustomer(kind, value, db) {
  const v = normIdentityValue(kind, value);
  if (!v) return null;
  const row = db.prepare("SELECT customer_id FROM identities WHERE kind = ? AND value = ?").get(kind, v);
  return row?.customer_id ?? null;
}

export function writeAudit(a, db) {
  const row = {
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

/** 建客户主档（cust_ ULID + audit customer.create）。与 lib/ledger.ts::createCustomer
 *  语义一致（修改必须同步）；mjs 侧无连接单例，db 必须显式传入。 */
export function createCustomer(input = {}, db, actor = "system") {
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
    is_test: input.is_test ? 1 : 0,
  };
  const tx = db.transaction(() => {
    db.prepare(
      `INSERT INTO customers (id, display_name, primary_contact, tg_user_id, source, notes, created_at, updated_at, is_test)
       VALUES (@id, @display_name, @primary_contact, @tg_user_id, @source, @notes, @created_at, @updated_at, @is_test)`
    ).run(rowValues);
    writeAudit({ actor, action: "customer.create", entity: "customer", entity_id: id, detail: rowValues }, db);
  });
  tx();
  return rowValues;
}

/** 给客户挂身份标识（幂等）。同 (kind,value) 已属于其他客户时不抢占，返回冲突信息。
 *  与 lib/ledger.ts::attachIdentity 语义一致（修改必须同步）。 */
export function attachIdentity(customerId, kind, value, db, actor = "system") {
  if (!IDENTITY_KINDS.includes(kind)) throw new TypeError(`attachIdentity: bad kind ${kind}`);
  const v = normIdentityValue(kind, value);
  if (!v) throw new TypeError("attachIdentity: empty value");
  const tx = db.transaction(() => {
    const existing = db.prepare("SELECT customer_id FROM identities WHERE kind = ? AND value = ?").get(kind, v);
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

/** create-on-miss 小工具：建客户主档（实时归档钩子用）。identities 由调用方随后挂。
 *  ⚠️ 与 lib/ledger.ts::createCustomerForContact 一致，修改必须同步！ */
function createCustomerForContact(input, db) {
  const cust = createCustomer(
    {
      display_name: input.display,
      primary_contact: input.primaryContact,
      tg_user_id: input.tgUserId,
      source: "auto:order-lead",
      notes: "下单/留资实时自动建档（强信号）",
      is_test: input.isTest ? 1 : 0,
    },
    db,
    "system"
  );
  return cust.id;
}

/** 订单自动归属：先按身份匹配已有客户，未命中且有强信号（handle/email/phone/tg id）
 *  则自动建档并回填。⚠️ 与 lib/ledger.ts::ensureCustomerForOrder 语义一致，修改必须同步！ */
export function ensureCustomerForOrder(orderKey, db) {
  const o = db
    .prepare("SELECT id, source_key, customer_id, contact, fingerprint, notify_chat, is_test FROM orders WHERE id = ? OR source_key = ?")
    .get(orderKey, orderKey);
  if (!o) return null;
  if (o.customer_id) return o.customer_id;
  const chat = String(o.notify_chat ?? "").trim();
  const tgId = /^\d+$/.test(chat) ? chat : null; // 客户本人深链绑定的私聊 chat_id = 其 user id
  const candidates = [
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
  const strong = classifyStrongContact(o.contact);
  if (!strong && !tgId) return null;
  const cid = createCustomerForContact(
    {
      display: strong?.display ?? `tg:${tgId}`,
      primaryContact: o.contact,
      tgUserId: tgId,
      isTest: !!o.is_test || isTestSignal(o.contact),
    },
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

/** 留资自动归属：语义同订单。⚠️ 与 lib/ledger.ts::ensureCustomerForLead 一致，修改必须同步！ */
export function ensureCustomerForLead(sourceKey, db) {
  const l = db
    .prepare("SELECT source_key, customer_id, name, contact, raw, is_test FROM leads WHERE source_key = ?")
    .get(sourceKey);
  if (!l) return null;
  if (l.customer_id) return l.customer_id;
  let tgUserId = null;
  if (l.source_key.startsWith("tg:") && /^\d+$/.test(l.source_key.slice(3))) tgUserId = l.source_key.slice(3);
  let rawTgId = null;
  try {
    const raw = l.raw ? JSON.parse(l.raw) : null;
    if (raw?.tg_user_id != null && /^\d+$/.test(String(raw.tg_user_id))) rawTgId = String(raw.tg_user_id);
  } catch {
    /* raw 非 JSON → 忽略 */
  }
  const tgId = tgUserId ?? rawTgId;
  const candidates = [
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
  const strong = classifyStrongContact(l.contact);
  if (!strong && !tgId) return null;
  const cid = createCustomerForContact(
    {
      display: s(l.name) ?? strong?.display ?? (tgId ? `tg:${tgId}` : null),
      primaryContact: l.contact,
      tgUserId: tgId,
      isTest: !!l.is_test || isTestSignal(l.contact, l.name),
    },
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

// ── 行转换（与 lib/ledger-sync.ts 的 orderEntryToRow/leadEntryToRow 一致）──
export function orderEntryToRow(o) {
  // sku_id/product_id：来件自带（新订单 createOrder 出生即填）则直传；历史订单缺失时
  // 先走 ORDER_SKU_MAP 静态映射推断，product_id 仍不中再走 inferProductId 宽松推断。
  const resolved = resolveOrderSku(o.plan, o.edition, o.period);
  return {
    source_key: o.id,
    product_id: o.product_id ?? resolved.productId ?? inferProductId(o.plan, o.edition),
    sku_id: o.sku_id ?? resolved.skuId,
    plan: o.plan ?? null,
    edition: o.edition ?? null,
    period: o.period ?? null,
    amount: o.amount ?? null,
    pay_amount: o.pay_amount ?? null,
    currency: o.currency ?? null,
    status: o.status ?? null,
    contact: o.contact ?? null,
    fingerprint: o.fingerprint ?? null,
    lang: o.lang ?? null,
    created_at: o.t ?? null,
    paid_at: o.paid_at ?? null,
    activated_at: o.activated_at ?? null,
    notify_chat: o.notify_chat != null ? String(o.notify_chat) : null,
    code: o.code ?? null,
    raw: JSON.stringify(o),
    // e2e/演练单出生即标（contact 如 e2e-notify@internal、指纹如 E2ENOTIFYFP01）
    is_test: isTestSignal(o.contact, o.fingerprint) ? 1 : 0,
  };
}

export function leadEntryToRow(e) {
  return {
    source_key: e.id,
    name: e.name ?? null,
    contact: e.contact ?? null,
    interest: e.interest ?? null,
    message: e.message ?? null,
    lang: e.lang ?? null,
    source: e.source ?? null,
    utm: e.utm ?? null,
    status: e.status ?? null,
    first_seen: e.firstSeen ?? null,
    last_seen: e.lastSeen ?? null,
    count: e.count ?? null,
    raw: JSON.stringify(e),
    // e2e/演练留资出生即标（contact/name/来源键任一命中测试信号）
    is_test: isTestSignal(e.contact, e.name, e.id) ? 1 : 0,
  };
}

// ── 统计（与 ledger.ts::getStats 一致）─────────────────────────────
export function getStats(db) {
  const count = (table) => db.prepare(`SELECT COUNT(*) AS c FROM ${table}`).get().c;
  const byStatus = {};
  for (const r of db
    .prepare("SELECT COALESCE(status, '(null)') AS status, COUNT(*) AS c FROM orders GROUP BY status")
    .all()) {
    byStatus[r.status] = r.c;
  }
  const now = nowIso();
  const until = new Date(Date.now() + 30 * 86400000).toISOString();
  const expiring = db
    .prepare(
      `SELECT COUNT(*) AS c FROM licenses
       WHERE expires_at IS NOT NULL AND expires_at > ? AND expires_at <= ?
         AND (status IS NULL OR status NOT IN ('revoked','expired'))`
    )
    .get(now, until).c;
  return {
    customers: count("customers"),
    identities: count("identities"),
    leads: count("leads"),
    orders: count("orders"),
    licenses: count("licenses"),
    audit: count("audit"),
    ordersByStatus: byStatus,
    licensesExpiringIn30d: expiring,
    generatedAt: now,
  };
}
