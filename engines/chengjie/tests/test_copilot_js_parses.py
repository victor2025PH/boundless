# -*- coding: utf-8 -*-
"""shared/copilot JS 语法门禁——node --check 全扫。

2026-08-01 实锤事故：sidebar-chrome.js 的块注释里写了 relationship_* / custom
的紧凑通配写法，「星号+斜杠」相邻提前终结了注释 → 整文件 SyntaxError →
浏览器端静默降级（各消费方 null 守卫兜住：折叠记忆 / tab 徽章 / 推荐映射
失效约 55 分钟无人察觉）。Python 门禁全体漏网——双树同步门禁只比对字节哈希、
i18n / 前端门禁不解析 JS；唯一能抓的 desktop npm test 不在默认收口清单里。

本门禁把「共享 JS 至少能被解析」变成 pytest 常驻项。node 缺失时 skip
（双树同步门禁保证两份内容一致，任一有 node 的环境跑过即覆盖）。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_TREE = _ROOT / "shared" / "copilot"

_NODE = shutil.which("node")


@pytest.mark.skipif(not _NODE, reason="node 不在 PATH（有 node 的环境跑过即覆盖）")
def test_all_shared_copilot_js_parse():
    assert _TREE.is_dir(), f"缺 {_TREE}"
    bad = []
    for p in sorted(_TREE.rglob("*.js")):
        r = subprocess.run([_NODE, "--check", str(p)],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            msg = (r.stderr or r.stdout or "").strip().splitlines()
            bad.append(f"{p.relative_to(_ROOT)}: {' | '.join(msg[:3])}")
    assert not bad, (
        "共享组件 JS 语法错误——整文件的导出/自定义元素注册会静默消失，"
        "消费方 null 守卫只会让功能悄悄退化而不是报错：\n  " + "\n  ".join(bad))
