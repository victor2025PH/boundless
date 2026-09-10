/* eslint-disable */
/**
 * QQNT 主进程内 agent（B 段真驱动的「进程内一半」）。CommonJS、零依赖——它会被复制进运行时
 * QQ 的 resources/app/ 目录并由 zhiliao_launcher.js 在原 main 之前 require。
 *
 * 移植自 LLOneBot v4.9.4 的 wrapper.node 挂钩形态（GPL-2.0，见 NOTICE.md）：
 *   · 拦 .node 模块加载拿到 wrapper 导出；
 *   · 代理 NodeIQQNTWrapperSession.create 拿 session，代理 NodeIKernelLoginService 构造拿登录服务；
 *   · 代理各 service 的 addKernelXxxListener，把 QQ 自己注册的监听器包一层，我们旁路读事件——
 *     不替换 QQ 的监听器、不改 QQ 行为，QQ 自身 UI 照常工作；
 *   · 收发直接调 session.getMsgService() 等内核方法，**签名由 QQ 内核完成**，不连任何外部服务。
 * 智聊改动：去掉 OneBot/Satori 层与全部 HTTP 面，只留一条本机命名管道 RPC（qqnt-ipc.js 协议）；
 *   token 经环境变量进来、只在内存里比对；本文件不写任何日志文件，控制台只在 ZHILIAO_QQ_DEBUG=1 时出。
 *
 * API_PROFILES：按 qqnt-versions.js 的 api 键选方法签名；新 build 若签名变了在这里加档，不改旧档。
 */
"use strict";
const net = require("net");
const fs = require("fs");
const path = require("path");
const crypto = require("crypto");

const PIPE = process.env.ZHILIAO_QQ_PIPE || "";
const TOKEN = process.env.ZHILIAO_QQ_TOKEN || "";
const API = process.env.ZHILIAO_QQ_API || "nt-2025q3";
const DEBUG = process.env.ZHILIAO_QQ_DEBUG === "1";
const dbg = (...a) => { if (DEBUG) { try { console.log("[zhiliao-qq-agent]", ...a); } catch (_) {} } };

if (!PIPE || !TOKEN) { dbg("no pipe/token in env; agent idle"); module.exports = {}; return; }

const API_PROFILES = {
  "nt-2025q3": {
    // 方法签名档：C 段真机若发现某方法签名不同，在这里按 build 另开一档，不要改这档
    sendMsg: (svc, peer, elements) => svc.sendMsg("0", peer, elements, new Map()),
    recallMsg: (svc, peer, msgIds) => svc.recallMsg(peer, msgIds),
    setMsgRead: (svc, peer) => svc.setMsgRead(peer),
    getMsgsBySeq: (svc, peer, seq) => svc.getMsgsBySeqAndCount(peer, String(seq), 1, true, true),
    getRichMediaPath: (rich, arg) => rich.getRichMediaFilePathForGuild(arg),
    getQRCodePicture: (login) => login.getQRCodePicture(),
    getLoginList: (login) => login.getLoginList(),
    quickLoginWithUin: (login, uin) => login.quickLoginWithUin(String(uin)),
    kickMember: (grp, groupCode, uids, reject) => grp.kickMember(String(groupCode), uids, !!reject, ""),
    modifyGroupName: (grp, groupCode, name) => grp.modifyGroupName(String(groupCode), String(name), false),
    approvalFriendRequest: (buddy, req) => buddy.approvalFriendRequest(req),
  },
};
const P = API_PROFILES[API] || API_PROFILES["nt-2025q3"];

// ── 状态 ────────────────────────────────────────────────────────────────────
const S = {
  wrapper: null, session: null, loginService: null,
  self: { uin: "", uid: "", nick: "" },
  loginState: "logged_out",           // logged_out | qr_wait | scanned | logged_in
  uinToUid: new Map(), uidToUin: new Map(),
  seqToMsgId: new Map(),              // `${chatType}:${peerUid}:${seq}` -> msgId（近 5000 条）
  friendReqs: new Map(),              // friendUid -> reqTime（approvalFriendRequest 需要）
  sendWaiters: [],                    // { peerKey, resolve, timer }
};
function remember(k, v) { S.seqToMsgId.set(k, v); if (S.seqToMsgId.size > 5000) S.seqToMsgId.delete(S.seqToMsgId.keys().next().value); }

