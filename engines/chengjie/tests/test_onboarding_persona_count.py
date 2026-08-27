"""首启向导人设计数（2026-08-21 内测实锤「当前已有 undefined 个人设」）钉。

根因：`/api/personas/profiles` 的 ``profiles`` 字段是 **id→档案字典**（数组形状
在 ``summary``/``ids``），向导旧代码对字典取 ``.length`` → undefined 漏进 UI。
钉两点：①向导消费 summary 数组优先+字典 Object.values 兜底；②不再对
``d.profiles`` 裸取 length。
"""
from pathlib import Path

_SRC = (Path(__file__).resolve().parents[1]
        / "src" / "web" / "templates" / "welcome.html"
        ).read_text(encoding="utf-8")


def test_wizard_persona_count_uses_array_shapes():
    i = _SRC.index("async function loadPersonas")
    body = _SRC[i:i + 1600]
    assert "Array.isArray(d.summary)" in body, "向导应优先消费 summary 数组"
    assert "Object.values(d.profiles)" in body, "字典形状必须 Object.values 兜底"
    assert "r.data && (r.data.profiles || r.data.items)" not in body, (
        "退回了把字典当数组的旧取法——「undefined 个人设」会回归")
    assert "ST.psCount ? 'ok'" in body or "list.length || 0" in body
