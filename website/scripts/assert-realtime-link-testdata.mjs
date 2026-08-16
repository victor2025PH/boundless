#!/usr/bin/env node
// 断言：实施22 —— ① 客户归并实时化（ensureCustomerFor* 的 create-on-miss）
// ② 测试数据标记（is_test 出生标 + 回扫 + KPI 排除）。
// 系统临时目录建一次性账本，造混合数据（真实 tg/handle 单 + e2e@internal 单），
// 走与生产钩子完全相同的 orderEntryToRow/leadEntryToRow → upsert → ensure* 路径，
// 再跑 ledger-mark-testdata.mjs 回扫与 kpi-weekly-report.mjs 排除口径，跑完删临时库。
//   node scripts/assert-realtime-link-testdata.mjs
import { execFileSync } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  createCustomer,
  attachIdentity,
  ensureCustomerForLead,
  ensureCustomerForOrder,
  leadEntryToRow,
  openLedgerDb,
  orderEntryToRow,
  upsertLeadRow,
  upsertOrderRow,
} from "./ledger-lib.mjs";

const here = path.dirname(fileURLToPath(import.meta.url));
const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "rt-link-test-"));
const dbPath = path.join(tmpDir, "group-ledger.db");

const checks = [];
const ok = (desc, cond) => checks.push([desc, !!cond]);

// ── 模拟生产双写钩子：syncOrderEntry / syncLeadEntry 的库内两步 ──
function syncOrder(db, entry) {
  upsertOrderRow(orderEntryToRow(entry), db);
  return ensureCustomerForOrder(entry.id, db);
}
function syncLead(db, entry) {
  upsertLeadRow(leadEntryToRow(entry), db);
  return ensureCustomerForLead(entry.id, db);
}

let db = openLedgerDb(dbPath);

// ── 0) schema v6 迁移到位 ──
const schemaVersion = db.prepare("SELECT value FROM meta WHERE key='schema_version'").get()?.value;
ok("新库迁移到 schema v6", schemaVersion === "6");
const hasCol = (t) => db.prepare(`PRAGMA table_info(${t})`).all().some((c) => c.name === "is_test");
ok("customers/orders/leads/licenses 都有 is_test 列", ["customers", "orders", "leads", "licenses"].every(hasCol));

// ── 1) 真实 tg handle 单：无主 → 实时建档 + 回填 ──
const realOrder = {
  id: "AH-20260718-REAL01", t: "2026-07-18T05:00:00Z", status: "paid", plan: "translate-pro",
  edition: "pro", period: "monthly", amount: 249, pay_amount: 249.17, currency: "USDT",
  contact: "@RealGuy", fingerprint: "FPREAL01", lang: "zh", paid_at: "2026-07-18T05:10:00Z",
};
const cidReal = syncOrder(db, realOrder);
const realRow = db.prepare("SELECT * FROM orders WHERE source_key='AH-20260718-REAL01'").get();
const realCust = cidReal ? db.prepare("SELECT * FROM customers WHERE id=?").get(cidReal) : null;
ok("真实 tg handle 单：实时建档（返回 cust_ id）", !!cidReal && cidReal.startsWith("cust_"));
ok("真实单回填 customer_id", realRow?.customer_id === cidReal);
ok("真实客户 is_test=0", realCust?.is_test === 0);
ok("真实订单 is_test=0", realRow?.is_test === 0);
ok("真实客户 source=auto:order-lead · display=@realguy", realCust?.source === "auto:order-lead" && realCust?.display_name === "@realguy");
const realIdents = db.prepare("SELECT kind, value FROM identities WHERE customer_id=? ORDER BY kind, value").all(cidReal);
ok(
  "真实客户身份 = contact:@realguy + contact:realguy（原文+规范化 handle）",
  JSON.stringify(realIdents.map((r) => `${r.kind}:${r.value}`)) === JSON.stringify(["contact:@realguy", "contact:realguy"])
);
const auditCreate = db
  .prepare("SELECT COUNT(*) AS c FROM audit WHERE action='auto_create' AND entity='order' AND entity_id='AH-20260718-REAL01'")
  .get().c;
ok("实时建档写 auto_create 审计", auditCreate === 1);

// ── 2a) e2e@internal 单（生产 e2e 脚本同款 contact）：订单出生即标测试；
//        @internal 缺 TLD 不是合法 email 形态 → 非强信号，不建档（测试数据不污染客户表）──
const e2eOrder = {
  id: "AH-20260718-E2E001", t: "2026-07-18T06:00:00Z", status: "pending", plan: "pro",
  edition: "pro", period: "monthly", amount: 249, pay_amount: 249.31, currency: "USDT",
  contact: "e2e-SLA-test@internal", fingerprint: "", lang: "zh",
};
const cidE2e = syncOrder(db, e2eOrder);
const e2eRow = db.prepare("SELECT * FROM orders WHERE source_key='AH-20260718-E2E001'").get();
ok("e2e@internal 订单出生即 is_test=1", e2eRow?.is_test === 1);
ok("e2e@internal 非合法 email 形态：不建档（无主，客户表不进测试垃圾）", cidE2e === null && e2eRow?.customer_id === null);

