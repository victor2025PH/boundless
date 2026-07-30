"use strict";

/* 会话人设组件「失败分型纯核心」常驻门禁：node test/cp-persona-failtype.test.js
 *
 * 背景（2026-07-31 人设切换事故 P3 收口）：P1 把「所有失败一律『请重试』」拆成
 * 按 HTTP 状态分型的文案 + 出路按钮 + 上报口径，但那三段逻辑此前只有真浏览器
 * verify（SKIP-able、非常驻）覆盖——而分型本身（改一个 status 分支，坐席看到的
 * 文案/出路、看板收到的类型就变了）恰恰最容易随手改错。P3 把它抽成四个不依赖
 * this/DOM/i18n 的纯方法（_failKey/_failEtype/_failActions/_failAssertsUnchanged），
 * 本门禁逐分支钉死。真浏览器层（tools/verify_persona_failtip.py）另验「分型→真的
 * 渲染成对应 DOM/按钮」的接线。
 *
 * 源码以 repo 根 shared/copilot 为准（桌面份由 copy-shared 镜像，双树字节一致由
 * tests/test_copilot_shared_sync.py 保证 → 本门禁跑一次即覆盖两端）。
 * 加载方式：vm 沙箱塞最小 stub（空 Base + customElements + HTMLElement），组件
 * IIFE 挂 window 后从 customElements 取类，纯方法经 prototype 直接 call（不实例化，
 * 不触发 constructor 的 shadow DOM）。
 */
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SRC = path.resolve(
  __dirname, "..", "..", "shared", "copilot", "components", "cp-persona.js");
const BASE_SRC = path.resolve(
  __dirname, "..", "..", "shared", "copilot", "components", "cp-panel-base.js");

let pass = 0;
function ok(name, cond) { assert.ok(cond, name); pass++; }
// 字符串结果用严格相等（primitive 跨 realm 安全）
function eq(name, a, b) { assert.strictEqual(a, b, `${name}: ${JSON.stringify(a)} != ${JSON.stringify(b)}`); pass++; }
// 数组结果用 join 比较——组件在 vm 沙箱 realm 里造的数组原型 ≠ 主 realm，
// deepStrictEqual 会因 [[Prototype]] 不同而误判不等；join 成字符串规避（realm 无关）。
function eqArr(name, a, b) { assert.strictEqual(a.join("|"), b.join("|"), `${name}: ${JSON.stringify(a)} != ${JSON.stringify(b)}`); pass++; }

// —— 加载组件类（最小沙箱；只为拿到 prototype 上的纯方法）——
function loadCpPersona() {
  const sandbox = {
    console,
    HTMLElement: class {},
    customElements: {
      _m: {},
      get(n) { return this._m[n]; },
      define(n, c) { this._m[n] = c; },
    },
  };
  sandbox.window = sandbox;
  // 组件顶部要求 CopilotShared.CpPanelBase 存在，否则 early-return 不定义类。
  // 真 Base 依赖 HTMLElement/attachShadow；纯方法不碰 Base，故塞空类即可。
  sandbox.CopilotShared = { CpPanelBase: class {} };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(SRC, "utf8"), sandbox);
  const C = sandbox.customElements.get("cp-persona");
  assert.ok(C, "cp-persona 类应已定义（沙箱 stub 生效）");
  return C;
}

const Cls = loadCpPersona();
const P = Cls.prototype;
const key = (s, c, d) => P._failKey.call(null, s, c, d);
const etype = (s, c) => P._failEtype.call(null, s, c);
const acts = (s, c) => P._failActions.call(null, s, c).map((a) => a.act);
const actLabels = (s, c) => P._failActions.call(null, s, c).map((a) => a.label);
const asserts = (s) => P._failAssertsUnchanged.call(null, s);

// ── 结构自证：四个纯方法真的挂在 prototype 上（防重构漏抽/改名） ──
["_failKey", "_failEtype", "_failActions", "_failAssertsUnchanged"].forEach((m) => {
  ok(`prototype 有 ${m}`, typeof P[m] === "function");
});

