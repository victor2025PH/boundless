"use strict";

// #254 D-P2「允许局域网设备连接」开关的纯决策层（node 直跑单测：test/lan-access.test.js）。
//
// 出厂态：后端只绑 127.0.0.1（backend-launcher.lanServeHost 只认 backend.lan_access===true）。
// 手机扫码操控需要局域网入口时，用户在系统设置显式打开本开关：
//   · 写 config.json backend.lan_access=true，重启后端后 serve 升 0.0.0.0；
//   · Windows 当前网络类别为 Public（咖啡馆 / 机场 Wi-Fi）时，打开需要二次确认——
//     MTRCH2 复测就是「公共 Wi-Fi 上后台整个暴露」。
// 本文件不碰 Electron / 进程：网络类别探测与 config 写盘由 main.js 注入。

const IPV4_PRIVATE = [
  /^10\./,
  /^192\.168\./,
  /^172\.(1[6-9]|2\d|3[01])\./,
];

/**
 * 从 os.networkInterfaces() 结果挑一个「手机最可能连得上」的本机 IPv4。
 * 私网段优先，其次任意非回环 IPv4；没有 → ""。
 * @param {Record<string, Array<{family:string|number, address:string, internal:boolean}>>} ifaces
 * @returns {string}
 */
function pickLanIp(ifaces) {
  const all = [];
  for (const list of Object.values(ifaces || {})) {
    for (const it of (list || [])) {
      if (!it || it.internal) continue;
      const fam = String(it.family);
      if (fam !== "IPv4" && fam !== "4") continue;
      const ip = String(it.address || "").trim();
      if (!ip || ip.startsWith("169.254.")) continue;   // APIPA 无网关，手机连不上
      all.push(ip);
    }
  }
  const priv = all.find((ip) => IPV4_PRIVATE.some((re) => re.test(ip)));
  return priv || all[0] || "";
}

/**
 * 解析 `Get-NetConnectionProfile | Select -Expand NetworkCategory` 的输出。
 * 多张网卡取最坏：任一 Public 即 "Public"；否则有 Private/DomainAuthenticated 取之；空 → ""。
 * @param {string} out
 * @returns {""|"Public"|"Private"|"DomainAuthenticated"}
 */
function parseNetworkCategory(out) {
  const lines = String(out || "").split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
  let seen = "";
  for (const ln of lines) {
    if (/^public$/i.test(ln)) return "Public";
    if (/^private$/i.test(ln)) seen = seen || "Private";
    else if (/^domainauthenticated$/i.test(ln)) seen = seen || "DomainAuthenticated";
  }
  return seen;
}

/**
 * 开关切换决策。
 * @param {{on:boolean, confirmed?:boolean, category?:string}} p
 * @returns {{allow:boolean, needs_confirm:boolean, category:string}}
 */
function decideToggle(p) {
  const on = !!(p && p.on);
  const category = String((p && p.category) || "");
  if (!on) return { allow: true, needs_confirm: false, category };
  if (category === "Public" && !(p && p.confirmed === true)) {
    return { allow: false, needs_confirm: true, category };
  }
  return { allow: true, needs_confirm: false, category };
}

/**
 * 供设置页 / 壳横幅消费的状态快照。
 * @param {object} config desktop config.json
 * @param {{lanIp?:string}} [opts]
 * @returns {{lan_access:boolean, lan_ip:string, port:string, lan_url:string}}
 */
function lanStatus(config, opts) {
  const b = (config && config.backend) || {};
  const lan_access = b.lan_access === true;
  let port = "18799";
  try {
    if (b.base_url) port = new URL(String(b.base_url)).port || port;
  } catch (e) { /* 非法 base_url：用默认端口 */ }
  const lan_ip = String((opts && opts.lanIp) || "");
  return {
    lan_access,
    lan_ip,
    port,
    lan_url: lan_ip ? `http://${lan_ip}:${port}` : "",
  };
}

module.exports = { pickLanIp, parseNetworkCategory, decideToggle, lanStatus };
