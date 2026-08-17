"use strict";

const { app, BrowserWindow, ipcMain, session, clipboard, Menu, Notification, shell, dialog, nativeImage, powerMonitor } = require("electron");
const path = require("path");
const fs = require("fs");
const { spawn, exec } = require("child_process");
const { chromeLikeUserAgent, isWhatsappUrl, needsChromeUa, urlNeedsChromeUa } = require("./webview-ua.js");
const { fingerprintArg, accountIdFromPartition } = require("./inject/fingerprint.js");
const { createBackendManager } = require("./backend-launcher.js");
const { createAllSidecarManagers } = require("./sidecar-launcher.js");
const brandUtil = require("./brand-util.js");
const tokenUtil = require("./token-util.js");

// 面向用户的版本串：package.json 的 displayVersion 优先（内测「1.001」这类展示号
// 不是合法 semver，进不了 version 字段——那是 electron-builder/updater 的机器版本），
// 缺字段回落 app.getVersion()。更新比对仍用 semver，勿拿本函数结果参与版本比较。
function displayVersion() {
  try {
    const dv = require("./package.json").displayVersion;
    if (dv) return String(dv);
  } catch (e) { /* 读不到 package.json → 回落 semver */ }
  try { return app.getVersion(); } catch (e) { return "dev"; }
}

// `--first-run`：无视「只弹一次」标记重看首启向导。写进 env 而不是走 IPC，是为了让
// shell-preload 能同步读到（向导在 DOM 就绪那一刻就要判断弹不弹，等不起一次往返）。
// 在这里而非 ready 里设置：preload 可能先于任何 ready 回调求值。
if (process.argv.includes("--first-run")) process.env.AITR_FORCE_FIRSTRUN = "1";

// D3：每账号确定性指纹缓存（account_id → fingerprint）。启动/运行时新增账号前拉取，
// 供 session UA / Accept-Language / webview additionalArguments 注入，使多号内嵌互不关联。
const FP_BY_ACCOUNT = {};
function fpEnabled() {
  const f = (config && config.fingerprint) || {};
  return f.enabled !== false; // 默认开启
}

// 配置路径：打包态 __dirname 在只读 asar 内，fs.writeFileSync 必抛（首启向导保存
// 语言/令牌静默失败、设置不持久）→ 落 app.getPath("userData")/config.json（可写、
// 卸载重装/升级均保留）；首启若 userData 无 config.json 而随包有种子（asar 内可读），
// 先拷贝再用。开发态保持仓库内路径不变（直接编辑可生效，开发流程零变化）。
// 时序：app.getPath("userData") 在 app ready 前即可用（基于 appData+productName），
// 本文件顶层 loadConfig() 前求值安全。
function resolveConfigPath() {
  const devPath = path.join(__dirname, "config.json");
  if (!app.isPackaged) return devPath;
  const userPath = path.join(app.getPath("userData"), "config.json");
  try {
    if (!fs.existsSync(userPath)) {
      fs.mkdirSync(path.dirname(userPath), { recursive: true });
      // 用 readFileSync+writeFileSync 而非 copyFileSync：asar 虚拟 fs 对前者支持最稳
      if (fs.existsSync(devPath)) fs.writeFileSync(userPath, fs.readFileSync(devPath));
    }
  } catch (e) {
    // 种子拷贝失败不阻断启动：loadConfig 走内置默认，saveConfigPatch 时再尝试写 userPath
  }
  return userPath;
}

const CONFIG_PATH = resolveConfigPath();

function loadConfig() {
  try {
    // ⚠ 必须剥 BOM 再 parse（2026-08-15 173 实锤）：运维用 PowerShell 5.1
    // `Set-Content -Encoding UTF8` 改 config.json 会带 UTF-8 BOM，裸 JSON.parse 直接抛
    // → 回落内置默认（token=admin）→ 托管版启动时 maybeRotateManagedToken 见默认令牌
    // 即轮换并把**内存里的默认配置整份写回**——用户的账号/标签/开关全部被静默清空。
    return JSON.parse(fs.readFileSync(CONFIG_PATH, "utf-8").replace(/^\uFEFF/, ""));
  } catch (e) {
    return {
      backend: { base_url: "http://127.0.0.1:18799", token: "admin" },
      translate: { target_lang: "zh", auto: false },
      platforms: [],
      accounts: [],
    };
  }
}

let config = loadConfig();

// 出厂默认值迁移（2026-08-14）：userData/config.json 是首启种子、升级保留——产品级
// 改名（统一收件箱 → AI 工作台 → 人工操作台）会被老副本的旧出厂值永久顶住（lianbei 升 1.0.31 实锤）。
// 只迁「等于旧出厂默认」的值（用户显式自定义过的标签不动），幂等，写失败不阻断启动。
(function migrateFactoryDefaults() {
  try {
    const ui = config && config.unified_inbox;
    if (ui && (ui.label === "统一收件箱" || ui.label === "AI 工作台")) {
      ui.label = "人工操作台";
      fs.writeFileSync(CONFIG_PATH, JSON.stringify(config, null, 2));
    }
  } catch (e) { /* 迁移失败保持旧名，不影响启动 */ }
})();

// 托管版（managed edition）：客户拿到的是「我们预置好 AI、按字符卖额度」的成品，
// 首启向导里绝不该出现「配置 AI 模型」「后台访问令牌」这类自建/开发者概念——客户
// 不知道 API Key 是什么，看到只会以为买错了东西。开发态（从源码 npm start）反过来
// 需要这些字段来调试。
//
// 判定优先级：env AITR_MANAGED_EDITION（QA 手动覆盖）> config.json 显式 managed 字段
// （将来若出「自建版」打包，可在其种子里设 false）> 默认 app.isPackaged。
// 默认取 isPackaged 的好处：现有打包流程零改动——打出来给客户的包天然是托管版，
// 而 npm start / electron . 开发态天然非托管。与 --first-run 同款：写进 env 让
// shell-preload 同步读到（向导 DOM 就绪那一刻就要决定显示哪些字段，等不起一次 IPC）。
let _managedEdition = !!app.isPackaged;
if (typeof config.managed === "boolean") _managedEdition = config.managed;
if (process.env.AITR_MANAGED_EDITION === "1") _managedEdition = true;
else if (process.env.AITR_MANAGED_EDITION === "0") _managedEdition = false;
process.env.AITR_MANAGED_EDITION = _managedEdition ? "1" : "0";

// 内置默认产品图标（copy-shared 落地）。窗口创建时用它作原生图标兜底，
// 白标改回默认时也还原到它。用 ChatX 产品标而非母标：任务栏 / Alt-Tab 里能直接
// 认出「这是智聊」，多个无界产品并存时可区分；白标客户仍由自定义 logo 覆盖
// （isDefaultMark 判定的是后端返回的 mark URL，与本地图标路径无关，故切换零副作用）。
const DEFAULT_BRAND_ICON = path.join(__dirname, "renderer", "brand", "chatx.png");

// 品牌信息统一形状：{ product, company, website, mark }（纯逻辑见 brand-util.js）。
// 离线兜底：config.brand → copy-shared 落地的 renderer/brand/brand.json → 硬编码默认。
function brandInfoLocal() {
  let brandJson = null;
  try {
    brandJson = JSON.parse(
      fs.readFileSync(path.join(__dirname, "renderer", "brand", "brand.json"), "utf-8"));
  } catch (e) { /* 无 brand.json → 走硬编码兜底 */ }
  return brandUtil.pickBrandLocal((config && config.brand) || {}, brandJson);
}

// 运行期白标：向后端拉实时生效品牌（settings 页改了即时反映到桌面壳），
// 2s 超时 + 任何异常回落本地——绝不阻断桌面壳。
async function fetchLiveBrand() {
  const { base_url, token } = config.backend || {};
  if (!base_url) return null;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 2000);
  try {
    const r = await fetch(`${base_url}/api/admin/branding`, {
      headers: { Authorization: `Bearer ${token || ""}` },
      signal: ctl.signal,
    });
    if (!r.ok) return null;
    return brandUtil.normalizeLiveBrand(await r.json());
  } catch (e) {
    return null; // 后端未起 / 超时 / 无授权 → 用本地兜底
  } finally {
    clearTimeout(timer);
  }
}

// 实时优先、本地兜底的统一入口（关于弹窗 / 图标用）。
async function resolveBrand() {
  return (await fetchLiveBrand()) || brandInfoLocal();
}

// macOS dock 图标运行期热替换（Win/Linux 无 app.dock，静默跳过）。
function _setDockIcon(img) {
  try {
    if (process.platform === "darwin" && app.dock && img && !img.isEmpty()) {
      app.dock.setIcon(img);
    }
  } catch (e) { /* dock 图标设置失败不影响使用 */ }
}

// 运行期把生效品牌 logo 同步成原生窗口/任务栏图标——settings 页改了 logo，
// 桌面壳窗口 focus 时（节流后）重取并热替换，无需重开窗口即时生效。
//   custom  : 白标自定义 logo（且与上次不同）→ 下载 + setIcon
//   default : 白标改回默认（上次是自定义）→ 还原内置图标
//   none    : 无变化 → 不动，避免无谓下载/闪烁
// 默认无界 mark 在窗口创建时已用本地文件设过；任何失败都保留现图标、不阻断。
async function applyLiveWindowBranding(win, { force = false } = {}) {
  try {
    if (!win || win.isDestroyed()) return;
    const now = Date.now();
    if (!force && !brandUtil.shouldCheckBrand(now, win._brandLastCheck)) return;
    win._brandLastCheck = now;
    const bi = await fetchLiveBrand();
    if (!bi) return;
    const act = brandUtil.resolveIconAction(bi.mark, win._brandMark);
    if (act.action === "custom") {
      const url = brandUtil.resolveBackendUrl((config.backend || {}).base_url, act.mark);
      if (!url) return;
      const r = await fetch(url);
      if (!r.ok) return;
      const img = nativeImage.createFromBuffer(Buffer.from(await r.arrayBuffer()));
      if (!img.isEmpty() && !win.isDestroyed()) {
        win.setIcon(img);
        _setDockIcon(img);
        win._brandMark = act.mark;
      }
    } else if (act.action === "default") {
      if (fs.existsSync(DEFAULT_BRAND_ICON) && !win.isDestroyed()) {
        const img = nativeImage.createFromPath(DEFAULT_BRAND_ICON);
        win.setIcon(img);
        _setDockIcon(img);
        win._brandMark = null;
      }
    }
  } catch (e) { /* 图标热替换失败不影响使用 */ }
}

