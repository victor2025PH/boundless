"use strict";

// 多账号容器 + 账号栏健康三态纯函数单测（无框架,node 直跑）：node test/webmulti.test.js
const assert = require("assert");
const {
  accountHealthState, railBadge, sortRail, unreadTotal, containerPlan,
} = require("../renderer/webmulti.js");

let pass = 0;
function ok(name, cond) { assert.ok(cond, name); pass++; }

// ── accountHealthState：三维取最差 ───────────────────────────────────────────
ok("health 全好→ok", (() => { const s = accountHealthState({ online: true, inject: "ok", translateOk: true }); return s.level === "ok" && s.text === "正常"; })());
ok("health 掉线→bad", (() => { const s = accountHealthState({ online: false, inject: "ok", translateOk: true }); return s.level === "bad" && s.text === "掉线"; })());
ok("health 注入失配→warn", (() => { const s = accountHealthState({ online: true, inject: "warn", translateOk: true }); return s.level === "warn" && s.text === "注入失配"; })());
ok("health 翻译不可达→warn", (() => { const s = accountHealthState({ online: true, inject: "ok", translateOk: false }); return s.level === "warn" && s.text === "翻译不可达"; })());
ok("health 最差维胜出(掉线>失配)", accountHealthState({ online: false, inject: "warn", translateOk: true }).level === "bad");
ok("health 全未知→wait", accountHealthState({}).level === "wait");
ok("health 无注入档案→bad", accountHealthState({ online: true, inject: "unsupported", translateOk: true }).level === "bad");
ok("health null 安全", accountHealthState(null).level === "wait");
ok("health 维度齐备", (() => { const d = accountHealthState({ online: true, inject: "ok", translateOk: true }).dims; return d.session && d.inject && d.translate; })());

// ── railBadge ─────────────────────────────────────────────────────────────────
ok("badge ok→on", railBadge({ level: "ok", text: "正常" }).dot === "on");
ok("badge bad→off", railBadge({ level: "bad", text: "掉线" }).dot === "off");
ok("badge warn→warn", railBadge({ level: "warn", text: "x" }).dot === "warn");
ok("badge wait→idle", railBadge({ level: "wait", text: "x" }).dot === "idle");
ok("badge null 安全", railBadge(null).dot === "idle");

// ── sortRail：置顶 > 未读 > 活跃 > id ────────────────────────────────────────
const sorted = sortRail([
  { id: "a", unread: 0, lastActive: 100 },
  { id: "b", unread: 5, lastActive: 50 },
  { id: "c", unread: 0, lastActive: 200, pinned: true },
  { id: "d", unread: 0, lastActive: 10 },
]);
ok("sort 置顶最前", sorted[0].id === "c");
ok("sort 未读次之", sorted[1].id === "b");
ok("sort 活跃再次(a>d)", sorted[2].id === "a" && sorted[3].id === "d");
ok("sort epoch 毫秒不截断", (() => {
  // 两个真实 epoch ms:较新的应在前（验证未用 |0 截断）
  const r = sortRail([{ id: "old", unread: 0, lastActive: 1_700_000_000_000 }, { id: "new", unread: 0, lastActive: 1_700_000_050_000 }]);
  return r[0].id === "new";
})());

// ── unreadTotal ───────────────────────────────────────────────────────────────
ok("unread 求和", unreadTotal([{ unread: 3 }, { unread: 5 }, {}]) === 8);
ok("unread 空安全", unreadTotal(null) === 0);

// ── containerPlan：聚焦恒 live + 择优保活 + 余者挂起 ──────────────────────────
const accts = [
  { id: "a", unread: 0, lastActive: 100 },
  { id: "b", unread: 5, lastActive: 50 },
  { id: "c", unread: 0, lastActive: 200, pinned: true },
  { id: "d", unread: 0, lastActive: 10 },
];
const plan = containerPlan(accts, { focusId: "d", maxLive: 2 });
ok("plan 聚焦恒 live 且居首", plan.live[0] === "d");
ok("plan 择优保活(置顶 c)", plan.live.indexOf("c") >= 0 && plan.live.length === 2);
ok("plan 余者挂起", plan.suspended.indexOf("a") >= 0 && plan.suspended.indexOf("b") >= 0 && plan.suspended.indexOf("c") < 0);

const plan2 = containerPlan(accts, { focusId: "z", maxLive: 2 }); // 聚焦不在列表
ok("plan 聚焦缺席→纯择优", plan2.live[0] === "c" && plan2.live.length === 2);

const plan3 = containerPlan(
  [{ id: "x", unread: 0, lastActive: 999 }, { id: "y", unread: 3, lastActive: 1 }],
  { maxLive: 1 }
);
ok("plan 未读优先于活跃", plan3.live[0] === "y" && plan3.suspended[0] === "x");

ok("plan 空列表安全", containerPlan([], { maxLive: 3 }).live.length === 0);
ok("plan null 安全", containerPlan(null, {}).live.length === 0);

console.log(`webmulti.test.js: ${pass} passed`);
