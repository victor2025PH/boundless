/**
 * close-policy.js 决策纯函数门禁（node --test，零新依赖）。
 *
 * 为什么只测 close-policy 不 import server.js：server.js 顶层 app.listen（import 即起
 * HTTP 服务、扫 sessions 目录），单测环境不可用也不该用；决策逻辑已抽成纯函数，
 * 这里锁死的是 2026-07-22 断网假死事故的行为契约——曾配对账号绝不能被判 expired。
 */
import test from "node:test";
import assert from "node:assert/strict";
import {
  decideCloseAction, CLOSE_CODES, nextForbiddenState, forbiddenRoundsMax,
  FORBIDDEN_ROUNDS_DEFAULT,
} from "../close-policy.js";

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

test("CLOSE_CODES 契约：restartRequired=515 / loggedOut=401 / forbidden=403（Baileys DisconnectReason 同值）", () => {
  // server.js 启动时会与真实枚举比对；这里锁死字面值，防止两边同时被误改。
  assert.equal(CLOSE_CODES.restartRequired, 515);
  assert.equal(CLOSE_CODES.loggedOut, 401);
  assert.equal(CLOSE_CODES.forbidden, 403);
});

// ── J-6 A：403 forbidden 终态阀（skuio 88MP86：一个 403 账号 10h 38 轮重连刷 185 条日志）──

const PAIRED = Object.freeze({ status: "authorized", accountId: "12132684190" });
const ATTEMPTS = 5; // 与 server.js _RECONNECT_MAX 同值：一轮 = 5 次快退避后 giving up

/** 模拟 server.js 一轮「close(403) → 5 次重连各 close(403) → giving up」的状态机推进。 */
function oneForbiddenRound(state) {
  let s = state;
  for (let i = 0; i < ATTEMPTS + 1; i++) s = nextForbiddenState(s, { type: "close", code: 403 });
  return nextForbiddenState(s, { type: "exhausted", attemptsPerRound: ATTEMPTS });
}

test("403 首轮：rounds=0 → 仍是 reconnect（走完一轮退避，防瞬时 403 误判）", () => {
  assert.deepEqual(
    decideCloseAction(PAIRED, CLOSE_CODES.forbidden, false, { forbiddenRounds: 0, forbiddenRoundsMax: 2 }),
    { action: "reconnect" });
  // 第一轮 giving up 后 rounds=1，仍未达默认阈值 2 → 第二轮继续 reconnect
  const s1 = oneForbiddenRound(undefined);
  assert.equal(s1.rounds, 1);
  assert.deepEqual(
    decideCloseAction(PAIRED, CLOSE_CODES.forbidden, false, { forbiddenRounds: s1.rounds, forbiddenRoundsMax: 2 }),
    { action: "reconnect" });
});

test("403 第二轮 giving up 后 → 下一次 403 close 判 forbidden 终态", () => {
  const s2 = oneForbiddenRound(oneForbiddenRound(undefined));
  assert.equal(s2.rounds, 2);
  assert.deepEqual(
    decideCloseAction(PAIRED, CLOSE_CODES.forbidden, false, { forbiddenRounds: s2.rounds, forbiddenRoundsMax: 2 }),
    { action: "forbidden" });
  // 缺省阈值（ctx 不带 forbiddenRoundsMax）= FORBIDDEN_ROUNDS_DEFAULT
  assert.equal(FORBIDDEN_ROUNDS_DEFAULT, 2);
  assert.deepEqual(
    decideCloseAction(PAIRED, CLOSE_CODES.forbidden, false, { forbiddenRounds: 2 }),
    { action: "forbidden" });
});

test("编排器护送场景：giving up 永不触发（rounds 恒 0），连续 403 streak 折算等效轮数也能到终态", () => {
  // Python 编排器每次退避重启打 /reconnect 清 _reconnectAttempts → exhausted 事件不来，
  // rounds 停在 0；但每次 close(403) 的 streak 照常累积。一轮 = 1+ATTEMPTS 次 close。
  let s;
  for (let i = 0; i < 2 * (ATTEMPTS + 1) - 1; i++) s = nextForbiddenState(s, { type: "close", code: 403 });
  assert.equal(s.rounds, 0);
  // 差一次不到两轮 → 仍 reconnect
  assert.deepEqual(
    decideCloseAction(PAIRED, 403, false, { forbiddenRounds: 0, forbiddenRoundsMax: 2,
      forbiddenStreak: s.streak, attemptsPerRound: ATTEMPTS }),
    { action: "reconnect" });
  s = nextForbiddenState(s, { type: "close", code: 403 });
  assert.equal(s.streak, 2 * (ATTEMPTS + 1));
  assert.deepEqual(
    decideCloseAction(PAIRED, 403, false, { forbiddenRounds: 0, forbiddenRoundsMax: 2,
      forbiddenStreak: s.streak, attemptsPerRound: ATTEMPTS }),
    { action: "forbidden" });
  // 不传 attemptsPerRound → 不折算（旧契约：只看 rounds）
  assert.deepEqual(
    decideCloseAction(PAIRED, 403, false, { forbiddenRounds: 0, forbiddenRoundsMax: 2, forbiddenStreak: 999 }),
    { action: "reconnect" });
  // rounds 已达阈值时 streak 小也判终态（取大）
  assert.deepEqual(
    decideCloseAction(PAIRED, 403, false, { forbiddenRounds: 2, forbiddenRoundsMax: 2,
      forbiddenStreak: 1, attemptsPerRound: ATTEMPTS }),
    { action: "forbidden" });
});

