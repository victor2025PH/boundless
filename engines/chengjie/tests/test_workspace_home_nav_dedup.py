# -*- coding: utf-8 -*-
"""「返回工作台」去重接线静态门禁（P1-③ 2026-08-11）。

背景：坐席工作台的窗口唯一性由三层保证（_win_unique 入口收敛 / BC 跨组交接 /
__wsMultiWin 待机协调），但「子页 / SSE toast / 铃铛 → location.href='/workspace'」
这族**原地导航**长期漏网——辅助窗被就地变成第二个坐席工作台（2026-08-11 老板实录
「为什么还会出现两个智聊坐席工作台」，遥测 mw.takeover_auto/reclaim 坐实）。

修复＝window.__wsGoHome（别处已有活跃坐席 → 交接不重开；没有 → 原地导航）+
_win_unique 内的委托点击拦截器（<a href="/workspace"> 同标签导航零接线自动获益）。

本门禁钉三件事（真浏览器行为由 tools/verify_multiwin_ui.py 场景 8 钉）：
1. _win_unique.html 必须导出 __wsGoHome（兜底桩 + 真实现 + 委托拦截器 + allowPlain）；
2. 工作台侧模板里凡 JS 直跳精确 '/workspace' 的语句，同行或紧邻上文必须带
   __wsGoHome 守卫——新增裸导航（＝重新打开二重工作台漏洞）即红；
3. i18n 键 base.winuniq.switched 中英齐备（toast 不许裸键名上生产）。
"""
from __future__ import annotations

import re
from pathlib import Path

TPL = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"

# JS 直跳精确 /workspace（不含 /workspace/xxx 子页——子页导航不产生第二个坐席）
_BARE_NAV = re.compile(r"location\.href\s*=\s*['\"]/workspace['\"]")

# 已接线的模板（workspace_base 的 SSE/铃铛兜底 + 四个带回跳的子页）。
# 刻意不含 unified_inbox.html：它本身就是坐席页（/workspace），其内部跳转
# 不构成「第二个坐席」，且该文件是多线活跃热区。
_GUARDED_TEMPLATES = [
    "workspace_base.html",
    "workspace_dashboard.html",
    "contact360.html",
    "queue_monitor.html",
    "escalation_log.html",
]

# 守卫允许出现在同行或其上 N 行内（if(__wsGoHome…) return; 换行式写法）
_GUARD_WINDOW = 3


def test_win_unique_exports_gohome():
    src = (TPL / "_win_unique.html").read_text(encoding="utf-8")
    # 兜底桩（降级环境 onclick/调用方不炸）
    assert "window.__wsGoHome = function(){ return false; };" in src
    # 真实现挂载
    assert "window.__wsGoHome = _goHome;" in src
    # BC 纯探活扩展（无深链参数也能判重）
    assert "allowPlain" in src
    # 委托点击拦截器（<a href="/workspace"> 零接线自动去重）
    assert re.search(r"document\.addEventListener\(\s*['\"]click['\"]", src), \
        "_win_unique.html 缺少委托点击拦截器——裸 <a href='/workspace'> 将重新漏网"


def test_frozen_tab_patient_probe_contract():
    """P2 冻结标签页补丁：耐心探活 + 跨文件心跳键契约。

    被冻结的坐席答不了 BC ping，但主控心跳还躺在 localStorage——
    `aitr.mw.primary::/workspace` 这个键名同时活在 __wsMultiWin（写方）与
    _win_unique（读方 _seatLikelyAlive），两边漂移＝探活永远误判「无坐席」。
    """
    wu = (TPL / "_win_unique.html").read_text(encoding="utf-8")
    wb = (TPL / "workspace_base.html").read_text(encoding="utf-8")
    key = "aitr.mw.primary::"
    assert key in wu, "_win_unique 缺心跳读取（_seatLikelyAlive）"
    assert key in wb, "workspace_base 缺主控心跳写入（__wsMultiWin K_PRIMARY）"
    assert "_seatLikelyAlive" in wu and "_probeSeat" in wu
    assert "waitMs" in wu, "耐心探活第二轮放宽窗口（waitMs）缺失"


def test_central_handoff_toast_single_render_path():
    """P2 中央交接提示：toast 单一渲染路径 + 自带提示页面的让行旗标。

    此前只有 cases 页监听 aitr:ws-handoff，其余入口页交接成功后静默
    （「点了像没反应」→ 用户再点＝窗口翻倍诱因）。中央监听收口后：
    _notifySwitched 必须只有定义 + 中央监听一个调用点（散落直调会双弹）；
    自带提示的页面必须置 __aitrWsHandoffToastOwned 声明让行。
    """
    wu = (TPL / "_win_unique.html").read_text(encoding="utf-8")
    assert re.search(r"addEventListener\(\s*['\"]aitr:ws-handoff['\"]", wu), \
        "_win_unique 缺中央 aitr:ws-handoff 监听"
    assert "__aitrWsHandoffToastOwned" in wu
    calls = re.findall(r"_notifySwitched\s*\(", wu)
    # 1 处定义（function _notifySwitched(）+ 1 处调用（中央监听内）
    assert len(calls) == 2, (
        f"_notifySwitched 出现 {len(calls)} 处——toast 必须单一渲染路径"
        "（新增直调会与中央监听双弹）")
    cases = (TPL / "cases.html").read_text(encoding="utf-8")
    if "aitr:ws-handoff" in cases:
        assert "__aitrWsHandoffToastOwned" in cases, \
            "cases.html 自带交接提示但未置让行旗标——中央 toast 会双弹"


def test_no_bare_home_navigation_in_workspace_templates():
    """凡 JS 直跳精确 /workspace，同行或紧邻上文必须带 __wsGoHome 守卫。

    新增一处裸 location.href='/workspace' ＝ 重新打开「辅助窗变第二个坐席」的洞，
    该在这里红。合法写法二选一：
      if(!(window.__wsGoHome&&window.__wsGoHome({cid:x}))) location.href='/workspace';
    或  if(window.__wsGoHome&&window.__wsGoHome({cid:x})) return;
        location.href='/workspace';
    """
    offenders = []
    for name in _GUARDED_TEMPLATES:
        path = TPL / name
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if not _BARE_NAV.search(line):
                continue
            lo = max(0, i - _GUARD_WINDOW)
            ctx = "\n".join(lines[lo:i + 1])
            if "__wsGoHome" not in ctx:
                offenders.append(f"{name}:{i + 1}: {line.strip()[:120]}")
    assert not offenders, (
        "发现未接 __wsGoHome 守卫的裸 /workspace 导航（会把当前窗口变成第二个坐席工作台）：\n"
        + "\n".join(offenders)
    )


def test_switched_toast_key_bilingual():
    from src.web.i18n_packs import inbox_workspace

    assert "base.winuniq.switched" in inbox_workspace.ZH
    assert "base.winuniq.switched" in inbox_workspace.EN
    assert inbox_workspace.ZH["base.winuniq.switched"].strip()
    assert inbox_workspace.EN["base.winuniq.switched"].strip()
