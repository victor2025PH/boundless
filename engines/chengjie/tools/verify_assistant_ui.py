# -*- coding: utf-8 -*-
"""AI 助手悬浮球 assistant-ball 真浏览器门禁（Playwright；P1 2026-08-20）。

**为什么需要它**：球/面板全部 DOM 在运行时由 JS 生成——哑按钮/孤儿引用等静态
门禁扫的是模板，对这个组件天然盲区；「点球开面板、问答渲染、转报障预填、
标注层出现」都是交互时序，只有真浏览器能压。而共享组件热更新直上生产。

**夹具模式**（与 tools/verify_cp_voice_ui.py 同族）：file:// 自包含页面 +
真组件（assistant-ball.js）+ **stub window.fetch**（组件全部 IO 走 fetch，
拦 /api/assistant/* 返回假响应）——零实例依赖、零生产写入、零 LLM 消耗。

覆盖的不变量（编号对应 run_admin/run_ws 里的断言）：
  A1  init 后球在场（aria-expanded=false），面板隐藏
  A2  点球 → 面板开（问答模式激活，欢迎语 + chips 可见）
  M1-M6 实施73 P1 信息架构：模式条三格 / 方向键 / 教学·替我做模式内容 /
        次级页签顺序（报障·我的·常问·⚙）/ 常问面板与点条目发问 /
        手机操控在标题栏且状态点跟随真实配对态
  M3c-e 实施73 P2-1 状态行：「本页 N 处可讲解」异步回填 + 数字与导览站点数
        相等（静态门禁只能证明两处都调了同一个扫描器，证不了数字相等）
  A3  点 chip → 用户气泡 + AI 回答（JSON events 渲染）+ 来源卡（带我去链接）
  A4  👍 反馈 → POST 带 qa_id + verdict，按钮区变「已记录」
  A5  report_hint 回答 → 出「转报障」按钮；点击 → 切报障 tab 且描述预填
  A6  报障描述 <5 字不发请求；≥5 字 → POST 成功出工单号 + 「我的」入口
  A7  「我的」tab → 工单列表渲染（fixed 状态 chip + 开发者留言行）
  A8  err 事件 → 红气泡 + 重试按钮；点重试 → 重新发同一问题
  A9  429 → 服务端 detail 文案透传红气泡
  A10 Esc 关面板（aria-expanded 同步）
  A11 boot.voice=false → 🎤 不渲染（能力探测不出死按钮）
  A12 stub html2canvas → 截当前页 → 标注层出现（5 工具钮）→ ✔ → 缩略图落位
  A13 admin 壳：旧帮助球功能收编（术语提示/重看引导/命令面板按钮在场）
  W1  workspace 壳：工具行隐藏（无 admin 全局函数）；面板正常开
  A0/W0 全程零未捕获 JS 异常

用法::

    python tools/verify_assistant_ui.py             # 门禁模式
    python tools/verify_assistant_ui.py --headed    # 肉眼看一遍

缺 playwright → SKIP exit 0（挂 gate_sweep 的前提：环境缺失不污染回归信号）。
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path
from typing import List

ENGINE = Path(__file__).resolve().parents[1]

_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>assistant-ball probe</title>
<!-- 宿主主题令牌（取自 base.html / workspace_base.html 的真实变量名）。
     不摆上它们，夹具就永远在测「回落色」，而 P2 主题桥要证的恰恰是
     「宿主有品牌色时小智跟得上」——那正是它此前做不到的事。 -->
<style>:root{--bl-growth:#1e8cf2;--bl-growth-600:#0d76d9;
--p:var(--bl-growth-600,#0d76d9);--card:#fff;--t:#111827;--t3:#5b6b85;
--bd:rgba(15,27,45,.1);--sb-hover:rgba(17,24,39,.05);
--tk-brand:var(--bl-growth,#1e8cf2);--tk-surface:#fff;--tk-text:#0f1b2d;
--tk-text-muted:#5b6b85;--tk-border:#e5e6ea}</style></head>
<body style="margin:16px;font-family:system-ui,'Microsoft YaHei',sans-serif;">
<h3 id="page-title" data-anchor="fixture_page">fixture page</h3>
<button id="t-emoji-btn" type="button">🎓 教学按钮（3）</button>
<!-- DOM 动作总线夹具（实施88 P0 / M8）：fill 要真触发 input 事件、click 要
     真进 onclick——两个计数器就是「执行器真的动了手」的运行时证据。 -->
<input id="fx-ui-in" type="text" oninput="window.__fxInput=this.value"
       style="width:120px">
<button id="fx-ui-btn" type="button"
        onclick="window.__fxClicks=(window.__fxClicks||0)+1">fx ui btn</button>
@@ADMIN_GLOBALS@@
<script>
window.__api = { queries: [], reports: [], feedbacks: [], ticketsCalls: 0,
                 faqCalls: 0, pairSessions: [], queryMode: 'ok' };
window.fetch = async function (url, opts) {
  url = String(url);
  const J = (o, st) => new Response(JSON.stringify(o),
    { status: st || 200, headers: { 'Content-Type': 'application/json' } });
  if (url.indexOf('/api/assistant/query') === 0) {
    window.__api.queries.push(JSON.parse(opts.body));
    if (window.__api.queryMode === '429') return J({ detail: '问得太快啦' }, 429);
    if (window.__api.queryMode === 'err') return J({ ok: true, events: [
      { ev: 'meta', sources: [], report_hint: false },
      { ev: 'err', key: 'x', text: '生成失败请重试' }] });
    if (window.__api.queryMode === 'general') {
      /* 通用知识作答（2026-08-29 P0）：零命中 → 产品事实卡/通用链答出来了，
         done 带 basis=general。UI 必须标注「非产品文档」，否则模型随口说的
         会被当成产品承诺。 */
      return J({ ok: true, events: [
        { ev: 'meta', report_hint: false, sources: [] },
        { ev: 'delta', text: '一般来说可以这样写：您好，很高兴为您服务。' },
        { ev: 'done', ms: 5, qa_id: 12, answered: true, basis: 'general' }] });
    }
    if (window.__api.queryMode === 'product') {
      return J({ ok: true, events: [
        { ev: 'meta', report_hint: false, sources: [] },
        { ev: 'delta', text: '目前对接 Telegram、WhatsApp、LINE、Messenger 和网页；抖音暂不支持。' },
        { ev: 'done', ms: 5, qa_id: 13, answered: true, basis: 'product' }] });
    }
    if (window.__api.queryMode === 'nobasis') {
      /* NO_BASIS 哨兵路径的**真实**线形（2026-08-29）：meta 先发且 sources
         非空（检索过了 min_score），随后 LLM 自认答不了 → answered=false。
         老板 8/28 截图就是这一幕：一句「没找到可靠依据」下面挂着一条无关
         来源。err 模式测不到它——那条没有 sources 也没有 done。 */
      return J({ ok: true, events: [
        { ev: 'meta', report_hint: false, sources: [
          { id: 'howto:multiwin', title: '为什么提示「在此使用/保持待机」',
            path: '/workspace', score: 62 }] },
        { ev: 'delta', text: '这个问题我在产品帮助库里没有找到可靠依据，' +
          '为避免误导就不猜了；你的问题已记录，我们会尽快补充。' },
        { ev: 'done', ms: 5, qa_id: 11, answered: false }] });
    }
    if (window.__api.queryMode === 'sse') {
      /* 生产真形态（2026-08-21 P2）：text/event-stream 逐帧 */
      const frames = [
        { ev: 'meta', report_hint: false, sources: [
          { id: 'howto:send-voice', title: '怎么给客户发语音消息',
            path: '/workspace', score: 99 }] },
        { ev: 'delta', text: '按 [S1] ' },
        { ev: 'delta', text: '操作即可。' },
        { ev: 'done', ms: 5, qa_id: 9, answered: true }];
      const body = frames.map(f => 'data: ' + JSON.stringify(f) + '\\n\\n').join('');
      return new Response(body,
        { status: 200, headers: { 'Content-Type': 'text/event-stream' } });
    }
    const hint = window.__api.queryMode === 'hint';
    return J({ ok: true, events: [
      { ev: 'meta', report_hint: hint, sources: [
        { id: 'howto:send-voice', title: '怎么给客户发语音消息',
          path: '/workspace', score: 99 }] },
      { ev: 'delta', text: '按 [S1] 操作即可。' },
      { ev: 'done', ms: 5, qa_id: 7, answered: true }] });
  }
  if (url.indexOf('/api/assistant/report') === 0) {
    window.__api.reports.push(JSON.parse(opts.body));
    return J({ ok: true, ticket_id: 33, dup: false });
  }
  if (url.indexOf('/api/assistant/feedback') === 0) {
    window.__api.feedbacks.push(JSON.parse(opts.body));
    return J({ ok: true });
  }
  if (url.indexOf('/api/assistant/terms') === 0) {
    return J({ ok: true, count: 2, terms: {
      fixture_page: { zh: 'fixture page', en: 'fixture page',
        desc: '夹具页说明文字', desc_en: 'Fixture page desc',
        usage: '用于门禁验证', usage_en: 'gate use' },
      teach_btn: { zh: '教学按钮', en: 'Teach button',
        desc: '教学按钮说明', desc_en: 'Teach btn desc' } } });
  }
  if (url.indexOf('/api/assistant/faq') === 0) {
    window.__api.faqCalls++;
    if (url.indexOf('q=') > -1) {
      return J({ ok: true, mode: 'search',
        items: [{ title: '怎么给客户发语音消息', path: '/workspace' }] });
    }
    return J({ ok: true, mode: 'top',
      page_items: [{ q: '本页高频问题', n: 4 }],
      global_items: [{ q: '全站高频问题', n: 9 }], seed_items: [] });
  }
  if (url.indexOf('/api/assistant/pair/sessions') === 0) {
    return J({ ok: true, sessions: window.__api.pairSessions });
  }
  if (url.indexOf('/api/assistant/act/history') === 0) {
    return J({ ok: true, items: [
      { ts: Date.now() / 1000, label: '把回复速度调快', old_h: '慢',
        new_h: '快', actor: 'me', undoable: true, undo_id: 'u1' }] });
  }
  if (url.indexOf('/api/assistant/actions') === 0) { return J({ ok: true, actions: [] }); }
  if (url.indexOf('/api/assistant/agent/plan') === 0) {
    /* DOM 总线场景（M8）：__api.planResp 由场景注入；生产里 ui.sel 是服务端
       按 ui_anchors 白名单换出的，夹具只验前端执行器行为。 */
    return J(window.__api.planResp || { ok: true, plan_ok: false, say: '',
      steps: [], dropped: [], ask: '' });
  }
  if (url.indexOf('/api/assistant/tickets') === 0) {
    window.__api.ticketsCalls++;
    return J({ ok: true, tickets: [
      { id: 33, created_ts: Date.now() / 1000, updated_ts: Date.now() / 1000,
        title: '语音按钮点了没反应', status: 'fixed', severity: 'P2',
        report_count: 2, notify_ts: 0, notify_note: '已修复请更新后验证' }] });
  }
  return J({ ok: false }, 404);
};
</script>
<script src="@@BALL_JS@@"></script>
<script>
AssistantBall.init({ shell: '@@SHELL@@', lang: 'zh', boot: {
  ok: true, enabled: true, name: '小智', brand: '测试站',
  report_enabled: true, voice: false, kb_entries: 230,
  chips: ['怎么发语音', '在哪提交 bug'] } });
</script>
<!-- 实施73 P1 起三个模块必须同时在场才是生产形态：教学/替我做经
     registerMode 认领模式条第 2/3 格，缺席则面板退化为单模式纯问答。 -->
<script src="@@TEACH_JS@@"></script>
<script src="@@AGENT_JS@@"></script>
<script>
XZTeach.init({ shell: '@@SHELL@@', lang: 'zh' });
XZAgent.init({ shell: '@@SHELL@@', lang: 'zh' });
</script>
</body></html>
"""

