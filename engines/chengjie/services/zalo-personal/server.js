/**
 * Zalo 个人号（zca-js）扫码登录 + 收发微服务。
 *
 * 竞品「支持个人号」的真相：用逆向的 Zalo Web 协议（zca-js）模拟浏览器会话，一号一持久化
 * context，扫码登录（loginQR），再用协议层收发消息。本服务即 Zalo 的这条路——与 Python
 * 主进程（src/integrations/zalo_personal_login.py）通过本地 HTTP 桥接，契约**逐一对齐**
 * whatsapp-baileys / messenger-web：
 *
 *   POST /login/start            -> { login_id, qr_image, status }   发起一次扫码登录
 *   GET  /login/:id/status       -> { status, account_id, qr_image, display_name, avatar_url }
 *   POST /login/:id/cancel       -> { ok }                           取消该登录
 *   POST /accounts/restore       -> { ok, restored }                 恢复磁盘已持久化的会话
 *   GET  /accounts               -> { accounts: [...] }              已登录账号（含 logged_in）
 *   POST /accounts/:id/send      -> { ok, message_id }               发文字
 *   POST /accounts/:id/send-media-> { ok, message_id }               发媒体（图片/语音/文件）
 *   POST /accounts/:id/logout    -> { ok, account_id }               登出并清 context
 *   GET  /health                 -> { ok: true }
 *
 * status 取值：pending | scanned | authorized | expired | failed（与 baileys 对齐，Python 侧归一）。
 * 每账号一个持久化目录 sessions/<account_id>/context.json（zca-js getContext → 免重复扫码）。
 *
 * 运行：
 *   cd services/zalo-personal && npm install && PORT=8792 node server.js
 *
 * ⚠️ zca-js 是**非官方** Zalo API（模拟 Zalo Web），有账号被限制/封禁风险。请：
 *   ① 使用小号；② 一号一独立代理；③ 接受风险。主进程需开
 *   config.platform_login.zalo.web_enabled=true 并指向 zca_url。
 *
 * 注意：zca-js 依赖 Zalo Web 的私有协议，平台改版可能需要升级 zca-js 版本。
 */

import express from "express";
import pino from "pino";
import { fileURLToPath } from "url";
import path from "path";
import fs from "fs";
import { Zalo, ThreadType } from "zca-js";
import { createGroupRegistry, resolveThreadType } from "./group-registry.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SESSIONS_DIR = process.env.ZALO_SESSIONS_DIR || path.join(__dirname, "sessions");
const PORT = Number(process.env.PORT || 8792);
// 只绑回环：入站路由无鉴权中间件，绑 0.0.0.0 等于把账号操作面开给整个局域网。
// 真实调用方只有本机 Python 引擎，全仓无远程引用。确需跨机时用 BIND_HOST 覆盖，
// 但**必须先给入站加鉴权**再放开。
const HOST = String(process.env.BIND_HOST || '127.0.0.1');
const logger = pino({ level: process.env.LOG_LEVEL || "info" });

// Python 主进程统一收件箱入站桥（可选；未配置则不上报）。
const PY_INGEST_URL = process.env.PY_INGEST_URL || "";
const PY_API_TOKEN = process.env.PY_API_TOKEN || "";
const PY_STATUS_URL =
  process.env.PY_STATUS_URL ||
  (PY_INGEST_URL ? PY_INGEST_URL.replace(/\/ingest\s*$/, "/session-status") : "");
// 入站同步开关（0 关闭入站，仅登录+发送）。
const ZALO_SYNC = String(process.env.ZALO_SYNC ?? "1") !== "0";

fs.mkdirSync(SESSIONS_DIR, { recursive: true });

