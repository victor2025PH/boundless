# -*- coding: utf-8 -*-
"""工作目标卡（cp-goal）窄宽布局 + 画像交互链 真浏览器门禁（Playwright；2026-08-01 P21）。

**为什么需要它**：P20 修的竖排事故（画像头部在默认 300px 侧栏被 flex 挤压 →
CJK 逐字换行）之所以能上生产，就是因为**没有窄宽回归**——静态门禁只能钉
「CSS 里没有 width:52px」，证不了「216px 容器里标题真的只占一行」。shadow DOM
计算布局 + 点击事件链只有真浏览器测得到，而共享组件热更新直上生产。

**与 verify_* 家族的刻意差异（P21 决策）**：不登录真实例、不驱动 /workspace，
改用 **file:// 自包含夹具页**——真组件文件（cp-panel-base.js + cp-goal.js）+
假 fetch（fixture 数据）+ 假 sendBeacon：
  1. 零实例依赖：实例宕/重启冷却/忙时照样可跑，CI 也能跑；只缺 playwright 才 SKIP。
  2. **零遥测污染**：探针要点「拟稿去问」按钮——在真工作台里跑会给 goal_slot_ask
     漏斗计数灌水，恰好污染 7 天复盘要读的那个数；夹具页把 beacon 全断掉。
  3. 不加载 cp-i18n.js（共享树上它常被别的线编辑，半保存态会让探针闪红）；
     i18n 用 goals.py pack 的**真实 zh 词条**内嵌替身——布局测的就是真实标签宽度。
夹具页证不了「宿主 script 标签接线/键存在」——那两条已有静态门禁
（test_goal_ui_revamp 的 ?v= 断言 + i18n 双语门禁），职责不重叠。

覆盖的不变量：
  A. 窄宽布局（216/236/276px 三档，含 280px 最窄侧栏的内容区）：
     - 画像标题「客户画像」单行（高度 <26px；竖排会是 4 行 ≈60px+）
     - 「补录」按钮单行
     - 任何 .gl-sec 不横向溢出（scrollWidth<=clientWidth+1）
     - 完成度行存在且两条 bar（fixture A 有填充）
  B. 双零冷启动（fixture B）：不渲染 .gl-fillrow、空态文案在、缺口 chips 仍在
  C. 交互链（静态门禁测不到的部分）：
     - 点缺口 chip → 追问行出现（建议问法 + 双按钮）
     - 点「拟稿去问」→ cp-goal-drive-draft 事件带已替换占位符的 intent（含槽位标签）
     - 点「我来补录」→ 补录表单打开且焦点落在对应字段
     - 点已填 chip → 表单打开且焦点落在该字段
     - 产品行：pitch 常显（offsetHeight>0）、价格文本在、外链 href 在
  E. 改期限 + 临期决策行（P25/P1）：
     - 点「第X/Y天」→ 内联表单：min=day_index、低于下限的预设 chip 隐藏、
       近30天同模板基准线（fixture report：终局6 → 67%·6.5天）
     - 低于下限预检＝就地报错 + 零请求（与后端护栏同口径）；
       合法保存＝POST {deadline_days} 契约 + 表单收起
     - 临期夹具（第9/10天）→ 琥珀 chip「明天到期」+「今天收口/延长7天」，
       点延长 → POST deadline_days=17（10+7）
  D. 建目标两步向导（P24；空态 fixture，模板=真实注册表 list_templates()）：
     - 第一步：分组场景卡（≥2 组、≥7 卡、全带线稿图标）+ 自定义高亮卡
     - 第二步弹层：宽 >300px（逃离窄栏）、类别色带、节奏预览 4 段带力度 pill
     - 解锁项下拉（目录项+自定义）：称呼自动跟随所选项；切「自定义…」时
       item_id 归属互斥切换（select ↔ 手填框）
     - 参与度三张单选卡（默认 suggest）；点 auto 档出诚实信息气泡
     - 返回=弹层关第一步在；自定义面板 textarea+示例 chips 点击填入
     - 创建 POST 契约（template/item_id/item_label/autonomy/deadline_days）
       → 弹层关 + 「接下来会发生什么」提示出现

用法::

    python tools/verify_goal_card_ui.py                      # 门禁模式
    python tools/verify_goal_card_ui.py --shots tmp_probe    # 附截图（调试/验收）
    python tools/verify_goal_card_ui.py --headed             # 肉眼看一遍

缺 playwright → SKIP exit 0（挂 gate_sweep -Full 的前提：环境缺失不污染回归信号）。
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))

WIDTHS = (216, 236, 276)
TITLE_MAX_H = 26.0   # 单行 11px 字号 ≈16-17px；竖排（4 行）≈60px+，余量充足
BTN_MAX_H = 26.0

# ── fixture（标签/槽位来自真实注册表与 i18n 包，布局量的就是真实文本宽度）──


def _zh_map() -> Dict[str, str]:
    from src.web.i18n_packs.goals import ZH
    out = {k: v for k, v in ZH.items() if k.startswith("inbox.goal.")}
    out.update({
        "cp.common.cancel": "取消",
        "cp.common.loading": "加载中…",
        "cp.common.save_fail": "保存失败",
    })
    return out


def _slots(filled: bool) -> List[Dict[str, Any]]:
    from src.companion.goals.profile_slots import SLOTS
    rows: List[Dict[str, Any]] = []
    fills = {"name": ("阿龙", "auto"), "occupation": ("民宿老板", "agent")}
    for s in SLOTS:
        v, src = ("", "")
        if filled and s["key"] in fills:
            v, src = fills[s["key"]]
        rows.append({"key": s["key"], "track": s["track"],
                     "label": s["label_zh"], "value": v, "src": src, "ts": 0})
    return rows


def _templates_fixture() -> Dict[str, Any]:
    """建目标向导的模板 fixture＝真实注册表形状（含 P24 push_curve）——
    向导渲染的就是生产同款 schema。caps 置 false 以覆盖「auto 档诚实气泡」；
    刻意不带 pickers：走 /api/monetize/catalog 回落路径（旧后端兼容面）。"""
    from src.companion.goals.templates import (
        AUTONOMY_LEVELS,
        GOAL_STATUSES,
        list_templates,
    )
    return {
        "templates": list_templates(),
        "autonomy_levels": list(AUTONOMY_LEVELS),
        "statuses": list(GOAL_STATUSES),
        "caps": {"bridge_enabled": False, "proactive_enabled": False},
    }


def _fixtures() -> Dict[str, Any]:
    goal = {
        "goal_id": "g_probe", "conversation_id": "telegram:acc:qa_probe_goal",
        "status": "active", "template": "acquire_and_convert",
        "template_name": "获客转化（官网产品）", "title": "获客转化（官网产品）",
        "autonomy": "suggest", "hold": "",
        "milestones": [{"zh": "破冰", "en": "Icebreak"}, {"zh": "摸清底细", "en": "Qualify"},
                       {"zh": "种草", "en": "Seed"}, {"zh": "收口", "en": "Close"}],
        "milestone_idx": 1, "day_index": 4, "total_days": 10, "progress": 0.18,
        "profile_slots": True,
        "today": {"intent": "顺着生意话题摸一摸对方的日常痛点（人手/回消息/语言/获客）",
                  "push_level": "soft", "status": "planned", "detail": ""},
        "products": [
            {"name": "智聊 ChatX", "price_from": "$58/月",
             "pitch": "多平台 AI 客服，人手不够也能秒回", "url": "https://bd2026.cc/chatx"},
            {"name": "通译 LingoX", "price_from": "$99/月",
             "pitch": "聊天实时互译，外语客户直接聊"},
        ],
    }
    prof_a = {"platform": "telegram", "chat_key": "qa_probe_goal",
              "slots": _slots(True),
              "fill": {"relation": 0.25, "bant": 0.17, "filled": 2, "total": 11},
              "missing_bant": ["need", "channel", "team_size",
                               "budget", "authority", "timeline"],
              "updated_at": 0}
    prof_b = {"platform": "telegram", "chat_key": "qa_probe_goal",
              "slots": _slots(False),
              "fill": {"relation": 0.0, "bant": 0.0, "filled": 0, "total": 11},
              "missing_bant": ["need", "channel", "team_size",
                               "budget", "authority", "timeline"],
              "updated_at": 0}
    catalog = {
        "currency": "USD",
        "items": {
            "bazi_reading": {"price": 4.99, "label": "命理详批"},
            "exclusive_album": {"price": 4.99, "label": "专属相册"},
        },
        "tiers": {
            "free": {"monthly": 0, "label": "免费"},
            "vip": {"monthly": 9.9, "label": "VIP 会员"},
        },
    }
    # P25/P1：临期变体（第 9/10 天=明天到期 → chip+双快捷动作）与近30天基准
    # report fixture（acquire_and_convert 终局 6=done4+failed1+expired1 → 67%）
    goal_due = dict(goal)
    goal_due.update({"goal_id": "g_due", "day_index": 9, "total_days": 10,
                     "conversation_id": "telegram:acc:qa_due_goal"})
    report = {
        "by_template": {
            "acquire_and_convert": {
                "done": 4, "failed": 1, "expired": 1, "cancelled": 2,
                "n": 8, "avg_days_to_done": 6.5,
            },
        },
    }
    return {"goal": goal, "goalDue": goal_due, "profA": prof_a,
            "profB": prof_b, "templates": _templates_fixture(),
            "catalog": catalog, "report": report}


_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>cp-goal probe</title></head>
<body style="margin:16px;font-family:system-ui,'Segoe UI','Microsoft YaHei',sans-serif;
             display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start;">
<script>
window.__FX__ = @@FIXTURES@@;
window.__ZH__ = @@ZH@@;
window.__drives = [];
document.addEventListener('cp-goal-drive-draft', (e) => window.__drives.push(e.detail || {}));
// i18n 替身：真实 zh 词条（布局量真实标签宽度）；Tf 做 {var} 替换
function __fmt(s, vars) {
  s = String(s == null ? '' : s);
  if (vars) for (const k of Object.keys(vars)) s = s.split('{' + k + '}').join(String(vars[k]));
  return s;
}
window.T = (k) => (window.__ZH__[k] != null ? window.__ZH__[k] : k);
window.Tf = (k, vars) => __fmt(window.T(k), vars);
window.CopilotShared = { lang: 'zh', t: (k, vars) => __fmt(window.T(k), vars) };
// 零遥测外发：探针点击不许污染 ui-event 漏斗（那正是 7 天复盘要读的数）
try { Object.defineProperty(navigator, 'sendBeacon', { value: () => true }); }
catch (_e) { navigator.sendBeacon = () => true; }
// 假 fetch：按 URL 发 fixture；profile 按 window.__PROFILE_MODE 取 A/B；
// 向导流按 window.__GOAL_MODE（empty=空态起步，创建成功后翻 active 镜像生产）
window.__PROFILE_MODE = 'A';
window.__GOAL_MODE = 'active';
window.__GOAL_VARIANT = '';
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
  if (url.indexOf('/api/goals/report') >= 0)
    return __respond(window.__FX__.report);
  if (url.indexOf('/api/goals/for-conversation') >= 0)
    return __respond(window.__GOAL_MODE === 'empty'
      ? { goal: null, last: null }
      : { goal: window.__GOAL_VARIANT === 'due'
          ? window.__FX__.goalDue : window.__FX__.goal, last: null });
  if (url.indexOf('/api/goals/profile') >= 0)
    return __respond(window.__PROFILE_MODE === 'B' ? window.__FX__.profB : window.__FX__.profA);
  if (method === 'POST' && url.indexOf('/update') >= 0) {
    // 改期限 POST：记契约、回带 goal（生产同形状）——须在 g_probe GET 之前匹配
    try { window.__posts.push(JSON.parse(String((opts && opts.body) || '{}'))); }
    catch (e) { window.__posts.push({ __bad: String(e) }); }
    return __respond({ ok: true, goal: window.__GOAL_VARIANT === 'due'
      ? window.__FX__.goalDue : window.__FX__.goal });
  }
  if (url.indexOf('/api/goals/g_probe') >= 0 || url.indexOf('/api/goals/g_due') >= 0)
    return __respond({ ok: true, actions: [] });
  if (method === 'POST' && url.endsWith('/api/goals')) {
    try { window.__posts.push(JSON.parse(String((opts && opts.body) || '{}'))); }
    catch (e) { window.__posts.push({ __bad: String(e) }); }
    window.__GOAL_MODE = 'active';  // 创建成功 → refresh 拿到活跃目标（生产同流）
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
    # 占位符刻意用 @@..@@ 形式——首版用 __ZH__ 撞上变量名 window.__ZH__ 自身，
    # str.replace 把变量名也换成了 JSON → 内联脚本语法崩溃（fetch 替身全灭）。
    html = (_HTML
            .replace("@@FIXTURES@@", json.dumps(_fixtures(), ensure_ascii=False))
            .replace("@@ZH@@", json.dumps(_zh_map(), ensure_ascii=False))
            .replace("@@BASE_JS@@",
                     (ENGINE / "shared/copilot/components/cp-panel-base.js").as_uri())
            .replace("@@GOAL_JS@@",
                     (ENGINE / "shared/copilot/components/cp-goal.js").as_uri()))
    fp = tmp / "probe.html"
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
        print(f"\n== 工作目标卡窄宽/交互验证: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


# 挂载一个 cp-goal 并量布局。所有等待都是轮询 shadowRoot（refresh 是异步的）。
_MOUNT_JS = r"""
async (a) => {
  window.__PROFILE_MODE = a.mode;
  const host = document.createElement('div');
  host.id = a.hostId;
  host.style.cssText = 'width:' + a.width + 'px;flex:0 0 auto;outline:1px dashed #cbd5e1;';
  document.body.appendChild(host);
  const el = document.createElement('cp-goal');
  el.context = { conversationId: 'telegram:acc:qa_' + a.hostId };
  host.appendChild(el);
  const sr = el.shadowRoot;
  let profSec = null;
  for (let i = 0; i < 60; i++) {
    profSec = Array.from(sr.querySelectorAll('.gl-sec.gl-prof'))
      .find((s) => s.querySelector('[data-act="prof_toggle"]')) || null;
    if (profSec) break;
    await new Promise((r) => setTimeout(r, 50));
  }
  if (!profSec) return { error: 'profile section not rendered' };
  const prodSec = Array.from(sr.querySelectorAll('.gl-sec.gl-prof'))
    .find((s) => s.querySelector('.gl-prods')) || null;
  const rect = (q, root) => {
    const n = (root || profSec).querySelector(q);
    if (!n) return null;
    const r = n.getBoundingClientRect();
    return { w: r.width, h: r.height };
  };
  const overflow = [];
  sr.querySelectorAll('.gl-sec, .wrap').forEach((s) => {
    if (s.scrollWidth > s.clientWidth + 1)
      overflow.push((s.className || 'wrap') + ':' + s.scrollWidth + '>' + s.clientWidth);
  });
  return {
    title: rect('.gl-prof-t'),
    editBtn: rect('[data-act="prof_toggle"]'),
    fillrow: !!profSec.querySelector('.gl-fillrow'),
    bars: profSec.querySelectorAll('.gl-fillbar').length,
    pcts: Array.from(profSec.querySelectorAll('.gl-fillbar .pct')).map((n) => n.textContent),
    missChips: profSec.querySelectorAll('button[data-act="slot_menu"]').length,
    filledChips: profSec.querySelectorAll('button[data-act="slot_edit"]').length,
    emptyText: !!profSec.querySelector('.gl-prof-empty'),
    overflow: overflow,
    prod: prodSec ? {
      pitchH: (prodSec.querySelector('.gl-prod-pitch') || { offsetHeight: 0 }).offsetHeight,
      price: (prodSec.querySelector('.gl-prod-price') || {}).textContent || '',
      link: (prodSec.querySelector('a.gl-prod') || {}).href || '',
    } : null,
  };
}
"""

# 在既有挂载上执行一步交互（按 hostId 找回组件；步骤见 op 分支注释）。
_STEP_JS = r"""
async (a) => {
  const host = document.getElementById(a.hostId);
  if (!host) return { error: 'host missing' };
  const el = host.querySelector('cp-goal');
  const sr = el.shadowRoot;
  const profSec = () => Array.from(sr.querySelectorAll('.gl-sec.gl-prof'))
    .find((s) => s.querySelector('[data-act="prof_toggle"]'));
  const wait = async (fn) => {
    for (let i = 0; i < 40; i++) {
      const v = fn();
      if (v) return v;
      await new Promise((r) => setTimeout(r, 50));
    }
    return null;
  };
  if (a.op === 'open_menu') {           // 点缺口 chip → 追问行出现
    const chip = profSec().querySelector('button[data-act="slot_menu"]');
    if (!chip) return { error: 'no miss chip' };
    const slot = chip.getAttribute('data-slot');
    chip.click();
    const ask = await wait(() => profSec().querySelector('.gl-ask'));
    if (!ask) return { error: 'ask row not shown' };
    return { slot: slot, lead: (ask.querySelector('.gl-ask-q') || {}).textContent || '',
             btns: Array.from(ask.querySelectorAll('button')).map((b) => b.getAttribute('data-act')) };
  }
  if (a.op === 'ask') {                 // 点「拟稿去问」→ 事件带替换后的 intent
    const b = profSec().querySelector('.gl-ask button[data-act="slot_ask"]');
    if (!b) return { error: 'no ask button' };
    const before = window.__drives.length;
    b.click();
    const got = await wait(() => window.__drives.length > before);
    if (!got) return { error: 'cp-goal-drive-draft not emitted' };
    return { detail: window.__drives[window.__drives.length - 1] };
  }
  if (a.op === 'fill') {                // 点「我来补录」→ 表单开 + 焦点在该字段
    const chip = profSec().querySelector('button[data-act="slot_menu"]');
    chip.click();
    await wait(() => profSec().querySelector('.gl-ask'));
    const slot = chip.getAttribute('data-slot');
    const b = profSec().querySelector('.gl-ask button[data-act="slot_fill"]');
    b.click();
    const form = await wait(() => profSec() && profSec().querySelector('.gl-prof-form'));
    if (!form) return { error: 'form not opened' };
    const acts = sr.activeElement;
    return { slot: slot, groups: profSec().querySelectorAll('.gl-pf-group').length,
             placeholders: Array.from(profSec().querySelectorAll('input[placeholder]'))
               .filter((i) => i.getAttribute('placeholder')).length,
             focusKey: acts ? acts.getAttribute('data-prof-key') : '' };
  }
  if (a.op === 'edit_filled') {         // 点已填 chip → 表单开 + 焦点在该字段
    const chip = profSec().querySelector('button[data-act="slot_edit"]');
    if (!chip) return { error: 'no filled chip' };
    const slot = chip.getAttribute('data-slot');
    chip.click();
    const form = await wait(() => profSec() && profSec().querySelector('.gl-prof-form'));
    if (!form) return { error: 'form not opened' };
    const acts = sr.activeElement;
    return { slot: slot, focusKey: acts ? acts.getAttribute('data-prof-key') : '' };
  }
  return { error: 'unknown op ' + a.op };
}
"""

# 建目标两步向导整链（P24）：空态挂载 → 第一步分组卡 → 解锁弹层（下拉联动/
# 自定义切换/参与度卡/诚实气泡）→ 返回 → 自定义面板（示例填入）→ 创建 POST。
_WIZ_JS = r"""
async (a) => {
  window.__GOAL_MODE = 'empty';
  window.__posts = [];
  const host = document.createElement('div');
  host.id = a.hostId;
  host.style.cssText = 'width:' + a.width + 'px;flex:0 0 auto;outline:1px dashed #cbd5e1;';
  document.body.appendChild(host);
  const el = document.createElement('cp-goal');
  el.context = { conversationId: 'telegram:acc:qa_' + a.hostId };
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
  const tick = () => new Promise((r) => setTimeout(r, 60));
  const out = {};
  const setBtn = await wait(() => sr.querySelector('[data-act="open_form"]'));
  if (!setBtn) return { error: 'no set button' };
  setBtn.click();
  const list = await wait(() => sr.querySelector('.gl-scenlist'));
  if (!list) return { error: 'step1 not rendered' };
  out.groups = sr.querySelectorAll('.gl-grp').length;
  out.cards = sr.querySelectorAll('.gl-scen').length;
  out.customCard = !!sr.querySelector('.gl-scen-custom');
  out.icons = sr.querySelectorAll('.gl-scen .gl-scen-ic svg').length;
  // ── 第二步：付费解锁弹层 ─────────────────────────────────────────
  const unlockCard = sr.querySelector('.gl-scen[data-tid="conversion_unlock"]');
  if (!unlockCard) return Object.assign(out, { error: 'no unlock card' });
  unlockCard.click();
  const modal = await wait(() => sr.querySelector('.gl-ov .gl-modal'));
  if (!modal) return Object.assign(out, { error: 'modal not shown' });
  out.modalW = modal.getBoundingClientRect().width;
  out.modalKind = String(modal.className || '');
  out.band = !!modal.querySelector('.gl-mband');
  out.arcRows = modal.querySelectorAll('.gl-arc-row').length;
  out.arcPills = modal.querySelectorAll('.gl-arc-row .gl-pill').length;
  const sel = modal.querySelector('select[data-chg="unlock_sel"]');
  if (!sel) return Object.assign(out, { error: 'no unlock select' });
  out.unlockOpts = sel.options.length;
  out.unlockParamKey = sel.getAttribute('data-param-key');
  const labelInp = modal.querySelector('input[data-param-key="item_label"]');
  out.labelInit = labelInp ? labelInp.value : '';
  // 换选第二项 → 称呼跟随
  sel.value = sel.options[1].value;
  sel.dispatchEvent(new Event('change', { bubbles: true }));
  await tick();
  out.labelFollow = labelInp ? labelInp.value : '';
  out.opt2Label = String(sel.options[1].textContent || '').split(' \u00b7 ')[0];
  // 「自定义…」：item_id 归属互斥切换（select ↔ 手填框）
  sel.value = '__custom__';
  sel.dispatchEvent(new Event('change', { bubbles: true }));
  await tick();
  const cwrap = modal.querySelector('[data-ref="unlock_custom"]');
  const cinp = modal.querySelector('[data-ref="unlock_custom_id"]');
  out.customShown = !!(cwrap && !cwrap.hidden);
  out.customOwnsKey = !!(cinp && cinp.getAttribute('data-param-key') === 'item_id')
    && !sel.getAttribute('data-param-key');
  sel.value = sel.options[0].value;
  sel.dispatchEvent(new Event('change', { bubbles: true }));
  await tick();
  out.customHidden = !!(cwrap && cwrap.hidden);
  // 参与度三卡 + auto 档诚实气泡（caps.bridge_enabled=false）
  out.autoCards = modal.querySelectorAll('.gl-auto-card').length;
  const selCard = modal.querySelector('.gl-auto-card.sel');
  out.autoDefault = selCard ? selCard.getAttribute('data-lvl') : '';
  const note0 = modal.querySelector('[data-ref="anote"]');
  out.noteHiddenBefore = !!(note0 && note0.hidden);
  const autoCard = modal.querySelector('.gl-auto-card[data-lvl="auto"]');
  if (autoCard) autoCard.click();
  await tick();
  out.noteShownAfterAuto = !!(note0 && !note0.hidden
    && String(note0.textContent || '').length > 4);
  const sugCard = modal.querySelector('.gl-auto-card[data-lvl="suggest"]');
  if (sugCard) sugCard.click();
  // 返回 → 弹层关、第一步在
  modal.querySelector('.gl-mfoot [data-act="form_back"]').click();
  await wait(() => !sr.querySelector('.gl-ov'));
  out.backToStep1 = !sr.querySelector('.gl-ov') && !!sr.querySelector('.gl-scenlist');
  // ── 自定义目标面板：textarea + 示例 chips 填入 ────────────────────
  sr.querySelector('.gl-scen-custom').click();
  const modal3 = await wait(() => sr.querySelector('.gl-ov .gl-modal.gk-custom'));
  out.customModal = !!modal3;
  if (modal3) {
    const ta = modal3.querySelector('textarea[data-param-key="note"]');
    out.customTa = !!ta;
    out.exChips = modal3.querySelectorAll('[data-act="note_example"]').length;
    const chip = modal3.querySelector('[data-act="note_example"]');
    if (chip && ta) {
      chip.click();
      await tick();
      out.exFilled = String(ta.value || '').length > 4;
    }
    modal3.querySelector('.gl-mfoot [data-act="form_back"]').click();
    await wait(() => !sr.querySelector('.gl-ov'));
  }
  // ── 重开解锁弹层 → 创建 → POST 契约 + 建后提示 ────────────────────
  sr.querySelector('.gl-scen[data-tid="conversion_unlock"]').click();
  const modal2 = await wait(() => sr.querySelector('.gl-ov .gl-modal'));
  if (!modal2) return Object.assign(out, { error: 'modal2 not shown' });
  modal2.querySelector('.gl-mfoot [data-act="create"]').click();
  const posted = await wait(() => window.__posts.length > 0);
  out.post = posted ? window.__posts[0] : null;
  await wait(() => !sr.querySelector('.gl-ov'));
  out.modalClosedAfterCreate = !sr.querySelector('.gl-ov');
  out.createdHint = !!(await wait(() => sr.querySelector('.gl-created')));
  return out;
}
"""


# 改期限内联表单 + 临期决策行（P25/P1）：variant=''=常规（开表单/预检/保存契约），
# variant='due'=临期夹具（chip+快捷动作 POST 契约）。
_DL_JS = r"""
async (a) => {
  window.__GOAL_MODE = 'active';
  window.__GOAL_VARIANT = a.variant || '';
  window.__posts = [];
  const host = document.createElement('div');
  host.id = a.hostId;
  host.style.cssText = 'width:' + a.width + 'px;flex:0 0 auto;outline:1px dashed #cbd5e1;';
  document.body.appendChild(host);
  const el = document.createElement('cp-goal');
  el.context = { conversationId: 'telegram:acc:qa_' + a.hostId };
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
  const metaBtn = await wait(() => sr.querySelector('.gl-meta-btn'));
  if (!metaBtn) return { error: 'meta button not rendered' };
  const out = {};
  const dueChip = sr.querySelector('.gl-due-chip');
  // 剥 U+23F3 沙漏符：Windows GBK 控制台打印检查详情时编不出该字符
  out.dueChip = dueChip
    ? String(dueChip.textContent || '').replace(/\u23F3/g, '').trim() : '';
  out.dueBtns = Array.from(sr.querySelectorAll('.gl-due button'))
    .map((b) => b.getAttribute('data-act'));
  if (a.variant === 'due') {
    const b = sr.querySelector('[data-act="due_extend"]');
    if (!b) return Object.assign(out, { error: 'no extend button' });
    b.click();
    const posted = await wait(() => window.__posts.length > 0);
    out.extendPost = posted ? window.__posts[0] : null;
    return out;
  }
  // 常规态：开表单 → 读 min/chips → 等基准线 → 预检拦截 → 合法保存。
  // 每步动作前重查节点：基准线为 DOM 注入不重渲染（组件保证），但探针
  // 不依赖该实现细节——若未来改回重渲染，这里也不该假红。
  metaBtn.click();
  const q = (sel) => sr.querySelector(sel);
  const inp0 = await wait(() => q('[data-ref="dl_days"]'));
  if (!inp0) return Object.assign(out, { error: 'deadline form not opened' });
  out.min = inp0.getAttribute('min');
  out.val = inp0.value;
  out.chips = Array.from(sr.querySelectorAll('[data-act="deadline_chip"]'))
    .map((b) => b.getAttribute('data-days'));
  const bench = await wait(() => q('.gl-dl-bench'));
  out.bench = bench ? String(bench.textContent || '') : '';
  out.focusKept = sr.activeElement === inp0 || document.activeElement === el
    || !!q('[data-ref="dl_days"]');   // 基准线注入不得重建表单（节点仍是同一个）
  out.sameNode = q('[data-ref="dl_days"]') === inp0;
  // 低于下限 → 就地报错、零请求（预检与后端护栏同口径）
  const inp1 = q('[data-ref="dl_days"]');
  inp1.value = String(Math.max(0, parseInt(out.min, 10) - 1));
  inp1.dispatchEvent(new Event('input', { bubbles: true }));
  q('[data-act="deadline_save"]').click();
  await new Promise((r) => setTimeout(r, 120));
  const err = q('[data-ref="dl_err"]');
  out.errShown = !!(err && !err.hidden && String(err.textContent || '').length > 2);
  out.postsAfterInvalid = window.__posts.length;
  // 合法值 → POST 契约 + 表单收起
  const inp2 = q('[data-ref="dl_days"]');
  inp2.value = '5';
  inp2.dispatchEvent(new Event('input', { bubbles: true }));
  q('[data-act="deadline_save"]').click();
  const posted = await wait(() => window.__posts.length > 0);
  out.savePost = posted ? window.__posts[window.__posts.length - 1] : null;
  out.formClosed = !!(await wait(() => !q('[data-ref="dl_days"]')));
  return out;
}
"""


def run(headed: bool, shots: str) -> int:
    from playwright.sync_api import sync_playwright

    ck = Checker()
    shots_dir = None
    if shots:
        shots_dir = Path(shots)
        shots_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="cpgoal_probe_") as td:
        page_fp = build_fixture_page(Path(td))
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not headed)
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.goto(page_fp.as_uri())
            ck.check("cp-goal 组件已注册",
                     page.evaluate("!!customElements.get('cp-goal')"))

            # ── A. 窄宽布局（fixture A，三档宽度）────────────────────────
            for w in WIDTHS:
                hid = f"m_a_{w}"
                r = page.evaluate(_MOUNT_JS, {"hostId": hid, "width": w, "mode": "A"})
                if not isinstance(r, dict) or r.get("error"):
                    ck.check(f"[{w}px] 挂载渲染", False, str(r))
                    continue
                t, b = r.get("title") or {}, r.get("editBtn") or {}
                ck.check(f"[{w}px] 标题单行（h={t.get('h', 0):.0f} < {TITLE_MAX_H:.0f}）",
                         t and t["h"] < TITLE_MAX_H)
                ck.check(f"[{w}px] 补录按钮单行（h={b.get('h', 0):.0f}）",
                         b and b["h"] < BTN_MAX_H)
                ck.check(f"[{w}px] 无横向溢出", not r.get("overflow"),
                         str(r.get("overflow")))
                ck.check(f"[{w}px] 完成度行两条 bar", r.get("fillrow") and r.get("bars") == 2,
                         f"bars={r.get('bars')} pcts={r.get('pcts')}")
                ck.check(f"[{w}px] 缺口 chips ≤3 且可点",
                         0 < int(r.get("missChips") or 0) <= 3)
                prod = r.get("prod") or {}
                ck.check(f"[{w}px] 产品 pitch 常显 + 价格",
                         prod.get("pitchH", 0) > 0 and "$" in (prod.get("price") or ""),
                         f"pitchH={prod.get('pitchH')} price={prod.get('price')}")
                if shots_dir:
                    page.locator(f"#{hid}").screenshot(
                        path=str(shots_dir / f"goal_card_{w}px.png"))

            # ── B. 双零冷启动（fixture B）────────────────────────────────
            rb = page.evaluate(_MOUNT_JS, {"hostId": "m_b_236", "width": 236, "mode": "B"})
            if isinstance(rb, dict) and not rb.get("error"):
                ck.check("[双零] 不渲染完成度行", not rb.get("fillrow"))
                ck.check("[双零] 空态文案在", rb.get("emptyText"))
                ck.check("[双零] 缺口 chips 仍在（动作入口不丢）",
                         int(rb.get("missChips") or 0) > 0)
                if shots_dir:
                    page.locator("#m_b_236").screenshot(
                        path=str(shots_dir / "goal_card_zero_state.png"))
            else:
                ck.check("[双零] 挂载渲染", False, str(rb))

            # ── C. 交互链（216px 最窄档上做，布局最苛刻处交互也必须活）──
            hid = "m_i_216"
            ri = page.evaluate(_MOUNT_JS, {"hostId": hid, "width": 216, "mode": "A"})
            if isinstance(ri, dict) and not ri.get("error"):
                r1 = page.evaluate(_STEP_JS, {"hostId": hid, "op": "open_menu"})
                okk = isinstance(r1, dict) and not r1.get("error")
                ck.check("[交互] 点缺口 chip → 追问行出现", okk, str(r1 if not okk else ""))
                if okk:
                    ck.check("[交互] 追问行=建议问法+双按钮",
                             bool(r1.get("lead"))
                             and r1.get("btns") == ["slot_fill", "slot_ask"],
                             f"lead={bool(r1.get('lead'))} btns={r1.get('btns')}")
                    if shots_dir:
                        page.locator(f"#{hid}").screenshot(
                            path=str(shots_dir / "goal_card_ask_row.png"))
                    r2 = page.evaluate(_STEP_JS, {"hostId": hid, "op": "ask"})
                    d = (r2 or {}).get("detail") or {}
                    intent = str(d.get("intent") or "")
                    ck.check("[交互] 拟稿去问 → drive-draft 事件",
                             isinstance(r2, dict) and not r2.get("error"), str(r2))
                    ck.check("[交互] intent 已替换占位符且带槽位语境",
                             intent and "{" not in intent
                             and not intent.startswith("inbox.goal.")
                             and d.get("goalId") == "g_probe",
                             f"intent={intent[:48]}…")
                    # P22：事件须带 instruction（坐席指令）+ pushLevel，宿主据此 setDirective
                    instr = str(d.get("instruction") or "")
                    ck.check("[交互] P22 drive-draft 带 instruction+pushLevel",
                             bool(instr) and bool(d.get("pushLevel")),
                             f"instruction={instr[:40]}… push={d.get('pushLevel')}")
                    # P22.1：slot 拟稿带 label；英雄卡 API 存在
                    ck.check("[交互] P22.1 slot_ask 带 label",
                             bool(d.get("label")),
                             f"label={d.get('label')!r}")
                    has_hero_api = page.evaluate(
                        """(hid) => {
                          const host = document.getElementById(hid);
                          const el = host && host.querySelector('cp-goal');
                          return !!(el && typeof el.driveDraftFromToday === 'function');
                        }""",
                        hid,
                    )
                    ck.check("[交互] P22.1 driveDraftFromToday API", bool(has_hero_api))
                # 新挂载走「我来补录」（上一个的表单态不干扰）
                page.evaluate(_MOUNT_JS, {"hostId": "m_f_236", "width": 236, "mode": "A"})
                r3 = page.evaluate(_STEP_JS, {"hostId": "m_f_236", "op": "fill"})
                okk3 = isinstance(r3, dict) and not r3.get("error")
                ck.check("[交互] 我来补录 → 表单开+焦点对位",
                         okk3 and r3.get("focusKey") == r3.get("slot"),
                         str(r3))
                if okk3:
                    ck.check("[交互] 表单按轨分组 + placeholder 话术",
                             int(r3.get("groups") or 0) >= 2
                             and int(r3.get("placeholders") or 0) >= 8,
                             f"groups={r3.get('groups')} ph={r3.get('placeholders')}")
                    if shots_dir:
                        page.locator("#m_f_236").screenshot(
                            path=str(shots_dir / "goal_card_form_grouped.png"))
                page.evaluate(_MOUNT_JS, {"hostId": "m_e_236", "width": 236, "mode": "A"})
                r4 = page.evaluate(_STEP_JS, {"hostId": "m_e_236", "op": "edit_filled"})
                ck.check("[交互] 已填 chip → 编辑聚焦对位",
                         isinstance(r4, dict) and not r4.get("error")
                         and r4.get("focusKey") == r4.get("slot"), str(r4))
            else:
                ck.check("[交互] 挂载渲染", False, str(ri))

            # ── D. 建目标两步向导（236px；P24）────────────────────────────
            rw = page.evaluate(_WIZ_JS, {"hostId": "m_w_236", "width": 236})
            if isinstance(rw, dict) and not rw.get("error"):
                ck.check("[向导] 第一步分组场景卡+图标",
                         int(rw.get("groups") or 0) >= 2
                         and int(rw.get("cards") or 0) >= 7
                         and int(rw.get("icons") or 0) >= 7,
                         f"groups={rw.get('groups')} cards={rw.get('cards')}"
                         f" icons={rw.get('icons')}")
                ck.check("[向导] 自定义目标高亮卡在", rw.get("customCard"))
                ck.check("[向导] 弹层逃离窄栏（宽>300）",
                         float(rw.get("modalW") or 0) > 300, f"w={rw.get('modalW')}")
                ck.check("[向导] 弹层类别色带+节奏预览带力度",
                         rw.get("band") and int(rw.get("arcRows") or 0) == 4
                         and int(rw.get("arcPills") or 0) >= 1,
                         f"rows={rw.get('arcRows')} pills={rw.get('arcPills')}")
                ck.check("[向导] 解锁项下拉（目录项+自定义）",
                         int(rw.get("unlockOpts") or 0) >= 3
                         and rw.get("unlockParamKey") == "item_id",
                         f"opts={rw.get('unlockOpts')}")
                ck.check("[向导] 称呼自动跟随所选项",
                         rw.get("labelInit") == "命理详批"
                         and rw.get("labelFollow") == rw.get("opt2Label"),
                         f"init={rw.get('labelInit')} follow={rw.get('labelFollow')}")
                ck.check("[向导] 自定义解锁项切换（item_id 归属互斥）",
                         rw.get("customShown") and rw.get("customOwnsKey")
                         and rw.get("customHidden"))
                ck.check("[向导] 参与度三卡+auto 档诚实气泡",
                         int(rw.get("autoCards") or 0) == 3
                         and rw.get("autoDefault") == "suggest"
                         and rw.get("noteHiddenBefore")
                         and rw.get("noteShownAfterAuto"),
                         f"cards={rw.get('autoCards')} def={rw.get('autoDefault')}")
                ck.check("[向导] 返回=弹层关、第一步在", rw.get("backToStep1"))
                ck.check("[向导] 自定义面板（textarea+示例填入）",
                         rw.get("customModal") and rw.get("customTa")
                         and int(rw.get("exChips") or 0) == 3 and rw.get("exFilled"),
                         f"chips={rw.get('exChips')}")
                post = rw.get("post") or {}
                pp = post.get("params") or {}
                ck.check("[向导] 创建 POST 契约（item_id/称呼/参与度/期限）",
                         post.get("template") == "conversion_unlock"
                         and pp.get("item_id") == "bazi_reading"
                         and pp.get("item_label") == "命理详批"
                         and post.get("autonomy") == "suggest"
                         and float(post.get("deadline_days") or 0) == 14.0,
                         json.dumps(post, ensure_ascii=False)[:160])
                ck.check("[向导] 创建后弹层关+「接下来」提示在",
                         rw.get("modalClosedAfterCreate") and rw.get("createdHint"))
            else:
                ck.check("[向导] 挂载渲染", False, str(rw))

            # ── E. 改期限 + 临期决策行（P25/P1；236px）──────────────────────
            rd = page.evaluate(_DL_JS, {"hostId": "m_dl_236", "width": 236,
                                        "variant": ""})
            if isinstance(rd, dict) and not rd.get("error"):
                ck.check("[期限] 常规态（第4/10天）不出临期行",
                         not rd.get("dueChip"), str(rd.get("dueChip")))
                ck.check("[期限] 点「第X/Y天」→ 表单开（min=day_index, 值=当前总天数）",
                         rd.get("min") == "4" and rd.get("val") == "10",
                         f"min={rd.get('min')} val={rd.get('val')}")
                chips = rd.get("chips") or []
                ck.check("[期限] chips=今天收口(4)+7/14，低于下限的 3 隐藏",
                         chips[:1] == ["4"] and "7" in chips
                         and "14" in chips and "3" not in chips, str(chips))
                ck.check("[期限] 近30天基准线（n=6→67%·6.5天）",
                         "67" in str(rd.get("bench")) and "6.5" in str(rd.get("bench")),
                         str(rd.get("bench")))
                ck.check("[期限] 基准线注入不重建表单（输入焦点/节点保持）",
                         rd.get("sameNode"))
                ck.check("[期限] 低于下限预检拦截（就地报错+零请求）",
                         rd.get("errShown")
                         and int(rd.get("postsAfterInvalid") or 0) == 0)
                sp = rd.get("savePost") or {}
                ck.check("[期限] 保存 POST 契约 deadline_days=5 + 表单收起",
                         float(sp.get("deadline_days") or 0) == 5.0
                         and rd.get("formClosed"),
                         json.dumps(sp, ensure_ascii=False)[:80])
                if shots_dir:
                    page.locator("#m_dl_236").screenshot(
                        path=str(shots_dir / "goal_card_deadline_form.png"))
            else:
                ck.check("[期限] 挂载渲染", False, str(rd))
            rdue = page.evaluate(_DL_JS, {"hostId": "m_due_236", "width": 236,
                                          "variant": "due"})
            if isinstance(rdue, dict) and not rdue.get("error"):
                ck.check("[临期] 第9/10天 → 琥珀 chip「明天到期」+ 收口/延长双动作",
                         "明天到期" in str(rdue.get("dueChip") or "")
                         and rdue.get("dueBtns") == ["due_wrap", "due_extend"],
                         f"chip={rdue.get('dueChip')} btns={rdue.get('dueBtns')}")
                ep = rdue.get("extendPost") or {}
                ck.check("[临期] 点「延长 7 天」→ POST deadline_days=17",
                         float(ep.get("deadline_days") or 0) == 17.0,
                         json.dumps(ep, ensure_ascii=False)[:80])
                if shots_dir:
                    page.locator("#m_due_236").screenshot(
                        path=str(shots_dir / "goal_card_due_row.png"))
            else:
                ck.check("[临期] 挂载渲染", False, str(rdue))

            browser.close()
    return ck.summary()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--shots", default="")
    args = ap.parse_args()
    try:
        import playwright  # noqa: F401
    except Exception:
        print("[SKIP] playwright 未安装，跳过（exit 0）")
        return 0
    return run(args.headed, args.shots)


if __name__ == "__main__":
    sys.exit(main())
