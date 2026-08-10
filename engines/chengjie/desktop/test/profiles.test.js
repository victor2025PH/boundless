"use strict";

// 选择器档案纯函数单测（无框架/无 DOM，node 直跑）：node test/profiles.test.js
const assert = require("assert");
const {
  detectPlatform,
  makeGenericProfile,
  applySelectorOverlay,
  resolveProfile,
  selectorHealth,
  BUILTIN_PROFILES,
  OVERLAYABLE_KEYS,
  textWithoutInjected,
} = require("../../shared/inject/profiles.js");
const { needsChromeUa, urlNeedsChromeUa } = require("../webview-ua.js");

let pass = 0;
function ok(name, cond) {
  assert.ok(cond, name);
  pass++;
}

// ── detectPlatform：hostname → 平台 id ────────────────────────────────────────
ok("telegram", detectPlatform("web.telegram.org") === "telegram");
ok("whatsapp", detectPlatform("web.whatsapp.com") === "whatsapp");
ok("instagram", detectPlatform("www.instagram.com") === "instagram");
ok("messenger", detectPlatform("www.messenger.com") === "messenger");
ok("facebook→messenger", detectPlatform("www.facebook.com") === "messenger");
ok("x.com", detectPlatform("x.com") === "x");
ok("twitter→x", detectPlatform("twitter.com") === "x");
ok("zalo", detectPlatform("chat.zalo.me") === "zalo");
ok("line", detectPlatform("line.me") === "line");
ok("unknown", detectPlatform("example.com") === "unknown");

// ── BUILTIN_PROFILES：6 平台齐全且形态正确 ──────────────────────────────────
["telegram", "whatsapp", "instagram", "messenger", "x", "zalo"].forEach((p) => {
  ok(`builtin 有 ${p}`, !!BUILTIN_PROFILES[p]);
  ok(`${p} 有 bubble`, typeof BUILTIN_PROFILES[p].bubble === "string");
});
// 内置定制档默认可回流；通用工厂档默认关闭回流（宁缺毋错，待现场校准）
ok("telegram canIngest", BUILTIN_PROFILES.telegram.canIngest === true);
ok("whatsapp canIngest", BUILTIN_PROFILES.whatsapp.canIngest === true);
ok("instagram canIngest 默认关", BUILTIN_PROFILES.instagram.canIngest === false);
ok("x canIngest 默认关", BUILTIN_PROFILES.x.canIngest === false);
ok("通用档标记 generic", BUILTIN_PROFILES.zalo.generic === true);
ok("定制档非 generic", !BUILTIN_PROFILES.telegram.generic);

// ── makeGenericProfile：声明式 → 档案对象 ────────────────────────────────────
const g = makeGenericProfile({
  platform: "demo",
  bubble: ".b",
  bubbleText: ".t",
  composer: ".c",
  sendBtn: ".s",
  outFlag: "out",
});
ok("generic platform", g.platform === "demo");
ok("generic supported 默认真", g.supported === true);
ok("generic canIngest 默认假", g.canIngest === false);
ok("generic 有 text 函数", typeof g.text === "function");
ok("generic 有 isOut 函数", typeof g.isOut === "function");
// isOut 走 outFlag（classList 判定）
ok("isOut outFlag 命中", g.isOut({ classList: { contains: (x) => x === "out" } }) === true);
ok("isOut outFlag 未中", g.isOut({ classList: { contains: () => false } }) === false);
ok("isOut null 安全", g.isOut(null) === false);

// ── applySelectorOverlay：白名单覆盖 + 类型守卫 + 不改原对象 ──────────────────
const base = BUILTIN_PROFILES.instagram;
const patched = applySelectorOverlay(base, { bubble: ".new-bubble", canIngest: true });
ok("覆盖 bubble", patched.bubble === ".new-bubble");
ok("覆盖布尔 canIngest", patched.canIngest === true);
ok("原对象不变(bubble)", base.bubble !== ".new-bubble");
ok("原对象不变(canIngest)", base.canIngest === false);
ok("函数随原型保留", typeof patched.text === "function");

// 类型守卫：布尔字段拒收字符串；字符串字段拒收空串/非串；未知字段忽略
const guarded = applySelectorOverlay(base, {
  canIngest: "yes",        // 非布尔 → 忽略
  bubble: "",              // 空串 → 忽略
  composer: 123,           // 非串 → 忽略
  notAKey: "x",            // 非白名单 → 忽略
  sendBtn: ".ok-send",     // 合法 → 接受
});
ok("非布尔不覆盖 canIngest", guarded.canIngest === false);
ok("空串不覆盖 bubble", guarded.bubble === base.bubble);
ok("非串不覆盖 composer", guarded.composer === base.composer);
ok("未知键不进对象", guarded.notAKey === undefined);
ok("合法 sendBtn 覆盖", guarded.sendBtn === ".ok-send");

// null patch / null profile 安全
ok("null patch 返回浅拷贝", applySelectorOverlay(base, null).bubble === base.bubble);
ok("null profile 返回 null", applySelectorOverlay(null, {}) === null);

