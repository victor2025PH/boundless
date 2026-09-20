"use strict";

// 🩺 自动化健康看板（桌面壳层）——把后端既有聚合 API 下发的「全账号注入健康 + 受控出站队列概览」
// 渲染到 copilot 头部 🩺 面板，让运营在桌面壳内一眼看清：
//   ① 各内嵌账号注入是否健康（持续失配=疑似官方网页改版，可走 D1 selector-profiles 热修）；
//   ② 全自动回复经 send-gate/kill-switch 后，受控出站队列是否正常流转 / 有无卡死。
// 数据来源：window.shell.injectHealthList() / outboundStats()（主进程代理后端，复用既有路由）。
//
// 双模式：浏览器经 <script> 加载执行 DOM 装配；Node 经 require 取纯函数（health-panel.test.js 单测）。
// 纯渲染模型与 inject-status.js::deriveInjectState 的语义对齐（同一套失配分类）。

// ⚠ 文案单源（2026-08-20 i18n 收口）：本文件此前 100+ 条中文写死在渲染模型里，英文壳
// 的 🩺 看板整块是中文——而这是运营判断「全自动到底发没发出去」的唯一入口。现在全部经
// T() 走 renderer/shell-i18n.js 的 `hp.*` 段（注入状态复用 `inject.*`，与状态条同一说法）。
//   · 浏览器：<script> 已装载 shell-i18n.js → 直接用 window.SH；
//   · Node（health-panel.test.js）：require 同一个词典 → 断言仍读中文，零测试改写。
// 两侧同一份 DICT，绝不各写一份。
let _SHI = null;
function T(key, vars) {
  if (typeof SH === "function") return SH(key, vars);
  if (_SHI === null) {
    try { _SHI = (typeof require === "function") ? require("./shell-i18n.js") : false; }
    catch (e) { _SHI = false; }
  }
  return (_SHI && _SHI.t) ? _SHI.t(key, vars) : String(key);
}

function _num(obj, key) {
  const v = obj && obj[key];
  const n = Number(v);
  return Number.isFinite(n) ? n : 0;
}

// 注入健康总徽标：bad>warn>wait>ok 的严重度优先。
function injectBadge(summary) {
  const s = summary || {};
  const persistent = _num(s, "persistent_mismatch");
  const mismatch = _num(s, "mismatch");
  const total = _num(s, "total");
  if (persistent > 0) {
    return { cls: "bad", text: T("hp.inj_persistent", { n: persistent }), hint: T("hp.inj_persistent_h") };
  }
  if (mismatch > 0) {
    return { cls: "warn", text: T("hp.inj_mismatch", { n: mismatch }), hint: T("hp.inj_mismatch_h") };
  }
  if (total === 0) {
    return { cls: "wait", text: T("hp.inj_none"), hint: T("hp.inj_none_h") };
  }
  return { cls: "ok", text: T("hp.inj_ok", { n: total }), hint: "" };
}

// 受控出站队列徽标：failed > 待审(held) > 活动中(pending/claimed) > 空闲。
function outboundBadge(summary) {
  const s = summary || {};
  const failed = _num(s, "failed");
  const held = _num(s, "held");
  const pending = _num(s, "pending");
  const claimed = _num(s, "claimed");
  if (failed > 0) {
    return { cls: "bad", text: T("hp.ob_failed", { n: failed }), hint: T("hp.ob_failed_h") };
  }
  if (held > 0) {
    return { cls: "warn", text: T("hp.ob_held", { n: held }), hint: T("hp.ob_held_h") };
  }
  if (pending + claimed > 0) {
    return { cls: "warn", text: T("hp.ob_active", { pending, claimed }), hint: "" };
  }
  return { cls: "ok", text: T("hp.ob_idle"), hint: "" };
}

// 出站状态码 → 人话（词条键 hp.st_*，未知码原样回显便于排障）。
function outboundStatusText(status) {
  switch (status) {
    case "pending": return T("hp.st_pending");
    case "claimed": return T("hp.st_claimed");
    case "sent": return T("hp.st_sent");
    case "failed": return T("hp.st_failed");
    case "held": return T("hp.st_held");
    case "cancelled": return T("hp.st_cancelled");
    default: return String(status || "");
  }
}

const _OB_COLOR = {
  pending: "#7f8c8d", claimed: "#f1c40f", sent: "#2ecc71",
  failed: "#e74c3c", held: "#f39c12", cancelled: "#7f8c8d",
};

// 按状态决定可用的人审动作（claimed=飞行中、sent/cancelled=终态 → 无动作）。
function outboundActions(status) {
  switch (status) {
    case "pending": return [{ act: "cancel", label: T("hp.act_cancel") }, { act: "hold", label: T("hp.act_hold") }, { act: "edit", label: T("hp.act_edit") }];
    case "held": return [{ act: "release", label: T("hp.act_release") }, { act: "cancel", label: T("hp.act_cancel") }, { act: "edit", label: T("hp.act_edit") }];
    case "failed": return [{ act: "retry", label: T("hp.act_retry") }];
    default: return [];
  }
}

// 单条出站命令 → 行展示模型（纯函数、可单测）。
function outboundRowModel(item) {
  const it = item || {};
  const status = String(it.status || "");
  return {
    id: it.id,
    status,
    statusText: outboundStatusText(status),
    actions: outboundActions(status),
    preview: String(it.preview != null ? it.preview : (it.text || "")),
    account_id: String(it.account_id || ""),
  };
}

