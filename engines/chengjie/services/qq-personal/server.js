/**
 * 智聊自研 QQ 个人号协议边车 —— Milky HTTP/WS 外壳。
 *
 * 定位：注入本机 QQ 客户端、驱动其内核收发（签名由 QQ 自身完成，**不连外部签名服务、
 * 不需任何第三方 token/审核**），对上以 Milky 协议暴露，供智聊后端
 * src/integrations/qq_milky.py 对接。底层收发由 ntq/driver.js 提供的驱动完成
 * （真实注入 qqnt-driver / 离线 mock-driver，见 ntq/NOTICE.md）。
 *
 * 对上契约（Milky 1.3 子集 + 智聊扩展）：
 *   POST /api/:api    -> { status:"ok"|"failed", retcode, data|message }
 *      系统:   get_login_info / get_impl_info
 *      消息:   send_private_message / send_group_message / recall_private_message /
 *              recall_group_message / mark_message_as_read
 *      文件:   upload_private_file / upload_group_file /
 *              get_private_file_download_url / get_group_file_download_url
 *      好友群: accept_friend_request / kick_group_member / set_group_name
 *      扩展:   x_get_login_qrcode / x_quick_login_list / x_quick_login / x_logout
 *      其余 api -> retcode -404（qq_milky.MilkyClient 归一为「协议端不支持」）
 *   GET  /event       -> WebSocket，逐条推 { time, self_id, event_type, data }
 *   GET  /health      -> { ok, svc:"qq-personal", qq_installed, qq_version, driver,
 *                          login_state, self, download_progress }
 *
 * 鉴权：Milky 标准 `Authorization: Bearer <token>`（QQ_MILKY_TOKEN，留空=不校验，仅本机回环）。
 * 端口：QQ_MILKY_PORT（默认 8792，与 desktop/sidecar-launcher.js SPECS.qq 同源）。
 *
 * 入站另回推 Python 统一收件箱（PY_INGEST_URL/PY_STATUS_URL + PY_API_TOKEN），与
 * whatsapp-baileys / zalo-personal 边车同一桥接口径——但主链路是 Milky /event（qq_milky
 * 的 QQPersonalWorker 直接消费），回推仅为与其它边车对齐的可选旁路，默认关。
 */
import express from "express";
import pino from "pino";
import { WebSocketServer } from "ws";
import { createDriver } from "./ntq/driver.js";

const SVC_ID = "qq-personal";
const PORT = Number(process.env.QQ_MILKY_PORT || process.env.PORT || 8792);
const HOST = String(process.env.BIND_HOST || "127.0.0.1");
const TOKEN = String(process.env.QQ_MILKY_TOKEN || "").trim();
const PY_INGEST_URL = process.env.PY_INGEST_URL || "";
const PY_STATUS_URL = process.env.PY_STATUS_URL || "";
const PY_API_TOKEN = process.env.PY_API_TOKEN || "";
const PUSH_INGEST = String(process.env.QQ_PUSH_INGEST ?? "0") !== "0";

const logger = pino({ level: process.env.LOG_LEVEL || "info" });
const nowSec = () => Math.floor(Date.now() / 1000);

let driver = null;
const wsClients = new Set();

function ok(data) { return { status: "ok", retcode: 0, data: data || {} }; }
function failed(retcode, message) { return { status: "failed", retcode: Number(retcode || -1), message: String(message || "") }; }

async function postJson(url, payload) {
  if (!url) return;
  try {
    const headers = { "Content-Type": "application/json" };
    if (PY_API_TOKEN) headers["Authorization"] = `Bearer ${PY_API_TOKEN}`;
    await fetch(url, { method: "POST", headers, body: JSON.stringify(payload) });
  } catch (e) { logger.debug({ e: String(e), url }, "postJson failed"); }
}

function broadcastEvent(ev) {
  const line = JSON.stringify(ev);
  for (const ws of wsClients) {
    try { if (ws.readyState === ws.OPEN) ws.send(line); } catch (e) { /* 单连失败不影响其余 */ }
  }
  if (PUSH_INGEST && ev && ev.event_type === "message_receive") {
    postJson(PY_INGEST_URL, { platform: "qq", raw_event: ev });  // 可选旁路，默认关
  }
}

function _int(v) { const n = Number(v); return Number.isFinite(n) ? Math.trunc(n) : null; }

