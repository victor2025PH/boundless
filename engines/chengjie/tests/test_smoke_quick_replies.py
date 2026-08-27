# -*- coding: utf-8 -*-
"""快捷回复链冒烟工具的判词门禁（纯函数，零网络零实例）。

钉住 tools/smoke_quick_replies.py 的判定口径：
- LOADED / RIDES_RESTART / BROKEN 三态语义（等重启≠故障，半装载=点名）；
- 机器键名泄漏判据与 curation 门禁同一正则口径；
- 只有 BROKEN 进非零退出。
"""

import sys
from pathlib import Path

_ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(_ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(_ENGINE_ROOT))

from tools.smoke_quick_replies import (  # noqa: E402
    machine_label_leaks,
    overall_exit,
    verdict_p0,
    verdict_p1,
    verdict_v0,
    verdict_v1,
)


def _tpl(ok=True, rows=None, can_edit=None):
    d = {"ok": ok, "templates": rows or []}
    if can_edit is not None:
        d["can_edit"] = can_edit
    return d


def test_machine_label_leaks_same_bar_as_curation_gate():
    rows = [{"label": "问候语"}, {"label": "greeting #2"}, {"label": "order_query"},
            {"label": "问候语 · 备选2"}]
    assert machine_label_leaks(rows) == ["greeting #2", "order_query"]


def test_p0_verdicts():
    ok_rows = [{"label": "问候语", "key": "greeting"}]
    assert verdict_p0(_tpl(rows=ok_rows))[0] == "LOADED"
    v, r = verdict_p0(_tpl(rows=[{"label": "greeting", "key": "greeting"}]))
    assert v == "BROKEN" and "泄漏" in r[0]
    v, _ = verdict_p0(_tpl(rows=[{"label": "自检", "key": "test"}]))
    assert v == "BROKEN"
    assert verdict_p0(None)[0] == "BROKEN"


def test_p1_verdicts_four_states():
    # 全在 = LOADED
    assert verdict_p1(_tpl(can_edit=False), 200)[0] == "LOADED"
    # 全不在 = 等重启（不是故障）
    assert verdict_p1(_tpl(), 404)[0] == "RIDES_RESTART"
    # 半装载 = 点名 BROKEN（不该出现的状态）
    assert verdict_p1(_tpl(can_edit=True), 404)[0] == "BROKEN"
    assert verdict_p1(_tpl(), 200)[0] == "BROKEN"


def test_v0_verdicts_openapi_is_hard_evidence():
    # 形参在 + 响应健康 = LOADED（零译稿/零命中也判得出——首跑误报的修复钉）
    assert verdict_v0({"ok": True, "entries": []}, True)[0] == "LOADED"
    assert verdict_v0({"ok": False, "entries": [], "error": "kb_unavailable"}, True)[0] == "LOADED"
    # 形参在但响应坏 = BROKEN
    assert verdict_v0(None, True)[0] == "BROKEN"
    # 形参不在 = 未装载
    assert verdict_v0({"ok": True, "entries": []}, False)[0] == "RIDES_RESTART"
    # openapi 不可得：条目级语言标记能证已装载；否则保守按未装载
    rows = {"ok": True, "entries": [{"answer": "x", "lang": "en"}]}
    assert verdict_v0(rows, None)[0] == "LOADED"
    assert verdict_v0({"ok": True, "entries": []}, None)[0] == "RIDES_RESTART"


def test_openapi_param_present():
    from tools.smoke_quick_replies import openapi_param_present
    doc = {"paths": {"/api/unified-inbox/kb-search": {
        "get": {"parameters": [{"name": "q"}, {"name": "lang"}]}}}}
    assert openapi_param_present(doc, "/api/unified-inbox/kb-search", "lang") is True
    assert openapi_param_present(doc, "/api/unified-inbox/kb-search", "nope") is False
    assert openapi_param_present(doc, "/missing", "lang") is False
    assert openapi_param_present(None, "/x", "lang") is None


def test_v1_verdicts():
    rows_with = [{"label": "问候语", "i18n": {"en": {"text": "Hey", "approved": True}}}]
    assert verdict_v1(_tpl(rows=rows_with), [])[0] == "LOADED"
    v, r = verdict_v1(_tpl(rows=[{"label": "问候语"}]), ["x/config/templates_i18n.yaml"])
    assert v == "RIDES_RESTART" and "聚合未装载" in r[0]
    v, r = verdict_v1(_tpl(rows=[{"label": "问候语"}]), [])
    assert v == "RIDES_RESTART" and "未播种" in r[0]


def test_overall_exit_only_broken_nonzero():
    assert overall_exit({"a": "LOADED", "b": "RIDES_RESTART"}) == 0
    assert overall_exit({"a": "LOADED", "b": "BROKEN"}) == 2
