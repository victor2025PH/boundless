/**
 * 入站鉴权（P0-1，2026-10-08）：边车所有路由（/health 除外）要求调用方带边车专用令牌。
 *
 * 为什么存在：server.js 暴露 /accounts/:id/send、logout、login/start 等账号操作面，
 * 历史上**零入站鉴权**，只靠「默认绑回环」挡；而一旦有人用 BIND_HOST 放开或旧版
 * `app.listen(PORT)` 绑全网卡（176:8790 实测局域网可达），同网段任何人都能代发、
 * 登出、配新号。本模块把鉴权做成零依赖纯函数 + 中间件，可被 node --test 单测
 * （server.js 顶层 app.listen，测试没法安全 import 它）。
 *
 * 契约：
 * - 令牌来源：环境变量 SIDECAR_TOKEN（start.ps1 从实例 config 目录的
 *   wa_sidecar_token.key 读出，不存在就生成；Python 侧读同一文件）。
 *   **独立令牌**：不再复用 web_admin 管理 token（管理/总线/边车一把钥匙，泄露面太大）。
 * - 调用方二选一：`Authorization: Bearer <token>` 或 `X-Sidecar-Token: <token>`。
 * - 比较用常量时间（两侧先 sha256 成等长摘要再 timingSafeEqual，长度不同也不早退）。
 * - /health 永远放行：桌面壳/看门狗/就绪探针靠它认身份（svc 字段），不带令牌。
 * - 未配令牌：只允许回环监听（向后兼容桌面壳与老部署，启动时告警）；
 *   非回环 + 无令牌 → 拒绝启动（fail-closed，绝不把无鉴权的账号操作面开到局域网）。
 * - 令牌太短或像占位符 → 视为配置错误，拒绝启动（比不配更危险：可猜的钥匙）。
 */

import crypto from "node:crypto";

export const OPEN_PATHS = Object.freeze(["/health"]);
export const MIN_TOKEN_LEN = 24;
const PLACEHOLDER_MARKS = ["CHANGE_ME", "CHANGEME", "YOUR_", "PLACEHOLDER", "EXAMPLE", "<TOKEN>"];

/** 读令牌：SIDECAR_TOKEN（trim 后）；空串表示未配置。 */
export function resolveSidecarToken(env) {
  const raw = (env && env.SIDECAR_TOKEN) || "";
  return String(raw).trim();
}

/** 令牌自检：返回 "" 表示可用，否则返回原因（不含令牌本身）。 */
export function tokenProblem(token) {
  const t = String(token || "");
  if (!t) return "";
  if (t.length < MIN_TOKEN_LEN) return `too short (${t.length} < ${MIN_TOKEN_LEN})`;
  const up = t.toUpperCase();
  for (const m of PLACEHOLDER_MARKS) if (up.includes(m)) return "looks like a placeholder";
  return "";
}

/** 是否回环地址（127.0.0.0/8、::1、localhost、IPv4 映射回环）。空串/0.0.0.0/:: 都不是。 */
export function isLoopbackHost(host) {
  let h = String(host || "").trim().toLowerCase();
  if (!h) return false;
  if (h.startsWith("[") && h.endsWith("]")) h = h.slice(1, -1);
  if (h === "localhost" || h === "::1" || h === "0:0:0:0:0:0:0:1") return true;
  if (h.startsWith("::ffff:")) h = h.slice(7);
  return /^127(\.\d{1,3}){3}$/.test(h);
}

/**
 * 启动前的监听策略判定。
 * @returns {{ok: boolean, mode: "auth"|"loopback-open"|"refuse", reason: string}}
 */
export function checkBindPolicy(host, token) {
  const bad = tokenProblem(token);
  if (bad) return { ok: false, mode: "refuse", reason: `SIDECAR_TOKEN ${bad}` };
  if (token) return { ok: true, mode: "auth", reason: "" };
  if (isLoopbackHost(host)) {
    return { ok: true, mode: "loopback-open", reason: "SIDECAR_TOKEN not set: inbound auth disabled (loopback only)" };
  }
  return {
    ok: false, mode: "refuse",
    reason: `refusing to listen on non-loopback host '${host}' without SIDECAR_TOKEN`,
  };
}

/** 从请求头取令牌：Bearer 优先，其次 X-Sidecar-Token。 */
export function extractToken(headers) {
  const h = headers || {};
  const auth = String(h.authorization || h.Authorization || "");
  const m = /^Bearer\s+(.+)$/i.exec(auth.trim());
  if (m) return m[1].trim();
  const x = h["x-sidecar-token"] || h["X-Sidecar-Token"] || "";
  return String(Array.isArray(x) ? x[0] : x).trim();
}

/** 常量时间比较：两侧先摘要成 32 字节再 timingSafeEqual（长度不同不早退、不泄露长度）。 */
export function tokenMatches(given, expected) {
  if (!expected) return false;
  const a = crypto.createHash("sha256").update(String(given || ""), "utf8").digest();
  const b = crypto.createHash("sha256").update(String(expected), "utf8").digest();
  return crypto.timingSafeEqual(a, b) && String(given || "").length > 0;
}

function pathOf(req) {
  const u = String(req.path || req.url || "/");
  const q = u.indexOf("?");
  return q >= 0 ? u.slice(0, q) : u;
}

/**
 * 中间件（Express 与裸 node:http 通用：只用 res.statusCode/setHeader/end）。
 * token 为空 → 直接放行（只有 checkBindPolicy 判定 loopback-open 时才会走到这里）。
 */
export function makeSidecarAuth({ token, openPaths = OPEN_PATHS, onReject = null } = {}) {
  const open = new Set(openPaths);
  return function sidecarAuth(req, res, next) {
    if (!token) return next();
    if (open.has(pathOf(req))) return next();
    if (tokenMatches(extractToken(req.headers), token)) return next();
    if (typeof onReject === "function") {
      try { onReject({ method: req.method, path: pathOf(req), remote: req.socket && req.socket.remoteAddress }); } catch (_e) { /* 日志失败不影响拒绝 */ }
    }
    res.statusCode = 401;
    res.setHeader("Content-Type", "application/json; charset=utf-8");
    res.setHeader("WWW-Authenticate", 'Bearer realm="wa-baileys"');
    res.end(JSON.stringify({ ok: false, error: "unauthorized" }));
  };
}
