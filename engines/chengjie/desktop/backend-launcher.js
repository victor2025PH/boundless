"use strict";

// 后端 sidecar 生命周期管理（P0 交付：免手动起 Python）。
//
// 设计目标：把「后端如何产出」与「Electron 如何管理后端进程」解耦——
//   · 开发态：用系统 Python 跑 `python main.py`（仓库根）。
//   · 发布态：用 PyInstaller 打出的自包含二进制（随安装包放进 resources/backend/）。
// 两者经同一解析器切换。对「先手动起后端」的老流程零回归：拉起前先探活，已在跑就跳过。
//
// 本模块拆成「纯解析器（resolveBackendSpawn，可单测）」+「生命周期（spawn/health/kill）」两层。

const FP_HEALTH_PATH = "/login"; // 无需鉴权即返回 200，任何 HTTP 响应都代表后端可达
// 身份探针：判断目标端口上的**是不是自家后端**。原先只要有 HTTP 响应就"复用，不重复
// 拉起"——端口被别的程序或上一版本残留后端占着时，壳会连上去当自己的用，症状是
// 工作台能开、部分页面 404/500，极难排查。老后端没有此端点 → 返回 null，按旧行为放行。
const FP_IDENTITY_PATH = "/api/desktop/ping";
const EXPECTED_APP_ID = "chengjie";

/**
 * 解析后端启动命令（纯函数，便于单测）。
 *
 * @param {object} o
 * @param {object} o.config        desktop config.json（读 backend.base_url / backend.spawn）
 * @param {boolean} o.isPackaged   app.isPackaged
 * @param {string} o.resourcesPath process.resourcesPath（发布态二进制所在）
 * @param {string} o.appDir        __dirname（desktop 目录；其上级=仓库根）
 * @param {string} o.platform      process.platform（'win32' | 'darwin' | 'linux'）
 * @param {string} [o.dataDir]     发布态可写数据根（cwd + AITR_* env 指向此处；缺省=不重定向）
 * @param {(p:string)=>boolean} o.exists  文件存在判断（注入以便单测）
 * @returns {{command:string,args:string[],cwd:string,kind:string,env:object}|null}
 *          null = 不应由桌面拉起（显式关闭，或无可用产出）
 */
/**
 * 从桌面 config.json 的 backend 段派生后端 web env：让「后端 serve 的 host/port/token」
 * 与「renderer 调用的 base_url/token」强一致——否则随包 example 的端口(18787)/占位令牌
 * 与桌面默认(18799/admin)不符，全新安装的桌面壳永远连不上后端。base_url 解析失败则跳过
 * host/port（回落 config 默认），不抛错。
 * @param {{base_url?:string, token?:string}} backend
 * @returns {{AITR_WEB_HOST?:string, AITR_WEB_PORT?:string, AITR_WEB_TOKEN?:string}}
 */
function webEnvFromBackend(backend) {
  const env = {};
  try {
    if (backend && backend.base_url) {
      const u = new URL(String(backend.base_url));
      if (u.hostname) env.AITR_WEB_HOST = u.hostname;
      if (u.port) env.AITR_WEB_PORT = u.port;
    }
  } catch (e) {
    /* base_url 非法：跳过 host/port，后端用 config 默认端口 */
  }
  if (backend && backend.token) env.AITR_WEB_TOKEN = String(backend.token);
  return env;
}

