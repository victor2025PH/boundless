"use strict";

// 统一收件箱 webview 的 preload（2026-08-11 双栏融合）：向 /workspace 页面暴露
// 极小桌面壳桥 window.__chatxShell —— 页面把「打开官方网页版」等动作交给壳，
// 并把未读总数回推给壳的收件箱标签徽标（webview 内容摸不到壳 DOM，只能走 ipc）。
//
// 契约（与 desktop/renderer/renderer.js::onShellBridge 成对，boot-invariants 钉住）：
//   sendToHost("chatx-bridge", { cmd: "openEmbedded", platform, opts })   // opts 可带 shell_tab_id 精确点名壳标签
//   sendToHost("chatx-bridge", { cmd: "badge", unread })
//   sendToHost("chatx-bridge", { cmd: "stripReady" })                     // P2：页内标签条已渲染，壳竖栏让位
//
// 纯浏览器打开同一页面时没有这份 preload → window.__chatxShell 不存在，
// 页面按桥缺失降级（不渲染入口、不推送徽标），桌面/网页共用一份模板零分叉。
// 安全面：只出站消息、不暴露 require/ipcRenderer 本体；平台合法性由壳侧校验。

const { ipcRenderer, contextBridge } = require("electron");

function send(payload) {
  try { ipcRenderer.sendToHost("chatx-bridge", payload); } catch (_) { /* 壳侧未挂监听时静默 */ }
}

// 壳 → 页 状态通道（P2）：已打开的内嵌标签清单 + 注入健康（renderer.pushShellState
// 经 wv.send("chatx-bridge-state") 抵达）。转发进页面世界走 window.postMessage
// （contextIsolation 下 DOM 事件跨世界可见）；页面另可经 getState() 拉当前快照——
// 打开抽屉时主动取一次，防「状态先推、抽屉后开」错过事件。
let _state = null;
try {
  ipcRenderer.on("chatx-bridge-state", (_e, st) => {
    _state = (st && typeof st === "object") ? st : null;
    try { window.postMessage({ type: "chatx-shell-state", state: _state }, "*"); } catch (_) {}
  });
} catch (_) { /* ipc 不可用（异常环境）：页面拿不到状态，按无状态降级 */ }

// 悬浮副驾原生窗事件通道（cp PiP shell 桥 P1 2026-08-18）：主进程经本 webContents
// send("cp-pip-evt") 回吐 app.html 消息（cp-ready/cp-fill/cp-send）与 cp-pip-closed，
// 这里原样转投页面世界；页面侧按 msg.type 消费（契约同 DOM PiP 路径）。
try {
  ipcRenderer.on("cp-pip-evt", (_e, m) => {
    try { window.postMessage({ type: "chatx-cp-pip", msg: m }, "*"); } catch (_) { /* 页面未就绪时丢弃 */ }
  });
} catch (_) { /* ipc 不可用：PiP 事件失联，页面哨兵会兜住 */ }

const api = {
  desktop: true,
  // 打开（或切到已开的）某平台官方网页版标签。opts 目前仅作前向兼容占位。
  openEmbedded(platform, opts) {
    send({ cmd: "openEmbedded", platform: String(platform || ""), opts: opts && typeof opts === "object" ? opts : {} });
  },
  // 收件箱未读总数 → 壳收件箱标签徽标（页面在未读聚合刷新处推送，口径单源在页面）。
  pushBadge(payload) {
    send({ cmd: "badge", unread: Number((payload || {}).unread) || 0 });
  },
  // 当前壳侧标签状态快照（{strip,enabled,embedded:[{id,platform,label,health,assist}]} | null）。
  getState() { return _state; },
  // P2 握手：页面在工作台头部之下渲染完 cx-shell-strip 后回报——壳收到才隐藏竖栏。
  // 旧页面不调用 → 竖栏保持可见（兜底导航永在）。
  stripReady() { send({ cmd: "stripReady" }); },
  // 悬浮副驾原生置顶窗（cp PiP shell 桥 P1）：{action:'open'|'close'|'post'|'status',…}
  // invoke 直达主进程 desktop:copilot-pip（不经壳 renderer——PiP 是主进程原生窗，
  // 绕一跳纯增故障面）；回吐经上方 cp-pip-evt → window.postMessage 抵页面世界。
  copilotPip(payload) {
    try {
      return ipcRenderer.invoke("desktop:copilot-pip", payload && typeof payload === "object" ? payload : {});
    } catch (_) {
      return Promise.resolve({ ok: false, error: "ipc unavailable" });
    }
  },
  // 应用菜单内迁（P0 2026-08-22）：工作台顶栏渲染壳应用菜单（文件/编辑/视图/窗口/
  // 帮助）。spec/action 直达主进程（与 copilotPip 同款）；旧壳无这两个方法 → 页面
  // typeof 探测不点亮（旧壳原生菜单条本就还在，页内不画=零双菜单）。
  menuSpec() {
    try { return ipcRenderer.invoke("desktop:app-menu-spec"); }
    catch (_) { return Promise.resolve(null); }
  },
  menuAction(id) {
    try { return ipcRenderer.invoke("desktop:app-menu-action", String(id || "")); }
    catch (_) { return Promise.resolve({ ok: false, error: "ipc unavailable" }); }
  },
  // #57 手机扫码操控（2026-08-30）：一键放行 Windows 防火墙（主进程提权 UAC，
  // 程序级规则只放行随包 backend.exe）。小智配对弹窗按 typeof 探测渲染按钮；
  // 纯浏览器/旧壳无此方法 → 按钮不出现，页面自然降级。
  pairLanFix() {
    try {
      return ipcRenderer.invoke("desktop:pair-lan-fix");
    } catch (_) {
      return Promise.resolve({ ok: false, reason: "ipc" });
    }
  },
};

// contextIsolation 开（Electron webview 默认）→ contextBridge；关 → 直挂 window 兜底。
try {
  contextBridge.exposeInMainWorld("__chatxShell", api);
} catch (_) {
  try { window.__chatxShell = api; } catch (_) { /* 环境异常时放弃暴露，页面自然降级 */ }
}

// B54 焦点自愈桥（与 popup-preload.js 同名契约 __chatxShellFocus）：原生
// confirm/alert 关闭后 Chromium 焦点态失步 → 页面经此请主进程 blur+focus 复位。
// 独立命名空间：后台弹窗只挂这一个极小桥，页面侧探测一个名字通吃两形态。
const focusApi = {
  focusFix() {
    try { return ipcRenderer.invoke("desktop:focus-fix"); }
    catch (_) { return Promise.resolve({ ok: false, error: "ipc unavailable" }); }
  },
};
try {
  contextBridge.exposeInMainWorld("__chatxShellFocus", focusApi);
} catch (_) {
  try { window.__chatxShellFocus = focusApi; } catch (_) { /* 页面自然降级 */ }
}

// ── 壳能力标记（2026-08-29，与 popup-preload.js 同名同契约）───────────────────
// wsRoute＝壳主进程把「打开 /workspace」收敛到主窗收件箱标签。收件箱页自己点到
// /workspace 链接（会话深链等）时，_win_unique 据此直接 window.open 交主进程，
// 深链经 open-conv 转回本页站内切会话；旧壳无标记＝维持 BC 探活旧链。
const capsApi = { wsRoute: true };
try {
  contextBridge.exposeInMainWorld("__chatxCaps", capsApi);
} catch (_) {
  try { window.__chatxCaps = capsApi; } catch (_) { /* 页面自然降级 */ }
}
