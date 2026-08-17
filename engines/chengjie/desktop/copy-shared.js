"use strict";
/* prestart 钩子:把 repo 根的 shared/copilot 同步到 renderer/shared/copilot。
   桌面 CSP 'self' 需本地加载;单一事实来源仍在 repo 根 shared/copilot。 */
const fs = require("fs");
const path = require("path");

const SRC = path.resolve(__dirname, "..", "shared", "copilot");
const DST = path.join(__dirname, "renderer", "shared", "copilot");
const INJECT_SRC = path.resolve(__dirname, "..", "shared", "inject");
const INJECT_DST = path.join(__dirname, "shared", "inject");
const BRAND_SRC = path.resolve(__dirname, "..", "src", "web", "static", "brand");
const BRAND_DST = path.join(__dirname, "renderer", "brand");

function copyDir(src, dst) {
  fs.mkdirSync(dst, { recursive: true });
  for (const ent of fs.readdirSync(src, { withFileTypes: true })) {
    const s = path.join(src, ent.name);
    const d = path.join(dst, ent.name);
    if (ent.isDirectory()) copyDir(s, d);
    else fs.copyFileSync(s, d);
  }
}

try {
  if (!fs.existsSync(SRC)) {
    console.warn(`[copy-shared] 源不存在,跳过: ${SRC}`);
    process.exit(0);
  }
  copyDir(SRC, DST);
  console.log(`[copy-shared] ok: ${SRC} → ${DST}`);
  // 主进程/outbound-pace 必须 require 进 asar 内的副本。装机版从 app.asar
  // 用 ../shared/inject 跨出 resources/ 在 Electron 31 主进程会 MODULE_NOT_FOUND
  // （1.0.28 实锤：开发机与 extraResources 文件都在，双击仍起不来）。
  if (fs.existsSync(INJECT_SRC)) {
    copyDir(INJECT_SRC, INJECT_DST);
    console.log(`[copy-shared] ok: ${INJECT_SRC} → ${INJECT_DST}`);
  }
  if (fs.existsSync(BRAND_SRC)) {
    copyDir(BRAND_SRC, BRAND_DST);
    console.log(`[copy-shared] ok: ${BRAND_SRC} → ${BRAND_DST}`);
  }
} catch (e) {
  console.error(`[copy-shared] 失败: ${e}`);
  process.exit(0); // 不阻断启动
}
