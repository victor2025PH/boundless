"use strict";

// 活动海报纯函数模型（P0 2026-08-21：新人 6U 首启海报）。
// 双模式：浏览器经 <script> 挂 window.CampaignModel（poster.js 消费）；
//        Node 经 require 取 module.exports（main.js 选品 + desktop/test/campaign-model.test.js 单测）。
//
// 设计：不碰 DOM / IPC / fs——feed 归一化、资格判定、频控、倒计时格式化全在这里，
// 保证「弹不弹、弹哪张」可在无 Electron 环境下回归。
//
// feed 来源：官网 public/downloads/campaigns.json（与 announcements.json 同域同缓存模式，
// 市场改 JSON 即上/下活动，无需发版）。安全边界：客户端只渲染**具名版式**（layout），
// 远端只下发文本字段，绝不下发 HTML/JS；CTA 链接只放行 https。

var CM_DEFAULT_MAX_SHOWS = 3;
var CM_DEFAULT_MIN_INTERVAL_HOURS = 12;
/** 已识别版式白名单：未知版式的活动直接丢弃（老客户端拿到新版式 = 静默无海报，优雅降级）。
 *  poster-hero（P1 2026-08-22）＝带内置礼盒视觉资产的强化版式——SVG 随安装包走、
 *  文本仍全部来自 feed（「远端只下发文本」安全红线不变）；老客户端未知即丢。 */
var CM_KNOWN_LAYOUTS = ["poster-countdown", "poster-plain", "poster-hero"];

function cmStr(v) { return typeof v === "string" ? v : ""; }

function cmText(v) {
  // {zh, en} 双语文本块；缺失/坏形状 → 两语空串（渲染层跳过该行）
  var o = v && typeof v === "object" ? v : {};
  return { zh: cmStr(o.zh), en: cmStr(o.en) };
}

function cmTextList(v) {
  var o = v && typeof v === "object" ? v : {};
  var norm = function (arr) {
    if (!Array.isArray(arr)) return [];
    var out = [];
    for (var i = 0; i < arr.length && out.length < 6; i++) {
      var s = cmStr(arr[i]).trim();
      if (s) out.push(s);
    }
    return out;
  };
  return { zh: norm(o.zh), en: norm(o.en) };
}

function cmParseTs(v) {
  var t = Date.parse(cmStr(v));
  return isNaN(t) ? 0 : t;
}

