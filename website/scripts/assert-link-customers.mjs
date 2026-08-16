#!/usr/bin/env node
// 断言：ledger-link-customers.mjs 归并正确性 + 幂等（系统临时目录建一次性账本，
// 造假数据 → dry-run 零写入 → 实跑归并 → 二次实跑零新增，跑完删临时库）。
//   node scripts/assert-link-customers.mjs
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  attachIdentity,
  createCustomer,
  openLedgerDb,
  upsertLeadRow,
  upsertLicenseRow,
  upsertOrderRow,
  newId,
} from "./ledger-lib.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "link-cust-test-"));
const dbPath = path.join(tmpDir, "group-ledger.db");

// ── 造假数据 ────────────────────────────────────────────────────────
let db = openLedgerDb(dbPath);

// 场景 8：既有客户甲（有归属订单，脚本必须绕开）
const cust1 = createCustomer({ display_name: "既有客户甲" }, db, "seed");
upsertOrderRow({ source_key: "AH-20260605-EEEEEE", contact: "@Carol", customer_id: cust1.id }, db);
// 场景 9：既有客户乙已挂 email 身份 → 无主订单应「并入」而非建新档
const cust2 = createCustomer({ display_name: "既有客户乙" }, db, "seed");
attachIdentity(cust2.id, "email", "dave@x.com", db, "seed");
upsertOrderRow({ source_key: "AH-20260606-FFFFFF", contact: "Dave@X.com" }, db);

// 场景 1：同 tg handle 两笔订单 + 一条留资（大小写/空白差异 + tg 数字 id）
upsertOrderRow({ source_key: "AH-20260601-AAAAAA", contact: "@Alice" }, db);
upsertOrderRow({ source_key: "AH-20260602-BBBBBB", contact: " @alice " }, db);
upsertLeadRow(
  { source_key: "tg:12345", name: "Alice王", contact: "@ALICE", raw: JSON.stringify({ tg_user_id: "12345" }) },
  db
);

// 场景 2：同名 license + persona（纯名字弱信号）
upsertLicenseRow(
  { source_system: "avatarhub", source_key: "LIC-001", raw: JSON.stringify({ customer_name: "张三" }) },
  db
);
const prs1 = newId("prs");
db.prepare(
  `INSERT INTO personas (id, customer_id, source_system, source_key, display_name, slot_face, slot_voice, slot_prompt, slot_knowledge, slots_detail, tags, status, created_at, updated_at, synced_at)
   VALUES (?, NULL, 'avatarhub', 'PA-001', '张三的人设', 0, 1, 0, 0, ?, NULL, 'active', ?, ?, ?)`
).run(prs1, JSON.stringify({ voice: { present: true }, _meta: { customer_name: "张三" } }), "2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z");

// 场景 5：email 归并（订单 ↔ c: 留资）
upsertOrderRow({ source_key: "AH-20260603-CCCCCC", contact: "Bob@Mail.com" }, db);
upsertLeadRow({ source_key: "c:bob@mail.com", contact: "bob@mail.com" }, db);

// 场景 6：订单 notify_chat ↔ 留资 tg: 数字 id（contact 均为自由文本，不参与分组）
upsertOrderRow({ source_key: "AH-20260604-DDDDDD", contact: "联系我", notify_chat: "888777" }, db);
upsertLeadRow(
  { source_key: "tg:888777", name: "小明", contact: "小明", raw: JSON.stringify({ tg_user_id: "888777" }) },
  db
);

// 场景 10：无标识 persona → 跳过
const prs2 = newId("prs");
db.prepare(
  `INSERT INTO personas (id, customer_id, source_system, source_key, display_name, slot_face, slot_voice, slot_prompt, slot_knowledge, slots_detail, tags, status, created_at, updated_at, synced_at)
   VALUES (?, NULL, 'avatarhub', 'PA-002', '匿名人设', 0, 0, 0, 0, NULL, NULL, 'active', ?, ?, ?)`
).run(prs2, "2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z", "2026-07-01T00:00:00Z");

const snapshot = (d) => ({
  customers: d.prepare("SELECT COUNT(*) AS c FROM customers").get().c,
  identities: d.prepare("SELECT COUNT(*) AS c FROM identities").get().c,
  audit: d.prepare("SELECT COUNT(*) AS c FROM audit").get().c,
  linkedOrders: d.prepare("SELECT COUNT(*) AS c FROM orders WHERE customer_id IS NOT NULL").get().c,
  linkedLeads: d.prepare("SELECT COUNT(*) AS c FROM leads WHERE customer_id IS NOT NULL").get().c,
  linkedLicenses: d.prepare("SELECT COUNT(*) AS c FROM licenses WHERE customer_id IS NOT NULL").get().c,
  linkedPersonas: d.prepare("SELECT COUNT(*) AS c FROM personas WHERE customer_id IS NOT NULL").get().c,
});
const before = snapshot(db);
db.close();

const run = (extra = []) =>
  execFileSync(process.execPath, [path.join(here, "ledger-link-customers.mjs"), "--db", dbPath, ...extra], {
    encoding: "utf8",
  });

// ── dry-run：零写入 ─────────────────────────────────────────────────
console.log("===== DRY-RUN 输出 =====");
console.log(run(["--dry-run"]));
db = openLedgerDb(dbPath);
const afterDry = snapshot(db);
db.close();

