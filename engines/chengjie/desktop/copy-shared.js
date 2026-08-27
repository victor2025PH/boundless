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
// 热补丁落地脚本：运维侧（push_chatx_hotpatch → SSH 节点）与壳内自助更新用**同一份**
// deploy/desktop/apply_chatx_hotpatch_node.ps1。壳需要它随包（退出后由脱离进程执行），
// 所以在这里镜像进 app 目录而不是复制一份源码——两份脚本迟早会漂移，而漂移的那天
// 是「装了一半的补丁」。
const HOTPATCH_SRC = path.resolve(__dirname, "..", "..", "..", "deploy", "desktop", "apply_chatx_hotpatch_node.ps1");
const HOTPATCH_DST = path.join(__dirname, "hotpatch", "apply_chatx_hotpatch_node.ps1");

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
  if (fs.existsSync(HOTPATCH_SRC)) {
    fs.mkdirSync(path.dirname(HOTPATCH_DST), { recursive: true });
    fs.copyFileSync(HOTPATCH_SRC, HOTPATCH_DST);
    console.log(`[copy-shared] ok: ${HOTPATCH_SRC} → ${HOTPATCH_DST}`);
  } else {
    console.warn(`[copy-shared] 热补丁落地脚本不存在，壳内自助更新将不可用: ${HOTPATCH_SRC}`);
  }
} catch (e) {
  console.error(`[copy-shared] 失败: ${e}`);
  process.exit(0); // 不阻断启动
}