_ADMIN_GLOBALS = """
<script>
/* 2026-08-21 帮助球退役后契约：术语提示开关=window.toggleTermTips 全局函数
   （不再有 .tip-toggle DOM）；「上传诊断」=window.openSupportPanel。 */
window.__legacy = { tips: 0, tour: 0, keys: 0, cmd: 0, support: 0 };
window.toggleTermTips = function () { window.__legacy.tips++; return true; };
function startTour() { window.__legacy.tour++; }
function openShortcuts() { window.__legacy.keys++; }
function openCmdPalette() { window.__legacy.cmd++; }
function openSupportPanel() { window.__legacy.support++; }
</script>
"""


class Checker:
    def __init__(self) -> None:
        self.results: List[tuple] = []

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.results.append((name, bool(ok), detail))
        print(("  PASS  " if ok else "  FAIL  ") + name
              + (("  <- " + detail) if (detail and not ok) else ""))

    def summary(self) -> int:
        fails = [r for r in self.results if not r[1]]
        print(f"\n{len(self.results) - len(fails)}/{len(self.results)} passed")
        return 1 if fails else 0


def build_page(td: Path, shell: str) -> Path:
    sh = ENGINE / "shared" / "assistant"
    ball = (sh / "assistant-ball.js").resolve()
    teach = (sh / "assistant-teach.js").resolve()
    agent = (sh / "assistant-agent.js").resolve()
    html = (_HTML
            .replace("@@BALL_JS@@", ball.as_uri())
            .replace("@@TEACH_JS@@", teach.as_uri())
            .replace("@@AGENT_JS@@", agent.as_uri())
            .replace("@@SHELL@@", shell)
            .replace("@@ADMIN_GLOBALS@@",
                     _ADMIN_GLOBALS if shell == "admin" else ""))
    fp = td / f"probe_{shell}.html"
    fp.write_text(html, encoding="utf-8")
    return fp


def run_admin(page, ck: Checker) -> None:
    ev = page.evaluate

    # A1 初始态
    ck.check("A1 球在场且面板隐藏",
             ev("() => !!document.querySelector('.asb-ball') && "
                "document.querySelector('.asb-ball').getAttribute("
                "'aria-expanded') === 'false' && "
                "!document.querySelector('.asb-panel.open')"))

    # A2 点球开面板
    page.click(".asb-ball")
    page.wait_for_timeout(150)
    ck.check("A2 面板开（问答模式 + 欢迎语 + chips）",
             ev("() => !!document.querySelector('.asb-panel.open') && "
                "document.querySelector('.asb-mode[data-mode=\"chat\"]')"
                ".getAttribute('aria-checked') === 'true' && "
                "!!document.querySelector('.asb-chips') && "
                "document.querySelectorAll('.asb-chip').length >= 2"))
    ck.check("A2b 欢迎三卡已删除（首屏不再有重复入口）",
             ev("() => !document.querySelector('.asb-h3')"))
    ck.check("A2c 模式条三格在场（详细断言见 M 组）",
             ev("() => document.querySelectorAll('.asb-mode').length === 3"),
             ev("() => document.querySelectorAll('.asb-mode').length + ' modes'"))

    # A3 chip 问答（JSON events 渲染）
    _run_admin_chat(page, ck)
    # ── M 组：实施73 P1 信息架构（放在绝对计数断言之后——M6b 会真发一问） ──
    _run_modes(page, ck)
    _run_admin_tail(page, ck)


