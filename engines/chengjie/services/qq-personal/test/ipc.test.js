// 边车 ⇄ agent 命名管道 RPC：真连一次（同进程模拟 agent 客户端），钉 token 鉴权 / call-ret / 事件推送 / 断连清理。
import assert from "node:assert/strict";
import net from "node:net";
import { test } from "node:test";
import { AgentServer, newPipePath, newToken } from "../ntq/qqnt-ipc.js";

const quiet = { info() {}, warn() {}, debug() {}, error() {} };

function client(pipe) {
  const sock = net.createConnection(pipe);
  const frames = [];
  let buf = "";
  sock.setEncoding("utf8");
  sock.on("data", (c) => { buf += c; let nl; while ((nl = buf.indexOf("\n")) >= 0) { const l = buf.slice(0, nl); buf = buf.slice(nl + 1); if (l.trim()) frames.push(JSON.parse(l)); } });
  const ready = new Promise((r) => sock.once("connect", r));
  return { sock, frames, ready, send(o) { sock.write(JSON.stringify(o) + "\n"); } };
}

test("bad token → 连接被断，waitHello 仍在等；good token → hello 通过、call/ret、ev 推送、断连回 agent_gone", async () => {
  const pipe = newPipePath("zhiliao-qq-test");
  const token = newToken();
  const srv = new AgentServer({ pipePath: pipe, token, logger: quiet, handshakeMs: 3000 });
  await srv.listen();
  const hello = srv.waitHello();

  const bad = client(pipe); await bad.ready;
  bad.send({ t: "hello", token: "x".repeat(token.length), pid: 1 });
  await new Promise((r) => bad.sock.once("close", r));
  assert.equal(srv.hello, null, "bad token must not attach");

  const good = client(pipe); await good.ready;
  good.send({ t: "hello", token, pid: 4242, qq_version: "9.9.26-44343", api: "nt-2025q3" });
  const h = await hello;
  assert.equal(h.pid, 4242); assert.equal(h.qq_version, "9.9.26-44343");

  const events = [];
  srv.onEvent((ev) => events.push(ev));
  // agent 端回应 call
  good.sock.on("data", () => {
    for (const f of good.frames.splice(0)) if (f.t === "call") good.send({ id: f.id, t: "ret", ok: f.m === "ping", r: { pong: f.p.x }, e: f.m === "ping" ? undefined : "boom" });
  });
  const r = await srv.call("ping", { x: 7 }, 2000);
  assert.deepEqual(r, { pong: 7 });
  await assert.rejects(srv.call("explode", {}, 2000), /boom/);
  good.send({ t: "ev", ev: { t: "login_state", state: "qr_wait" } });
  await new Promise((r2) => setTimeout(r2, 30));
  assert.deepEqual(events[0], { t: "login_state", state: "qr_wait" });

  good.sock.destroy();
  await new Promise((r2) => setTimeout(r2, 30));
  assert.ok(events.some((e) => e.t === "agent_gone"));
  await assert.rejects(srv.call("ping", {}, 500), /agent not attached/);
  await srv.close();
});

test("handshake 超时 → waitHello reject（driver 据此回退 mock）", async () => {
  const srv = new AgentServer({ pipePath: newPipePath("zhiliao-qq-test"), token: newToken(), logger: quiet, handshakeMs: 100 });
  await srv.listen();
  await assert.rejects(srv.waitHello(), /agent handshake timeout/);
  await srv.close();
});
