# -*- coding: utf-8 -*-
"""CSS 语义 token 引用完整性门禁（2026-08-10 接入弹窗「暗色看不清」事故沉淀）。

事故机制：模板 / CSS / 前端 JS 里写 ``var(--tk-amber-ink, #b45309)`` 这类**带亮色
回落值**的引用，而该 token 从未在任何主题块里定义 → 变量恒走回落值：亮色恰好
正确、**暗色下 ≈3:1 直接不可读**，且不报错不变红，只能靠人眼在暗色下撞见
（``--tk-amber-ink`` 在接入弹窗风险横幅 / 断线横幅 / AI 草稿提示 / 账号卡 stall
提示等 9+ 处踩过，修复＝workspace_base 补别名指向 ``--tk-warn-ink``）。

门禁不变量：``src/web`` 下模板、静态 CSS 与静态 JS 引用的 ``--tk-*`` / ``--bdg-*``
语义 token（这两族按约定必须在主题块里亮暗各有效，见
``.cursor/rules/frontend-theme.mdc``）必须在扫描范围内**至少有一处定义**。

已知边界（刻意接受）：定义按全局并集判，不建模 Jinja extends 继承链——
「A 页定义、B 页引用但 B 不继承 A」这种错位抓不到；本门禁的目标是掐灭
「全站零定义、恒走回落」这一类（正是事故形态），零假阳性优先于覆盖。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Set

SRC_WEB = Path(__file__).resolve().parents[1] / "src" / "web"

_REF_RE = re.compile(r"var\(\s*(--(?:tk|bdg)-[a-zA-Z0-9_-]+)")
_DEF_RE = re.compile(r"(--(?:tk|bdg)-[a-zA-Z0-9_-]+)\s*:")

# ---------------------------------------------------------------------------
# 全族扫描（2026-08-20 实施49 P1-7：浅色主题审计）
#
# 上面那条门禁只守 --tk-/--bdg- 两族，而 2026-08-19「亮色下右键菜单黑底黑字」
# 事故引用的是 --card-bg/--card-bd/--th-hover/--th-accent，全都不在两族里，
# 门禁一句话都没说。本批把口径扩到**所有** CSS 自定义属性：凡被 var() 引用却
# 全站零定义者，要么是运行时注入（列 _RUNTIME_SET，且必须真有 setProperty 写点），
# 要么是注释里的示意写法（_PROSE_ARTIFACTS），否则就是「恒走回落值」的主题漂移
# 债务（_PENDING_UNDEFINED，附页面与症状，修一个删一条）。
# ---------------------------------------------------------------------------
_ANY_REF_RE = re.compile(r"var\(\s*(--[a-zA-Z0-9_-]+)")
_ANY_DEF_RE = re.compile(r"(--[a-zA-Z0-9_-]+)\s*:")
_SETPROP_RE = re.compile(r"setProperty\(\s*['\"](--[a-zA-Z0-9_-]+)")

#: 运行时由 JS 写入元素 style 的 token —— CSS 里没有静态定义是正确的。
_RUNTIME_SET: Dict[str, str] = {
    "--tip-arrow-y": "unified_inbox.html 侧栏 tip 箭头对准图标中心，按位置逐次写入",
    "--lvl": "voice_call.html 麦克风电平，每帧写入驱动光球动画",
    "--bbl-fs": "appearance.js 气泡外观自定义（字号）",
    "--bbl-radius": "appearance.js 气泡外观自定义（圆角）",
    "--bbl-in-bg": "appearance.js 气泡外观自定义（入站底色）",
    "--bbl-in-fg": "appearance.js 气泡外观自定义（入站字色，按底色算最佳墨色）",
    "--bbl-out-bg": "appearance.js 气泡外观自定义（出站底色）",
    "--bbl-out-fg": "appearance.js 气泡外观自定义（出站字色）",
    "--chat-wall": "appearance.js 聊天背景（未选背景时 removeProperty，故只能有回落值）",
}

#: 出现在**注释/说明文字**里的示意写法，不是真实引用（扫描器不解析注释，故显式登记）。
_PROSE_ARTIFACTS: Dict[str, str] = {
    "--chrome-": "workspace_base.html 注释里描述 chrome 族消费约定的通配写法",
    "--th-": "agent_perf.html / rpa_overview.html 注释里描述 th 族迁移层的通配写法",
    "--token": "ops_overview.html 注释里解释 SVG presentation attribute 不解析变量",
}

#: 真债务：引用了全站没定义的 token → 恒走回落值（无回落值时该属性直接不生效）。
#: P1-7 全站审计的账本；修掉一个删一条，别在这里堆新的。
_PENDING_UNDEFINED: Dict[str, str] = {
    # 2026-08-20 实施49 P1-7 第二批已清空本账：
    #   --bg2/--chip-bg/--ok/--ok-bg/--warn-bg/--emerald/--cyan → base.html 亮暗两块补定义
    #     （能接现有语义的接成别名，只有 --bg2/--cyan 给字面值）；
    #   --th-bg-teal6 → 补进 inline_color_codemod 映射表并重生成 theme-tokens.css
    #     （那是该文件的单源，勿手改 CSS）；
    #   --mt/--tx/--d → 引用侧手滑（全站从没有过这三个名字），就地换成本壳既有词汇
    #     --t3/--t/--red。**无回落值的引用是最高优先级**：那不是配色偏差，是整条
    #     声明失效（teal6 那处图标底恒透明、--tx 那处按钮字色不生效）。
    # 新增条目必须写清「哪一页、有没有回落值、症状是什么」，否则无法排优先级。
}

#: 刻意允许「引用了但全站未定义」的 token → 原因（新增必须附原因；理想恒空）。
_ALLOWED_UNDEFINED: Dict[str, str] = {}


def _scan_files() -> List[Path]:
    files = list((SRC_WEB / "templates").rglob("*.html"))
    files += list((SRC_WEB / "static").rglob("*.css"))
    files += list((SRC_WEB / "static").rglob("*.js"))
    return files


def _collect() -> tuple[Dict[str, List[str]], Set[str]]:
    refs: Dict[str, List[str]] = {}
    defs: Set[str] = set()
    for fp in _scan_files():
        text = fp.read_text(encoding="utf-8", errors="ignore")
        for m in _REF_RE.finditer(text):
            refs.setdefault(m.group(1), []).append(fp.name)
        for m in _DEF_RE.finditer(text):
            defs.add(m.group(1))
    return refs, defs


def test_semantic_token_refs_all_defined():
    refs, defs = _collect()
    undefined = {
        name: sorted(set(where))[:4]
        for name, where in refs.items()
        if name not in defs and name not in _ALLOWED_UNDEFINED
    }
    assert not undefined, (
        "以下语义 token 被引用但全站零定义（暗色下恒走亮色回落值=不可读；"
        "请在 workspace_base / 页面主题块补定义或别名，勿直接加豁免）：\n"
        + "\n".join(f"  {k}  <-  {v}" for k, v in sorted(undefined.items()))
    )


def test_allowlist_not_stale():
    # 豁免表里的 token 一旦真的有了定义，必须把豁免拆掉（防豁免表变垃圾场）。
    _, defs = _collect()
    stale = [k for k in _ALLOWED_UNDEFINED if k in defs]
    assert not stale, f"豁免表过期（token 已有定义，请移除豁免）：{stale}"


def _collect_any() -> tuple[Dict[str, List[str]], Set[str], Set[str]]:
    refs: Dict[str, List[str]] = {}
    defs: Set[str] = set()
    setprops: Set[str] = set()
    for fp in _scan_files():
        text = fp.read_text(encoding="utf-8", errors="ignore")
        for m in _ANY_REF_RE.finditer(text):
            refs.setdefault(m.group(1), []).append(fp.name)
        for m in _ANY_DEF_RE.finditer(text):
            defs.add(m.group(1))
        for m in _SETPROP_RE.finditer(text):
            setprops.add(m.group(1))
    return refs, defs, setprops


def test_all_css_token_refs_accounted_for():
    """任何 var(--x) 引用要么有定义，要么在三本账里之一（各有语义）。"""
    refs, defs, _ = _collect_any()
    known = set(_RUNTIME_SET) | set(_PROSE_ARTIFACTS) | set(_PENDING_UNDEFINED)
    orphan = {
        name: sorted(set(where))[:4]
        for name, where in refs.items()
        if name not in defs and name not in known
    }
    assert not orphan, (
        "以下 CSS 变量被引用但全站零定义（恒走回落值＝主题不跟随；无回落值时属性直接不生效）。"
        "请补定义，或按语义登记进 _RUNTIME_SET / _PROSE_ARTIFACTS / _PENDING_UNDEFINED：\n"
        + "\n".join(f"  {k}  <-  {v}" for k, v in sorted(orphan.items()))
    )


def test_runtime_set_tokens_have_write_site():
    """_RUNTIME_SET 的理由必须是真的——每个都得找得到 setProperty 写点。"""
    _, _, setprops = _collect_any()
    missing = sorted(t for t in _RUNTIME_SET if t not in setprops)
    assert not missing, (
        f"登记为「运行时注入」但全站没有 setProperty 写点，说明它其实是漏定义：{missing}"
    )


def test_any_ledgers_not_stale():
    """三本账里的 token 一旦真有了定义，必须销账（防账本变垃圾场）。"""
    _, defs, _ = _collect_any()
    stale = sorted(
        t for t in (set(_PROSE_ARTIFACTS) | set(_PENDING_UNDEFINED)) if t in defs
    )
    assert not stale, f"账本过期（token 已有定义，请从账本删除）：{stale}"


def test_inbox_overlay_tokens_defined_in_both_themes():
    """P1-7 修复锚：收件箱弹层/菜单族的六个 token 必须亮暗各有值。

    掉任何一档就退回 B7 那个形态——回落值各写各的，一档正常另一档断层。
    """
    html = (SRC_WEB / "templates" / "unified_inbox.html").read_text(encoding="utf-8")
    dark_at = html.find(':root[data-cp-theme="dark"]')
    assert dark_at > 0, "unified_inbox.html 找不到暗色主题块"
    light, dark = html[:dark_at], html[dark_at:]
    # --border-soft / --text-dim 是别名（指向同族 token），随宿主自动翻转，故只查亮档。
    for token in ("--bg-hover", "--line-soft", "--text-med", "--ink-slate5"):
        assert re.search(rf"{token}\s*:", light), f"亮色块缺 {token}"
        assert re.search(rf"{token}\s*:", dark), f"暗色块缺 {token}"
    for token in ("--border-soft", "--text-dim"):
        assert re.search(rf"{token}\s*:\s*var\(", light), f"{token} 应定义为同族别名"


def test_acct_more_menu_warn_uses_token():
    """B7 断层点：账号「更多操作」菜单的橙色项不得退回硬编码 #d97706（白底仅 3.4:1）。"""
    css = (SRC_WEB / "static" / "workspace" / "unified-inbox.css").read_text(
        encoding="utf-8"
    )
    rules = [
        (sel, body)
        for sel, body in re.findall(r"([^{}\n]*\.acct-menu-item\.warn[^{}\n]*)\{([^}]*)\}", css)
        # 只看规则本身（含 :hover/:focus），跳过 `.warn .ui-ic` 这类后代选择器
        if ".acct-menu-item.warn " not in sel
    ]
    assert rules, "找不到 .acct-menu-item.warn 规则"
    for sel, body in rules:
        assert "--tk-warn-ink" in body, f"{sel.strip()} 的橙色未走 token：{body}"


def test_amber_ink_alias_pinned():
    # 本次事故的修复锚：--tk-amber-ink 必须保持定义为 --tk-warn-ink 的别名
    # （workspace_base 主题块）。谁删了别名，全站 9+ 处旧引用立刻回到暗色不可读。
    base = (SRC_WEB / "templates" / "workspace_base.html").read_text(encoding="utf-8")
    assert re.search(r"--tk-amber-ink\s*:\s*var\(\s*--tk-warn-ink\s*\)", base), (
        "workspace_base.html 缺少 --tk-amber-ink → --tk-warn-ink 兼容别名"
    )