function renderOutboundRowHtml(item) {
  const m = outboundRowModel(item);
  const color = _OB_COLOR[m.status] || "#7f8c8d";
  let btns = "";
  for (const a of m.actions) {
    btns += '<button data-act="' + a.act + '" data-id="' + _esc(m.id)
      + '" style="font-size:.66rem;padding:.05rem .35rem;border:1px solid #ffffff33;'
      + 'border-radius:5px;background:transparent;color:inherit;cursor:pointer;flex:0 0 auto">'
      + _esc(a.label) + "</button>";
  }
  return '<div data-oid="' + _esc(m.id) + '" style="display:flex;gap:.35rem;align-items:center;'
    + 'padding:.2rem 0;border-top:1px solid #ffffff14;font-size:.73rem">'
    + '<span style="color:' + color + ';flex:0 0 auto">' + _esc(m.statusText) + "</span>"
    + '<span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#bbb">'
    + _esc(m.account_id) + T("hp.colon") + _esc(m.preview) + "</span>"
    + btns + "</div>";
}

// 失配持续时长 → 人话（秒/分/时）。
function formatDuration(secs) {
  const n = Math.max(0, Math.floor(Number(secs) || 0));
  if (n < 60) return T("hp.dur_sec", { n });
  if (n < 3600) {
    const m = Math.floor(n / 60);
    const s = n % 60;
    return s ? T("hp.dur_min_sec", { m, s }) : T("hp.dur_min", { m });
  }
  const h = Math.floor(n / 3600);
  const m = Math.floor((n % 3600) / 60);
  return m ? T("hp.dur_hour_min", { h, m }) : T("hp.dur_hour", { h });
}

// 注入状态码 → 人话。**刻意复用 inject.* 词条**（inject-status.js 状态条同一套说法）：
// 同一件事在状态条与 🩺 看板里必须逐字一致，各写一份迟早分叉。
function injectStatusText(status) {
  switch (status) {
    case "ok":
    case "mismatch_composer":
    case "mismatch_bubble":
    case "unsupported":
      return T("inject." + status);
    case "unknown": return T("hp.st_unreported");
    default: return status ? String(status) : T("health.unknown");
  }
}

// 单个账号 → 行展示模型。stale（数据陈旧）单列，避免与失配混淆。
function accountRowModel(a) {
  const acc = a || {};
  const status = acc.status || "unknown";
  const isMismatch = status === "mismatch_composer" || status === "mismatch_bubble";
  let cls = "ok";
  if (acc.stale) cls = "stale";
  else if (status === "unsupported") cls = "bad";
  else if (isMismatch) cls = "warn";
  else if (status !== "ok") cls = "wait";
  const ms = _num(acc, "mismatch_secs");
  return {
    cls,
    platform: String(acc.platform || ""),
    account_id: String(acc.account_id || ""),
    statusText: injectStatusText(status),
    stale: !!acc.stale,
    durationText: isMismatch && ms > 0 ? formatDuration(ms) : "",
  };
}

// 🔴 红点预警模型：持续失配（连续超阈值，非抖动）账号数 > 0 即亮。优先用 alerts.alerts 精确计数，
// 退回 injectHealthList.summary.persistent_mismatch。返回 {on,count,title}（title 同步到 🩺 图标 tooltip）。
// 人审 SLA 超时（纯函数）：held>0 且最久待审 ≥ 阈值秒 → 分级（warn/urgent）告警。
// 阈值优先取 payload 的 review_sla_sec/review_sla_urgent_sec（后端配置驱动），其次入参，再次默认。
function slaBreachModel(out, thresholdSec, urgentSec) {
  const o = out || {};
  const count = _num(o.summary, "held");
  const ageSec = _num(o, "review_oldest_age_sec");
  const warn = _num(o, "review_sla_sec") || thresholdSec || 300;
  let urgent = _num(o, "review_sla_urgent_sec") || urgentSec || warn * 3;
  if (urgent < warn) urgent = warn;
  let level = "none";
  if (count > 0) {
    if (ageSec >= urgent) level = "urgent";
    else if (ageSec >= warn) level = "warn";
  }
  return {
    breach: level !== "none", urgent: level === "urgent", level,
    ageSec, count, warnSec: warn, urgentSec: urgent,
  };
}

function alertDotModel(injectData, alertsData, sla) {
  let count = 0;
  if (alertsData && Array.isArray(alertsData.alerts)) {
    count = alertsData.alerts.length;
  } else if (injectData && injectData.summary) {
    count = _num(injectData.summary, "persistent_mismatch");
  }
  if (count > 0) {
    // 注入持续失配优先（更紧急：全自动发不出去）
    return {
      on: true,
      count,
      level: "mismatch",
      title: T("hp.dot_mismatch", { n: count }),
    };
  }
  if (sla && sla.breach) {
    const urgent = sla.level === "urgent";
    return {
      on: true,
      count: sla.count,
      level: sla.level,
      // 严重/一般各一条整句词条（英文语序与中文差太远，拼装式会碎成不通顺的句子）
      title: T(urgent ? "hp.dot_sla_urgent" : "hp.dot_sla_warn", { n: sla.count }),
    };
  }
  return {
    on: false,
    count: 0,
    level: "none",
    title: T("hp.dot_default"),
  };
}

// 覆写文件校验结果 → 展示模型（纯函数）。后端 validate 返回 {ok,exists,valid,profiles,dropped,error?}。
function formatValidateResult(r) {
  if (!r || !r.ok) return { cls: "bad", text: T("hp.val_failed", { err: (r && r.error) || T("hp.val_no_backend") }) };
  if (!r.exists) return { cls: "wait", text: T("hp.val_absent") };
  if (!r.valid) return { cls: "bad", text: T("hp.val_invalid", { err: r.error || T("hp.val_parse_failed") }) };
  const dropped = Array.isArray(r.dropped) ? r.dropped : [];
  let text = T("hp.val_ok", { n: _num(r, "profiles") });
  if (dropped.length) {
    text += T("hp.val_dropped", {
      n: dropped.length,
      list: dropped.slice(0, 3).join(T("sep.enum")) + (dropped.length > 3 ? "…" : ""),
    });
  }
  return { cls: dropped.length ? "warn" : "ok", text };
}