// login_id -> { status, qrImage, accountId, api, ctx, displayName, avatarUrl }
const logins = new Map();
// account_id -> { api, ctx, loginId, loggedIn, displayName, avatarUrl }
const accounts = new Map();
// 群会话注册表（2026-08-19 P0）：出站自动判 ThreadType.Group ——
// 入站学习 + 登录 getAllGroups 预热 + groups.json 持久化；显式 chat_type 参数可覆盖。
const groupReg = createGroupRegistry({ sessionsDir: SESSIONS_DIR });

const nowSec = () => Math.floor(Date.now() / 1000);
const genId = () => "zl_" + Math.random().toString(36).slice(2, 12);

/** 通用 best-effort JSON POST（带鉴权头；失败只记 debug，绝不抛）。 */
async function postJson(url, payload) {
  if (!url) return;
  try {
    const headers = { "Content-Type": "application/json" };
    if (PY_API_TOKEN) headers["Authorization"] = `Bearer ${PY_API_TOKEN}`;
    await fetch(url, { method: "POST", headers, body: JSON.stringify(payload) });
  } catch (e) {
    logger.debug({ e: String(e), url }, "postJson failed");
  }
}

/** 会话健康状态 push（authorized / logged_out / expired），best-effort。 */
async function postStatus(accountId, status, detail, extra = {}) {
  if (!PY_STATUS_URL) return;
  await postJson(PY_STATUS_URL, {
    platform: "zalo",
    account_id: String(accountId || ""),
    status: String(status || ""),
    detail: String(detail || ""),
    display_name: String(extra.displayName || ""),
    avatar_url: String(extra.avatarUrl || ""),
    ts: nowSec(),
  });
}

const ctxPath = (accountId) => path.join(SESSIONS_DIR, String(accountId), "context.json");

function saveContext(accountId, ctx) {
  try {
    const dir = path.join(SESSIONS_DIR, String(accountId));
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(ctxPath(accountId), JSON.stringify(ctx || {}), "utf8");
  } catch (e) {
    logger.warn({ e: String(e), accountId }, "saveContext failed");
  }
}

function loadContext(accountId) {
  try {
    return JSON.parse(fs.readFileSync(ctxPath(accountId), "utf8"));
  } catch {
    return null;
  }
}

/** 把一条 Zalo 入站消息 push 到 Python 统一收件箱（best-effort）。 */
async function ingestMessage(accountId, message) {
  if (!ZALO_SYNC || !PY_INGEST_URL) return;
  try {
    if (message && message.isSelf) return; // 只回流对端消息，自发消息不入站
    const d = (message && message.data) || {};
    const content = d.content;
    // zca-js 的 content：文字为 string；富媒体为对象（此版本先只回流文字，媒体占位）。
    const text = typeof content === "string" ? content : "";
    const isGroup = message && message.type === ThreadType.Group;
    const threadId = String((message && message.threadId) || d.uidFrom || "");
    // 群会话学习：出站 ThreadType 自动解析的数据来源之一（见 group-registry.js）。
    if (isGroup && threadId) groupReg.remember(accountId, threadId);
    await postJson(PY_INGEST_URL, {
      platform: "zalo",
      account_id: String(accountId || ""),
      chat_key: threadId,
      // 群会话不把发言人名当会话名（dName=本条发言人；当群名会随发言人漂移）。
      // 空串在 ingest 侧「绝不覆盖已有非空值」，新群暂显 id ——诚实缺名。
      name: isGroup ? "" : String(d.dName || ""),
      text: text || "[媒体]",
      direction: "in",
      msg_id: String(d.msgId || d.cliMsgId || ""),
      ts: Number(d.ts ? Math.floor(Number(d.ts) / 1000) : nowSec()),
      chat_type: isGroup ? "group" : "",
      // P4-11E 同款群发言人结构化字段（对齐 WhatsApp）：气泡上方显示发言人名+稳定色。
      sender_id: isGroup ? String(d.uidFrom || "") : "",
      sender_name: isGroup ? String(d.dName || "") : "",
    });
  } catch (e) {
    logger.debug({ e: String(e) }, "ingestMessage failed");
  }
}