// ── 2b) 合法 email 形态的测试联系方式：建档路径走通且客户出生即标测试 ──
const smokeOrder = {
  id: "AH-20260718-SMOKE1", t: "2026-07-18T06:30:00Z", status: "pending", plan: "voice",
  edition: "basic", period: "monthly", amount: 99, pay_amount: 99.44, currency: "USDT",
  contact: "smoke-qa@test.example.com", fingerprint: "", lang: "zh",
};
const cidSmoke = syncOrder(db, smokeOrder);
const smokeRow = db.prepare("SELECT * FROM orders WHERE source_key='AH-20260718-SMOKE1'").get();
const smokeCust = cidSmoke ? db.prepare("SELECT * FROM customers WHERE id=?").get(cidSmoke) : null;
ok("smoke 测试单（合法 email）：实时建档 + 回填", !!cidSmoke && smokeRow?.customer_id === cidSmoke);
ok("smoke 订单出生即 is_test=1", smokeRow?.is_test === 1);
ok("smoke 客户建档即 is_test=1", smokeCust?.is_test === 1);

// ── 3) 真实 tg 留资 → 建档；随后同 tg 的订单（notify_chat）并入同一客户，不新建 ──
const tgLead = {
  id: "tg:987654", t: "2026-07-18T07:00:00Z", status: "new", name: "老王", contact: "老王微信",
  interest: "digital-human", message: "想做直播分身", lang: "zh", source: "telegram",
  firstSeen: "2026-07-18T07:00:00Z", lastSeen: "2026-07-18T07:00:00Z", count: 1, tg_user_id: "987654",
};
const cidLead = syncLead(db, tgLead);
const leadRow = db.prepare("SELECT * FROM leads WHERE source_key='tg:987654'").get();
ok("真实 tg 留资：实时建档（tg id 信号）", !!cidLead && cidLead.startsWith("cust_"));
ok("留资回填 customer_id · is_test=0 · display=老王", leadRow?.customer_id === cidLead && leadRow?.is_test === 0 && db.prepare("SELECT display_name FROM customers WHERE id=?").get(cidLead)?.display_name === "老王");
const custCountBefore = db.prepare("SELECT COUNT(*) AS c FROM customers").get().c;
const followOrder = {
  id: "AH-20260718-FOLLOW", t: "2026-07-18T08:00:00Z", status: "pending", plan: "realtime-creator",
  edition: "creator", period: "monthly", amount: 999, pay_amount: 999.42, currency: "USDT",
  contact: "老王微信", fingerprint: "", lang: "zh", notify_chat: "987654",
};
const cidFollow = syncOrder(db, followOrder);
ok("同 tg 客户的后续订单：auto_link 并入（不新建）", cidFollow === cidLead && db.prepare("SELECT COUNT(*) AS c FROM customers").get().c === custCountBefore);
const followAudit = db
  .prepare("SELECT COUNT(*) AS c FROM audit WHERE action='auto_link' AND entity_id='AH-20260718-FOLLOW'")
  .get().c;
ok("并入走 auto_link 审计（非 auto_create）", followAudit === 1);

// ── 4) 自由文本联系方式：不建档（防垃圾档） ──
const vagueOrder = {
  id: "AH-20260718-VAGUE1", t: "2026-07-18T09:00:00Z", status: "pending", plan: "voice",
  edition: "basic", period: "monthly", amount: 99, pay_amount: 99.55, currency: "USDT",
  contact: "找小李", fingerprint: "", lang: "zh",
};
const cidVague = syncOrder(db, vagueOrder);
ok("自由文本 contact 不建档（返回 null，行保持无主）", cidVague === null && db.prepare("SELECT customer_id FROM orders WHERE source_key='AH-20260718-VAGUE1'").get().customer_id === null);

// ── 5) 已归属行：ensure* 幂等跳过 ──
const cidAgain = ensureCustomerForOrder("AH-20260718-REAL01", db);
ok("已归属订单再跑 ensure：原样返回不重建", cidAgain === cidReal);

// ── 6) 二次双写（JSON 镜像重放）：零新增客户/身份，is_test 不被抹掉 ──
const snap = () => ({
  customers: db.prepare("SELECT COUNT(*) AS c FROM customers").get().c,
  identities: db.prepare("SELECT COUNT(*) AS c FROM identities").get().c,
  testOrders: db.prepare("SELECT COUNT(*) AS c FROM orders WHERE is_test=1").get().c,
});
const s1 = snap();
syncOrder(db, realOrder);
syncOrder(db, e2eOrder);
syncLead(db, tgLead);
const s2 = snap();
ok("双写重放幂等：客户/身份/测试标全不变", JSON.stringify(s1) === JSON.stringify(s2));

