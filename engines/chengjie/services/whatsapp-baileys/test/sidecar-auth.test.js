/**
 * sidecar-auth.js 门禁（node --test，零新依赖）。P0-1 边车入站鉴权。
 *
 * 与 close-policy / upstream-timeout 同哲学：不 import server.js（顶层 app.listen），
 * 纯函数 + 中间件用裸 node:http 真起服务打请求；server.js 接线用源文本断言钉住。
 * 令牌在运行时拼出（不写高熵字面量，免得密钥扫描误报）。
 */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import {
  resolveSidecarToken, tokenProblem, isLoopbackHost, checkBindPolicy,
  extractToken, tokenMatches, makeSidecarAuth, MIN_TOKEN_LEN,
} from "../sidecar-auth.js";

const TOKEN = "t".repeat(16) + "-sidecar-" + "9".repeat(16);
const __dirname = path.dirname(fileURLToPath(import.meta.url));

function startServer(mw) {
  const srv = http.createServer((req, res) => {
    mw(req, res, () => {
      res.statusCode = 200;
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify({ ok: true, path: req.url }));
    });
  });
  return new Promise((resolve) => srv.listen(0, "127.0.0.1", () => resolve(srv)));
}

function req(srv, method, p, headers = {}) {
  const { port } = srv.address();
  return new Promise((resolve, reject) => {
    const r = http.request({ host: "127.0.0.1", port, method, path: p, headers }, (res) => {
      let body = "";
      res.on("data", (c) => { body += c; });
      res.on("end", () => resolve({ status: res.statusCode, body, headers: res.headers }));
    });
    r.on("error", reject);
    r.end();
  });
}

test("无 token 访问受保护路由 → 401", async () => {
  const srv = await startServer(makeSidecarAuth({ token: TOKEN }));
  try {
    for (const [m, p] of [["POST", "/accounts/a1/send"], ["GET", "/accounts"], ["POST", "/login/start"], ["POST", "/accounts/a1/logout"]]) {
      const r = await req(srv, m, p);
      assert.equal(r.status, 401, `${m} ${p}`);
      assert.match(r.body, /unauthorized/);
      assert.ok(!r.body.includes(TOKEN), "401 响应不得回显令牌");
    }
  } finally { srv.close(); }
});

test("错 token → 401；Bearer 或 X-Sidecar-Token 正确 → 200", async () => {
  const srv = await startServer(makeSidecarAuth({ token: TOKEN }));
  try {
    assert.equal((await req(srv, "GET", "/accounts", { Authorization: "Bearer wrong" })).status, 401);
    assert.equal((await req(srv, "GET", "/accounts", { Authorization: `Bearer ${TOKEN}x` })).status, 401);
    assert.equal((await req(srv, "GET", "/accounts", { "X-Sidecar-Token": "" })).status, 401);
    assert.equal((await req(srv, "GET", "/accounts", { Authorization: `Bearer ${TOKEN}` })).status, 200);
    assert.equal((await req(srv, "POST", "/accounts/a1/send", { "X-Sidecar-Token": TOKEN })).status, 200);
    assert.equal((await req(srv, "GET", "/accounts?x=1", { authorization: `bearer ${TOKEN}` })).status, 200);
  } finally { srv.close(); }
});

test("/health 无 token 放行（桌面壳/看门狗/就绪探针认身份用）", async () => {
  const srv = await startServer(makeSidecarAuth({ token: TOKEN }));
  try {
    assert.equal((await req(srv, "GET", "/health")).status, 200);
    assert.equal((await req(srv, "GET", "/health?probe=1")).status, 200);
    // 只放行精确路径：前缀相似的不放
    assert.equal((await req(srv, "GET", "/healthz")).status, 401);
    assert.equal((await req(srv, "GET", "/health/../accounts")).status, 401);
  } finally { srv.close(); }
});

test("拒绝回调收到方法与路径、不含令牌", async () => {
  const seen = [];
  const srv = await startServer(makeSidecarAuth({ token: TOKEN, onReject: (i) => seen.push(i) }));
  try {
    await req(srv, "POST", "/accounts/a1/send", { Authorization: "Bearer nope" });
    assert.equal(seen.length, 1);
    assert.equal(seen[0].method, "POST");
    assert.equal(seen[0].path, "/accounts/a1/send");
    assert.ok(!JSON.stringify(seen[0]).includes("nope"));
  } finally { srv.close(); }
});