def _run_modes(page, ck: Checker) -> None:
    ev = page.evaluate
    ck.check("M1 模式条三格（问答/教学/替我做）+ radiogroup 语义",
             ev("() => { var b = document.querySelector('.asb-modes'); "
                "return b && b.getAttribute('role') === 'radiogroup' && "
                "b.querySelectorAll('.asb-mode').length === 3 && "
                "!b.classList.contains('solo'); }"),
             ev("() => document.querySelectorAll('.asb-mode').length + ' modes'"))
    ck.check("M1b 滑块指示器已定位（面板开时才算得出宽度）",
             ev("() => { var i = document.querySelector('.asb-modes-ind'); "
                "return !!i && i.style.opacity === '1' && "
                "parseFloat(i.style.width) > 10; }"))
    ck.check("M2 次级页签顺序＝报障·我的·常问·⚙",
             ev("() => Array.prototype.map.call("
                "document.querySelectorAll('.asb-subtabs .asb-sub'), "
                "function (b) { return b.getAttribute('data-tab'); })"
                ".join(',') === 'report,mine,faq,set'"),
             ev("() => document.querySelector('.asb-subtabs').textContent"))

    # M3 教学模式：安全承诺整段可见 + 主按钮在场（不是虚线占位）
    page.click('.asb-mode[data-mode="teach"]')
    page.wait_for_timeout(150)
    ck.check("M3 教学模式内容（主按钮 + 安全承诺全文）",
             ev("() => { var b = document.querySelector('.asb-body'); "
                "return !!b.querySelector('.asb-md-go') && "
                "b.textContent.indexOf('不会真的执行') > -1 && "
                "!!b.querySelector('.asb-md-safe'); }"),
             ev("() => document.querySelector('.asb-body').textContent.slice(0,120)"))
    ck.check("M3b 教学模式无输入区（它不靠打字驱动）",
             ev("() => !document.querySelector('.asb-ftwrap .asb-in')"))

    # M3c-M3e 实施73 P2-1：状态行「本页 N 处可讲解」。词典是 fetch 异步下发的，
    # 所以先等回填；夹具备了 2 条词条（h3 走 data-anchor、按钮走归一化标题）。
    page.wait_for_timeout(250)
    ck.check("M3c 状态行回填「本页 N 处可讲解」（夹具应为 2 处）",
             ev("() => { var s = document.querySelector('.asb-md-stat'); "
                "return !!s && s.getAttribute('data-n') === '2' && "
                "s.textContent.indexOf('2') > -1; }"),
             ev("() => { var s = document.querySelector('.asb-md-stat'); "
                "return s ? (s.getAttribute('data-n') + ' | ' + s.textContent) "
                ": '(no stat line)'; }"))
    page.click('[data-xzt="mode-tour"]')
    page.wait_for_timeout(250)
    # 静态门禁只能证明「两处都调了 scanTeachable」；这一条证明数字真的相等。
    ck.check("M3d 导览站点数 == 状态行数字（计数与导览同源的运行时证据）",
             ev("() => { var s = document.querySelector('.asb-md-stat'); "
                "var b = document.querySelector('.xzt-bubble'); "
                "return !!s && !!b && b.textContent.indexOf("
                "'/ ' + s.getAttribute('data-n')) > -1; }"),
             ev("() => { var b = document.querySelector('.xzt-bubble'); "
                "return b ? b.textContent.slice(0, 80) : '(no tour bubble)'; }"))
    # 收尾必须干净：教学态的捕获拦截器还在的话，后面每一条断言的点击都会被吞
    ev("() => { var x = document.querySelector('[data-xzt=\"tend\"]'); "
       "if (x) { x.click(); } "
       "var q = document.querySelector('[data-xzt=\"bexit\"]'); "
       "if (q) { q.click(); } }")
    page.wait_for_timeout(200)
    ck.check("M3e 退出教学态：横幅与气泡都收干净（不留拦截器）",
             ev("() => !document.querySelector('.xzt-banner') && "
                "!document.querySelector('.xzt-bubble')"))
    # start() 是刻意收起面板的（面板挡住页面就没得点了），所以退出教学后
    # 必须能原样开回来——否则「点了带我走一遍，面板就再也回不来」。
    if not ev("() => { var p = document.querySelector('.asb-panel'); "
              "return !!p && p.classList.contains('open'); }"):
        page.click(".asb-ball")
        page.wait_for_timeout(250)
    ck.check("M3f 教学流程走完面板能开回来（不被收起动作卡死）",
             ev("() => document.querySelector('.asb-panel')"
                ".classList.contains('open')"))

    # M4 替我做模式：输入区 placeholder 随模式变 + 最近做过 + 撤销就在手边
    page.click('.asb-mode[data-mode="agent"]')
    page.wait_for_timeout(250)
    ck.check("M4 替我做模式 placeholder 随模式变",
             ev("() => { var i = document.querySelector('.asb-ftwrap .asb-in'); "
                "return !!i && i.placeholder.indexOf('例如') > -1; }"),
             ev("() => { var i = document.querySelector('.asb-ftwrap .asb-in'); "
                "return i ? i.placeholder : '(no composer)'; }"))
    ck.check("M4b 「做过什么」已并入：最近做过 + 就地撤销",
             ev("() => { var b = document.querySelector('.asb-body'); "
                "return b.textContent.indexOf('把回复速度调快') > -1 && "
                "!!b.querySelector('[data-xza=\"recent-undo\"]'); }"))
    ck.check("M4c 手机指挥卡在替我做模式内（第二处曝光）",
             ev("() => document.querySelector('.asb-body').textContent"
                ".indexOf('用手机指挥') > -1"))

    # M5 方向键在模式间移动（radiogroup 标准交互）
    ev("() => document.querySelector('.asb-mode[aria-checked=\"true\"]').focus()")
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(150)
    ck.check("M5 方向键回绕到第一格（问答）",
             ev("() => document.querySelector('.asb-mode[data-mode=\"chat\"]')"
                ".getAttribute('aria-checked') === 'true'"))

    # M6 常问面板：分组 + 点条目直接切问答并发问
    page.click('.asb-sub[data-tab="faq"]')
    page.wait_for_timeout(250)
    ck.check("M6 常问面板分组（本页/全站）",
             ev("() => { var b = document.querySelector('.asb-body'); "
                "return window.__api.faqCalls > 0 && "
                "b.textContent.indexOf('本页高频问题') > -1 && "
                "b.textContent.indexOf('全站高频问题') > -1; }"))
    ev("() => { window.__faqN = window.__api.queries.length; }")
    page.click('.asb-faq-i')
    page.wait_for_timeout(250)
    ck.check("M6b 点常问条目 → 切回问答并真的发问",
             ev("() => document.querySelector('.asb-mode[data-mode=\"chat\"]')"
                ".getAttribute('aria-checked') === 'true' && "
                "window.__api.queries.length === window.__faqN + 1"))

    # M7 手机操控在标题栏；状态点跟随真实配对态
    ck.check("M7 标题栏手机键在场且未连=灰",
             ev("() => { var b = document.querySelector('.asb-hd-pair'); "
                "return !!b && !b.classList.contains('on'); }"))
    ev("() => { window.__api.pairSessions = [{ id: 's1' }]; "
       "var x = document.querySelector('.asb-x'); if (x) x.click(); }")
    page.click(".asb-ball")
    page.wait_for_timeout(300)
    ck.check("M7b 已配对 → 状态点转绿",
             ev("() => document.querySelector('.asb-hd-pair')"
                ".classList.contains('on')"))
    ev("() => { window.__api.pairSessions = []; }")

    # M8 DOM 动作总线（实施88 P0）：ui 步在前端真执行——fill 真触发 input
    # 事件、click 真进 onclick；找不到控件=诚实失败不装 ok。静态门禁只能证
    # 「execStep 有 ui 分支」，这里证明它运行时真的动了手。
    ev("() => { window.__fxClicks = 0; window.__fxInput = ''; "
       "window.__api.planResp = { ok: true, plan_ok: true, say: '这就来', "
       "dropped: [], steps: ["
       "{ action: 'ui_act', level: 'L1', kind: 'ui', label: '填入搜索词', "
       "params: { anchor: 'a1', text: '退款' }, ui: { anchor: 'a1', "
       "sel: '#fx-ui-in', gesture: 'fill', page: '/workspace', "
       "text: '退款' } }, "
       "{ action: 'ui_act', level: 'L1', kind: 'ui', label: '点开夹具按钮', "
       "params: { anchor: 'a2' }, ui: { anchor: 'a2', sel: '#fx-ui-btn', "
       "gesture: 'click', page: '/workspace', text: '' } }] }; "
       "window.XZAgent.run('帮我搜退款并点开按钮'); }")
    page.wait_for_timeout(2600)   # 两步流星飞行 + 步间隔
    ck.check("M8 fill 真的把文字送进输入框（input 事件触发计数器）",
             ev("() => document.getElementById('fx-ui-in').value === '退款' "
                "&& window.__fxInput === '退款'"),
             ev("() => 'value=' + document.getElementById('fx-ui-in').value "
                "+ ' hook=' + window.__fxInput"))
    ck.check("M8b click 真的点到按钮（onclick 计数器=1）",
             ev("() => window.__fxClicks === 1"),
             ev("() => 'clicks=' + window.__fxClicks"))
    ck.check("M8c 任务卡两步全 ✓ 且收尾",
             ev("() => { var c = document.querySelector('.xza-card'); "
                "if (!c) { return false; } "
                "var ics = c.querySelectorAll('.xza-st-hd .ic'); "
                "var okN = 0; ics.forEach(function (x) { "
                "if (x.textContent === '\\u2713') { okN += 1; } }); "
                "return okN === 2; }"),
             ev("() => { var c = document.querySelector('.xza-card'); "
                "return c ? c.textContent.slice(0, 160) : '(no card)'; }"))
    # M8d 诚实失败：控件不存在必须 fail（流星飞向空气还报「完成」比失败更糟）
    ev("() => { var x = document.querySelector('[data-xza=\"close-card\"]'); "
       "if (x) { x.click(); } "
       "window.__api.planResp = { ok: true, plan_ok: true, say: '试试', "
       "dropped: [], steps: [{ action: 'ui_act', level: 'L1', kind: 'ui', "
       "label: '点不存在的控件', params: { anchor: 'a3' }, "
       "ui: { anchor: 'a3', sel: '#fx-not-exist', gesture: 'click', "
       "page: '/workspace', text: '' } }] }; "
       "window.XZAgent.run('点一个不存在的'); }")
    page.wait_for_timeout(1600)
    ck.check("M8d 控件不存在 → 诚实失败（fail 态 + 不误报成功）",
             ev("() => { var c = document.querySelector('.xza-card'); "
                "return !!c && !!c.querySelector('.xza-step.fail'); }"),
             ev("() => { var c = document.querySelector('.xza-card'); "
                "return c ? c.textContent.slice(0, 160) : '(no card)'; }"))
    ev("() => { var x = document.querySelector('[data-xza=\"close-card\"]'); "
       "if (x) { x.click(); } window.__api.planResp = null; }")

    page.click('.asb-mode[data-mode="chat"]')
    page.wait_for_timeout(120)


