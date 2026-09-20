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

  // ── 无感换发：举报 → 换组 → 阈值隔离（2026-08-10 API_ID_INVALID 事故闭环）──
  // 扩容 C 组给换发腾位（A 满员 B 满员）
  process.env.POOL_TG_CREDS = JSON.stringify([
    { api_id: "1001", api_hash: "a".repeat(32), max: 2, name: "grp-A" },
    { api_id: "2002", api_hash: "b".repeat(32), max: 1, name: "grp-B" },
    { api_id: "3003", api_hash: "c".repeat(32), max: 9, name: "grp-C" },
  ]);
  process.env.POOL_TG_QUARANTINE_THRESHOLD = "2";

  // least-used 序回顾：0001→A、0002→B、0003→A（上一段已断言 A×2+B×1）

  // 没被分到 A 组的机器谎报 A 废 → 拒收（防任何持令牌客户端逐组打烊）
  const lie = pool.reportInvalid("MID0-0000-0000-0002", "1001"); // 0002 粘在 B
  assert.deepEqual(lie, { accepted: false, reason: "not_assigned", quarantined: false, distinct: 0 });

  // 真被分到 A 的 0001 举报 → 接受、删粘定；重分配须排除 A → 落 C
  const rep1 = pool.reportInvalid("MID0-0000-0000-0001", "1001");
  assert.ok(rep1.accepted && !rep1.quarantined && rep1.distinct === 1);
  const re1 = pool.assignCred("MID0-0000-0000-0001", { excludeApiId: "1001" });
  assert.ok(re1.ok && re1.api_id === "3003", `换发应落 C，实际 ${JSON.stringify(re1)}`);

  // 第二台不同机器（0003 也粘在 A）举报 → 达阈值 2 → A 整组隔离
  const rep2 = pool.reportInvalid("MID0-0000-0000-0003", "1001");
  assert.ok(rep2.accepted && rep2.quarantined && rep2.distinct === 2);
  assert.ok(pool.quarantinedIds().has("1001"));

  // 隔离后：新机器绝不会再分到 A（A 刚被腾空，least-used 本该首选它）
  const fresh = pool.assignCred("MID0-0000-0000-0005");
  assert.ok(fresh.ok && fresh.api_id !== "1001", `隔离组仍被派发: ${JSON.stringify(fresh)}`);

  // 健康组的既有粘定不受隔离波及（0002 仍拿回 B，reused）
  const stick = pool.assignCred("MID0-0000-0000-0002");
  assert.ok(stick.ok && stick.reused && stick.api_id === "2002");

  // 观测：poolStats 出隔离态
  const st2 = pool.poolStats();
  assert.equal(st2.quarantined_groups, 1);
  assert.ok(st2.groups.find((g) => g.name === "grp-A")?.quarantined);

  // 解除隔离（运营核实误报后）→ 可重新派发
  assert.ok(pool.unquarantine("1001"));
  assert.equal(pool.poolStats().quarantined_groups, 0);

  // ── P2-⑨ 组级出口 + 智能派发（tg_direct→preferProxy）──────────────────────
  // 全新库避免与上文粘定/隔离状态纠缠
  process.env.AI_GATEWAY_QUOTA_DB = path.join(
    fs.mkdtempSync(path.join(os.tmpdir(), "tgpool2-")), "gw.db");
  const pool2 = await import(`./tg-cred-pool?p2=${Date.now()}`);
  process.env.POOL_TG_CREDS = JSON.stringify([
    { api_id: "5001", api_hash: "a".repeat(32), max: 50, name: "direct" },
    { api_id: "5002", api_hash: "b".repeat(32), max: 50, name: "viaproxy",
      proxy: { scheme: "socks5", host: "1.2.3.4", port: 1080, username: "u", password: "p" } },
    { api_id: "5003", api_hash: "c".repeat(32), max: 50, name: "halfproxy",
      proxy: { host: "", port: 0 } }, // 半个代理 → 解析掉，视作无出口组
  ]);

  // 直连不通（tg_direct=false → preferProxy=true）→ 落带出口的组，且回带 proxy
  const blocked = pool2.assignCred("BLK0-0000-0000-0001", { preferProxy: true });
  assert.ok(blocked.ok && blocked.api_id === "5002", `直连不通应分到出口组: ${JSON.stringify(blocked)}`);
  assert.ok(blocked.ok && blocked.proxy && blocked.proxy.host === "1.2.3.4");
  // 半个代理组不得被当出口组
  const st3 = pool2.poolStats();
  type G = { name: string; has_proxy: boolean };
  assert.ok(st3.groups.find((g: G) => g.name === "halfproxy")?.has_proxy === false);
  assert.ok(st3.groups.find((g: G) => g.name === "viaproxy")?.has_proxy === true);

  // 直连通畅（preferProxy=false）→ 优先无出口组（省出口容量）
  const direct = pool2.assignCred("DIR0-0000-0000-0001", { preferProxy: false });
  assert.ok(direct.ok && direct.api_id !== "5002", `直连机不该占用出口组: ${JSON.stringify(direct)}`);
  assert.ok(direct.ok && !direct.proxy);

  // 软偏好不硬过滤：出口组占满时，直连不通的机器照样能分到无出口组（接入优先）
  process.env.POOL_TG_CREDS = JSON.stringify([
    { api_id: "6001", api_hash: "a".repeat(32), max: 50, name: "direct-only" },
    { api_id: "6002", api_hash: "b".repeat(32), max: 1, name: "proxy-tiny",
      proxy: { scheme: "socks5", host: "9.9.9.9", port: 1080 } },
  ]);
  const p3db = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "tgpool3-")), "gw.db");
  process.env.AI_GATEWAY_QUOTA_DB = p3db;
  const pool3 = await import(`./tg-cred-pool?p3=${Date.now()}`);
  const fill = pool3.assignCred("PXY0-0000-0000-0001", { preferProxy: true });
  assert.ok(fill.ok && fill.api_id === "6002"); // 占满唯一出口位
  const spill = pool3.assignCred("PXY0-0000-0000-0002", { preferProxy: true });
  assert.ok(spill.ok && spill.api_id === "6001", `出口满应回落无出口组: ${JSON.stringify(spill)}`);

  console.log("tg-cred-pool.test.ts OK");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
