/**
 * Instagram 个人号网页托管登录 + 收发微服务（Playwright 加载 instagram.com）。
 *
 * 与 Python 主进程（src/integrations/instagram_web_login.py）通过本地 HTTP 桥接，契约
 * **逐一对齐** services/messenger-web：
 *
 *   POST /login/start            -> { login_id, qr_image, status }   发起一次登录（弹官方登录页）
 *   GET  /login/:id/status       -> { status, account_id, qr_image, name, avatar_url, hint_code }
 *   POST /login/:id/cancel       -> { ok }                           取消该登录上下文
 *   POST /accounts/restore       -> { ok, restored }                 恢复磁盘已持久化的登录
 *   GET  /accounts               -> { accounts: [...] }              已登录账号（含 logged_in）
 *   POST /accounts/:id/send      -> { ok, message_id }               发私信（DOM 自动化）
 *   POST /accounts/:id/send-media-> { ok, message_id }               发媒体（图片/视频）
 *   POST /accounts/:id/logout    -> { ok, account_id }               登出并清 profile
 *   GET  /health                 -> { ok: true }
 *
 * status：pending | scanned | authorized | expired | failed（与 messenger 对齐，Python 侧归一）。
 * 每账号独立持久化 userDataDir（sessions/<login_id>/）→ cookie 持久化免重复登录。
 * account_id ＝ Instagram 的 ds_user_id cookie（稳定数字 id，不依赖 DOM）。
 *
 * 登录交互：默认 headed（IG_HEADLESS=0）——弹真实浏览器窗口，运营在本机窗口内完成官方
 * 登录（账密 / 2FA / checkpoint）；Playwright 只负责持久化 + 检测登录成功 + 收发。
 * restore / 恢复默认 headless（IG_RESTORE_HEADLESS=1）。
 *
 * ⚠️ 非官方接入：依赖 instagram.com 的 DOM，平台改版可能需微调选择器（见 SEL_* 常量集中区）。
 *    有 ToS / 风控风险，请配套一号一指纹一代理 + 养号，用小号。
 *
 * 运行：cd services/instagram-web && npm install && PORT=8793 node server.js
 */

import express from "express";
import pino from "pino";
import { fileURLToPath } from "url";
import path from "path";
import fs from "fs";
import { chromium } from "playwright";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const SESSIONS_DIR = process.env.IG_SESSIONS_DIR || path.join(__dirname, "sessions");
const PORT = Number(process.env.PORT || 8793);
const logger = pino({ level: process.env.LOG_LEVEL || "info" });

const HEADLESS = String(process.env.IG_HEADLESS ?? "0") === "1";
const RESTORE_HEADLESS = String(process.env.IG_RESTORE_HEADLESS ?? "1") === "1";
const BROWSER_CHANNEL = process.env.IG_BROWSER_CHANNEL || "chrome"; // 真 Chrome 优先；无则回落捆绑 chromium
const POLL_MS = Number(process.env.IG_POLL_MS || 8000); // 入站轮询间隔；0 关闭入站
const REAL_UA = process.env.IG_UA ||
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";

const PY_INGEST_URL = process.env.PY_INGEST_URL || "";
const PY_API_TOKEN = process.env.PY_API_TOKEN || "";
const PY_STATUS_URL =
  process.env.PY_STATUS_URL ||
  (PY_INGEST_URL ? PY_INGEST_URL.replace(/\/ingest\s*$/, "/session-status") : "");
const IG_SYNC = String(process.env.IG_SYNC ?? "1") !== "0";

// ── instagram.com 选择器/URL 集中区（平台改版时只改这里；真号联调核对）───────────
const URL_HOME = "https://www.instagram.com/";
const URL_LOGIN = "https://www.instagram.com/accounts/login/";
const URL_INBOX = "https://www.instagram.com/direct/inbox/";
const urlThread = (tid) => `https://www.instagram.com/direct/t/${encodeURIComponent(tid)}/`;
const SEL_LOGGED_IN = 'a[href="/direct/inbox/"], svg[aria-label="Home"], svg[aria-label="首页"]';
const SEL_COMPOSER = 'textarea[placeholder], div[contenteditable="true"][role="textbox"]';
const SEL_INBOX_THREAD = 'a[href^="/direct/t/"]';