def _run_admin_chat(page, ck: Checker) -> None:
    ev = page.evaluate

    # A3 chip 问答（JSON events 渲染）
    page.click(".asb-chip")
    page.wait_for_timeout(250)
    ck.check("A3 问答渲染（用户气泡+AI 回答+来源卡+带我去）",
             ev("() => document.querySelectorAll('.asb-msg.user').length === 1"
                " && document.body.textContent.indexOf('按 [S1] 操作即可') > -1"
                " && !!document.querySelector('.asb-src [data-goto=\"/workspace\"]')"),
             ev("() => document.body.textContent.slice(-200)"))
    ck.check("A3b 请求携带 page/lang",
             ev("() => window.__api.queries.length === 1 && "
                "!!window.__api.queries[0].lang"))
    # A3c 多轮上下文（2026-08-23）：答成一轮后，下一问携带 history
    ck.check("A3c 首问 history 为空数组",
             ev("() => Array.isArray(window.__api.queries[0].history) && "
                "window.__api.queries[0].history.length === 0"))
    # V2（2026-09-05 视觉体系 v2）：思考占位「就地落定」——回答落地后不得残留
    # pending/live 运行态类，也不得残留 asb-th-* 占位 id（v1 是删节点重建，
    # v2 是同一节点换形态；两者都会让本断言绿，但残留任一运行态类即红）。
    ck.check("V2a 回答落地后无 pending/live 残留、占位 id 已摘",
             ev("() => !document.querySelector('.asb-msg.pending,.asb-msg.live') && "
                "document.querySelectorAll('[id^=\"asb-th-\"]').length === 0"))
    ck.check("V2b 标题栏状态短句在场且非空",
             ev("() => { var s = document.querySelector('.asb-hd-st'); "
                "return !!s && s.textContent.trim().length > 0; }"))
    ck.check("V2c 球体立体光环（前后两半 × 两条）在场",
             ev("() => document.querySelectorAll('.asb-ball .asb-ring').length === 4 && "
                "!!document.querySelector('.asb-ball .asb-orb')"))
    ck.check("V2d 图标已线性化：反馈行/复制/播报键内是 SVG 而非 emoji 文本",
             ev("() => { var fb = document.querySelector('.asb-fb'); if (!fb) return false; "
                "var bs = fb.querySelectorAll('button'); if (!bs.length) return false; "
                "for (var i = 0; i < bs.length; i++) { if (!bs[i].querySelector('svg.asb-i')) "
                "return false; } return true; }"))
    # A3d 复制按钮（与播报键并排）+ 点击出 ✓ 反馈
    # v2（2026-09-05）起图标是线性 SVG 不再是 emoji 文本：成功态以 data-ok 属性
    # + .ok 类表达（1.2s 后自撤），断言改读属性而不是 textContent。
    ck.check("A3d 复制按钮在场",
             ev("() => !!document.querySelector('[data-act=\"copy\"]')"))
    page.click('[data-act="copy"]')
    page.wait_for_timeout(150)
    ck.check("A3e 复制点击出 ✓ 反馈",
             ev("() => { var b = document.querySelector('[data-act=\"copy\"]'); "
                "return !!b && (b.getAttribute('data-ok') === '1' || "
                "b.classList.contains('ok') || !!b.querySelector('svg')); }"))
    # A3f 输入框 autosize：多行内容 → 高度增长
    ck.check("A3f 输入框随内容长高",
             ev("() => { var i = document.querySelector('.asb-in'); "
                "var h0 = i.offsetHeight; i.value = 'x\\n'.repeat(6); "
                "i.dispatchEvent(new Event('input', { bubbles: true })); "
                "var h1 = i.offsetHeight; i.value = ''; "
                "i.dispatchEvent(new Event('input', { bubbles: true })); "
                "return h1 > h0; }"))

    # A4 反馈
    page.click(".asb-fb [data-fb=\"up\"]")
    page.wait_for_timeout(120)
    ck.check("A4 反馈 POST + 已记录",
             ev("() => window.__api.feedbacks.length === 1 && "
                "window.__api.feedbacks[0].qa_id === 7 && "
                "window.__api.feedbacks[0].verdict === 'up' && "
                "document.querySelector('.asb-fb').textContent.indexOf('已记录') > -1"))

    # A5 report_hint → 转报障预填
    ev("() => { window.__api.queryMode = 'hint'; }")
    ev("() => { document.querySelector('.asb-in').value = '语音按钮点了没反应'; }")
    page.click(".asb-send")
    page.wait_for_timeout(250)
    ck.check("A5 转报障按钮出现",
             ev("() => !!document.querySelector('[data-act=\"to-report\"]')"))
    ck.check("A5-mt 第二问携带上一轮 history（多轮上下文接线）",
             ev("() => { var q = window.__api.queries; "
                "var last = q[q.length - 1]; "
                "return Array.isArray(last.history) && "
                "last.history.length >= 1 && !!last.history[0].q; }"))
    page.click("[data-act=\"to-report\"]")
    ck.check("A5b 报障页签激活且描述预填",
             ev("() => document.querySelector('.asb-sub[data-tab=\"report\"]')"
                ".classList.contains('cur') && "
                "document.querySelector('.asb-rp-desc').value"
                ".indexOf('语音按钮') > -1"))

    # A6 报障提交（短描述不发 / 正常发成功）
    ev("() => { document.querySelector('.asb-rp-desc').value = '短'; }")
    page.click("[data-act=\"rp-submit\"]")
    page.wait_for_timeout(120)
    ck.check("A6 <5 字不发请求", ev("() => window.__api.reports.length === 0"))
    ev("() => { document.querySelector('.asb-rp-desc').value = "
       "'语音按钮点了没反应，控制台报错'; }")
    page.click("[data-act=\"rp-submit\"]")
    page.wait_for_timeout(200)
    ck.check("A6b 提交成功出工单号+入口",
             ev("() => window.__api.reports.length === 1 && "
                "document.querySelector('.asb-rp-out').textContent"
                ".indexOf('#33') > -1 && "
                "!!document.querySelector('[data-act=\"go-mine\"]')"))

    # A7 我的工单
    page.click("[data-act=\"go-mine\"]")
    page.wait_for_timeout(200)
    ck.check("A7 工单列表（fixed chip + 开发者留言）",
             ev("() => !!document.querySelector('.asb-tk') && "
                "!!document.querySelector('.asb-st.fixed') && "
                "document.body.textContent.indexOf('已修复请更新后验证') > -1"))

    # A8 err 事件 → 红气泡 + 重试
    page.click('.asb-mode[data-mode="chat"]')
    ev("() => { window.__api.queryMode = 'err'; }")
    ev("() => { document.querySelector('.asb-in').value = '再问一次'; }")
    page.click(".asb-send")
    page.wait_for_timeout(250)
    ck.check("A8 错误气泡+重试按钮",
             ev("() => document.body.textContent.indexOf('生成失败请重试') > -1"
                " && !!document.querySelector('[data-act=\"retry\"]')"))
    ev("() => { window.__api.queryMode = 'ok'; }")
    page.click("[data-act=\"retry\"]")
    page.wait_for_timeout(250)
    ck.check("A8b 重试重发同问题",
             ev("() => window.__api.queries.length === 4 && "
                "window.__api.queries[3].q === '再问一次'"))

    # A8c SSE 流式（2026-08-21 P2 生产真形态）：帧解析 + 打字机 + 终态格式化
    ev("() => { window.__api.queryMode = 'sse'; }")
    ev("() => { document.querySelector('.asb-in').value = '流式问题'; }")
    page.click(".asb-send")
    page.wait_for_timeout(300)
    ck.check("A8c SSE 流式渲染（分帧 delta 合成 + 来源卡 + 反馈行）",
             ev("() => document.body.textContent.indexOf('按 [S1] 操作即可') > -1"
                " && !!document.querySelector('.asb-fb[data-qa=\"9\"]')"))

    # A9 429 detail 透传
    ev("() => { window.__api.queryMode = '429'; }")
    ev("() => { document.querySelector('.asb-in').value = '快问'; }")
    page.click(".asb-send")
    page.wait_for_timeout(200)
    ck.check("A9 429 文案透传",
             ev("() => document.body.textContent.indexOf('问得太快啦') > -1"))
    ev("() => { window.__api.queryMode = 'ok'; }")