// upsert 不带 is_test 的重放（模拟旧调用方）不清标：MAX 语义
upsertOrderRow({ source_key: "AH-20260718-E2E001", plan: "pro", contact: "e2e-SLA-test@internal" }, db);
ok("upsert 缺省 is_test 重放不清已有测试标（MAX 只升不降）", db.prepare("SELECT is_test FROM orders WHERE source_key='AH-20260718-E2E001'").get().is_test === 1);

// ── 7) 回扫脚本：存量未标数据（绕过出生标直接落库）→ dry-run 零写入 → 实跑补标 → 幂等 ──
// 存量测试单：显式 is_test:0 落库（模拟迁移前旧行），并手工归到一个真实建的客户
const legacyCust = createCustomer({ display_name: "smoke 演练号", source: "seed" }, db, "seed");
attachIdentity(legacyCust.id, "email", "smoke-drill@internal", db, "seed");
upsertOrderRow(
  { source_key: "AH-20260601-LEGACY", contact: "smoke-drill@internal", customer_id: legacyCust.id, plan: "pro", is_test: 0 },
  db
);
ok("存量演练单落库时未标（is_test=0）", db.prepare("SELECT is_test FROM orders WHERE source_key='AH-20260601-LEGACY'").get().is_test === 0);
db.close();

const run = (script, extra = []) =>
  execFileSync(process.execPath, [path.join(here, script), ...extra], { encoding: "utf8" });

console.log("===== ledger-mark-testdata --dry-run 输出 =====");
const dryOut = run("ledger-mark-testdata.mjs", ["--db", dbPath, "--dry-run"]);
console.log(dryOut);
db = openLedgerDb(dbPath);
ok("dry-run 零写入（LEGACY 单仍未标）", db.prepare("SELECT is_test FROM orders WHERE source_key='AH-20260601-LEGACY'").get().is_test === 0);
db.close();

console.log("===== ledger-mark-testdata 实跑输出 =====");
console.log(run("ledger-mark-testdata.mjs", ["--db", dbPath]));
db = openLedgerDb(dbPath);
ok("实跑后 LEGACY 单补标 is_test=1", db.prepare("SELECT is_test FROM orders WHERE source_key='AH-20260601-LEGACY'").get().is_test === 1);
ok("LEGACY 客户联动补标（smoke 名 + 名下测试单）", db.prepare("SELECT is_test FROM customers WHERE id=?").get(legacyCust.id).is_test === 1);
ok(
  "真实客户不被误标（@realguy / 老王 保持 is_test=0；测试客户恰 2 个）",
  db.prepare("SELECT COUNT(*) AS c FROM customers WHERE is_test=1").get().c === 2 &&
    db.prepare("SELECT is_test FROM customers WHERE id=?").get(cidReal).is_test === 0 &&
    db.prepare("SELECT is_test FROM customers WHERE id=?").get(cidLead).is_test === 0
);
ok("真实订单不被误标（REAL01/FOLLOW/VAGUE1 保持 is_test=0）", db.prepare("SELECT COUNT(*) AS c FROM orders WHERE is_test=0").get().c === 3);
const markSnap1 = {
  c: db.prepare("SELECT COUNT(*) AS c FROM customers WHERE is_test=1").get().c,
  o: db.prepare("SELECT COUNT(*) AS c FROM orders WHERE is_test=1").get().c,
  l: db.prepare("SELECT COUNT(*) AS c FROM leads WHERE is_test=1").get().c,
  aud: db.prepare("SELECT COUNT(*) AS c FROM audit").get().c,
};
db.close();

console.log("===== ledger-mark-testdata 二次实跑（幂等） =====");
const secondMark = run("ledger-mark-testdata.mjs", ["--db", dbPath]);
console.log(secondMark);
db = openLedgerDb(dbPath);
const markSnap2 = {
  c: db.prepare("SELECT COUNT(*) AS c FROM customers WHERE is_test=1").get().c,
  o: db.prepare("SELECT COUNT(*) AS c FROM orders WHERE is_test=1").get().c,
  l: db.prepare("SELECT COUNT(*) AS c FROM leads WHERE is_test=1").get().c,
  aud: db.prepare("SELECT COUNT(*) AS c FROM audit").get().c,
};
ok("回扫二次跑零新增（含零审计写入）", JSON.stringify(markSnap1) === JSON.stringify(markSnap2));
ok("回扫二次跑输出报 0", /新标测试: customers 0 · orders 0 · leads 0 · licenses 0/.test(secondMark));