fs.mkdirSync(SESSIONS_DIR, { recursive: true });

// login_id -> { status, qrImage, accountId, context, page, name, avatarUrl, hintCode }
const logins = new Map();
// account_id -> { context, page, loginId, loggedIn, name, avatarUrl }
const accounts = new Map();

const nowSec = () => Math.floor(Date.now() / 1000);
const genId = () => "ig_" + Math.random().toString(36).slice(2, 12);

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
    platform: "instagram",
    account_id: String(accountId || ""),
    status: String(status || ""),
    detail: String(detail || ""),
    name: String(extra.name || ""),
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

/** 起持久化上下文：优先真 Chrome 通道，机器没装 Chrome 才回落捆绑 Chromium。 */
async function launchPersistent(userDataDir, proxyUrl, headless) {
  try {
    return await chromium.launchPersistentContext(userDataDir, launchOptions(proxyUrl, BROWSER_CHANNEL, headless));
  } catch (e) {
    if (!BROWSER_CHANNEL || !isChannelUnavailable(e)) throw e;
    logger.warn({ e: String(e) }, "chrome channel unavailable → bundled chromium fallback");
    return await chromium.launchPersistentContext(userDataDir, launchOptions(proxyUrl, "", headless));
  }
}

/** 从 context cookie 读 ds_user_id（IG 的稳定账号数字 id）。 */
async function accountIdFromContext(context) {
  try {
    const cookies = await context.cookies("https://www.instagram.com");
    const c = (cookies || []).find((x) => x.name === "ds_user_id");
    return c ? String(c.value) : "";
  } catch {
    return "";
  }
}

