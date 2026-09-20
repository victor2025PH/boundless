"use strict";

// 托管令牌硬化纯逻辑单测：node test/token-util.test.js
const assert = require("assert");
const { DEFAULT_TOKEN, shouldRotateToken, generateToken } = require("../token-util.js");

let pass = 0;
function ok(name, cond) {
  assert.ok(cond, name);
  pass++;
}

// ── 何时换：仅托管 && 出厂默认/空 ──
ok("托管+默认令牌 → 换", shouldRotateToken(true, "admin") === true);
ok("托管+空令牌 → 换", shouldRotateToken(true, "") === true);
ok("托管+null → 换", shouldRotateToken(true, null) === true);
ok("托管+空白串 → 换", shouldRotateToken(true, "   ") === true);
ok("托管+已随机化 → 不动", shouldRotateToken(true, "a1b2c3d4") === false);
ok("托管+用户自配 → 不动", shouldRotateToken(true, "my-secret") === false);
ok("非托管永不换（开发态要可预期的 admin）", shouldRotateToken(false, "admin") === false);
ok("非托管空令牌也不换", shouldRotateToken(false, "") === false);
ok("默认令牌常量对齐", DEFAULT_TOKEN === "admin");

// ── 生成：48 位 hex，注入可复现，默认随机不重复 ──
const fixed = generateToken(function (n) { return Buffer.alloc(n, 7); });
ok("注入随机源可复现", fixed === Buffer.alloc(24, 7).toString("hex"));
ok("长度 48", fixed.length === 48);
const t1 = generateToken();
const t2 = generateToken();
ok("默认随机 48 位 hex", /^[0-9a-f]{48}$/.test(t1));
ok("两次生成不同", t1 !== t2);
ok("生成值不是默认令牌", shouldRotateToken(true, t1) === false);

console.log(`token-util.test.js: ${pass} passed`);
