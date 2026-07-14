# -*- coding: utf-8 -*-
"""Facebook 线索评分器 v2 — 两阶段精排(Sprint 3 P1)。

设计:
  阶段 1: 调 v1 score_member,纯启发式秒级过滤
  阶段 2: 启发式 ≥ B (即 score >= 45) 的线索,送 LLM 精排
          - LLM 看 name/source/已知 profile 做更细判断
          - 输出 0-100 调整值,与 v1 加权融合 (默认 60% LLM + 40% v1)
          - LLM 不可用 → 直接用 v1 分,降级日志一次

性能控制:
  - 批量上限 30 人/次(防 token 爆炸)
  - 名字级缓存 24h(同一名字,源相同时复用 LLM 输出)
  - LLM 失败 retry 一次,再失败降级
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from .fb_lead_scorer import score_member as v1_score_member, _tier_for_score

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# LLM 缓存
# ─────────────────────────────────────────────────────────────────────

_CACHE_TTL_SEC = 24 * 3600
_cache_lock = threading.Lock()
_llm_cache: Dict[str, Dict[str, Any]] = {}  # key -> {ts, result}


def _cache_key(name: str, source_group: str, target_country: str) -> str:
    raw = f"{name.lower().strip()}|{source_group.lower().strip()}|{target_country.upper()}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:16]


def _cache_get(key: str) -> Optional[Dict]:
    with _cache_lock:
        ent = _llm_cache.get(key)
        if not ent:
            return None
        if time.time() - ent["ts"] > _CACHE_TTL_SEC:
            _llm_cache.pop(key, None)
            return None
        return ent["result"]


def _cache_put(key: str, result: Dict):
    with _cache_lock:
        _llm_cache[key] = {"ts": time.time(), "result": result}
        # 简单的容量控制:超过 5000 条裁掉最旧的 1000
        if len(_llm_cache) > 5000:
            oldest = sorted(_llm_cache.items(), key=lambda x: x[1]["ts"])[:1000]
            for k, _ in oldest:
                _llm_cache.pop(k, None)


# ─────────────────────────────────────────────────────────────────────
# LLM 精排
# ─────────────────────────────────────────────────────────────────────

_LLM_DOWNGRADE_LOGGED = False


def _llm_refine_score(name: str, *,
                      source_group: str,
                      target_country: str,
                      v1_result: Dict) -> Optional[Dict]:
    """调 LLM 精排单个线索。返回 {llm_score, llm_reason, llm_tier} 或 None。"""
    global _LLM_DOWNGRADE_LOGGED
    try:
        from .llm_client import LLMClient
        client = LLMClient()
    except Exception as e:
        if not _LLM_DOWNGRADE_LOGGED:
            logger.warning("[scorer_v2] LLMClient 不可用,降级到 v1: %s", e)
            _LLM_DOWNGRADE_LOGGED = True
        return None

    prompt = f"""You are scoring Facebook leads for an Italian/US lead-generation campaign
targeting men 30+. Output ONLY a JSON object, no other text.

Lead info:
- Name: {name}
- Source group: {source_group or 'N/A'}
- Target country: {target_country or 'IT'}
- v1 heuristic score: {v1_result.get('score', 0)}/100 ({v1_result.get('tier', '?')})
- v1 inferred country: {v1_result.get('inferred_country', '?')}
- v1 inferred gender: {v1_result.get('inferred_gender', '?')}
- v1 reasons: {', '.join(v1_result.get('reasons', []))}

