/**
 * 试用→付费聚合冒烟：npx tsx lib/trial-paid-funnel.test.ts（不依赖 Next runtime）。
 * env（LEADS_DIR / LEDGER_DB）必须在动态 import 前设好。
 */
import assert from "assert";
import fs from "fs";
import os from "os";
import path from "path";

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "tpf-"));
process.env.LEADS_DIR = TMP;
process.env.LEDGER_DB = path.join(TMP, "group-ledger.db");

async function main() {
  const claims = await import("./trial-claim-store");
  const fpPaidByFp = "AAAA-BBBB-CCCC-1001";
  const fpPaidByContact = "AAAA-BBBB-CCCC-1002";
  const fpPaidByIdentity = "AAAA-BBBB-CCCC-1003";
  const fpUnpaid = "AAAA-BBBB-CCCC-1004";
  for (const [fp, contact] of [
    [fpPaidByFp, "a@x.com"],
    [fpPaidByContact, "@PaidTg"],
    [fpPaidByIdentity, "c@x.com"],
    [fpUnpaid, "d@x.com"],
  ] as Array<[string, string]>) {
    const r = await claims.createClaim({ fingerprint: fp, contact });
    assert.ok(r.ok, `createClaim 失败: ${JSON.stringify(r)}`);
  }

  const mod = await import("./trial-paid-funnel");

  // ① 空库态（营收前常态；getLedgerDb 自带 mkdir 自愈，「文件缺席」在网站侧
  //    不构成真实状态）：库开着、零付费订单 → paid=0 / rate=0（诚实零，不是装 0）
  const empty = await mod.trialPaidFunnel(7);
  assert.equal(empty.ledger_present, true);
  assert.equal(empty.claimed, 4);
  assert.equal(empty.paid, 0);
  assert.equal(empty.rate, 0);

  // ② 插入付费订单：三条 join 路径各中一条 + 未付一条
  const { getLedgerDb } = await import("./ledger");
  const db = getLedgerDb();
  const ins = db.prepare(
    "INSERT INTO orders(id, source_key, customer_id, contact, fingerprint, " +
    "status, created_at, paid_at) VALUES (?,?,?,?,?,?,?,?)");
  const now = new Date().toISOString();
  ins.run("o1", "src-1", "", "", fpPaidByFp, "paid", now, now);
  ins.run("o2", "src-2", "", "paidtg", "", "paid", now, now);          // 归一后命中 @PaidTg
  ins.run("o3", "src-3", "cust-9", "", "", "paid", now, now);          // 经身份归并
  ins.run("o4", "src-4", "", "nobody@else.com", "ZZZZ-0000", "pending",
          now, "");                                                    // 未付＝不算
  db.prepare(
    "INSERT INTO customers(id, display_name, created_at) VALUES (?,?,?)"
  ).run("cust-9", "身份归并客户", now);
  db.prepare(
    "INSERT INTO identities(customer_id, kind, value, created_at) VALUES (?,?,?,?)"
  ).run("cust-9", "fingerprint", fpPaidByIdentity, now);

  const w = await mod.trialPaidFunnel(7);
  assert.equal(w.ledger_present, true);
  assert.equal(w.claimed, 4);
  assert.equal(w.paid, 3, `应 3 条转化，实际 ${w.paid} (${JSON.stringify(w.matched)})`);
  assert.deepEqual(w.matched, { fingerprint: 1, contact: 1, identity: 1 });
  assert.equal(w.rate, 75.0);

  // ③ 窗口语义：0 天窗（sinceMs=now）→ claimed 0 → rate null
  const w0 = await mod.trialPaidFunnel(0);
  assert.equal(w0.claimed, 0);
  assert.equal(w0.rate, null);

  console.log("trial-paid-funnel 冒烟全绿 ✔");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
