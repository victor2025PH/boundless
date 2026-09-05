/**
 * WhatsApp 连接关闭（connection.update "close"）后的动作决策——零依赖纯函数。
 *
 * 为什么单独成文件：server.js 顶层会 app.listen（import 即拉起 HTTP 服务），测试没法安全
 * import；把「close 之后该干什么」的分支决策抽到这里，node --test 可直接单测，server.js
 * 在 close 处理器里只消费决策结果（执行副作用：改状态 / scheduleReconnect / postStatus）。
 *
 * 2026-07-22 事故背景（本决策表的直接依据）：断网 8 分钟，已授权会话 428 掉线进入重连；
 * 重连中 startLogin 建了新 entry（status="pending"），新 socket 因 DNS 失败再次 close，
 * 旧逻辑按「非 authorized → expired」终结会话——此时快重连计数才 1/5、慢重试也未武装，
 * 从此再无任何重试，也不上报 Python → 会话假死 2 小时。因此规则改为：**曾配对过的账号
 * （accountId 非空，来自持久化凭据）无论当前处于哪个中间态，close 后都必须继续重连**；
 * 只有从未配对成功的纯扫码流程失败才置 expired。
 */

// Baileys DisconnectReason 中本决策需要的两个码（WhatsApp 流协议常量，语义固定多年）。
// 刻意不 import @whiskeysockets/baileys：保持本模块零依赖，测试不必拉起重量级库；
// server.js 启动时会与权威枚举比对一次，上游罕见改值时大声告警（防静默漂移）。
export const CLOSE_CODES = Object.freeze({
  restartRequired: 515, // 配对完成后协议要求重启 socket（正常流程，非故障）
  loggedOut: 401, // 设备被解绑 / 手机端登出 → 必须人工重新配对，自动重连无意义
  forbidden: 403, // 账号被 WhatsApp 限制/封禁（"Connection Failure"）→ 重连只会再吃 403
});

/**
 * 403 forbidden 终态阀（J-6 A，2026-09-05）。
 *
 * 事故：skuio 88MP86 一个 403 账号 10 小时 38 轮「5 次快退避 → giving up → 15min 慢重试」，
 * 刷出 185 条 reconnect 日志把真问题淹没。403 语义上与 401 一样是「服务端拒绝这个账号」，
 * 但瞬时 403 也偶见（风控抖动/网关短暂拒绝），所以不在首个 403 就判死：**连续
 * FORBIDDEN_ROUNDS_DEFAULT 轮 giving up 全是 403** 才进终态；中间任何一次非 403 的 close
 * 或一次成功 open 都把计数清零。0 = 关闭终态判定（恢复旧行为，只当 428 处理）。
 */
export const FORBIDDEN_ROUNDS_DEFAULT = 2;

/** 读环境变量 WA_FORBIDDEN_ROUNDS（非法/缺省 → 默认 2；0 = 不判终态）。 */
export function forbiddenRoundsMax(env) {
  const raw = env && env.WA_FORBIDDEN_ROUNDS;
  if (raw === undefined || raw === null || String(raw).trim() === "") return FORBIDDEN_ROUNDS_DEFAULT;
  const n = Number(raw);
  if (!Number.isFinite(n) || n < 0) return FORBIDDEN_ROUNDS_DEFAULT;
  return Math.floor(n);
}

/**
 * 403 连续计数状态机（纯函数：输入旧状态 + 事件，返回新状态，不改入参）。
 *
 * state = { streak: 连续 403 close 次数, rounds: 连续「整轮都是 403」的 giving-up 轮数 }
 * 事件：
 *   {type:"close", code}                 — 一次 close：403 → streak+1；其他码 → 全清零
 *   {type:"exhausted", attemptsPerRound} — scheduleReconnect 耗尽：本轮 ≥attemptsPerRound
 *                                           次 close 全是 403 → rounds+1，否则 rounds 清零
 *   {type:"open"}                        — 连上了 → 全清零
 */
