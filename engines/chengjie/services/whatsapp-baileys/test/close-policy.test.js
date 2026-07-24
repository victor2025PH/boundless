/**
 * close-policy.js 决策纯函数门禁（node --test，零新依赖）。
 *
 * 为什么只测 close-policy 不 import server.js：server.js 顶层 app.listen（import 即起
 * HTTP 服务、扫 sessions 目录），单测环境不可用也不该用；决策逻辑已抽成纯函数，
 * 这里锁死的是 2026-07-22 断网假死事故的行为契约——曾配对账号绝不能被判 expired。
 */
import test from "node:test";
import assert from "node:assert/strict";
import { decideCloseAction, CLOSE_CODES } from "../close-policy.js";

// Baileys DisconnectReason 语义（与 close-policy 内联常量对齐；server.js 启动时另有
// 与权威枚举的比对告警）。428=connectionClosed 是断网/踢线最常见码。
const CONNECTION_CLOSED = 428;

test("authorized 掉线（428 断网/踢线）→ reconnect", () => {
  const entry = { status: "authorized", accountId: "639270135480" };
  assert.deepEqual(decideCloseAction(entry, CONNECTION_CLOSED, false),
    { action: "reconnect" });
});

test("事故死径：accountId 非空但 pending（重连中 DNS 失败，code=0）→ reconnect 而非 expire", () => {
  // 2026-07-22 实锤场景：428 掉线后 startLogin 建新 entry（pending），新 socket
  // ENOTFOUND close（无 statusCode → 0）。旧逻辑按「非 authorized」置 expired 且不再
  // 重试 → 假死 2 小时。曾配对（accountId 来自持久化凭据）必须继续重连。
  const entry = { status: "pending", accountId: "639270135480" };
  assert.deepEqual(decideCloseAction(entry, 0, false), { action: "reconnect" });
});

test("reconnecting 中再失败（带 accountId）→ 仍是 reconnect（退避链继续走）", () => {
  const entry = { status: "reconnecting", accountId: "639270135480" };
  assert.deepEqual(decideCloseAction(entry, CONNECTION_CLOSED, false),
    { action: "reconnect" });
});

test("无 accountId 的纯 QR 流程失败 → expire（等用户重新扫码，不自动重连）", () => {
  const entry = { status: "pending", accountId: "" };
  assert.deepEqual(decideCloseAction(entry, CONNECTION_CLOSED, false),
    { action: "expire" });
});

test("loggedOut(401 设备解绑) → logged_out（终态，优先于曾配对的 reconnect）", () => {
  // 即使 accountId 非空且曾 authorized：设备端解绑后自动重连只会再次被拒，必须人工重配。
  const entry = { status: "authorized", accountId: "639270135480" };
  assert.deepEqual(decideCloseAction(entry, CLOSE_CODES.loggedOut, false),
    { action: "logged_out" });
});

test("restartRequired(515 配对后协议重启) → restart（不走退避，立即重建）", () => {
  const entry = { status: "pending", accountId: "639270135480" };
  assert.deepEqual(decideCloseAction(entry, CLOSE_CODES.restartRequired, false),
    { action: "restart" });
});

test("stale（entry 已被 startLogin 换代）→ ignore，压过一切其他分支", () => {
  // 旧 socket 迟到的 close 不得影响新 entry：连 restartRequired/loggedOut 也忽略——
  // 换代本身就是重启，重复处理会造出双 socket 抢 authDir 的 440 互踢循环。
  for (const code of [0, CONNECTION_CLOSED, CLOSE_CODES.restartRequired, CLOSE_CODES.loggedOut]) {
    assert.deepEqual(
      decideCloseAction({ status: "authorized", accountId: "639270135480" }, code, true),
      { action: "ignore" });
  }
});

test("防御性：entry 为 null（会话已被移除）且非 stale → expire（不炸、不复活）", () => {
  assert.deepEqual(decideCloseAction(null, CONNECTION_CLOSED, false),
    { action: "expire" });
});

test("CLOSE_CODES 契约：restartRequired=515 / loggedOut=401（Baileys DisconnectReason 同值）", () => {
  // server.js 启动时会与真实枚举比对；这里锁死字面值，防止两边同时被误改。
  assert.equal(CLOSE_CODES.restartRequired, 515);
  assert.equal(CLOSE_CODES.loggedOut, 401);
});
