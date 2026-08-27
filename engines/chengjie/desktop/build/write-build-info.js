"use strict";
// predist 钩子：把「这个安装包到底是哪个源码状态打出来的」写成包内事实
// （extraResources → resources/build-info.json）。
//
// 背景（2026-08-13 复盘实锤）：1.0.23 打包时共享工作树带着 8/12~8/13 多条并行线的
// 未提交改动出货——事后想回答「包里到底是什么代码」只能靠文件 mtime 考古。共享树
// 多线并发下「发版快照 ≠ 任何一个 commit」是常态，包内自述是唯一可靠的追溯面。
//
// 消费方：
//   · deploy/desktop/chatx_fleet_status.ps1 -Telemetry —— SSH 读坐席机
//     resources/build-info.json，舰队表直接显示每台机器的 commit / 脏文件数；
//   · 人工核查：纯 JSON 明文，远程 type 一眼可读，不需要 asar 工具。
//
// 纪律：追溯信息缺失**不阻断打包**（git 不可用降级 unknown）；但文件本身必须
// 存在——after-pack REQUIRED 表有它，predist 漏跑这一步安装包拒绝出生。
const { execSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const desktopDir = path.resolve(__dirname, "..");
// git 仓库根 = boundless 单一大仓（engines/chengjie 的祖父目录）
const repoRoot = path.resolve(desktopDir, "..", "..", "..");

// 脏文件观察范围＝「会被打进包的源码路径」（与 extraResources / build_backend DATAS
// 对齐；写歪只影响追溯精度，不影响打包）。上限防爆，总数另记 dirty_count。
const SCOPE = [
  "engines/chengjie/main.py",
  "engines/chengjie/src",
  "engines/chengjie/shared",
  "engines/chengjie/desktop",
  "engines/chengjie/services",
  "engines/chengjie/config",
  "engines/chengjie/domains",
  "platform/credpool",
  "platform/licensing",
];
const DIRTY_CAP = 400;

function git(args) {
  return execSync("git " + args, {
    cwd: repoRoot,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
    windowsHide: true,
    timeout: 20000,
    maxBuffer: 8 * 1024 * 1024,
  }).trim();
}

function collect() {
  const pkg = JSON.parse(
    fs.readFileSync(path.join(desktopDir, "package.json"), "utf8"));
  const info = {
    name: "chatx-build-info",
    version: String(pkg.version || ""),
    displayVersion: String(pkg.displayVersion || ""),
    // internal=内测包（随包生产数据种子）；clean=对外干净包（零数据种子）。
    // 舰队/人工核查一眼分清装的是哪种形态，别再靠文件名与目录考古。
    flavor: ["clean", "lite"].includes(process.env.CHATX_FLAVOR) ? process.env.CHATX_FLAVOR : "internal",
    builtAt: new Date().toISOString(),
    builtOn: process.env.COMPUTERNAME || process.env.HOSTNAME || "",
    git: { commit: "unknown", branch: "unknown", dirty_count: -1, dirty: [] },
    backend: {},
  };
  try {
    info.git.commit = git("rev-parse HEAD");
    info.git.branch = git("rev-parse --abbrev-ref HEAD");
    const scoped = SCOPE.map((s) => '"' + s + '"').join(" ");
    const porcelain = git("status --porcelain -- " + scoped);
    const lines = porcelain ? porcelain.split(/\r?\n/).filter(Boolean) : [];
    info.git.dirty_count = lines.length;
    info.git.dirty = lines.slice(0, DIRTY_CAP);
    if (lines.length > DIRTY_CAP) {
      info.git.dirty.push("... (+" + (lines.length - DIRTY_CAP) + " more)");
    }
  } catch (e) {
    // git 不可用/超时：保留 unknown，不阻断打包（追溯降级但包照出）
  }
  try {
    const fp = JSON.parse(fs.readFileSync(
      path.join(__dirname, "backend-dist", ".source-fingerprint.json"), "utf8"));
    info.backend = {
      fingerprint: String(fp.aggregate || ""),
      built_at: String(fp.built_at || ""),
      file_count: Number(fp.file_count || 0),
    };
  } catch (e) { /* 后端未烤/无指纹：留空对象，如实表示未知 */ }
  return info;
}

const out = path.join(__dirname, "build-info.json");
fs.writeFileSync(out, JSON.stringify(collect(), null, 2) + "\n", "utf8");
console.log("[build-info] ok: " + out);