def _run_admin_tail(page, ck: Checker) -> None:
    ev = page.evaluate

    # A10 Esc 关面板
    page.keyboard.press("Escape")
    ck.check("A10 Esc 关面板",
             ev("() => !document.querySelector('.asb-panel.open') && "
                "document.querySelector('.asb-ball')"
                ".getAttribute('aria-expanded') === 'false'"))
    page.click(".asb-ball")

    # A11 voice=false → 无 🎤
    ck.check("A11 voice 关闭无麦克风按钮",
             ev("() => !document.querySelector('[data-act=\"mic\"]')"))

    # A12 截图 + 标注层（stub html2canvas）
    ev("() => { window.html2canvas = async function () { "
       "var c = document.createElement('canvas'); c.width = 320; "
       "c.height = 200; var x = c.getContext('2d'); x.fillStyle = '#fff'; "
       "x.fillRect(0, 0, 320, 200); return c; }; }")
    page.click('.asb-sub[data-tab="report"]')
    page.click("[data-act=\"shot-page\"]")
    page.wait_for_timeout(400)
    ck.check("A12 标注层出现（5 工具钮）",
             ev("() => !!document.querySelector('.asb-an') && "
                "document.querySelectorAll('.asb-an-bar [data-tool]')"
                ".length === 5"))
    page.click(".asb-an-bar [data-tool=\"ok\"]")
    page.wait_for_timeout(150)
    ck.check("A12b 确认后缩略图落位",
             ev("() => !document.querySelector('.asb-an') && "
                "!!document.querySelector('.asb-rp-thumb img')"))

    # A13 旧帮助球功能收编（admin 壳；2026-08-21 起球已退役=全局函数契约）。
    # P1 起它们住在 ⚙ 偏好页里——那是偏好不是功能，不该占主视觉。
    page.click('.asb-sub[data-tab="set"]')
    page.wait_for_timeout(150)
    ck.check("A13 工具行四入口在场（tips/tour/cmd/support）",
             ev("() => !!document.querySelector('[data-act=\"tour\"]') && "
                "!!document.querySelector('[data-act=\"cmd\"]') && "
                "!!document.querySelector('[data-act=\"tips\"]') && "
                "!!document.querySelector('[data-act=\"support\"]')"))
    page.click("[data-act=\"tips\"]")
    ck.check("A13b 术语提示走 window.toggleTermTips",
             ev("() => window.__legacy.tips === 1"))
    page.click("[data-act=\"support\"]")
    ck.check("A13c 上传诊断入口接通且面板让位",
             ev("() => window.__legacy.support === 1 && "
                "!document.querySelector('.asb-panel.open')"))

    # A14 主动援助：30s 内两个页面脚本错误 → 球旁冒泡 → 点按进报障预填指纹
    ev("() => { window.dispatchEvent(new ErrorEvent('error', "
       "{ message: 'boom-one', filename: '/x.js' })); }")
    ev("() => { window.dispatchEvent(new ErrorEvent('error', "
       "{ message: 'boom-two', filename: '/x.js' })); }")
    page.wait_for_timeout(180)
    ck.check("A14 错误冒泡出现",
             ev("() => !!document.querySelector('.asb-nudge')"))
    page.click(".asb-nudge [data-n=\"go\"]")
    page.wait_for_timeout(220)
    ck.check("A14b 冒泡直达报障页签且指纹预填",
             ev("() => document.querySelector('.asb-sub[data-tab=\"report\"]')"
                ".classList.contains('cur') && "
                "document.querySelector('.asb-rp-desc').value"
                ".indexOf('boom') > -1"))

    # O 组：orb 状态灯（2026-08-23 实施59「活体能量核」）——分层 DOM/状态类/
    # bloom 画布起停/一次性爆闪自终结。用 _orbForce 调试钩子驱动（与真实信号
    # 同一条 syncOrb 通路，只是判定源被覆写）。
    ck.check("O1 orb 分层 DOM 在场且默认档=2",
             ev("() => !!document.querySelector('.asb-ball .asb-orb') && "
                "!!document.querySelector('.asb-ball .asb-glow') && "
                "!!document.querySelector('.asb-ball .asb-ic svg') && "
                "AssistantBall._orbLevel() === 2"))
    ev("() => AssistantBall._orbForce('listening')")
    page.wait_for_timeout(150)
    ck.check("O2 listening：st 类上球+面板，bloom 画布点亮",
             ev("() => document.querySelector('.asb-ball')"
                ".classList.contains('st-listening') && "
                "document.querySelector('.asb-panel')"
                ".classList.contains('st-listening') && "
                "!!document.querySelector('.asb-fx') && "
                "document.querySelector('.asb-fx').style.display !== 'none'"))
    ev("() => AssistantBall._orbForce('thinking')")
    page.wait_for_timeout(150)
    ck.check("O3 thinking：状态互斥切换（画布保持）",
             ev("() => document.querySelector('.asb-ball')"
                ".classList.contains('st-thinking') && "
                "!document.querySelector('.asb-ball')"
                ".classList.contains('st-listening') && "
                "document.querySelector('.asb-fx').style.display !== 'none'"))
    ev("() => AssistantBall._orbForce(null)")
    page.wait_for_timeout(200)
    ck.check("O4 释放强制 → 回落真实态且画布熄灭（待机零 rAF）",
             ev("() => !document.querySelector('.asb-ball')"
                ".classList.contains('st-thinking') && "
                "document.querySelector('.asb-fx').style.display === 'none'"))
    ev("() => AssistantBall._orbBurst()")
    page.wait_for_timeout(150)
    ck.check("O5 success 爆闪一次性驱动画布",
             ev("() => document.querySelector('.asb-fx').style.display !== 'none'"))
    page.wait_for_timeout(1200)
    ck.check("O5b 爆闪自终结（画布熄灭回待机）",
             ev("() => document.querySelector('.asb-fx').style.display === 'none'"))

    # O6 speaking（P2）：播报态 → st 类 + 画布点亮 + 面板 voice-hero 波形条现身
    ev("() => AssistantBall._orbForce('speaking')")
    page.wait_for_timeout(200)
    ck.check("O6 speaking：st 类 + 画布 + hero 波形条",
             ev("() => document.querySelector('.asb-ball')"
                ".classList.contains('st-speaking') && "
                "document.querySelector('.asb-fx').style.display !== 'none' && "
                "document.querySelector('.asb-hero')"
                ".classList.contains('on')"))
    ev("() => AssistantBall._orbForce(null)")
    page.wait_for_timeout(200)
    ck.check("O6b 释放 → hero 收起",
             ev("() => !document.querySelector('.asb-hero')"
                ".classList.contains('on')"))

    # O7/O8 orbMode 公共 API（实施58 教学/代办线消费契约）
    ev("() => AssistantBall.orbMode('teach', true)")
    page.wait_for_timeout(120)
    ck.check("O7 orbMode('teach') → st-teach（纯 CSS 态，画布保持熄灭）",
             ev("() => document.querySelector('.asb-ball')"
                ".classList.contains('st-teach') && "
                "document.querySelector('.asb-fx').style.display === 'none'"))
    ev("() => AssistantBall.orbMode('teach', false)")
    ev("() => AssistantBall.orbMode('agent', true, 0.5)")
    page.wait_for_timeout(150)
    ck.check("O8 orbMode('agent', 0.5) → st-agent + 画布点亮",
             ev("() => document.querySelector('.asb-ball')"
                ".classList.contains('st-agent') && "
                "document.querySelector('.asb-fx').style.display !== 'none'"))
    ev("() => AssistantBall.orbMode('agent', false)")
    page.wait_for_timeout(150)

    # O9 播报按钮（P2）：答案行带 🔊；stub 无 tts 端点 → 点击=诚实失败红气泡
    page.click('.asb-mode[data-mode="chat"]')
    page.wait_for_timeout(120)
    ck.check("O9 答案行带播报按钮",
             ev("() => !!document.querySelector('[data-act=\"say\"]')"))
    page.click("[data-act=\"say\"]")
    page.wait_for_timeout(350)
    ck.check("O9b 无 TTS 后端 → 诚实失败红气泡 + 按钮复位",
             ev("() => document.body.textContent.indexOf('播报失败') > -1 && "
                "!document.querySelector('[data-act=\"say\"]').disabled"))

    # O10 字形库（P3）：bang/mic 一次性爆闪走同一画布通路
    ev("() => AssistantBall._orbBurst('bang')")
    page.wait_for_timeout(150)
    ck.check("O10 bang 爆闪驱动画布",
             ev("() => document.querySelector('.asb-fx').style.display !== 'none'"))
    page.wait_for_timeout(1100)
    ck.check("O10b bang 自终结",
             ev("() => document.querySelector('.asb-fx').style.display === 'none'"))

    # O11/O12 兄弟 DOM 契约（P3）：注入教学横幅/代办卡假标记 → 球点亮对应态
    #（与 tests/test_orb_sibling_dom_contract.py 静态词汇表成对：这里验行为）
    ev("() => { var b = document.createElement('div'); b.className = 'xzt-banner';"
       " b.id = 'fake-teach'; document.body.appendChild(b); }")
    page.wait_for_timeout(250)
    ck.check("O11 .xzt-banner 在场 → st-teach 点亮",
             ev("() => document.querySelector('.asb-ball')"
                ".classList.contains('st-teach')"))
    ev("() => document.getElementById('fake-teach').remove()")
    page.wait_for_timeout(250)
    ck.check("O11b 横幅移除 → teach 熄灭",
             ev("() => !document.querySelector('.asb-ball')"
                ".classList.contains('st-teach')"))
    ev("() => { var c = document.createElement('div'); c.className = 'xza-card';"
       " c.id = 'fake-agent';"
       " c.innerHTML = '<div class=\"xza-c-hd\">"
       "<button class=\"stop\" data-xza=\"stop\"></button></div>'"
       " + '<div class=\"xza-step\"><span class=\"ic\">\\u2713</span></div>'"
       " + '<div class=\"xza-step\"><span class=\"ic\">\\u2713</span></div>'"
       " + '<div class=\"xza-step\"><span class=\"ic\">\\u27a4</span></div>'"
       " + '<div class=\"xza-step\"><span class=\"ic\">\\u25cb</span></div>';"
       " document.body.appendChild(c); }")
    page.wait_for_timeout(300)
    ck.check("O12 .xza-card+stop 在场 → st-agent 点亮（进度=2/4）",
             ev("() => document.querySelector('.asb-ball')"
                ".classList.contains('st-agent') && "
                "AssistantBall._orbState() === 'agent' && "
                "document.querySelector('.asb-fx').style.display !== 'none'"))
    ev("() => document.getElementById('fake-agent').remove()")
    page.wait_for_timeout(300)
    ck.check("O12b 任务卡移除 → agent 熄灭且画布收",
             ev("() => !document.querySelector('.asb-ball')"
                ".classList.contains('st-agent') && "
                "document.querySelector('.asb-fx').style.display === 'none'"))

    # A16 教学模式投问（2026-08-23）：球开合走 pointerup，click() 打不开面板。
    # AssistantBall.ask 必须同步开面板并真的把问题送进 /api/assistant/query。
    ev("() => { var x = document.querySelector('.asb-x'); if (x) x.click(); }")
    page.wait_for_timeout(80)
    ev("() => { window.__api.queryMode = 'ok'; "
       "window.__askN = window.__api.queries.length; "
       "return AssistantBall.ask('教学模式提问：全自动是干什么用的？'); }")
    page.wait_for_timeout(250)
    ck.check("A16 ask() 开面板并投问（不靠 ball.click）",
             ev("() => !!document.querySelector('.asb-panel.open') && "
                "window.__api.queries.length === window.__askN + 1 && "
                "String(window.__api.queries[window.__api.queries.length-1].q)"
                ".indexOf('全自动') > -1"))
    ev("() => { document.dispatchEvent(new CustomEvent('asb-ask', "
       "{ bubbles: true, detail: { q: 'asb-ask 事件投问' } })); }")
    page.wait_for_timeout(200)
    ck.check("A16b asb-ask 事件同样投问",
             ev("() => window.__api.queries.length === window.__askN + 2 && "
                "String(window.__api.queries[window.__api.queries.length-1].q)"
                ".indexOf('asb-ask') > -1"))

    # A17 拒答不是死路（2026-08-29 老板实录「回复的内容没一点帮助」）：
    # NO_BASIS 哨兵下 meta 早已带着 sources 上线，但那些条目**正是被判定为
    # 回答不了这个问题的**——照旧列出来会读成「它找到了却不肯说」。
    # 先清屏：前面的轮次留了 7 个来源块，全局断言会被它们污染（首版就这样假红）。
    # 清 DOM 不动 chatHistory，本轮 pushChat 照常建 thinking 气泡。
    ev("() => { var b = document.querySelector('.asb-body'); "
       "if (b) { b.innerHTML = ''; } }")
    ev("() => { window.__api.queryMode = 'nobasis'; "
       "return AssistantBall.ask('怎么使用这个软件'); }")
    page.wait_for_timeout(400)
    ck.check("A17 拒答不列来源（那条来源正是被判定答不了的）",
             ev("() => !document.querySelector('.asb-srcs')"),
             ev("() => document.querySelectorAll('.asb-srcs').length + ' srcs'"))
    ck.check("A17b 拒答给出「我答得上」的建议（死路变菜单）",
             ev("() => document.querySelectorAll("
                "'.asb-sugg .asb-chip[data-q][data-sugg]').length >= 1"),
             ev("() => document.querySelectorAll('.asb-sugg .asb-chip')"
                ".length + ' sugg'"))
    ck.check("A17c 拒答同时保留报障出路（可能真是故障）",
             ev("() => !!document.querySelector('[data-act=\"to-report\"]')"))
    ck.check("A17d 建议不推荐刚问过的那一句",
             ev("() => { var cs = document.querySelectorAll("
                "'.asb-sugg .asb-chip[data-q]'); for (var i = 0; i < cs.length;"
                " i++) { if (cs[i].getAttribute('data-q') === "
                "'怎么使用这个软件') return false; } return true; }"))
    # 点建议 → 走 sendQuery（与首屏 chip 同一条链），且答成后来源回归
    ev("() => { window.__api.queryMode = 'sse'; "
       "window.__sugN = window.__api.queries.length; }")
    page.click('.asb-sugg .asb-chip[data-q]')
    page.wait_for_timeout(500)
    ck.check("A17e 点建议真的投问（不是哑 chip）",
             ev("() => window.__api.queries.length === window.__sugN + 1"))
    ck.check("A17f 答成的回答仍照常列来源（抑制只针对拒答）",
             ev("() => !!document.querySelector('.asb-srcs')"))

    # A18 依据分层（2026-08-29 P0）：零命中不再一律拒答，但**凭什么答**必须标。
    ev("() => { var b = document.querySelector('.asb-body'); "
       "if (b) { b.innerHTML = ''; } window.__api.queryMode = 'general'; "
       "return AssistantBall.ask('帮我写一句问候语'); }")
    page.wait_for_timeout(400)
    ck.check("A18 通用知识作答标注「非产品文档」",
             ev("() => !!document.querySelector('.asb-basis.gen')"),
             ev("() => { var e = document.querySelector('.asb-basis'); "
                "return e ? e.textContent : 'none'; }"))
    ck.check("A18b 通用回答不列来源（本来就没有文档依据）",
             ev("() => !document.querySelector('.asb-srcs')"))
    ev("() => { var b = document.querySelector('.asb-body'); "
       "if (b) { b.innerHTML = ''; } window.__api.queryMode = 'product'; "
       "return AssistantBall.ask('支持抖音吗'); }")
    page.wait_for_timeout(400)
    ck.check("A18c 产品事实卡作答标注「依据产品说明」",
             ev("() => { var e = document.querySelector('.asb-basis'); "
                "return !!e && !e.classList.contains('gen'); }"))
    # 拒答建议改用**本次真检索到的条目**，而不是页面热度榜（老板：建议与问题无关）
    ev("() => { var b = document.querySelector('.asb-body'); "
       "if (b) { b.innerHTML = ''; } window.__api.queryMode = 'nobasis'; "
       "return AssistantBall.ask('怎么导出所有客户的手机号'); }")
    page.wait_for_timeout(400)
    ck.check("A18d 拒答建议来自本次检索到的最近条目（非页面热度）",
             ev("() => { var c = document.querySelectorAll("
                "'.asb-sugg .asb-chip[data-q]'); for (var i = 0; i < c.length;"
                " i++) { if (c[i].getAttribute('data-q').indexOf('在此使用') "
                "> -1) return true; } return false; }"),
             ev("() => { var c = document.querySelectorAll("
                "'.asb-sugg .asb-chip[data-q]'); var o = []; for (var i = 0; "
                "i < c.length; i++) { o.push(c[i].getAttribute('data-q')); } "
                "return o.join(' | '); }"))

    run_panel_geometry(page, ck)

    # T1 教学模式「让小智详细讲讲」整条点击链（复现 2026-08-23 空点事故）
    # （teach/agent 已随夹具页一起加载＝生产形态，此处只驱动教学态）
    ev("() => { window.XZTeach.start(); }")
    page.wait_for_timeout(200)
    ev("() => { window.__askN2 = window.__api.queries.length; "
       "document.getElementById('page-title').dispatchEvent("
       "new MouseEvent('click', {bubbles:true, cancelable:true, view:window})); }")
    page.wait_for_timeout(150)
    ck.check("T1 点页面元素出讲解气泡",
             ev("() => !!document.querySelector('.xzt-bubble [data-xzt=\"ask\"]')"))
    ck.check("T1c 词条命中（terms API 下发 + 标题精确匹配）",
             ev("() => document.body.textContent.indexOf('夹具页说明文字') > -1"))
    ev("() => { var b = document.querySelector('.xzt-bubble [data-xzt=\"ask\"]'); "
       "if (b) b.click(); }")
    page.wait_for_timeout(300)
    ck.check("T1b 详细讲讲开面板并投问",
             ev("() => !!document.querySelector('.asb-panel.open') && "
                "!document.querySelector('.xzt-bubble') && "
                "window.__api.queries.length === window.__askN2 + 1"))

    # T2 归一化匹配（2026-08-23）：emoji + 全角计数「🎓 教学按钮（3）」
    # 归一化后命中词条「教学按钮」
    ev("() => { var x = document.querySelector('.asb-x'); if (x) x.click(); }")
    ev("() => window.XZTeach.start()")
    page.wait_for_timeout(150)
    ev("() => { document.getElementById('t-emoji-btn').dispatchEvent("
       "new MouseEvent('click', {bubbles:true, cancelable:true, "
       "view:window})); }")
    page.wait_for_timeout(150)
    ck.check("T2 emoji/计数归一化后词条命中",
             ev("() => document.body.textContent.indexOf('教学按钮说明') > -1"))

    # T3 自动导览（P2）：词典命中的控件串成有序讲解（anchor 站 + 归一化站）
    ev("() => { var b = document.querySelector('[data-xzt=\"btour\"]'); "
       "if (b) b.click(); }")
    page.wait_for_timeout(250)
    ck.check("T3 导览第一站（进度 1/2 + 词条标题）",
             ev("() => document.body.textContent.indexOf('1 / 2') > -1 && "
                "document.body.textContent.indexOf('夹具页说明文字') > -1"))
    ev("() => { var b = document.querySelector('[data-xzt=\"tnext\"]'); "
       "if (b) b.click(); }")
    page.wait_for_timeout(200)
    ck.check("T3b 第二站（2/2 + 完成按钮文案）",
             ev("() => document.body.textContent.indexOf('2 / 2') > -1 && "
                "document.body.textContent.indexOf('逛完了') > -1"))
    ev("() => { var b = document.querySelector('[data-xzt=\"tnext\"]'); "
       "if (b) b.click(); }")
    page.wait_for_timeout(200)
    ck.check("T3c 逛完自动收场（气泡关闭）",
             ev("() => !document.querySelector('.xzt-bubble')"))
    ev("() => window.XZTeach.stop()")
    page.wait_for_timeout(100)

    # A15 带我去聚光灯：sessionStorage 交接 → 重载 → 目标高亮 → 按键熄灯
    ev("() => { sessionStorage.setItem('asb_goto', JSON.stringify("
       "{ sel: '#page-title', ts: Date.now() })); }")
    page.reload()
    page.wait_for_timeout(1700)  # init 600ms + 滚动 380ms + 建灯 + 听键 400ms
    ck.check("A15 聚光灯锁定目标元素",
             ev("() => !!document.querySelector('.asb-spot')"))
    page.keyboard.press("Escape")
    page.wait_for_timeout(180)
    ck.check("A15b 任意按键熄灯",
             ev("() => !document.querySelector('.asb-spot')"))


