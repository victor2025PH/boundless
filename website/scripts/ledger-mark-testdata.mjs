#!/usr/bin/env node
// ledger-mark-testdata.mjs — 回扫集团账本存量，把测试/演练数据（e2e/smoke/drill/@internal…）
// 打上 is_test=1（schema v6），让 KPI/商机只算真实数据。幂等可重复跑；绝不删数据、
// 绝不把已标行降回 0（只升不降）。
//
// 判定口径唯一真相：ledger-lib.mjs::isTestSignal（与 lib/ledger.ts 逐字一致）——
// 词边界保守匹配 e2e/test/drill/smoke + 邮箱 @internal 结尾；"latest"/"contest"
// 这类误伤词不命中（宁可漏标不误标，漏标可再跑，误标会把真实数据从 KPI 滤掉）。
//
// 各表判定字段（只看内容列，不看自然键里的日期/随机段）：
//   customers: display_name / primary_contact / 名下 identities.value
//   orders:    contact / fingerprint
//   leads:     source_key / contact / name
//   licenses:  source_key / raw 里的 customer_name & customer_contact
// 客户联动：名下 orders/leads/licenses 有任一测试行 → 客户补标（e2e 联系方式都是
// 假值，正常归并不会挂到真实客户名下；联动名单全部打印供人工复核）。
//
// 用法：node scripts/ledger-mark-testdata.mjs [--db group-ledger.db] [--dry-run]
//   --dry-run  只读打开（不迁 schema、不写库），打印将标记的行与计数。
//   实跑用 openLedgerDb：库自动迁到 schema v6（is_test 列条件式 ALTER，幂等）。
import fs from "node:fs";
import path from "node:path";
import Database from "better-sqlite3";
import { isTestSignal, openLedgerDb, resolveLedgerDbPath, writeAudit } from "./ledger-lib.mjs";

const ACTOR = "mark-testdata";

function parseArgs(argv) {
  const args = { db: null, dryRun: false };
  for (let i = 2; i < argv.length; i++) {
    const a = argv[i];
    if (a === "--db") args.db = argv[++i];
    else if (a === "--dry-run") args.dryRun = true;
    else if (a === "--help" || a === "-h") {
      console.log("用法: node scripts/ledger-mark-testdata.mjs [--db group-ledger.db] [--dry-run]");
      process.exit(0);
    } else {
      console.error(`未知参数: ${a}（--help 查看用法）`);
      process.exit(1);
    }
  }
  return args;
}

function parseJson(text) {
  if (!text) return null;
  try {
    const v = JSON.parse(text);
    return v && typeof v === "object" ? v : null;
  } catch {
    return null;
  }
}

