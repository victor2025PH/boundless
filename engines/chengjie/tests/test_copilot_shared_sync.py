# -*- coding: utf-8 -*-
"""两端共享 copilot 组件库同步门禁（E2，GOALS_UI_REVAMP_PLAN 配套工程项）。

``shared/copilot/``（web 经 admin.py 挂 ``/copilot`` 静态服务）与
``desktop/renderer/shared/copilot/``（桌面壳打包用）是**同一份组件库的两份拷贝**，
靠人肉「改完记得拷过去」同步——git status 里两份长期同时 M，漂移只在桌面用户
报障时才暴露（web 修了的 bug 桌面还在，或反之）。

三个不变量：
1. 两树文件集合完全相同（不许单边新增/删除）；
2. 同名文件逐字节一致（改 web 份必须同步桌面份）;
3. 宿主模板（unified_inbox.html）引用的 ``/copilot/*.js`` 都真实存在
   （防改名/删文件后 script 404 → 组件静默不渲染）。

修法（红了照做）：以 ``shared/copilot`` 为编辑源，整份拷到
``desktop/renderer/shared/copilot``，并 bump 宿主 ``?v=`` 缓存参。
"""

import hashlib
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
WEB_TREE = _ROOT / "shared" / "copilot"
DESKTOP_TREE = _ROOT / "desktop" / "renderer" / "shared" / "copilot"
INBOX_TPL = _ROOT / "src" / "web" / "templates" / "unified_inbox.html"


def _tree_files(root: Path):
    return {p.relative_to(root).as_posix(): p
            for p in root.rglob("*") if p.is_file()}


def _sha1(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()


def test_trees_exist():
    assert WEB_TREE.is_dir(), f"缺 {WEB_TREE}（web 共享组件库）"
    assert DESKTOP_TREE.is_dir(), f"缺 {DESKTOP_TREE}（桌面壳拷贝）"


def test_same_file_sets():
    """文件集合一致：单边新增/删除即红（漂移最常见形态是「web 加了新组件忘拷」）。"""
    web = set(_tree_files(WEB_TREE))
    desk = set(_tree_files(DESKTOP_TREE))
    only_web = sorted(web - desk)
    only_desk = sorted(desk - web)
    assert not only_web and not only_desk, (
        "两端组件库文件集合不一致——把 shared/copilot 整份拷到 "
        "desktop/renderer/shared/copilot：\n"
        f"  只在 web:    {only_web}\n  只在桌面: {only_desk}")


def test_same_bytes():
    """同名文件逐字节一致（唯一修法＝从 shared/copilot 拷贝覆盖桌面份）。"""
    web = _tree_files(WEB_TREE)
    desk = _tree_files(DESKTOP_TREE)
    drifted = [rel for rel in sorted(set(web) & set(desk))
               if _sha1(web[rel]) != _sha1(desk[rel])]
    assert not drifted, (
        "两端组件库内容漂移（web 与桌面不同字节）——以 shared/copilot 为准"
        "整份拷贝到 desktop/renderer/shared/copilot，并 bump 宿主 ?v= 缓存参：\n  "
        + "\n  ".join(drifted))


def test_inbox_script_srcs_resolve():
    """宿主引用的 /copilot/*.js 必须真实存在（改名/删除后 404=组件静默消失）。"""
    html = INBOX_TPL.read_text(encoding="utf-8")
    srcs = re.findall(r'src="/copilot/([^"?]+)(?:\?[^"]*)?"', html)
    assert srcs, "unified_inbox.html 未引用任何 /copilot 资源（结构变了？更新本门禁）"
    missing = [s for s in sorted(set(srcs)) if not (WEB_TREE / s).is_file()]
    assert not missing, f"宿主引用了不存在的共享组件文件: {missing}"


def test_component_registry_covers_goal():
    """cp-goal 必须仍在两端组件清单里（本门禁因它而立，防有人「顺手清理」）。"""
    for tree in (WEB_TREE, DESKTOP_TREE):
        assert (tree / "components" / "cp-goal.js").is_file(), \
            f"{tree} 缺 components/cp-goal.js"
