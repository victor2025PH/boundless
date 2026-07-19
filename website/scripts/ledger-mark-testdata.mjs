// ledger-mark-testdata.mjs — 标记集团账本里的测试/演练数据（e2e/smoke/@internal…），
// 让真实经营口径（KPI/商机/客户 360）能把它们排除或标注。幂等、保守、只读判定不删数据。
//
// 自包含设计：不依赖 ledger-lib.mjs 的 schema——直接 PRAGMA 探列、按需 ALTER 补 is_test 列，
// 与未来 ledger.ts 的 DDL 用同名列（is_test INTEGER DEFAULT 0）保持兼容（加列前先探测存在）。
//
// 用法：
//   node scripts/ledger-mark-testdata.mjs [--db <路径>] [--dry-run]
//   缺省 db：env LEDGER_DB / DATA_DIR/group-ledger.db（与 ledger.ts 同源），VPS 上通常
//   /home/ubuntu/hualing-leads/group-ledger.db（DATA_DIR 指向该目录）。
import Database from "better-sqlite3";
import path from "node:path";
import process from "node:process";

const args = process.argv.slice(2);
const DRY = args.includes("--dry-run");
const dbFlag = args.indexOf("--db");
const DB_PATH =
  dbFlag >= 0
    ? args[dbFlag + 1]
    : process.env.LEDGER_DB ||
      path.join(process.env.DATA_DIR || process.cwd(), "group-ledger.db");

// 测试信号（保守）：@internal 邮箱 / 含 e2e|test|smoke|drill|demo|dummy 的标识或名字。
const TEST_RE = /(^|[^a-z])(e2e|smoke|drill|dummy)([^a-z]|$)|test|@internal/i;
function isTestSignal(...vals) {
  return vals.some((v) => v && TEST_RE.test(String(v)));
}

const db = new Database(DB_PATH);
db.pragma("journal_mode = WAL");

function hasColumn(table, col) {
  return db.prepare(`PRAGMA table_info(${table})`).all().some((c) => c.name === col);
}
function ensureIsTest(table) {
  if (!hasColumn(table, "is_test")) {
    if (DRY) {
      console.log(`[dry-run] 将为 ${table} 添加列 is_test INTEGER NOT NULL DEFAULT 0`);
    } else {
      db.exec(`ALTER TABLE ${table} ADD COLUMN is_test INTEGER NOT NULL DEFAULT 0`);
      console.log(`已添加列 ${table}.is_test`);
    }
  }
}

console.log(`账本 DB: ${DB_PATH}`);
console.log(`模式: ${DRY ? "DRY-RUN（只看不写）" : "实跑"}`);

for (const t of ["customers", "orders", "leads", "licenses"]) {
  try {
    ensureIsTest(t);
  } catch (e) {
    console.warn(`[warn] ${t} 加列失败（可能已存在）: ${e.message}`);
  }
}

// dry-run 且列还不存在时，用内存标记模拟统计
const canRead = (t) => DRY || hasColumn(t, "is_test");

let marked = { customers: 0, orders: 0, leads: 0, licenses: 0 };

function markTable(table, pickCols, updateWhereCol = null) {
  if (!DRY && !hasColumn(table, "is_test")) return;
  const rows = db.prepare(`SELECT * FROM ${table}`).all();
  const hits = [];
  for (const r of rows) {
    const vals = pickCols.map((c) => r[c]);
    if (isTestSignal(...vals) && Number(r.is_test || 0) !== 1) hits.push(r);
  }
  marked[table] = hits.length;
  if (!DRY && hits.length) {
    const pk = updateWhereCol || "id";
    const upd = db.prepare(`UPDATE ${table} SET is_test = 1 WHERE ${pk} = ?`);
    const tx = db.transaction((list) => {
      for (const r of list) upd.run(r[pk]);
    });
    tx(hits);
  }
  // 打印命中样本（最多 6 条）
  for (const r of hits.slice(0, 6)) {
    const label = r.display_name || r.source_key || r.value || r.id;
    console.log(`  [test] ${table}: ${label}`);
  }
}

// 各表用于判定的字段（按 ledger schema 常见列，缺列自动忽略）+ 各表主键列（orders/leads/
// licenses 自然键是 source_key，customers 是 id）
markTable("customers", ["display_name", "id"], "id");
markTable("orders", ["source_key", "customer_id", "contact", "email"], "source_key");
markTable("leads", ["source_key", "contact", "email", "name"], "source_key");
markTable("licenses", ["source_key", "customer_name"], "source_key");

// 客户联动：某客户名下有测试订单/留资，或其身份含测试信号 → 客户也标测试
if (!DRY && hasColumn("customers", "is_test")) {
  const ids = new Set();
  for (const t of ["orders", "leads", "licenses"]) {
    if (!hasColumn(t, "customer_id") || !hasColumn(t, "is_test")) continue;
    for (const r of db
      .prepare(`SELECT DISTINCT customer_id FROM ${t} WHERE is_test = 1 AND customer_id IS NOT NULL`)
      .all())
      ids.add(r.customer_id);
  }
  if (hasColumn("identities", "value")) {
    for (const r of db.prepare("SELECT customer_id, value FROM identities").all()) {
      if (isTestSignal(r.value)) ids.add(r.customer_id);
    }
  }
  const upd = db.prepare("UPDATE customers SET is_test = 1 WHERE id = ? AND is_test != 1");
  let n = 0;
  const tx = db.transaction((list) => {
    for (const id of list) n += upd.run(id).changes;
  });
  tx([...ids]);
  if (n) {
    marked.customers += n;
    console.log(`  [test] customers 经关联补标 ${n} 条`);
  }
}

console.log("── 摘要 ──────────────────────────────────");
console.log(
  `标测试：customers ${marked.customers} · orders ${marked.orders} · leads ${marked.leads} · licenses ${marked.licenses}`
);
if (!DRY) {
  for (const t of ["customers", "orders", "leads", "licenses"]) {
    if (hasColumn(t, "is_test")) {
      const c = db.prepare(`SELECT COUNT(*) n FROM ${t} WHERE is_test = 1`).get().n;
      const tot = db.prepare(`SELECT COUNT(*) n FROM ${t}`).get().n;
      console.log(`  ${t}: ${c}/${tot} 标为测试`);
    }
  }
}
db.close();
