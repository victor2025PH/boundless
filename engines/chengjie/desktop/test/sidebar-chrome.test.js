"use strict";

// 业务助手侧栏 chrome 纯函数单测：node test/sidebar-chrome.test.js
const assert = require("assert");
const sc = require("../renderer/shared/copilot/sidebar-chrome.js");

let pass = 0;
function ok(name, cond) {
  assert.ok(cond, name);
  pass++;
}

const t = (k) => k;
const tf = (k, v) => {
  const m = {
    "inbox.pill.chain_failed": "{n} failed",
    "inbox.pill.chain_running": "{n} running",
  };
  let s = m[k] || t(k);
  if (v) Object.keys(v).forEach((p) => { s = s.split("{" + p + "}").join(String(v[p])); });
  return s;
};

// ── normalizeTab ─────────────────────────────────────────────────────────────
ok("normalize insight→customer", sc.normalizeTab("insight") === "customer");
ok("normalize invalid→reply", sc.normalizeTab("nope") === "reply");
ok("normalize tools", sc.normalizeTab("tools") === "tools");

// ── panelSuffix ──────────────────────────────────────────────────────────────
ok("panelSuffix ws-cp-", sc.panelSuffix("ws-cp-draft") === "draft");
ok("panelSuffix cp-", sc.panelSuffix("cp-relstage") === "relstage");

// ── pillMetaFromCpLoaded ─────────────────────────────────────────────────────
ok("draft guard high", sc.pillMetaFromCpLoaded(
  { ok: true, panelId: "ws-cp-draft", data: { generated: true, guardRisk: "high" } },
  { t, tf }
).tone === "danger");
ok("draft guard jump", !!sc.pillMetaFromCpLoaded(
  { ok: true, panelId: "cp-draft", data: { generated: true, guardRisk: "high" } },
  { t, tf }
).jump);
ok("draft guard no jump", sc.pillMetaFromCpLoaded(
  { ok: true, panelId: "cp-draft", data: { generated: true, guardRisk: "medium" } },
  { t, tf, jump: false }
).jump == null);
ok("draft ready", sc.pillMetaFromCpLoaded(
  { ok: true, panelId: "ws-cp-draft", data: { generated: true, intent: "hello world intent" } },
  { t, tf }
).tone === "ok");
ok("relstage reunion danger", sc.pillMetaFromCpLoaded(
  { ok: true, panelId: "ws-cp-relstage", data: { display_stage_label: "熟悉", progress_pct: 40, reunion: true } },
  { t, tf }
).tone === "danger");
ok("chain fail", sc.pillMetaFromCpLoaded(
  { ok: true, panelId: "ws-cp-chain", data: { executions: [{ status: "failed" }, { status: "failed" }] } },
  { t, tf }
).text.indexOf("2") >= 0);
// （原 nba pill 用例已于 2026-08-14 随「AI 下一步」面板整体下线删除）

// ── tabBadgeFromPillMeta ─────────────────────────────────────────────────────
ok("tab badge draft warn→reply", sc.tabBadgeFromPillMeta({ text: "x", tone: "warn" }, "ws-cp-draft").tab === "reply");
ok("tab badge relstage danger→customer", sc.tabBadgeFromPillMeta({ text: "x", tone: "danger" }, "cp-relstage").tab === "customer");
ok("tab badge relstage accent→null", sc.tabBadgeFromPillMeta({ text: "x", tone: "accent" }, "cp-relstage") === null);
// 2026-08-01：链卡随目标卡迁入「客户&关系」tab（web/app 两端同布局），徽章跟卡走
ok("tab badge chain fail→customer", sc.tabBadgeFromPillMeta({ text: "2 fail", tone: "danger" }, "ws-cp-chain").tab === "customer");

// ── badgeMetaFromLoaded ──────────────────────────────────────────────────────
ok("badgeMeta draft high", sc.badgeMetaFromLoaded(
  { ok: true, panelId: "cp-draft", data: { generated: true, guardRisk: "high" } },
  t
).tab === "reply");
ok("badgeMeta chain run", sc.badgeMetaFromLoaded(
  { ok: true, panelId: "ws-cp-chain", data: { executions: [{ status: "running" }] } },
  t
).tone === "ok");

// ── applyTabBadge (DOM) ──────────────────────────────────────────────────────
const el = { classList: { contains: () => true }, className: "", textContent: "" };
sc.applyTabBadge(el, "abcdefghijklmnopqrs", "warn");
ok("applyTabBadge dot glyph", el.textContent === "●");
ok("applyTabBadge full text→title", el.title === "abcdefghijklmnopqrs");
ok("applyTabBadge warn class", el.className.indexOf("warn") >= 0);
sc.applyTabBadge(el, "", "");
ok("applyTabBadge clears dot+title", el.textContent === "" && el.title === "");

