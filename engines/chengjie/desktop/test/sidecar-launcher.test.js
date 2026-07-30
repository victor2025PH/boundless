"use strict";

// 协议边车解析纯函数单测（无框架，node 直跑）：node test/sidecar-launcher.test.js
//
// 重点覆盖那些**静默失败**的边界（都是本仓踩过的同类事故形态）：
//   · 只有 server.js 没有 node_modules → 起来必 MODULE_NOT_FOUND，必须当「没随包」
//   · 媒体目录写偏 → 图片稳定 404（tests/test_static_asset_paths.py 那类事故）
//   · 会话目录落进只读安装区 → 每次重启都要重新登录
//   · 缺 PY_API_TOKEN → 入站消息被 401 静默丢弃（能发不能收）
//   · 强制捆绑 Chromium → Facebook 抓自动化信号把登录弹回去
const assert = require("assert");
const path = require("path");
const fs = require("fs");
const {
  SPECS, resolveSidecarSpawn, resolveMediaDir, buildSidecarEnv,
  sidecarEnabled, classifySidecarIdentity, healthUrl, createAllSidecarManagers,
} = require("../sidecar-launcher.js");

let pass = 0;
function ok(name, cond) {
  assert.ok(cond, name);
  pass++;
}

const APP_DIR = "/repo/desktop";      // desktop 目录；其上级=引擎根
// 期望值与实现同源派生：path.resolve 在 Windows 上会补盘符（D:\repo），
// 写死 "/repo" 会让断言只在 posix 成立。
const REPO_ROOT = path.resolve(APP_DIR, "..");
const RES = "/Resources";

const WA = SPECS.whatsapp;
const MSG = SPECS.messenger;

// ── ① 规格表本身的不变量 ─────────────────────────────────────────────────────
ok("两个边车都在表里", !!WA && !!MSG);
ok("端口不撞", WA.port !== MSG.port);
ok("会话 env 键不撞（否则两边车互相覆盖对方的会话目录）",
  WA.envKeys.sessions !== MSG.envKeys.sessions);
ok("媒体子目录不撞（否则两平台的图混在一个目录）",
  WA.mediaSubdir !== MSG.mediaSubdir);

// ── ② 发布态：随包边车（server.js + node_modules 齐全）→ bundled ──────────────
for (const spec of [WA, MSG]) {
  const dir = path.join(RES, "services", spec.dirName);
  const set = new Set([path.join(dir, "server.js"), path.join(dir, "node_modules")]);
  const r = resolveSidecarSpawn(spec, {
    isPackaged: true, resourcesPath: RES, appDir: APP_DIR,
    execPath: "C:/app/智聊.exe", exists: (p) => set.has(p),
  });
  ok(`${spec.name} bundled kind`, r && r.kind === "bundled");
  ok(`${spec.name} bundled cwd`, r.cwd === dir);
  ok(`${spec.name} bundled entry`, r.args[0] === path.join(dir, "server.js"));
  // 关键：用 Electron 自己的 exe 当 Node 运行时（省掉随包第二份 node.exe）
  ok(`${spec.name} 用 execPath 当 node`, r.command === "C:/app/智聊.exe");
}

// ── ③ 缺 node_modules → 当作没随包（宁可如实不可用，不要起一个必崩的进程）──────
ok(
  "只有 server.js 没依赖 → null",
  resolveSidecarSpawn(WA, {
    isPackaged: true, resourcesPath: RES, appDir: APP_DIR,
    exists: (p) => p === path.join(RES, "services", WA.dirName, "server.js"),
  }) === null
);

// ── ④ 开发态回落引擎根 services/ ─────────────────────────────────────────────
const repoDir = path.join(REPO_ROOT, "services", MSG.dirName);
const repoSet = new Set([path.join(repoDir, "server.js"), path.join(repoDir, "node_modules")]);
const repo = resolveSidecarSpawn(MSG, {
  isPackaged: false, appDir: APP_DIR, exists: (p) => repoSet.has(p),
});
ok("repo kind", repo && repo.kind === "repo");
ok("repo cwd", repo.cwd === repoDir);

// ── ⑤ 两处都没有 → null（不是错误：未随包形态该显示不可用）───────────────────
ok(
  "都不存在 → null",
  resolveSidecarSpawn(WA, { isPackaged: true, resourcesPath: RES, appDir: APP_DIR, exists: () => false })
  === null
);

