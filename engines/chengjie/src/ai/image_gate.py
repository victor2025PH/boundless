"""出图自检闸门（Vision Gate）——生成的人设照片发出前先过本地 VLM 体检。

背景（2026-07-13「狗图事故」）：prompt 层的修复只能降低概率，不能保证生成结果
一定是"单人、性别年龄贴人设、无文字水印"的照片。本模块在**发出前**用局域网
Qwen2.5-VL（``VisionClient``，176/140 双活）对生成图做结构化体检：

    生成 → VLM 判定(JSON) → 不合格 → 换种子重生成(≤retries 次) → 仍不合格 → 放弃回落文字

设计原则：
- **纯函数可单测**：``build_gate_prompt`` / ``parse_gate_response`` / ``gate_verdict``
  零 IO；``check_image`` / ``generate_with_gate`` 只做接线。
- **软失败放行**：VLM 不可达/超时/回答解析不出 → 放行（soft-pass）并计数——
  自检是锦上添花，绝不能因为体检仪坏了把整条发图链拖死（当晚 176 就掉线过 20 分钟）。
- **明确拒绝才拦**：只有 VLM 明确回答"不是单人/性别不符/有水印/NSFW"才拒。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# ── 观测（进程内累计；经 image_autosend.metrics_snapshot 并入 autosend-status）──
_METRICS: Dict[str, Any] = {
    "checked": 0, "passed": 0, "rejected": 0, "retry_ok": 0,
    "soft_pass": 0, "last_reason": "", "last_ts": 0.0,
}
_METRICS_LOCK = threading.Lock()


def _record(key: str, reason: str = "") -> None:
    with _METRICS_LOCK:
        _METRICS[key] = int(_METRICS.get(key, 0)) + 1
        if reason:
            _METRICS["last_reason"] = str(reason)
        _METRICS["last_ts"] = time.time()


def metrics_snapshot() -> Dict[str, Any]:
    with _METRICS_LOCK:
        return dict(_METRICS)


def reset_metrics() -> None:
    """测试钩子。"""
    with _METRICS_LOCK:
        for k in ("checked", "passed", "rejected", "retry_ok", "soft_pass"):
            _METRICS[k] = 0
        _METRICS["last_reason"] = ""


# ── 纯函数：提示词 / 解析 / 判定 ─────────────────────────────────────────

def build_gate_prompt() -> str:
    """让 VLM 输出结构化体检 JSON 的指令（英文——本地 VLM 对英文指令遵循度最好）。

    ``people_count`` 只数**前景主体**：真机实测（2026-07-13）街景自拍会把虚化背景
    路人也数进去 → 合格照片被误拒为 multiple_people，故显式要求忽略背景行人。

    ``explicit`` 与 ``nsfw`` 分开取（2026-07-14 尺度分级）：``nsfw``=任何露肤性感即真，
    ``explicit``=真正露骨（露点/生殖器/性行为）。suggestive 档只拦 explicit，sfw 档拦 nsfw。
    ``looks_underage`` 独立取——未成年性化是硬红线，任何档位、无视 nsfw/explicit 一律拦。
    """
    return (
        "Inspect this image and answer with STRICT JSON only (no prose, no markdown):\n"
        '{"people_count": <int>, "gender": "male"|"female"|"unknown", '
        '"apparent_age": <int, 0 if no person>, "is_animal_subject": <bool>, '
        '"visible_text_or_watermark": <bool>, "nsfw": <bool>, '
        '"explicit": <bool>, "looks_underage": <bool>}\n'
        "Rules: people_count = number of MAIN human subjects in the foreground only — "
        "IGNORE blurred or distant passers-by in the background; "
        "gender/apparent_age describe the main subject; "
        "is_animal_subject = true when the MAIN subject is an animal/pet; "
        "visible_text_or_watermark = true ONLY for watermark/logo/subtitle/caption text "
        "STAMPED OVER the image — shop signs, neon signs or street text that are part of "
        "the real scene do NOT count; "
        "nsfw = true for any nudity, lingerie, swimwear or sexually suggestive posing; "
        "explicit = true ONLY for exposed genitalia/nipples, or a depicted sexual act "
        "(a clothed or swimwear photo is NOT explicit); "
        "looks_underage = true if the main person appears younger than 18."
    )


def parse_gate_response(text: Any) -> Optional[Dict[str, Any]]:
    """从 VLM 回复中鲁棒地抠出 JSON（容忍 ```json 围栏/前后废话）；解析不出返回 None。"""
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


def gate_verdict(
    parsed: Optional[Dict[str, Any]], *,
    expect_gender: str = "",
    expect_age: int = 0,
    age_tolerance: int = 18,
    strict_watermark: bool = False,
    content_rating: str = "sfw",
) -> Tuple[bool, str]:
    """体检判定（纯函数）。返回 ``(是否放行, 原因)``。

    - ``parsed=None``（解析失败）→ 放行 ``parse_fail``（软失败原则，调用方计 soft_pass）。
    - **未成年性化**（``looks_underage`` && 有性感/露骨信号）→ 硬拒 ``underage``（红线，
      无视档位，最优先）。
    - 人数 != 1 / 主体是动物 → 拒（狗图事故的直接防线）。
    - 性别与人设明确冲突 → 拒；VLM 回答 unknown 不拦（模糊不误伤）。
    - 年龄偏差 > ``age_tolerance`` 岁 → 拒（22 岁少女生成成老太太也是穿帮）。
    - **尺度分级**（``content_rating``）：
        ``sfw``（默认）→ 任何 ``nsfw`` 即拒（历史行为不变）；
        ``suggestive`` → 放行性感，但 ``explicit``（露点/性行为）仍硬拒。
    - 文字/水印：**默认只记录不拦**（``watermark_ignored``）——真机实测 VLM 分不清
      "盖在图上的水印"与"场景里的店招/霓虹"，误拒率高；安全关键是前几项。
      ``strict_watermark=True`` 才升级为拦截。
    """
    if not isinstance(parsed, dict):
        return True, "parse_fail"
    rating = str(content_rating or "sfw").strip().lower()
    # ── 硬红线：未成年 + 任何性化信号 → 必拒（最优先，无视档位）──────────────
    sexual_signal = bool(parsed.get("nsfw")) or bool(parsed.get("explicit"))
    if bool(parsed.get("looks_underage")) and (sexual_signal or rating == "suggestive"):
        return False, "underage"
    try:
        n = int(parsed.get("people_count", -1))
    except Exception:
        n = -1
    if bool(parsed.get("is_animal_subject")):
        return False, "animal_subject"
    if n == 0:
        return False, "no_person"
    if n > 1:
        return False, "multiple_people"
    # 尺度分级：suggestive 只拦露骨(explicit)；sfw（及未知档）拦一切 nsfw。
    if rating == "suggestive":
        if bool(parsed.get("explicit")):
            return False, "explicit"
    elif bool(parsed.get("nsfw")):
        return False, "nsfw"
    eg = str(expect_gender or "").strip().lower()
    got_g = str(parsed.get("gender") or "").strip().lower()
    if eg in ("male", "female") and got_g in ("male", "female") and eg != got_g:
        return False, f"gender_mismatch({got_g})"
    try:
        age = int(parsed.get("apparent_age", 0))
    except Exception:
        age = 0
    if expect_age > 0 and age > 0 and abs(age - int(expect_age)) > int(age_tolerance):
        return False, f"age_mismatch({age})"
    if bool(parsed.get("visible_text_or_watermark")):
        if strict_watermark:
            return False, "text_watermark"
        return True, "watermark_ignored"
    return True, "ok"


def build_object_gate_prompt(subject: str) -> str:
    """物体图体检指令：主体是否与要发的东西相符（要蛋糕别发面条）+ 基础安全项。"""
    subj = str(subject or "").strip()[:80] or "the requested object"
    return (
        "Inspect this image and answer with STRICT JSON only (no prose, no markdown):\n"
        '{"main_subject": "<short noun phrase>", "subject_match": <bool>, '
        '"visible_text_or_watermark": <bool>, "nsfw": <bool>}\n'
        f'Rules: subject_match = true if the main subject of the image matches "{subj}" '
        "(same kind of thing counts — e.g. any noodle dish matches noodles); "
        "visible_text_or_watermark = true ONLY for watermark/logo/caption text stamped "
        "over the image; nsfw = nudity or sexual content."
    )


def object_gate_verdict(
    parsed: Optional[Dict[str, Any]], *, subject: str = "",
) -> Tuple[bool, str]:
    """物体图判定（纯函数）：NSFW 必拒；给了 subject 且 VLM 明确说不匹配 → 拒；
    水印只记录（与人像口径一致）；解析失败软放行。

    物体图（食物/风景/物件）本就不该有性内容，故**不参与尺度分级**——任何 nsfw 一律拒。"""
    if not isinstance(parsed, dict):
        return True, "parse_fail"
    if bool(parsed.get("nsfw")):
        return False, "nsfw"
    if str(subject or "").strip() and parsed.get("subject_match") is False:
        got = str(parsed.get("main_subject") or "?")[:40]
        return False, f"subject_mismatch({got})"
    if bool(parsed.get("visible_text_or_watermark")):
        return True, "watermark_ignored"
    return True, "ok"


def persona_expectations(persona: Any) -> Tuple[str, int]:
    """从 persona dict 提取（期望性别, 期望年龄）；缺失回 ("", 0)=不校验该项。"""
    if not isinstance(persona, dict):
        return "", 0
    try:
        from src.ai.companion_selfie import _persona_gender_word
        g = {"woman": "female", "man": "male"}.get(
            _persona_gender_word(persona) or "", "")
    except Exception:
        g = ""
    try:
        age = int(persona.get("age") or 0)
    except Exception:
        age = 0
    return g, age


# ── 接线：VLM 体检 + 带重试的生成 ────────────────────────────────────────

def resolve_gate_cfg(scfg: Dict[str, Any]) -> Dict[str, Any]:
    """取 ``companion.selfie.vision_gate``（默认启用、重试 1 次、水印只记录不拦）。

    ``content_rating`` 从父级 ``companion.selfie.content_rating`` 继承（体检档位须与出图
    档位一致：出 suggestive 图却按 sfw 体检会把自己生成的性感照全拒）。vision_gate 段可
    显式覆盖。
    """
    raw = (scfg or {}).get("vision_gate")
    cfg = dict(raw) if isinstance(raw, dict) else {}
    cfg.setdefault("enabled", True)
    cfg.setdefault("retries", 1)
    cfg.setdefault("age_tolerance", 18)
    cfg.setdefault("strict_watermark", False)
    cfg.setdefault("content_rating", str((scfg or {}).get("content_rating") or "sfw"))
    return cfg


async def check_image(
    image_path: str, persona: Any, root_config: Dict[str, Any], *,
    age_tolerance: int = 18,
    strict_watermark: bool = False,
    kind: str = "selfie",
    subject: str = "",
    content_rating: str = "sfw",
) -> Tuple[bool, str]:
    """对单张图跑 VLM 体检（``kind``：selfie=人像项 / object=主体匹配项）。

    ``content_rating``（``sfw`` | ``suggestive``）透传给 ``gate_verdict`` 做尺度分级；
    object 图不参与分级（食物/物件本不该有性内容）。
    基础设施故障一律放行（soft-pass），仅明确不合格才 False。"""
    vcfg = dict((root_config or {}).get("vision") or {})
    if not vcfg:
        _record("soft_pass", "no_vision_cfg")
        return True, "no_vision_cfg"
    is_object = str(kind or "").lower() == "object"
    gate_prompt = build_object_gate_prompt(subject) if is_object else build_gate_prompt()
    try:
        from src.vision_client import VisionClient
        vc = VisionClient(vcfg)
        if not vc.initialize():
            _record("soft_pass", "vision_init_fail")
            return True, "vision_init_fail"
        raw = await vc.describe_image(image_path, prompt=gate_prompt)
    except Exception as ex:  # noqa: BLE001
        logger.debug("[image_gate] VLM 调用异常（放行）", exc_info=True)
        _record("soft_pass", f"vlm_error:{type(ex).__name__}")
        return True, "vlm_error"
    parsed = parse_gate_response(raw)
    if is_object:
        ok, reason = object_gate_verdict(parsed, subject=subject)
    else:
        eg, age = persona_expectations(persona)
        ok, reason = gate_verdict(
            parsed, expect_gender=eg, expect_age=age, age_tolerance=age_tolerance,
            strict_watermark=strict_watermark, content_rating=content_rating)
    _record("checked")
    if reason == "parse_fail":
        _record("soft_pass", reason)
    elif ok:
        _record("passed")
    else:
        _record("rejected", reason)
        logger.info("[image_gate] 出图体检不合格 kind=%s reason=%s file=%s raw=%r",
                    kind, reason, image_path, str(raw or "")[:160])
    return ok, reason


async def generate_with_gate(
    provider: Any, prompt: str, *,
    persona: Any = None,
    root_config: Optional[Dict[str, Any]] = None,
    gate_cfg: Optional[Dict[str, Any]] = None,
    seed: int = -1,
    kind: str = "selfie",
    subject: str = "",
    **gen_kwargs: Any,
):
    """生成 + 体检 + 换种子重试的统一入口（autosend 链与 Stage A 共用）。

    - 闸门关/album 后端 → 等价直调 ``provider.generate``（零行为变更）。
    - 体检不合格 → ``seed + 7919*attempt``（固定质数步长，确定性可复现）重生成，
      最多 ``retries`` 次；全部不合格 → 返回 ``ok=False, error=vision_gate:<reason>``，
      调用方按既有失败路径回落文字/语音。
    - ``kind="object"`` 走主体匹配体检（``subject``=期望主体，如 "a bowl of noodles"）。
    """
    gcfg = gate_cfg or {}
    gate_on = bool(gcfg.get("enabled", True))
    backend = str(getattr(provider, "backend", "")).lower()
    res = await provider.generate(prompt, seed=seed, **gen_kwargs)
    if not gate_on or backend == "album" or not getattr(res, "ok", False):
        return res
    retries = max(0, int(gcfg.get("retries", 1) or 0))
    tol = int(gcfg.get("age_tolerance", 18) or 18)
    strict_wm = bool(gcfg.get("strict_watermark", False))
    rating = str(gcfg.get("content_rating") or "sfw")
    ok, reason = await check_image(
        res.image_path, persona, root_config or {}, age_tolerance=tol,
        strict_watermark=strict_wm, kind=kind, subject=subject,
        content_rating=rating)
    if ok:
        return res
    last_reason = reason
    for attempt in range(1, retries + 1):
        new_seed = ((seed + 7919 * attempt) % (2 ** 31)) if seed >= 0 else -1
        logger.info("[image_gate] 重试出图 attempt=%d seed=%s（上次:%s）",
                    attempt, new_seed, last_reason)
        res2 = await provider.generate(prompt, seed=new_seed, **gen_kwargs)
        if not getattr(res2, "ok", False):
            res = res2
            break
        ok2, reason2 = await check_image(
            res2.image_path, persona, root_config or {}, age_tolerance=tol,
            strict_watermark=strict_wm, kind=kind, subject=subject,
            content_rating=rating)
        if ok2:
            _record("retry_ok")
            return res2
        last_reason = reason2
        res = res2
    try:
        from src.ai.companion_selfie import SelfieResult
        return SelfieResult(
            ok=False, prompt=str(prompt or ""),
            provider=str(getattr(provider, "backend", "") or ""),
            error=f"vision_gate:{last_reason}")
    except Exception:  # pragma: no cover - 导入兜底
        res.ok = False
        res.error = f"vision_gate:{last_reason}"
        return res


__all__ = [
    "build_gate_prompt",
    "build_object_gate_prompt",
    "parse_gate_response",
    "gate_verdict",
    "object_gate_verdict",
    "persona_expectations",
    "resolve_gate_cfg",
    "check_image",
    "generate_with_gate",
    "metrics_snapshot",
    "reset_metrics",
]
