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
};

// contextIsolation 开（Electron webview 默认）→ contextBridge；关 → 直挂 window 兜底。
try {
  contextBridge.exposeInMainWorld("__chatxShell", api);
} catch (_) {
  try { window.__chatxShell = api; } catch (_) { /* 环境异常时放弃暴露，页面自然降级 */ }
}
