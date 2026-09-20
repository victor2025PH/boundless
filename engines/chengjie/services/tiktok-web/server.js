/**
 * TikTok 个人号网页托管登录 + **只读**收件箱边车（Playwright 加载 tiktok.com）。TK-3 ②-A 阶段 1。
 *
 * 定位：真机（huoke）路不可用时的**备用读路**——把个人号私信读进智聊统一收件箱，让坐席看得见、
 * 智聊能起草；**不代发**（assistOnly）。发送由真机路承担；边车人工可发（②-B）要等 ①+②-A 跑满
 * 72 小时自有账号验证之后再开，且永不自动发。
 *
 * 与 Python 主进程（src/integrations/tiktok_web_login.py）通过本地 HTTP 桥接，契约对齐 services/instagram-web：
 *
 *   POST /login/start            -> { login_id, qr_image, status }   发起一次登录（弹官方登录页；qr_image 是登录页截图）
 *   GET  /login/:id/status       -> { status, account_id, qr_image, name, username, avatar_url, hint_code }
 *   POST /login/:id/cancel       -> { ok }
 *   POST /accounts/restore       -> { ok, restored }                 恢复磁盘已持久化的登录
 *   GET  /accounts               -> { accounts: [...] }
 *   POST /accounts/:id/send      -> 501 { ok:false, error:"assist_only" }   阶段 1 刻意不实现
 *   POST /accounts/:id/send-media-> 501 { ok:false, error:"assist_only" }
 *   POST /accounts/:id/logout    -> { ok, account_id }
 *   GET  /health                 -> { ok: true, svc: "tiktok-web", assist_only: true }
 *
 * status：pending | scanned | authorized | expired | failed（Python 侧归一）。
 * 每账号独立持久化 userDataDir（sessions/<login_id>/）。
 * account_id ＝ TikTok 数字 uid（页面水合数据 __UNIVERSAL_DATA_FOR_REHYDRATION__ → webapp.app-context.user.uid；
 *              取不到回落 cookie uid_tt）——**联调核对项**。
 *
 * ⚠️ 非官方接入：依赖 tiktok.com 的 DOM / 水合数据，改版可能需微调（SEL_* / URL_* / HYDRATION_* 集中区）。
 *    有 ToS / 风控风险：小号 + 一号一代理 + 养号；默认关，需主配置 platform_login.tiktok.web_enabled 显式开启。
 *    选择器尚未用可用区真实账号采样核对（TikTok 私信网页版在部分地区不可用）——首次联调按 README 清单逐项核对。
 *
 * 运行：cd services/tiktok-web && npm install && PORT=8794 node server.js
 */

import express from "express";
import pino from "pino";
import { fileURLToPath } from "url";
import path from "path";
import fs from "fs";
import { chromium } from "playwright";
import { parseTtThreadRow, threadPreviewKey } from "./tt_threads.js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SESSIONS_DIR = process.env.TT_SESSIONS_DIR || path.join(__dirname, "sessions");
const PORT = Number(process.env.PORT || 8794);
// 只绑回环：入站路由无鉴权中间件，绑 0.0.0.0 等于把账号操作面开给整个局域网。
const HOST = String(process.env.BIND_HOST || "127.0.0.1");
const logger = pino({ level: process.env.LOG_LEVEL || "info" });

const HEADLESS = String(process.env.TT_HEADLESS ?? "0") === "1";
const RESTORE_HEADLESS = String(process.env.TT_RESTORE_HEADLESS ?? "1") === "1";
const BROWSER_CHANNEL = process.env.TT_BROWSER_CHANNEL || "chrome";
const POLL_MS = Number(process.env.TT_POLL_MS || 15000); // 入站轮询间隔（比 IG 更慢：TikTok 风控更敏感）；0 关闭
const REAL_UA = process.env.TT_UA ||
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";
// 阶段 1 硬编码 assistOnly：不是配置项，是产品决策（D-P：边车永不自动发；人工可发是 ②-B 另开的口）。
const ASSIST_ONLY = true;

