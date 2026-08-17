"""出站前「图文核对」：配文声称的东西，画面里到底有没有（P2，2026-08-08）。

背景（实录事故）：配文由 LLM **在看不见图的情况下**按对话上下文写出来，于是
自拍被配上「给你瞅瞅我卧室的样子」「之前拍的存货，家里随便吃吃嘛」——客户看到
的是一张人脸照，文字却在说房间/食物。P0（配文指令钉死「这是你的人像照」）与
P1（无货就不发图）从源头减少这类产出，本模块是**最后一道出站防线**：真让 VLM
看一眼图，只拦「配文明确声称画面里有 X，而画面里根本没有 X」这一类高置信违和。

设计口径与 :mod:`src.ai.image_gate` 一致：
- **软失败一律放行**（无 vision 配置/初始化失败/超时/解析失败）——核对是增益，
  绝不能因为一次 VLM 抖动就把已经生成好的图卡住不发；
- **只拒 VLM 明确说不匹配**（``caption_claims_match is False``）；``null``/不确定
  一律放行（分类器没把握不误伤）；
- 拒了不丢图：调用方把配文换成不声称画面内容的诚实兜底文案再发（图本身没问题，
  错的是那句话）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SEC = 12.0

_STATS: Dict[str, Any] = {
    "checked": 0, "passed": 0, "rewritten": 0, "soft_pass": 0,
    "last_reason": "", "last_caption": "", "last_ts": 0.0,
    "soft_reasons": {},
}
_STATS_LOCK = threading.Lock()


def _record(kind: str, reason: str = "", caption: str = "") -> None:
    with _STATS_LOCK:
        if kind in _STATS:
            _STATS[kind] = int(_STATS[kind]) + 1
        if kind == "soft_pass" and reason:
            b = _STATS.setdefault("soft_reasons", {})
            b[reason] = int(b.get(reason, 0)) + 1
        if reason:
            _STATS["last_reason"] = str(reason)[:60]
        if caption:
            _STATS["last_caption"] = str(caption)[:80]
        _STATS["last_ts"] = time.time()


def stats_snapshot() -> Dict[str, Any]:
    with _STATS_LOCK:
        snap = dict(_STATS)
        snap["soft_reasons"] = dict(_STATS.get("soft_reasons") or {})
    return snap


def resolve_caption_guard_cfg(scfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """取 ``companion.selfie.caption_guard``（新子系统约定：默认关）。

    ``enabled``＝是否真打 VLM；``timeout_sec``＝单次核对上限（超时放行，发图
    路径本就延迟敏感）；``kinds``＝对哪些图核对（默认只核**人像**——物体图已有
    ``image_gate`` 的 subject_match 后验，重复核对纯烧 GPU）。
    """
    raw = (scfg or {}).get("caption_guard")
    cfg = dict(raw) if isinstance(raw, dict) else {}
    cfg.setdefault("enabled", False)
    cfg.setdefault("timeout_sec", DEFAULT_TIMEOUT_SEC)
    kinds = cfg.get("kinds")
    cfg["kinds"] = tuple(str(k) for k in kinds) if isinstance(kinds, (list, tuple)) \
        else ("selfie", "video", "photo")
    return cfg


def build_caption_check_prompt(caption: str) -> str:
    """图文核对指令（纯函数）：只问一件事——配文声称的东西画面里有没有。"""
    cap = str(caption or "").strip()[:200]
    return (
        "You are checking whether a chat caption honestly matches the photo.\n"
        f'Caption (may be Chinese): "{cap}"\n'
        "Answer with STRICT JSON only (no prose, no markdown):\n"
        '{"main_subject": "<short noun phrase in English>", '
        '"is_person_portrait": <bool>, "caption_claims_match": <true|false|null>}\n'
        "Rules: caption_claims_match = false ONLY if the caption clearly presents "
        "the photo as showing something that is NOT in the image — e.g. the caption "
        "says it shows a room, food, a pet, an object or a place, but the image is "
        "just a portrait of a person. If the caption only conveys mood, a greeting, "
        "or where the person is/what they are doing, answer true. "
        "If you are not sure, answer null."
    )


def parse_caption_check(text: Any) -> Optional[Dict[str, Any]]:
    """从 VLM 回复里鲁棒抠 JSON（容忍 ```json 围栏/前后废话）；抠不出返回 None。"""
    s = str(text or "").strip()
    if not s:
        return None
    m = re.search(r"\{.*\}", s, flags=re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def caption_verdict(parsed: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    """核对判定（纯函数）→ ``(配文是否可用, 原因)``。

    只有 ``caption_claims_match`` **显式为 False** 才判不可用；``True``/``null``/
    缺字段/解析失败一律放行——这条防线宁可漏拦，不可把正常配文判成谎话。
    """
    if not isinstance(parsed, dict):
        return True, "parse_fail"
    if parsed.get("caption_claims_match") is False:
        got = str(parsed.get("main_subject") or "?").strip()[:40]
        return False, f"caption_mismatch({got})"
    return True, "ok"


async def check_caption(
    image_path: str, caption: str, root_config: Optional[Dict[str, Any]], *,
    timeout_sec: float = DEFAULT_TIMEOUT_SEC,
) -> Tuple[bool, str]:
    """对「这张图 + 这句配文」跑一次 VLM 核对。基础设施故障一律放行。"""
    cap = str(caption or "").strip()
    if not cap or not str(image_path or "").strip():
        return True, "no_input"
    vcfg = dict((root_config or {}).get("vision") or {})
    if not vcfg:
        _record("soft_pass", "no_vision_cfg")
        return True, "no_vision_cfg"
    try:
        from src.vision_client import VisionClient
        vc = VisionClient(vcfg)
        if not vc.initialize():
            _record("soft_pass", "vision_init_fail")
            return True, "vision_init_fail"
        raw = await asyncio.wait_for(
            vc.describe_image(image_path, prompt=build_caption_check_prompt(cap)),
            timeout=max(1.0, float(timeout_sec or DEFAULT_TIMEOUT_SEC)))
    except asyncio.TimeoutError:
        _record("soft_pass", "timeout")
        return True, "timeout"
    except Exception as ex:  # noqa: BLE001
        logger.debug("[caption_guard] VLM 调用异常（放行）", exc_info=True)
        _record("soft_pass", f"vlm_error:{type(ex).__name__}")
        return True, "vlm_error"
    ok, reason = caption_verdict(parse_caption_check(raw))
    _record("checked")
    if reason == "parse_fail":
        _record("soft_pass", reason)
    elif ok:
        _record("passed")
    else:
        _record("rewritten", reason, caption=cap)
        logger.info("[caption_guard] 配文与画面不符(%s) file=%s caption=%r raw=%r",
                    reason, image_path, cap[:60], str(raw or "")[:160])
    return ok, reason


async def ensure_truthful_caption(
    image_path: str, caption: str, *,
    root_config: Optional[Dict[str, Any]] = None,
    scfg: Optional[Dict[str, Any]] = None,
    kind: str = "selfie",
    fallback: str = "",
) -> Tuple[str, str]:
    """发送前的配文兜底：核对不过 → 换成不声称画面内容的 ``fallback``。

    返回 ``(最终配文, 原因)``；``原因="ok"/软失败原因`` 表示配文原样保留。
    ``fallback`` 为空时不改配文（宁可发出略有出入的文案，也不发空配文的裸图——
    裸图在坐席实录里比配文不准更像机器人）。
    """
    cfg = resolve_caption_guard_cfg(scfg)
    if not bool(cfg.get("enabled", False)):
        return caption, "disabled"
    if str(kind or "") not in tuple(cfg.get("kinds") or ()):
        return caption, "kind_skipped"
    ok, reason = await check_caption(
        image_path, caption, root_config,
        timeout_sec=float(cfg.get("timeout_sec", DEFAULT_TIMEOUT_SEC) or
                          DEFAULT_TIMEOUT_SEC))
    if ok:
        return caption, reason
    fb = str(fallback or "").strip()
    return (fb or caption), reason


__all__ = [
    "resolve_caption_guard_cfg", "build_caption_check_prompt",
    "parse_caption_check", "caption_verdict", "check_caption",
    "ensure_truthful_caption", "stats_snapshot",
]
