"use strict";

// 后端 sidecar 命令解析纯函数单测（无框架，node 直跑）：node test/backend-launcher.test.js
const assert = require("assert");
const path = require("path");
const {
  resolveBackendSpawn, healthUrl, identityUrl, webEnvFromBackend,
  createBackendManager, classifyBackendIdentity, sentinelPathFor,
  electronNodeEnv,
} = require("../backend-launcher.js");

let pass = 0;
function ok(name, cond) {
  assert.ok(cond, name);
  pass++;
}

const APP_DIR = "/repo/desktop"; // 假定 desktop 目录；其上级=仓库根 /repo
const REPO = path.resolve(APP_DIR, "..");

// 确定性：宿主机若恰好设了部署档 env，会污染下方「默认 cloud_light」断言
delete process.env.AITR_DEPLOY_PROFILE;

// ── ① 显式关闭 → null（用户自管，零回归）────────────────────────────────────
ok(
  "spawn.enabled=false → null",
  resolveBackendSpawn({
    config: { backend: { spawn: { enabled: false } } },
    appDir: APP_DIR, platform: "win32", exists: () => true,
  }) === null
);

// ── ② 显式 command 覆写优先级最高 ────────────────────────────────────────────
const explicit = resolveBackendSpawn({
  config: { backend: { spawn: { command: "C:/py/python.exe", args: ["server.py"], cwd: "C:/app" } } },
  appDir: APP_DIR, platform: "win32", exists: () => true,
});
ok("explicit kind", explicit && explicit.kind === "explicit");
ok("explicit command", explicit.command === "C:/py/python.exe");
ok("explicit args", explicit.args.join(",") === "server.py");
ok("explicit cwd", explicit.cwd === "C:/app");

// ── ③ 发布态：随包二进制存在 → bundled ───────────────────────────────────────
const binPath = path.join("/Resources", "backend", "backend.exe");
const bundled = resolveBackendSpawn({
  config: {}, isPackaged: true, resourcesPath: "/Resources",
  appDir: APP_DIR, platform: "win32", exists: (p) => p === binPath,
});
ok("bundled kind", bundled && bundled.kind === "bundled");
ok("bundled command", bundled.command === binPath);
// 无 dataDir → cwd 回退二进制目录；仍注入桌面模式标记（打包态默认桌面）
ok("bundled 无dataDir cwd=dirname", bundled.cwd === path.dirname(binPath));
ok("bundled 桌面模式标记", bundled.env && bundled.env.AITR_DESKTOP_MODE === "1");
ok("bundled 无dataDir 不重定向数据", bundled.env && !bundled.env.AITR_DATA_DIR);
// WP-1：打包态默认 cloud_light 部署档；外部 env 显式设置时尊重之（可覆盖）
ok("bundled 默认部署档 cloud_light",
  bundled.env.AITR_DEPLOY_PROFILE === "cloud_light");
process.env.AITR_DEPLOY_PROFILE = "custom_x";
try {
  const bundledCustom = resolveBackendSpawn({
    config: {}, isPackaged: true, resourcesPath: "/Resources",
    appDir: APP_DIR, platform: "win32", exists: (p) => p === binPath,
  });
  ok("bundled 部署档可被外部 env 覆盖",
    bundledCustom.env.AITR_DEPLOY_PROFILE === "custom_x");
} finally {
  delete process.env.AITR_DEPLOY_PROFILE;
}

// 发布态 + dataDir → cwd/env 指向可写数据根（核心：config 落可写区）
const DATA = path.join("/Users/me/AppData", "data");
const bundledData = resolveBackendSpawn({
  config: {}, isPackaged: true, resourcesPath: "/Resources", dataDir: DATA,
  appDir: APP_DIR, platform: "win32", exists: (p) => p === binPath,
});
ok("bundled+dataDir cwd=dataDir", bundledData.cwd === DATA);
ok("bundled+dataDir AITR_DATA_DIR", bundledData.env.AITR_DATA_DIR === DATA);
ok("bundled+dataDir AITR_CONFIG_PATH", bundledData.env.AITR_CONFIG_PATH === path.join(DATA, "config", "config.yaml"));
ok("bundled+dataDir 桌面模式标记", bundledData.env.AITR_DESKTOP_MODE === "1");

