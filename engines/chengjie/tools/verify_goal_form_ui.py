# -*- coding: utf-8 -*-
"""工作目标「建目标弹层」草稿幸存层 + 编辑防打断 真浏览器门禁（Playwright；
P0 2026-08-04 随「弹层误触即丢/编辑中被外部刷新摧毁」修复落地）。

**为什么需要它**：坐席实录三连抱怨——鼠标移出弹层随手一点内容全丢、没动鼠标
编辑中弹层也会自己消失、丢了没有任何恢复手段。根因全是组件生命周期竞态
（背板=form_back 直连 / 宿主重喂 context → refresh() 整块 innerHTML 重建 /
renderData 分支切换强关表单），**静态门禁一条都抓不住**，只有真浏览器能证明
「输入在重建风暴里幸存」。修复三层：输入实时快照（sessionStorage 按会话 id）→
重建后回填（_applyFormDraft）→ 编辑期挂起同会话外部刷新（set context 覆写）。

**夹具模式**（与 tools/verify_goal_card_ui.py 同族，P21 决策）：file:// 自包含
页面 + 真组件文件（cp-panel-base.js + cp-goal.js）+ 假 fetch/假 sendBeacon——
零实例依赖（实例宕/重启冷却照跑）、零遥测污染（不给 goal_* 漏斗灌水）、
不加载 cp-i18n.js（i18n 用 goals.py pack 真 zh 词条内嵌替身）。

覆盖的不变量（编号对应 run() 里的断言）：
  S1  空态 → 设定目标 → 自定义卡 → gk-custom 弹层打开
  S2  输入推进方向/期限 → sessionStorage 出现草稿（params.note/tid 契约）
  S3  同会话重喂 context ×2 → **弹层 DOM 节点原封未动**（挂起而非重渲染）、值在
  S4  真 refresh()（loading 重建）→ 弹层回来、值恢复、「已恢复」提示条在
  S5  背板点击（有输入）→ 弹层不关、暂存提示可见、值在
  S6  Esc → 回第一步；自定义卡带「有草稿」徽标
  S7  重进同场景 → 值恢复 + 恢复条在
  S8  清空重填 → 值回默认、恢复条消失、sessionStorage 草稿清除
  S9  再输入 → 挂起一轮重喂 → Esc → 取消关表单 → **挂起的刷新补上**（fetch 日志增长）
  S10 重开「设定目标」→ 断点续写自动跳回第二步、值在
  S11 组件销毁重建（模拟页面刷新，同会话）→ 重开表单 → 值从 sessionStorage 恢复
  S12 取数失败（500）+ refresh → 表单不被错误行顶掉、值在
  S13 编辑中冒出进行中目标 + refresh → 表单不被强关、值在
  S14 清空后（干净表单）背板点击 → 照旧回第一步（误触防线只拦有输入的）
  S15 创建（Ctrl+Enter 键盘流）POST 契约带所填 note；成功后草稿清除、活跃视图接管
  P1 增量（同日）：
  S16 弹层新开＝自动聚焦第一个输入控件；S4 重建后焦点回到编辑位（lastFocusRef）
  S17 Tab 陷阱：末位控件按 Tab 回卷到弹层首位（不逃逸到工作台）
  S18 ×＝关闭整个表单（与「返回」分离）；草稿保留、重开即续写
  S2  输入时 footer 出常驻「自动暂存中」灰字微反馈（.dim）；S5 误触警示为 .warn
  P3 增量（2026-08-05）：
  S20 基类同会话刷新不清屏——慢取数（400ms）中途旧弹层 DOM 原封不动（无 loading 闪烁）
  S21 窄屏（390×844）弹层＝底部抽屉：贴底、全宽、只留顶部圆角

用法::

    python tools/verify_goal_form_ui.py             # 门禁模式
    python tools/verify_goal_form_ui.py --headed    # 肉眼看一遍

缺 playwright → SKIP exit 0（挂 gate_sweep -Full 的前提：环境缺失不污染回归信号）。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, List, Tuple

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(ENGINE / "tools"))

TEXT_A = "引导TA本周试用国际版并下单"
TEXT_B = "先聊旅行话题拉近关系再提产品"
TEXT_C = "引导TA把我们推荐给一位同行"

_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>cp-goal form probe</title></head>
<body style="margin:16px;font-family:system-ui,'Segoe UI','Microsoft YaHei',sans-serif;">
<script>
window.__FX__ = @@FIXTURES@@;
window.__ZH__ = @@ZH@@;
function __fmt(s, vars) {
  s = String(s == null ? '' : s);
  if (vars) for (const k of Object.keys(vars)) s = s.split('{' + k + '}').join(String(vars[k]));
  return s;
}
window.T = (k) => (window.__ZH__[k] != null ? window.__ZH__[k] : k);
window.Tf = (k, vars) => __fmt(window.T(k), vars);
window.CopilotShared = { lang: 'zh', t: (k, vars) => __fmt(window.T(k), vars) };
try { Object.defineProperty(navigator, 'sendBeacon', { value: () => true }); }
catch (_e) { navigator.sendBeacon = () => true; }
window.__GOAL_MODE = 'empty';
window.__FAIL_GOAL = 0;
window.__DELAY_GOAL = 0;
window.__fetchLog = [];
window.__posts = [];
const __respond = (d, st) => new Response(JSON.stringify(d),
  { status: st || 200, headers: { 'Content-Type': 'application/json' } });
window.fetch = async (url, opts) => {
  url = String(url);
  const method = String((opts && opts.method) || 'GET').toUpperCase();
  if (url.indexOf('/api/goals/templates') >= 0)
    return __respond(window.__FX__.templates);
  if (url.indexOf('/api/monetize/catalog') >= 0)
    return __respond({ ok: true, catalog: window.__FX__.catalog });
  if (url.indexOf('/api/goals/for-conversation') >= 0) {
    window.__fetchLog.push(url);
    if (window.__DELAY_GOAL) await new Promise((r) => setTimeout(r, window.__DELAY_GOAL));
    if (window.__FAIL_GOAL) return __respond({}, 500);
    return __respond(window.__GOAL_MODE === 'empty'
      ? { goal: null, last: null }
      : { goal: window.__FX__.goal, last: null });
  }
  if (url.indexOf('/api/goals/profile') >= 0)
    return __respond(window.__FX__.profA);
  if (url.indexOf('/api/goals/report') >= 0)
    return __respond(window.__FX__.report);
  if (url.indexOf('/api/goals/g_probe') >= 0)
    return __respond({ ok: true, actions: [] });
  if (method === 'POST' && url.endsWith('/api/goals')) {
    try { window.__posts.push(JSON.parse(String((opts && opts.body) || '{}'))); }
    catch (e) { window.__posts.push({ __bad: String(e) }); }
    window.__GOAL_MODE = 'active';
    return __respond({ ok: true, goal_id: 'g_new' });
  }
  return __respond({}, 404);
};
</script>
<script src="@@BASE_JS@@"></script>
<script src="@@GOAL_JS@@"></script>
</body></html>
"""


