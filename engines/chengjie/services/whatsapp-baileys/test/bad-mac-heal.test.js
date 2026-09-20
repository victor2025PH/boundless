/**
 * J-6 C：Bad MAC 自愈决策 —— 纯函数门禁（node --test）。
 *
 * 锁死的契约：
 *  - 只有 CIPHERTEXT stub 且文案含 "Bad MAC" 才算；缺 key / 无内容的 CIPHERTEXT 不算。
 *  - 同对端**连续** ≥ threshold 才 heal；中间一条明文即清零；threshold=0 只观测永不 heal。
 *  - heal 后进冷却：冷却内再连击只计数不重删；冷却过后可再删。
 *  - 计数按 (loginId, jid) 独立；clear(loginId) 不影响其他登录。
 *  - jid 打码只留末 4 位 + 域。
 */
import test from "node:test";
import assert from "node:assert/strict";
import {
  STUB_CIPHERTEXT, BAD_MAC_DEFAULT, badMacConfig, isBadMacStub, isPeerPlaintext,
  peerSessionJids, maskJid, BadMacTracker, badMacDetailTag,
  decryptFailIngestFields, DECRYPT_FAIL_PLACEHOLDER,
} from "../bad-mac-heal.js";

const badMac = (jid, extra = {}) => ({
  key: { remoteJid: jid, fromMe: false, id: "X" },
  messageStubType: STUB_CIPHERTEXT,
  messageStubParameters: ["Bad MAC"],
  ...extra,
});
const plain = (jid) => ({ key: { remoteJid: jid, fromMe: false, id: "Y" }, message: { conversation: "hi" } });

test("isBadMacStub: only CIPHERTEXT + Bad MAC text", () => {
  assert.equal(isBadMacStub(badMac("1@s.whatsapp.net")), true);
  assert.equal(isBadMacStub(badMac("1@s.whatsapp.net", { messageStubParameters: ["Session error:Error: Bad MAC"] })), true);
  assert.equal(isBadMacStub(badMac("1@s.whatsapp.net", { messageStubParameters: ["Key used already or never filled"] })), false);
  assert.equal(isBadMacStub({ ...badMac("1@s.whatsapp.net"), messageStubType: 1 }), false);
  assert.equal(isBadMacStub(plain("1@s.whatsapp.net")), false);
  assert.equal(isBadMacStub(null), false);
});

test("decryptFailIngestFields: stub → placeholder + decrypt_fail; plaintext → null", () => {
  const f = decryptFailIngestFields(badMac("1@s.whatsapp.net"));
  assert.equal(f.text, DECRYPT_FAIL_PLACEHOLDER);
  assert.equal(f.decrypt_fail, true);
  assert.equal(f.backfill, undefined);
  assert.equal(decryptFailIngestFields(plain("1@s.whatsapp.net")), null);
  assert.equal(decryptFailIngestFields(null), null);
});

test("isPeerPlaintext: decrypted, not fromMe, not stub", () => {
  assert.equal(isPeerPlaintext(plain("1@s.whatsapp.net")), true);
  assert.equal(isPeerPlaintext({ key: { remoteJid: "1@s.whatsapp.net", fromMe: true }, message: {} }), false);
  assert.equal(isPeerPlaintext(badMac("1@s.whatsapp.net")), false);
  assert.equal(isPeerPlaintext({ key: { remoteJid: "1@s.whatsapp.net" } }), false);
});

test("peerSessionJids: private → remoteJid(+alt); group → participant only; dedup", () => {
  assert.deepEqual(peerSessionJids(badMac("639@s.whatsapp.net")), ["639@s.whatsapp.net"]);
  assert.deepEqual(
    peerSessionJids({ key: { remoteJid: "259146280653037@lid", remoteJidAlt: "639@s.whatsapp.net" } }),
    ["259146280653037@lid", "639@s.whatsapp.net"]);
  assert.deepEqual(
    peerSessionJids({ key: { remoteJid: "123-456@g.us", participant: "777@s.whatsapp.net" } }),
    ["777@s.whatsapp.net"]);
  assert.deepEqual(
    peerSessionJids({ key: { remoteJid: "777@s.whatsapp.net", participant: "777@s.whatsapp.net" } }),
    ["777@s.whatsapp.net"]);
  assert.deepEqual(peerSessionJids({ key: { remoteJid: "status@broadcast" } }), []);
  assert.deepEqual(peerSessionJids({}), []);
});

test("maskJid keeps domain + last 4", () => {
  assert.equal(maskJid("639270135480@s.whatsapp.net"), "********5480@s.whatsapp.net");
  assert.equal(maskJid("12@lid"), "**@lid");
  assert.equal(maskJid(""), "");
});

