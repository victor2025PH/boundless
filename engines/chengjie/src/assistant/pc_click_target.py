# -*- coding: utf-8 -*-
"""小智「视觉定位点击」编排（实施91 P3-B，2026-08-31）。

补齐「任意界面按描述点」——UIA 拿不到结构的纯画布/自绘控件（click_control 会
noinvoke/notfound）走这条：**截图(runner) → VLM grounding 换屏幕坐标(服务端) →
click_point(runner)**。

红线（方案 §4①）：LLM 只给**自然语言目标描述**（params.target），坐标由**服务端**从
真实截图经 Qwen3-VL 导出（[0,1000] 归一化，探针实测），**绝非 LLM 生成**；runner 侧
click_point 再经 _point_in_screen 越界兜底。任一步不确定即诚实失败，**绝不瞎点**
（fail-closed）。

seam 注入（inspect_fn/describe_fn）：门禁在无 runner/无 VLM 下测全链决策矩阵。
默认关（`assistant.pc_runner.vision.enabled: false`）——由路由把闸。
门禁 `tests/test_pc_click_target.py`。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from src.assistant import pc_vision

# grounding prompt：只描述目标，要中心点 JSON；不可见回 (-1,-1)。模型按自身习惯
# 回 [0,1000] 归一化（探针实测），pc_vision 负责换算——此处不指定 scale。
_GROUND_PROMPT = (
    "You are given a screenshot of a Windows application window. Find the single "
    "UI element the operator wants to click, described as: \"{target}\". Reply with "
    "ONLY a compact JSON object of its CENTER coordinates, e.g. {{\"x\": 512, "
    "\"y\": 344}}. If that element is not visible in the image, reply exactly "
    "{{\"x\": -1, \"y\": -1}}. No other text, no code fence."
)

# grounding 送图尺寸：大于常见窗宽即不缩放（坐标 [0,1000] 分辨率无关，缩放不影响
# 换算、只影响小目标清晰度）。1400 兼顾细节与 VLM 上下文。
_GROUND_IMAGE_DIM = 1400


def _default_describe(path: str, target: str, vision_cfg: Optional[dict],
                      global_vision: Optional[dict]) -> Optional[str]:
    """真 VLM 定位：VisionClient 打 grounding prompt，回文本。任何缺失/失败回 None
    （交编排层 fail-closed）。"""
    try:
        from src.vision_client import VisionClient
    except Exception:
        return None
    cfg = dict(vision_cfg or {})
    if not cfg:
        return None
    # 送大图保细节；坐标格式与分辨率无关，缩放不改换算
    cfg.setdefault("max_image_dim", _GROUND_IMAGE_DIM)
    try:
        vc = VisionClient(cfg)
        if not vc.initialize():
            return None
        return vc.describe_image_sync(
            path, prompt=_GROUND_PROMPT.format(target=str(target)[:200]))
    except Exception:
        return None


def resolve_and_click(
    machine: str, window: str, target: str, config: Any, actor: str = "", *,
    vision_cfg: Optional[dict] = None, global_vision: Optional[dict] = None,
    inspect_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    describe_fn: Optional[Callable[..., Optional[str]]] = None,
) -> Dict[str, Any]:
    """截图→VLM 定位→click_point 全链编排。返回 runner_client 风格 dict：
    成功 {ok:True, x, y, target, via:"vision"}；失败 {ok:False, error} —— error ∈
    screenshot_failed / screenshot_incomplete / vision_no_answer / not_located /
    click_failed。任一步不确定即失败，绝不瞎点。"""
    if inspect_fn is None:
        from src.assistant import runner_client
        inspect_fn = runner_client.inspect
    describe = describe_fn or _default_describe

    shot = inspect_fn(machine, "screenshot", {"window": window}, config, actor)
    if not shot.get("ok"):
        return {"ok": False, "error": "screenshot_failed",
                "detail": str(shot.get("error") or "")}
    w, h = shot.get("width"), shot.get("height")
    left, top = shot.get("left") or 0, shot.get("top") or 0
    path = shot.get("path")
    if not w or not h or not path:
        return {"ok": False, "error": "screenshot_incomplete"}

    text = describe(path, target, vision_cfg, global_vision)
    if not text:
        return {"ok": False, "error": "vision_no_answer"}

    pt = pc_vision.parse_ground_point(text)
    # 负坐标 = 模型明示「不可见」（见 _GROUND_PROMPT）→ 诚实未定位，绝不点
    if pt is None or pt[0] < 0 or pt[1] < 0:
        return {"ok": False, "error": "not_located", "target": target}
    ip = pc_vision.to_image_point(pt[0], pt[1], w, h)
    if ip is None:
        return {"ok": False, "error": "not_located", "target": target}
    screen = pc_vision.to_screen_point(ip[0], ip[1], left, top)
    if screen is None:
        return {"ok": False, "error": "not_located", "target": target}
    sx, sy = screen

    res = inspect_fn(machine, "click_point", {"x": sx, "y": sy}, config, actor)
    if not res.get("ok"):
        return {"ok": False, "error": "click_failed",
                "detail": str(res.get("error") or ""), "x": sx, "y": sy}
    return {"ok": True, "x": sx, "y": sy, "target": target, "via": "vision"}