// ── ⑥ 媒体目录：必须落后端真正 serve 的 static 下 ────────────────────────────
// 冻结态 = resources/backend/_internal/src/web/static（PyInstaller 6.x onedir 布局）。
// 写偏一层，前端按 /static/protocol_media/... 取图就是永久 404。
const internalStatic = path.join(RES, "backend", "_internal", "src", "web", "static");
for (const spec of [WA, MSG]) {
  ok(
    `${spec.name} 冻结态媒体目录走 _internal`,
    resolveMediaDir(spec, {
      isPackaged: true, resourcesPath: RES, appDir: APP_DIR,
      exists: (p) => p === internalStatic,
    }) === path.join(internalStatic, "protocol_media", spec.mediaSubdir)
  );
}
// onefile / 旧版布局：数据落 backend/ 同级
const flatStatic = path.join(RES, "backend", "src", "web", "static");
ok(
  "无 _internal 时回落同级",
  resolveMediaDir(WA, {
    isPackaged: true, resourcesPath: RES, appDir: APP_DIR,
    exists: (p) => p === flatStatic,
  }) === path.join(flatStatic, "protocol_media", "whatsapp")
);
const repoStatic = path.join(REPO_ROOT, "src", "web", "static");
ok(
  "开发态媒体目录走引擎根",
  resolveMediaDir(WA, {
    isPackaged: false, appDir: APP_DIR, exists: (p) => p === repoStatic,
  }) === path.join(repoStatic, "protocol_media", "whatsapp")
);
ok("static 找不到 → 空串（跳过媒体落地，文字照常）",
  resolveMediaDir(WA, { isPackaged: true, resourcesPath: RES, appDir: APP_DIR, exists: () => false }) === "");

// ── ⑦ env 组装（两边车共有部分）──────────────────────────────────────────────
const DATA = "C:/Users/u/AppData/Roaming/智聊/data";
for (const spec of [WA, MSG]) {
  const env = buildSidecarEnv(spec, {
    dataDir: DATA,
    backendBaseUrl: "http://127.0.0.1:18799/",
    token: "tok123",
    mediaDir: "C:/app/static/protocol_media/x",
  });
  ok(`${spec.name} ELECTRON_RUN_AS_NODE=1`, env.ELECTRON_RUN_AS_NODE === "1");
  ok(`${spec.name} 端口来自规格`, env.PORT === String(spec.port));
  // 会话＝登录成果，必须落用户可写区，否则每次重启都要重新登录
  ok(`${spec.name} 会话目录在 dataDir 下`,
    env[spec.envKeys.sessions] === path.join(DATA, `${spec.name}-sessions`));
  // 基址尾斜杠不能带进拼接（//api/... 在某些反代下 404）
  ok(`${spec.name} ingest URL 去重尾斜杠`,
    env.PY_INGEST_URL === "http://127.0.0.1:18799/api/internal/protocol/ingest");
  ok(`${spec.name} status URL`,
    env.PY_STATUS_URL === "http://127.0.0.1:18799/api/internal/protocol/session-status");
  ok(`${spec.name} 令牌透传（缺了入站会被 401 静默丢弃）`, env.PY_API_TOKEN === "tok123");
  ok(`${spec.name} 媒体 URL 前缀与后端挂载一致`,
    env[spec.envKeys.mediaUrlBase] === `/static/protocol_media/${spec.mediaSubdir}`);
}

// ── ⑧ Messenger 专属 env ────────────────────────────────────────────────────
const msgEnv = buildSidecarEnv(MSG, { dataDir: DATA, backendBaseUrl: "http://127.0.0.1:18799" });
// 不设它 → playwright 去找 %LOCALAPPDATA%\ms-playwright，客户机上那里是空的
ok("PLAYWRIGHT_BROWSERS_PATH=0（从 node_modules 取随包浏览器）",
  msgEnv.PLAYWRIGHT_BROWSERS_PATH === "0");
ok("MSG_RESTORE_ON_BOOT=1（重启后不必重新人工登录）",
  msgEnv.MSG_RESTORE_ON_BOOT === "1");
// 核心不变量：**绝不**强制捆绑 Chromium。服务默认优先系统真 Chrome，捆绑版只作兜底；
// 钉死成捆绑版会让 UA 与 Sec-CH-UA 自相矛盾 → Facebook 把登录弹回去。
ok("不设 MSG_BROWSER_CHANNEL（保住「优先系统 Chrome」的默认）",
  !("MSG_BROWSER_CHANNEL" in msgEnv));