function resolveBackendSpawn(o) {
  const cfg = (o && o.config) || {};
  const backend = cfg.backend || {};
  const spawnCfg = backend.spawn || {};
  // 显式关闭：用户自管后端（保留老流程）
  if (spawnCfg.enabled === false) return null;

  const platform = o.platform || process.platform;
  const exists = o.exists || (() => false);
  const isWin = platform === "win32";
  const path = require("path");

  // ① 显式覆写：config.backend.spawn.command（+ args/cwd）优先级最高
  if (spawnCfg.command) {
    return {
      command: String(spawnCfg.command),
      args: Array.isArray(spawnCfg.args) ? spawnCfg.args.map(String) : [],
      cwd: spawnCfg.cwd ? String(spawnCfg.cwd) : (o.appDir || process.cwd()),
      kind: "explicit",
      env: {},
    };
  }

  // ② 发布态：随包二进制 resources/backend/backend(.exe)
  if (o.isPackaged && o.resourcesPath) {
    const binName = isWin ? "backend.exe" : "backend";
    const binPath = path.join(o.resourcesPath, "backend", binName);
    if (exists(binPath)) {
      // 关键：cwd/env 指向用户可写 dataDir，使 config + 兄弟文件（dbs/json/logs）落到可写区，
      // 而非只读安装包。PyInstaller bootloader 经 exe 路径定位 _internal，与 cwd 无关，故可安全改 cwd。
      const dataDir = o.dataDir ? String(o.dataDir) : "";
      // 打包态默认桌面模式：后端跳过 config-Telegram 协议号初始化，
      // 让「纯收件箱/网页翻译」形态无需任何凭证即可开机；
      // 并把 web host/port/token 对齐桌面壳，保证 renderer 连得上后端。
      // 把壳版本注入后端：/api/desktop/ping 据此自报版本，壳复用时才能发现
      // 「装了新版却连着旧后端」这种错配（见 app_identity.py）。
      const env = Object.assign(
        { AITR_DESKTOP_MODE: "1" },
        o.appVersion ? { AITR_APP_VERSION: String(o.appVersion) } : {},
        webEnvFromBackend(backend));
      if (dataDir) {
        env.AITR_DATA_DIR = dataDir;
        env.AITR_CONFIG_PATH = path.join(dataDir, "config", "config.yaml");
      }
      return {
        command: binPath,
        args: [],
        cwd: dataDir || path.dirname(binPath),
        kind: "bundled",
        env,
      };
    }
  }

  // ③ 开发态：系统 Python 跑仓库根 main.py（desktop 的上级即仓库根；保持仓库相对，零回归）
  const repoRoot = path.resolve(o.appDir || process.cwd(), "..");
  const mainPy = path.join(repoRoot, "main.py");
  if (exists(mainPy)) {
    const python = spawnCfg.python ? String(spawnCfg.python) : (isWin ? "python" : "python3");
    return { command: python, args: ["main.py"], cwd: repoRoot, kind: "python", env: {} };
  }

  return null;
}

/** 后端健康探针 URL。 */
function healthUrl(config) {
  const base = ((config || {}).backend || {}).base_url || "http://127.0.0.1:18799";
  return String(base).replace(/\/+$/, "") + FP_HEALTH_PATH;
}

function identityUrl(config) {
  const base = ((config || {}).backend || {}).base_url || "http://127.0.0.1:18799";
  return String(base).replace(/\/+$/, "") + FP_IDENTITY_PATH;
}

/**
 * 判定「端口上这个后端能不能复用」（纯函数，便于单测）。
 *
 * @param {object|null} identity  /api/desktop/ping 的响应；null = 拿不到（老后端/非 JSON）
 * @param {string} shellVersion   桌面壳自身版本（package.json）
 * @returns {{reusable:boolean, foreign:boolean, versionMismatch:boolean, detail:string}}
 */
function classifyBackendIdentity(identity, shellVersion) {
  // 拿不到身份 = 老版本后端（0.2.2 之前没有该端点）→ 保持旧行为放行，
  // 否则新壳配旧后端会直接罢工，比它要解决的问题更严重。
  if (!identity || typeof identity !== "object" || !identity.app) {
    return { reusable: true, foreign: false, versionMismatch: false, detail: "legacy-or-unknown" };
  }
  const app = String(identity.app);
  if (app !== EXPECTED_APP_ID) {
    return {
      reusable: false, foreign: true, versionMismatch: false,
      detail: `端口被另一个服务占用（app=${app}）`,
    };
  }
  const ver = String(identity.version || "");
  // 版本不一致仍复用：它确实是自家后端，开发态壳/后端版本错开是常态。
  // 但要记下来——「装了新版却还连着旧后端」的疑难杂症全靠这条线索。
  const mismatch = !!(ver && shellVersion && ver !== "dev" && ver !== String(shellVersion));
  return {
    reusable: true, foreign: false, versionMismatch: mismatch,
    detail: mismatch ? `后端版本 ${ver} ≠ 壳版本 ${shellVersion}` : "",
  };
}

/**
 * 生命周期管理器。注入 electron/node 依赖以便测试与复用。
 *
 * @param {object} deps
 * @param {object} deps.app          electron app
 * @param {Function} deps.spawn      child_process.spawn
 * @param {Function} deps.exec       child_process.exec（Windows taskkill 用）
 * @param {object} deps.fs          node fs
 * @param {Function} deps.fetch     全局 fetch（健康探针）
 * @param {Function} [deps.log]     日志函数
 */