test("badMacConfig: defaults and env overrides", () => {
  assert.deepEqual(badMacConfig({}), { threshold: 3, cooldownMs: 30 * 60 * 1000 });
  assert.deepEqual(badMacConfig({ WA_BAD_MAC_THRESHOLD: "5", WA_BAD_MAC_COOLDOWN_MIN: "1" }),
    { threshold: 5, cooldownMs: 60 * 1000 });
  assert.equal(badMacConfig({ WA_BAD_MAC_THRESHOLD: "0" }).threshold, 0);
  assert.equal(badMacConfig({ WA_BAD_MAC_THRESHOLD: "abc" }).threshold, BAD_MAC_DEFAULT.threshold);
});

test("tracker: heal on 3rd consecutive; plaintext resets streak", () => {
  const t = new BadMacTracker({ threshold: 3, cooldownMs: 60_000 });
  const L = "login1", J = "1@s.whatsapp.net";
  assert.equal(t.record(L, J, 1000).heal, false);
  assert.equal(t.record(L, J, 1001).heal, false);
  t.noteOk(L, J);
  assert.equal(t.record(L, J, 1002).heal, false, "streak reset by plaintext");
  assert.equal(t.record(L, J, 1003).heal, false);
  const r = t.record(L, J, 1004);
  assert.equal(r.heal, true);
  assert.equal(r.streak, 3);
  assert.equal(t.total, 5);
});

test("tracker: cooldown after heal; re-heal once cooldown passed", () => {
  const t = new BadMacTracker({ threshold: 2, cooldownMs: 10_000 });
  const L = "l", J = "2@lid";
  t.record(L, J, 0);
  assert.equal(t.record(L, J, 1).heal, true);
  t.markHealed(L, J, 1);
  assert.equal(t.healTotal, 1);
  // 冷却内再连击 2 次：只计数
  t.record(L, J, 2);
  const r2 = t.record(L, J, 3);
  assert.equal(r2.heal, false);
  assert.equal(r2.inCooldown, true);
  // 冷却过后（≥10s）连击达阈 → 可再删
  const r3 = t.record(L, J, 20_001);
  assert.equal(r3.heal, true);
});

test("tracker: threshold=0 never heals, still counts", () => {
  const t = new BadMacTracker({ threshold: 0, cooldownMs: 0 });
  for (let i = 0; i < 10; i++) assert.equal(t.record("l", "3@lid", i).heal, false);
  assert.equal(t.snapshot().bad_mac_total, 10);
});

test("tracker: per-(login,jid) isolation + clear(loginId)", () => {
  const t = new BadMacTracker({ threshold: 3, cooldownMs: 0 });
  t.record("A", "p@lid", 1); t.record("A", "p@lid", 2);
  t.record("B", "p@lid", 3);
  assert.equal(t.record("B", "p@lid", 4).heal, false, "B has its own streak");
  assert.equal(t.record("A", "p@lid", 5).heal, true);
  t.clear("A");
  const s = t.snapshot();
  assert.equal(s.bad_mac_peers, 1);
  assert.equal(s.top[0].login, "B");
  assert.equal(s.top[0].peer, "*@lid");
});

test("snapshot shape for /health", () => {
  const t = new BadMacTracker({ threshold: 3, cooldownMs: 30 * 60 * 1000 });
  t.record("l", "639270135480@s.whatsapp.net", 1);
  const s = t.snapshot();
  assert.deepEqual(Object.keys(s).sort(),
    ["bad_mac_active", "bad_mac_peers", "bad_mac_total", "cooldown_min", "heals", "threshold", "top"].sort());
  assert.equal(s.threshold, 3);
  assert.equal(s.cooldown_min, 30);
  assert.equal(s.bad_mac_active, 1);
  assert.deepEqual(s.top, [{ login: "l", peer: "********5480@s.whatsapp.net", streak: 1, total: 1, heals: 0 }]);
});

test("snapshotFor: per-login cumulative snapshot; badMacDetailTag encodes it for Python", () => {
  const t = new BadMacTracker({ threshold: 2, cooldownMs: 1000 });
  const A = "111@s.whatsapp.net", B = "222@s.whatsapp.net", C = "333@s.whatsapp.net";
  t.record("L1", A, 0); t.record("L1", A, 1); // heal at 2
  t.markHealed("L1", A, 1);
  t.record("L1", B, 2);                       // streak 1 (active)
  t.record("L2", C, 3);                       // other login
  assert.deepEqual(t.snapshotFor("L1"), { total: 3, peers: 2, heals: 1, active: 1 });
  assert.deepEqual(t.snapshotFor("L2"), { total: 1, peers: 1, heals: 0, active: 1 });
  assert.deepEqual(t.snapshotFor("nope"), { total: 0, peers: 0, heals: 0, active: 0 });
  assert.equal(badMacDetailTag(t.snapshotFor("L1")), "[bm:total=3,peers=2,heals=1,active=1]");
  // 缺字段/垃圾值 → 0（Python 侧正则只吃非负整数）
  assert.equal(badMacDetailTag({ total: -4, heals: "x" }), "[bm:total=0,peers=0,heals=0,active=0]");
  assert.equal(badMacDetailTag(null), "[bm:total=0,peers=0,heals=0,active=0]");
});
