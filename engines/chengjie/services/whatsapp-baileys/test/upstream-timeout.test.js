/**
 * upstream-timeout.js 门禁（node --test，零新依赖）。
 *
 * 为什么存在：WA socket 半死时 sock.profilePictureUrl 无限挂起（Baileys 无内建超时），
 * Express 响应挂死 → 调用方连接被占满（2026-08-05 实锤：每个头像挂 20s+，几行 WA
 * 会话占死浏览器同源 6 连接、整页请求饿死）。本模块是服务端 8s 兜底自保；
 * 熔断判定权留在 Python 层 4s（两层值刻意不同，见模块 docstring）。
 *
 * 与 close-policy.test.js 同哲学：不 import server.js（顶层 app.listen），
 * 纯函数模块单测 + server.js 接线用源文本断言钉住。
 */
import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { withTimeout, UpstreamTimeoutError, AVATAR_QUERY_TIMEOUT_MS } from "../upstream-timeout.js";

test("快 promise：值原样透传", async () => {
  const v = await withTimeout(Promise.resolve("https://pps.whatsapp.net/x.jpg"), 1000);
  assert.equal(v, "https://pps.whatsapp.net/x.jpg");
});

test("挂起 promise：到点 reject UpstreamTimeoutError（挂死收口）", async () => {
  const hang = new Promise(() => {});   // 永不 settle = 半死 socket 的 query
  await assert.rejects(withTimeout(hang, 30), UpstreamTimeoutError);
});

test("上游 reject：原错误透传（不被吞成超时）", async () => {
  await assert.rejects(
    withTimeout(Promise.reject(new Error("item-not-found")), 1000),
    /item-not-found/);
});

test("超时值分层：Node 兜底(8s) 必须大于 Python 熔断层(4s)——判定权留在 Python", () => {
  assert.equal(AVATAR_QUERY_TIMEOUT_MS, 8000);
  assert.ok(AVATAR_QUERY_TIMEOUT_MS > 4000);
});

test("server.js 接线：avatar 端点必须经 withTimeout 包裹 + 超时回 504", () => {
  const dir = path.dirname(fileURLToPath(import.meta.url));
  const src = fs.readFileSync(path.join(dir, "..", "server.js"), "utf8");
  const seg = src.slice(src.indexOf('app.get("/accounts/:id/avatar"'));
  const handler = seg.slice(0, seg.indexOf("app.", 10));
  assert.ok(handler.includes("withTimeout("), "profilePictureUrl 失去超时包裹（挂死回归）");
  assert.ok(handler.includes("UpstreamTimeoutError"), "超时分支缺失");
  assert.ok(handler.includes("504"), "超时应回 504（与「无头像空 url」语义区分）");
});