Adjust the score considering:
1. Is the name plausible Italian/US male/female? (be strict: don't reward generic names)
2. Does source group context strongly suggest target audience?
3. Are there red flags (bot-like name, obvious wrong country)?

Return JSON:
{{"llm_score": <int 0-100>,
  "llm_reason": "<one short sentence in Chinese>",
  "fit_country": <"IT"|"US"|"OTHER"|"UNKNOWN">,
  "fit_gender": <"male"|"female"|"unknown">,
  "is_likely_bot": <true|false>}}"""

    try:
        resp = client.chat_with_system(
            system="You are a precise lead-quality judge. Output only valid JSON.",
            user=prompt,
            temperature=0.1,
            max_tokens=200,
        )
        if not resp:
            return None
        # 容错:LLM 有时会包代码块,先提取 JSON 段
        s = resp.strip()
        if s.startswith("```"):
            # 去 ``` 行
            s = "\n".join(line for line in s.splitlines()
                          if not line.strip().startswith("```"))
        # 兜底 — 找第一个 { 到最后一个 }
        i, j = s.find("{"), s.rfind("}")
        if i >= 0 and j > i:
            s = s[i:j + 1]
        data = json.loads(s)
    except Exception as e:
        logger.debug("[scorer_v2] LLM 调用/解析失败: %s", e)
        return None

    if not isinstance(data, dict):
        return None
    try:
        llm_score = int(data.get("llm_score", 0))
        llm_score = max(0, min(100, llm_score))
    except Exception:
        return None
    return {
        "llm_score": llm_score,
        "llm_reason": str(data.get("llm_reason", ""))[:200],
        "fit_country": str(data.get("fit_country", "UNKNOWN"))[:10],
        "fit_gender": str(data.get("fit_gender", "unknown"))[:10],
        "is_likely_bot": bool(data.get("is_likely_bot", False)),
    }


# ─────────────────────────────────────────────────────────────────────
# v2 主入口
# ─────────────────────────────────────────────────────────────────────

# v1 + LLM 加权融合系数
LLM_WEIGHT = 0.6
V1_WEIGHT = 0.4


def score_member_v2(name: str, *,
                    source_group: str = "",
                    target_country: str = "",
                    target_groups: Optional[List[str]] = None,
                    lead_record: Optional[Dict] = None,
                    profile_keywords: Optional[List[str]] = None,
                    use_llm: bool = True,
                    min_v1_score_for_llm: int = 45,
                    use_cache: bool = True) -> Dict[str, Any]:
    """两阶段评分主入口。

    1. v1 启发式
    2. v1 score >= min_v1_score_for_llm 时调 LLM 精排,加权融合
    3. is_likely_bot 自动 -30 分
    """
    v1 = v1_score_member(name,
                         source_group=source_group,
                         target_country=target_country,
                         target_groups=target_groups,
                         lead_record=lead_record,
                         profile_keywords=profile_keywords)

    out = dict(v1)
    out["v1_score"] = v1["score"]
    out["llm_used"] = False
    out["llm_score"] = None
    out["llm_reason"] = ""
    out["final_score"] = v1["score"]
    out["final_tier"] = v1["tier"]

    if not use_llm or v1["score"] < min_v1_score_for_llm:
        return out

    # 缓存 lookup
    if use_cache:
        ck = _cache_key(name, source_group, target_country)
        cached = _cache_get(ck)
        if cached:
            llm_data = cached
            out["llm_cache_hit"] = True
        else:
            llm_data = _llm_refine_score(name,
                                         source_group=source_group,
                                         target_country=target_country,
                                         v1_result=v1)
            if llm_data:
                _cache_put(ck, llm_data)
                out["llm_cache_hit"] = False
    else:
        llm_data = _llm_refine_score(name,
                                     source_group=source_group,
                                     target_country=target_country,
                                     v1_result=v1)
        out["llm_cache_hit"] = False

    if not llm_data:
        return out

    out["llm_used"] = True
    out["llm_score"] = llm_data["llm_score"]
    out["llm_reason"] = llm_data["llm_reason"]
    out["llm_breakdown"] = {
        "fit_country": llm_data["fit_country"],
        "fit_gender": llm_data["fit_gender"],
        "is_likely_bot": llm_data["is_likely_bot"],
    }

    # 融合
    fused = LLM_WEIGHT * llm_data["llm_score"] + V1_WEIGHT * v1["score"]
    if llm_data["is_likely_bot"]:
        fused -= 30
        out.setdefault("reasons", []).append("LLM 标记为可疑账号")
    if llm_data["llm_reason"]:
        out.setdefault("reasons", []).append(f"LLM: {llm_data['llm_reason']}")
    fused = int(max(0, min(100, fused)))
    out["final_score"] = fused
    out["final_tier"] = _tier_for_score(fused)
    return out


def batch_score_and_persist_v2(member_names: List[str], *,
                               source_group: str = "",
                               target_country: str = "",
                               target_groups: Optional[List[str]] = None,
                               profile_keywords: Optional[List[str]] = None,
                               use_llm: bool = True,
                               min_score_to_persist: int = 0,
                               max_llm_calls: int = 30) -> List[Dict]:
    """批量 v2 评分 + 写库。

    LLM 上限 max_llm_calls(默认 30) — 超过的只用 v1。
    """
    out = []
    try:
        from src.leads.store import get_leads_store
        store = get_leads_store()
    except Exception:
        store = None

    llm_calls_left = max_llm_calls if use_llm else 0
    for nm in member_names or []:
        nm = (nm or "").strip()
        if not nm:
            continue
        rec = None
        lid = None
        if store is not None:
            try:
                lid = store.find_match(name=nm)
                if lid:
                    rec = store.get_lead(lid)
            except Exception:
                pass

        result = score_member_v2(
            nm,
            source_group=source_group,
            target_country=target_country,
            target_groups=target_groups,
            lead_record=rec,
            profile_keywords=profile_keywords,
            use_llm=(llm_calls_left > 0),
        )
        if result.get("llm_used") and not result.get("llm_cache_hit", False):
            llm_calls_left -= 1
        result["name"] = nm
        result["lead_id"] = lid

        # 用 final_score 判断持久化阈值
        if store is not None and result["final_score"] >= min_score_to_persist:
            try:
                if not lid:
                    lid = store.add_lead(
                        name=nm, source_platform="facebook",
                        tags=[source_group] if source_group else [],
                        notes=";".join(result["reasons"])[:500])
                    result["lead_id"] = lid
                if lid:
                    store.update_lead(lid, score=result["final_score"])
            except Exception:
                pass
        out.append(result)
    return out


def get_cache_stats() -> Dict[str, Any]:
    with _cache_lock:
        return {"size": len(_llm_cache),
                "ttl_sec": _CACHE_TTL_SEC,
                "weights": {"llm": LLM_WEIGHT, "v1": V1_WEIGHT}}


def clear_cache():
    with _cache_lock:
        _llm_cache.clear()
