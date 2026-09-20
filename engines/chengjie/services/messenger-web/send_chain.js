/**
 * Q-24（#298 P0，2026-09-12）：Messenger 网页代发链止血——纯函数层（零 DOM / 零 IO，node --test 可测）。
 *
 * 事故形态（DXAPAX / FJM9ER 四份诊断包同型）：composer ElementHandle 在 click / type 瞬间被
 * Messenger 重绘换掉 → `Element is not attached to the DOM` → 边车 500 → Python 只写「发送失败」
 * → 连败 4 次 backoff 40s → 坐席零感知、客户被晾。本模块把三件事收成可测的判定：
 *   ① 失败原因码归一（七码契约，Python / 工作台按码出人话 + 重试按钮）；
 *   ② 每会话连败计数 + 待重试队列（60s 一拍、最多 3 次；成功/人工成功即出队）；
 *   ③ 失败响应体形状（{ok:false, code, reason, detail, retry_after_ms, retries}，
 *      reason_code 保留旧字段名兼容老 Python）。
 *
 * 七码契约（与 docs/指令_Q-24 B 段一致；工作台 inbox.ms.fail.<code> 词条一一对应）：
 *   composer_detached   输入框重绘/丢失（重查重试后仍失败）
 *   thread_not_found    线程页没渲染 / 导航失败 / 白屏
 *   e2ee_pin_pending    加密会话未解锁（PIN 浮层在场）
 *   call_overlay        通话 / 来电浮层挡住输入
 *   send_backoff        连败退避窗内 / 平台临时封锁冻结
 *   login_expired       边车无已授权会话 / 掉到登录页
 *   upload_failed       媒体附件未能挂上
 */

export const SEND_FAIL_CODES = Object.freeze([
  "composer_detached", "thread_not_found", "e2ee_pin_pending", "call_overlay",
  "send_backoff", "login_expired", "upload_failed",
]);
const _CODE_SET = new Set(SEND_FAIL_CODES);

/** Playwright「元素失联」族错误（重查同线程 composer 即可恢复的一类，不该直接 500）。 */
export function isDetachedError(msg) {
  const s = String((msg && msg.message) || msg || "");
  return /not attached|detached|stale|is not stable|Execution context was destroyed|Cannot find context/i
    .test(s);
}

/** 通话 / 来电浮层文案（messenger.com 中英）。纯文本判据，供 DOM 探针的 innerText 用。 */
export function isCallOverlayText(text) {
  const t = String(text || "").slice(0, 4000);
  return /Ongoing call|is calling you|Incoming (video )?call|Join call|Video chat has ended|正在通话|来电|视频通话邀请|加入通话|通话中/i
    .test(t);
}

/**
 * 把边车内部散落的 reason（classifyComposerBlock 六码 / gate 码 / 异常文本）归一到七码。
 * 判定顺序＝语义硬度：登录丢失 > PIN 锁 > 退避/封锁 > 通话浮层 > 附件 > 线程未渲染 > 输入框。
 * 认不出的一律 composer_detached（DXAPAX 实锤：exception 分支 100% 是 detached 族），
 * 原始 reason / message 由响应体 reason / detail 如实带出，不丢证据。
 */
export function normalizeSendFailCode({ reason = "", message = "", status = 0 } = {}) {
  const r = String(reason || "").trim().toLowerCase();
  const m = String(message || "");
  if (_CODE_SET.has(r)) return r;
  if (/^(not_logged_in|login_page|logged_out|expired|needs_login)$/.test(r) || Number(status) === 404
      || /not_logged_in|not logged in|login required|re-login/i.test(m)) return "login_expired";
  if (/pin/.test(r) || /recovery pin|e2ee pin|PIN/.test(m)) return "e2ee_pin_pending";
  if (/^(send_backoff|account_blocked|temporarily_blocked|blocked)$/.test(r)
      || /backoff|temporarily blocked/i.test(m)) return "send_backoff";
  if (/call|ongoing/.test(r) || isCallOverlayText(m)) return "call_overlay";
  // 「Element is not attached to the DOM」含 attach 字样——先按失联族归 composer_detached，
  // 否则会被下面的附件判据误吞成 upload_failed（门禁回放抓到的）。
  if (isDetachedError(m)) return "composer_detached";
  if (/upload|attach|media|file_chooser|filechooser/.test(r)
      || /file chooser|setInputFiles|upload|attach/i.test(m)) return "upload_failed";
  if (/^(page_not_rendered|render_timeout|thread_not_found|nav_failed|navigation)$/.test(r)
      || /net::ERR|Navigation (failed|timeout)|page\.goto|Target page, context or browser has been closed/i.test(m)) {
    return "thread_not_found";
  }
  return "composer_detached";
}

/**
 * 失败响应体（send / send-media 共用一个形状）。``reason`` 是边车内部细码（保留证据），
 * ``code`` 是七码契约；``reason_code`` 与老 Python `http_error_fields` 兼容（它读 reason_code）。
 */
