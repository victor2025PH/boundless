/**
 * Messenger 网页模式（web mode）登录 + 收发微服务。
 *
 * 竞品「支持一堆 app」的真相：内嵌隔离浏览器加载平台**官方网页版**，一号一隔离 profile，
 * 网页里登录（扫码/账密/2FA 都在官方页完成），再用 DOM/网络层读消息、填输入框点发送。
 * 本服务即 Messenger 的这条路——用 Playwright 驱动一个持久化 Chromium 上下文加载
 * https://www.messenger.com/ ，功能对齐官方网页版；与 Python 主进程
 * （src/integrations/messenger_web_login.py）通过本地 HTTP 桥接，契约对齐 whatsapp-baileys。
 *
 *   POST /login/start            -> { login_id, qr_image, status }   发起一次登录（弹出登录页）
 *   GET  /login/:id/status       -> { status, account_id, qr_image } 轮询登录状态（qr_image=登录页截图）
 *   POST /login/:id/cancel       -> { ok }                           取消/关闭该登录上下文
 *   POST /accounts/restore       -> { ok, restored }                 恢复磁盘已持久化的登录
 *   GET  /accounts               -> { accounts: [...] }              已登录账号
 *   POST /accounts/:id/send      -> { ok, message_id }               发消息（DOM 自动化）
 *   POST /accounts/:id/logout    -> { ok, account_id }               登出并清 profile 目录
 *   GET  /health                 -> { ok: true }
 *
 * status 取值：pending | scanned | authorized | expired | failed（与 baileys 对齐，Python 侧统一归一）
 * 每个账号用独立的持久化 userDataDir（sessions/<login_id>/），cookie 持久化 → 免重复登录。
 *
 * 运行：
 *   cd services/messenger-web && npm install && PORT=8791 node server.js
 *   （npm install 会经 postinstall 自动 playwright install chromium）
 *
 * 登录交互：默认 headed（MSG_HEADLESS=0）——弹出真实浏览器窗口，运营在本机窗口内完成
 * 官方登录（扫码 / 账密 / 2FA 都可），Playwright 只负责「持久化 + 检测登录成功 + 收发」。
 * restore / 开机恢复 / 崩溃自愈已授权会话默认 headless（MSG_RESTORE_HEADLESS=1）——
 * 稳态零可见窗口，仅新增账号时临时弹交互窗（关掉它也会以无头自动回来）。
 *
 * 注意：网页自动化依赖 messenger.com 的 DOM 结构，平台改版可能需要微调选择器
 * （见 SEL_* 常量集中区）。存在 ToS / 风控风险，请配套一号一指纹一代理 + 养号。
 */

import express from "express";
import pino from "pino";
import { fileURLToPath } from "url";
import path from "path";
import fs from "fs";
import { chromium } from "playwright";
import { classifyLoginPage, actionableCode, checkpointFlavor, STAGE, isE2eePinText, isTemporarilyBlockedText } from "./login_classify.js";
import { relayStepFromStage, sanitizeSubmit, fillPlanFor, RELAY_STEP } from "./login_relay.js";
import { resolveLaunch, shouldAutoHideAfterLogin } from "./login_window.js";
import {
  synthMsgId, parseReactionFromAria, isUnsentPreview, isUnsentTombstone,
  classifyInboxHint, resolveInboxHint, pinHealBump, adaptiveReqEvery, canOpenThread, normalizePin, autoPinGate,
  warmBackfillBatch, sendFastPathEligible, composerTextMatches,
  parseMsgAria, threadReadSample,
  normalizeRequestAction, REQUEST_ACTION_LABELS,
  matchQuotedTarget, msgrPaletteTarget, reactAriaCandidates,
  pickUnreadForced, classifyRequestsScan, sentRingPush, sentRingHit, echoTextHit,
  classifyComposerBlock, pickManualOutMirror,
  inferGroupFromInboundSenders, updateSenderRoster,
  sendFailureBackoffMs, manualProbeDecision,
  ariaDatetimeToEpoch,
} from "./msg_ops.js";
import {
  parseCurrentUserInitialData, pickSelfAvatar, summarizeCandidates,
  nextSelfProfileRecaptureMs,
} from "./self_profile.js";
import {
  isDetachedError, isCallOverlayText, normalizeSendFailCode, sendFailBody, sendFailHttpStatus,
  bumpJidFailStreak, shouldQueueRetry, retryQueueUpsert, retryQueueDue, retryQueueSettle,
  RETRY_QUEUE_INTERVAL_MS, RETRY_QUEUE_MAX_TRIES, REACTION_ARIA_RE, parseReactionPill,
  inputWithRebind,
} from "./send_chain.js";
import crypto from "crypto";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// ── worker 代码版本指纹（P0 2026-08-15，「半部署」盲区解药）─────────────────────
// 实锤：server.js 的 E2EE PIN 自愈修复 08-15 00:17 已落盘，但在跑进程是 08-14 08:27
// 启动的——磁盘代码 ≠ 在跑代码，且没有任何观测面能看见这个分叉（Python 实例重启
// 与本 Node worker 重启是两个独立动作，极易只做前者）。/health 与 /accounts 现在
// 自报 boot_ts + 代码指纹 + code_stale（=磁盘内容指纹 ≠ 启动时指纹），看板/巡检
// 一眼可判「写好的修复到底上没上」。任何异常回落安全值——观测面绝不影响业务。
// 2026-08-31 判据升级 mtime → 内容对比：git 工作树操作/编辑器全存盘会把文件按原样
// 重写（mtime 变、内容没变），mtime 判据一旦触发就钉死 true 直到重启，watchdog 每
// 4h 空喊一次「代码分叉」（当日实锤：内容与在跑逐字节一致仍告警）。现在 stat 签名
// （mtime+size）只作缓存失效键：文件没动零哈希开销；动过才重算指纹与启动基线比——
// touch 不再误报，真改照样抓，半写态瞬时差异随下次保存自愈。
const WORKER_BOOT_TS = Date.now();
// 指纹集合必须覆盖**全部**运行时装载的自家模块（P4 实锤：首版只含 server.js+
// msg_ops.js，login_classify.js 的修复上线后指纹纹丝不动——半部署探测器自己
// 留了半部署盲区）。新增模块文件时同步补这里。
const _CORE_FILES = [
  fileURLToPath(import.meta.url),
  ...["msg_ops.js", "login_classify.js", "login_relay.js", "login_window.js", "self_profile.js"]
    .map((f) => path.join(path.dirname(fileURLToPath(import.meta.url)), f)),
];
function _hashCoreFiles() {
  const h = crypto.createHash("sha1");
  for (const f of _CORE_FILES) h.update(fs.readFileSync(f));
  return h.digest("hex").slice(0, 12);
}
const WORKER_CODE_FP = (() => {
  try { return _hashCoreFiles(); } catch (_) { return ""; }
})();
// 磁盘指纹缓存：stat 签名没变直接复用上次结果（每次调用只 stat 6 个文件，微秒级）；
// 重算失败不写缓存＝下次自动重试，瞬时 IO 错不会把结论钉死。
let _diskFp = { sig: null, fp: WORKER_CODE_FP };
function workerCodeInfo() {
  let stale = false;
  try {
    if (WORKER_CODE_FP) { // 启动基线都没算出来就没有可比性，保持 false
      const sig = _CORE_FILES
        .map((f) => { const s = fs.statSync(f); return s.mtimeMs + ":" + s.size; })
        .join("|");
      if (sig !== _diskFp.sig) _diskFp = { sig, fp: _hashCoreFiles() };
      stale = _diskFp.fp !== WORKER_CODE_FP;
    }
  } catch (_) { stale = false; }
  return {
    boot_ts: Math.floor(WORKER_BOOT_TS / 1000),
    code_fp: WORKER_CODE_FP,
    code_stale: stale,
  };
}
const SESSIONS_DIR = process.env.MSG_SESSIONS_DIR || path.join(__dirname, "sessions");
const PORT = Number(process.env.PORT || 8791);
// 只绑回环：入站路由无鉴权中间件，绑 0.0.0.0 等于把账号操作面开给整个局域网。
// 真实调用方只有本机 Python 引擎，全仓无远程引用。确需跨机时用 BIND_HOST 覆盖，
// 但**必须先给入站加鉴权**再放开。
const HOST = String(process.env.BIND_HOST || '127.0.0.1');
const logger = pino({ level: process.env.LOG_LEVEL || "info" });

// ── 进程遗言钩子（实施72 排查 2026-08-27 加，纯日志零行为变更）────────────────
// 当日实锤：03:42-05:29 六只 node 进程**静默死亡**（日志里最后一行都是正常业务行，
// 无任何堆栈/退出痕迹），死因至今不可归因——stdout/stderr 均已被启动器 2>&1 落盘，
// 说明进程要么被外部强杀（taskkill /F 不给遗言机会），要么 exit 前没有任何输出。
// 这里给「还来得及说话」的每一种死法都留一行：下一次成簇死亡时，「有遗言=进程内
// 因（含具体异常）；无遗言=外部强杀」这条二分法直接把排查范围砍半。
// 刻意不改变行为：uncaughtException/unhandledRejection 记录后仍按 node 默认语义
// 退出（记完 flush 由 pino 同步 stdout 保证）——吞异常保活会把半死态变成新常态。
process.on("exit", (code) => {
  try { logger.warn({ code }, "process exit"); } catch (_) {}
});
for (const sig of ["SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"]) {
  try {
    process.on(sig, () => {
      try { logger.warn({ sig }, "signal received, exiting"); } catch (_) {}
      process.exit(128);
    });
  } catch (_) { /* 平台不支持该信号则跳过 */ }
}
process.on("uncaughtException", (err) => {
  try { logger.error({ err: String(err && err.stack || err) }, "uncaughtException (exiting)"); } catch (_) {}
  process.exit(1);
});
process.on("unhandledRejection", (reason) => {
  try { logger.error({ reason: String(reason && reason.stack || reason) }, "unhandledRejection (exiting)"); } catch (_) {}
  process.exit(1);
});

// 交互登录时是否隐藏浏览器窗口。默认 headed（"0"）：运营需在弹窗里完成官方登录
// （扫码 / 账密 / 2FA）。这条只管**新发起的交互登录**。
const HEADLESS = String(process.env.MSG_HEADLESS ?? "0") === "1";
// restore / 开机恢复 / 崩溃自愈**已授权**会话时是否无头。默认 "1"（无头后台保活）：
// 这些会话已持久化 cookie、无需人工介入，headed 只会白弹窗口。与交互登录正交——
// 稳态（重启后 restoreAll）0 可见窗口，仅「新增账号」时临时弹一个交互窗；交互登录窗
// 被运营关掉后，scheduleRecovery 走 isRestore=true 也会以无头自动回来。多账号规模化
// 的窗口治理主要靠这条（10 号从 10 个可见窗口 → 0）。置 "0" 恢复「restore 也 headed」旧行为。
const RESTORE_HEADLESS = String(process.env.MSG_RESTORE_HEADLESS ?? "1") === "1";
// B65（2026-08-24 老板拍板）：headed 登录窗在**授权成功后自动无头回归**。登录前窗口必须
// 可见可操作（账密/2FA/checkpoint 都在真窗口里完成，配合登录看门狗的 stage 前置顶）；
// 授权后窗口的使命结束——继续可见就是「三个页面来回刷新」的木偶秀（轮询 readPage/reqPage
// 每 4s 导航），用户会去点/关它反而干扰自动化。做法＝复用崩溃自愈同一条恢复链：优雅关闭
// headed context → startLogin(isRestore=true) 按 RESTORE_HEADLESS 无头回归（profile 持久化
// + cookie 已落盘，通常 2-4s 内免扫重连）。置 "0" 回到「窗口留到人手关」旧行为。
const HIDE_AFTER_LOGIN = String(process.env.MSG_HIDE_AFTER_LOGIN ?? "1") === "1";
// 授权到隐藏之间的缓冲：让人看清「登录成功 + 养号提示」横幅，也让 saveCookies/身份采集
// 落定。下限 3s 防手抖配置把成功横幅闪没。
const HIDE_AFTER_LOGIN_DELAY_MS = Math.max(
  3000, Number(process.env.MSG_HIDE_AFTER_LOGIN_DELAY_MS || 12000));
// Q-24 E（#298）：授权后 PIN 浮层在场时窗口最多再露多久（人可就地输 PIN），到点强制隐藏。
const HIDE_PIN_GRACE_MS = Math.max(15000, Number(process.env.MSG_HIDE_PIN_GRACE_MS || 120000));
// Q-24 E：Chromium 沙箱默认开（Playwright 默认 chromiumSandbox:false 会加 --no-sandbox →
// Chrome 顶栏黄条「--no-sandbox 不受支持」，TKV3HB 实锤）。老机器沙箱起不来时 MSG_NO_SANDBOX=1 回旧行为。
const CHROMIUM_SANDBOX = String(process.env.MSG_NO_SANDBOX || "") !== "1";
// B65：登录期「需要人操作」的分段——看门狗观测到进入这些 stage 时把 headed 窗口
// 前置一次（每个 stage 只前置一次，防 1.5s tick 反复夺焦点把整台机器搞到没法用）。
const HUMAN_ACTION_STAGES = new Set([
  STAGE.LOGIN_FORM, STAGE.TWO_FACTOR, STAGE.CHECKPOINT,
  STAGE.E2EE_PIN, STAGE.PASSWORD_ERROR, STAGE.ACCOUNT_LOCKED,
]);
// 页面回收（省内存，规模化关键）：读会话 readPage / 读请求箱 reqPage 用完即弃，下次用到
// 经既有 isClosed() 守卫懒重建。稳态每账号只常驻 entry.page（列表轮询）1 个 renderer——
// 无头解决了「窗口」，这条解决「页面 renderer 随账号数线性涨」（10 号从 ~30 页 → ~10 页）。
// reqPage 每轮 reqDue 用完即关；readPage 仅当本轮真读过线程才保留（活跃账号不抖动）。
// 所有 readPage/reqPage 使用点均有 isClosed() 重建守卫，回收在 finally（await 全完成后）
// 且先置空引用再后台关闭，绝不打断在途读取。默认开；"0" 关（读页常驻旧行为，调试用）。
const PAGE_RECLAIM = String(process.env.MSG_PAGE_RECLAIM ?? "1") === "1";
// 入站轮询间隔（毫秒）；0 关闭入站同步（仅登录+发送）。
const POLL_MS = Number(process.env.MSG_POLL_MS || 4000);
// 首连历史回填：登录/恢复后对最近 N 个会话各回填末尾若干条历史（0 关闭）。
// P1 之前这是个从未被使用的死配置；现在真实接线——baseline 建立后逐条（每 tick 1 条）
// readThreadTail 读末尾消息推给 Python /thread-history（空会话幂等，历史不惊动 AI）。
const MSG_BACKFILL = Number(process.env.MSG_BACKFILL || 20);
// 预热提速：重启/首连后回填期，一个 poll tick 串行读几条线程（非并发，不制造风控尖峰），
// 把「E2EE 会话逐条解密」的稀疏窗口从 ~N×POLL 压到 ~N/batch×POLL。默认 3；设 1 = 逐字节旧
// 行为（每 tick 1 条）。条间小憩 GAP_MS 防速限。_polling 再入闸保证 tick 变长不与下一 tick 重叠。
const BACKFILL_WARM_BATCH = Number(process.env.MSG_BACKFILL_WARM_BATCH || 3);
const BACKFILL_WARM_GAP_MS = Math.max(0, Number(process.env.MSG_BACKFILL_WARM_GAP_MS || 600));
// 是否轮询「消息请求」文件夹（陌生人首次来讯落这里；默认开）。
const MSG_REQUESTS = String(process.env.MSG_REQUESTS ?? "1") !== "0";
// 「进线程读正文」权威模式（默认开）：列表预览仅当变更探测器；一旦某会话有变更，进线程按
// 每条消息的无障碍标签（消息由<发送者>发送于<时间>：<正文>）读到**方向权威 + 全文 + 真实发送者**，
// 据此判定「最后一条是否对端新消息」再上报。彻底解决①预览截断②E2EE 预览不可读③方向误判自回复。
// 关闭（=0）则回落旧的「列表预览直报」（带前缀/自回声/状态名护栏）。
const MSG_READ_THREAD = String(process.env.MSG_READ_THREAD ?? "1") !== "0";
// 每轮最多进线程读取的会话数（限流，避免大量导航像 bot / 触发风控）。
const MSG_MAX_OPENS = Number(process.env.MSG_MAX_OPENS || 5);
// 未读驱动强制读取（P0 2026-08-15）：预览指纹不变但有未读证据（行级未读标记 /
// 全局未读+新鲜加密占位行）→ 强制进线程读一次。解「E2EE 占位预览永不变化 → 客户
// 新消息隐形丢失」盲区（当晚实锤丢 2 条），顺带覆盖「客户逐字重发同一句」盲区；
// 打开线程同时给 E2EE PIN 就地自愈一次机会。强制候选排在常规候选之后、共享
// MSG_MAX_OPENS 总预算（总导航量不增），另有单轮上限与每线程冷却。"0" 关。
const MSG_UNREAD_FORCE = String(process.env.MSG_UNREAD_FORCE ?? "1") !== "0";
// 同一线程两次强制读取的最小间隔（默认 10min——密钥未恢复的 E2EE 线程读不出正文，
// 不设冷却会每 4s 反复导航=风控敞口；每条新客户消息重新点亮未读，最多一个冷却窗后重试）。
const MSG_UNREAD_FORCE_COOLDOWN_MS = Math.max(
  60 * 1000, Number(process.env.MSG_UNREAD_FORCE_COOLDOWN_MS || 10 * 60 * 1000));
// 单轮最多强制读取条数（叠加在 MSG_MAX_OPENS 预算内）。
const MSG_UNREAD_FORCE_CAP = Math.max(1, Number(process.env.MSG_UNREAD_FORCE_CAP || 2));
// fresh_placeholder 级（弱证据兜底）认定「行相对时间新鲜」的窗口。
const MSG_UNREAD_FRESH_MS = Math.max(
  60 * 1000, Number(process.env.MSG_UNREAD_FRESH_MS || 30 * 60 * 1000));
// 手机/原生页手发出站实时回流（2026-08-16）：预览变成 "你:/You:" 且**非本 worker
// 自发**（自发经 isSelfEcho/镜像环拦下）→ 进线程镜像该出站进统一收件箱。旧行为
// 是直接跳过——运营在手机 App 里手发的消息要等客户回复触发下次读线程才回流，
// 首条更是被水位线基线吞掉＝坐席端永久隐形（实录：老板手机聊单坐席全程看不见）。
// "0" 关（回旧行为）。
const MSG_MANUAL_OUT_SYNC = String(process.env.MSG_MANUAL_OUT_SYNC ?? "1") !== "0";
// 单轮最多为「镜像手发出站」进线程的条数（追加在候选队尾＝优先级最低，仍受
// MSG_MAX_OPENS 总预算约束——真实客户入站永远先行）。
const MSG_MANUAL_OUT_CAP = Math.max(1, Number(process.env.MSG_MANUAL_OUT_CAP || 2));
// 消息请求（陌生人首讯）是否也进线程读全文（默认开）。真号联调确认「打开≠接受」——仅导航读取
// 不会接受/移出请求箱；读到全文 → 人设化首复更准；读不到（E2EE 不可读）→ 回落列表预览逻辑
// （占位=加密横幅非真消息，仍按旧行为跳过，不入库脏数据）。
const MSG_READ_REQUESTS = String(process.env.MSG_READ_REQUESTS ?? "1") !== "0";
// 「消息请求」文件夹降载 + 风控退避 -------------------------------------------------
// 每 N 个基础 tick 才拉一次请求文件夹（此前硬编码 5≈20s；默认调大到 15≈60s，降低对 /requests/
// 的访问频率——FB 会把高频访问该功能判为「过度使用此功能」并临时封禁）。
const MSG_REQ_EVERY = Math.max(1, Number(process.env.MSG_REQ_EVERY || 15));
// 每轮最多进线程读取的「请求」会话数（独立于主收件箱 MSG_MAX_OPENS，默认更低——请求区更敏感）。
// 置 0 → 不进请求线程读全文，仅用列表预览（不等于关闭请求同步，仍会入库预览）。
const MSG_MAX_REQ_OPENS = Math.max(0, Number(process.env.MSG_MAX_REQ_OPENS || 2));
// 检测到 /requests/ 被临时封禁（「你暂时被禁止使用此功能」/ "temporarily blocked"）时的退避冷却：
// 基础时长（默认 30min），连续命中每次翻倍直到上限（默认 6h）；冷却窗口内完全不导航 /requests/，
// 冷却结束自动恢复。
const MSG_REQ_BLOCK_COOLDOWN_MS = Math.max(0, Number(process.env.MSG_REQ_BLOCK_COOLDOWN_MS || 30 * 60 * 1000));
const MSG_REQ_BLOCK_MAX_MS = Math.max(
  MSG_REQ_BLOCK_COOLDOWN_MS,
  Number(process.env.MSG_REQ_BLOCK_MAX_MS || 6 * 60 * 60 * 1000)
);
// 入站媒体落地目录（对齐 whatsapp-baileys 的 WA_MEDIA_DIR 模式）：进线程读到媒体气泡时，
// 用浏览器会话下载并写入 Python 静态目录（同机共享），前端按 /static URL 加载。未配置则不下载
// 媒体（回落占位文本 [图片]/[视频]…，行为退回纯文本）。默认指向 messenger 静态子目录。
const MSG_MEDIA_DIR = process.env.MSG_MEDIA_DIR || "";
const MSG_MEDIA_URL_BASE = (
  process.env.MSG_MEDIA_URL_BASE || "/static/protocol_media/messenger"
).replace(/\/+$/, "");

fs.mkdirSync(SESSIONS_DIR, { recursive: true });

// Python 主进程统一收件箱入站桥（可选；未配置则不上报）。
const PY_INGEST_URL = process.env.PY_INGEST_URL || "";
const PY_API_TOKEN = process.env.PY_API_TOKEN || "";
const MSG_SYNC = String(process.env.MSG_SYNC ?? "1") !== "0";
// P2：表情 / 撤回上报（对齐 WA Baileys → 既有 Python /reaction /message-op 桥）。
// Messenger DOM 无 wamid——依赖 synthMsgId 在 ingest 时写入稳定 msg_id，否则挂不上。
const PY_REACTION_URL = process.env.PY_REACTION_URL
  || (PY_INGEST_URL ? PY_INGEST_URL.replace(/\/ingest\s*$/, "/reaction") : "");
const PY_MSGOP_URL = process.env.PY_MSGOP_URL
  || (PY_INGEST_URL ? PY_INGEST_URL.replace(/\/ingest\s*$/, "/message-op") : "");
const MSG_SYNC_REACTIONS = String(process.env.MSG_SYNC_REACTIONS ?? "1") !== "0";
const MSG_SYNC_EDITS = String(process.env.MSG_SYNC_EDITS ?? "1") !== "0";

const MESSENGER_URL = process.env.MSG_BASE_URL || "https://www.messenger.com/";

/** login_id -> { context, page, status, qrImage, accountId, name, avatarUrl,
 *               createdAt, userDataDir, proxyUrl, seen(Map thread->lastMsgId), pollTimer } */
const sessions = new Map();

function newLoginId() {
  return "msg_" + Math.random().toString(36).slice(2, 10);
}

/** 通用 best-effort JSON POST（带鉴权头；失败只记 debug，绝不抛）。 */
async function postJson(url, payload) {
  if (!url) return;
  try {
    const headers = { "Content-Type": "application/json" };
    if (PY_API_TOKEN) headers["Authorization"] = `Bearer ${PY_API_TOKEN}`;
    await fetch(url, { method: "POST", headers, body: JSON.stringify(payload) });
  } catch (e) {
    logger.debug({ e, url }, "postJson failed");
  }
}

/** 同 postJson 但回报是否 2xx（历史回填等需要「失败重排队」语义的调用方用）。绝不抛。 */
async function postJsonOk(url, payload) {
  if (!url) return false;
  try {
    const headers = { "Content-Type": "application/json" };
    if (PY_API_TOKEN) headers["Authorization"] = `Bearer ${PY_API_TOKEN}`;
    const res = await fetch(url, {
      method: "POST", headers, body: JSON.stringify(payload) });
    return !!(res && res.ok);
  } catch (e) {
    logger.debug({ e, url }, "postJsonOk failed");
    return false;
  }
}

// 联调用：最近入站检测环形缓冲（不依赖主程序即可核验轮询是否抓到新消息）。
const RECENT_INBOUND = [];
function recordInbound(payload) {
  RECENT_INBOUND.push({ ...payload, detected_at: new Date().toISOString() });
  while (RECENT_INBOUND.length > 40) RECENT_INBOUND.shift();
}

async function postIngest(payload) {
  recordInbound(payload);
  await postJson(PY_INGEST_URL, payload);
}

/** P2：上报一条表情回应（挂到目标 synth msg_id）。best-effort。 */
async function postReaction(entry, r) {
  if (!MSG_SYNC_REACTIONS || !PY_REACTION_URL || !entry || !entry.accountId || !r) return;
  const chatKey = String(r.chat_key || "");
  const targetId = String(r.target_id || "");
  if (!chatKey || !targetId) return;
  await postJson(PY_REACTION_URL, {
    platform: "messenger",
    account_id: entry.accountId,
    chat_key: chatKey,
    target_id: targetId,
    emoji: String(r.emoji || ""),
    sender: r.sender === "me" ? "me" : String(r.sender || "peer"),
  });
}

/** P2：上报撤回/编辑（op=revoke|edit）。best-effort。 */
async function postMessageOp(entry, info) {
  if (!MSG_SYNC_EDITS || !PY_MSGOP_URL || !entry || !entry.accountId || !info) return;
  const chatKey = String(info.chat_key || "");
  const targetId = String(info.target_id || "");
  const op = String(info.op || "").toLowerCase();
  if (!chatKey || !targetId || (op !== "revoke" && op !== "edit")) return;
  await postJson(PY_MSGOP_URL, {
    platform: "messenger",
    account_id: entry.accountId,
    chat_key: chatKey,
    target_id: targetId,
    op,
    text: String(info.text || ""),
  });
}

/** 给消息打稳定 msg_id，并记下最近入/出站 id（供撤回挂载）。 */
function stampMsgId(entry, chatKey, m) {
  if (!m) return "";
  const mid = m.msg_id || synthMsgId({
    chatKey, direction: m.direction, tsLabel: m.ts, text: m.text, mediaRef: m.media_ref,
  });
  m.msg_id = mid;
  if (!entry) return mid;
  if (!entry._lastInboundId) entry._lastInboundId = new Map();
  if (!entry._lastOutboundId) entry._lastOutboundId = new Map();
  if (!entry._reactionSeen) entry._reactionSeen = new Set();
  if (m.direction === "in") entry._lastInboundId.set(String(chatKey), mid);
  else if (m.direction === "out") entry._lastOutboundId.set(String(chatKey), mid);
  return mid;
}

/**
 * P2：从已读到的线程尾处理表情 + 撤回墓碑。
 * 副作用清单：本函数**不再次导航**——只消费 readThreadTail 已取到的 DOM 快照。
 */
async function processThreadOps(entry, chatKey, tail) {
  if (!entry || !chatKey || !Array.isArray(tail) || !tail.length) return;
  for (const m of tail) {
    stampMsgId(entry, chatKey, m);
    // 撤回墓碑：尽量挂到该方向最近一条真实消息 id（墓碑本身没有可 ingest 正文）
    if (isUnsentTombstone(m.text)) {
      const map = m.direction === "out" ? entry._lastOutboundId : entry._lastInboundId;
      const target = map && map.get(String(chatKey));
      if (target) {
        await postMessageOp(entry, { chat_key: chatKey, target_id: target, op: "revoke" });
      }
      continue;
    }
    if (Array.isArray(m.reactions)) {
      for (const r of m.reactions) {
        if (!r || !r.emoji) continue;
        const dedupe = `${chatKey}|${m.msg_id}|${r.sender}|${r.emoji}`;
        if (entry._reactionSeen.has(dedupe)) continue;
        entry._reactionSeen.add(dedupe);
        // 有界：防长期运行撑爆（约 2k 条指纹）
        if (entry._reactionSeen.size > 2000) {
          entry._reactionSeen = new Set(Array.from(entry._reactionSeen).slice(-1000));
        }
        await postReaction(entry, {
          chat_key: chatKey, target_id: m.msg_id, emoji: r.emoji, sender: r.sender,
        });
      }
    }
  }
}

// 会话健康事件上报（P0-2 闭环）：登录/掉线/放弃自愈等关键转移主动 push 给 Python，
// 不再只靠 Python 侧轮询 /accounts 才后知后觉。URL 默认由 PY_INGEST_URL 推导
// （…/ingest → …/session-status），也可用 PY_STATUS_URL 显式覆盖。best-effort 不抛。
const PY_STATUS_URL = process.env.PY_STATUS_URL
  || (PY_INGEST_URL ? PY_INGEST_URL.replace(/\/ingest\s*$/, "/session-status") : "");
// 入站健康心跳桥（P0 2026-08-04「登录态在、消息读不到」半死态解药）：周期 push
// {侧栏未读数 + 进线程读取滚动窗成败 + 最近成功入站时刻}。Python 侧据「有未读却读取持续
// 全失败」升级告警（此前 cookie 快照恢复登录但 E2EE 密钥没恢复 → 消息永久卡 Loading，
// 轮询一路健康、坐席看未读零入站，掉进去 4 天无人知）。URL 默认由 PY_INGEST_URL 推导。
const PY_INBOX_HEALTH_URL = process.env.PY_INBOX_HEALTH_URL
  || (PY_INGEST_URL ? PY_INGEST_URL.replace(/\/ingest\s*$/, "/inbox-health") : "");
// 每 N 个基础 tick push 一次入站健康（默认 15≈60s；心跳性质，不必每轮）。
const MSG_INBOX_HEALTH_EVERY = Math.max(1, Number(process.env.MSG_INBOX_HEALTH_EVERY || 15));
// 进线程读取成败滚动窗大小：判「持续全失败」的样本深度（偶发导航超时被稀释）。
const READ_WIN_MAX = Math.max(2, Number(process.env.MSG_READ_WIN || 8));
// ── P1 目录同步 / 首连历史回填（对齐 Telegram「接入即见全量会话+上下文」）────────
// 会话列表 → Python 会话占位（/api/internal/protocol/chats，WhatsApp Baileys 同款链路）。
// 每 4s 轮询本就读到整个侧栏（key/名字/头像/预览），此前这些数据用完即弃——推给平台后
// 「接入 1 分钟内看到全部会话、可主动发起」。URL 由 PY_INGEST_URL 推导。
const PY_CHATS_URL = process.env.PY_CHATS_URL
  || (PY_INGEST_URL ? PY_INGEST_URL.replace(/\/ingest\s*$/, "/chats") : "");
// 首连历史回填落点（空会话幂等 + 「历史不惊动 AI」语义在 Python 侧保证）。
const PY_THREAD_HISTORY_URL = process.env.PY_THREAD_HISTORY_URL
  || (PY_INGEST_URL ? PY_INGEST_URL.replace(/\/ingest\s*$/, "/thread-history") : "");
// 目录同步节流：首轮全量推送，之后每 N 个 tick（默认 75≈5min）且内容指纹变化才推
//（哈希去重防写放大——store upsert 便宜但没必要每 4s 刷一遍）。
const MSG_DIR_SYNC_EVERY = Math.max(1, Number(process.env.MSG_DIR_SYNC_EVERY || 75));
// 首连回填并发节流：每个 poll tick 只回填 1 条会话（top-N 20 条 ≈ 80s 内完成），
// 导航节奏与正常轮询一致，不制造风控尖峰。MSG_BACKFILL（上限条数）在上方声明。
// ── P1 合并注记（两条并行线 2026-08-05 00:40 在本文件撞车后的合流，勿再重复实现）──
// 目录同步 / 首连回填 / burst 已由上方常量 + postDirectory/backfillStep + pollInbound
// 接线完整落地（PY_CHATS_URL / PY_THREAD_HISTORY_URL / MSG_DIR_SYNC_EVERY）。
// 本块保留另一线的三个增量配置并已接进同一实现：
// - MSG_DIR_SYNC_MIN_MS：目录推送防抖（postDirectory 内生效，与指纹去重叠加）；
// - MSG_BURST_MAX：burst 单轮上限（主列表候选循环内生效）；
// - MSG_PROBE_EVERY：**刻意未接线**——「主动读会话补样本」会开未读线程（FB 侧标已读
//   = 对客户「已读不回」）；半死态的零样本盲区已由零副作用的「E2EE 占位预览占比」
//   信号覆盖（postInboxHealth 的 e2ee_ratio → Python 第二支判定），无需导航探针。
const MSG_DIR_SYNC_MIN_MS = Math.max(5000, Number(process.env.MSG_DIR_SYNC_MIN_MS || 30000));
// burst 上报上限：一次变更最多补报的对端新消息条数（防刷屏；超出取最新的）。
const MSG_BURST_MAX = Math.max(1, Number(process.env.MSG_BURST_MAX || 6));
// （见上方合并注记：探针刻意不接线，保留 env 位仅为兼容）
const MSG_PROBE_EVERY = Math.max(0, Number(process.env.MSG_PROBE_EVERY || 225));
async function postStatus(loginId, entry, status, detail) {
  if (!PY_STATUS_URL) return;
  // 掉线/待重登时 entry.accountId 常为空（尚未晋级）→ 尽力从 c_user cookie 补
  // （登出后 c_user 通常残留），让 Python 侧不健康登记与后续恢复对得上同一账号。
  let acct = String((entry && entry.accountId) || "");
  if (!acct && entry && entry.context) {
    try { acct = await readAccountId(entry.context); } catch (_) { acct = ""; }
  }
  await postJson(PY_STATUS_URL, {
    platform: "messenger",
    account_id: acct,
    login_id: String(loginId || ""),
    status: String(status || ""),
    detail: String(detail || ""),
    // P1 身份化：authorized 时携带自身昵称/头像（promoteIfLoggedIn 已采集），
    // Python session-status 端点据此富集 registry meta.self_*（重启重连即回填）。
    pushname: String((entry && entry.name) || ""),
    avatar_url: String((entry && entry.avatarUrl) || ""),
    ts: Math.floor(Date.now() / 1000),
  });
}

// ── 入站健康心跳（P0 半死态探测）────────────────────────────────────────────
// 进线程读取成败滚动窗：readThreadTail 每次真实调用后记录。采样三态见
// msg_ops.threadReadSample（ok / fail / skip——空读+占位预览＝锁死旧线程，不入窗）。
// 另加**每线程失败冷却**：同一条永远读不出的线程（锁死会话 / 认不出的请求区页面）
// 会被 sig 漂移或请求区扫描反复重读，若每次都记失败，一条卡死线程就能垄断整个
// 窗口把账号钉成「半死」——真实管线故障的特征是**多个不同线程**都在失败，
// 单线程重复失败 10 分钟只记一次即可保住该特征且不放大单点噪声。
const READ_FAIL_COOLDOWN_MS = 10 * 60 * 1000;
const READ_FAIL_BOOK_MAX = 64;
function recordThreadRead(entry, tail, opts = {}) {
  if (!entry) return;
  const key = String((opts && opts.key) || "");
  const sample = threadReadSample(tail, {
    previewPlaceholder: !(opts && opts.previewPlaceholder === false),
  });
  if (sample === "skip") return;
  if (!entry._readFailBook) entry._readFailBook = new Map();
  if (sample === "ok") {
    if (key) entry._readFailBook.delete(key);
  } else if (key) {
    const now = Date.now();
    const last = entry._readFailBook.get(key) || 0;
    if (now - last < READ_FAIL_COOLDOWN_MS) return; // 同线程失败冷却期内不重复入窗
    entry._readFailBook.set(key, now);
    while (entry._readFailBook.size > READ_FAIL_BOOK_MAX) {
      const oldest = entry._readFailBook.keys().next().value;
      entry._readFailBook.delete(oldest);
    }
    // 失败样本落一条 info（2026-08-11 教训：全败 8/8 却查不到「谁在失败」——
    // null 路径全程静默，只能靠猜）。冷却门内最多每线程 10min 一条，噪声有界。
    logger.info({ accountId: entry.accountId, thread: key,
      kind: tail === null ? "null" : "empty" }, "thread read fail sample");
  }
  if (!entry._readWin) entry._readWin = [];
  entry._readWin.push(sample === "ok");
  while (entry._readWin.length > READ_WIN_MAX) entry._readWin.shift();
}

// 侧栏未读总数：messenger.com 无障碍文本自带「Chats · N unread」（中文 UI「聊天」区
// 同有未读计数文案）。读不到/无未读 → 0。best-effort，绝不抛。
async function readInboxUnread(page) {
  try {
    return await page.evaluate(() => {
      const t = (document.body && document.body.innerText) || "";
      const m = t.match(/Chats\s*[·•]\s*(\d+)\s*unread/i)
        || t.match(/(\d+)\s*unread/i)
        || t.match(/(\d+)\s*条?未读/);
      return m ? parseInt(m[1], 10) || 0 : 0;
    });
  } catch (_) { return 0; }
}

