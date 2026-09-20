"""P1-XF（2026-08-16）智能融合翻译：多线路候选 → LLM 择优合成一条最优译文。

对标腾讯 Hunyuan-MT-Chimera 的「集成翻译」思路（多候选进、更优结果出，
官方报告 XCOMET +2.3%），但用现有干净翻译 LLM 通道实现——零新模型部署、
GPU 显存零新增；将来 Chimera-7B 本地评估通过后，只需替换 ``_fusion_chat``
的后端，闸门与消费面不动。

设计不变量（改本模块前先读）：
1. **融合只做编辑择优，不做再创作**：prompt 明确「从候选中综合出最优译文」，
   候选本身已经过 compare_translations 的术语 mask→restore（术语合规成品）。
2. **融合绝不劣于单引擎**（接受闸门）：融合稿必须过语言完整性护栏 +
   确定性置信度地板（TIER_LOW 同刻度），且不明显低于最佳候选
   （margin 给意译压分留余量）；不过闸 → ``ok=False`` 带原因，前端不出卡，
   坐席照旧从候选里挑——fail-open，绝不阻塞对照功能本身。
3. **同质候选不融合**（unanimous）：全部候选归一化后相同 = 融合是纯延迟税，
   跳过并如实给原因。
4. 失败候选/空文本从不进融合；候选 <2 不融合（没有「综合」可言）。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

# 接受闸门刻度：与 translation_confidence.TIER_LOW(0.5) 同口径的地板 +
# 「不明显低于最佳候选」的余量（LLM 意译常在字符信号上压分，0.15 经
# P0-2 对照徽标同一 scorer 的分布观感取值；融合稿语言错/空译在地板就死）。
CONF_FLOOR = 0.5
CONF_BEST_MARGIN = 0.15
MIN_CANDIDATES = 2

_WS_RE = re.compile(r"\s+")


def _norm(text: str) -> str:
    """候选同质判定的归一化：折叠空白 + 去首尾（不动标点——标点差异是真差异）。"""
    return _WS_RE.sub(" ", str(text or "")).strip()


def usable_candidates(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """可进融合的候选：ok 且有译文；按归一化文本去重（保留首个引擎归属）。"""
    seen: Dict[str, Dict[str, Any]] = {}
    for c in candidates or []:
        if not (isinstance(c, dict) and c.get("ok") and c.get("translated_text")):
            continue
        key = _norm(c["translated_text"])
        if not key or key in seen:
            continue
        seen[key] = c
    return list(seen.values())


def build_fusion_prompt(
    source_text: str,
    candidates: List[Dict[str, Any]],
    *,
    source_lang: str = "",
    target_lang: str = "",
) -> str:
    """融合 prompt（纯函数便于门禁）：编辑择优语义 + 只输出终稿。"""
    from src.ai.translation_service import LANG_NAMES

    src = str(source_lang or "").strip().lower()
    tgt = str(target_lang or "").strip().lower()
    source_name = LANG_NAMES.get(src, src or "the source language")
    target_name = LANG_NAMES.get(tgt, tgt)
    lines = [
        "You are a translation editor. Several machine translation engines "
        f"translated the same chat message into {target_name}. Combine their "
        "strengths into ONE best translation: accurate meaning, natural chat "
        "tone, correct names/numbers/terms. If one candidate is already best, "
        "output it as-is. Output ONLY the final translation - no explanations.",
        "",
        f"Source ({source_name}): {source_text}",
    ]
    for i, c in enumerate(candidates, 1):
        lines.append(f"Candidate {i} [{c.get('engine', '?')}]: {c.get('translated_text', '')}")
    return "\n".join(lines)


async def fuse_compare_candidates(
    svc: Any,
    *,
    text: str,
    candidates: List[Dict[str, Any]],
    source_lang: str = "",
    target_lang: str = "",
) -> Dict[str, Any]:
    """把对照候选交 LLM 择优合成。返回 fusion dict（见模块 docstring 的闸门语义）。

    ``svc`` = TranslationService（用其 router 定位 ``ai`` 引擎的干净翻译通道）。
    任何异常/不过闸 → ``{"ok": False, "reason": ...}``，绝不抛出。
    """
    from src.ai.translation_confidence import confidence_tier, translation_confidence
    from src.ai.translation_engines import _output_lang_sane
    from src.ai.translation_service import _clean_translation

    cands = usable_candidates(candidates)
    if len(cands) < MIN_CANDIDATES:
        # 成功候选 <2 或全同质：分开报原因（unanimous 对坐席是好消息——各线一致）
        all_ok = [c for c in (candidates or [])
                  if isinstance(c, dict) and c.get("ok") and c.get("translated_text")]
        reason = "unanimous" if len(all_ok) >= MIN_CANDIDATES else "not_enough_candidates"
        return {"ok": False, "reason": reason}

    router = getattr(svc, "_router", None)
    ai_eng = router.engine_by_name("ai") if router is not None else None
    if ai_eng is None or not getattr(ai_eng, "available", False) \
            or not hasattr(ai_eng, "bare_chat"):
        return {"ok": False, "reason": "no_llm"}

    prompt = build_fusion_prompt(
        text, cands, source_lang=source_lang, target_lang=target_lang)
    try:
        raw = await ai_eng.bare_chat(prompt)
    except Exception:  # noqa: BLE001
        return {"ok": False, "reason": "fusion_error"}
    fused = _clean_translation(str(raw or ""))
    if not fused or not _output_lang_sane(fused, target_lang):
        return {"ok": False, "reason": "bad_output"}

    conf = translation_confidence(text, fused, target_lang)
    best_conf = max((float(c.get("confidence", 0.0) or 0.0) for c in cands), default=0.0)
    if conf < CONF_FLOOR or conf < (best_conf - CONF_BEST_MARGIN):
        return {"ok": False, "reason": "below_best",
                "confidence": conf, "best_candidate_confidence": best_conf}
    return {
        "ok": True,
        "text": fused,
        "engine": "fusion",
        "engines_used": [str(c.get("engine") or "?") for c in cands],
        "confidence": conf,
        "confidence_tier": confidence_tier(conf),
        "reason": "",
    }