const PY_INGEST_URL = process.env.PY_INGEST_URL || "";
const PY_API_TOKEN = process.env.PY_API_TOKEN || "";
const PY_STATUS_URL =
  process.env.PY_STATUS_URL ||
  (PY_INGEST_URL ? PY_INGEST_URL.replace(/\/ingest\s*$/, "/session-status") : "");
const TT_SYNC = String(process.env.TT_SYNC ?? "1") !== "0";

// ── tiktok.com 选择器 / URL / 水合数据 集中区（改版只改这里；**联调核对**）────────────────
const URL_HOME = "https://www.tiktok.com/";
const URL_LOGIN = "https://www.tiktok.com/login";
const URL_INBOX = "https://www.tiktok.com/messages";
const SEL_LOGGED_IN = 'a[href="/messages"], [data-e2e="profile-icon"], [data-e2e="nav-profile"]';
const SEL_INBOX_ROW = '[data-e2e="chat-list-item"], a[href^="/messages?u="]';
const HYDRATION_SCRIPT_ID = "__UNIVERSAL_DATA_FOR_REHYDRATION__";
const HYDRATION_USER_PATH = ["__DEFAULT_SCOPE__", "webapp.app-context", "user"];
const COOKIE_UID_FALLBACK = "uid_tt";

fs.mkdirSync(SESSIONS_DIR, { recursive: true });

// login_id -> { status, qrImage, accountId, context, page, name, username, avatarUrl, hintCode }
const logins = new Map();
// account_id -> { context, page, loginId, loggedIn, name, username, avatarUrl }
const accounts = new Map();

const nowSec = () => Math.floor(Date.now() / 1000);
const genId = () => "tt_" + Math.random().toString(36).slice(2, 12);

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

async function postStatus(accountId, status, detail, extra = {}) {
  if (!PY_STATUS_URL) return;
  await postJson(PY_STATUS_URL, {
    platform: "tiktok",
    account_id: String(accountId || ""),
    status: String(status || ""),
    detail: String(detail || ""),
    name: String(extra.name || ""),
    username: String(extra.username || ""),
    avatar_url: String(extra.avatarUrl || ""),
    ts: nowSec(),
  });
}

function launchOptions(proxyUrl, channel, headless) {
  const opts = { headless, viewport: { width: 1180, height: 820 }, userAgent: REAL_UA };
  if (channel) opts.channel = channel;
  if (proxyUrl) opts.proxy = { server: proxyUrl };
  return opts;
}

function isChannelUnavailable(err) {
  const s = String((err && err.message) || err || "");
  return /executable doesn't exist|Chromium distribution|is not found|Failed to launch|ENOENT|cannot find/i.test(s);
}

async function launchPersistent(userDataDir, proxyUrl, headless) {
  try {
    return await chromium.launchPersistentContext(userDataDir, launchOptions(proxyUrl, BROWSER_CHANNEL, headless));
  } catch (e) {
    if (!BROWSER_CHANNEL || !isChannelUnavailable(e)) throw e;
    logger.warn({ e: String(e) }, "chrome channel unavailable → bundled chromium fallback");
    return await chromium.launchPersistentContext(userDataDir, launchOptions(proxyUrl, "", headless));
  }
}

/** 页面水合数据里的当前用户（uid / uniqueId / nickName / avatarUri）；取不到 → null。 */
async function hydratedUser(page) {
  try {
    return await page.evaluate(([sid, pathParts]) => {
      const el = document.getElementById(sid);
      if (!el) return null;
      let node = JSON.parse(el.textContent || "{}");
      for (const p of pathParts) { node = node && node[p]; }
      if (!node || typeof node !== "object") return null;
      return {
        uid: String(node.uid || node.id || ""),
        uniqueId: String(node.uniqueId || node.unique_id || ""),
        nickName: String(node.nickName || node.nickname || ""),
        avatarUri: String(node.avatarUri || node.avatar_thumb || ""),
      };
    }, [HYDRATION_SCRIPT_ID, HYDRATION_USER_PATH]);
  } catch {
    return null;
  }
}

