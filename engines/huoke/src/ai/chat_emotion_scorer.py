# -*- coding: utf-8 -*-
"""L3 客户情感评分 — 用本地 LLM 评 4 个维度.

业务上下文 (jp_female_midlife: 日本 37-60 女性情感陪护):
    每条入站消息进来都跑一次评分, 写入客户 ai_profile.emotion_scores,
    同时给 referral_gate 的 soft_score 路径加一个新维度. 评分越高表示
    这个客户当前越愿意接受引流加 LINE.

输出 4 维 (每维 0.0-1.0):
    trust         — 信任度: 客户主动分享生活/家庭/感受 → 高
    interest      — 兴趣度: 客户主动问问题/回复长度大/主动延展话题 → 高
    frustration   — 不耐烦: 敷衍("嗯""哦"句越来越短) / 抱怨 → 高 (这维高=不该引)
    topic_match   — 话题匹配度: 是否在情感陪护话题, 还是抱怨工作/价格 → 高 (情感话题=匹配)

综合分 = trust × 0.4 + interest × 0.3 + (1 - frustration) × 0.2 + topic_match × 0.1
范围 0.0 - 1.0, 越高越适合引流.

调用频率 (victor 拍板): 每条入站都调.
缓存策略: hash(最近 5 条) → result, TTL 10 分钟, LRU 1000 条.
LLM 失败 fallback: 中性分 0.5 (不阻塞业务).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 4 个维度的 weight (总和 1.0, 由综合分公式驱动)
DIM_WEIGHTS = {
    "trust": 0.4,
    "interest": 0.3,
    "frustration_inverse": 0.2,  # (1 - frustration) 的权重
    "topic_match": 0.1,
}
NEUTRAL_DIM_SCORES = {
    "trust": 0.5, "interest": 0.5, "frustration": 0.5,
    "topic_match": 0.5, "rationale": "fallback (LLM unavailable)",
}
NEUTRAL_OVERALL = 0.5

CACHE_TTL_SEC = 600.0  # 10 分钟
CACHE_MAX = 1000
LLM_TIMEOUT_SEC = 8.0
MAX_HISTORY = 5  # 评分时取最近 5 条

PROMPT_TEMPLATE = """你是一个情感分析专家。给定客户最近的聊天记录, 评估这个客户当前对于"加 LINE 转人工聊天"的接受度, 从 4 个维度评分 (每项 0.0-1.0):

- trust (信任度): 客户主动分享个人信息/家庭/感受/生活? 回复速度快? 情感开放? 高 = 信任建立.
- interest (兴趣度): 客户会主动问问题? 回复长度合理? 主动延展话题? 高 = 兴趣浓.
- frustration (不耐烦): 客户负面情绪? 敷衍("嗯""哦")? 句子越来越短? 高 = 不耐烦, 不该引流.
- topic_match (话题匹配度): 是否在情感陪护话题 (家庭/感情/兴趣/生活)? 还是抱怨工作/产品询价等无关话题? 高 = 话题匹配.

业务背景: persona = {persona_key} (例如 jp_female_midlife = 日本 37-60 女性情感陪护).

聊天记录 (最近 {n} 条, 按时间排序):
{history}

