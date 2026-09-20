"use strict";

/* 出站拟人节奏计划（单一事实来源,桌面 preload 与浏览器扩展共用）。
 *
 * 现有 core.js 的 sendComposer 是即时点发。竞品最诱人也最危险的功能是「群发助手」——
 * 批量、等间隔、机器味十足正是最高封号风险面。本模块把出站节奏收成纯函数,让自动发送/
 * 群发像真人:逐字高斯打字耗时、消息间泊松间隔、每分钟/每日频控、安静时段闸门。
 *
 * 与 huoke 仓的 human-behavior 技能同哲学（贝塞尔滑动 / 高斯打字 / 泊松等待）,但这里是
 * telegram-mtproto-ai 侧网页注入链路的纯计划层:只算「什么时候发第几条、要不要延后」,
 * 不触碰 DOM。全部经注入 rng（可 seed）确定性,Node 可直跑单测。
 */

// ── 可 seed 的 PRNG（mulberry32）：测试确定性,生产传 Math.random ────────────
function makeRng(seed) {
  let a = (seed >>> 0) || 0x9e3779b9;
  return function () {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// 标准正态（Box–Muller,单值）
function _gaussian(rng, mean, sd) {
  const u1 = Math.max(1e-9, rng());
  const u2 = rng();
  const z = Math.sqrt(-2 * Math.log(u1)) * Math.cos(2 * Math.PI * u2);
  return mean + z * sd;
}

// ── 打字耗时（ms）：字符数 / 每秒字数,叠高斯扰动,夹上下限 ───────────────────
function composeMs(text, opts) {
  const o = opts || {};
  const rng = o.rng || Math.random;
  const cps = o.cps > 0 ? o.cps : 5;          // 每秒字数（真人聊天档 ~4-6）
  const sd = o.sd != null ? o.sd : 0.25;      // 相对抖动
  const min = o.min != null ? o.min : 600;
  const max = o.max != null ? o.max : 12000;
  const n = String(text || "").length;
  const base = (n / cps) * 1000;
  const factor = Math.max(0.4, _gaussian(rng, 1, sd)); // 不为负/不过快
  return Math.round(Math.min(max, Math.max(min, base * factor)));
}

// ── 消息间间隔（ms）：指数分布（泊松到达）,夹上下限 ───────────────────────────
function interMessageGapMs(opts) {
  const o = opts || {};
  const rng = o.rng || Math.random;
  const mean = o.meanMs > 0 ? o.meanMs : 45000;
  const min = o.min != null ? o.min : 8000;
  const max = o.max != null ? o.max : 300000;
  const u = Math.min(1 - 1e-9, Math.max(1e-9, rng()));
  const gap = -mean * Math.log(1 - u);
  return Math.round(Math.min(max, Math.max(min, gap)));
}

// ── 安静时段判定（纯）：本地小时是否落在 [start,end) 静默窗（支持跨零点）─────
function withinQuietHours(hour, cfg) {
  const c = cfg || {};
  if (c.enabled === false) return false;
  const h = ((hour % 24) + 24) % 24;
  const start = c.start != null ? c.start : 23;
  const end = c.end != null ? c.end : 8;
  if (start === end) return false;
  if (start < end) return h >= start && h < end;      // 同日窗
  return h >= start || h < end;                        // 跨零点窗（23→8）
}

// ── 群发计划（纯）：给收件人排出发送时刻,超频/超日/安静时段的延后并给出原因 ──
// recipients: string[] | {to}[]
// opts: {
//   startTs, rng, perMinuteCap, dailyCap, sentToday,
//   gap:{meanMs,min,max}, quiet:{enabled,start,end}, hourOf(ts)->0..23
// }
// 返回 { sends:[{to, at, gapMs, index}], deferred:[{to, reason}], plannedTotal, lastAt }
function planBatchSend(recipients, opts) {
  const o = opts || {};
  const rng = o.rng || Math.random;
  const startTs = o.startTs != null ? o.startTs : Date.now();
  const perMinuteCap = o.perMinuteCap > 0 ? o.perMinuteCap : 8;
  const dailyCap = o.dailyCap > 0 ? o.dailyCap : 200;
  const sentToday = Math.max(0, o.sentToday | 0);
  const minGapByCap = Math.ceil(60000 / perMinuteCap); // 每分钟频控 → 最小平均间隔
  const gapCfg = Object.assign({}, o.gap, { rng });
  const quiet = o.quiet || null;
  const hourOf = typeof o.hourOf === "function"
    ? o.hourOf
    : (ts) => new Date(ts).getHours();

  const list = (Array.isArray(recipients) ? recipients : []).map((r) =>
    typeof r === "string" ? { to: r } : (r || {})
  );

  const sends = [];
  const deferred = [];
  let at = startTs;
  let remainingDaily = Math.max(0, dailyCap - sentToday);

  for (let idx = 0; idx < list.length; idx++) {
    const r = list[idx];
    const to = String(r.to || "");
    if (!to) { deferred.push({ to, reason: "invalid" }); continue; }
    if (remainingDaily <= 0) { deferred.push({ to, reason: "daily_cap" }); continue; }
    if (quiet && withinQuietHours(hourOf(at), quiet)) {
      deferred.push({ to, reason: "quiet_hours" });
      continue;
    }
    let gapMs = 0;
    if (sends.length > 0) {
      // 频控下限与真人间隔取较大者:既不超频,又保留自然抖动
      gapMs = Math.max(minGapByCap, interMessageGapMs(gapCfg));
      at += gapMs;
      // 位移后可能落入安静时段
      if (quiet && withinQuietHours(hourOf(at), quiet)) {
        deferred.push({ to, reason: "quiet_hours" });
        continue;
      }
    }
    sends.push({ to, at, gapMs, index: idx });
    remainingDaily--;
  }

  return {
    sends,
    deferred,
    plannedTotal: sends.length,
    lastAt: sends.length ? sends[sends.length - 1].at : startTs,
  };
}

const _api = {
  makeRng, composeMs, interMessageGapMs, withinQuietHours, planBatchSend,
};
if (typeof module !== "undefined" && module.exports) module.exports = _api;
if (typeof globalThis !== "undefined") globalThis.AHumanPace = _api;