// ── _failKey：每个分支 + 兜底路径 ──
eq("net(status0)", key(0, "", ""), "cp.persona.fail_net");
eq("auth(401)", key(401, "", ""), "cp.persona.fail_auth");
eq("csrf via code", key(403, "csrf", ""), "cp.persona.fail_csrf");
eq("csrf via code 优先于 403", key(403, "csrf", "whatever"), "cp.persona.fail_csrf");
eq("csrf via detail(旧后端无 code)", key(403, "", "CSRF token missing or invalid"),
   "cp.persona.fail_csrf");
eq("denied(403 无 csrf 特征)", key(403, "", "forbidden"), "cp.persona.fail_denied");
eq("gone(404)", key(404, "", ""), "cp.persona.fail_gone");
eq("conflict(409)", key(409, "", ""), "cp.persona.fail_conflict");
eq("未分型(500)", key(500, "", ""), "");
eq("未分型(400)", key(400, "", ""), "");
// code=csrf 即便状态非 403 也判 csrf（客户端归一化后 status 可能为 0/403）
eq("csrf code 不依赖 403", key(0, "csrf", ""), "cp.persona.fail_net");  // status0 优先级更高
eq("csrf code 在 400", key(400, "csrf", ""), "cp.persona.fail_csrf");

// ── _failEtype：白名单内的 HTTP 分型（must match frontend_error_stats._KNOWN_TYPES） ──
eq("etype net", etype(0, ""), "neterr");
eq("etype 401", etype(401, ""), "http_401");
eq("etype 403 csrf", etype(403, "csrf"), "http_403_csrf");
eq("etype 403 plain", etype(403, ""), "http_403");
eq("etype 404", etype(404, ""), "http_404");
eq("etype 409", etype(409, ""), "http_409");
eq("etype 500", etype(500, ""), "http_5xx");
eq("etype 503", etype(503, ""), "http_5xx");
eq("etype 其他 → Error", etype(418, ""), "Error");

// ── _failActions：确定性拒绝绝不给「重试」，网络/5xx 才给 ──
eqArr("401 出路=重新登录", acts(401, ""), ["fail-reload"]);
eqArr("401 label", actLabels(401, ""), ["cp.persona.act_relogin"]);
eqArr("csrf 出路=刷新页面", acts(403, "csrf"), ["fail-reload"]);
eqArr("csrf label", actLabels(403, "csrf"), ["cp.persona.act_reload"]);
eqArr("403 出路=刷新页面", acts(403, ""), ["fail-reload"]);
eqArr("404 出路=刷新面板", acts(404, ""), ["fail-refresh"]);
eqArr("409 出路=刷新面板", acts(409, ""), ["fail-refresh"]);
eqArr("网络 出路=重试+刷新", acts(0, ""), ["fail-retry", "fail-refresh"]);
eqArr("5xx 出路=重试+刷新", acts(500, ""), ["fail-retry", "fail-refresh"]);
// 核心不变量：确定性拒绝（401/403/404/409）绝不出现 fail-retry
[401, 403, 404, 409].forEach((s) => {
  ok(`${s} 不给「重试」（确定性失败重试必复现）`, acts(s, "").indexOf("fail-retry") < 0);
  ok(`${s} 不给「重试」(csrf)`, acts(s, "csrf").indexOf("fail-retry") < 0);
});

// ── _failAssertsUnchanged：仅 4xx 断言「未生效」，网络/5xx 绝不谎报状态 ──
ok("400 断言未生效", asserts(400) === true);
ok("401 断言未生效", asserts(401) === true);
ok("409 断言未生效", asserts(409) === true);
ok("499 断言未生效", asserts(499) === true);
ok("网络(0) 不断言（可能已落地）", asserts(0) === false);
ok("500 不断言（可能已落地）", asserts(500) === false);
ok("200 不断言", asserts(200) === false);

// ── 一致性：出路与状态断言口径不打架（4xx 必有确定性出路，非 4xx 必含重试） ──
[400, 401, 403, 404, 409].forEach((s) => {
  ok(`${s} 断言未生效`, asserts(s) === true);
});
[0, 500, 503].forEach((s) => {
  ok(`${s} 给重试且不断言`, acts(s, "").indexOf("fail-retry") >= 0 && asserts(s) === false);
});

console.log(`cp-persona-failtype.test.js: ${pass} passed`);
