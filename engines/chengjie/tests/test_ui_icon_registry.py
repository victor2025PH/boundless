"""全站「哑图标」门禁：图标名引用必须能在 ui_icons.js 注册表解析。

背景（2026-08-08 实锤）：ui_icons.js 是全站 uiIcon() 单一事实来源，但 unified_inbox.html
曾内联一份**同名不同集**的页内 uiIcon/_UIIC 覆盖全局——共享顶栏（workspace_base.html）
在收件箱页取到的是页内子集：shield（发前确认药丸）/pin（待跟进药丸）/bell（toast、
通知中心）等名字查不到 → 静默返回空串 → 坐席看到「只剩一个圆圈」的空药丸、
通知面板出彩色空圆。与「哑按钮」（定义了但没挂 window）同族：失败无声、线上才暴露。

三条不变量：
1. 【引用可解析】模板 / 静态 JS 里所有**字面量**图标名引用（uiIcon('x') / _pillIc('x') /
   data-ui-icon="x" / data-cp-ic="x" / iconName:'x'）必须存在于 ui_icons.js 的 P 表
   （或经 ALIAS 转发后存在）。动态名（变量 / Jinja 插值 / 拼接）不在静态判定范围，
   由 ui_icons.js 运行时 icon_miss 遥测（frontend-error 通道）兜底。
2 .【定义唯一】全站只允许 ui_icons.js 定义 uiIcon（模板再声明同名全局 = 覆盖库 →
   图标集分叉），unified_inbox 的历史副本已删除，本门禁防复发。
3. 【别名闭环】ALIAS 的每个目标名必须真实存在于 P 表（防转发到空处）。
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TPL_DIR = _ROOT / "src" / "web" / "templates"
_STATIC_DIR = _ROOT / "src" / "web" / "static"
_ICON_LIB = _STATIC_DIR / "ui_icons.js"

# —— 注册表解析 ————————————————————————————————————————————————
# P 表条目：值以 '< 开头（SVG 路径字符串）；键允许 foo / "foo-bar" 两种写法。
_P_ENTRY = re.compile(r"""^\s*(?:"([a-z][a-z0-9-]*)"|([a-z][a-z0-9-]*))\s*:\s*'<""", re.M)
_ALIAS_BLOCK = re.compile(r"var\s+ALIAS\s*=\s*\{(.*?)\};", re.S)
_ALIAS_ENTRY = re.compile(r'(?:"([a-z][a-z0-9-]*)"|([a-z][a-z0-9-]*))\s*:\s*"([a-z][a-z0-9-]*)"')


def _load_registry():
    src = _ICON_LIB.read_text(encoding="utf-8")
    names = {m.group(1) or m.group(2) for m in _P_ENTRY.finditer(src)}
    aliases = {}
    mb = _ALIAS_BLOCK.search(src)
    if mb:
        for m in _ALIAS_ENTRY.finditer(mb.group(1)):
            aliases[m.group(1) or m.group(2)] = m.group(3)
    return names, aliases


# —— 引用扫描 ————————————————————————————————————————————————
# 只抓**字面量**首参/属性值；变量、Jinja、拼接天然不匹配。
_REF_PATTERNS = [
    # uiIcon('x' / uiIcon("x"  （含 window.uiIcon）
    re.compile(r"""\buiIcon\(\s*(['"])([a-z][a-z0-9-]*)\1"""),
    # workspace_base 顶栏包装器：_pillIc('x') / _ui('x', n)
    re.compile(r"""\b_pillIc\(\s*(['"])([a-z][a-z0-9-]*)\1"""),
    re.compile(r"""\b_ui\(\s*(['"])([a-z][a-z0-9-]*)\1"""),
    # 声明式：data-ui-icon="x" / data-cp-ic="x"
    re.compile(r"""\bdata-ui-icon\s*=\s*(['"])([a-z][a-z0-9-]*)\1"""),
    re.compile(r"""\bdata-cp-ic\s*=\s*(['"])([a-z][a-z0-9-]*)\1"""),
    # 结构化元数据：iconName:'x'（通知中心 _TYPE_META、确认弹窗 opts）
    re.compile(r"""\biconName\s*:\s*(['"])([a-z][a-z0-9-]*)\1"""),
]

# UiIcons.svg 工厂之外自定义 uiIcon 的定义点（模板里再定义＝覆盖全局库，禁止）。
_DEF_PATTERNS = [
    re.compile(r"\bfunction\s+uiIcon\s*\("),
    # 赋值才算定义（== / === 比较不算）；库内 root.uiIcon = svg 不在扫描范围（文件被排除）。
    re.compile(r"\bwindow\.uiIcon\s*=(?!=)"),
    re.compile(r"\broot\.uiIcon\s*=(?!=)"),
]


def _scan_files():
    files = sorted(_TPL_DIR.rglob("*.html"))
    for p in sorted(_STATIC_DIR.rglob("*.js")):
        if p == _ICON_LIB:
            continue
        if "vendor" in p.parts:
            continue
        files.append(p)
    return files


