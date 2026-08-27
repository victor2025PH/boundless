"use strict";

// 多账号网页多开：容器模型 + 账号栏健康三态（纯函数）。
// 双模式：浏览器经 <script> 加载后成为全局函数（renderer.js 直接用）；
//        Node 经 require 取 module.exports（desktop/test/webmulti.test.js 单测）。
//
// 解决竞品相对我们的两个差距点:
//   ①同时多账号视图:一排账号,每个各自内嵌隔离 webview——但 N 个 Chromium 全常驻会撑爆内存,
//     故 containerPlan 决定「哪些保持挂载(live)、哪些挂起(suspended)」,聚焦与高未读优先保活。
//   ②账号栏健康:把注入(inject)/会话在线(session)/翻译可达(translate)三维滚成一枚栏徽标,
//     坐席一眼看清每个号「能不能用」,而不只看当前聚焦 webview。

// 严重度优先级：bad > warn > wait > ok
const _SEV = { ok: 0, wait: 1, warn: 2, bad: 3 };
function _worst(a, b) { return (_SEV[a] || 0) >= (_SEV[b] || 0) ? a : b; }
// lastActive 存 epoch 毫秒（~1.7e12）,`|0` 会 32 位截断损坏,必须数值强转。
function _num(x) { const n = Number(x); return Number.isFinite(n) ? n : 0; }

// ⚠ 三维只出稳定 `key`，**不出文案**（2026-08-20 i18n 收口）：这些字进的是账号栏
// tooltip——英文壳里写死中文＝坐席每天看见的那一行永远是中文。展示层经
// SH(key) 取当前语言文案（词典在 renderer/shell-i18n.js 的 health.* 段）。

// 单维:会话在线态 → 三态
function _sessionDim(online) {
  if (online === true) return { cls: "ok", key: "health.session_online" };
  if (online === false) return { cls: "bad", key: "health.session_offline" };
  return { cls: "wait", key: "health.unknown" };
}
// 单维:注入态（透传 inject-status 的 cls；unsupported→bad）
function _injectDim(inject) {
  if (inject === "unsupported") return { cls: "bad", key: "health.inject_unsupported" };
  if (["ok", "warn", "bad", "wait"].indexOf(inject) >= 0) {
    return { cls: inject, key: "health.inject_" + inject };
  }
  return { cls: "wait", key: "health.inject_wait" };
}
// 单维:翻译链路可达
function _translateDim(translateOk) {
  if (translateOk === true) return { cls: "ok", key: "health.translate_ok" };
  if (translateOk === false) return { cls: "warn", key: "health.translate_down" };
  return { cls: "wait", key: "health.unknown" };
}

// 账号综合健康三态：三维取最差,并给出人话
// a: { online?:bool, inject?:string, translateOk?:bool }
function accountHealthState(a) {
  const x = a || {};
  const session = _sessionDim(x.online);
  const inject = _injectDim(x.inject);
  const translate = _translateDim(x.translateOk);
  let level = "ok";
  level = _worst(level, session.cls);
  level = _worst(level, inject.cls);
  level = _worst(level, translate.cls);
  // 文案取「最差那一维」的解释,让坐席直接知道卡在哪
  let textKey = "health.all_ok";
  const worstDim = [session, inject, translate].reduce((w, d) =>
    (_SEV[d.cls] || 0) > (_SEV[w.cls] || 0) ? d : w
  );
  if (level !== "ok") textKey = worstDim.key;
  return { level, textKey, dims: { session, inject, translate } };
}

// 账号栏徽标模型（textKey 同样只是键，取词在展示层）
function railBadge(state) {
  const s = state && state.level ? state : { level: "wait", textKey: "health.waiting" };
  const dot = { ok: "on", warn: "warn", bad: "off", wait: "idle" }[s.level] || "idle";
  return { cls: s.level, dot, textKey: s.textKey || "" };
}

// 账号栏排序:置顶 > 未读多 > 最近活跃 > id 稳定
function sortRail(accounts) {
  const arr = (Array.isArray(accounts) ? accounts : []).slice();
  arr.sort((a, b) => {
    const pa = a.pinned ? 1 : 0, pb = b.pinned ? 1 : 0;
    if (pa !== pb) return pb - pa;
    const ua = a.unread | 0, ub = b.unread | 0;
    if (ua !== ub) return ub - ua;
    const la = _num(a.lastActive), lb = _num(b.lastActive);
    if (la !== lb) return lb - la;
    return String(a.id || "").localeCompare(String(b.id || ""));
  });
  return arr;
}

function unreadTotal(accounts) {
  return (Array.isArray(accounts) ? accounts : []).reduce((s, a) => s + (a.unread | 0), 0);
}

// 容器挂载计划:聚焦号恒 live,其余按(置顶/未读/活跃)择优保活至 maxLive,余者挂起省内存。
// accounts: [{id, unread?, lastActive?, pinned?}]；opts:{focusId, maxLive=3}
// 返回 { live:[id...], suspended:[id...] }（live 含聚焦号,顺序 = 聚焦优先 + 评分降序）
function containerPlan(accounts, opts) {
  const o = opts || {};
  const maxLive = Math.max(1, o.maxLive | 0 || 3);
  const list = (Array.isArray(accounts) ? accounts : []).filter((a) => a && a.id != null);
  const focusId = o.focusId != null ? String(o.focusId) : "";

  function score(a) {
    return (a.pinned ? 1e9 : 0) + (a.unread | 0) * 1e4 + _num(a.lastActive) / 1e6;
  }
  const rest = list.filter((a) => String(a.id) !== focusId);
  rest.sort((a, b) => score(b) - score(a));

  const live = [];
  const focusAcc = list.find((a) => String(a.id) === focusId);
  if (focusAcc) live.push(String(focusAcc.id));
  for (const a of rest) {
    if (live.length >= maxLive) break;
    live.push(String(a.id));
  }
  const liveSet = new Set(live);
  const suspended = list.map((a) => String(a.id)).filter((id) => !liveSet.has(id));
  return { live, suspended };
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    accountHealthState, railBadge, sortRail, unreadTotal, containerPlan,
  };
}