/** account_id：水合 uid 优先；回落 cookie uid_tt（联调核对）。 */
async function accountIdFromPage(page, context) {
  const u = await hydratedUser(page);
  if (u && u.uid) return u.uid;
  try {
    const cookies = await context.cookies("https://www.tiktok.com");
    const c = (cookies || []).find((x) => x.name === COOKIE_UID_FALLBACK);
    return c ? String(c.value) : "";
  } catch {
    return "";
  }
}

/** 已登录：有账号 id、不在登录 / 验证页、且出现主导航锚点。 */
async function pageLoggedIn(page, context) {
  try {
    const url = page.url() || "";
    if (/\/login|\/passport\/|captcha|verify/i.test(url)) return false;
    const aid = await accountIdFromPage(page, context);
    if (!aid) return false;
    const anchor = await page.$(SEL_LOGGED_IN);
    return !!anchor;
  } catch {
    return false;
  }
}

/** 登录页此刻在要什么（实时提示码）。 */
async function loginHint(page) {
  const url = page.url() || "";
  if (/captcha|verify/i.test(url)) return "captcha";
  if (/two|2fa|verification/i.test(url)) return "two_factor";
  return "";
}

async function fetchProfile(page) {
  const out = { name: "", username: "", avatarUrl: "" };
  const u = await hydratedUser(page);
  if (u) {
    out.name = u.nickName || u.uniqueId || "";
    out.username = u.uniqueId || "";
    out.avatarUrl = u.avatarUri || "";
  }
  return out;
}

async function screenshotDataUri(page) {
  try {
    const buf = await page.screenshot({ type: "png" });
    return "data:image/png;base64," + buf.toString("base64");
  } catch {
    return "";
  }
}

/** 一条 TikTok 私信入站 → Python 统一收件箱（best-effort）。source 标 tiktok_web / mode web（来源徽标「网页」）。 */
async function ingestMessage(accountId, payload) {
  if (!TT_SYNC || !PY_INGEST_URL) return;
  await postJson(PY_INGEST_URL, {
    platform: "tiktok",
    account_id: String(accountId || ""),
    direction: "in",
    source: { source: "tiktok_web", mode: "web", assist_only: true },
    ...payload,
  });
}

// ── 入站轮询（只读收件箱列表 + 末条预览；刻意不进线程：进线程给对方留已读回执）────────
const _lastPreview = new Map(); // `${accountId}:${chatKey}` -> 上次语义预览键

