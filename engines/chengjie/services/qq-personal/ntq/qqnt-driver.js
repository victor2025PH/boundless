/**
 * QQNT 真驱动（B 段，2026-09-10）——driver.js 契约的真实实现；外壳 server.js 对它与 mock 一视同仁。
 *
 * 结构（GPL-2.0 移植自 LLOneBot v4.9.4 直连注入形态，见 NOTICE.md；全部代码留在 services/qq-personal/）：
 *   qqnt-versions.js  版本表：不在表的 QQ build → 抛 UnsupportedQqVersion → driver.js 回退 mock 并标 driver_reason
 *   qqnt-agent.cjs    QQ 主进程内 agent（被复制进运行时 QQ 的 resources/app/，launcher 先 require 它）
 *   qqnt-ipc.js       本机命名管道 RPC（随机 token 经环境变量给 QQ 子进程，不落盘不入日志）
 *   qqnt-elements.js  Milky 段 ⇄ NT 元素（纯函数）
 *   本文件            进程管理 + 契约方法 → agent RPC；登录三态 / 事件流 / 媒体落盘与回放
 *
 * 启动链（createQqntDriver）：
 *   locateQQ → resolveQqntTarget（版本表 + 只碰运行时目录）→ ensureInjected（写 launcher + agent、改 package.json main，
 *   幂等、留 .zhiliao.bak）→ AgentServer.listen → spawn QQ.exe（env 带 pipe/token）→ waitHello（45s）→ ping
 *   任一步失败即 **throw**：driver.js 捕获后回退 mock，/health 如实 driver="mock" + driver_reason。
 *
 * 三条红线（QQ个人号接入协议与风险须知.md）在代码上的落点：
 *   ① 不接第三方签名 / 中转：本文件只 spawn 本机 QQ.exe、只连本机管道，无任何外网地址；
 *   ② token / 会话密钥不出本机：token 只在环境变量 + 内存，agent 比对后即弃；QQ 自己的会话在 QQ_RUNTIME_DIR 的 nt_qq 目录；
 *   ③ 日志不落正文：log 只记 方法名 / 场景 / peer 是否有值 / 段数 / seq，绝不记 segments 内容或 png。
 *
 * 测试注入：opts.agent（实现 listen/waitHello/call/onEvent/close）+ opts.skipLaunch=true → 不定位、不 spawn，
 * driver-contract.test.js 用假 agent 跑同一组 shape 断言。
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import crypto from "node:crypto";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { locateQQ } from "./locate.js";
import { resolveQqntTarget } from "./qqnt-versions.js";
import { AgentServer, newPipePath, newToken } from "./qqnt-ipc.js";
import { imageMeta, milkyToElements, recordToMilky } from "./qqnt-elements.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const LAUNCHER_NAME = "zhiliao_launcher.js";
const AGENT_NAME = "zhiliao_qqnt_agent.cjs";
const IMPL_VERSION = (() => { try { return JSON.parse(fs.readFileSync(path.join(HERE, "..", "package.json"), "utf8")).version || "0.1.0"; } catch { return "0.1.0"; } })();

/** 把运行时 QQ 的入口改成先加载我们的 agent（只对 source=runtime 的目录做；幂等；备份原 package.json）。 */
export function ensureInjected(target, opts = {}) {
  const appDir = path.join(target.root, target.appDir);
  const pkgPath = path.join(appDir, "package.json");
  if (!fs.existsSync(pkgPath)) throw new Error(`qq app package.json missing: ${target.appDir}`);
  const wrapper = path.join(appDir, target.wrapperRel);
  if (!fs.existsSync(wrapper)) throw new Error(`wrapper.node missing under ${target.appDir}`);
  const agentSrc = opts.agentSource || path.join(HERE, "qqnt-agent.cjs");
  const agentDst = path.join(appDir, AGENT_NAME);
  const srcBuf = fs.readFileSync(agentSrc);
  if (!fs.existsSync(agentDst) || !fs.readFileSync(agentDst).equals(srcBuf)) fs.writeFileSync(agentDst, srcBuf);
  const launcher = `// zhiliao qq-personal launcher (generated) - loads the in-process agent, then QQ's own entry\n`
    + `require("./${AGENT_NAME}");\nrequire("${target.entryMain}");\n`;
  const launcherDst = path.join(appDir, LAUNCHER_NAME);
  if (!fs.existsSync(launcherDst) || fs.readFileSync(launcherDst, "utf8") !== launcher) fs.writeFileSync(launcherDst, launcher);
  const pkg = JSON.parse(fs.readFileSync(pkgPath, "utf8"));
  const want = `./${LAUNCHER_NAME}`;
  if (pkg.main !== want) {
    if (pkg.main !== target.entryMain) throw new Error(`unexpected package.json main ${JSON.stringify(pkg.main)} (version table says ${target.entryMain})`);
    const bak = pkgPath + ".zhiliao.bak";
    if (!fs.existsSync(bak)) fs.copyFileSync(pkgPath, bak);
    pkg.main = want;
    fs.writeFileSync(pkgPath, JSON.stringify(pkg, null, 2));
  }
  return { appDir, launcher: launcherDst, agent: agentDst };
}

