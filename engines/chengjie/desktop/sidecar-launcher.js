"use strict";

// 协议边车（Node 微服务）的随包与生命周期管理 —— 「下载即可用」的最后一公里。
//
// 背景：WhatsApp 的「协议多开」与 Messenger 的「服务器托管登录」在后端只是 HTTP 客户端，
// 真正干活的是 services/ 下两个 Node 服务。它们此前只由 start.ps1 / 计划任务在我们自己的
// 生产机上起，**从没进过安装包** —— 于是客户装完打开接入弹窗，那两条路全灰，只剩要求
// 自备安卓手机的「真机 / 模拟器」。
//
// 三个刻意的设计选择：
//   · **不随包 node.exe**：用 `process.execPath` + `ELECTRON_RUN_AS_NODE=1` 把 Electron
//     自带的 Node 当运行时（Electron 31 → Node 20，满足两个服务的 engines 要求），
//     省掉第二份运行时，也免了「装了 App 还得装 Node」。已真机验证：baileys 在该运行时下
//     正常起服、/health 返回 ok。（唯一原生依赖 sharp 走 N-API 预编译，ABI 通用。）
//   · **可写目录分离**：会话凭据/浏览器 profile 落 dataDir（用户可写），媒体落后端真正
//     serve 的 static 目录 —— 后者不是随便挑的：后端按 `Path(__file__)` 推 protocol_media
//     根（protocol_bridge.protocol_media_root），写偏一层就是图片稳定 404（该类事故仓里
//     已有实锤，见 tests/test_static_asset_paths.py）。
//   · **Messenger 不强制用捆绑 Chromium**：服务默认 `MSG_BROWSER_CHANNEL=chrome`（系统真
//     Chrome），机器没装才自动回落捆绑 Chromium。这个优先级是实测结论、别倒过来：捆绑版
//     的 UA 与它自己发出的 Sec-CH-UA 自相矛盾（品牌报 Chromium、版本落后），是 Facebook
//     最容易抓的自动化信号，也是「账密验证码都对却被弹回登录页」的成因（见 server.js
//     BROWSER_CHANNEL 注释）。随包 Chromium 的意义是**兜底**：没装 Chrome 的机器也能用。
//
// 结构同 backend-launcher.js：「纯解析器（可单测）」+「生命周期（spawn/probe/kill）」两层。

const path = require("path");

// 每个边车的规格。端口必须与随包种子配置里的 baileys_url / web_url 一致
// （由 tests/test_desktop_seed_deliverable.py 的端口漂移门禁钉住）。
const SPECS = {
  whatsapp: {
    name: "whatsapp",
    dirName: "whatsapp-baileys",
    port: 8790,
    // /health 的 svc 字段（服务侧同名常量）。用于把「自家边车」与「占了同一端口的
    // 别家服务」分开——只看 ok:true 会把外来服务当自己的用，见 classifySidecarIdentity。
    svcId: "wa-baileys",
    logName: "wa-sidecar.log",
    mediaSubdir: "whatsapp",
    envKeys: {
      sessions: "WA_SESSIONS_DIR",
      mediaDir: "WA_MEDIA_DIR",
      mediaUrlBase: "WA_MEDIA_URL_BASE",
    },
    extraEnv: {},
  },
  messenger: {
    name: "messenger",
    dirName: "messenger-web",
    port: 8791,
    svcId: "messenger-web",
    logName: "msg-sidecar.log",
    mediaSubdir: "messenger",
    envKeys: {
      sessions: "MSG_SESSIONS_DIR",
      mediaDir: "MSG_MEDIA_DIR",
      mediaUrlBase: "MSG_MEDIA_URL_BASE",
    },
    extraEnv: {
      // Playwright 找浏览器的位置：0 = 从 node_modules/playwright-core/.local-browsers
      // 取（我们就是这么随包的）。不设它会去找 %LOCALAPPDATA%\ms-playwright —— 客户机
      // 上那里是空的，于是「没装 Chrome 的机器」连兜底浏览器都没有。
      PLAYWRIGHT_BROWSERS_PATH: "0",
      // headed 也要开机恢复已登录会话，否则重启后要重新人工登录一次
      MSG_RESTORE_ON_BOOT: "1",
    },
  },
};