// ── uiIcon ───────────────────────────────────────────────────────────────────
ok("uiIcon known→svg", sc.uiIcon("spark").indexOf("<svg") === 0);
ok("uiIcon size", sc.uiIcon("zap", 20).indexOf('width="20"') > 0);
ok("uiIcon unknown→empty", sc.uiIcon("nope") === "");

// ── createCardController：状态记忆 + 切换 + 懒展开回调（localStorage/document 模拟） ─
global.localStorage = (function () {
  let s = {};
  return { getItem: (k) => (k in s ? s[k] : null), setItem: (k, v) => { s[k] = String(v); }, removeItem: (k) => { delete s[k]; } };
})();
const _cards = {};
function _mkCard(name, collapsed) {
  const set = new Set(collapsed ? ["collapsed"] : []);
  const cel = {
    _set: set,
    getAttribute: (a) => (a === "data-cp-card" ? name : null),
    classList: {
      contains: (c) => set.has(c),
      toggle: (c, on) => { if (on === undefined) on = !set.has(c); on ? set.add(c) : set.delete(c); return on; },
      add: (c) => set.add(c), remove: (c) => set.delete(c),
    },
  };
  _cards[name] = cel;
  return cel;
}
_mkCard("draft", false);
_mkCard("voice", true);
let _icQuery = null;
global.document = {
  querySelector: (sel) => { const m = /\[data-cp-card="(.+?)"\]/.exec(sel); return m ? _cards[m[1]] : null; },
  querySelectorAll: (sel) => (sel === "[data-cp-ic]" && _icQuery ? _icQuery : Object.values(_cards)),
};
let _expanded = [];
const ctrl = sc.createCardController({ storageKey: "ws_cp_cards_v1", defaultCollapsed: { voice: 1 }, onExpand: (n) => _expanded.push(n) });
ok("ctrl default collapsed", ctrl.isCollapsed("voice") === true);
ok("ctrl default expanded", ctrl.isCollapsed("draft") === false);
ctrl.toggle("voice");
ok("ctrl toggle expands", ctrl.isCollapsed("voice") === false);
ok("ctrl toggle fires onExpand", _expanded.indexOf("voice") >= 0);
ctrl.toggle("draft");
ok("ctrl toggle collapses", ctrl.isCollapsed("draft") === true);
ok("ctrl collapse no onExpand", _expanded.indexOf("draft") < 0);
ok("ctrl persist to storage", JSON.parse(global.localStorage.getItem("ws_cp_cards_v1")).draft === 1);
ctrl.apply();
ok("apply → draft collapsed class", _cards.draft._set.has("collapsed"));
ok("apply → voice expanded class", !_cards.voice._set.has("collapsed"));

/* 三级优先：静态 class="collapsed" 是「HTML 侧声明的默认值」（2026-08-28）。
   此前 apply() 无条件按 storage/defaults 覆写 → HTML 写了 collapsed 却被展开
   （nurture 卡两个宿主都中招）。既不许被覆盖，也不许压过前两级。 */
_mkCard("nurture", true);          // HTML 声明收起、不在 defaults、无 storage
_mkCard("image", false);           // HTML 未声明、不在 defaults → 仍展开
ok("static collapsed class = 第三级默认", ctrl.isCollapsed("nurture") === true);
ok("无静态 class 且不在 defaults → 展开", ctrl.isCollapsed("image") === false);
ctrl.apply();
ok("apply 不抹掉 HTML 声明的 collapsed", _cards.nurture._set.has("collapsed"));
ok("apply 不给未声明的卡加 collapsed", !_cards.image._set.has("collapsed"));
ctrl.toggle("nurture");            // 坐席显式展开 → 存储该压过静态 class
ok("storage 压过静态 class", ctrl.isCollapsed("nurture") === false);
ok("toggle 后 DOM 同步展开", !_cards.nurture._set.has("collapsed"));
ctrl.apply();
ok("apply 后仍尊重坐席选择", !_cards.nurture._set.has("collapsed"));
/* defaults 压过静态 class：voice 在 defaults=1，若 HTML 反着写也按 defaults */
_mkCard("voice2", false);
const ctrl2 = sc.createCardController({ storageKey: "ws_cp_cards_v2",
  defaultCollapsed: { voice2: 1 } });
ok("defaults 压过静态 class（第二级优先第三级）", ctrl2.isCollapsed("voice2") === true);

// ── decorateCardIcons：填 SVG + 幂等 ─────────────────────────────────────────
const _icEl = { __icDone: 0, getAttribute: () => "spark", innerHTML: "" };
_icQuery = [_icEl];
sc.decorateCardIcons(global.document, 14);
ok("decorate fills svg", _icEl.innerHTML.indexOf("<svg") === 0);
ok("decorate sets idempotent flag", _icEl.__icDone === 1);

console.log("sidebar-chrome.test.js: " + pass + " passed");
if (pass < 32) process.exit(1);
