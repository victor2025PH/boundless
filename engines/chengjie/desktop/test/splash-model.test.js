"use strict";

// splash-model.js 纯函数单测（开机动画：阶段推导 / 进度映射 / 文案轮换 / ETA / 模式判定）
// + 与 shell-i18n 词典的双向契约（氛围池每个索引都必须有 zh/en 词条，池数改动两边同步）。
// node test/splash-model.test.js
const assert = require("assert");
const M = require("../renderer/splash-model.js");
const shellI18n = require("../renderer/shell-i18n.js");
const DICT = shellI18n._dict;

let passed = 0;
function ok(cond, msg) {
  assert.ok(cond, msg);
  passed++;
}

// ── 阶段推导：信号 → 阶段（只进不退）─────────────────────────────────────
ok(M.spPhase({}, "shell") === "shell", "无信号 → shell");
ok(M.spPhase({ spawnStatus: "probing" }, "shell") === "probe", "probing → probe");
ok(M.spPhase({ spawnStatus: "idle" }, "shell") === "probe", "idle → probe");
ok(M.spPhase({ spawnStatus: "disabled" }, "shell") === "probe", "disabled（外部后端）→ probe");
ok(M.spPhase({ spawnStatus: "starting" }, "probe") === "engine", "starting → engine");
ok(M.spPhase({ healthOk: true }, "engine") === "link", "探活通过 → link");
ok(M.spPhase({ spawnStatus: "ready" }, "engine") === "link", "spawn ready → link");
ok(M.spPhase({ spawnStatus: "running-external" }, "shell") === "link", "外部后端在跑 → link");
ok(M.spPhase({ healthOk: true, wsNav: true }, "link") === "load", "webview 真实导航 → load");
ok(M.spPhase({ wsReady: true }, "load") === "done", "renderer 判 ready → done");
// 只进不退：健康后 spawn 状态抖回 starting / failed，阶段不倒车
ok(M.spPhase({ spawnStatus: "starting" }, "link") === "link", "spawn 抖动不把 link 拉回 engine");
ok(M.spPhase({ spawnStatus: "failed" }, "engine") === "engine", "failed 冻结阶段（错误是正交维度）");
ok(M.spPhase({}, "load") === "load", "信号全丢仍守住已达阶段");
ok(M.spPhase({ wsReady: true }, "bogus") === "done", "非法 prev 按 shell 兜底再推导");

// ── 进度映射：段内插值 + 封顶 + 只增不减 ─────────────────────────────────
ok(M.spProgressTarget("shell", 0, 30000) === 0, "shell 段起点 0");
ok(M.spProgressTarget("shell", 99999, 30000) === 5, "shell 段封顶 5");
ok(M.spProgressTarget("probe", 99999, 30000) === 12, "probe 段封顶 12");
{
  // engine 段：ETA 中点应明显推进但不越 66.5 封顶；封顶 < link 起点 68（脉冲跳变刻意保留）
  const mid = M.spProgressTarget("engine", 15000, 30000);
  ok(mid > 30 && mid < 66.5, `engine 段 ETA 中点在 (30, 66.5) 内（实测 ${mid.toFixed(1)}）`);
  ok(M.spProgressTarget("engine", 999999, 30000) === 66.5, "engine 段封顶 66.5");
  ok(M.SEGMENTS.engine.hi < M.SEGMENTS.link.lo, "engine 封顶 < link 起点（里程碑跳变契约）");
  ok(M.spProgressTarget("link", 0, 30000) === 68, "link 段起点 68");
  ok(M.spProgressTarget("load", 999999, 30000) === 99, "load 段封顶 99（100 只属真实 ready）");
  ok(M.spProgressTarget("done", 0, 30000) === 100, "done = 100");
}
{
  // 全程模拟：阶段推进 + 时间推进，进度必须单调不减
  const steps = [
    ["shell", 200], ["shell", 500], ["probe", 300], ["probe", 1600],
    ["engine", 3000], ["engine", 12000], ["engine", 29000], ["engine", 45000],
    ["link", 400], ["link", 2600], ["load", 1000], ["load", 9000], ["done", 0],
  ];
  let prev = 0;
  for (const [phase, t] of steps) {
    const next = M.spProgress(prev, M.spProgressTarget(phase, t, 30000));
    ok(next >= prev, `进度只增不减（${phase}@${t}ms：${prev.toFixed(1)} → ${next.toFixed(1)}）`);
    prev = next;
  }
  ok(prev === 100, "走完全程收敛到 100");
}
ok(M.spProgress(50, 30) === 50, "目标回落时保持已显示进度（不倒车）");
ok(M.spProgress(150, 30) === 100, "进度全局封顶 100");

// ── 阶段序号（1..6，真实行 [阶段 n/6] 用）────────────────────────────────
ok(M.spStageNum("shell") === 1 && M.spStageNum("done") === 6, "阶段序号 1..6");
ok(M.PHASES.length === 6, "六阶段契约");