const SIDECAR_ENTRY = "server.js";

// 崩溃自愈：指数退避重启，超过上限停手（避免坏环境下无限刷进程）。
// 放弃后该平台在接入弹窗里显示 service_down —— 如实告知，而不是假装可用。
const MAX_RESTARTS = 5;
const RESTART_BACKOFF_MS = [2000, 5000, 10000, 30000, 60000];

/**
 * 解析边车启动命令（纯函数，便于单测）。
 *
 * @param {object} spec SPECS 里的一项
 * @param {object} o
 * @param {boolean} o.isPackaged      app.isPackaged
 * @param {string} o.resourcesPath    process.resourcesPath（发布态 extraResources 落点）
 * @param {string} o.appDir           __dirname（desktop 目录；其上级=引擎根）
 * @param {string} o.execPath         process.execPath（Electron 可执行，用作 Node 运行时）
 * @param {(p:string)=>boolean} o.exists 文件存在判断（注入以便单测）
 * @returns {{command:string,args:string[],cwd:string,kind:string}|null}
 *          null = 包里没有这个边车（不是错误：未随包的形态就该如实显示不可用）
 */
function resolveSidecarSpawn(spec, o) {
  const exists = (o && o.exists) || (() => false);
  const candidates = [];
  // ① 发布态：electron-builder extraResources → resources/services/<name>/
  if (o && o.isPackaged && o.resourcesPath) {
    candidates.push({
      dir: path.join(o.resourcesPath, "services", spec.dirName),
      kind: "bundled",
    });
  }
  // ② 开发态：引擎根 services/<name>/（desktop 的上级即引擎根）
  if (o && o.appDir) {
    candidates.push({
      dir: path.join(path.resolve(o.appDir, ".."), "services", spec.dirName),
      kind: "repo",
    });
  }

  for (const c of candidates) {
    const entry = path.join(c.dir, SIDECAR_ENTRY);
    // node_modules 必须同在：只有 server.js 而没有依赖，起来必是 MODULE_NOT_FOUND，
    // 不如当作「没随包」如实报不可用（打包门禁另有两道，见 build/after-pack.js）。
    if (exists(entry) && exists(path.join(c.dir, "node_modules"))) {
      return {
        command: String((o && o.execPath) || process.execPath),
        args: [entry],
        cwd: c.dir,
        kind: c.kind,
      };
    }
  }
  return null;
}

/**
 * 后端真正读写的媒体落地目录（纯函数，便于单测）。
 *
 * 唯一判据＝后端 `protocol_bridge.protocol_media_root()` 落在哪：
 *   ① 有数据根契约（`AITR_DATA_DIR`，打包态 backend-launcher 注入 `<userData>/data`）
 *      → `<dataDir>/protocol_media/<sub>`；
 *   ② 无契约（开发态裸跑）→ 引擎代码树 `src/web/static/protocol_media/<sub>`。
 *
 * ①**必须优先**：2026-08-19 起后端媒体根迁到数据根（媒体随实例走、进备份包），
 * 而这里原先只认代码树 static —— 打包态那是**只读安装目录**，边车要么写失败、
 * 要么写进后端根本不读的地方。症状极其阴：前端仍能播（admin.py ProtocolMediaStatic
 * 双根兜底 + 边车写成功的场景），只是后端识别链（ASR/图片 VLM/OCR/声纹/贴纸）全
 * 拿不到文件 → 无兜底纪律拦下整条回复，表现为「AI 突然不说话」（08-20 whatsapp
 * 语音实锤）。改这里前先读 `protocol_media_root()` 的 docstring，两边必须逐字同址。
 *
 * @param {object} o
 * @param {string} [o.dataDir] 后端数据根（= backend-launcher 注入的 AITR_DATA_DIR）
 * @returns {string} 绝对路径；无法判定时返回 ""（跳过媒体落地，文字照常收发）
 */
