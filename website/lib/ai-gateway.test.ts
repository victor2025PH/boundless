/**
 * 纯函数冒烟：npx tsx lib/ai-gateway.test.ts（不依赖 Next runtime）。
 * env 必须在动态 import 前设好（模块常量在 import 时求值）。
 */
import assert from "assert";
import fs from "fs";
import os from "os";
import path from "path";

process.env.DEEPSEEK_API_KEY = "sk-test-for-unit";
process.env.AI_GATEWAY_SECRET = "unit-test-secret";
process.env.AI_GATEWAY_DAILY_CHARS = "100";
process.env.AI_GATEWAY_GLOBAL_DAILY_CHARS = "150";
process.env.AI_GATEWAY_MAX_TOKENS = "2048";
process.env.AI_GATEWAY_MODELS = "deepseek-chat,deepseek-v4-flash";
process.env.AI_GATEWAY_QUOTA_DB = path.join(
  fs.mkdtempSync(path.join(os.tmpdir(), "gwtest-")),
  "quota.db"
);

async function main() {
  const gw = await import("./ai-gateway");

  // ── 指纹归一 + 令牌往返 ──
  assert.equal(gw.normalizeFingerprint("aaaa-bbbb-cccc-dddd"), "AAAA-BBBB-CCCC-DDDD");
  const fp = "AAAA-BBBB-CCCC-DDDD";
  const { token, claims } = gw.mintDeviceToken(fp);
  assert.ok(token.startsWith("cx."));
  const v = gw.verifyDeviceToken(token);
  assert.ok(v && v.mid === fp && v.exp === claims.exp);
  assert.equal(gw.verifyDeviceToken("cx.bad.sig"), null);
  assert.equal(gw.verifyDeviceToken(""), null);
  // 篡改载荷必拒
  const [h, body] = token.split(".");
  assert.equal(gw.verifyDeviceToken(`${h}.${body}x.${token.split(".")[2]}`), null);

  // ── model / max_tokens 钳制 ──
  assert.equal(gw.clampModel("deepseek-v4-flash"), "deepseek-v4-flash");
  assert.equal(gw.clampModel("deepseek-reasoner"), "deepseek-chat"); // 白名单外回落默认
  assert.equal(gw.clampModel(undefined), "deepseek-chat");
  assert.equal(gw.clampMaxTokens(99999), 2048);
  assert.equal(gw.clampMaxTokens(100), 100);
  assert.equal(gw.clampMaxTokens(undefined), 1024);

  // ── 字符估算 ──
  assert.equal(
    gw.estimateRequestChars({ messages: [{ role: "user", content: "你好世界" }] }),
    4
  );

  // ── 双层额度：单机 100 / 全局 150 ──
  const a = await gw.consumeQuota("AAAA-0000-0000-0001", 60);
  assert.ok(a.ok && a.remaining === 40);
  const a2 = await gw.consumeQuota("AAAA-0000-0000-0001", 50); // 60+50 > 100 → 单机拒
  assert.ok(!a2.ok && a2.which === "machine");
  const b = await gw.consumeQuota("AAAA-0000-0000-0002", 80); // 全局 60+80=140 ≤ 150
  assert.ok(b.ok);
  const c = await gw.consumeQuota("AAAA-0000-0000-0003", 20); // 全局 140+20 > 150 → 全局拒
  assert.ok(!c.ok && c.which === "global");
  const snap = await gw.quotaSnapshot("AAAA-0000-0000-0003");
  assert.equal(snap.used, 0);
  assert.equal(snap.global_used, 140);
  assert.equal(snap.busy, false);
  const d = await gw.consumeQuota("AAAA-0000-0000-0003", 10); // 恰到 150 → busy
  assert.ok(d.ok);
  assert.equal((await gw.quotaSnapshot("AAAA-0000-0000-0003")).busy, true);

  // ── 当日聚合（console 卡）──
  const stats = await gw.gatewayDayStats();
  assert.equal(stats.machines, 3); // 0001/0002/0003（__GLOBAL__ 不算机器）
  assert.equal(stats.chars, 150);
  assert.equal(stats.machine_budget, 100);

  // ── 生产环境缺 AI_GATEWAY_SECRET → 网关整体禁用 ──
  assert.equal(gw.gatewayEnabled(), true);
  const oldEnv = process.env.NODE_ENV;
  const oldSecret = process.env.AI_GATEWAY_SECRET;
  (process.env as Record<string, string | undefined>).NODE_ENV = "production";
  delete process.env.AI_GATEWAY_SECRET;
  assert.equal(gw.gatewayEnabled(), false);
  process.env.AI_GATEWAY_SECRET = oldSecret;
  assert.equal(gw.gatewayEnabled(), true);
  (process.env as Record<string, string | undefined>).NODE_ENV = oldEnv;

  console.log("ai-gateway.test.ts OK");
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