// ── 氛围文案轮换：确定性 + 界内 ──────────────────────────────────────────
ok(M.spAmbientIndex("engine", 0) === 0, "轮换起点 idx=0");
ok(M.spAmbientIndex("engine", 0) === M.spAmbientIndex("engine", 0), "同输入恒同输出");
ok(M.spAmbientIndex("engine", M.AMBIENT_ROTATE_MS) === 1, "一个节拍后推进到 idx=1");
{
  const n = M.AMBIENT_COUNTS.engine;
  ok(M.spAmbientIndex("engine", M.AMBIENT_ROTATE_MS * n) === 0, "轮换按池大小回绕");
  for (let t = 0; t < M.AMBIENT_ROTATE_MS * (n + 2); t += 700) {
    const i = M.spAmbientIndex("engine", t);
    ok(i >= 0 && i < n, `索引恒在池内（t=${t} → ${i}）`);
  }
}
ok(M.spAmbientKey("engine", 0) === "splash.amb.engine.0", "氛围键拼接格式");

// ── 与 shell-i18n 词典的双向契约 ─────────────────────────────────────────
for (const phase of M.PHASES) {
  const n = M.AMBIENT_COUNTS[phase];
  ok(n >= 1, `${phase} 氛围池非空`);
  for (let i = 0; i < n; i++) {
    const k = M.spAmbientKey(phase, i);
    ok(typeof DICT.zh[k] === "string" && DICT.zh[k].trim(), `zh 词条存在：${k}`);
    ok(typeof DICT.en[k] === "string" && DICT.en[k].trim(), `en 词条存在：${k}`);
  }
  ok(!(M.spAmbientKey(phase, n) in DICT.zh),
    `池大小与词典一致（${phase} 不存在越界键 .${n}——多写了词条就把 AMBIENT_COUNTS 同步调大）`);
  ok(`splash.stage.${phase}` in DICT.zh && `splash.stage.${phase}` in DICT.en, `阶段词条齐平：${phase}`);
  if (phase !== "done") {
    ok(`splash.term.${phase}` in DICT.zh && `splash.term.${phase}` in DICT.en, `终端自检词条齐平：${phase}`);
  }
}
for (const k of ["splash.real", "splash.real_eta", "splash.real_slow", "splash.skip",
  "splash.brand.co", "splash.tagline", "splash.err.title", "splash.err.retry",
  "splash.err.copy", "splash.err.copied"]) {
  ok(k in DICT.zh && k in DICT.en, `固定词条齐平：${k}`);
}
// 老板点名的三条氛围文案必须在池内（挪位/删除＝需求回归）
ok(DICT.zh["splash.amb.engine.0"].indexOf("量子计算机群") >= 0, "「唤醒量子计算机群」在 engine 池");
ok(DICT.zh["splash.amb.engine.1"].indexOf("深度神经网络") >= 0, "「深度神经网络数据搭建」在 engine 池");
ok(DICT.zh["splash.amb.link.0"].indexOf("加密安全隧道") >= 0, "「高强度加密安全隧道」在 link 池");

// ── ETA / 剩余 / 慢速判定 ────────────────────────────────────────────────
ok(M.spEtaMs(null) === 30000, "ETA 缺省 30s");
ok(M.spEtaMs("garbage") === 30000, "非法输入回缺省");
ok(M.spEtaMs(1000) === 8000, "ETA 下夹 8s");
ok(M.spEtaMs(600000) === 90000, "ETA 上夹 90s（waitForReady 放弃线）");
ok(M.spEtaMs(22000) === 22000, "实测值直用");
ok(M.spRemainSecs(1000, 30000) === null, "前 2.5s 不给预估（还没校准感）");
ok(M.spRemainSecs(10000, 30000) === 20, "中段给出剩余秒数");
ok(M.spRemainSecs(31000, 30000) === null, "超过 ETA 不给预估（不编数字）");
ok(M.spSlow(30000, 30000) === false, "刚到 ETA 不算慢");
ok(M.spSlow(43000, 30000) === true, "超 1.4×ETA 判慢（换诚实安抚口径）");
ok(M.spSlow(12500, 8000) === false, "短 ETA 至少超 6s 才判慢");
ok(M.spSlow(14500, 8000) === true, "短 ETA 超 6s 判慢");

// ── 模式判定（老板拍板：冷启全幕 / 暖启闪场 / 白标让位）──────────────────
ok(M.spInitMode({ whiteLabel: true, healthOk: true }) === "skip", "白标最高优先：整层让位");
ok(M.spInitMode({ healthOk: true }) === "warm", "首探活即健康 → 暖场闪现");
ok(M.spInitMode({ healthOk: false, reducedMotion: true }) === "cold-min", "reduced-motion → 极简");
ok(M.spInitMode({ pref: "min" }) === "cold-min", "用户偏好极简 → 极简");
ok(M.spInitMode({}) === "cold-full", "默认冷启动 → 三幕全版");

// ── 错误态与遥测分桶 ─────────────────────────────────────────────────────
ok(M.spIsErrorStatus("failed") && M.spIsErrorStatus("port-conflict"), "failed/port-conflict 归错误");
ok(!M.spIsErrorStatus("starting") && !M.spIsErrorStatus(""), "常规态不归错误");
ok(M.spDurBucket(5000) === "lt10" && M.spDurBucket(15000) === "lt30", "时长分桶 lt10/lt30");
ok(M.spDurBucket(45000) === "lt60" && M.spDurBucket(120000) === "ge60", "时长分桶 lt60/ge60");

console.log(`splash-model.test.js: ${passed} passed`);