// ── IPC client（连回边车）────────────────────────────────────────────────────
let sock = null, connected = false, backoff = 500;
function send(obj) { if (sock && connected) { try { sock.write(JSON.stringify(obj) + "\n"); } catch (_) {} } }
function emit(ev) { send({ t: "ev", ev }); }
function connect() {
  sock = net.createConnection(PIPE);
  let buf = "";
  sock.setEncoding("utf8");
  sock.on("connect", () => {
    connected = true; backoff = 500;
    sock.write(JSON.stringify({ t: "hello", token: TOKEN, pid: process.pid, api: API,
      qq_version: qqVersion() }) + "\n");
    emit({ t: "login_state", state: S.loginState, self: S.self.uin ? { uin: Number(S.self.uin), nickname: S.self.nick } : null });
  });
  sock.on("data", (c) => {
    buf += c; let nl;
    while ((nl = buf.indexOf("\n")) >= 0) {
      const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
      if (!line.trim()) continue;
      let f; try { f = JSON.parse(line); } catch (_) { continue; }
      if (f.t === "call") handleCall(f);
    }
  });
  const retry = () => { connected = false; sock = null; setTimeout(connect, backoff); backoff = Math.min(backoff * 2, 10000); };
  sock.on("error", retry); sock.on("close", () => { if (connected) retry(); });
}
function qqVersion() {
  try {
    const cfg = JSON.parse(fs.readFileSync(path.join(path.dirname(process.execPath), "versions", "config.json"), "utf8"));
    return String(cfg.curVersion || "");
  } catch (_) { return ""; }
}
async function handleCall(f) {
  const fn = METHODS[f.m];
  if (!fn) return send({ id: f.id, t: "ret", ok: false, e: `unknown method ${f.m}` });
  try { send({ id: f.id, t: "ret", ok: true, r: await fn(f.p || {}) }); }
  catch (e) { send({ id: f.id, t: "ret", ok: false, e: String((e && e.message) || e).slice(0, 200) }); }
}