// ── 实跑 ────────────────────────────────────────────────────────────
console.log("===== 实跑输出 =====");
console.log(run());
db = openLedgerDb(dbPath);
const after1 = snapshot(db);
const cid = (sql, ...p) => db.prepare(sql).get(...p)?.customer_id ?? null;
const o1 = cid("SELECT customer_id FROM orders WHERE source_key='AH-20260601-AAAAAA'");
const o2 = cid("SELECT customer_id FROM orders WHERE source_key='AH-20260602-BBBBBB'");
const l1 = cid("SELECT customer_id FROM leads WHERE source_key='tg:12345'");
const lic1 = cid("SELECT customer_id FROM licenses WHERE source_system='avatarhub' AND source_key='LIC-001'");
const p1 = cid("SELECT customer_id FROM personas WHERE source_key='PA-001'");
const p2 = cid("SELECT customer_id FROM personas WHERE source_key='PA-002'");
const o3 = cid("SELECT customer_id FROM orders WHERE source_key='AH-20260603-CCCCCC'");
const l2 = cid("SELECT customer_id FROM leads WHERE source_key='c:bob@mail.com'");
const o4 = cid("SELECT customer_id FROM orders WHERE source_key='AH-20260604-DDDDDD'");
const l3 = cid("SELECT customer_id FROM leads WHERE source_key='tg:888777'");
const o5 = cid("SELECT customer_id FROM orders WHERE source_key='AH-20260605-EEEEEE'");
const o6 = cid("SELECT customer_id FROM orders WHERE source_key='AH-20260606-FFFFFF'");
const aliceIdents = db
  .prepare("SELECT kind, value FROM identities WHERE customer_id = ? ORDER BY kind, value")
  .all(o1)
  .map((r) => `${r.kind}:${r.value}`);
const aliceCust = db.prepare("SELECT display_name, source, tg_user_id FROM customers WHERE id = ?").get(o1);
const nameCust = lic1 ? db.prepare("SELECT display_name, source FROM customers WHERE id = ?").get(lic1) : null;
const nameIdents = lic1 ? db.prepare("SELECT COUNT(*) AS c FROM identities WHERE customer_id = ?").get(lic1).c : -1;
const oppJoin = db
  .prepare(
    "SELECT COUNT(*) AS c FROM personas p JOIN customers c ON c.id = p.customer_id WHERE p.customer_id IS NOT NULL AND p.status = 'active'"
  )
  .get().c;
const linkAudit = db.prepare("SELECT COUNT(*) AS c FROM audit WHERE action='link_customer' AND actor='link-script'").get().c;
db.close();

// ── 二次实跑：幂等 ──────────────────────────────────────────────────
console.log("===== 二次实跑输出（幂等） =====");
const second = run();
console.log(second);
db = openLedgerDb(dbPath);
const after2 = snapshot(db);
db.close();

// ── 断言 ────────────────────────────────────────────────────────────
const checks = [
  ["dry-run 零写入（customers/identities/audit/归属数全不变）", JSON.stringify(afterDry) === JSON.stringify(before)],
  ["实跑新建 4 客户（alice组/张三/bob组/tg888777组），2+4=6", after1.customers === 6],
  ["同 handle 两订单 + tg 留资并成同一客户", !!o1 && o1 === o2 && o1 === l1],
  ["alice 组 display_name 取 lead.name（Alice王）", aliceCust?.display_name === "Alice王"],
  ["alice 组 tg_user_id=12345 · source=link-customers", aliceCust?.tg_user_id === "12345" && aliceCust?.source === "link-customers"],
  [
    "alice 组身份 = contact:@alice + contact:alice + tg:12345",
    JSON.stringify(aliceIdents) === JSON.stringify(["contact:@alice", "contact:alice", "tg:12345"]),
  ],
  ["同名 license + persona 并成同一客户（纯名字）", !!lic1 && lic1 === p1],
  ["名字档 source=link-customers:name 且不登记 identities", nameCust?.source === "link-customers:name" && nameIdents === 0],
  ["email 订单 ↔ c: 留资并成同一客户", !!o3 && o3 === l2],
  ["订单 notify_chat ↔ 留资 tg 数字 id 并成同一客户", !!o4 && o4 === l3],
  ["已归属订单不动（仍属既有客户甲）", o5 === cust1.id],
  ["无主订单按已有 email 身份并入既有客户乙（不建新档）", o6 === cust2.id],
  ["无标识 persona 保持未归属", p2 === null],
  ["回填行都写了 link_customer 审计（10 行）", linkAudit === 10],
  ["商机引擎 JOIN 条件满足（active persona × customers ≥ 1）", oppJoin >= 1],
  ["二次跑零新增客户/身份", after2.customers === after1.customers && after2.identities === after1.identities],
  ["二次跑零审计写入（无任何动作）", after2.audit === after1.audit],
  ["二次跑归属行数不变", JSON.stringify(after2) === JSON.stringify(after1)],
  ["二次跑输出报 0 新建 0 回填", /新建客户 0（强信号 0 \+ 纯名字 0）/.test(second) && /回填行 0:/.test(second)],
];

let failed = 0;
for (const [desc, ok] of checks) {
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${desc}`);
  if (!ok) failed++;
}
fs.rmSync(tmpDir, { recursive: true, force: true });
if (failed) {
  console.error(`== assert-link-customers: ${failed} 项失败 ==`);
  process.exit(1);
}
console.log("== assert-link-customers: 全部通过 ==");
