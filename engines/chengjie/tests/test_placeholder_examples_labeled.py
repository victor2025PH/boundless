# -*- coding: utf-8 -*-
"""示例型占位符必须带「例：/e.g.」标识（B20 病类门禁，2026-08-21）。

事故：内测用户把示例占位文字当成**生效配置**报障（B20：「telegram:default」
「zh-CN-XiaoxiaoNeural」两处被当真值）。根治=示例值与生效值视觉可区分。

刻意窄口径（精度>覆盖）：只扫**高置信示例签名**的 placeholder——
音色名（zh-CN-*）、账号选择器（telegram:123 / platform:telegram /
whatsapp:456）。普通提示语占位（「输入问题…」）不属示例型，不扫。
新增同签名占位时带上「例：」（zh）/「e.g.」（en）即绿。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
PACKS = Path(__file__).resolve().parents[1] / "src" / "web" / "i18n_packs"

# 高置信「这是示例值」签名（都来自真实事故/真实占位面）
_EXAMPLE_SIG = re.compile(
    r"(zh-CN-[A-Za-z]|telegram:\d|platform:telegram|whatsapp:\d)")
_LABEL = re.compile(r"(例[：:]|e\.g\.)", re.IGNORECASE)

_PLACEHOLDER_ATTR = re.compile(r'placeholder="([^"]*)"')


def _violations_in(text: str) -> list[str]:
    out = []
    for m in _PLACEHOLDER_ATTR.finditer(text):
        val = m.group(1)
        if _EXAMPLE_SIG.search(val) and not _LABEL.search(val):
            out.append(val[:80])
    return out


def test_template_example_placeholders_labeled():
    bad: list[str] = []
    for fp in TEMPLATES.rglob("*.html"):
        text = fp.read_text(encoding="utf-8", errors="replace")
        for v in _violations_in(text):
            bad.append(f"{fp.name}: {v}")
    assert not bad, (
        "示例型占位符缺「例：/e.g.」标识（用户会把示例当生效配置，B20 实锤）：\n"
        + "\n".join("  - " + b for b in bad))


def test_pack_example_placeholder_values_labeled():
    """i18n pack 里的 *_ph 键若含示例签名，也必须带标识（运行时以 pack 为准，
    只查模板兜底会漏真值）。"""
    bad: list[str] = []
    for fp in PACKS.glob("*.py"):
        text = fp.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r'"([a-z0-9_.]+_ph)":\s*"([^"]*)"', text):
            key, val = m.group(1), m.group(2)
            if _EXAMPLE_SIG.search(val) and not _LABEL.search(val):
                bad.append(f"{fp.name}:{key}: {val[:70]}")
    assert not bad, (
        "pack 占位值缺「例：/e.g.」标识：\n"
        + "\n".join("  - " + b for b in bad))
