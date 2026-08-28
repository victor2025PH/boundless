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
<html><head><meta charset="utf-8"><title>assistant-ball probe</title></head>
<body style="margin:16px;font-family:system-ui,'Microsoft YaHei',sans-serif;">
<h3 id="page-title" data-anchor="fixture_page">fixture page</h3>
<button id="t-emoji-btn" type="button">🎓 教学按钮（3）</button>
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
    # A3d 复制按钮（与 🔊 并排）+ 点击出 ✓ 反馈
    ck.check("A3d 复制按钮在场",
             ev("() => !!document.querySelector('[data-act=\"copy\"]')"))
    page.click('[data-act="copy"]')
    page.wait_for_timeout(150)
    ck.check("A3e 复制点击出 ✓ 反馈",
             ev("() => document.querySelector('[data-act=\"copy\"]')"
                ".textContent.indexOf('✓') > -1 || "
                "document.querySelector('[data-act=\"copy\"]')"
                ".textContent.indexOf('📋') > -1"))
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


def run_ws(page, ck: Checker) -> None:
    ev = page.evaluate
    page.click(".asb-ball")
    page.wait_for_timeout(200)
    ck.check("W1 workspace 壳面板开且模式条三格在场",
             ev("() => !!document.querySelector('.asb-panel.open') && "
                "document.querySelectorAll('.asb-mode').length === 3"),
             ev("() => document.querySelectorAll('.asb-mode').length + ' modes'"))
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