async function pollInbox(accountId, rec) {
  if (!POLL_MS || !rec || !rec.loggedIn || !rec.page) return;
  try {
    const page = rec.page;
    await page.goto(URL_INBOX, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForTimeout(2000);
    const rows = await page.$$eval(SEL_INBOX_ROW, (els) =>
      els.slice(0, 20).map((el) => {
        const a = el.closest("a") || el.querySelector("a") || el;
        const href = a.getAttribute ? (a.getAttribute("href") || "") : "";
        const rowText = (el.textContent || "").trim().replace(/\s+/g, " ");
        const spans = Array.from(el.querySelectorAll("span, div, p"))
          .filter((x) => !x.querySelector("span, div, p"))
          .map((x) => (x.textContent || "").trim().replace(/\s+/g, " "))
          .filter(Boolean);
        return { href, rowText, spans };
      })
    );
    for (const raw of rows) {
      const row = parseTtThreadRow(raw);
      if (!row.chatKey) continue;
      const key = `${accountId}:${row.chatKey}`;
      const pkey = threadPreviewKey(row);
      const prev = _lastPreview.get(key);
      if (prev === pkey) continue;
      _lastPreview.set(key, pkey);
      if (prev === undefined) continue; // 首见只建基线，不把历史当新消息惊动 AI
      if (row.directionHint === "out") continue; // 自己发的（真机 / 手机端）不当入站；出站镜像以智聊自己的为准
      await ingestMessage(accountId, {
        chat_key: row.chatKey,
        name: row.title,
        text: row.text || "[新消息]",
        msg_id: `${row.chatKey}:${nowSec()}`,
        ts: nowSec(),
        // 判据定位锚：能力矩阵按「含 direction: 键的对象字面量」找 ingest payload 块
        direction: "in",
      });
    }
  } catch (e) {
    logger.debug({ e: String(e), accountId }, "pollInbox failed");
  }
}

let _pollTimer = null;
function startPollLoop() {
  if (_pollTimer || !POLL_MS) return;
  _pollTimer = setInterval(async () => {
    for (const [accountId, rec] of accounts.entries()) {
      await pollInbox(accountId, rec);
    }
  }, POLL_MS);
}

function attachAccount(accountId, context, page, loginId, profile) {
  const rec = {
    context, page, loginId: loginId || "", loggedIn: true,
    name: (profile && profile.name) || "",
    username: (profile && profile.username) || "",
    avatarUrl: (profile && profile.avatarUrl) || "",
  };
  accounts.set(String(accountId), rec);
  startPollLoop();
  return rec;
}

const app = express();
app.use(express.json({ limit: "2mb" }));

app.get("/health", (_req, res) => res.json({ ok: true, svc: "tiktok-web", assist_only: ASSIST_ONLY }));

app.post("/login/start", async (req, res) => {
  const loginId = genId();
  const proxyUrl = (req.body && req.body.proxy_url) || "";
  const entry = { status: "pending", qrImage: "", accountId: "", context: null, page: null };
  logins.set(loginId, entry);
  try {
    const userDataDir = path.join(SESSIONS_DIR, loginId);
    fs.mkdirSync(userDataDir, { recursive: true });
    const context = await launchPersistent(userDataDir, proxyUrl, HEADLESS);
    entry.context = context;
    const page = context.pages()[0] || (await context.newPage());
    entry.page = page;
    await page.goto(URL_HOME, { waitUntil: "domcontentloaded", timeout: 45000 });
    if (await pageLoggedIn(page, context)) {
      await finalizeLogin(loginId, entry);
    } else {
      await page.goto(URL_LOGIN, { waitUntil: "domcontentloaded", timeout: 45000 }).catch(() => {});
      entry.qrImage = await screenshotDataUri(page);
      pollLogin(loginId, entry).catch((e) => logger.debug({ e: String(e) }, "pollLogin err"));
    }
    res.json({ login_id: loginId, qr_image: entry.qrImage, status: entry.status });
  } catch (e) {
    entry.status = "failed";
    logger.error({ e: String(e), loginId }, "login/start failed");
    res.status(500).json({ ok: false, error: String(e) });
  }
});

async function finalizeLogin(loginId, entry) {
  const accountId = await accountIdFromPage(entry.page, entry.context);
  if (!accountId) return;
  const profile = await fetchProfile(entry.page);
  entry.accountId = accountId;
  entry.name = profile.name;
  entry.username = profile.username;
  entry.avatarUrl = profile.avatarUrl;
  entry.status = "authorized";
  attachAccount(accountId, entry.context, entry.page, loginId, profile);
  await postStatus(accountId, "authorized", "login ok", profile);
  logger.info({ accountId, loginId, username: profile.username }, "tiktok login authorized (assist-only sidecar)");
}

async function pollLogin(loginId, entry) {
  const deadline = Date.now() + 30 * 60 * 1000;
  while (Date.now() < deadline) {
    const e = logins.get(loginId);
    if (!e || e.status === "authorized" || e.status === "failed") return;
    try {
      if (await pageLoggedIn(entry.page, entry.context)) {
        await finalizeLogin(loginId, entry);
        return;
      }
      e.hintCode = await loginHint(entry.page);
      e.qrImage = await screenshotDataUri(entry.page);
    } catch (err) {
      logger.debug({ err: String(err), loginId }, "pollLogin tick err");
    }
    await new Promise((r) => setTimeout(r, 3000));
  }
  const e = logins.get(loginId);
  if (e && e.status === "pending") e.status = "expired";
}

app.get("/login/:id/status", (req, res) => {
  const entry = logins.get(req.params.id);
  if (!entry) return res.status(404).json({ status: "failed", error: "unknown login_id" });
  res.json({
    status: entry.status,
    account_id: entry.accountId || "",
    qr_image: entry.status === "authorized" ? "" : (entry.qrImage || ""),
    name: entry.name || "",
    username: entry.username || "",
    avatar_url: entry.avatarUrl || "",
    hint_code: entry.hintCode || "",
  });
});

app.post("/login/:id/cancel", async (req, res) => {
  const entry = logins.get(req.params.id);
  if (entry) {
    if (entry.status !== "authorized" && entry.context) {
      try { await entry.context.close(); } catch { /* ignore */ }
    }
    logins.delete(req.params.id);
  }
  res.json({ ok: true });
});

app.post("/accounts/restore", async (_req, res) => {
  let restored = 0;
  let dirs = [];
  try {
    dirs = fs.readdirSync(SESSIONS_DIR, { withFileTypes: true }).filter((d) => d.isDirectory()).map((d) => d.name);
  } catch { dirs = []; }
  for (const loginId of dirs) {
    try {
      const userDataDir = path.join(SESSIONS_DIR, loginId);
      const context = await launchPersistent(userDataDir, "", RESTORE_HEADLESS);
      const page = context.pages()[0] || (await context.newPage());
      await page.goto(URL_HOME, { waitUntil: "domcontentloaded", timeout: 45000 }).catch(() => {});
      if (await pageLoggedIn(page, context)) {
        const accountId = await accountIdFromPage(page, context);
        if (accountId && !(accounts.has(accountId) && accounts.get(accountId).loggedIn)) {
          const profile = await fetchProfile(page);
          attachAccount(accountId, context, page, loginId, profile);
          await postStatus(accountId, "authorized", "restored", profile);
          restored += 1;
          continue;
        }
      }
      await context.close().catch(() => {});
    } catch (e) {
      logger.warn({ e: String(e), loginId }, "restore failed");
    }
  }
  res.json({ ok: true, restored });
});

app.get("/accounts", (_req, res) => {
  const out = [];
  for (const [accountId, rec] of accounts.entries()) {
    out.push({ account_id: accountId, logged_in: !!rec.loggedIn, name: rec.name || "", username: rec.username || "",
               avatar_url: rec.avatarUrl || "", assist_only: ASSIST_ONLY });
  }
  res.json({ accounts: out });
});

// 阶段 1 assistOnly：发送端点存在但**明确拒绝**（501），让调用方拿到可判定的 reason 而不是 404 误判「路由没挂」。
// ②-B 人工可发要等 ① + ②-A 跑满 72h 自有账号验证再开；自动发永不。
function assistOnlyReject(_req, res) {
  res.status(501).json({ ok: false, delivered: false, error: "assist_only",
                         detail: "tiktok-web 阶段 1 只读：不代发私信；发送走 huoke 真机路" });
}
app.post("/accounts/:id/send", assistOnlyReject);
app.post("/accounts/:id/send-media", assistOnlyReject);

app.post("/accounts/:id/logout", async (req, res) => {
  const accountId = req.params.id;
  const rec = accounts.get(accountId);
  try { if (rec && rec.context) await rec.context.close(); } catch { /* ignore */ }
  accounts.delete(accountId);
  if (rec && rec.loginId) {
    try { fs.rmSync(path.join(SESSIONS_DIR, rec.loginId), { recursive: true, force: true }); } catch { /* ignore */ }
  }
  await postStatus(accountId, "logged_out", "manual logout").catch(() => {});
  res.json({ ok: true, account_id: accountId });
});

const server = app.listen(PORT, HOST, async () => {
  logger.info(`TikTok web login service (assist-only) on :${PORT} (sessions: ${SESSIONS_DIR})`);
  try {
    const dirs = fs.readdirSync(SESSIONS_DIR, { withFileTypes: true }).filter((d) => d.isDirectory());
    if (dirs.length) {
      await fetch(`http://127.0.0.1:${PORT}/accounts/restore`, { method: "POST" }).catch(() => {});
    }
  } catch { /* ignore */ }
});

server.on("error", (e) => {
  if (e && e.code === "EADDRINUSE") {
    logger.warn(`port ${PORT} already in use — another instance is running; exiting`);
    process.exit(0);
  }
  logger.error({ e: String(e) }, "listen error");
  process.exit(1);
});