// ── resolveProfile：内置 → 覆写 → 兜底 ───────────────────────────────────────
ok("resolve 内置", resolveProfile("telegram", null).platform === "telegram");
ok(
  "resolve 覆写",
  resolveProfile("instagram", { instagram: { canIngest: true } }).canIngest === true
);
ok("resolve unsupported", resolveProfile("nope", null).supported === false);

// ── OVERLAYABLE_KEYS 与后端契约对齐（含关键字段）────────────────────────────
["bubble", "composer", "sendBtn", "canIngest"].forEach((k) =>
  ok(`overlayable 含 ${k}`, OVERLAYABLE_KEYS.indexOf(k) >= 0)
);

// ── D1b selectorHealth：逐选择器命中探针 ─────────────────────────────────────
function fakeDoc(present) {
  return { querySelector: (sel) => (present.indexOf(sel) >= 0 ? {} : null) };
}
const prof = makeGenericProfile({
  platform: "demo2",
  bubble: ".b",
  bubbleText: ".t",
  composer: ".c",
  sendBtn: ".s",
  peerTitle: ".p",
});
let h = selectorHealth(prof, fakeDoc([".b", ".c", ".s", ".p"]));
ok("health 全命中 bubble", h.bubble === true);
ok("health 全命中 composer", h.composer === true);
ok("health 全命中 sendBtn", h.sendBtn === true);
ok("health 全命中 peerTitle", h.peerTitle === true);
h = selectorHealth(prof, fakeDoc([".b", ".p"]));
ok("health composer 失配", h.composer === false);
ok("health sendBtn 失配", h.sendBtn === false);
ok("health bubble 仍命中", h.bubble === true);
// 空选择器 / 探针异常 → 一律未命中（不抛）
const empty = selectorHealth({ bubble: "", composer: null }, fakeDoc([]));
ok("空选择器视为未命中", empty.bubble === false && empty.composer === false);
const thrower = { querySelector: () => { throw new Error("bad selector"); } };
ok("探针异常吞掉", selectorHealth(prof, thrower).bubble === false);

// ── webview-ua：多平台 Chrome UA 伪装判定 ────────────────────────────────────
ok("ua whatsapp", needsChromeUa("whatsapp") === true);
ok("ua instagram", needsChromeUa("instagram") === true);
ok("ua x", needsChromeUa("x") === true);
ok("ua telegram 不伪装", needsChromeUa("telegram") === false);
ok("ua url instagram", urlNeedsChromeUa("https://www.instagram.com/direct/inbox/") === true);
ok("ua url telegram 不伪装", urlNeedsChromeUa("https://web.telegram.org/k/") === false);

// ── 取原文必须摘掉我们自己注入的译文/按钮 ─────────────────────────────────────
// 迷你假 DOM：只实现 textWithoutInjected 用到的四种能力（querySelector / querySelectorAll +
// remove / cloneNode / textContent），够钉住「注入物有没有被摘掉」这一个不变量。
function fakeNode(parts) {
  const node = {
    _parts: parts.map((p) => ({ cls: p.cls, text: p.text })),
    get textContent() { return this._parts.map((p) => p.text).join(""); },
    _match(sel) {
      const want = String(sel).split(",").map((s) => s.trim().replace(/^\./, ""));
      return this._parts.filter((p) => want.indexOf(p.cls) >= 0);
    },
    querySelector(sel) { return this._match(sel)[0] || null; },
    querySelectorAll(sel) {
      const owner = this;
      return this._match(sel).map((p) => ({
        remove() { owner._parts = owner._parts.filter((x) => x !== p); },
      }));
    },
    cloneNode() { return fakeNode(this._parts); },
  };
  return node;
}

const clean = fakeNode([{ cls: "", text: "Hello there" }]);
ok("无注入物：原样返回（零克隆快路径）", textWithoutInjected(clean) === "Hello there");
const dirty = fakeNode([
  { cls: "", text: "Hello there" },
  { cls: "aitr-box", text: "你好呀" },
  { cls: "aitr-btn", text: "点击翻译" },
]);
ok("摘掉译文块与按钮", textWithoutInjected(dirty) === "Hello there");
ok("摘除只作用于克隆,原节点不动", dirty.textContent === "Hello there你好呀点击翻译");
ok("null 安全", textWithoutInjected(null) === "");

// WhatsApp 回落取文路径的回归钉：注入控件挂在 `.copyable-text` 里,而媒体气泡没有
// selectable-text → 不摘注入物就会把译文当原文读回去（"Hello there你好呀"）,污染 ingest
// 与智能回复上下文,并让原文指纹每轮判「已变」→ 陈旧标记与自动重译死循环。
const waInjected = fakeNode([
  { cls: "", text: "Hello there" },
  { cls: "aitr-box", text: "你好呀" },
]);
const waBubble = {
  classList: { contains: (c) => c === "message-in" },
  getAttribute: (k) => (k === "data-id" ? "false_1@c.us_ABC" : null),
  querySelector: (sel) => {
    const s = String(sel);
    if (s.indexOf("selectable-text") >= 0) return null; // 媒体气泡：无可选文本节点
    if (s.indexOf(".copyable-text, .copyable-area") >= 0) return waInjected;
    return null;
  },
};
ok("wa 回落取文摘掉注入译文", BUILTIN_PROFILES.whatsapp.text(waBubble) === "Hello there");

console.log(`profiles.test.js: ${pass} passed`);
