/**
 * J-6 B：close 原因分类 / 滚动窗口计数 / DNS 短退避 / 配对时延提示 —— 纯函数门禁（node --test）。
 *
 * 锁死的契约：
 *  - ENOTFOUND（Baileys 映成 408，报文 "WebSocket Error (getaddrinfo ENOTFOUND web.whatsapp.com)"）
 *    必须归 dns，不得与 428/ECONNRESET 混成一个 net；403/401/515 只看 code。
 *  - DNS 短退避阶段不消耗常规预算（dnsPhase=true），连击超限回到指数曲线。
 *  - 配对 ≥60s 且期间有 DNS 失败才出 hint_code="dns_retry"；配对完成后不再提示。
 */
import test from "node:test";
import assert from "node:assert/strict";
import {
  classifyCloseReason, REASON_CLASSES, ReasonWindow, dnsRetryConfig, reconnectDelay,
  DNS_RETRY_DEFAULT,
} from "../close-policy.js";
import { pairingObservation, PAIRING_DNS_HINT_MS } from "../scan-signal.js";

const ENOTFOUND_MSG = "WebSocket Error (getaddrinfo ENOTFOUND web.whatsapp.com)";

test("REASON_CLASSES 枚举固定（看板列顺序契约）", () => {
  assert.deepEqual([...REASON_CLASSES],
    ["dns", "net", "server", "forbidden", "logged_out", "restart", "other"]);
});

test("classifyCloseReason：ENOTFOUND（Baileys 映 408）→ dns，不是 net", () => {
  assert.equal(classifyCloseReason(408, ENOTFOUND_MSG), "dns");
  assert.equal(classifyCloseReason(0, "getaddrinfo ENOTFOUND web.whatsapp.com"), "dns"); // startLogin 直接抛
  assert.equal(classifyCloseReason(408, "WebSocket Error", "ENOTFOUND"), "dns"); // 只有 err.code 也认
  assert.equal(classifyCloseReason(408, "getaddrinfo EAI_AGAIN web.whatsapp.com"), "dns");
});

test("classifyCloseReason：传输层错误 → net；428/408 无特征 → net", () => {
  assert.equal(classifyCloseReason(408, "WebSocket Error (read ECONNRESET)"), "net");
  assert.equal(classifyCloseReason(408, "connect ETIMEDOUT 1.2.3.4:443"), "net");
  assert.equal(classifyCloseReason(428, "Connection Terminated"), "net");
  assert.equal(classifyCloseReason(408, "Connection Failure"), "net");
  assert.equal(classifyCloseReason(0, "socket hang up"), "net");
});

test("classifyCloseReason：服务端族 440/500/503/411 → server", () => {
  for (const c of [440, 500, 503, 411]) assert.equal(classifyCloseReason(c, "Stream Errored"), "server", String(c));
});

test("classifyCloseReason：账号态码只看 code（报文千篇一律）", () => {
  assert.equal(classifyCloseReason(403, "Connection Failure"), "forbidden");
  assert.equal(classifyCloseReason(401, "Connection Failure"), "logged_out");
  assert.equal(classifyCloseReason(515, "Stream Errored (restart required)"), "restart");
  // 即便报文含 ENOTFOUND 字样，403 仍是 forbidden（账号态优先）
  assert.equal(classifyCloseReason(403, ENOTFOUND_MSG), "forbidden");
});

test("classifyCloseReason：无 code 无特征 → other", () => {
  assert.equal(classifyCloseReason(0, ""), "other");
  assert.equal(classifyCloseReason(0, "something weird"), "other");
});

test("ReasonWindow：10 分钟滚动窗，窗外事件出窗、进程累计不滚", () => {
  let t = 1_000_000;
  const w = new ReasonWindow({ windowMs: 10 * 60 * 1000, now: () => t });
  w.record("dns"); w.record("dns"); w.record("net");
  assert.deepEqual(w.counts(), { dns: 2, net: 1, server: 0, forbidden: 0, logged_out: 0, restart: 0, other: 0 });
  t += 9 * 60 * 1000;
  w.record("server");
  assert.equal(w.counts().dns, 2);
  t += 2 * 60 * 1000; // 前三条已过 10min
  const c = w.counts();
  assert.equal(c.dns, 0); assert.equal(c.net, 0); assert.equal(c.server, 1);
  assert.deepEqual({ ...w.total }, { dns: 2, net: 1, server: 1 });
});