// ── wrapper.node 挂钩 ───────────────────────────────────────────────────────
function wrapListener(listener, tap) {
  // 把 QQ 自己的监听器包一层：每个方法先旁路给我们，再原样调 QQ 的
  return new Proxy(listener, {
    get(target, prop) {
      const orig = target[prop];
      if (typeof orig !== "function") return orig;
      return function (...args) {
        try { tap(String(prop), args); } catch (e) { dbg("tap error", prop, String(e)); }
        return orig.apply(target, args);
      };
    },
  });
}
function hookLoginService(svc) {
  if (S.loginService) return svc;
  S.loginService = svc;
  const origAdd = svc.addKernelLoginListener;
  svc.addKernelLoginListener = function (listener) {
    return origAdd.call(svc, wrapListener(listener, onLoginEvent));
  };
  return svc;
}
function onLoginEvent(name, args) {
  const a = args[0] || {};
  if (name === "onQRCodeGetPicture") {
    S.loginState = "qr_wait";
    emit({ t: "qr", png_base64: String(a.pngBase64QrcodeData || "").replace(/^data:image\/png;base64,/, ""), url: String(a.qrcodeUrl || "") });
  } else if (name === "onQRCodeSessionUserScaned") {
    S.loginState = "scanned"; emit({ t: "login_state", state: S.loginState });
  } else if (name === "onQRCodeLoginSucceed") {
    S.self.uin = String(a.uin || ""); S.self.uid = String(a.uid || "");
    S.loginState = "logged_in"; emit({ t: "login_state", state: S.loginState, self: { uin: Number(S.self.uin), nickname: S.self.nick } });
  } else if (name === "onQRCodeSessionFailed" || name === "onLoginFailed" || name === "onQRCodeSessionQuickLoginFailed") {
    S.loginState = "logged_out"; emit({ t: "login_state", state: S.loginState, reason: String(a.errMsg || a.errorMsg || name) });
  } else if (name === "onLoginState" || name === "onQQLoginNumLimited") {
    // 状态位变化只作日志，不改我们的三态（以扫码成功 / 会话建立为准）
  }
}
function hookSession(session) {
  if (S.session) return session;
  S.session = session;
  const tapSvc = (getter, addName, handler) => {
    const orig = session[getter];
    if (typeof orig !== "function") return;
    session[getter] = function (...args) {
      const svc = orig.apply(session, args);
      if (svc && !svc.__zhiliao_hooked && typeof svc[addName] === "function") {
        const add = svc[addName];
        svc[addName] = function (listener) { return add.call(svc, wrapListener(listener, handler)); };
        try { Object.defineProperty(svc, "__zhiliao_hooked", { value: true }); } catch (_) {}
      }
      return svc;
    };
  };
  tapSvc("getMsgService", "addKernelMsgListener", onMsgEvent);
  tapSvc("getBuddyService", "addKernelBuddyListener", onBuddyEvent);
  tapSvc("getGroupService", "addKernelGroupListener", () => {});
  if (S.loginState !== "logged_in") { S.loginState = "logged_in"; }
  emit({ t: "session_ready" });
}
function onMsgEvent(name, args) {
  if (name === "onRecvMsg" || name === "onRecvActiveMsg") {
    for (const rec of (args[0] || [])) {
      remember(`${rec.chatType}:${rec.peerUid}:${rec.msgSeq}`, String(rec.msgId));
      if (rec.senderUid && rec.senderUin) { S.uidToUin.set(String(rec.senderUid), String(rec.senderUin)); S.uinToUid.set(String(rec.senderUin), String(rec.senderUid)); }
      if (rec.peerUid && rec.peerUin) { S.uidToUin.set(String(rec.peerUid), String(rec.peerUin)); S.uinToUid.set(String(rec.peerUin), String(rec.peerUid)); }
      if (Number(rec.msgType) === 5 && Number(rec.subMsgType) === 8) continue;   // 灰条系统提示
      emit({ t: "msg", rec: slimRecord(rec) });
    }
  } else if (name === "onAddSendMsg" || name === "onMsgInfoListUpdate") {
    const list = name === "onAddSendMsg" ? [args[0]] : (args[0] || []);
    for (const rec of list) {
      if (!rec || Number(rec.sendStatus) === 0) continue;
      const key = `${rec.chatType}:${rec.peerUid}`;
      remember(`${key}:${rec.msgSeq}`, String(rec.msgId));
      for (const w of S.sendWaiters.splice(0)) {
        if (w.peerKey === key) { clearTimeout(w.timer); w.resolve(rec); } else S.sendWaiters.push(w);
      }
    }
  }
}
function onBuddyEvent(name, args) {
  if (name === "onBuddyReqChange") {
    const a = args[0] || {};
    for (const r of (a.buddyReqs || [])) {
      if (r.friendUid && !r.isDecide) { S.friendReqs.set(String(r.friendUid), String(r.reqTime || "")); emit({ t: "friend_request", uid: String(r.friendUid), uin: String(r.friendUin || "") }); }
    }
  }
}
/** 只把 Milky 转换需要的字段送出去（不整条 raw 外泄） */
function slimRecord(rec) {
  return {
    msgId: String(rec.msgId || ""), msgSeq: String(rec.msgSeq || ""), msgTime: Number(rec.msgTime || 0),
    chatType: Number(rec.chatType || 0), peerUid: String(rec.peerUid || ""), peerUin: String(rec.peerUin || ""),
    peerName: String(rec.peerName || ""), senderUid: String(rec.senderUid || ""), senderUin: String(rec.senderUin || ""),
    sendNickName: String(rec.sendNickName || ""), sendMemberName: String(rec.sendMemberName || ""), sendRemarkName: String(rec.sendRemarkName || ""),
    msgType: Number(rec.msgType || 0), subMsgType: Number(rec.subMsgType || 0),
    elements: (rec.elements || []).map((el) => ({
      elementType: Number(el.elementType || 0), textElement: el.textElement || undefined, picElement: el.picElement || undefined,
      faceElement: el.faceElement || undefined, replyElement: el.replyElement || undefined, fileElement: el.fileElement || undefined,
      pttElement: el.pttElement || undefined, videoElement: el.videoElement || undefined,
    })),
  };
}
function hookWrapper(exp) {
  if (S.wrapper || !exp || typeof exp !== "object") return exp;
  S.wrapper = exp;
  const SessionCls = exp.NodeIQQNTWrapperSession;
  if (SessionCls && typeof SessionCls.create === "function") {
    const create = SessionCls.create;
    SessionCls.create = function (...a) { const s = create.apply(this, a); try { hookSession(s); } catch (e) { dbg("hookSession", String(e)); } return s; };
  }
  const LoginCls = exp.NodeIKernelLoginService;
  if (typeof LoginCls === "function") {
    exp.NodeIKernelLoginService = new Proxy(LoginCls, {
      construct(target, a) { const inst = new target(...a); try { hookLoginService(inst); } catch (e) { dbg("hookLogin", String(e)); } return inst; },
    });
  }
  dbg("wrapper hooked");
  return exp;
}
(function installModuleHook() {
  const Module = require("module");
  const origNode = Module._extensions[".node"];
  Module._extensions[".node"] = function (mod, filename) {
    const r = origNode.call(this, mod, filename);
    if (/wrapper\.node$/i.test(filename)) { try { mod.exports = hookWrapper(mod.exports); } catch (e) { dbg("hookWrapper", String(e)); } }
    return r;
  };
  const origDlopen = process.dlopen;
  process.dlopen = function (mod, filename, ...rest) {
    const r = origDlopen.call(process, mod, filename, ...rest);
    if (/wrapper\.node$/i.test(String(filename))) { try { mod.exports = hookWrapper(mod.exports); } catch (e) { dbg("hookWrapper(dlopen)", String(e)); } }
    return r;
  };
})();