export function nextForbiddenState(prev, ev) {
  const s = prev && typeof prev === "object"
    ? { streak: Number(prev.streak) || 0, rounds: Number(prev.rounds) || 0 }
    : { streak: 0, rounds: 0 };
  const type = ev && ev.type;
  if (type === "open") return { streak: 0, rounds: 0 };
  if (type === "close") {
    if (Number(ev.code) === CLOSE_CODES.forbidden) return { streak: s.streak + 1, rounds: s.rounds };
    return { streak: 0, rounds: 0 };
  }
  if (type === "exhausted") {
    const per = Math.max(1, Number(ev.attemptsPerRound) || 1);
    // 一轮 = 触发 close + attemptsPerRound 次重连 close；streak ≥ per 即「这一轮全是 403」
    if (s.streak >= per) return { streak: s.streak, rounds: s.rounds + 1 };
    return { streak: s.streak, rounds: 0 };
  }
  return s;
}

/**
 * 决定 close 事件后的动作。只读输入、无任何副作用。
 *
 * @param {object|null} entry  会话条目（只读 status / accountId 两个字段）
 * @param {number} code        lastDisconnect 的 statusCode（取不到时调用方传 0）
 * @param {boolean} isStale    事件是否来自已被替换的旧 socket（sessions 槽位已换代）
 * @param {{forbiddenRounds?: number, forbiddenRoundsMax?: number,
 *          forbiddenStreak?: number, attemptsPerRound?: number}} [ctx]
 *        403 终态阀上下文：forbiddenRounds=该 login 已连续多少轮 giving up 全是 403
 *        （见 nextForbiddenState），forbiddenRoundsMax=阈值（缺省 FORBIDDEN_ROUNDS_DEFAULT，
 *        0=关）。forbiddenStreak+attemptsPerRound 可选：按「连续 403 close 次数」折算
 *        等效轮数（一轮 = 触发 close + attemptsPerRound 次重连 close），与 rounds 取大——
 *        Python 编排器每次退避重启都会打 /reconnect 清重连计数，giving-up 事件可能永远
 *        不触发，只按 rounds 判会让 403 账号在编排器护送下无限重连；streak 不受此影响。
 *        不传 ctx 时 403 与 428 同待遇（向后兼容旧调用方/旧测试）。
 * @returns {{action: "ignore"|"restart"|"logged_out"|"forbidden"|"reconnect"|"expire"}}
 */
export function decideCloseAction(entry, code, isStale, ctx) {
  // 陈旧事件最高优先级：startLogin（重连/重启/手动 reconnect）会替换 sessions 里的 entry，
  // 旧 socket 迟到的 close 不得影响新会话——否则会把刚建的新连接状态改坏、触发幽灵重连，
  // 双 socket 抢同一 authDir → WhatsApp 440 connectionReplaced 冲突循环。
  if (isStale) return { action: "ignore" };
  // 配对后的协议性重启：立即重建 socket（无需退避，属正常流程）。
  if (code === CLOSE_CODES.restartRequired) return { action: "restart" };
  // 设备端解绑/登出：终态，重连只会再次被拒，必须人工重新扫码配对。
  if (code === CLOSE_CODES.loggedOut) return { action: "logged_out" };
  // 403 forbidden：连续 N 轮 giving up 都是 403 → 终态（与 logged_out 平级，停快/慢重连，
  // 需换号或申诉后人工重新配对）。未达阈值的 403 仍按下方「曾配对 → reconnect」走完退避
  // （防瞬时 403 误判）。
  if (code === CLOSE_CODES.forbidden && ctx) {
    const max = ctx.forbiddenRoundsMax === undefined ? FORBIDDEN_ROUNDS_DEFAULT
      : Number(ctx.forbiddenRoundsMax) || 0;
    let rounds = Number(ctx.forbiddenRounds) || 0;
    const per = Number(ctx.attemptsPerRound) || 0;
    if (per > 0) {
      const streak = Number(ctx.forbiddenStreak) || 0;
      rounds = Math.max(rounds, Math.floor(streak / (per + 1)));
    }
    if (max > 0 && rounds >= max) return { action: "forbidden" };
  }
  // 曾配对的账号（当前 authorized，或 accountId 非空＝持久化凭据里有 me）：无论此刻处于
  // pending/reconnecting 哪个中间态，掉线都继续重连。退避与放弃由 scheduleReconnect 统一
  // 负责（耗尽 → expired + postStatus + 慢重试兜底），这里绝不直接判死。
  if (entry && (entry.status === "authorized" || entry.accountId)) {
    return { action: "reconnect" };
  }
  // 纯扫码流程（从未配对成功、无凭据）失败 → expired，等用户重新发起扫码。
  return { action: "expire" };
}