const _CLS_COLOR = {
  ok: "#2ecc71", warn: "#f1c40f", bad: "#e74c3c", wait: "#7f8c8d", stale: "#7f8c8d",
};

function _badgeHtml(b) {
  const color = _CLS_COLOR[b.cls] || "#7f8c8d";
  const hint = b.hint ? ' title="' + _esc(b.hint) + '"' : "";
  return '<span style="display:inline-block;padding:.1rem .5rem;border-radius:999px;'
    + 'font-size:.72rem;background:' + color + '22;color:' + color + ';border:1px solid ' + color + '55"'
    + hint + ">" + _esc(b.text) + "</span>";
}

function _esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// selector key → 人话标签（与后端 SELECTOR_KEYS 对齐）。**函数而非常量**：词条要在
// 语言就绪后才取，模块加载期求值会把中文烙死在英文壳里。
const SELECTOR_KEYS = ["composer", "sendBtn", "bubble", "peerTitle"];
function selectorLabel(key) {
  return SELECTOR_KEYS.indexOf(key) >= 0 ? T("hp.sel_" + key) : String(key || "");
}

// 逐选择器失配诊断（纯函数，P9）：优先用后端聚合 selector_diagnosis，缺失则从 alerts[].selectors
// 客户端兜底统计。让运营从「N 个账号失配」下钻到「哪个 selector key 抓空最多」，精准热修该键。
function selectorDiagnosisModel(alertsData) {
  const d = alertsData || {};
  let entries = [];
  if (Array.isArray(d.selector_diagnosis)) {
    entries = d.selector_diagnosis
      .map((e) => ({ key: String((e && e.key) || ""), missing: _num(e, "missing") }))
      .filter((e) => e.missing > 0);
  } else {
    const alerts = Array.isArray(d.alerts) ? d.alerts : [];
    const order = SELECTOR_KEYS;
    const counts = {};
    for (const a of alerts) {
      const sel = (a && a.selectors) || {};
      for (const k of order) if (sel[k] === false) counts[k] = (counts[k] || 0) + 1;
    }
    entries = order.filter((k) => counts[k] > 0).map((k) => ({ key: k, missing: counts[k] }));
  }
  entries.sort((a, b) => b.missing - a.missing);
  entries.forEach((e) => { e.label = selectorLabel(e.key); });
  if (!entries.length) return { has: false, text: "", entries: [] };
  return {
    has: true, entries,
    text: T("hp.diag", { list: entries.map((e) => e.label + " ✗" + e.missing).join(" · ") }),
  };
}

// 「持续失配」告警块（纯函数）：仅当有 alerts 时渲染醒目红框，置于面板顶部引导 D1 热修；无则返回 ""。
function renderAlertsHtml(alertsData) {
  const alerts = alertsData && Array.isArray(alertsData.alerts) ? alertsData.alerts : [];
  if (!alerts.length) return "";
  let rows = "";
  for (const a of alerts.slice(0, 6)) {
    const m = accountRowModel(a);
    const dur = m.durationText ? _esc(T("hp.for_duration", { d: m.durationText })) : "";
    rows += '<div style="display:flex;align-items:center;gap:.4rem;padding:.2rem 0;font-size:.74rem">'
      + '<span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
      + _esc(m.platform) + " / " + _esc(m.account_id) + "</span>"
      + '<span style="color:#e74c3c;flex:0 0 auto">' + _esc(m.statusText) + dur + "</span>"
      + "</div>";
  }
  const diag = selectorDiagnosisModel(alertsData);
  const diagHtml = diag.has
    ? '<div style="font-size:.7rem;color:#e67e22;margin-top:.25rem;font-weight:600" '
      + 'title="' + _esc(T("hp.diag_title")) + '">'
      + _esc(diag.text) + "</div>"
    : "";
  return '<div style="background:#e74c3c1a;border:1px solid #e74c3c66;border-radius:8px;padding:.45rem .6rem;margin-bottom:.5rem">'
    + '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:.15rem">'
    + '<span style="font-size:.78rem;color:#e74c3c;font-weight:600">' + _esc(T("hp.alerts_hdr", { n: alerts.length })) + "</span>"
    + '<button id="cp-health-fix" style="font-size:.7rem;padding:.12rem .55rem;border:1px solid #e74c3c88;border-radius:6px;background:#e74c3c22;color:#e74c3c;cursor:pointer" title="'
    + _esc(T("hp.fix_title")) + '">' + _esc(T("hp.fix_btn")) + "</button>"
    + "</div>" + rows + diagHtml
    + '<div style="font-size:.68rem;color:#c0392b;margin-top:.2rem">' + _esc(T("hp.alerts_ft")) + "</div>"
    + "</div>";
}