// 后端 sidecar 生命周期：免手动起 Python。拉起前先探活（已在跑→复用），退出回收。
const backendManager = createBackendManager({ app, spawn, exec, fs, fetch: global.fetch });

// 协议边车（WhatsApp/Baileys + Messenger/Web）：随包 + 自动拉起，使这两条接入路
// 装完即可用，而不是灰着显示「未启用 / 需运维配置」。缺包或起不来一律软降级
// （后端诊断报 service_down，弹窗如实说「服务未运行」），绝不阻断桌面启动。
const sidecars = createAllSidecarManagers({ app, spawn, exec, fs, fetch: global.fetch });

/** 浅合并并持久化 config.json（首启向导用）；同步更新内存 config。返回 {ok}。
 *  首启完成会写 onboarding: { completed, completed_at, edition }——与 renderer
 *  localStorage 旗标双权威，清缓存后仍可不弹向导。managed 布尔见文件顶部判定。 */
function saveConfigPatch(patch) {
  try {
    const next = Object.assign({}, config);
    for (const [k, v] of Object.entries(patch || {})) {
      next[k] = (v && typeof v === "object" && !Array.isArray(v))
        ? Object.assign({}, config[k] || {}, v) : v;
    }
    fs.mkdirSync(path.dirname(CONFIG_PATH), { recursive: true });
    fs.writeFileSync(CONFIG_PATH, JSON.stringify(next, null, 2), "utf-8");
    config = next;
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
}

ipcMain.handle("desktop:save-config", (_e, patch) => saveConfigPatch(patch || {}));

/** 调用后端翻译接口（主进程发起，规避 webview 的 CORS / 混合内容限制）。 */
async function backendTranslate(text, targetLang) {
  const { base_url, token } = config.backend || {};
  const r = await fetch(`${base_url}/api/unified-inbox/translate`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ text, target_lang: targetLang || (config.translate || {}).target_lang || "zh" }),
  });
  const d = await r.json();
  if (!d.ok) return "";
  const t = d.translation || {};
  return t.translated_text || t.text || t.translated || "";
}

// 批量翻译对齐辅助（纯函数,与注入调度器共用同一实现,已单测）。
const { alignBatchResponse: _alignBatchResponse } = require("./shared/inject/translate-scheduler.js");

/** 批量翻译（主进程发起）：一次往返译整批,返回「与输入等长、按序对齐」的 string[]。
 *  复用后端 /api/unified-inbox/translate-batch（服务端 gather + 信号量,与单条同一 TranslationService）。 */
async function backendTranslateBatch(texts, targetLang) {
  const { base_url, token } = config.backend || {};
  const arr = Array.isArray(texts) ? texts : [];
  const items = arr.map((t, i) => ({ id: String(i), text: String(t || "") }));
  const r = await fetch(`${base_url}/api/unified-inbox/translate-batch`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ items, target_lang: targetLang || (config.translate || {}).target_lang || "zh" }),
  });
  const d = await r.json();
  return _alignBatchResponse(arr, d);
}

/** 调用后端媒体翻译：图片 OCR(/translate-image) 或 语音转写(/translate-voice) → 翻译。
 *  kind=image|voice；b64 为去掉 dataURL 前缀的纯 base64。主进程发起以规避 webview CORS/CSP。 */
async function backendTranslateMedia(kind, b64, targetLang) {
  const { base_url, token } = config.backend || {};
  const isImg = kind === "image";
  const pathname = isImg ? "/api/unified-inbox/translate-image" : "/api/unified-inbox/translate-voice";
  const tgt = targetLang || (config.translate || {}).target_lang || "zh";
  const body = isImg ? { image_b64: b64, target_lang: tgt } : { audio_b64: b64, target_lang: tgt };
  const r = await fetch(`${base_url}${pathname}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify(body),
  });
  return await r.json();
}

/** 调用后端智能回复（上下文版）。转发 persona_id/platform/chat_key，
 *  否则面板选的人设到不了后端、永远用默认人设。 */
async function backendSmartReply(payload) {
  const { base_url, token } = config.backend || {};
  const p = payload || {};
  const r = await fetch(`${base_url}/api/desktop/smart-reply`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({
      messages: Array.isArray(p.messages) ? p.messages : [],
      persona_id: p.persona_id || "",
      platform: p.platform || "",
      chat_key: p.chat_key || "",
      target_lang: p.target_lang || "",
    }),
  });
  return await r.json();
}

/** 把桌面端官方 web 看到的消息回流统一收件箱（P1 同步桥）。account_id 按平台从 config 补齐。 */
async function backendIngest(payload) {
  const { base_url, token } = config.backend || {};
  const plat = String(payload.platform || "");
  const pcfg = (config.platforms || []).find((p) => p.id === plat) || {};
  const account_id = payload.account_id || pcfg.account_id || `${plat}-desktop`;
  const r = await fetch(`${base_url}/api/desktop/ingest`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ ...payload, account_id }),
  });
  return await r.json();
}

/** 通用后端 GET（主进程发起，规避 webview/renderer 的 CSP/CORS）。 */
async function backendGet(pathname, query) {
  const { base_url, token } = config.backend || {};
  const qs = query
    ? "?" + new URLSearchParams(Object.entries(query).filter(([, v]) => v != null && v !== "")).toString()
    : "";
  const r = await fetch(`${base_url}${pathname}${qs}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  return await r.json();
}

ipcMain.handle("desktop:apply-whatsapp-ua", async (_e, acc) => {
  await applyWhatsappSessionUa(acc || {});
  return { ok: true };
});

ipcMain.handle("desktop:config", () => ({
  ...config,
  whatsapp_user_agent: chromeLikeUserAgent(process.versions.chrome),
}));

// ui_visibility 服务端旗标（GET /api/desktop/ui-flags，端点免鉴权）。renderer 是
// file:// 源且后端默认不回 CORS 头 → 只能由主进程代取（backendGet 同款理由）。
// 2.5s 超时快败：后端未起时不拖慢壳启动，renderer 侧按 fail-open 走本地配置。
ipcMain.handle("desktop:ui-flags", async () => {
  const { base_url } = config.backend || {};
  if (!base_url) return null;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 2500);
  try {
    const r = await fetch(`${base_url.replace(/\/+$/, "")}/api/desktop/ui-flags`, { signal: ctrl.signal });
    return r.ok ? await r.json() : null;
  } catch (e) {
    return null;
  } finally {
    clearTimeout(timer);
  }
});

// 工作台分区的 Service Worker 外科清除（2026-08-15 117 实锤）：分区里残留的旧版 SW
// 会把 /workspace 导航整个吞掉挂死（无 dom-ready 无 did-fail-load，遮罩钉死「正在载入
// 工作台…」），而 SW 只在页面成功导航时才自更新——导航被它自己吞了就永远升不了级，
// 形成自锁，只能从进程侧清存储。由 renderer 的装载看门狗在导航长期静默时调用。
ipcMain.handle("desktop:clear-workspace-sw", async () => {
  try {
    const ses = session.fromPartition("persist:backend-workspace");
    await ses.clearStorageData({ storages: ["serviceworkers", "cachestorage"] });
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
});

/** 后端可达性探针（主进程发起，规避 webview/renderer 的 CSP/CORS）。
 *  /login 无需鉴权即返回 200；任何 HTTP 响应都代表后端可达。用于「后端未起→自动重连」。 */
// 后端拉起状态（idle/probing/starting/ready/running-external/failed/disabled/stopped）。
// renderer 可据此把「正在连接后台」细化为「正在启动后台服务…」并在 failed 时给指引。
ipcMain.handle("desktop:backend-spawn-status", () => backendManager.getStatus());

// 边车拉起状态：接入弹窗排障用（absent=没随包 / failed=起不来 / running-external=用户自管）。
ipcMain.handle("desktop:sidecar-status", () => sidecars.getStatus());

ipcMain.handle("desktop:backend-health", async () => {
  const { base_url } = config.backend || {};
  if (!base_url) return { ok: false, error: "no base_url" };
  // 端口冲突时「有响应」恰恰是最危险的信号：应答的不是自家后端。此处以 launcher 的
  // 身份判定为准（见 backend-launcher.classifyBackendIdentity），否则 renderer 会把
  // 占了端口的别家服务当成后台加载进来。
  try {
    const st = backendManager.getStatus();
    if (st && st.status === "port-conflict") {
      return { ok: false, conflict: true, error: st.lastError || "port conflict" };
    }
  } catch (e) { /* 状态不可读时按旧逻辑走探针 */ }
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 2500);
  try {
    const r = await fetch(`${base_url}/login`, { method: "GET", redirect: "manual", signal: ctrl.signal });
    return { ok: true, status: r.status };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  } finally {
    clearTimeout(timer);
  }
});

