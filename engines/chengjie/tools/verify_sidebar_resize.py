# -*- coding: utf-8 -*-
"""业务助手侧栏「拖宽」真浏览器门禁（Playwright；P0 2026-08-17 拖拽性能重构随批落地）。

**为什么需要它**：拖宽卡顿/粘滞的四个根因全是**交互时序层**的（每帧写宽强制 reflow、
每帧 getBoundingClientRect、mouse 事件滑进 iframe/<webview> 被吞、桌面 transition
追帧），静态门禁只能证「字符串在」，证不了「拖起来是什么手感」。本门禁用真实鼠标
输入（CDP 注入，走浏览器真 hit-testing 与 pointer capture 语义）压重构后的行为契约。

**夹具模式**（与 tools/verify_cp_voice_ui.py 同族）：file:// 自包含页面 + 真
sidebar-chrome.js（shared/copilot 单源，Web/桌面壳/App 三面同一实现）+ 主内容区
铺满 iframe **模拟 App 模式右栏/桌面壳 webview 的事件吞没场景**——旧实现指针滑进
iframe 即「粘住」，重构后 pointer capture + 全窗护罩必须保证拖动不断。
零实例依赖、零生产写入。

覆盖的不变量（编号对应 run() 里的断言）：
  R1  init：恢复默认宽 + a11y（role=separator / aria-valuenow / tabindex）
  R2  pointerdown：handle 带 .dragging、全窗护罩挂载、body 光标 col-resize
  R3  拖动跟手：向左拖 → 宽度随动（rAF 合帧后等值）；宽度气泡显示实时 px
  R4  指针滑过 iframe 区域拖动不断（capture+护罩；旧实现此处「粘住」）
  R5  上限夹紧：拖过 _dynMax（min(720,45vw,父宽-520)）钉在上限
  R6  下限夹紧：反向拖过 min 钉在 280
  R7  pointerup：护罩移除、.dragging 摘除、气泡隐藏、宽度持久化 ws_sidebar_w
  R8  双击恢复默认宽并持久化（pointer 化后兼容鼠标事件链必须活着——
      preventDefault(pointerdown) 会杀掉它，这里是回归钉）
  R9  键盘调宽：← 加宽 16 / → 收窄 16，aria-valuenow 同步 + 持久化
  R10 静态钉：桌面壳 style.css 拖拽 transition 豁免 + max-width 与 JS 上限对齐
  R11 静态钉：unified_inbox.html 与 app.html 的 sidebar-chrome.js ?v= 戳一致
      （bump 一处忘一处 = 两面跑不同代码）

用法::

    python tools/verify_sidebar_resize.py             # 门禁模式
    python tools/verify_sidebar_resize.py --headed    # 肉眼看一遍

缺 playwright → SKIP exit 0（挂 gate_sweep 的前提：环境缺失不污染回归信号）。
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, List, Tuple

ENGINE = Path(__file__).resolve().parents[1]

# 布局契约：视口 1280 → _dynMax = min(720, 45vw=576, 1280-520=760) = 576
VIEW_W = 1280
VIEW_H = 800
DYN_MAX = 576
W_MIN = 280
W_DEF = 300

_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>sidebar resize probe</title>
<style>
  /* 对齐真实站点全局 box-sizing（unified-inbox / 桌面壳皆 border-box）；
     content-box 下 1px 边框会把 offsetWidth 撑大 1 → 全部等值断言假红 */
  *,*::before,*::after{box-sizing:border-box;}
  html,body{margin:0;height:100%;}
  #row{display:flex;flex-direction:row;height:100%;}
  #main{flex:1;min-width:0;position:relative;background:#f3f4f6;}
  #main iframe{position:absolute;inset:0;width:100%;height:100%;border:0;}
  #panel{position:relative;flex-shrink:0;background:#fff;border-left:1px solid #ddd;}
  #hd{position:absolute;left:0;top:0;bottom:0;width:5px;cursor:col-resize;z-index:3;touch-action:none;}
</style></head>
<body>
<div id="row">
  <div id="main"><iframe src="about:blank" title="swallow-probe"></iframe></div>
  <div id="panel"><div id="hd"></div></div>
</div>
<script src="@@CHROME_JS@@"></script>
<script>
window.__ctrl = window.CopilotShared.sidebarChrome.createResizeController({
  panel: 'panel', handle: 'hd', defaultWidth: @@W_DEF@@,
});
window.__ctrl.init();
window.snap = () => {
  const p = document.getElementById('panel');
  const hd = document.getElementById('hd');
  const sh = document.getElementById('ws-cp-resize-shield');
  const bb = document.getElementById('ws-cp-w-bubble');
  return {
    w: p.offsetWidth,
    dragging: hd.classList.contains('dragging'),
    shield: !!(sh && sh.parentNode),
    cursor: document.body.style.cursor || '',
    bubbleShown: !!(bb && bb.classList.contains('show')),
    bubbleText: bb ? bb.textContent : '',
    stored: (() => { try { return localStorage.getItem('ws_sidebar_w'); } catch (e) { return null; } })(),
    role: hd.getAttribute('role') || '',
    ariaNow: hd.getAttribute('aria-valuenow') || '',
    tabindex: hd.getAttribute('tabindex') || '',
  };
};
</script>
</body></html>
"""