def build_fixture_page(tmp: Path) -> Path:
    import verify_goal_card_ui as cardgate  # 夹具单一事实源：模板/词条与卡门禁同源

    fixtures = cardgate._fixtures()
    html = (_HTML
            .replace("@@FIXTURES@@", json.dumps(fixtures, ensure_ascii=False))
            .replace("@@ZH@@", json.dumps(cardgate._zh_map(), ensure_ascii=False))
            .replace("@@BASE_JS@@",
                     (ENGINE / "shared/copilot/components/cp-panel-base.js").as_uri())
            .replace("@@GOAL_JS@@",
                     (ENGINE / "shared/copilot/components/cp-goal.js").as_uri()))
    fp = tmp / "probe_form.html"
    fp.write_text(html, encoding="utf-8")
    return fp


class Checker:
    def __init__(self) -> None:
        self.results: List[Tuple[str, bool]] = []

    def check(self, name: str, cond: Any, detail: str = "") -> bool:
        okk = bool(cond)
        self.results.append((name, okk))
        print(f"  [{'PASS' if okk else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        return okk

    def summary(self) -> int:
        fails = [n for n, okk in self.results if not okk]
        total = len(self.results)
        print(f"\n== 建目标弹层草稿幸存验证: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


# 整链场景（S1..S15）：单组件贯穿，模拟坐席真实编辑节奏 + 三类破坏事件。
_SCEN_JS = r"""
async (a) => {
  const cid = 'telegram:acc:qa_form_probe';
  const KEY = 'cp_goal_form_draft_v1:' + cid;
  try { sessionStorage.removeItem(KEY); } catch (_e) {}
  window.__GOAL_MODE = 'empty';
  window.__FAIL_GOAL = 0;
  const host = document.createElement('div');
  host.id = 'm_form';
  host.style.cssText = 'width:260px;outline:1px dashed #cbd5e1;';
  document.body.appendChild(host);
  let el = document.createElement('cp-goal');
  el.context = { conversationId: cid };
  host.appendChild(el);
  const sr = () => el.shadowRoot;
  const ta = () => sr().querySelector('textarea[data-param-key="note"]');
  const wait = async (fn, n) => {
    for (let i = 0; i < (n || 60); i++) {
      const v = fn();
      if (v) return v;
      await new Promise((r) => setTimeout(r, 50));
    }
    return null;
  };
  const tick = () => new Promise((r) => setTimeout(r, 80));
  const out = {};

  // ── S1 空态 → 设定目标 → 自定义卡 → 弹层 ─────────────────────────────
  const setBtn = await wait(() => sr().querySelector('[data-act="open_form"]'));
  if (!setBtn) return { error: 'no set button' };
  setBtn.click();
  const custom = await wait(() => sr().querySelector('.gl-scen-custom'));
  if (!custom) return { error: 'step1 not rendered' };
  custom.click();
  const m1 = await wait(() => sr().querySelector('.gl-ov .gl-modal.gk-custom'));
  out.s1_modal = !!m1;
  if (!m1) return Object.assign(out, { error: 'custom modal not shown' });
  await tick();
  // S16 新开弹层自动聚焦第一个输入控件（custom 场景＝推进方向 textarea）
  out.s16_focus = sr().activeElement === ta();

  // ── S2 输入 → sessionStorage 草稿契约 ────────────────────────────────
  ta().value = a.TEXT_A;
  ta().dispatchEvent(new Event('input', { bubbles: true }));
  const dy = sr().querySelector('[data-ref="days"]');
  dy.value = '5';
  dy.dispatchEvent(new Event('input', { bubbles: true }));
  await tick();
  out.s2_store = (() => {
    try {
      const d = JSON.parse(sessionStorage.getItem(KEY) || 'null');
      return !!(d && d.tid === 'custom' && d.cid === cid
        && d.params && d.params.note === a.TEXT_A && String(d.days) === '5');
    } catch (_e) { return false; }
  })();
  const mh2 = sr().querySelector('[data-ref="mhint"]');
  out.s2_autosaveNote = !!(mh2 && !mh2.hidden && mh2.classList.contains('dim'));

  // ── S3 同会话重喂 ×2 → 节点原封未动（挂起） ──────────────────────────
  const modal1 = sr().querySelector('.gl-modal');
  modal1.__mark = 'k1';
  el.context = { conversationId: cid };
  el.context = { conversationId: cid };
  await tick();
  const modal2 = sr().querySelector('.gl-modal');
  out.s3_sameNode = !!(modal2 && modal2 === modal1 && modal2.__mark === 'k1');
  out.s3_deferred = !!el._ctxDeferred;
  out.s3_value = ta().value === a.TEXT_A;

  // ── S4 真 refresh（loading 重建）→ 值恢复 + 恢复条 ───────────────────
  await el.refresh();
  const m4 = await wait(() => sr().querySelector('.gl-ov .gl-modal'));
  out.s4_modalBack = !!m4;
  out.s4_value = !!m4 && ta().value === a.TEXT_A
    && String(sr().querySelector('[data-ref="days"]').value) === '5';
  out.s4_restoreBar = !!sr().querySelector('.gl-mrestore');
  await tick();
  out.s4_focus = sr().activeElement === ta();   // 重建后焦点回到编辑位

  // ── S17 Tab 陷阱：末位控件按 Tab 回卷到弹层首位（×按钮） ─────────────
  const createBtn = sr().querySelector('.gl-mfoot [data-act="create"]');
  createBtn.focus();
  createBtn.dispatchEvent(new KeyboardEvent('keydown',
    { key: 'Tab', bubbles: true, composed: true }));
  await tick();
  out.s17_trap = sr().activeElement === sr().querySelector('.gl-mclose');
  ta().focus();   // 还原编辑位，后续场景不受影响

  // ── S5 背板点击（有输入）→ 不关 + 提示 ──────────────────────────────
  sr().querySelector('.gl-ov').click();
  await tick();
  out.s5_kept = !!sr().querySelector('.gl-modal');
  const mh = sr().querySelector('[data-ref="mhint"]');
  out.s5_hint = !!(mh && !mh.hidden && String(mh.textContent || '').length > 3
    && mh.classList.contains('warn'));
  out.s5_value = ta() && ta().value === a.TEXT_A;

  // ── S6 Esc → 回第一步 + 草稿徽标 ─────────────────────────────────────
  ta().dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await wait(() => !sr().querySelector('.gl-ov'));
  out.s6_step1 = !sr().querySelector('.gl-ov') && !!sr().querySelector('.gl-scenlist');
  out.s6_badge = !!sr().querySelector('.gl-scen-custom .gl-scen-draftb');

  // ── S7 重进同场景 → 恢复 ────────────────────────────────────────────
  sr().querySelector('.gl-scen-custom').click();
  const m7 = await wait(() => sr().querySelector('.gl-ov .gl-modal.gk-custom'));
  out.s7_value = !!m7 && ta().value === a.TEXT_A;
  out.s7_restoreBar = !!sr().querySelector('.gl-mrestore');

  // ── S8 清空重填 → 回默认 + 草稿清除 ─────────────────────────────────
  sr().querySelector('[data-act="draft_clear"]').click();
  await tick();
  out.s8_pristine = !!ta() && !ta().value && !sr().querySelector('.gl-mrestore');
  out.s8_storeGone = !sessionStorage.getItem(KEY);

  // ── S9 再输入 → 挂起一轮 → Esc → 取消 → 挂起刷新补上 ────────────────
  ta().value = a.TEXT_B;
  ta().dispatchEvent(new Event('input', { bubbles: true }));
  await tick();
  el.context = { conversationId: cid };
  out.s9_deferred = !!el._ctxDeferred;
  const n0 = window.__fetchLog.length;
  ta().dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await wait(() => !sr().querySelector('.gl-ov'));
  const cancel = sr().querySelector('[data-act="close_form"]');
  if (!cancel) return Object.assign(out, { error: 'no cancel button at step1' });
  cancel.click();
  out.s9_flushed = !!(await wait(() => window.__fetchLog.length > n0));
  await wait(() => sr().querySelector('[data-act="open_form"]'));

  // ── S10 重开表单 → 断点续写自动跳第二步 ─────────────────────────────
  sr().querySelector('[data-act="open_form"]').click();
  const m10 = await wait(() => sr().querySelector('.gl-ov .gl-modal.gk-custom'));
  out.s10_jumped = !!m10;
  out.s10_value = !!m10 && ta().value === a.TEXT_B;

  // ── S18 ×＝关闭整个表单（与「返回」分离）；草稿保留、重开续写 ─────────
  sr().querySelector('.gl-mclose').click();
  await wait(() => !sr().querySelector('.gl-ov') && sr().querySelector('[data-act="open_form"]'));
  out.s18_closedAll = !sr().querySelector('.gl-scenlist')
    && !!sr().querySelector('[data-act="open_form"]');
  sr().querySelector('[data-act="open_form"]').click();
  const m18 = await wait(() => sr().querySelector('.gl-ov .gl-modal.gk-custom'));
  out.s18_reopen = !!m18 && ta().value === a.TEXT_B;

  // ── S11 组件销毁重建（模拟页面刷新）→ sessionStorage 恢复 ────────────
  el.remove();
  el = document.createElement('cp-goal');
  el.context = { conversationId: cid };
  host.appendChild(el);
  const set11 = await wait(() => sr().querySelector('[data-act="open_form"]'));
  if (!set11) return Object.assign(out, { error: 'recreate: no set button' });
  set11.click();
  const m11 = await wait(() => sr().querySelector('.gl-ov .gl-modal.gk-custom'));
  out.s11_jumped = !!m11;
  out.s11_value = !!m11 && ta().value === a.TEXT_B;

  // ── S12 取数失败不打断 ───────────────────────────────────────────────
  window.__FAIL_GOAL = 1;
  await el.refresh();
  await tick();
  out.s12_kept = !!sr().querySelector('.gl-modal') && ta() && ta().value === a.TEXT_B;
  window.__FAIL_GOAL = 0;

  // ── S13 编辑中冒出进行中目标不强关 ──────────────────────────────────
  window.__GOAL_MODE = 'active';
  await el.refresh();
  await tick();
  out.s13_kept = !!sr().querySelector('.gl-modal') && ta() && ta().value === a.TEXT_B;
  window.__GOAL_MODE = 'empty';

  // ── S20 同会话刷新不清屏（P3 基类）：慢取数期间旧弹层原地不动 ─────────
  window.__DELAY_GOAL = 400;
  const m20 = sr().querySelector('.gl-modal');
  m20.__p3 = 'keep';
  const p20 = el.refresh();
  await new Promise((r) => setTimeout(r, 150));
  const mid20 = sr().querySelector('.gl-modal');
  out.s20_noFlash = !!(mid20 && mid20.__p3 === 'keep' && ta() && ta().value === a.TEXT_B);
  await p20;
  window.__DELAY_GOAL = 0;
  await tick();
  out.s20_after = !!sr().querySelector('.gl-modal') && ta().value === a.TEXT_B;

  // ── S14 清空后干净背板 → 照旧回第一步 ───────────────────────────────
  sr().querySelector('[data-act="draft_clear"]').click();
  await tick();
  out.s14_pristine = !!ta() && !ta().value && !sessionStorage.getItem(KEY);
  sr().querySelector('.gl-ov').click();
  await wait(() => !sr().querySelector('.gl-ov'));
  out.s14_backdropBack = !sr().querySelector('.gl-ov')
    && !!sr().querySelector('.gl-scenlist');

  // ── S15 创建契约（Ctrl+Enter 键盘流）+ 草稿收尾 ─────────────────────
  sr().querySelector('.gl-scen-custom').click();
  await wait(() => sr().querySelector('.gl-ov .gl-modal'));
  ta().value = a.TEXT_C;
  ta().dispatchEvent(new Event('input', { bubbles: true }));
  await tick();
  window.__posts = [];
  ta().dispatchEvent(new KeyboardEvent('keydown',
    { key: 'Enter', ctrlKey: true, bubbles: true, composed: true }));
  const posted = await wait(() => window.__posts.length > 0);
  out.s15_post = posted ? window.__posts[0] : null;
  await wait(() => !sr().querySelector('.gl-ov'));
  out.s15_storeGone = !sessionStorage.getItem(KEY);
  out.s15_activeView = !!(await wait(() =>
    sr().querySelector('.gl-created') || sr().querySelector('.gl-track')));
  return out;
}
"""


# S21 窄屏底部抽屉：390×844 视口（media query 随 resize 即时生效）新挂载一个组件，
# 打开自定义弹层后量计算样式——贴底(flex-end)/全宽/顶部圆角/底边零间隙。
_NARROW_JS = r"""
async () => {
  window.__GOAL_MODE = 'empty';
  const host = document.createElement('div');
  host.id = 'm_narrow';
  host.style.cssText = 'width:260px;';
  document.body.appendChild(host);
  const el = document.createElement('cp-goal');
  el.context = { conversationId: 'telegram:acc:qa_narrow' };
  host.appendChild(el);
  const sr = el.shadowRoot;
  const wait = async (fn, n) => {
    for (let i = 0; i < (n || 60); i++) {
      const v = fn();
      if (v) return v;
      await new Promise((r) => setTimeout(r, 50));
    }
    return null;
  };
  const setBtn = await wait(() => sr.querySelector('[data-act="open_form"]'));
  if (!setBtn) return { error: 'no set button' };
  setBtn.click();
  const custom = await wait(() => sr.querySelector('.gl-scen-custom'));
  if (!custom) return { error: 'no custom card' };
  custom.click();
  const ov = await wait(() => sr.querySelector('.gl-ov'));
  const modal = sr.querySelector('.gl-modal');
  if (!ov || !modal) return { error: 'modal not shown' };
  const cs = getComputedStyle(ov);
  const ms = getComputedStyle(modal);
  const r = modal.getBoundingClientRect();
  return {
    align: cs.alignItems,
    width: r.width, vw: window.innerWidth,
    radTL: ms.borderTopLeftRadius, radBL: ms.borderBottomLeftRadius,
    bottomGap: window.innerHeight - r.bottom,
  };
}
"""


def run(headed: bool) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    with tempfile.TemporaryDirectory(prefix="cpgoal_form_probe_") as td:
        page_fp = build_fixture_page(Path(td))
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(page_fp.as_uri())
            ck.check("cp-goal 组件已注册",
                     page.evaluate("!!customElements.get('cp-goal')"))
            r = page.evaluate(_SCEN_JS, {"TEXT_A": TEXT_A, "TEXT_B": TEXT_B,
                                         "TEXT_C": TEXT_C})
            if not isinstance(r, dict) or r.get("error"):
                ck.check("场景链执行", False, str(r))
                browser.close()
                return ck.summary()
            ck.check("[S1] 空态→自定义弹层打开", r.get("s1_modal"))
            ck.check("[S16] 弹层新开自动聚焦第一个输入控件", r.get("s16_focus"))
            ck.check("[S2] 输入实时进 sessionStorage（note/days/tid 契约）",
                     r.get("s2_store"))
            ck.check("[S2b] 输入时常驻「自动暂存中」灰字微反馈", r.get("s2_autosaveNote"))
            ck.check("[S3] 同会话重喂被挂起（弹层节点原封未动）",
                     r.get("s3_sameNode") and r.get("s3_deferred") and r.get("s3_value"))
            ck.check("[S4] 真 refresh 重建后值恢复 + 恢复条 + 焦点回编辑位",
                     r.get("s4_modalBack") and r.get("s4_value")
                     and r.get("s4_restoreBar") and r.get("s4_focus"))
            ck.check("[S17] Tab 陷阱：末位控件回卷到弹层首位", r.get("s17_trap"))
            ck.check("[S5] 有输入时背板点击不关 + 琥珀暂存警示",
                     r.get("s5_kept") and r.get("s5_hint") and r.get("s5_value"))
            ck.check("[S6] Esc 回第一步 + 场景卡草稿徽标",
                     r.get("s6_step1") and r.get("s6_badge"))
            ck.check("[S7] 重进同场景值恢复 + 恢复条",
                     r.get("s7_value") and r.get("s7_restoreBar"))
            ck.check("[S8] 清空重填＝回默认 + 草稿清除",
                     r.get("s8_pristine") and r.get("s8_storeGone"))
            ck.check("[S9] 关表单补跑挂起的外部刷新",
                     r.get("s9_deferred") and r.get("s9_flushed"))
            ck.check("[S10] 重开表单断点续写（自动跳第二步+值在）",
                     r.get("s10_jumped") and r.get("s10_value"))
            ck.check("[S18] ×＝关闭整个表单；草稿保留、重开续写",
                     r.get("s18_closedAll") and r.get("s18_reopen"))
            ck.check("[S11] 组件重建（模拟刷新）后从 sessionStorage 恢复",
                     r.get("s11_jumped") and r.get("s11_value"))
            ck.check("[S12] 取数失败不打断编辑", r.get("s12_kept"))
            ck.check("[S13] 编辑中冒出进行中目标不强关", r.get("s13_kept"))
            ck.check("[S20] 同会话慢刷新不清屏（弹层节点中途原封不动）",
                     r.get("s20_noFlash") and r.get("s20_after"))
            ck.check("[S14] 干净表单背板照旧回第一步",
                     r.get("s14_pristine") and r.get("s14_backdropBack"))
            post = r.get("s15_post") or {}
            pp = post.get("params") or {}
            ck.check("[S15] Ctrl+Enter 创建 POST 带所填 note + 草稿收尾 + 活跃视图接管",
                     post.get("template") == "custom" and pp.get("note") == TEXT_C
                     and r.get("s15_storeGone") and r.get("s15_activeView"),
                     json.dumps(post, ensure_ascii=False)[:120])
            # ── S21 窄屏底部抽屉（视口切 390×844，media query 即时生效）────
            page.set_viewport_size({"width": 390, "height": 844})
            rn = page.evaluate(_NARROW_JS)
            if isinstance(rn, dict) and not rn.get("error"):
                # bottomGap 合法值就是 0 → 不能用 `or` 兜底（falsy 零会被吞成缺省）
                _bg = rn.get("bottomGap")
                ck.check("[S21] 窄屏弹层＝底部抽屉（贴底/全宽/顶圆角/零底隙）",
                         rn.get("align") == "flex-end"
                         and abs(float(rn.get("width") or 0) - float(rn.get("vw") or 0)) <= 1.5
                         and str(rn.get("radTL")) == "14px"
                         and str(rn.get("radBL")) == "0px"
                         and abs(float(_bg if _bg is not None else 9)) <= 1.5,
                         json.dumps(rn, ensure_ascii=False)[:120])
            else:
                ck.check("[S21] 窄屏挂载渲染", False, str(rn))
            browser.close()
    return ck.summary()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    try:
        import playwright  # noqa: F401
    except Exception:
        print("[SKIP] playwright 未安装，跳过（exit 0）")
        return 0
    return run(args.headed)


if __name__ == "__main__":
    sys.exit(main())