async function postInboxHealth(entry) {
  if (!PY_INBOX_HEALTH_URL || !entry || !entry.accountId
      || entry.status !== "authorized") return;
  const win = entry._readWin || [];
  const fails = win.filter((ok) => !ok).length;
  let unread = 0;
  try { unread = await readInboxUnread(entry.page); } catch (_) { unread = 0; }
  entry._lastUnread = unread; // /accounts 观测用
  const cs = entry._convStats || { count: 0, ph: 0 };
  const e2eeRatio = cs.count > 0 ? cs.ph / cs.count : -1;
  // P0（2026-08-14，173 实测盲区）：PIN 缺失/未通过是确定性证据（_pinState 只在
  // detectPinPrompt 亲证浮层在场后置位），resolveInboxHint 让它**独立**出精确码——
  // 旧写法 `if (hint && pinState…)` 被 classifyInboxHint 的占位比 ≥0.6 闸住，173
  // 占位比 0.39 时 PIN 明明缺失却全程零提示，坐席只看到泛泛「通道离线」。
  const hint = resolveInboxHint({
    baseHint: classifyInboxHint({
      unread, readAttempts: win.length, readFails: fails,
      e2eeRatio, convCount: cs.count,
    }),
    pinState: entry._pinState,
  });
  entry._inboxHintCode = hint; // /accounts 出网（前端热区 ACTIVE 时先把字段备好）
  await postJson(PY_INBOX_HEALTH_URL, {
    platform: "messenger",
    account_id: entry.accountId,
    unread,
    read_attempts: win.length,
    read_fails: fails,
    last_inbound_ts: Math.floor((entry._lastInboundOkTs || 0) / 1000),
    // P1 占位盲区信号：侧栏 E2EE 加密占位预览占比（健康会话预览是解密后正文，
    // 大面积占位＝解密不可用）。零导航零标已读，弥补「存量未读预览不变 → 读取窗
    // 永远零样本」的检测盲区。conv_count 供 Python 侧小样本护栏。
    e2ee_ratio: e2eeRatio,
    conv_count: cs.count,
    // 「消息请求」文件夹被 FB 限频的退避截止（观测用；0=未被限）
    requests_blocked_until: Math.floor((entry._reqBlockedUntil || 0) / 1000),
    // P2：半死态产品码（detail 携带——路由热区 ACTIVE 时不改 Python 形参，
    // 运维/日志仍可读；正式字段等路由放开后升格）。
    detail: hint || "",
    // P1 PIN 自愈观测（2026-08-14）：状态 + 尝试/成败计数随心跳落 Python 健康行
    //（老 worker 不带这些键 → Python 侧按未知处理，不误报）。
    pin_state: String(entry._pinState || ""),
    pin_heal_attempts: Number((entry._pinHeal || {}).attempts || 0),
    pin_heal_ok: Number((entry._pinHeal || {}).ok || 0),
    pin_heal_fail: Number((entry._pinHeal || {}).fail || 0),
    // P0 2026-08-15 未读驱动强制读取（附加字段，Python 侧未消费前按未知键忽略）：
    // 触发计数 + worker 代码指纹/半部署位——「改了 server.js 没重启 Node」在心跳可见。
    unread_forced_picked: Number((entry._unreadForceStats || {}).picked || 0),
    worker_code_fp: WORKER_CODE_FP,
    worker_code_stale: workerCodeInfo().code_stale,
    // P2 2026-08-15：请求页可疑空转 streak（区分「真没人来」与「改版读不到」）
    requests_suspect_streak: Number(entry._reqSuspectStreak || 0),
    ts: Math.floor(Date.now() / 1000),
  });
}

// ── P1 目录同步 + 首连历史回填 ─────────────────────────────────────────────

// 侧栏相对时间 → epoch 秒（best-effort；认不出返回 0=占位沉底，绝不猜）。
// messenger 侧栏格式："3m"/"2h"/"1d"/"16w"/"3 分钟"/"2 小时"/"5天"/"2周" 等。
function parseRelativeTime(label, nowMs = Date.now()) {
  const s = String(label || "").trim();
  if (!s) return 0;
  const m = s.match(/^(\d+)\s*(分钟?|小时|天|周|月|年|min|m|h|d|w|mo|y)\b/i);
  if (!m) return 0;
  const n = parseInt(m[1], 10);
  if (!Number.isFinite(n) || n < 0) return 0;
  const unit = m[2].toLowerCase();
  const SEC = { m: 60, h: 3600, d: 86400, w: 604800, mo: 2592000, y: 31536000 };
  let mult = 0;
  if (unit === "分" || unit === "分钟" || unit === "min" || unit === "m") mult = SEC.m;
  else if (unit === "小时" || unit === "h") mult = SEC.h;
  else if (unit === "天" || unit === "d") mult = SEC.d;
  else if (unit === "周" || unit === "w") mult = SEC.w;
  else if (unit === "月" || unit === "mo") mult = SEC.mo;
  else if (unit === "年" || unit === "y") mult = SEC.y;
  if (!mult) return 0;
  return Math.floor(nowMs / 1000) - n * mult;
}

// 目录同步：把主收件箱会话行推成 Python 会话占位。哈希去重（首轮全量 + 之后内容
// 变化才推）；只推主列表（消息请求不建占位——陌生请求由入站链路带 is_request 处理）。
async function postDirectory(entry, convs, { force = false } = {}) {
  if (!PY_CHATS_URL || !entry || !entry.accountId
      || entry.status !== "authorized" || !Array.isArray(convs) || !convs.length) return;
  const rows = [];
  for (const c of convs.slice(0, 200)) {
    if (!c || !c.key || !c.name) continue;
    // 名字位是状态行（"Active now"/"在线"）或列表头（"Chats · N unread"）＝整行
    // 被误抓——落进目录就是脏占位名（生产实锤 3 条），跳过；该线程有真消息时
    // ingest 链路会带真名建会话，宁缺勿脏。
    if (DIRTY_NAME_RE.test(c.name) || /unread|未读|^Chats\b|^聊天\b/i.test(c.name)) continue;
    rows.push({
      jid: String(c.key),
      name: String(c.name || ""),
      avatar_url: String(c.avatar || ""),
      ts: parseRelativeTime(c.rel),
      // unread 刻意不带（侧栏行级未读标记抓取不可靠，宁缺勿假）
    });
  }
  if (!rows.length) return;
  // 指纹只含 key/name/avatar（ts 是相对时间推算值，每轮都会漂移，进指纹会失去去重意义）
  const fp = rows.map((r) => `${r.jid}|${r.name}|${r.avatar_url}`).join("\n");
  let hash = 0;
  for (let i = 0; i < fp.length; i++) { hash = ((hash << 5) - hash + fp.charCodeAt(i)) | 0; }
  if (!force && entry._dirSyncHash === hash) return;
  // 防抖（MSG_DIR_SYNC_MIN_MS，另一线的增量配置）：头像 CDN token 轮换会让指纹
  // 高频漂移，指纹去重之上再加最小间隔，两次推送至少隔这么久。
  const nowMs = Date.now();
  if (!force && entry._dirLastPushMs && (nowMs - entry._dirLastPushMs) < MSG_DIR_SYNC_MIN_MS) return;
  entry._dirSyncHash = hash;
  entry._dirLastPushMs = nowMs;
  await postJson(PY_CHATS_URL, {
    platform: "messenger",
    account_id: entry.accountId,
    chats: rows,
  });
  logger.info({ accountId: entry.accountId, n: rows.length }, "directory sync pushed");
}

// 首连历史回填：baseline 建立后逐条（每 tick 1 条）读线程末尾消息推给 Python。
// 空会话幂等在 Python 侧（not_empty 即跳过）——重启/重连后的重复回填天然无害。
async function backfillStep(entry) {
  if (!PY_THREAD_HISTORY_URL || !entry || entry.status !== "authorized") return;
  const q = entry._backfillQueue;
  if (!Array.isArray(q) || !q.length) return;
  const item = q.shift();
  if (!item || !item.key) return;
  try {
    // skipMedia：回填是文字上下文，不下载媒体（20 线程 × 若干图没必要，真媒体走实时链）
    const tail = await readThreadTail(entry, item.key, null, { skipMedia: true });
    // 回填队列无 preview 可判 → 按占位宽松处理（空读=skip 不入窗；锁死旧线程在
    // 回填里大量出现，把它们记失败会在启动后立刻把窗口打成全败）。成功仍入窗。
    recordThreadRead(entry, tail, { key: item.key });
    if (!Array.isArray(tail) || !tail.length) {
      // E2EE 冷启动读空（Labyrinth 尚未解密该线程）：进一次性重试队列——主队排空后再补读
      // 一轮（那时解密多半已跟上）。每线程最多重试一次（_retried 钉死），绝不循环。
      if (!item._retried) {
        item._retried = true;
        if (!entry._backfillRetry) entry._backfillRetry = [];
        entry._backfillRetry.push(item);
      }
      return;
    }
    const nowSec = Math.floor(Date.now() / 1000);
    await postJson(PY_THREAD_HISTORY_URL, {
      platform: "messenger",
      account_id: entry.accountId,
      chat_key: String(item.key),
      name: String(item.name || ""),
      avatar_url: String(item.avatar || ""),
      ts: nowSec,
      // Q-31 D（#317 PUUWJB）：只加字段。走既有 backfill 不起草 / dormant_review。
      backfill: true,
      backfill_source: "msg_backfill",
      messages: tail
        .filter((m) => m && !isUnsentTombstone(m.text))
        .map((m) => {
          const mid = stampMsgId(entry, item.key, m);
          return {
            direction: m.direction === "out" ? "out" : "in",
            sender: String(m.sender || ""),
            text: String(m.text || ""),
            media_type: String(m.media_type || ""),
            media_ref: String(m.media_ref || ""),
            // 前向兼容：当前 thread-history 路由尚未透传 msg_id（热区 ACTIVE 暂不动
            // Python）；实时 ingest 路径已带。两侧同公式，路由放开后历史表情可挂。
            msg_id: mid,
            // 实施72 P4：aria 时间文本（parseMsgAria 早已解析出）保守转 epoch——
            // 认出＝真实时间（Python 侧存真值、approx=0）；认不出＝0（Python 侧
            // 照旧按数组序回推 + 打 approx 标，失败方向永远退回现状）。
            ts: ariaDatetimeToEpoch(m.ts) || 0,
          };
        }),
    });
    try { await processThreadOps(entry, item.key, tail); } catch (_) { /* best-effort */ }
    logger.info({ accountId: entry.accountId, thread: item.key, n: tail.length,
                  left: q.length }, "backfill pushed");
  } catch (e) {
    logger.debug({ e, thread: item && item.key }, "backfillStep failed");
  }
}

// ── DOM 选择器集中区（messenger.com 改版时改这里；已按真号联调校准）─────────────
// 会话列表左栏：普通会话是 a[href^="/t/"]，端到端加密会话是 a[href^="/e2ee/t/"]。
// 二者都要抓（真号实测 E2EE 占多数），线程 id 统一取 /t/<数字> 段。
const SEL_CONV_LINKS = 'a[href^="/t/"], a[href^="/e2ee/t/"]';
// 打开某会话后的消息气泡容器（Messenger 用 role=row 承载每条消息）。
const SEL_MSG_ROWS = 'div[role="row"]';
// 输入框（contenteditable 富文本）。
const SEL_COMPOSER = 'div[role="textbox"][contenteditable="true"]';

// 会话预览里代表「本方发出/系统占位」的前缀 → 入站轮询应跳过（非对端来信）。
// "你:"/"你发送了…" = 自己发的；E2EE 占位 = 无正文可读。
const OUTBOUND_PREVIEW_RE = /^(你[:：]|你发送了|你撤回了|你回复了|You sent|You:|You unsent|You replied)/;
const E2EE_PLACEHOLDER_RE = /端到端加密|end-to-end encrypt|无法显示消息|无法显示此消息|can't display/i;
// 「在线/活跃/正在输入」等**状态行**，绝非消息正文。E2EE 会话列表行常把状态/联系人名当成
// 「预览」抓出（无可读正文）→ 若不拦，会把状态或人名当消息去自动回复（如把对方名字当消息，
// 回一句"这听起来像全名"）。用于：① 选预览时跳过 ② 名字位若命中状态词说明整行被误抓→弃用该行。
const STATUS_LINE_RE = /^(在线|活跃|刚刚活跃|在线状态|正在输入.*|对方正在输入.*|active(\s+now)?|online|typing.*)$/i;

// 「绝不可能是人名」的脏名黑名单（2026-08-14，修「好友名单显示 Active now」事故）：
// STATUS_LINE_RE 只挡严格的 "Active now"，挡不住 "Active 3m ago"/"Active yesterday"，
// 而 "Message request(s)" 此前完全零过滤——DOM 改版后这些行会排到 gridcell 第一行，
// 旧「name = lines[0]」直接把它们当昵称入库并覆盖真名。本正则是**取名/目录/入库**
// 三处共用的名字校验superset（STATUS_LINE_RE 仍保留给「预览/消息正文」语义）。
// ⚠ 跨语言契约：pattern 与 src/integrations/protocol_bridge.py::_DIRTY_NAME_PATTERN
// **逐字一致**，由 tests/test_peer_name_sanitizer.py 抽取比对 + 金标样本钉住；
// 改这里必须同步改 Python 侧并跑该门禁。
const DIRTY_NAME_RE = /^(?:[·•]|回复？|是否跟进？|在线|在线状态|刚刚活跃|昨天活跃|(?:\d+\s*(?:分钟|小时|天|周)前)?活跃|正在输入.*|对方正在输入.*|typing.*|online|active(?:\s+(?:now|today|yesterday))?|active\s+\d+\s*(?:m|min|mins|minutes?|h|hr|hrs|hours?|d|days?|w|weeks?)?(?:\s+ago)?|消息请求|message\s+requests?|未读消息.*|unread\s+messages?.*|chats?\s*[·•].*|聊天\s*[·•].*)$/i;

// ── 自回声抑制（根治「自己回自己的消息」死循环）───────────────────────────────
// 轮询只能从「列表预览」推断新消息，没有 msg_id/方向/时间戳。本方一发出回复，该回复就成为
// 该会话的最新预览 → 下一轮被误判为「新入站」→ 再次自动回复 → 死循环。前缀正则(你：/You:)不
// 够稳（截断/改版/请求线程格式差异会漏判）。故这里**显式记住本服务刚发出的文本**，轮询时凡
// 与近期自发文本吻合的预览一律跳过——方向判定不再依赖脆弱前缀，确定性消除自回复。
const SENT_ECHO_TTL_MS = 10 * 60 * 1000; // 自发文本的抑制窗口（10 分钟）

// 归一化预览/自发文本：剥离「你：/You:/你发送了/You sent」方向前缀 + 折叠空白 + 去尾部省略号
// （预览常被 messenger 截断）+ 小写。用于自回声/重复的鲁棒比对。
function normPreview(s) {
  return String(s || "")
    .replace(/^(你[:：]\s*|You[:：]\s*|你发送了[:：]?\s*|You sent[:：]?\s*)/i, "")
    .replace(/[\u2026…]+$/, "")
    .replace(/\s+/g, " ")
    .trim()
    .toLowerCase();
}

// 记录本服务发出的消息（chat_key + 归一化文本 + 时间），有界 + 过期清理。
function recordSent(entry, key, text) {
  if (!entry || !key || !text) return;
  if (!entry.sentLog) entry.sentLog = [];
  entry.sentLog.push({ key: String(key), norm: normPreview(text), ts: Date.now() });
  const cutoff = Date.now() - SENT_ECHO_TTL_MS;
  entry.sentLog = entry.sentLog.filter((e) => e.ts >= cutoff).slice(-100);
  // P3 2026-08-15 镜像自发环（无 TTL、按条数封顶）：sentLog 的 10min TTL 对
  // 「发送后很久才被首次读取」的线程（E2EE 占位线程常态）失效——自己发的消息
  // 会被 mirrorManualOutbound 误当人工消息以新时间戳重灌，进而污染 Python 出站
  // 近重复守卫的 180s 窗（生产实锤：把 AI 对客户新问题的回复静默拦掉）。
  if (!entry.mirrorSentRing) entry.mirrorSentRing = new Map();
  sentRingPush(entry.mirrorSentRing, key, normPreview(text));
}

// 某预览是否吻合近期本方发出的消息。比对核心收口到 msg_ops.echoTextHit（与镜像
// 自发环同一判据）：全串相等，或双方 ≥16 字且 24 字前缀互为前缀（预览截断容错）。
// ⚠ 短文本只认全等——2026-08-16 实锤：刚发「在」，客户 10 分钟窗内回「在吗」，
// 旧的无条件前缀匹配把它判成回声 → 跳过+推进 seen，客户消息永久隐形（线程
// 发送后停在打开态=永无未读标记，未读兜底也救不了）。
function isSelfEcho(entry, key, preview) {
  if (!entry || !entry.sentLog || !entry.sentLog.length) return false;
  const np = normPreview(preview);
  if (!np) return false;
  const cutoff = Date.now() - SENT_ECHO_TTL_MS;
  for (const e of entry.sentLog) {
    if (e.ts < cutoff || e.key !== String(key)) continue;
    if (echoTextHit(np, e.norm)) return true;
  }
  return false;
}

// ── 驾驶舱 P2（2026-08-13）：人工出站回流 ────────────────────────────────────
// 坐席在**原生 messenger.com 页**手动发的消息此前只活在 FB 侧——工作台看不见，
// 交还 AI 后引擎不知道人说过什么（交接失忆，双面分工方案 v2 的已知断点）。
// 本函数在**既有 tail 读取**上顺路镜像 direction:"out"（零新增导航/零新副作用）：
//   - 水位线基线：每会话首次观察只建水位不上报——sidecar 重启后 sentLog（内存态）
//     已清空，tail 里的历史出站多为编排器早已镜像过的 AI 消息，重灌＝重复行；
//   - 自发回声跳过：sentLog 命中（sidecar 自己发的，编排器已镜像）不重复上报；
//     同文撞车误判＝宁缺勿重，可接受；
//   - E2EE 占位/撤回墓碑跳过；找不到水位（窗口滑出）保守只报最后一条（与入站
//     burst 同哲学）；synthMsgId 稳定 id，python 端 INSERT OR IGNORE 二次兜底。
// 诚实边界（2026-08-16 起分两档）：
//   - 普通读取（客户回复触发等）：捕获时机＝下一次进线程读，人工消息在该客户
//     消息**之前**入库，AI 拟稿历史完整；不为镜像实时性新增导航成本。
//   - manualTrigger（预筛发现非自发的 "你:/You:" 预览、专门为镜像进线程）：
//     手发后 ~一个 poll tick 内即回流（≈4-10s），首次观察也允许镜像**触发那条**
//     （取件决策收口 msg_ops.pickManualOutMirror——文本须与触发预览吻合，
//     重启前历史绝不连带）。
async function mirrorManualOutbound(entry, chatKey, tail, opts = {}) {
  try {
    if (!Array.isArray(tail) || !tail.length) return;
    if (!entry._outMirrorMark) entry._outMirrorMark = new Map();
    const key = String(chatKey);
    const outs = tail.filter((m) => m && m.direction === "out"
      && (m.text || m.media_ref)
      && !(m.text && !m.media_ref && (E2EE_PLACEHOLDER_RE.test(m.text)
        || isUnsentTombstone(m.text))));
    if (!outs.length) return;
    const sigOf = (m) => `${normPreview(m.text)}|${m.media_ref || ""}|${m.ts}`;
    const { fresh, newMark } = pickManualOutMirror(outs, {
      mark: entry._outMirrorMark.get(key),
      sigOf,
      normOf: (m) => normPreview(m.text || ""),
      rowPreviewNorm: String(opts.rowPreviewNorm || ""),
      manualTrigger: !!opts.manualTrigger,
      burstMax: MSG_BURST_MAX,
    });
    if (newMark) entry._outMirrorMark.set(key, newMark);
    for (const m of fresh) {
      if (isSelfEcho(entry, key, m.text || "")) continue; // sidecar 自发，编排器已镜像
      // P3 2026-08-15：无 TTL 自发环二次核对——sentLog 10min TTL 过期后，服务
      // 自己发的消息在迟到的首读里会伪装成「人工消息」（ts=now 重灌 → 污染出站
      // 近重复守卫窗，实锤拦掉 AI 对新问题的回复）。环按条数封顶，重启丢失无害
      // （水位线基线本就防重启重灌）。
      if (sentRingHit(entry.mirrorSentRing, key, normPreview(m.text || ""))) continue;
      const mid = stampMsgId(entry, key, m);
      try {
        await postIngest({
          platform: "messenger", account_id: entry.accountId, chat_key: key,
          // 行昵称透传：手发到「全新线程」时 Python 侧靠它把会话建成真名而非裸 id
          //（净化在服务端 sanitize_peer_name，脏名会被拦）。
          name: String(opts.rowName || ""), text: m.text || "",
          media_type: m.media_type || "", media_ref: m.media_ref || "",
          ts: Math.floor(Date.now() / 1000), msg_id: mid,
          // Q-24 C（#298）：手机/其它设备发出的消息回抄——origin=external 让 Python 侧
          // 只落行（不起草、不算坐席动作、不触发让位）；sender_id=external 供气泡打「手机端发出」小标。
          direction: "out", origin: "external", sender_id: "external",
          is_request: false, request_category: "",
        });
        bumpOp(entry, "manual_out_mirrored");
      } catch (_) { /* best-effort：镜像失败绝不影响入站主流程 */ }
    }
  } catch (_) { /* 镜像是增强，绝不抛 */ }
}

// ── 进线程读正文：解析每条消息的无障碍标签 → 权威方向 + 发送者 + 全文 ─────────────
// Messenger 每条消息是 div[role="button"]，aria-label 词序已历三代（zh 旧 / en 旧 /
// en 新 2026-08「Message sent <datetime> by <sender>」）——解析器 parseMsgAria 与锚点
// MSG_ARIA_RE 收口在 msg_ops.js（纯函数配 node --test 门禁），本文件只消费。
// 发送者为「你/You」→ 本方(out)，否则对端(in)。正文为**未截断全文**；对 E2EE 会话同样可读
// （对话正文渲染在主 frame 的 role=log 区，而非 fbsbx 沙箱 iframe）。
// ⚠ 各 page.evaluate 里的消息锚点正则是浏览器作用域的**字面量副本**（evaluate 无法引用
// Node 侧常量），词形必须与 msg_ops.MSG_ARIA_RE 同源同义：/(消息由.*发送于|Message sent )/i

