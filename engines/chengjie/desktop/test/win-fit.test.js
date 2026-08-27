"use strict";

// win-fit 纯函数单测（无框架，node 直跑）：node test/win-fit.test.js
// 背景：2026-08-17 .173 事故——4K@300% 逻辑桌面 1280x720 矮于壳固定 1280x820，
// composer 工具栏永久在折叠线下。这里钉住自适配与面包屑两块纯逻辑。
const assert = require("assert");
const { fitWindowBounds, displayBreadcrumb } = require("../win-fit.js");

let pass = 0;
function ok(name, cond) {
  assert.ok(cond, name);
  pass++;
}

// ── fitWindowBounds ──────────────────────────────────────────────────
// 正常桌面（1920x1040 工作区）：期望值原样通过，不最大化
let f = fitWindowBounds({ width: 1920, height: 1040 }, { width: 1280, height: 820 });
ok("正常桌面宽不动", f.width === 1280);
ok("正常桌面高不动", f.height === 820);
ok("正常桌面不最大化", f.maximize === false);

// .173 事故形态（4K@300% → 工作区 1280x672）：高被 clamp + 要求最大化
f = fitWindowBounds({ width: 1280, height: 672 }, { width: 1280, height: 820 });
ok("小桌面高 clamp 进工作区", f.height === 672);
ok("小桌面宽收敛", f.width === 1280);
ok("小桌面要求最大化", f.maximize === true);

// 恰好等于工作区：不算放不下，不最大化
f = fitWindowBounds({ width: 1280, height: 820 }, { width: 1280, height: 820 });
ok("恰好贴合不最大化", f.maximize === false);

// 窄屏（竖屏 1080x1860）：宽被 clamp
f = fitWindowBounds({ width: 1080, height: 1860 }, { width: 1280, height: 820 });
ok("竖屏宽 clamp", f.width === 1080 && f.maximize === true);

// 坏输入：workArea 缺失/为 0 → 回退期望值，不最大化，不抛
f = fitWindowBounds(null, { width: 1280, height: 820 });
ok("null 工作区回退期望值", f.width === 1280 && f.height === 820 && f.maximize === false);
f = fitWindowBounds({ width: 0, height: 0 }, { width: 1280, height: 820 });
ok("零工作区回退期望值", f.width === 1280 && f.height === 820 && f.maximize === false);

// desired 缺省 → 1280x820 出厂值
f = fitWindowBounds({ width: 800, height: 600 }, null);
ok("desired 缺省用出厂值并 clamp", f.width === 800 && f.height === 600 && f.maximize === true);

// winOpts 整对象直接喂（真实调用形态：含 title/webPreferences 等多余键）
f = fitWindowBounds({ width: 1280, height: 672 }, { width: 1280, height: 820, title: "x", webPreferences: {} });
ok("多余键不干扰", f.height === 672 && f.maximize === true);

// ── displayBreadcrumb ────────────────────────────────────────────────
// 4K@200%（.173 修复后形态）：logical 1920x1080，phys 反推 3840x2160
let b = displayBreadcrumb({
  size: { width: 1920, height: 1080 },
  workAreaSize: { width: 1920, height: 1032 },
  scaleFactor: 2,
});
ok("面包屑 scalePct", b.scalePct === 200);
ok("面包屑 phys 反推", b.physW === 3840 && b.physH === 2160);
ok("面包屑 logical", b.logicalW === 1920 && b.logicalH === 1080);
ok("面包屑工作区", b.workW === 1920 && b.workH === 1032);
ok("面包屑版本号", b.v === 1);
ok("面包屑时间戳存在", typeof b.ts === "string" && b.ts.length > 0);

// 125% 非整数 scale：scalePct 取整
b = displayBreadcrumb({ size: { width: 1536, height: 864 }, scaleFactor: 1.25 });
ok("125% 取整", b.scalePct === 125 && b.physW === 1920 && b.physH === 1080);

// 坏输入：null display / 无 scaleFactor → 回退 1，不抛
b = displayBreadcrumb(null);
ok("null display 不抛", b.scalePct === 100 && b.logicalW === 0);
b = displayBreadcrumb({ size: { width: 1280, height: 720 }, scaleFactor: 0 });
ok("scaleFactor=0 回退 1", b.scalePct === 100 && b.physW === 1280);

// extra 合并（displays 计数由调用方注入）
b = displayBreadcrumb({ size: { width: 100, height: 100 }, scaleFactor: 1 }, { displays: 2 });
ok("extra 合并", b.displays === 2);

// JSON 可序列化且 ASCII（探针 ConvertFrom-Json 消费）
const s = JSON.stringify(b);
ok("JSON 序列化 ASCII", /^[\x20-\x7e]+$/.test(s));

console.log(`win-fit.test.js: ${pass} assertions passed`);
