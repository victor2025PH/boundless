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
const winFit = require("./win-fit.js");
const hotpatchApply = require("./hotpatch-apply.js");
const hotpatchStage = require("./hotpatch-stage.js");
const shellLangSync = require("./shell-lang-sync.js");

// 本机已落地的热补丁（resources/hotpatch.json，由 apply_chatx_hotpatch_node.ps1 写）。
// 进程内只读一次：该文件只在「本进程已退出、helper 正在替换文件」时变，读到的永远是
// 本次运行对应的那份。缺文件/坏 JSON 一律按「没打过补丁」。
let _localPatchInfo;
function localHotpatchInfo() {
  if (_localPatchInfo !== undefined) return _localPatchInfo;
  _localPatchInfo = null;
  try {
    if (app.isPackaged && process.resourcesPath) {
      const raw = JSON.parse(fs.readFileSync(path.join(process.resourcesPath, "hotpatch.json"), "utf-8"));
      _localPatchInfo = hotpatchApply.readLocalPatch(raw);
    }
  } catch (e) { /* 没打过补丁 / 文件坏 → 按未打过 */ }
  return _localPatchInfo;
}

/** 已落地的补丁号（0=没打过）。基线对不上的残留记录按 0——那是上一档安装包留下的。 */
function localPatchLevel() {
  try {
    const lp = localHotpatchInfo();
    if (lp && lp.patch > 0 && lp.baseAppVersion === app.getVersion()) return lp.patch;
  } catch (e) { /* app 未就绪等：按未打过 */ }
  return 0;
}

// 面向用户的版本串：package.json 的 displayVersion 优先（内测「1.001」这类展示号
// 不是合法 semver，进不了 version 字段——那是 electron-builder/updater 的机器版本），
// 缺字段回落 app.getVersion()。更新比对仍用 semver，勿拿本函数结果参与版本比较。
// 打过热补丁再缀 `+pN`（1.056+p3）：同一个安装包的第几号补丁必须一眼可见，否则报障
// 时 support 只看到 semver，分不清对面跑的是 p0 还是 p3。app.getVersion() 保持纯
// semver（updater 比对、遥测 manifest_version 都指着它）。
function displayVersion() {
  let base = "";
  try {
    const dv = require("./package.json").displayVersion;
    if (dv) base = String(dv);
  } catch (e) { /* 读不到 package.json → 回落 semver */ }
  if (!base) {
    try { base = app.getVersion(); } catch (e) { base = "dev"; }
  }
  return hotpatchApply.displayLabel(base, localPatchLevel());
}

// `--first-run`：无视「只弹一次」标记重看首启向导。写进 env 而不是走 IPC，是为了让
// shell-preload 能同步读到（向导在 DOM 就绪那一刻就要判断弹不弹，等不起一次往返）。
// 在这里而非 ready 里设置：preload 可能先于任何 ready 回调求值。
if (process.argv.includes("--first-run")) process.env.AITR_FORCE_FIRSTRUN = "1";

// `--poster-preview`：活动海报**验收通道**（P0 2026-08-22）——跳过资格/频控强制弹出，
// 不记展示频控、埋点走独立 poster6u_preview。没有它，验收海报要碰巧凑齐 8 道展示闸门
// （托管版/onboarding 72h 窗/频控/feed…），内部老机器永远不合格，每次发版都在问
// 「为什么没弹」。与 --first-run 同款走 env 让 preload 同步读到。
if (process.argv.includes("--poster-preview")) process.env.AITR_POSTER_PREVIEW = "1";

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

// Document PiP 浮窗副驾（rider 2026-08-18）：后端是 http 裸源（127.0.0.1:18799），
// Chromium 视为不安全上下文 → documentPictureInPicture API 不存在。把后端源列入
// 「视作安全」白名单以点亮壳内浮窗副驾；页面侧已出货+门禁（verify_cp_panel_modes 6.5）。
// 必须在 app ready 之前 appendSwitch 才生效（backendOrigin 为函数声明，提升可用）。
try { app.commandLine.appendSwitch("unsafely-treat-insecure-origin-as-secure", backendOrigin()); } catch (e) { /* 白名单失败仅浮窗不可用，不阻断启动 */ }

