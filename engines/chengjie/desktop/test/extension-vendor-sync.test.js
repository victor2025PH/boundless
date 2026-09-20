"use strict";

/* 扩展 vendor 镜像同步门禁（无框架,node 直跑）：node test/extension-vendor-sync.test.js
 *
 * extension/vendor/*.js 是从单一源 shared/inject/ 拷出的**构建产物**（MV3 content_scripts
 * 只能引用扩展包内文件）。这类镜像靠人记必然漂移：改完 shared/inject 忘跑
 * `node extension/build.js`,扩展就静默打包一份旧核心——功能「改好了」而装扩展的坐席永远
 * 踩旧 bug,全程零报错零迹象。本门禁把漂移变成红灯：
 *   ① 每个 vendor 文件必须与源逐字节一致；
 *   ② build.js 的拷贝清单与 manifest 的加载清单必须互相覆盖（拷了不加载＝死文件；
 *      加载了不拷＝扩展装不起来）；
 *   ③ core 的可选依赖必须走 globalThis 回落 + 各模块自注册（扩展侧无 require,这是
 *      「进包即生效、bridge.js 零改动」的唯一凭据）。
 */

const assert = require("assert");
const fs = require("fs");
const path = require("path");

let pass = 0;
function ok(name, cond) { assert.ok(cond, name); pass++; }

const ROOT = path.join(__dirname, "..", "..");
const SRC = path.join(ROOT, "shared", "inject");
const VENDOR = path.join(ROOT, "extension", "vendor");

// ── ①② 清单互相覆盖 ─────────────────────────────────────────────────────────
const buildSrc = fs.readFileSync(path.join(ROOT, "extension", "build.js"), "utf8");
const m = buildSrc.match(/const FILES = \[([\s\S]*?)\]/);
ok("build.js 有可解析的拷贝清单", !!m);
const copied = m[1]
  .split(",")
  .map((s) => s.trim().replace(/\/\/.*$/gm, "").trim().replace(/^["']|["']$/g, ""))
  .filter((s) => s.endsWith(".js"));
ok("拷贝清单非空", copied.length > 0);

const manifest = JSON.parse(
  fs.readFileSync(path.join(ROOT, "extension", "manifest.json"), "utf8")
);
const loaded = (manifest.content_scripts || [])
  .reduce((acc, cs) => acc.concat(cs.js || []), [])
  .filter((p) => String(p).indexOf("vendor/") === 0)
  .map((p) => p.slice("vendor/".length));
ok("manifest 加载清单非空", loaded.length > 0);

copied.forEach((f) => ok(`manifest 加载了拷进来的 ${f}`, loaded.indexOf(f) >= 0));
loaded.forEach((f) => ok(`build.js 会拷 manifest 要加载的 ${f}`, copied.indexOf(f) >= 0));
// core 必须最后加载：它在 createInject 时读 globalThis 上的可选依赖
ok("core.js 排在 vendor 加载序最后", loaded[loaded.length - 1] === "core.js");

// ── ① 逐字节一致 ────────────────────────────────────────────────────────────
copied.forEach((f) => {
  const s = path.join(SRC, f);
  const d = path.join(VENDOR, f);
  ok(`源存在 ${f}`, fs.existsSync(s));
  ok(`vendor 已生成 ${f}`, fs.existsSync(d));
  if (fs.existsSync(s) && fs.existsSync(d)) {
    ok(
      `vendor 与源逐字节一致 ${f}（红了就跑 node extension/build.js）`,
      fs.readFileSync(s).equals(fs.readFileSync(d))
    );
  }
});

// ── ③ 可选依赖的 globalThis 契约 ─────────────────────────────────────────────
const coreSrc = fs.readFileSync(path.join(SRC, "core.js"), "utf8");
ok("core 认 globalThis.ATranslateScheduler", coreSrc.indexOf("globalThis.ATranslateScheduler") >= 0);
ok("core 认 globalThis.ABubbleModel", coreSrc.indexOf("globalThis.ABubbleModel") >= 0);
[
  ["translate-scheduler.js", "ATranslateScheduler"],
  ["bubble-model.js", "ABubbleModel"],
  ["profiles.js", "AInjectProfiles"],
].forEach(([f, gname]) => {
  const s = fs.readFileSync(path.join(SRC, f), "utf8");
  ok(`${f} 自注册 globalThis.${gname}`, s.indexOf("globalThis." + gname) >= 0);
  ok(`${f} 同时保留 CommonJS 导出（桌面 preload 走 require）`, s.indexOf("module.exports") >= 0);
});

// 扩展侧也必须显式接线（隐式摸 globalThis 漏了不报错,只静默退回旧行为）
const bridgeSrc = fs.readFileSync(
  path.join(ROOT, "extension", "content", "bridge.js"), "utf8"
);
ok("bridge 显式接调度器", /createInject\([\s\S]{0,200}translateScheduler/.test(bridgeSrc));
ok("bridge 显式接渲染模型", /createInject\([\s\S]{0,200}bubbleModel/.test(bridgeSrc));

console.log(`extension-vendor-sync.test.js: ${pass} passed`);