// 发布态 + backend 配置 → web host/port/token 对齐桌面壳（renderer 才连得上后端）
const bundledWeb = resolveBackendSpawn({
  config: { backend: { base_url: "http://127.0.0.1:18799", token: "admin" } },
  isPackaged: true, resourcesPath: "/Resources",
  appDir: APP_DIR, platform: "win32", exists: (p) => p === binPath,
});
ok("bundled web host", bundledWeb.env.AITR_WEB_HOST === "127.0.0.1");
ok("bundled web port", bundledWeb.env.AITR_WEB_PORT === "18799");
ok("bundled web token", bundledWeb.env.AITR_WEB_TOKEN === "admin");

// webEnvFromBackend 纯函数：解析 + 容错
// #254 D-P2（2026-09-08）起：serve host 只在 lan_access===true 时升 0.0.0.0；
// 强令牌不再是放开依据（MTRCH2：公共 Wi-Fi 上后台全网卡暴露）。renderer 仍连 base_url。
const we = webEnvFromBackend({ base_url: "http://localhost:9000", token: "t" });
ok("webEnv 强令牌回环默认仍回环（不再自动 0.0.0.0）", we.AITR_WEB_HOST === "localhost");
ok("webEnv port", we.AITR_WEB_PORT === "9000");
ok("webEnv token", we.AITR_WEB_TOKEN === "t");
ok("webEnv lan_access=true → serve 0.0.0.0",
  webEnvFromBackend({ base_url: "http://localhost:9000", token: "t", lan_access: true }).AITR_WEB_HOST === "0.0.0.0");
ok("webEnv 非法 base_url 容错", Object.keys(webEnvFromBackend({ base_url: "::::" })).length === 0);
ok("webEnv 空 backend → 空", Object.keys(webEnvFromBackend({})).length === 0);

// ── #254 D-P2 lanServeHost 决策表（出厂态回环：只有 lan_access===true 才放开）─────
const { lanServeHost } = require("../backend-launcher.js");
ok("lan 弱令牌(admin)默认不放开",
  lanServeHost({ base_url: "http://127.0.0.1:18799", token: "admin" }) === "");
ok("lan 空令牌默认不放开",
  lanServeHost({ base_url: "http://127.0.0.1:18799", token: "" }) === "");
ok("lan 强令牌回环也不自动放开（D-P2：令牌强弱不是监听全网卡的依据）",
  lanServeHost({ base_url: "http://127.0.0.1:18799", token: "a1b2c3" }) === "");
ok("lan 无 backend 段 → 回环",
  lanServeHost(undefined) === "" && lanServeHost({}) === "");
ok("lan lan_access=false 硬关",
  lanServeHost({ base_url: "http://127.0.0.1:18799", token: "a1b2c3", lan_access: false }) === "");
ok("lan lan_access=true 显式打开 → 0.0.0.0",
  lanServeHost({ base_url: "http://127.0.0.1:18799", token: "a1b2c3", lan_access: true }) === "0.0.0.0");
ok("lan lan_access=true 弱令牌也放（用户明示自担）",
  lanServeHost({ base_url: "http://127.0.0.1:18799", token: "admin", lan_access: true }) === "0.0.0.0");
ok("lan lan_access 字符串 'true' 不算显式打开（只认布尔）",
  lanServeHost({ base_url: "http://127.0.0.1:18799", token: "a1b2c3", lan_access: "true" }) === "");
ok("lan 远端 base_url 不动（serve 归远端自管）",
  lanServeHost({ base_url: "http://192.168.0.9:18799", token: "a1b2c3", lan_access: true }) === "");
ok("lan 非法 base_url 按回环处理（开关开才放）",
  lanServeHost({ base_url: "::::", token: "a1b2c3", lan_access: true }) === "0.0.0.0");
// 整包装机（token=admin 或随机强令牌）路径回归：AITR_WEB_HOST 保持 127.0.0.1 不升
ok("webEnv 弱令牌不升 serve host",
  webEnvFromBackend({ base_url: "http://127.0.0.1:18799", token: "admin" })
    .AITR_WEB_HOST === "127.0.0.1");