/** 给一个已登录的 api 挂上入站监听 + 记账到 accounts 表。 */
function attachAccount(accountId, api, ctx, loginId, profile) {
  const rec = {
    api,
    ctx,
    loginId: loginId || "",
    loggedIn: true,
    displayName: (profile && profile.displayName) || "",
    avatarUrl: (profile && profile.avatarUrl) || "",
  };
  accounts.set(String(accountId), rec);
  try {
    api.listener.on("message", (message) => {
      ingestMessage(accountId, message).catch(() => {});
    });
    api.listener.on("error", (err) => {
      logger.warn({ err: String(err), accountId }, "listener error");
    });
    // 掉线 → 主动上报会话健康，让 Python 侧快速失败 + 提示重登。
    api.listener.on("closed", () => {
      rec.loggedIn = false;
      postStatus(accountId, "logged_out", "listener closed").catch(() => {});
    });
    api.listener.start();
  } catch (e) {
    logger.warn({ e: String(e), accountId }, "attach listener failed");
  }
  // 群清单预热（best-effort）：getAllGroups → 注册表；失败不影响登录，
  // 入站学习会渐进补齐。跨版本返回形状差异由 extractGroupIds 防御。
  (async () => {
    try {
      if (api && typeof api.getAllGroups === "function") {
        const res = await api.getAllGroups();
        const added = groupReg.primeFromResult(accountId, res);
        logger.info(
          { accountId, added, total: groupReg.sizeOf(accountId) },
          "zalo groups primed"
        );
      }
    } catch (e) {
      logger.debug({ e: String(e), accountId }, "getAllGroups prime failed");
    }
  })();
  return rec;
}

async function fetchProfile(api) {
  try {
    const info = await api.fetchAccountInfo();
    const p = (info && (info.profile || info)) || {};
    return {
      displayName: String(p.displayName || p.zaloName || p.username || ""),
      avatarUrl: String(p.avatar || p.avatarUrl || ""),
    };
  } catch {
    return { displayName: "", avatarUrl: "" };
  }
}

const app = express();
app.use(express.json({ limit: "8mb" }));

app.get("/health", (_req, res) => res.json({ ok: true, svc: "zalo-personal" }));

app.post("/login/start", async (req, res) => {
  const loginId = genId();
  const proxyUrl = (req.body && req.body.proxy_url) || "";
  const entry = { status: "pending", qrImage: "", accountId: "", api: null, ctx: null };
  logins.set(loginId, entry);
  try {
    // proxy 透传：zca-js v2 支持在构造/登录时传代理（不同版本入口略有差异，best-effort）。
    const zalo = new Zalo({ selfListen: false, checkUpdate: false, logging: false });
    const options = {};
    if (proxyUrl) options.proxy = proxyUrl;
    // loginQR：回调收 QR 生成/扫码/成功等事件；promise resolve 时即登录成功。
    const apiPromise = zalo.loginQR(options, (event) => {
      try {
        const t = event && event.type;
        const data = (event && event.data) || {};
        // QRCodeGenerated：data.image 为 base64 PNG（无 data: 前缀）
        if (data.image) {
          entry.qrImage = String(data.image).startsWith("data:")
            ? String(data.image)
            : "data:image/png;base64," + String(data.image);
        }
        const ts = String(t || "").toLowerCase();
        if (ts.includes("scanned")) entry.status = "scanned";
        else if (ts.includes("expired")) entry.status = "expired";
        else if (ts.includes("declined")) entry.status = "failed";
      } catch (e) {
        logger.debug({ e: String(e) }, "qr callback err");
      }
    });
    // 后台等登录完成（不阻塞 /login/start 返回，前端轮询 status）。
    apiPromise
      .then(async (api) => {
        entry.api = api;
        const accountId = String(api.getOwnId ? api.getOwnId() : "") || genId();
        const ctx = api.getContext ? api.getContext() : null;
        const profile = await fetchProfile(api);
        entry.accountId = accountId;
        entry.ctx = ctx;
        entry.displayName = profile.displayName;
        entry.avatarUrl = profile.avatarUrl;
        entry.status = "authorized";
        saveContext(accountId, ctx);
        attachAccount(accountId, api, ctx, loginId, profile);
        await postStatus(accountId, "authorized", "login ok", profile);
        logger.info({ accountId, loginId }, "zalo login authorized");
      })
      .catch((e) => {
        entry.status = "failed";
        logger.warn({ e: String(e), loginId }, "loginQR failed");
      });
    // 给回调一点时间产出首帧 QR
    await new Promise((r) => setTimeout(r, 800));
    res.json({ login_id: loginId, qr_image: entry.qrImage, status: entry.status });
  } catch (e) {
    entry.status = "failed";
    logger.error({ e: String(e), loginId }, "login/start failed");
    res.status(500).json({ ok: false, error: String(e) });
  }
});

