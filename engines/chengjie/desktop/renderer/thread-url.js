"use strict";

// 双面板融合 P0（2026-08-13）：跨面深链的「平台 → 会话直达 URL」纯函数。
// 工作台会话头「原生页打开」经 inbox-preload 桥把 {thread, account_id} 送到壳侧，
// 壳切到/建出该平台官方网页标签后，用本模块把 thread 变成可直接导航的 URL。
//
// 双模式：浏览器经 <script> 加载成全局函数（renderer.js::locateEmbeddedThread 用）；
//         Node 经 require 取 module.exports（desktop/test/thread-url.test.js 单测）。
//
// 设计约束：
// - **只做「失败也无害」的官方路由**（P0 的顾虑是「伪造 URL 导航去 404 比不定位更糟」，
//   故逐平台按失败形态论证）：
//   · messenger：messenger.com/t/<thread_id>（官方路径路由）。
//   · telegram（第二批 2026-08-13）：web.telegram.org/k/#<peer_id>——tweb 的 hash
//     路由，chat_key 即 bot-api 风格 dialog id（用户正数/群负数/频道 -100 前缀），
//     与壳嵌的 /k/ 变体同源＝**同文档导航**（不整页重载）；hash 不被识别时 tweb
//     只是留在会话列表＝与「仅切标签」等价，不存在 404 形态。⚠ 勿改成 /a/：
//     WebK 与 WebA 登录态各自独立，跨变体导航会把坐席踢到扫码页。
//   · whatsapp（第二批）：web.whatsapp.com/send?phone=<E164 裸号>——官方 wa.me
//     同款打开会话流（整页重载数秒，可接受）；**仅私聊数字号**（形态表拦住
//     群 jid「…@g.us」→ 返回 "" 仅切标签；号码不存在时 WA 页内自提示，非 404）。
// - thread 白名单消毒（防注入/路径穿越进 webview src）：仅 [A-Za-z0-9_.:@-]，
//   长度 1..64；不合法一律返回 ""。messenger 线程 id 实际为长数字串，白名单
//   留有余量以兼容 sidecar 未来可能上报的用户名形态。
// - 平台级**形态表**（THREAD_SHAPE）比全局白名单更严：收件箱 chat_key 到不了
//   该平台 URL 语义的（tg 非数字、wa 群 jid）一律返回 ""（仅切标签），绝不硬拼。

var THREAD_URL_TEMPLATES = {
  messenger: "https://www.messenger.com/t/{thread}",
  telegram: "https://web.telegram.org/k/#{thread}",
  whatsapp: "https://web.whatsapp.com/send?phone={thread}",
};

// 平台级 thread 形态（缺席＝只受全局白名单约束）：
//   telegram：pyrogram chat.id 字符串形态（用户正数 / 群负数 / 频道 -100 前缀）；
//   whatsapp：私聊裸 E.164（与服务端 _WA_PHONE_RE 同刻度 6..20 位）；群无 URL 路由。
var THREAD_SHAPE = {
  telegram: /^-?\d{1,20}$/,
  whatsapp: /^\d{6,20}$/,
};

var _THREAD_OK_RE = /^[A-Za-z0-9_.:@-]{1,64}$/;

function sanitizeThreadId(thread) {
  var t = String(thread == null ? "" : thread).trim();
  return _THREAD_OK_RE.test(t) ? t : "";
}

function threadLocateSupported(platform) {
  return Object.prototype.hasOwnProperty.call(
    THREAD_URL_TEMPLATES, String(platform || "").toLowerCase());
}

function threadUrl(platform, thread) {
  var plat = String(platform || "").toLowerCase();
  var tpl = THREAD_URL_TEMPLATES[plat];
  if (!tpl) return "";
  var t = sanitizeThreadId(thread);
  if (!t) return "";
  var shape = THREAD_SHAPE[plat];
  if (shape && !shape.test(t)) return "";
  return tpl.replace("{thread}", encodeURIComponent(t));
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    THREAD_URL_TEMPLATES: THREAD_URL_TEMPLATES,
    THREAD_SHAPE: THREAD_SHAPE,
    sanitizeThreadId: sanitizeThreadId,
    threadLocateSupported: threadLocateSupported,
    threadUrl: threadUrl,
  };
}
