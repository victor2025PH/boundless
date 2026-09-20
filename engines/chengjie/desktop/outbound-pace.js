"use strict";

// 受控出站「拟人节奏」策略层（纯函数 + 每账号小状态机）。
// 双模式：浏览器经 <script> 加载成全局（renderer.js 用）；Node 经 require 单测。
//
// 为什么不是直接调 shared/inject/human-pace.js 的默认值 —— 这是本模块存在的全部理由：
// human-pace 的缺省档（间隔均值 45s / 下限 8s、每分钟 8 条、安静时段、日上限）是为
// **群发/主动触达**调的；受控出站队列走的是**回复客户**，把 45s 均值套上去＝客户问一句
// 等一分钟，产品直接不可用。故这里按「回复」语义另配一套：打字耗时随字数（真人手速），
// 消息间只留 1~3s 抖动（防两个气泡撞在同一帧、看起来像脚本），每分钟上限只当**安全阀**。
//
// 三条刻意的边界（都是「宁可慢、不可错」）：
//   ① **只延后，绝不丢**：超频返回 throttled，让调用方把命令留在队列里（不 ack → 服务端
//      180s 后自动回收重取），而不是丢弃或标失败。丢一条＝客户永远等不到回复。
//   ② **不做安静时段 / 日上限**：那两个是「主动打扰」的闸门，服务端 send-gate 已管；在客户端
//      拿它们拦**回复**，等于夜里客户来问、我们静默不答（比机器味严重得多）。
//   ③ 打字耗时上限 9s：填入 composer 会派发 input 事件 → 对端看到「正在输入…」，故这段等待
//      是**有产出的真实感**而非空耗；但再长就拖垮队列，且真人也会分条发。
// 刻意住在**主进程**（而不是 renderer）：renderer 是 contextIsolation+nodeIntegration:false
// 的纯浏览器上下文，require 不存在 → 只能另建一份 shared 镜像（又一处会静默漂移的副本）。
// 放主进程还顺手解掉两个问题：节奏状态跨 renderer 重载存活（重载后不会突然爆发连发），
// 以及「每账号一个 pacer」的唯一权威只有一份。renderer 经 IPC 问计划。
const _HP = require("./shared/inject/human-pace.js");

const REPLY_GAP = { meanMs: 2200, min: 900, max: 6000 };
const REPLY_COMPOSE = { cps: 5, min: 600, max: 9000 };
const DEFAULT_PER_MINUTE_CAP = 10;
const WINDOW_MS = 60000;

function _num(x, dflt) { const n = Number(x); return Number.isFinite(n) ? n : dflt; }

function _composeMs(text, o) { return _HP.composeMs(text, o); }
function _gapMs(o) { return _HP.interMessageGapMs(o); }

function createOutboundPacer(opts) {
  const o = opts || {};
  const rng = typeof o.rng === "function" ? o.rng : Math.random;
  const perMinuteCap = Math.max(1, _num(o.perMinuteCap, DEFAULT_PER_MINUTE_CAP));
  const gapCfg = Object.assign({}, REPLY_GAP, o.gap || {}, { rng });
  const composeCfg = Object.assign({}, REPLY_COMPOSE, o.compose || {}, { rng });
  let sent = [];      // 近 WINDOW_MS 内的发送时刻（升序）
  let lastAt = 0;     // 最近一次发送时刻
  // 「发过没有」用独立标记而非 lastAt>0：时间戳 0 是合法时刻（单测/自定义时钟会用），
  // 拿 0 当哨兵会让那一刻的发送被当成「从没发过」→ 少等一次间隔。
  let hasSent = false;

  function _prune(now) {
    const cut = now - WINDOW_MS;
    if (sent.length && sent[0] <= cut) sent = sent.filter((t) => t > cut);
  }

  return {
    /** 计算这一条该怎么发：{ typingMs, waitMs, throttled, reason }。
     *  throttled=true 时 waitMs 是「距最早那条滑出窗口」的毫秒数，调用方应停止本轮派发。 */
    plan(args) {
      const a = args || {};
      const now = _num(a.now, Date.now());
      _prune(now);
      if (sent.length >= perMinuteCap) {
        const freeAt = sent[0] + WINDOW_MS;
        return {
          typingMs: 0,
          waitMs: Math.max(0, Math.round(freeAt - now)),
          throttled: true,
          reason: "per_minute_cap",
        };
      }
      const typingMs = _composeMs(a.text, composeCfg);
      // 与上一条的自然间隔：只补足差额（上一条已经过去很久就不必再等）
      const waitMs = hasSent ? Math.max(0, Math.round(lastAt + _gapMs(gapCfg) - now)) : 0;
      return { typingMs, waitMs, throttled: false, reason: "" };
    },

    /** 真发出去之后记账（只记成功的：失败不该占用频控名额）。 */
    noteSent(now) {
      const t = _num(now, Date.now());
      _prune(t);
      sent.push(t);
      sent.sort((x, y) => x - y);
      lastAt = hasSent ? Math.max(lastAt, t) : t;
      hasSent = true;
    },

    /** 诊断用快照（账号栏/日志）。 */
    snapshot(now) {
      const t = _num(now, Date.now());
      _prune(t);
      return { recent: sent.length, perMinuteCap, lastAt };
    },
  };
}

/** 每账号一个 pacer 的注册表（renderer 的轮询循环按 account_id 取）。 */
function createPacerRegistry(opts) {
  const map = new Map();
  return {
    for(accountId) {
      const k = String(accountId || "");
      if (!map.has(k)) map.set(k, createOutboundPacer(opts));
      return map.get(k);
    },
    drop(accountId) { map.delete(String(accountId || "")); },
    size() { return map.size; },
  };
}

/** 把 inject 回执（fill-result）判成「该怎么 ack」——单一事实来源，renderer 与单测同口径。
 *
 * 服务端语义（desktop_outbound）：ack(ok=true)→sent 终态；ack(ok=false)→failed 终态（要人工
 * 点重试）；**不 ack**→保持 claimed，180s 后自动回收成 pending 重取。据此分三档：
 *   · 确认发出        → ack ok（唯一该记成功的情形）
 *   · 确认没发出且原因是 DOM 控件缺失/未清空 → ack failed（选择器坏了，重试也白搭，
 *     要让它出现在人审队列 + 由注入健康遥测告警；静默重试只会把 attempts 刷爆）
 *   · 压根没等到回执（inject 没装载 / 页面在跳转）→ **不 ack**，交服务端回收重取；
 *     这条正是旧实现最致命的地方：它无条件 ack ok，于是注入死掉时每条命令都被记成
 *     「已发送」而客户什么也没收到（1.016/1.017 的漏包让这不是假设）。
 */
function ackDecision(result) {
  const r = result || {};
  if (r.timeout === true || r.received === false) {
    return { ack: false, ok: false, error: "no_inject_ack", retryable: true };
  }
  if (r.ok === true) return { ack: true, ok: true, error: "", retryable: false };
  const reason = String(r.reason || "unknown");
  return { ack: true, ok: false, error: reason, retryable: false };
}

module.exports = { createOutboundPacer, createPacerRegistry, ackDecision, REPLY_GAP, REPLY_COMPOSE };