function mediaDir(runtimeDir) {
  const d = runtimeDir ? path.join(runtimeDir, "..", "zhiliao-qq-media") : path.join(os.tmpdir(), "zhiliao-qq-media");
  fs.mkdirSync(d, { recursive: true });
  return d;
}

/** Milky 资源 uri（file:// / http(s):// / base64://）→ 本机文件路径（http/base64 先落盘）。 */
async function stageUri(uri, dir) {
  uri = String(uri || "");
  if (uri.startsWith("file://")) return fileURLToPath(uri);
  if (uri.startsWith("base64://")) {
    const buf = Buffer.from(uri.slice("base64://".length), "base64");
    const p = path.join(dir, crypto.createHash("md5").update(buf).digest("hex") + "." + (imageMeta(buf).ext || "bin"));
    if (!fs.existsSync(p)) fs.writeFileSync(p, buf);
    return p;
  }
  if (/^https?:\/\//i.test(uri)) {
    const res = await fetch(uri);
    if (!res.ok) throw new Error(`fetch ${res.status}`);
    const buf = Buffer.from(await res.arrayBuffer());
    const p = path.join(dir, crypto.createHash("md5").update(buf).digest("hex") + "." + (imageMeta(buf).ext || "bin"));
    if (!fs.existsSync(p)) fs.writeFileSync(p, buf);
    return p;
  }
  if (path.isAbsolute(uri) && fs.existsSync(uri)) return uri;
  throw new Error("unsupported uri scheme");
}

