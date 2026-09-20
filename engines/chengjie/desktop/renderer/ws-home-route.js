"use strict";

// #158（2026-09-04）：「审批页进得去出不来」的壳侧路由纯函数。
//
// 事故链：收件箱页的「去审批」是**同窗** location.href='/workspace/drafts'——在壳内
// 这就是把主窗收件箱 webview 自己导航去了子页。之后点顶栏「聊天」：
//   _win_unique（新壳 wsRoute）→ window.open('/workspace') → main.js focusMainInbox
//   → renderer onOpenWorkspace 只做 Inbox.activate（标签本来就激活）→ **什么都没发生**。
// 旧承接的隐含前提是「收件箱 webview 永远停在 /workspace」，webview 自己漂走就破了。
//
// 本模块决定「主窗收到『打开 /workspace』请求时该做什么」：
//   navigate ＝ webview 不在收件箱主路径（漂到 /workspace/* 子页或后台页）→ 把它导航
//              回 homeUrl，深链参数（conv/focus/mid/flag/case）原样并进去——页面自带
//              ?conv=/&mid= 解析，无需再 postMessage；
//   deliver  ＝ webview 就在主路径且请求带会话深链 → 站内 postMessage open-conv（旧行为）；
//   none     ＝ 就在主路径且无深链 → 只切标签（旧行为）。
// 刻意不动的形态：about:blank/空（启动闸门期，bootLoad 负责）、/login（登录链自带
// next= 回跳，硬导航会打断自动登录）、跨源（不该发生，宁可不猜）。
//
// 双模式：浏览器经 <script> 加载成全局 window.WsHomeRoute（renderer.js 消费）；
//         Node 经 require 取 module.exports（desktop/test/ws-home-route.test.js 单测）。

var WS_DEEP_KEYS = ["mid", "flag", "case"];

function wsPathOf(url) {
  try {
    var u = new URL(String(url || ""), "http://cx.local");
    return (u.pathname || "/").replace(/\/+$/, "") || "/";
  } catch (e) {
    return "";
  }
}

// 与 _win_unique._deepLinkParts 口径一致：conv|focus / mid / flag / case
function wsDeepLinkParts(url) {
  try {
    var u = new URL(String(url || ""), "http://cx.local");
    var q = u.searchParams;
    var cid = q.get("conv") || q.get("focus") || "";
    if (!cid) return null;
    return { cid: cid, mid: q.get("mid") || "", flag: q.get("flag") || "", caseId: q.get("case") || "" };
  } catch (e) {
    return null;
  }
}

// homeUrl + 请求里的深链参数 → 可直接 loadURL 的绝对地址（home 自带 ?lang=/&theme= 保留）
function wsHomeWithDeepLink(homeUrl, requestUrl) {
  var home = new URL(String(homeUrl));
  var req = null;
  try { req = new URL(String(requestUrl || ""), home.origin); } catch (e) { req = null; }
  if (req) {
    var cid = req.searchParams.get("conv") || req.searchParams.get("focus") || "";
    if (cid) {
      home.searchParams.set("conv", cid);
      for (var i = 0; i < WS_DEEP_KEYS.length; i++) {
        var k = WS_DEEP_KEYS[i];
        var v = req.searchParams.get(k);
        if (v) home.searchParams.set(k, v);
      }
    }
  }
  return home.toString();
}

function resolveWorkspaceHome(currentUrl, homeUrl, requestUrl) {
  var parts = wsDeepLinkParts(requestUrl);
  var stay = parts ? { action: "deliver", parts: parts } : { action: "none", parts: null };
  var home = null;
  try { home = new URL(String(homeUrl || "")); } catch (e) { home = null; }
  if (!home) return stay;                                   // 无 home 口径（极老壳）→ 旧行为
  var cur = String(currentUrl || "");
  if (!cur || cur.indexOf("about:") === 0) return stay;    // 启动闸门期空白页：bootLoad 会导航
  var curU = null;
  try { curU = new URL(cur); } catch (e) { curU = null; }
  if (!curU || curU.origin !== home.origin) return stay;   // 跨源/不可解析：不猜
  var curPath = wsPathOf(cur);
  var homePath = wsPathOf(home.toString());
  if (curPath === homePath) return stay;                    // 就在收件箱：旧行为
  if (curPath === "/login") return stay;                    // 登录链自带 next= 回跳
  return { action: "navigate", url: wsHomeWithDeepLink(home.toString(), requestUrl), parts: parts };
}

var WsHomeRoute = {
  wsPathOf: wsPathOf,
  wsDeepLinkParts: wsDeepLinkParts,
  wsHomeWithDeepLink: wsHomeWithDeepLink,
  resolveWorkspaceHome: resolveWorkspaceHome,
};

if (typeof module !== "undefined" && module.exports) {
  module.exports = WsHomeRoute;
}
if (typeof window !== "undefined") {
  try { window.WsHomeRoute = WsHomeRoute; } catch (e) { /* 只读环境：renderer 按缺席降级 */ }
}