// ── 辅助：uin/uid 解析、富媒体上传 ──────────────────────────────────────────
async function uidOf(uin) {
  uin = String(uin);
  if (S.uinToUid.has(uin)) return S.uinToUid.get(uin);
  const prof = S.session && S.session.getProfileService && S.session.getProfileService();
  if (!prof) throw new Error("session not ready");
  let uid = "";
  if (typeof prof.getUidByUinV2 === "function") { const m = await prof.getUidByUinV2([uin]); uid = String((m && (m.get ? m.get(uin) : m[uin])) || ""); }
  if (!uid && typeof prof.getUidByUin === "function") { const m = await prof.getUidByUin("FriendsServiceImpl", [uin]); uid = String((m && (m.get ? m.get(uin) : m[uin])) || ""); }
  if (!uid) throw new Error(`uid not found for ${uin}`);
  S.uinToUid.set(uin, uid); S.uidToUin.set(uid, uin);
  return uid;
}
async function peerOf(scene, id) {
  const chatType = scene === "group" ? 2 : scene === "temp" ? 100 : 1;
  const peerUid = chatType === 2 ? String(id) : await uidOf(id);
  return { chatType, peerUid, guildId: "" };
}
function md5File(p) { return crypto.createHash("md5").update(fs.readFileSync(p)).digest("hex"); }
async function stageFile(uri, elementType, fileNameHint) {
  // uri: file:///path 或 本机绝对路径（边车已把 http(s)/base64 落盘再交给我们）
  let src = uri.startsWith("file://") ? decodeURIComponent(uri.slice(process.platform === "win32" ? 8 : 7)) : uri;
  if (!fs.existsSync(src)) throw new Error("file not found");
  const md5 = md5File(src); const size = fs.statSync(src).size;
  const ext = path.extname(src).replace(".", "") || (elementType === 2 ? "jpg" : "bin");
  const fileName = fileNameHint || `${md5}.${ext}`;
  const rich = S.session.getRichMediaService();
  const dest = await P.getRichMediaPath(rich, { md5HexStr: md5, fileName, elementType, elementSubType: 0, thumbSize: 0, needCreate: true, downloadType: 1, file_uuid: "" });
  const destPath = typeof dest === "string" ? dest : String((dest && dest.path) || "");
  if (!destPath) throw new Error("rich media path unavailable");
  fs.mkdirSync(path.dirname(destPath), { recursive: true });
  if (!fs.existsSync(destPath)) fs.copyFileSync(src, destPath);
  return { md5, size, ext, fileName, path: destPath };
}
function waitSent(peer, timeoutMs) {
  return new Promise((resolve) => {
    const peerKey = `${peer.chatType}:${peer.peerUid}`;
    const timer = setTimeout(() => { S.sendWaiters = S.sendWaiters.filter((w) => w.timer !== timer); resolve(null); }, timeoutMs);
    S.sendWaiters.push({ peerKey, resolve, timer });
  });
}
function need(x, what) { if (!x) throw new Error(`${what} not ready`); return x; }

