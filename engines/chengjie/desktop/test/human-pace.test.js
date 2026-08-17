"use strict";

// 出站拟人节奏纯函数单测（无框架,node 直跑）：node test/human-pace.test.js
const assert = require("assert");
const {
  makeRng, composeMs, interMessageGapMs, withinQuietHours, planBatchSend,
} = require("../../shared/inject/human-pace.js");

let pass = 0;
function ok(name, cond) { assert.ok(cond, name); pass++; }

// ── makeRng 确定性 ────────────────────────────────────────────────────────────
const r1 = makeRng(42), r2 = makeRng(42);
ok("rng 同 seed 同序", r1() === r2() && r1() === r2());
ok("rng 值域 [0,1)", (() => { const r = makeRng(7); for (let i = 0; i < 50; i++) { const v = r(); if (v < 0 || v >= 1) return false; } return true; })());

// ── composeMs（打字耗时）─────────────────────────────────────────────────────
const cm = composeMs("hello world", { rng: makeRng(1), cps: 5, min: 600, max: 12000 });
ok("compose 整数 ms", Number.isInteger(cm));
ok("compose 落上下限内", cm >= 600 && cm <= 12000);
ok("compose 空文取下限", composeMs("", { rng: makeRng(1), min: 600 }) === 600);
ok("compose 确定性", composeMs("abcabc", { rng: makeRng(9) }) === composeMs("abcabc", { rng: makeRng(9) }));
// 长文本(不触顶)平均应比短文本久:比较同 seed 下的确定值
const shortMs = composeMs("hi", { rng: makeRng(3), cps: 5, max: 60000 });
const longMs = composeMs("this is a much longer sentence to type", { rng: makeRng(3), cps: 5, max: 60000 });
ok("compose 长文更久", longMs > shortMs);

// ── interMessageGapMs ────────────────────────────────────────────────────────
const gap = interMessageGapMs({ rng: makeRng(5), meanMs: 45000, min: 8000, max: 300000 });
ok("gap 落上下限内", gap >= 8000 && gap <= 300000 && Number.isInteger(gap));
ok("gap 确定性", interMessageGapMs({ rng: makeRng(5) }) === interMessageGapMs({ rng: makeRng(5) }));

// ── withinQuietHours ──────────────────────────────────────────────────────────
ok("quiet 跨零点 2 点静默", withinQuietHours(2, { start: 23, end: 8 }) === true);
ok("quiet 跨零点 12 点非静默", withinQuietHours(12, { start: 23, end: 8 }) === false);
ok("quiet 边界 start 命中", withinQuietHours(23, { start: 23, end: 8 }) === true);
ok("quiet 边界 end 不含", withinQuietHours(8, { start: 23, end: 8 }) === false);
ok("quiet 同日窗内", withinQuietHours(12, { start: 9, end: 17 }) === true);
ok("quiet 同日窗外", withinQuietHours(18, { start: 9, end: 17 }) === false);
ok("quiet 关闭→永不静默", withinQuietHours(2, { enabled: false, start: 23, end: 8 }) === false);

// ── planBatchSend ─────────────────────────────────────────────────────────────
// 正常:3 人,高频控,白天,无安静
const day = () => 12;
const plan1 = planBatchSend(["a", "b", "c"], {
  startTs: 1_000_000, rng: makeRng(11), perMinuteCap: 30, dailyCap: 100, hourOf: day,
});
ok("plan 全部排入", plan1.sends.length === 3 && plan1.deferred.length === 0);
ok("plan 首条在 startTs", plan1.sends[0].at === 1_000_000 && plan1.sends[0].gapMs === 0);
ok("plan 后续有正间隔", plan1.sends[1].at > plan1.sends[0].at && plan1.sends[1].gapMs > 0);
ok("plan lastAt = 末条", plan1.lastAt === plan1.sends[2].at);

// 频控 cap=1 → 最小间隔 60s
const plan2 = planBatchSend(["a", "b"], {
  startTs: 0, rng: makeRng(2), perMinuteCap: 1, dailyCap: 100, hourOf: day,
});
ok("plan 频控最小间隔≥60s", plan2.sends[1].at - plan2.sends[0].at >= 60000);

// 每日上限:remaining = dailyCap - sentToday
const plan3 = planBatchSend(["a", "b", "c"], {
  startTs: 0, rng: makeRng(2), perMinuteCap: 30, dailyCap: 2, sentToday: 1, hourOf: day,
});
ok("plan 日限剩 1→1 发 2 延后", plan3.sends.length === 1 && plan3.deferred.length === 2);
ok("plan 日限延后原因", plan3.deferred.every((d) => d.reason === "daily_cap"));

// 安静时段:全部落在静默窗 → 全延后
const night = () => 2;
const plan4 = planBatchSend(["a", "b"], {
  startTs: 0, rng: makeRng(2), perMinuteCap: 30, dailyCap: 100,
  quiet: { start: 23, end: 8 }, hourOf: night,
});
ok("plan 安静时段全延后", plan4.sends.length === 0 && plan4.deferred.every((d) => d.reason === "quiet_hours"));

// 非法收件人跳过
const plan5 = planBatchSend(["", "b"], { startTs: 0, rng: makeRng(2), perMinuteCap: 30, hourOf: day });
ok("plan 空收件人→invalid", plan5.deferred.some((d) => d.reason === "invalid") && plan5.sends.length === 1);

ok("plan 空列表安全", planBatchSend([], {}).sends.length === 0);
ok("plan null 安全", planBatchSend(null, {}).plannedTotal === 0);

console.log(`human-pace.test.js: ${pass} passed`);
