/* 前端运行时错误自动上报 → POST /api/clientlog
 * 捕获 window.onerror（含资源/脚本错误）与未处理的 Promise 拒绝。
 * 去重 + 单会话上限，避免刷屏/自我放大。无任何依赖。 */
(function () {
  "use strict";
  var HUB = (window.HUB || (location.origin)) ;
  var ENDPOINT = HUB.replace(/\/+$/, "") + "/api/clientlog";
  var seen = Object.create(null);
  var sent = 0;
  var MAX_PER_SESSION = 40;

  function post(rec) {
    if (sent >= MAX_PER_SESSION) return;
    var sig = (rec.kind || "") + "|" + (rec.msg || "") + "|" + (rec.line || "");
    if (seen[sig]) return;            // 同一错误本会话只报一次
    seen[sig] = 1;
    sent++;
    rec.page = location.pathname + location.search;
    var body = JSON.stringify(rec);
    try {
      if (navigator.sendBeacon) {
        navigator.sendBeacon(ENDPOINT, new Blob([body], { type: "application/json" }));
        return;
      }
    } catch (e) {}
    try {
      fetch(ENDPOINT, { method: "POST", body: body,
        headers: { "Content-Type": "application/json" }, keepalive: true }).catch(function () {});
    } catch (e) {}
  }

  window.addEventListener("error", function (ev) {
    try {
      if (ev && ev.error) {                       // 脚本运行时错误
        post({ kind: "pageerror", level: "error",
          msg: String(ev.message || ev.error.message || ev.error),
          stack: String(ev.error.stack || ""),
          src: String(ev.filename || ""), line: ev.lineno, col: ev.colno });
      } else if (ev && ev.target && (ev.target.src || ev.target.href)) {  // 资源加载失败
        var rsrc = String(ev.target.src || ev.target.href || "");
        // 忽略「空 src」误报：<img/audio/video src=""> 会把当前页 URL 当资源去取，
        // 解码失败触发 error——这并非真实 404，过滤掉以免污染 client_errors。
        if (rsrc === location.href || rsrc === location.origin + location.pathname) return;
        post({ kind: "resource", level: "warn",
          msg: "resource load failed: " + (ev.target.tagName || ""),
          src: rsrc });
      }
    } catch (e) {}
  }, true);   // 捕获阶段才能拿到资源错误

  window.addEventListener("unhandledrejection", function (ev) {
    try {
      var r = ev && ev.reason;
      post({ kind: "promise", level: "error",
        msg: String((r && (r.message || r)) || "unhandledrejection"),
        stack: String((r && r.stack) || "") });
    } catch (e) {}
  });

  // 供页面主动上报：window.__clientlog({kind,msg,...})
  window.__clientlog = post;
})();
