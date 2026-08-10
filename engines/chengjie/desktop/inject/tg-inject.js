"use strict";

// 桌面 webview preload —— Electron 侧 Host 适配器（薄）。
//
// 平台无关核心、选择器档案、媒体格式化都在 repo 根 shared/inject/（单一事实来源，
// 与浏览器扩展 content script 共用同一份逻辑）。本文件只负责：
//   ①用 ipcRenderer 实现 host 接口（→ 主进程 → 本仓库 FastAPI 后端，规避 webview 跨域/CSP）；
//   ②把宿主下行事件（set-persona / set-reply-lang / set-account / fill-composer）桥接成回调；
//   ③启动共享核心。
//
// preload 在隔离世界运行但可用 require（main.js 已对 webview 关 sandbox）；
// 故直接 require repo 根 shared/inject。
//
// ⚠ 这条相对路径在**装机版里跨出了 app.asar**：包内本文件在 app.asar/inject/ 下，
// `../../shared/inject/*` 落到 resources/shared/inject/ —— 只有 package.json 的
// extraResources 映射（`../shared/inject` → `shared/inject`）把它随包送过去才成立。
// 那条映射曾经不存在，于是 1.016/1.017 出货时整个注入层在客户机 MODULE_NOT_FOUND
// 静默死掉（开发机因为能直接读到 repo 根，永远复现不出）。现由
// test/package-layout.test.js（按真实 require 反推包内路径）+ after-pack REQUIRED 双守。

const { ipcRenderer } = require("electron");

const profiles = require("../../shared/inject/profiles.js");
let mediaFormat = null;
try {
  mediaFormat = require("../../shared/inject/media-format.js");
} catch (e) {
  /* 媒体格式化模块缺失：媒体翻译降级关闭，不影响文本链路 */
}
let translateScheduler = null;
try {
  translateScheduler = require("../../shared/inject/translate-scheduler.js");
} catch (e) {
  /* 调度器缺失：core 回落逐条 translate，行为不变 */
}
let bubbleModel = null;
try {
  bubbleModel = require("../../shared/inject/bubble-model.js");
} catch (e) {
  /* 渲染模型缺失：core 回落单块译文旧渲染，行为不变 */
}
const { createInject } = require("../../shared/inject/core.js");

const host = {
  translate: (args) => ipcRenderer.invoke("desktop:translate", args),
  translateBatch: (args) => ipcRenderer.invoke("desktop:translate-batch", args),
  translateMedia: (args) => ipcRenderer.invoke("desktop:translate-media", args),
  smartReply: (args) => ipcRenderer.invoke("desktop:smart-reply", args),
  ingest: (args) => ipcRenderer.invoke("desktop:ingest", args),
  getConfig: () => ipcRenderer.invoke("desktop:config"),
  getSelectorProfiles: () => ipcRenderer.invoke("desktop:selector-profiles"),
  diag: (msg) => {
    try {
      ipcRenderer.invoke("desktop:diag", msg);
    } catch (e) {
      /* 忽略 */
    }
  },
  injectHealth: (payload) => {
    try {
      ipcRenderer.invoke("desktop:inject-health", payload);
    } catch (e) {
      /* 后端不可达：下次心跳重试 */
    }
  },
  // 桌面壳顶栏注入状态条：经 webview→host 通道上报
  reportInjectStatus: (payload) => {
    try {
      ipcRenderer.sendToHost("inject-status", payload);
    } catch (e) {
      /* 非 webview 宿主：忽略 */
    }
  },
  reportActiveChat: (payload) => {
    try {
      ipcRenderer.sendToHost("active-chat", payload);
    } catch (e) {
      /* 非 webview 宿主：忽略 */
    }
  },
  // 受控出站回执：fill-composer 到底填没填上、发没发出去（宿主据此 ack 后端队列）。
  // 没有它，宿主只能「发完就当成功」——注入失效时会把每条命令谎报成已送达。
  reportFillResult: (payload) => {
    try {
      ipcRenderer.sendToHost("fill-result", payload);
    } catch (e) {
      /* 非 webview 宿主：忽略 */
    }
  },
  onSetPersona: (cb) => ipcRenderer.on("set-persona", (_e, payload) => cb(payload)),
  onSetReplyLang: (cb) => ipcRenderer.on("set-reply-lang", (_e, payload) => cb(payload)),
  onSetAccount: (cb) => ipcRenderer.on("set-account", (_e, payload) => cb(payload)),
  onFillComposer: (cb) => ipcRenderer.on("fill-composer", (_e, payload) => cb(payload)),
};

createInject(host, { profiles, mediaFormat, translateScheduler, bubbleModel }).autostart();