ok("webEnv 强令牌默认不升 serve host（clean 装 netstat 只见 127.0.0.1）",
  webEnvFromBackend({ base_url: "http://127.0.0.1:18799", token: "R4nd0mStr0ngT0ken" })
    .AITR_WEB_HOST === "127.0.0.1");

// 退出哨兵路径（正常关闭清哨兵，防 taskkill /F 把正常关误报成崩溃）
ok("sentinelPathFor 拼 logs/run_sentinel.json",
  sentinelPathFor("/data/root") === path.join("/data/root", "logs", "run_sentinel.json"));
ok("sentinelPathFor 空 dataDir → null（开发态不清哨兵，真崩溃仍侦测）",
  sentinelPathFor("") === null && sentinelPathFor(null) === null);

// 默认端口（无显式端口）→ 不注入 AITR_WEB_PORT，后端用 config 默认
ok("webEnv 无端口不注入", webEnvFromBackend({ base_url: "https://example.com" }).AITR_WEB_PORT === undefined);

// ── Electron-as-Node：LINE 扫码的 Node 运行时（okline 的 X-Hmac 桥要它）──────────
// 后端只在 PATH 上没有真 node 时才回落到它，所以这里只负责"把路径告诉后端"。
// 刻意**不**在壳里设 ELECTRON_RUN_AS_NODE：那会传染后端的所有子进程，
// 由后端在真正选中 Electron 时按需设（见 line_protocol_login.ensure_node_runtime）。
ok("electronNodeEnv 注入 execPath",
  electronNodeEnv("C:\\app\\ChatX.exe").AITR_ELECTRON_NODE === "C:\\app\\ChatX.exe");
ok("electronNodeEnv 空值不注入",
  Object.keys(electronNodeEnv("")).length === 0 && Object.keys(electronNodeEnv(null)).length === 0);
ok("electronNodeEnv 不设 RUN_AS_NODE（避免传染子进程）",
  electronNodeEnv("C:\\app\\ChatX.exe").ELECTRON_RUN_AS_NODE === undefined);
{
  // 发布态：随包后端必须拿到 Electron 路径，否则没装 Node 的客户机 LINE 扫不了码
  const withElectron = resolveBackendSpawn({
    config: {}, isPackaged: true, resourcesPath: "/Resources",
    appDir: APP_DIR, platform: "win32", execPath: "C:\\app\\ChatX.exe",
    exists: (p) => p === binPath,
  });
  ok("发布态注入 AITR_ELECTRON_NODE",
    withElectron && withElectron.env.AITR_ELECTRON_NODE === "C:\\app\\ChatX.exe");
  // 没给 execPath（老调用方）→ 不注入，且不得崩
  const noElectron = resolveBackendSpawn({
    config: {}, isPackaged: true, resourcesPath: "/Resources",
    appDir: APP_DIR, platform: "win32",
    exists: (p) => p === binPath,
  });
  ok("无 execPath 不注入且不崩",
    noElectron && noElectron.env.AITR_ELECTRON_NODE === undefined);
}

// 发布态但二进制缺失 → 回退（有 main.py 则 python）
const fallbackMain = path.join(REPO, "main.py");
const bundledMissing = resolveBackendSpawn({
  config: {}, isPackaged: true, resourcesPath: "/Resources",
  appDir: APP_DIR, platform: "linux", exists: (p) => p === fallbackMain,
});
ok("二进制缺失→回退 python", bundledMissing && bundledMissing.kind === "python");

// ── ④ 开发态：系统 Python 跑仓库根 main.py ───────────────────────────────────
const devWin = resolveBackendSpawn({
  config: {}, isPackaged: false, appDir: APP_DIR, platform: "win32",
  exists: (p) => p === fallbackMain,
});
ok("dev kind=python", devWin && devWin.kind === "python");
ok("dev win 默认 python", devWin.command === "python");
ok("dev args=main.py", devWin.args.join(",") === "main.py");
ok("dev cwd=仓库根", devWin.cwd === REPO);
ok("dev 不注入 env（零回归）", devWin.env && Object.keys(devWin.env).length === 0);

const devPosix = resolveBackendSpawn({
  config: {}, isPackaged: false, appDir: APP_DIR, platform: "darwin",
  exists: (p) => p === fallbackMain,
});
ok("dev posix 默认 python3", devPosix.command === "python3");

