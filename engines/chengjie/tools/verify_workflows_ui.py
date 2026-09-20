# -*- coding: utf-8 -*-
"""工作流管理页（workflows.html）UX 契约真浏览器门禁（P2 2026-08-09）。

**为什么需要它**：本页 2026-08-09 批次落了一堆纯前端行为承诺——暗色页内变量组
（花斑修复）、tab 记忆/hash 深链、启用开关「编辑不复活停用链」、运行中徽标、
页头价值条/漏斗横条（P2）——全部是模板热更新直上生产的 JS/CSS，静态门禁只能
证「函数挂了 window、i18n 键存在」，证不了「暗色变量真的翻转、PUT 真的带回
enabled=0」。回归风险敞口最大的一批承诺由本工具钉住。

**夹具模式**（与 verify_goal_form_ui / verify_guided_tour_ui 同族）：
不 render 整个 workspace_base（底座依赖多且其组件各有自己的门禁——blTour 归
verify_guided_tour_ui 管），而是用真 Jinja 渲染 workflows.html 的 head/content
两个 block（页面自己的 CSS+HTML+JS 100% 真实），拼进自制壳：壳里提供
window.T/Tf（真 zh 词条注入）、apiFetch mock（记录请求 + 喂固定 fixtures，
零实例依赖零生产写入）、blTour stub（只收 steps 不真播）。file:// 打开。

覆盖的不变量（编号对应 run() 断言）：
  S1  亮色骨架：.wf-wrap 背景=改造前字面量 #f7f8fa（token 收口后亮色零变化）
  S2  暗色变量组生效：data-cp-theme=dark 下 .wf-wrap=#15171c、卡片=#1e2026、
      badge 暗变体换色——「白天模式花斑」的反向回归钉
  S3  默认落「工作链」tab（panel-chains active）
  S4  点「分流路由」→ active 切换 + localStorage wf_last_tab 记忆
  S5  #hash 深链优先于记忆（#monitor 打开 → monitor active）
  S6  运行中徽标：running 执行按链聚合成 wf-run-pill
  S7  停用链渲染：off 灰化类 + 「已停用」徽章 + toggle 未勾选
  S8  **编辑不复活停用链**：editChain(停用链)→saveChain → PUT body.enabled===0
  S9  页头价值条：数字与 funnel fixture 一致；「看明细」点击跳 monitor tab
  S10 漏斗横条：3 行、completed 宽度≈占比、0 值零宽不画假条
  S11 空态含示例包预览行（wf_empty_packs）
  S12 步骤时间轴容器（.wf-steps-tl）挂上且步骤卡带累计时间标注
  S13 「▶ 播放引导」→ blTour.start 收到 5 步且首步锚点真实存在
  S14 首访自动播（wf_tour_v1 未设 → 自动 start；已设不纠缠）
  S15 EN 渲染：页面 DOM 可见文本零 CJK 泄漏（词条缺 en 值当场暴露）

用法::

    python tools/verify_workflows_ui.py             # 门禁模式
    python tools/verify_workflows_ui.py --headed    # 肉眼看一遍

缺 playwright → SKIP exit 0（挂 gate_sweep -Full 的前提：环境缺失不污染回归信号）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, List, Tuple

ENGINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE))
sys.stdout.reconfigure(encoding="utf-8")


# ── fixtures（apiFetch mock 喂的固定数据；c2 是停用链=S7/S8 的主角）──────────
FX = {
    "chains": {
        "ok": True,
        "chains": [
            {
                "chain_id": "c1", "name": "新客破冰 · 7日跟进", "enabled": 1,
                "trigger_conditions": {"silence_days": 3},
                "steps": [
                    {"action_type": "template", "note": "破冰开场", "delay_hours": 0},
                    {"action_type": "template", "note": "第3天轻分享", "delay_hours": 48},
                    {"action_type": "task", "note": "第7天复盘", "delay_hours": 96},
                ],
            },
            {
                "chain_id": "c2", "name": "沉默唤回 · 3步", "enabled": 0,
                "trigger_conditions": {},
                "steps": [{"action_type": "template", "note": "轻唤回", "delay_hours": 0}],
            },
        ],
    },
    "running": {"ok": True, "executions": [
        {"exec_id": "e1", "chain_id": "c1", "status": "running"},
    ], "count": 1, "running_count": 1},
    "executions": {"ok": True, "executions": [], "count": 0, "running_count": 0},
    "funnel": {
        "ok": True, "days": 14,
        "total": {"started": 6, "running": 2, "completed": 3, "failed": 1,
                  "cancelled": 0, "reply_rate": 0.42, "replied_n": 3, "mature_n": 7,
                  "attributed": 2, "attr_reply_rate": 0.5},
        "chains": [{"chain_id": "c1", "chain_name": "新客破冰 · 7日跟进",
                    "started": 4, "completed": 2, "failed": 1,
                    "reply_rate": 0.5, "replied_n": 2, "mature_n": 4,
                    "by_step": [
                        {"step_idx": 0, "action_type": "template", "note": "破冰开场",
                         "attempts": 6, "ok": 5, "failed": 1},
                        {"step_idx": 1, "action_type": "template", "note": "第3天轻分享",
                         "attempts": 4, "ok": 4, "failed": 0},
                        {"step_idx": 2, "action_type": "task", "note": "第7天复盘",
                         "attempts": 2, "ok": 1, "failed": 1},
                    ]}],
        "rec_follow": {"followed": 1, "eligible": 2, "rate": 0.5},
    },
    "rules": {"ok": True, "rules": []},
}

_SHELL = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>workflows probe</title>
<script>
window.__I18N__ = @@I18N@@;
window.__FX__ = @@FX@@;
function __fmt(s, vars) {
  s = String(s == null ? '' : s);
  if (vars) for (const k of Object.keys(vars)) s = s.split('{' + k + '}').join(String(vars[k]));
  return s;
}
window.T = (k) => (window.__I18N__[k] != null ? window.__I18N__[k] : k);
window.Tf = (k, vars) => __fmt(window.T(k), vars);
window.__CALLS__ = [];
window.__PUTS__ = [];
const __resp = (d) => Promise.resolve({ ok: true, json: async () => d });
window.apiFetch = (url, opts) => {
  url = String(url);
  const method = String((opts && opts.method) || 'GET').toUpperCase();
  window.__CALLS__.push(method + ' ' + url);
  if (method === 'PUT' && url.indexOf('/api/workspace/workflow-chains/') >= 0) {
    window.__PUTS__.push(JSON.parse(opts.body));
    return __resp({ ok: true });
  }
  if (url.indexOf('/api/workspace/workflow-chains/seed') >= 0)
    return __resp({ ok: true, imported: [] });
  if (url.indexOf('/api/workspace/workflow-chains') >= 0) return __resp(window.__FX__.chains);
  if (url.indexOf('status=running') >= 0) return __resp(window.__FX__.running);
  if (url.indexOf('/api/workspace/chain-executions') >= 0) return __resp(window.__FX__.executions);
  if (url.indexOf('/api/workspace/chain-funnel') >= 0) return __resp(window.__FX__.funnel);
  if (url.indexOf('/api/workspace/routing-rules') >= 0) return __resp(window.__FX__.rules);
  return __resp({ ok: true });
};
window.__tourStarts = [];
window.blTour = {
  start: (steps) => { window.__tourStarts.push(steps); },
  stop: () => {},
  seen: () => false,
};
</script>
@@HEAD@@
</head><body>
@@CONTENT@@
</body></html>
"""