export function sendFailBody({ code = "", reason = "", detail = "", retryAfterMs = 0,
  retries = 0, extra = null } = {}) {
  const c = _CODE_SET.has(String(code || "")) ? String(code)
    : normalizeSendFailCode({ reason, message: detail });
  const body = {
    ok: false, delivered: false,
    code: c,
    reason: String(reason || c),
    detail: String(detail || "").slice(0, 300),
    retry_after_ms: Math.max(0, Math.floor(Number(retryAfterMs) || 0)),
    retries: Math.max(0, Math.floor(Number(retries) || 0)),
    reason_code: String(reason || c),
    error: String(detail || reason || c).slice(0, 300),
  };
  if (extra && typeof extra === "object") Object.assign(body, extra);
  return body;
}

/** 七码 → HTTP 状态（Python 只看 body，状态码仅供日志 / 老客户端语义连续）。 */
export function sendFailHttpStatus(code) {
  switch (String(code || "")) {
    case "login_expired": return 404;
    case "send_backoff": return 429;
    case "e2ee_pin_pending": return 503;
    case "thread_not_found": return 502;
    case "call_overlay": return 409;
    case "upload_failed": return 502;
    default: return 500;
  }
}

/**
 * 输入一击的重绑编排（A 段，可注入回调 → node --test 可回放 DXAPAX 序列）：
 *   input()      在当前 composer 上 click+type；抛 detached 族错误表示元素失联
 *   requery()    同线程重查稳定 composer（返回 truthy=拿到新 handle）
 *   renavigate() 重导航一次并重查（返回 {box, reason}）
 *   clear()      重试前清残留输入（防叠字双发）
 * 顺序：input → detached? → clear+requery+input（同线程重试 1 次）→ detached? →
 *       renavigate+requery+input（只 1 次）→ detached? → 抛 code=composer_detached。
 * 非 detached 错误原样上抛（不是我们要治的病）。返回 { attempts, renavigated, accepted }。
 */
export async function inputWithRebind({ input, requery, renavigate, clear, log = () => {} }) {
  let attempts = 0;
  const tryInput = async () => { attempts += 1; await input(); };
  try {
    await tryInput();
    return { attempts, renavigated: false, accepted: false };
  } catch (e1) {
    if (!isDetachedError(e1)) throw e1;
    log("detached_retry_inthread", e1);
  }
  if (clear) await clear();
  const fresh = await requery();
  if (fresh) {
    try {
      await tryInput();
      return { attempts, renavigated: false, accepted: false };
    } catch (e2) {
      if (!isDetachedError(e2)) throw e2;
      log("detached_renavigate", e2);
    }
  } else {
    log("requery_empty_renavigate", null);
  }
  const rec = await renavigate();
  if (!rec || !rec.box) {
    const err = new Error(`composer not found after re-navigation (${(rec && rec.reason) || "unknown"})`);
    err.sendCode = normalizeSendFailCode({ reason: (rec && rec.reason) || "composer_not_found" });
    err.sendReason = (rec && rec.reason) || "composer_detached";
    throw err;
  }
  try {
    await tryInput();
    return { attempts, renavigated: true, accepted: !!rec.accepted };
  } catch (e3) {
    if (!isDetachedError(e3)) throw e3;
    const err = new Error("composer detached 3x (in-thread retry + re-navigate exhausted)");
    err.sendCode = "composer_detached";
    err.sendReason = "composer_detached";
    throw err;
  }
}

// ── 每会话连败 + 待重试队列 ─────────────────────────────────────────────────────

export const RETRY_QUEUE_THRESHOLD = 2;      // 同会话连败 ≥2 → 进待重试队列 + 铃铛
export const RETRY_QUEUE_INTERVAL_MS = 60000;  // 后台每 60s 一拍
export const RETRY_QUEUE_MAX_TRIES = 3;        // 最多重试 3 次，之后终局失败

/** 推进 per-jid 连败计数（Map<jid, n>）；ok=true 清零。返回新计数。 */
export function bumpJidFailStreak(map, jid, ok) {
  if (!(map instanceof Map) || !jid) return 0;
  const k = String(jid);
  if (ok) { map.delete(k); return 0; }
  const n = (Number(map.get(k)) || 0) + 1;
  map.set(k, n);
  while (map.size > 512) {
    const oldest = map.keys().next().value;
    if (oldest === undefined) break;
    map.delete(oldest);
  }
  return n;
}

/**
 * 失败后是否进待重试队列。只在**可自愈的瞬态码**且同会话连败达阈值时排队；
 * login_expired / e2ee_pin_pending / send_backoff 不排（重试也过不去，靠人 / 靠 PIN / 靠退避到期）。
 */
export function shouldQueueRetry({ streak = 0, code = "", threshold = RETRY_QUEUE_THRESHOLD } = {}) {
  const c = String(code || "");
  if (!/^(composer_detached|thread_not_found|call_overlay|upload_failed)$/.test(c)) return false;
  return (Number(streak) || 0) >= Math.max(1, Number(threshold) || RETRY_QUEUE_THRESHOLD);
}