test("ReasonWindow：未知类归 other；事件数封顶丢最旧", () => {
  const w = new ReasonWindow({ windowMs: 60_000, maxEvents: 10, now: () => 5_000_000 });
  w.record("bogus");
  assert.equal(w.counts().other, 1);
  for (let i = 0; i < 30; i++) w.record("dns");
  assert.equal(w.counts().dns, 10);
  assert.equal(w.counts().other, 0);
  assert.equal(w.total.dns, 30);
});

test("dnsRetryConfig：缺省 3s×10；非法回默认；count=0 允许（关短退避）", () => {
  assert.deepEqual(dnsRetryConfig({}), { ...DNS_RETRY_DEFAULT });
  assert.deepEqual(dnsRetryConfig({ WA_DNS_RETRY_MS: "5000", WA_DNS_RETRY_COUNT: "4" }), { delayMs: 5000, count: 4 });
  assert.deepEqual(dnsRetryConfig({ WA_DNS_RETRY_MS: "abc", WA_DNS_RETRY_COUNT: "-1" }), { ...DNS_RETRY_DEFAULT });
  assert.equal(dnsRetryConfig({ WA_DNS_RETRY_MS: "100" }).delayMs, DNS_RETRY_DEFAULT.delayMs); // <500ms 拒（防热循环）
  assert.equal(dnsRetryConfig({ WA_DNS_RETRY_COUNT: "0" }).count, 0);
});

test("reconnectDelay：DNS 连击 1..count 固定 3s 且 dnsPhase=true（不烧预算）", () => {
  for (let s = 1; s <= 10; s++) {
    assert.deepEqual(reconnectDelay({ attempt: 1, dnsStreak: s }), { delayMs: 3000, dnsPhase: true }, `streak ${s}`);
  }
});

test("reconnectDelay：连击超过 count 回到常规指数曲线（3→6→12→24→48→60 封顶）", () => {
  const exp = [3000, 6000, 12000, 24000, 48000, 60000, 60000];
  exp.forEach((d, i) => {
    assert.deepEqual(reconnectDelay({ attempt: i + 1, dnsStreak: 11 }), { delayMs: d, dnsPhase: false }, `attempt ${i + 1}`);
  });
});

test("reconnectDelay：非 DNS close（streak=0）走常规曲线；count=0 关短退避", () => {
  assert.deepEqual(reconnectDelay({ attempt: 2, dnsStreak: 0 }), { delayMs: 6000, dnsPhase: false });
  assert.deepEqual(reconnectDelay({ attempt: 2, dnsStreak: 3, dnsCfg: { delayMs: 3000, count: 0 } }),
    { delayMs: 6000, dnsPhase: false });
});

test("pairingObservation：进行中 <60s 不提示；≥60s 且有 DNS 失败 → dns_retry", () => {
  const start = 10_000_000;
  const e = { pairingStartedAt: start, pairingDnsFails: 2, pairingMs: 0 };
  assert.deepEqual(pairingObservation(e, start + 30_000),
    { pairing_ms: 30_000, pairing_dns_fails: 2, hint_code: "" });
  assert.deepEqual(pairingObservation(e, start + PAIRING_DNS_HINT_MS),
    { pairing_ms: PAIRING_DNS_HINT_MS, pairing_dns_fails: 2, hint_code: "dns_retry" });
});

test("pairingObservation：≥60s 但零 DNS 失败不提示（慢是别的原因，别误导查 DNS）", () => {
  const start = 10_000_000;
  const r = pairingObservation({ pairingStartedAt: start, pairingDnsFails: 0 }, start + 300_000);
  assert.equal(r.hint_code, "");
  assert.equal(r.pairing_ms, 300_000);
});

test("pairingObservation：配对完成定格 pairingMs，不再随时间涨、不再提示", () => {
  const r = pairingObservation({ pairingStartedAt: 0, pairingDnsFails: 3, pairingMs: 245_000 }, 99_999_999_999);
  assert.deepEqual(r, { pairing_ms: 245_000, pairing_dns_fails: 3, hint_code: "" });
});

test("pairingObservation：从未配对（磁盘恢复老会话）→ 全零", () => {
  assert.deepEqual(pairingObservation({ status: "authorized" }, 1),
    { pairing_ms: 0, pairing_dns_fails: 0, hint_code: "" });
  assert.deepEqual(pairingObservation(null, 1), { pairing_ms: 0, pairing_dns_fails: 0, hint_code: "" });
});