def test_all_icon_name_references_resolve():
    names, aliases = _load_registry()
    assert names, "ui_icons.js 注册表解析为空——P 表格式变了请同步更新本门禁的解析正则"
    missing = {}
    for f in _scan_files():
        text = f.read_text(encoding="utf-8", errors="replace")
        bad = set()
        for pat in _REF_PATTERNS:
            for m in pat.finditer(text):
                name = m.group(2)
                resolved = aliases.get(name, name)
                if resolved not in names:
                    bad.add(name)
        if bad:
            missing[str(f.relative_to(_ROOT))] = sorted(bad)
    assert not missing, (
        "发现**哑图标**引用（名字不在 ui_icons.js 注册表 → uiIcon 返回空串 → 界面出空位/空药丸）：\n"
        + "\n".join(f"  {k}: {v}" for k, v in missing.items())
        + "\n修法：往 src/web/static/ui_icons.js 的 P 表补图标（24x24 线性 stroke），"
        "或把调用点改为已有名字；历史短名走 ALIAS 转发，不要在模板里再建图标表。"
    )


def test_uiicon_defined_only_in_library():
    offenders = []
    for f in _scan_files():
        text = f.read_text(encoding="utf-8", errors="replace")
        for pat in _DEF_PATTERNS:
            if pat.search(text):
                offenders.append(str(f.relative_to(_ROOT)))
                break
    assert not offenders, (
        "以下文件自定义了 uiIcon（会覆盖 /static/ui_icons.js 的全局库 → 图标集分叉，"
        "共享顶栏/toast/通知中心在该页图标静默变空——2026-08-08 unified_inbox 实锤事故）：\n"
        + "\n".join(f"  {p}" for p in offenders)
        + "\n修法：删除页内副本，图标进 ui_icons.js 的 P 表统一维护。"
    )


def test_alias_targets_exist():
    names, aliases = _load_registry()
    dangling = {k: v for k, v in aliases.items() if v not in names}
    assert not dangling, f"ALIAS 指向不存在的图标名：{dangling}"


def test_registry_names_are_kebab_ascii():
    """图标名恒为小写 kebab-case ASCII（library 头部声明的不变量，防 CJK/大写混入）。"""
    names, aliases = _load_registry()
    bad = [n for n in list(names) + list(aliases) if not re.fullmatch(r"[a-z][a-z0-9-]*", n)]
    assert not bad, f"图标名不符合小写 kebab-case：{bad}"


# —— P2B（2026-08-08）：sidebar-chrome 回落子集表 = 库的逐字节拷贝 ——————————————
# 副驾组件自包含约束（独立 iframe / 桌面壳可能不载 /static/ui_icons.js）→ sidebar-chrome
# 内置回落子集表。两份美术一旦漂移，同一图标网页/桌面长得不一样——子集必须严格 ⊆ 库。
_SIDEBAR_CHROME = _ROOT / "shared" / "copilot" / "sidebar-chrome.js"


def _load_sidebar_subset():
    src = _SIDEBAR_CHROME.read_text(encoding="utf-8")
    mb = re.search(r"var\s+UI_ICONS\s*=\s*\{(.*?)\n  \};", src, re.S)
    assert mb, "sidebar-chrome.js 的 UI_ICONS 表解析失败——结构变了请同步本门禁"
    entries = {}
    for m in re.finditer(
        r"""(?:"([a-z][a-z0-9-]*)"|([a-z][a-z0-9-]*))\s*:\s*'(<.*?)'(?:,|\s*$)""",
        mb.group(1), re.M,
    ):
        entries[m.group(1) or m.group(2)] = m.group(3)
    return entries


def _load_library_arts():
    src = _ICON_LIB.read_text(encoding="utf-8")
    arts = {}
    for m in re.finditer(
        r"""^\s*(?:"([a-z][a-z0-9-]*)"|([a-z][a-z0-9-]*))\s*:\s*'(<.*?)',?\s*$""",
        src, re.M,
    ):
        arts[m.group(1) or m.group(2)] = m.group(3)
    return arts


def test_sidebar_chrome_subset_matches_library():
    subset = _load_sidebar_subset()
    assert subset, "sidebar-chrome UI_ICONS 子集为空——解析失败或表被删除"
    arts = _load_library_arts()
    missing = sorted(n for n in subset if n not in arts)
    assert not missing, (
        f"sidebar-chrome 子集表存在库里没有的图标名：{missing}\n"
        "新图标先进 src/web/static/ui_icons.js，再拷贝进子集表。"
    )
    drifted = sorted(n for n in subset if subset[n] != arts[n])
    assert not drifted, (
        f"sidebar-chrome 子集表与库的美术**漂移**（同名不同形，网页/桌面会长得不一样）：{drifted}\n"
        "修法：把 ui_icons.js 里对应条目原样拷贝进 sidebar-chrome.js 的 UI_ICONS。"
    )