// ── P0-1 首启向导：AI Key 状态 / 测试 / 保存（主进程转发，规避 renderer CSP/CORS）──
// 保存走后端 POST /api/setup/ai-key → 写 config.local.yaml overlay（不动主 config 注释）
// 并热重建后端 AI 运行时（翻译免重启生效）。
ipcMain.handle("desktop:setup-ai-status", async () => {
  const { base_url, token } = config.backend || {};
  try {
    const r = await fetch(`${base_url}/api/setup/ai`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    return await r.json();
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
});

// 首启体验额度状态（P2）：首启向导收尾步用它告知「你有多少额度、能用多久」。
// 走后端 /api/workspace/quota（任意登录用户可读，无敏感字段）。
ipcMain.handle("desktop:trial-status", async () => {
  const { base_url, token } = config.backend || {};
  try {
    const r = await fetch(`${base_url}/api/workspace/quota`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    return await r.json();
  } catch (e) {
    // 后端还没起来 / 老版本没这个端点 → 视为"没有体验额度"，向导直接跳过该步，
    // 绝不凭空承诺一份可能不存在的额度。
    return { ok: false, visible: false };
  }
});

// ── P2 注册领 7 天：建单 / 轮询 / 取客服绑定码 ──────────────────────────────
// 三个都用 backendGet/Post 的容错语义：后端没起来或版本旧 → 返回错误码而不是抛，
// 首启向导据此显示「可跳过的提示」，绝不把人卡在开机第一屏。

ipcMain.handle("desktop:trial-claim", async (_e, body) => {
  try {
    return await backendPost("/api/admin/license/trial-claim", body || {});
  } catch (e) {
    return { ok: false, error: "network" };
  }
});

ipcMain.handle("desktop:trial-claim-status", async () => {
  try {
    return await backendGet("/api/admin/license/trial-claim");
  } catch (e) {
    return { ok: false, error: "network" };
  }
});

ipcMain.handle("desktop:trial-bind-code", async () => {
  try {
    return await backendPost("/api/admin/license/trial-bind-code", {});
  } catch (e) {
    return { ok: false, error: "network" };
  }
});

// 首启漏斗埋点：向导曝光/领取/跳过等事件经本地后端转发官网 /api/track。
// 纯 fire-and-forget：失败静默（埋点绝不影响向导），事件名白名单在后端收口。
ipcMain.handle("desktop:trial-funnel", async (_e, body) => {
  try {
    return await backendPost("/api/admin/license/trial-funnel", body || {});
  } catch (e) {
    return { ok: false, error: "network" };
  }
});

// 壳层 UI 交互埋点 → 后端 /api/telemetry/ui-event（进程计数 + ops.ui_event_trend 按日落库）。
// 首个用例＝副驾双实现退役读数（cpshell_* 前缀：🧪 手动切换 / iframe 看门狗回退与恢复）——
// 「原生 aside 何时可删」从拍脑袋变成看 /api/admin/ui-event-trend?prefix=cpshell_。
//
// P1 增量（2026-08-13 舰队遥测实测坐实）：纯 fire-and-forget 有个结构性盲区——
// 冷启动窗的事件（iframe 回退/启动闸门等待，恰是最需要观测的那批）发出时后端
// 还没起来，POST 必失败 → 事件永久丢失。198 上 7 天 cpshell_ 读数为零就是这么来的：
// 不是没发生，是信使死在了它要报告的那场事故里。改为「失败进内存队列、后端就绪
// 后补发」：容量 200 丢最旧、30s 重试、逐条清空；队列只活在内存（App 退出即弃，
// 刻意不落盘——埋点丢一次会话可接受，写盘反而引入新故障面）。
const _uiEvtQueue = [];
let _uiEvtFlushTimer = null;

function _scheduleUiEvtFlush() {
  if (_uiEvtFlushTimer) return;
  _uiEvtFlushTimer = setInterval(async () => {
    if (!_uiEvtQueue.length) {
      clearInterval(_uiEvtFlushTimer);
      _uiEvtFlushTimer = null;
      return;
    }
    try {
      // 首条成功＝后端已就绪，顺势逐条清空；中途再断则留队下一轮
      while (_uiEvtQueue.length) {
        await backendPost("/api/telemetry/ui-event", _uiEvtQueue[0]);
        _uiEvtQueue.shift();
      }
    } catch (e) { /* 后端仍未就绪：下一轮再试 */ }
  }, 30000);
}

ipcMain.handle("desktop:ui-event", async (_e, body) => {
  const b = body || {};
  const payload = {
    page: String(b.page || "desktop-shell"),
    action: String(b.action || ""),
  };
  try {
    return await backendPost("/api/telemetry/ui-event", payload);
  } catch (e) {
    if (_uiEvtQueue.length >= 200) _uiEvtQueue.shift();
    _uiEvtQueue.push(payload);
    _scheduleUiEvtFlush();
    return { ok: false, error: "queued" };
  }
});

// 外链白名单。renderer 里跑着第三方 webview，把裸 openExternal 暴露给它等于把
// 「用默认程序打开任意 URL」的能力交出去；这里只放行客服深链两个域。
const EXTERNAL_ALLOW = /^https:\/\/(t\.me|wa\.me|api\.whatsapp\.com)\//i;
ipcMain.handle("desktop:open-external", async (_e, url) => {
  const u = String(url || "");
  if (!EXTERNAL_ALLOW.test(u)) return { ok: false, error: "not_allowed" };
  try {
    await shell.openExternal(u);
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
});

ipcMain.handle("desktop:setup-test-ai", async (_e, body) => {
  const { base_url, token } = config.backend || {};
  try {
    const r = await fetch(`${base_url}/api/setup/test-ai`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify(body || {}),
    });
    return await r.json();
  } catch (e) {
    return { ok: false, msg: String((e && e.message) || e) };
  }
});

ipcMain.handle("desktop:setup-save-ai-key", async (_e, body) => {
  const { base_url, token } = config.backend || {};
  try {
    const r = await fetch(`${base_url}/api/setup/ai-key`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify(body || {}),
    });
    return await r.json();
  } catch (e) {
    return { ok: false, detail: String((e && e.message) || e) };
  }
});

ipcMain.handle("desktop:copy", (_e, text) => {
  try {
    clipboard.writeText(String(text || ""));
    return true;
  } catch (e) {
    return false;
  }
});

ipcMain.handle("desktop:diag", (_e, msg) => {
  console.log(`[inject] ${msg}`);
  return true;
});

// 原生系统通知（新私聊消息弹窗，由工作台前端按用户选择的「弹窗方式」调用）。
// 点击通知 → 唤起并聚焦主窗口。系统不支持/被禁用时返回 {ok:false}，前端回落浏览器通知/应用内提示。
ipcMain.handle("desktop:notify", (_e, args) => {
  try {
    const a = args || {};
    if (!Notification.isSupported()) return { ok: false, error: "unsupported" };
    const n = new Notification({
      title: String(a.title || "新消息"),
      body: String(a.body || ""),
      silent: a.silent === true,
    });
    n.on("click", () => {
      const w = BrowserWindow.getAllWindows()[0];
      if (w) { try { if (w.isMinimized()) w.restore(); w.show(); w.focus(); } catch (e) { /* ignore */ } }
    });
    n.show();
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
});

// 选择器覆写层（D1 热更新）：注入脚本启动时拉取后端下发的选择器修正。
// 官方改版导致选择器失配时，运营改 config/desktop_selector_profiles.json 即可热修，
// 无需重发桌面包。后端不可达/无覆写时返回 {ok:true, profiles:{}}，注入静默用内置档。
ipcMain.handle("desktop:selector-profiles", async () => {
  try {
    return await backendGet("/api/desktop/selector-profiles");
  } catch (e) {
    return { ok: false, profiles: {}, error: String(e) };
  }
});

// 注入健康信标（D1b）：注入脚本把「逐选择器命中」状态上报后端，供运营看板判断
// 哪个账号/平台的注入因官方改版失配（而非笼统「坏了」）。后端不可达静默忽略。
ipcMain.handle("desktop:inject-health", async (_e, payload) => {
  try {
    return await backendPost("/api/desktop/inject-health", payload || {});
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

// 自动化健康看板（壳层聚合读）：把后端「全账号注入健康 + 持续失配」汇总下发给壳层 🩺 面板，
// 让运营在桌面壳内一眼看清各内嵌账号注入是否健康（而非只看当前聚焦 webview）。后端不可达返回空摘要。
ipcMain.handle("desktop:inject-health-list", async (_e, args) => {
  try {
    const persist_sec = (args && args.persist_sec) || undefined;
    return await backendGet("/api/desktop/inject-health", { persist_sec });
  } catch (e) {
    return { ok: false, summary: {}, accounts: [], error: String(e) };
  }
});

// 受控出站队列概览（D4b 壳层读）：pending/claimed/sent/failed 计数 + 近期命令预览，
// 让运营看清全自动回复经 send-gate/kill-switch 后是否在正常流转/有无卡死。后端不可达返回空摘要。
ipcMain.handle("desktop:outbound-stats", async (_e, args) => {
  try {
    const limit = (args && args.limit) || undefined;
    return await backendGet("/api/desktop/outbound/stats", { limit });
  } catch (e) {
    return { ok: false, summary: {}, recent: [], error: String(e) };
  }
});

// 注入「持续失配」告警流（壳层 🩺 红点预警 + 面板告警块用）：只有**连续**失配超阈值才进 alerts
// （即时抖动自愈、不误报）；events 为状态跃迁趋势。后端不可达返回空告警（红点不亮）。
ipcMain.handle("desktop:inject-alerts", async (_e, args) => {
  try {
    const persist_sec = (args && args.persist_sec) || undefined;
    const limit = (args && args.limit) || undefined;
    return await backendGet("/api/desktop/inject-health/alerts", { persist_sec, limit });
  } catch (e) {
    return { ok: false, alerts: [], events: [], error: String(e) };
  }
});

// D1 一键热修：向后端取「覆写文件本地路径」（不存在则首次写模板），用系统默认编辑器打开。
// 后端返回的路径与注入读取的 selector-profiles 同一文件（同 config 目录），避免「打开 A 却读 B」。
ipcMain.handle("desktop:open-selectors", async () => {
  try {
    const r = await backendGet("/api/desktop/selector-profiles/path");
    if (!r || !r.ok || !r.path) {
      return { ok: false, error: (r && r.error) || "后端未返回路径" };
    }
    const err = await shell.openPath(r.path); // 成功返回 ""，失败返回错误串
    if (err) return { ok: false, path: r.path, error: err };
    return { ok: true, path: r.path, created: !!r.created };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

// D1 校验：读覆写文件给运营显式反馈（解析失败/被忽略字段），与注入读取同一文件。
ipcMain.handle("desktop:validate-selectors", async () => {
  try {
    return await backendGet("/api/desktop/selector-profiles/validate");
  } catch (e) {
    return { ok: false, valid: false, error: String(e), profiles: 0, platforms: [], dropped: [] };
  }
});

// D4 受控出站桥：轮询「受控出站队列」取走发给本内嵌账号的全自动回复（已先过后端
// send-gate/kill-switch 闸门），renderer 据此调 webview fill-composer 在官方页 DOM 发送，
// 再回执。后端不可达静默忽略（autopilot 命令仍留在队列，下轮重取）。
ipcMain.handle("desktop:outbound-pull", async (_e, { platform, account_id, limit }) => {
  try {
    return await backendGet("/api/desktop/outbound", { platform, account_id, limit: limit || 20 });
  } catch (e) {
    return { ok: false, items: [], error: String(e) };
  }
});

ipcMain.handle("desktop:outbound-ack", async (_e, { id, ok, error }) => {
  try {
    return await backendPost("/api/desktop/outbound/ack", { id, ok: ok !== false, error: error || "" });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

// ── 受控出站「拟人节奏 + 诚实回执」（outbound-pace.js 是唯一策略源）─────────────────
// 节奏 pacer 常驻主进程：状态跨 renderer 重载存活（重载后不会突然爆发连发），
// 且每账号只有一个权威。renderer 只报事实（这条要发多长的文本 / 注入回执说发没发出去），
// 判断留在这里——与注入健康「前端报事实、后端分类」同一套哲学。
const { createPacerRegistry, ackDecision } = require("./outbound-pace.js");
const _pacers = createPacerRegistry({});

ipcMain.handle("desktop:pace-plan", async (_e, { account_id, text }) => {
  try {
    return _pacers.for(account_id).plan({ text, now: Date.now() });
  } catch (e) {
    // 策略层出问题绝不能卡住回复：退化成「不等待直接发」（旧行为）
    return { typingMs: 0, waitMs: 0, throttled: false, reason: "pace_error" };
  }
});

// 注入回执 → 该不该 ack、ack 成还是败。三档语义见 outbound-pace.ackDecision 的注释；
// 「不 ack」是刻意的：命令留 claimed，服务端 180s 后自动回收重取，比谎报成功或标死终态都好。
ipcMain.handle("desktop:outbound-report", async (_e, { id, account_id, result }) => {
  const d = ackDecision(result);
  if (d.ok) {
    try { _pacers.for(account_id).noteSent(Date.now()); } catch (e) { /* 记账失败不影响回执 */ }
  }
  if (!d.ack) return { ok: true, acked: false, decision: d };
  try {
    await backendPost("/api/desktop/outbound/ack", { id, ok: d.ok, error: d.error });
    return { ok: true, acked: true, decision: d };
  } catch (e) {
    // ack 发不出去也不要紧：服务端回收机制会把它当未完成重取（重复发的风险由
    // 注入侧「composer 已清空」回读把住，见 core.js::sendAndConfirm）
    return { ok: false, acked: false, decision: d, error: String(e) };
  }
});

// 受控出站「人审介入」（P2）：拦截/暂停/放行/改写/重试某条命令。
ipcMain.handle("desktop:outbound-action", async (_e, { id, ids, action, text, reason, ai_suggestion, source }) => {
  try {
    return await backendPost("/api/desktop/outbound/action", {
      id, ids, action, text: text || "", reason: reason || "",
      ai_suggestion: ai_suggestion || "", source: source || "",
    });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

// AI 重写助手（P4.1）：给一条命令生成更好的候选回复（不落库，供人审采纳）。
ipcMain.handle("desktop:outbound-rewrite", async (_e, { id }) => {
  try {
    return await backendPost("/api/desktop/outbound/rewrite", { id });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

// 纠正样本导出（P5）：拉 JSONL（偏好对）→ 保存对话框写文件，供离线 fine-tune/eval。
ipcMain.handle("desktop:export-corrections", async (_e, opts) => {
  try {
    const { base_url, token } = config.backend || {};
    if (!base_url) return { ok: false, error: "no base_url" };
    const q = new URLSearchParams({ format: "jsonl", limit: "5000" });
    if (opts && opts.source) q.set("source", opts.source);
    if (opts && opts.kind) q.set("kind", opts.kind);
    const r = await fetch(`${base_url}/api/desktop/outbound/corrections?${q.toString()}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const text = await r.text();
    const count = text ? text.split("\n").filter(Boolean).length : 0;
    if (!count) return { ok: false, error: "无样本可导出" };
    const win = BrowserWindow.getFocusedWindow();
    const def = `desktop_corrections_${new Date().toISOString().slice(0, 10)}.jsonl`;
    const res = await dialog.showSaveDialog(win, {
      defaultPath: def,
      filters: [{ name: "JSONL", extensions: ["jsonl"] }],
    });
    if (res.canceled || !res.filePath) return { ok: false, canceled: true };
    fs.writeFileSync(res.filePath, text, "utf-8");
    return { ok: true, path: res.filePath, count };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
});

ipcMain.handle("desktop:translate", async (_e, { text, target_lang }) => {
  try {
    return { ok: true, text: await backendTranslate(String(text || ""), target_lang) };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:translate-batch", async (_e, { texts, target_lang }) => {
  try {
    return { ok: true, texts: await backendTranslateBatch(texts || [], target_lang) };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:translate-media", async (_e, { kind, b64, target_lang }) => {
  try {
    return await backendTranslateMedia(kind === "image" ? "image" : "voice", String(b64 || ""), target_lang);
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:smart-reply", async (_e, payload) => {
  try {
    return await backendSmartReply(payload || {});
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:ingest", async (_e, payload) => {
  try {
    return await backendIngest(payload || {});
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

/** 通用 DELETE（Bearer）。 */
async function backendDelete(pathname, query) {
  const { base_url, token } = config.backend || {};
  const qs = query
    ? "?" + new URLSearchParams(Object.entries(query).filter(([, v]) => v != null && v !== "")).toString()
    : "";
  const r = await fetch(`${base_url}${pathname}${qs}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${token}` },
  });
  try { return await r.json(); } catch (e) { return { ok: false, status: r.status }; }
}

// ── 语音克隆 / TTS / 发送（与统一收件箱同源 API）──────────────────────────────
ipcMain.handle("desktop:voice-profiles", async () => {
  try { return await backendGet("/api/voice/profiles"); }
  catch (e) { return { ok: false, error: String(e) }; }
});

// 音色状态条（P1 2026-08-05）：带会话上下文的实际解析快照（与 send-voice 同源）
ipcMain.handle("desktop:voice-effective-config", async (_e, args) => {
  try {
    const a = args || {};
    const q = new URLSearchParams();
    if (a.persona_id) q.set("persona_id", a.persona_id);
    if (a.chat_key) q.set("chat_key", a.chat_key);
    if (a.platform) q.set("platform", a.platform);
    if (a.account_id) q.set("account_id", a.account_id);
    return await backendGet(`/api/voice/effective-config?${q.toString()}`);
  } catch (e) { return { ok: false, error: String(e) }; }
});

ipcMain.handle("desktop:voice-tts", async (_e, { text, persona_id, chat_key, platform, account_id }) => {
  try {
    // 会话上下文透传（试听=发送 契约；旧渲染层不传=旧行为）
    const d = await backendPost("/api/voice/tts-test", {
      text, persona_id: persona_id || undefined,
      chat_key: chat_key || undefined,
      platform: platform || undefined,
      account_id: account_id || undefined,
    });
    if (d.audio_url) return { ...d, ok: d.ok !== false };
    if (!d.filename) return d;
    const { base_url, token } = config.backend || {};
    const r = await fetch(
      `${base_url}/api/voice/tts-file/${encodeURIComponent(d.filename)}`,
      { headers: { Authorization: `Bearer ${token}` } });
    if (!r.ok) return { ok: false, message: `音频拉取失败 ${r.status}` };
    const b64 = Buffer.from(await r.arrayBuffer()).toString("base64");
    const mt = String(d.format || "mp3").includes("ogg") ? "audio/ogg" : "audio/mpeg";
    return { ...d, ok: true, dataUrl: `data:${mt};base64,${b64}` };
  } catch (e) { return { ok: false, error: String(e) }; }
});

ipcMain.handle("desktop:send-voice", async (_e, body) => {
  try { return await backendPost("/api/unified-inbox/send-voice", body || {}); }
  catch (e) { return { ok: false, error: String(e) }; }
});

ipcMain.handle("desktop:voice-reconcile", async () => {
  try { return await backendGet("/api/voice/reconcile"); }
  catch (e) { return { ok: false, error: String(e) }; }
});

ipcMain.handle("desktop:voice-purge", async (_e, body) => {
  try { return await backendPost("/api/voice/purge", body || {}); }
  catch (e) { return { ok: false, error: String(e) }; }
});

ipcMain.handle("desktop:voice-purge-orphans", async () => {
  try { return await backendPost("/api/voice/purge-orphans", {}); }
  catch (e) { return { ok: false, error: String(e) }; }
});

ipcMain.handle("desktop:voice-unbind", async (_e, { persona_id, purge_cloud }) => {
  try {
    const q = purge_cloud ? { purge_cloud: "1" } : {};
    return await backendDelete(`/api/voice/profiles/${encodeURIComponent(persona_id || "")}`, q);
  } catch (e) { return { ok: false, error: String(e) }; }
});

ipcMain.handle("desktop:voice-rebind", async (_e, body) => {
  try { return await backendPost("/api/voice/rebind", body || {}); }
  catch (e) { return { ok: false, error: String(e) }; }
});

ipcMain.handle("desktop:voice-enroll", async (_e, payload) => {
  try {
    const p = payload || {};
    const { base_url, token } = config.backend || {};
    const buf = Buffer.from(String(p.audio_b64 || ""), "base64");
    if (!buf.length) return { ok: false, message: "空音频" };
    const fd = new FormData();
    fd.append("file", new Blob([buf]), String(p.filename || "voice.wav"));
    fd.append("persona_id", String(p.persona_id || ""));
    fd.append("preferred_name", String(p.preferred_name || ""));
    fd.append("language_type", String(p.language_type || "Japanese"));
    if (p.reference_text) fd.append("reference_text", String(p.reference_text));
    const r = await fetch(`${base_url}/api/voice/enroll`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token}` },
      body: fd,
    });
    return await r.json();
  } catch (e) { return { ok: false, error: String(e) }; }
});

// ── P2 业务右栏：复用后端统一收件箱 API ──────────────────────────────────────
ipcMain.handle("desktop:profile", async (_e, { platform, account_id, chat_key }) => {
  try {
    return await backendGet("/api/unified-inbox/profile", { platform, account_id, chat_key });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:kb-search", async (_e, { q, platform, intent }) => {
  try {
    return await backendGet("/api/unified-inbox/kb-search", { q, platform, intent, limit: 6 });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:templates", async () => {
  try {
    return await backendGet("/api/unified-inbox/templates");
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:personas", async () => {
  try {
    return await backendGet("/api/personas/profiles");
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

/** 通用后端 POST（主进程发起，规避 CSP）。 */
async function backendPost(pathname, body) {
  const { base_url, token } = config.backend || {};
  const r = await fetch(`${base_url}${pathname}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify(body || {}),
  });
  // 错误形状与 web 客户端对齐（2026-07-31）：非 2xx 归一化补 ok/status/error，
  // 组件的失败分型（401 过期/403 权限/404 已删除…）在桌面 IPC 链路同样可用。
  let d = null;
  try { d = await r.json(); } catch (_e) { d = null; }
  if (d === null || typeof d !== "object") {
    return { ok: false, status: r.status, code: r.ok ? "badjson" : "",
             error: r.ok ? "invalid JSON response" : `HTTP ${r.status}` };
  }
  if (!r.ok) {
    if (d.ok === undefined) d.ok = false;
    if (d.status === undefined) d.status = r.status;
    if (!d.error && d.detail) d.error = String(d.detail);
  }
  return d;
}

ipcMain.handle("desktop:persona-bindings", async () => {
  try {
    return await backendGet("/api/persona/bindings");
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:persona-bind", async (_e, { chat_id, persona }) => {
  try {
    return await backendPost("/api/persona/bind", { chat_id, persona });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:guard-check", async (_e, { text }) => {
  try {
    return await backendPost("/api/desktop/guard-check", { text });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:persona-unbind", async (_e, { chat_id }) => {
  try {
    return await backendPost("/api/persona/unbind", { chat_id });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

// 会话级人设覆写（2026-07-26 方案 A）:读生效全景 / 换绑 / 解除 / 账号级整号换绑。
// 壳只做薄代理——语义(开关闸/权限/审计)全在后端;后端旧版本时前端组件自动降级 legacy UI。
ipcMain.handle("desktop:persona-effective", async (_e, args) => {
  try {
    const a = args || {};
    return await backendGet("/api/persona/effective", {
      conversation_id: a.conversation_id || "",
      platform: a.platform || "", account_id: a.account_id || "",
      chat_key: a.chat_key || "",
    });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:persona-bind-conv", async (_e, { conversation_id, profile_id }) => {
  try {
    return await backendPost("/api/persona/bind",
      { scope: "conversation", conversation_id, profile_id });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:persona-unbind-conv", async (_e, { conversation_id }) => {
  try {
    return await backendPost("/api/persona/unbind",
      { scope: "conversation", conversation_id });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:persona-account-set", async (_e, { platform, account_id, profile_id }) => {
  try {
    return await backendPost("/api/persona/account-persona",
      { platform, account_id, profile_id: profile_id || "" });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:thread", async (_e, { platform, account_id, chat_key }) => {
  try {
    return await backendGet("/api/unified-inbox/thread", { platform, account_id, chat_key, limit: 100 });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

// P1 共享组件:关系阶段(conv 级端点,conversation_id 由 renderer 本地拼)
ipcMain.handle("desktop:rel-stage", async (_e, { conversation_id }) => {
  try {
    const cid = String(conversation_id || "");
    if (!cid) return { ok: false, error: "missing conversation_id" };
    return await backendGet(`/api/workspace/conv/${encodeURIComponent(cid)}/relationship-stage`);
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:rel-confirm", async (_e, { conversation_id }) => {
  try {
    return await backendPost(`/api/workspace/conv/${encodeURIComponent(String(conversation_id || ""))}/relationship-stage/confirm`, {});
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:rel-downgrade", async (_e, { conversation_id, reason }) => {
  try {
    return await backendPost(`/api/workspace/conv/${encodeURIComponent(String(conversation_id || ""))}/relationship-stage/downgrade`, { reason: reason || "" });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:rel-reunion", async (_e, { conversation_id }) => {
  try {
    return await backendPost(`/api/workspace/conv/${encodeURIComponent(String(conversation_id || ""))}/relationship-stage/reunion`, {});
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:rel-sync", async (_e, { contact_id, mode }) => {
  try {
    return await backendPost(`/api/workspace/contact/${encodeURIComponent(String(contact_id || ""))}/relationship-stage/sync`, { mode: mode || "to_contact" });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

// P2 共享组件:协作上下文 / 工作链执行(conv 级端点)
ipcMain.handle("desktop:collab-context", async (_e, { conversation_id }) => {
  try {
    const cid = String(conversation_id || "");
    if (!cid) return { ok: false, error: "missing conversation_id" };
    return await backendGet(`/api/workspace/conv/${encodeURIComponent(cid)}/collab-context`);
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:chain-executions", async (_e, { conversation_id, limit }) => {
  try {
    const cid = String(conversation_id || "");
    if (!cid) return { ok: false, error: "missing conversation_id" };
    return await backendGet(`/api/workspace/conv/${encodeURIComponent(cid)}/chain-executions`, { limit: limit || 8 });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:chain-cancel", async (_e, { exec_id }) => {
  try {
    return await backendPost(`/api/workspace/chain-executions/${encodeURIComponent(String(exec_id || ""))}/cancel`, {});
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

// Phase 2 账号管理:统一清单 + 扫码登录 + 编排器启停（与 web 后台共用后端接口）
ipcMain.handle("desktop:accounts-list", async () => {
  try {
    return await backendGet("/api/accounts");
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:platform-modes", async (_e, { platform }) => {
  try {
    return await backendGet(`/api/platforms/${encodeURIComponent(String(platform || ""))}/modes`);
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:login-start", async (_e, args) => {
  try {
    const a = args || {};
    return await backendPost(`/api/platforms/${encodeURIComponent(String(a.platform || ""))}/login/start`, {
      mode: a.mode || "",
      account_id: a.account_id || "",
      label: a.label || "",
      proxy_id: a.proxy_id || "",
      use_fingerprint: !!a.use_fingerprint,
    });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:login-status", async (_e, { platform, login_id }) => {
  try {
    return await backendGet(
      `/api/platforms/${encodeURIComponent(String(platform || ""))}/login/${encodeURIComponent(String(login_id || ""))}/status`
    );
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:login-cancel", async (_e, { platform, login_id }) => {
  try {
    return await backendPost(
      `/api/platforms/${encodeURIComponent(String(platform || ""))}/login/${encodeURIComponent(String(login_id || ""))}/cancel`, {}
    );
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:account-start", async (_e, { platform, account_id }) => {
  try {
    return await backendPost(
      `/api/accounts/${encodeURIComponent(String(platform || ""))}/${encodeURIComponent(String(account_id || ""))}/start`, {}
    );
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:account-stop", async (_e, { platform, account_id }) => {
  try {
    return await backendPost(
      `/api/accounts/${encodeURIComponent(String(platform || ""))}/${encodeURIComponent(String(account_id || ""))}/stop`, {}
    );
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:account-auto-reply", async (_e, { platform, account_id, enabled }) => {
  try {
    return await backendPost(
      `/api/accounts/${encodeURIComponent(String(platform || ""))}/${encodeURIComponent(String(account_id || ""))}/auto-reply`,
      { enabled: !!enabled }
    );
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:account-auto-reply-override", async (_e, { platform, account_id, override }) => {
  try {
    return await backendPost(
      `/api/accounts/${encodeURIComponent(String(platform || ""))}/${encodeURIComponent(String(account_id || ""))}/auto-reply/override`,
      override || {}
    );
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:auto-reply-audit", async (_e, args) => {
  try {
    const { limit, platform, account_id, since } = args || {};
    return await backendGet("/api/accounts/auto-reply/audit", { limit, platform, account_id, since });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:auto-reply-config-get", async () => {
  try {
    return await backendGet("/api/accounts/auto-reply/config");
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:auto-reply-health", async () => {
  try {
    return await backendGet("/api/accounts/auto-reply/health");
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:auto-reply-webhooks-get", async () => {
  try {
    return await backendGet("/api/accounts/auto-reply/webhooks");
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:alert-catalog", async () => {
  try {
    return await backendGet("/api/accounts/auto-reply/alert-catalog");
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:auto-reply-webhooks-set", async (_e, list) => {
  try {
    return await backendPost("/api/accounts/auto-reply/webhooks", { webhooks: list || [] });
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:auto-reply-webhooks-test", async (_e, payload) => {
  try {
    return await backendPost("/api/accounts/auto-reply/webhooks/test", payload || {});
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:auto-reply-config-set", async (_e, args) => {
  try {
    return await backendPost("/api/accounts/auto-reply/config", args || {});
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

ipcMain.handle("desktop:analyze", async (_e, { messages, chat }) => {
  try {
    const { base_url, token } = config.backend || {};
    const r = await fetch(`${base_url}/api/unified-inbox/analyze`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
      body: JSON.stringify({ messages: Array.isArray(messages) ? messages : [], chat: chat || {} }),
    });
    return await r.json();
  } catch (e) {
    return { ok: false, error: String(e) };
  }
});

/** 给某账号的 session 分区配置代理（防关联，可选）。 */
async function applyProxyForAccount(acc) {
  if (!acc || !acc.proxy) return;
  try {
    const part = `persist:${acc.id}`;
    await session.fromPartition(part).setProxy({ proxyRules: acc.proxy });
  } catch (e) {
    // 代理配置失败不阻断启动
  }
}

/** WhatsApp/Instagram/Messenger/X/Zalo 等拒载含 Electron 的 UA；在 partition 级伪装 Chrome
 *  （须在首次导航前）。telegram 用默认 UA 已验证可用 → 跳过，保持零回归。 */
async function applyWhatsappSessionUa(acc) {
  if (!acc || !needsChromeUa(acc.platform)) return;
  try {
    await session.fromPartition(`persist:${acc.id}`).setUserAgent(
      chromeLikeUserAgent(process.versions.chrome));
  } catch (e) {
    // UA 设置失败不阻断启动
  }
}

function bindWhatsappWebviewUa(wc) {
  const waUa = chromeLikeUserAgent(process.versions.chrome);
  function maybeSet(url) {
    if (urlNeedsChromeUa(url)) {
      try { wc.setUserAgent(waUa); } catch (_) { /* ignore */ }
    }
  }
  maybeSet(wc.getURL());
  wc.on("did-start-navigation", (_e, url) => maybeSet(url));
  wc.on("will-navigate", (_e, url) => maybeSet(url));
}

ipcMain.handle("desktop:set-title", (_e, title) => {
  const w = BrowserWindow.getFocusedWindow() || BrowserWindow.getAllWindows()[0];
  if (w && title) {
    try { w.setTitle(String(title)); } catch (e) { /* ignore */ }
  }
  return { ok: true };
});

// 收件箱 webview 分区。target=_blank 默认弹窗走壳的 default session，没有这里的
// 登录 cookie → 管理台 HTML 被 _api_auth 打成 {"detail":"Unauthorized"}（Chromium
// JSON 预览，菜单栏仍是本应用）。弹窗必须复用这个分区才能带上已登录态。
const BACKEND_WORKSPACE_PARTITION = "persist:backend-workspace";

function backendOrigin() {
  try {
    return new URL((config.backend || {}).base_url || "http://127.0.0.1:18799").origin;
  } catch (e) {
    return "http://127.0.0.1:18799";
  }
}

function isBackendUrl(url) {
  try {
    const u = new URL(String(url || ""));
    return (u.protocol === "http:" || u.protocol === "https:") && u.origin === backendOrigin();
  } catch (e) {
    return false;
  }
}

function bindBackendPopupLogin(win, intendedUrl) {
  const token = String(((config.backend || {}).token) || "");
  if (!token) return;
  let attempted = false;
  win.webContents.on("did-finish-load", () => {
    if (attempted) return;
    let u;
    try { u = new URL(win.webContents.getURL()); } catch (e) { return; }
    if (u.pathname !== "/login" && u.pathname !== "/login/") return;
    attempted = true;
    let next = "/";
    try {
      const dest = new URL(intendedUrl);
      next = dest.pathname + dest.search || "/";
    } catch (e) { /* keep / */ }
    if (!String(next).startsWith("/")) next = "/";
    const js =
      "(function(){try{" +
      "var n=" + JSON.stringify(next) + ";" +
      "fetch('/login',{method:'POST'," +
      "headers:{'Content-Type':'application/x-www-form-urlencoded'}," +
      "body:'auth_token='+encodeURIComponent(" + JSON.stringify(token) + ")," +
      "credentials:'same-origin'})" +
      ".then(function(){location.replace(n);})" +
      ".catch(function(){location.replace(n);});" +
      "}catch(e){}})();";
    win.webContents.executeJavaScript(js).catch(() => {});
  });
}

// ── 后台弹窗窗口唯一性（2026-08-14，修「后台管理/坐席工作台越点越多」回归）─────────
// 壳内 window.open 没有浏览器的命名窗口寻址语义：_win_unique.html 的 Electron 分支只兜
// 「/workspace 的 BC 交接」，admin 入口与探活 miss 的 workspace 都落到这里——旧实现每次
// new BrowserWindow ＝ 点一次多一个原生窗。改为按槽位去重复用（与 _win_unique 三槽同构）：
//   workspace（精确 /workspace 坐席收件箱）/ wsub（/workspace/* 子页）/ admin（后台壳其余页）
// 已有窗口 → 聚焦复用；深链(?/#)或换页才导航；admin 槽的裸 "/" 泛入口只聚焦不重载
// （后台多为列表/编辑页，硬拉回首页会丢编辑现场——与 _win_unique 浏览器侧同语义）。
const backendPopupWins = new Map(); // slot → BrowserWindow

function backendPopupSlot(url) {
  let p = "/";
  try { p = (new URL(String(url)).pathname || "/").replace(/\/+$/, "") || "/"; } catch (e) { /* keep "/" */ }
  if (p === "/workspace") return "workspace";
  if (p.indexOf("/workspace/") === 0) return "wsub";
  return "admin";
}

function reuseBackendPopup(slot, url) {
  const win = backendPopupWins.get(slot);
  if (!win || win.isDestroyed()) return null;
  try {
    const u = new URL(String(url));
    const wantPath = (u.pathname || "/").replace(/\/+$/, "") || "/";
    const deep = !!(u.search || u.hash);
    let curPath = "";
    try { curPath = (new URL(win.webContents.getURL()).pathname || "/").replace(/\/+$/, "") || "/"; } catch (e) { curPath = ""; }
    const genericAdminHome = slot === "admin" && wantPath === "/" && !deep;
    if (!genericAdminHome && (deep || curPath !== wantPath)) win.loadURL(url);
    if (win.isMinimized()) win.restore();
    win.focus();
    return win;
  } catch (e) {
    return null; // 复用失败回落新开，绝不吞点击
  }
}

function openBackendPopup(url) {
  const slot = backendPopupSlot(url);
  const reused = reuseBackendPopup(slot, url);
  if (reused) return reused;
  const child = new BrowserWindow({
    width: 1100,
    height: 800,
    title: "智聊",
    webPreferences: {
      partition: BACKEND_WORKSPACE_PARTITION,
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: false,
    },
  });
  try {
    if (fs.existsSync(DEFAULT_BRAND_ICON)) child.setIcon(DEFAULT_BRAND_ICON);
  } catch (e) { /* 图标缺失不阻断 */ }
  backendPopupWins.set(slot, child);
  child.on("closed", () => {
    if (backendPopupWins.get(slot) === child) backendPopupWins.delete(slot);
  });
  // 弹窗里再点后台 target=_blank 链接（如后台侧栏「坐席工作台」）同走本唯一性收敛
  child.webContents.setWindowOpenHandler(makeBackendPopupHandler());
  wireEditContextMenu(child.webContents);   // 后台弹窗（admin/workspace 子页）同享右键编辑菜单
  bindBackendPopupLogin(child, url);
  child.loadURL(url);
  return child;
}

function makeBackendPopupHandler() {
  return ({ url }) => {
    if (!isBackendUrl(url)) return { action: "allow" };
    setImmediate(() => {
      try { openBackendPopup(url); } catch (e) {
        console.log("[popup] open failed: " + ((e && e.message) || e));
      }
    });
    return { action: "deny" };
  };
}

// ── 系统右键菜单（composer 批 2026-08-17）────────────────────────────────────
// Electron 默认不弹任何右键菜单：坐席在聊天输入框右键「粘贴」一直没反应（浏览器端
// 访问同页不受影响）。给壳内全部 webContents（主窗 chrome / 官方页与工作台 webview /
// 后台弹窗）统一挂原生编辑菜单：可编辑区=剪贴板全套（粘贴走标准 DOM paste 事件 →
// 工作台既有「粘贴截图暂存媒体」链零改动直通）；选中文本=复制；链接=复制链接；
// 图片=复制图片。动作用显式 webContents 方法而非 role（role 依赖焦点窗语义，
// webview 场景会打错目标）；按 editFlags 置灰，绝不出现「点了没反应」的死项。
function wireEditContextMenu(wc) {
  if (!wc || wc.__cxCtxMenuWired) return;
  wc.__cxCtxMenuWired = true;
  wc.on("context-menu", (_e, params) => {
    try {
      const p = params || {};
      const ef = p.editFlags || {};
      const items = [];
      if (p.isEditable) {
        let clipHasImage = false;
        try { clipHasImage = clipboard.availableFormats().some((f) => String(f).indexOf("image/") === 0); } catch (err) { /* ignore */ }
        items.push(
          { label: "撤销", enabled: !!ef.canUndo, click: () => wc.undo() },
          { label: "重做", enabled: !!ef.canRedo, click: () => wc.redo() },
          { type: "separator" },
          { label: "剪切", enabled: !!ef.canCut, click: () => wc.cut() },
          { label: "复制", enabled: !!ef.canCopy, click: () => wc.copy() },
          { label: clipHasImage ? "粘贴图片" : "粘贴", enabled: !!ef.canPaste, click: () => wc.paste() },
          { label: "粘贴为纯文本", enabled: !!ef.canPaste, click: () => wc.pasteAndMatchStyle() },
          { type: "separator" },
          { label: "全选", enabled: !!ef.canSelectAll, click: () => wc.selectAll() },
        );
      } else if (p.selectionText && p.selectionText.trim()) {
        items.push({ label: "复制", click: () => wc.copy() });
      }
      if (p.linkURL) {
        if (items.length) items.push({ type: "separator" });
        items.push({ label: "复制链接地址", click: () => clipboard.writeText(p.linkURL) });
      }
      if (p.mediaType === "image" && p.srcURL) {
        if (items.length) items.push({ type: "separator" });
        items.push({ label: "复制图片", click: () => { try { wc.copyImageAt(p.x, p.y); } catch (err) { /* ignore */ } } });
      }
      if (!items.length) return; // 空白处右键保持无菜单（与主流聊天软件一致）
      Menu.buildFromTemplate(items).popup();
    } catch (err) {
      console.log("[ctxmenu] popup failed: " + ((err && err.message) || err));
    }
  });
  // 探测位：工作台页面据此区分「新壳（原生菜单已接管）/ 旧壳（出一次性 Ctrl+V 指路）」；
  // 官方页/后台页不消费该全局，注入无副作用。页面 preventDefault 的自绘菜单区
  // （消息行/会话行）不会触发本事件，双菜单无叠加面。
  wc.on("dom-ready", () => {
    wc.executeJavaScript("try{window.__cxNativeCtxMenu=1}catch(e){};0").catch(() => {});
  });
}

async function createWindow() {
  for (const acc of config.accounts || []) {
    await applyProxyForAccount(acc);
    await applyWhatsappSessionUa(acc);
  }

  // 原生窗口/任务栏图标兜底：内嵌 webview 报告的 page-favicon 不会传染给外层窗口，
  // 故显式用 copy-shared 落地的品牌 mark，保证桌面壳始终是无界图标（缺文件则回落 Electron 默认）。
  const winOpts = {
    width: 1280,
    height: 820,
    title: "智聊 · 桌面工作台",
    webPreferences: {
      preload: path.join(__dirname, "shell-preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      webviewTag: true,
    },
  };
  try {
    if (fs.existsSync(DEFAULT_BRAND_ICON)) winOpts.icon = DEFAULT_BRAND_ICON;
  } catch (e) { /* 图标缺失不阻断启动 */ }
  const win = new BrowserWindow(winOpts);

  win.webContents.on("did-finish-load", () => {
    console.log("[diag] renderer loaded ok");
    applyLiveWindowBranding(win, { force: true });
  });
  // 运行中改了 logo 无需重开窗口：切回桌面壳时（节流后）重取品牌热替换图标。
  win.on("focus", () => applyLiveWindowBranding(win));
  win.webContents.on("did-fail-load", (_e, code, desc) =>
    console.log(`[diag] renderer load FAILED ${code} ${desc}`));
  win.webContents.on("render-process-gone", (_e, d) =>
    console.log(`[diag] render process gone: ${JSON.stringify(d)}`));
  win.webContents.on("console-message", (_e, _lvl, msg) =>
    console.log(`[renderer] ${msg}`));
  // webview 子 webContents 在 Electron 默认是 sandboxed（与父窗口 sandbox:false 无关），
  // 沙箱内 preload 只能 require electron，无法 require 本地模块（./profiles.js / ./media-format.js）→
  // 注入脚本 tg-inject.js 整体加载失败「module not found: ./profiles.js」。这里对内嵌 webview 关闭
  // 沙箱，使 preload 能加载选择器档案/媒体格式化模块（DOM 注入与 ipcRenderer 不受影响）。
  win.webContents.on("will-attach-webview", (_e, webPreferences) => {
    webPreferences.sandbox = false;
  });
  win.webContents.setWindowOpenHandler(makeBackendPopupHandler());
  wireEditContextMenu(win.webContents);   // 主窗 chrome（首跑向导等原生输入件）
  win.webContents.on("did-attach-webview", (_e, wc) => {
    console.log("[diag] webview attached");
    bindWhatsappWebviewUa(wc);
    wireEditContextMenu(wc);   // 官方页 + 工作台 webview：右键粘贴的主战场
    wc.setWindowOpenHandler(makeBackendPopupHandler());
    wc.on("did-finish-load", () => console.log("[diag] webview page loaded"));
    wc.on("did-fail-load", (_e2, code, desc) =>
      console.log(`[diag] webview load FAILED ${code} ${desc}`));
    wc.on("console-message", (_e2, _lvl, msg, line, sourceId) =>
      console.log(`[webview] ${msg}${sourceId ? ` (${sourceId}:${line})` : ""}`));
  });

  console.log(`[diag] platforms enabled: ${(config.platforms || []).filter((p) => p.enabled).map((p) => p.id).join(",")}`);
  win.loadFile(path.join(__dirname, "renderer", "index.html"));
  if (process.argv.includes("--dev")) win.webContents.openDevTools({ mode: "detach" });
}

// 中文应用菜单(替换默认英文菜单;保留 role 以维持快捷键与原生行为)
function buildChineseMenu() {
  const template = [
    {
      label: "文件",
      submenu: [
        { label: "重新加载", role: "reload" },
        { label: "强制重新加载", role: "forceReload" },
        { type: "separator" },
        { label: "退出", role: "quit" },
      ],
    },
    {
      label: "编辑",
      submenu: [
        { label: "撤销", role: "undo" },
        { label: "重做", role: "redo" },
        { type: "separator" },
        { label: "剪切", role: "cut" },
        { label: "复制", role: "copy" },
        { label: "粘贴", role: "paste" },
        { label: "全选", role: "selectAll" },
      ],
    },
    {
      label: "视图",
      submenu: [
        { label: "实际大小", role: "resetZoom" },
        { label: "放大", role: "zoomIn" },
        { label: "缩小", role: "zoomOut" },
        { type: "separator" },
        { label: "全屏", role: "togglefullscreen" },
        { label: "开发者工具", role: "toggleDevTools" },
      ],
    },
    {
      label: "窗口",
      submenu: [
        { label: "最小化", role: "minimize" },
        { label: "关闭窗口", role: "close" },
      ],
    },
    {
      label: "帮助",
      submenu: [
        {
          label: "关于",
          click: async () => {
            const w = BrowserWindow.getFocusedWindow() || BrowserWindow.getAllWindows()[0];
            const bi = await resolveBrand();
            const ver = displayVersion();
            const detail =
              `Telegram / WhatsApp / Messenger / LINE · 人工操作台 + 业务助手\n\n` +
              `版本 v${ver}  ·  Electron ${process.versions.electron}  ·  Chromium ${process.versions.chrome}\n` +
              `${bi.company} · ${bi.website}`;
            const res = await dialog.showMessageBox(w, {
              type: "info",
              title: `关于 ${bi.product}`,
              message: `${bi.product} · 桌面工作台`,
              detail,
              buttons: ["访问官网", "关闭"],
              defaultId: 1,
              cancelId: 1,
              noLink: true,
            });
            if (res.response === 0 && bi.website) {
              shell.openExternal(bi.website).catch(() => {});
            }
          },
        },
        {
          label: "检查更新",
          click: () => checkForUpdatesManual(
            BrowserWindow.getFocusedWindow() || BrowserWindow.getAllWindows()[0]),
        },
      ],
    },
  ];
  return Menu.buildFromTemplate(template);
}

// ── 更新与公告通知中心（P0 2026-08-14）─────────────────────────────────────
// 决策纯函数在 update-notify.js（Node 直跑可测）；这里只做 IO：updater 事件、
// 公告 HTTP 拉取（含本地缓存）、已读/稍后状态落盘、向 renderer 广播当前通知。
// 旧行为（更新下载完只写日志、下次重启才生效）升级为：横幅 +「立即重启更新」一键完成。
const updateNotify = require("./update-notify.js");

function _noticeStatePath() { return path.join(app.getPath("userData"), "shell-notices.json"); }
function _annCachePath() { return path.join(app.getPath("userData"), "announcements-cache.json"); }
function _loadJsonSoft(p, fallback) {
  try { return JSON.parse(fs.readFileSync(p, "utf-8")); } catch (e) { return fallback; }
}

let _noticeState = null; // { read_ids: [], update_snoozed_until: ms }
function _getNoticeState() {
  if (!_noticeState) {
    const raw = _loadJsonSoft(_noticeStatePath(), {}) || {};
    _noticeState = {
      read_ids: Array.isArray(raw.read_ids) ? raw.read_ids.map(String) : [],
      update_snoozed_until: Number(raw.update_snoozed_until) || 0,
    };
  }
  return _noticeState;
}
function _saveNoticeState() {
  try {
    fs.mkdirSync(path.dirname(_noticeStatePath()), { recursive: true });
    fs.writeFileSync(_noticeStatePath(), JSON.stringify(_getNoticeState(), null, 2), "utf-8");
  } catch (e) { /* 已读状态丢失最多重看一次横幅，不阻断 */ }
}

let _updateInfo = { phase: "idle", version: "", percent: 0 };
let _feed = null; // 懒加载：首次访问读本地缓存（离线也有上次内容）；{items, minSupportedVersion}

function _getFeed() {
  if (_feed === null) {
    _feed = updateNotify.normalizeFeed(_loadJsonSoft(_annCachePath(), null));
  }
  return _feed;
}
function _getAnnouncements() { return _getFeed().items; }

function currentShellNotice() {
  const st = _getNoticeState();
  const feed = _getFeed();
  return updateNotify.pickNotice({
    update: _updateInfo,
    announcements: feed.items,
    // 强制升级线只对打包态生效：dev 跑源码没有 updater，横幅催了也无路可走
    minSupportedVersion: app.isPackaged ? feed.minSupportedVersion : "",
    appVersion: app.getVersion(),
    readIds: st.read_ids,
    snoozedUntil: st.update_snoozed_until,
    now: Date.now(),
  });
}
function broadcastShellNotice() {
  const n = currentShellNotice();
  for (const w of BrowserWindow.getAllWindows()) {
    try { w.webContents.send("desktop:shell-notice", n); } catch (e) { /* 窗口正在销毁等，忽略 */ }
  }
}

/** 公告源：更新源同域（publish url + announcements.json），config.updates.announcements_url 可覆写。 */
function _announcementsUrl() {
  try {
    const cfgUrl = ((config || {}).updates || {}).announcements_url;
    if (cfgUrl) return String(cfgUrl);
  } catch (e) { /* config 未就绪按默认 */ }
  try {
    const pub = require("./package.json").build.publish[0].url;
    return pub.replace(/\/+$/, "") + "/announcements.json";
  } catch (e) { return ""; }
}

async function refreshAnnouncements() {
  const url = _announcementsUrl();
  if (!url) return;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 10000);
  try {
    const r = await fetch(url, { signal: ctl.signal, cache: "no-store" });
    if (!r.ok) return;
    _feed = updateNotify.normalizeFeed(await r.json());
    try {
      fs.writeFileSync(_annCachePath(), JSON.stringify({
        items: _feed.items, min_supported_version: _feed.minSupportedVersion,
      }, null, 2), "utf-8");
    } catch (e) { /* 缓存写失败不影响本次展示 */ }
    broadcastShellNotice();
    // 触线客户端别干等 4h 定时器：强制升级横幅出现的同时立刻查一轮更新，
    // 让「已停止支持 → 下载中 → 一键重启」尽快推进（runUpdateCheck 自带 busy 防重入）。
    if (app.isPackaged && _updateInfo.phase === "idle"
        && updateNotify.forcedUpgradeActive(app.getVersion(), _feed.minSupportedVersion)) {
      runUpdateCheck("min-supported");
    }
  } catch (e) {
    /* 公告拉取失败静默：非关键链路，下轮定时器/下次启动自然重试 */
  } finally { clearTimeout(timer); }
}

ipcMain.handle("desktop:shell-notice", () => currentShellNotice());
ipcMain.handle("desktop:notice-ack", (_e, args) => {
  const a = args || {};
  const st = _getNoticeState();
  if (a.action === "read" && a.id) {
    const id = String(a.id);
    if (!st.read_ids.includes(id)) st.read_ids.push(id);
    if (st.read_ids.length > 200) st.read_ids = st.read_ids.slice(-200); // 防无限增长
    _saveNoticeState();
  } else if (a.action === "snooze") {
    st.update_snoozed_until = updateNotify.nextSnooze(Date.now(), a.hours);
    _saveNoticeState();
  }
  return currentShellNotice(); // 回传新状态：renderer 原地渲染下一条（或隐藏）
});
ipcMain.handle("desktop:notice-open", async (_e, id) => {
  const item = _getAnnouncements().find((x) => x.id === String(id || ""));
  const link = (item && item.link) || "";
  if (!/^https:\/\//i.test(link)) return { ok: false }; // 只放行 https，公告数据不可执行本地任何东西
  try { await shell.openExternal(link); return { ok: true }; } catch (e) { return { ok: false }; }
});
ipcMain.handle("desktop:update-restart", () => {
  const u = _getUpdater();
  if (!u || _updateInfo.phase !== "downloaded") return { ok: false, error: "not_ready" };
  // isSilent=false 让 NSIS 走静默安装参数由 updater 默认处理；forceRunAfter=true 装完自动拉起。
  // setImmediate：先把 IPC 应答送回 renderer（按钮已进「正在重启…」态），再触发退出流程。
  setImmediate(() => {
    try { u.quitAndInstall(false, true); } catch (e) { console.log(`[updater] quitAndInstall: ${String((e && e.message) || e)}`); }
  });
  return { ok: true };
});

/** updater 单例：手动检查与后台自动检查共用同一实例与事件接线（旧实现两处各自
 *  require + 配置，事件只挂在自动链上——手动触发的下载完成横幅收不到）。
 *
 * ⚠ disableDifferentialDownload=true（2026-07-29 实机事故）：0.2.6→0.2.7 在测试机上
 * 差分下载**卡死在 0 字节**——blockmap（235KB）下来了，随后 temp-*.exe 建出来就再无进展，
 * 用户端表现为「一直不更新，问题还在」。整包 216MB 直下反而稳（LAN/公网都实测过）。
 * 差分省的那点流量，换不来「更新链路静默失效」的代价。
 */
let _updater;
function _getUpdater() {
  if (_updater !== undefined) return _updater;
  _updater = null;
  if (!app.isPackaged) return _updater;
  try {
    const { autoUpdater } = require("electron-updater");
    autoUpdater.autoDownload = true;
    autoUpdater.disableDifferentialDownload = true;
    autoUpdater.on("error", (e) => console.log(`[updater] ${String((e && e.message) || e)}`));
    autoUpdater.on("update-available", (info) => {
      _updateInfo = { phase: "downloading", version: String((info && info.version) || ""), percent: 0 };
      broadcastShellNotice();
    });
    autoUpdater.on("download-progress", (p) => {
      const pct = Math.round((p && p.percent) || 0);
      console.log(`[updater] 下载中 ${pct}%`);
      // 每 +10% 才广播一次：216MB 整包的 progress 事件很密，逐条推 IPC 纯属噪音
      if (_updateInfo.phase === "downloading" && pct >= (_updateInfo.percent || 0) + 10) {
        _updateInfo.percent = pct;
        broadcastShellNotice();
      }
    });
    autoUpdater.on("update-downloaded", (info) => {
      console.log("[updater] 更新已下载，横幅提示一键重启");
      _updateInfo = { phase: "downloaded", version: String((info && info.version) || _updateInfo.version), percent: 100 };
      broadcastShellNotice();
    });
    _updater = autoUpdater;
  } catch (e) {
    console.log(`[updater] 不可用：${String((e && e.message) || e)}`);
  }
  return _updater;
}

let _updCheckBusy = false;
async function runUpdateCheck(reason) {
  const u = _getUpdater();
  if (!u || _updCheckBusy) return null;
  _updCheckBusy = true;
  try {
    return await u.checkForUpdates();
  } catch (e) {
    console.log(`[updater] check(${reason}) failed: ${String((e && e.message) || e)}`);
    return null;
  } finally { _updCheckBusy = false; }
}

/** 手动「检查更新」：dev 说明不检查；已就绪直接给「立即重启更新」；否则真查一轮。 */
async function checkForUpdatesManual(win) {
  const ver = app.getVersion(); // semver：仅用于与更新源比对
  const shown = displayVersion(); // 展示串（内测 1.001）
  if (!app.isPackaged) {
    await dialog.showMessageBox(win, {
      type: "info", title: "检查更新",
      message: "当前为开发版", detail: `版本 v${shown}（开发模式不检查更新）`,
      buttons: ["好的"], noLink: true,
    });
    return;
  }
  const u = _getUpdater();
  if (!u) {
    await dialog.showMessageBox(win, {
      type: "error", title: "检查更新", message: "更新组件不可用",
      detail: "请从官网重新下载安装包。", buttons: ["好的"], noLink: true,
    });
    return;
  }
  if (_updateInfo.phase === "downloaded") {
    const res = await dialog.showMessageBox(win, {
      type: "info", title: "检查更新", message: `新版本 v${_updateInfo.version} 已就绪`,
      detail: "重启即完成更新（约 30 秒，不影响账号与聊天记录）。",
      buttons: ["立即重启更新", "稍后"], defaultId: 0, cancelId: 1, noLink: true,
    });
    if (res.response === 0) {
      setImmediate(() => { try { u.quitAndInstall(false, true); } catch (e) { /* 失败下次重启仍会装 */ } });
    }
    return;
  }
  try {
    const r = await runUpdateCheck("manual");
    const latest = r && r.updateInfo && r.updateInfo.version;
    if (latest && latest !== ver) {
      await dialog.showMessageBox(win, {
        type: "info", title: "检查更新", message: `发现新版本 v${latest}`,
        detail: "正在后台下载，完成后窗口顶部会出现「立即重启更新」提示，点一下即可完成。",
        buttons: ["好的"], noLink: true,
      });
    } else {
      await dialog.showMessageBox(win, {
        type: "info", title: "检查更新", message: "已是最新版本",
        detail: `当前 v${shown}`, buttons: ["好的"], noLink: true,
      });
    }
  } catch (e) {
    await dialog.showMessageBox(win, {
      type: "error", title: "检查更新", message: "检查更新失败",
      detail: String((e && e.message) || e), buttons: ["好的"], noLink: true,
    });
  }
}

/** 自动更新（仅发布态；dev 跳过。失败不阻断启动）。需 package.json::build.publish 指向真实更新源。
 *  节奏：启动即查 + 每 4h 定期复查 + 睡眠唤醒补查（旧实现只在启动查一次——
 *  桌面壳常驻数天不重启，坐席永远等不到「下次启动」）。 */
function setupAutoUpdate() {
  if (!app.isPackaged) return;
  runUpdateCheck("boot");
  const t = setInterval(() => runUpdateCheck("interval"), 4 * 60 * 60 * 1000);
  if (t.unref) t.unref();
  try {
    let lastResume = 0;
    powerMonitor.on("resume", () => {
      const now = Date.now();
      if (now - lastResume < 5 * 60 * 1000) return; // 连续唤醒去抖
      lastResume = now;
      setTimeout(() => runUpdateCheck("resume"), 15000); // 给网络恢复留缓冲
    });
  } catch (e) { /* powerMonitor 异常不阻断：定时器仍在 */ }
}

/** 公告轮询（dev 也跑：公告展示链路不依赖打包态，便于开发期直接验证）。 */
function setupAnnouncements() {
  refreshAnnouncements();
  const t = setInterval(refreshAnnouncements, 6 * 60 * 60 * 1000);
  if (t.unref) t.unref();
}

// ── 版本遥测心跳（P1 2026-08-14）───────────────────────────────────────────
// 复用官网既有 /api/telemetry 匿名回执端点（白名单 schema：未知字段服务端一律丢弃）：
// 每台安装每 24h 报一次 {kind:"chatx_heartbeat", manifest_version=当前版本, anon_id}。
// 运营价值＝「版本分布 / 多少台还卡旧版」有真读数——强制升级 min_supported_version
// 划线之前先看这张表，不再盲划。anon_id 是本机随机 uuid（userData 下落盘复用），
// 不含账号/主机名/路径任何可识别信息；config.updates.telemetry=false 一键全关。
function _telemetryUrl() {
  try {
    const cfg = (config || {}).updates || {};
    if (cfg.telemetry === false) return "";
    if (cfg.telemetry_url) return String(cfg.telemetry_url);
  } catch (e) { /* config 未就绪按默认 */ }
  try {
    const pub = require("./package.json").build.publish[0].url; // https://bd2026.cc/downloads
    return pub.replace(/\/downloads\/?$/, "") + "/api/telemetry";
  } catch (e) { return ""; }
}
function _anonId() {
  const p = path.join(app.getPath("userData"), "telemetry-id.txt");
  try {
    const v = fs.readFileSync(p, "utf-8").trim();
    if (/^[0-9a-f-]{16,64}$/i.test(v)) return v;
  } catch (e) { /* 首次运行无文件 */ }
  const id = require("crypto").randomUUID();
  try {
    fs.mkdirSync(path.dirname(p), { recursive: true });
    fs.writeFileSync(p, id, "utf-8");
  } catch (e) { /* 写失败=下次换新 id，只影响装机数去重精度 */ }
  return id;
}
async function sendVersionBeacon() {
  const url = _telemetryUrl();
  if (!url) return;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 10000);
  try {
    await fetch(url, {
      method: "POST",
      signal: ctl.signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        schema: 1,
        ts: new Date().toISOString(),
        kind: "chatx_heartbeat", // 服务端 kind 限长 16，本串 15
        manifest_version: app.getVersion(),
        channel: "stable",
        platform: `${process.platform}-${process.arch}`,
        anon_id: _anonId(),
      }),
    });
  } catch (e) {
    /* 遥测失败静默：观测链路绝不反噬主功能 */
  } finally { clearTimeout(timer); }
}
function setupVersionTelemetry() {
  if (!app.isPackaged) return; // dev 跑源码不上报，防开发机刷脏版本分布
  const t0 = setTimeout(sendVersionBeacon, 60 * 1000); // 启动 60s 后发：避开开机网络抖动
  if (t0.unref) t0.unref();
  const t = setInterval(sendVersionBeacon, 24 * 60 * 60 * 1000);
  if (t.unref) t.unref();
}

// 单实例锁（双实例竞态根治）：多开桌面壳会各自 backendManager.start() → 各自探活后
// 各自 spawn 后端，端口先到者赢、后到者绑定失败成僵尸实例（曾观测到双 python main.py）。
// 第二个实例直接退出，并把已有窗口唤到前台（符合「再次启动=聚焦既有窗口」的预期）。
const _gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!_gotSingleInstanceLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    const w = BrowserWindow.getAllWindows()[0];
    if (w) {
      try { if (w.isMinimized()) w.restore(); w.show(); w.focus(); } catch (e) { /* ignore */ }
    }
  });

  // 托管版令牌硬化：首次托管启动把出厂默认 "admin" 换成每机随机值（客户全程无感）。
  // 必须发生在 backendManager.start **之前**——spawn 时经 AITR_WEB_TOKEN 注入新令牌。
  // 若后端已在跑（升级后热启动/外部自管），它手里还是旧令牌，此刻换会让所有
  // Bearer 调用当场 401 → 本次跳过，下次冷启动自然完成轮换。
  async function maybeRotateManagedToken() {
    try {
      if (!tokenUtil.shouldRotateToken(_managedEdition, (config.backend || {}).token)) return;
      const { base_url } = config.backend || {};
      if (base_url) {
        const ctl = new AbortController();
        const timer = setTimeout(() => ctl.abort(), 1500);
        try {
          await fetch(`${base_url}/login`, { method: "GET", redirect: "manual", signal: ctl.signal });
          return; // 探活成功 = 已有后端在用旧令牌跑，本次不换
        } catch (e) { /* 连不上 = 后端没起，安全轮换 */ }
        finally { clearTimeout(timer); }
      }
      saveConfigPatch({ backend: { token: tokenUtil.generateToken() } });
    } catch (e) { /* 轮换失败不阻断启动：维持默认令牌（行为同旧版） */ }
  }

  app.whenReady().then(async () => {
    Menu.setApplicationMenu(buildChineseMenu());
    await maybeRotateManagedToken();
    // 后台自拉起（不阻塞开窗：renderer 已有「正在连接后台→自动重连」遮罩兜底）。
    backendManager.start(config).catch((e) => console.log(`[backend] start error: ${e}`));
    // 边车在后端之后拉起：它们要用 config.backend.token 回推入站消息，而该令牌可能刚被
    // maybeRotateManagedToken 换过。同样不阻塞开窗（登录成功前没有任何回推流量）。
    sidecars.startAll(config).catch((e) => console.log(`[sidecar] start error: ${e}`));
    createWindow();
    setupAutoUpdate();
    setupAnnouncements();
    setupVersionTelemetry();
  });
}

// 退出时回收后端进程，避免残留 Python/二进制占端口。
let _backendStopped = false;
app.on("before-quit", () => {
  if (_backendStopped) return;
  _backendStopped = true;
  try { backendManager.stop(); } catch (e) { /* 回收失败不阻断退出 */ }
  try { sidecars.stopAll(); } catch (e) { /* 同上：边车残留不该阻断退出 */ }
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