/** 页面当前是否已登录：有 ds_user_id 且不在登录/challenge 页 + 出现主导航锚点。 */
async function pageLoggedIn(page, context) {
  try {
    const url = page.url() || "";
    if (/\/accounts\/login|\/challenge\//.test(url)) return false;
    const aid = await accountIdFromContext(context);
    if (!aid) return false;
    const anchor = await page.$(SEL_LOGGED_IN);
    return !!anchor;
  } catch {
    return false;
  }
}

/** 登录页此刻在要什么（实时提示码，非终态）。 */
async function loginHint(page) {
  const url = page.url() || "";
  if (/\/challenge\//.test(url)) return "checkpoint";
  if (/two_factor|2fa/i.test(url)) return "two_factor";
  return "";
}

async function fetchProfile(page, context) {
  const out = { name: "", avatarUrl: "" };
  try {
    // 用户名：ds_user_id 无法直接给昵称；从导航头像 alt / og:title 尽力取（best-effort）。
    const og = await page.$('meta[property="og:title"]');
    if (og) out.name = String((await og.getAttribute("content")) || "").replace(/ .*$/, "");
    const img = await page.$('nav img, header img');
    if (img) out.avatarUrl = String((await img.getAttribute("src")) || "");
  } catch {
    /* best-effort */
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

/** 把一条 IG 入站消息 push 到 Python 统一收件箱（best-effort）。 */
async function ingestMessage(accountId, payload) {
  if (!IG_SYNC || !PY_INGEST_URL) return;
  await postJson(PY_INGEST_URL, {
    platform: "instagram",
    account_id: String(accountId || ""),
    direction: "in",
    ...payload,
  });
}

// ── 入站轮询（首版：读 DM 收件箱线程列表 + 末条预览；逐条正文读取为后续增强）─────
const _lastPreview = new Map(); // `${accountId}:${threadId}` -> last preview text

async function pollInbox(accountId, rec) {
  if (!POLL_MS || !rec || !rec.loggedIn || !rec.page) return;
  try {
    const page = rec.page;
    await page.goto(URL_INBOX, { waitUntil: "domcontentloaded", timeout: 30000 });
    await page.waitForTimeout(1500);
    const rows = await page.$$eval(SEL_INBOX_THREAD, (els) =>
      els.slice(0, 20).map((a) => {
        const href = a.getAttribute("href") || "";
        const tid = (href.match(/\/direct\/t\/([^/]+)/) || [])[1] || "";
        const txt = (a.textContent || "").trim().replace(/\s+/g, " ");
        return { tid, txt };
      })
    );
    for (const r of rows) {
      if (!r.tid) continue;
      const key = `${accountId}:${r.tid}`;
      const prev = _lastPreview.get(key);
      if (prev === r.txt) continue; // 无变化不重复上报
      _lastPreview.set(key, r.txt);
      if (prev === undefined) continue; // 首见只建基线，不把历史当新消息惊动 AI
      // 首版只上报预览文本（线程名 + 末条截断）；逐条方向权威/全文读取为后续增强。
      await ingestMessage(accountId, {
        chat_key: r.tid,
        name: "",
        text: r.txt || "[新消息]",
        msg_id: `${r.tid}:${nowSec()}`,
        ts: nowSec(),
        chat_type: "",
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
    context,
    page,
    loginId: loginId || "",
    loggedIn: true,
    name: (profile && profile.name) || "",
    avatarUrl: (profile && profile.avatarUrl) || "",
  };
  accounts.set(String(accountId), rec);
  startPollLoop();
  return rec;
}

const app = express();
app.use(express.json({ limit: "8mb" }));

app.get("/health", (_req, res) => res.json({ ok: true, svc: "instagram-web" }));

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
    // 已登录（持久化 profile 命中）→ 直接完成
    if (await pageLoggedIn(page, context)) {
      await finalizeLogin(loginId, entry);
    } else {
      await page.goto(URL_LOGIN, { waitUntil: "domcontentloaded", timeout: 45000 }).catch(() => {});
      entry.qrImage = await screenshotDataUri(page);
      // 后台轮询检测登录成功
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
  const context = entry.context;
  const page = entry.page;
  const accountId = await accountIdFromContext(context);
  if (!accountId) return; // 还没真登录
  const profile = await fetchProfile(page, context);
  entry.accountId = accountId;
  entry.name = profile.name;
  entry.avatarUrl = profile.avatarUrl;
  entry.status = "authorized";
  attachAccount(accountId, context, page, loginId, profile);
  await postStatus(accountId, "authorized", "login ok", profile);
  logger.info({ accountId, loginId }, "instagram login authorized");
}

async function pollLogin(loginId, entry) {
  const deadline = Date.now() + 30 * 60 * 1000; // hosted 会话最长 30min（对齐 Python HOSTED_TTL）
  while (Date.now() < deadline) {
    const e = logins.get(loginId);
    if (!e || e.status === "authorized" || e.status === "failed") return;
    try {
      if (await pageLoggedIn(entry.page, entry.context)) {
        await finalizeLogin(loginId, entry);
        return;
      }
      e.hintCode = await loginHint(entry.page);
      e.qrImage = await screenshotDataUri(entry.page); // 实时预览刷新
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
    avatar_url: entry.avatarUrl || "",
    hint_code: entry.hintCode || "",
  });
});

app.post("/login/:id/cancel", async (req, res) => {
  const entry = logins.get(req.params.id);
  if (entry) {
    // 已授权的上下文交由 accounts 保活；仅未完成登录才关掉上下文。
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
    dirs = fs.readdirSync(SESSIONS_DIR, { withFileTypes: true })
      .filter((d) => d.isDirectory()).map((d) => d.name);
  } catch { dirs = []; }
  for (const loginId of dirs) {
    try {
      const userDataDir = path.join(SESSIONS_DIR, loginId);
      const context = await launchPersistent(userDataDir, "", RESTORE_HEADLESS);
      const page = context.pages()[0] || (await context.newPage());
      await page.goto(URL_HOME, { waitUntil: "domcontentloaded", timeout: 45000 }).catch(() => {});
      if (await pageLoggedIn(page, context)) {
        const accountId = await accountIdFromContext(context);
        if (accountId && !(accounts.has(accountId) && accounts.get(accountId).loggedIn)) {
          const profile = await fetchProfile(page, context);
          attachAccount(accountId, context, page, loginId, profile);
          await postStatus(accountId, "authorized", "restored", profile);
          restored += 1;
          continue;
        }
      }
      await context.close().catch(() => {}); // 未登录/重复 → 不占资源
    } catch (e) {
      logger.warn({ e: String(e), loginId }, "restore failed");
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
      name: rec.name || "",
      avatar_url: rec.avatarUrl || "",
    });
  }
  res.json({ accounts: out });
});

async function openThreadComposer(rec, threadId) {
  const page = rec.page;
  await page.goto(urlThread(threadId), { waitUntil: "domcontentloaded", timeout: 30000 });
  await page.waitForSelector(SEL_COMPOSER, { timeout: 15000 });
  return page.$(SEL_COMPOSER);
}

app.post("/accounts/:id/send", async (req, res) => {
  const rec = accounts.get(req.params.id);
  if (!rec || !rec.page) return res.status(404).json({ ok: false, error: "account not connected" });
  const threadId = String((req.body && req.body.thread_id) || "");
  const text = String((req.body && req.body.text) || "");
  if (!threadId) return res.status(400).json({ ok: false, error: "thread_id required" });
  try {
    const box = await openThreadComposer(rec, threadId);
    if (!box) throw new Error("composer not found");
    await box.click();
    await box.fill(text).catch(async () => { await box.type(text); });
    await rec.page.keyboard.press("Enter");
    await rec.page.waitForTimeout(800);
    res.json({ ok: true, delivered: true, message_id: `${threadId}:${nowSec()}` });
  } catch (e) {
    logger.warn({ e: String(e), id: req.params.id }, "send failed");
    res.status(502).json({ ok: false, delivered: false, error: String(e) });
  }
});

app.post("/accounts/:id/send-media", async (req, res) => {
  const rec = accounts.get(req.params.id);
  if (!rec || !rec.page) return res.status(404).json({ ok: false, error: "account not connected" });
  const threadId = String((req.body && req.body.thread_id) || "");
  const mediaPath = String((req.body && req.body.media_path) || "");
  const caption = String((req.body && req.body.caption) || "");
  if (!threadId) return res.status(400).json({ ok: false, error: "thread_id required" });
  if (!mediaPath || !fs.existsSync(mediaPath)) return res.status(400).json({ ok: false, error: "media_path missing" });
  try {
    const page = rec.page;
    await page.goto(urlThread(threadId), { waitUntil: "domcontentloaded", timeout: 30000 });
    // IG DM 的图片/视频经隐藏 <input type="file"> 上传（联调核对该选择器）。
    const fileInput = await page.$('input[type="file"]');
    if (!fileInput) throw new Error("file input not found");
    await fileInput.setInputFiles(mediaPath);
    await page.waitForTimeout(1500);
    if (caption) {
      const box = await page.$(SEL_COMPOSER);
      if (box) { await box.click(); await box.type(caption); }
    }
    await page.keyboard.press("Enter");
    await page.waitForTimeout(1200);
    res.json({ ok: true, delivered: true, message_id: `${threadId}:${nowSec()}` });
  } catch (e) {
    logger.warn({ e: String(e), id: req.params.id }, "send-media failed");
    res.status(502).json({ ok: false, delivered: false, error: String(e) });
  }
});

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

const server = app.listen(PORT, async () => {
  logger.info(`Instagram web login service on :${PORT} (sessions: ${SESSIONS_DIR})`);
  try {
    const dirs = fs.readdirSync(SESSIONS_DIR, { withFileTypes: true }).filter((d) => d.isDirectory());
    if (dirs.length) {
      await fetch(`http://127.0.0.1:${PORT}/accounts/restore`, { method: "POST" }).catch(() => {});
    }
  } catch { /* ignore */ }
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