// 活动海报判定行（P0 2026-08-22 可诊断化，纯函数）：desktop:campaign-diag 的快照 →
// 「最近一次选品结果 + 原因人话 + feed 条数」。此前 cmEligible 的精确原因被 IPC 丢弃，
// 「为什么没弹」要翻代码+翻 %APPDATA%——现在运营在 🩺 面板一眼看到并知道下一步
//（验收走 --poster-preview）。skip 多数属预期（如 72h 窗已过）→ wait 灰而非 warn。
function campaignDiagModel(diag) {
  const d = diag || {};
  if (!d.ok) return { has: false, text: "", cls: "wait", hint: "" };
  const last = d.last || null;
  const feed = _num(d, "feed_count");
  const feedTxt = T("hp.camp_feed", { n: feed });
  if (!last) {
    return { has: true, cls: "wait", text: T("hp.camp_no_query") + " · " + feedTxt, hint: T("hp.camp_hint") };
  }
  if (last.result === "preview") {
    return { has: true, cls: "warn", text: T("hp.camp_preview"), hint: T("hp.camp_hint") };
  }
  if (last.result === "show") {
    return {
      has: true, cls: "ok",
      text: T("hp.camp_shown", { id: String(last.id || "") }) + " · " + feedTxt,
      hint: T("hp.camp_hint"),
    };
  }
  const reason = String(last.reason || "");
  const knownReasons = [
    "no_feed", "not_managed", "no_onboarding", "window_passed",
    "max_shows", "interval", "opted_out", "not_started", "ended",
  ];
  const why = knownReasons.indexOf(reason) >= 0 ? T("hp.camp_r_" + reason) : (reason || "?");
  return {
    has: true,
    // feed 都没拉到是链路问题（值得注意）；其余 skip 是资格判定按设计工作 → 中性灰
    cls: reason === "no_feed" ? "warn" : "wait",
    text: T("hp.camp_skip", { why }) + " · " + feedTxt,
    hint: T("hp.camp_hint"),
  };
}

function renderCampaignRowHtml(diag) {
  const m = campaignDiagModel(diag);
  if (!m.has) return "";
  const color = _CLS_COLOR[m.cls] || "#7f8c8d";
  return '<div style="display:flex;align-items:center;gap:.4rem;margin-top:.45rem;'
    + 'padding-top:.35rem;border-top:1px solid #ffffff14" title="' + _esc(m.hint) + '">'
    + '<span style="font-size:.72rem;color:#bbb;flex:0 0 auto">🎁 ' + _esc(T("hp.camp")) + "</span>"
    + '<span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;'
    + 'font-size:.7rem;color:' + color + '">' + _esc(m.text) + "</span></div>";
}

// 近期拦截率读数（纯函数）：cancelled/(sent+failed+cancelled)。样本不足时不渲染颜色告警。
function interceptRateModel(out) {
  const o = out || {};
  const sample = _num(o, "intercept_sample");
  const rate = Number(o.intercept_rate);
  if (!sample || !isFinite(rate)) {
    return { pct: "—", text: T("hp.rate_nosample"), cls: "ok" };
  }
  const pct = Math.round(rate * 100) + "%";
  const cls = rate >= 0.5 ? "bad" : rate >= 0.2 ? "warn" : "ok";
  return { pct, text: T("hp.rate", { pct, n: sample }), cls };
}

// 纠正样本读数（纯函数）：沉淀的「AI 失误/协同」数据量。无样本则不渲染。
function correctionsModel(out) {
  const c = (out && out.corrections) || {};
  const total = _num(c, "total");
  if (!total) return { has: false, text: "" };
  const edit = _num(c, "edit");
  const ai = _num(c, "ai_assisted");
  const aiPart = ai > 0 ? T("hp.corr_ai", { n: ai }) : "";
  return { has: true, text: T("hp.corr", { total, edit, ai: aiPart }) };
}

// 保存改写时的 action 载荷（纯函数）：据是否用过 AI 候选推断 source，凑黄金三元组。
function editSavePayload(id, text, aiSuggestion) {
  const t = String(text == null ? "" : text);
  const ai = String(aiSuggestion == null ? "" : aiSuggestion);
  let source = "human";
  if (ai) source = t.trim() === ai.trim() ? "ai_adopted" : "ai_edited";
  return { id, action: "edit", text: t, ai_suggestion: ai, source };
}

// 结构化拦截理由分类（P7）：**code 是持久化契约**（落 corrections 台账 → 聚类/DPO 样本），
// 绝不因语言变化；label 只是当下语言的显示层，故按需 T() 取而非常量表烙死。
const REASON_CODES = ["off_topic", "tone", "factual", "over_boundary", "redundant", "other"];

function reasonLabel(code) {
  return REASON_CODES.indexOf(code) >= 0 ? T("hp.rsn_" + code) : String(code || "");
}

function reasonOptions() {
  return REASON_CODES.map((code) => ({ code, label: reasonLabel(code) }));
}

// 拦截理由 chips（纯函数）：点「拦截」展开，点某分类即带结构化 reason 拦截。
function renderInterceptChipsHtml(id) {
  let html = '<span style="font-size:.66rem;color:#e74c3c;flex:0 0 auto">' + _esc(T("hp.rsn_prompt")) + "</span>";
  for (const o of reasonOptions()) {
    html += '<button data-cancel-reason="' + o.code + '" data-id="' + _esc(id) + '" '
      + 'style="font-size:.64rem;padding:.04rem .35rem;border:1px solid #e74c3c66;border-radius:5px;'
      + 'background:#e74c3c1a;color:#e74c3c;cursor:pointer;flex:0 0 auto">' + _esc(o.label) + "</button>";
  }
  html += '<button data-cancel-abort="1" style="font-size:.64rem;padding:.04rem .35rem;'
    + 'border:1px solid #ffffff33;border-radius:5px;background:transparent;color:inherit;cursor:pointer;flex:0 0 auto">✕</button>';
  return html;
}