test("未配令牌：中间件放行（仅回环监听时才会出现）", async () => {
  const srv = await startServer(makeSidecarAuth({ token: "" }));
  try {
    assert.equal((await req(srv, "GET", "/accounts")).status, 200);
  } finally { srv.close(); }
});

test("监听策略：非回环 + 无令牌拒绝启动；回环无令牌告警放行；有令牌任意地址", () => {
  assert.deepEqual(checkBindPolicy("127.0.0.1", "").mode, "loopback-open");
  assert.equal(checkBindPolicy("::1", "").ok, true);
  assert.equal(checkBindPolicy("localhost", "").ok, true);
  for (const h of ["0.0.0.0", "::", "", "192.168.0.176", "10.0.0.5"]) {
    const r = checkBindPolicy(h, "");
    assert.equal(r.ok, false, h);
    assert.equal(r.mode, "refuse", h);
  }
  assert.equal(checkBindPolicy("0.0.0.0", TOKEN).mode, "auth");
  assert.equal(checkBindPolicy("127.0.0.1", TOKEN).mode, "auth");
});

test("坏令牌（太短/占位符）一律拒绝启动，哪怕只绑回环", () => {
  assert.equal(checkBindPolicy("127.0.0.1", "short").ok, false);
  assert.equal(checkBindPolicy("127.0.0.1", "CHANGE_ME_" + "x".repeat(30)).ok, false);
  assert.match(tokenProblem("a".repeat(MIN_TOKEN_LEN - 1)), /too short/);
  assert.equal(tokenProblem(TOKEN), "");
  assert.ok(!checkBindPolicy("127.0.0.1", "qzxw").reason.includes("qzxw"), "原因里不回显令牌");
});

test("回环判定", () => {
  for (const h of ["127.0.0.1", "127.1.2.3", "::1", "[::1]", "localhost", "::ffff:127.0.0.1"]) assert.ok(isLoopbackHost(h), h);
  for (const h of ["0.0.0.0", "::", "", "192.168.0.1", "127.example.com", "::ffff:10.0.0.1"]) assert.ok(!isLoopbackHost(h), h);
});

test("取令牌与常量时间比较", () => {
  assert.equal(extractToken({ authorization: `Bearer  ${TOKEN} ` }), TOKEN);
  assert.equal(extractToken({ "x-sidecar-token": TOKEN }), TOKEN);
  assert.equal(extractToken({}), "");
  assert.ok(tokenMatches(TOKEN, TOKEN));
  assert.ok(!tokenMatches("", TOKEN));
  assert.ok(!tokenMatches(TOKEN, ""));
  assert.ok(!tokenMatches(TOKEN.slice(0, -1), TOKEN));
  assert.equal(resolveSidecarToken({ SIDECAR_TOKEN: `  ${TOKEN}\n` }), TOKEN);
  assert.equal(resolveSidecarToken({}), "");
  const src = fs.readFileSync(path.join(__dirname, "..", "sidecar-auth.js"), "utf8");
  assert.match(src, /timingSafeEqual/, "必须常量时间比较");
});

test("server.js 接线：鉴权中间件挂在 json 解析之前、拒绝策略在 listen 之前", () => {
  const src = fs.readFileSync(path.join(__dirname, "..", "server.js"), "utf8");
  assert.match(src, /from "\.\/sidecar-auth\.js"/);
  assert.match(src, /process\.env\.BIND_HOST \|\| '127\.0\.0\.1'/, "默认只绑回环");
  const iAuth = src.indexOf("app.use(makeSidecarAuth(");
  const iJson = src.indexOf("app.use(express.json())");
  const iFirstRoute = src.indexOf('app.get("/health"');
  assert.ok(iAuth > 0 && iJson > iAuth && iFirstRoute > iJson, "顺序必须是 auth → json → 路由");
  const iPolicy = src.indexOf("checkBindPolicy(HOST, SIDECAR_TOKEN)");
  const iExit = src.indexOf("process.exit(78)");
  const iListen = src.indexOf("app.listen(PORT, HOST");
  assert.ok(iPolicy > 0 && iExit > iPolicy && iListen > iExit, "不合规监听必须在 listen 前退出");
  assert.ok(!/app\.listen\(PORT\s*,\s*(async|\()/.test(src), "listen 必须显式带 HOST");
});