/** 归一化远端 feed → { campaigns: [...] }；任何坏条目静默丢弃（营销链路绝不抛错）。 */
function cmNormalizeFeed(raw) {
  var out = [];
  var list = raw && Array.isArray(raw.campaigns) ? raw.campaigns : [];
  for (var i = 0; i < list.length; i++) {
    var c = list[i] || {};
    var id = cmStr(c.id).trim();
    var layout = cmStr(c.layout).trim();
    if (!id || CM_KNOWN_LAYOUTS.indexOf(layout) < 0) continue;
    var surfaces = Array.isArray(c.surfaces) ? c.surfaces.map(cmStr) : [];
    if (surfaces.indexOf("desktop_popup") < 0) continue;
    var freq = c.frequency && typeof c.frequency === "object" ? c.frequency : {};
    var ctaRaw = c.cta && typeof c.cta === "object" ? c.cta : {};
    var ctaUrl = cmStr(ctaRaw.url).trim();
    if (!/^https:\/\//i.test(ctaUrl)) continue; // CTA 只认 https，远端数据不可指挥本地执行任何东西
    out.push({
      id: id,
      layout: layout,
      audience: cmStr(c.audience) === "all" ? "all" : "new_user",
      startsAt: cmParseTs(c.starts_at),
      endsAt: cmParseTs(c.ends_at),
      priority: Number(c.priority) || 0,
      maxShows: Math.max(1, Number(freq.max_shows) || CM_DEFAULT_MAX_SHOWS),
      minIntervalMs: Math.max(0, (Number(freq.min_interval_hours) || CM_DEFAULT_MIN_INTERVAL_HOURS) * 3600e3),
      /** 相对入职完成时刻的资格窗（小时；0 = 不限窗）——「注册 72h 内」的本地近似锚，
       *  服务端履约仍是最终权威（购买时按注册时间核验）。 */
      windowHoursAfterOnboarding: Math.max(0, Number(c.window_hours_after_onboarding) || 0),
      title: cmText(c.title),
      headline: cmText(c.headline),
      sub: cmText(c.sub),
      bullets: cmTextList(c.bullets),
      ctaLabel: cmText(ctaRaw.label),
      /** CTA 按钮下方小字（P1 排版分层：按钮放价值、价格放这里）；老客户端不读=忽略。 */
      ctaSub: cmText(ctaRaw.sub),
      ctaUrl: ctaUrl,
      dismissLabel: cmText(c.dismiss_label),
      neverLabel: cmText(c.never_label),
    });
  }
  return { campaigns: out };
}

/** 该活动对本用户的硬截止（ms）；0 = 无截止。锚 = onboarding.completed_at。 */
function cmDeadline(c, onboardingMs) {
  if (!c || !c.windowHoursAfterOnboarding || !onboardingMs) return 0;
  return onboardingMs + c.windowHoursAfterOnboarding * 3600e3;
}

/** 单活动资格判定；ctx = { now, onboardingMs, managed, state }。
 *  state 形状（main.js 持久化在 userData/campaign-state.json）：
 *    { shows: { [id]: { count, last_ts } }, never: { [id]: true } } */
function cmEligible(c, ctx) {
  var now = Number(ctx && ctx.now) || 0;
  var state = (ctx && ctx.state) || {};
  var shows = (state.shows && state.shows[c.id]) || { count: 0, last_ts: 0 };
  if (!ctx || !ctx.managed) return { ok: false, reason: "not_managed" };
  if (c.startsAt && now < c.startsAt) return { ok: false, reason: "not_started" };
  if (c.endsAt && now > c.endsAt) return { ok: false, reason: "ended" };
  if (state.never && state.never[c.id]) return { ok: false, reason: "opted_out" };
  if (c.audience === "new_user") {
    if (!ctx.onboardingMs) return { ok: false, reason: "no_onboarding" };
    var dl = cmDeadline(c, ctx.onboardingMs);
    if (dl && now > dl) return { ok: false, reason: "window_passed" };
  }
  if ((Number(shows.count) || 0) >= c.maxShows) return { ok: false, reason: "max_shows" };
  if (shows.last_ts && now - shows.last_ts < c.minIntervalMs) return { ok: false, reason: "interval" };
  return { ok: true, reason: "" };
}

/** 从归一化活动列表里挑当前该弹的一张（priority 降序，同分取先声明的）；无 → null。 */
function cmPick(campaigns, ctx) {
  var best = null;
  var list = Array.isArray(campaigns) ? campaigns : [];
  for (var i = 0; i < list.length; i++) {
    var c = list[i];
    if (!cmEligible(c, ctx).ok) continue;
    if (!best || c.priority > best.priority) best = c;
  }
  return best;
}

/** 无可弹活动时的原因汇总（P0 2026-08-22 可诊断化）：有一张合格 → null；
 *  否则返回**最高优先级活动**的拒绝原因（feed 为空 → no_feed）。
 *  cmEligible 一直算得出精确原因，此前被 IPC 层丢弃——「为什么没弹」全靠翻
 *  %APPDATA%；现在诊断面板 / 日志 / skip 埋点三个消费面都吃这里的单一口径。 */
function cmSkipSummary(campaigns, ctx) {
  var list = Array.isArray(campaigns) ? campaigns : [];
  if (!list.length) return { id: "", reason: "no_feed" };
  var top = null;
  var topReason = "";
  for (var i = 0; i < list.length; i++) {
    var c = list[i];
    var v = cmEligible(c, ctx);
    if (v.ok) return null;
    if (!top || c.priority > top.priority) { top = c; topReason = v.reason; }
  }
  return { id: top.id, reason: topReason };
}

/** headline 里挑出「最大的数字」供 CountUp 滚动（P1 数字仪式感）。
 *  feed 只下发整句文本（安全红线），数字层级只能在渲染端解析——宁缺勿滥：
 *  找不到 ≥1000 的数字（"6U" 之类不值得滚）→ null，渲染层原样静态输出。 */
function cmHeadlineParts(text) {
  var s = cmStr(text);
  var re = /\d{1,3}(?:,\d{3})+|\d+/g;
  var m;
  var best = null;
  while ((m = re.exec(s))) {
    var val = Number(m[0].replace(/,/g, ""));
    if (!isFinite(val)) continue;
    if (!best || val > best.value) best = { value: val, idx: m.index, num: m[0] };
  }
  if (!best || best.value < 1000) return null;
  return {
    before: s.slice(0, best.idx),
    num: best.num,
    value: best.value,
    after: s.slice(best.idx + best.num.length),
  };
}

/** 倒计时紧迫档（P1）：剩余 (0, 24h) 为 urgent——渲染层转橙红 + 加「最后 X 小时」前缀。
 *  ≤0（已过期）或无截止不紧迫；阈值与「稍后再说 12h 冷却」错开，第二次弹出时
 *  多数场景已进入紧迫带（重复曝光自带增量信息，不是复读机）。 */
function cmCountdownUrgent(msLeft) {
  var n = Number(msLeft);
  return isFinite(n) && n > 0 && n < 24 * 3600e3;
}

/** 记一次展示（返回新 state，纯函数——调用方负责持久化）。 */
function cmRecordShow(state, id, now) {
  var st = state && typeof state === "object" ? state : {};
  var shows = {};
  var k;
  for (k in st.shows || {}) shows[k] = st.shows[k];
  var cur = shows[id] || { count: 0, last_ts: 0 };
  shows[id] = { count: (Number(cur.count) || 0) + 1, last_ts: Number(now) || 0 };
  var never = {};
  for (k in st.never || {}) never[k] = true;
  return { shows: shows, never: never };
}

/** 「不再提醒」（返回新 state）。 */
function cmOptOut(state, id) {
  var st = state && typeof state === "object" ? state : {};
  var shows = {};
  var k;
  for (k in st.shows || {}) shows[k] = st.shows[k];
  var never = {};
  for (k in st.never || {}) never[k] = true;
  never[id] = true;
  return { shows: shows, never: never };
}

/** 倒计时文本：剩余 ms → "HH:MM:SS"（72h 窗显示 "71:59:59" 式累计小时）；≤0 → ""。 */
function cmCountdownText(msLeft) {
  var s = Math.floor(Number(msLeft) / 1000);
  if (!isFinite(s) || s <= 0) return "";
  var h = Math.floor(s / 3600);
  var m = Math.floor((s % 3600) / 60);
  var sec = s % 60;
  var pad = function (n) { return (n < 10 ? "0" : "") + n; };
  return pad(h) + ":" + pad(m) + ":" + pad(sec);
}

/** 双语取文：lang 以 en 开头取 en（缺则回落 zh），否则 zh。 */
function cmLangText(block, lang) {
  var b = block || {};
  var en = String(lang || "").toLowerCase().indexOf("en") === 0;
  return (en ? (b.en || b.zh) : (b.zh || b.en)) || "";
}

/** CTA 链接追加注册时间锚 reg_ts（秒）——官网 /order 金卡据此渲染 72h 真倒计时
 *（官网侧 2026-08-22 已上线消费端；匿名直开官网仍无倒计时，红线不变）。
 *  守卫（任一不满足原样返回，营销链路绝不抛错）：
 *  · 仅 https；· onboardingMs 必须为正（未完成首启不带）；
 *  · 仅 allowedHosts 白名单域（精确或其子域）——注册时间不带给任何第三方链接；
 *  · 已有 reg_ts 参数不重复追加（幂等，feed 将来自带占位也不冲突）。 */
function cmCtaUrlWithReg(url, onboardingMs, allowedHosts) {
  var u = cmStr(url);
  var ms = Number(onboardingMs) || 0;
  if (!/^https:\/\//i.test(u) || ms <= 0) return u;
  try {
    var parsed = new URL(u);
    var hosts = Array.isArray(allowedHosts) ? allowedHosts : [];
    var host = String(parsed.hostname || "").toLowerCase();
    var allowed = false;
    for (var i = 0; i < hosts.length; i++) {
      var h = String(hosts[i] || "").toLowerCase();
      if (h && (host === h || host.slice(-(h.length + 1)) === "." + h)) { allowed = true; break; }
    }
    if (!allowed) return u;
    if (parsed.searchParams.has("reg_ts")) return u;
    parsed.searchParams.set("reg_ts", String(Math.floor(ms / 1000)));
    return parsed.toString();
  } catch (e) {
    return u;
  }
}

/** --poster-preview 验收通道的内置样例（feed 不可达时也能验收视觉/文案）。
 *  形状与线上 6U 活动同构、zh/en 双语齐备（与 feed 数据走同一渲染路径，不是
 *  待迁 i18n 债）；id 带 preview 前缀防误入真实频控账本。 */
var CM_PREVIEW_SAMPLE = {
  campaigns: [{
    id: "preview-sample-6u",
    layout: "poster-countdown",
    audience: "all",
    surfaces: ["desktop_popup"],
    priority: 1,
    frequency: { max_shows: 3, min_interval_hours: 12 },
    window_hours_after_onboarding: 72,
    title: { zh: "新人首充大礼包 · 每账号一次", en: "Newcomer pack · once per account" },
    headline: { zh: "6U → 18,000 Token", en: "6U → 18,000 tokens" },
    sub: {
      zh: "双倍到账（$0.33/千，全场最低单价）≈ 1,800 条 AI 回复；注册 72 小时内专享。",
      en: "Double rate ($0.33/1k) ≈ 1,800 AI replies. Within 72h of signup only.",
    },
    bullets: {
      zh: ["不占用首充加赠资格", "跨智聊 / 通译同一钱包", "Token 用尽自动降级免费引擎"],
      en: ["Keeps your first-top-up bonus", "One wallet across products", "Degrades to free engines when empty"],
    },
    cta: {
      label: { zh: "立即领取 18,000 Token", en: "Claim 18,000 tokens now" },
      sub: { zh: "仅需 6U · 双倍到账", en: "Only 6U · double rate" },
      url: "https://bd2026.cc/order?plan=recharge-newbie-6&utm_source=chatx_desktop&utm_medium=poster&utm_campaign=newbie6u",
    },
    dismiss_label: { zh: "稍后再说", en: "Maybe later" },
    never_label: { zh: "不再提醒", en: "Don't show again" },
  }],
};

var CM_EXPORTS = {
  CM_PREVIEW_SAMPLE: CM_PREVIEW_SAMPLE,
  cmNormalizeFeed: cmNormalizeFeed,
  cmDeadline: cmDeadline,
  cmEligible: cmEligible,
  cmPick: cmPick,
  cmSkipSummary: cmSkipSummary,
  cmHeadlineParts: cmHeadlineParts,
  cmCountdownUrgent: cmCountdownUrgent,
  cmRecordShow: cmRecordShow,
  cmOptOut: cmOptOut,
  cmCountdownText: cmCountdownText,
  cmLangText: cmLangText,
  cmCtaUrlWithReg: cmCtaUrlWithReg,
};

/* eslint-disable no-undef */
if (typeof module !== "undefined" && module.exports) {
  module.exports = CM_EXPORTS;
}
if (typeof window !== "undefined") {
  window.CampaignModel = CM_EXPORTS;
}
