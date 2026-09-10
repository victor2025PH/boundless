// 驱动契约测试（B 段）：mock 与 qqnt 两个实现对**同一张契约表**跑同一组 shape 断言。
// qqnt 侧不碰真 QQ：注入一个假 agent（实现 qqnt-ipc 的 listen/waitHello/call/onEvent/close），
// 验证的是「驱动 ⇄ agent 协议 + 段转换 + 返回 shape」这一层；真机行为归 C 段。
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { test } from "node:test";
import { createDriver } from "../ntq/driver.js";
import { createMockDriver } from "../ntq/mock-driver.js";
import { createQqntDriver, ensureInjected } from "../ntq/qqnt-driver.js";
import { imageMeta, milkyToElements, recordToMilky } from "../ntq/qqnt-elements.js";
import { SUPPORTED_QQNT, UnsupportedQqVersion, isInjectableBuild, resolveQqntTarget } from "../ntq/qqnt-versions.js";

const quiet = { info() {}, warn() {}, debug() {}, error() {} };

// ── 契约表：方法名 → 参数 → 返回 shape 校验（登录后）───────────────────────────
const isInt = (v) => Number.isInteger(v);
const isStr = (v) => typeof v === "string";
const CONTRACT = [
  ["getImplInfo", [], (r) => { assert.ok(isStr(r.impl_name) && r.impl_name); assert.ok(isStr(r.impl_version)); assert.ok(isStr(r.qq_version)); assert.equal(r.milky_version, "1.3"); }],
  ["sendPrivate", [42, [{ type: "text", data: { text: "hi" } }]], (r) => { assert.equal(r.ok, true); assert.ok(isInt(r.message_seq)); }],
  ["sendGroup", [10086, [{ type: "text", data: { text: "hi" } }, { type: "mention", data: { user_id: 42 } }]], (r) => { assert.equal(r.ok, true); assert.ok(isInt(r.message_seq)); }],
  ["recallPrivate", [42, 5001], (r) => assert.equal(r.ok, true)],
  ["recallGroup", [10086, 5002], (r) => assert.equal(r.ok, true)],
  ["markRead", ["friend", 42, 5001], (r) => assert.equal(r.ok, true)],
  ["uploadPrivateFile", [42, "file:///" + "__TMPFILE__", "a.txt"], (r) => { assert.equal(r.ok, true); assert.ok(isStr(r.file_id) && r.file_id); }],
  ["uploadGroupFile", [10086, "file:///" + "__TMPFILE__", "a.txt"], (r) => { assert.equal(r.ok, true); assert.ok(isStr(r.file_id) && r.file_id); }],
  ["privateFileUrl", [42, "f1", ""], (r) => { assert.equal(r.ok, true); assert.match(r.download_url, /^https?:\/\//); }],
  ["groupFileUrl", [10086, "f1"], (r) => { assert.equal(r.ok, true); assert.match(r.download_url, /^https?:\/\//); }],
  ["kickGroupMember", [10086, 42, false], (r) => assert.equal(r.ok, true)],
  ["setGroupName", [10086, "新群名"], (r) => assert.equal(r.ok, true)],
  ["acceptFriend", ["u_abc", false], (r) => assert.equal(r.ok, true)],
];
// 未登录时这些必须回 {ok:false, retcode:-403}，绝不抛
const GUARDED = ["sendPrivate", "sendGroup", "recallPrivate", "recallGroup", "markRead", "uploadPrivateFile",
  "uploadGroupFile", "privateFileUrl", "groupFileUrl", "kickGroupMember", "setGroupName", "acceptFriend"];

function tmpFile() {
  const p = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "qqdrv-")), "a.txt");
  fs.writeFileSync(p, "hello");
  return p;
}
function fileUri(p) { return "file:///" + p.replace(/\\/g, "/").replace(/^\//, ""); }

// ── 假 agent：模拟 QQ 主进程内 agent 的 RPC 面（协议与 qqnt-agent.cjs 一致）─────────
function fakeAgent() {
  let evCb = null; let seq = 7000; let loggedIn = false;
  const calls = [];
  const a = {
    calls,
    async listen() {}, async waitHello() { return { qq_version: "9.9.26-44343", pid: 1, api: "nt-2025q3" }; },
    onEvent(cb) { evCb = cb; },
    push(ev) { evCb?.(ev); },
    async call(m, p) {
      calls.push([m, p]);
      switch (m) {
        case "ping": return { pid: 1, login_state: loggedIn ? "logged_in" : "logged_out", qq_version: "9.9.26-44343", api: "nt-2025q3" };
        case "login_state": return { state: loggedIn ? "logged_in" : "logged_out", self: loggedIn ? { uin: 10001, nickname: "测试号" } : null };
        case "qr_start": setTimeout(() => { evCb?.({ t: "qr", png_base64: "iVBORw0KGgo=", url: "https://ti.qq.com/x" }); }, 10); return { ok: true };
        case "quick_login_list": return [{ uin: 10001, nickname: "测试号" }];
        case "quick_login": loggedIn = true; setTimeout(() => evCb?.({ t: "login_state", state: "logged_in", self: { uin: 10001, nickname: "测试号" } }), 5); return { ok: true };
        case "send": {
          // 契约：elements 已是 NT 形状；files 已落盘为本机路径
          assert.ok(Array.isArray(p.elements) && p.elements.length);
          for (const f of p.files || []) assert.ok(fs.existsSync(f.uri), "driver must stage files before calling agent");
          return { message_seq: ++seq, msg_id: `m${seq}` };
        }
        case "recall": case "mark_read": case "kick": case "set_group_name": case "accept_friend": return { ok: true };
        case "upload_file": return { file_id: `m${++seq}`, message_seq: seq };
        case "file_url": return { path: tmpFile() };
        case "resolve_uids": return Object.fromEntries((p.uins || []).map((u) => [String(u), `u_${u}`]));
        default: throw new Error(`unknown method ${m}`);
      }
    },
    async close() {},
  };
  return a;
}

async function makeMock() {
  process.env.QQ_MOCK_AUTOLOGIN_MS = "30";
  const d = await createMockDriver({ logger: quiet });
  return { d, async login() { await d.startLogin(); await new Promise((r) => setTimeout(r, 80)); } };
}
async function makeQqnt() {
  const agent = fakeAgent();
  const d = await createQqntDriver({ logger: quiet, agent, skipLaunch: true, mediaBaseUrl: "http://127.0.0.1:8792" });
  return { d, agent, async login() { await d.quickLogin(10001); await new Promise((r) => setTimeout(r, 30)); } };
}

for (const [name, make] of [["mock", makeMock], ["qqnt", makeQqnt]]) {
  test(`[${name}] info()/loginState()/selfInfo() 未登录形状`, async () => {
    const { d } = await make();
    const info = await d.info();
    assert.equal(typeof info.qq_installed, "boolean");
    assert.ok(isStr(info.qq_version)); assert.equal(info.driver, name); assert.ok(isStr(info.driver_reason));
    assert.equal(d.loginState(), "logged_out"); assert.equal(d.selfInfo(), null);
    assert.ok(Array.isArray(d.quickLoginList()));
    await d.stop();
  });

  test(`[${name}] 未登录时受保护方法回 -403 且不抛`, async () => {
    const { d } = await make();
    for (const m of GUARDED) {
      const r = await d[m](1, 2, 3);
      assert.equal(r.ok, false, m); assert.equal(r.retcode, -403, m); assert.ok(isStr(r.error), m);
    }
    await d.stop();
  });

  test(`[${name}] startLogin 出码形状 → 登录 → 契约表全部方法 shape`, async () => {
    const { d, login } = await make();
    const qr = await d.startLogin();
    assert.equal(qr.ok, true); assert.ok(isStr(qr.qr_png_base64) && qr.qr_png_base64.length > 4);
    assert.ok(isStr(qr.qr_url)); assert.ok(isInt(qr.expire_sec) && qr.expire_sec > 0);
    assert.equal(d.loginState(), "qr_wait");
    await login();
    assert.equal(d.loginState(), "logged_in");
    const self = d.selfInfo(); assert.ok(self && isInt(self.uin) && isStr(self.nickname));
    const tf = tmpFile();
    for (const [m, args, check] of CONTRACT) {
      const a = args.map((x) => (typeof x === "string" && x.includes("__TMPFILE__") ? fileUri(tf) : x));
      const r = await d[m](...a);
      try { check(r); } catch (e) { throw new Error(`[${name}] ${m}: ${e.message} got ${JSON.stringify(r)}`); }
    }
    const out = await d.logout(); assert.equal(out.ok, true); assert.equal(d.loginState(), "logged_out");
    await d.stop();
  });

  test(`[${name}] 事件流：onEvent 收到 Milky Event 形状`, async () => {
    const { d, agent, login } = await make();
    const got = [];
    d.onEvent((ev) => got.push(ev));
    await d.startLogin(); await login();
    if (agent) agent.push({ t: "msg", rec: { msgId: "1", msgSeq: "9", msgTime: 1700000000, chatType: 1, peerUid: "u_42", peerUin: "42", senderUid: "u_42", senderUin: "42", sendNickName: "阿强",
      elements: [{ elementType: 1, textElement: { content: "你好", atType: 0 } }] } });
    await new Promise((r) => setTimeout(r, 20));
    const msg = got.find((e) => e.event_type === "message_receive");
    assert.ok(msg, "message_receive expected");
    assert.ok(isInt(msg.time) && isInt(msg.self_id));
    assert.equal(msg.data.message_scene, "friend"); assert.ok(isInt(msg.data.peer_id) && isInt(msg.data.sender_id) && isInt(msg.data.message_seq));
    assert.ok(Array.isArray(msg.data.segments) && msg.data.segments[0].type === "text" && isStr(msg.data.segments[0].data.text));
    assert.ok(msg.data.friend && isInt(msg.data.friend.user_id));
    await d.stop();
  });
}

// ── qqnt 独有：未登录不 spawn、版本表、回退 ─────────────────────────────────────
test("[qqnt] 不支持的段返回 -400，不打 agent", async () => {
  const { d, agent, login } = await makeQqnt();
  await login();
  const before = agent.calls.length;
  const r = await d.sendPrivate(42, [{ type: "record", data: { uri: "file:///x.silk" } }]);
  assert.equal(r.ok, false); assert.equal(r.retcode, -400); assert.match(r.error, /unsupported segment: record/);
  assert.equal(agent.calls.length, before);
  await d.stop();
});

test("[qqnt] 日志不落正文：send 日志只有 scene/segs/files", async () => {
  const lines = [];
  const spyLog = { info(o, m) { lines.push(JSON.stringify([o, m])); }, warn() {}, debug() {}, error() {} };
  const agent = fakeAgent();
  const d = await createQqntDriver({ logger: spyLog, agent, skipLaunch: true });
  await d.quickLogin(10001); await new Promise((r) => setTimeout(r, 20));
  await d.sendPrivate(42, [{ type: "text", data: { text: "SECRET_BODY_TEXT" } }]);
  assert.ok(lines.some((l) => l.includes("[qqnt] send")));
  assert.ok(!lines.some((l) => l.includes("SECRET_BODY_TEXT")), "message body must never reach logs");
  await d.stop();
});

test("[qqnt] 版本表：表内可注入、表外抛 UnsupportedQqVersion（带版本串）、未装抛 not installed、用户自装拒改", () => {
  assert.ok(isInjectableBuild("44343") && isInjectableBuild("39038") && !isInjectableBuild("1"));
  for (const [b, s] of Object.entries(SUPPORTED_QQNT)) assert.ok(s.version.endsWith(`-${b}`) && s.appRel && s.entryMain && s.api, b);
  assert.throws(() => resolveQqntTarget({ installed: false }), /qq not installed/);
  assert.throws(() => resolveQqntTarget({ installed: true, build: "99999", version: "9.9.99-99999", source: "runtime", root: "x", exe: "x" }),
    (e) => e instanceof UnsupportedQqVersion && /unsupported qq version 9\.9\.99-99999/.test(e.message));
  assert.throws(() => resolveQqntTarget({ installed: true, build: "44343", version: "9.9.26-44343", source: "registry", root: "x", exe: "x" }), /user-owned/);
  const t = resolveQqntTarget({ installed: true, build: "44343", version: "9.9.26-44343", source: "runtime", root: "R", exe: "R/QQ.exe" });
  assert.equal(t.appDir, "versions/9.9.26-44343/resources/app");
  assert.equal(t.api, "nt-2025q3");
});

test("[qqnt] ensureInjected：改 main → launcher、复制 agent、备份原 package.json、幂等", () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "qqinj-"));
  const appDir = path.join(root, "versions", "9.9.26-44343", "resources", "app");
  fs.mkdirSync(path.join(appDir, "app_launcher"), { recursive: true });
  fs.writeFileSync(path.join(appDir, "package.json"), JSON.stringify({ name: "qq", main: "./app_launcher/index.js" }));
  fs.writeFileSync(path.join(appDir, "wrapper.node"), "node");
  const target = resolveQqntTarget({ installed: true, build: "44343", version: "9.9.26-44343", source: "runtime", root, exe: path.join(root, "QQ.exe") });
  const r1 = ensureInjected(target);
  const pkg = JSON.parse(fs.readFileSync(path.join(appDir, "package.json"), "utf8"));
  assert.equal(pkg.main, "./zhiliao_launcher.js");
  assert.ok(fs.existsSync(path.join(appDir, "package.json.zhiliao.bak")));
  assert.ok(fs.existsSync(r1.agent) && fs.readFileSync(r1.agent, "utf8").includes("ZHILIAO_QQ_PIPE"));
  const launcher = fs.readFileSync(r1.launcher, "utf8");
  assert.ok(launcher.includes('require("./zhiliao_qqnt_agent.cjs")') && launcher.includes('require("./app_launcher/index.js")'));
  const r2 = ensureInjected(target);   // 第二次：不改、不报错
  assert.equal(r2.launcher, r1.launcher);
  assert.equal(JSON.parse(fs.readFileSync(path.join(appDir, "package.json.zhiliao.bak"), "utf8")).main, "./app_launcher/index.js");
  fs.rmSync(root, { recursive: true, force: true });
});

test("[driver.js] QQ_DRIVER=qqnt 但本机无受支持 QQ → 自动回退 mock 并如实标 driver_reason", async () => {
  const rt = fs.mkdtempSync(path.join(os.tmpdir(), "qqnone-"));   // 空运行时目录 = 未安装
  const old = { drv: process.env.QQ_DRIVER, rt: process.env.QQ_RUNTIME_DIR };
  process.env.QQ_DRIVER = "qqnt"; process.env.QQ_RUNTIME_DIR = rt;
  try {
    const d = await createDriver({ logger: quiet });
    const info = await d.info();
    assert.equal(info.driver, "mock");
    assert.match(info.driver_reason, /qqnt-driver unavailable: (qq not installed|qq install is user-owned|unsupported qq version)/);
    await d.stop();
  } finally {
    if (old.drv === undefined) delete process.env.QQ_DRIVER; else process.env.QQ_DRIVER = old.drv;
    if (old.rt === undefined) delete process.env.QQ_RUNTIME_DIR; else process.env.QQ_RUNTIME_DIR = old.rt;
    fs.rmSync(rt, { recursive: true, force: true });
  }
});

test("[driver.js] 表外版本 → 回退 mock，driver_reason 含 unsupported qq version x.y.z", { skip: process.platform !== "win32" }, async () => {
  const rt = fs.mkdtempSync(path.join(os.tmpdir(), "qqold-"));
  const ver = "9.9.99-99999";
  fs.mkdirSync(path.join(rt, "versions", ver, "resources", "app"), { recursive: true });
  fs.writeFileSync(path.join(rt, "QQ.exe"), "MZ");
  fs.writeFileSync(path.join(rt, "versions", "config.json"), JSON.stringify({ curVersion: ver }));
  const old = { drv: process.env.QQ_DRIVER, rt: process.env.QQ_RUNTIME_DIR };
  process.env.QQ_DRIVER = "qqnt"; process.env.QQ_RUNTIME_DIR = rt;
  try {
    const d = await createDriver({ logger: quiet });
    const info = await d.info();
    assert.equal(info.driver, "mock");
    assert.match(info.driver_reason, /unsupported qq version 9\.9\.99-99999/);
    await d.stop();
  } finally {
    if (old.drv === undefined) delete process.env.QQ_DRIVER; else process.env.QQ_DRIVER = old.drv;
    if (old.rt === undefined) delete process.env.QQ_RUNTIME_DIR; else process.env.QQ_RUNTIME_DIR = old.rt;
    fs.rmSync(rt, { recursive: true, force: true });
  }
});

// ── 段转换纯函数 ───────────────────────────────────────────────────────────────
test("[elements] Milky → NT：text/mention/mention_all/face/reply/image/file；record 拒；空拒", () => {
  const r = milkyToElements([
    { type: "text", data: { text: "a" } }, { type: "mention", data: { user_id: 42 } }, { type: "mention_all", data: {} },
    { type: "face", data: { face_id: 14 } }, { type: "reply", data: { message_seq: 9 } },
    { type: "image", data: { uri: "file:///x.png", summary: "[图]" } }, { type: "file", data: { uri: "file:///a.bin", name: "a.bin" } },
  ]);
  assert.ok(!r.error);
  assert.deepEqual(r.elements.map((e) => e.elementType), [1, 1, 1, 6, 7, 2, 3]);
  assert.equal(r.elements[1].textElement.atType, 2); assert.equal(r.elements[2].textElement.atType, 1);
  assert.equal(r.elements[4].replyElement.replayMsgSeq, "9");
  assert.deepEqual(r.needs.uids, [{ index: 1, uin: "42" }]);
  assert.equal(r.needs.files.length, 2); assert.equal(r.needs.files[0].kind, "image"); assert.equal(r.needs.files[1].name, "a.bin");
  assert.match(milkyToElements([{ type: "record", data: {} }]).error, /unsupported segment: record/);
  assert.match(milkyToElements([]).error, /empty/);
});

test("[elements] NT → Milky：群消息带 group/group_member；私聊带 friend；未知元素不丢", () => {
  const g = recordToMilky({ msgId: "1", msgSeq: "5", msgTime: 1700000000, chatType: 2, peerUid: "10086", peerUin: "10086", peerName: "群",
    senderUid: "u_42", senderUin: "42", sendNickName: "阿强", sendMemberName: "群名片",
    elements: [{ elementType: 1, textElement: { content: "@x", atType: 2, atUid: "7" } }, { elementType: 2, picElement: { md5HexStr: "AB", picWidth: 1, picHeight: 2, originImageUrl: "/download?x" } }, { elementType: 99 }] }, 10001);
  assert.equal(g.event_type, "message_receive"); assert.equal(g.self_id, 10001);
  assert.equal(g.data.message_scene, "group"); assert.equal(g.data.group.group_id, 10086); assert.equal(g.data.group_member.card, "群名片");
  assert.deepEqual(g.data.segments.map((s) => s.type), ["mention", "image", "text"]);
  assert.equal(g.data.segments[0].data.user_id, 7); assert.equal(g.data.segments[1].data.resource_id, "AB");
  const f = recordToMilky({ msgId: "2", msgSeq: "6", msgTime: 1, chatType: 1, peerUid: "u_42", peerUin: "42", senderUid: "u_42", senderUin: "42", sendNickName: "阿强", sendRemarkName: "备注", elements: [] }, 1);
  assert.equal(f.data.message_scene, "friend"); assert.equal(f.data.friend.user_id, 42); assert.equal(f.data.friend.remark, "备注");
  assert.equal(recordToMilky(null), null);
});

test("[elements] imageMeta：PNG/GIF/JPEG 头解析", () => {
  const png = Buffer.concat([Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0, 0, 0, 13, 0x49, 0x48, 0x44, 0x52]), Buffer.from([0, 0, 0, 100, 0, 0, 0, 50, 8, 6, 0, 0, 0])]);
  assert.deepEqual(imageMeta(png), { width: 100, height: 50, ext: "png" });
  const gif = Buffer.from([0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 10, 0, 20, 0, 0, 0]);
  assert.deepEqual(imageMeta(gif), { width: 10, height: 20, ext: "gif" });
  const jpg = Buffer.from([0xff, 0xd8, 0xff, 0xc0, 0, 17, 8, 0, 30, 0, 40, 3, 1, 0x22, 0, 2, 0x11, 1, 3, 0x11, 1]);
  assert.deepEqual(imageMeta(jpg), { width: 40, height: 30, ext: "jpg" });
  assert.deepEqual(imageMeta(Buffer.from("nope")), { width: 0, height: 0, ext: "" });
});
