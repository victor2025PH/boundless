# -*- coding: utf-8 -*-
"""视觉定位点击编排门禁（实施91 P3-B，2026-08-31）。

seam 注入（inspect_fn/describe_fn）测全链决策矩阵，无 runner/无 VLM：
截图失败/不全/VLM 空答/未定位(含模型明示 -1,-1)/点击失败/成功换算+越界兜底。
坐标由 VLM 从真实截图导出（[0,1000] 归一化，探针实测）→ pc_vision 换算 → click_point，
**绝非 LLM 生成**；任一步不确定即诚实失败绝不瞎点（fail-closed）。
"""
from src.assistant import pc_click_target as pct


def _mk_inspect(shot, click=None, calls=None):
    """假 runner_client.inspect：screenshot 回 shot，click_point 回 click（默认回显 ok）。"""
    def _fn(machine, tool, args, config, actor=""):
        if calls is not None:
            calls.append((tool, dict(args or {})))
        if tool == "screenshot":
            return shot
        if tool == "click_point":
            return click or {"ok": True, "x": args["x"], "y": args["y"]}
        return {"ok": False, "error": "unexpected_tool:" + tool}
    return _fn


_SHOT_OK = {"ok": True, "path": "C:/tmp/s.png",
            "width": 1280, "height": 800, "left": 100, "top": 50}


def test_happy_path_screenshot_ground_click():
    calls = []
    inspect = _mk_inspect(_SHOT_OK, calls=calls)
    # [0,1000]: x500→640, y250→200；+窗原点(100,50) → 屏幕 (740,250)
    res = pct.resolve_and_click(
        "zhuji", "画图", "蓝色按钮", {}, "actor",
        vision_cfg={"provider": "openai_compatible"},
        inspect_fn=inspect, describe_fn=lambda *a: '{"x": 500, "y": 250}')
    assert res["ok"] and res["via"] == "vision"
    assert res["x"] == 740 and res["y"] == 250
    # 先截图后点击，且点的是换算后的屏幕坐标
    assert calls[0][0] == "screenshot"
    assert calls[1] == ("click_point", {"x": 740, "y": 250})


def test_screenshot_failed_short_circuits():
    calls = []
    inspect = _mk_inspect({"ok": False, "error": "offline"}, calls=calls)
    res = pct.resolve_and_click("zhuji", "画图", "按钮", {}, "a",
                                inspect_fn=inspect,
                                describe_fn=lambda *a: '{"x":1,"y":1}')
    assert not res["ok"] and res["error"] == "screenshot_failed"
    assert all(t != "click_point" for t, _ in calls)  # 绝不点


def test_screenshot_incomplete_no_dims():
    inspect = _mk_inspect({"ok": True, "path": "x.png"})  # 缺 width/height
    res = pct.resolve_and_click("zhuji", "画图", "按钮", {}, "a",
                                inspect_fn=inspect,
                                describe_fn=lambda *a: '{"x":1,"y":1}')
    assert not res["ok"] and res["error"] == "screenshot_incomplete"


def test_vision_no_answer():
    calls = []
    inspect = _mk_inspect(_SHOT_OK, calls=calls)
    res = pct.resolve_and_click("zhuji", "画图", "按钮", {}, "a",
                                inspect_fn=inspect, describe_fn=lambda *a: None)
    assert not res["ok"] and res["error"] == "vision_no_answer"
    assert all(t != "click_point" for t, _ in calls)


def test_not_located_on_negative_coords():
    # 模型明示不可见 (-1,-1) → 诚实未定位，绝不点
    calls = []
    inspect = _mk_inspect(_SHOT_OK, calls=calls)
    res = pct.resolve_and_click("zhuji", "画图", "不存在的东西", {}, "a",
                                inspect_fn=inspect,
                                describe_fn=lambda *a: '{"x": -1, "y": -1}')
    assert not res["ok"] and res["error"] == "not_located"
    assert res["target"] == "不存在的东西"
    assert all(t != "click_point" for t, _ in calls)


def test_not_located_on_unparseable():
    inspect = _mk_inspect(_SHOT_OK)
    res = pct.resolve_and_click("zhuji", "画图", "按钮", {}, "a",
                                inspect_fn=inspect,
                                describe_fn=lambda *a: "I cannot find it.")
    assert not res["ok"] and res["error"] == "not_located"


def test_click_failed_propagates():
    inspect = _mk_inspect(_SHOT_OK,
                          click={"ok": False, "error": "click_offscreen"})
    res = pct.resolve_and_click("zhuji", "画图", "按钮", {}, "a",
                                inspect_fn=inspect,
                                describe_fn=lambda *a: '{"x": 500, "y": 250}')
    assert not res["ok"] and res["error"] == "click_failed"
    assert res["x"] == 740 and res["y"] == 250  # 换算对，只是点没成