// 自定义 python 解释器
const devCustomPy = resolveBackendSpawn({
  config: { backend: { spawn: { python: "/usr/bin/python3.11" } } },
  isPackaged: false, appDir: APP_DIR, platform: "linux",
  exists: (p) => p === fallbackMain,
});
ok("dev 自定义 python", devCustomPy.command === "/usr/bin/python3.11");

// ── ⑤ 无任何产出 → null ───────────────────────────────────────────────────────
ok(
  "无 main.py 无二进制 → null",
  resolveBackendSpawn({
    config: {}, isPackaged: false, appDir: APP_DIR, platform: "win32", exists: () => false,
  }) === null
);

// ── ⑥ healthUrl 归一化 ───────────────────────────────────────────────────────
ok("healthUrl 默认", healthUrl({}) === "http://127.0.0.1:18799/login");
ok("healthUrl 去尾斜杠", healthUrl({ backend: { base_url: "http://x:9/" } }) === "http://x:9/login");
ok("identityUrl 默认", identityUrl({}) === "http://127.0.0.1:18799/api/desktop/ping");

// ── ⑦ 身份判定：只有「确认是别家服务」才拒绝复用 ─────────────────────────────
//   探针的价值在于区分三种「端口上有东西在应答」：自家后端 / 老版自家后端 / 别家程序。
//   过严会让新壳配旧后端直接罢工（比要解决的问题更严重），故拿不到身份一律放行。
{
  const legacy = classifyBackendIdentity(null, "0.2.2");
  ok("拿不到身份→放行（老后端无该端点）", legacy.reusable === true && legacy.foreign === false);

  const foreign = classifyBackendIdentity({ app: "some-other-app", version: "1.0" }, "0.2.2");
  ok("别家服务→拒绝复用", foreign.reusable === false && foreign.foreign === true);
  ok("别家服务→给得出原因", /some-other-app/.test(foreign.detail));

  const same = classifyBackendIdentity({ app: "chengjie", version: "0.2.2" }, "0.2.2");
  ok("自家同版本→复用且无告警", same.reusable === true && same.versionMismatch === false);

  const skew = classifyBackendIdentity({ app: "chengjie", version: "0.2.1" }, "0.2.2");
  ok("自家版本错配→仍复用", skew.reusable === true);
  ok("自家版本错配→记下线索", skew.versionMismatch === true && /0\.2\.1/.test(skew.detail));

  const devBackend = classifyBackendIdentity({ app: "chengjie", version: "dev" }, "0.2.2");
  ok("源码态 dev 后端不算错配", devBackend.versionMismatch === false);

  const junk = classifyBackendIdentity({ nope: 1 }, "0.2.2");
  ok("响应无 app 字段→按未知放行", junk.reusable === true && junk.foreign === false);
}

console.log(`backend-launcher.test.js: ${pass} passed`);

