"use strict";

// #254 D-P2 「允许局域网设备连接」纯决策层单测：node test/lan-access.test.js
const assert = require("assert");
const { pickLanIp, parseNetworkCategory, decideToggle, lanStatus } = require("../lan-access.js");

let pass = 0;
function ok(name, cond) { assert.ok(cond, name); pass++; }

// ── pickLanIp ────────────────────────────────────────────────────────────────
ok("私网 IPv4 优先于公网",
  pickLanIp({
    eth: [{ family: "IPv4", address: "203.0.113.5", internal: false }],
    wifi: [{ family: "IPv4", address: "192.168.1.23", internal: false }],
  }) === "192.168.1.23");
ok("跳过回环 / IPv6 / APIPA",
  pickLanIp({
    lo: [{ family: "IPv4", address: "127.0.0.1", internal: true }],
    v6: [{ family: "IPv6", address: "fe80::1", internal: false }],
    apipa: [{ family: "IPv4", address: "169.254.3.3", internal: false }],
  }) === "");
ok("Node 18 family 数字 4 也认",
  pickLanIp({ a: [{ family: 4, address: "10.0.0.8", internal: false }] }) === "10.0.0.8");
ok("空入参不崩", pickLanIp(null) === "" && pickLanIp({}) === "");

// ── parseNetworkCategory ─────────────────────────────────────────────────────
ok("任一 Public 即 Public（多网卡取最坏）",
  parseNetworkCategory("Private\r\nPublic\r\n") === "Public");
ok("全 Private → Private", parseNetworkCategory("Private\n") === "Private");
ok("域网络", parseNetworkCategory("DomainAuthenticated") === "DomainAuthenticated");
ok("空输出 → 未知", parseNetworkCategory("") === "" && parseNetworkCategory(null) === "");
ok("大小写宽容", parseNetworkCategory("public") === "Public");

// ── decideToggle ─────────────────────────────────────────────────────────────
ok("关开关永远放行", decideToggle({ on: false, category: "Public" }).allow === true);
ok("Private 网络直接开",
  decideToggle({ on: true, category: "Private" }).allow === true);
ok("Public 网络首次开 → 需二次确认",
  (() => { const d = decideToggle({ on: true, category: "Public" }); return d.allow === false && d.needs_confirm === true && d.category === "Public"; })());
ok("Public 网络已确认 → 放行",
  decideToggle({ on: true, category: "Public", confirmed: true }).allow === true);
ok("网络类别未知（非 win / 探测失败）→ 不拦（fail-open 但仍在设置页明示）",
  decideToggle({ on: true, category: "" }).allow === true);
ok("confirmed 只认布尔 true",
  decideToggle({ on: true, category: "Public", confirmed: "true" }).needs_confirm === true);

// ── lanStatus ────────────────────────────────────────────────────────────────
const stOff = lanStatus({ backend: { base_url: "http://127.0.0.1:18799" } }, { lanIp: "192.168.1.5" });
ok("默认 lan_access=false", stOff.lan_access === false);
ok("端口从 base_url 取", stOff.port === "18799" && stOff.lan_url === "http://192.168.1.5:18799");
ok("显式 true 才算开",
  lanStatus({ backend: { lan_access: true } }, {}).lan_access === true
  && lanStatus({ backend: { lan_access: "true" } }, {}).lan_access === false);
ok("无 IP 时 lan_url 为空", lanStatus({ backend: {} }, {}).lan_url === "");
ok("空 config 不崩", lanStatus(null).lan_access === false);

console.log(`lan-access.test.js: ${pass} passed`);
