"use strict";

/* 把单一源 shared/inject/*.js 同步进扩展 vendor/（MV3 content_scripts 只能引用包内文件）。
 * 与 desktop/copy-shared.js 同理：唯一事实来源仍在 repo 根 shared/inject。
 * 用法：node extension/build.js（加载/打包扩展前先跑一次）。 */

const fs = require("fs");
const path = require("path");

const SRC = path.resolve(__dirname, "..", "shared", "inject");
const DST = path.join(__dirname, "vendor");
// 清单必须与 manifest.json 的 content_scripts 加载清单一致（门禁
// desktop/test/extension-vendor-sync.test.js 双向钉住：拷了不加载=死文件,加载了不拷=装不上）。
// translate-scheduler / bubble-model 是 core 的可选依赖：扩展侧没有 require,靠各模块
// 自注册到 globalThis 被 core 认到,故只要进包就自动生效,bridge.js 零改动。
const FILES = [
  "profiles.js",
  "media-format.js",
  "translate-scheduler.js",
  "bubble-model.js",
  "core.js",
];

try {
  if (!fs.existsSync(SRC)) {
    console.error(`[ext-build] 源不存在: ${SRC}`);
    process.exit(1);
  }
  fs.mkdirSync(DST, { recursive: true });
  for (const f of FILES) {
    fs.copyFileSync(path.join(SRC, f), path.join(DST, f));
  }
  console.log(`[ext-build] ok: ${SRC} → ${DST}（${FILES.join(", ")}）`);
} catch (e) {
  console.error(`[ext-build] 失败: ${e}`);
  process.exit(1);
}