// ── ⑦ 生命周期：探活复用 + 幂等防重复 spawn（双实例竞态防线）─────────────────
//   双实例根治分两层：跨进程靠 main.js 的 Electron 单实例锁；同进程靠 start() 的
//   幂等卫（本节验证）——防 probe→spawn 的 TOCTOU 窗口被并发 start() 重复利用。
(async () => {
  let lpass = 0;
  function lok(name, cond) { assert.ok(cond, name); lpass++; }

  function fakeChild() {
    return { pid: 4242, stdout: { on() {} }, stderr: { on() {} }, on() {} };
  }
  const baseDeps = (over) => Object.assign({
    app: { isPackaged: false, getPath: () => "/tmp" },
    exec: () => {},
    fs: {
      existsSync: () => true,
      mkdirSync() {},
      createWriteStream: () => ({ write() {}, end() {} }),
    },
    log: () => {},
    readyTries: 2, readyIntervalMs: 1, // 单测加速：不等 90s
  }, over);
  const CFG = { backend: { base_url: "http://127.0.0.1:18799" } };

  // (a) 后端已可达 → running-external，绝不重复 spawn（零回归：复用既有/手动起的后端）
  {
    let spawned = 0;
    const mgr = createBackendManager(baseDeps({
      spawn: () => { spawned++; return fakeChild(); },
      fetch: async () => ({ status: 200 }), // 探活成功=已有后端在跑
    }));
    await mgr.start(CFG);
    lok("已有后端→不 spawn", spawned === 0);
    lok("已有后端→状态 running-external", mgr.getStatus().status === "running-external");
  }

  // (b) 无既有后端 + 并发二次进入 start() → 幂等卫保证只 spawn 一次（核心：防竞态僵尸）
  {
    let spawned = 0;
    const mgr = createBackendManager(baseDeps({
      spawn: () => { spawned++; return fakeChild(); },
      fetch: async () => { throw new Error("unreachable"); }, // 无既有后端→进入 spawn 路径
    }));
    await Promise.all([mgr.start(CFG), mgr.start(CFG)]);
    lok("并发 start → 只 spawn 一次", spawned === 1);
  }

  // (c) 已有存活子进程后再调 start() → 跳过（不二次 spawn）
  {
    let spawned = 0;
    const mgr = createBackendManager(baseDeps({
      spawn: () => { spawned++; return fakeChild(); },
      fetch: async () => { throw new Error("unreachable"); },
    }));
    await mgr.start(CFG);          // 第一次：spawn 一次（之后 child 存活）
    await mgr.start(CFG);          // 第二次：child 非空 → 跳过
    lok("child 存活→再次 start 不二次 spawn", spawned === 1);
  }

  // (d) 端口被别家程序占着 → 不复用、不 spawn、状态 port-conflict（可被 UI 直接指路）
  {
    let spawned = 0;
    const mgr = createBackendManager(baseDeps({
      spawn: () => { spawned++; return fakeChild(); },
      fetch: async (url) => (String(url).endsWith("/api/desktop/ping")
        ? { ok: true, status: 200, json: async () => ({ app: "grafana", version: "11" }) }
        : { status: 200 }),
    }));
    await mgr.start(CFG);
    const st = mgr.getStatus();
    lok("别家占端口→状态 port-conflict", st.status === "port-conflict");
    lok("别家占端口→不 spawn（端口本就被占，抢也没用）", spawned === 0);
    lok("别家占端口→lastError 说得出是谁", /grafana/.test(st.lastError || ""));
  }

  // (e) 自家旧版本后端在跑（开发态）→ 照常复用，但把版本错配记进状态供排查；
  //     开发态壳/后端版本错开是常态，绝不收割（exec 零调用）
  {
    let execCalls = 0;
    const mgr = createBackendManager(baseDeps({
      app: { isPackaged: false, getPath: () => "/tmp", getVersion: () => "0.2.2" },
      spawn: () => fakeChild(),
      exec: () => { execCalls++; },
      fetch: async (url) => (String(url).endsWith("/api/desktop/ping")
        ? { ok: true, status: 200, json: async () => ({ app: "chengjie", version: "0.2.1" }) }
        : { status: 200 }),
    }));
    await mgr.start(CFG);
    const st = mgr.getStatus();
    lok("自家旧后端→仍复用", st.status === "running-external");
    lok("自家旧后端→记录版本错配", st.versionMismatch === true);
    lok("开发态版本错配→绝不收割", execCalls === 0);
  }

  // (e2/e3) 打包态版本错配 = 上一版本升级残留的孤儿后端（B57 运行时兜底）：
  //   按精确 exe 路径收割 → 端口静默（会话文件已释放）→ 拉起当前版本随包后端；
  //   收割失败 → 退回复用（旧后端也比没有后端强）。win32 专属路径（打包发行面）。
  if (process.platform === "win32") {
    const savedResources = process.resourcesPath;
    process.resourcesPath = path.join(path.sep, "Resources");
    const staleBin = path.join(process.resourcesPath, "backend", "backend.exe");
    try {
      // (e2) 收割成功 → 重新 spawn 当前版本
      {
        let spawned = 0;
        let reapCmd = "";
        let reaped = false;
        let spawnedFlag = false;
        const mgr = createBackendManager(baseDeps({
          app: { isPackaged: true, getPath: () => "/tmp", getVersion: () => "0.2.2" },
          spawn: () => { spawned++; spawnedFlag = true; return fakeChild(); },
          exec: (cmd) => { reapCmd = String(cmd); reaped = true; },
          fetch: async (url) => {
            if (String(url).endsWith("/api/desktop/ping")) {
              return { ok: true, status: 200, json: async () => ({ app: "chengjie", version: "0.2.1" }) };
            }
            if (spawnedFlag) return { status: 200 };   // 新后端就绪
            if (reaped) throw new Error("port silent"); // 收割后端口静默
            return { status: 200 };                     // 初始：旧后端在应答
          },
          reapRoundMs: 50, reapPollMs: 1,
        }));
        await mgr.start(CFG);
        const st = mgr.getStatus();
        lok("打包态残留后端→收割后重新 spawn", spawned === 1);
        lok("打包态残留后端→就绪", st.status === "ready");
        lok("打包态残留后端→错配标记已清", st.versionMismatch === false);
        lok("收割命令按精确路径全等（不误伤别人的 backend.exe）",
          reapCmd.includes("Stop-Process") && reapCmd.includes(staleBin));
      }

      // (e3) 收割失败（进程杀不掉/端口一直有人应答）→ 退回复用，不 spawn
      {
        let spawned = 0;
        const mgr = createBackendManager(baseDeps({
          app: { isPackaged: true, getPath: () => "/tmp", getVersion: () => "0.2.2" },
          spawn: () => { spawned++; return fakeChild(); },
          exec: () => {},
          fetch: async (url) => (String(url).endsWith("/api/desktop/ping")
            ? { ok: true, status: 200, json: async () => ({ app: "chengjie", version: "0.2.1" }) }
            : { status: 200 }),
          reapRoundMs: 5, reapPollMs: 1,
        }));
        await mgr.start(CFG);
        const st = mgr.getStatus();
        lok("收割失败→退回复用", st.status === "running-external");
        lok("收割失败→不盲目 spawn（防双进程抢会话）", spawned === 0);
        lok("收割失败→版本错配线索保留", st.versionMismatch === true);
      }
    } finally {
      if (savedResources === undefined) delete process.resourcesPath;
      else process.resourcesPath = savedResources;
    }
  }

  // (f) stopAndWait：等到进程真死才返回 true（B57 升级风暴修复——updater 必须
  //     确认旧后端已释放 pyrogram 会话文件，才能放 NSIS 装新版拉新后端）
  {
    let killIssued = 0;
    let alive = true;
    const mgr = createBackendManager(baseDeps({
      spawn: () => fakeChild(),
      fetch: async () => { throw new Error("unreachable"); },
      exec: () => { killIssued++; alive = false; }, // 击杀后进程消亡
      pidAlive: () => alive,
    }));
    await mgr.start(CFG);
    const dead = await mgr.stopAndWait(3000);
    lok("stopAndWait→击杀已发出", killIssued >= 1 || process.platform !== "win32");
    lok("stopAndWait→进程死透返回 true", dead === true);
    lok("stopAndWait→状态 stopped 且 pid 清空", mgr.getStatus().status === "stopped" && mgr.getStatus().pid === null);
  }

  // (g) stopAndWait：杀不掉的进程超时返回 false（不把退出流程永远锁死）
  {
    const mgr = createBackendManager(baseDeps({
      spawn: () => fakeChild(),
      fetch: async () => { throw new Error("unreachable"); },
      exec: () => {},
      pidAlive: () => true, // 永远杀不死
    }));
    await mgr.start(CFG);
    const dead = await mgr.stopAndWait(600);
    lok("stopAndWait→超时如实返回 false", dead === false);
  }

  // (h) stopAndWait：无子进程（外部自管/未拉起）→ 直接 true
  {
    const mgr = createBackendManager(baseDeps({
      spawn: () => fakeChild(),
      fetch: async () => ({ status: 200 }), // running-external：child 恒空
    }));
    await mgr.start(CFG);
    lok("stopAndWait→无 child 直接 true", (await mgr.stopAndWait(1000)) === true);
  }

  // (i) 后端异常退出 → 退避自愈重拉（2026-09-04 kouxing 事故：后端撞 FD 上限走防幽灵
  //     exit 78 自杀，壳只把状态记成 failed 就不管了 → 端口再没人 LISTENING，坐席
  //     「一直连不上」，得有人远程 stop+relaunch 才回来）
  {
    const timers = [];
    let spawned = 0;
    let alive = false;   // 后端是否真的在应答（死了之后探活必须失败，否则重拉会误判成「复用外部后端」）
    let exitCb = null;
    const mgr = createBackendManager(baseDeps({
      spawn: () => {
        spawned++; alive = true;
        return { pid: 4242, stdout: { on() {} }, stderr: { on() {} },
          on(evt, cb) { if (evt === "exit") exitCb = cb; } };
      },
      fetch: async () => { if (alive) return { status: 200 }; throw new Error("unreachable"); },
      setTimeout: (fn) => { timers.push(fn); return { unref() {} }; },
      respawnDelaysMs: [1], respawnMax: 2, respawnStableMs: 999999,
    }));
    const crash = async (drainOne) => {
      alive = false;
      exitCb(78, null);
      if (!drainOne) return;
      const fn = timers.shift();
      if (fn) fn();
      await new Promise((r) => setTimeout(r, 30));
    };
    await mgr.start(CFG);
    lok("首次拉起就绪", spawned === 1 && mgr.getStatus().status === "ready");

    await crash(false);                     // 防幽灵自杀
    lok("异常退出→已排一次自愈重拉", timers.length === 1);
    const t1 = timers.shift(); t1();
    await new Promise((r) => setTimeout(r, 30));
    lok("exit 78 后壳真的把后端重拉起来了", spawned === 2);

    await crash(true);
    lok("第二次异常退出仍在额度内", spawned === 3);

    // 起飞即坠：额度用尽后停手，线索留在 lastError（壳红条/健康看板可见），
    // 刻意不无限重启——那只会把真故障掩盖成一条永远在闪的状态灯
    await crash(false);
    lok("额度用尽→不再排重拉", timers.length === 0);
    lok("额度用尽→lastError 留线索", /gave up/.test(mgr.getStatus().lastError || ""));
    lok("额度用尽→spawn 次数封顶", spawned === 3);
  }

  // (i2) 稳定跑过一段时间后才崩 = 偶发崩溃 → 额度归零，永远能自愈
  {
    const timers = [];
    let spawned = 0;
    let alive = false;
    let exitCb = null;
    const mgr = createBackendManager(baseDeps({
      spawn: () => {
        spawned++; alive = true;
        return { pid: 4242, stdout: { on() {} }, stderr: { on() {} },
          on(evt, cb) { if (evt === "exit") exitCb = cb; } };
      },
      fetch: async () => { if (alive) return { status: 200 }; throw new Error("unreachable"); },
      setTimeout: (fn) => { timers.push(fn); return { unref() {} }; },
      respawnDelaysMs: [1], respawnMax: 1, respawnStableMs: 0, // 0 = 每次都算「跑够久」
    }));
    await mgr.start(CFG);
    for (let i = 0; i < 3; i++) {
      alive = false;
      exitCb(78, null);
      const fn = timers.shift();
      if (fn) fn();
      await new Promise((r) => setTimeout(r, 30));
    }
    lok("偶发崩溃→额度归零，三次都自愈", spawned === 4);
  }

  // (i3) 壳主动关闭（stopAndWait 已置 quitting）→ 绝不重拉（否则退不掉应用）
  {
    const timers = [];
    let spawned = 0;
    let exitCb = null;
    const mgr = createBackendManager(baseDeps({
      spawn: () => {
        spawned++;
        return { pid: 4242, stdout: { on() {} }, stderr: { on() {} },
          on(evt, cb) { if (evt === "exit") exitCb = cb; } };
      },
      fetch: async () => { throw new Error("unreachable"); },
      setTimeout: (fn) => { timers.push(fn); return { unref() {} }; },
      exec: () => {},
      pidAlive: () => false,
      respawnDelaysMs: [1], respawnMax: 3,
    }));
    await mgr.start(CFG);
    await mgr.stopAndWait(500);
    exitCb(0, "SIGTERM");
    lok("主动关闭→不排重拉", timers.length === 0);
    lok("主动关闭→不再 spawn", spawned === 1);
  }

  console.log(`backend-launcher.test.js lifecycle: ${lpass} passed`);
})().catch((e) => { console.error(e); process.exit(1); });
