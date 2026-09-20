"use strict";

// 验收参照页防陈旧门禁：acceptance-preview.html 是六批改动的「一站式当前状态」人眼验收页，
// 必须由**真** pure 模块驱动、且覆盖全部六批——漏一批＝验收时那批没被看到就上了车。
// 同时钉住旧的三个分批夹具已删（合并进本页，避免四份重叠夹具漂移）。
// 跑法：node test/acceptance-preview.test.js

const assert = require("assert");
const fs = require("fs");
const path = require("path");

const dir = path.join(__dirname);
const html = fs.readFileSync(path.join(dir, "acceptance-preview.html"), "utf8");

let pass = 0;
function ok(name, cond) { assert.ok(cond, name); pass++; }

// 必须加载全部四个真模块（单一事实源；不得改成硬编码副本）。路径按各自**真实位置**钉：
// inject-status/webmulti/copilot-capabilities 在 desktop/renderer；bubble-model 在 shared/inject
// （真壳经 webview preload 加载它）——路径写错＝404＝整页渲染 abort（本轮实锤踩过）。
const _MODS = [
  ["inject-status.js", "/desktop/renderer/"],
  ["webmulti.js", "/desktop/renderer/"],
  // 2026-08-12 能力橱窗数据迁入 shared/copilot/ 单源（web 统一 App 同吃）
  ["cp-capabilities.js", "/desktop/renderer/shared/copilot/"],
  ["bubble-model.js", "/shared/inject/"],
];
_MODS.forEach(([m, loc]) => {
  ok("加载真模块 " + loc + m, html.indexOf(loc + m) > 0);
  // 该路径下真有此文件（防再次写错位置导致 404 白页）
  const p = path.join(dir, "..", "..", loc.replace(/^\//, ""), m);
  ok("模块文件存在 " + m, fs.existsSync(p));
});

// 必须调用真 pure 函数渲染（而非贴死 HTML）——每批的驱动函数各点一次名
ok("P1/P1.5 走 bubbleRenderModel + bubbleStyleCss",
  html.indexOf("bubbleRenderModel(") > 0 && html.indexOf("bubbleStyleCss()") > 0);
ok("P2 走 deriveInjectState + railBadge",
  html.indexOf("deriveInjectState(") > 0 && html.indexOf("railBadge(") > 0);
ok("P3 走 capabilityShowcase", html.indexOf("capabilityShowcase()") > 0);
// FAB 演示走 SVG spark（sparkSvg/createElementNS）而非 emoji 图标。注意：文件头验收清单里
// 会出现「无 🤖」这类**说明性**字样,故不做全文件 emoji 扫描,只钉渲染路径用 SVG。
ok("FAB 用 SVG spark 渲染", html.indexOf("createElementNS") > 0 && html.indexOf("sparkSvg(") > 0);

// 六批分区标题都在（漏一批＝验收看不到）
["P1 / P1.5", "FAB", "P2", "P3"].forEach((k) => {
  ok("含分区 " + k, html.indexOf(k) > 0);
});

// 文件头两层验收清单在（机械层 predist + 人眼层真壳）
ok("含机械层清单", html.indexOf("机械层") > 0 && html.indexOf("predist") > 0);
ok("含人眼层清单", html.indexOf("人眼层") > 0 && html.indexOf("遥测") > 0);

// 旧三个分批夹具已删（合并进本页，防四份重叠漂移）
["bubble-visual-preview.html", "rail-health-preview.html", "copilot-showcase-preview.html"].forEach((f) => {
  ok("旧夹具已删 " + f, !fs.existsSync(path.join(dir, f)));
});

console.log("acceptance-preview.test.js: " + pass + " passed");