严格输出 JSON, 不要任何其它文字:
{{"trust": <0.0-1.0>, "interest": <0.0-1.0>, "frustration": <0.0-1.0>, "topic_match": <0.0-1.0>, "rationale": "<一句话>"}}"""


# ── 缓存 (带 TTL, 进程内 LRU) ────────────────────────────────────────
_cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
_cache_lock = threading.Lock()


def _cache_key(messages: List[Dict[str, str]], persona_key: str) -> str:
    """hash(最近 N 条 role+content) + persona_key."""
    blob = json.dumps(
        {"persona": persona_key, "msgs": [
            {"r": m.get("role", ""), "c": m.get("content", "")} for m in messages
        ]},
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> Optional[Dict[str, Any]]:
    now = time.time()
    with _cache_lock:
        rec = _cache.get(key)
        if not rec:
            return None
        if now > rec.get("expires_at", 0):
            _cache.pop(key, None)
            return None
        # LRU: move to end
        _cache.move_to_end(key)
        return rec["scores"]


def _cache_put(key: str, scores: Dict[str, Any]) -> None:
    with _cache_lock:
        _cache[key] = {
            "scores": scores,
            "expires_at": time.time() + CACHE_TTL_SEC,
        }
        # LRU evict
        while len(_cache) > CACHE_MAX:
            _cache.popitem(last=False)
        _cache.move_to_end(key)


def clear_cache_for_tests() -> None:
    with _cache_lock:
        _cache.clear()


# ── 综合分公式 ───────────────────────────────────────────────────────
def compute_overall_score(scores: Dict[str, Any]) -> float:
    """4 维 → 0-1 综合分. frustration 反向."""
    try:
        trust = float(scores.get("trust", 0.5))
        interest = float(scores.get("interest", 0.5))
        frustration = float(scores.get("frustration", 0.5))
        topic_match = float(scores.get("topic_match", 0.5))
    except (TypeError, ValueError):
        return NEUTRAL_OVERALL
    overall = (
        trust * DIM_WEIGHTS["trust"]
        + interest * DIM_WEIGHTS["interest"]
        + (1.0 - frustration) * DIM_WEIGHTS["frustration_inverse"]
        + topic_match * DIM_WEIGHTS["topic_match"]
    )
    return max(0.0, min(1.0, overall))


# ── LLM 调用 ─────────────────────────────────────────────────────────
def _format_history(messages: List[Dict[str, str]]) -> str:
    """把 messages 格式化成 'incoming/outgoing: text' 多行."""
    lines = []
    for m in messages[-MAX_HISTORY:]:
        role = m.get("role", "")
        content = m.get("content", "") or ""
        if role in ("user", "incoming"):
            lines.append(f"对方: {content}")
        elif role in ("assistant", "outgoing"):
            lines.append(f"我: {content}")
        else:
            lines.append(f"{role}: {content}")
    return "\n".join(lines) or "(无聊天历史)"


def _parse_llm_json(raw: str) -> Optional[Dict[str, Any]]:
    """LLM 输出 JSON parse. 失败返 None.

    宽容: LLM 偶尔会带 markdown ```json ... ``` 或前后说明文字, 用正则抓.
    """
    if not raw:
        return None
    # 试着抓 {...} 第一个完整对象
    m = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
    blob = m.group(0) if m else raw.strip()
    try:
        data = json.loads(blob)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    # 校验关键字段都在 0-1 范围
    for k in ("trust", "interest", "frustration", "topic_match"):
        if k not in data:
            return None
        try:
            v = float(data[k])
            if not (0.0 <= v <= 1.0):
                return None
        except (TypeError, ValueError):
            return None
    return data


def _call_llm(messages: List[Dict[str, str]], persona_key: str) -> Optional[Dict[str, Any]]:
    """调本地 LLM 拿评分. 失败返 None.

    优先用 LLM Router (PR #87, 主控离线自动 fallback 远程 API). 没装 router
    时退回默认 LLMClient (config/ai.yaml 里的 provider).
    """
    try:
        from src.ai.llm_client import get_llm_client
        client = get_llm_client()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[emotion_scorer] llm_client init failed: %s", exc)
        return None

    prompt = PROMPT_TEMPLATE.format(
        persona_key=persona_key or "default",
        n=min(MAX_HISTORY, len(messages)),
        history=_format_history(messages),
    )
    try:
        raw = client.chat_messages(
            [
                {"role": "system", "content": "You are a precise JSON-only scorer."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=200,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[emotion_scorer] LLM call failed: %s", exc)
        return None
    return _parse_llm_json(raw)


# ── 主入口 ───────────────────────────────────────────────────────────
def score_emotion(
    messages: List[Dict[str, str]],
    persona_key: str = "",
    use_cache: bool = True,
) -> Dict[str, Any]:
    """评分入口. 失败 fallback 中性分, 永不抛.

    Args:
        messages: list of {"role": "user/assistant/incoming/outgoing", "content": "..."},
            按时间排序, 最旧 → 最新. 取最后 MAX_HISTORY 条.
        persona_key: 当前 persona (e.g. "jp_female_midlife"), 影响 LLM prompt 上下文.
        use_cache: True 用 hash(最近5条) 查 10 分钟缓存; False 强制重算.

    Returns:
        {"trust": 0-1, "interest": 0-1, "frustration": 0-1, "topic_match": 0-1,
         "rationale": str, "overall": 0-1, "cached": bool, "fallback": bool}
    """
    if not messages:
        result = dict(NEUTRAL_DIM_SCORES)
        result["overall"] = NEUTRAL_OVERALL
        result["cached"] = False
        result["fallback"] = True
        return result

    key = _cache_key(messages[-MAX_HISTORY:], persona_key)
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            r = dict(cached)
            r["cached"] = True
            return r

    raw = _call_llm(messages, persona_key)
    if raw is None:
        # LLM 失败 fallback: 中性分, 不阻塞业务
        result = dict(NEUTRAL_DIM_SCORES)
        result["overall"] = NEUTRAL_OVERALL
        result["cached"] = False
        result["fallback"] = True
        return result

    raw["overall"] = compute_overall_score(raw)
    raw["fallback"] = False
    _cache_put(key, dict(raw))
    raw["cached"] = False
    return raw