function resolveMediaDir(spec, o) {
  const exists = (o && o.exists) || (() => false);
  if (o && o.dataDir) {
    return path.join(String(o.dataDir), "protocol_media", spec.mediaSubdir);
  }
  const roots = [];
  if (o && o.isPackaged && o.resourcesPath) {
    const backend = path.join(o.resourcesPath, "backend");
    // PyInstaller 6.x onedir 把数据放 _internal/；onefile / 旧版落在同级。
    roots.push(path.join(backend, "_internal"));
    roots.push(backend);
  }
  if (o && o.appDir) roots.push(path.resolve(o.appDir, ".."));

  for (const r of roots) {
    const staticDir = path.join(r, "src", "web", "static");
    if (exists(staticDir)) {
      return path.join(staticDir, "protocol_media", spec.mediaSubdir);
    }
  }
  return "";
}

/**
 * 组装边车环境变量（纯函数，便于单测）。
 *
 * 刻意**不**重复 start.ps1 里那一串 WA_SYNC_* / MSG_* 业务开关：server.js 自身的缺省值
 * 与之相同，在两处各写一份只会漂移。这里只给「桌面态必须覆盖的」：运行时开关、监听端口、
 * 可写会话目录、回推后端的地址与令牌，以及各边车规格里的 extraEnv。
 *
 * @param {object} spec SPECS 里的一项
 * @param {object} o
 * @param {string} o.dataDir        用户可写数据根（会话凭据/浏览器 profile 落此）
 * @param {string} o.backendBaseUrl 后端基址（如 http://127.0.0.1:18799）
 * @param {string} o.token          后端 web_admin.auth_token（缺了入站会被 401 静默丢弃）
 * @param {string} [o.mediaDir]     媒体落地目录（resolveMediaDir 的结果）
 * @returns {object} env 增量（调用方与 process.env 合并）
 */
function buildSidecarEnv(spec, o) {
  const base = String((o && o.backendBaseUrl) || "").replace(/\/+$/, "");
  const env = Object.assign({
    // 把 Electron 当纯 Node 跑（不初始化 app / 不吃单实例锁）
    ELECTRON_RUN_AS_NODE: "1",
    PORT: String(spec.port),
  }, spec.extraEnv || {});

  if (o && o.dataDir) {
    // 会话凭据＝登录成果，必须落用户可写区：写进只读安装目录会让每次重启都要重新登录。
    env[spec.envKeys.sessions] = path.join(String(o.dataDir), `${spec.name}-sessions`);
  }
  if (base) {
    env.PY_INGEST_URL = base + "/api/internal/protocol/ingest";
    env.PY_STATUS_URL = base + "/api/internal/protocol/session-status";
  }
  // 令牌必须与后端在跑的 auth_token 一致，否则入站消息全被 401 拒收且**没有任何报错**
  // （客户侧症状＝能登录、能发出去、就是收不到消息）。
  if (o && o.token) env.PY_API_TOKEN = String(o.token);
  if (o && o.mediaDir) {
    env[spec.envKeys.mediaDir] = String(o.mediaDir);
    env[spec.envKeys.mediaUrlBase] = `/static/protocol_media/${spec.mediaSubdir}`;
  }
  return env;
}

/** 边车健康探针 URL。 */
function healthUrl(port) {
  return `http://127.0.0.1:${Number(port)}/health`;
}

/**
 * 判定端口上那个服务「是不是自家边车」（纯函数，便于单测）。
 *
 * 为什么需要：原判据只看 `/health` 回 ok —— 任何程序都能满足。端口被别家占着时，壳会
 * 把它当自家边车「复用、不重复拉起」，于是登录看着成了、消息永远收不到，且没有任何
 * 报错。后端 sidecar 正是踩过同一个坑才加了身份探针（backend-launcher
 * classifyBackendIdentity），这里沿用它那条已验证的三态口径：
 *
 * @param {object} spec SPECS 里的一项
 * @param {object|null} health `/health` 的响应；null = 没响应/非 JSON
 * @returns {{reusable:boolean, foreign:boolean, detail:string}}
 *   · 身份对上           → 复用（自家边车已在跑，多为用户自管或上次没回收）
 *   · **没有** svc 字段  → 按「旧版本边车」放行（否则新壳配旧边车直接罢工，
 *                          比它要解决的问题更严重；升级期必然出现这一态）
 *   · svc 字段但对不上   → 外来服务，**不复用**，据此报 port-conflict
 */