export async function createQqntDriver(opts = {}) {
  const log = opts.logger || console;
  const runtimeDir = opts.runtimeDir || process.env.QQ_RUNTIME_DIR || "";
  const mediaBase = String(opts.mediaBaseUrl || process.env.QQ_MEDIA_BASE_URL || `http://127.0.0.1:${process.env.QQ_MILKY_PORT || 8792}`);
  let child = null;
  let target = null;
  let agent = opts.agent || null;
  let qqVersion = "";

  if (!opts.skipLaunch) {
    const loc = locateQQ({ runtimeDir });
    target = resolveQqntTarget(loc, { allowUserInstall: String(process.env.QQ_ALLOW_USER_INSTALL || "0") === "1" });
    qqVersion = target.version;
    ensureInjected(target);
    const pipePath = newPipePath();
    const token = newToken();
    agent = new AgentServer({ pipePath, token, logger: log, handshakeMs: Number(opts.handshakeMs || 45000) });
    await agent.listen();
    const env = { ...process.env, ZHILIAO_QQ_PIPE: pipePath, ZHILIAO_QQ_TOKEN: token, ZHILIAO_QQ_API: target.api };
    child = spawn(target.exe, [], { cwd: target.root, env, detached: false, stdio: "ignore", windowsHide: false });
    child.on("exit", (code) => { log.warn?.({ code }, "[qqnt] QQ process exited"); });
    try {
      await agent.waitHello();
    } catch (e) {
      try { child.kill(); } catch {}
      await agent.close();
      throw new Error(`${e.message} (qq ${target.version}, verified=${target.verified || "no"})`);
    }
  } else {
    if (!agent) throw new Error("skipLaunch requires opts.agent");
    await agent.listen?.();
    await agent.waitHello?.();
  }

  // ── 状态：从 agent 事件流维护 ──────────────────────────────────────────────
  let state = "logged_out";
  let self = null;
  let lastQr = null;              // { png_base64, url, at }
  let evCb = null;
  const media = new Map();        // media id → 本机路径（/media/:id 由 server.js 回放）
  const emit = (ev) => { if (evCb) { try { evCb(ev); } catch (e) { log.debug?.({ e: String(e) }, "[qqnt] emit failed"); } } };
  const mediaId = (p) => { const id = crypto.createHash("sha1").update(p).digest("hex").slice(0, 24); media.set(id, p); return `${mediaBase}/media/${id}`; };

  agent.onEvent((ev) => {
    if (!ev || typeof ev !== "object") return;
    if (ev.t === "login_state") {
      state = String(ev.state || state);
      if (ev.self && ev.self.uin) self = { uin: Number(ev.self.uin), nickname: String(ev.self.nickname || "") };
      if (state === "logged_out") self = null;
      log.info?.({ state }, "[qqnt] login state");
    } else if (ev.t === "session_ready") {
      if (state !== "logged_in") state = "logged_in";
      emit({ time: Math.floor(Date.now() / 1000), self_id: self?.uin || 0, event_type: "bot_online", data: {} });
    } else if (ev.t === "qr") {
      state = "qr_wait";
      lastQr = { png_base64: String(ev.png_base64 || ""), url: String(ev.url || ""), at: Date.now() };
    } else if (ev.t === "msg") {
      const m = recordToMilky(ev.rec, self?.uin || 0);
      if (!m) return;
      // 图片 temp_url：NT 原图链接需要 rkey，本版先给资源 id；C 段按真机回包补 rkey 拼接（手册 §5）
      log.info?.({ scene: m.data.message_scene, seq: m.data.message_seq, segs: m.data.segments.length }, "[qqnt] inbound");
      emit(m);
    } else if (ev.t === "friend_request") {
      emit({ time: Math.floor(Date.now() / 1000), self_id: self?.uin || 0, event_type: "friend_request",
             data: { initiator_id: Number(ev.uin || 0), initiator_uid: String(ev.uid || ""), comment: "", via: "" } });
    } else if (ev.t === "agent_gone") {
      state = "logged_out"; self = null;
      emit({ time: Math.floor(Date.now() / 1000), self_id: 0, event_type: "bot_offline", data: { reason: "qq process detached" } });
    }
  });

  const initial = await agent.call("login_state", {}, 8000).catch(() => null);
  if (initial) { state = String(initial.state || state); if (initial.self) self = initial.self; }
  if (!qqVersion) { const p = await agent.call("ping", {}, 8000).catch(() => null); qqVersion = String((p && p.qq_version) || ""); }

  const guard = () => (state === "logged_in" ? null : { ok: false, error: "not logged in", retcode: -403 });
  const wrap = async (fn) => { try { return await fn(); } catch (e) { return { ok: false, error: String((e && e.message) || e).slice(0, 200), retcode: -1 }; } };

  async function sendTo(scene, peerId, segs) {
    const g = guard(); if (g) return g;
    const conv = milkyToElements(segs);
    if (conv.error) return { ok: false, error: conv.error, retcode: -400 };
    return wrap(async () => {
      const files = [];
      for (const f of conv.needs.files) {
        const p = await stageUri(f.uri, mediaDir(runtimeDir));
        const meta = f.kind === "image" ? imageMeta(fs.readFileSync(p)) : {};
        files.push({ index: f.index, kind: f.kind, uri: p, name: f.name, summary: f.summary, sub_type: f.sub_type, meta });
      }
      log.info?.({ scene, segs: conv.elements.length, files: files.length }, "[qqnt] send");
      const r = await agent.call("send", { scene, peer_id: peerId, elements: conv.elements, files }, 30000);
      return { ok: true, message_seq: Number(r.message_seq || 0) };
    });
  }

  return {
    kind: "qqnt",
    __driverReason: "",
    async info() {
      return { qq_installed: true, qq_version: qqVersion, driver: "qqnt", driver_reason: this.__driverReason };
    },
    loginState() { return state; },
    selfInfo() { return state === "logged_in" ? self : null; },

    async startLogin() {
      if (state === "logged_in") return { ok: false, error: "already logged in", retcode: -409 };
      return wrap(async () => {
        lastQr = null;
        await agent.call("qr_start", {}, 15000);
        const deadline = Date.now() + 20000;
        while (!lastQr && Date.now() < deadline) await new Promise((r) => setTimeout(r, 200));
        if (!lastQr) return { ok: false, error: "qr not delivered", retcode: -504 };
        state = "qr_wait";
        return { ok: true, qr_png_base64: lastQr.png_base64, qr_url: lastQr.url, expire_sec: 120 };
      });
    },
    quickLoginList() { return this._quick || []; },
    async refreshQuickLoginList() { this._quick = await agent.call("quick_login_list", {}, 8000).catch(() => []); return this._quick; },
    async quickLogin(uin) {
      return wrap(async () => { const r = await agent.call("quick_login", { uin: String(uin) }, 20000); return r.ok ? { ok: true } : { ok: false, error: r.err || "quick login failed", retcode: -403 }; });
    },
    async logout() {
      // NT 内核无「登出但保留进程」的干净入口：断开 = 结束我们拉起的 QQ 进程（本地会话保留，下次可 quickLogin）
      state = "logged_out"; self = null;
      try { child?.kill(); } catch {}
      return { ok: true };
    },

    async getImplInfo() {
      return { impl_name: "ZhiliaoQQConnector(qqnt)", impl_version: IMPL_VERSION, qq_version: qqVersion, milky_version: "1.3" };
    },

    async sendPrivate(userId, segs) { return sendTo("friend", userId, segs); },
    async sendGroup(groupId, segs) { return sendTo("group", groupId, segs); },
    async recallPrivate(userId, seq) { return guard() || wrap(async () => { await agent.call("recall", { scene: "friend", peer_id: userId, message_seq: seq }); return { ok: true }; }); },
    async recallGroup(groupId, seq) { return guard() || wrap(async () => { await agent.call("recall", { scene: "group", peer_id: groupId, message_seq: seq }); return { ok: true }; }); },
    async markRead(scene, peerId) { return guard() || wrap(async () => { await agent.call("mark_read", { scene, peer_id: peerId }); return { ok: true }; }); },
    async uploadPrivateFile(userId, uri, name) {
      return guard() || wrap(async () => { const p = await stageUri(uri, mediaDir(runtimeDir)); const r = await agent.call("upload_file", { scene: "friend", peer_id: userId, uri: p, name }, 120000); return { ok: true, file_id: String(r.file_id || "") }; });
    },
    async uploadGroupFile(groupId, uri, name) {
      return guard() || wrap(async () => { const p = await stageUri(uri, mediaDir(runtimeDir)); const r = await agent.call("upload_file", { scene: "group", peer_id: groupId, uri: p, name }, 120000); return { ok: true, file_id: String(r.file_id || "") }; });
    },
    async privateFileUrl(userId, fileId) {
      return guard() || wrap(async () => { const r = await agent.call("file_url", { scene: "friend", peer_id: userId, file_id: fileId }, 60000); if (!r.path) throw new Error("file path unavailable"); return { ok: true, download_url: mediaId(r.path) }; });
    },
    async groupFileUrl(groupId, fileId) {
      return guard() || wrap(async () => { const r = await agent.call("file_url", { scene: "group", peer_id: groupId, file_id: fileId }, 60000); if (!r.path) throw new Error("file path unavailable"); return { ok: true, download_url: mediaId(r.path) }; });
    },
    async kickGroupMember(groupId, userId, reject) { return guard() || wrap(async () => { await agent.call("kick", { group_id: groupId, user_id: userId, reject: !!reject }); return { ok: true }; }); },
    async setGroupName(groupId, name) { return guard() || wrap(async () => { await agent.call("set_group_name", { group_id: groupId, name }); return { ok: true }; }); },
    async acceptFriend(initiatorUid) { return guard() || wrap(async () => { await agent.call("accept_friend", { uid: initiatorUid }); return { ok: true }; }); },

    /** server.js GET /media/:id 回放 NT 本地缓存文件（仅本驱动登记过的 id；不暴露任意路径） */
    mediaFile(id) { return media.get(String(id || "")) || ""; },

    onEvent(cb) { evCb = cb; },
    async stop() {
      try { await agent.close(); } catch {}
      try { child?.kill(); } catch {}
    },
  };
}