/**
 * 待重试队列 upsert（Map<jid, item>）。同会话只留一条（最新文案覆盖——坐席改稿重发时
 * 旧稿不该再补发）；``tries`` 从 0 起。返回 { queued, item, replaced }。
 */
export function retryQueueUpsert(queue, jid, { text = "", manual = false, quoted = null,
  code = "", now = Date.now(), intervalMs = RETRY_QUEUE_INTERVAL_MS, mediaPath = "",
  mediaType = "", caption = "" } = {}) {
  if (!(queue instanceof Map) || !jid) return { queued: false, item: null, replaced: false };
  if (!String(text || "") && !String(mediaPath || "")) {
    return { queued: false, item: null, replaced: false };
  }
  const k = String(jid);
  const replaced = queue.has(k);
  const item = {
    jid: k, text: String(text || ""), manual: !!manual, quoted: quoted || null,
    mediaPath: String(mediaPath || ""), mediaType: String(mediaType || ""),
    caption: String(caption || ""),
    code: String(code || ""), tries: 0, enqueuedAt: now,
    nextAt: now + Math.max(1000, Number(intervalMs) || RETRY_QUEUE_INTERVAL_MS),
    lastError: "",
  };
  queue.set(k, item);
  return { queued: true, item, replaced };
}

/** 到点待重试项（nextAt <= now），按 nextAt 升序。 */
export function retryQueueDue(queue, now = Date.now()) {
  if (!(queue instanceof Map)) return [];
  return [...queue.values()].filter((it) => it && it.nextAt <= now)
    .sort((a, b) => a.nextAt - b.nextAt);
}

/**
 * 一次重试的结局推进：ok → 出队；失败 → tries+1，未达上限则排下一拍，达上限 → 出队 + 终局。
 * 返回 { done, final, item }：done=已出队；final=终局失败（应报 Python + 铃铛）。
 */
export function retryQueueSettle(queue, jid, { ok = false, error = "", now = Date.now(),
  intervalMs = RETRY_QUEUE_INTERVAL_MS, maxTries = RETRY_QUEUE_MAX_TRIES } = {}) {
  if (!(queue instanceof Map) || !jid) return { done: true, final: false, item: null };
  const k = String(jid);
  const item = queue.get(k);
  if (!item) return { done: true, final: false, item: null };
  if (ok) { queue.delete(k); return { done: true, final: false, item }; }
  item.tries = (Number(item.tries) || 0) + 1;
  item.lastError = String(error || "").slice(0, 200);
  if (item.tries >= Math.max(1, Number(maxTries) || RETRY_QUEUE_MAX_TRIES)) {
    queue.delete(k);
    return { done: true, final: true, item };
  }
  item.nextAt = now + Math.max(1000, Number(intervalMs) || RETRY_QUEUE_INTERVAL_MS);
  return { done: false, final: false, item };
}

/**
 * 反应 aria 判据放宽（D 段，P7P8FY 实锤）：messenger.com 当前反应胶囊的 aria 不再是
 * 「X用👍回应了 / X reacted with 👍」，而是 「查看回应 / See who reacted to this message」
 * 或直接 「👍 1」——旧正则 /回应了|reacted with/ 一条都匹不到，反应从未出边车。
 * 这里只判「这个 aria 像不像反应胶囊」，emoji 抽取交给 parseReactionPill。
 */
export const REACTION_ARIA_RE = /回应|反应|reacted|reaction|Double tap to like|点赞|赞了/i;

const _EMOJI_RE = /(\p{Extended_Pictographic}(?:\uFE0F|\u200D\p{Extended_Pictographic})*)/u;

/**
 * 从反应胶囊的 aria / 可见文本里抽 emoji + 发送者（me / peer / unknown）。
 * 认不出发送者时按 ``fallbackSender``（调用方按消息方向推：本方出站气泡上的反应＝对方点的）。
 */
export function parseReactionPill(aria, text = "", fallbackSender = "peer") {
  const a = String(aria || "").trim();
  const t = String(text || "").trim();
  if (!a && !t) return null;
  let sender = fallbackSender;
  if (/^(你|You)\b/i.test(a) || /^(你|You)\s*用/.test(a)) sender = "me";
  else if (/^(?!你|You\b)(.+?)\s*(用|reacted)/i.test(a) && !/See who|查看/i.test(a)) sender = "peer";
  const src = `${t} ${a}`;
  const m = src.match(_EMOJI_RE);
  let emoji = m ? m[1] : "";
  if (!emoji) {
    if (/like|thumbs|赞|大拇指|Double tap/i.test(src)) emoji = "👍";
    else if (/love|heart|爱心|红心/i.test(src)) emoji = "❤️";
    else if (/haha|laugh|大笑|哈哈/i.test(src)) emoji = "😆";
    else if (/wow|哇/i.test(src)) emoji = "😮";
    else if (/sad|cry|难过|哭/i.test(src)) emoji = "😢";
    else if (/angry|生气|怒/i.test(src)) emoji = "😡";
  }
  if (!emoji) return null;
  return { emoji, sender };
}
