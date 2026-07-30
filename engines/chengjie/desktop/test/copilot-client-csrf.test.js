"use strict";

/* 共享 copilot 客户端「写请求自带 CSRF 通行证」门禁：node test/copilot-client-csrf.test.js
 *
 * 2026-07-31 人设切换事故沉淀：写通道的 X-CSRF-Token 此前只由 workspace_base 的
 * 页面级 fetch 补丁注入——共享组件在 iframe App / 其他宿主里一张证都不带，
 * Referer 一被隐私设置剥掉就全灭且报错只有一句「请重试」。此门禁钉住传输层契约：
 *   1. _post 写请求必须带 X-CSRF-Token（cookie 有值时）；
 *   2. cookie 缺失 → 先补种（HEAD /manifest.webmanifest）再发；
 *   3. 403+code=csrf → 补种后自动重试一次（服务端零副作用，可安全重放）；
 *   4. 非 2xx 归一化 {ok:false, status, error}（组件分型提示的依据）；
 *   5. 网络异常不上抛，归一化 status:0 code:network；
 *   6. DELETE（voiceUnbind）同样带头；带 Bearer 时 Authorization 在场。
 * 源码以 repo 根 shared/copilot 为准（桌面份由 copy-shared 镜像，同步性另有
 * tests/test_copilot_shared_sync.py 门禁）。
 */
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const CLIENT_SRC = path.resolve(
  __dirname, "..", "..", "shared", "copilot", "client", "copilot-client.js");
const code = fs.readFileSync(CLIENT_SRC, "utf8");

let pass = 0;
function ok(name, cond) { assert.ok(cond, name); pass++; }

/* 构造隔离沙箱：script 为经典 IIFE（挂 window），fetch/document 全部可编程。
 * respondFn(url, opts) → {status, body}；HEAD 播种请求由 onSeed 钩子模拟服务端补种 cookie。 */