def render_blocks(lang: str) -> Tuple[str, str]:
    """真 Jinja 只渲 workflows.html 的 head/content 两个 block（extends 的父模板
    不执行主体）——页面自己的 CSS/HTML/JS 与生产逐字节同源。"""
    from jinja2 import Environment, FileSystemLoader

    from src.web.web_i18n import get_translations

    env = Environment(loader=FileSystemLoader(str(ENGINE / "src/web/templates")))
    tpl = env.get_template("workflows.html")
    ctx = tpl.new_context({"i18n": get_translations(lang)})
    head = "".join(tpl.blocks["head"](ctx))
    content = "".join(tpl.blocks["content"](ctx))
    return head, content


def build_fixture(lang: str) -> str:
    from src.web.web_i18n import get_translations

    head, content = render_blocks(lang)
    return (_SHELL
            .replace("@@I18N@@", json.dumps(get_translations(lang), ensure_ascii=False))
            .replace("@@FX@@", json.dumps(FX, ensure_ascii=False))
            .replace("@@HEAD@@", head)
            .replace("@@CONTENT@@", content))


class Checker:
    def __init__(self) -> None:
        self.results: List[Tuple[str, bool]] = []

    def check(self, name: str, cond: Any, detail: str = "") -> bool:
        ok = bool(cond)
        self.results.append((name, ok))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
        return ok

    def summary(self) -> int:
        fails = [n for n, ok in self.results if not ok]
        total = len(self.results)
        print(f"\n== workflows 页 UX 契约: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


_CJK = re.compile(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        print("SKIP: playwright 不可用（pip install playwright && playwright install chromium）")
        return 0

    ck = Checker()
    zh = build_fixture("zh")
    en = build_fixture("en")

    with tempfile.TemporaryDirectory() as td:
        fp_zh = Path(td) / "wf_probe_zh.html"
        fp_en = Path(td) / "wf_probe_en.html"
        fp_zh.write_text(zh, encoding="utf-8")
        fp_en.write_text(en, encoding="utf-8")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headed)
            page = browser.new_page(viewport={"width": 1360, "height": 900})
            # 默认场景：抑制首访自动播（专测场景另开），清 tab 记忆保 S3 确定性
            page.add_init_script(
                "try{localStorage.clear();localStorage.setItem('wf_tour_v1','1');}catch(_){}")
            page.goto(fp_zh.as_uri())
            page.wait_for_timeout(250)

            # S1 亮色骨架零变化
            bg = page.evaluate(
                "getComputedStyle(document.querySelector('.wf-wrap')).backgroundColor")
            ck.check("S1 亮色骨架 .wf-wrap=#f7f8fa", bg == "rgb(247, 248, 250)", bg)

            # S3 默认 tab=chains
            ck.check("S3 默认落「工作链」tab",
                     page.evaluate("document.getElementById('panel-chains').classList.contains('active')"))

            # S6 运行中徽标（c1 聚合 1 条 running）
            pill = (page.text_content(".wf-run-pill") or "").strip()
            ck.check("S6 运行中徽标聚合", "1" in pill, pill)

            # S16 每链回复率 pill：c1 有成熟数（0.5, 2/4）→ 显示 50%；c2 无数据 → 无 pill
            s16 = page.evaluate("""() => {
                const cards = [...document.querySelectorAll('.wf-chain-card')];
                const c1 = cards.find(c => c.textContent.includes('新客破冰'));
                const c2 = cards.find(c => c.textContent.includes('沉默唤回'));
                const p1 = c1 && c1.querySelector('.wf-reply-pill');
                return {t: p1 ? p1.textContent : '', c2none: c2 && !c2.querySelector('.wf-reply-pill')};
            }""")
            ck.check("S16 每链回复率 pill（有成熟数才显示）",
                     isinstance(s16, dict) and "50%" in str(s16.get("t", ""))
                     and s16.get("c2none"), str(s16))

            # S7 停用链渲染（c2）：off 灰化 + 徽章 + toggle 未勾选
            s7 = page.evaluate("""() => {
                const cards = [...document.querySelectorAll('.wf-chain-card')];
                const off = cards.find(c => c.textContent.includes('沉默唤回'));
                if (!off) return 'no-card';
                const tg = off.querySelector('.toggle-sw input');
                return {off: off.classList.contains('off'),
                        badge: !!off.querySelector('.badge-cancelled'),
                        unchecked: tg && !tg.checked};
            }""")
            ck.check("S7 停用链＝灰化+徽章+开关关",
                     isinstance(s7, dict) and s7.get("off") and s7.get("badge") and s7.get("unchecked"),
                     str(s7))

            # S12 步骤时间轴容器 + 累计时间标注
            s12 = page.evaluate("""() => {
                const tl = document.querySelector('.wf-chain-card .wf-steps-tl');
                if (!tl) return 'no-tl';
                const t = [...tl.querySelectorAll('.wf-step-t')].map(e => e.textContent).join('|');
                return {steps: tl.querySelectorAll('.step-card').length, times: t};
            }""")
            ck.check("S12 时间轴容器 + 累计标注",
                     isinstance(s12, dict) and s12.get("steps", 0) >= 3
                     and "96" in str(s12.get("times", "")),
                     str(s12))

            # S9 页头价值条（fixture: started6/completed3/42%/归因2）
            s9 = page.evaluate("""() => {
                const bar = document.getElementById('wf-value-bar');
                return {on: bar.classList.contains('on'), txt: bar.textContent};
            }""")
            t9 = str(s9.get("txt", "")) if isinstance(s9, dict) else ""
            ck.check("S9 价值条可见且数字同源",
                     isinstance(s9, dict) and s9.get("on")
                     and "6" in t9 and "42%" in t9 and "2" in t9, t9[:80])

            # S9b 「看明细」跳 monitor
            page.click("#wf-value-bar .wf-vb-go")
            page.wait_for_timeout(150)
            ck.check("S9b 价值条「看明细」跳执行监控",
                     page.evaluate("document.getElementById('panel-monitor').classList.contains('active')"))

            # S10 漏斗横条：3 行 + completed 宽度≈50%（3/6）+ cancelled 无假条
            page.wait_for_timeout(200)
            s10 = page.evaluate("""() => {
                const rows = [...document.querySelectorAll('.wf-funnel-bars .wf-fb-row')];
                const done = document.querySelector('.wf-fb-fill.done');
                const track = done && done.parentElement.getBoundingClientRect().width;
                const w = done && done.getBoundingClientRect().width;
                return {n: rows.length, ratio: (track ? w / track : 0)};
            }""")
            ck.check("S10 漏斗横条 3 行 + completed≈50% 宽",
                     isinstance(s10, dict) and s10.get("n") == 3
                     and abs(float(s10.get("ratio", 0)) - 0.5) < 0.06, str(s10))

            # S18 环节明细下钻：按钮在 → 点开出三环节（数字同源）→ 模拟 30s 轮询
            #     整块重渲 → 展开态幸存 → 收起后消失
            has_btn = page.evaluate("!!document.querySelector('#funnel-box .wf-bs-btn')")
            page.evaluate("document.querySelector('#funnel-box .wf-bs-btn').click()")
            page.wait_for_timeout(150)
            s18a = page.evaluate("""() => {
                const rows = [...document.querySelectorAll('#funnel-box .wf-bs-item')];
                const t = rows.map(r => r.textContent.replace(/\\s+/g, '')).join('|');
                return {n: rows.length, t: t};
            }""")
            page.evaluate("WF.loadFunnel()")   # 模拟轮询：fresh 拉取 + 整块重渲
            page.wait_for_timeout(200)
            s18b = page.evaluate(
                "document.querySelectorAll('#funnel-box .wf-bs-item').length")
            page.evaluate("document.querySelector('#funnel-box .wf-bs-btn').click()")
            page.wait_for_timeout(150)
            s18c = page.evaluate(
                "document.querySelectorAll('#funnel-box .wf-bs-item').length")
            ck.check("S18 环节明细：开→数字对→轮询重渲幸存→收起",
                     has_btn and isinstance(s18a, dict) and s18a.get("n") == 3
                     and "✓5" in str(s18a.get("t", "")) and "✗1" in str(s18a.get("t", ""))
                     and s18b == 3 and s18c == 0,
                     f"a={s18a} b={s18b} c={s18c}")

            # S4 tab 记忆
            page.click('[data-tab="routing"]')
            page.wait_for_timeout(100)
            ck.check("S4 tab 切换 + wf_last_tab 记忆",
                     page.evaluate("localStorage.getItem('wf_last_tab')") == "routing")

            # S8 编辑不复活停用链：editChain(c2) → saveChain → PUT enabled===0
            page.evaluate("WF.editChain('c2')")
            page.wait_for_timeout(150)
            page.evaluate("WF.saveChain()")
            page.wait_for_timeout(150)
            s8 = page.evaluate("window.__PUTS__.length ? window.__PUTS__[window.__PUTS__.length-1] : null")
            ck.check("S8 编辑停用链保存回传 enabled=0",
                     isinstance(s8, dict) and s8.get("chain_id") == "c2" and s8.get("enabled") == 0,
                     json.dumps(s8, ensure_ascii=False)[:100] if s8 else "no-put")

            # S13 播放引导：5 步且首步锚点真实存在
            page.evaluate("window.__tourStarts.length = 0")
            page.click("#wf-tour-btn")
            page.wait_for_timeout(100)
            s13 = page.evaluate("""() => {
                const s = window.__tourStarts[0] || [];
                return {n: s.length,
                        first: s.length ? !!document.querySelector(s[0].sel) : false};
            }""")
            ck.check("S13 引导 5 步 + 首步锚点在场",
                     isinstance(s13, dict) and s13.get("n") == 5 and s13.get("first"), str(s13))

            # S2 暗色变量组（同页切 data-cp-theme，无需重载）
            page.evaluate("document.documentElement.setAttribute('data-cp-theme','dark')")
            page.wait_for_timeout(100)
            s2 = page.evaluate("""() => {
                const wrap = getComputedStyle(document.querySelector('.wf-wrap'));
                const card = getComputedStyle(document.querySelector('.wf-chain-card'));
                const badge = document.querySelector('.badge-cancelled');
                return {bg: wrap.backgroundColor, card: card.backgroundColor,
                        ok: wrap.getPropertyValue('--wf-ok').trim(),
                        badge: badge ? getComputedStyle(badge).color : ''};
            }""")
            ck.check("S2 暗色：页底/卡片/状态色/badge 全翻转",
                     isinstance(s2, dict) and s2.get("bg") == "rgb(21, 23, 28)"
                     and s2.get("card") == "rgb(30, 32, 38)"
                     and s2.get("ok") == "#3fb950"
                     and s2.get("badge") == "rgb(148, 163, 184)", str(s2))

            # S5 hash 深链优先于记忆（此刻记忆=routing，经 S4 写入 wf_last_tab=routing
            #    ——若 hash 不优先，会落回 routing）。⚠ 同 URL 仅 hash 变化的 goto
            #    是同文档导航（IIFE 不重跑），必须先离开再进。
            page.goto("about:blank")
            page.goto(fp_zh.as_uri() + "#monitor")
            page.wait_for_timeout(250)
            ck.check("S5 #hash 深链优先于记忆",
                     page.evaluate("document.getElementById('panel-monitor').classList.contains('active')"))

            # S11 空态包名预览（清空 fixture 再 loadChains）
            page.evaluate("window.__FX__.chains = {ok: true, chains: []}; WF.loadChains();")
            page.wait_for_timeout(150)
            empty_txt = page.evaluate(
                "(document.getElementById('chains-list')||{}).textContent || ''")
            ck.check("S11 空态含示例包预览", "破冰" in empty_txt and "唤回" in empty_txt,
                     empty_txt.strip()[:60])

            # S17 价值条空账面 CTA：零启动 + onboard 已关 → 引导行；onboard 在场 → 避让隐藏
            page.evaluate("""() => {
                window.__FX__.funnel = {ok: true, days: 14,
                    total: {started: 0}, chains: [], rec_follow: {}};
                WF._funnelP = null;
                localStorage.setItem('wf_onboard_v1', '1');
                WF.loadValueBar();
            }""")
            page.wait_for_timeout(150)
            s17a = page.evaluate("""() => {
                const b = document.getElementById('wf-value-bar');
                return {on: b.classList.contains('on'), txt: b.textContent};
            }""")
            page.evaluate(
                "localStorage.removeItem('wf_onboard_v1'); WF.loadValueBar();")
            page.wait_for_timeout(150)
            s17b = page.evaluate(
                "document.getElementById('wf-value-bar').classList.contains('on')")
            ck.check("S17 空账面 CTA：onboard 关→出场 / 在场→避让",
                     isinstance(s17a, dict) and s17a.get("on")
                     and "导入" in str(s17a.get("txt", "")) and (s17b is False),
                     f"a={s17a} b={s17b}")

            # S14 首访自动播（新 context：不预设 wf_tour_v1）
            page2 = browser.new_page(viewport={"width": 1360, "height": 900})
            page2.add_init_script("try{localStorage.clear();}catch(_){}")
            page2.goto(fp_zh.as_uri())
            page2.wait_for_timeout(900)   # 首播延时 600ms
            auto_n = page2.evaluate("window.__tourStarts.length")
            mark = page2.evaluate("localStorage.getItem('wf_tour_v1')")
            ck.check("S14 首访自动播一次 + play-once 落章", auto_n == 1 and mark == "1",
                     f"starts={auto_n} mark={mark}")
            page2.close()

            # S15 EN 渲染可见文本零 CJK（词条缺 en 当场暴露）
            page3 = browser.new_page(viewport={"width": 1360, "height": 900})
            page3.add_init_script(
                "try{localStorage.clear();localStorage.setItem('wf_tour_v1','1');}catch(_){}")
            page3.goto(fp_en.as_uri())
            page3.wait_for_timeout(300)
            vis_text = page3.evaluate("document.body.innerText || ''")
            leaks = sorted(set(_CJK.findall(vis_text)))
            # fixture 链名/步骤 note 是数据（运营中文内容），不算词条泄漏
            data_words = {"新客破冰", "日跟进", "沉默唤回", "步", "破冰开场", "第3天轻分享",
                          "第7天复盘", "轻唤回"}
            leaks = [w for w in leaks if not any(d in w or w in d for d in data_words)]
            ck.check("S15 EN 渲染零 CJK 泄漏", not leaks, str(leaks[:5]))
            page3.close()

            browser.close()

    return ck.summary()


if __name__ == "__main__":
    sys.exit(main())