// ── 8) 商机引擎输入口径：测试客户集合正确（lib/opportunities.ts 按此集合过滤输出） ──
const testCustIds = db.prepare("SELECT id FROM customers WHERE COALESCE(is_test,0)=1 ORDER BY id").all().map((r) => r.id);
ok("商机过滤集合 = {smoke 建档客户, legacy 演练客户}", JSON.stringify(testCustIds) === JSON.stringify([cidSmoke, legacyCust.id].sort()));
db.close();

// ── 9) KPI 周报（真脚本，只读）：排除 is_test 后的漏斗数 ──
console.log("===== kpi-weekly-report（窗口含全部造数） =====");
const kpiJson = execFileSync(
  process.execPath,
  [path.join(here, "kpi-weekly-report.mjs"), "--since", "2026-06-01", "--until", "2026-07-19", "--format", "json"],
  { encoding: "utf8", env: { ...process.env, LEDGER_DB: dbPath, EVENTS_DB: path.join(tmpDir, "no-events.db") } }
);
const kpi = JSON.parse(kpiJson);
// 窗口内订单 6 笔：REAL01 / E2E001 / SMOKE1 / FOLLOW / VAGUE1 / LEGACY，
// 其中 E2E001 + SMOKE1 + LEGACY 是测试 → KPI 只算 3 笔真实
ok("KPI 订单数排除测试（6 笔实存，KPI=3）", kpi.funnel.orders?.cur === 3);
ok("KPI 支付数排除测试（paid 仅 REAL01）", kpi.funnel.paid?.cur === 1);
ok("KPI 线索数（真实 tg 留资 1 条）", kpi.funnel.leads?.cur === 1);
ok("KPI 健康区回报测试排除口径（orders 3 · leads 0）", kpi.health.test_excluded?.orders === 3 && kpi.health.test_excluded?.leads === 0);

// ── 10) 两侧 DDL/迁移一致性（ts ↔ mjs 文本对比）──
// 磁盘行尾两文件不同（git 索引即如此：ledger.ts CRLF / ledger-lib.mjs LF），
// 「逐字一致」按行尾归一后的文本比较。
const norm = (t) => t?.replace(/\r\n/g, "\n") ?? null;
const tsSrc = norm(fs.readFileSync(path.join(here, "..", "lib", "ledger.ts"), "utf8"));
const mjsSrc = norm(fs.readFileSync(path.join(here, "ledger-lib.mjs"), "utf8"));
const pull = (src, name) => {
  const m = src.match(new RegExp(`const ${name} = \`([\\s\\S]*?)\`;`));
  return m ? m[1] : null;
};
let ddlSame = true;
for (const n of ["DDL_V1", "DDL_V2", "DDL_V3", "DDL_V4", "DDL_V5"]) {
  if (!pull(tsSrc, n) || pull(tsSrc, n) !== pull(mjsSrc, n)) {
    ddlSame = false;
    console.log(`  [DDL 不一致] ${n}`);
  }
}
ok("DDL_V1..V5 两侧逐字一致（行尾归一后）", ddlSame);
const pullRe = (src) => src.match(/const TEST_SIGNAL_RE = (.+);/)?.[1];
ok("isTestSignal 正则两侧逐字一致", !!pullRe(tsSrc) && pullRe(tsSrc) === pullRe(mjsSrc));
// migrateV6：去 TS 类型标注（参数注解 / as 断言及其必需括号）后逐字比对
const pullV6Body = (src) =>
  src
    .match(/function migrateV6\([^)]*\) \{([\s\S]*?)\n\}/)?.[1]
    .replace(/\(t: string, c: string\)/g, "(t, c)")
    .replace(/\((d\.prepare\(`PRAGMA table_info\(\$\{t\}\)`\)\.all\(\)) as \{ name: string \}\[\]\)/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
ok("migrateV6 迁移体两侧一致（去 TS 类型标注后）", !!pullV6Body(tsSrc) && pullV6Body(tsSrc) === pullV6Body(mjsSrc));
const version = (src) => src.match(/LEDGER_SCHEMA_VERSION = (\d+)/)?.[1];
ok("LEDGER_SCHEMA_VERSION 两侧同为 6", version(tsSrc) === "6" && version(mjsSrc) === "6");

// ── 汇总 ──
let failed = 0;
console.log("===== 断言结果 =====");
for (const [desc, pass] of checks) {
  console.log(`  ${pass ? "PASS" : "FAIL"}  ${desc}`);
  if (!pass) failed++;
}
fs.rmSync(tmpDir, { recursive: true, force: true });
if (failed) {
  console.error(`== assert-realtime-link-testdata: ${failed} 项失败 ==`);
  process.exit(1);
}
console.log("== assert-realtime-link-testdata: 全部通过 ==");