// ── Milky API 分发（纯路由到 driver；driver 保证不抛） ──────────────────────
async function dispatch(api, p) {
  const d = driver;
  switch (api) {
    case "get_login_info": {
      const self = d.selfInfo();
      if (!self) return failed(-403, "QQ 尚未登录");
      return ok({ uin: self.uin, nickname: self.nickname });
    }
    case "get_impl_info": {
      // Milky 标准字段 + 智聊扩展三字段（driver/login_state/qq_version 由 /health 同源）：
      // qq_milky worker 的 status() 直接把它们透到运维面，不用再单独打 /health
      const impl = await d.getImplInfo();
      const info = await d.info();
      return ok({ ...impl, driver: info.driver || "", login_state: d.loginState(),
                  qq_version: impl.qq_version || info.qq_version || "" });
    }
    case "send_private_message": {
      const r = await d.sendPrivate(_int(p.user_id), p.message || []);
      return r.ok ? ok({ message_seq: r.message_seq, time: nowSec() }) : failed(r.retcode ?? -1, r.error);
    }
    case "send_group_message": {
      const r = await d.sendGroup(_int(p.group_id), p.message || []);
      return r.ok ? ok({ message_seq: r.message_seq, time: nowSec() }) : failed(r.retcode ?? -1, r.error);
    }
    case "recall_private_message": {
      const r = await d.recallPrivate(_int(p.user_id), _int(p.message_seq));
      return r.ok ? ok({}) : failed(r.retcode ?? -1, r.error);
    }
    case "recall_group_message": {
      const r = await d.recallGroup(_int(p.group_id), _int(p.message_seq));
      return r.ok ? ok({}) : failed(r.retcode ?? -1, r.error);
    }
    case "mark_message_as_read": {
      const r = await d.markRead(String(p.message_scene || "friend"), _int(p.peer_id), _int(p.message_seq));
      return r.ok ? ok({}) : failed(r.retcode ?? -1, r.error);
    }
    case "upload_private_file": {
      const r = await d.uploadPrivateFile(_int(p.user_id), String(p.file_uri || ""), String(p.file_name || "file"));
      return r.ok ? ok({ file_id: r.file_id }) : failed(r.retcode ?? -1, r.error);
    }
    case "upload_group_file": {
      const r = await d.uploadGroupFile(_int(p.group_id), String(p.file_uri || ""), String(p.file_name || "file"));
      return r.ok ? ok({ file_id: r.file_id }) : failed(r.retcode ?? -1, r.error);
    }
    case "get_private_file_download_url": {
      const r = await d.privateFileUrl(_int(p.user_id), String(p.file_id || ""), String(p.file_hash || ""));
      return r.ok ? ok({ download_url: r.download_url }) : failed(r.retcode ?? -1, r.error);
    }
    case "get_group_file_download_url": {
      const r = await d.groupFileUrl(_int(p.group_id), String(p.file_id || ""));
      return r.ok ? ok({ download_url: r.download_url }) : failed(r.retcode ?? -1, r.error);
    }
    case "accept_friend_request": {
      const r = await d.acceptFriend(String(p.initiator_uid || ""), !!p.is_filtered);
      return r.ok ? ok({}) : failed(r.retcode ?? -1, r.error);
    }
    case "kick_group_member": {
      const r = await d.kickGroupMember(_int(p.group_id), _int(p.user_id), !!p.reject_add_request);
      return r.ok ? ok({}) : failed(r.retcode ?? -1, r.error);
    }
    case "set_group_name": {
      const r = await d.setGroupName(_int(p.group_id), String(p.new_group_name || ""));
      return r.ok ? ok({}) : failed(r.retcode ?? -1, r.error);
    }
    // ── 智聊扩展（Milky 标准无，x_ 前缀）─────────────────────────────────
    case "x_get_login_qrcode": {
      const r = await d.startLogin();
      return r.ok ? ok({ qr_png_base64: r.qr_png_base64, qr_url: r.qr_url, expire_sec: r.expire_sec })
                  : failed(r.retcode ?? -1, r.error);
    }
    case "x_quick_login_list":
      return ok({ accounts: d.quickLoginList() });
    case "x_quick_login": {
      const r = await d.quickLogin(String(p.uin || ""));
      return r.ok ? ok({}) : failed(r.retcode ?? -1, r.error);
    }
    case "x_logout": {
      const r = await d.logout();
      return r.ok ? ok({}) : failed(r.retcode ?? -1, r.error);
    }
    default:
      return failed(-404, `unsupported api: ${api}`);
  }
}

async function main() {
  driver = await createDriver({ logger });
  driver.onEvent((ev) => broadcastEvent(ev));

  const app = express();
  app.use(express.json({ limit: "32mb" }));

  // Milky 鉴权：设了 token 才校验（本机回环默认不设）
  app.use("/api", (req, res, next) => {
    if (!TOKEN) return next();
    const h = String(req.headers["authorization"] || "");
    if (h === `Bearer ${TOKEN}`) return next();
    return res.status(401).json(failed(-401, "access_token 不匹配"));
  });

  app.post("/api/:api", async (req, res) => {
    try {
      const out = await dispatch(String(req.params.api || ""), req.body || {});
      res.json(out);
    } catch (e) {
      logger.error({ e: String(e), api: req.params.api }, "dispatch crashed");
      res.json(failed(-1, String((e && e.message) || e)));
    }
  });

  app.get("/health", async (req, res) => {
    const info = await driver.info();
    res.json({
      // svc 值必须与 desktop/sidecar-launcher.js SPECS.qq.svcId 一致（跨文件漂移门禁钉住）
      ok: true, svc: "qq-personal",
      qq_installed: !!info.qq_installed, qq_version: info.qq_version || "",
      driver: info.driver || "mock", driver_reason: info.driver_reason || "",
      login_state: driver.loginState(), self: driver.selfInfo(),
      download_progress: info.download_progress ?? null,
    });
  });

  const server = app.listen(PORT, HOST, () => {
    logger.info(`[qq-personal] Milky 服务 http://${HOST}:${PORT} (driver=${driver.kind || "?"})`);
  });

  // /event WebSocket：连上即持续推事件（同 Milky 通信标准）
  const wss = new WebSocketServer({ server, path: "/event" });
  wss.on("connection", (ws, req) => {
    if (TOKEN) {
      const h = String((req.headers && req.headers["authorization"]) || "");
      if (h !== `Bearer ${TOKEN}`) { try { ws.close(1008, "unauthorized"); } catch (e) {} return; }
    }
    wsClients.add(ws);
    ws.on("close", () => wsClients.delete(ws));
    ws.on("error", () => wsClients.delete(ws));
  });

  const shutdown = async () => {
    try { await driver.stop(); } catch (e) {}
    try { server.close(); } catch (e) {}
    process.exit(0);
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
}

main().catch((e) => { logger.error({ e: String(e) }, "qq-personal 启动失败"); process.exit(1); });