// 失误聚类读数（纯函数）：reason_clusters → 按数量降序「答非所问 N · 事实错误 M」。
function reasonClusterModel(out) {
  const clusters = (out && out.reason_clusters) || {};
  const entries = Object.keys(clusters)
    .map((k) => ({ code: k, label: reasonLabel(k), count: _num(clusters, k) }))
    .filter((e) => e.count > 0)
    .sort((a, b) => b.count - a.count);
  if (!entries.length) return { has: false, text: "", entries: [] };
  return {
    has: true,
    entries,
    text: T("hp.cluster", { list: entries.map((e) => e.label + " " + e.count).join(" · ") }),
  };
}

// 待审队列块（纯函数）：held 命令 FIFO + 批量「全部放行 / 全部拦截」。无待审则返回空串。
function renderReviewHtml(review) {
  const list = Array.isArray(review) ? review : [];
  if (!list.length) return "";
  let rows = "";
  for (const it of list.slice(0, 20)) {
    const m = Object.assign({}, it, { status: "held" });
    rows += renderOutboundRowHtml(m);
  }
  const ids = list.map((it) => it.id).filter((x) => x != null).join(",");
  return '<div style="border:1px solid #f39c1255;background:#f39c120f;border-radius:8px;'
    + 'padding:.4rem .5rem;margin-bottom:.45rem">'
    + '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:.15rem">'
    + '<strong style="font-size:.78rem;color:#f39c12">' + _esc(T("hp.review_hdr", { n: list.length })) + "</strong>"
    + '<span style="display:flex;gap:.3rem">'
    + '<button data-bulk="release" data-ids="' + _esc(ids) + '" style="font-size:.66rem;'
    + 'padding:.05rem .4rem;border:1px solid #2ecc7188;border-radius:5px;background:#2ecc7122;'
    + 'color:#2ecc71;cursor:pointer">' + _esc(T("hp.bulk_release")) + "</button>"
    + '<button data-bulk="cancel" data-ids="' + _esc(ids) + '" style="font-size:.66rem;'
    + 'padding:.05rem .4rem;border:1px solid #e74c3c88;border-radius:5px;background:#e74c3c22;'
    + 'color:#e74c3c;cursor:pointer">' + _esc(T("hp.bulk_cancel")) + "</button>"
    + "</span></div>" + rows + "</div>";
}

// 行内编辑器 HTML（纯函数）：输入框 + AI 重写 + 保存 + 取消。供 startInlineEdit 装配、可单测。
function renderInlineEditHtml(id) {
  return '<input class="cp-ob-edit" type="text" placeholder="' + _esc(T("hp.edit_ph")) + '" '
    + 'style="flex:1;min-width:120px;font-size:.72rem;background:#0003;border:1px solid #ffffff33;'
    + 'border-radius:5px;color:inherit;padding:.15rem .35rem"/>'
    + '<button data-edit-airewrite="' + _esc(id) + '" title="' + _esc(T("hp.ai_title")) + '" '
    + 'style="font-size:.66rem;padding:.05rem .4rem;border:1px solid #9b8cff88;border-radius:5px;'
    + 'background:#9b8cff22;color:#9b8cff;cursor:pointer;flex:0 0 auto">' + _esc(T("hp.ai_btn")) + "</button>"
    + '<button data-edit-save="' + _esc(id) + '" style="font-size:.66rem;padding:.05rem .4rem;'
    + 'border:1px solid #2ecc7188;border-radius:5px;background:#2ecc7122;color:#2ecc71;cursor:pointer;flex:0 0 auto">'
    + _esc(T("hp.save")) + "</button>"
    + '<button data-edit-cancel="1" style="font-size:.66rem;padding:.05rem .4rem;'
    + 'border:1px solid #ffffff33;border-radius:5px;background:transparent;color:inherit;cursor:pointer;flex:0 0 auto">'
    + _esc(T("hp.cancel")) + "</button>";
}

