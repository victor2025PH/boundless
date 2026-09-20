/**
 * QQ 客户端按需下载（不随安装包：QQ.exe 属腾讯，且 ~200MB）。
 *
 * 触发：本机没有受支持版本的 QQ 时，智聊连接弹窗给「下载并安装 QQ」按钮 → 边车 x_download_qq。
 * 行为：从腾讯官方地址（SUPPORTED_QQ_BUILDS[PINNED].url）流式下载安装包到 QQ_RUNTIME_DIR，
 *       进度写 state（/health.download_progress 每秒可读），完成后**静默安装**到运行时目录
 *       （QQ 官方安装器支持 /S 静默 + /D=目标目录；安装进用户可写区，不动用户自己的 QQ）。
 * 幂等：同一 build 已在下载/已安装 → 直接返回当前状态；失败可重试（state.error 带因）。
 * 安全：只允许 https://dldir1.qq.com / dldir1v6.qq.com 域（腾讯官方 CDN），禁止任意 URL。
 */
import fs from "node:fs";
import path from "node:path";
import { spawn } from "node:child_process";
import { PINNED_QQ_BUILD, SUPPORTED_QQ_BUILDS, locateQQ } from "./locate.js";

const ALLOWED_HOSTS = new Set(["dldir1.qq.com", "dldir1v6.qq.com"]);

const state = {
  phase: "idle",          // idle | downloading | installing | done | error
  build: "",
  url: "",
  received: 0,
  total: 0,
  percent: 0,
  error: "",
  installer: "",
  startedAt: 0,
  finishedAt: 0,
};

export function downloadState() { return { ...state }; }

function assertOfficial(url) {
  const u = new URL(url);
  if (u.protocol !== "https:" || !ALLOWED_HOSTS.has(u.hostname)) {
    throw new Error(`refusing non-official QQ download host: ${u.hostname}`);
  }
}

async function downloadTo(url, dest, onProgress) {
  assertOfficial(url);
  const res = await fetch(url, { redirect: "follow" });
  if (!res.ok || !res.body) throw new Error(`download HTTP ${res.status}`);
  const total = Number(res.headers.get("content-length") || 0);
  const tmp = dest + ".part";
  const out = fs.createWriteStream(tmp);
  let received = 0;
  const reader = res.body.getReader();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    received += value.byteLength;
    if (!out.write(Buffer.from(value))) await new Promise((r) => out.once("drain", r));
    onProgress(received, total);
  }
  await new Promise((r, j) => out.end((e) => (e ? j(e) : r())));
  fs.renameSync(tmp, dest);
}

function runInstaller(installer, targetDir) {
  return new Promise((resolve, reject) => {
    // QQ NT NSIS 安装器：/S 静默；/D=<dir> 必须是最后一个参数且不能带引号（NSIS 约定）
    const child = spawn(installer, ["/S", `/D=${targetDir}`], { stdio: "ignore", windowsHide: true });
    child.on("error", reject);
    child.on("exit", (code) => (code === 0 ? resolve() : reject(new Error(`installer exit ${code}`))));
  });
}

/**
 * 开始（或续接）按需下载 + 静默安装。返回当前 state 快照；实际工作在后台推进。
 * @param {object} o  { runtimeDir, build?, logger? }
 */
export function startDownload(o = {}) {
  const runtimeDir = String(o.runtimeDir || process.env.QQ_RUNTIME_DIR || "").trim();
  if (!runtimeDir) { state.phase = "error"; state.error = "QQ_RUNTIME_DIR not set"; return downloadState(); }
  const build = String(o.build || PINNED_QQ_BUILD);
  const spec = SUPPORTED_QQ_BUILDS[build];
  if (!spec) { state.phase = "error"; state.error = `unsupported build ${build}`; return downloadState(); }
  if (state.phase === "downloading" || state.phase === "installing") return downloadState();
  const already = locateQQ({ runtimeDir });
  if (already.installed && already.build === build) {
    Object.assign(state, { phase: "done", build, percent: 100, error: "", finishedAt: Date.now() });
    return downloadState();
  }
  const log = o.logger || console;
  Object.assign(state, { phase: "downloading", build, url: spec.url, received: 0, total: 0, percent: 0,
                         error: "", startedAt: Date.now(), finishedAt: 0 });
  fs.mkdirSync(runtimeDir, { recursive: true });
  const installer = path.join(runtimeDir, `QQ_${spec.version}_x64.exe`);
  state.installer = installer;
  (async () => {
    try {
      if (!fs.existsSync(installer)) {
        await downloadTo(spec.url, installer, (recv, total) => {
          state.received = recv; state.total = total;
          state.percent = total ? Math.min(99, Math.floor((recv / total) * 100)) : 0;
        });
      }
      state.phase = "installing"; state.percent = 99;
      if (process.platform === "win32") await runInstaller(installer, runtimeDir);
      const after = locateQQ({ runtimeDir });
      if (!after.installed) throw new Error("installer finished but QQ.exe not found in runtime dir");
      Object.assign(state, { phase: "done", percent: 100, finishedAt: Date.now() });
      log.info?.(`[qq-download] QQ ${spec.version} 已安装到 ${runtimeDir}`);
    } catch (e) {
      Object.assign(state, { phase: "error", error: String((e && e.message) || e).slice(0, 200) });
      log.warn?.(`[qq-download] 失败: ${state.error}`);
    }
  })();
  return downloadState();
}
