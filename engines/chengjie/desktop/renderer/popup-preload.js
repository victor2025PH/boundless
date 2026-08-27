"use strict";

// 后台弹窗（admin / workspace 子页原生窗）的极小 preload——只暴露 B54 焦点自愈桥。
//
// 背景（实施68 P1-16）：Electron 原生 confirm/alert 关闭后 Chromium 焦点态失步
// （上游久悬 bug），整窗输入框点不进；弹回工作台重进才恢复。修复=页面侧
// （_focus_selfheal.html）在壳内包一层 confirm/alert/prompt，返回即经本桥请主进程
// 把窗口 blur+focus 复位。刻意不复用 inbox-preload.js：那份带 sendToHost 桥 /
// PiP / 菜单等 webview 专属面，弹窗暴露整套=纯增故障面；本桥与 inbox-preload 的
// __chatxShellFocus 同名同契约，页面探测一个名字通吃两形态。
// 纯浏览器打开同页无此桥 → 页面按桥缺失降级（浏览器的 confirm 本就无此病）。

const { ipcRenderer, contextBridge } = require("electron");

const focusApi = {
  focusFix() {
    try { return ipcRenderer.invoke("desktop:focus-fix"); }
    catch (_) { return Promise.resolve({ ok: false, error: "ipc unavailable" }); }
  },
};

try {
  contextBridge.exposeInMainWorld("__chatxShellFocus", focusApi);
} catch (_) {
  try { window.__chatxShellFocus = focusApi; } catch (_) { /* 环境异常：页面自然降级 */ }
}