function makeSandbox({ cookie = "", respond, onSeed } = {}) {
  const calls = [];
  const sandbox = {
    console,
    URLSearchParams,
    document: { cookie },
    fetch: async (url, opts) => {
      calls.push({ url: String(url), opts: opts || {} });
      if (String(url).indexOf("/manifest.webmanifest") >= 0) {
        if (onSeed) onSeed(sandbox);
        return { ok: true, status: 200, statusText: "OK", json: async () => ({}) };
      }
      const r = respond(String(url), opts || {}, calls.length);
      if (r instanceof Error) throw r;
      return {
        ok: r.status >= 200 && r.status < 300,
        status: r.status,
        statusText: r.statusText || "",
        json: async () => {
          if (r.nonJson) throw new Error("invalid json");
          return r.body;
        },
      };
    },
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(code, sandbox);
  return { sandbox, calls, shared: sandbox.CopilotShared };
}

function hdr(call, name) {
  const h = (call.opts && call.opts.headers) || {};
  return h[name];
}

(async () => {
  // ── 1. cookie 在场：写请求带 X-CSRF-Token ──
  {
    const { calls, shared } = makeSandbox({
      cookie: "sid=1; csrf_token=tok123",
      respond: () => ({ status: 200, body: { ok: true, to: "p1" } }),
    });
    const c = new shared.WebCopilotClient();
    const r = await c.bindConvPersona({ conversationId: "telegram:a:b", profileId: "p1" });
    ok("成功响应原样透传", r.ok === true && r.to === "p1");
    ok("只发一次请求（无多余播种）", calls.length === 1);
    ok("写请求带 X-CSRF-Token", hdr(calls[0], "X-CSRF-Token") === "tok123");
    ok("Content-Type JSON", hdr(calls[0], "Content-Type") === "application/json");
  }

  // ── 2. cookie 缺失：先补种再发，头用新种的值 ──
  {
    const { calls, shared } = makeSandbox({
      cookie: "sid=1",
      onSeed: (sb) => { sb.document.cookie = "sid=1; csrf_token=freshTok"; },
      respond: () => ({ status: 200, body: { ok: true } }),
    });
    const c = new shared.WebCopilotClient();
    const r = await c.bindConvPersona({ conversationId: "t:a:b", profileId: "p" });
    ok("缺 cookie 时先补种（HEAD manifest）", calls[0].url.indexOf("/manifest.webmanifest") >= 0);
    ok("补种请求用 HEAD", (calls[0].opts.method || "") === "HEAD");
    ok("补种后写请求带新 token", hdr(calls[1], "X-CSRF-Token") === "freshTok");
    ok("补种后成功", r.ok === true);
  }

  // ── 3. 403+code=csrf → 补种 + 重试一次；仍失败则归一化透出 ──
  {
    let posts = 0;
    const { calls, shared } = makeSandbox({
      cookie: "csrf_token=oldTok",
      onSeed: (sb) => { sb.document.cookie = "csrf_token=newTok"; },
      respond: () => {
        posts++;
        return { status: 403, body: { detail: "CSRF token missing or invalid", code: "csrf" } };
      },
    });
    const c = new shared.WebCopilotClient();
    const r = await c.bindConvPersona({ conversationId: "t:a:b", profileId: "p" });
    ok("csrf 403 自动重试恰好一次", posts === 2);
    ok("重试间隔有播种请求", calls.some((x) => x.url.indexOf("manifest") >= 0));
    ok("重试用了新 token", hdr(calls[calls.length - 1], "X-CSRF-Token") === "newTok");
    ok("重试仍败 → ok:false", r.ok === false);
    ok("状态码透传", r.status === 403);
    ok("code 透传（组件分型依据）", r.code === "csrf");
  }

  // ── 4. 非 2xx 归一化：error 取后端 detail ──
  {
    const { shared } = makeSandbox({
      cookie: "csrf_token=t",
      respond: () => ({ status: 404, body: { detail: "人设不存在" } }),
    });
    const c = new shared.WebCopilotClient();
    const r = await c.bindConvPersona({ conversationId: "t:a:b", profileId: "ghost" });
    ok("404 → ok:false", r.ok === false);
    ok("404 → status 透传", r.status === 404);
    ok("404 → error=detail", r.error === "人设不存在");
  }

  // ── 5. 网络异常：不上抛，status:0 code:network ──
  {
    const { shared } = makeSandbox({
      cookie: "csrf_token=t",
      respond: () => new Error("connection refused"),
    });
    const c = new shared.WebCopilotClient();
    const r = await c.bindConvPersona({ conversationId: "t:a:b", profileId: "p" });
    ok("网络异常不抛", r && r.ok === false);
    ok("网络异常 status:0", r.status === 0);
    ok("网络异常 code:network", r.code === "network");
  }

  // ── 6. 2xx 非 JSON：按失败归一化（旧行为=抛给组件 catch，语义不回退） ──
  {
    const { shared } = makeSandbox({
      cookie: "csrf_token=t",
      respond: () => ({ status: 200, body: null, nonJson: true }),
    });
    const c = new shared.WebCopilotClient();
    const r = await c.bindConvPersona({ conversationId: "t:a:b", profileId: "p" });
    ok("2xx 非 JSON → ok:false + badjson", r.ok === false && r.code === "badjson");
  }

  // ── 7. voiceUnbind（DELETE）同样带 CSRF 头 ──
  {
    const { calls, shared } = makeSandbox({
      cookie: "csrf_token=delTok",
      respond: () => ({ status: 200, body: { ok: true } }),
    });
    const c = new shared.WebCopilotClient();
    await c.voiceUnbind({ persona_id: "p1" });
    const del = calls.find((x) => (x.opts.method || "") === "DELETE");
    ok("DELETE 请求存在", !!del);
    ok("DELETE 带 X-CSRF-Token", hdr(del, "X-CSRF-Token") === "delTok");
  }

  // ── 8. Bearer 模式：Authorization 在场且不做播种（桌面 iframe 跨站 cookie 本就不可用） ──
  {
    const { calls, shared } = makeSandbox({
      cookie: "",
      respond: () => ({ status: 200, body: { ok: true } }),
    });
    shared.setAuthToken("bearer-xyz");
    const c = new shared.WebCopilotClient();
    await c.bindConvPersona({ conversationId: "t:a:b", profileId: "p" });
    ok("Bearer 模式不播种（首个请求即业务 POST）",
       calls[0].url.indexOf("manifest") < 0);
    ok("Authorization 在场", hdr(calls[0], "Authorization") === "Bearer bearer-xyz");
    shared.setAuthToken("");
  }

  console.log(`copilot-client-csrf.test.js: ${pass} passed`);
})().catch((e) => { console.error(e); process.exit(1); });