// ── RPC 方法面（与 qqnt-driver.js 一一对应）───────────────────────────────────
const METHODS = {
  async ping() { return { pid: process.pid, login_state: S.loginState, qq_version: qqVersion(), api: API }; },
  async login_state() { return { state: S.loginState, self: S.self.uin ? { uin: Number(S.self.uin), nickname: S.self.nick } : null }; },
  async qr_start() { need(S.loginService, "login service"); await P.getQRCodePicture(S.loginService); S.loginState = "qr_wait"; return { ok: true }; },
  async quick_login_list() {
    need(S.loginService, "login service");
    const r = await P.getLoginList(S.loginService);
    const list = (r && (r.LocalLoginInfoList || r.localLoginInfoList)) || [];
    return list.filter((x) => x && x.isQuickLogin).map((x) => ({ uin: Number(x.uin), nickname: String(x.nickName || "") }));
  },
  async quick_login({ uin }) { need(S.loginService, "login service"); const r = await P.quickLoginWithUin(S.loginService, uin); return { ok: !!(r && (r.result === 0 || r.result === "0")), err: String((r && r.loginErrorInfo && r.loginErrorInfo.errMsg) || "") }; },
  async self_info() {
    if (!S.self.uin) return null;
    if (!S.self.nick && S.session && S.session.getProfileService) {
      try { const p = S.session.getProfileService(); const info = typeof p.getUserDetailInfoByUin === "function" ? await p.getUserDetailInfoByUin(S.self.uin) : null; S.self.nick = String((info && info.detail && info.detail.simpleInfo && info.detail.simpleInfo.coreInfo && info.detail.simpleInfo.coreInfo.nick) || (info && info.info && info.info.nick) || ""); } catch (_) {}
    }
    return { uin: Number(S.self.uin), nickname: S.self.nick };
  },
  async resolve_uids({ uins }) { const out = {}; for (const u of uins || []) { try { out[String(u)] = await uidOf(u); } catch (_) { out[String(u)] = ""; } } return out; },
  async send({ scene, peer_id, elements, files }) {
    const session = need(S.session, "session");
    const peer = await peerOf(scene, peer_id);
    // @ 目标补 uid；图片/文件先落 NT 缓存再补元素
    for (const el of elements) if (el.textElement && Number(el.textElement.atType) === 2 && el.textElement.atUid) el.textElement.atNtUid = await uidOf(el.textElement.atUid);
    for (const f of files || []) {
      const info = await stageFile(f.uri, f.kind === "image" ? 2 : 3, f.name);
      if (f.kind === "image") {
        const meta = f.meta || {};
        elements[f.index].picElement = { md5HexStr: info.md5, fileSize: String(info.size), picWidth: Number(meta.width || 0), picHeight: Number(meta.height || 0), fileName: info.fileName, sourcePath: info.path, original: true, picType: info.ext === "gif" ? 2000 : 1000, picSubType: f.sub_type === "sticker" ? 1 : 0, fileUuid: "", fileSubId: "", thumbFileSize: 0, summary: String(f.summary || "") };
      } else {
        elements[f.index].fileElement = { fileMd5: info.md5, fileName: f.name || info.fileName, filePath: info.path, fileSize: String(info.size), picHeight: 0, picWidth: 0, picThumbPath: new Map(), file10MMd5: "", fileSha: "", fileSha3: "", fileUuid: "", fileSubId: "", thumbFileSize: 750, fileBizId: 0 };
      }
    }
    const waiter = waitSent(peer, 12000);
    const r = await P.sendMsg(session.getMsgService(), peer, elements);
    if (r && Number(r.result) !== 0) throw new Error(`sendMsg result=${r.result} ${String(r.errMsg || "")}`);
    const sent = await waiter;
    return { message_seq: Number((sent && sent.msgSeq) || 0), msg_id: String((sent && sent.msgId) || "") };
  },
  async recall({ scene, peer_id, message_seq }) {
    const session = need(S.session, "session"); const peer = await peerOf(scene, peer_id);
    let msgId = S.seqToMsgId.get(`${peer.chatType}:${peer.peerUid}:${message_seq}`);
    if (!msgId) { const r = await P.getMsgsBySeq(session.getMsgService(), peer, message_seq); msgId = String(((r && r.msgList) || [])[0]?.msgId || ""); }
    if (!msgId) throw new Error("message not found");
    const r = await P.recallMsg(session.getMsgService(), peer, [msgId]);
    if (r && Number(r.result) !== 0) throw new Error(`recall result=${r.result}`);
    return { ok: true };
  },
  async mark_read({ scene, peer_id }) { const session = need(S.session, "session"); await P.setMsgRead(session.getMsgService(), await peerOf(scene, peer_id)); return { ok: true }; },
  async upload_file({ scene, peer_id, uri, name }) {
    // Milky upload_*_file = 发一条文件消息；file_id 用 md5（下载时靠它找）
    const out = await METHODS.send({ scene, peer_id, elements: [{ elementType: 3, elementId: "", fileElement: null }], files: [{ index: 0, kind: "file", uri, name }] });
    return { file_id: out.msg_id || "", message_seq: out.message_seq };
  },
  async file_url({ scene, peer_id, file_id }) {
    // NT 只给本地缓存路径：找回消息 → downloadRichMedia → file:// 路径（边车侧转成可拉取的 URL）
    const session = need(S.session, "session"); const peer = await peerOf(scene, peer_id);
    const rich = session.getRichMediaService();
    const r = await session.getMsgService().getMsgsByMsgId(peer, [String(file_id)]);
    const rec = ((r && r.msgList) || [])[0]; if (!rec) throw new Error("message not found");
    const el = (rec.elements || []).find((e) => e.fileElement); if (!el) throw new Error("not a file message");
    const fe = el.fileElement;
    if (fe.filePath && fs.existsSync(fe.filePath)) return { path: fe.filePath };
    await rich.downloadRichMedia({ fileModelId: "0", downloadSourceType: 0, triggerType: 1, msgId: String(rec.msgId), chatType: peer.chatType, peerUid: peer.peerUid, elementId: String(el.elementId), thumbSize: 0, downloadType: 1, filePath: fe.filePath || "" });
    return { path: fe.filePath || "" };
  },
  async kick({ group_id, user_id, reject }) { const session = need(S.session, "session"); const r = await P.kickMember(session.getGroupService(), group_id, [await uidOf(user_id)], reject); if (r && Number(r.result) !== 0) throw new Error(`kick result=${r.result}`); return { ok: true }; },
  async set_group_name({ group_id, name }) { const session = need(S.session, "session"); const r = await P.modifyGroupName(session.getGroupService(), group_id, name); if (r && Number(r.result) !== 0) throw new Error(`modifyGroupName result=${r.result}`); return { ok: true }; },
  async accept_friend({ uid }) {
    const session = need(S.session, "session");
    const reqTime = S.friendReqs.get(String(uid)) || "";
    const r = await P.approvalFriendRequest(session.getBuddyService(), { friendUid: String(uid), reqTime, accept: true });
    if (r && Number(r.result) !== 0) throw new Error(`approvalFriendRequest result=${r.result}`);
    S.friendReqs.delete(String(uid));
    return { ok: true };
  },
};

connect();
module.exports = { __zhiliao_agent: true };