// 完整面板 HTML（纯函数：便于单测，不碰 DOM）。alertsData 可选，给定时顶部渲染持续失配红框；
// campDiag 可选（活动海报判定行，P0 可诊断化）——缺省不渲染该行，旧调用零影响。
function renderPanelHtml(injectData, outboundData, alertsData, campDiag) {
  const inj = injectData || {};
  const out = outboundData || {};
  const injB = injectBadge(inj.summary);
  const outB = outboundBadge(out.summary);
  const accounts = Array.isArray(inj.accounts) ? inj.accounts : [];
  const recent = Array.isArray(out.recent) ? out.recent : [];
  const review = Array.isArray(out.review) ? out.review : [];
  const rateM = interceptRateModel(out);
  const corrM = correctionsModel(out);
  const clusterM = reasonClusterModel(out);

  let rows = "";
  if (!accounts.length) {
    rows = '<div style="color:#7f8c8d;font-size:.74rem;padding:.3rem 0">' + _esc(T("hp.no_inject")) + "</div>";
  } else {
    for (const a of accounts) {
      const m = accountRowModel(a);
      const color = _CLS_COLOR[m.cls] || "#7f8c8d";
      const dur = m.durationText ? _esc(T("hp.for_duration", { d: m.durationText })) : "";
      const staleTag = m.stale ? ' · <span style="color:#7f8c8d">' + _esc(T("hp.stale")) + "</span>" : "";
      rows += '<div style="display:flex;align-items:center;gap:.4rem;padding:.22rem 0;border-top:1px solid #ffffff14">'
        + '<span style="width:7px;height:7px;border-radius:50%;background:' + color + ';flex:0 0 auto"></span>'
        + '<span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.76rem">'
        + _esc(m.platform) + " / " + _esc(m.account_id) + "</span>"
        + '<span style="font-size:.72rem;color:' + color + '">' + _esc(m.statusText) + dur + staleTag + "</span>"
        + "</div>";
    }
  }

  let outRows = "";
  if (!recent.length) {
    outRows = '<div style="color:#7f8c8d;font-size:.74rem;padding:.3rem 0">' + _esc(T("hp.no_outbound")) + "</div>";
  } else {
    for (const it of recent.slice(0, 8)) outRows += renderOutboundRowHtml(it);
  }

  return ''
    + '<div style="padding:.5rem .65rem">'
    + '  <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:.35rem">'
    + '    <strong style="font-size:.82rem">' + _esc(T("hp.title")) + "</strong>"
    + '    <button id="cp-health-refresh" style="font-size:.72rem;padding:.12rem .55rem;border:1px solid #ffffff33;border-radius:6px;background:transparent;color:inherit;cursor:pointer">'
    + _esc(T("hp.refresh")) + "</button>"
    + "  </div>"
    + renderAlertsHtml(alertsData)
    + '  <div style="display:flex;flex-direction:column;gap:.5rem">'
    + '    <div>'
    + '      <div style="display:flex;align-items:center;gap:.4rem;margin-bottom:.2rem"><span style="font-size:.76rem;color:#bbb">'
    + _esc(T("hp.sec_inject")) + "</span>" + _badgeHtml(injB) + "</div>"
    + rows
    + '      <div style="display:flex;gap:.35rem;align-items:center;margin-top:.4rem;flex-wrap:wrap">'
    + '        <button id="cp-health-validate" style="font-size:.7rem;padding:.12rem .55rem;border:1px solid #ffffff33;border-radius:6px;background:transparent;color:inherit;cursor:pointer" title="'
    + _esc(T("hp.validate_title")) + '">' + _esc(T("hp.validate_btn")) + "</button>"
    + '        <button id="cp-health-reload" style="font-size:.7rem;padding:.12rem .55rem;border:1px solid #ffffff33;border-radius:6px;background:transparent;color:inherit;cursor:pointer" title="'
    + _esc(T("hp.reload_title")) + '">' + _esc(T("hp.reload_btn")) + "</button>"
    + '        <span id="cp-health-tools-msg" style="font-size:.7rem;color:#7f8c8d"></span>'
    + "      </div>"
    + "    </div>"
    + '    <div>'
    + '      <div style="display:flex;align-items:center;gap:.4rem;margin-bottom:.2rem;flex-wrap:wrap"><span style="font-size:.76rem;color:#bbb">'
    + _esc(T("hp.sec_outbound")) + "</span>" + _badgeHtml(outB)
    + '<span style="font-size:.68rem;color:' + (_CLS_COLOR[rateM.cls] || "#7f8c8d") + '">' + _esc(rateM.text) + "</span>"
    + (corrM.has ? '<span style="font-size:.68rem;color:#9b8cff" title="' + _esc(T("hp.corr_title")) + '">' + _esc(corrM.text) + "</span>" : "")
    + (corrM.has ? '<button id="cp-corr-export" title="' + _esc(T("hp.export_title"))
      + '" style="font-size:.64rem;padding:.03rem .35rem;border:1px solid #9b8cff66;border-radius:5px;background:#9b8cff1a;color:#9b8cff;cursor:pointer">'
      + _esc(T("hp.export_btn")) + "</button>" : "")
    + '<span id="cp-corr-export-msg" style="font-size:.66rem;color:#7f8c8d"></span>'
    + "</div>"
    + (clusterM.has ? '<div style="font-size:.66rem;color:#c39bd3;margin:.05rem 0 .1rem" title="' + _esc(T("hp.cluster_title")) + '">' + _esc(clusterM.text) + "</div>" : "")
    + renderReviewHtml(review)
    + outRows
    + "    </div>"
    + "  </div>"
    + renderCampaignRowHtml(campDiag)
    + '  <div style="margin-top:.45rem;font-size:.68rem;color:#7f8c8d">' + _esc(T("hp.footer")) + "</div>"
    + "</div>";
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    injectBadge, outboundBadge, formatDuration, injectStatusText,
    accountRowModel, renderPanelHtml, alertDotModel, renderAlertsHtml,
    formatValidateResult, outboundStatusText, outboundActions,
    outboundRowModel, renderOutboundRowHtml,
    interceptRateModel, renderReviewHtml, correctionsModel,
    renderInlineEditHtml, editSavePayload, slaBreachModel,
    reasonLabel, renderInterceptChipsHtml, reasonClusterModel,
    selectorDiagnosisModel, campaignDiagModel, renderCampaignRowHtml,
    // 持久化契约（落台账/供聚类）+ 显示层分离后的可断言面
    REASON_CODES, SELECTOR_KEYS, reasonOptions, selectorLabel,
  };
}

