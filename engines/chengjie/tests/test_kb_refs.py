"""kb_refs.extract_kb_refs — 草稿证据链纯函数门禁。"""

from __future__ import annotations

from src.utils.kb_refs import extract_kb_refs


def test_extracts_basic_fields_and_snippet_priority():
    res = {"entries": [
        {"id": "e1", "title": "退款政策", "category": "售后",
         "example_reply_zh": "7 天内可退", "steps": "步骤A"},
        {"id": "e2", "title": "发货时效", "category": "物流", "steps": "48h 内发出"},
    ]}
    refs = extract_kb_refs(res)
    assert len(refs) == 2
    assert refs[0] == {"entry_id": "e1", "title": "退款政策",
                       "category": "售后", "snippet": "7 天内可退"}
    assert refs[1]["snippet"] == "48h 内发出"   # 无 example_reply → steps 顶上


def test_snippet_truncated_and_limit_respected():
    res = {"entries": [
        {"id": str(i), "title": f"T{i}", "example_reply_zh": "长" * 300}
        for i in range(5)
    ]}
    refs = extract_kb_refs(res, limit=3, max_snippet=50)
    assert len(refs) == 3
    assert refs[0]["snippet"].endswith("…") and len(refs[0]["snippet"]) == 51


def test_defensive_on_garbage():
    assert extract_kb_refs(None) == []
    assert extract_kb_refs({}) == []
    assert extract_kb_refs({"entries": ["not-a-dict", 42]}) == []
    # 无 title 无 snippet 的空壳条目剔除
    assert extract_kb_refs({"entries": [{"id": "x"}]}) == []
