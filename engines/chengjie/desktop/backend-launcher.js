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
 *
 * #57（2026-08-30）「手机扫码操控连不上」根因就在这里：base_url 默认 127.0.0.1 →
 * AITR_WEB_HOST=127.0.0.1 → 后端只绑回环，二维码里的 LAN 地址（192.168.x.x:18799）
 * 手机永远打不开，弹窗自诊红字「局域网入口未就绪」说的就是它。现改为：serve 侧
 * 按 lanServeHost 决策是否绑 0.0.0.0（renderer 仍连 base_url 的回环地址，两者解耦）。
 * @param {{base_url?:string, token?:string, lan_access?:boolean}} backend
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
  const lanHost = lanServeHost(backend);
  if (lanHost) env.AITR_WEB_HOST = lanHost;
  return env;
}

/**
 * #57：后端 serve 是否放开到局域网（返回 "0.0.0.0" 或 ""=维持 base_url 回环）。
 *
 * 决策表（安全第一，逐条有据）：
 *   - `backend.lan_access === false` → 永不放开（显式关，运维逃生门）。
 *   - 令牌仍是出厂默认 "admin"/空 → **不放开**，除非 `lan_access === true` 显式
 *     兜底——admin/admin 面板暴露到整个局域网是比「扫码连不上」更糟的事故；
 *     托管版首启已轮换随机令牌（token-util），所以正常客户机天然满足强令牌。
 *   - base_url 指向非回环地址（用户自配远端后端）→ 不动（serve 归远端自己管）。
 *   - 其余（强令牌 + 回环 base_url）→ "0.0.0.0"：renderer 照走 127.0.0.1，
 *     手机经 LAN IP 可达，「手机扫码操控」开箱即用。
 * @param {{base_url?:string, token?:string, lan_access?:boolean}} backend
 * @returns {string}
 */
function lanServeHost(backend) {
  const b = backend || {};
  if (b.lan_access === false) return "";
  const tok = String(b.token == null ? "" : b.token).trim();
  const strong = !!tok && tok !== "admin";   // token-util.DEFAULT_TOKEN 同义
  if (!strong && b.lan_access !== true) return "";
  let host = "127.0.0.1";
  try {
    if (b.base_url) host = new URL(String(b.base_url)).hostname || host;
  } catch (e) { /* 非法 base_url：按回环处理 */ }
  if (host !== "127.0.0.1" && host !== "localhost" && host !== "::1") return "";
  return "0.0.0.0";
}

/**
 * 把 Electron 自身作为「Node 运行时」暴露给后端（LINE 协议扫码要用）。
 *
 * okline（LINE 扫码 + 之后每一次收发的 X-Hmac 签名）靠一个**持久 Node 子进程**加载
 * ltsm.wasm 算签名，没有 node 就扫不了码、也发不出消息。这里沿用 sidecar-launcher 里
 * 已实测的同款做法：不随包第二份 node.exe，直接把 `process.execPath` +
 * `ELECTRON_RUN_AS_NODE=1` 当 Node 20 用（Electron 31）。已实测 okline 的 LTSM 桥在该
 * 运行时下能正常 curvekey_generate + 算出 44 字节 X-Hmac，与真 node 无差别。
 *
 * 只是**回落**：后端仅在 PATH 上没有真 node 时才用它（见
 * line_protocol_login.resolve_node_runtime），装了 Node 的机器行为完全不变。
 * `ELECTRON_RUN_AS_NODE` 刻意**不**在这里设 —— 那会让后端的所有子进程都带上它；
 * 由后端在真正选中 Electron 时按需设（okline 的 Popen 继承 os.environ）。
 *
 * @param {string} execPath process.execPath
 * @returns {{AITR_ELECTRON_NODE?:string}}
 */
function electronNodeEnv(execPath) {
  const p = String(execPath || "").trim();
  return p ? { AITR_ELECTRON_NODE: p } : {};
}