test("阀=0 关闭终态判定 → 403 永远按 reconnect（恢复旧行为）；不传 ctx 亦向后兼容", () => {
  assert.deepEqual(
    decideCloseAction(PAIRED, CLOSE_CODES.forbidden, false, { forbiddenRounds: 99, forbiddenRoundsMax: 0 }),
    { action: "reconnect" });
  assert.deepEqual(
    decideCloseAction(PAIRED, CLOSE_CODES.forbidden, false, { forbiddenRounds: 0, forbiddenRoundsMax: 0,
      forbiddenStreak: 999, attemptsPerRound: ATTEMPTS }),
    { action: "reconnect" });
  assert.deepEqual(decideCloseAction(PAIRED, CLOSE_CODES.forbidden, false), { action: "reconnect" });
});

test("403 计数被非 403 close 打断即清零（一轮里混进 428/408 → 不算「整轮 403」）", () => {
  let s = nextForbiddenState(undefined, { type: "close", code: 403 });
  s = nextForbiddenState(s, { type: "close", code: 403 });
  s = nextForbiddenState(s, { type: "close", code: 428 });
  assert.deepEqual(s, { streak: 0, rounds: 0 });
  // 已攒 1 轮，第二轮夹一次 408 → exhausted 时 streak < 5 → rounds 清零
  s = oneForbiddenRound(undefined);
  for (let i = 0; i < 3; i++) s = nextForbiddenState(s, { type: "close", code: 403 });
  s = nextForbiddenState(s, { type: "close", code: 408 });
  for (let i = 0; i < 3; i++) s = nextForbiddenState(s, { type: "close", code: 403 });
  s = nextForbiddenState(s, { type: "exhausted", attemptsPerRound: ATTEMPTS });
  assert.equal(s.rounds, 0);
});

test("open 全清零；stale 的 403 close 仍 ignore；401/428 语义不变（2026-07-22 回归钉）", () => {
  const s2 = oneForbiddenRound(oneForbiddenRound(undefined));
  assert.deepEqual(nextForbiddenState(s2, { type: "open" }), { streak: 0, rounds: 0 });
  const ctx = { forbiddenRounds: 2, forbiddenRoundsMax: 2 };
  assert.deepEqual(decideCloseAction(PAIRED, CLOSE_CODES.forbidden, true, ctx), { action: "ignore" });
  assert.deepEqual(decideCloseAction(PAIRED, CLOSE_CODES.loggedOut, false, ctx), { action: "logged_out" });
  assert.deepEqual(decideCloseAction(PAIRED, CONNECTION_CLOSED, false, ctx), { action: "reconnect" });
  assert.deepEqual(decideCloseAction({ status: "pending", accountId: "12132684190" }, 0, false, ctx),
    { action: "reconnect" });
  assert.deepEqual(decideCloseAction({ status: "pending", accountId: "" }, CONNECTION_CLOSED, false, ctx),
    { action: "expire" });
});

test("nextForbiddenState 是纯函数：不改入参、坏输入不炸", () => {
  const prev = { streak: 3, rounds: 1 };
  const out = nextForbiddenState(prev, { type: "close", code: 403 });
  assert.deepEqual(prev, { streak: 3, rounds: 1 });
  assert.deepEqual(out, { streak: 4, rounds: 1 });
  assert.deepEqual(nextForbiddenState(null, null), { streak: 0, rounds: 0 });
  assert.deepEqual(nextForbiddenState("junk", { type: "nope" }), { streak: 0, rounds: 0 });
});

test("forbiddenRoundsMax 读 WA_FORBIDDEN_ROUNDS：缺省 2 / 非法回默认 / 0 关 / 小数取整", () => {
  assert.equal(forbiddenRoundsMax({}), 2);
  assert.equal(forbiddenRoundsMax(undefined), 2);
  assert.equal(forbiddenRoundsMax({ WA_FORBIDDEN_ROUNDS: "" }), 2);
  assert.equal(forbiddenRoundsMax({ WA_FORBIDDEN_ROUNDS: "abc" }), 2);
  assert.equal(forbiddenRoundsMax({ WA_FORBIDDEN_ROUNDS: "-1" }), 2);
  assert.equal(forbiddenRoundsMax({ WA_FORBIDDEN_ROUNDS: "0" }), 0);
  assert.equal(forbiddenRoundsMax({ WA_FORBIDDEN_ROUNDS: "3.7" }), 3);
});
