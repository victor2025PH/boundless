"use strict";

// 悬浮副驾原生窗 wrapper（pip.html）的 preload（cp PiP shell 桥 P1 2026-08-18）：
// 只做两根管子，不暴露 ipcRenderer 本体——
//   出站：wrapper 把 app.html 回吐的消息（cp-ready/cp-fill/cp-send…）经 ipc 送主进程
//   入站：主进程转来的宿主消息（cp-context/cp-cmd）回调给 wrapper 转投 iframe
// 消息体是纯数据，语义契约在页面侧（unified_inbox.html PiP 段）与 app.html 之间，
// 本层零解析零改写（中继层做语义过滤＝双份契约漂移源）。

const { ipcRenderer, contextBridge } = require("electron");

const api = {
  out(msg) {
    try { ipcRenderer.send("cp-pip-out", msg); } catch (_) { /* 主进程未挂监听时静默 */ }
  },
  onIn(cb) {
    if (typeof cb !== "function") return;
    try {
      ipcRenderer.on("cp-pip-in", (_e, m) => { try { cb(m); } catch (_) { /* wrapper 回调异常不断桥 */ } });
    } catch (_) { /* ipc 不可用（异常环境）：入站失联，wrapper 按无宿主降级 */ }
  },
};

// contextIsolation 开（本窗显式开启）→ contextBridge；关 → 直挂 window 兜底。
try {
  contextBridge.exposeInMainWorld("__cpPipBridge", api);
} catch (_) {
  try { window.__cpPipBridge = api; } catch (_) { /* 放弃暴露，wrapper 自然降级 */ }
}