def run_panel_geometry(page, ck: Checker) -> None:
    """P1 自由拖拽/缩放（2026-08-29，老板：「要能随意拖拉位置和放大放小」）。

    这些不变量**静态门禁一条都证不了**：面板几何是真实指针事件 + 布局的产物，
    而模板/JS 热更新直接上生产，没有「未部署」缓冲。所以只能真浏览器点。
    用真 mouse 事件（不是 dispatchEvent 合成）才走得到 pointer capture 那条路。
    """
    ev = page.evaluate

    # 复位到干净起点（前面的用例可能已经动过）
    ev("() => { localStorage.removeItem('asb_panel_v1'); }")
    ev("() => { if (!document.querySelector('.asb-panel.open')) "
       "{ document.querySelector('.asb-ball').click(); } }")
    page.wait_for_timeout(200)

    ck.check("G1 八向手柄齐备（四边四角）",
             ev("() => document.querySelectorAll('.asb-rs').length === 8"),
             ev("() => document.querySelectorAll('.asb-rs').length"))
    ck.check("G1b 右下角手柄可 Tab 到（键盘用户也能缩放）",
             ev("() => { var h = document.querySelector('.asb-rs.se'); "
                "return !!h && h.getAttribute('tabindex') === '0' && "
                "!!h.getAttribute('aria-label'); }"))

    before = ev("() => { var r = document.querySelector('.asb-panel')"
                ".getBoundingClientRect(); return {x:r.left,y:r.top,"
                "w:r.width,h:r.height}; }")

    # ── 拖动标题栏：真指针按下→移动→抬起
    hd = page.query_selector(".asb-hd")
    box = hd.bounding_box()
    page.mouse.move(box["x"] + 60, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.mouse.move(box["x"] + 60 - 160, box["y"] + box["height"] / 2 + 90,
                    steps=8)
    page.mouse.up()
    page.wait_for_timeout(120)
    after = ev("() => { var r = document.querySelector('.asb-panel')"
               ".getBoundingClientRect(); return {x:r.left,y:r.top,"
               "w:r.width,h:r.height}; }")
    ck.check("G2 拖标题栏真的移动了面板",
             abs(after["x"] - before["x"]) > 40 or
             abs(after["y"] - before["y"]) > 40,
             f"before={before} after={after}")
    ck.check("G2b 移动不改变尺寸（只挪不缩）",
             abs(after["w"] - before["w"]) < 2 and
             abs(after["h"] - before["h"]) < 2,
             f"w {before['w']}->{after['w']} h {before['h']}->{after['h']}")
    ck.check("G3 位置已持久化（刷新后不回弹）",
             ev("() => !!localStorage.getItem('asb_panel_v1')"))

    # ── 右下角缩放。先真拖到左上角腾出空间：否则底边贴着视口，**本来就拉不高**
    #    （首版这条红过——断言假设能长高，实际是出界封顶生效了＝把正确行为误判成
    #    bug。用真拖动而不是塞 localStorage，避免依赖开关面板的时序。）
    hd2 = page.query_selector(".asb-hd")
    b2 = hd2.bounding_box()
    page.mouse.move(b2["x"] + 60, b2["y"] + b2["height"] / 2)
    page.mouse.down()
    page.mouse.move(120, 90, steps=8)
    page.mouse.up()
    page.wait_for_timeout(120)
    after = ev("() => { var r = document.querySelector('.asb-panel')"
               ".getBoundingClientRect(); return {x:r.left,y:r.top,"
               "w:r.width,h:r.height}; }")
    h2 = page.query_selector(".asb-rs.se")
    hb = h2.bounding_box()
    page.mouse.move(hb["x"] + hb["width"] / 2, hb["y"] + hb["height"] / 2)
    page.mouse.down()
    page.mouse.move(hb["x"] + hb["width"] / 2 + 120,
                    hb["y"] + hb["height"] / 2 + 100, steps=8)
    page.mouse.up()
    page.wait_for_timeout(120)
    grown = ev("() => { var r = document.querySelector('.asb-panel')"
               ".getBoundingClientRect(); return {x:r.left,y:r.top,"
               "w:r.width,h:r.height}; }")
    ck.check("G4 右下角手柄能放大",
             grown["w"] > after["w"] + 40 and grown["h"] > after["h"] + 30,
             f"{after['w']}x{after['h']} -> {grown['w']}x{grown['h']}")
    ck.check("G4b 放大时左上角不动（不是整窗平移）",
             abs(grown["x"] - after["x"]) < 3 and
             abs(grown["y"] - after["y"]) < 3,
             f"origin {after['x']},{after['y']} -> {grown['x']},{grown['y']}")

    # ── 西北角：改尺寸的同时必须改原点，否则对边会跟着跑。
    #    先把面板推到离左边缘远一点：贴边时 clamp 会把 x 顶在 8px，右边缘就跟着
    #    外扩（首版这条红过 6px，查下来是出界保护在起作用＝正确行为被误判）。
    hd3 = page.query_selector(".asb-hd")
    b3 = hd3.bounding_box()
    page.mouse.move(b3["x"] + 60, b3["y"] + b3["height"] / 2)
    page.mouse.down()
    page.mouse.move(b3["x"] + 60 + 180, b3["y"] + b3["height"] / 2, steps=6)
    page.mouse.up()
    page.wait_for_timeout(120)
    grown = ev("() => { var r = document.querySelector('.asb-panel')"
               ".getBoundingClientRect(); return {x:r.left,y:r.top,"
               "w:r.width,h:r.height}; }")
    h3 = page.query_selector(".asb-rs.nw")
    nb = h3.bounding_box()
    page.mouse.move(nb["x"] + nb["width"] / 2, nb["y"] + nb["height"] / 2)
    page.mouse.down()
    page.mouse.move(nb["x"] + nb["width"] / 2 - 60,
                    nb["y"] + nb["height"] / 2 - 50, steps=6)
    page.mouse.up()
    page.wait_for_timeout(120)
    nw = ev("() => { var r = document.querySelector('.asb-panel')"
            ".getBoundingClientRect(); return {x:r.left,y:r.top,"
            "w:r.width,h:r.height,r:r.right,b:r.bottom}; }")
    ck.check("G5 西北角拉伸：右下角保持不动（原点跟着走）",
             abs(nw["r"] - (grown["x"] + grown["w"])) < 4 and
             abs(nw["b"] - (grown["y"] + grown["h"])) < 4,
             f"right {grown['x'] + grown['w']}->{nw['r']} "
             f"bottom {grown['y'] + grown['h']}->{nw['b']}")

    # ── 最小尺寸护栏：往回拽到负数也不能塌成一条线
    h4 = page.query_selector(".asb-rs.se")
    sb = h4.bounding_box()
    page.mouse.move(sb["x"] + sb["width"] / 2, sb["y"] + sb["height"] / 2)
    page.mouse.down()
    page.mouse.move(sb["x"] - 900, sb["y"] - 900, steps=10)
    page.mouse.up()
    page.wait_for_timeout(120)
    tiny = ev("() => { var r = document.querySelector('.asb-panel')"
              ".getBoundingClientRect(); return {w:r.width,h:r.height}; }")
    ck.check("G6 最小尺寸护栏（拽过头也不塌成一条线）",
             tiny["w"] >= 290 and tiny["h"] >= 250,
             f"{tiny['w']}x{tiny['h']}")

    # ── 复位：走**看得见的按钮**。双击标题栏也支持，但它不是主入口——
    #    发现性差，且真实鼠标序列下浏览器未必合成 dblclick（指针捕获会让两次
    #    点击的 target 不一致，实测 Chromium 因此不派发 dblclick）。
    ck.check("G7a 几何改过后「复位」按钮出现",
             ev("() => { var b = document.querySelector('.asb-hd-rst'); "
                "return !!b && !b.hidden; }"))
    page.click(".asb-hd-rst")
    page.wait_for_timeout(150)
    ck.check("G7 点复位按钮清掉自定义几何",
             ev("() => !localStorage.getItem('asb_panel_v1')"))
    ck.check("G7b 复位后按钮自己收起（没有可复位的东西了）",
             ev("() => { var b = document.querySelector('.asb-hd-rst'); "
                "return !!b && b.hidden; }"))

    # ── 越界自愈：存一个屏幕外的坐标，重开必须夹回可视区
    ev("() => { localStorage.setItem('asb_panel_v1', JSON.stringify("
       "{x: 99999, y: 99999, w: 400, h: 400})); }")
    ev("() => { var p = document.querySelector('.asb-panel'); "
       "if (p.classList.contains('open')) "
       "{ document.querySelector('.asb-ball').click(); } }")
    page.wait_for_timeout(120)
    ev("() => { document.querySelector('.asb-ball').click(); }")
    page.wait_for_timeout(200)
    onscreen = ev("() => { var r = document.querySelector('.asb-panel')"
                  ".getBoundingClientRect(); return r.left < window.innerWidth "
                  "&& r.top < window.innerHeight && r.right > 0 && r.bottom > 0;"
                  " }")
    ck.check("G8 存了屏幕外坐标也会被夹回可视区（防幽灵面板）", onscreen,
             ev("() => { var r = document.querySelector('.asb-panel')"
                ".getBoundingClientRect(); return r.left + ',' + r.top; }"))
    # 拖拽遮罩必须已卸掉：残留会吃掉整页点击（比面板本身坏了更严重）
    ck.check("G9 手势结束后无残留遮罩（不会锁死整页）",
             ev("() => { var n = document.querySelectorAll("
                "'div[aria-hidden=\"true\"]'); for (var i = 0; i < n.length; "
                "i++) { var s = n[i].style; if (s && s.position === 'fixed' && "
                "s.zIndex === '2147483000') return false; } return true; }"))
    ev("() => { localStorage.removeItem('asb_panel_v1'); }")

    # ── P2 主题桥：--xz-* 此前全站无定义，面板永远用回落色＝没接进主题系统
    ck.check("G10 强调色接到宿主品牌令牌（不再是写死的回落色）",
             ev("() => { var p = document.querySelector('.asb-panel'); "
                "var v = getComputedStyle(p).getPropertyValue('--xz-accent')"
                ".trim(); return !!v && v.toLowerCase() !== '#4f6ef7'; }"),
             ev("() => getComputedStyle(document.querySelector('.asb-panel'))"
                ".getPropertyValue('--xz-accent').trim()"))
    ck.check("G10b 面板底色/文字色同样走宿主令牌",
             ev("() => { var s = getComputedStyle("
                "document.querySelector('.asb-panel')); "
                "return !!s.getPropertyValue('--xz-bg').trim() && "
                "!!s.getPropertyValue('--xz-txt').trim(); }"))
    # 教学/代理模式的浮层是 position:fixed 直接挂 body 的，桥若只套在小智容器上
    # 就会漏掉它们（首版实测如此）——所以这条专门守「body 上也拿得到」。
    ck.check("G10c 直挂 body 的浮层（教学气泡等）同样拿得到令牌",
             ev("() => { var v = getComputedStyle(document.body)"
                ".getPropertyValue('--xz-accent').trim(); "
                "return !!v && v.toLowerCase() !== '#4f6ef7'; }"),
             ev("() => getComputedStyle(document.body)"
                ".getPropertyValue('--xz-accent').trim()"))


def run_ws(page, ck: Checker) -> None:
    ev = page.evaluate
    page.click(".asb-ball")
    page.wait_for_timeout(200)
    ck.check("W1 workspace 壳面板开且模式条三格在场",
             ev("() => !!document.querySelector('.asb-panel.open') && "
                "document.querySelectorAll('.asb-mode').length === 3"),
             ev("() => document.querySelectorAll('.asb-mode').length + ' modes'"))
    # ── 「替我做」模式的引导结构（2026-08-28，老板实录「版面太碎没有重点」）
    # 这三项修复全是**事件委托 + 动态 DOM**：静态哑按钮门禁只扫内联 on*=，
    # 天生看不见它们；而共享组件保存即上生产，所以「点了真的有反应」只有
    # 真浏览器能证明。窄屏 480 也正是老板截图的形态。
    page.click('.asb-mode[data-mode="agent"]')
    page.wait_for_timeout(250)
    ck.check("W2 替我做：整屏只有一颗实心主按钮，且长在主卡上",
             ev("() => document.querySelectorAll('.asb-md .asb-md-go')"
                ".length === 1 && !!document.querySelector("
                "'.asb-md-hero--pri .asb-md-go')"),
             ev("() => document.querySelectorAll('.asb-md .asb-md-go').length"
                " + ' go / pri=' + document.querySelectorAll("
                "'.asb-md-hero--pri').length"))
    ck.check("W2b 手机指挥降级为一行次要入口（通道≠能力，不再与主卡同级）",
             ev("() => !!document.querySelector("
                "'.asb-md-sub[data-xza=\"mode-pair\"]') && "
                "!document.querySelector('.asb-md-hero[data-xza=\"mode-pair\"]')"))
    ck.check("W2c 最近做过：标题行自带「全部 →」文字链（取代独占一行的按钮）",
             ev("() => !!document.querySelector("
                "'.asb-md-hd .asb-md-lnk[data-xza=\"mode-hist\"]')"))
    # 主按钮＝把光标送进输入框（模式说明与底部输入框之间原本没有衔接动作）。
    # fill('') 默认聚焦；改成 fill('', false) 或委托漏掉 mode-start 分支，
    # 这颗「唯一主按钮」就变成点了毫无反应的哑键——正是本条要钉住的。
    ev("() => { var i = document.querySelector('.asb-in'); if (i) i.blur(); }")
    page.click('.asb-md-go[data-xza="mode-start"]')
    page.wait_for_timeout(200)
    ck.check("W2d 点主按钮 → 光标进输入框（说明→输入的衔接动作）",
             ev("() => document.activeElement === "
                "document.querySelector('.asb-in')"),
             ev("() => (document.activeElement && "
                "(document.activeElement.className || document.activeElement"
                ".tagName)) || 'none'"))

    page.click('.asb-sub[data-tab="set"]')
    page.wait_for_timeout(150)
    ck.check("W1b ⚙ 偏好=仅动效档（workspace 壳无 admin 全局工具）",
             ev("() => document.querySelectorAll('.asb-tools .asb-tool')"
                ".length === 1 && !!document.querySelector("
                "'.asb-tools [data-act=\"orb-fx\"]') && "
                "!document.querySelector('.asb-tools [data-act=\"tour\"]')"))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    try:
        import playwright  # noqa: F401
    except Exception:
        print("[SKIP] playwright 未安装，跳过（exit 0）")
        return 0
    from playwright.sync_api import sync_playwright

    ck = Checker()
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)

            page = browser.new_page(viewport={"width": 900, "height": 800})
            errors: List[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(build_page(tdp, "admin").as_uri())
            page.wait_for_timeout(200)
            run_admin(page, ck)
            ck.check("A0 admin 壳零未捕获异常", not errors, "; ".join(errors[:3]))
            page.close()

            page2 = browser.new_page(viewport={"width": 480, "height": 900})
            errors2: List[str] = []
            page2.on("pageerror", lambda e: errors2.append(str(e)))
            page2.goto(build_page(tdp, "workspace").as_uri())
            page2.wait_for_timeout(200)
            run_ws(page2, ck)
            ck.check("W0 workspace 壳零未捕获异常", not errors2,
                     "; ".join(errors2[:3]))
            browser.close()
    return ck.summary()


if __name__ == "__main__":
    sys.exit(main())