function main() {
  const args = parseArgs(process.argv);
  const dry = args.dryRun;
  const file = path.resolve(args.db || resolveLedgerDbPath());

  let db;
  if (dry) {
    if (!fs.existsSync(file)) {
      console.error(`库文件不存在: ${file}`);
      process.exit(1);
    }
    db = new Database(file, { readonly: true, fileMustExist: true });
  } else {
    db = openLedgerDb(file); // 自动迁 schema v6（is_test 列就位）
  }
  console.log(`账本 DB: ${file}`);
  console.log(`模式: ${dry ? "DRY-RUN（只读，不迁 schema 不写库）" : "实跑（写库）"}`);

  const hasCol = (t, c) => db.prepare(`PRAGMA table_info(${t})`).all().some((x) => x.name === c);
  if (dry && !hasCol("orders", "is_test")) {
    console.log("[提示] 库尚无 is_test 列（schema < v6）：实跑会自动迁移加列；下方按全量未标估算。");
  }
  // 兼容 dry-run 打在未迁移旧库上：无列时当作全 0
  const val = (r, col) => (col in r ? Number(r[col] ?? 0) : 0);

  const plan = { customers: [], orders: [], leads: [], licenses: [] };

  // ── orders：contact / fingerprint ──
  for (const r of db.prepare("SELECT * FROM orders").all()) {
    if (val(r, "is_test") === 1) continue;
    if (isTestSignal(r.contact, r.fingerprint)) plan.orders.push({ key: r.source_key, label: `${r.source_key} · ${r.contact ?? ""}` });
  }

  // ── leads：source_key / contact / name ──
  for (const r of db.prepare("SELECT * FROM leads").all()) {
    if (val(r, "is_test") === 1) continue;
    if (isTestSignal(r.source_key, r.contact, r.name)) plan.leads.push({ key: r.source_key, label: `${r.source_key} · ${r.contact ?? ""}` });
  }

  // ── licenses：source_key / raw.customer_name / raw.customer_contact（导入器把原始
  //    记录留在 raw：顶层或再嵌一层 raw.raw 都探，与 ledger-link-customers.mjs 同款）──
  for (const r of db.prepare("SELECT * FROM licenses").all()) {
    if (val(r, "is_test") === 1) continue;
    const raw = parseJson(r.raw);
    const name = raw?.customer_name ?? raw?.raw?.customer_name ?? null;
    const contact = raw?.customer_contact ?? raw?.raw?.customer_contact ?? null;
    if (isTestSignal(r.source_key, name, contact)) plan.licenses.push({ key: r.id, label: `${r.source_system}:${r.source_key}` });
  }

  // ── customers：display_name / primary_contact / 名下 identities.value ──
  const identsByCust = new Map();
  for (const r of db.prepare("SELECT customer_id, value FROM identities").all()) {
    let arr = identsByCust.get(r.customer_id);
    if (!arr) identsByCust.set(r.customer_id, (arr = []));
    arr.push(r.value);
  }
  const custDirect = new Set();
  for (const r of db.prepare("SELECT * FROM customers").all()) {
    if (val(r, "is_test") === 1) continue;
    if (isTestSignal(r.display_name, r.primary_contact, ...(identsByCust.get(r.id) ?? []))) {
      custDirect.add(r.id);
      plan.customers.push({ key: r.id, label: `${r.id} 「${r.display_name ?? "（未命名）"}」`, via: "self" });
    }
  }

  // ── 客户联动：名下有本轮或既往标测试的 orders/leads/licenses → 客户补标 ──
  const linkedIds = new Set();
  const planKeys = {
    orders: new Set(plan.orders.map((x) => x.key)),
    leads: new Set(plan.leads.map((x) => x.key)),
    licenses: new Set(plan.licenses.map((x) => x.key)),
  };
  const linkedFrom = (table, keyCol) => {
    for (const r of db.prepare(`SELECT * FROM ${table} WHERE customer_id IS NOT NULL`).all()) {
      if (val(r, "is_test") === 1 || planKeys[table].has(r[keyCol])) linkedIds.add(r.customer_id);
    }
  };
  linkedFrom("orders", "source_key");
  linkedFrom("leads", "source_key");
  linkedFrom("licenses", "id");
  const custRow = db.prepare("SELECT * FROM customers WHERE id = ?");
  for (const cid of linkedIds) {
    if (custDirect.has(cid)) continue;
    const c = custRow.get(cid);
    if (!c || val(c, "is_test") === 1) continue;
    plan.customers.push({ key: cid, label: `${cid} 「${c.display_name ?? "（未命名）"}」`, via: "linked" });
  }

  // ── 输出计划 ──
  for (const [table, rows] of Object.entries(plan)) {
    for (const x of rows) {
      console.log(`[${dry ? "计划" : "标记"}] ${table}: ${x.label}${x.via === "linked" ? "（名下测试行联动，请复核）" : ""}`);
    }
  }

  // ── 写库（只升不降；每表一条 audit 汇总，不逐行刷审计量）──
  if (!dry) {
    const tx = db.transaction(() => {
      for (const x of plan.orders) db.prepare("UPDATE orders SET is_test = 1 WHERE source_key = ? AND is_test = 0").run(x.key);
      for (const x of plan.leads) db.prepare("UPDATE leads SET is_test = 1 WHERE source_key = ? AND is_test = 0").run(x.key);
      for (const x of plan.licenses) db.prepare("UPDATE licenses SET is_test = 1 WHERE id = ? AND is_test = 0").run(x.key);
      for (const x of plan.customers) db.prepare("UPDATE customers SET is_test = 1 WHERE id = ? AND is_test = 0").run(x.key);
      for (const [table, rows] of Object.entries(plan)) {
        if (!rows.length) continue;
        writeAudit(
          {
            actor: ACTOR,
            action: "mark_testdata",
            entity: table,
            entity_id: `${rows.length} rows`,
            detail: { keys: rows.map((x) => x.key).slice(0, 50), total: rows.length },
          },
          db
        );
      }
    });
    tx();
  }

  // ── 摘要 ──
  console.log("── 摘要 ──────────────────────────────────");
  console.log(
    `${dry ? "（DRY-RUN 计划值，未写库）" : ""}新标测试: customers ${plan.customers.length} · orders ${plan.orders.length} · leads ${plan.leads.length} · licenses ${plan.licenses.length}`
  );
  if (!dry) {
    for (const t of ["customers", "orders", "leads", "licenses"]) {
      const c = db.prepare(`SELECT COUNT(*) AS n FROM ${t} WHERE is_test = 1`).get().n;
      const tot = db.prepare(`SELECT COUNT(*) AS n FROM ${t}`).get().n;
      console.log(`  ${t}: ${c}/${tot} 已标测试`);
    }
  }
  db.close();
}

main();
