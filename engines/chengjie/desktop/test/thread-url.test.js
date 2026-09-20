"use strict";

// 跨面深链 URL 纯函数单测（无框架/无 DOM，node 直跑）：node test/thread-url.test.js
const assert = require("assert");
const {
  THREAD_URL_TEMPLATES,
  sanitizeThreadId,
  threadLocateSupported,
  threadUrl,
} = require("../renderer/thread-url.js");

let pass = 0;
function ok(name, cond) {
  assert.ok(cond, name);
  pass++;
}

// ── 平台支持面 ────────────────────────────────────────────────────────────────
ok("messenger 支持定位", threadLocateSupported("messenger") === true);
ok("大小写不敏感", threadLocateSupported("Messenger") === true);
// 第二批（2026-08-13）：telegram=tweb hash 路由（同文档导航，失败形态=no-op 非 404）；
// whatsapp=官方 /send?phone= 流（仅私聊数字号，形态表拦群 jid）。
ok("telegram 支持定位", threadLocateSupported("telegram") === true);
ok("whatsapp 支持定位", threadLocateSupported("whatsapp") === true);
ok("空平台不支持", threadLocateSupported("") === false);
ok("line 不支持", threadLocateSupported("line") === false);
ok("模板表只含白名单平台", Object.keys(THREAD_URL_TEMPLATES).join(",") === "messenger,telegram,whatsapp");

// ── URL 生成 ─────────────────────────────────────────────────────────────────
ok("messenger 数字线程", threadUrl("messenger", "1000123456789") === "https://www.messenger.com/t/1000123456789");
ok("平台大小写归一", threadUrl("MESSENGER", "42") === "https://www.messenger.com/t/42");
ok("首尾空白剥离", threadUrl("messenger", "  42  ") === "https://www.messenger.com/t/42");
ok("不支持平台返回空", threadUrl("line", "12345") === "");
ok("空 thread 返回空", threadUrl("messenger", "") === "");
ok("null thread 返回空", threadUrl("messenger", null) === "");

// ── telegram：tweb hash 路由（chat_key = bot-api 风格 dialog id）──────────────
ok("tg 用户正数 id", threadUrl("telegram", "123456789") === "https://web.telegram.org/k/#123456789");
ok("tg 群负数 id", threadUrl("telegram", "-987654") === "https://web.telegram.org/k/#-987654");
ok("tg 频道 -100 前缀", threadUrl("telegram", "-1001234567890") === "https://web.telegram.org/k/#-1001234567890");
ok("tg 非数字拒绝（用户名不猜）", threadUrl("telegram", "someuser") === "");
ok("tg 混入字母拒绝", threadUrl("telegram", "123abc") === "");
ok("tg 双负号拒绝", threadUrl("telegram", "--100") === "");

// ── whatsapp：官方 /send?phone= 流（仅私聊裸 E.164）─────────────────────────
ok("wa 私聊裸号", threadUrl("whatsapp", "8613800138000") === "https://web.whatsapp.com/send?phone=8613800138000");
ok("wa 群 jid 拒绝（无 URL 路由）", threadUrl("whatsapp", "120363123456789012@g.us") === "");
ok("wa 短于 6 位拒绝", threadUrl("whatsapp", "12345") === "");
ok("wa 带 + 拒绝（存储态为裸号）", threadUrl("whatsapp", "+8613800138000") === "");

// ── thread 消毒（防注入 webview src）─────────────────────────────────────────
ok("路径穿越拒绝", threadUrl("messenger", "../evil") === "");
ok("斜杠拒绝", threadUrl("messenger", "a/b") === "");
ok("引号拒绝", threadUrl("messenger", 'x"y') === "");
ok("空格拒绝", threadUrl("messenger", "a b") === "");
ok("超长拒绝", threadUrl("messenger", "x".repeat(65)) === "");
ok("CJK 拒绝", threadUrl("messenger", "线程") === "");
ok("用户名形态放行", sanitizeThreadId("john.doe_42") === "john.doe_42");
ok("@ 冒号连字符放行", sanitizeThreadId("a@b:c-d") === "a@b:c-d");
ok("消毒空输入", sanitizeThreadId(undefined) === "");

console.log(`thread-url.test.js: ${pass} assertions passed`);