app.get("/login/:id/status", (req, res) => {
  const entry = logins.get(req.params.id);
  if (!entry) return res.status(404).json({ status: "failed", error: "unknown login_id" });
  res.json({
    status: entry.status,
    account_id: entry.accountId || "",
    qr_image: entry.status === "authorized" ? "" : entry.qrImage,
    display_name: entry.displayName || "",
    avatar_url: entry.avatarUrl || "",
  });
});

app.post("/login/:id/cancel", async (req, res) => {
  const entry = logins.get(req.params.id);
  if (entry) {
    try {
      if (entry.api && entry.api.listener) entry.api.listener.stop();
    } catch {
      /* ignore */
    }
    logins.delete(req.params.id);
  }
  res.json({ ok: true });
});

app.post("/accounts/restore", async (_req, res) => {
  let restored = 0;
  let dirs = [];
  try {
    dirs = fs
      .readdirSync(SESSIONS_DIR, { withFileTypes: true })
      .filter((d) => d.isDirectory())
      .map((d) => d.name);
  } catch {
    dirs = [];
  }
  for (const accountId of dirs) {
    if (accounts.has(accountId) && accounts.get(accountId).loggedIn) continue;
    const ctx = loadContext(accountId);
    if (!ctx) continue;
    try {
      const zalo = new Zalo({ selfListen: false, checkUpdate: false, logging: false });
      // zca-js v2：用持久化的 cookie/imei/userAgent 免扫码恢复。
      const api = await zalo.login({
        cookie: ctx.cookie,
        imei: ctx.imei,
        userAgent: ctx.userAgent,
        language: ctx.language,
      });
      const profile = await fetchProfile(api);
      attachAccount(accountId, api, ctx, "", profile);
      await postStatus(accountId, "authorized", "restored", profile);
      restored += 1;
    } catch (e) {
      logger.warn({ e: String(e), accountId }, "restore failed");
      await postStatus(accountId, "expired", "restore failed").catch(() => {});
    }
  }
  res.json({ ok: true, restored });
});

app.get("/accounts", (_req, res) => {
  const out = [];
  for (const [accountId, rec] of accounts.entries()) {
    out.push({
      account_id: accountId,
      logged_in: !!rec.loggedIn,
      display_name: rec.displayName || "",
      avatar_url: rec.avatarUrl || "",
    });
  }
  res.json({ accounts: out });
});

/** 出站线程类型：显式 chat_type 最高优先 → 群注册表命中 → User。
 *  修复 2026-08-19 P0：Python 侧从不传 chat_type → 群回复按 User 发（错目标）。 */
function threadTypeFor(accountId, threadId, chatType) {
  const kind = resolveThreadType({
    explicit: chatType,
    isKnownGroup: groupReg.isGroup(accountId, threadId),
  });
  return { kind, tt: kind === "group" ? ThreadType.Group : ThreadType.User };
}