// ── J-6 B：close 原因分类 + 滚动窗口计数 + DNS 短退避 ─────────────────────────
//
// 事故：`WebSocket Error (getaddrinfo ENOTFOUND web.whatsapp.com)` 与真正的服务端踢线
// （428/440/500）在日志里都只是「WA connection closed」+ 一串报文，配对慢（#181，4-5 分钟）
// 与断线抖动的根因都要人肉翻 reason 字符串才分得出。这里把 (code, reason) 归成固定枚举
// reason_class，随 close 日志 / postStatus / /health 一起出——运维读一个字段就知道该查
// DNS/网络还是账号。

/** reason_class 枚举（顺序即 /health 输出顺序）。 */
export const REASON_CLASSES = Object.freeze([
  "dns", // 域名解析失败：ENOTFOUND / EAI_AGAIN / getaddrinfo（本机 DNS/代理/断网首要嫌疑）
  "net", // 传输层：ECONNRESET / ETIMEDOUT / 408 timedOut / 428 connectionClosed 等网络抖动
  "server", // WhatsApp 服务端主动关闭：440 connectionReplaced / 500 badSession / 503 / 411
  "forbidden", // 403 账号被限制/封禁
  "logged_out", // 401 设备解绑/手机端登出
  "restart", // 515 配对后协议性重启（正常流程，计数只为对账不告警）
  "other", // 归不进上面任何一类（code=0 且报文无特征）
]);

const _DNS_RE = /ENOTFOUND|EAI_AGAIN|EAI_NODATA|EAI_NONAME|getaddrinfo/i;
const _NET_RE = /ECONNRESET|ECONNREFUSED|ETIMEDOUT|EHOSTUNREACH|ENETUNREACH|ENETDOWN|EPIPE|ECONNABORTED|socket hang up|network/i;
const _SERVER_CODES = new Set([440, 500, 503, 411, 405]);
const _NET_CODES = new Set([408, 428]);

/**
 * 把一次 close 归类为 REASON_CLASSES 之一（纯函数）。
 *
 * @param {number} code    lastDisconnect statusCode（取不到传 0）
 * @param {string} reason  错误报文（Boom message / startLogin 抛出的 message）
 * @param {string} [errCode] 底层 Node 错误码（err.code / err.data.code，如 "ENOTFOUND"），可选
 * @returns {string} reason_class
 *
 * 优先级：账号态码（403/401/515）> 报文里的 Node 错误特征（DNS > 传输层）> 状态码族。
 * 账号态码先判是因为 Baileys 把 DNS/ECONNRESET 都映成 408，靠 code 分不出 DNS；而 403/401
 * 报文千篇一律（"Connection Failure"），只有 code 可信。
 */
export function classifyCloseReason(code, reason, errCode) {
  const c = Number(code) || 0;
  if (c === CLOSE_CODES.forbidden) return "forbidden";
  if (c === CLOSE_CODES.loggedOut) return "logged_out";
  if (c === CLOSE_CODES.restartRequired) return "restart";
  const r = String(reason || "");
  const ec = String(errCode || "").toUpperCase();
  if (_DNS_RE.test(ec) || _DNS_RE.test(r)) return "dns";
  if ((ec && _NET_RE.test(ec)) || _NET_RE.test(r)) return "net";
  if (_SERVER_CODES.has(c)) return "server";
  if (_NET_CODES.has(c)) return "net";
  return "other";
}