ok("不强制 headless（桌面登录要能看见浏览器窗口）", !("MSG_HEADLESS" in msgEnv));

// 无 dataDir / 无 token 时不应写出空值键（空 token 比没有更糟：会被当成有效值发出去）
const bare = buildSidecarEnv(WA, { backendBaseUrl: "http://127.0.0.1:18799" });
ok("无 dataDir → 不写会话目录键", !(WA.envKeys.sessions in bare));
ok("无 token → 不写 PY_API_TOKEN", !("PY_API_TOKEN" in bare));
ok("无 mediaDir → 不写媒体目录键", !(WA.envKeys.mediaDir in bare));

// ── ⑨ 开关语义：缺省拉起（「下载即可用」），显式 false 才不拉 ─────────────────
ok("缺省启用 whatsapp", sidecarEnabled({}, "whatsapp") === true);
ok("缺省启用 messenger", sidecarEnabled({ backend: {} }, "messenger") === true);
ok("显式 false → 不拉起",
  sidecarEnabled({ sidecars: { whatsapp: { enabled: false } } }, "whatsapp") === false);
ok("单关 whatsapp 不影响 messenger",
  sidecarEnabled({ sidecars: { whatsapp: { enabled: false } } }, "messenger") === true);

// ── ⑩ 健康探针 URL ──────────────────────────────────────────────────────────
ok("health URL", healthUrl(WA.port) === `http://127.0.0.1:${WA.port}/health`);

// ── ⑫ 身份判定：只看 ok:true 会把外来服务当自己的用 ──────────────────────────
{
  const own = classifySidecarIdentity(WA, { ok: true, svc: "wa-baileys" });
  ok("身份对上 → 复用", own.reusable && !own.foreign);
  // 升级期必然出现：新壳 + 还没重启的旧边车。按旧行为放行，否则新版一装就罢工。
  const legacy = classifySidecarIdentity(WA, { ok: true });
  ok("无 svc 字段 → 按旧版放行", legacy.reusable && !legacy.foreign);
  // 核心：别家服务恰好也回 ok:true —— 绝不能复用
  const foreign = classifySidecarIdentity(WA, { ok: true, svc: "messenger-web" });
  ok("svc 对不上 → 判外来、不复用", !foreign.reusable && foreign.foreign);
  ok("外来判定带出对方身份便于排查", foreign.detail.includes("messenger-web"));
  ok("两边车互不误认",
    classifySidecarIdentity(MSG, { ok: true, svc: "wa-baileys" }).foreign === true);
  ok("没响应 → 不复用也不算外来",
    (() => { const r = classifySidecarIdentity(WA, null); return !r.reusable && !r.foreign; })());
  ok("ok 不为 true → 不复用",
    !classifySidecarIdentity(WA, { ok: false, svc: "wa-baileys" }).reusable);
}

// ── ⑬ svcId 必须与服务端 /health 真回的值一致（新的跨文件漂移点）─────────────
// 对不上 → 壳把自家边车判成「外来服务」，WhatsApp/Messenger 在客户机上直接报端口冲突。
for (const spec of [WA, MSG]) {
  const src = fs.readFileSync(
    path.join(__dirname, "..", "..", "services", spec.dirName, "server.js"), "utf8");
  ok(`${spec.name} 服务端 /health 真回 svc:"${spec.svcId}"`,
    src.includes(`svc: "${spec.svcId}"`));
}

// ── ⑪ 多边车管理器：缺包不得让另一个也不启动 ─────────────────────────────────
(async () => {
  const started = [];
  const all = createAllSidecarManagers({
    app: { isPackaged: false, getPath: () => "/ud" },
    spawn: () => { throw new Error("boom"); },   // 让 spawn 必败
    exec: () => {},
    fs: { existsSync: () => false, mkdirSync: () => {}, createWriteStream: () => null },
    fetch: null,
  });
  for (const [k, m] of Object.entries(all.managers)) {
    const origStart = m.start;
    m.start = async (c) => { started.push(k); return origStart(c); };
  }
  await all.startAll({});
  ok("startAll 两个边车都被尝试（一个失败不拖累另一个）", started.length === 2);
  const st = all.getStatus();
  ok("状态按边车名分开", "whatsapp" in st && "messenger" in st);
  ok("缺包时报 absent 而非崩溃",
    st.whatsapp.status === "absent" && st.messenger.status === "absent");
  all.stopAll(); // 不该抛
  console.log(`✓ sidecar-launcher: ${pass} 项断言全过`);
})();