app.post("/accounts/:id/send", async (req, res) => {
  const rec = accounts.get(req.params.id);
  if (!rec || !rec.api) return res.status(404).json({ ok: false, error: "account not connected" });
  const threadId = String((req.body && req.body.thread_id) || "");
  const text = String((req.body && req.body.text) || "");
  const { kind, tt } = threadTypeFor(req.params.id, threadId, req.body && req.body.chat_type);
  if (!threadId) return res.status(400).json({ ok: false, error: "thread_id required" });
  try {
    const r = await rec.api.sendMessage({ msg: text }, threadId, tt);
    const messageId = String((r && (r.msgId || r.message_id)) || "");
    res.json({ ok: true, delivered: true, message_id: messageId, thread_type: kind });
  } catch (e) {
    logger.warn({ e: String(e), id: req.params.id, thread_type: kind }, "send failed");
    res.status(502).json({ ok: false, delivered: false, error: String(e) });
  }
});

app.post("/accounts/:id/send-media", async (req, res) => {
  const rec = accounts.get(req.params.id);
  if (!rec || !rec.api) return res.status(404).json({ ok: false, error: "account not connected" });
  const threadId = String((req.body && req.body.thread_id) || "");
  const mediaPath = String((req.body && req.body.media_path) || "");
  const caption = String((req.body && req.body.caption) || "");
  const { kind, tt } = threadTypeFor(req.params.id, threadId, req.body && req.body.chat_type);
  if (!threadId) return res.status(400).json({ ok: false, error: "thread_id required" });
  if (!mediaPath || !fs.existsSync(mediaPath))
    return res.status(400).json({ ok: false, error: "media_path missing" });
  try {
    // zca-js v2：sendMessage 支持 attachments（本地文件路径数组）。
    const r = await rec.api.sendMessage(
      { msg: caption, attachments: [mediaPath] },
      threadId,
      tt
    );
    const messageId = String((r && (r.msgId || r.message_id)) || "");
    res.json({ ok: true, delivered: true, message_id: messageId, thread_type: kind });
  } catch (e) {
    logger.warn({ e: String(e), id: req.params.id, thread_type: kind }, "send-media failed");
    res.status(502).json({ ok: false, delivered: false, error: String(e) });
  }
});

app.post("/accounts/:id/logout", async (req, res) => {
  const accountId = req.params.id;
  const rec = accounts.get(accountId);
  try {
    if (rec && rec.api && rec.api.listener) rec.api.listener.stop();
  } catch {
    /* ignore */
  }
  accounts.delete(accountId);
  try {
    fs.rmSync(path.join(SESSIONS_DIR, String(accountId)), { recursive: true, force: true });
  } catch {
    /* ignore */
  }
  await postStatus(accountId, "logged_out", "manual logout").catch(() => {});
  res.json({ ok: true, account_id: accountId });
});

const server = app.listen(PORT, HOST, async () => {
  logger.info(`Zalo personal login service on :${PORT} (sessions: ${SESSIONS_DIR})`);
  // 开机自动恢复已持久化的会话（幂等；Python 侧 worker 也会触发 /accounts/restore）。
  try {
    const dirs = fs
      .readdirSync(SESSIONS_DIR, { withFileTypes: true })
      .filter((d) => d.isDirectory());
    if (dirs.length) {
      await fetch(`http://127.0.0.1:${PORT}/accounts/restore`, { method: "POST" }).catch(
        () => {}
      );
    }
  } catch {
    /* ignore */
  }
});

// 端口占用守卫：计划任务在已有实例常驻时被触发（AtLogOn 双起）→ 干净退出而非噪声崩溃。
server.on("error", (e) => {
  if (e && e.code === "EADDRINUSE") {
    logger.warn(`port ${PORT} already in use — another instance is running; exiting`);
    process.exit(0);
  }
  logger.error({ e: String(e) }, "listen error");
  process.exit(1);
});
