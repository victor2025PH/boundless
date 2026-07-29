/**
 * 纯函数冒烟：npx tsx lib/tg-cred-pool.test.ts（不依赖 Next runtime）。
 * env + DB 路径必须在动态 import 前设好。
 */
import assert from "assert";
import fs from "fs";
import os from "os";
import path from "path";

process.env.AI_GATEWAY_QUOTA_DB = path.join(
  fs.mkdtempSync(path.join(os.tmpdir(), "tgpool-")),
  "gw.db"
);

async function main() {
  // 池未配 → 禁用
  delete process.env.POOL_TG_CREDS;
  let pool = await import("./tg-cred-pool");
  assert.equal(pool.poolEnabled(), false);
  assert.deepEqual(pool.assignCred("AAAA-BBBB-CCCC-DDDD"), { ok: false, error: "pool_disabled" });

  // 配两组：api-A max=2，api-B max=1
  process.env.POOL_TG_CREDS = JSON.stringify([
    { api_id: "1001", api_hash: "a".repeat(32), max: 2, name: "grp-A" },
    { api_id: "2002", api_hash: "b".repeat(32), max: 1, name: "grp-B" },
  ]);
  // 重新 import（模块级无缓存的凭据，loadPoolCreds 每次读 env，但 tsx 缓存模块——
  // 本模块把 loadPoolCreds 设计为每次读 env，故同一模块实例即可）
  assert.equal(pool.poolEnabled(), true);

  // 三台机器：least-used 分配 → A,B,A（A 容量 2、B 容量 1）
  const r1 = pool.assignCred("MID0-0000-0000-0001");
  const r2 = pool.assignCred("MID0-0000-0000-0002");
  const r3 = pool.assignCred("MID0-0000-0000-0003");
  assert.ok(r1.ok && r2.ok && r3.ok);
  const ids = [r1, r2, r3].map((r) => (r.ok ? r.api_id : "")).sort();
  assert.deepEqual(ids, ["1001", "1001", "2002"]); // A×2 + B×1

  // 第 4 台 → 全满 → pool_full
  const r4 = pool.assignCred("MID0-0000-0000-0004");
  assert.deepEqual(r4, { ok: false, error: "pool_full" });

  // 粘定：同机重领拿回同一组（reused）
  const again = pool.assignCred("MID0-0000-0000-0001");
  assert.ok(again.ok && again.reused && again.api_id === (r1.ok ? r1.api_id : ""));

  // 脏指纹拒
  assert.deepEqual(pool.assignCred("x"), { ok: false, error: "bad_fingerprint" });

  // 观测：api_hash 绝不出现
  const st = pool.poolStats();
  assert.equal(st.enabled, true);
  assert.equal(st.total_used, 3);
  assert.equal(st.total_cap, 3);
  const dump = JSON.stringify(st);
  assert.ok(!dump.includes("a".repeat(32)) && !dump.includes("b".repeat(32)));

  console.log("tg-cred-pool.test.ts OK");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