// ── 浏览器 DOM 装配（Node 单测时跳过）────────────────────────────────────────
if (typeof document !== "undefined" && document.addEventListener) {
  document.addEventListener("DOMContentLoaded", () => {
    const toggle = document.getElementById("cp-health-toggle");
    const panel = document.getElementById("cp-health-panel");
    if (!toggle || !panel) return;
    const accountsPanel = document.getElementById("cp-accounts-panel");
    let timer = null;
    let bgTimer = null;

    // 🔴 在 🩺 图标右上角放一个红点（持续失配时亮）。复用同一 span，避免重复创建。
    function setAlertDot(model) {
      toggle.style.position = "relative";
      toggle.title = model.title;
      let dot = toggle.querySelector(".cp-health-dot");
      if (!model.on) {
        if (dot) dot.remove();
        return;
      }
      if (!dot) {
        dot = document.createElement("span");
        dot.className = "cp-health-dot";
        toggle.appendChild(dot);
      }
      // urgent → 深红 + 外发光高亮，与普通红点区分严重度
      const urgent = model.level === "urgent";
      dot.setAttribute(
        "style",
        "position:absolute;top:-2px;right:-2px;min-width:13px;height:13px;"
        + "padding:0 3px;border-radius:999px;color:#fff;"
        + "font-size:9px;line-height:13px;text-align:center;font-weight:700;"
        + (urgent
          ? "background:#c0392b;box-shadow:0 0 0 2px #c0392b66,0 0 6px #e74c3c;"
          : "background:#e74c3c;")
      );
      dot.textContent = model.count > 9 ? "9+" : String(model.count);
    }

    // 人审 SLA：阈值默认值（后端无配置时回落）；分级 warn/urgent 各只通知一次。
    const SLA_THRESHOLD_SEC = 300;
    let slaWarnNotified = false;
    let slaUrgentNotified = false;

    function _notify(title, body) {
      if (window.shell && window.shell.notify) {
        try { window.shell.notify({ title, body }); } catch (_) {}
      }
    }

    // 统一健康评估：注入失配 + 人审 SLA → 红点；SLA 超时分级去重通知。inj 后台轮询可为 null。
    function applyHealth(inj, al, out) {
      const sla = slaBreachModel(out, SLA_THRESHOLD_SEC);
      setAlertDot(alertDotModel(inj, al, sla));
      const mins = Math.round(sla.ageSec / 60);
      if (sla.level === "urgent") {
        slaWarnNotified = true;  // 已越过 warn 阶段
        if (!slaUrgentNotified) {
          slaUrgentNotified = true;
          _notify(T("hp.sla_urgent_title"), T("hp.sla_urgent_body", { n: sla.count, mins }));
        }
      } else if (sla.level === "warn") {
        if (!slaWarnNotified) {
          slaWarnNotified = true;
          _notify(T("hp.sla_warn_title"), T("hp.sla_warn_body", { n: sla.count, mins }));
        }
      } else {
        slaWarnNotified = false;
        slaUrgentNotified = false;  // 超时解除 → 下次再超时可重新分级通知
      }
    }

    // 后台低频红点：取 alerts + 出站统计（含待审 SLA），面板未展开也能预警。
    async function refreshDot() {
      if (!window.shell || !window.shell.injectAlerts) return;
      try {
        const [al, out] = await Promise.all([
          window.shell.injectAlerts({}).catch(() => ({})),
          window.shell.outboundStats ? window.shell.outboundStats({}).catch(() => ({})) : Promise.resolve({}),
        ]);
        applyHealth(null, al, out);
        return al;
      } catch (e) {
        return null;
      }
    }

    async function refresh() {
      if (!window.shell || !window.shell.injectHealthList) return;
      try {
        const [inj, out, al, camp] = await Promise.all([
          window.shell.injectHealthList({}).catch(() => ({})),
          window.shell.outboundStats({}).catch(() => ({})),
          window.shell.injectAlerts ? window.shell.injectAlerts({}).catch(() => ({})) : Promise.resolve({}),
          window.shell.campaignDiag ? window.shell.campaignDiag().catch(() => null) : Promise.resolve(null),
        ]);
        panel.innerHTML = renderPanelHtml(inj, out, al, camp);
        applyHealth(inj, al, out);
        const rb = document.getElementById("cp-health-refresh");
        if (rb) rb.addEventListener("click", () => { refresh().catch(() => {}); });
        const fx = document.getElementById("cp-health-fix");
        if (fx && window.shell && window.shell.openSelectors) {
          fx.addEventListener("click", async () => {
            fx.disabled = true;
            const prev = fx.textContent;
            fx.textContent = T("hp.opening");
            try {
              const r = await window.shell.openSelectors();
              fx.textContent = r && r.ok ? T("hp.opened") : T("hp.open_failed");
            } catch (e) {
              fx.textContent = T("hp.open_failed");
            }
            setTimeout(() => { fx.textContent = prev; fx.disabled = false; }, 2500);
          });
        }
        const toolsMsg = document.getElementById("cp-health-tools-msg");
        const vb = document.getElementById("cp-health-validate");
        if (vb && window.shell && window.shell.validateSelectors) {
          vb.addEventListener("click", async () => {
            if (toolsMsg) toolsMsg.textContent = T("hp.validating");
            try {
              const r = await window.shell.validateSelectors();
              const m = formatValidateResult(r);
              if (toolsMsg) {
                toolsMsg.textContent = m.text;
                toolsMsg.style.color = _CLS_COLOR[m.cls] || "#7f8c8d";
              }
            } catch (e) {
              if (toolsMsg) toolsMsg.textContent = T("hp.val_fail_short");
            }
          });
        }
        const ex = document.getElementById("cp-corr-export");
        const exMsg = document.getElementById("cp-corr-export-msg");
        if (ex && window.shell && window.shell.exportCorrections) {
          ex.addEventListener("click", async () => {
            ex.disabled = true;
            if (exMsg) { exMsg.textContent = T("hp.exporting"); exMsg.style.color = "#7f8c8d"; }
            try {
              const r = await window.shell.exportCorrections({});
              if (exMsg) {
                if (r && r.ok) {
                  exMsg.textContent = T("hp.exported", { n: r.count });
                  exMsg.style.color = "#2ecc71";
                } else if (r && r.canceled) {
                  exMsg.textContent = "";
                } else {
                  exMsg.textContent = (r && r.error) || T("hp.export_failed");
                  exMsg.style.color = "#e74c3c";
                }
              }
            } catch (e) {
              if (exMsg) { exMsg.textContent = T("hp.export_failed"); exMsg.style.color = "#e74c3c"; }
            }
            ex.disabled = false;
          });
        }
        const rl = document.getElementById("cp-health-reload");
        if (rl) {
          rl.addEventListener("click", () => {
            const wvs = document.querySelectorAll("#webviews webview");
            let n = 0;
            wvs.forEach((wv) => { try { wv.reload(); n++; } catch (e) { /* 忽略单个失败 */ } });
            if (toolsMsg) {
              toolsMsg.textContent = n ? T("hp.reloaded", { n }) : T("hp.reload_none");
              toolsMsg.style.color = "#7f8c8d";
            }
          });
        }
      } catch (e) {
        panel.innerHTML = '<div style="padding:.6rem;color:#e74c3c;font-size:.76rem">'
          + _esc(T("hp.read_failed", { err: String(e) })) + "</div>";
      }
    }

    function show() {
      if (accountsPanel) accountsPanel.hidden = true;
      panel.hidden = false;
      refresh().catch(() => {});
      if (!timer) timer = setInterval(() => { refresh().catch(() => {}); }, 10000);
    }
    function hide() {
      panel.hidden = true;
      if (timer) { clearInterval(timer); timer = null; }
    }

    toggle.addEventListener("click", () => {
      if (panel.hidden) show(); else hide();
    });

    // 受控出站「人审介入」——委托点击（一次绑定；innerHTML 重渲不丢监听）。
    function startInlineEdit(btn, id) {
      const row = btn.closest("[data-oid]");
      if (!row) return;
      row.style.flexWrap = "wrap";
      row.innerHTML = renderInlineEditHtml(id);
      const inp = row.querySelector(".cp-ob-edit");
      if (inp) inp.focus();
    }

    // 拦截：展开结构化原因 chips（点分类即带 reason 拦截，留结构化负例样本）。
    function startInterceptReason(btn, id) {
      const row = btn.closest("[data-oid]");
      if (!row) return;
      row.style.flexWrap = "wrap";
      row.innerHTML = renderInterceptChipsHtml(id);
    }

    panel.addEventListener("click", async (e) => {
      const bulk = e.target.closest("[data-bulk]");
      if (bulk) {
        const action = bulk.getAttribute("data-bulk");
        const ids = (bulk.getAttribute("data-ids") || "").split(",").map(Number).filter(Boolean);
        if (ids.length && window.shell && window.shell.outboundAction) {
          if (action === "cancel" && typeof window.confirm === "function"
              && !window.confirm(T("hp.confirm_bulk_cancel", { n: ids.length }))) {
            return;
          }
          bulk.disabled = true;
          try { await window.shell.outboundAction({ ids, action }); } catch (_) {}
        }
        refresh().catch(() => {});
        return;
      }
      const air = e.target.closest("[data-edit-airewrite]");
      if (air) {
        const id = Number(air.getAttribute("data-edit-airewrite"));
        const row = air.closest("[data-oid]");
        const inp = row && row.querySelector(".cp-ob-edit");
        if (!id || !window.shell || !window.shell.outboundRewrite) return;
        const prev = air.textContent;
        air.disabled = true;
        air.textContent = T("hp.generating");
        try {
          const r = await window.shell.outboundRewrite({ id });
          if (r && r.ok && r.reply) {
            if (inp) { inp.value = r.reply; inp.dataset.aiSuggestion = r.reply; inp.focus(); }
            air.textContent = T("hp.ai_filled");
          } else {
            air.textContent = r && r.detail ? T("hp.no_context") : T("hp.failed_short");
          }
        } catch (_) {
          air.textContent = T("hp.failed_short");
        }
        setTimeout(() => { air.textContent = prev; air.disabled = false; }, 2500);
        return;
      }
      const save = e.target.closest("[data-edit-save]");
      if (save) {
        const id = Number(save.getAttribute("data-edit-save"));
        const row = save.closest("[data-oid]");
        const inp = row && row.querySelector(".cp-ob-edit");
        const text = inp ? inp.value : "";
        const aiSuggestion = inp && inp.dataset ? inp.dataset.aiSuggestion || "" : "";
        if (id && text.trim() && window.shell && window.shell.outboundAction) {
          try { await window.shell.outboundAction(editSavePayload(id, text, aiSuggestion)); } catch (_) {}
        }
        refresh().catch(() => {});
        return;
      }
      if (e.target.closest("[data-edit-cancel]")) { refresh().catch(() => {}); return; }
      const cr = e.target.closest("[data-cancel-reason]");
      if (cr) {
        const id = Number(cr.getAttribute("data-id"));
        const reason = cr.getAttribute("data-cancel-reason");
        if (id && window.shell && window.shell.outboundAction) {
          try { await window.shell.outboundAction({ id, action: "cancel", reason }); } catch (_) {}
        }
        refresh().catch(() => {});
        return;
      }
      if (e.target.closest("[data-cancel-abort]")) { refresh().catch(() => {}); return; }
      const btn = e.target.closest("[data-act]");
      if (!btn) return;
      const id = Number(btn.getAttribute("data-id"));
      const act = btn.getAttribute("data-act");
      if (!id) return;
      if (act === "edit") { startInlineEdit(btn, id); return; }
      if (act === "cancel") { startInterceptReason(btn, id); return; }
      if (!window.shell || !window.shell.outboundAction) return;
      btn.disabled = true;
      try { await window.shell.outboundAction({ id, action: act }); } catch (_) {}
      refresh().catch(() => {});
    });

    // 后台低频红点预警（面板未展开也提示）：首刷 + 每 60s。
    refreshDot().catch(() => {});
    bgTimer = setInterval(() => { if (panel.hidden) refreshDot().catch(() => {}); }, 60000);
    void bgTimer;
  });
}