def build_fixture_page(tmp: Path) -> Path:
    html = (_HTML
            .replace("@@CHROME_JS@@", (ENGINE / "shared/copilot/sidebar-chrome.js").as_uri())
            .replace("@@W_DEF@@", str(W_DEF)))
    fp = tmp / "probe_resize.html"
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
        print(f"\n== 侧栏拖宽验证: {total - len(fails)}/{total} PASS"
              + (f"  FAILED: {fails}" if fails else " =="))
        return 1 if fails else 0


def _handle_x(page) -> float:
    box = page.evaluate("() => { const r = document.getElementById('hd').getBoundingClientRect();"
                        " return { x: r.left + 2, y: r.top + r.height / 2 }; }")
    return box


def run(page, ck: Checker) -> None:
    ev = page.evaluate

    # R1 init
    s = ev("snap()")
    ck.check("R1 恢复默认宽", s["w"] == W_DEF, f"w={s['w']}")
    ck.check("R1 a11y separator 语义",
             s["role"] == "separator" and s["tabindex"] == "0" and s["ariaNow"] == str(W_DEF),
             f"role={s['role']} now={s['ariaNow']}")

    # R2/R3 真实鼠标拖动：down → 分步左移 150px
    pos = _handle_x(page)
    page.mouse.move(pos["x"], pos["y"])
    page.mouse.down()
    s = ev("snap()")
    ck.check("R2 拖起：.dragging + 护罩挂载 + col-resize",
             s["dragging"] and s["shield"] and s["cursor"] == "col-resize")
    for step in range(1, 6):
        page.mouse.move(pos["x"] - 30 * step, pos["y"])
    page.wait_for_timeout(80)   # rAF 合帧落定
    s = ev("snap()")
    want = W_DEF + 150
    ck.check("R3 拖动跟手（rAF 后等值）", s["w"] == want, f"w={s['w']} want={want}")
    ck.check("R3 宽度气泡实时显示", s["bubbleShown"] and s["bubbleText"] == f"{want}px",
             s["bubbleText"])

    # R4 指针深入 iframe 腹地（主内容区中央）拖动不断——旧实现在这里「粘住」
    page.mouse.move(400, VIEW_H // 2)
    page.wait_for_timeout(80)
    s = ev("snap()")
    want_iframe = min(DYN_MAX, W_DEF + (pos["x"] - 400))
    ck.check("R4 滑过 iframe 拖动不断（capture+护罩）",
             s["dragging"] and abs(s["w"] - want_iframe) <= 1, f"w={s['w']} want~{want_iframe}")

    # R5 上限夹紧（拖到极左）
    page.mouse.move(60, VIEW_H // 2)
    page.wait_for_timeout(80)
    s = ev("snap()")
    ck.check("R5 上限钉在 _dynMax", s["w"] == DYN_MAX, f"w={s['w']} cap={DYN_MAX}")

    # R6 下限夹紧（反向拖过右缘）
    page.mouse.move(VIEW_W - 10, VIEW_H // 2)
    page.wait_for_timeout(80)
    s = ev("snap()")
    ck.check("R6 下限钉在 min", s["w"] == W_MIN, f"w={s['w']} min={W_MIN}")

    # R7 松手收尾：护罩/气泡/dragging 全清 + 持久化
    page.mouse.up()
    page.wait_for_timeout(40)
    s = ev("snap()")
    ck.check("R7 松手清场（护罩/气泡/.dragging/光标）",
             (not s["dragging"]) and (not s["shield"]) and (not s["bubbleShown"])
             and s["cursor"] == "")
    ck.check("R7 宽度持久化 ws_sidebar_w", s["stored"] == str(s["w"]),
             f"stored={s['stored']} w={s['w']}")

    # R8 双击恢复默认（pointer 化后兼容鼠标链回归钉）
    pos = _handle_x(page)
    page.mouse.dblclick(pos["x"], pos["y"])
    page.wait_for_timeout(40)
    s = ev("snap()")
    ck.check("R8 双击恢复默认宽并持久化",
             s["w"] == W_DEF and s["stored"] == str(W_DEF), f"w={s['w']} stored={s['stored']}")

    # R9 键盘调宽（a11y）：← 加宽 / → 收窄，步长 16
    ev("() => document.getElementById('hd').focus()")
    page.keyboard.press("ArrowLeft")
    page.wait_for_timeout(30)
    s = ev("snap()")
    ck.check("R9 ← 加宽 16 + aria/持久化同步",
             s["w"] == W_DEF + 16 and s["ariaNow"] == str(W_DEF + 16)
             and s["stored"] == str(W_DEF + 16), f"w={s['w']}")
    page.keyboard.press("ArrowRight")
    page.wait_for_timeout(30)
    s = ev("snap()")
    ck.check("R9 → 收窄回默认", s["w"] == W_DEF, f"w={s['w']}")


def static_checks(ck: Checker) -> None:
    """R10/R11 静态钉：不进浏览器也必须成立的发布配套。"""
    desk = (ENGINE / "desktop/renderer/style.css").read_text(encoding="utf-8")
    ck.check("R10 桌面壳拖拽 transition 豁免",
             ":has(> .cp-sidebar-resize.dragging)" in desk)
    ck.check("R10 桌面壳 max-width 与 JS 上限对齐（不再 480 打架）",
             re.search(r"#copilot\s*\{[^}]*max-width:\s*min\(720px,\s*45vw\)", desk) is not None)

    inbox = (ENGINE / "src/web/templates/unified_inbox.html").read_text(encoding="utf-8")
    app = (ENGINE / "shared/copilot/app.html").read_text(encoding="utf-8")
    m_inbox = re.search(r"sidebar-chrome\.js\?v=([\w.-]+)", inbox)
    m_app = re.search(r"sidebar-chrome\.js\?v=([\w.-]+)", app)
    ck.check("R11 两面 sidebar-chrome ?v= 戳一致",
             m_inbox and m_app and m_inbox.group(1) == m_app.group(1),
             f"inbox={m_inbox and m_inbox.group(1)} app={m_app and m_app.group(1)}")


def main() -> int:
    # Windows 控制台默认 GBK：emoji/生僻字直接 UnicodeEncodeError，统一改 UTF-8 输出
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
    static_checks(ck)
    with tempfile.TemporaryDirectory() as td:
        page_fp = build_fixture_page(Path(td))
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            page = browser.new_page(viewport={"width": VIEW_W, "height": VIEW_H})
            errors: List[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(page_fp.as_uri())
            page.wait_for_timeout(120)
            run(page, ck)
            ck.check("R0 全程零未捕获 JS 异常", not errors, "; ".join(errors[:3]))
            browser.close()
    return ck.summary()


if __name__ == "__main__":
    sys.exit(main())