// 用浏览器会话下载线程里的媒体元素到 MSG_MEDIA_DIR，返回 {media_type, media_ref} 或 {}。
// scontent CDN 直链用 page.request（带会话 cookie）；blob: 用页内 fetch→base64 回传。
// 失败/未配置 MSG_MEDIA_DIR → {}（上层回落占位文本，绝不阻断入站）。
async function downloadThreadMedia(page, media) {
  if (!media || !media.src || !MSG_MEDIA_DIR) return {};
  try {
    let buf = null;
    if (media.src.startsWith("data:")) {
      // E2EE 解密后内联的真图：data:[mime][;base64],<payload> → 直接解码，无需网络。
      const comma = media.src.indexOf(",");
      if (comma > 0) {
        const meta = media.src.slice(0, comma);
        const payload = media.src.slice(comma + 1);
        buf = /;base64/i.test(meta)
          ? Buffer.from(payload, "base64")
          : Buffer.from(decodeURIComponent(payload));
      }
    } else if (media.src.startsWith("blob:")) {
      const b64 = await page.evaluate(async (u) => {
        try {
          const r = await fetch(u);
          const ab = await r.arrayBuffer();
          let s = ""; const bytes = new Uint8Array(ab);
          for (let i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
          return btoa(s);
        } catch (_) { return ""; }
      }, media.src);
      if (b64) buf = Buffer.from(b64, "base64");
    } else {
      const resp = await page.request.get(media.src, { timeout: 15000 }).catch(() => null);
      if (resp && resp.ok()) buf = Buffer.from(await resp.body());
    }
    if (!buf || !buf.length) return {};
    // 优先按 data: 的 MIME 定扩展名（图片可能是 png/webp/gif），否则按媒体类型缺省。
    let ext = media.kind === "image" ? ".jpg"
      : media.kind === "video" ? ".mp4"
      : media.kind === "voice" ? ".mp4" : ".bin";
    if (media.src.startsWith("data:")) {
      const mm = media.src.slice(0, 40).match(/^data:image\/(png|webp|gif|jpeg|jpg)/i);
      if (mm) ext = "." + mm[1].toLowerCase().replace("jpeg", "jpg");
    }
    const fname = `msg_${Date.now()}_${Math.random().toString(36).slice(2, 8)}${ext}`;
    fs.mkdirSync(MSG_MEDIA_DIR, { recursive: true });
    fs.writeFileSync(path.join(MSG_MEDIA_DIR, fname), buf);
    return { media_type: media.kind, media_ref: `${MSG_MEDIA_URL_BASE}/${fname}` };
  } catch (e) {
    logger.debug({ e }, "downloadThreadMedia failed");
    return {};
  }
}

// 媒体占位文本（下载失败/未配置目录时回落，至少让 AI/坐席知道「客户发了媒体」，对齐 Telegram）。
const MEDIA_PLACEHOLDER = { image: "[图片]", video: "[视频]", voice: "[语音]", file: "[文件]" };

// 等线程出现「真实消息」（非加密横幅）再读，防 fresh 导航只抓到「…受端到端加密保护…」占位。
// 命中一条非横幅的 MSG_ARIA 即返回 true；超时 false（上层可换 /e2ee/t 路径重试或按现状返回）。
async function waitThreadContent(page, timeoutMs = 3000) {
  try {
    await page.waitForFunction(() => {
      const region = document.querySelector('[role="log"]') || document.body;
      return Array.from(region.querySelectorAll("[aria-label]"))
        .map((e) => e.getAttribute("aria-label") || "")
        .filter((a) => /(消息由.*发送于|Message sent )/i.test(a))
        .some((a) => !/端到端加密|end-to-end encrypt|无法显示|can't display/i.test(a));
    }, { timeout: timeoutMs });
    return true;
  } catch (_) { return false; }
}

// 打开线程读末尾若干条消息（权威）。用专用 readPage（与发送用的 entry.page 隔离，避免互相打断）。
// 返回按 DOM 顺序的 [{sender,direction,ts,text,media_type,media_ref,reactions?}]（含媒体气泡：
// 下载成功带 media_ref，失败回落占位文本；纯系统项仍过滤）。
//
// ── 打开线程副作用清单（P2，可执行决策见 canOpenThread）────────────────────
// 1. FB 侧标已读 → 未读回填 / 主动探针禁用（backfill_skips_unread / probe_disabled）
// 2. 请求区打开 ≠ 接受（已联调）→ request_read 允许，但仍受 MSG_MAX_REQ_OPENS 限流
// 3. 发送 / 坐席拉更早 / 实时入站 → 允许（「正在处理」语义）
// 4. 半死态探测改走 e2ee_ratio 零导航信号，禁止为补样本而开线程
async function readThreadTail(entry, key, extPage = null, opts = {}) {
  if (!entry || !entry.context) return [];
  try {
    // extPage：调用方自备页（/debug/tail 用）。生产轮询与调试曾共用 entry.readPage，
    // 轮询 finally 的页面回收会把调试刚建的页关掉 → 调试恒 readOk=false 误导排查
    // （2026-08-04 实锤：差点把并发缺陷当成「线程读不到」的证据）。
    let page = extPage;
    if (!page) {
      if (!entry.readPage || entry.readPage.isClosed()) {
        entry.readPage = await entry.context.newPage();
      }
      entry._readUsed = true; // 本轮用过读页 → pollInbound finally 不回收它（活跃账号不抖动）
      page = entry.readPage;
    }
    await page.goto(`${MESSENGER_URL}t/${key}`, { waitUntil: "domcontentloaded", timeout: 20000 });
    // 等消息区就绪（role=log 出现）；未出现视为渲染未就绪 → 返回 null 让上层下轮重试（不误判为空）。
    const logReady = await page.waitForSelector('[role="log"]', { timeout: 4500 })
      .then(() => true).catch(() => false);
    if (!logReady) return null;
    // 等真实消息渲染（非加密横幅）。E2EE 线程走 /t/ 有时只渲染横幅 → 换 /e2ee/t/ 再试一次。
    let hasContent = await waitThreadContent(page, 3000);
    if (!hasContent) {
      await page.goto(`${MESSENGER_URL}e2ee/t/${key}`, { waitUntil: "domcontentloaded", timeout: 20000 })
        .catch(() => {});
      await page.waitForSelector('[role="log"]', { timeout: 4500 }).catch(() => {});
      hasContent = await waitThreadContent(page, 3000);
    }
    // E2EE 半死就地自愈（2026-08-10 .198 事故）：/t/ 与 /e2ee/t/ 都读不出真实内容、且本页正
    // 挂着「输入恢复 PIN」浮层 → 设备密钥丢失的半死态。auto-PIN 此前只挂在登录观察循环 + 心跳
    // 自愈（探 entry.page 侧栏），而 PIN 浮层**只在打开加密线程时**现身 → 那两处永远撞不到，
    // 于是 e2ee_pin_set=true 却 e2ee_pin_state=""（存了从不输）。读线程本就要导航到该线程，
    // 就地探浮层→输 PIN→重载一次，不新增任何为探测而做的导航（遵守零标已读探针纪律）。
    if (!hasContent && getE2eePin(entry._loginId || "") && await detectPinPrompt(page)) {
      entry._pinPromptSeen = true;
      const healed = await tryAutoE2eePin(entry._loginId || "", entry, page).catch(() => false);
      if (healed) {
        await page.goto(`${MESSENGER_URL}e2ee/t/${key}`, { waitUntil: "domcontentloaded", timeout: 20000 })
          .catch(() => {});
        await page.waitForSelector('[role="log"]', { timeout: 4500 }).catch(() => {});
        hasContent = await waitThreadContent(page, 3000);
      }
    }
    await page.waitForTimeout(hasContent ? 400 : 800);
    // 滚到底 + 等图片解码：E2EE 图片经客户端解密后才以 blob: 挂载，初始是 data: 模糊占位；不等则
    // 只看到 data: 占位（被跳过）→ 整条媒体消息漏读。滚到底触发懒加载/解密，再等"渲染够大且非
    // data:"的图片出现（无大图=纯文本线程，立即通过不空等）。
    try {
      await page.evaluate(() => {
        const r = document.querySelector('[role="log"]') || document.scrollingElement || document.body;
        if (r) r.scrollTop = r.scrollHeight;
      });
    } catch (_) {}
    await page.waitForFunction(() => {
      const region = document.querySelector('[role="log"]') || document.body;
      const big = Array.from(region.querySelectorAll("img")).filter((im) => {
        const rc = im.getBoundingClientRect();
        return rc.width >= 128 && rc.height >= 128;
      });
      if (!big.length) return true; // 无大图 → 纯文本线程，不空等
      // 大图已解码(自然尺寸就绪)即可读；E2EE 图解密后自然尺寸变大(无论 data:/blob:/scontent)。
      return big.some((im) => (im.naturalWidth || 0) >= 128);
    }, { timeout: 3500 }).catch(() => {});
    // 每条消息取 aria-label + 其气泡容器内的媒体源（图片/视频/音频）。保守取媒体：仅当元素够大
    // （避免头像/emoji/链接预览缩略图误判为客户媒体）；有文本的气泡不强行贴图（防误贴头像）。
    const items = await page.evaluate((reactRe) => {
      const region = document.querySelector('[role="log"]')
        || document.querySelector('[aria-label*="消息"],[aria-label*="Messages"],[aria-label*="对话"]')
        || document.querySelector('[role="main"]') || document.body;
      const MSG_ARIA = /(消息由.*发送于|Message sent )/i;
      const nodes = Array.from(region.querySelectorAll('[aria-label]'))
        .filter((e) => MSG_ARIA.test(e.getAttribute("aria-label") || ""))
        .slice(-15);
      return nodes.map((e) => {
        const aria = e.getAttribute("aria-label") || "";
        // 上溯到消息行容器（role=row 或最多 4 层父级）再找媒体
        let box = e;
        for (let i = 0; i < 4 && box.parentElement; i++) {
          if (box.getAttribute && box.getAttribute("role") === "row") break;
          box = box.parentElement;
        }
        // P2：同行内的表情回应 aria（「你用👍回应了」/「X reacted with …」）
        // Q-24 D（#298，P7P8FY 实锤）：当前 messenger.com 反应胶囊 aria 是「查看回应 /
        // See who reacted to this message」+ 可见 emoji 文本，旧正则一条匹不到 → 反应从未出边车。
        // 判据放宽（reactRe 由 Node 侧传入 = send_chain.REACTION_ARIA_RE），同时带出可见文本抽 emoji。
        const reactions = [];
        try {
          const rre = new RegExp(reactRe, "i");
          for (const el of box.querySelectorAll("[aria-label]")) {
            if (el === e) continue;
            const ra = el.getAttribute("aria-label") || "";
            if (!rre.test(ra)) continue;
            const rt = ((el.innerText || el.textContent) || "").trim().slice(0, 40);
            reactions.push({ aria: ra, text: rt });
          }
        } catch (_) { /* best-effort */ }
        let media = null;
        const vid = box.querySelector("video");
        const aud = box.querySelector("audio");
        const vSrc = vid && (vid.currentSrc || vid.src
          || (vid.querySelector("source") && vid.querySelector("source").src));
        if (vSrc) {
          media = { kind: "video", src: vSrc };
        } else if (aud && (aud.currentSrc || aud.src)) {
          media = { kind: "voice", src: aud.currentSrc || aud.src };
        } else {
          // 图片：容器内"渲染够大"的一张（rect 尺寸为主，兼容自然尺寸尚未 load 完）；跳过 data:
          // 占位与头像/emoji/小图标。E2EE 图解密后为 blob:/scontent，非 data: 才算真图。
          let best = null, bestArea = 0;
          for (const img of box.querySelectorAll("img")) {
            const src = img.currentSrc || img.src || "";
            if (!src) continue;
            const natW = img.naturalWidth || 0, natH = img.naturalHeight || 0;
            // data: 既可能是模糊占位(自然尺寸很小)，也可能是 E2EE 客户端解密后内联的真图(自然尺寸大，
            // 实测 natW=1080)。仅按"自然尺寸<128"滤掉占位；真图保留。非 data: 一律进尺寸门。
            if (src.startsWith("data:") && natW < 128) continue;
            const rc = img.getBoundingClientRect();
            const w = Math.max(rc.width, natW, img.width || 0);
            const h = Math.max(rc.height, natH, img.height || 0);
            const area = w * h;
            if (w >= 128 && h >= 128 && area > bestArea) { best = src; bestArea = area; }
          }
          // 背景图瓦片兜底（部分图片用 div background-image 渲染，非 <img>）
          if (!best) {
            for (const el of box.querySelectorAll("div,span,a")) {
              const rc = el.getBoundingClientRect();
              if (rc.width < 128 || rc.height < 128) continue;
              const bg = getComputedStyle(el).backgroundImage || "";
              const mm = bg.match(/url\(["']?((?:https?:|blob:)[^"')]+)["']?\)/);
              if (mm) { best = mm[1]; break; }
            }
          }
          if (best) media = { kind: "image", src: best };
        }
        return { aria, media, reactions };
      });
    }, REACTION_ARIA_RE.source);
    const msgs = [];
    for (const it of items) {
      const p = parseMsgAria(it.aria);
      if (!p) continue;
      p.media_type = ""; p.media_ref = "";
      // P2：解析同行反应 aria → [{emoji,sender}]（挂到本条消息）
      // Q-24 D：旧 aria 词序优先；匹不到走 parseReactionPill（emoji 取可见文本，发送者按
      // 消息方向推——本方出站气泡上的反应＝对方点的，入站气泡上的＝我们点的）。
      p.reactions = [];
      for (const rr of (it.reactions || [])) {
        const ra = typeof rr === "string" ? rr : String((rr && rr.aria) || "");
        const rt = typeof rr === "string" ? "" : String((rr && rr.text) || "");
        const r = parseReactionFromAria(ra)
          || parseReactionPill(ra, rt, p.direction === "out" ? "peer" : "me");
        if (r && r.emoji) p.reactions.push(r);
      }
      // 有媒体：文本气泡只在「无正文」时贴媒体（防把带头像的文本消息误标成图片）
      if (it.media && (!p.text || it.media.kind !== "image")) {
        if (opts.skipMedia) {
          // 历史回填等轻量读取：不下载媒体（20 线程 × 若干图的下载没必要），
          // 无正文时回落占位文本——上下文完整性够用，真媒体走实时入站路径。
          if (!p.text) p.text = MEDIA_PLACEHOLDER[it.media.kind] || "[媒体]";
        } else {
          const dl = await downloadThreadMedia(page, it.media);
          if (dl.media_ref) {
            p.media_type = dl.media_type; p.media_ref = dl.media_ref;
            if (!p.text) p.text = ""; // 媒体可无正文
          } else if (!p.text) {
            p.text = MEDIA_PLACEHOLDER[it.media.kind] || "[媒体]"; // 下载失败→占位
          }
        }
      }
      // 有正文/媒体，或纯撤回墓碑（P2 要上报 revoke）才留
      if (p.text || p.media_ref || isUnsentTombstone(p.text)) msgs.push(p);
    }
    return msgs; // 数组（可能为空=已读到但无文本/媒体消息）
  } catch (e) {
    logger.debug({ e, key }, "readThreadTail failed");
    return null; // 硬失败 → 上层不推进 seen，下轮重试
  }
}

/** 从 href（/t/123456）取线程 key。 */
function threadKeyFromHref(href) {
  const s = String(href || "");
  const m = s.match(/\/t\/([^/?#]+)/);
  return m ? m[1] : "";
}

/** 读取 c_user cookie（Facebook 数字账号 id）作为 account_id。 */
async function readAccountId(context) {
  try {
    const cookies = await context.cookies();
    const c = cookies.find((x) => x.name === "c_user");
    return (c && String(c.value)) || "";
  } catch (_) {
    return "";
  }
}

/** 是否已登录：须同时有 c_user（账号 id）+ xs（会话鉴权密钥）。
 *  只看 c_user 会误报——c_user 常在登出后残留，真正代表已鉴权会话的是 xs。 */
async function isLoggedIn(context) {
  try {
    const cookies = await context.cookies();
    const hasUser = cookies.some((x) => x.name === "c_user" && x.value);
    const hasAuth = cookies.some((x) => x.name === "xs" && x.value);
    return hasUser && hasAuth;
  } catch (_) {
    return false;
  }
}

/** 登录表单上是否挂着错误框（密码错/账号不存在）。任何异常一律 false——
 *  宁可漏判也不能把「正常等人输入」误报成「密码错」，更不能让观测弄崩看门狗。 */
async function hasLoginErrorBox(page) {
  try {
    return await page.evaluate(() => {
      const sels = ["#error_box", "[data-testid='royal_login_error']", "form [role='alert']"];
      for (const s of sels) {
        const el = document.querySelector(s);
        if (el && (el.innerText || "").trim().length > 2) return true;
      }
      return false;
    });
  } catch (_) {
    return false;
  }
}

/** 截当前登录页截图作为「二维码/登录面板」回传前端（data URI）。 */
async function snapshot(page) {
  try {
    const buf = await page.screenshot({ type: "png" });
    return "data:image/png;base64," + buf.toString("base64");
  } catch (_) {
    return "";
  }
}

/** best-effort 读取账号自身昵称/头像（供 self_profile 富集）。
 *
 *  页内只做**有界的候选收集**，「哪张才是自己的脸」交给 self_profile.js 的纯函数判定
 *  （可 node --test 直跑）。头像选取是 fail-closed 的：认不出就交空，绝不再退回
 *  「任意 img / 任意 fbcdn 图」——那条兜底 2026-08-15 实锤把**会话对端**的头像写成了
 *  账号自身头像（Calixa Lopez 的号挂着 Micah Bindo 的脸），且被 URL 指纹去重永久固化。
 *  账号卡缺头像只是少信息，挂错脸是错信息（运营据此判断这号绑的是谁）。 */
async function readSelfProfile(page) {
  const out = { name: "", avatarUrl: "", strategy: "", reason: "" };
  try {
    const raw = await page.evaluate(() => {
      // 自身身份唯一权威源：FB 页面标配的 CurrentUserInitialData 内嵌 JSON（NAME/USER_ID，
      // 多年稳定）。DOM 上任何可见文字/图片都可能属于对端，不能当自身身份用。
      const slices = [];
      try {
        for (const s of document.querySelectorAll("script")) {
          const t = s.textContent || "";
          const i = t.indexOf("CurrentUserInitialData");
          if (i < 0) continue;
          slices.push(t.slice(i, i + 2000));
          if (slices.length >= 4) break;
        }
      } catch (_) {}
      // 候选图（含 SVG <image>）：带上 alt、尺寸、**祖先链属性**——判定要用的全部证据。
      // 祖先链而非只取 href：实测这版页面头像 alt 全空、也没有 /me/ 链接，自身身份的
      // 唯一痕迹是账号菜单按钮的 aria-label（role/aria 都得带上，见 self_profile.js）。
      const cands = [];
      try {
        for (const el of document.querySelectorAll("img, image")) {
          if (cands.length >= 40) break;
          const src = el.getAttribute("src") || el.getAttribute("xlink:href")
            || el.getAttribute("href") || "";
          if (!src || src.startsWith("data:")) continue;
          const chain = [];
          let p = el;
          for (let hop = 0; p && hop < 8; hop++) {
            chain.push({
              tag: p.tagName,
              role: (p.getAttribute && p.getAttribute("role")) || "",
              aria: (p.getAttribute && p.getAttribute("aria-label")) || "",
              href: (p.getAttribute && p.getAttribute("href")) || "",
            });
            p = p.parentElement;
          }
          let w = Number(el.naturalWidth) || 0;
          let h = Number(el.naturalHeight) || 0;
          if (!w || !h) {
            try {
              const r = el.getBoundingClientRect();
              w = w || Math.round(r.width);
              h = h || Math.round(r.height);
            } catch (_) {}
          }
          cands.push({ src, alt: el.getAttribute("alt") || "", w, h, chain });
        }
      } catch (_) {}
      return { slices, cands };
    });
    const ident = parseCurrentUserInitialData(raw && raw.slices);
    const pick = pickSelfAvatar(raw && raw.cands, {
      selfName: ident.name, selfId: ident.userId,
    });
    out.name = ident.name;
    out.avatarUrl = pick.url;
    out.strategy = pick.strategy;
    out.reason = pick.reason;
    if (!pick.url) {
      // 认不出时留下证据（有界摘要，不打完整签名 URL）——下次调选择器靠这个而不是猜。
      logger.debug({
        reason: pick.reason, selfName: ident.name, selfId: ident.userId,
        candidates: summarizeCandidates(raw && raw.cands),
      }, "self avatar unresolved (fail-closed, will retry on recapture)");
    }
  } catch (_) {}
  return out;
}

// 仅在回落到捆绑 Chromium 时才用的兜底 UA（真 Chrome 通道下不覆盖，理由见 launchOptions）。
const REAL_UA = process.env.MSG_UA ||
  "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36";
const LOCALE = process.env.MSG_LOCALE || "zh-CN";
const TZ = process.env.MSG_TZ || "Asia/Shanghai";
// 浏览器通道：默认走系统安装的 Google Chrome，而非 Playwright 捆绑的 Chromium
// （Chrome for Testing）。捆绑版有两处硬伤：二进制自带 for-Testing 特征；版本长期落后于
// 真 Chrome，而一旦按新版伪造 UA，浏览器自己发出的 Sec-CH-UA 仍报真实内核版本与
// "Chromium" 品牌 —— UA 与 Client Hints 自相矛盾，是最容易被抓的自动化信号，
// 也是「账密+验证码都对却被弹回登录页、cookie 只剩 datr」的成因。
// 置空串可强制用捆绑 Chromium；机器没装 Chrome 时 launchContext 会自动回落。
const BROWSER_CHANNEL = process.env.MSG_BROWSER_CHANNEL ?? "chrome";
// 回落用的捆绑通道。Playwright 在 channel 为空且 headless 时会挑 chromium_headless_shell-*，
// 而 channel:"chromium" 无论 headless 与否都用完整的 chromium-*（getExecutableName）。
// 安装包只随包交付 chromium-*（headless shell 那棵树的路径最长 269 字符，超 MAX_PATH，
// 曾让 NSIS 升级覆盖失败弹「无法关闭」；见 desktop/build/after-pack.js），所以回落
// 必须显式点名 "chromium"，否则没装 Chrome 的客户机 restore 流会找不到浏览器。
const BUNDLED_CHANNEL = "chromium";

/** 浏览器启动参数（一号一代理 + 反自动化检测）。
 *  Facebook/Meta 会检测自动化浏览器（navigator.webdriver、AutomationControlled、
 *  HeadlessChrome UA 等），命中即登录后一导航就作废会话弹回登录页 → 必须 stealth。 */
function launchOptions(proxyUrl, channel = BROWSER_CHANNEL, headless = HEADLESS, extraArgs = [],
                       humanWindow = false) {
  const opts = {
    headless: headless,
    locale: LOCALE,
    timezoneId: TZ,
    // B64（2026-08-23 `_316`/`_317`）：给人看的 headed 登录窗必须用真实窗口视口
    // （viewport:null=跟随窗口）——固定 1280×800 时用户「最大化/拉大窗口」页面纹丝
    // 不动，FB checkpoint 机器人验证组件被压在一小块里放不大、过不去、永远等不到
    // 2FA 码。离屏/headless 流保持固定视口（截图流与探测面要确定性尺寸）。
    viewport: humanWindow ? null : { width: 1280, height: 800 },
    args: [
      "--disable-blink-features=AutomationControlled",
      "--disable-features=IsolateOrigins,site-per-process",
      "--no-default-browser-check",
      "--no-first-run",
      // 交互登录窗口模式的附加参数（离屏定位 / --headless=new），由 resolveLaunch 决定。
      ...(Array.isArray(extraArgs) ? extraArgs : []),
    ],
    ignoreDefaultArgs: ["--enable-automation"],
    // Q-24 E：沙箱开着就不会出 --no-sandbox 黄条（黄条本身也是「这是自动化浏览器」的指纹）
    chromiumSandbox: CHROMIUM_SANDBOX,
  };
  if (channel) opts.channel = channel;
  // UA 只在显式指定、或回落到捆绑 Chromium 时才覆盖：真 Chrome 自带的 UA 与它发出的
  // Client Hints 本就一致，此时再伪造只会重新制造上面那条矛盾。
  if (process.env.MSG_UA) opts.userAgent = process.env.MSG_UA;
  else if (!channel || channel === BUNDLED_CHANNEL) opts.userAgent = REAL_UA;
  if (proxyUrl) opts.proxy = { server: proxyUrl };
  return opts;
}

/** 该错误是否意味着「这台机器用不了这个通道」。
 *  只有这类才值得降级重开。冷启途中被取消/关窗抛的 "browser has been closed" 属于
 *  用户意图——把它当通道不可用会拿更弱的指纹把已取消的登录再开一次，等于白送一次
 *  风控命中（实测已发生：取消后自动用捆绑 Chromium 重开）。 */
function isChannelUnavailable(err) {
  const s = String((err && err.message) || err || "");
  return /executable doesn't exist|Chromium distribution|is not found|Failed to launch|ENOENT|cannot find/i
    .test(s);
}

/** 起持久化上下文：优先真 Chrome 通道，机器没装 Chrome 才回落捆绑 Chromium。
 *  headless 由调用方按「交互登录 vs restore/恢复」决定（见 startLogin）。
 *  humanWindow=true（headed 且非离屏＝真给人操作的窗口）→ viewport 跟随窗口（B64）。 */
async function launchPersistent(userDataDir, proxyUrl, headless = HEADLESS, extraArgs = [],
                                humanWindow = false) {
  try {
    return await chromium.launchPersistentContext(
      userDataDir, launchOptions(proxyUrl, BROWSER_CHANNEL, headless, extraArgs, humanWindow));
  } catch (e) {
    if (!BROWSER_CHANNEL || !isChannelUnavailable(e)) throw e;
    logger.warn({ e: String(e), channel: BROWSER_CHANNEL },
      "channel unavailable on this host → falling back to bundled chromium (weaker stealth)");
    return await chromium.launchPersistentContext(
      userDataDir, launchOptions(proxyUrl, BUNDLED_CHANNEL, headless, extraArgs, humanWindow));
  }
}

/** 登录会话 cookie 快照路径（与 profile 目录并列）。 */
function cookiesPath(loginId) {
  return path.join(SESSIONS_DIR, `${loginId}.cookies.json`);
}

/** 保存全部 cookie（含会话级）到 JSON——持久化上下文不落盘会话级 cookie（xs/c_user），
 *  必须自存自灌。会话级（expires<0）统一顶到 +1 年，重新注入时即成持久 cookie。 */
async function saveCookies(loginId, context) {
  try {
    const cookies = await context.cookies();
    if (!cookies || !cookies.length) return;
    const oneYear = Math.floor(Date.now() / 1000) + 365 * 24 * 3600;
    const norm = cookies.map((c) => ({
      ...c,
      expires: (!c.expires || c.expires < 0) ? oneYear : c.expires,
    }));
    fs.writeFileSync(cookiesPath(loginId), JSON.stringify(norm), "utf8");
  } catch (e) {
    logger.debug({ e, loginId }, "saveCookies failed");
  }
}

/** restore 时把快照 cookie 重新注入上下文（导航前调用）。 */
async function loadCookies(loginId, context) {
  try {
    const p = cookiesPath(loginId);
    if (!fs.existsSync(p)) return false;
    const cookies = JSON.parse(fs.readFileSync(p, "utf8"));
    if (Array.isArray(cookies) && cookies.length) {
      await context.addCookies(cookies);
      return true;
    }
  } catch (e) {
    logger.debug({ e, loginId }, "loadCookies failed");
  }
  return false;
}

// ── E2EE 恢复 PIN 自动恢复（P3，2026-08-05「入站半死」根治）────────────────────
// 病灶链：进程重启 → profile 丢登录态 → cookie 快照恢复登录，但 E2EE 设备密钥
// **不在 cookie 里**（存于 profile 本地存储）→ Messenger 把本机当新设备、挂出
// 「输入恢复 PIN」浮层 → 没人输 → 加密会话正文永远解不开＝半死态（登录绿、侧栏在、
// 正文读取全败）。此前唯一出路是人工重登+手输 PIN。现在：运营把 PIN 交给 worker
// 一次（落 sessions/<loginId>.secrets.json，与 cookie 快照同级同敏感度、绝不入日志），
// 之后每次恢复/自愈撞到 PIN 浮层都自动输入——半死态从「等人发现再修」变成自愈。
const E2EE_PIN_MAX_TRIES = Math.max(1, Number(process.env.MSG_E2EE_PIN_MAX_TRIES || 2));
const E2EE_PIN_COOLDOWN_MS = Math.max(
  60000, Number(process.env.MSG_E2EE_PIN_COOLDOWN_MS || 10 * 60 * 1000));

/** 账号级机密 sidecar（当前仅 e2ee_pin）。与 .cookies.json 并列、同 gitignore 语义。 */
function secretsPath(loginId) {
  return path.join(SESSIONS_DIR, `${loginId}.secrets.json`);
}

function loadSecrets(loginId) {
  try {
    const p = secretsPath(loginId);
    if (!fs.existsSync(p)) return {};
    const obj = JSON.parse(fs.readFileSync(p, "utf8"));
    return (obj && typeof obj === "object") ? obj : {};
  } catch (e) {
    logger.debug({ e, loginId }, "loadSecrets failed");
    return {};
  }
}

function saveSecrets(loginId, obj) {
  try {
    fs.writeFileSync(secretsPath(loginId), JSON.stringify(obj || {}), "utf8");
    return true;
  } catch (e) {
    logger.warn({ e: String((e && e.message) || e), loginId }, "saveSecrets failed");
    return false;
  }
}

function getE2eePin(loginId) {
  if (!loginId) return "";
  return normalizePin(loadSecrets(loginId).e2ee_pin);
}

/** 当前页面是否挂着 E2EE PIN 浮层（best-effort 不抛）。
 *
 * 两路信号取或（2026-08-10 .198 事故加固）：
 * ① 文本判据（与登录分类器同源 `isE2eePinText`）——主路；
 * ② 文案改版兜底：URL 带 e2ee/加密恢复标记 **且** 页面有 PIN 风格输入（数字/一次性码/
 *    带 PIN 标注）**且非账号登录表单**。Messenger 的 PIN 页文案时有微调，只靠 ① 可能漏判
 *    （今晚登录观察是靠 URL 标记才认出 e2ee_pin 段的，而本函数此前只看文本）；② 让检测
 *    与登录分类器的 URL 判据对齐。普通已解密线程无 PIN 输入 → 不会误判；登录/2FA 页
 *    URL 无 e2ee 标记或含账密框 → 不会误判（侧栏根页同样无 e2ee 标记，行为不变）。
 */
async function detectPinPrompt(page) {
  try {
    const info = await page.evaluate(() => {
      const txt = ((document.body && document.body.innerText) || "").slice(0, 1200);
      const loginForm = !!document.querySelector(
        'input[name="pass"], input[name="email"]');
      const pinInput = !loginForm && !!document.querySelector(
        'input[inputmode="numeric"], input[autocomplete="one-time-code"], '
        + 'input[aria-label*="PIN" i], input[name*="pin" i]');
      return { txt, url: String(location.href || ""), pinInput };
    });
    if (isE2eePinText(info.txt)) return true;
    const u = info.url.toLowerCase();
    const urlMark = /(e2ee|encryption_pin|encrypted_backup|device_password|restore_chat|secret_conversation|key_change)/.test(u);
    return !!(urlMark && info.pinInput);
  } catch (_) {
    return false;
  }
}

/**
 * B70（实施67 P1-9，`_344`/`_345` 死等实录）：托管 PIN 后**主动重放**一次。
 *
 * 旧行为＝PIN 存好后「等下次浮层出现」由心跳/读线程顺带自愈——浮层被关掉 /
 * 页面早已导航走时永远等不到，账号钉死在「入站半死+发送失败」。本函数把等待
 * 变成驱动：① 当前页浮层在场 → 就地提交（零导航零副作用）；② 不在场 → 找左栏
 * 首条 e2ee 线程链接导航过去钓浮层（副作用=该线程被标已读，vs 整账号瘫痪——
 * 两害相权），试完导航回原页；③ 全程找不到浮层 → 如实回 prompt_not_found
 * （PIN 已武装，下次浮层出现自动填）。绝不抛；结果回写 entry._pinState。
 */
async function driveE2eePinReplay(loginId, entry) {
  if (!entry || !entry.page) return { attempted: false, healed: false, state: "offline" };
  const page = entry.page;
  // B99 ②可观测：replay 全程 INFO 落日志——CUN2TM 诊断包里「零 replay 行」无法
  // 区分「没执行」vs「执行了没记」，这里把入口/结局钉成必出现的日志锚点。
  logger.info({ loginId }, "e2ee pin replay: start");
  try {
    if (await detectPinPrompt(page)) {
      const healed = await tryAutoE2eePin(loginId, entry, page).catch(() => false);
      logger.info({ loginId, healed, state: String(entry._pinState || "") },
        "e2ee pin replay: prompt on current page → submitted");
      return { attempted: true, healed, state: String(entry._pinState || "") };
    }
    const href = await page.evaluate(() => {
      const a = document.querySelector('a[href^="/e2ee/t/"]');
      return a ? String(a.getAttribute("href") || "") : "";
    }).catch(() => "");
    if (!href) {
      logger.info({ loginId }, "e2ee pin replay: no e2ee thread link → prompt_not_found");
      return { attempted: false, healed: false, state: "prompt_not_found" };
    }
    const prevUrl = String(page.url() || "");
    await page.goto(MESSENGER_URL.replace(/\/$/, "") + href,
      { waitUntil: "domcontentloaded", timeout: 20000 });
    await page.waitForTimeout(2500);
    let healed = false;
    let attempted = false;
    if (await detectPinPrompt(page)) {
      attempted = true;
      healed = await tryAutoE2eePin(loginId, entry, page).catch(() => false);
    }
    // 回原页 best-effort（回不去也无害——下一轮读线程自会导航）
    if (prevUrl && prevUrl !== String(page.url() || "")) {
      await page.goto(prevUrl, { waitUntil: "domcontentloaded", timeout: 15000 })
        .catch(() => {});
    }
    logger.info({ loginId, attempted, healed,
      state: attempted ? String(entry._pinState || "") : "prompt_not_found" },
    "e2ee pin replay: done");
    return {
      attempted, healed,
      state: attempted ? String(entry._pinState || "") : "prompt_not_found",
    };
  } catch (e) {
    logger.warn({ e: String((e && e.message) || e), loginId },
      "e2ee pin replay: failed");
    return { attempted: false, healed: false, state: "error" };
  }
}

/**
 * 尝试把已配置的恢复 PIN 输进当前浮层。返回 true=浮层已消失（密钥恢复成功）。
 * 安全护栏：① 纯函数闸门 autoPinGate 管预算/冷却（误触键盘的风险面收敛成可单测的
 * 判定）；② 只在 PIN 浮层文案命中时动手；③ 登录表单（email/pass 输入框在场）绝不碰
 * ——PIN 与账号密码是两个世界，打错地方比不打危害大；④ PIN 明文绝不入日志。
 */
async function tryAutoE2eePin(loginId, entry, page) {
  const pin = getE2eePin(loginId);
  const gate = autoPinGate({
    pin,
    tries: entry._pinTries || 0,
    lastTryTs: entry._pinLastTs || 0,
    now: Date.now(),
    maxTries: E2EE_PIN_MAX_TRIES,
    cooldownMs: E2EE_PIN_COOLDOWN_MS,
  });
  if (!gate.ok) {
    if (gate.reason === "no_pin") entry._pinState = "missing";
    return false;
  }
  if (gate.reset) entry._pinTries = 0;
  if (!(await detectPinPrompt(page))) return false;
  // B99 ③观测：上次成功恢复后 24h 内浮层又出现＝设备密钥没被 FB 认持久
  //（profile 本身是 launchPersistentContext 持久化的——若此行高频出现，嫌疑
  // 在 FB 侧对指纹/代理变化的重验，证据留给下一个诊断包）。
  if (entry._pinLastOkTs && Date.now() - entry._pinLastOkTs < 86400000) {
    logger.warn({ loginId,
      minsSinceHeal: Math.round((Date.now() - entry._pinLastOkTs) / 60000) },
    "e2ee pin prompt REAPPEARED after successful heal (device-key persistence suspect)");
  }
  const isLoginForm = await page.evaluate(
    () => !!document.querySelector('input[name="pass"], input[name="email"]'))
    .catch(() => true);
  if (isLoginForm) return false;
  entry._pinTries = (entry._pinTries || 0) + 1;
  entry._pinLastTs = Date.now();
  // P1 观测（2026-08-14）：尝试/成败此前只进日志——「托管 PIN 后有没有真自愈」
  // 要翻 sidecar 日志才知道。计数经 /accounts + 心跳出网，纯函数推进可单测。
  entry._pinHeal = pinHealBump(entry._pinHeal, "attempt");
  try {
    const input = page.locator(
      'input[type="password"]:visible, input[inputmode="numeric"]:visible, '
      + 'input[autocomplete="one-time-code"]:visible, input[aria-label*="PIN" i]:visible, '
      + 'input[name*="pin" i]:visible').first();
    if (!(await input.count())) {
      logger.warn({ loginId }, "e2ee pin prompt visible but no input located");
      entry._pinState = "failed";
      entry._pinHeal = pinHealBump(entry._pinHeal, "fail");
      return false;
    }
    await input.click({ timeout: 3000 });
    await input.fill("", { timeout: 3000 }).catch(() => {});
    await input.type(pin, { delay: 90, timeout: 10000 });
    const btn = page.locator('div[role="button"], button')
      .filter({ hasText: /^(继续|确认|確認|提交|完成|Continue|Submit|Confirm|Done|Next)$/i })
      .first();
    if (await btn.count()) {
      await btn.click({ timeout: 3000 }).catch(() => {});
    } else {
      await input.press("Enter").catch(() => {});
    }
    await page.waitForTimeout(4000);
    const stillPrompt = await detectPinPrompt(page);
    if (!stillPrompt) {
      logger.info({ loginId, tries: entry._pinTries },
        "e2ee recovery pin accepted (device keys restored)");
      entry._pinState = "ok";
      entry._pinLastOkTs = Date.now();
      entry._pinHeal = pinHealBump(entry._pinHeal, "ok");
      // 密钥恢复后旧样本全部失真：清读取窗 + 占位统计——否则本拍心跳会据「恢复前
      // 的高占位比例」误报一次半死（placeholder_blind 支），要等下一次列表读取
      // （最长 60s）才自愈。清零后即按恢复后的真实读取重新积累。
      entry._readWin = [];
      entry._convStats = { count: 0, ph: 0 };
      // Q-24 C：PIN 过了 → 自动回填该账号（历史 + 手机端发出的消息此前都读不出）
      scheduleBackfillAfterPin(entry, "pin_accepted");
      return true;
    }
    logger.warn({ loginId, tries: entry._pinTries },
      "e2ee pin attempt did not clear the prompt (wrong pin / new layout?)");
    entry._pinState = "failed";
    entry._pinHeal = pinHealBump(entry._pinHeal, "fail");
    return false;
  } catch (e) {
    logger.warn({ e: String((e && e.message) || e), loginId }, "auto e2ee pin failed");
    entry._pinState = "failed";
    entry._pinHeal = pinHealBump(entry._pinHeal, "fail");
    return false;
  }
}

/**
 * 已授权会话的半死自愈钩子（心跳节奏调用）：读取窗全败或侧栏大面积加密占位时，
 * 便宜地看一眼主页面是否挂着 PIN 浮层——在则尝试自动输入。刻意不主动导航
 * （零标已读副作用纪律）；浮层不在主页面时保持现状（人工重登路径仍在）。
 */
async function maybeAutoPinSelfHeal(entry) {
  try {
    if (!entry || entry.status !== "authorized" || !entry._loginId) return;
    const cs = entry._convStats || { count: 0, ph: 0 };
    const ratio = cs.count > 0 ? cs.ph / cs.count : -1;
    const win = entry._readWin || [];
    const fails = win.filter((ok) => !ok).length;
    const suspicious = (cs.count >= 5 && ratio >= 0.6)
      || (win.length >= 2 && fails >= win.length);
    if (!suspicious) return;
    if (!(await detectPinPrompt(entry.page))) return;
    entry._pinPromptSeen = true;
    if (!getE2eePin(entry._loginId)) { entry._pinState = "missing"; return; }
    await tryAutoE2eePin(entry._loginId, entry, entry.page);
  } catch (e) {
    logger.debug({ e }, "auto pin self-heal tick failed");
  }
}

/** 给上下文注入反检测脚本（每页文档创建前执行）。 */
async function applyStealth(context) {
  try {
    await context.addInitScript(() => {
      // 抹掉 webdriver 标识
      Object.defineProperty(navigator, "webdriver", { get: () => undefined });
      // 伪造 plugins / languages（无头/自动化常为空 → 明显特征）
      try {
        Object.defineProperty(navigator, "languages", { get: () => ["zh-CN", "zh", "en"] });
        Object.defineProperty(navigator, "plugins", { get: () => [1, 2, 3, 4, 5] });
      } catch (_) {}
      // chrome 运行时对象（真实 Chrome 有 window.chrome）
      if (!window.chrome) window.chrome = { runtime: {} };
      // 权限查询伪装
      try {
        const orig = window.navigator.permissions && window.navigator.permissions.query;
        if (orig) {
          window.navigator.permissions.query = (p) =>
            p && p.name === "notifications"
              ? Promise.resolve({ state: Notification.permission })
              : orig(p);
        }
      } catch (_) {}
    });
  } catch (e) {
    logger.debug({ e }, "applyStealth failed");
  }
}

/** 非破坏性读会话列表：直接抓左栏，取 {线程 key, 昵称, 末条预览, 头像 URL}。
 *  messenger.com 是双栏 SPA——左栏列表在打开任意会话时都常驻，故**无需导航**即可读；
 *  仅当当前不在 messenger.com 域内时才 goto 一次。绝不逐会话点进（避免标记已读/打断运营）。 */
async function readConversations(page) {
  try {
    const url = page.url() || "";
    if (!/messenger\.com/.test(url)) {
      await page.goto(MESSENGER_URL, { waitUntil: "domcontentloaded", timeout: 15000 });
      await page.waitForTimeout(1500);
    }
    const rows = await page.$$eval(SEL_CONV_LINKS, (els, re) => {
      // 正则以 source 串传入（$$eval 回调在浏览器上下文执行，闭不到 Node 常量）
      const DIRTY = new RegExp(re.dirty, "i");
      const OUTBOUND = new RegExp(re.outbound, "i");
      const TIME_ROW = /^[·•]?\s*\d+\s*(分钟?|小时|天|周|月|年|min|m|h|d|w|mo|y)\b/i;
      const seen = new Set();
      const out = [];
      for (const a of els) {
        const href = a.getAttribute("href") || "";
        // 只认数字线程 id（/t/123 或 /e2ee/t/123），排除 /marketplace/t、/requests/t、/archived/t
        const m = href.match(/\/t\/(\d+)/);
        if (!m) continue;
        // 跳过「跳到主内容」的无障碍导航锚点（?focus_target）：它不是会话行，行文本是
        // 「Chats · N unread」——不滤则：① 目录同步落一条脏名占位 ② 它按 key 去重
        // 抢先占位，把**当前打开线程**的真实侧栏行顶掉 ③ 未读数一变 sig 就变 →
        // 每次都触发一次白读线程的假候选。
        if (href.includes("focus_target")) continue;
        const key = m[1];
        if (seen.has(key)) continue;
        // 爬到承载整行的 gridcell 容器
        let row = a;
        for (let i = 0; i < 6 && row.parentElement; i++) {
          row = row.parentElement;
          if (row.getAttribute && row.getAttribute("role") === "gridcell") break;
        }
        const text = ((row.innerText || a.innerText || "") + "").trim();
        if (!text) continue; // 跳过空文本（当前打开会话的头部锚点）
        const lines = text.split("\n").map((s) => s.trim()).filter(Boolean);
        // 名字 = **首个非脏行**（2026-08-14 修「Active now 当昵称」）：DOM 改版后
        // 状态行（Active 3m ago）/导航行（Message request）可能排到第一行，旧
        // 「lines[0] 即名字」直接把它们当昵称。脏行/时间行/未读标记/出站前缀一律
        // 跳过；全部命中 → name 空串（诚实缺名，服务端回落通讯录名/chat_key）。
        let nameIdx = -1;
        for (let i = 0; i < lines.length; i++) {
          const l = lines[i];
          if (DIRTY.test(l) || TIME_ROW.test(l) || OUTBOUND.test(l)
              || /^(未读消息|Unread message)/i.test(l)) continue;
          nameIdx = i;
          break;
        }
        const name = nameIdx >= 0 ? lines[nameIdx] : "";
        // 预览 = 名字之外第一行「非分隔符/非相对时间/非行动标签/非状态行」。
        // rel = 第一个相对时间行（"3m"/"·1d"/"2 小时"）——P1 目录同步用它换算会话
        // 排序时间（认不出 → 0 占位沉底，绝不猜）。时间行判定先于预览判定，
        // 且补上纯 "m"（旧正则只认 min，"3m" 理论上可能漏进预览）。
        // 注意：出站前缀行（"你: ok"）**必须保留**为预览候选——下游靠
        // OUTBOUND_PREVIEW_RE 识别「本方已回」跳过，滤掉会误开线程。
        let preview = "";
        let rel = "";
        let unread = false;
        for (let i = 0; i < lines.length; i++) {
          if (i === nameIdx) continue;
          const l = lines[i];
          if (/^(未读消息|Unread message)/i.test(l)) { unread = true; continue; }
          if (TIME_ROW.test(l)) {
            if (!rel) rel = l.replace(/^[·•]\s*/, "");
            continue;
          }
          if (DIRTY.test(l)) continue;
          if (!preview) preview = l;
        }
        // 头像抓取（冗余兜底，防单一选择器随 messenger 改版失配 → 头像整片丢）：
        //   ① 优先 FB CDN（fbcdn/scontent）真头像；② 退而取行内任意 http(s) 图；③ 都无→空。
        const avatar = (function () {
          const imgs = row.querySelectorAll("img");
          for (const im of imgs) {
            const s = im.getAttribute("src") || "";
            if (/fbcdn|scontent/i.test(s)) return s;
          }
          for (const im of imgs) {
            const s = im.getAttribute("src") || "";
            if (/^https?:/i.test(s)) return s;
          }
          return "";
        })();
        seen.add(key);
        out.push({ key, name, preview, avatar, rel, unread });
      }
      return out.slice(0, 200);
    }, { dirty: DIRTY_NAME_RE.source, outbound: OUTBOUND_PREVIEW_RE.source });
    return rows || [];
  } catch (e) {
    logger.debug({ e }, "readConversations failed");
    return [];
  }
}

// 请求文件夹地址（陌生人首次来讯落这里，不在主列表）。
const REQUESTS_URL = MESSENGER_URL.replace(/\/$/, "") + "/requests/";
// 预览里「未读消息：」「Unread message:」等前缀 → 清洗掉只留正文。
const PREVIEW_PREFIX_RE = /^(未读消息[:：]\s*|Unread message[:：]?\s*)/;
// 是否抓「垃圾信息」子 tab（FB 判为垃圾的陌生请求）。默认开；置 0 只抓「可能认识」。
const MSG_SPAM_REQUESTS = String(process.env.MSG_SPAM_REQUESTS ?? "1") !== "0";
// 每 N 次「请求文件夹轮询」才顺带扫一次「垃圾信息」子 tab（此前每轮都切 tab 往返；切 tab 也是一次
// 操作，拉稀到每 N 次即可进一步降载）。默认 4；置 0 或关闭 MSG_SPAM_REQUESTS 则不扫 spam。
const MSG_SPAM_EVERY = Math.max(0, Number(process.env.MSG_SPAM_EVERY ?? 4));

/** 抓当前「消息请求」视图（左栏列表）里的请求行。纯 DOM 提取，绝不抛。
 *  返回 [{key, name, preview, avatar}]。 */
async function scrapeRequestRows(page) {
  try {
    return await page.$$eval(
      'a[href*="/requests/t/"], a[href*="/e2ee/requests/t/"]', (els, re) => {
        // 与 readConversations 同款取名护栏（2026-08-14）：请求区此前连预览级状态词
        // 过滤都没有，"Message request"/"Active 3m ago" 直接当昵称是本区最高发。
        const DIRTY = new RegExp(re.dirty, "i");
        const OUTBOUND = new RegExp(re.outbound, "i");
        const TIME_ROW = /^[·•]?\s*\d+\s*(分钟?|小时|天|周|月|年|min|m|h|d|w|mo|y)\b/i;
        const seen = new Set();
        const out = [];
        for (const a of els) {
          const href = a.getAttribute("href") || "";
          const m = href.match(/\/t\/(\d+)/);
          if (!m) continue;
          const key = m[1];
          if (seen.has(key)) continue;
          let row = a;
          for (let i = 0; i < 6 && row.parentElement; i++) {
            row = row.parentElement;
            if (row.getAttribute && row.getAttribute("role") === "gridcell") break;
          }
          const text = ((row.innerText || a.innerText || "") + "").trim();
          if (!text) continue;
          const lines = text.split("\n").map((s) => s.trim()).filter(Boolean);
          let nameIdx = -1;
          for (let i = 0; i < lines.length; i++) {
            const l = lines[i];
            if (DIRTY.test(l) || TIME_ROW.test(l) || OUTBOUND.test(l)
                || /^(未读消息|Unread message)/i.test(l)) continue;
            nameIdx = i;
            break;
          }
          const name = nameIdx >= 0 ? lines[nameIdx] : "";
          let preview = "";
          for (let i = 0; i < lines.length; i++) {
            if (i === nameIdx) continue;
            const l = lines[i];
            // 「未读消息：<正文>」承载真实内容（下游 PREVIEW_PREFIX_RE 剥前缀），
            // 必须保留为预览；裸「未读消息」标记则落入 DIRTY 跳过。
            if (/^(未读消息|Unread message)[:：]\s*\S/i.test(l)) { preview = l; break; }
            if (TIME_ROW.test(l) || DIRTY.test(l)) continue;
            preview = l;
            break;
          }
          const img = row.querySelector('img[src*="fbcdn"]');
          const avatar = img ? (img.getAttribute("src") || "") : "";
          seen.add(key);
          out.push({ key, name, preview, avatar });
        }
        return out.slice(0, 100);
      }, { dirty: DIRTY_NAME_RE.source, outbound: OUTBOUND_PREVIEW_RE.source });
  } catch (_) {
    return [];
  }
}

/** 切到指定 tab（Messenger 请求页用 role=tab 的 SPA 子视图，就地换列表、不改 URL）。
 *  names 依次尝试（中/英），命中即点，返回是否点到。绝不抛。 */
async function clickRequestTab(page, names) {
  for (const name of names) {
    try {
      const loc = page.getByRole("tab", { name });
      if (await loc.count()) { await loc.first().click({ timeout: 3000 }); return true; }
    } catch (_) {}
  }
  return false;
}

/** 探测 /requests/ 是否被 FB 临时风控封禁（扳手错误页：「你暂时被禁止使用此功能 / 似乎你过度
 *  使用了此功能」/ English "You're temporarily blocked … misusing this feature by going too fast"）。
 *  命中 → 调用方进入退避冷却，停止再导航/切 tab/点开，避免续命甚至加重封禁。绝不抛。
 *  为降误报：仅当命中文案「且」页面无任何真实请求线程链接时才判封禁（预览恰好含关键词不误伤）。 */
const REQUESTS_BLOCK_RE = /暂时被禁止使用此功能|过度使用了此功能|暂时被阻止|temporarily blocked|misusing this feature|going too fast/i;
async function isRequestsBlocked(page) {
  try {
    return await page.evaluate((reSrc) => {
      const re = new RegExp(reSrc, "i");
      const txt = (document.body && document.body.innerText) || "";
      if (!re.test(txt)) return false;
      const hasRows = document.querySelector(
        'a[href*="/requests/t/"], a[href*="/e2ee/requests/t/"]');
      return !hasRows;
    }, REQUESTS_BLOCK_RE.source);
  } catch (_) {
    return false;
  }
}

/** 读「消息请求」文件夹（陌生人首次来讯），抓两个子 tab：
 *   ①「可能认识」(FB 已滤掉明显垃圾) → category=general（允许人设自动回）；
 *   ②「垃圾信息」                    → category=spam   （只进收件箱、不自动回）。
 *  用专用页签常驻，不打扰主收件箱页。best-effort、绝不抛。 */
async function readRequests(reqPage, entry = null) {
  try {
    // 每轮都重新 goto（比 SPA reload 更可靠地拉到最新未读）；等请求链接出现或超时兜底。
    await reqPage.goto(REQUESTS_URL, { waitUntil: "domcontentloaded", timeout: 15000 });
    await reqPage.waitForSelector(
      'a[href*="/requests/t/"], a[href*="/e2ee/requests/t/"]',
      { timeout: 4000 }).catch(() => {});
    // 风控封禁探测（放在等待之后，确保 SPA 已渲染错误页文案）：命中 → 立刻返回 blocked，
    // 绝不再切 tab / 等待 / 点开，交由调用方进入退避冷却。
    if (await isRequestsBlocked(reqPage)) {
      return { blocked: true, rows: [], structSeen: true };
    }
    await reqPage.waitForTimeout(1200);
    // 结构证据（P2 2026-08-15）：页面是否渲染出请求页骨架（子 tab / 标题 / 空态文案）。
    // 「零行 + 有骨架」＝文件夹真的空；「零行 + 无骨架」＝改版/漂移可疑（classifyRequestsScan）。
    const structSeen = await reqPage.evaluate(() => {
      const t = (document.body && document.body.innerText) || "";
      return /可能认识|You may know|垃圾信息|\bSpam\b|消息请求|Message requests?|没有消息请求|No message requests?/i.test(t);
    }).catch(() => false);
    // ① 默认视图＝「可能认识」
    const mayKnow = await scrapeRequestRows(reqPage);
    // ② 切「垃圾信息」子 tab 抓 spam（就地换列表）；抓完切回，避免详情态残留。
    //    降载：不再每轮都切 tab，改为首轮 + 每 MSG_SPAM_EVERY 次请求轮询才扫一次 spam。
    let spam = [];
    const pollCount = entry ? (entry._reqPollCount = (entry._reqPollCount || 0) + 1) : 1;
    const doSpam = MSG_SPAM_REQUESTS && MSG_SPAM_EVERY > 0 &&
      (pollCount === 1 || pollCount % MSG_SPAM_EVERY === 0);
    if (doSpam && (await clickRequestTab(reqPage, ["垃圾信息", "Spam"]))) {
      await reqPage.waitForTimeout(1500);
      spam = await scrapeRequestRows(reqPage);
      await clickRequestTab(reqPage, ["可能认识", "You may know"]);
    }
    const spamKeys = new Set(spam.map((r) => r.key));
    const norm = (r, category) => ({
      ...r,
      preview: (r.preview || "").replace(PREVIEW_PREFIX_RE, "").trim(),
      isRequest: true,
      category,
    });
    const out = [];
    // spam key 优先归 spam（防同一线程在两 tab 都出现时误判为可自动回）
    for (const r of mayKnow) { if (!spamKeys.has(r.key)) out.push(norm(r, "general")); }
    for (const r of spam) out.push(norm(r, "spam"));
    return { blocked: false, rows: out, structSeen };
  } catch (e) {
    logger.debug({ e }, "readRequests failed");
    // 导航/求值异常＝无结构证据 → 上游按 suspect 计（与「真空文件夹」区分）
    return { blocked: false, rows: [], structSeen: false };
  }
}

/** 入站轮询：抓左栏列表预览，把「新出现的对端来信」push 进 Python。best-effort、绝不抛。
 *  非破坏性——只读列表，不点进会话（不改已读态、不打断运营手动操作）。
 *  首轮只建立 seen 基线不上报（避免把历史会话末条全当新消息灌进来）。 */
async function pollInbound(entry) {
  if (!MSG_SYNC || !PY_INGEST_URL || !entry.accountId || entry.status !== "authorized") return;
  if (entry._polling) return;
  // P3：写操作（send/react）进行中 → 本 tick 礼让（轮询是只读 $$eval 不抢焦点，
  // 但 entry.page 上的 DOM 读在写操作 hover/导航期间多为白读；下个 tick 自然补上）。
  if (entry._opLock) return;
  entry._polling = true;
  entry._readUsed = false;   // 本轮是否读过线程（用过 readPage）→ 决定 finally 是否回收读页
  try {
    const firstPass = !entry._baselined;
    // 主收件箱列表（常规会话）
    const convs = await readConversations(entry.page);
    // 消息请求（陌生人首次来讯，不在主列表）：专用页签常驻 /requests/。
    // P2 自适应节奏：底数 MSG_REQ_EVERY + 空转拉长（adaptiveReqEvery）+ 封禁指数退避。
    // 冷却窗口内完全不导航 /requests/，冷却结束自动恢复。
    let requests = [];
    entry._reqTick = (entry._reqTick || 0) + 1;
    const inCooldown = entry._reqBlockedUntil && Date.now() < entry._reqBlockedUntil;
    const reqEvery = adaptiveReqEvery({
      baseEvery: MSG_REQ_EVERY,
      emptyStreak: entry._reqEmptyStreak || 0,
      blocked: !!inCooldown,
    });
    entry._reqEveryEffective = reqEvery; // /accounts 观测
    const reqDue = MSG_REQUESTS && (firstPass || entry._reqTick % reqEvery === 0);
    if (reqDue && inCooldown) {
      logger.debug(
        { accountId: entry.accountId, until: entry._reqBlockedUntil },
        "requests poll skipped (block cooldown)");
    } else if (reqDue) {
      try {
        if (!entry.reqPage || entry.reqPage.isClosed()) {
          entry.reqPage = await entry.context.newPage();
        }
        const res = await readRequests(entry.reqPage, entry);
        if (res.blocked) {
          entry._reqBlockStreak = (entry._reqBlockStreak || 0) + 1;
          const cd = Math.min(
            MSG_REQ_BLOCK_MAX_MS,
            MSG_REQ_BLOCK_COOLDOWN_MS * Math.pow(2, entry._reqBlockStreak - 1));
          entry._reqBlockedUntil = Date.now() + cd;
          logger.warn(
            { accountId: entry.accountId, cooldownMs: cd, streak: entry._reqBlockStreak },
            "messenger requests folder temporarily blocked by FB; backing off");
        } else {
          requests = res.rows;
          if (requests.length) entry._reqEmptyStreak = 0;
          else entry._reqEmptyStreak = (entry._reqEmptyStreak || 0) + 1;
          // P2 2026-08-15 空转判别：零行分「真空（有页面骨架）」与「可疑（无任何
          // 结构证据=漂移/导航失败）」。suspect 连续累计，rows/empty_ok 即清零；
          // streak 每 8 次 warn 一条（节流），读数经 /accounts + 心跳出网。
          const scan = classifyRequestsScan({
            blocked: false, rowCount: requests.length,
            structSeen: !!res.structSeen,
          });
          if (scan === "suspect") {
            entry._reqSuspectStreak = (entry._reqSuspectStreak || 0) + 1;
            if (entry._reqSuspectStreak % 8 === 1) {
              logger.warn(
                { accountId: entry.accountId, streak: entry._reqSuspectStreak },
                "requests folder scan suspect (no rows, no structural evidence)");
            }
          } else {
            entry._reqSuspectStreak = 0;
          }
          if (entry._reqBlockStreak) {
            logger.info(
              { accountId: entry.accountId },
              "messenger requests folder recovered; block cooldown cleared");
          }
          entry._reqBlockStreak = 0;
          entry._reqBlockedUntil = 0;
        }
      } catch (e) { logger.debug({ e }, "requests poll failed"); }
    }
    // P1 侧栏统计（占位盲区信号原料）：E2EE 加密占位预览占比 → 心跳携带。
    try {
      const ph = convs.filter((c) => E2EE_PLACEHOLDER_RE.test(c.preview || "")).length;
      entry._convStats = { count: convs.length, ph };
    } catch (_) { /* 统计失败不影响轮询 */ }
    // P1 目录同步：首轮全量推送会话占位，之后每 MSG_DIR_SYNC_EVERY 轮且指纹变化才推。
    try {
      entry._dirTick = (entry._dirTick || 0) + 1;
      if (firstPass || entry._dirTick % MSG_DIR_SYNC_EVERY === 0) {
        await postDirectory(entry, convs, { force: firstPass });
      }
    } catch (_) { /* 目录同步失败不影响轮询 */ }
    const all = convs.concat(requests);
    // 头像直链缓存：供 GET /accounts/:id/avatar 按需返回（不额外导航/点进会话）。每轮用当前
    // 列表整体刷新 → 天然有界 且直链保持新鲜（scontent token 有时效，Python 取后立即
    // 下载落 /static 稳定托管）。
    const ac = new Map();
    for (const c of all) { if (c.avatar) ac.set(c.key, c.avatar); }
    entry.avatarCache = ac;
    if (!entry.lastInboundSig) entry.lastInboundSig = new Map();

    // ── 目录同步（P1）：侧

    // ── 主列表会话：变更探测 → 权威「进线程读正文」───────────────────────────────
    const candidates = [];
    const manualOutCands = []; // 手发出站镜像候选（优先级最低，见下方装配）
    for (const c of convs) {
      const preview = (c.preview || "").trim();
      if (!preview) continue;
      const sig = `${c.key}:${preview.slice(0, 160)}`;
      if (entry.seen.get(c.key) === sig) continue; // 预览无变化 → 无新活动
      if (firstPass) { entry.seen.set(c.key, sig); continue; } // 首轮仅建基线
      const _nm = (c.name || "").trim();
      // 便宜预筛（不必进线程即可判定「非对端新消息」）：本方近期自发（自回声）、明显 "你:" 出站、
      // 状态行/名字误抓噪声 → 直接推进 seen 跳过，避免无谓导航（也降风控/减少标已读）。
      // P2：本方撤回预览 → 挂 revoke 到最近出站 synth id（不再进线程，零标已读副作用）。
      if (isUnsentPreview(preview)) {
        const target = entry._lastOutboundId && entry._lastOutboundId.get(c.key);
        if (target) {
          postMessageOp(entry, { chat_key: c.key, target_id: target, op: "revoke" })
            .catch(() => {});
        }
        entry.seen.set(c.key, sig);
        continue;
      }
      if (isSelfEcho(entry, c.key, preview)
          || DIRTY_NAME_RE.test(_nm) || STATUS_LINE_RE.test(preview)
          || (normPreview(preview) === normPreview(_nm) && normPreview(_nm))) {
        entry.seen.set(c.key, sig);
        continue;
      }
      // "你:/You:" 出站预览（2026-08-16 分流）：本 worker 自发已被上面 isSelfEcho
      // 拦下（发送时同步 recordSent），走到这里的出站预览＝**别的登录端手发**
      // （手机 App / 原生网页）。旧行为直接跳过 → 手发消息在坐席端隐形（要等客户
      // 回复才顺路回流，首条还会被水位基线吞掉）。现改为：进线程镜像回流
      //（manualOut 候选，优先级最低）；镜像环二次核对防旧自发漏网误灌。
      if (OUTBOUND_PREVIEW_RE.test(preview)) {
        if (MSG_MANUAL_OUT_SYNC
            && !sentRingHit(entry.mirrorSentRing, c.key, normPreview(preview))) {
          manualOutCands.push({ c, sig, manualOut: true });
        } else {
          entry.seen.set(c.key, sig);
        }
        continue;
      }
      // 有变更且疑似对端 → 候选（含 E2EE 占位预览：列表读不到，但进线程能读到正文）。
      candidates.push({ c, sig });
    }

    // ── P0 未读驱动强制读取（2026-08-15，E2EE 占位盲区解药）───────────────────
    // 预览指纹是唯一变更触发源 → E2EE 占位预览永不变化 → 客户新消息隐形（当晚实锤：
    // 侧栏未读=2 挂 30+ 分钟、read_attempts 纹丝不动、两条真实消息丢失）。这里把
    // 「有未读证据但没进常规候选」的线程追加到候选**尾部**（低于真 sig 变化的优先级、
    // 共享 MSG_MAX_OPENS 预算=总导航量不增），走完全同一条 读取→自愈→ingest 管线；
    // 读不出正文也给 readThreadTail 内的 E2EE PIN 就地自愈一次机会。
    if (MSG_UNREAD_FORCE && !firstPass && MSG_READ_THREAD) {
      if (!entry._unreadForceLog) entry._unreadForceLog = new Map();
      const forcedPicks = pickUnreadForced(convs, {
        candidateKeys: new Set(candidates.map((x) => x.c.key)),
        attemptLog: entry._unreadForceLog,
        now: Date.now(),
        globalUnread: Number(entry._lastUnread || 0),
        cooldownMs: MSG_UNREAD_FORCE_COOLDOWN_MS,
        cap: MSG_UNREAD_FORCE_CAP,
        freshMs: MSG_UNREAD_FRESH_MS,
        placeholderRe: E2EE_PLACEHOLDER_RE,
        relToTs: parseRelativeTime,
      });
      for (const p of forcedPicks) {
        const c = convs.find((x) => x && x.key === p.key);
        if (!c) continue;
        entry._unreadForceLog.set(p.key, Date.now());
        // 冷却账本有界化：只留最近 64 条（会话数级别，防长跑泄漏）
        while (entry._unreadForceLog.size > 64) {
          entry._unreadForceLog.delete(entry._unreadForceLog.keys().next().value);
        }
        if (!entry._unreadForceStats) {
          entry._unreadForceStats = { picked: 0, row_unread: 0, fresh_placeholder: 0 };
        }
        entry._unreadForceStats.picked += 1;
        entry._unreadForceStats[p.why] = (entry._unreadForceStats[p.why] || 0) + 1;
        const sig = `${c.key}:${(c.preview || "").trim().slice(0, 160)}`;
        candidates.push({ c, sig, forced: p.why });
        logger.info({ accountId: entry.accountId, key: p.key, why: p.why },
          "unread-forced read queued");
      }
    }

    // ── 手发出站镜像候选装配（2026-08-16）───────────────────────────────────
    // 追加在队尾＝真实入站/未读兜底永远先用 MSG_MAX_OPENS 预算；单轮
    // MSG_MANUAL_OUT_CAP 封顶；没排上的不推进 seen → 预览指纹仍算「有变更」，
    // 下轮自然重试，不丢。
    if (MSG_MANUAL_OUT_SYNC && !firstPass && MSG_READ_THREAD && manualOutCands.length) {
      for (const mo of manualOutCands.slice(0, MSG_MANUAL_OUT_CAP)) {
        candidates.push(mo);
        bumpOp(entry, "manual_out_reads");
        logger.info({ accountId: entry.accountId, key: mo.c.key },
          "manual-out mirror read queued");
      }
    }

    if (!firstPass && MSG_READ_THREAD) {
      // 进线程按无障碍标签读**方向权威 + 全文 + 真实发送者**，据「最后一条对端消息」上报；
      // 本方出站永不推进 lastInboundSig → 从根上杜绝自回复。每轮限流 MSG_MAX_OPENS。
      let opened = 0;
      for (const { c, sig, manualOut } of candidates) {
        if (opened >= MSG_MAX_OPENS) break; // 超额留待下轮（seen 未推进→自然重试）
        opened++;
        // 副作用清单：inbound 允许开线程（「正在处理」）；见 canOpenThread。
        if (!canOpenThread({ purpose: "inbound", unread: !!c.unread }).ok) continue;
        const tail = await readThreadTail(entry, c.key);
        // 半死态探测采样：占位预览行的空读=skip（锁死旧线程非管线故障证据）
        recordThreadRead(entry, tail, { key: c.key,
          previewPlaceholder: E2EE_PLACEHOLDER_RE.test(c.preview || "") });
        if (tail === null) continue; // 渲染未就绪/失败 → 不推进 seen，下轮重试
        entry.seen.set(c.key, sig); // 已成功读取（含空）→ 推进变更基线
        // P2：表情 / 撤回墓碑（不二次导航）
        try { await processThreadOps(entry, c.key, tail); } catch (_) { /* best-effort */ }
        // 驾驶舱 P2：人工出站回流（顺路镜像，先于下方入站上报——AI 拟稿历史才完整）
        // manualOut 触发的读取带触发预览（首观察也能镜像触发那条，见 pickManualOutMirror）。
        await mirrorManualOutbound(entry, c.key, tail, {
          manualTrigger: !!manualOut,
          rowPreviewNorm: normPreview((c.preview || "").trim()),
          rowName: (c.name || "").trim(),
        });
        // 只在**整段会话的最后一条**是对端消息时才回：若最后一条是本方发出（我们已回过），
        // 绝不回；这也确定性杜绝了「回自己」——本方出站永远不会成为待回的 last。
        const last = tail.length ? tail[tail.length - 1] : null;
        // 最后一条须是对端消息，且有正文**或媒体**（媒体气泡可无正文）。
        if (!last || last.direction !== "in" || (!last.text && !last.media_ref)) continue;
        // 加密横幅 / 撤回墓碑非真消息 → 不入库（无媒体时）。
        if (last.text && !last.media_ref && (E2EE_PLACEHOLDER_RE.test(last.text)
            || isUnsentTombstone(last.text))) continue;
        // P1 burst 修复：客户两轮之间连发多条时不再只报最后一条。
        // 定位「上次已上报的那条」在窗口内的位置 → 取其后全部对端消息；定位不到
        //（首次/超出 15 条窗/上次那条被撤回）→ **保守回退旧行为只报最后一条**——
        // 末尾连续 in 段可能混着 baseline 之前的老消息（客户上周发的没人回 + 今天
        // 新发一条），全量上报会把老消息当新消息灌进来。锚点自本轮建立，之后的
        // 连发即可全量捕获。
        const sigOf = (m) => `${normPreview(m.text)}|${m.media_ref || ""}|${m.ts}`;
        const prevSig = entry.lastInboundSig.get(c.key) || "";
        let fresh = [];
        if (prevSig) {
          let startIdx = -1;
          for (let i = tail.length - 1; i >= 0; i--) {
            if (tail[i].direction === "in" && sigOf(tail[i]) === prevSig) {
              startIdx = i;
              break;
            }
          }
          if (startIdx >= 0) {
            fresh = tail.slice(startIdx + 1).filter((m) => m.direction === "in");
          }
        }
        if (!fresh.length) fresh = [last];
        // 逐条护栏（与旧单条路径同口径）+ 单轮上限防异常长窗灌爆（MSG_BURST_MAX）
        fresh = fresh.filter((m) => (m.text || m.media_ref)
          && !(m.text && !m.media_ref && (E2EE_PLACEHOLDER_RE.test(m.text)
            || isUnsentTombstone(m.text))))
          .slice(-MSG_BURST_MAX);
        if (!fresh.length) continue;
        const newSig = sigOf(fresh[fresh.length - 1]);
        if (entry.lastInboundSig.get(c.key) === newSig) continue; // 已上报过
        entry.lastInboundSig.set(c.key, newSig);
        // P2：跨轮询累计发言人——安静群（单轮只有一人说话）也能在第二位发言人
        // 出现时升群；excludeName=列表行名挡住 DM（含改名）误升。进程内状态，
        // worker 重启后重新累计（store 侧 chat_type 非 private 粘住，已升的不回退）。
        if (!entry._inboundSenders) entry._inboundSenders = new Map();
        const _senderRoster = updateSenderRoster(
          entry._inboundSenders, c.key,
          tail.filter((row) => row && row.direction === "in").map((row) => row.sender),
          { excludeName: c.name || "" },
        );
        const groupBySenders = inferGroupFromInboundSenders([..._senderRoster]);
        for (const m of fresh) {
          // P2：稳定 msg_id——表情/撤回按 platform_msg_id 挂载的前提
          const mid = stampMsgId(entry, c.key, m);
          await postIngest({
            platform: "messenger",
            account_id: entry.accountId,
            chat_key: c.key,
            // 群：会话标题用列表名，发言人走 sender_name（勿把群名改成最后说话的人）
            name: groupBySenders ? (c.name || m.sender || "") : (m.sender || c.name || ""),
            sender_name: m.sender || "",
            ...(groupBySenders ? { chat_type: "group" } : {}),
            avatar_url: c.avatar || "",
            text: m.text, // 未截断全文（媒体可为空）
            media_type: m.media_type || "",
            media_ref: m.media_ref || "",
            ts: Math.floor(Date.now() / 1000),
            msg_id: mid,
            direction: "in",
            is_request: false,
            request_category: "",
          });
          entry._lastInboundOkTs = Date.now(); // 半死态探测：最近一次成功入站
        }
      }
    } else if (!firstPass) {
      // 回落：MSG_READ_THREAD=0 → 旧的「列表预览直报」（前面预筛已挡掉出站/自回声/噪声）。
      // P4：预览路径也写 synth msg_id（与请求区回落同口径）——空 id 会让表情/撤回
      // 永远挂不上；指纹与进线程 stampMsgId 在「无 tsLabel」时对同正文一致。
      for (const { c, sig } of candidates) {
        entry.seen.set(c.key, sig);
        if (E2EE_PLACEHOLDER_RE.test(c.preview || "")) continue; // 占位不可读→不报
        const previewText = (c.preview || "").trim();
        const previewMid = synthMsgId({
          chatKey: c.key, direction: "in", tsLabel: "", text: previewText, mediaRef: "",
        });
        await postIngest({
          platform: "messenger", account_id: entry.accountId, chat_key: c.key,
          name: c.name || "", avatar_url: c.avatar || "", text: previewText,
          ts: Math.floor(Date.now() / 1000), msg_id: previewMid, direction: "in",
          is_request: false, request_category: "",
        });
        entry._lastInboundOkTs = Date.now();
      }
    }

    // ── 消息请求（陌生人首次来讯）：变更探测 → 权威进线程读全文（保 category）+ 读不到回落预览 ──
    // 「打开≠接受」已真号联调确认（仅导航读取不接受/不移出请求箱）。读到全文 → 人设化首复更准；
    // 读到空/仍是 E2EE 加密横幅 → 按旧行为不入库（横幅非真消息）。限流独立 reqOpened。
    let reqOpened = 0;
    for (const c of requests) {
      const preview = (c.preview || "").trim();
      if (!preview) continue;
      const sig = `${c.key}:${preview.slice(0, 160)}`;
      if (entry.seen.get(c.key) === sig) continue;
      if (firstPass) { entry.seen.set(c.key, sig); continue; }
      const _nm = (c.name || "").trim();
      // 噪声护栏（请求本是入站，出站/自回声一般不命中，保留防御）→ 推进 seen 跳过
      if (OUTBOUND_PREVIEW_RE.test(preview) || isSelfEcho(entry, c.key, preview)
          || DIRTY_NAME_RE.test(_nm) || STATUS_LINE_RE.test(preview)
          || (normPreview(preview) === normPreview(_nm) && normPreview(_nm))) {
        entry.seen.set(c.key, sig);
        continue;
      }
      const cat = c.category || "general"; // general 可自动回 / spam 仅入收件箱
      let text = preview, name = c.name || "", mType = "", mRef = "";
      let reqMsgId = "";
      if (MSG_READ_THREAD && MSG_READ_REQUESTS && MSG_MAX_REQ_OPENS > 0) {
        if (reqOpened >= MSG_MAX_REQ_OPENS) continue; // 超额：不推进 seen，下次请求扫描重试
        if (!canOpenThread({ purpose: "request_read", isRequest: true }).ok) continue;
        reqOpened++;
        const tail = await readThreadTail(entry, c.key);
        // 请求区读取同样入窗（同占位/冷却语义——认不出的请求页反复 null 不垄断窗口）
        recordThreadRead(entry, tail, { key: c.key,
          previewPlaceholder: E2EE_PLACEHOLDER_RE.test(c.preview || "") });
        if (tail === null) continue; // 读失败→不推进 seen，下轮重试
        try { await processThreadOps(entry, c.key, tail); } catch (_) { /* best-effort */ }
        const last = tail.length ? tail[tail.length - 1] : null;
        if (last && last.direction === "in" && (last.text || last.media_ref)
            && !isUnsentTombstone(last.text)) {
          text = last.text; name = last.sender || name; // 未截断全文 + 真实发送者
          mType = last.media_type || ""; mRef = last.media_ref || ""; // 首讯即媒体
          reqMsgId = stampMsgId(entry, c.key, last);
        }
        // 读到空（E2EE 不可读）→ text 保留 preview 回落，交由下方占位判定
      }
      entry.seen.set(c.key, sig);
      // 无正文且无媒体，或仍是加密横幅 → 非真消息，不入库
      if ((!text && !mRef) || (text && (E2EE_PLACEHOLDER_RE.test(text)
          || isUnsentTombstone(text)))) continue;
      const inSig = `${normPreview(text)}|${mRef}`;
      if (entry.lastInboundSig.get(c.key) === inSig) continue; // 该请求正文已上报过
      entry.lastInboundSig.set(c.key, inSig);
      if (!reqMsgId) {
        reqMsgId = synthMsgId({
          chatKey: c.key, direction: "in", tsLabel: "", text, mediaRef: mRef,
        });
      }
      await postIngest({
        platform: "messenger",
        account_id: entry.accountId,
        chat_key: c.key,
        name, // 真实发送者（进线程读到时更准）
        avatar_url: c.avatar || "",
        text, // 读到→未截断全文；读不到→列表预览
        media_type: mType,
        media_ref: mRef,
        ts: Math.floor(Date.now() / 1000),
        msg_id: reqMsgId,
        direction: "in",
        is_request: true, // 陌生人首次来讯（消息请求）→ 供前端识别新客户/待接受
        request_category: cat,
      });
    }
    if (firstPass) {
      entry._baselined = true;
      // P1 首连历史回填：top-N 会话进队列，之后每 tick 消化 1 条（节奏与正常轮询
      // 一致，不制造导航尖峰；E2EE 读不出的线程 readThreadTail 为空 → 自然跳过）。
      // 本进程只建一次队列；重启后重建也无害——Python 侧「空会话才收」幂等。
      // ⚠ **跳过未读线程**（2026-08-05 实弹教训）：打开线程＝FB 侧标已读。首版把
      // 19 条线程全开了一遍，未读数 5→0——客户看到「已读不回」、侧栏未读被清、
      // 半死态判定的 unread>=1 前提也被拆掉。未读线程留给实时链路（有新消息才打开，
      // 那是「正在处理」语义）；回填只碰已读旧会话（坐席要的上下文本来就在这里）。
      if (MSG_BACKFILL > 0 && PY_THREAD_HISTORY_URL && !entry._backfillQueue) {
        // 副作用清单：backfill 绝不碰未读（canOpenThread + 显式 filter 双闸）
        entry._backfillQueue = convs
          .filter((c) => canOpenThread({ purpose: "backfill", unread: !!c.unread }).ok)
          .slice(0, MSG_BACKFILL)
          .map((c) => ({ key: c.key, name: c.name, avatar: c.avatar }));
        if (entry._backfillQueue.length) {
          logger.info({ accountId: entry.accountId,
                        n: entry._backfillQueue.length }, "backfill queued");
        }
      }
    }
    // Q-24 C（#298）：PIN 刚通过 → 重建回填队列（此前队列消化的全是加密占位空读）。
    // 同首连口径：只碰已读会话；Python 侧 thread-history 幂等，重灌无害。
    if (!firstPass && entry._backfillRebuildPending && MSG_BACKFILL > 0 && PY_THREAD_HISTORY_URL) {
      entry._backfillRebuildPending = false;
      entry._backfillQueue = convs
        .filter((c) => canOpenThread({ purpose: "backfill", unread: !!c.unread }).ok)
        .slice(0, MSG_BACKFILL)
        .map((c) => ({ key: c.key, name: c.name, avatar: c.avatar }));
      entry._backfillRetry = null;
      logger.info({ accountId: entry.accountId, n: entry._backfillQueue.length },
        "backfill queue rebuilt after e2ee pin passed");
    }
    // P1 首连回填推进 + 预热提速：每 tick 串行消化一小批（batch 条，非并发），把稀疏窗口
    // 压短（20 条 batch=3 ≈ 7 tick）。串行复用同一 readPage、条间 GAP 小憩，导航形态与逐条
    // 一致、只是更密；_polling 再入闸保证本 tick 变长不与下一 tick 重叠。设 batch=1 即旧行为。
    if (!firstPass && entry._backfillQueue && entry._backfillQueue.length) {
      const batch = warmBackfillBatch(entry._backfillQueue.length, BACKFILL_WARM_BATCH);
      for (let i = 0; i < batch && entry._backfillQueue && entry._backfillQueue.length; i++) {
        try { await backfillStep(entry); } catch (_) { /* 单条失败不影响轮询 */ }
        if (i < batch - 1 && BACKFILL_WARM_GAP_MS > 0) {
          await new Promise((r) => setTimeout(r, BACKFILL_WARM_GAP_MS));
        }
      }
    } else if (!firstPass && entry._backfillQueue && !entry._backfillQueue.length
               && entry._backfillRetry && entry._backfillRetry.length) {
      // 主队排空 → 换入「E2EE 读空重试队」补读一轮（backfillStep 里 _retried 钉死单次，
      // 重试仍空读则彻底放弃交实时链路）。到这里离首读已隔主队整个排空期，解密多半已跟上。
      entry._backfillQueue = entry._backfillRetry;
      entry._backfillRetry = null;
      logger.info({ accountId: entry.accountId,
                    n: entry._backfillQueue.length }, "backfill retry queued");
    }
    // 高频刷新 cookie 快照（xs 会轮换）：每 2 轮≈8s 存一次。此前 60s 一次 → 崩溃/被强杀时快照
    // 常滞后于已轮换的 xs，自愈 restore 灌回过期 xs 即登出。8s 窗口内 xs 几乎不会已失效。
    entry._pollCount = (entry._pollCount || 0) + 1;
    if (entry._loginId && entry._pollCount % 2 === 0) {
      await saveCookies(entry._loginId, entry.context);
    }
    // 轮询健康信号：登录态确认（cookie 未失效）+ 时间戳。用于 Python 侧 healthy() 判活，
    // 修「cookie 失效但列表仍显示 authorized」的假健康（此前 /accounts 只看 status 字段）。
    // 每 ~15 轮(≈60s)做一次 pageLoggedIn 复检，避免每轮都跑 DOM 查询。
    try {
      if ((entry._pollCount || 0) % 15 === 0) {
        const wasLoggedIn = entry._loggedIn !== false;
        let li = await pageLoggedIn(entry.page);
        // P3 修：E2EE 恢复 PIN 浮层同样用 password 输入框，会被 pageLoggedIn 误判成
        // 「掉回登录页」。但它**不是登出**——账号仍 authorized，只是缺设备密钥。
        // 若不甄别，authorized 会话每 60s 误推一次 needs_login，且 PIN 清除后无反向
        // authorized push（软退出只认 true→false 边沿）→ Python 卡在「需登录」。
        // 故 pageLoggedIn=false 时二次甄别：确是 PIN 浮层 → 维持登录态（半死由
        // inbox-health 覆盖），并顺手尝试自动输入 PIN。
        if (li === false && (await detectPinPrompt(entry.page))) {
          entry._pinPromptSeen = true;
          li = true; // PIN 浮层≠登出
          tryAutoE2eePin(entry._loginId || "", entry, entry.page).catch(() => {});
        }
        entry._loggedIn = li;
        // 软退出闭环（2026-07-31）：页面掉回登录表单（context 活着但会话没了——被踢/
        // 网页端手动退出）此前只改内部标志，session-status 停留在 authorized，Python
        // 要等健康轮询才后知后觉且注册表不落 offline。true→false 边沿主动 push 一次
        // needs_login（仅边沿、不逐轮重发，防唠叨）；恢复登录由 promoteIfLoggedIn 的
        // authorized push 归位。
        if (wasLoggedIn && li === false) {
          postStatus(entry._loginId || "", entry, "needs_login",
            "page fell back to login form (soft logout detected by poll)")
            .catch(() => {});
        }
      }
    } catch (_) { /* 复检失败不改判定，保守留旧值 */ }
    // 入站健康心跳（P0 半死态探测）：每 ~15 轮(≈60s) push 侧栏未读 + 读取滚动窗成败。
    // best-effort 不 await 应答语义（postJson 内部吞错），失败不影响轮询。
    try {
      if ((entry._pollCount || 0) % MSG_INBOX_HEALTH_EVERY === 0) {
        // P3：先探 PIN 浮层 / 尝试自愈，再上报——这样 _pinState 是本拍新鲜值，
        // 心跳能立刻带上精确的 e2ee_pin_required 提示码（否则 UI 会先给一拍
        // 泛泛「重登」CTA）。自愈成功会清 _readWin，下一拍读取重建即转健康。
        await maybeAutoPinSelfHeal(entry);
        // Q-24 C：PIN 曾缺失/失败，而浮层已不在（手机端确认 / 人工输入过了）→ 转 ok + 自动回填
        if (entry._pinPromptSeen && /^(missing|failed)$/.test(String(entry._pinState || ""))) {
          // 只在页面停在加密线程上时判「浮层消失＝已解锁」（非 e2ee 页本就没有浮层，不算证据）
          const onE2ee = /\/e2ee\//.test(String(entry.page.url() || ""));
          if (onE2ee && !(await detectPinPrompt(entry.page))) {
            entry._pinState = "ok";
            entry._pinLastOkTs = Date.now();
            scheduleBackfillAfterPin(entry, "pin_cleared_heartbeat");
          }
        }
        await postInboxHealth(entry);
      }
    } catch (_) { /* 心跳失败不影响轮询 */ }
    entry._lastPollOkTs = Date.now();
    entry._lastPollErr = "";
  } catch (e) {
    entry._lastPollErr = String((e && e.message) || e || "poll failed");
    entry._lastPollErrTs = Date.now();
    logger.debug({ e }, "pollInbound failed");
  } finally {
    entry._polling = false;
    // 页面回收（此刻本轮所有 await 已完成，读取/媒体下载不会被打断）：先置空引用
    // 让下轮 isClosed() 守卫懒重建，再后台 close 释放 renderer（不 await，不拖轮询）。
    if (PAGE_RECLAIM) {
      // reqPage 只在本轮 reqDue 内用过 → 用完即弃（每 reqDue 才重建，60s 一次，开销可忽略）。
      if (entry.reqPage) { const p = entry.reqPage; entry.reqPage = null; try { p.close().catch(() => {}); } catch (_) {} }
      // readPage 仅当本轮没读过任何线程才回收——活跃账号 _readUsed=true 时保留，避免每轮重建抖动。
      if (!entry._readUsed && entry.readPage) { const p = entry.readPage; entry.readPage = null; try { p.close().catch(() => {}); } catch (_) {} }
    }
  }
}

function startPolling(entry) {
  if (!POLL_MS || entry.pollTimer) return;
  entry.pollTimer = setInterval(() => { pollInbound(entry).catch(() => {}); }, POLL_MS);
}

function stopPolling(entry) {
  if (entry && entry.pollTimer) {
    clearInterval(entry.pollTimer);
    entry.pollTimer = null;
  }
  // 自身资料重采定时器跟着轮询一起收：stopPolling 是全部 7 个会话拆除点（放弃登录 /
  // 崩溃自愈 / 重登 / cancel / logout / 优雅退出）的共同出口，挂在这里＝零新增调用点
  // 全覆盖，不会漏下一个指向已关闭 page 的定时器。
  clearSelfProfileTimer(entry);
}

// ── 自身资料周期重采 ────────────────────────────────────────────────────────
// 此前 Messenger 侧**只在登录/恢复那一刻采一次**（WhatsApp 早有 scheduleSelfProfileRecapture），
// 于是手机上换了头像本机永远不知道——2026-08-15 实锤那张错脸就是这样挂了两个多月。
// 两档节奏（判据是纯函数 nextSelfProfileRecaptureMs）：
//   - 还没拿到头像 → 按 RETRY_MS 短周期补采。登录瞬间页面常停在某个会话上、自身头像
//     不在 DOM 里，fail-closed 交空属正常态，过一会儿就有；
//   - 已拿到 → 按 EVERY_MS 长周期对齐手机侧改动。
// 变化才 postStatus(authorized, "profile-refresh") —— Python 的 session-status 端点对每次
// authorized push 都会 enrich_from_fields，故无需 Python 侧改动即贯通到 registry meta.self_*。
// FB CDN 头像直链带轮换签名参数 → URL 每次都不同 → 重采即重下（一张几十 KB 的小图），
// 这也是唯一能让「手机换了头像」传导过来的口径：URL 比对在签名轮换下本就无鉴别力。
// 置 MSG_SELF_PROFILE_EVERY_MS=0 可整体关掉，行为回到旧的「只采一次」。
const SELF_PROFILE_EVERY_MS = Math.max(0,
  Number(process.env.MSG_SELF_PROFILE_EVERY_MS ?? 6 * 3600 * 1000));
const SELF_PROFILE_RETRY_MS = Math.max(0,
  Number(process.env.MSG_SELF_PROFILE_RETRY_MS ?? 3 * 60 * 1000));

function clearSelfProfileTimer(entry) {
  if (entry && entry.selfProfileTimer) {
    clearTimeout(entry.selfProfileTimer);
    entry.selfProfileTimer = null;
  }
}

/** 重采一次自身昵称/头像；有变化才上报。绝不抛（观测/身份链不得影响收发）。 */
async function recaptureSelfProfile(loginId, entry) {
  if (!entry || entry.status !== "authorized" || !entry.page) return;
  // 让路：轮询与写操作（send/react）都在用 entry.page，此刻读多为白读且会互相干扰。
  // 下个周期自然补上——重采是慢节奏工作，没有任何抢占的理由。
  if (entry._polling || entry._opLock) return;
  try { if (entry.page.isClosed && entry.page.isClosed()) return; } catch (_) { return; }
  const prof = await readSelfProfile(entry.page);
  if (!prof.name && !prof.avatarUrl) return;   // 全空＝这轮没读到，保留既有值
  const changed = (!!prof.name && prof.name !== entry.name)
    || (!!prof.avatarUrl && prof.avatarUrl !== entry.avatarUrl);
  // 空值不覆盖非空（与 Python 侧 merge_self_profile_meta 同语义）：一次读空不该把
  // 已有身份擦掉。
  if (prof.name) entry.name = prof.name;
  if (prof.avatarUrl) entry.avatarUrl = prof.avatarUrl;
  if (!changed) return;
  logger.info({
    loginId, accountId: entry.accountId,
    name: entry.name, strategy: prof.strategy,
  }, "self profile recaptured (changed) → push profile-refresh");
  postStatus(loginId, entry, "authorized", "profile-refresh").catch(() => {});
}

/** 排下一次重采（自递归 setTimeout：间隔随「有没有头像」自适应，比 setInterval 贴切）。 */
function scheduleSelfProfileRecapture(loginId, entry) {
  if (!entry) return;
  clearSelfProfileTimer(entry);
  const delay = nextSelfProfileRecaptureMs(
    { hasAvatar: !!entry.avatarUrl },
    { everyMs: SELF_PROFILE_EVERY_MS, retryMs: SELF_PROFILE_RETRY_MS },
  );
  if (!delay) return;
  entry.selfProfileTimer = setTimeout(() => {
    entry.selfProfileTimer = null;
    recaptureSelfProfile(loginId, entry)
      .catch(() => {})
      .finally(() => {
        // 会话还在册才续排（拆除后不复活；sessions 换了 entry 说明已重建，由新 entry 自己排）
        if (sessions.get(loginId) === entry && entry.status === "authorized") {
          scheduleSelfProfileRecapture(loginId, entry);
        }
      });
  }, delay);
}

/** 页面级登录校验：真正进了收件箱（无登录表单、且有收件箱骨架）才算数。
 *  单看 cookie 会误报——c_user/xs 可能残留或失效，服务端仍渲染登录页。 */
async function pageLoggedIn(page) {
  try {
    return await page.evaluate(() => {
      // 有邮箱/密码输入框 → 还在登录页 → 未登录
      const loginForm = document.querySelector(
        'input[name="pass"], input[type="password"], input[name="email"]');
      if (loginForm) return false;
      // 正向信号：会话链接 / 富文本输入框 / 网格骨架任一存在
      const inbox = document.querySelector(
        'a[href^="/t/"], div[role="textbox"][contenteditable="true"], [role="grid"]');
      return !!inbox;
    });
  } catch (_) {
    return false;
  }
}

/** 检测到已登录则把 entry 晋级为 authorized（采身份 + 开轮询），幂等。
 *  被登录看门狗与 /status 轮询共用 → 即使看门狗超时退出，一次状态轮询也能复检补救。
 *  返回是否已授权。 */
async function promoteIfLoggedIn(loginId, entry) {
  if (!entry) return false;
  if (entry.status === "authorized") return true;
  try {
    // cookie 预检（快）+ 页面级确认（准）：两者都过才判已登录，杜绝 xs 残留误报。
    if (!(await isLoggedIn(entry.context))) return false;
    if (!(await pageLoggedIn(entry.page))) return false;
    entry.accountId = await readAccountId(entry.context);
    entry.status = "authorized";
    entry._loggedIn = true;
    try {
      let prof = await readSelfProfile(entry.page);
      if (!prof.name || !prof.avatarUrl) {
        // P1：restore 时页面常停在上次的线程 URL，CurrentUserInitialData 脚本可能
        // 不在该文档流里 → 昵称恒空（账号卡分不清绑的是谁的另一半根因）。回首页再试一次。
        //
        // ⚠ 条件必须同时看头像（2026-08-15）：昵称改由 CurrentUserInitialData 取得后
        // `!prof.name` 几乎恒假，这个重试分支等于死代码；而头像 fail-closed 后正是
        // 「停在会话页 → 自身头像不在 DOM 里 → 空」最需要回首页的那一档。首页（收件箱
        // 列表）本就是轮询期望的落点，导航过去无副作用。
        try {
          await entry.page.goto(MESSENGER_URL,
            { waitUntil: "domcontentloaded", timeout: 15000 });
          await entry.page.waitForTimeout(800);
          prof = await readSelfProfile(entry.page);
        } catch (_) {}
      }
      entry.name = prof.name;
      entry.avatarUrl = prof.avatarUrl;
    } catch (_) {}
    logger.info({ loginId, accountId: entry.accountId }, "Messenger connected");
    entry._loginId = loginId;
    _recoveryAttempts.delete(loginId); // 成功授权 → 清零自愈退避计数
    // M-2 C/D（#233）：登录成功 / 通道恢复 → 重置发送退避（此前 context 自愈重拉后旧 entry
    // 的 streak 随 entry 丢弃、但同一登录档案在 Python 侧仍被当作退避中；这里显式清并落行）。
    if ((entry._sendFailStreak || 0) || (entry._sendBackoffUntil || 0) > Date.now()) {
      logger.info({ loginId, streak: entry._sendFailStreak || 0 }, "send backoff reset on (re)authorization");
    }
    noteSendSuccess(entry);
    entry._manualProbeAt = 0;
    const _srt = _slowRetryTimers.get(loginId); // 已恢复 → 撤掉排队中的慢重试
    if (_srt) { clearTimeout(_srt); _slowRetryTimers.delete(loginId); }
    await saveCookies(loginId, entry.context); // 落盘会话级 cookie，扛住重启
    startPolling(entry);
    scheduleSelfProfileRecapture(loginId, entry); // 头像没采到 → 短周期补；采到了 → 长周期跟手机改动
    postStatus(loginId, entry, "authorized", "connected").catch(() => {});
    maybeAutoHideAfterLogin(loginId, entry); // B65：headed 登录窗授权后自动无头回归
    return true;
  } catch (e) {
    logger.debug({ e, loginId }, "promoteIfLoggedIn failed");
    return false;
  }
}

/** 拉起一个登录/账号上下文（持久化 profile）。loginId 复用即为 restore。 */
// ── 崩溃自愈 ─────────────────────────────────────────────────────────────────
// 浏览器 context 崩溃/被关（headed 长时运行 + 进线程频繁导航偶发；或被系统/FB 干掉）会让轮询
// 静默失败 → 服务「假死」：HTTP 仍在、/health ok，但读不到消息也不回。此前无自愈 → 一崩就一直
// 冻着。此处监听 context 'close' 事件：清理旧 entry/定时器 → 延时用 startLogin 重启（复用磁盘
// profile + 回灌 cookie，通常免重扫）。用 _recovering 防并发重建；_shuttingDown 时不自愈（正常退出）。
const _recovering = new Set();
// 主动关闭（cancel / logout）的 login_id：这两条路径同样会触发 context 'close'，而自愈
// 分不清「崩溃」和「我们自己关的」。一次性标记，close 事件消费掉即失效，真崩溃不受影响。
const _intentionalClose = new Set();
// 自愈退避 + 上限：连续崩溃时用指数退避（3s→6s→12s→24s→48s，封顶 60s），超过 5 次即放弃自愈、
// 置 expired 并告警——防「崩溃循环无限 3s 重启」既堆 Chromium 又高频重连触发 FB 反自动化登出。
// 距上次尝试 >5min 视为新事件，计数清零（promoteIfLoggedIn 成功授权后亦清零）。
const _recoveryAttempts = new Map();
const _RECOVERY_MAX = 5;
// 放弃自愈后的慢速重试（P2）：快自愈（3s→60s 退避 ×5 次）放弃后不再是「死等人工」——
// 每 MSG_RECOVERY_SLOW_RETRY_MS（默认 15min，0=关）安排一次全新自愈周期（计数清零）。
// 频率足够低：不堆 Chromium、对 FB 只是一次导航（cookie 失效时停在登录页，无登录尝试），
// 但能自动救回「系统资源暂时耗尽/网络中断恢复」这类过后即愈的场景。人工重登成功会清零一切。
const MSG_RECOVERY_SLOW_RETRY_MS = Math.max(
  0, Number(process.env.MSG_RECOVERY_SLOW_RETRY_MS ?? 15 * 60 * 1000));
const _slowRetryTimers = new Map();
function scheduleSlowRetry(loginId) {
  if (!MSG_RECOVERY_SLOW_RETRY_MS || _shuttingDown) return;
  if (_slowRetryTimers.has(loginId)) return; // 已排队
  const t = setTimeout(() => {
    _slowRetryTimers.delete(loginId);
    if (_shuttingDown) return;
    const cur = sessions.get(loginId);
    if (cur && cur.status === "authorized") return; // 期间已恢复（人工登录/自行回来）
    logger.info({ loginId }, "slow-retry: starting a fresh auto-recovery cycle");
    _recoveryAttempts.delete(loginId); // 全新退避预算
    scheduleRecovery(loginId);
  }, MSG_RECOVERY_SLOW_RETRY_MS);
  if (typeof t.unref === "function") t.unref();
  _slowRetryTimers.set(loginId, t);
}
function scheduleRecovery(loginId) {
  if (_shuttingDown || _recovering.has(loginId)) return;
  // 人为关闭不是崩溃：不拦的话，坐席一关接入弹窗（前端会自动 cancel）浏览器就在 3 秒后
  // 被重新拉起；logout 更糟——profile 已删，自愈等于凭空弹出一个全新的空白登录窗口。
  if (_intentionalClose.delete(loginId)) {
    logger.debug({ loginId }, "context closed intentionally → skip auto-recovery");
    return;
  }
  // 还没登录成功就被关掉：headed 模式下坐席直接点窗口右上角的 X 是最自然的放弃方式，
  // 而 close 事件与真崩溃长得一模一样。这时重新弹窗毫无意义——没人在等它，只会骚扰
  // （关一次弹一次）。本地置 expired 收摊（前端登录轮询契约），profile 是空壳一并清掉；
  // 坐席想登随时可重新发起。只有 authorized 会话才值得自愈：那是在收发消息的生产会话。
  // ⚠ 对 Python 侧上报 **abandoned** 而非 expired（2026-08-14 事故）：放弃的登录窗
  // 从来不是真实账号，报 expired 会让临时 login_id（msg_xxx）以「不健康」挂进坐席
  // 顶栏红条几小时——坐席看不懂也无从处理。abandoned 不在 UNHEALTHY_STATUSES，
  // 天然不进横幅/看门狗/注册表翻转，仅留 by_status 计数（可观测放弃率）。
  const cur0 = sessions.get(loginId);
  if (cur0 && cur0.status !== "authorized") {
    logger.info({ loginId, status: cur0.status },
      "login window closed before authorization → treat as abandoned (no relaunch)");
    stopPolling(cur0);
    cur0.status = "expired";
    sessions.delete(loginId);
    purgeProfile(loginId);
    postStatus(loginId, cur0, "abandoned", "login window closed before authorization").catch(() => {});
    return;
  }
  const now = Date.now();
  const rec = _recoveryAttempts.get(loginId) || { count: 0, lastTs: 0 };
  if (now - rec.lastTs > 5 * 60 * 1000) rec.count = 0;
  rec.count += 1; rec.lastTs = now;
  _recoveryAttempts.set(loginId, rec);
  const old0 = sessions.get(loginId);
  if (rec.count > _RECOVERY_MAX) {
    logger.error({ loginId, attempts: rec.count },
      "context crash-loop → GIVING UP auto-recovery (manual re-login needed). "
      + "Not relaunching to avoid Chromium pile-up / anti-automation logout.");
    if (old0) { stopPolling(old0); old0.status = "expired"; }
    postStatus(loginId, old0, "expired",
      `context crash-loop (${rec.count} attempts) → auto-recovery given up; manual re-login needed`)
      .catch(() => {});
    scheduleSlowRetry(loginId); // 不死等人工：低频再给自愈机会
    return;
  }
  _recovering.add(loginId);
  if (old0) { stopPolling(old0); sessions.delete(loginId); }
  const delay = Math.min(3000 * Math.pow(2, rec.count - 1), 60000);
  logger.warn({ loginId, attempt: rec.count, delayMs: delay },
    "browser context closed unexpectedly → auto-recover");
  setTimeout(async () => {
    try {
      if (_shuttingDown) return;
      await startLogin(loginId, "", true);
      logger.info({ loginId }, "context auto-recovered (relaunched from persisted profile)");
    } catch (e) {
      logger.error({ e, loginId }, "context auto-recovery failed");
    } finally {
      _recovering.delete(loginId);
    }
  }, delay);
}

// ── B65：headed 登录窗授权后自动无头回归 ─────────────────────────────────────
// 登录成功横幅（隐藏前的缓冲期展示）：告知「窗口将自动隐藏」消除「窗口去哪了」的困惑，
// 顺带承载养号提示（老板拍板：不做新号强制人审，只做提醒）。全屏遮罩还有个副作用红利：
// 缓冲期内用户点不到页面里的会话/输入框，不会在交接瞬间误操作真实账号。best-effort。
async function injectPostLoginBanner(page) {
  // B70（实施67 P1-9，`_341` 实锤）：授权 ≠ 页面可遮——cookie 双全时 e2ee PIN
  // 恢复页可能正挂在面前，全屏成功横幅会把 PIN 输入盖死（用户永远输不了）。
  // checkpoint/e2ee 浮层在场 → 本次不注入（横幅只是体验糖，遮住验证是事故）。
  try {
    if (await detectPinPrompt(page)) {
      logger.info("post-login banner skipped: e2ee pin prompt on screen");
      return;
    }
  } catch (_) { /* 探测失败按无浮层，维持旧行为 */ }
  await page.evaluate(() => {
    if (document.getElementById("__aitr_postlogin_banner")) return;
    const d = document.createElement("div");
    d.id = "__aitr_postlogin_banner";
    d.style.cssText = "position:fixed;inset:0;z-index:2147483647;"
      + "background:rgba(4,12,22,.88);color:#e8f6ff;display:flex;align-items:center;"
      + "justify-content:center;text-align:center;"
      + "font:15px/1.9 system-ui,'Microsoft YaHei',sans-serif;padding:32px;";
    d.innerHTML = "<div style='max-width:520px'>"
      + "<div style='font-size:22px;font-weight:700;margin-bottom:10px'>&#9989; 登录成功，AI 托管已开始</div>"
      + "<div>本窗口将自动隐藏，转入后台无头运行；无需保持打开，账号照常收发。</div>"
      + "<div style='margin-top:14px;padding:10px 14px;border:1px solid rgba(255,215,106,.5);"
      + "border-radius:10px;color:#ffd76a'>&#127793; 新账号请注意养号：头几天建议控制自动发送频率、"
      + "避免高频群发，降低平台风控概率。</div></div>";
    document.documentElement.appendChild(d);
  });
}

/** B70：成功横幅的反悔面——注入后才发现 PIN 浮层（时序竞态）时摘掉，还回输入。 */
async function removePostLoginBanner(page) {
  try {
    await page.evaluate(() => {
      const d = document.getElementById("__aitr_postlogin_banner");
      if (d) d.remove();
    });
  } catch (_) { /* best-effort */ }
}

/** headed 登录窗授权后的自动隐藏：缓冲期展示成功横幅 → 优雅关闭可见 context →
 *  以 restore 语义重启（RESTORE_HEADLESS ⇒ 无头）。判定纯函数 shouldAutoHideAfterLogin
 *  有门禁；_intentionalClose 抑制崩溃自愈误判；失败回落 scheduleRecovery 兜底
 * （profile/cookie 已落盘，恢复链是每天在跑的成熟路径，最坏丢一次换头、绝不丢会话）。 */
function maybeAutoHideAfterLogin(loginId, entry) {
  if (!shouldAutoHideAfterLogin({
    windowMode: entry && entry.windowMode,
    restoreHeadlessEnv: RESTORE_HEADLESS,
    hideEnv: HIDE_AFTER_LOGIN,
  })) return;
  injectPostLoginBanner(entry.page).catch(() => {});
  logger.info({ loginId, delayMs: HIDE_AFTER_LOGIN_DELAY_MS },
    "headed login window authorized → will auto-swap to headless");
  entry._hideArmedAt = Date.now();
  const arm = (ms) => {
    const t = setTimeout(() => { hideTick().catch((e) => logger.debug({ e }, "hide tick failed")); }, ms);
    if (typeof t.unref === "function") t.unref();
  };
  const hideTick = async () => {
    const cur = sessions.get(loginId);
    // 只动「还是同一个 entry 且仍授权」的会话；期间被 relogin/cancel/logout 换掉就不掺和。
    if (_shuttingDown || !cur || cur !== entry || cur.status !== "authorized") return;
    if (_recovering.has(loginId)) return; // 崩溃自愈已在处理 → 让它走
    // B70（实施67 P1-9）：隐藏前最后核对——e2ee PIN 浮层在场时把窗口无头化
    // ＝把「等人输 PIN」的账号推进入站半死（`_344`/`_345` 死等实录）。
    // 有托管 PIN 先就地自愈（成功→照常隐藏）。
    // Q-24 E（#298，TKV3HB 实锤）：旧版「本轮不隐藏 + return」= 永不再武装 → 窗口永久
    // 露着、每次导航都抢前台。现在：PIN 在场且未自愈 → 记 pinState=missing/failed 让
    // 心跳把 e2ee_pin_required 送到工作台黄条（会话头「需在手机确认 PIN / 托管 PIN」），
    // 每 15s 复查一次；给人 HIDE_PIN_GRACE_MS 宽限（默认 2min）在窗口里输 PIN，宽限到
    // 仍未过 → **强制隐藏**（托管 PIN 后 headless 里 driveE2eePinReplay 照样能自愈）。
    // 每次复查都落 `[msg-sidecar] window visible reason=…` 行，可见就有痕。
    try {
      if (await detectPinPrompt(cur.page)) {
        cur._pinPromptSeen = true;
        let healed = false;
        if (getE2eePin(loginId)) {
          healed = await tryAutoE2eePin(loginId, cur, cur.page).catch(() => false);
        } else {
          cur._pinState = "missing";
        }
        if (!healed) {
          const visibleFor = Date.now() - (cur._hideArmedAt || Date.now());
          if (visibleFor < HIDE_PIN_GRACE_MS) {
            logger.warn({ loginId, visibleMs: visibleFor, graceMs: HIDE_PIN_GRACE_MS,
              pinState: String(cur._pinState || "") },
            "[msg-sidecar] window visible reason=e2ee_pin_prompt (auto-hide deferred, re-check in 15s)");
            await removePostLoginBanner(cur.page);
            postInboxHealth(cur).catch(() => {});
            arm(15000);
            return;
          }
          logger.warn({ loginId, visibleMs: visibleFor, pinState: String(cur._pinState || "") },
            "[msg-sidecar] window visible reason=e2ee_pin_prompt grace exhausted → FORCE hide "
            + "(PIN via workbench hosted-PIN; headless replay will heal)");
        } else {
          // PIN 刚过：加密线程此前读不出 → 重建回填队列，历史/手机端消息补抄进来（C 段）
          scheduleBackfillAfterPin(cur, "pin_healed_before_hide");
        }
      } else if (cur._pinPromptSeen && cur._pinState !== "ok") {
        // 人在窗口里手输 PIN 过了（浮层消失）→ 同样视为解锁：清状态 + 回填
        cur._pinState = "ok";
        cur._pinLastOkTs = Date.now();
        scheduleBackfillAfterPin(cur, "pin_cleared_manually");
      }
    } catch (_) { /* 探测异常按旧行为继续隐藏 */ }
    try {
      logger.info({ loginId }, "auto-hide: swapping headed login window to headless");
      _intentionalClose.add(loginId); // 我们自己关的，别按崩溃自愈
      stopPolling(cur);
      try { await cur.context.close(); } catch (_) {}
      sessions.delete(loginId);
      await startLogin(loginId, cur.proxyUrl || "", true); // isRestore ⇒ RESTORE_HEADLESS 无头
      logger.info({ loginId }, "auto-hide: headless session is back");
    } catch (e) {
      logger.error({ e, loginId },
        "auto-hide swap failed → falling back to crash-recovery path");
      _intentionalClose.delete(loginId);
      scheduleRecovery(loginId);
    }
  };
  arm(HIDE_AFTER_LOGIN_DELAY_MS);
}

/**
 * Q-24 C（#298）：PIN 通过后自动回填——加密线程在 PIN 未过时 readThreadTail 全是占位/空，
 * 首连回填队列早已消化完（消化的是空读）。PIN 一过就把队列重建（复用首连 warmBackfillBatch
 * 口径：只碰已读会话，未读留给实时链，绝不额外标已读），下几拍轮询自然把历史 + 手机端
 * 发出的消息（direction=out origin=external）抄进来。同一 entry 5 分钟内只重建一次。
 */
function scheduleBackfillAfterPin(entry, reason) {
  try {
    if (!entry || entry.status !== "authorized") return;
    const now = Date.now();
    if (entry._pinBackfillAt && now - entry._pinBackfillAt < 300000) return;
    entry._pinBackfillAt = now;
    entry._backfillRebuildPending = true;
    entry._readWin = [];
    entry._convStats = { count: 0, ph: 0 };
    logger.info({ loginId: entry._loginId, reason }, "e2ee pin passed → backfill queue rebuild scheduled");
    postInboxHealth(entry).catch(() => {});
  } catch (_) { /* best-effort */ }
}

async function startLogin(loginId, proxyUrl, isRestore = false, interactive = false) {
  const userDataDir = path.join(SESSIONS_DIR, loginId);
  // 窗口模式决策收拢到 login_window.resolveLaunch（纯函数 + 回归网）：非交互 / restore 逐字节
  // 保持旧行为（headed / RESTORE_HEADLESS）；交互登录（Form-Relay）走 offscreen（默认＝无可见
  // 窗口 + headed 反检测）/ new_headless / headless，由 env MSG_INTERACTIVE_WINDOW 选。
  const launch = resolveLaunch({
    interactive, isRestore,
    headlessEnv: HEADLESS, restoreHeadlessEnv: RESTORE_HEADLESS,
    interactiveMode: process.env.MSG_INTERACTIVE_WINDOW,
  });
  const headless = launch.headless;
  logger.info({ loginId, mode: launch.mode, headless, interactive, isRestore },
    "login browser launching");
  // B64：headed（真给人操作）的登录窗视口跟随窗口，可最大化/缩放过 FB 机器人验证；
  // offscreen/headless（截图流/无人窗口）保持固定视口。
  const context = await launchPersistent(
    userDataDir, proxyUrl, headless, launch.args, launch.mode === "headed");
  // 崩溃自愈：context 意外关闭 → 自动重启（正常退出由 _shuttingDown 拦掉）。
  context.on("close", () => scheduleRecovery(loginId));
  await applyStealth(context);
  const page = context.pages()[0] || (await context.newPage());

  const entry = {
    context, page,
    status: "pending",
    qrImage: "",
    accountId: "",
    name: "",
    avatarUrl: "",
    createdAt: Date.now(),
    userDataDir,
    proxyUrl: proxyUrl || "",
    seen: new Map(),
    pollTimer: null,
    // B65：窗口模式随 entry 走——授权后自动隐藏（maybeAutoHideAfterLogin）与登录期
    // stage 前置顶都只对可见 headed 窗口生效，offscreen/headless 不折腾。
    windowMode: launch.mode,
    // M-2 D：restore 拉起的会话在 /accounts.worker.pending 里可辨（Python 探测不误判需重登）
    _isRestore: !!isRestore,
  };
  sessions.set(loginId, entry);

  // B65：登录前窗口主动弹出（老板拍板「登录前的各个页面不要隐藏，反而要主动弹出」）。
  // 仅可见 headed + 非 restore（restore 无人在等窗口）；best-effort，弹不起来不阻塞登录链。
  if (launch.mode === "headed" && !isRestore) {
    page.bringToFront().catch(() => {});
  }

  // profile-first restore：先用持久化 profile 自身的 cookie 导航——它往往已含**最新** xs
  // （持久化上下文会随浏览器写盘）。仅当 profile 自身未登录时，才回落注入快照 .cookies.json 并重载。
  // 杜绝「用滞后的快照 xs 覆盖 profile 里更新的会话 → 反被打成过期登出」这条根因。
  try {
    await page.goto(MESSENGER_URL, { waitUntil: "domcontentloaded", timeout: 30000 });
  } catch (e) {
    logger.debug({ e }, "initial goto failed");
  }
  try {
    if (!(await pageLoggedIn(page)) && (await loadCookies(loginId, context))) {
      logger.info({ loginId }, "profile not logged in → injecting cookie snapshot fallback");
      await page.goto(MESSENGER_URL, { waitUntil: "domcontentloaded", timeout: 30000 }).catch(() => {});
    }
  } catch (e) {
    logger.debug({ e }, "profile-first cookie fallback failed");
  }

  // 后台盯登录状态：登录成功 → 记 accountId、采身份、开轮询。
  // 交互登录窗口 30 分钟（含扫码/2FA/找窗口）；restore/自愈无人工介入 → 90s 判不出即置 expired
  // 并**显式告警**（不再静默限 30 分钟；编排器/UI 能立刻看到「需重新登录」）。
  // 即便超时置 expired，/status 轮询仍会 promoteIfLoggedIn 复检补救（用户手动登录后自动恢复）。
  (async () => {
    // 交互登录 30min；restore/自愈给足人工重登时间 10min，但**一旦确认没自动授权即刻告警**
    // （编排器/运营即时看到「需重登」），并继续盯到超时——期间人工登录成功即自动 promote。
    const started = Date.now();
    const deadline = started + (isRestore ? 1000 * 60 * 10 : 1000 * 60 * 30);
    let warnedReLogin = false;
    let lastSig = "";
    let lastStage = "";
    while (sessions.has(loginId) && entry.status !== "authorized" && Date.now() < deadline) {
      try {
        if (await promoteIfLoggedIn(loginId, entry)) break;
        entry.qrImage = await snapshot(page);
        // 登录失败不抛异常——页面被静默弹回登录页、凭证 cookie 始终不下发，全程无声。
        // 把「当前 URL + 关键 cookie 是否到位」的每次变化记成一条，失败时的跳转序列
        // （登录页 → checkpoint → 登录页）才有据可查。仅变化时写，不刷屏。
        try {
          const url = page.url();
          const names = (await context.cookies()).map((c) => c.name);
          const hasCUser = names.includes("c_user");
          const hasXs = names.includes("xs");
          // 旁观信号 → 分段（纯函数，见 login_classify.js）。两个用途：
          // ① hint_code 实时回前端——托管登录卡在 2FA/检查点时，坐席看到的不再是
          //    一句恒定的「已打开登录窗口」，而是「需要完成两步验证」；
          // ② 窗口期结束仍没登上时当失败原因码，漏斗里「12 次全是 checkpoint」
          //    一眼看出是账号风控而非代码故障。
          const hasErrorBox = await hasLoginErrorBox(page);
          // P2：E2EE PIN 页常落在根路径，靠可见文案兜底（只取前 800 字，零隐私落盘）。
          let pageText = "";
          try {
            pageText = await page.evaluate(() =>
              ((document.body && document.body.innerText) || "").slice(0, 800));
          } catch (_) { pageText = ""; }
          // 表单中继关键信号：DOM 上是否有账密输入框。messenger.com 未登录落根路径（URL 无
          // /login 标记），只靠 URL classifyLoginPage 会误判 unknown → 中继流永远停 wait
          // （2026-08-13 canary 捕获）。用 tryAutoE2eePin 同款字段名判据（多年稳定）。
          let hasLoginForm = false;
          try {
            hasLoginForm = await page.evaluate(() =>
              !!document.querySelector('input[name="email"], input[name="pass"]'));
          } catch (_) { hasLoginForm = false; }
          lastStage = classifyLoginPage({ url, hasCUser, hasXs, hasErrorBox, pageText, hasLoginForm });
          entry.hintCode = actionableCode(lastStage);
          // 表单中继只读探针（/login/:id/relay-step）复用这份缓存，不重复抓页。
          entry.lastStage = lastStage;
          // B64 三期：checkpoint 子味（等手机确认 vs 机器人验证/泛检查点）——仅细化
          // relay-step 的 code（前端手机叙事视图），hint_code/reason_code 词汇不动
          //（Python 漏斗契约保持：窗口期失败归因仍是 checkpoint）。
          entry.lastFlavor = (lastStage === STAGE.CHECKPOINT) ? checkpointFlavor(pageText) : "";
          // B65：进入需要人操作的分段（账密/2FA/checkpoint/PIN/密码错/锁定）→ headed 窗口
          // 主动置顶一次（同一 stage 不重复夺焦点）。offscreen/headless 不折腾。
          if (entry.windowMode === "headed" && lastStage
              && lastStage !== entry._frontedStage && HUMAN_ACTION_STAGES.has(lastStage)) {
            entry._frontedStage = lastStage;
            page.bringToFront().catch(() => {});
          }
          const sig = `${url}|${hasCUser}|${hasXs}|${lastStage}`;
          if (sig !== lastSig) {
            lastSig = sig;
            logger.info({ loginId, url, hasCUser, hasXs, stage: lastStage }, "login page state");
          }
          // P3 自动恢复 PIN：恢复/自愈/重登路径撞到 E2EE PIN 页且已配 PIN → 自动输入
          //（闸门限次防重复敲键盘）。成功后下一轮 promoteIfLoggedIn 正常晋级。
          if (lastStage === STAGE.E2EE_PIN) {
            entry._pinPromptSeen = true;
            await tryAutoE2eePin(loginId, entry, page);
          }
        } catch (_) {}
        // 正常自愈通常 2-4s 内就 re-auth；>10s 仍停在登录页 → 判定需人工重登，告警一次（不误报）。
        if (isRestore && !warnedReLogin && Date.now() - started > 10000) {
          warnedReLogin = true;
          logger.warn({ loginId },
            "restore: not auto-authorized after 10s (cookies expired/invalidated) → "
            + "MANUAL RE-LOGIN likely required; watching 10min for manual login");
          postStatus(loginId, entry, "needs_login",
            "cookies expired/invalidated; manual re-login required").catch(() => {});
        }
      } catch (e) {
        logger.debug({ e }, "login watch tick failed");
      }
      await new Promise((r) => setTimeout(r, 1500));
    }
    if (entry.status !== "authorized" && sessions.has(loginId)) {
      entry.status = "expired";
      // 窗口期内最后观察到的分段即失败归因：checkpoint=账号被风控、two_factor=没人
      // 完成二因子、password_error=凭据错。认不出就留空（上游按会话超时归因），
      // 绝不硬凑——错的归因比没有归因更耽误人。已有终态原因码不覆盖。
      const stageReason = actionableCode(lastStage);
      if (stageReason && !entry.reasonCode) entry.reasonCode = stageReason;
      if (isRestore) {
        logger.warn({ loginId }, "restore/recover watch window ended without auth → status=expired");
        postStatus(loginId, entry, "expired",
          "restore watch window ended without auth (manual re-login required)").catch(() => {});
      }
    }
  })().catch((e) => logger.debug({ e }, "login watcher crashed"));

  return entry;
}

/** 恢复磁盘上已持久化的所有账号 profile（开机/主动调用，幂等）。
 *
 *  M-2 D（#232，2026-09-06）：`restored 0 persisted Messenger session(s)` 此前是一行孤零零的
 *  结论，没人知道是「目录空 / 有目录没 cookie 快照 / 拉起失败」哪一种（ZGKVQB 实锤：13:55
 *  重启 restored 0 后两小时零日志，账号界面却显示已登录）。现在每个目录的去向各落一条，
 *  汇总带 sessions_dir + 四个计数；并发恢复期间 `_restoring` 计数暴露在 /accounts.worker，
 *  Python 探测据此在 restore 进行中不误判 needs_login。 */
let _restoring = 0;
async function restoreAll() {
  let dirs = [];
  let readErr = "";
  try {
    dirs = fs.readdirSync(SESSIONS_DIR, { withFileTypes: true })
      .filter((d) => d.isDirectory()).map((d) => d.name);
  } catch (e) {
    dirs = [];
    readErr = String((e && e.code) || e);
  }
  let restored = 0, noCookies = 0, failed = 0, alreadyLive = 0;
  _restoring += 1;
  try {
    for (const loginId of dirs) {
      if (sessions.has(loginId)) { alreadyLive += 1; continue; }
      // 没有 cookie 快照 = 从没登录成功过（saveCookies 只在授权后写）。这种空壳目录多半是
      // 「发起接入又取消」留下的，拉起来也只会停在登录页，白弹一个窗口再盯 10 分钟。
      // cancel 时的 purge 偶尔会因文件仍被占用而放弃，这里兜底再清一次。
      if (!fs.existsSync(cookiesPath(loginId))) {
        noCookies += 1;
        logger.info({ loginId }, "skip restore: never authorized (no cookie snapshot) → purging shell profile");
        purgeProfile(loginId);
        continue;
      }
      try {
        await startLogin(loginId, "", true);
        restored += 1;
        logger.info({ loginId }, "restore: session launched from persisted profile (watching for auto-auth)");
      } catch (e) {
        failed += 1;
        logger.warn({ e, loginId, message: String((e && e.message) || e) }, "restore session failed");
      }
    }
  } finally {
    _restoring = Math.max(0, _restoring - 1);
  }
  // 汇总行：restored=0 时一眼看出是哪种零（目录空 / 全是空壳 / 全部拉起失败 / 目录读不了）
  logger[(dirs.length && restored === 0) ? "warn" : "info"]({
    sessions_dir: SESSIONS_DIR, dirs: dirs.length, restored, no_cookies: noCookies,
    failed, already_live: alreadyLive, read_error: readErr || undefined,
  }, "restoreAll summary");
  return restored;
}

/** 消息请求线程里点「接受」按钮（接受后才出现输入框）。找到并点击返回 true。
 *  兼容中英：接受/Accept。非请求线程无此按钮 → 返回 false（无副作用）。 */
async function clickAcceptRequest(page) {
  for (const name of ["接受", "Accept"]) {
    try {
      const loc = page.getByRole("button", { name, exact: true });
      if (await loc.count()) {
        await loc.first().click({ timeout: 3000 });
        return true;
      }
    } catch (_) {}
  }
  // 兜底：按可见文本精确匹配的 role=button
  try {
    const loc = page.locator('div[role="button"]', { hasText: /^(接受|Accept)$/ });
    if (await loc.count()) {
      await loc.first().click({ timeout: 3000 });
      return true;
    }
  } catch (_) {}
  return false;
}

/** 点某组文案（中英）匹配的 role=button，命中即点、返回 true；无则 false（无副作用）。
 *  与 clickAcceptRequest 同款保守定位：getByRole 精确名 → div[role=button] 文本兜底。 */
async function clickButtonByLabels(page, labels, { timeout = 3000 } = {}) {
  for (const name of labels) {
    try {
      const loc = page.getByRole("button", { name, exact: true });
      if (await loc.count()) { await loc.first().click({ timeout }); return true; }
    } catch (_) {}
  }
  try {
    const re = new RegExp("^(" + labels.map((s) =>
      s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")$");
    const loc = page.locator('div[role="button"]', { hasText: re });
    if (await loc.count()) { await loc.first().click({ timeout }); return true; }
  } catch (_) {}
  return false;
}

/** 某组文案（中英）的按钮当前是否存在（只探测不点击，供 decline 探测优先路径）。 */
async function hasButtonByLabels(page, labels) {
  for (const name of labels) {
    try {
      if (await page.getByRole("button", { name, exact: true }).count()) return true;
    } catch (_) {}
  }
  try {
    const re = new RegExp("^(" + labels.map((s) =>
      s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|") + ")$");
    if (await page.locator('div[role="button"]', { hasText: re }).count()) return true;
  } catch (_) {}
  return false;
}

/** 发送后校验：输入框应已清空（回车成功发出后 Messenger 会清空 composer）。
 *  若我们刚输入的文本仍在 → 多半没发出去。 */
async function verifyComposerCleared(page) {
  try {
    const txt = await page.$eval(SEL_COMPOSER,
      (el) => ((el.innerText || el.textContent || "") + "").trim());
    return txt.length === 0;
  } catch (_) {
    return false;
  }
}

// 消息行内/行侧的「发送失败」标记（Messenger 在失败气泡下方标注）。仅在**匹配到我们
// 刚发的那条 out 气泡的行容器内**查找 → 客户消息正文里偶然含这些词不会误伤。
const SEND_FAIL_MARKER_RE =
  /(无法发送|未能发送|发送失败|couldn[’']t send|didn[’']t send|failed to send|message failed)/i;

/** 送达二次确认（P1 升级）：composer 清空只是「大概率发出」，这里回读消息区最后几条
 *  out 气泡，确认我们的文本已渲染为本方消息、且该气泡行内没有「无法发送」失败标记。
 *  返回 {found, rowFail}：
 *    found=true, rowFail=false → 确认送达；
 *    found=true, rowFail=true  → 渲染了但被标失败（确定性失败）；
 *    found=false               → 超时未见（**不定态**，调用方须按「已发出」处理避免重发刷屏）。 */
async function readbackLastOutgoing(page, text, timeoutMs = 5000) {
  const want = normPreview(text).slice(0, 48);
  if (!want) return { found: false, rowFail: false };
  const t0 = Date.now();
  while (true) {
    try {
      const probe = await page.evaluate(() => {
        const region = document.querySelector('[role="log"]') || document.body;
        const MSG = /(消息由.*发送于|Message sent )/i;
        const nodes = Array.from(region.querySelectorAll("[aria-label]"))
          .filter((e) => MSG.test(e.getAttribute("aria-label") || ""))
          .slice(-6);
        return nodes.map((e) => {
          // 上溯到消息行容器（role=row 或最多 4 层父级），带出行文本供失败标记检测
          let box = e;
          for (let i = 0; i < 4 && box.parentElement; i++) {
            if (box.getAttribute && box.getAttribute("role") === "row") break;
            box = box.parentElement;
          }
          const sib = box.nextElementSibling;
          return {
            aria: e.getAttribute("aria-label") || "",
            rowText: ((box.innerText || "") + " " + ((sib && sib.innerText) || "")).slice(0, 400),
          };
        });
      });
      for (const it of (probe || []).reverse()) {
        const p = parseMsgAria(it.aria);
        if (!p || p.direction !== "out") continue;
        const got = normPreview(p.text).slice(0, 48);
        if (got && (got.startsWith(want) || want.startsWith(got))) {
          return { found: true, rowFail: SEND_FAIL_MARKER_RE.test(it.rowText || "") };
        }
      }
    } catch (_) { /* 探测失败不改判定，重试到超时 */ }
    if (Date.now() - t0 >= timeoutMs) return { found: false, rowFail: false };
    await page.waitForTimeout(400);
  }
}

/** 每账号 DOM 写操作互斥锁（P3 双面板融合 2026-08-13）。
 *
 *  背景：send 与出站表情（/react）都在 **entry.page** 上做「导航 + hover + 键入」，
 *  两个写操作并发＝互踩焦点/导航（send 打字打到一半被 react 的 hover 拽走）。此前只有
 *  send 一个写操作、天然无并发；/react 落地后必须串行。轮询（只读 $$eval，不抢焦点）
 *  不进锁，只在写操作进行中礼让跳过本 tick（见 pollInbound 顶部）。
 *
 *  语义：等待队列 FIFO-ish；**fail-open**——等锁超时（90s）就不带锁继续（回到今天的
 *  无锁行为），绝不让锁故障把发送闸死。释放走 try/finally 保证。 */
/** 富交互出站结果计数（P5 双面板融合 2026-08-13）：react/quote 是脆 DOM 操作，
 *  FB 改版即碎——把成败按原因累计进每账号 op_stats（/accounts 暴露），让选择器漂移
 *  在读数上可见（与 inject_health/prerender_miss 同哲学），不必等坐席报障。进程级，
 *  重启清零；best-effort，绝不抛。 */
function bumpOp(entry, key, reason) {
  try {
    if (!entry._opStats) {
      entry._opStats = {
        react_ok: 0, react_fail: {}, quote_applied: 0, quote_degraded: 0,
        manual_out_mirrored: 0, manual_out_reads: 0,
      };
    }
    const s = entry._opStats;
    if (key === "react_ok") s.react_ok++;
    else if (key === "react_fail") s.react_fail[reason || "unknown"] =
      (s.react_fail[reason || "unknown"] || 0) + 1;
    else if (key === "manual_out_mirrored") s.manual_out_mirrored =
      (s.manual_out_mirrored || 0) + 1;
    else if (key === "manual_out_reads") s.manual_out_reads =
      (s.manual_out_reads || 0) + 1;
    else if (key === "quote_applied") s.quote_applied++;
    else if (key === "quote_degraded") s.quote_degraded++;
  } catch (_) { /* 计数失败绝不影响主流程 */ }
}

async function acquireAccountOp(entry, name) {
  const t0 = Date.now();
  while (entry._opLock) {
    if (Date.now() - t0 > 90000) {
      logger.warn({ name }, "account op lock wait timeout → proceeding WITHOUT lock (fail-open)");
      return () => {};
    }
    await entry._opLock.catch(() => {});
  }
  let release;
  entry._opLock = new Promise((resolve) => {
    release = () => { entry._opLock = null; resolve(); };
  });
  entry._opLockName = name;
  return release;
}

/** 按被引用/被回应文本定位线程内的**消息行元素**（P3 校准重构 2026-08-13）。
 *
 *  关键教训（真机校准实锤）：`div[role="row"].innerText` 混入时间戳/发送者/表情 chip，
 *  纯文本匹配必败（target_not_found）。改用本仓**久经考验**的抽取路径——`[role="log"]`
 *  下 aria-label 命中 `MSG_ARIA_RE` 的节点，`parseMsgAria` 抽出干净的 {direction,text}
 *  （与 readbackLastOutgoing/readThreadTail 同源）。定位仍交纯函数 matchQuotedTarget
 *  （宁缺勿滥、歧义弃权）。
 *
 *  DOM↔Node 边界：evaluate 内给每个消息行容器（上溯到 role=row）打 data-cx-mi 序号 +
 *  回传 aria 串；Node 侧 parseMsgAria 解析（复用已测解析器，evaluate 内不能引模块常量）→
 *  matchQuotedTarget 得下标 → `page.$([data-cx-mi=i])` 取真元素（供 Playwright 真 hover）。
 *  返回 {handle, rowsFound, sampleTexts}（诊断字段供失败可观测/选择器漂移排查）。 */
async function locateRowByText(page, wantText) {
  let arias = [];
  try {
    arias = await page.evaluate(() => {
      const region = document.querySelector('[role="log"]') || document.body;
      const MSG = /(消息由.*发送于|Message sent )/i;
      const out = [];
      let i = 0;
      for (const e of Array.from(region.querySelectorAll("[aria-label]"))) {
        if (!MSG.test(e.getAttribute("aria-label") || "")) continue;
        let box = e;
        for (let k = 0; k < 5 && box.parentElement; k++) {
          if (box.getAttribute && box.getAttribute("role") === "row") break;
          box = box.parentElement;
        }
        try { box.setAttribute("data-cx-mi", String(i)); } catch (_) { /* readonly? skip */ }
        out.push({ i, aria: e.getAttribute("aria-label") || "" });
        i++;
      }
      return out;
    });
  } catch (_) { arias = []; }
  const texts = arias.map((a) => {
    const p = parseMsgAria(a.aria);
    return (p && p.text) ? p.text : "";
  });
  const idx = matchQuotedTarget(texts, wantText);
  const sampleTexts = texts.filter((t) => t).slice(-6);
  let handle = null;
  if (idx >= 0) {
    handle = await page.$(`[data-cx-mi="${arias[idx].i}"]`).catch(() => null);
  }
  // 清 data-cx-mi（取到 handle 后立即清，元素不脱离；失败也清，绝不留脏属性）
  await page.evaluate(() => {
    document.querySelectorAll("[data-cx-mi]").forEach((e) => e.removeAttribute("data-cx-mi"));
  }).catch(() => {});
  return { handle, rowsFound: arias.length, sampleTexts };
}

/** 引用回复（P2 双面板融合 2026-08-13，best-effort + degrade-safe）：在打开的线程里
 *  按被引用文本定位目标气泡 → hover 出操作条 → 点「回复」，让 composer 进入引用态，
 *  之后 send 路由正常 type+Enter 即以引用形式发出。**绝不阻断发送**：任一步失败一律
 *  返回 false，调用方走普通发送（引用是增强、不是前提）。 */
async function tryQuoteTarget(page, quotedText) {
  const loc = await locateRowByText(page, quotedText).catch(() => null);
  if (!loc || !loc.handle) {
    logger.warn({ rowsFound: loc && loc.rowsFound },
      "quote: target row not located (aria-based)");
    return false;
  }
  const row = loc.handle;
  try {
    await row.scrollIntoViewIfNeeded().catch(() => {});
    await row.hover();
    await page.waitForTimeout(200);
  } catch (_) { return false; }
  // 悬停后行内出现操作条；「回复」按钮中英 aria 兼容，行内找不到再全页兜底一次
  const SEL_REPLY = '[aria-label="回复"], [aria-label="Reply"], '
    + 'div[role="button"][aria-label*="回复"], div[role="button"][aria-label*="Reply"]';
  let btn = await row.$(SEL_REPLY).catch(() => null);
  if (!btn) btn = await page.$(SEL_REPLY).catch(() => null);
  if (!btn) return false;
  // hover-reveal 的操作条会淡入/重定位——先 hover 按钮本身把它钉稳，再给足超时点击
  // （校准实锤：1500ms 对刚淡入的工具条 not-stable 超时；4000ms + 预 hover 稳定命中）
  if (!(await clickRevealed(page, btn, 4000))) return false;
  await page.waitForTimeout(250);
  return true;
}

/** 点击 hover 淡入的工具条按钮（react/reply 共用）：先 hover 按钮把动画钉稳、再点。
 *  淡入/重定位期 Playwright 判 not-stable → 直接 click 会在紧超时下 TimeoutError。 */
async function clickRevealed(page, btn, timeoutMs) {
  try {
    await btn.scrollIntoViewIfNeeded().catch(() => {});
    await btn.hover().catch(() => {});
    await page.waitForTimeout(250);
    await btn.click({ timeout: timeoutMs || 4000 });
    return true;
  } catch (_) {
    return false;
  }
}

/** 轮询等待 composer 清空（发出成功的确定性信号）。发送后 Messenger 通常瞬间清空，
 *  但慢网/重渲染下可能滞后；轮询避免把「慢」误判成「没发出去」。返回是否已清空。 */
async function waitComposerCleared(page, timeoutMs = 3000) {
  const t0 = Date.now();
  // 首检立即做（成功路径几乎无延迟）；未清空则短间隔重试到超时。
  // eslint-disable-next-line no-constant-condition
  while (true) {
    if (await verifyComposerCleared(page)) return true;
    if (Date.now() - t0 >= timeoutMs) return false;
    await page.waitForTimeout(300);
  }
}

/** 从 composer 区选一个最合适的 <input type=file>：视觉媒体(图片/视频)优先挑 accept 含
 *  image/video 的；音频/文件挑不限制 image 的；都没命中则回落第一个。找不到返回 null。 */
async function pickFileInput(page, mediaType) {
  const infos = await page.$$eval('input[type="file"]', (els) =>
    els.map((el, i) => ({ i, accept: (el.getAttribute("accept") || "").toLowerCase() })));
  if (!infos.length) return null;
  const isVisual = /^(image|photo|video)/.test(String(mediaType || ""));
  let pick = infos.find((x) => isVisual
    ? (x.accept.includes("image") || x.accept.includes("video"))
    : (!x.accept || (!x.accept.includes("image") && !x.accept.includes("video"))));
  if (!pick) pick = infos[0];
  const handles = await page.$$('input[type="file"]');
  return handles[pick.i] || handles[0] || null;
}

/** 等附件预览出现（缩略图旁的「移除」键）再发送，避免回车发空。best-effort（超时也继续）。 */
async function waitForAttachmentPreview(page, timeoutMs = 8000) {
  const removeSel = [
    '[aria-label="移除"]', '[aria-label="删除"]', '[aria-label="移除附件"]',
    '[aria-label="Remove"]', '[aria-label="Remove attachment"]',
  ].join(",");
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    if (await page.$(removeSel)) return true;
    await page.waitForTimeout(400);
  }
  return false;
}

/** 把本地文件挂到 Messenger 输入区并发送（图片/视频/音频/文件通吃）。
 *  找不到 file input 时先点「附加/添加照片或视频」唤出再重试。 */
async function attachAndSend(page, mediaPath, mediaType, caption) {
  let input = await pickFileInput(page, mediaType);
  if (!input) {
    for (const s of [
      '[aria-label="附加文件"]', '[aria-label="选择文件"]', '[aria-label="添加照片或视频"]',
      '[aria-label="Attach a file"]', '[aria-label="Choose a file to upload"]',
      '[aria-label="Add photos/videos"]',
    ]) {
      const b = await page.$(s);
      if (b) { await b.click().catch(() => {}); await page.waitForTimeout(600); break; }
    }
    input = await pickFileInput(page, mediaType);
  }
  if (!input) throw new Error("file input not found");
  await input.setInputFiles(mediaPath);
  await waitForAttachmentPreview(page);
  if (caption) {
    const box = await page.$(SEL_COMPOSER);
    if (box) { await box.click(); await box.type(caption, { delay: 20 }); }
  }
  await page.keyboard.press("Enter");
  await page.waitForTimeout(2500);
}

/** 按 accountId 找已授权 session。 */
function findByAccount(accountId) {
  for (const [, e] of sessions.entries()) {
    if (e.status === "authorized" && e.accountId === String(accountId)) return e;
  }
  return null;
}

const app = express();
app.use(express.json());

// `svc` 是**身份**字段（与 wa-baileys 同款）：桌面壳据它区分「自家边车」与「占了同一
// 端口的别家服务」。只看 ok:true 会把外来服务当自己的用 —— 后端 sidecar 正是踩过这个
// 坑才加了 /api/desktop/ping。旧版本没有该字段 → 壳按「旧版」放行，不影响升级。
app.get("/health", (_req, res) => res.json({ ok: true, svc: "messenger-web", ...workerCodeInfo() }));

// 联调用：查看轮询最近检测到的入站消息（核验入站链路，不依赖主程序）。
app.get("/debug/inbound", (_req, res) => res.json({ recent: RECENT_INBOUND }));

// 联调用：直接跑一遍「消息请求」解析，核验陌生人首次来讯抓取。
app.get("/debug/requests", async (req, res) => {
  const id = String(req.query.id || "");
  const entry = id ? sessions.get(id) : [...sessions.values()].find((e) => e.status === "authorized");
  if (!entry || entry.status !== "authorized") return res.status(404).json({ error: "no authorized session" });
  try {
    if (!entry.reqPage || entry.reqPage.isClosed()) entry.reqPage = await entry.context.newPage();
    const rr = await readRequests(entry.reqPage, entry);
    res.json({ requests: rr.rows, blocked: rr.blocked });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

/** 自身头像取证探针（只读、不导航）：把「判定要用的全部证据」一次性打出来。
 *
 *  存在理由：2026-08-15 事故里错脸能长期存活，根子是没人能看见「页面上到底有哪些
 *  候选、自己那张凭什么认出来」——只能靠猜选择器，猜错了还是静默。实测这版
 *  messenger.com 上所有头像 `alt` 全为空，光看 img 属性永远认不出自己；证据在
 *  **祖先链**（role/aria-label/href）与**页内脚本**（viewer 自身头像 URI）里。
 *  FB 改版后先打这个端点，再动 self_profile.js 的判据。 */
app.get("/debug/self-profile", async (req, res) => {
  const id = String(req.query.id || "");
  const entry = id ? sessions.get(id)
    : [...sessions.values()].find((e) => e.status === "authorized");
  if (!entry || entry.status !== "authorized") {
    return res.status(404).json({ error: "no authorized session" });
  }
  try {
    const raw = await entry.page.evaluate(() => {
      const out = { url: location.href, cands: [], scripts: [] };
      const slices = [];
      try {
        for (const s of document.querySelectorAll("script")) {
          const t = s.textContent || "";
          const i = t.indexOf("CurrentUserInitialData");
          if (i < 0) continue;
          slices.push(t.slice(i, i + 2000));
          if (slices.length >= 4) break;
        }
      } catch (_) {}
      out.slices = slices;
      // 候选图 + 祖先链证据（tag/role/aria-label/href/data-visualcompletion）
      try {
        for (const el of document.querySelectorAll("img, image")) {
          if (out.cands.length >= 25) break;
          const src = el.getAttribute("src") || el.getAttribute("xlink:href")
            || el.getAttribute("href") || "";
          if (!src || src.startsWith("data:")) continue;
          if (!/fbcdn|fna\.fbcdn/.test(src)) continue;   // 表情/静态图不占额度
          const chain = [];
          let p = el;
          for (let hop = 0; p && hop < 8; hop++) {
            chain.push({
              tag: p.tagName,
              role: p.getAttribute && p.getAttribute("role") || "",
              aria: p.getAttribute && p.getAttribute("aria-label") || "",
              href: p.getAttribute && p.getAttribute("href") || "",
              vc: p.getAttribute && p.getAttribute("data-visualcompletion") || "",
            });
            p = p.parentElement;
          }
          let w = Number(el.naturalWidth) || 0;
          let h = Number(el.naturalHeight) || 0;
          try {
            const r = el.getBoundingClientRect();
            w = w || Math.round(r.width);
            h = h || Math.round(r.height);
          } catch (_) {}
          out.cands.push({
            src: src.slice(0, 150), alt: el.getAttribute("alt") || "", w, h, chain,
          });
        }
      } catch (_) {}
      // 页内脚本：viewer 自身头像常在初始数据 blob 里（profile_picture.uri 等）
      try {
        let hits = 0;
        for (const s of document.querySelectorAll("script")) {
          if (hits >= 6) break;
          const t = s.textContent || "";
          for (const kw of ["profile_picture", "profilePicLarge", "profilePicture"]) {
            const i = t.indexOf(kw);
            if (i < 0) continue;
            out.scripts.push({ kw, around: t.slice(Math.max(0, i - 120), i + 400) });
            hits++;
            break;
          }
        }
      } catch (_) {}
      return out;
    });
    res.json({
      url: raw.url,
      ident: parseCurrentUserInitialData(raw.slices),
      pick: pickSelfAvatar(raw.cands, (() => {
        const it = parseCurrentUserInitialData(raw.slices);
        return { selfName: it.name, selfId: it.userId };
      })()),
      cands: raw.cands,
      scripts: raw.scripts,
    });
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// 临时联调：只看当前页状态（不导航），判断会话是否稳定。
app.get("/debug/state", async (req, res) => {
  const id = String(req.query.id || "");
  const entry = id ? sessions.get(id) : [...sessions.values()][0];
  if (!entry) return res.status(404).json({ error: "no session" });
  try {
    // 枚举所有 frame（含 iframe），逐一找 composer / 接受按钮 → 定位 E2EE 对话区所在 frame
    const frameInfo = [];
    for (const f of entry.page.frames()) {
      try {
        const r = await f.evaluate(() => ({
          composer: document.querySelectorAll('div[role="textbox"][contenteditable="true"]').length,
          acceptTxt: Array.from(document.querySelectorAll("div,span,button"))
            .some((el) => /^(接受|Accept)$/.test(((el.innerText || el.textContent || "") + "").trim())),
        }));
        frameInfo.push({ url: (f.url() || "").slice(0, 80), name: f.name(),
          composer: r.composer, accept: r.acceptTxt });
      } catch (_) {
        frameInfo.push({ url: (f.url() || "").slice(0, 80), name: f.name(), err: true });
      }
    }
    const info = await entry.page.evaluate(() => ({
      url: location.href,
      title: document.title,
      hasLoginForm: !!document.querySelector('input[name="pass"], input[type="password"]'),
      convCount: document.querySelectorAll('a[href^="/t/"]').length,
      hasComposer: !!document.querySelector('div[role="textbox"][contenteditable="true"]'),
      webdriver: navigator.webdriver,
      // 列出所有可编辑框 → 区分「消息输入框」vs「搜索框」，用于校准 composer 选择器
      textboxes: Array.from(document.querySelectorAll(
        '[role="textbox"], [contenteditable="true"], input[type="text"], input[type="search"]'
      )).slice(0, 10).map((el) => ({
        tag: el.tagName,
        role: el.getAttribute("role"),
        aria: el.getAttribute("aria-label"),
        placeholder: el.getAttribute("placeholder"),
        editable: el.getAttribute("contenteditable"),
      })),
      // 找文本正好是「接受」的元素，回溯祖先看谁是可点击容器（校准 clickAcceptRequest）
      acceptBtns: (() => {
        const out = [];
        const all = document.querySelectorAll("div,span,a,button");
        for (const el of all) {
          const t = ((el.innerText || el.textContent || "") + "").trim();
          if (!/^(接受|Accept)$/.test(t)) continue;
          const chain = [];
          let cur = el;
          for (let i = 0; i < 4 && cur; i++) {
            chain.push({ tag: cur.tagName, role: cur.getAttribute("role"),
              aria: cur.getAttribute("aria-label"), tabindex: cur.getAttribute("tabindex") });
            cur = cur.parentElement;
          }
          out.push({ text: t, chain });
          if (out.length >= 4) break;
        }
        return out;
      })(),
      bodyHead: (document.body.innerText || "").slice(0, 200),
    }));
    info.frames = frameInfo;
    const cookies = await entry.context.cookies();
    info.cookieNames = cookies.map((c) => c.name).filter((n) =>
      ["c_user", "xs", "datr", "sb", "fr"].includes(n));
    try {
      const buf = await entry.page.screenshot({ type: "png" });
      fs.writeFileSync(path.join(__dirname, "_state_shot.png"), buf);
      info.shot = "_state_shot.png";
    } catch (_) {}
    res.json(info);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// ── 临时联调用：dump 真实 DOM 结构以校准选择器（self_profile / 会话列表）。────────
// 生产可删；只在已授权 session 上工作，navigate 到收件箱首页后取样。
app.get("/debug/dom", async (req, res) => {
  const id = String(req.query.id || "");
  const entry = id ? sessions.get(id) : [...sessions.values()].find((e) => e.status === "authorized");
  if (!entry || entry.status !== "authorized") {
    return res.status(404).json({ error: "no authorized session" });
  }
  try {
    const target = req.query.path ? (MESSENGER_URL.replace(/\/$/, "") + String(req.query.path)) : MESSENGER_URL;
    await entry.page.goto(target, { waitUntil: "domcontentloaded", timeout: 20000 });
    await entry.page.waitForTimeout(2500);
    const dump = await entry.page.evaluate(() => {
      const pick = (el) => el ? {
        tag: el.tagName,
        role: el.getAttribute("role"),
        aria: el.getAttribute("aria-label"),
        alt: el.getAttribute("alt"),
        src: (el.getAttribute("src") || el.getAttribute("xlink:href") || "").slice(0, 120),
        text: (el.innerText || el.textContent || "").slice(0, 80),
      } : null;
      // 身份候选：页面上所有带 alt 的 image/img + 顶栏账号菜单
      const imgs = Array.from(document.querySelectorAll("image[alt], img[alt]"))
        .slice(0, 12).map(pick);
      // 会话列表：含普通/E2EE/请求/marketplace 各类线程链接
      const convAnchors = Array.from(document.querySelectorAll(
        'a[href^="/t/"], a[href^="/e2ee/t/"], a[href*="/requests/t/"], a[href*="/marketplace/t/"]'
      )).slice(0, 12);
      const convs = convAnchors.map((a) => {
        // 尽量爬到承载整行的容器（role=row 或 li 或 grid cell）
        let row = a;
        for (let i = 0; i < 6 && row.parentElement; i++) {
          row = row.parentElement;
          if (row.getAttribute && (row.getAttribute("role") === "row" ||
              row.getAttribute("role") === "gridcell" || row.tagName === "LI")) break;
        }
        return {
          href: a.getAttribute("href"),
          aria: a.getAttribute("aria-label"),
          anchorText: (a.innerText || "").slice(0, 120),
          rowRole: row.getAttribute && row.getAttribute("role"),
          rowText: (row.innerText || "").slice(0, 200),
          imgAlt: (() => { const im = row.querySelector && row.querySelector("image[alt], img[alt]"); return im ? im.getAttribute("alt") : ""; })(),
          imgSrc: (() => { const im = row.querySelector && row.querySelector("image, img"); return im ? (im.getAttribute("src") || im.getAttribute("xlink:href") || "").slice(0, 120) : ""; })(),
        };
      });
      // 全站锚点 href 前缀直方图 + 关键 role 计数，判断会话列表究竟怎么渲染
      const hrefHist = {};
      for (const a of Array.from(document.querySelectorAll("a[href]"))) {
        const h = a.getAttribute("href") || "";
        const key = h.split("?")[0].split("/").slice(0, 3).join("/") || h.slice(0, 20);
        hrefHist[key] = (hrefHist[key] || 0) + 1;
      }
      const roleCount = {};
      for (const r of ["row", "gridcell", "grid", "listitem", "list", "navigation", "main"]) {
        roleCount[r] = document.querySelectorAll(`[role="${r}"]`).length;
      }
      const bodyText = (document.body.innerText || "").slice(0, 1500);
      return { title: document.title, url: location.href, imgs, convs, hrefHist, roleCount, bodyText };
    });
    try {
      const buf = await entry.page.screenshot({ type: "png", fullPage: false });
      fs.writeFileSync(path.join(__dirname, "_authed_shot.png"), buf);
      dump.shot = "_authed_shot.png";
    } catch (_) {}
    res.json(dump);
  } catch (e) {
    res.status(500).json({ error: String(e) });
  }
});

// 联调：跑一遍 readThreadTail（进线程读正文的完整路径），核验解析后的方向/全文/发送者。
app.get("/debug/tail", async (req, res) => {
  const id = String(req.query.id || "");
  const entry = id ? sessions.get(id) : [...sessions.values()].find((e) => e.status === "authorized");
  if (!entry || entry.status !== "authorized") return res.status(404).json({ error: "no authorized session" });
  const thread = String(req.query.thread || "");
  if (!thread) return res.status(400).json({ error: "thread required" });
  // 用独立页跑（不碰 entry.readPage）：轮询 finally 的页面回收曾把本端点刚建的
  // readPage 关掉 → 恒 readOk=false + probe null，误导排查。用完即关。
  let dbgPage = null;
  try { dbgPage = await entry.context.newPage(); } catch (_) { dbgPage = null; }
  if (!dbgPage) return res.status(500).json({ error: "cannot open debug page" });
  let tail = null;
  try { tail = await readThreadTail(entry, thread, dbgPage); } catch (_) { tail = null; }
  let lastIn = null;
  if (Array.isArray(tail)) { for (const m of tail) { if (m.direction === "in") lastIn = m; } }
  // 诊断：导航后到底渲染了什么（定位「只抓到加密横幅」的根因）。
  let probe = null;
  try {
    const page = dbgPage;
    probe = await page.evaluate(() => {
      const all = Array.from(document.querySelectorAll("[aria-label]"))
        .map((e) => e.getAttribute("aria-label") || "");
      const msgLike = all.filter((a) => /(消息由.*发送于|Message sent )/i.test(a));
      return {
        url: location.href,
        hasLog: !!document.querySelector('[role="log"]'),
        rowCount: document.querySelectorAll('div[role="row"]').length,
        iframeCount: document.querySelectorAll("iframe").length,
        ariaTotal: all.length,
        msgLikeCount: msgLike.length,
        msgSample: msgLike.slice(-6).map((a) => a.slice(0, 60)),
        imgCount: document.querySelectorAll("img").length,
        videoCount: document.querySelectorAll("video").length,
        // 最后一条消息行的图片候选明细（诊断媒体漏读：看 src 前缀/自然尺寸/渲染尺寸）
        lastRowImgs: (() => {
          const region2 = document.querySelector('[role="log"]') || document.body;
          const MSG = /(消息由.*发送于|Message sent )/i;
          const nodes = Array.from(region2.querySelectorAll("[aria-label]"))
            .filter((e) => MSG.test(e.getAttribute("aria-label") || ""));
          const last = nodes[nodes.length - 1];
          if (!last) return [];
          let box = last;
          for (let i = 0; i < 4 && box.parentElement; i++) {
            if (box.getAttribute && box.getAttribute("role") === "row") break;
            box = box.parentElement;
          }
          return Array.from(box.querySelectorAll("img")).slice(0, 8).map((im) => {
            const rc = im.getBoundingClientRect();
            const src = im.currentSrc || im.src || "";
            return { src: src.slice(0, 22), natW: im.naturalWidth || 0,
              rW: Math.round(rc.width), rH: Math.round(rc.height) };
          });
        })(),
        // 2026-08-11 诊断：E2EE 线程渲染 19 行但 MSG aria 零命中（FB 改版）——
        // 逐行转储 role=row 的文本/aria/结构，供新解析器选锚点。诊断专用，生产路径不消费。
        rowDump: (() => {
          const region2 = document.querySelector('[role="log"]') || document.body;
          return Array.from(region2.querySelectorAll('div[role="row"]')).slice(-12).map((r) => ({
            t: (r.innerText || "").replace(/\n/g, "⏎").slice(0, 140),
            rowAria: (r.getAttribute("aria-label") || "").slice(0, 80),
            aria: Array.from(r.querySelectorAll("[aria-label]")).slice(0, 6)
              .map((e) => ((e.getAttribute("aria-label") || "") + "@" + e.tagName + (e.getAttribute("role") ? "/" + e.getAttribute("role") : "")).slice(0, 90)),
            hs: Array.from(r.querySelectorAll("h1,h2,h3,h4,h5")).slice(0, 2)
              .map((h) => (h.innerText || "").slice(0, 40)),
            imgAlts: Array.from(r.querySelectorAll("img[alt]")).slice(0, 3)
              .map((im) => (im.getAttribute("alt") || "").slice(0, 40)),
            dirAuto: Array.from(r.querySelectorAll('[dir="auto"]')).slice(0, 4)
              .map((d) => (d.innerText || "").replace(/\n/g, "⏎").slice(0, 60)),
          }));
        })(),
        // 区域内 aria-label 去重样本（找新的消息级锚点词形）
        ariaSample: (() => {
          const region2 = document.querySelector('[role="log"]') || document.body;
          const seen = new Set();
          const out = [];
          for (const e of region2.querySelectorAll("[aria-label]")) {
            const a = (e.getAttribute("aria-label") || "").slice(0, 90);
            if (!a || seen.has(a)) continue;
            seen.add(a);
            out.push(a);
            if (out.length >= 30) break;
          }
          return out;
        })(),
      };
    });
  } catch (e) { probe = { error: String(e) }; }
  try { await dbgPage.close(); } catch (_) {}
  res.json({ readOk: tail !== null, count: Array.isArray(tail) ? tail.length : 0, lastIn, tail, probe });
});

app.post("/accounts/restore", async (_req, res) => {
  const restored = await restoreAll();
  res.json({ ok: true, restored });
});

// 人工重登通道（P2 自愈闭环）：cookie 彻底失效/自愈放弃后，运营从后台一键触发——
// 复用**同一 profile 目录**重启上下文并打开 30 分钟交互登录窗口（headed 弹窗，运营在
// 窗口内完成官方账密/2FA）。不新建 loginId → 不堆积孤儿 Chromium profile，登录成功后
// account_id 不变，编排器/收件箱无需任何重绑。:id 可为 account_id 或 login_id。
app.post("/accounts/:id/relogin", async (req, res) => {
  const want = String(req.params.id || "");
  let loginId = "";
  // 先按 login_id 直查，再按 account_id 反查（不健康会话多半未 authorized，
  // findByAccount 只认 authorized → 这里全量扫）。
  if (sessions.has(want)) {
    loginId = want;
  } else {
    for (const [id, e] of sessions.entries()) {
      if (String(e.accountId || "") === want) { loginId = id; break; }
    }
  }
  // 内存无会话（崩溃后被清）→ 磁盘 profile 还在也可重登
  if (!loginId && fs.existsSync(path.join(SESSIONS_DIR, want))) loginId = want;
  if (!loginId) {
    return res.status(404).json({ ok: false, error: "no session/profile found for id" });
  }
  try {
    const old = sessions.get(loginId);
    const proxyUrl = (old && old.proxyUrl) || "";
    // 压住崩溃自愈竞态：context.close 会触发 scheduleRecovery → 用 _recovering 占位。
    _recovering.add(loginId);
    try {
      if (old) {
        stopPolling(old);
        sessions.delete(loginId);
        try { await old.context.close(); } catch (_) {}
      }
      _recoveryAttempts.delete(loginId); // 人工介入 → 自愈放弃计数清零
      const _t = _slowRetryTimers.get(loginId); // 撤掉排队中的慢重试（人工接管）
      if (_t) { clearTimeout(_t); _slowRetryTimers.delete(loginId); }
      const entry = await startLogin(loginId, proxyUrl, false); // 交互窗口 30min
      try { await entry.page.bringToFront(); } catch (_) {}
      logger.info({ loginId }, "manual relogin window opened (30min interactive watch)");
      res.json({ ok: true, login_id: loginId, status: entry.status });
    } finally {
      _recovering.delete(loginId);
    }
  } catch (e) {
    logger.error({ e, loginId }, "relogin failed");
    res.status(500).json({ ok: false, error: String(e) });
  }
});

// P3：托管某账号的 E2EE 恢复 PIN（落 sessions/<loginId>.secrets.json，绝不入日志）。
// :id 兼容 login_id / account_id（与 relogin 同解析）。body {pin:"123456"}；pin="" 清除。
// 配好后：恢复/自愈/半死自愈钩子撞到 PIN 浮层即自动输入——「重启后消息读不到」自愈闭环。
app.post("/accounts/:id/e2ee-pin", async (req, res) => {
  const want = String(req.params.id || "");
  let loginId = "";
  if (sessions.has(want)) {
    loginId = want;
  } else {
    for (const [id, e] of sessions.entries()) {
      if (String(e.accountId || "") === want) { loginId = id; break; }
    }
  }
  if (!loginId && fs.existsSync(path.join(SESSIONS_DIR, want))) loginId = want;
  if (!loginId) {
    return res.status(404).json({ ok: false, error: "no session/profile found for id" });
  }
  const raw = String((req.body && req.body.pin) ?? "");
  const secrets = loadSecrets(loginId);
  if (raw === "") {
    delete secrets.e2ee_pin;
    if (!saveSecrets(loginId, secrets)) {
      return res.status(500).json({ ok: false, error: "persist failed" });
    }
    logger.info({ loginId }, "e2ee pin cleared");
    return res.json({ ok: true, login_id: loginId, pin_set: false });
  }
  const pin = normalizePin(raw);
  if (!pin) {
    return res.status(400).json({ ok: false, error: "invalid pin (expect 4-12 digits)" });
  }
  secrets.e2ee_pin = pin;
  if (!saveSecrets(loginId, secrets)) {
    return res.status(500).json({ ok: false, error: "persist failed" });
  }
  const entry = sessions.get(loginId);
  if (entry) {
    // 新 PIN → 重置尝试预算与状态，下一个心跳/看门狗 tick 即可用它自愈
    entry._pinTries = 0;
    entry._pinLastTs = 0;
    if (entry._pinState === "missing" || entry._pinState === "failed") {
      entry._pinState = "";
    }
  }
  logger.info({ loginId }, "e2ee pin stored (auto-restore armed)");
  // B70（实施67 P1-9）：托管即重放——不再死等「下次浮层出现」。异步驱动
  // （不阻塞响应），结果回写 entry._pinState 经 /accounts 心跳出网给上层
  // 读进度。会话不在线 → replay:"offline"（下次上线由既有自愈链接管）。
  let replay = "offline";
  if (entry && entry.page) {
    replay = "started";
    setImmediate(() => {
      driveE2eePinReplay(loginId, entry).then((r) => {
        logger.info({ loginId, attempted: r.attempted, healed: r.healed,
          state: r.state }, "e2ee pin replay after store");
      }).catch(() => {});
    });
  }
  res.json({ ok: true, login_id: loginId, pin_set: true, replay });
});

// P0（2026-08-14 必答弹窗配套）：立即验证所存 PIN——「所存即所验」。
// 只有页面正挂着 PIN 浮层时才能真验（输入→浮层消失=通过）；浮层不在场无从验证，
// 如实回 prompt:false（PIN 已武装，下次浮层出现由自愈钩子自动输入）。刻意**不主动
// 导航**去钓浮层（零标已读副作用纪律，与 maybeAutoPinSelfHeal 同款克制）。
// 返回 {verified: true|false|null, prompt, state}——null=本次无法当场判定。
app.post("/accounts/:id/e2ee-pin/verify", async (req, res) => {
  const want = String(req.params.id || "");
  let entry = sessions.get(want) || null;
  if (!entry) {
    for (const e of sessions.values()) {
      if (String(e.accountId || "") === want) { entry = e; break; }
    }
  }
  if (!entry || !entry.page) {
    return res.json({ ok: true, verified: null, prompt: false, state: "offline" });
  }
  const loginId = entry._loginId || "";
  if (!getE2eePin(loginId)) {
    return res.json({ ok: true, verified: false, prompt: false, state: "missing" });
  }
  try {
    const prompt = await detectPinPrompt(entry.page);
    if (!prompt) {
      return res.json({
        ok: true, verified: null, prompt: false,
        state: String(entry._pinState || ""),
      });
    }
    // 人工显式验证 → 重置尝试预算（绝不因旧失败计数把这次显式请求闸掉）
    entry._pinTries = 0;
    entry._pinLastTs = 0;
    const healed = await tryAutoE2eePin(loginId, entry, entry.page).catch(() => false);
    return res.json({
      ok: true, verified: !!healed, prompt: true,
      state: String(entry._pinState || ""),
    });
  } catch (e) {
    logger.debug({ e: String((e && e.message) || e), loginId }, "e2ee pin verify failed");
    return res.json({ ok: true, verified: null, prompt: false, state: "error" });
  }
});

// 优雅关闭端点（Windows 强杀不触发信号处理 → 用它先刷盘再退出，防登录丢失）。
app.post("/shutdown", async (_req, res) => {
  res.json({ ok: true });
  setTimeout(() => gracefulShutdown("http"), 200);
});

// 冷启期间被 cancel 的 login_id：startLogin 是后台任务，取消时它的浏览器可能还没起来，
// 等它起来后必须立刻收掉，否则留下一个谁也管不着的孤儿 Chromium 窗口。
const _cancelledBoots = new Set();

/** 删掉「没登成」留下的空壳 profile（best-effort，延迟等浏览器彻底放开文件句柄）。
 *  不删会越攒越多，而每次服务启动 restoreAll 都要把它们逐个拉起浏览器、等 10s 判未授权、
 *  再盯 10 分钟——全是噪音窗口。已授权的 profile 绝不碰，那是免重登的登录资产。 */
function purgeProfile(loginId, attempt = 0) {
  // Chromium 关闭后还会攥着 BrowserMetrics/*.pma 之类的句柄一小会儿，首删常被拒 → 退避重试。
  const delays = [2000, 6000, 15000];
  if (attempt >= delays.length) {
    logger.debug({ loginId }, "purge profile gave up (still locked)");
    return;
  }
  setTimeout(() => {
    const dir = path.join(SESSIONS_DIR, loginId);
    try {
      if (!fs.existsSync(dir)) return;
      fs.rmSync(dir, { recursive: true, force: true });
      logger.debug({ loginId }, "purged unauthorized profile");
    } catch (_) {
      purgeProfile(loginId, attempt + 1);
    }
  }, delays[attempt]);
}

app.post("/login/start", async (req, res) => {
  try {
    const loginId = newLoginId();
    const proxyUrl = (req.body && req.body.proxy_url) || "";
    // 表单中继（Form-Relay）交互登录：Python 侧仅在 interactive_login 开启时下发 interactive:true。
    // → startLogin 走无窗口模式（offscreen / new_headless），登录页由应用侧原生表单驱动。
    const interactive = !!(req.body && req.body.interactive);
    // P3：接号时可顺带托管 E2EE 恢复 PIN（可选）——首登若撞「恢复加密聊天」浮层
    // 立即自动输入，接号 SOP 里不再依赖运营记得手动处理那一屏。非法 PIN 静默忽略
    // （登录流程照走，只是少了自动恢复；/accounts/:id/e2ee-pin 随时可补）。
    const bootPin = normalizePin(String((req.body && req.body.e2ee_pin) || ""));
    if (bootPin) {
      const s0 = loadSecrets(loginId);
      s0.e2ee_pin = bootPin;
      saveSecrets(loginId, s0);
    }
    // 不等浏览器起来就回：Chromium 冷启 + goto messenger.com + 首帧截图实测 ~36s，
    // 远超调用方 20s 超时 → 请求被判失败，UI 报「连接服务未运行」，而服务其实好好的
    // （这正是 Messenger 接入一直走不通的根因）。契约本就对齐 baileys：start 只发 login_id，
    // 登录页截图由 /login/:id/status 送达，前端每 2.5s 轮询本就在等它。
    // 占位会话先入表——startLogin 内部要到浏览器就绪才 sessions.set，中间这段空窗期若
    // 表里没有该 id，轮询会拿到 "session not found" 并被归一化成 expired。
    sessions.set(loginId, {
      status: "pending", booting: true, qrImage: "", accountId: "",
      name: "", avatarUrl: "", createdAt: Date.now(),
      proxyUrl: proxyUrl || "", seen: new Map(), pollTimer: null,
      interactive,
    });
    startLogin(loginId, proxyUrl, false, interactive).then(() => {
      if (!_cancelledBoots.delete(loginId)) return;
      const cur = sessions.get(loginId);
      if (!cur) return;
      _intentionalClose.add(loginId); // 同 cancel：这是我们自己收的，不是崩溃
      stopPolling(cur);
      if (cur.context) cur.context.close().catch(() => {});
      sessions.delete(loginId);
      if (cur.status !== "authorized") purgeProfile(loginId);
      logger.info({ loginId }, "boot finished after cancel - context closed");
    }).catch((e) => {
      logger.error({ e, loginId }, "start failed (background boot)");
      _cancelledBoots.delete(loginId);
      const cur = sessions.get(loginId);
      // 只在仍是占位时置终态：startLogin 若已换上真 entry，它自己的状态机说了算。
      // reason_code 让前端出「怎么办」而非笼统失败——冷启就崩多为 Chromium/依赖异常，
      // 归 login_failed（service_down 是 Python 侧连不上 Node 的语义，不混用）。
      if (cur && cur.booting) { cur.status = "failed"; cur.detail = String(e); cur.reasonCode = "login_failed"; }
      // 启动就失败 → profile 必然是空壳，不清会留到下次开机被 restore 扫到
      if (!cur || cur.status !== "authorized") purgeProfile(loginId);
    });
    res.json({ login_id: loginId, qr_image: "", status: "pending" });
  } catch (e) {
    logger.error({ e }, "start failed");
    res.status(500).json({ error: String(e) });
  }
});

app.get("/login/:id/status", async (req, res) => {
  const entry = sessions.get(req.params.id);
  if (!entry) return res.json({ status: "expired", detail: "session not found" });
  // 浏览器还在冷启（start 已秒回但 context/page 尚未就绪）：如实回 pending，别去碰还不存在
  // 的 page。前端此时显示「准备环境」，截图在浏览器就绪后的下一轮轮询自然到达。
  if (entry.booting && !entry.page) {
    // reason_code 必须带上：冷启失败（Chromium/依赖异常）正是走这条早退分支——
    // 后台 boot 的 catch 已把 reasonCode 置好，这里漏字段等于原因码永远送不出去，
    // 前端只能显示笼统失败。（2026-08-02 实锤：上一版就漏在这里。）
    return res.json({ status: entry.status || "pending", account_id: "",
                      name: "", avatar_url: "", qr_image: "",
                      detail: String(entry.detail || ""),
                      reason_code: String(entry.reasonCode || "") });
  }
  // 未授权时先复检登录态（看门狗超时/慢登录的补救）；仍未登录才刷新登录页截图。
  if (entry.status !== "authorized") {
    const ok = await promoteIfLoggedIn(req.params.id, entry);
    if (!ok) {
      try { entry.qrImage = await snapshot(entry.page); } catch (_) {}
    }
  }
  res.json({
    status: entry.status,
    account_id: entry.accountId,
    name: entry.name || "",
    avatar_url: entry.avatarUrl || "",
    qr_image: entry.status === "authorized" ? "" : entry.qrImage,
    // 结构化失败原因（契约枚举，Python 侧 _poll 透传、前端按码出「怎么办」）；
    // 无则空串，前端回落通用失败文案。
    reason_code: String(entry.reasonCode || ""),
    // 实时提示码（**非终态**，与 reason_code 同一套词汇）：此刻登录页在要什么。
    // 授权后清空——已经登上了还挂着「需要二次验证」是纯噪音。
    hint_code: entry.status === "authorized" ? "" : String(entry.hintCode || ""),
  });
});

// 表单中继（Form-Relay）只读探针：把登录页此刻的分类段翻成「应用该渲染哪一步原生表单」
// （credentials / twofactor / e2ee_pin / checkpoint / done / wait）。复用看门狗每 1.5s 缓存的
// entry.lastStage —— 刻意不在本 handler 里重新抓页：那会与看门狗抢同一 page、徒增延迟、还可能
// 读到过渡态。纯读、零副作用；headed / 截图预览旧路径与 headless 交互新路径都能读它，是否走
// 中继链由前端 + 默认关的开关决定，与本端点是否存在无关。截图仅在需要回落（checkpoint / 认不出
// 等待）时随包带出，常规账密 / 2FA 步走原生表单不背截图流量。
app.get("/login/:id/relay-step", (req, res) => {
  const entry = sessions.get(req.params.id);
  if (!entry) {
    return res.json({ status: "expired", booting: false, step: "wait",
                      fields: [], error: false, escalate: false, code: "", qr_image: "" });
  }
  const authorized = entry.status === "authorized";
  // 冷启中（占位 entry 无 page）/ 首个看门狗 tick 之前：lastStage 为 undefined → 归 wait。
  const rs = relayStepFromStage(entry.lastStage, { authorized });
  const needShot = !authorized && (rs.escalate || rs.step === "wait");
  // B64 三期：泛 checkpoint 且看门狗判出「等手机确认」子味 → code 细化为 device_confirm
  //（前端据此切手机主视觉 + 三步清单）；account_locked 等具体码不受影响。
  const code = (rs.code === "checkpoint" && entry.lastFlavor === "device_confirm")
    ? "device_confirm" : rs.code;
  res.json({
    status: entry.status || "pending",
    booting: !!(entry.booting && !entry.page),
    step: rs.step,
    fields: rs.fields,
    error: rs.error,
    escalate: rs.escalate,
    code: code,
    qr_image: needShot ? String(entry.qrImage || "") : "",
  });
});

// 表单中继填页：按计划把字段值填进登录页（多重 :visible 选择器回落，仿 tryAutoE2eePin）。
// 返回 {filled, notFound}——任一字段全落空即 notFound 命中，调用方据此结构化失败回落，绝不
// 盲填错框。逐字延迟输入（delay:70）走拟人节奏，与既有 PIN 输入同风格。
async function relayFillFields(page, plan, values) {
  const filled = [];
  const notFound = [];
  for (const f of (plan.fields || [])) {
    if (!(f.name in values)) continue;
    const sel = (f.selectors || []).map((s) => `${s}:visible`).join(", ");
    try {
      const loc = page.locator(sel).first();
      if (!(await loc.count())) { notFound.push(f.name); continue; }
      await loc.click({ timeout: 3000 });
      await loc.fill("", { timeout: 3000 }).catch(() => {});
      await loc.type(String(values[f.name]), { delay: 70, timeout: 15000 });
      filled.push(f.name);
    } catch (_) {
      notFound.push(f.name);
    }
  }
  return { filled, notFound };
}

// B64 二期（2026-08-24，手机确认后卡死实录）：检查点「继续」推进——用户在手机 App
// 确认「是本人登录」后，headless 登录页停在「请验证你的 Facebook 帐户」并等页面上那个
// 「继续 / This was me」按钮被点（窗口离屏，用户点不到）→ 整个登录卡死。此函数替用户点它。
// 与 relayClickSubmit 分开：词表更宽（含 This was me / 是我本人 / 是的），且**不回车兜底**
// ——检查点页回车常触发「重新发码」等副作用，只认明确的推进按钮，点不到就如实回 false
// （前端保持「等手机确认」态，下一轮页面若已自行前进由分类器接管）。
async function relayClickContinue(page) {
  const RE = /^(继续|繼續|下一步|确认|確認|确定|確定|是的|是我本人|这是我本人|完成|Continue|Next|This\s+Was\s+Me|Yes,?\s*This\s+Was\s+Me|Yes|Confirm|Done|OK)$/i;
  try {
    const b = page.locator('div[role="button"]:visible, button:visible, [role="button"]:visible')
      .filter({ hasText: RE }).first();
    if (await b.count()) { await b.click({ timeout: 3000 }); return true; }
  } catch (_) {}
  // aria-label 兜底（FB 部分检查点按钮文字在 aria-label 而非可见文本里）
  try {
    for (const lab of ["继续", "Continue", "This was me", "确认", "Confirm"]) {
      const b = page.locator(`[aria-label="${lab}"]:visible`).first();
      if (await b.count()) { await b.click({ timeout: 3000 }); return true; }
    }
  } catch (_) {}
  return false;
}

// 提交：先试计划里的 CSS 选择器，全落空再按可见按钮文案兜底（多语，仿 tryAutoE2eePin），
// 最后回车兜底。任一成功即返回 true。
async function relayClickSubmit(page, plan) {
  for (const s of (plan.submit || [])) {
    try {
      const b = page.locator(`${s}:visible`).first();
      if (await b.count()) { await b.click({ timeout: 3000 }); return true; }
    } catch (_) {}
  }
  try {
    const b = page.locator('div[role="button"], button')
      .filter({ hasText: /^(继续|登录|登錄|確認|确认|提交|完成|Continue|Log In|Login|Submit|Confirm|Done|Next)$/i })
      .first();
    if (await b.count()) { await b.click({ timeout: 3000 }); return true; }
  } catch (_) {}
  try { await page.keyboard.press("Enter"); return true; } catch (_) {}
  return false;
}

// 表单中继写入端：把应用侧原生表单的字段值填进 headless 登录页并提交。fire-and-forget——
// 填完即回，真正的结果（→2FA / 密码错 / 授权）由看门狗 1.5s 内重分类、前端轮询 relay-step
// 观测。服务端复核当前步骤（页面可能已跳步），不匹配即回当前步骤让前端重同步，绝不错填。
// 交互登录（interactive_login）开启时才由 Python 侧经此下发；关闭时 relay_submit_fn=None。
app.post("/login/:id/relay-submit", async (req, res) => {
  const entry = sessions.get(req.params.id);
  if (!entry) return res.json({ ok: false, status: "expired", reason_code: "expired" });
  if (entry.status === "authorized") {
    return res.json({ ok: true, status: "authorized", submitted: false });
  }
  if (entry.booting && !entry.page) {
    return res.json({ ok: false, status: entry.status || "pending", reason_code: "booting" });
  }
  const step = String((req.body && req.body.step) || "");
  const values = (req.body && typeof req.body.values === "object" && req.body.values) || {};
  // 复核：页面可能已从账密跳到 2FA（缓存 lastStage 每 1.5s 刷新）。不匹配即回当前步骤，
  // 前端重渲对应表单——把 email 填进 2FA 页毫无意义且危险。
  const cur = relayStepFromStage(entry.lastStage, { authorized: false });
  if (cur.step !== step) {
    return res.json({ ok: false, reason_code: "step_changed", step: cur.step,
                      fields: cur.fields, error: cur.error, escalate: cur.escalate });
  }
  // B64 二期：检查点「继续」推进——检查点步无输入字段，走 sanitize 会被判 not_fillable
  // 拒掉。这里在 sanitize 之前特判：替用户点登录页的「继续 / This was me」，把手机确认后
  // 卡死的登录推向下一步。点得到=submitted，点不到=如实回（前端保持等待态）。
  if (step === RELAY_STEP.CHECKPOINT) {
    const clicked = await relayClickContinue(entry.page).catch(() => false);
    logger.info({ loginId: req.params.id, clicked }, "relay-continue (checkpoint advance)");
    return res.json({ ok: true, submitted: !!clicked, clicked: !!clicked, step });
  }
  const clean = sanitizeSubmit(step, values);
  if (!clean.accepts) return res.json({ ok: false, reason_code: "not_fillable", step });
  if (!clean.ok) return res.json({ ok: false, reason_code: "missing_fields", missing: clean.missing, step });
  try {
    if (step === RELAY_STEP.E2EE_PIN) {
      // 委托既有 PIN 自动输入机（gate/retry/cooldown/校验 + 更全选择器全在里面）：
      // 存 PIN → 触发。用户显式提交＝一次全新尝试，清退避计数。
      const s0 = loadSecrets(req.params.id);
      s0.e2ee_pin = clean.values.pin;
      saveSecrets(req.params.id, s0);
      entry._pinTries = 0;
      const accepted = await tryAutoE2eePin(req.params.id, entry, entry.page).catch(() => false);
      return res.json({ ok: true, submitted: true, accepted: !!accepted, step });
    }
    const plan = fillPlanFor(step);
    if (!plan) return res.json({ ok: false, reason_code: "not_fillable", step });
    const fr = await relayFillFields(entry.page, plan, clean.values);
    if (fr.notFound.length) {
      // 字段选择器全落空＝页面结构变了 / 步骤已变：交前端回落截图预览，绝不盲提交。
      return res.json({ ok: false, reason_code: "field_not_found",
                        not_found: fr.notFound, filled: fr.filled, step });
    }
    await relayClickSubmit(entry.page, plan);
    logger.info({ loginId: req.params.id, step, filled: fr.filled }, "relay-submit filled + submitted");
    return res.json({ ok: true, submitted: true, filled: fr.filled, step });
  } catch (e) {
    logger.warn({ e: String((e && e.message) || e), loginId: req.params.id, step },
      "relay-submit failed");
    return res.json({ ok: false, reason_code: "submit_failed", step,
                      detail: String((e && e.message) || e) });
  }
});

app.post("/login/:id/cancel", async (req, res) => {
  const entry = sessions.get(req.params.id);
  if (entry) {
    // 冷启途中被取消：浏览器还没起来，删表拦不住后台任务——记下 id，等它起来再收
    if (entry.booting && !entry.context) _cancelledBoots.add(req.params.id);
    _intentionalClose.add(req.params.id); // 主动关，别让自愈把它当崩溃再拉起来
    stopPolling(entry);
    try { await entry.context.close(); } catch (_) {}
    sessions.delete(req.params.id);
    // 没登成就取消 → profile 是空壳，留着只会污染下次开机 restore
    if (entry.status !== "authorized" && !_cancelledBoots.has(req.params.id)) {
      purgeProfile(req.params.id);
    }
  }
  res.json({ ok: true });
});

app.get("/accounts", (_req, res) => {
  const accounts = [];
  for (const [id, e] of sessions.entries()) {
    if (e.status === "authorized") {
      accounts.push({
        login_id: id,
        account_id: e.accountId,
        // 健康细节（P0-2）：Python healthy() 据此识别「status 仍 authorized 但登录态已丢/
        // 轮询早已停摆」的假健康。logged_in 由轮询周期性 pageLoggedIn 复检维护。
        logged_in: e._loggedIn !== false,
        last_poll_ok_ts: Math.floor((e._lastPollOkTs || 0) / 1000),
        last_poll_err: String(e._lastPollErr || ""),
        // P1 身份化：自身昵称/头像（promoteIfLoggedIn 采集），与 WA /accounts 对齐
        pushname: String(e.name || ""),
        avatar_url: String(e.avatarUrl || ""),
        // 规模化可观测：该账号 context 当前打开的页面(标签)数——验证页面回收生效、
        // 也是「N 账号占多少 renderer」的读数入口（稳态应 ~1，活跃时 2-3）。best-effort。
        pages: (() => { try { return e.context ? e.context.pages().length : 0; } catch (_) { return 0; } })(),
        // 入站健康（P0 半死态探测，与 inbox-health 心跳同源）：滚动窗读取成败 +
        // 最近成功入站时刻 + 上次心跳看到的侧栏未读数。零样本时 attempts=0（无判定）。
        read_attempts: (e._readWin || []).length,
        read_fails: (e._readWin || []).filter((ok) => !ok).length,
        last_inbound_ts: Math.floor((e._lastInboundOkTs || 0) / 1000),
        inbox_unread: Number(e._lastUnread || 0),
        // P2：半死态产品提示 + 请求节奏观测（前端热区放开后直接渲染，无需再改 Node）
        inbox_hint_code: String(e._inboxHintCode || ""),
        e2ee_ratio: (e._convStats && e._convStats.count)
          ? (e._convStats.ph / e._convStats.count) : -1,
        conv_count: Number((e._convStats && e._convStats.count) || 0),
        // 预热观测：首连回填队列剩余（空读不出 info 日志，暴露这里才能看见静默排空进度）
        backfill_left: Array.isArray(e._backfillQueue) ? e._backfillQueue.length : 0,
        requests_blocked_until: Math.floor((e._reqBlockedUntil || 0) / 1000),
        requests_every: Number(e._reqEveryEffective || MSG_REQ_EVERY),
        requests_empty_streak: Number(e._reqEmptyStreak || 0),
        // P2 2026-08-15：零行且无结构证据的连续次数（>0 持续增长=请求页漂移/读不到，
        // 与 empty_streak「真没人来」区分）
        requests_suspect_streak: Number(e._reqSuspectStreak || 0),
        // P3 E2EE 自动恢复观测：PIN 是否已托管 + 最近一次自动输入的结局
        //（""=没撞过浮层 / ok / failed / missing）。PIN 明文绝不出网。
        e2ee_pin_set: !!getE2eePin(id),
        e2ee_pin_state: String(e._pinState || ""),
        // P1 自愈战绩：尝试/成败计数 + 时刻（「托管了 PIN 有没有真自愈」不再翻日志）
        pin_heal: e._pinHeal
          || { attempts: 0, ok: 0, fail: 0, last_ts: 0, last_ok_ts: 0 },
        // 富交互出站结果计数（P5）：react/quote 脆 DOM 操作的成败读数（选择器漂移可见）
        op_stats: e._opStats || {
          react_ok: 0, react_fail: {}, quote_applied: 0, quote_degraded: 0,
          manual_out_mirrored: 0, manual_out_reads: 0,
        },
        // P0 2026-08-15 未读驱动强制读取观测：picked=触发次数（按证据级分桶）。
        // 长期 picked 增长但入站不增 → E2EE 密钥仍未恢复（配合 pin_heal 读数定位）。
        unread_forced: e._unreadForceStats
          || { picked: 0, row_unread: 0, fresh_placeholder: 0 },
      });
    }
  }
  // worker 段：boot_ts / code_fp / code_stale（磁盘内容指纹 ≠ 在跑指纹=改了没重启）。
  // M-2 D：restoring（开机/主动 restore 正在进行）+ pending（拉起了但还没授权的会话数与
  // 状态）——Python healthy() 据此把「还在恢复」与「根本没有该账号会话」分开判。
  const pending = [];
  for (const [id, e] of sessions.entries()) {
    if (e.status !== "authorized") {
      pending.push({ login_id: id, status: String(e.status || ""),
                     account_id: String(e.accountId || ""), is_restore: !!e._isRestore });
    }
  }
  res.json({ accounts, worker: { ...workerCodeInfo(), restoring: _restoring,
                                 pending_count: pending.length, pending } });
});

/** composer 缺席时的页面探针快照（诊断用，best-effort 绝不抛）。
 *  字段与 msg_ops.classifyComposerBlock 的判据一一对应。 */
async function probeComposerBlockers(page) {
  let info = {};
  try {
    info = await page.evaluate(() => ({
      url: String(location.href || "").slice(0, 120),
      composerCount: document.querySelectorAll(
        'div[role="textbox"][contenteditable="true"]').length,
      hasLog: !!document.querySelector('[role="log"]'),
      acceptSeen: Array.from(document.querySelectorAll('div[role="button"],button,span'))
        .some((el) => /^(接受|Accept)$/.test(
          (((el.innerText || el.textContent) || "") + "").trim())),
      loginForm: !!document.querySelector('input[name="pass"], input[name="email"]'),
    }));
  } catch (e) {
    info = { evalFailed: String((e && e.message) || e).slice(0, 80) };
  }
  try { info.pinPrompt = await detectPinPrompt(page); } catch (_) { info.pinPrompt = false; }
  return info;
}

// ── B99 发送护栏三件套（实施68 P1-9 残留 ④⑤，0825 CUN2TM/_485 实锤）────────────
// 事故链：E2EE PIN 态整号瘫 → 上游对失败稿反复重投 → 每次都整页重导航/重试 →
// FB 判为滥用弹「你已被暂时阻止」。三道闸：
//   ① 连败指数退避（5s→…→5min，成功清零）：退避窗内直接 429，不碰浏览器；
//   ② PIN 未就绪快速失败（503 e2ee_pin_pending）：composer 交互超时的真身是
//      「加密会话没解锁」，别再当 DOM flake 去撞；
//   ③ 临时封锁检出（页面文案）→ 冻结本号自动发送 + postStatus("blocked") 让
//      Python 接 ban_signal 账号级暂停（原因进拦截横幅）。
const SEND_BLOCKED_FREEZE_MS = Number(process.env.MSG_BLOCKED_FREEZE_MS || 7200000); // 2h

function sendGateCheck(entry) {
  const now = Date.now();
  if ((entry._blockedUntil || 0) > now) {
    return { ok: false, code: 423, reason: "account_blocked",
      retryAfterMs: entry._blockedUntil - now };
  }
  const until = entry._sendBackoffUntil || 0;
  if (until > now) {
    return { ok: false, code: 429, reason: "send_backoff",
      retryAfterMs: until - now };
  }
  return { ok: true };
}

function noteSendFailure(entry, reason) {
  entry._sendFailStreak = (entry._sendFailStreak || 0) + 1;
  const ms = sendFailureBackoffMs(entry._sendFailStreak);
  entry._sendBackoffUntil = Date.now() + ms;
  logger.warn({ loginId: entry._loginId, streak: entry._sendFailStreak,
    backoffMs: ms, reason }, "send failure → backoff armed");
}

function noteSendSuccess(entry) {
  entry._sendFailStreak = 0;
  entry._sendBackoffUntil = 0;
}

/** 页面是否挂着「临时封锁」风控文案；命中即冻结本号自动发送 + 上报 Python。 */
async function maybeMarkBlocked(entry, page) {
  let text = "";
  try {
    text = await page.evaluate(
      () => ((document.body && document.body.innerText) || "").slice(0, 4000));
  } catch (_) { return false; }
  if (!isTemporarilyBlockedText(text)) return false;
  const first = !(entry._blockedUntil && entry._blockedUntil > Date.now());
  entry._blockedUntil = Date.now() + SEND_BLOCKED_FREEZE_MS;
  entry._blockedReason = "temporarily_blocked";
  logger.error({ loginId: entry._loginId, freezeMs: SEND_BLOCKED_FREEZE_MS },
    "platform TEMPORARILY BLOCKED detected → freezing outbound for this account");
  if (first) {
    // fire-and-forget：Python session-status 收 "blocked" → ban_signal 账号级暂停
    postStatus(entry._loginId || "", entry, "blocked",
      "fb_temporarily_blocked").catch(() => {});
  }
  return true;
}

/** send / send-media 共用：首轮等不到 composer 时的恢复一击 + 终局诊断。
 *
 * 2026-08-15 173 实锤：新建 E2EE 线程首开（+浏览器刚重启首次导航）SPA 渲染可超
 * 「2s 稳定 + 10s 等待」预算 → 旧代码直接 500 且**不留任何日志**（/send 里唯一
 * 静默的 5xx 分支），坐席手发与 L2 自动稿双双丢失，排查只能靠后端截断的 httpx 文本。
 * 恢复动作全部发生在「输入任何文字之前」，绝无重复发送风险：
 *   重新导航 → 稳定期 → PIN 自愈（已托管才动手）→ 补点「接受」→ 再等一轮。
 * 返回 { box, accepted, reason, probe }：box 空＝调用方应 500，reason/probe 供
 * 响应体（Python 侧会把 reason_code 带进投递失败日志）与边车 error 日志。
 */
async function recoverComposerOnce(entry, page, jid) {
  logger.warn({ jid }, "send: composer not found in first wait -> re-navigating once");
  try {
    await page.goto(`${MESSENGER_URL}t/${jid}`, {
      waitUntil: "domcontentloaded", timeout: 20000,
    });
  } catch (_) {}
  await page.waitForTimeout(2000);
  try {
    if (getE2eePin(entry._loginId || "") && await detectPinPrompt(page)) {
      entry._pinPromptSeen = true;
      await tryAutoE2eePin(entry._loginId || "", entry, page).catch(() => false);
      await page.waitForTimeout(1000);
    }
  } catch (_) {}
  const accepted = await clickAcceptRequest(page);
  if (accepted) await page.waitForTimeout(2000);
  const box = await page.waitForSelector(SEL_COMPOSER, { timeout: 10000 }).catch(() => null);
  if (box) {
    logger.info({ jid, accepted }, "send: composer recovered after re-navigation");
    return { box, accepted, reason: "", probe: null };
  }
  const probe = await probeComposerBlockers(page);
  const reason = classifyComposerBlock(probe);
  logger.error({ jid, reason, probe }, "send: composer not found after recovery");
  return { box: null, accepted, reason, probe };
}

// ── Q-24 A/B（#298 P0，2026-09-12）：composer 重绑 + 结构化失败 + 待重试队列 ─────────
// DXAPAX / FJM9ER 四份诊断包同型：ElementHandle 抓到手 → Messenger 重绘 composer →
// click/type 抛 `Element is not attached to the DOM` → 500 → Python 只写「发送失败」→
// 连败 backoff 40s 静默。三处止血：
//   ① 输入前**重查** composer 并等 DOM 安静（MutationObserver 300ms 静默，≤2s 预算）再
//      focus → 输入；② detached 族错误**同线程重查重试 1 次**（不重导航），仍失败才
//      re-navigate 一次（recoverComposerOnce）；③ 同会话连败 ≥2 → 待重试队列（60s 一拍、
//      ≤3 次）+ postStatus("send_stuck") 让工作台铃铛出声，不再只靠 40s backoff 静默。
// 失败一律经 failSend 出网：{ok:false, code, reason, detail, retry_after_ms, retries}。
const COMPOSER_STABLE_BUDGET_MS = Number(process.env.MSG_COMPOSER_STABLE_MS || 2000);
const COMPOSER_QUIET_MS = 300;

/** 重查 composer → 等其所在行 DOM 安静 → 取**新鲜** handle 并 focus。找不到返回 null。 */
async function resolveComposerStable(page, { timeoutMs = 10000 } = {}) {
  const first = await page.waitForSelector(SEL_COMPOSER, { timeout: timeoutMs }).catch(() => null);
  if (!first) return null;
  // DOM 安静期：composer 父容器 quiet 300ms 或预算耗尽即放行（预算内最多等 2s，不拖慢常态发送）
  try {
    await page.evaluate(({ sel, quiet, budget }) => new Promise((resolve) => {
      const el = document.querySelector(sel);
      if (!el) return resolve(false);
      let root = el;
      for (let i = 0; i < 4 && root.parentElement; i++) root = root.parentElement;
      let timer = setTimeout(() => { obs.disconnect(); resolve(true); }, quiet);
      const hard = setTimeout(() => { obs.disconnect(); clearTimeout(timer); resolve(false); }, budget);
      const obs = new MutationObserver(() => {
        clearTimeout(timer);
        timer = setTimeout(() => { obs.disconnect(); clearTimeout(hard); resolve(true); }, quiet);
      });
      obs.observe(root, { childList: true, subtree: true, attributes: true });
    }), { sel: SEL_COMPOSER, quiet: COMPOSER_QUIET_MS, budget: COMPOSER_STABLE_BUDGET_MS });
  } catch (_) { /* 探针失败按已稳定 */ }
  // 安静期后重新取 handle（安静期内可能又换了一轮节点）
  const fresh = await page.$(SEL_COMPOSER).catch(() => null);
  const box = fresh || first;
  try { await box.focus(); } catch (_) { /* focus 失败由后续 click 兜底 */ }
  return box;
}

/** 通话 / 来电浮层探测（E 段）：在场则尝试点「关闭/拒绝/结束」，返回 { seen, dismissed }。 */
async function detectCallOverlay(page) {
  let txt = "";
  try {
    txt = await page.evaluate(() => {
      const dlg = document.querySelector('[role="dialog"], [aria-modal="true"]');
      return ((dlg && dlg.innerText) || "").slice(0, 1500);
    });
  } catch (_) { return { seen: false, dismissed: false }; }
  if (!isCallOverlayText(txt)) return { seen: false, dismissed: false };
  let dismissed = false;
  try {
    const btn = page.locator('[role="dialog"] [role="button"], [role="dialog"] button, [aria-modal="true"] [role="button"]')
      .filter({ hasText: /^(关闭|拒绝|结束|忽略|Close|Decline|Dismiss|Ignore|End call|Leave)$/i }).first();
    if (await btn.count()) { await btn.click({ timeout: 2000 }); dismissed = true; }
    else { await page.keyboard.press("Escape").catch(() => {}); }
  } catch (_) { dismissed = false; }
  logger.warn({ dismissed, snippet: txt.slice(0, 80) }, "send: call overlay on screen");
  return { seen: true, dismissed };
}

/**
 * 统一失败出口：退避计数 + 每会话连败 + 待重试队列 + 铃铛 + 结构化响应。
 * 绝不换文案、绝不在这里重发（重发只在 retry tick，且回读去重）。
 */
function failSend(entry, res, { code = "", reason = "", detail = "", jid = "", status = 0,
  retryAfterMs = 0, manual = false, text = "", quoted = null, mediaPath = "", mediaType = "",
  caption = "", skipBackoff = false, extra = null } = {}) {
  const c = normalizeSendFailCode({ reason, message: detail, status });
  if (!skipBackoff) noteSendFailure(entry, reason || c);
  if (!entry._jidFailStreak) entry._jidFailStreak = new Map();
  const streak = jid ? bumpJidFailStreak(entry._jidFailStreak, jid, false) : 0;
  let queued = false;
  if (jid && shouldQueueRetry({ streak, code: c })) {
    if (!entry._sendRetryQueue) entry._sendRetryQueue = new Map();
    const r = retryQueueUpsert(entry._sendRetryQueue, jid, {
      text, manual, quoted, code: c, mediaPath, mediaType, caption,
    });
    queued = r.queued;
    if (queued) {
      logger.warn({ loginId: entry._loginId, jid, streak, code: c, replaced: r.replaced },
        "send: consecutive failures → queued for background retry (60s x3) + agent bell");
      postStatus(entry._loginId || "", entry, "send_stuck",
        `${c}|jid=${jid}|streak=${streak}|preview=${String(text || caption || "[media]").slice(0, 40)}`)
        .catch(() => {});
      ensureRetryTicker(entry);
    }
  }
  const body = sendFailBody({
    code: c, reason: reason || c, detail,
    retryAfterMs: retryAfterMs || Math.max(0, (entry._sendBackoffUntil || 0) - Date.now()),
    retries: Math.max(0, streak - 1),
    extra: { accepted: false, manual: !!manual, queued, streak, ...(extra || {}) },
  });
  logger.warn({ loginId: entry._loginId, jid, code: c, reason, streak, queued, manual,
    detail: String(detail || "").slice(0, 160) }, "send failed (structured)");
  return res.status(status || sendFailHttpStatus(c)).json(body);
}

/** 成功出口：清连败 + 出队（人工/自动任一成功即视为该会话通了）。 */
function noteJidSendOk(entry, jid) {
  noteSendSuccess(entry);
  if (entry._jidFailStreak && jid) bumpJidFailStreak(entry._jidFailStreak, jid, true);
  if (entry._sendRetryQueue && jid && entry._sendRetryQueue.has(String(jid))) {
    retryQueueSettle(entry._sendRetryQueue, jid, { ok: true });
    logger.info({ loginId: entry._loginId, jid }, "send: retry queue item cleared by a successful send");
  }
}

/** 待重试队列的后台拍子：每 entry 一个 ticker，队列空即停。 */
function ensureRetryTicker(entry) {
  if (entry._retryTicker) return;
  entry._retryTicker = setInterval(() => {
    retryTick(entry).catch((e) => logger.debug({ e }, "retry tick failed"));
  }, 15000);
  if (typeof entry._retryTicker.unref === "function") entry._retryTicker.unref();
}

async function retryTick(entry) {
  const q = entry._sendRetryQueue;
  if (!q || !q.size) {
    if (entry._retryTicker) { clearInterval(entry._retryTicker); entry._retryTicker = null; }
    return;
  }
  if (_shuttingDown || entry.status !== "authorized" || !entry.page) return;
  if (!sendGateCheck(entry).ok) return; // 退避 / 封锁窗内不撞
  const due = retryQueueDue(q);
  if (!due.length) return;
  const item = due[0];
  if (item.mediaPath) {
    // 媒体重试暂不做（附件链需 file chooser 全程），到点直接终局报告，不静默
    const st = retryQueueSettle(q, item.jid, { ok: false, error: "media_retry_unsupported",
      maxTries: 1 });
    if (st.final) reportRetryFinal(entry, item, "media_retry_unsupported");
    return;
  }
  const release = await acquireAccountOp(entry, "send_retry");
  try {
    logger.info({ loginId: entry._loginId, jid: item.jid, tries: item.tries + 1 },
      "send retry: attempting queued message");
    const r = await performTextSend(entry, item.jid, item.text, { quoted: item.quoted, manual: item.manual,
      retry: true });
    if (r.ok) {
      retryQueueSettle(q, item.jid, { ok: true });
      noteJidSendOk(entry, item.jid);
      logger.info({ loginId: entry._loginId, jid: item.jid, alreadyOnPage: !!r.alreadyOnPage },
        "send retry: delivered");
      // 回抄给 Python：失败留痕行改标 resent（origin=retry_queue 让 ingest 路由认领），坐席看到「已补发」
      await postIngest({
        platform: "messenger", account_id: entry.accountId, chat_key: item.jid,
        name: "", text: item.text, ts: Math.floor(Date.now() / 1000),
        msg_id: r.messageId || synthMsgId({ chatKey: item.jid, direction: "out", tsLabel: "", text: item.text, mediaRef: "" }),
        direction: "out", origin: "retry_queue", is_request: false, request_category: "",
      }).catch(() => {});
      postStatus(entry._loginId || "", entry, "send_recovered",
        `jid=${item.jid}|tries=${item.tries + 1}`).catch(() => {});
    } else {
      const st = retryQueueSettle(q, item.jid, { ok: false, error: r.reason || r.code });
      logger.warn({ loginId: entry._loginId, jid: item.jid, code: r.code, reason: r.reason,
        tries: st.item ? st.item.tries : "-", final: st.final }, "send retry: failed");
      if (st.final) reportRetryFinal(entry, item, r.code || r.reason || "retry_exhausted");
    }
  } finally {
    release();
  }
}

function reportRetryFinal(entry, item, code) {
  logger.error({ loginId: entry._loginId, jid: item.jid, code, tries: item.tries },
    "send retry: exhausted → final failure reported to agent");
  postStatus(entry._loginId || "", entry, "send_stuck_final",
    `${code}|jid=${item.jid}|tries=${item.tries}|preview=${String(item.text || "[media]").slice(0, 40)}`)
    .catch(() => {});
}

/**
 * 文本发送核心（/send 与 retryTick 共用）。返回 { ok, code, reason, detail, accepted, verified,
 * quoted, messageId, alreadyOnPage }。不写响应、不计退避——由调用方决定。
 * retry=true 时先回读页面：我们的文本已是最后一条本方气泡 → 视为已发出（防双发，VNA2Q3 教训）。
 */
async function performTextSend(entry, jid, text, { quoted = null, manual = false, retry = false } = {}) {
  const page = entry.page;
  const fastPath = sendFastPathEligible(page.url(), jid);
  if (!fastPath) {
    try {
      await page.goto(`${MESSENGER_URL}t/${jid}`, { waitUntil: "domcontentloaded", timeout: 20000 });
    } catch (e) {
      return { ok: false, code: "thread_not_found", reason: "nav_failed",
        detail: String((e && e.message) || e).slice(0, 200) };
    }
    await page.waitForTimeout(2000);
  }
  if (getE2eePin(entry._loginId || "") && await detectPinPrompt(page)) {
    entry._pinPromptSeen = true;
    const healed = await tryAutoE2eePin(entry._loginId || "", entry, page).catch(() => false);
    if (healed) {
      await page.goto(`${MESSENGER_URL}t/${jid}`, { waitUntil: "domcontentloaded", timeout: 20000 })
        .catch(() => {});
      await page.waitForTimeout(1500);
    }
  }
  if (await detectPinPrompt(page)) {
    entry._pinPromptSeen = true;
    if (!getE2eePin(entry._loginId || "")) entry._pinState = "missing";
    return { ok: false, code: "e2ee_pin_pending", reason: "e2ee_pin_pending",
      detail: "e2ee recovery pin prompt on screen (session locked; send would fail)",
      extra: { pin_set: !!getE2eePin(entry._loginId || "") } };
  }
  const call = await detectCallOverlay(page);
  if (call.seen && !call.dismissed) {
    return { ok: false, code: "call_overlay", reason: "call_overlay",
      detail: "call / incoming-call overlay blocks the composer" };
  }
  if (retry) {
    // 防双发：上一次其实已发出（composer 清空滞后 / 回读超时）→ 页面上已有我们的气泡
    try {
      const rb0 = await readbackLastOutgoing(page, text, 1500);
      if (rb0.found && !rb0.rowFail) {
        return { ok: true, alreadyOnPage: true, accepted: false, verified: true, quoted: false,
          messageId: synthMsgId({ chatKey: jid, direction: "out", tsLabel: "", text, mediaRef: "" }) };
      }
    } catch (_) { /* 回读失败照常重发 */ }
  }
  let accepted = await clickAcceptRequest(page);
  if (accepted) await page.waitForTimeout(2000);
  let box = await resolveComposerStable(page, { timeoutMs: 10000 });
  if (!box) {
    const rec = await recoverComposerOnce(entry, page, jid);
    accepted = accepted || rec.accepted;
    box = rec.box;
    if (!box) {
      await maybeMarkBlocked(entry, page);
      return { ok: false, code: normalizeSendFailCode({ reason: rec.reason || "composer_not_found" }),
        reason: rec.reason || "composer_not_found", accepted,
        detail: `composer not found (${rec.reason || "unknown"})` };
    }
  }
  recordSent(entry, jid, text);
  const outMsgId = synthMsgId({ chatKey: jid, direction: "out", tsLabel: "", text, mediaRef: "" });
  if (!entry._lastOutboundId) entry._lastOutboundId = new Map();
  entry._lastOutboundId.set(String(jid), outMsgId);
  let quoteApplied = false;
  if (quoted && quoted.text) {
    try { quoteApplied = await tryQuoteTarget(page, String(quoted.text)); }
    catch (_) { quoteApplied = false; }
    bumpOp(entry, quoteApplied ? "quote_applied" : "quote_degraded");
    if (!quoteApplied) {
      logger.warn({ jid }, "send: quote target not located → sending without quote (degraded)");
    }
  }
  const clearComposer = async () => {
    try {
      const b = await page.$(SEL_COMPOSER);
      if (b) { await b.click({ timeout: 2000 }); }
      await page.keyboard.press("Control+a");
      await page.keyboard.press("Backspace");
    } catch (_) {}
  };
  const typeIntoComposer = async () => {
    if (text.length > 80 && !text.includes("\n")) {
      await box.type(text.slice(0, 20), { delay: 20 });
      await page.keyboard.insertText(text.slice(20));
      await page.waitForTimeout(150);
      let cur = "";
      try {
        cur = await page.$eval(SEL_COMPOSER,
          (el) => ((el.innerText || el.textContent || "") + "").trim());
      } catch (_) { cur = ""; }
      if (composerTextMatches(cur, text)) return;
      logger.warn({ jid }, "send: insertText integrity check failed → falling back to full typing");
      await page.keyboard.press("Control+a").catch(() => {});
      await page.keyboard.press("Backspace").catch(() => {});
    }
    await box.type(text, { delay: 20 });
  };
  // 输入一击 = 重查稳定 composer → click → type。detached 族错误：同线程重查重试 1 次；
  // 仍 detached → re-navigate 一次再试；第三次仍失败 → composer_detached 终局（不再撞）。
  const inputOnce = async () => {
    await box.click();
    await typeIntoComposer();
  };
  const rebindLog = (stage, err) => logger.warn({ jid, stage,
    err: err ? String(err.message || err).slice(0, 120) : undefined },
    "send: composer detached → rebind (" + stage + ")");
  // 编排在 send_chain.inputWithRebind（可注入回调，node --test 回放 DXAPAX 序列）
  const doRebind = () => inputWithRebind({
    input: inputOnce,
    clear: clearComposer,
    requery: async () => { box = await resolveComposerStable(page, { timeoutMs: 8000 }); return !!box; },
    renavigate: async () => {
      const rec = await recoverComposerOnce(entry, page, jid);
      accepted = accepted || rec.accepted;
      if (rec.box) box = (await resolveComposerStable(page, { timeoutMs: 8000 })) || rec.box;
      return rec;
    },
    log: rebindLog,
  });
  const attemptSend = async () => {
    await doRebind();
    await page.keyboard.press("Enter");
    return await waitComposerCleared(page, 3000);
  };
  let sent;
  try {
    sent = await attemptSend();
    if (!sent) {
      let stillHasOurText = false;
      try {
        const cur = await page.$eval(SEL_COMPOSER,
          (el) => ((el.innerText || el.textContent || "") + "").trim());
        stillHasOurText = cur.length > 0 && text.trim().startsWith(cur.slice(0, 8));
      } catch (_) { stillHasOurText = false; }
      if (stillHasOurText) {
        logger.warn({ jid }, "send: composer not cleared → retrying once");
        await clearComposer();
        sent = await attemptSend();
      }
    }
  } catch (e) {
    const detail = String((e && e.message) || e).slice(0, 300);
    const code = e && e.sendCode ? e.sendCode
      : normalizeSendFailCode({ reason: "exception", message: detail });
    logger.error({ e, jid, manual, message: detail, loginId: entry._loginId, code }, "send failed");
    return { ok: false, code, reason: (e && e.sendReason) || "exception", detail, accepted };
  }
  if (!sent) {
    logger.error({ jid }, "send: composer still not cleared after retry → reporting NOT delivered");
    await maybeMarkBlocked(entry, page);
    return { ok: false, code: "composer_detached", reason: "composer_not_cleared", accepted,
      detail: "composer not cleared after send (message likely not delivered)", extra: { sent: false } };
  }
  const rb = await readbackLastOutgoing(page, text, 5000);
  if (rb.found && rb.rowFail) {
    logger.error({ jid }, "send: bubble rendered with FAIL marker → reporting NOT delivered");
    await maybeMarkBlocked(entry, page);
    return { ok: false, code: "thread_not_found", reason: "bubble_fail_marker", accepted,
      detail: "messenger marked the message as failed to send", extra: { sent: false, verified: true } };
  }
  if (!rb.found) {
    logger.warn({ jid }, "send: composer cleared but readback did not find our bubble "
      + "(treating as delivered, verified=false)");
  }
  return { ok: true, accepted, verified: !!rb.found, quoted: quoteApplied, messageId: outMsgId,
    fastPath };
}

app.post("/accounts/:id/send", async (req, res) => {
  const entry = findByAccount(req.params.id);
  const jid = String((req.body && req.body.jid) || "");
  const text = String((req.body && req.body.text) || "");
  const isManual = !!(req.body && (req.body.manual === true || req.body.manual === 1));
  if (!entry || !entry.page) {
    // M-2 D（#232）：这一行此前静默——ZGKVQB「restored 0 后两小时零日志」的直接原因之一。
    // 收得到（Python 侧历史/同步还在）发不出（边车没有该账号的已授权会话）必须留痕。
    logger.warn({ accountId: String(req.params.id || ""),
      known: [...sessions.entries()].map(([id, e]) => `${id}:${e.status}:${e.accountId || "-"}`),
      restoring: _restoring }, "send: account not connected (no authorized session) → 404");
    // 文案里带 not_logged_in 标记：Python note_send_auth_failure 据此立刻登记 needs_login
    // （账号栏「需重新登录」不必等编排器下一轮探测）。Q-24：七码 login_expired 同出。
    return res.status(404).json(sendFailBody({
      code: "login_expired", reason: "not_logged_in",
      detail: "account not connected (not_logged_in: no authorized session in sidecar; re-login required)",
      extra: { manual: isManual },
    }));
  }
  if (!jid || !text) {
    return res.status(400).json({ ok: false, error: "jid and text required" });
  }
  // M-2 C（#233）：手动发送与自动投递分开配额、手动优先——body.manual=true（Python 人工
  // 路由透传）在 send_backoff 窗内放行**一次**探测性发送（成功即 noteSendSuccess 解锁，
  // 失败照常续退避）。account_blocked（平台临时封锁）不放行：那是平台说的不许发。
  // P3：send 进每账号写操作互斥（与 /react 串行；等锁超时 fail-open 回无锁旧行为）
  const _opRelease = await acquireAccountOp(entry, "send");
  try {
    // B99 ①③：临时封锁冻结 / 连败退避窗内直接快速失败——不碰浏览器（重试风暴
    // 正是风控反噬的燃料），上游拿 code=send_backoff 决定改期而不是立刻再投。
    const gate = sendGateCheck(entry);
    if (!gate.ok) {
      const probeOk = manualProbeDecision({
        manual: isManual, gateReason: gate.reason, backoffUntil: entry._sendBackoffUntil || 0,
        streak: entry._sendFailStreak || 1, lastProbeAt: entry._manualProbeAt || 0,
      }).allow;
      if (probeOk) {
        entry._manualProbeAt = Date.now();
        logger.warn({ loginId: entry._loginId, jid, streak: entry._sendFailStreak,
          backoffLeftMs: gate.retryAfterMs }, "send: manual probe allowed through send_backoff (manual-first, once per window)");
      } else {
        // 退避窗内的失败不再叠退避、不进队列（队列 tick 自己会等窗到期）——但如实出码
        return res.status(gate.code).json(sendFailBody({
          code: "send_backoff", reason: gate.reason, retryAfterMs: gate.retryAfterMs,
          retries: Math.max(0, (entry._sendFailStreak || 0)),
          detail: gate.reason === "account_blocked"
            ? "account temporarily blocked by platform (auto-frozen)"
            : (isManual ? "send backoff active; manual probe already used this window"
                        : "send backoff active after consecutive failures"),
          extra: { manual: isManual, queued: !!(entry._sendRetryQueue && entry._sendRetryQueue.has(jid)) },
        }));
      }
    }
    const t0 = Date.now();
    const quoted = req.body && req.body.quoted;
    const r = await performTextSend(entry, jid, text, { quoted, manual: isManual });
    if (!r.ok) {
      return failSend(entry, res, {
        code: r.code, reason: r.reason, detail: r.detail, jid, manual: isManual, text, quoted,
        extra: { accepted: !!r.accepted, ...(r.extra || {}) },
      });
    }
    noteJidSendOk(entry, jid);   // B99 ④：成功清退避 + Q-24 清会话连败/出队
    // 发送耗时观测：fast=同线程快路是否命中；线上「发送慢」从体感变成可读数
    logger.info({ jid, ms: Date.now() - t0, fast: !!r.fastPath, len: text.length,
                  verified: !!r.verified, quoted: !!r.quoted }, "send ok");
    res.json({ ok: true, delivered: true, message_id: r.messageId, accepted: !!r.accepted, sent: true,
      verified: !!r.verified, quoted: !!r.quoted });
  } catch (e) {
    // M-2 D：Playwright 错误对象经 pino 序列化只剩 {log:[...],name:"Error"}（K9CY6R 实录），
    // message / jid / 是否手动 一并落行；Q-24：异常同样走结构化七码出口（不再裸 reason_code=exception）。
    const detail = String((e && e.message) || e).slice(0, 300);
    logger.error({ e, jid, manual: isManual, message: detail, loginId: entry._loginId }, "send failed (route)");
    return failSend(entry, res, { reason: "exception", detail, jid, manual: isManual, text });
  } finally {
    _opRelease();
  }
});

// 出站表情回应（P3 双面板融合 2026-08-13）：给某条消息挂 Messenger 默认面板表情。
// body { jid, emoji, target_text, target_id? }。
//
// 设计（与引用回复同族的 best-effort，但语义相反——引用失败仍发消息，表情失败就是
// 失败，如实回 ok:false 让坐席看到，绝不静默装成功）：
// ① 面板白名单先拦（msgrPaletteTarget：😂→😆 归一、🙏 等面板外 → unsupported_emoji，
//    绝不硬点「更多表情」网格）；
// ② 目标定位复用引用回复同一纯函数 matchQuotedTarget（宁缺勿滥、歧义弃权 →
//    target_not_found）——Messenger 无 wamid，只能按文本锚定；
// ③ 与 send 同过每账号写操作互斥锁（hover/点击与打字互踩是本锁的存在理由）；
// ④ picker 点击双轨：先按面板字符 textContent 精确匹配，不中按 aria 名称候选
//    （reactAriaCandidates 中英词表）兜底；都不中 → Esc 收场 + palette_not_found。
app.post("/accounts/:id/react", async (req, res) => {
  const entry = findByAccount(req.params.id);
  if (!entry || !entry.page) {
    return res.status(404).json({ ok: false, error: "account not connected" });
  }
  const jid = String((req.body && req.body.jid) || "");
  const emoji = String((req.body && req.body.emoji) || "");
  const targetText = String((req.body && req.body.target_text) || "");
  if (!jid || !emoji || !targetText) {
    return res.status(400).json({ ok: false, reason: "missing_field" });
  }
  const palette = msgrPaletteTarget(emoji);
  if (!palette) {
    return res.status(400).json({ ok: false, reason: "unsupported_emoji" });
  }
  const _opRelease = await acquireAccountOp(entry, "react");
  try {
    const page = entry.page;
    // 同线程快路径与 send 同款；不在目标线程才整页导航
    if (!sendFastPathEligible(page.url(), jid)) {
      await page.goto(`${MESSENGER_URL}t/${jid}`, {
        waitUntil: "domcontentloaded", timeout: 20000,
      });
      await page.waitForTimeout(2000);
    }
    // 定位目标气泡（aria-based，宁缺勿滥：未命中/歧义 → 如实失败 + 诊断字段供校准）
    const loc = await locateRowByText(page, targetText);
    if (!loc.handle) {
      bumpOp(entry, "react_fail", "target_not_found");
      return res.json({ ok: false, reason: "target_not_found",
        rows_found: loc.rowsFound, sample: loc.sampleTexts });
    }
    const row = loc.handle;
    await row.scrollIntoViewIfNeeded().catch(() => {});
    await row.hover();
    await page.waitForTimeout(250);
    // 悬停出操作条 → 「添加心情/React」按钮（行内优先，全页兜底一次）
    const SEL_REACT_BTN = '[aria-label="添加心情"], [aria-label="React"], '
      + 'div[role="button"][aria-label*="心情"], div[role="button"][aria-label*="React"]';
    let btn = await row.$(SEL_REACT_BTN).catch(() => null);
    if (!btn) btn = await page.$(SEL_REACT_BTN).catch(() => null);
    if (!btn) {
      bumpOp(entry, "react_fail", "react_ui_not_found");
      return res.json({ ok: false, reason: "react_ui_not_found" });
    }
    // hover-reveal 工具条淡入/重定位 → 先 hover 钉稳再点（校准实锤：紧超时直点会
    // TimeoutError not-stable）；点不动如实记 react_btn_unclickable，不静默成功
    if (!(await clickRevealed(page, btn, 4000))) {
      bumpOp(entry, "react_fail", "react_btn_unclickable");
      return res.json({ ok: false, reason: "react_btn_unclickable" });
    }
    await page.waitForTimeout(350);
    // picker：FB reaction 面板实测＝role=menu 里一排 <img alt="<emoji>">，alt 是**去 VS16**
    // 的 emoji 字符（❤ 而非 ❤️），且**愤怒用 😡 而非 😠**（真机校准 2026-08-13 实锤）。
    // 故按「VS16 归一后的 alt 目标集」精确匹配，其次 aria/alt 名称候选兜底；命中即点其
    // 可点祖先。失败回传 sample 供后续校准。altTargets 在 Node 侧算好传入。
    const altTargets = (() => {
      const strip = (s) => String(s || "").replace(/\uFE0F/g, "");
      const out = [strip(palette)];
      if (strip(palette) === strip("😠")) out.push("😡");  // FB 愤怒 alt=😡
      return out;
    })();
    const pick = await page.evaluate((args) => {
      const { altTargets, ariaNames } = args;
      const strip = (s) => String(s || "").replace(/\uFE0F/g, "");
      const roots = [
        document.querySelector('[role="menu"]'),
        document.querySelector('[role="dialog"]'),
        document.querySelector('[role="tooltip"]'),
      ].filter(Boolean);
      const scope = roots[0] || document.body;
      const cand = Array.from(scope.querySelectorAll(
        '[role="button"], [role="menuitem"], [role="option"], [role="img"], img[alt], [aria-label]'));
      const clickable = (el) => {
        let n = el;
        for (let i = 0; i < 4 && n; i++) {
          const r = n.getAttribute && n.getAttribute("role");
          if (r === "button" || r === "menuitem" || r === "option") return n;
          n = n.parentElement;
        }
        return el;
      };
      // ① alt/text 归一后精确命中目标 emoji（面板主路径）
      for (const el of cand) {
        const alt = strip(el.getAttribute("alt") || "");
        const txt = strip((el.textContent || "").trim());
        if (altTargets.includes(alt) || (txt && altTargets.includes(txt))) {
          clickable(el).click();
          return { clicked: true };
        }
      }
      // ② aria/alt/title 名称候选（like/love/大心…）兜底
      const attrs = (el) => ([
        el.getAttribute("aria-label") || "", el.getAttribute("alt") || "",
        el.getAttribute("title") || "",
      ].join(" ")).toLowerCase();
      for (const el of cand) {
        const a = attrs(el);
        if (a && ariaNames.some((n) => a.includes(n))) { clickable(el).click(); return { clicked: true }; }
      }
      const sample = cand.slice(0, 20).map((el) => ({
        role: el.getAttribute("role") || el.tagName,
        aria: (el.getAttribute("aria-label") || "").slice(0, 30),
        alt: (el.getAttribute("alt") || "").slice(0, 30),
        text: (el.textContent || "").trim().slice(0, 12),
      }));
      return { clicked: false, sample, scopeRole: scope.getAttribute && scope.getAttribute("role") };
    }, { altTargets, ariaNames: reactAriaCandidates(palette).map((s) => s.toLowerCase()) });
    if (!pick.clicked) {
      await page.keyboard.press("Escape").catch(() => {});
      bumpOp(entry, "react_fail", "palette_not_found");
      return res.json({ ok: false, reason: "palette_not_found",
        picker_scope: pick.scopeRole, picker_sample: pick.sample });
    }
    await page.waitForTimeout(250);
    bumpOp(entry, "react_ok");
    logger.info({ jid, emoji: palette }, "react ok");
    res.json({ ok: true, emoji: palette });
  } catch (e) {
    logger.error({ e }, "react failed");
    res.status(500).json({ ok: false, reason: "react_error", error: String(e) });
  } finally {
    _opRelease();
  }
});

// 消息请求（陌生人首讯）显式处置：接受（转正）/ 删除（移出请求箱）。坐席不必发消息也能处理。
// body { jid, action:"accept"|"decline", confirm?:bool }。
//
// 安全设计（2026-08-11，破坏性操作 + 无请求样本 + FB DOM 易改版三重风险下的收敛）：
// ① **请求线程前置闸**：仅当页面挂着「接受」按钮（＝确系待处置的消息请求）才允许 decline。
//    普通会话的「删除」是删**整段对话**（远更破坏）——无接受按钮即判 not_a_request 拒绝，
//    从根上杜绝把「拒绝陌生人」误伤成「删掉客户会话」。
// ② **decline 探测优先**：confirm!==true 只回报删除按钮在不在（button_found），绝不点击；
//    真删须显式 confirm:true。找不到按钮一律不瞎点（DOM 改版时宁可不动作也不误删）。
// ③ accept 复用 clickAcceptRequest（生产已验证、非破坏）：非请求会话找不到按钮＝no-op，
//    返回 was_request:false（安全幂等，可对普通会话验证端点而不产生副作用）。
app.post("/accounts/:id/request-action", async (req, res) => {
  const entry = findByAccount(req.params.id);
  if (!entry || !entry.page) {
    return res.status(404).json({ ok: false, error: "account not connected" });
  }
  const jid = String((req.body && req.body.jid) || "");
  const action = normalizeRequestAction((req.body && req.body.action) || "");
  const confirm = (req.body && req.body.confirm) === true;
  if (!jid || !action) {
    return res.status(400).json({
      ok: false, error: "jid and valid action (accept|decline) required" });
  }
  try {
    const page = entry.page;
    await page.goto(`${MESSENGER_URL}t/${jid}`, {
      waitUntil: "domcontentloaded", timeout: 20000 });
    await page.waitForTimeout(2000); // 请求线程会先乐观渲染、再换成接受/删除栏，等稳定
    const isRequestThread = await hasButtonByLabels(page, REQUEST_ACTION_LABELS.accept);
    if (action === "accept") {
      const accepted = await clickAcceptRequest(page);
      if (accepted) await page.waitForTimeout(1500);
      return res.json({ ok: true, action: "accept", was_request: !!accepted,
        note: accepted ? "accepted" : "no accept button (already a normal chat)" });
    }
    // decline
    if (!isRequestThread) {
      // 无接受按钮＝不是待处置请求（可能已接受/已是普通会话）→ 绝不碰「删除对话」
      return res.status(409).json({ ok: false, action: "decline",
        error: "not_a_request", note: "no Accept button on this thread; refusing to "
          + "delete (would delete a normal conversation)" });
    }
    const buttonFound = await hasButtonByLabels(page, REQUEST_ACTION_LABELS.decline);
    if (!confirm) {
      // 探测优先：只报告删除按钮在不在，不执行（真删须 confirm:true）
      return res.json({ ok: true, action: "decline", probe: true,
        button_found: buttonFound, is_request: true,
        note: "probe only; pass confirm:true to actually delete this request" });
    }
    if (!buttonFound) {
      return res.status(500).json({ ok: false, action: "decline",
        error: "decline_button_not_found",
        note: "confirmed request thread but no Delete button located (FB layout drift?)"
          + " — handle in the official web tab" });
    }
    const clicked = await clickButtonByLabels(page, REQUEST_ACTION_LABELS.decline);
    if (clicked) await page.waitForTimeout(1200);
    // 二次确认弹窗（Messenger 删除常弹「Delete conversation?」需再点删除）——best-effort
    await clickButtonByLabels(page, REQUEST_ACTION_LABELS.decline, { timeout: 2000 })
      .catch(() => false);
    return res.json({ ok: !!clicked, action: "decline", deleted: !!clicked });
  } catch (e) {
    logger.error({ e, jid, action }, "request-action failed");
    return res.status(500).json({ ok: false, action, error: String(e) });
  }
});

// M-parity①：出站媒体（图片/视频/音频/文件）——挂本地文件到 composer 发送，使 Messenger 与
// Telegram 的 send_media 对称。Python 侧 MessengerWebWorker 有 send_media 即被编排器判为
// owns_media=True → 工作台「图片/语音/视频/文件」按钮对 Messenger 一并点亮。语音走
// media_type=voice（作为音频文件发出，Messenger 内联可播放）。media_path 为主机本地绝对路径
// （Node 与 Python 同机，直接 setInputFiles，无需上传）。
app.post("/accounts/:id/send-media", async (req, res) => {
  const entry = findByAccount(req.params.id);
  const jid = String((req.body && req.body.jid) || "");
  const mediaPath = String((req.body && req.body.media_path) || "");
  const mediaType = String((req.body && req.body.media_type) || "");
  const caption = String((req.body && req.body.caption) || "");
  const isManual = !!(req.body && (req.body.manual === true || req.body.manual === 1));
  if (!entry || !entry.page) {
    return res.status(404).json(sendFailBody({
      code: "login_expired", reason: "not_logged_in",
      detail: "account not connected (not_logged_in: no authorized session in sidecar; re-login required)",
    }));
  }
  if (!jid || !mediaPath) {
    return res.status(400).json({ ok: false, error: "jid and media_path required" });
  }
  if (!fs.existsSync(mediaPath)) {
    return res.status(400).json(sendFailBody({
      code: "upload_failed", reason: "media_path_missing", detail: "media_path not found on host",
    }));
  }
  // B99 ①③：与文本 send 同一发送闸（冻结/退避窗内不碰浏览器）
  const _mGate = sendGateCheck(entry);
  if (!_mGate.ok) {
    return res.status(_mGate.code).json(sendFailBody({
      code: "send_backoff", reason: _mGate.reason, retryAfterMs: _mGate.retryAfterMs,
      retries: Math.max(0, (entry._sendFailStreak || 0)),
      detail: _mGate.reason === "account_blocked"
        ? "account temporarily blocked by platform (auto-frozen)"
        : "send backoff active after consecutive failures",
    }));
  }
  const failMedia = (o) => failSend(entry, res, {
    ...o, jid, manual: isManual, text: caption, mediaPath, mediaType, caption,
  });
  // Q-24：媒体发送也进每账号写互斥（此前不锁 → 与文本 send / 轮询导航互相踩页面）
  const _opRelease = await acquireAccountOp(entry, "send-media");
  try {
    const page = entry.page;
    try {
      await page.goto(`${MESSENGER_URL}t/${jid}`, { waitUntil: "domcontentloaded", timeout: 20000 });
    } catch (e) {
      return failMedia({ code: "thread_not_found", reason: "nav_failed",
        detail: String((e && e.message) || e).slice(0, 200) });
    }
    await page.waitForTimeout(2000);
    // B99 ②：加密会话未解锁 → 快速失败（与文本 send 同码）
    if (await detectPinPrompt(page)) {
      entry._pinPromptSeen = true;
      if (!getE2eePin(entry._loginId || "")) entry._pinState = "missing";
      return failMedia({ code: "e2ee_pin_pending", reason: "e2ee_pin_pending",
        detail: "e2ee recovery pin prompt on screen (session locked; send would fail)",
        extra: { pin_set: !!getE2eePin(entry._loginId || "") } });
    }
    const call = await detectCallOverlay(page);
    if (call.seen && !call.dismissed) {
      return failMedia({ code: "call_overlay", reason: "call_overlay",
        detail: "call / incoming-call overlay blocks the composer" });
    }
    let accepted = await clickAcceptRequest(page);
    if (accepted) await page.waitForTimeout(2000);
    let box = await resolveComposerStable(page, { timeoutMs: 10000 });
    if (!box) {
      // 与文本 send 同款救济：重导航一轮 + 探针分类（此前同样是静默 500 分支）。
      const rec = await recoverComposerOnce(entry, page, jid);
      accepted = accepted || rec.accepted;
      box = rec.box;
      if (!box) {
        await maybeMarkBlocked(entry, page);
        return failMedia({ reason: rec.reason || "composer_not_found",
          detail: `composer not found (${rec.reason || "unknown"})`, extra: { accepted } });
      }
    }
    try {
      await attachAndSend(page, mediaPath, mediaType, caption);
    } catch (e) {
      const detail = String((e && e.message) || e).slice(0, 300);
      logger.error({ e, jid, message: detail }, "send-media: attach/send failed");
      if (isDetachedError(e)) {
        // 附件链 detached：同线程重查一次再挂（不重导航——file chooser 已可能半开）
        logger.warn({ jid }, "send-media: composer detached during attach → re-query in-thread (retry 1/1)");
        box = await resolveComposerStable(page, { timeoutMs: 8000 });
        if (!box) return failMedia({ code: "composer_detached", reason: "composer_detached", detail });
        try {
          await attachAndSend(page, mediaPath, mediaType, caption);
        } catch (e2) {
          const d2 = String((e2 && e2.message) || e2).slice(0, 300);
          return failMedia({ code: isDetachedError(e2) ? "composer_detached" : "upload_failed",
            reason: isDetachedError(e2) ? "composer_detached" : "attach_failed", detail: d2 });
        }
      } else {
        return failMedia({ code: "upload_failed", reason: "attach_failed", detail });
      }
    }
    // 记录自发（含 caption）→ 轮询自回声抑制；无 caption 记媒体占位。
    recordSent(entry, jid, caption || "[媒体]");
    // P3：媒体出站也回传 synth id（与文本 send 对齐），供撤回/账本挂 platform_msg_id。
    // 指纹含 mediaPath（同 caption 连发两张图也不撞）；caption 空时用路径尾段兜底。
    const outMsgId = synthMsgId({
      chatKey: jid, direction: "out", tsLabel: "",
      text: caption || "", mediaRef: mediaPath,
    });
    if (!entry._lastOutboundId) entry._lastOutboundId = new Map();
    entry._lastOutboundId.set(String(jid), outMsgId);
    // 送达二次确认（P1，媒体版）：有 caption 才回读（按文本匹配我们的气泡 + 失败标记）；
    // 无 caption 的纯媒体无法可靠锚定「我们这条」→ 跳过校验（宁可不定态，不冒误报失败
    // 触发重发刷屏的险）。命中失败标记 → 如实失败码。
    let verified = false;
    if (caption) {
      const rb = await readbackLastOutgoing(page, caption, 5000);
      if (rb.found && rb.rowFail) {
        logger.error({ jid }, "send-media: bubble rendered with FAIL marker → NOT delivered");
        await maybeMarkBlocked(entry, page);
        return failMedia({ code: "upload_failed", reason: "bubble_fail_marker",
          detail: "messenger marked the media message as failed to send",
          extra: { accepted, sent: false, verified: true } });
      }
      verified = !!rb.found;
    }
    noteJidSendOk(entry, jid);
    res.json({ ok: true, delivered: true, message_id: outMsgId, accepted, verified });
  } catch (e) {
    const detail = String((e && e.message) || e).slice(0, 300);
    logger.error({ e, jid, message: detail }, "send-media failed");
    return failMedia({ reason: "exception", detail });
  } finally {
    _opRelease();
  }
});

// C：会话头像直链——复用入站轮询已抓到的 scontent 缓存（entry.avatarCache），不额外导航/点进会话
// （避免打扰运营、触发风控）。Python 侧据此下载落 /static 稳定托管，规避 scontent token 时效+跨域。
// 无缓存/未开轮询/无该线程 → 空 url（Python 回落首字母头像，绝不因头像拖累会话渲染）。
app.get("/accounts/:id/avatar", (req, res) => {
  const entry = findByAccount(req.params.id);
  if (!entry) return res.status(404).json({ ok: false, error: "account not connected" });
  const thread = String(req.query.thread || "");
  if (!thread) return res.status(400).json({ ok: false, error: "thread required" });
  const url = (entry.avatarCache && entry.avatarCache.get(thread)) || "";
  res.json({ ok: true, url });
});

// P1「拉更早」（对齐 Telegram 会话内补历史）：打开线程向上滚动加载更早消息并读取，
// 供 Python /api/platforms/messenger/{id}/history 分支消费（去重与落库在 Python 侧）。
// 网页端（尤其 E2EE）本地可加载深度有限——节点数不再增长即停，如实返回现有窗口。
// 独立页（不与轮询争 readPage）；不下载媒体（拉更早的价值是文字上下文）。
app.post("/accounts/:id/thread-history", async (req, res) => {
  const entry = findByAccount(req.params.id);
  if (!entry) return res.status(404).json({ error: "account not connected" });
  const thread = String((req.body || {}).thread || "");
  if (!thread) return res.status(400).json({ error: "thread required" });
  const count = Math.max(1, Math.min(200, Number((req.body || {}).count || 50)));
  let page = null;
  const msgCount = async () => page.evaluate(() => {
    const region = document.querySelector('[role="log"]') || document.body;
    const MSG_ARIA = /(消息由.*发送于|Message sent )/i;
    return Array.from(region.querySelectorAll("[aria-label]"))
      .filter((e) => MSG_ARIA.test(e.getAttribute("aria-label") || "")).length;
  }).catch(() => -1);
  try {
    page = await entry.context.newPage();
    // 借 readThreadTail 打开线程并等内容就绪（含 /t → /e2ee/t 回退）；其返回值不直接用
    // ——滚动加载后必须**就地**全量重读（再走 readThreadTail 会重新导航、重置滚动）。
    const probe = await readThreadTail(entry, thread, page, { skipMedia: true });
    if (probe === null || !probe.length) {
      return res.json({ ok: true, messages: [] }); // 读不到内容（E2EE 锁/空会话）→ 如实空
    }
    for (let round = 0; round < 12; round++) {
      const before = await msgCount();
      if (before < 0 || before >= count) break;
      await page.evaluate(() => {
        const r = document.querySelector('[role="log"]');
        if (r) r.scrollTop = 0; // 滚到顶触发向上懒加载
      }).catch(() => {});
      await page.waitForTimeout(1400);
      const after = await msgCount();
      if (after <= before) break; // 没有更多可加载（本地历史到头）
    }
    const arias = await page.evaluate(() => {
      const region = document.querySelector('[role="log"]') || document.body;
      const MSG_ARIA = /(消息由.*发送于|Message sent )/i;
      return Array.from(region.querySelectorAll("[aria-label]"))
        .map((e) => e.getAttribute("aria-label") || "")
        .filter((a) => MSG_ARIA.test(a));
    }).catch(() => []);
    const msgs = [];
    for (const aria of arias.slice(-count)) {
      const p = parseMsgAria(aria);
      if (!p || !p.text) continue; // 只要文字上下文（媒体无稳定指纹，Python 侧也不收）
      if (E2EE_PLACEHOLDER_RE.test(p.text)) continue;
      // 实施72 P4：真实时间随行下发（认不出=0，Python 侧回落锚定+approx 标）
      msgs.push({ direction: p.direction, sender: p.sender, text: p.text,
        ts: ariaDatetimeToEpoch(p.ts) || 0 });
    }
    res.json({ ok: true, messages: msgs });
  } catch (e) {
    res.status(500).json({ error: String((e && e.message) || e) });
  } finally {
    if (page) { try { await page.close(); } catch (_) {} }
  }
});

app.post("/accounts/:id/logout", async (req, res) => {
  const accountId = String(req.params.id);
  let loginId = "";
  let entry = null;
  for (const [id, e] of sessions.entries()) {
    if (e.accountId === accountId) { loginId = id; entry = e; break; }
  }
  if (entry) {
    if (loginId) _intentionalClose.add(loginId); // 同 cancel：登出不是崩溃，别触发自愈
    stopPolling(entry);
    try { await entry.context.close(); } catch (_) {}
  }
  if (loginId) sessions.delete(loginId);
  // 清持久化 profile 目录 → 防 restoreAll 复活
  try {
    const dir = (entry && entry.userDataDir) ||
      (loginId ? path.join(SESSIONS_DIR, loginId) : "");
    if (dir && fs.existsSync(dir)) fs.rmSync(dir, { recursive: true, force: true });
  } catch (e) {
    logger.debug({ e }, "logout profile cleanup failed");
  }
  postStatus(loginId, entry, "logged_out", "logout requested via API").catch(() => {});
  res.json({ ok: true, account_id: accountId });
});

// 优雅关闭：先关所有持久化上下文（把 cookie/session 刷盘），再退出。
// 否则强杀 Chromium 会丢失最后一批未落盘 cookie → 登录状态丢失、需重扫。
let _shuttingDown = false;
async function gracefulShutdown(signal) {
  if (_shuttingDown) return;
  _shuttingDown = true;
  logger.info({ signal }, "shutting down, flushing browser contexts");
  for (const [id, e] of sessions.entries()) {
    stopPolling(e);
    if (e.status === "authorized") { try { await saveCookies(id, e.context); } catch (_) {} }
    try { await e.context.close(); } catch (_) {}
  }
  process.exit(0);
}
process.on("SIGINT", () => gracefulShutdown("SIGINT"));
process.on("SIGTERM", () => gracefulShutdown("SIGTERM"));
// Windows 下 Stop-Process 走 SIGBREAK；也挂上。
process.on("SIGBREAK", () => gracefulShutdown("SIGBREAK"));

const _server = app.listen(PORT, HOST, async () => {
  logger.info(`Messenger web login service on :${PORT} (sessions: ${SESSIONS_DIR})`);
  // 后台常驻场景（MSG_HEADLESS=1）开机恢复已登录账号。headed 交互登录一般不自动 restore。
  if (String(process.env.MSG_RESTORE_ON_BOOT ?? (HEADLESS ? "1" : "0")) === "1") {
    try {
      const restored = await restoreAll();
      logger.info(`restored ${restored} persisted Messenger session(s) on boot`);
    } catch (e) {
      logger.error({ e }, "boot restore failed");
    }
  }
});
// 单实例守卫：端口被占 = 已有一个 messenger-web 在跑。第二个实例若继续启动，会对**同一个
// 持久化 profile** 并发拉起浏览器上下文 → 互抢 userDataDir → 上下文崩溃循环 + Chromium 堆积
// + cookie 竞争登出（本次联调实测踩中两次）。直接退出，杜绝重复实例。
_server.on("error", (err) => {
  if (err && err.code === "EADDRINUSE") {
    logger.error(`port ${PORT} already in use — another messenger-web instance is running. `
      + `Exiting to avoid duplicate browser contexts on the same profile (prior crash-loop/logout root cause).`);
  } else {
    logger.error({ err }, "http server error → exiting");
  }
  process.exit(1);
});