function classifySidecarIdentity(spec, health) {
  if (!health || typeof health !== "object" || health.ok !== true) {
    return { reusable: false, foreign: false, detail: "no-response" };
  }
  const svc = String(health.svc || "").trim();
  if (!svc) {
    return { reusable: true, foreign: false, detail: "legacy-no-svc-field" };
  }
  if (svc === spec.svcId) {
    return { reusable: true, foreign: false, detail: "own" };
  }
  return { reusable: false, foreign: true, detail: `foreign:${svc}` };
}

/**
 * 判定是否该由桌面壳拉起某个边车（纯函数，便于单测）。
 *
 * 默认拉起 —— 「下载即可用」是产品要求。留 config.json 的
 * `sidecars.<name>.enabled: false` 给两类人：自管边车的运维、以及明确不用该渠道
 * 又计较那点内存的用户。
 */
function sidecarEnabled(config, name) {
  const s = ((config || {}).sidecars || {})[name] || {};
  return s.enabled !== false;
}

/**
 * 单个边车的生命周期管理器。依赖注入（app/spawn/fs/fetch）以便单测与主进程解耦。
 */
function createSidecarManager(deps, spec) {
  const { app, spawn, exec, fs, fetch } = deps || {};
  let child = null;
  // idle|disabled|absent|probing|starting|running|running-external|port-conflict|failed|stopped
  let status = "idle";
  let lastError = "";
  let restarts = 0;
  let quitting = false;
  let logStream = null;

  function log(msg) {
    const line = `[sidecar:${spec.name}] ${msg}`;
    console.log(line);
    try {
      if (logStream) logStream.write(`${new Date().toISOString()} ${line}\n`);
    } catch (e) { /* 日志写不动不影响服务 */ }
  }

  function getStatus() {
    return {
      name: spec.name, status, lastError, port: spec.port,
      pid: (child && child.pid) || 0, restarts,
    };
  }

  /** 探端口上那个服务的身份（不只是「有没有响应」）。 */
  async function probe() {
    if (!fetch) return classifySidecarIdentity(spec, null);
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 1500);
    try {
      const r = await fetch(healthUrl(spec.port), { signal: ctrl.signal });
      if (!r || !r.ok) return classifySidecarIdentity(spec, null);
      let body = null;
      try { body = await r.json(); } catch (e) { body = null; }
      return classifySidecarIdentity(spec, body);
    } catch (e) {
      return classifySidecarIdentity(spec, null);
    } finally {
      clearTimeout(timer);
    }
  }

  function openLog(dataDir) {
    try {
      if (!dataDir || !fs) return;
      const dir = path.join(dataDir, "logs");
      fs.mkdirSync(dir, { recursive: true });
      logStream = fs.createWriteStream(path.join(dir, spec.logName), { flags: "a" });
    } catch (e) { logStream = null; }
  }

  const _exists = (p) => { try { return fs.existsSync(p); } catch (e) { return false; } };

  async function start(config) {
    if (!sidecarEnabled(config, spec.name)) {
      status = "disabled";
      log(`sidecars.${spec.name}.enabled=false → 跳过拉起（由用户自管）`);
      return;
    }
    const backend = (config || {}).backend || {};
    let dataDir = "";
    if (app && app.isPackaged) {
      try {
        dataDir = path.join(app.getPath("userData"), "data");
      } catch (e) { dataDir = ""; }
    }
    openLog(dataDir);

    status = "probing";
    const id = await probe();
    if (id.reusable) {
      status = "running-external";
      log(`:${spec.port} 已有自家边车在跑（${id.detail}）→ 复用，不重复拉起`);
      return;
    }
    if (id.foreign) {
      // 刻意**不**去抢端口：那台服务不是我们的，杀它属越权，而在同端口再起一个只会
      // 绑定失败成僵尸。如实报冲突 → 后端诊断给 service_down，弹窗说「服务未运行」。
      status = "port-conflict";
      lastError = `:${spec.port} 被别的服务占用（${id.detail}）——请改用该端口的程序或联系运维`;
      log(lastError);
      return;
    }

    const ctx = {
      isPackaged: !!(app && app.isPackaged),
      resourcesPath: process.resourcesPath,
      appDir: __dirname,
      execPath: process.execPath,
      exists: _exists,
    };
    const resolved = resolveSidecarSpawn(spec, ctx);
    if (!resolved) {
      // 不是故障：未随包的形态本就该如实不可用。后端诊断会给 service_down，
      // 接入弹窗照实说「服务未运行」，不会假装能用。
      status = "absent";
      lastError = `边车不在包内（resources/services/${spec.dirName}）`;
      log(lastError);
      return;
    }

    const env = Object.assign({}, process.env, buildSidecarEnv(spec, {
      dataDir,
      backendBaseUrl: backend.base_url || "http://127.0.0.1:18799",
      token: backend.token || "",
      // dataDir 一并传入：有数据根契约时媒体根跟后端走 <dataDir>/protocol_media
      mediaDir: resolveMediaDir(spec, Object.assign({}, ctx, { dataDir })),
    }));

    status = "starting";
    log(`启动 ${resolved.kind}: ${resolved.args[0]} (port=${spec.port})`);
    try {
      child = spawn(resolved.command, resolved.args, {
        cwd: resolved.cwd,
        env,
        stdio: ["ignore", "pipe", "pipe"],
        windowsHide: true,
      });
    } catch (e) {
      status = "failed";
      lastError = String((e && e.message) || e);
      log(`spawn 失败：${lastError}`);
      return;
    }

    const pipe = (stream) => {
      try {
        stream.on("data", (b) => {
          const s = String(b).trimEnd();
          if (s) log(s);
        });
      } catch (e) { /* ignore */ }
    };
    pipe(child.stdout);
    pipe(child.stderr);

    child.on("exit", (code) => {
      child = null;
      if (quitting) { status = "stopped"; return; }
      lastError = `边车退出（code=${code}）`;
      if (restarts >= MAX_RESTARTS) {
        status = "failed";
        log(`${lastError}，已达重启上限 ${MAX_RESTARTS} → 停手`);
        return;
      }
      const delay = RESTART_BACKOFF_MS[Math.min(restarts, RESTART_BACKOFF_MS.length - 1)];
      restarts += 1;
      status = "starting";
      log(`${lastError}，${delay}ms 后第 ${restarts} 次重启`);
      setTimeout(() => { if (!quitting) start(config); }, delay);
    });

    status = "running";
  }

  /** 回收边车进程（win→taskkill /T /F；posix→SIGTERM）。退出时调用。
   *  /T 是必须的：messenger 边车底下还挂着一整棵 Chromium 进程树，只杀父进程会留下
   *  一堆孤儿 chrome.exe 占着 profile 锁，下次启动登录态直接坏掉。 */
  function stop() {
    quitting = true;
    if (logStream) { try { logStream.end(); } catch (e) { /* ignore */ } logStream = null; }
    if (!child || !child.pid) { status = "stopped"; return; }
    const pid = child.pid;
    try {
      if (process.platform === "win32" && exec) {
        exec(`taskkill /pid ${pid} /T /F`);
      } else {
        child.kill("SIGTERM");
      }
    } catch (e) { /* 回收失败不阻断退出 */ }
    child = null;
    status = "stopped";
  }

  return { start, stop, getStatus, probe, spec };
}

/** 建全部边车的管理器（含 startAll/stopAll，主进程只需接两个钩子）。 */
function createAllSidecarManagers(deps) {
  const managers = {};
  for (const key of Object.keys(SPECS)) {
    managers[key] = createSidecarManager(deps, SPECS[key]);
  }
  return {
    managers,
    async startAll(config) {
      // 并行拉起：两个边车互不依赖，串行只会把启动时间叠加。
      // 单个失败不得影响另一个（allSettled 而非 all）。
      await Promise.allSettled(
        Object.values(managers).map((m) => m.start(config)));
    },
    stopAll() {
      for (const m of Object.values(managers)) {
        try { m.stop(); } catch (e) { /* 单个回收失败不阻断其余 */ }
      }
    },
    getStatus() {
      const out = {};
      for (const [k, m] of Object.entries(managers)) out[k] = m.getStatus();
      return out;
    },
  };
}

module.exports = {
  SPECS,
  MAX_RESTARTS,
  resolveSidecarSpawn,
  resolveMediaDir,
  buildSidecarEnv,
  sidecarEnabled,
  classifySidecarIdentity,
  healthUrl,
  createSidecarManager,
  createAllSidecarManagers,
};