// 出厂默认值迁移（2026-08-14；2026-08-19 i18n 收口改为「清空＝跟随界面语言」）：
// userData/config.json 是首启种子、升级保留——产品级改名（统一收件箱 → AI 工作台 →
// 人工操作台）会被老副本的旧出厂值永久顶住（lianbei 升 1.0.31 实锤）。
// ⚠ 旧实现把新中文名**写进配置**，于是英文坐席机的侧栏永远是「人工操作台」——配置里
// 存了一个中文字面量，i18n 再怎么做也盖不住。现在改为迁成**空串**：renderer 的
// `ui.label || SH("console.manual")` 会按界面语言取词（zh 仍是「人工操作台」，逐字
// 不变；en 出 "Manual Console"）。用户显式自定义过的标签一律不动。
const FACTORY_INBOX_LABELS = ["统一收件箱", "AI 工作台", "人工操作台"];
(function migrateFactoryDefaults() {
  try {
    const ui = config && config.unified_inbox;
    if (ui && FACTORY_INBOX_LABELS.indexOf(ui.label) >= 0) {
      ui.label = "";
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
  // 第 4 参＝界面语言（与应用菜单 SS() / webview ?lang= 同源）：brand.json 双语列
  // 据此取 en/zh，兜底常量同理。部署方显式配的 config.brand 语言无关，仍最高优先。
  return brandUtil.pickBrandLocal((config && config.brand) || {}, brandJson, null, shellLang());
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
    // 缺字段回落**按界面语言**的兜底（此前恒回中文，英文壳会混出「智聊/无界科技」）
    return brandUtil.normalizeLiveBrand(await r.json(), brandUtil.brandFallback(shellLang()));
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

// 支持信息（机器码等，实施49 P1-9）：客服排障第一句永远是「把机器码发我」，
// 而机器码此前只在后台深处。壳的「关于」是用户找得到的唯一自述页，必须带上它。
// 2s 超时 + 任何异常回 null——关于框绝不能因为后端没起而打不开。
async function fetchSupportInfo() {
  const { base_url, token } = config.backend || {};
  if (!base_url) return null;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 2000);
  try {
    const r = await fetch(`${base_url}/api/support/info`, {
      headers: { Authorization: `Bearer ${token || ""}` },
      signal: ctl.signal,
    });
    if (!r.ok) return null;   // 旧后端无此路由 → 关于框少一行，不报错
    const d = await r.json();
    return (d && d.ok) ? d : null;
  } catch (e) {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

// 诊断直传（壳内，实施49 P1-9）。**为什么主进程也要有一份**：页面里已经有
// _support.html 的面板，但最需要报障的那一刻恰恰是「工作台白屏/打不开」——那时
// 页面内的入口一并没了，只剩托盘与应用菜单这条原生路径。后端还活着的白屏场景
// 因此能自助上传；后端也挂了则如实失败，用户仍可用「复制信息」把机器码给客服。
let _diagBusy = false;
async function uploadDiagFromShell(note) {
  const { base_url, token } = config.backend || {};
  if (!base_url) return { ok: false };
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 90000);   // 打包+上行比常规接口慢得多
  try {
    const r = await fetch(`${base_url}/api/support/diag-upload`, {
      method: "POST",
      headers: { Authorization: `Bearer ${token || ""}`, "Content-Type": "application/json" },
      body: JSON.stringify({ note: String(note || "").slice(0, 200) }),
      signal: ctl.signal,
    });
    return await r.json();
  } catch (e) {
    return { ok: false };
  } finally {
    clearTimeout(timer);
  }
}

// 原生报障流程：通知（上传中）→ 上传 → 结果弹窗（成功时可一键复制短码）。
// 「关于」框与「帮助」菜单共用同一份，两处文案与行为不会分叉。
async function runDiagFlow(win, product, ver, where) {
  if (_diagBusy) return;   // 打包要几十秒，期间再点＝重复烧一份包
  _diagBusy = true;
  // 原生弹窗是模态的，上传期间界面毫无反馈 → 先发一条系统通知，
  // 否则用户只会觉得「点了没反应」再点一次。
  try { new Notification({ title: product, body: SS("about.diag_busy") }).show(); } catch (e) { /* 通知不可用时静默 */ }
  const out = await uploadDiagFromShell(`${where} | v${ver}`);
  _diagBusy = false;
  const okc = !!(out && out.ok && out.code);
  const res = await dialog.showMessageBox(win, {
    type: okc ? "info" : "warning",
    title: `${SS("about.title_prefix")} ${product}`,
    // 成功文案的主语是「把这个编码告诉客服」——「上传成功」对用户是无动作信息
    message: okc ? `${SS("about.diag_ok")} ${out.code}` : SS("about.diag_fail"),
    detail: okc ? "" : String((out && out.detail) || ""),
    buttons: okc ? [SS("about.copy"), SS("about.close")] : [SS("about.close")],
    defaultId: 0,
    cancelId: okc ? 1 : 0,
    noLink: true,
  });
  if (okc && res.response === 0) {
    try { clipboard.writeText(String(out.code)); } catch (e) { /* 静默 */ }
  }
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
// 重启单个边车（连接弹窗 service_down 的「重启连接服务」按钮；QQ 个人号自研边车首个消费方）。
ipcMain.handle("desktop:sidecar-restart", (_e, name) => sidecars.restart(String(name || ""), config));

// #57 手机扫码操控（2026-08-30）：一键放行 Windows 防火墙（弹一次 UAC）。
// 绑定侧已由 backend-launcher.lanServeHost 解决（强令牌默认 0.0.0.0）；剩下的
// 拦路虎是防火墙对随包 backend.exe 的入站默认拒绝——per-user 安装器无提权加不了
// 规则，只能在用户显式点「放行」时提权补一条**程序级**规则（不开端口大门，
// 只放行自家 exe；卸载残留规则无害——程序没了规则空转）。开发态（python 跑
// 源码）不代劳：返回 reason 让页面给指引而不是静默失败。
ipcMain.handle("desktop:pair-lan-fix", async () => {
  if (process.platform !== "win32") return { ok: false, reason: "platform" };
  const path = require("path");
  const binPath = app.isPackaged
    ? path.join(process.resourcesPath, "backend", "backend.exe") : "";
  let exists = false;
  try { exists = !!binPath && fs.existsSync(binPath); } catch (e) { exists = false; }
  if (!exists) return { ok: false, reason: "no_bundled_backend" };
  // 嵌套引号地狱规避：真正的 netsh 命令写进临时 .ps1（UTF-8 带 BOM——安装路径
  // 常含 CJK，PS5.1 无 BOM 会按 GBK 读花），外层只负责「提权跑这个文件」。
  // 规则名 ASCII 无空格（免引号）；幂等=先删同名旧规则再加；规则是**程序级**
  // （只放行自家 backend.exe 的入站，不开端口大门）。
  const os = require("os");
  const tmpPs1 = path.join(os.tmpdir(), "chatx_pair_fw_" + Date.now() + ".ps1");
  const inner = [
    "netsh advfirewall firewall delete rule name=\"ChatXBackend\" | Out-Null",
    "netsh advfirewall firewall add rule name=\"ChatXBackend\" dir=in action=allow "
      + "program=\"" + binPath + "\" enable=yes profile=any",
    "exit $LASTEXITCODE",
  ].join("\r\n");
  try {
    fs.writeFileSync(tmpPs1, "\uFEFF" + inner, { encoding: "utf8" });
  } catch (e) {
    return { ok: false, reason: "tmp_write", error: String((e && e.message) || e) };
  }
  const escPs1 = tmpPs1.replace(/'/g, "''");
  const script = "$ErrorActionPreference='Stop'; try { "
    + "$p = Start-Process powershell -ArgumentList ('-NoProfile','-ExecutionPolicy','Bypass',"
    + "'-WindowStyle','Hidden','-File',('\"' + '" + escPs1 + "' + '\"')) -Verb RunAs -Wait -PassThru; "
    + "exit $p.ExitCode } catch { exit 1223 }";   // 1223 = ERROR_CANCELLED（UAC 被拒）
  return await new Promise((resolve) => {
    const done = (out) => {
      try { fs.unlinkSync(tmpPs1); } catch (e) { /* 临时文件清理 best-effort */ }
      resolve(out);
    };
    let child;
    try {
      child = spawn("powershell", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        { windowsHide: true });
    } catch (e) {
      done({ ok: false, reason: "spawn", error: String((e && e.message) || e) });
      return;
    }
    const timer = setTimeout(() => {
      try { child.kill(); } catch (e) { /* best-effort */ }
      done({ ok: false, reason: "timeout" });
    }, 120000);
    child.on("exit", (code) => {
      clearTimeout(timer);
      if (code === 0) done({ ok: true });
      else if (code === 1223) done({ ok: false, reason: "cancelled" });
      else done({ ok: false, reason: "exit_" + code });
    });
    child.on("error", (e) => {
      clearTimeout(timer);
      done({ ok: false, reason: "spawn", error: String((e && e.message) || e) });
    });
  });
});

// #254 D-P2「允许局域网设备连接」开关（2026-09-08）。出厂态后端只绑 127.0.0.1
// （backend-launcher.lanServeHost 只认 backend.lan_access===true）；本 IPC 是设置页 /
// 壳横幅 / 小智配对弹窗共用的唯一读写口：
//   {action:"status"}                    → 当前开关 + 本机 LAN 地址（横幅/设置页显示）
//   {action:"set", on, confirmed}        → 写 config.json；on 且 Windows 网络类别为 Public
//                                          且未 confirmed → 回 needs_confirm 让页面二次确认
//   {action:"relaunch"}                  → 重启应用（后端随壳重拉，新 serve host 生效）
// 决策纯函数在 lan-access.js（门禁直跑）；这里只做 Electron/进程胶水。
const lanAccess = require("./lan-access.js");
function probeNetworkCategory() {
  if (process.platform !== "win32") return Promise.resolve("");
  return new Promise((resolve) => {
    let out = "";
    let child;
    try {
      child = spawn("powershell", ["-NoProfile", "-NonInteractive", "-Command",
        "Get-NetConnectionProfile | Select-Object -ExpandProperty NetworkCategory"],
      { windowsHide: true });
    } catch (e) { resolve(""); return; }
    const timer = setTimeout(() => { try { child.kill(); } catch (e) { /* */ } resolve(""); }, 6000);
    try { child.stdout.on("data", (d) => { out += String(d); }); } catch (e) { /* */ }
    child.on("exit", () => { clearTimeout(timer); resolve(lanAccess.parseNetworkCategory(out)); });
    child.on("error", () => { clearTimeout(timer); resolve(""); });
  });
}
function lanAccessStatus() {
  let ifaces = {};
  try { ifaces = require("os").networkInterfaces(); } catch (e) { ifaces = {}; }
  return lanAccess.lanStatus(config, { lanIp: lanAccess.pickLanIp(ifaces) });
}
function broadcastLanAccess(st) {
  for (const w of BrowserWindow.getAllWindows()) {
    try { if (!w.isDestroyed()) w.webContents.send("cx-lan-access", st); } catch (e) { /* 窗已亡 */ }
  }
}
ipcMain.handle("desktop:lan-access", async (_e, args) => {
  const a = (args && typeof args === "object") ? args : {};
  const action = String(a.action || "status");
  if (action === "relaunch") {
    try { app.relaunch(); app.exit(0); } catch (e) { return { ok: false, error: String((e && e.message) || e) }; }
    return { ok: true };
  }
  if (action === "set") {
    const on = a.on === true;
    const category = on ? await probeNetworkCategory() : "";
    const d = lanAccess.decideToggle({ on, confirmed: a.confirmed === true, category });
    if (!d.allow) {
      return Object.assign({ ok: false, needs_confirm: true, category }, lanAccessStatus());
    }
    const r = saveConfigPatch({ backend: { lan_access: on } });
    if (!r.ok) return r;
    const st = Object.assign({ ok: true, restart_required: true, category }, lanAccessStatus());
    console.log(`[lan-access] backend.lan_access=${on} (category=${category || "unknown"}); restart required`);
    broadcastLanAccess(st);
    return st;
  }
  return Object.assign({ ok: true }, lanAccessStatus());
});

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
      title: String(a.title || SS("notif.default_title")),
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
      return { ok: false, error: (r && r.error) || SS("err.no_path") };
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
    if (!count) return { ok: false, error: SS("err.no_samples") };
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

ipcMain.handle("desktop:voice-tts", async (_e, { text, persona_id, chat_key, platform, account_id, target_lang }) => {
  try {
    // 会话上下文透传（试听=发送 契约；旧渲染层不传=旧行为）
    // P0-V2b 译声：target_lang（'auto'=会话客户语言，服务端解析）先译后念
    const d = await backendPost("/api/voice/tts-test", {
      text, persona_id: persona_id || undefined,
      chat_key: chat_key || undefined,
      platform: platform || undefined,
      account_id: account_id || undefined,
      target_lang: target_lang || undefined,
    });
    if (d.audio_url) return { ...d, ok: d.ok !== false };
    if (!d.filename) return d;
    const { base_url, token } = config.backend || {};
    const r = await fetch(
      `${base_url}/api/voice/tts-file/${encodeURIComponent(d.filename)}`,
      { headers: { Authorization: `Bearer ${token}` } });
    if (!r.ok) return { ok: false, message: SS("err.audio_fetch", { status: r.status }) };
    const b64 = Buffer.from(await r.arrayBuffer()).toString("base64");
    // #121：克隆链产物是 WAV（服务端 format=wav）——此前一律标 audio/mpeg，容器与
    // MIME 打架是浏览器解码启发式误判「无声」的温床之一；按真实格式标注。
    const fmt = String(d.format || "mp3").toLowerCase();
    const mt = fmt.includes("ogg") || fmt.includes("opus") ? "audio/ogg"
      : (fmt.includes("wav") ? "audio/wav" : "audio/mpeg");
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
    if (!buf.length) return { ok: false, message: SS("err.empty_audio") };
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
  // 用户管理 P0-2：坐席机 backend.auto_login=false → 弹窗也不拿 token 代登，走人的子帐号
  if ((config.backend || {}).auto_login === false) return;
  // B95（实施68 P1-14）：两处旧缺陷曾把「点数据洞察」变成「弹回后台首页」——
  //   ① 回跳目标写死为「开窗时的 URL」：会话中途过期后点任何侧栏页 → 服务端
  //      303 /login?next=<目标页> → 这里重登后 location.replace(开窗首页)，
  //      用户看到的就是「正在连接…→ 被弹回后台首页」，且毫无报错。
  //      现在优先取 /login?next=（本次真实目的地，服务端 safe_next_path 同规则
  //      校验过），开窗 URL 只作兜底。
  //   ② attempted 一次性：长寿命弹窗第二次过期就永远停在登录页。改为按次冷却
  //      （8s）+ 每窗上限 3 次（token 失效时不无限打转，登录页如实露出让人工
  //      处理）；任一次成功进入非登录页即重置配额。
  let tries = 0;
  let lastTry = 0;
  // 用户管理 P0-1（2026-09-11）：/logout 落地 /login?manual=1 =「人主动退出」——本窗进入
  // 手动登录态，不再拿 token 秒级重登（否则「退出登录」形同虚设、子帐号无法登录）。
  // 态粘性：密码错重渲染的 /login 无 query 也不自动登录；进入任何非登录页即解除。
  let manual = false;
  win.webContents.on("did-finish-load", () => {
    let u;
    try { u = new URL(win.webContents.getURL()); } catch (e) { return; }
    if (u.pathname !== "/login" && u.pathname !== "/login/") {
      tries = 0;   // 成功进站：重置重登配额（下次过期还有机会自愈）
      manual = false;
      return;
    }
    try { if (u.searchParams.get("manual") === "1") manual = true; } catch (e) {}
    if (manual) return;
    const now = Date.now();
    if (tries >= 3 || (now - lastTry) < 8000) return;
    tries++;
    lastTry = now;
    let next = "";
    try { next = String(u.searchParams.get("next") || ""); } catch (e) { next = ""; }
    // 与服务端 safe_next_path 同向的最小校验：仅站内相对路径（防 //evil）
    if (!next || next.charAt(0) !== "/" || next.indexOf("//") === 0) {
      try {
        const dest = new URL(intendedUrl);
        next = (dest.pathname + dest.search) || "/";
      } catch (e) { next = "/"; }
    }
    if (!String(next).startsWith("/")) next = "/";
    const js =
      "(function(){try{" +
      "var n=" + JSON.stringify(next) + ";" +
      "fetch('/login',{method:'POST'," +
      "headers:{'Content-Type':'application/x-www-form-urlencoded'}," +
      "body:'auth_token='+encodeURIComponent(" + JSON.stringify(token) + ")" +
      "+'&next='+encodeURIComponent(n)," +
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

// ── 坐席工作台「唯一容器」路由（2026-08-29，修「后台点坐席工作台仍多开一个」）──────
// 主窗常驻统一收件箱标签（renderer buildInboxTab，默认首屏）＝坐席工作台唯一正身；
// 再弹一个 /workspace 原生窗等于当场制造第二个工作台。此前两个体感都是「没生效」：
//   · BC 探活命中 → 入口页只弹「请切换」提示，主窗不会被拉到前台（网页无权聚焦别窗）；
//   · 探活 miss（webview 正在登录跳转/后端重启窗/刚启动）→ 落到本文件开弹窗，漏一个
//     就长期跟主窗标签并存（遥测 mw.takeover_auto 修复后 8 天仍 15 次）。
// 收敛为：精确 /workspace 的打开请求一律路由主窗——聚焦 + cx-open-workspace 通知
// renderer 切收件箱标签，?conv= 深链由 renderer 经页面既有 open-conv 契约转入
// （deliverToInbox 自带 ready 轮询，webview 未就绪深链不丢）。仅当收件箱标签被配置
// 关闭（unified_inbox.enabled=false 的纯内嵌形态）或主窗不在时，回落旧弹窗语义。
let mainSeatWin = null; // createWindow 登记；closed 清空（activate 重建会再登记）

function focusMainInbox(url) {
  const w = mainSeatWin;
  if (!w || w.isDestroyed()) return false;
  if (((config || {}).unified_inbox || {}).enabled === false) return false;
  try {
    w.webContents.send("cx-open-workspace", { url: String(url || "") });
    if (w.isMinimized()) w.restore();
    w.show();
    w.focus();
    return true;
  } catch (e) {
    return false; // 路由失败回落弹窗，绝不吞点击
  }
}

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

// #158（2026-09-04）：请求来自哪个弹窗（from.win / from.slot）。wsub 辅助弹窗（草稿审批等
// /workspace/* 子页）里点「聊天」→ 主窗接手后把辅助窗关掉：否则主窗在后面亮了、前台弹窗
// 原地不动，体感仍是「点了没反应」。admin 槽刻意不关——后台管理是用户另开的现场。
function closeHandedOffPopup(from) {
  if (!from || from.slot !== "wsub") return;
  const w = from.win;
  if (!w || w.isDestroyed()) return;
  try { w.close(); } catch (e) { /* 关不掉不阻断主窗聚焦 */ }
  try { if (mainSeatWin && !mainSeatWin.isDestroyed()) mainSeatWin.focus(); } catch (e) { /* 二次聚焦 best-effort */ }
}

function openBackendPopup(url, from) {
  const slot = backendPopupSlot(url);
  // 精确 /workspace 先路由主窗收件箱标签（见上方 focusMainInbox），不再产生
  // 「主窗标签 + workspace 弹窗」双工作台；主窗不可用才回落弹窗链。
  if (slot === "workspace" && focusMainInbox(url)) { closeHandedOffPopup(from); return null; }
  const reused = reuseBackendPopup(slot, url);
  if (reused) return reused;
  const child = new BrowserWindow({
    width: 1100,
    height: 800,
    title: SS("win.backend_popup"),
    // 与主窗同批（appmenu 内迁 2026-08-22）：弹窗保留系统标题栏但菜单条同样隐藏
    // （Alt 唤出）——否则主窗黑条没了、每个后台弹窗还顶着一条，观感割裂。
    autoHideMenuBar: true,
    webPreferences: {
      partition: BACKEND_WORKSPACE_PARTITION,
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: false,
      // B54：焦点自愈桥（confirm/alert 后焦点态失步的主进程复位通道）
      preload: path.join(__dirname, "renderer", "popup-preload.js"),
    },
  });
  try {
    if (fs.existsSync(DEFAULT_BRAND_ICON)) child.setIcon(DEFAULT_BRAND_ICON);
  } catch (e) { /* 图标缺失不阻断 */ }
  backendPopupWins.set(slot, child);
  child.on("closed", () => {
    if (backendPopupWins.get(slot) === child) backendPopupWins.delete(slot);
  });
  // 弹窗里再点后台 target=_blank 链接（如后台侧栏「坐席工作台」）同走本唯一性收敛；
  // 带上自己的槽位与句柄——wsub 子页弹窗点「聊天」交接主窗后由 closeHandedOffPopup 收窗。
  child.webContents.setWindowOpenHandler(makeBackendPopupHandler({ win: child, slot: slot }));
  wireEditContextMenu(child.webContents);   // 后台弹窗（admin/workspace 子页）同享右键编辑菜单
  watchWebLangSwitch(child.webContents);    // #151：后台页里切语言同样让壳跟随
  wireUnloadGuard(child.webContents, child); // #193：beforeunload 不再静默吞导航/关窗
  bindBackendPopupLogin(child, url);
  child.loadURL(url);
  return child;
}

function makeBackendPopupHandler(from) {
  return ({ url }) => {
    if (!isBackendUrl(url)) return { action: "allow" };
    setImmediate(() => {
      try { openBackendPopup(url, from); } catch (e) {
        console.log("[popup] open failed: " + ((e && e.message) || e));
      }
    });
    return { action: "deny" };
  };
}

// ── beforeunload 还原浏览器语义（#193，2026-09-05）────────────────────────────────
// 页面用 beforeunload 守未保存改动（人设工作室「全局规则」等）时，Electron 的默认处理是
// **静默取消**导航/关窗——不弹任何对话框。坐席体感：点侧栏「声音评测」像没反应，15s 后
// 页面早绘制载入层还误报「页面未能打开: /admin/voice-eval」（skuio #193）；关窗关不掉。
// 这里对壳内全部承载后台页的 webContents 挂 will-prevent-unload：原生确认框「离开 / 留下」，
// 选离开则 preventDefault 忽略 beforeunload 放行；默认/Esc＝留下（与浏览器一致）。
// 对话框自身异常时放行——宁可让页面走掉，也不把人锁在页面里再演一次「点了没反应」。
function wireUnloadGuard(wc, ownerWin) {
  if (!wc || wc.__cxUnloadWired) return;
  wc.__cxUnloadWired = true;
  wc.on("will-prevent-unload", (e) => {
    let leave = true;
    try {
      const w = (ownerWin && !ownerWin.isDestroyed()) ? ownerWin : BrowserWindow.fromWebContents(wc);
      const opts = {
        type: "question",
        buttons: [SS("unload.leave"), SS("unload.stay")],
        defaultId: 1,
        cancelId: 1,
        noLink: true,
        title: SS("unload.title"),
        message: SS("unload.msg"),
        detail: SS("unload.detail"),
      };
      const r = (w && !w.isDestroyed()) ? dialog.showMessageBoxSync(w, opts) : dialog.showMessageBoxSync(opts);
      leave = (r === 0);
    } catch (err) {
      leave = true;
    }
    if (leave) e.preventDefault();
  });
}

// ── 悬浮副驾原生置顶窗（cp PiP shell 桥 P1，2026-08-18）──────────────────────
// 浏览器端悬浮副驾走 Document PiP；Electron 31 的该 API 是 0×0 退化窗（探针实锤，
// 页面侧已按 UA 禁用）→ 壳内改由这里开原生 alwaysOnTop 窗装 wrapper
// （renderer/pip.html + iframe /copilot/app.html，共享热区 app.html 零改动）。
// 消息中继：workspace 页(webview) ⇄ inbox-preload(invoke/事件) ⇄ 这里 ⇄
// pip-preload ⇄ wrapper ⇄ app.html。窗全局唯一（再开=聚焦复用）；owner=最近
// open/post 的 webContents（cp-fill/cp-send 回吐只送它，销毁即无人可送）。
// 认证双保险：分区复用工作台登录 cookie + URL hash 带 token（app.html 自带兜底）。
let copilotPipWin = null;
let copilotPipOwner = null;

function copilotPipStatus() {
  return { ok: true, open: !!(copilotPipWin && !copilotPipWin.isDestroyed()) };
}

/* 位置/尺寸记忆（P2 2026-08-18）：关窗时存 bounds 进 config.copilot_pip.bounds，
   下次打开还原到坐席习惯的位置。记忆放主进程而非页面 localStorage——原生窗的
   bounds 只有主进程知道（页面拿不到），且 config.json 跨页面刷新/壳重启都在。
   还原前过屏幕边界校验：多屏拔线/换分辨率后旧坐标可能整窗落屏外，要求与任一
   显示器工作区至少 40px 交叠，不足则位置作废只还原尺寸（居中开，坐席再拖）。 */
function copilotPipSavedBounds() {
  const c = config.copilot_pip || {};
  const b = c.bounds;
  if (!b || typeof b !== "object") return null;
  const r = {
    x: parseInt(b.x, 10), y: parseInt(b.y, 10),
    width: parseInt(b.width, 10), height: parseInt(b.height, 10),
  };
  if (!Number.isFinite(r.width) || !Number.isFinite(r.height)) return null;
  r.width = Math.max(320, Math.min(r.width, 900));
  r.height = Math.max(360, Math.min(r.height, 1000));
  if (!Number.isFinite(r.x) || !Number.isFinite(r.y)) return { width: r.width, height: r.height };
  try {
    const { screen } = require("electron");
    const visible = screen.getAllDisplays().some((d) => {
      const a = d.workArea;
      return r.x < a.x + a.width - 40 && r.x + r.width > a.x + 40
        && r.y < a.y + a.height - 40 && r.y + r.height > a.y + 40;
    });
    if (!visible) return { width: r.width, height: r.height };
  } catch (e) {
    return { width: r.width, height: r.height }; // screen 不可用：保守只还原尺寸
  }
  return r;
}

function notifyCopilotPipOwner(msg) {
  try {
    if (copilotPipOwner && !copilotPipOwner.isDestroyed()) copilotPipOwner.send("cp-pip-evt", msg);
  } catch (e) { /* owner 已销毁：无人可通知，静默 */ }
}

function openCopilotPipWindow(sender, opts) {
  copilotPipOwner = sender;
  if (copilotPipWin && !copilotPipWin.isDestroyed()) {
    try {
      if (copilotPipWin.isMinimized()) copilotPipWin.restore();
      copilotPipWin.focus();
    } catch (e) { /* 聚焦失败不阻断 */ }
    return copilotPipStatus();
  }
  const o = opts && typeof opts === "object" ? opts : {};
  const b = config.backend || {};
  const base = String(b.base_url || "http://127.0.0.1:18799").replace(/\/+$/, "");
  const theme = o.theme === "light" ? "light" : "dark";
  const lang = o.lang === "en" ? "en" : "zh";
  // pip=1 仅作标记（app.html 忽略未知参数）；hostAccounts=1 与 renderer 内嵌副驾同参
  const appUrl = base + "/copilot/app.html?theme=" + theme + "&lang=" + lang + "&hostAccounts=1&pip=1"
    + (b.token ? "#token=" + encodeURIComponent(String(b.token)) : "");
  // 尺寸优先级：主进程记忆（含坐席上次拖出的位置）> 页面传参 > 默认
  const saved = copilotPipSavedBounds();
  const w = (saved && saved.width) || Math.max(320, Math.min(parseInt(o.w, 10) || 380, 900));
  const h = (saved && saved.height) || Math.max(360, Math.min(parseInt(o.h, 10) || 560, 1000));
  const winOpts = {
    width: w,
    height: h,
    minWidth: 320,
    minHeight: 360,
    title: SS("win.pip"),
    alwaysOnTop: true,
    autoHideMenuBar: true,
    webPreferences: {
      partition: BACKEND_WORKSPACE_PARTITION, // iframe 复用工作台登录态
      preload: path.join(__dirname, "renderer", "pip-preload.js"),
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: false,
    },
  };
  if (saved && Number.isFinite(saved.x) && Number.isFinite(saved.y)) {
    winOpts.x = saved.x;
    winOpts.y = saved.y;
  }
  copilotPipWin = new BrowserWindow(winOpts);
  try { copilotPipWin.setAlwaysOnTop(true, "floating"); } catch (e) { /* 平台不支持档位时保默认置顶 */ }
  try { if (fs.existsSync(DEFAULT_BRAND_ICON)) copilotPipWin.setIcon(DEFAULT_BRAND_ICON); } catch (e) { /* 图标缺失不阻断 */ }
  copilotPipWin.on("close", () => {
    // close（销毁前）才能取到 bounds；closed 时窗已亡。保存失败不阻断关窗。
    try { saveConfigPatch({ copilot_pip: { bounds: copilotPipWin.getBounds() } }); } catch (e) { /* 静默 */ }
  });
  copilotPipWin.on("closed", () => {
    copilotPipWin = null;
    notifyCopilotPipOwner({ type: "cp-pip-closed" });
    copilotPipOwner = null;
  });
  copilotPipWin.loadFile(path.join(__dirname, "renderer", "pip.html"), { query: { src: appUrl } });
  return copilotPipStatus();
}

// 工作台页（webview 的 inbox-preload）唯一入口；纵深防御再验一道发起方 URL。
// B54（实施68 P1-16）：Electron 原生 confirm/alert 关闭后 Chromium 焦点态失步
// （上游久悬 bug）——整窗输入框点不进、退出重进才恢复。钧实录「主动关怀 →
// 跳过关怀记录 → 取消 → 输入全死」与 skuio「人设编辑页偶发无光标」同根：两处
// 都是 window.confirm。唯一可靠解药=主进程把窗口 blur+focus 一轮，复位焦点态。
// 页面侧（_focus_selfheal.html）在壳内包一层 confirm/alert/prompt，返回即调本
// 通道；另有点击自愈兜底。窗口解析兼容两形态：弹窗自身 / webview 的宿主窗。
ipcMain.handle("desktop:focus-fix", (e) => {
  try {
    if (!isBackendUrl(e.sender.getURL())) return { ok: false, error: "forbidden" };
  } catch (err) {
    return { ok: false, error: "forbidden" };
  }
  try {
    let win = BrowserWindow.fromWebContents(e.sender);
    if (!win && e.sender.hostWebContents) {
      win = BrowserWindow.fromWebContents(e.sender.hostWebContents);
    }
    if (win && !win.isDestroyed()) {
      win.blur();
      win.focus();
      return { ok: true };
    }
  } catch (err) { /* 窗口已销毁等：如实返回 false */ }
  return { ok: false };
});

ipcMain.handle("desktop:copilot-pip", (e, req) => {
  try {
    if (!isBackendUrl(e.sender.getURL())) return { ok: false, error: "forbidden" };
  } catch (err) {
    return { ok: false, error: "forbidden" };
  }
  const r = req && typeof req === "object" ? req : {};
  if (r.action === "open") return openCopilotPipWindow(e.sender, r);
  if (r.action === "close") {
    try { if (copilotPipWin && !copilotPipWin.isDestroyed()) copilotPipWin.close(); } catch (err) { /* 已销毁视同已关 */ }
    return copilotPipStatus();
  }
  if (r.action === "post") {
    copilotPipOwner = e.sender; // 最近喂上下文的窗即当前宿主（页面刷新/多窗接管自然跟随）
    try {
      if (copilotPipWin && !copilotPipWin.isDestroyed() && r.msg && typeof r.msg === "object") {
        copilotPipWin.webContents.send("cp-pip-in", r.msg);
      }
    } catch (err) { /* 窗关闭竞态：丢弃本条，closed 事件会同步页面状态 */ }
    return copilotPipStatus();
  }
  if (r.action === "status") return copilotPipStatus();
  return { ok: false, error: "bad action" };
});

// wrapper（pip-preload）回吐 → 宿主页：只认自家 PiP 窗的 webContents
ipcMain.on("cp-pip-out", (e, msg) => {
  if (!copilotPipWin || copilotPipWin.isDestroyed() || e.sender !== copilotPipWin.webContents) return;
  if (!msg || typeof msg !== "object" || !msg.type) return;
  notifyCopilotPipOwner(msg);
});

// ── 壳层文案语言（i18n P0 2026-08-19；扩展语透传 2026-08-27）─────────────────
// 此前应用菜单/右键菜单/关于框硬编码中文——英文坐席开壳第一眼就是「文件 编辑 视图」。
// 语言源=壳配置 unified_inbox.lang（首启向导写入；与工作台 webview 的 ?lang= 同源）。
// 语言决策：显式配置最高（en / zh / 扩展语 vi/th/id/zh_hant 原样透传——webview
// ?lang= 让 Web 端按该语渲染，壳自身词典没有的语按表定底回落：vi/th/id → en，
// zh_hant → zh）；**配置为空=「跟随系统」**（首启向导默认项，2026-08-27 起真跟随：
// app.getLocale()=OS 界面语言 → 同一套家族映射；此前空值恒中文，「跟随系统」是假的）。
// 显式未知值（如手改成 ja）保守维持中文、不偷跟系统——显式≠委托推断。
// 菜单在启动期构建一次；工作台里切语言（/set_lang 导航）由 watchWebLangSwitch 同步
// 配置并 rebuildAppMenu（#151，此前要重启壳才生效）。
const SHELL_EXT_LANGS = { vi: "en", th: "en", id: "en", zh_hant: "zh" }; // 码→词典回落底
// BCP-47/配置值 → 壳语言码；认不出返回 ""（调用方决定回落方向）
function shellLangFromTag(raw) {
  const l = String(raw || "").trim().toLowerCase().replace(/-/g, "_");
  if (!l) return "";
  if (l === "en" || l.indexOf("en_") === 0) return "en";
  const parts = l.split("_");
  if (parts[0] === "zh") {
    return (parts.indexOf("tw") > 0 || parts.indexOf("hk") > 0
      || parts.indexOf("mo") > 0 || parts.indexOf("hant") > 0) ? "zh_hant" : "zh";
  }
  if (SHELL_EXT_LANGS[parts[0]]) return parts[0];
  return "";
}
function shellLang() {
  try {
    const cfg = String((((config || {}).unified_inbox) || {}).lang || "").trim();
    const explicit = shellLangFromTag(cfg);
    if (explicit) return explicit;
    if (!cfg) {
      // 空=跟随系统。app ready 前 getLocale 可能为空 → 落中文（下次构建即正确）
      const sys = shellLangFromTag(app.getLocale());
      if (sys) return sys;
    }
  } catch (e) { /* 配置/app 缺失回落中文 */ }
  return "zh";
}
const SHELL_STR = {
  zh: {
    "menu.file": "文件", "menu.reload": "重新加载", "menu.force_reload": "强制重新加载",
    "menu.quit": "退出", "menu.edit": "编辑", "menu.undo": "撤销", "menu.redo": "重做",
    "menu.cut": "剪切", "menu.copy": "复制", "menu.paste": "粘贴",
    "menu.paste_image": "粘贴图片", "menu.paste_plain": "粘贴为纯文本",
    "menu.select_all": "全选", "menu.view": "视图", "menu.zoom_reset": "实际大小",
    "menu.zoom_in": "放大", "menu.zoom_out": "缩小", "menu.fullscreen": "全屏",
    "menu.devtools": "开发者工具", "menu.window": "窗口", "menu.minimize": "最小化",
    "menu.close_win": "关闭窗口", "menu.help": "帮助", "menu.about": "关于",
    "menu.check_update": "检查更新", "menu.diag": "上传诊断给客服",
    // 开发者模式（版本号连点 12 次解锁，2026-08-22）：帮助菜单版本行与解锁提示
    "menu.devmode_on": "开发者模式已开启",
    "menu.devmode_off": "关闭开发者模式",
    "menu.devmode_hint": "再点 {n} 次开启开发者模式",
    "menu.support": "求助 / 发诊断给客服",
    "about.title_prefix": "关于", "about.workbench": "桌面工作台",
    "about.tagline": "人工操作台 + 业务助手", "about.version": "版本",
    "about.visit": "访问官网", "about.close": "关闭",
    "about.machine": "机器码", "about.copy": "复制信息",
    "about.diag": "上传诊断给客服", "about.diag_busy": "正在打包上传诊断信息，请稍候…",
    "about.diag_ok": "请把这个编码告诉客服：", "about.diag_fail": "上传失败，请检查网络后重试；也可点「复制信息」把机器码发给客服。",
    "ctx.copy_link": "复制链接地址", "ctx.copy_image": "复制图片",
    // 窗口标题 / 系统通知 / IPC 错误回执：都会原样出现在坐席眼前（任务栏、
    // 通知中心、渲染层 toast），此前全是中文字面量。
    "win.backend_popup": "智聊", "win.pip": "悬浮副驾",
    "notif.default_title": "新消息",
    "err.no_path": "后端未返回路径", "err.no_samples": "无样本可导出",
    "err.audio_fetch": "音频拉取失败 {status}", "err.empty_audio": "空音频",
    // 充值到账奖励时刻（海报 CTA → 浏览器付款 → 入账后的系统通知）
    "camp.credited_title": "充值已到账 🎉",
    "camp.credited_body": "+{n} 已入账，工作台额度已更新，感谢支持！",
    // #193：页面 beforeunload 拦截 → 原生「离开 / 留下」确认（此前 Electron 静默吞掉）
    "unload.title": "有未保存的改动",
    "unload.msg": "这个页面有未保存的改动。",
    "unload.detail": "离开会丢失这些改动；留下可以先保存。",
    "unload.leave": "离开", "unload.stay": "留下",
  },
  en: {
    "menu.file": "File", "menu.reload": "Reload", "menu.force_reload": "Force Reload",
    "menu.quit": "Quit", "menu.edit": "Edit", "menu.undo": "Undo", "menu.redo": "Redo",
    "menu.cut": "Cut", "menu.copy": "Copy", "menu.paste": "Paste",
    "menu.paste_image": "Paste Image", "menu.paste_plain": "Paste as Plain Text",
    "menu.select_all": "Select All", "menu.view": "View", "menu.zoom_reset": "Actual Size",
    "menu.zoom_in": "Zoom In", "menu.zoom_out": "Zoom Out", "menu.fullscreen": "Full Screen",
    "menu.devtools": "Developer Tools", "menu.window": "Window", "menu.minimize": "Minimize",
    "menu.close_win": "Close Window", "menu.help": "Help", "menu.about": "About",
    "menu.check_update": "Check for Updates", "menu.diag": "Send Diagnostics to Support",
    "menu.devmode_on": "Developer mode enabled",
    "menu.devmode_off": "Disable Developer Mode",
    "menu.devmode_hint": "{n} more clicks to enable developer mode",
    "menu.support": "Get Help / Send Diagnostics",
    "about.title_prefix": "About", "about.workbench": "Desktop Workbench",
    "about.tagline": "Agent console + business copilot", "about.version": "Version",
    "about.visit": "Visit Website", "about.close": "Close",
    "about.machine": "Machine code", "about.copy": "Copy details",
    "about.diag": "Send diagnostics", "about.diag_busy": "Packing and uploading diagnostics...",
    "about.diag_ok": "Give this code to support:", "about.diag_fail": "Upload failed. Check the network and retry, or use Copy details and send it to support.",
    "ctx.copy_link": "Copy Link Address", "ctx.copy_image": "Copy Image",
    "win.backend_popup": "ChatX", "win.pip": "Floating Copilot",
    "notif.default_title": "New message",
    "err.no_path": "Backend returned no path", "err.no_samples": "No samples to export",
    "err.audio_fetch": "Audio fetch failed ({status})", "err.empty_audio": "Empty audio",
    "camp.credited_title": "Top-up credited 🎉",
    "camp.credited_body": "+{n} just landed — your workspace quota is updated. Thank you!",
    "unload.title": "Unsaved changes",
    "unload.msg": "This page has unsaved changes.",
    "unload.detail": "Leaving will discard them; stay to save first.",
    "unload.leave": "Leave", "unload.stay": "Stay",
  },
};
// 扩展语壳词条 overlay（scripts/i18n_desktop_ext.py 生成 shell-str-ext.json；
// zh_hant 等语言的 SHELL_STR 后装。缺文件/坏 JSON＝静默维持 zh/en 双语，绝不崩壳）。
try {
  const _extStr = JSON.parse(
    fs.readFileSync(path.join(__dirname, "shell-str-ext.json"), "utf-8"));
  for (const _lg of Object.keys(_extStr || {})) {
    if (_extStr[_lg] && typeof _extStr[_lg] === "object") SHELL_STR[_lg] = _extStr[_lg];
  }
} catch (e) { /* overlay 可选 */ }
// vars 走 {name} 占位替换（与渲染层 SH() 同约定）：错误回执要带 status 之类的
// 动态量，拼字符串会把语序烙死（英文 "Audio fetch failed (404)" 括号在后）。
// 词典回落：扩展语走 SHELL_EXT_LANGS 表定底（vi/th/id→en，zh_hant→zh）；其余未知语维持 zh。
function SS(key, vars) {
  const l = shellLang();
  const d = SHELL_STR[l] || SHELL_STR[SHELL_EXT_LANGS[l]] || SHELL_STR.zh;
  let s = d[key] != null ? d[key] : (SHELL_STR.zh[key] != null ? SHELL_STR.zh[key] : key);
  if (vars) {
    s = String(s).replace(/\{(\w+)\}/g, (m, k) =>
      (vars[k] === undefined || vars[k] === null ? m : String(vars[k])));
  }
  return s;
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
          { label: SS("menu.undo"), enabled: !!ef.canUndo, click: () => wc.undo() },
          { label: SS("menu.redo"), enabled: !!ef.canRedo, click: () => wc.redo() },
          { type: "separator" },
          { label: SS("menu.cut"), enabled: !!ef.canCut, click: () => wc.cut() },
          { label: SS("menu.copy"), enabled: !!ef.canCopy, click: () => wc.copy() },
          { label: clipHasImage ? SS("menu.paste_image") : SS("menu.paste"), enabled: !!ef.canPaste, click: () => wc.paste() },
          { label: SS("menu.paste_plain"), enabled: !!ef.canPaste, click: () => wc.pasteAndMatchStyle() },
          { type: "separator" },
          { label: SS("menu.select_all"), enabled: !!ef.canSelectAll, click: () => wc.selectAll() },
        );
      } else if (p.selectionText && p.selectionText.trim()) {
        items.push({ label: SS("menu.copy"), click: () => wc.copy() });
      }
      if (p.linkURL) {
        if (items.length) items.push({ type: "separator" });
        items.push({ label: SS("ctx.copy_link"), click: () => clipboard.writeText(p.linkURL) });
      }
      if (p.mediaType === "image" && p.srcURL) {
        if (items.length) items.push({ type: "separator" });
        items.push({ label: SS("ctx.copy_image"), click: () => { try { wc.copyImageAt(p.x, p.y); } catch (err) { /* ignore */ } } });
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
    // 开机首帧防白闪（splash P0 2026-08-22）：深空底色（=brand --bl-ink-950）+
    // show:false 等 ready-to-show 再亮窗——双击到品牌首屏之间不再闪系统白底。
    backgroundColor: "#05060f",
    show: false,
    // 应用菜单内迁（P0 2026-08-22）：黑色原生菜单条默认隐藏，工作台顶栏渲染同构
    // 页内菜单（desktop:app-menu-spec/-action）。原生 ApplicationMenu 保留＝快捷键
    // role 全部照旧 + **Alt 可唤出**＝白屏时的应急后门（帮助>上传诊断仍可达）。
    // 刻意只用 autoHideMenuBar、不叠 setMenuBarVisibility(false)——后者连 Alt 唤出
    // 也封死，应急后门就没了。
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "shell-preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      webviewTag: true,
    },
  };
  // ── 融合标题栏（titlebar merge P2 2026-08-22）────────────────────────────
  // win32 隐藏系统标题栏：原生 min/max/close 以 overlay 悬浮在壳级 32px 细条
  // （renderer #cx-titlebar，经 ?tb=1 点亮）右缘——三层 chrome（系统标题+菜单条+
  // 页头）收敛成「细条+页头」一体品牌头。overlay 配色随工作台页面主题经
  // menuAction(theme_light|theme_dark) 运行时 setTitleBarOverlay 换肤。
  // mac 刻意保持原生标题栏（坐席机全为 Windows；mac 红绿灯语义另案）。
  const TB_MERGED = process.platform === "win32";
  if (TB_MERGED) {
    winOpts.titleBarStyle = "hidden";
    winOpts.titleBarOverlay = { color: "#191b21", symbolColor: "#e2e8f0", height: 32 };
  }
  try {
    if (fs.existsSync(DEFAULT_BRAND_ICON)) winOpts.icon = DEFAULT_BRAND_ICON;
  } catch (e) { /* 图标缺失不阻断启动 */ }
  // 小逻辑桌面自适配（2026-08-17 .173：4K@300% 逻辑桌面 1280x720 矮于固定 820 →
  // composer 工具栏永久在折叠线下）。clamp 进工作区，放不下则建窗后最大化；
  // 取不到 screen 一律保持固定尺寸旧行为。详见 desktop/win-fit.js。
  let _fit = null;
  try {
    const { screen } = require("electron");
    _fit = winFit.fitWindowBounds(screen.getPrimaryDisplay().workAreaSize, winOpts);
    winOpts.width = _fit.width;
    winOpts.height = _fit.height;
  } catch (e) { /* 自适配失败不阻断开窗 */ }
  const win = new BrowserWindow(winOpts);
  // 坐席工作台路由锚点（focusMainInbox 用）：主窗即统一收件箱的唯一容器
  mainSeatWin = win;
  win.on("closed", () => { if (mainSeatWin === win) mainSeatWin = null; });
  // 融合细条×全屏（titlebar merge P2b）：F11/视图菜单进全屏后原生窗控自动消失，
  // 32px 细条再常驻就是纯浪费——通知壳 renderer 收起（cx-tb-fs），退出全屏还原。
  if (TB_MERGED) {
    const _tbFs = (on) => { try { win.webContents.send("cx-titlebar-fs", !!on); } catch (e) { /* 窗已销毁等边角，忽略 */ } };
    win.on("enter-full-screen", () => _tbFs(true));
    win.on("leave-full-screen", () => _tbFs(false));
  }
  // show:false + ready-to-show：首帧渲染完成才亮窗。maximize 挪进回调——Windows 上
  // 对隐藏窗调 maximize 会立刻强制显示，提前调等于白闪回归。
  const _wantMax = !!(_fit && _fit.maximize);
  const _revealWin = () => {
    try {
      if (win.isDestroyed() || win.isVisible()) return;
      if (_wantMax) win.maximize(); // maximize 在 Windows 上自带 show
      win.show();
    } catch (e) { /* 亮窗失败不阻断启动 */ }
  };
  win.once("ready-to-show", _revealWin);
  // 兜底：渲染进程异常（资源缺失/崩溃）时 ready-to-show 永不触发——4s 后强制亮窗，
  // 宁可看到错误界面也不能「双击后无事发生」。
  setTimeout(_revealWin, 4000);

  win.webContents.on("did-finish-load", () => {
    console.log("[diag] renderer loaded ok");
    applyLiveWindowBranding(win, { force: true });
  });
  // 运行中改了 logo 无需重开窗口：切回桌面壳时（节流后）重取品牌热替换图标。
  win.on("focus", () => applyLiveWindowBranding(win));
  // ── B69（实施67 P2-j，`_336`）：菜单认领看门狗 ─────────────────────────
  // 原生菜单条 autoHideMenuBar 隐藏的前提＝页内菜单（app-menu-spec）会接管；
  // 更新装载后的混装/加载失败/白屏态页面没接管时＝两头落空（顶部无任何菜单，
  // 重启才恢复）。窗起 30s 内菜单未被认领 → 把原生菜单条还回来（Alt 后门升级
  // 为常驻可见）；页面随后认领 → 收回隐藏。did-fail-load 立即还原生条。
  const _menuBarFallback = (on) => {
    try {
      if (win.isDestroyed()) return;
      win.setMenuBarVisibility(!!on ? true : false);
      win.setAutoHideMenuBar(!on);
      if (on) console.log("[diag] app-menu watchdog: native menu bar restored");
    } catch (e) { /* 菜单条切换失败不伤主链 */ }
  };
  _onAppMenuClaimed = () => _menuBarFallback(false);
  const _menuWatchTimer = setTimeout(() => {
    if (!_appMenuClaimed) _menuBarFallback(true);
  }, 30000);
  if (_menuWatchTimer.unref) _menuWatchTimer.unref();
  win.webContents.on("did-fail-load", () => {
    if (!_appMenuClaimed) _menuBarFallback(true);
  });
  win.webContents.on("did-fail-load", (_e, code, desc) =>
    rendererDiagLog(`[diag] renderer load FAILED ${code} ${desc}`));
  win.webContents.on("render-process-gone", (_e, d) =>
    rendererDiagLog(`[diag] render process gone: ${JSON.stringify(d)}`));
  win.webContents.on("console-message", (_e, _lvl, msg) =>
    rendererDiagLog(`[renderer] ${msg}`));
  // webview 子 webContents 在 Electron 默认是 sandboxed（与父窗口 sandbox:false 无关），
  // 沙箱内 preload 只能 require electron，无法 require 本地模块（./profiles.js / ./media-format.js）→
  // 注入脚本 tg-inject.js 整体加载失败「module not found: ./profiles.js」。这里对内嵌 webview 关闭
  // 沙箱，使 preload 能加载选择器档案/媒体格式化模块（DOM 注入与 ipcRenderer 不受影响）。
  win.webContents.on("will-attach-webview", (_e, webPreferences) => {
    webPreferences.sandbox = false;
  });
  win.webContents.setWindowOpenHandler(makeBackendPopupHandler());
  wireEditContextMenu(win.webContents);   // 主窗 chrome（首跑向导等原生输入件）
  wireUnloadGuard(win.webContents, win);  // #193
  win.webContents.on("did-attach-webview", (_e, wc) => {
    console.log("[diag] webview attached");
    bindWhatsappWebviewUa(wc);
    wireEditContextMenu(wc);   // 官方页 + 工作台 webview：右键粘贴的主战场
    watchWebLangSwitch(wc);    // #151：工作台 /set_lang 切语言 → 壳配置 + 菜单跟随
    wireUnloadGuard(wc, win);  // #193：工作台 webview 漂到后台页时同样不被静默吞
    wc.setWindowOpenHandler(makeBackendPopupHandler());
    wc.on("did-finish-load", () => rendererDiagLog("[diag] webview page loaded"));
    wc.on("did-fail-load", (_e2, code, desc) =>
      rendererDiagLog(`[diag] webview load FAILED ${code} ${desc}`));
    wc.on("console-message", (_e2, _lvl, msg, line, sourceId) =>
      rendererDiagLog(`[webview] ${msg}${sourceId ? ` (${sourceId}:${line})` : ""}`));
  });

  console.log(`[diag] platforms enabled: ${(config.platforms || []).filter((p) => p.enabled).map((p) => p.id).join(",")}`);
  // ?lang= 把壳语言（shellLang()＝unified_inbox.lang，与应用菜单 SS() 同源）钉进页面
  // URL：renderer/shell-i18n.js 与 shared/copilot/i18n/cp-i18n.js 都从 location.search
  // 取词，于是壳静态文案 + 整条 cp-* 组件链**首帧即正确**，无需异步 IPC 也不会闪中文。
  // ?tb=1＝融合标题栏点亮信号（renderer.initTitlebar 按它加 body.cx-tb-on）：主进程
  // 是「开没开融合」的唯一权威，renderer 不做平台猜测；lang 必须保持首位（i18n 门禁钉）。
  win.loadFile(path.join(__dirname, "renderer", "index.html"), { query: { lang: shellLang(), tb: TB_MERGED ? "1" : "0" } });
  if (process.argv.includes("--dev")) win.webContents.openDevTools({ mode: "detach" });
}

// 「关于」弹窗（应用菜单内迁 P0 2026-08-22 抽出为共用函数：原生帮助菜单与
// 工作台顶栏帮助下拉两个入口同一实现，防止页内菜单再手搓一份漂移版）。
async function showAboutDialog(w) {
  const bi = await resolveBrand();
  const ver = displayVersion();
  // 机器码拿不到就整行不出现（宁可少一行，也别显示一个空标签让人以为坏了）
  const sup = await fetchSupportInfo();
  const mc = (sup && sup.machine_code) ? String(sup.machine_code) : "";
  const detail =
    `Telegram / WhatsApp / Messenger / LINE · ${SS("about.tagline")}\n\n` +
    `${SS("about.version")} v${ver}  ·  Electron ${process.versions.electron}  ·  Chromium ${process.versions.chrome}\n` +
    (mc ? `${SS("about.machine")}: ${mc}\n` : "") +
    `${bi.company} · ${bi.website}`;
  // 按钮按能力动态拼，并**按 id 派发**而不是按下标——下标算式在增删按钮时
  // 是静默错位（点「复制」变成打开官网），id 派发让新增按钮零风险。
  // 「复制信息」：用户对着弹窗手抄机器码是最容易抄错的一步，抄错＝客服查不到
  // 这台机器。「上传诊断」只在后端确认支持时出现（sup 非空即两个端点都在）。
  const acts = [{ id: "visit", label: SS("about.visit") }];
  if (mc) acts.push({ id: "copy", label: SS("about.copy") });
  if (sup && sup.upload) acts.push({ id: "diag", label: SS("about.diag") });
  acts.push({ id: "close", label: SS("about.close") });
  const res = await dialog.showMessageBox(w, {
    type: "info",
    title: `${SS("about.title_prefix")} ${bi.product}`,
    message: `${bi.product} · ${SS("about.workbench")}`,
    detail,
    buttons: acts.map((a) => a.label),
    defaultId: acts.length - 1,
    cancelId: acts.length - 1,
    noLink: true,
  });
  const act = (acts[res.response] || {}).id;
  if (act === "visit" && bi.website) {
    shell.openExternal(bi.website).catch(() => {});
  } else if (act === "copy") {
    try { clipboard.writeText(`${bi.product} v${ver}\n${SS("about.machine")}: ${mc}`); } catch (e) { /* 剪贴板不可用时静默 */ }
  } else if (act === "diag") {
    await runDiagFlow(w, bi.product, ver, "desktop-about");
  }
}

// 应用菜单（i18n P0 2026-08-19：原 buildChineseMenu 硬编码中文 → 按壳语言双语；
// 保留 role 以维持快捷键与原生行为。文案单源=SHELL_STR，勿再写字面量）
function buildAppMenu() {
  const template = [
    {
      label: SS("menu.file"),
      submenu: [
        { label: SS("menu.reload"), role: "reload" },
        { label: SS("menu.force_reload"), role: "forceReload" },
        { type: "separator" },
        { label: SS("menu.quit"), role: "quit" },
      ],
    },
    {
      label: SS("menu.edit"),
      submenu: [
        { label: SS("menu.undo"), role: "undo" },
        { label: SS("menu.redo"), role: "redo" },
        { type: "separator" },
        { label: SS("menu.cut"), role: "cut" },
        { label: SS("menu.copy"), role: "copy" },
        { label: SS("menu.paste"), role: "paste" },
        { label: SS("menu.select_all"), role: "selectAll" },
      ],
    },
    {
      label: SS("menu.view"),
      submenu: [
        { label: SS("menu.zoom_reset"), role: "resetZoom" },
        { label: SS("menu.zoom_in"), role: "zoomIn" },
        { label: SS("menu.zoom_out"), role: "zoomOut" },
        { type: "separator" },
        { label: SS("menu.fullscreen"), role: "togglefullscreen" },
        // 开发者工具对所有人隐藏（老板令 2026-08-22）：菜单项与 Ctrl+Shift+I role
        // 快捷键一并消失；工程调试走 --dev 启动参，坐席解锁走页内「版本号连点 12 次」
        // 开发者模式（appMenuSpec extras 供词条，页面注入菜单项经 desktop:app-menu-action
        // 的 devtools 分支执行——分支保留，入口全部收进解锁态）。
      ],
    },
    {
      label: SS("menu.window"),
      submenu: [
        { label: SS("menu.minimize"), role: "minimize" },
        { label: SS("menu.close_win"), role: "close" },
      ],
    },
    {
      label: SS("menu.help"),
      submenu: [
        {
          label: SS("menu.about"),
          click: () => showAboutDialog(
            BrowserWindow.getFocusedWindow() || BrowserWindow.getAllWindows()[0]).catch(() => {}),
        },
        {
          // 报障直达（实施49 P1-9）：白屏/卡死时页面内的入口一并没了，
          // 「帮助」菜单是那时唯一还点得动的地方，别逼用户先绕进「关于」。
          label: SS("menu.diag"),
          click: async () => {
            const w = BrowserWindow.getFocusedWindow() || BrowserWindow.getAllWindows()[0];
            const bi = await resolveBrand();
            await runDiagFlow(w, bi.product, displayVersion(), "desktop-help-menu");
          },
        },
        {
          label: SS("menu.check_update"),
          click: () => checkForUpdatesManual(
            BrowserWindow.getFocusedWindow() || BrowserWindow.getAllWindows()[0]),
        },
      ],
    },
  ];
  return Menu.buildFromTemplate(template);
}

// ── 壳语言跟随工作台切换（#151，2026-09-02）───────────────────────────────────
// 原图 _1086：切英文后界面主体全英，唯原生菜单条 + 帮助下拉仍中文。词条早就走
// SHELL_STR——问题是 SS() 读的 unified_inbox.lang 只由首启向导写，网页里 /set_lang
// 只改后端 cookie，壳配置停在装机那一刻；加之 Menu 是启动期一次性构建。
// 修：监听后台页 webContents 导航，命中 /set_lang?lang=<x>（页面切语言就是这一次
// 整页跳转）→ ①同步壳配置（下次 loadFile 的 ?lang= 与页面 cookie 从此一致）
// ②重建 Menu ③广播 cx-shell-lang 给壳 renderer 重取静态词。页内五菜单随页面重载
// 自会重拉 menuSpec()。纯函数决策在 shell-lang-sync.js（门禁直跑）。
function rebuildAppMenu() {
  try { Menu.setApplicationMenu(buildAppMenu()); } catch (e) {
    console.log("[i18n] app menu rebuild failed: " + ((e && e.message) || e));
  }
}
function applyShellLangFromNavigation(rawUrl) {
  let patch = null;
  try {
    patch = shellLangSync.shellLangPatchForNavigation(
      (((config || {}).unified_inbox) || {}).lang, rawUrl);
  } catch (e) { patch = null; }
  if (!patch) return false;
  const before = shellLang();
  saveConfigPatch({ unified_inbox: { lang: patch.lang } });
  const after = shellLang();
  console.log(`[i18n] shell lang synced from workspace: ${JSON.stringify(patch.lang)} (${before} -> ${after})`);
  if (after !== before) rebuildAppMenu();
  for (const w of BrowserWindow.getAllWindows()) {
    try { if (!w.isDestroyed()) w.webContents.send("cx-shell-lang", after); } catch (e) { /* 窗已亡 */ }
  }
  return true;
}
function watchWebLangSwitch(wc) {
  const onNav = (_e, url) => {
    try { if (isBackendUrl(url)) applyShellLangFromNavigation(url); } catch (e) { /* 非后台页/坏 URL 忽略 */ }
  };
  // will-navigate=页面发起的跳转（location.href），did-start-navigation 兜住重定向/
  // 编程式导航；两者都可能触发，patch 同值不重写（shell-lang-sync 内去重）。
  wc.on("will-navigate", onNav);
  wc.on("did-start-navigation", (_e, url, _inPlace, isMainFrame) => {
    if (isMainFrame === false) return;
    onNav(_e, url);
  });
}

// ── 应用菜单内迁（P0 2026-08-22）────────────────────────────────────────────
// 黑色原生菜单条已在主窗 autoHideMenuBar 隐藏（Alt 唤出=应急后门），工作台顶栏
// 经 __chatxShell.menuSpec() 取本清单渲染页内五菜单（文件/编辑/视图/窗口/帮助），
// 动作经 desktop:app-menu-action 回主进程执行。词条单源=SHELL_STR（与原生菜单同
// 一词典，随壳启动语言）；页面零兜底文案=无双源。帮助组的 support 带 local:1＝
// 页面本地动作（openSupportPanel 报障面板），不回主进程；原生 diag 流刻意不进
// 页内菜单——页面活着时报障面板严格更好，白屏时 Alt 唤出的原生帮助里 diag 仍在。
function appMenuSpec() {
  const it = (id, key, accel) => ({ id, label: SS(key), accel: accel || "" });
  return {
    ok: true,
    lang: shellLang(),
    menus: [
      { id: "file", label: SS("menu.file"), items: [
        it("reload", "menu.reload", "Ctrl+R"),
        it("force_reload", "menu.force_reload", "Ctrl+Shift+R"),
        { type: "sep" },
        it("quit", "menu.quit", "Alt+F4"),
      ] },
      { id: "edit", label: SS("menu.edit"), items: [
        it("undo", "menu.undo", "Ctrl+Z"),
        it("redo", "menu.redo", "Ctrl+Y"),
        { type: "sep" },
        it("cut", "menu.cut", "Ctrl+X"),
        it("copy", "menu.copy", "Ctrl+C"),
        it("paste", "menu.paste", "Ctrl+V"),
        it("select_all", "menu.select_all", "Ctrl+A"),
      ] },
      { id: "view", label: SS("menu.view"), items: [
        it("zoom_reset", "menu.zoom_reset", "Ctrl+0"),
        it("zoom_in", "menu.zoom_in", "Ctrl++"),
        it("zoom_out", "menu.zoom_out", "Ctrl+-"),
        { type: "sep" },
        it("fullscreen", "menu.fullscreen", "F11"),
        // devtools 不进默认清单（对所有人隐藏，老板令 2026-08-22）：页面在
        // 「开发者模式」解锁态（帮助菜单版本号连点 12 次）用 extras 词条自行注入，
        // 动作仍走 desktop:app-menu-action 的 devtools 分支。
      ] },
      { id: "window", label: SS("menu.window"), items: [
        it("minimize", "menu.minimize", ""),
        it("close_win", "menu.close_win", ""),
      ] },
      { id: "help", label: SS("menu.help"), items: [
        Object.assign(it("support", "menu.support", ""), { local: 1 }),
        it("about", "menu.about", ""),
        it("check_update", "menu.check_update", ""),
      ] },
    ],
    // 版本与开发者模式词条（2026-08-22）：帮助菜单底部由页面渲染版本行，
    // 连点 12 次解锁「开发者模式」（localStorage，页面侧注入 devtools 菜单项）。
    // 词条全部单源 SHELL_STR——页面零兜底文案，与五菜单同一纪律。
    version: displayVersion(),
    extras: {
      version_label: SS("about.version"),
      devtools: SS("menu.devtools"),
      devmode_on: SS("menu.devmode_on"),
      devmode_off: SS("menu.devmode_off"),
      devmode_hint: SS("menu.devmode_hint"),
    },
  };
}

// B69（实施67 P2-j，`_336` 实录：更新装完顶部菜单栏消失、重启可恢复）：
// 「菜单认领」信号——页面调 app-menu-spec ＝ 页内菜单已接管。主窗的菜单看门狗
// 据此决定要不要把原生菜单条还回来（见 createWindow 内 _menuClaimWatchdog）。
let _appMenuClaimed = false;
let _onAppMenuClaimed = null;

ipcMain.handle("desktop:app-menu-spec", (e) => {
  // 纵深防御：只有后端工作台页可取（与 desktop:copilot-pip 同款闸）。
  try { if (!isBackendUrl(e.sender.getURL())) return { ok: false, error: "forbidden" }; }
  catch (err) { return { ok: false, error: "forbidden" }; }
  _appMenuClaimed = true;
  try { if (_onAppMenuClaimed) _onAppMenuClaimed(); } catch (err) { /* 看门狗回调异常不伤主链 */ }
  return appMenuSpec();
});

ipcMain.handle("desktop:app-menu-action", (e, rawId) => {
  try { if (!isBackendUrl(e.sender.getURL())) return { ok: false, error: "forbidden" }; }
  catch (err) { return { ok: false, error: "forbidden" }; }
  const id = String(rawId || "");
  const wc = e.sender;
  // webview 场景 hostWebContents=壳 renderer，据此找宿主窗（窗控/全屏/退出用）。
  const host = wc.hostWebContents || wc;
  const win = BrowserWindow.fromWebContents(host)
    || BrowserWindow.getFocusedWindow() || BrowserWindow.getAllWindows()[0];
  try {
    switch (id) {
      // 刷新/缩放/DevTools 作用于**发起页 webContents**（坐席眼里的「页面」就是
      // 工作台 webview）：原生 role 是对壳整窗操作——重载会连官方网页标签一起
      // 重载（掉登录检查）、缩放只缩壳 chrome 缩不到 webview 内容，页内菜单按
      // 坐席直觉收敛到本页。编辑类显式打发起页＝wireEditContextMenu 同教训
      // （role 依赖焦点窗语义，webview 场景会打错目标）。
      case "reload": wc.reload(); return { ok: true };
      case "force_reload": wc.reloadIgnoringCache(); return { ok: true };
      case "quit": app.quit(); return { ok: true };
      case "undo": wc.undo(); return { ok: true };
      case "redo": wc.redo(); return { ok: true };
      case "cut": wc.cut(); return { ok: true };
      case "copy": wc.copy(); return { ok: true };
      case "paste": wc.paste(); return { ok: true };
      case "select_all": wc.selectAll(); return { ok: true };
      case "zoom_reset": wc.setZoomLevel(0); return { ok: true };
      case "zoom_in": wc.setZoomLevel(Math.min(wc.getZoomLevel() + 0.5, 5)); return { ok: true };
      case "zoom_out": wc.setZoomLevel(Math.max(wc.getZoomLevel() - 0.5, -5)); return { ok: true };
      case "fullscreen": if (win) win.setFullScreen(!win.isFullScreen()); return { ok: true };
      case "devtools": wc.toggleDevTools(); return { ok: true };
      case "minimize": if (win) win.minimize(); return { ok: true };
      case "close_win": if (win) win.close(); return { ok: true };
      case "about": showAboutDialog(win).catch(() => {}); return { ok: true };
      case "check_update": checkForUpdatesManual(win); return { ok: true };
      // 融合标题栏主题跟随（titlebar merge P2 2026-08-22）：工作台页面主题翻转时
      // 上报，这里同步换 ① 原生窗控 overlay 配色（win32 融合窗才有，异常静默）
      // ② 壳级细条配色（cx-titlebar-theme → renderer 翻 body.cx-tb-light）。
      // 色值与页面 ws-top 暗/亮两档同源（#191b21 渐变主段 / #ffffff + 深 ink）。
      case "theme_dark":
      case "theme_light": {
        const mode = id === "theme_light" ? "light" : "dark";
        try {
          if (win && typeof win.setTitleBarOverlay === "function") {
            win.setTitleBarOverlay(mode === "light"
              ? { color: "#ffffff", symbolColor: "#334155", height: 32 }
              : { color: "#191b21", symbolColor: "#e2e8f0", height: 32 });
          }
        } catch (err) { /* 非融合窗（mac/后台弹窗）无 overlay，忽略 */ }
        try { if (host && host !== wc) host.send("cx-titlebar-theme", mode); } catch (err) { /* 细条缺席不阻断 */ }
        return { ok: true };
      }
      default: return { ok: false, error: "unknown_action" };
    }
  } catch (err) {
    return { ok: false, error: String((err && err.message) || err) };
  }
});

// ── 融合标题栏「⋯」应急菜单（titlebar merge P2 2026-08-22）─────────────────
// 调用方＝壳级细条（renderer #cx-titlebar，file:// 页）。工作台 webview 白屏时
// 页内帮助菜单一并死掉，这条链是那时唯一还点得动的报障/更新入口——接替被
// autoHideMenuBar 藏起的原生帮助菜单。只放行 file:// 发起方（后端页走
// desktop:app-menu-action，不共用本口）。
ipcMain.handle("desktop:titlebar-menu", async (e, rawId) => {
  try { if (!String(e.sender.getURL() || "").startsWith("file://")) return { ok: false, error: "forbidden" }; }
  catch (err) { return { ok: false, error: "forbidden" }; }
  const id = String(rawId || "");
  const win = BrowserWindow.fromWebContents(e.sender)
    || BrowserWindow.getFocusedWindow() || BrowserWindow.getAllWindows()[0];
  try {
    if (id === "about") { showAboutDialog(win).catch(() => {}); return { ok: true }; }
    if (id === "diag") {
      const bi = await resolveBrand();
      await runDiagFlow(win, bi.product, displayVersion(), "desktop-titlebar-menu");
      return { ok: true };
    }
    if (id === "update") { checkForUpdatesManual(win); return { ok: true }; }
    return { ok: false, error: "unknown_action" };
  } catch (err) {
    return { ok: false, error: String((err && err.message) || err) };
  }
});

// ── 更新与公告通知中心（P0 2026-08-14）─────────────────────────────────────
// 决策纯函数在 update-notify.js（Node 直跑可测）；这里只做 IO：updater 事件、
// 公告 HTTP 拉取（含本地缓存）、已读/稍后状态落盘、向 renderer 广播当前通知。
// 旧行为（更新下载完只写日志、下次重启才生效）升级为：横幅 +「立即重启更新」一键完成。
const updateNotify = require("./update-notify.js");

// #70-②（0830 值守自察）：壳侧 renderer/webview console 落盘。打包态主进程
// console.log 没有去处，[renderer]/[webview] 取证线一直在丢——#67 那类「请求
// 根本没到后端」的前端层故障，诊断包收不到第一现场。落 userData/logs/
// renderer.log（与 backend.log 同目录，diag_upload.shell_backend_log_files
// 一并收进诊断包），10MB 轮转一份；写盘失败绝不影响壳运行。
const RENDERER_LOG_MAX_BYTES = 10 * 1024 * 1024;
function rendererDiagLog(line) {
  console.log(line);   // 开发态行为不变（终端仍可见）
  try {
    const dir = path.join(app.getPath("userData"), "logs");
    const fp = path.join(dir, "renderer.log");
    try { fs.mkdirSync(dir, { recursive: true }); } catch (_e) { /* noop */ }
    try {
      const st = fs.statSync(fp);
      if (st && st.size > RENDERER_LOG_MAX_BYTES) {
        try { fs.renameSync(fp, `${fp}.1`); } catch (_e) { /* noop */ }
      }
    } catch (_e) { /* 首写无文件属正常 */ }
    fs.appendFileSync(fp, `${new Date().toISOString()} ${String(line)}\n`);
  } catch (_e) { /* 诊断通道绝不反噬壳 */ }
}

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
/** 升级安装前的有序停机：先等旧后端真死（释放 pyrogram 会话文件），再交给
 *  NSIS 安装器。顺序倒过来就是 B57 升级风暴：装完自动拉起的新后端与没死透的
 *  旧后端并存，同一份会话双连 → Telegram 强制注销全部账号。 */
async function installUpdateNow(u) {
  await shutdownBackendAndWait();
  try { u.quitAndInstall(false, true); } catch (e) { console.log(`[updater] quitAndInstall: ${String((e && e.message) || e)}`); }
}

ipcMain.handle("desktop:update-restart", () => {
  // 热补丁与整包更新共用同一个「已就绪」横幅，但落地方式完全不同（整包交 NSIS，
  // 热补丁交 helper 替换 resources\）——路由错了就是装错东西，故先判热补丁。
  if (_hotpatchReady) {
    const hp = _hotpatchReady;
    // 停后端之前先确认暂存还在（杀软清理/用户手删 userData 都可能把它拿走）：
    // 等停完再发现没得装，用户就只剩一个没有后端的空壳。
    if (!_hotpatchFilesPresent(hp)) {
      console.log("[hotpatch] 暂存文件已不在，取消本次热补丁（旧文件原样保留）");
      _discardStagedHotpatch("staging_missing");
      broadcastShellNotice();
      return { ok: false, error: "not_ready" };
    }
    setImmediate(() => { void installHotpatchNow(hp); });
    return { ok: true };
  }
  const u = _getUpdater();
  if (!u || _updateInfo.phase !== "downloaded") return { ok: false, error: "not_ready" };
  // setImmediate：先把 IPC 应答送回 renderer（按钮已进「正在重启…」态），再触发退出流程。
  setImmediate(() => { void installUpdateNow(u); });
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
      // 整包永远优先：新 semver 里本就含着热补丁那点改动，且装完 base 就对不上了。
      // 已暂存的热补丁在这里作废，否则「点重启」会走进热补丁分支装错东西。
      _latestFullVersion = String((info && info.version) || _latestFullVersion);
      _discardStagedHotpatch("full_update_available");
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
      _latestFullVersion = String((info && info.version) || _latestFullVersion);
      _discardStagedHotpatch("full_update_ready");
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
    const r = await u.checkForUpdates();
    // 记下 latest.yml 上的整包版本：热补丁决策要靠它让路（比 update-available
    // 事件更早也更全——「已是最新」时不发事件，但这里同样拿得到版本号）。
    const v = r && r.updateInfo && r.updateInfo.version;
    if (v) _latestFullVersion = String(v);
    return r;
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
      setImmediate(() => { void installUpdateNow(u); });
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

// ── 热补丁拉取（P0 2026-08-27）─────────────────────────────────────────────
// 与 electron-updater 的分工：整包（新 semver）永远优先，热补丁只在**同一
// baseAppVersion 内**替换 asar / inject / sidecar 这类可替换文件——几十 MB、不过
// NSIS、不碰用户数据，用来把「改一行 JS 要等一次 400MB 发版」压成一次重启。
// 决策纯函数在 hotpatch-apply.js，落地纯函数在 hotpatch-stage.js；这里只做 IO。
//
// 刻意复用现有「立即重启更新」横幅（_updateInfo.phase="downloaded"）而不另起一套：
// 对用户这两件事是同一件事——「点一下，重启，就好了」；多一条通知只会稀释注意力。
//
// 失败纪律：任何一步不过一律保持旧文件、横幅不出现（或消失），日志留 [hotpatch]。
// 最坏结果必须是「没更新」，不能是「应用坏掉」。
const HOTPATCH_MAX_BYTES = 512 * 1024 * 1024; // manifest 撒谎也不至于把磁盘吃穿

let _hotpatchReady = null; // 校验通过、等用户点重启的暂存件
let _hotpatchBusy = false;
let _latestFullVersion = ""; // 最近一次 updater 检查看到的整包版本（让路判据）

function _hotpatchDir() { return path.join(app.getPath("userData"), "hotpatch-staging"); }

/** 指针源：与更新源同域（publish url + hotpatch.json）；config.updates 可覆写/关停。 */
function _hotpatchUrl() {
  try {
    const cfg = (config || {}).updates || {};
    if (cfg.hotpatch === false) return ""; // 一键关停（与 telemetry 同款开关语义）
    if (cfg.hotpatch_url) return String(cfg.hotpatch_url);
  } catch (e) { /* config 未就绪按默认 */ }
  try {
    return hotpatchApply.hotpatchUrlFromPublish(require("./package.json").build.publish[0].url);
  } catch (e) { return ""; }
}

/** 落地脚本：随包镜像（copy-shared 从 deploy/desktop 同步）优先，开发树兜底。 */
function _hotpatchHelperScript() {
  const cands = [
    path.join(__dirname, "hotpatch", "apply_chatx_hotpatch_node.ps1"),
    path.resolve(__dirname, "..", "..", "..", "deploy", "desktop", "apply_chatx_hotpatch_node.ps1"),
  ];
  for (const p of cands) {
    try { if (fs.existsSync(p)) return p; } catch (e) { /* 下一个候选 */ }
  }
  return "";
}

function _sha256File(p) {
  return new Promise((resolve) => {
    try {
      const h = require("crypto").createHash("sha256");
      const rs = fs.createReadStream(p);
      rs.on("data", (c) => h.update(c));
      rs.on("error", () => resolve(""));
      rs.on("end", () => resolve(h.digest("hex")));
    } catch (e) { resolve(""); }
  });
}

function _psQuote(s) { return "'" + String(s).replace(/'/g, "''") + "'"; }

/** 解包：与 helper 同一个 .NET API（ZipFile.ExtractToDirectory 自带 zip-slip 拒绝）。 */
function _extractZip(zipPath, destDir) {
  return new Promise((resolve) => {
    try {
      const cmd = "$ErrorActionPreference='Stop'; Add-Type -AssemblyName System.IO.Compression.FileSystem; "
        + `[IO.Compression.ZipFile]::ExtractToDirectory(${_psQuote(zipPath)}, ${_psQuote(destDir)})`;
      const child = spawn("powershell", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", cmd], { windowsHide: true });
      let err = "";
      child.stderr.on("data", (d) => { err += String(d); });
      child.on("error", (e) => { console.log(`[hotpatch] 解包进程起不来：${String((e && e.message) || e)}`); resolve(false); });
      child.on("close", (code) => {
        if (code === 0) return resolve(true);
        console.log(`[hotpatch] 解包失败 exit=${code} ${err.trim().slice(0, 300)}`);
        resolve(false);
      });
    } catch (e) { console.log(`[hotpatch] 解包异常：${String((e && e.message) || e)}`); resolve(false); }
  });
}

/** 流式下载并算整包 sha256（边下边算，不把整包读进内存）。 */
async function _downloadZip(url, dest, expectSha, expectBytes) {
  const { Readable } = require("stream");
  const { pipeline } = require("stream/promises");
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 15 * 60 * 1000);
  try {
    const r = await fetch(url, { signal: ctl.signal, cache: "no-store" });
    if (!r || !r.ok || !r.body) { console.log(`[hotpatch] 下载失败 HTTP ${r && r.status}`); return false; }
    const h = require("crypto").createHash("sha256");
    let total = 0;
    await pipeline(
      Readable.fromWeb(r.body),
      async function* (src) {
        for await (const c of src) {
          total += c.length;
          if (total > HOTPATCH_MAX_BYTES) throw new Error("zip over size cap");
          h.update(c);
          yield c;
        }
      },
      fs.createWriteStream(dest),
    );
    const got = h.digest("hex");
    if (got !== String(expectSha || "").toLowerCase()) {
      console.log(`[hotpatch] 整包 sha256 不符 expect=${expectSha} got=${got}`);
      return false;
    }
    if (expectBytes > 0 && total !== expectBytes) {
      console.log(`[hotpatch] 整包大小不符 expect=${expectBytes} got=${total}`);
      return false;
    }
    console.log(`[hotpatch] 已下载 ${(total / 1048576).toFixed(1)}MB，整包 sha256 通过`);
    return true;
  } catch (e) {
    console.log(`[hotpatch] 下载异常：${String((e && e.message) || e)}`);
    return false;
  } finally { clearTimeout(timer); }
}

/**
 * 下载 + 全套校验 + 备好 helper。任一步不过返回 null（调用方保持旧文件）。
 * 校验顺序刻意是「先便宜后昂贵」：manifest 自洽性 → 整包哈希 → 解包 → 逐文件哈希。
 */
async function _stageHotpatch(remote, manifestUrl, rawManifest) {
  const zipUrl = hotpatchStage.zipUrlFrom(manifestUrl, remote.zip);
  if (!zipUrl) { console.log(`[hotpatch] zip 地址非法或跨域，放弃：${remote.zip}`); return null; }
  if (!hotpatchStage.isSha256(remote.sha256)) { console.log("[hotpatch] manifest 缺整包 sha256，放弃"); return null; }
  const noHash = hotpatchStage.filesMissingHash(remote);
  if (noHash.length) { console.log(`[hotpatch] manifest 有 ${noHash.length} 个文件缺 sha256，放弃`); return null; }
  if (hotpatchStage.manifestTampered(rawManifest, remote)) {
    console.log("[hotpatch] manifest 含越权/重复路径（规范化后条目数对不上），整份拒绝——半套补丁比不套更危险");
    return null;
  }
  const script = _hotpatchHelperScript();
  if (!script) { console.log("[hotpatch] 落地脚本缺失（copy-shared 未同步？），放弃"); return null; }

  const names = hotpatchStage.stagedNames(remote.zip);
  const dir = _hotpatchDir();
  try {
    fs.rmSync(dir, { recursive: true, force: true }); // 上一轮残留：暂存永远只留一份
    fs.mkdirSync(dir, { recursive: true });
  } catch (e) { console.log(`[hotpatch] 暂存目录不可用：${String((e && e.message) || e)}`); return null; }

  const zipPath = path.join(dir, names.zip);
  if (!(await _downloadZip(zipUrl, zipPath, remote.sha256, remote.size_bytes))) return null;

  const unpacked = path.join(dir, names.unpacked);
  if (!(await _extractZip(zipPath, unpacked))) return null;

  // 逐文件：路径白名单再确认一次（不单点信任 normalizeManifest）+ 内容哈希逐字节对上。
  // 这一轮在**用户看到横幅之前**跑完：横幅出现 = 这份补丁已经整体可信。
  for (const f of remote.files) {
    if (!hotpatchApply.isSafeRelPath(f.path)) { console.log(`[hotpatch] 路径越权：${f.path}`); return null; }
    const abs = path.join(unpacked, ...f.path.split("/"));
    let st = null;
    try { st = fs.statSync(abs); } catch (e) { /* 下面统一报缺失 */ }
    if (!st || !st.isFile()) { console.log(`[hotpatch] zip 内缺文件：${f.path}`); return null; }
    if (f.bytes > 0 && st.size !== f.bytes) { console.log(`[hotpatch] 文件大小不符：${f.path}`); return null; }
    const got = await _sha256File(abs);
    if (got !== String(f.sha256 || "").toLowerCase()) { console.log(`[hotpatch] 文件 sha256 不符：${f.path}`); return null; }
  }
  console.log(`[hotpatch] 逐文件校验通过（${remote.files.length} 个）`);

  try {
    // 写**规范化后**的 manifest 而不是原始 JSON：helper 会用同一份白名单再判一次，
    // 两边口径必须逐字一致，否则就是「JS 说能套、PS 说不能」的静默失败。
    fs.writeFileSync(path.join(dir, names.manifest), JSON.stringify(remote, null, 2), "utf-8");
    fs.writeFileSync(path.join(dir, names.runner), hotpatchStage.wrapperScript(), "ascii");
    // readFileSync+writeFileSync 而非 copyFileSync：源在 app.asar 内，asar 虚拟 fs
    // 对前者支持最稳（copy-shared 同款教训）。
    fs.writeFileSync(path.join(dir, names.script), fs.readFileSync(script));
  } catch (e) {
    console.log(`[hotpatch] 暂存落盘失败：${String((e && e.message) || e)}`);
    return null;
  }

  let installDir = "";
  try { installDir = path.dirname(app.getPath("exe")); } catch (e) { /* 让 helper 自己探测 */ }
  return {
    dir,
    zip: zipPath,
    manifest: path.join(dir, names.manifest),
    runner: path.join(dir, names.runner),
    script: path.join(dir, names.script),
    log: path.join(dir, names.log),
    installDir,
    patch: remote.patch,
    version: hotpatchApply.displayLabel(app.getVersion(), remote.patch),
  };
}

function _hotpatchFilesPresent(hp) {
  try {
    return !!hp && [hp.zip, hp.manifest, hp.runner, hp.script].every((p) => fs.existsSync(p));
  } catch (e) { return false; }
}

/** 作废已暂存的热补丁（整包接管 / 暂存丢失）。文件留着，下一轮 stage 会清目录。
 *  why 用 ASCII 代号（与 decide() 的 reason 同一套词汇），只进日志不进 UI。 */
function _discardStagedHotpatch(why) {
  if (!_hotpatchReady) return;
  console.log(`[hotpatch] 暂存作废：${why}`);
  _hotpatchReady = null;
  if (_updateInfo.phase === "downloaded") _updateInfo = { phase: "idle", version: "", percent: 0 };
}

async function runHotpatchCheck(reason) {
  if (!app.isPackaged || process.platform !== "win32") return; // 落地链路是 Windows 专属
  if (_hotpatchBusy || _hotpatchReady) return; // 已就绪 = 等用户重启，别再拉
  if (_updateInfo.phase !== "idle") return; // 整包在下/已就绪：让路，不抢横幅
  const url = _hotpatchUrl();
  if (!url) return;
  _hotpatchBusy = true;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 10000);
  try {
    const r = await fetch(url, { signal: ctl.signal, cache: "no-store" });
    if (!r.ok) return;
    const raw = await r.json();
    const remote = hotpatchApply.normalizeManifest(raw);
    const d = hotpatchApply.decide({
      appVersion: app.getVersion(),
      local: localHotpatchInfo(),
      remote,
      latestFullVersion: _latestFullVersion,
    });
    if (d.action !== "apply") {
      // defer_full / mismatch / skip 一律什么都不做：整包更新链路照常，这里不插手。
      console.log(`[hotpatch] check(${reason}) → ${d.action}/${d.reason}`);
      return;
    }
    console.log(`[hotpatch] check(${reason}) → apply ${remote.baseAppVersion}+p${remote.patch}（${remote.files.length} 个文件）`);
    const ready = await _stageHotpatch(remote, url, raw);
    if (!ready) { console.log("[hotpatch] 暂存未通过，保持旧文件"); return; }
    if (_updateInfo.phase !== "idle") { console.log("[hotpatch] 下载期间整包更新已接管，热补丁让路"); return; }
    _hotpatchReady = ready;
    _updateInfo = { phase: "downloaded", version: ready.version, percent: 100 };
    console.log(`[hotpatch] 已就绪 ${ready.version}，等用户点「立即重启更新」`);
    broadcastShellNotice();
  } catch (e) {
    console.log(`[hotpatch] check(${reason}) 异常：${String((e && e.message) || e)}`);
  } finally {
    clearTimeout(timer);
    _hotpatchBusy = false;
  }
}

/** 落地：先等后端真死（B57），再把替换工作交给脱离进程的 helper。
 *  顺序不能反——装完自动拉起的新后端与没死透的旧后端并存，同一份 pyrogram 会话
 *  双连 → Telegram 强制注销全部账号。helper 另外还会等本进程 PID 消失才动文件
 *  （app.asar 运行中被锁，进程内换不掉）。 */
async function installHotpatchNow(hp) {
  await shutdownBackendAndWait();
  let spawned = false;
  try {
    const child = spawn("powershell", hotpatchStage.wrapperArgs({
      runner: hp.runner,
      shellPid: process.pid,
      script: hp.script,
      zip: hp.zip,
      manifest: hp.manifest,
      log: hp.log,
      installDir: hp.installDir,
    }), { detached: true, stdio: "ignore", windowsHide: true });
    child.unref();
    spawned = true;
    console.log(`[hotpatch] helper 已脱离启动 pid=${child.pid}，本进程退出后开始替换（日志 ${hp.log}）`);
  } catch (e) {
    console.log(`[hotpatch] helper 启动失败：${String((e && e.message) || e)}`);
  }
  if (!spawned) {
    // 后端已经停了，留在这儿只是个空壳：拉起一个干净的旧版本，
    // 让最坏结果停在「没更新」而不是「应用没了」。
    try { app.relaunch(); } catch (e) { /* relaunch 不可用就只能退出 */ }
  }
  try { app.quit(); } catch (e) {
    try { app.exit(0); } catch (e2) { /* 已在退出流程 */ }
  }
}

/** 热补丁轮询（仅打包态 Windows）：启动后 90s 首查 + 每 4h + 唤醒补查。
 *  首查刻意延后：让 setupAutoUpdate 的开机整包检查先把 _latestFullVersion 落下，
 *  否则「其实有整包新版本」的机器会先白下一个马上就要作废的热补丁。 */
function setupHotpatch() {
  if (!app.isPackaged || process.platform !== "win32") return;
  const t0 = setTimeout(() => runHotpatchCheck("boot"), 90 * 1000);
  if (t0.unref) t0.unref();
  const t = setInterval(() => runHotpatchCheck("interval"), 4 * 60 * 60 * 1000);
  if (t.unref) t.unref();
  try {
    let lastResume = 0;
    powerMonitor.on("resume", () => {
      const now = Date.now();
      if (now - lastResume < 5 * 60 * 1000) return; // 连续唤醒去抖
      lastResume = now;
      setTimeout(() => runHotpatchCheck("resume"), 25000); // 排在整包唤醒补查(15s)之后
    });
  } catch (e) { /* powerMonitor 异常不阻断：定时器仍在 */ }
}

/** 公告轮询（dev 也跑：公告展示链路不依赖打包态，便于开发期直接验证）。 */
function setupAnnouncements() {
  refreshAnnouncements();
  const t = setInterval(refreshAnnouncements, 6 * 60 * 60 * 1000);
  if (t.unref) t.unref();
}

// ── 活动海报 feed（P0 2026-08-21：新人 6U 首启海报）────────────────────────
// 与公告同域同模式：官网 public/downloads/campaigns.json——市场改 JSON 即上/下活动，
// 无需发版。选品/频控/资格判定纯函数在 renderer/campaign-model.js（Node 直跑可测）；
// 这里只做 IO：HTTP 拉取（含本地缓存）、展示状态落盘、CTA 外链放行（https 白名单）。
// 展示时序：audience=new_user 依赖 config.onboarding.completed_at ——首启向导没完成
// 天然不弹；完成向导 reload 后的那次进入工作台就是首个展示时机。
const campaignModel = require("./renderer/campaign-model.js");

function _campCachePath() { return path.join(app.getPath("userData"), "campaigns-cache.json"); }
function _campStatePath() { return path.join(app.getPath("userData"), "campaign-state.json"); }

let _campFeed = null; // 懒加载：首次访问读本地缓存（离线也有上次内容）
function _getCampaigns() {
  if (_campFeed === null) {
    _campFeed = campaignModel.cmNormalizeFeed(_loadJsonSoft(_campCachePath(), null));
  }
  return _campFeed.campaigns;
}

let _campState = null; // { shows: {id: {count,last_ts}}, never: {id: true} }
function _getCampState() {
  if (!_campState) {
    const raw = _loadJsonSoft(_campStatePath(), {}) || {};
    _campState = {
      shows: raw.shows && typeof raw.shows === "object" ? raw.shows : {},
      never: raw.never && typeof raw.never === "object" ? raw.never : {},
    };
  }
  return _campState;
}
function _saveCampState() {
  try {
    fs.mkdirSync(path.dirname(_campStatePath()), { recursive: true });
    fs.writeFileSync(_campStatePath(), JSON.stringify(_getCampState(), null, 2), "utf-8");
  } catch (e) { /* 状态丢失最多多看一次海报，不阻断 */ }
}

/** 活动源：更新源同域（publish url + campaigns.json），config.updates.campaigns_url 可覆写。 */
function _campaignsUrl() {
  try {
    const cfgUrl = ((config || {}).updates || {}).campaigns_url;
    if (cfgUrl) return String(cfgUrl);
  } catch (e) { /* config 未就绪按默认 */ }
  try {
    const pub = require("./package.json").build.publish[0].url;
    return pub.replace(/\/+$/, "") + "/campaigns.json";
  } catch (e) { return ""; }
}

let _campRefreshInflight = null; // 首启拉取在途 promise：海报查询等它收尾，消「+3.5s 时 feed 未就绪」竞态

async function refreshCampaigns() {
  const url = _campaignsUrl();
  if (!url) return;
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 10000);
  try {
    const r = await fetch(url, { signal: ctl.signal, cache: "no-store" });
    if (!r.ok) return;
    const raw = await r.json();
    _campFeed = campaignModel.cmNormalizeFeed(raw);
    try {
      fs.writeFileSync(_campCachePath(), JSON.stringify(raw, null, 2), "utf-8");
    } catch (e) { /* 缓存写失败不影响本次展示 */ }
  } catch (e) {
    /* 活动拉取失败静默：营销链路非关键，下轮定时器/下次启动自然重试 */
  } finally { clearTimeout(timer); }
}

/** 新用户资格锚：onboarding 完成时刻（本地近似；购买资格的最终权威在服务端履约）。 */
function _onboardingMs() {
  try {
    const ob = (config || {}).onboarding || {};
    if (!ob.completed) return 0;
    const t = Date.parse(String(ob.completed_at || ""));
    return isNaN(t) ? 0 : t;
  } catch (e) { return 0; }
}

// 内置预览样例（--poster-preview 时 feed 不可达也能验收）：双语数据本体在
// campaign-model.js::CM_PREVIEW_SAMPLE（活动域数据与模型同居，且不给 main.js
// 添未迁 i18n 债——它与 feed 数据走同一双语渲染路径，不是 UI 硬编码）。
const _CAMP_PREVIEW_SAMPLE = campaignModel.CM_PREVIEW_SAMPLE;

// 最近一次选品判定（诊断面板消费）：{ts, result:"show"|"skip"|"preview", id, reason}。
// cmEligible 早就算得出精确原因，此前在这里被丢成 null——「为什么没弹」于是要翻代码
// + 翻 %APPDATA%。现在原因三路透出：IPC 回执（渲染层发 skip 埋点）/ 主进程日志一行 /
// desktop:campaign-diag（🩺 健康看板「活动海报」行）。
let _campLastDecision = null;

ipcMain.handle("desktop:campaign-poster", async () => {
  try {
    // 首启竞态兜底：feed 尚空（首次安装无本地缓存）且首拉在途 → 等它收尾再判定。
    // refreshCampaigns 自带 10s abort，最坏 ~10s 后照常判定，不会挂死。
    if (!_getCampaigns().length && _campRefreshInflight) {
      try { await _campRefreshInflight; } catch (e) { /* refresh 从不 reject，保险而已 */ }
    }
    const onboardingMs = _onboardingMs();
    const lang = String(((config || {}).unified_inbox || {}).lang || "");
    if (process.env.AITR_POSTER_PREVIEW === "1") {
      // 验收通道：feed 首张（缺 feed 用内置样例）跳资格/频控直出；deadline 已过/缺失时
      // 造一个「满窗」的未来截止，让倒计时可视——预览的意义是看到真实渲染而非真实资格。
      const list = _getCampaigns();
      const c = list.length ? list[0] : campaignModel.cmNormalizeFeed(_CAMP_PREVIEW_SAMPLE).campaigns[0];
      let dl = campaignModel.cmDeadline(c, onboardingMs);
      if (!dl || dl <= Date.now()) {
        dl = c.windowHoursAfterOnboarding ? Date.now() + c.windowHoursAfterOnboarding * 3600e3 : 0;
      }
      _campLastDecision = { ts: Date.now(), result: "preview", id: c.id, reason: "" };
      console.log(`[campaign] preview: ${c.id} (--poster-preview, eligibility/frequency skipped)`);
      return { campaign: c, deadline: dl, lang, preview: true, showCount: 0 };
    }
    const ctx = {
      now: Date.now(),
      onboardingMs,
      managed: _managedEdition,
      state: _getCampState(),
    };
    const c = campaignModel.cmPick(_getCampaigns(), ctx);
    if (!c) {
      const skip = campaignModel.cmSkipSummary(_getCampaigns(), ctx) || { id: "", reason: "" };
      _campLastDecision = { ts: Date.now(), result: "skip", id: skip.id, reason: skip.reason };
      console.log(`[campaign] skip: ${skip.reason || "?"}${skip.id ? ` (${skip.id})` : ""}`
        + ` onboarding=${onboardingMs ? new Date(onboardingMs).toISOString() : "none"}`
        + ` feed=${_getCampaigns().length} managed=${_managedEdition}`);
      // campaign:null + skip 原因：老渲染层只认 payload.campaign（安全忽略），
      // 新渲染层据此发 poster6u_skip_{reason} 埋点（暗面漏斗）。
      return { campaign: null, skip, lang };
    }
    const shows = ((_getCampState().shows || {})[c.id]) || {};
    _campLastDecision = { ts: Date.now(), result: "show", id: c.id, reason: "" };
    return {
      campaign: c,
      deadline: campaignModel.cmDeadline(c, onboardingMs),
      lang,
      // 本次之前已展示次数：渲染层据此把第 2/3 次曝光降级为非模态角卡（减打扰）
      showCount: Number(shows.count) || 0,
    };
  } catch (e) { return null; }
});

// 🩺 健康看板「活动海报」行的数据口（P0 可诊断化）：最近判定 + feed/频控概览，零敏感字段。
ipcMain.handle("desktop:campaign-diag", () => {
  try {
    const st = _getCampState();
    const shows = st.shows || {};
    let shownTotal = 0;
    for (const k of Object.keys(shows)) shownTotal += Number((shows[k] || {}).count) || 0;
    return {
      ok: true,
      last: _campLastDecision,
      feed_count: _getCampaigns().length,
      onboarding_ms: _onboardingMs(),
      managed: _managedEdition,
      preview: process.env.AITR_POSTER_PREVIEW === "1",
      shown_total: shownTotal,
      never_count: Object.keys(st.never || {}).length,
    };
  } catch (e) { return { ok: false }; }
});

ipcMain.handle("desktop:campaign-act", async (_e, args) => {
  const a = args || {};
  const id = String(a.id || "");
  const preview = process.env.AITR_POSTER_PREVIEW === "1";
  if (!id) return { ok: false };
  if (a.action === "shown") {
    if (preview) return { ok: true, preview: true }; // 预览不落频控（渲染层本就不发，双保险）
    // 展示计数只记在 shown（click 不重复计——一次会话只消耗一次频控额度）
    _campState = campaignModel.cmRecordShow(_getCampState(), id, Date.now());
    _saveCampState();
    return { ok: true };
  }
  if (a.action === "never") {
    if (preview) return { ok: true, preview: true }; // 预览点「不再提醒」不永久静默真机
    _campState = campaignModel.cmOptOut(_getCampState(), id);
    _saveCampState();
    return { ok: true };
  }
  if (a.action === "click") {
    // 预览态 feed 可能为空（内置样例）：找不到条目时回落样例 CTA，验收也要能点开链接
    let c = _getCampaigns().find((x) => x.id === id);
    if (!c && preview) c = campaignModel.cmNormalizeFeed(_CAMP_PREVIEW_SAMPLE).campaigns[0];
    const url = (c && c.ctaUrl) || "";
    if (!/^https:\/\//i.test(url)) return { ok: false }; // 只放行 https，活动数据不可指挥本地执行任何东西
    // 追加注册时间锚 reg_ts（秒）：官网 /order 金卡据此渲染 72h 真倒计时——
    // 仅官网域白名单（注册时间不带给第三方链接），未完成首启 / 解析失败原样打开。
    const finalUrl = campaignModel.cmCtaUrlWithReg(url, _onboardingMs(), ["bd2026.cc"]);
    try {
      await shell.openExternal(finalUrl);
      // 奖励时刻（P2 2026-08-22）：用户去浏览器付款，付款→履约→入账全程发生在壳外，
      // 完成后桌面端此前零反馈（额度墙的 armWatch 只在墙内点「立即充值」才布防）。
      // 预览态不布防（验收点击不是真购买意图）。
      if (!preview) _armTopupArrivalWatch();
      return { ok: true };
    } catch (e) { return { ok: false }; }
  }
  return { ok: false };
});

// ── 充值到账「奖励时刻」（P2 2026-08-22）────────────────────────────────────
// 海报 CTA 点击后每 60s 盯一次 /api/workspace/quota（backendGet 带 Bearer，与
// ui-event 同一后端通道）最长 30 分钟：余量较基线增加 → 原生系统通知庆祝到账，
// 闭合「弹窗→下单→付款→到账→确认」最后一环。USDT 付款到履约通常几分钟，30 分钟
// 覆盖绝大多数真实链路；窗口耗尽静默放弃（工作台额度墙/顶栏徽章仍是兜底真相面）。
// fail-soft：后端不可达/字段缺失只是本轮跳过，绝不打扰、绝不影响海报链路。
let _topupWatch = null; // {timer, left, baseRemaining}
function _armTopupArrivalWatch() {
  try {
    if (_topupWatch && _topupWatch.timer) clearInterval(_topupWatch.timer); // 重复点击=重置窗口
    _topupWatch = { timer: null, left: 30, baseRemaining: null };
    const poll = async () => {
      const w = _topupWatch;
      if (!w) return;
      w.left -= 1;
      let q = null;
      try { q = await backendGet("/api/workspace/quota"); } catch (e) { /* 后端未就绪：下轮再试 */ }
      const rem = q && q.ok && q.remaining != null ? Number(q.remaining) : null;
      if (rem != null && isFinite(rem)) {
        if (w.baseRemaining == null) {
          w.baseRemaining = rem; // 首个可读快照=基线（点击时后端可能还没起来）
        } else if (rem > w.baseRemaining + 1) {
          const delta = rem - w.baseRemaining;
          try {
            if (Notification.isSupported()) {
              new Notification({
                title: SS("camp.credited_title"),
                body: SS("camp.credited_body", { n: delta.toLocaleString("en-US") }),
              }).show();
            }
          } catch (e) { /* 通知不可用静默 */ }
          console.log(`[campaign] topup credited +${delta} (${30 - w.left}min after poster CTA)`);
          clearInterval(w.timer);
          _topupWatch = null;
          return;
        }
      }
      if (w.left <= 0) {
        clearInterval(w.timer);
        _topupWatch = null;
      }
    };
    _topupWatch.timer = setInterval(() => { poll().catch(() => {}); }, 60000);
    if (_topupWatch.timer.unref) _topupWatch.timer.unref();
    poll().catch(() => {}); // 立即取基线
  } catch (e) { /* 盯梢失败不影响海报主链路 */ }
}

/** 活动轮询（与公告同节奏；dev 也跑便于验证——资格判定的 managed 闸会挡住开发态弹出）。 */
function setupCampaigns() {
  // 首拉 promise 留给海报查询等待（竞态兜底）；收尾即清引用，后续 6h 轮询不参与等待
  _campRefreshInflight = refreshCampaigns().finally(() => { _campRefreshInflight = null; });
  const t = setInterval(refreshCampaigns, 6 * 60 * 60 * 1000);
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
        manifest_version: app.getVersion(), // 保持纯 semver：版本分布/强制升级线都按它聚合
        patch: localPatchLevel(), // 已落地热补丁号（0=未打）；老服务端不认这个字段会直接丢
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
    Menu.setApplicationMenu(buildAppMenu());
    // 显示指纹面包屑：落 userData/display-metrics.json 供 fleet 台账/装后检查
    // 远程读精确缩放（SSH 侧 API 全撒谎，壳上报是唯一精确通道；best-effort）。
    try { winFit.installDisplayBreadcrumb(app, require("electron").screen); } catch (e) { /* 静默 */ }
    await maybeRotateManagedToken();
    // 后台自拉起（不阻塞开窗：renderer 已有「正在连接后台→自动重连」遮罩兜底）。
    backendManager.start(config).catch((e) => console.log(`[backend] start error: ${e}`));
    // 边车在后端之后拉起：它们要用 config.backend.token 回推入站消息，而该令牌可能刚被
    // maybeRotateManagedToken 换过。同样不阻塞开窗（登录成功前没有任何回推流量）。
    sidecars.startAll(config).catch((e) => console.log(`[sidecar] start error: ${e}`));
    createWindow();
    setupAutoUpdate();
    setupHotpatch(); // 整包之后：热补丁按 _latestFullVersion 让路，先后顺序有意义
    setupAnnouncements();
    setupCampaigns();
    setupVersionTelemetry();
  });
}

// 退出时回收后端进程，避免残留 Python/二进制占端口。
// B57 升级风暴修复：必须**等后端真死**再放行退出——旧 fire-and-forget taskkill
// 让「壳已退、装完新版自动拉起」跑在「旧后端还没死透」之前，新旧双进程抢同一份
// pyrogram 会话 → Telegram 强制注销全部账号。preventDefault + 等死 + 二次 quit；
// 二次进入时 _backendStopped=true 直接放行，绝无死循环。8s 超时兜底：杀不掉的
// 进程不该把用户永远锁在退出流程里（超时后照旧退出，风险如实留在日志）。
let _backendStopped = false;
async function shutdownBackendAndWait() {
  if (_backendStopped) return;
  _backendStopped = true;
  try { await backendManager.stopAndWait(8000); } catch (e) { /* 回收失败不阻断退出 */ }
  try { sidecars.stopAll(); } catch (e) { /* 同上：边车残留不该阻断退出 */ }
}
app.on("before-quit", (e) => {
  if (_backendStopped) return;
  e.preventDefault();
  const finish = () => { try { app.quit(); } catch (err) { try { app.exit(0); } catch (err2) {} } };
  shutdownBackendAndWait().then(finish, finish);
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