/**
 * 滚动时间窗计数器（默认 10 分钟）：record(cls) 记一次、counts() 出各类近窗计数。
 * 纯内存、零依赖；now 可注入便于单测。事件数封顶 maxEvents（防长时间断网刷爆内存，
 * 超限丢最旧）。
 */
export class ReasonWindow {
  constructor(opts) {
    const o = opts || {};
    this.windowMs = Math.max(1000, Number(o.windowMs) || 10 * 60 * 1000);
    this.maxEvents = Math.max(10, Number(o.maxEvents) || 2000);
    this._now = typeof o.now === "function" ? o.now : Date.now;
    this._events = []; // [{ts, cls}]，按 ts 递增
    this.total = Object.create(null); // 进程累计（不滚动）
  }
  record(cls, ts) {
    const k = REASON_CLASSES.includes(cls) ? cls : "other";
    const t = Number.isFinite(ts) ? ts : this._now();
    this._events.push({ ts: t, cls: k });
    this.total[k] = (this.total[k] || 0) + 1;
    if (this._events.length > this.maxEvents) this._events.splice(0, this._events.length - this.maxEvents);
    this._prune(t);
  }
  _prune(nowTs) {
    const cutoff = nowTs - this.windowMs;
    let i = 0;
    while (i < this._events.length && this._events[i].ts < cutoff) i++;
    if (i > 0) this._events.splice(0, i);
  }
  /** 近窗各类计数（所有类都出，缺省 0，便于看板固定列）。 */
  counts(ts) {
    const t = Number.isFinite(ts) ? ts : this._now();
    this._prune(t);
    const out = {};
    for (const k of REASON_CLASSES) out[k] = 0;
    for (const e of this._events) out[e.cls] += 1;
    return out;
  }
}

/** DNS 短退避缺省：ENOTFOUND 先 3s 固定间隔重试 10 次（本机 DNS 抖动/代理刚起通常几秒内恢复，
 *  走 3→6→12→24→48s 指数曲线会把 30s 就能恢复的断线拖成 90s+，配对期尤其明显）。 */
export const DNS_RETRY_DEFAULT = Object.freeze({ delayMs: 3000, count: 10 });

/** 读环境变量 WA_DNS_RETRY_MS / WA_DNS_RETRY_COUNT（非法/缺省 → 默认；count=0 关闭短退避）。 */
export function dnsRetryConfig(env) {
  const e = env || {};
  const d = Number(e.WA_DNS_RETRY_MS);
  const n = Number(e.WA_DNS_RETRY_COUNT);
  return {
    delayMs: Number.isFinite(d) && d >= 500 ? Math.floor(d) : DNS_RETRY_DEFAULT.delayMs,
    count: Number.isFinite(n) && n >= 0 ? Math.floor(n) : DNS_RETRY_DEFAULT.count,
  };
}

/**
 * 重连延迟决策（纯函数）。
 *
 * @param {{attempt: number, dnsStreak?: number, dnsCfg?: {delayMs:number,count:number}}} p
 *   attempt=常规曲线第几次（1 起）；dnsStreak=最近连续 dns 类 close 次数（0=上次不是 DNS）。
 * @returns {{delayMs: number, dnsPhase: boolean}}
 *   dnsPhase=true 表示处于 DNS 短退避阶段：调用方**不应**消耗常规重连预算（否则 10 次 3s
 *   重试瞬间把 5 次预算烧光进 giving up）；streak 超过 count 后回到常规指数曲线。
 */
export function reconnectDelay(p) {
  const o = p || {};
  const cfg = o.dnsCfg || DNS_RETRY_DEFAULT;
  const streak = Number(o.dnsStreak) || 0;
  if (streak > 0 && cfg.count > 0 && streak <= cfg.count) {
    return { delayMs: cfg.delayMs, dnsPhase: true };
  }
  const attempt = Math.max(1, Number(o.attempt) || 1);
  return { delayMs: Math.min(3000 * Math.pow(2, attempt - 1), 60000), dnsPhase: false };
}
