/**
 * 本机 QQNT 客户端定位（Windows 优先；Linux/macOS 走默认路径）——注入驱动的原料在哪。
 *
 * 判据与 QQ 官方安装布局一致：
 *   Windows  <InstallDir>\QQ.exe + <InstallDir>\versions\config.json{curVersion} + versions\<ver>\resources\app\wrapper.node
 *   Linux    /opt/QQ/qq + /opt/QQ/resources/app/wrapper.node
 *   macOS    /Applications/QQ.app/Contents/MacOS/QQ + …/Resources/app/wrapper.node
 *
 * 查找顺序（Windows）：
 *   ① QQ_RUNTIME_DIR（智聊按需下载到用户可写区的运行时，优先——不动用户自己的 QQ）
 *   ② 注册表 HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ 的 UninstallString 所在目录
 *   ③ 常见默认路径 %ProgramFiles%\Tencent\QQNT、%ProgramFiles(x86)%\Tencent\QQNT、%LOCALAPPDATA%\Tencent\QQNT
 *
 * 返回 { installed, exe, root, version, build, wrapper, source } —— 找不到 installed=false 其余空串。
 * 纯同步文件系统探测（毫秒级），/health 每次调用可承受；注册表读取用 reg.exe，失败静默跳过。
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";

/** 受支持的 QQ 版本表（版本锁定）：只在这张表里的 build 才允许注入；不在表里 → 用锁定版本下载到运行时目录。
 *  build = versions/config.json.curVersion 的 "-" 后半段（如 9.9.21-39038 → 39038）。
 *  每次 QQ 升级由工程侧验证后加一行（含官方下载地址），随边车热更下发。 */
export const SUPPORTED_QQ_BUILDS = {
  "39038": { version: "9.9.21-39038", url: "https://dldir1.qq.com/qqfile/qq/QQNT/Windows/QQ_9.9.21_250923_x64_01.exe" },
  "44343": { version: "9.9.26-44343", url: "https://dldir1.qq.com/qqfile/qq/QQNT/40d6045a/QQ9.9.26.44343_x64.exe" },
};
/** 按需下载时使用的锁定版本（表里经我们验证过的最新一档） */
export const PINNED_QQ_BUILD = "44343";

function exists(p) { try { return !!p && fs.existsSync(p); } catch { return false; } }

function readVersionsConfig(root) {
  try {
    const cfg = JSON.parse(fs.readFileSync(path.join(root, "versions", "config.json"), "utf8"));
    return String(cfg.curVersion || cfg.baseVersion || "");
  } catch { return ""; }
}

function winRegistryInstallDir() {
  if (process.platform !== "win32") return "";
  const keys = [
    "HKLM\\SOFTWARE\\WOW6432Node\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\QQ",
    "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\QQ",
    "HKCU\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\QQ",
  ];
  for (const k of keys) {
    try {
      const out = execFileSync("reg.exe", ["query", k, "/v", "UninstallString"], { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"], windowsHide: true });
      const m = /REG_SZ\s+(.+)$/m.exec(out);
      if (m) {
        const uninst = m[1].trim().replace(/^"|"$/g, "");
        const dir = path.dirname(uninst);
        if (exists(path.join(dir, "QQ.exe"))) return dir;
      }
    } catch { /* 键不存在/无权限：继续下一个 */ }
  }
  return "";
}

function winCandidates(runtimeDir) {
  const out = [];
  if (runtimeDir) out.push({ dir: runtimeDir, source: "runtime" });
  const reg = winRegistryInstallDir();
  if (reg) out.push({ dir: reg, source: "registry" });
  const pf = process.env["ProgramFiles"] || "C:\\Program Files";
  const pf86 = process.env["ProgramFiles(x86)"] || "C:\\Program Files (x86)";
  const lad = process.env["LOCALAPPDATA"] || path.join(os.homedir(), "AppData", "Local");
  for (const d of [path.join(pf, "Tencent", "QQNT"), path.join(pf86, "Tencent", "QQNT"), path.join(lad, "Tencent", "QQNT")]) {
    out.push({ dir: d, source: "default" });
  }
  return out;
}

/** 定位本机 QQNT。opts.runtimeDir = 智聊按需下载的运行时目录（优先）。 */
export function locateQQ(opts = {}) {
  const none = { installed: false, exe: "", root: "", version: "", build: "", wrapper: "", source: "" };
  if (process.platform === "win32") {
    for (const c of winCandidates(opts.runtimeDir || process.env.QQ_RUNTIME_DIR || "")) {
      const exe = path.join(c.dir, "QQ.exe");
      if (!exists(exe)) continue;
      const version = readVersionsConfig(c.dir);
      const build = version.includes("-") ? version.split("-").pop() : "";
      const wrapper = version ? path.join(c.dir, "versions", version, "resources", "app", "wrapper.node") : "";
      return { installed: true, exe, root: c.dir, version, build, wrapper: exists(wrapper) ? wrapper : "", source: c.source };
    }
    return none;
  }
  if (process.platform === "linux") {
    const root = "/opt/QQ";
    const exe = path.join(root, "qq");
    if (!exists(exe)) return none;
    const wrapper = path.join(root, "resources", "app", "wrapper.node");
    let version = "";
    try { version = String(JSON.parse(fs.readFileSync(path.join(root, "resources", "app", "package.json"), "utf8")).version || ""); } catch {}
    return { installed: true, exe, root, version, build: version.split("-").pop() || "", wrapper: exists(wrapper) ? wrapper : "", source: "default" };
  }
  if (process.platform === "darwin") {
    const root = "/Applications/QQ.app";
    const exe = path.join(root, "Contents", "MacOS", "QQ");
    if (!exists(exe)) return none;
    const wrapper = path.join(root, "Contents", "Resources", "app", "wrapper.node");
    return { installed: true, exe, root, version: "", build: "", wrapper: exists(wrapper) ? wrapper : "", source: "default" };
  }
  return none;
}

/** 该安装是否在支持表里（版本锁定判定）；未知 build → false（走按需下载锁定版）。 */
export function isSupportedBuild(build) {
  return Object.prototype.hasOwnProperty.call(SUPPORTED_QQ_BUILDS, String(build || ""));
}