/** 后端退出哨兵路径（纯函数，便于单测）。dataDir 空 → null（不清哨兵）。 */
function sentinelPathFor(dataDir) {
  const dir = String(dataDir || "").trim();
  if (!dir) return null;
  return require("path").join(dir, "logs", "run_sentinel.json");
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
      // 随包数据种子（内测/定制包）：resources/seed-data 存在才注入。后端在冻结态
      // 也会按 exe 位置自动发现同一目录（config_manager._seed_extras_dir），env 是
      // 显式契约 + 开发/冒烟态可覆写的那一半。标准包没有该目录 → 不注入，零变化。
      const seedDir = path.join(o.resourcesPath, "seed-data");
      // PYTHONUNBUFFERED（P1-198 可观测性实锤）：PyInstaller 后端 stdout 接进壳的
      // 管道后是**块缓冲**——启动猛刷一波后，零星日志攒不满 8KB 就不落
      // backend.log（198 排障时日志"断流"100 分钟，热重载完成行全憋在缓冲里）。
      // 行缓冲让 backend.log 实时可读，代价可忽略（日志量本就不大）。
      // WP-1 纯云起步档：打包桌面默认 cloud_light 部署档。后端只在「本次 config
      // 为全新播种」时才把 config/profiles/cloud_light.yaml 写进 overlay
      // （ConfigManager._ensure_deploy_profile 双闸），升级安装/老用户零影响；
      // 外部 process.env 显式设了别的档位则尊重之（spawn 时 resolved.env 覆盖
      // process.env，故这里必须自带回读）。
      const env = Object.assign(
        { AITR_DESKTOP_MODE: "1", PYTHONUNBUFFERED: "1",
          AITR_DEPLOY_PROFILE: process.env.AITR_DEPLOY_PROFILE || "cloud_light" },
        o.appVersion ? { AITR_APP_VERSION: String(o.appVersion) } : {},
        exists(seedDir) ? { AITR_SEED_DATA_DIR: seedDir } : {},
        electronNodeEnv(o.execPath),
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
    // 开发态也给 Electron-as-Node：让 LINE 扫码在 dev 与发布态走同一条回落路径
    // （否则「dev 能扫、装机不能」这类差异只会在客户机上才暴露）。
    return {
      command: python, args: ["main.py"], cwd: repoRoot, kind: "python",
      env: electronNodeEnv(o.execPath),
    };
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
  let backendDataDir = ""; // 本次拉起的后端数据根（stop() 清哨兵用，见 markCleanShutdown）
  let identity = null;        // 最近一次身份探针结果（null=未探到/老后端）
  let versionMismatch = false;
  let lastConfig = null;      // 最近一次拉起用的配置（崩溃自愈重拉要用同一份）
  let readySince = 0;         // 本次进程就绪时刻（判「稳过一段时间」→ 重置重拉计数）
  let respawnCount = 0;       // 连续自愈重拉次数（稳定运行后归零）
  let respawnTimer = null;

  function shellVersion() {
    // displayVersion（内测 1.001 展示号）优先：它同时喂给 AITR_APP_VERSION（后端
    // /api/desktop/ping 自报）与 classifyBackendIdentity 的壳侧比对——两侧同源
    // 才不会自己报自己「版本不一致」。缺字段回落 semver。
    try {
      const dv = require("./package.json").displayVersion;
      if (dv) return String(dv);
    } catch (e) { /* 回落 semver */ }
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

  /**
   * 收割「上一版本残留的随包后端」并等端口真静默（B57 升级风暴的运行时兜底）。
   *
   * 场景：安装器强杀壳的兜底路径（app 卡死/崩溃后升级）带不走 backend.exe——
   * 孤儿旧后端抱着 18799 与 pyrogram 会话文件继续跑。此时若照旧「复用」，新壳
   * 跑的是旧代码且孤儿无人看管；若不管不顾直接 spawn，新旧双进程抢同一份会话
   * → Telegram AuthKeyDuplicated 强制注销全部账号（skuio 机实录）。
   *
   * 安全边界：只按**精确 exe 路径全等**收割（绝不误伤别人的 backend.exe），且
   * 只在「打包态 + 身份确认是自家 + 版本确认错配 + 有随包二进制可拉起」四个
   * 条件齐备时被调用；端口静默（探活连续失败）才算「会话文件已释放」。
   */
  async function _reapStaleBundled(binPath, config) {
    if (process.platform !== "win32") return false; // 打包发行面 = Windows
    const esc = String(binPath || "").replace(/'/g, "''");
    if (!esc) return false;
    const cmd = `powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-Process | Where-Object { $_.Path -eq '${esc}' } | Stop-Process -Force -ErrorAction SilentlyContinue"`;
    const roundMs = Math.max(5, deps.reapRoundMs || 4000);
    const pollMs = Math.max(1, deps.reapPollMs || 400);
    for (let round = 0; round < 2; round++) {
      try { exec(cmd, { windowsHide: true }); } catch (e) { /* best-effort */ }
      const deadline = Date.now() + roundMs;
      while (Date.now() < deadline) {
        if (!(await probeHealth(config, 800))) return true; // 端口静默 = 旧后端已死
        await new Promise((res) => setTimeout(res, pollMs));
      }
    }
    return !(await probeHealth(config, 800));
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

  /**
   * 后端非主动退出后的退避重拉（2026-09-04 kouxing 事故）。
   *
   * 事故：后端撞满 Windows ``select()`` 的文件描述符上限 → 走防幽灵 ``exit 78``
   * 自杀，而壳这边只把 status 记成 failed 就不管了 → 端口再没人 LISTENING，
   * 壳一直空打 18799，坐席看到的是「一直连不上」，得有人远程 stop+relaunch
   * 才回来（实录静默掉线，靠值守巡检才发现）。
   *
   * 刻意**不做无限重启**：连续重拉封顶 ``respawnMax`` 次，之后停手并把 lastError
   * 留在 getStatus 里（壳红条/健康看板可见）——「起来就崩」必须让人看见，
   * 无限重启只会把真故障掩盖成一条永远在闪的状态灯。反之，只要新进程稳过
   * ``respawnStableMs`` 就把计数归零，于是「偶发崩一次」总能自愈。
   */
  function scheduleRespawn(code, signal) {
    const max = deps.respawnMax != null ? deps.respawnMax : 5;
    const delays = deps.respawnDelaysMs || [2000, 5000, 15000, 30000, 60000];
    const stableMs = deps.respawnStableMs != null ? deps.respawnStableMs : 120000;
    const later = deps.setTimeout || setTimeout;

    // 上一次跑够久 = 这是一次偶发崩溃，不是起飞即坠 → 重新给满额度
    if (readySince && (Date.now() - readySince) >= stableMs) respawnCount = 0;
    readySince = 0;

    if (max <= 0 || !lastConfig) return;
    if (respawnCount >= max) {
      // 复用 exit 处理已写好的中文 lastError，只补 ASCII 后缀——既保住坐席可读，
      // 又不给本文件的「硬编码中文串」ratchet 添新条目
      lastError = `${lastError} [auto-respawn gave up after ${respawnCount} tries]`;
      log(`giving up auto-respawn after ${respawnCount} tries (code=${code} signal=${signal}); see userData/logs/backend.log`);
      return;
    }
    const wait = delays[Math.min(respawnCount, delays.length - 1)];
    respawnCount += 1;
    log(`backend exited unexpectedly -> respawn in ${Math.round(wait / 1000)}s (attempt ${respawnCount}/${max})`);
    if (respawnTimer) { try { clearTimeout(respawnTimer); } catch (e) {} }
    respawnTimer = later(() => {
      respawnTimer = null;
      if (quitting || child) return;
      void start(lastConfig);
    }, wait);
    // 定时器不该把 Electron 主进程钉在事件循环里（退出时应能直接走）
    if (respawnTimer && typeof respawnTimer.unref === "function") {
      try { respawnTimer.unref(); } catch (e) {}
    }
  }

  async function _doStart(config) {
    lastConfig = config;
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
    backendDataDir = dataDir; // 供 stop() 定位哨兵

    const resolved = resolveBackendSpawn({
      config,
      isPackaged: !!(app && app.isPackaged),
      resourcesPath: process.resourcesPath,
      appDir: __dirname,
      platform: process.platform,
      dataDir,
      appVersion: shellVersion(),
      execPath: process.execPath,
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
      // 打包态 + 版本错配 + 有随包二进制 → 端口上是**上一版本升级后残留的孤儿后端**
      // （安装器强杀壳的兜底路径带不走 backend.exe）。旧行为「照样复用」意味着
      // 「装了新版却连着旧后端」永不自愈；而孤儿后端抱着 pyrogram 会话文件，
      // 与新后端并存即 AuthKeyDuplicated 全账号注销（B57）。故：按精确路径收割 →
      // 等端口真静默（=会话文件已释放）→ 落回正常 spawn 拉起当前版本。
      // 收割失败退回复用（旧后端也比没有后端强）；开发态（isPackaged=false）
      // 壳/后端版本错开是常态，保持旧的复用语义零回归。
      let reapedStale = false;
      if (verdict.versionMismatch && app && app.isPackaged &&
          resolved && resolved.kind === "bundled") {
        log(`stale backend from previous version detected (${verdict.detail}) -> reap by path, then spawn current`);
        reapedStale = await _reapStaleBundled(resolved.command, config);
        if (reapedStale) {
          identity = null;
          versionMismatch = false;
          log("stale backend reaped (port silent = session files released); spawning bundled backend");
        } else {
          log(`WARN stale backend reap failed, falling back to reuse (${verdict.detail})`);
        }
      }
      if (!reapedStale) {
        status = "running-external";
        let note = "";
        if (verdict.versionMismatch) note = `（注意：${verdict.detail}）`;
        // 把「无法确认身份」显式记下来：0.2.2 之前的后端没有 ping 端点，此时端口冲突
        // 仍是盲区。留一行日志，排查时能一眼看出判定是"确认过"还是"没得确认"。
        else if (!identity) note = "（未能确认后端身份：可能是 0.2.2 之前的旧后端）";
        log("检测到后端已在运行 → 复用，不重复拉起" + note);
        return;
      }
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
      scheduleRespawn(code, signal);
    });

    const ok = await waitForReady(config, {
      tries: deps.readyTries || 90,
      intervalMs: deps.readyIntervalMs || 1000,
    });
    if (ok) {
      status = "ready";
      readySince = Date.now();
      log("后端就绪");
    } else if (child) {
      status = "failed";
      lastError = "后端启动后 90s 内未就绪（可能仍在初始化或端口被占）";
      log(lastError);
    }
  }

  /**
   * 壳主动关闭前，把后端的退出哨兵删掉——标记「这是预期内的关闭，不是崩溃」。
   *
   * 背景：Windows 上 stop() 走 taskkill /F（强杀），后端来不及跑 atexit 清哨兵，
   * exit_sentinel 下次启动就把「用户正常关 App」误报成「异常退出/崩溃」，污染
   * 错误看板 + 触发误告警。壳知道 dataDir（=后端 CWD），退出前删掉
   * `<dataDir>/logs/run_sentinel.json` 即可：壳主动关=清哨兵（不报），真崩溃=
   * 哨兵留存（照常报）。best-effort，任何失败不影响退出。
   */
  function markCleanShutdown() {
    try {
      const sp = sentinelPathFor(backendDataDir);
      if (!sp) return; // 开发态/外部自管后端无 dataDir → 不动（其崩溃仍应被侦测）
      fs.unlinkSync(sp);
    } catch (e) { /* 哨兵不存在/删不掉都无妨 */ }
  }

  /** pid 是否仍存活（win/posix 通用：signal 0 探测，EPERM=存在但无权）。 */
  function _pidAliveDefault(pid) {
    try { process.kill(pid, 0); return true; } catch (e) { return !!(e && e.code === "EPERM"); }
  }
  const pidAlive = deps.pidAlive || _pidAliveDefault;

  function _issueKill(pid) {
    try {
      if (process.platform === "win32") {
        // windowsHide：GUI 进程（Electron）无控制台，exec 默认会为 cmd.exe 新建
        // 可见控制台 → 用户退出应用瞬间闪黑窗；CREATE_NO_WINDOW 消除
        exec(`taskkill /pid ${pid} /T /F`, { windowsHide: true });
      } else {
        try { process.kill(-pid, "SIGTERM"); } catch (e) { try { child && child.kill("SIGTERM"); } catch (e2) {} }
      }
    } catch (e) {
      try { child && child.kill(); } catch (e2) {}
    }
  }

  /**
   * 回收后端并**等到进程真死**（B57 升级风暴根子①的机制修复）。
   *
   * 旧 stop() 的 taskkill 是 fire-and-forget：更新器 quitAndInstall 后安装器
   * 立刻跑、装完自动拉起新版，而旧后端可能还没死透——新旧双进程抢同一份
   * pyrogram 会话 → Telegram AuthKeyDuplicated 强制注销全部账号（2026-08-23
   * skuio 机实录）。会话文件句柄只有在进程对象真正消亡时才由 OS 释放，所以
   * 「等 PID 死」是「会话已释放」的唯一可靠判据。轮询期中点若还活着会再补一刀
   * （win 重发 taskkill；posix 升级 SIGKILL）。超时如实返回 false（调用方自行
   * 决定是否继续退出——不能为一个杀不掉的进程把用户永远锁在退出流程里）。
   */
  async function stopAndWait(timeoutMs) {
    quitting = true;
    if (respawnTimer) { try { clearTimeout(respawnTimer); } catch (e) {} respawnTimer = null; }
    if (logStream) { try { logStream.end(); } catch (e) {} logStream = null; }
    const proc = child;
    if (!proc || !proc.pid) { status = "stopped"; return true; }
    markCleanShutdown(); // 强杀前先清哨兵，避免正常关闭被误报为崩溃
    const pid = proc.pid;
    _issueKill(pid);
    const budget = Math.max(500, timeoutMs || 8000);
    const deadline = Date.now() + budget;
    let escalated = false;
    while (Date.now() < deadline) {
      if (!pidAlive(pid)) {
        child = null;
        status = "stopped";
        log(`backend pid ${pid} exited (session files released)`);
        return true;
      }
      if (!escalated && Date.now() > deadline - budget / 2) {
        escalated = true;
        if (process.platform === "win32") _issueKill(pid);
        else { try { process.kill(-pid, "SIGKILL"); } catch (e) {} }
      }
      await new Promise((res) => setTimeout(res, 150));
    }
    child = null;
    status = "stopped";
    const dead = !pidAlive(pid);
    if (!dead) log(`WARN backend pid ${pid} still alive after ${budget}ms (session files may be held)`);
    return dead;
  }

  /** 回收后端进程（兼容旧同步语义：发出击杀即返回，不等死透）。 */
  function stop() {
    void stopAndWait(4000);
  }

  return { start, stop, stopAndWait, getStatus, probeHealth, probeIdentity, waitForReady };
}

module.exports = {
  resolveBackendSpawn, healthUrl, identityUrl, createBackendManager,
  classifyBackendIdentity, FP_HEALTH_PATH, FP_IDENTITY_PATH, EXPECTED_APP_ID,
  webEnvFromBackend, lanServeHost, sentinelPathFor, electronNodeEnv,
};