function createBackendManager(deps) {
  const app = deps.app;
  const spawn = deps.spawn;
  const exec = deps.exec;
  const fs = deps.fs;
  const path = require("path");
  const doFetch = deps.fetch || global.fetch;
  const log = deps.log || ((m) => console.log(`[backend] ${m}`));

  let child = null;
  let quitting = false;
  let starting = false; // 同进程重复/并发调用 start() 的幂等卫（防 TOCTOU 重复 spawn）
  // idle | probing | starting | ready | running-external | port-conflict | failed | disabled | stopped
  let status = "idle";
  let lastError = "";
  let logStream = null;
  let identity = null;        // 最近一次身份探针结果（null=未探到/老后端）
  let versionMismatch = false;

  function shellVersion() {
    try { return String(app && app.getVersion ? app.getVersion() : ""); } catch (e) { return ""; }
  }

  function getStatus() {
    return {
      status, lastError, pid: child && child.pid ? child.pid : null,
      identity, versionMismatch,
    };
  }

  async function probeHealth(config, timeoutMs) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs || 2500);
    try {
      const r = await doFetch(healthUrl(config), { method: "GET", redirect: "manual", signal: ctrl.signal });
      return !!r; // 任何响应=可达
    } catch (e) {
      return false;
    } finally {
      clearTimeout(t);
    }
  }

  /** 身份探针：拿 /api/desktop/ping 的 {app, version}；拿不到（老后端/非 JSON）返回 null。 */
  async function probeIdentity(config, timeoutMs) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs || 2500);
    try {
      const r = await doFetch(identityUrl(config), { method: "GET", signal: ctrl.signal });
      if (!r || !r.ok) return null;          // 404 = 老后端，按未知处理
      const j = await r.json();
      return (j && typeof j === "object" && j.app) ? j : null;
    } catch (e) {
      return null;
    } finally {
      clearTimeout(t);
    }
  }

  /** 轮询等待后端就绪。 */
  async function waitForReady(config, { tries = 60, intervalMs = 1000 } = {}) {
    for (let i = 0; i < tries; i++) {
      if (await probeHealth(config, 2000)) return true;
      await new Promise((res) => setTimeout(res, intervalMs));
    }
    return false;
  }

  function openLogStream() {
    try {
      const dir = path.join(app.getPath("userData"), "logs");
      fs.mkdirSync(dir, { recursive: true });
      logStream = fs.createWriteStream(path.join(dir, "backend.log"), { flags: "a" });
      logStream.write(`\n===== backend spawn @ ${new Date().toISOString()} =====\n`);
    } catch (e) {
      logStream = null;
    }
  }

  /**
   * 启动后端：先探活（已在跑→跳过，零回归）；否则解析命令并 spawn，
   * stdout/stderr 落 userData/logs/backend.log + 控制台镜像。
   */
  async function start(config) {
    // 幂等卫：已在拉起（starting）或已有存活子进程（child）→ 跳过，防 probe→spawn 的
    // TOCTOU 窗口被并发 start() 利用而重复 spawn（端口竞态→僵尸实例）。跨进程的重复
    // （多开桌面壳）由 main.js 的 Electron 单实例锁拦截，二者一内一外形成双保险。
    if (starting || child) {
      log(`start() 复用既有状态[${status}]，跳过重复拉起`);
      return;
    }
    starting = true;
    try {
      return await _doStart(config);
    } finally {
      starting = false;
    }
  }

  async function _doStart(config) {
    // 发布态：可写数据根 = userData/data；config + dbs/json/logs 都落这里（避免写只读安装包）。
    let dataDir = "";
    if (app && app.isPackaged) {
      try {
        dataDir = path.join(app.getPath("userData"), "data");
        fs.mkdirSync(path.join(dataDir, "config"), { recursive: true });
      } catch (e) {
        dataDir = "";
      }
    }

    const resolved = resolveBackendSpawn({
      config,
      isPackaged: !!(app && app.isPackaged),
      resourcesPath: process.resourcesPath,
      appDir: __dirname,
      platform: process.platform,
      dataDir,
      appVersion: shellVersion(),
      exists: (p) => { try { return fs.existsSync(p); } catch (e) { return false; } },
    });

    if (resolved === null && ((config.backend || {}).spawn || {}).enabled === false) {
      status = "disabled";
      log("backend.spawn.enabled=false → 由用户自管后端，跳过拉起");
      return;
    }

    // 已有后端在跑（含用户手动起 / 上次残留）→ 不重复拉起，避免端口冲突
    status = "probing";
    if (await probeHealth(config, 2000)) {
      // 端口上有东西在应答——但它是谁？核对身份再决定复用（见 classifyBackendIdentity）。
      identity = await probeIdentity(config, 2000);
      const verdict = classifyBackendIdentity(identity, shellVersion());
      versionMismatch = verdict.versionMismatch;
      if (!verdict.reusable) {
        status = "port-conflict";
        lastError = verdict.detail;
        log(`拒绝复用：${verdict.detail}。请改 config.json 的 backend.base_url 端口，或停掉占用该端口的程序`);
        return;
      }
      status = "running-external";
      let note = "";
      if (verdict.versionMismatch) note = `（注意：${verdict.detail}）`;
      // 把「无法确认身份」显式记下来：0.2.2 之前的后端没有 ping 端点，此时端口冲突
      // 仍是盲区。留一行日志，排查时能一眼看出判定是"确认过"还是"没得确认"。
      else if (!identity) note = "（未能确认后端身份：可能是 0.2.2 之前的旧后端）";
      log("检测到后端已在运行 → 复用，不重复拉起" + note);
      return;
    }

    if (!resolved) {
      status = "failed";
      lastError = "未找到后端产出（发布态缺 resources/backend，开发态缺 main.py 或 Python）";
      log(lastError);
      return;
    }

    openLogStream();
    status = "starting";
    log(`拉起后端[${resolved.kind}]：${resolved.command} ${resolved.args.join(" ")}（cwd=${resolved.cwd}）`);
    try {
      child = spawn(resolved.command, resolved.args, {
        cwd: resolved.cwd,
        env: Object.assign(
          {}, process.env,
          { PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" },
          resolved.env || {}),
        // posix 用独立进程组以便整组回收；win 用 taskkill /T 回收子树
        detached: process.platform !== "win32",
        windowsHide: true,
        stdio: ["ignore", "pipe", "pipe"],
      });
    } catch (e) {
      status = "failed";
      lastError = String((e && e.message) || e);
      log(`spawn 失败：${lastError}`);
      return;
    }

    const pipe = (buf) => {
      const s = buf.toString();
      if (logStream) { try { logStream.write(s); } catch (e) {} }
    };
    if (child.stdout) child.stdout.on("data", pipe);
    if (child.stderr) child.stderr.on("data", pipe);
    child.on("exit", (code, signal) => {
      const wasChild = child;
      child = null;
      if (quitting) return;
      status = "failed";
      lastError = `后端进程退出（code=${code} signal=${signal}）`;
      log(lastError + "；详见 userData/logs/backend.log");
      void wasChild;
    });

    const ok = await waitForReady(config, {
      tries: deps.readyTries || 90,
      intervalMs: deps.readyIntervalMs || 1000,
    });
    if (ok) {
      status = "ready";
      log("后端就绪");
    } else if (child) {
      status = "failed";
      lastError = "后端启动后 90s 内未就绪（可能仍在初始化或端口被占）";
      log(lastError);
    }
  }

  /** 回收后端进程（win→taskkill /T /F；posix→杀进程组）。退出时调用。 */
  function stop() {
    quitting = true;
    if (logStream) { try { logStream.end(); } catch (e) {} logStream = null; }
    if (!child || !child.pid) { status = "stopped"; return; }
    const pid = child.pid;
    try {
      if (process.platform === "win32") {
        // windowsHide：GUI 进程（Electron）无控制台，exec 默认会为 cmd.exe 新建
        // 可见控制台 → 用户退出应用瞬间闪黑窗；CREATE_NO_WINDOW 消除
        exec(`taskkill /pid ${pid} /T /F`, { windowsHide: true });
      } else {
        try { process.kill(-pid, "SIGTERM"); } catch (e) { try { child.kill("SIGTERM"); } catch (e2) {} }
        setTimeout(() => { try { process.kill(-pid, "SIGKILL"); } catch (e) {} }, 4000);
      }
    } catch (e) {
      try { child.kill(); } catch (e2) {}
    }
    child = null;
    status = "stopped";
  }

  return { start, stop, getStatus, probeHealth, probeIdentity, waitForReady };
}

module.exports = {
  resolveBackendSpawn, healthUrl, identityUrl, createBackendManager,
  classifyBackendIdentity, FP_HEALTH_PATH, FP_IDENTITY_PATH, EXPECTED_APP_ID,
  webEnvFromBackend,
};
