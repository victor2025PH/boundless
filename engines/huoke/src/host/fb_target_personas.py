# -*- coding: utf-8 -*-
"""Facebook 目标画像配置读取层（2026-04-21 P2-4 Sprint A）。

职责：
    * 读取 ``config/fb_target_personas.yaml``（mtime 热加载）。
    * 提供 ``get_persona(key)`` / ``list_personas()`` / ``get_default_persona()``
      / ``get_vlm_config()`` / ``get_quotas()`` 等便捷访问函数。

配置文件缺失或解析失败时回退到内置 ``_FALLBACK`` 结构，
**不影响主流程（规则评分仍可工作，VLM 走默认 endpoint）**。
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any, Dict, List, Optional

from src.host._yaml_cache import YamlCache
from src.host.device_registry import config_file, data_file

logger = logging.getLogger(__name__)

_personas_path = config_file("fb_target_personas.yaml")
# 运行时覆盖默认客群（不改 YAML；重启进程后仍生效，便于面板切换）
_ACTIVE_PERSONA_OVERRIDE = data_file("fb_active_persona_override.json")


_FALLBACK = {
    "version": 1,
    "default_persona": "jp_female_midlife",
    "quotas": {
        "l1_per_device_per_day": 1000,
        "l2_per_device_per_day": 100,
        "l2_min_interval_sec": 20,
        "l2_jitter_sec": [6, 14],
    },
    "dedup_window_hours": 168,
    "vlm": {
        "provider": "ollama",
        "model": "qwen2.5vl:7b",
        "endpoint": "http://127.0.0.1:11434",
        "timeout_sec": 30,
        "max_retries": 2,
        "temperature": 0.2,
        "max_images_per_call": 3,
        "max_image_side_px": 1280,
        "jpeg_quality": 85,
        "monthly_budget_usd": 50.0,
    },
    "risk_guard": {
        "pause_l2_after_risk_hours": 12,
        "max_profile_visits_per_hour": 30,
    },
    "personas": {
        "jp_female_midlife": {
            "name": "日本 37-60 岁女性",
            "active": True,
            "display_flag": "🇯🇵",
            "display_label": "🇯🇵 日本 · 女性 · 37-60",
            "short_label": "日本中年女性",
            "country_code": "JP",
            "country_zh": "日本",
            "language": "ja",
            "referral_priority": ["line", "instagram", "whatsapp", "telegram"],
            "interest_topics": [
                "子育て", "料理", "美容・健康", "節約・家計", "韓ドラ",
                "旅行", "手芸", "ペット", "ガーデニング", "更年期",
            ],
            "seed_group_keywords": [
                "ママ友", "アラフィフ 趣味", "アラフォー 女子会",
                "韓ドラ 好き", "節約生活", "手芸部",
            ],
            "age_min": 37,
            "age_max": 60,
            "gender": "female",
            "locale": "ja-JP",
            "l1": {"pass_threshold": 30, "rules": []},
            "vlm_prompt": "",
            "match_criteria": {
                "age_bands_allowed": ["30s", "40s", "50s", "60s"],
                "genders_allowed": ["female"],
                "require_is_japanese": True,
                "min_overall_confidence": 0.55,
                "min_japanese_confidence": 0.50,
            },
        },
    },
}

# 全渠道默认优先级（persona 未声明 referral_priority 时回退）
# 顺序 = 全球通用值，persona 覆盖层会替换整段，不做并集。
_DEFAULT_REFERRAL_PRIORITY = ["whatsapp", "telegram", "instagram", "line"]


def _post_process(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        return copy.deepcopy(_FALLBACK)
    data = copy.deepcopy(_FALLBACK)
    for key in ("version", "default_persona", "dedup_window_hours"):
        if raw.get(key) is not None:
            data[key] = raw[key]
    for section in ("quotas", "vlm", "risk_guard"):
        if isinstance(raw.get(section), dict):
            data[section].update(raw[section])
    personas = raw.get("personas")
    if isinstance(personas, dict) and personas:
        data["personas"] = copy.deepcopy(personas)

    active = [k for k, v in data["personas"].items() if isinstance(v, dict) and v.get("active", True)]
    logger.info(
        "fb_target_personas 加载: personas=%s default=%s quotas(L1/L2)=%s/%s model=%s",
        active,
        data.get("default_persona"),
        data["quotas"].get("l1_per_device_per_day"),
        data["quotas"].get("l2_per_device_per_day"),
        data["vlm"].get("model"),
    )
    return data


_CACHE = YamlCache(
    path=_personas_path,
    defaults=_FALLBACK,
    post_process=_post_process,
    log_label="fb_target_personas.yaml",
    logger=logger,
)


def load_config(force_reload: bool = False) -> Dict[str, Any]:
    return _CACHE.get(force_reload=force_reload)


def reload_config() -> Dict[str, Any]:
    return _CACHE.reload()


def config_mtime() -> float:
    return _CACHE.mtime()


# ── 便捷访问 ─────────────────────────────────────────────────────────

def list_personas(only_active: bool = True) -> List[Dict[str, Any]]:
    cfg = load_config()
    out = []
    for key, p in (cfg.get("personas") or {}).items():
        if not isinstance(p, dict):
            continue
        if only_active and not p.get("active", True):
            continue
        out.append({"persona_key": key, **p})
    return out


def read_active_persona_override() -> Optional[str]:
    """返回 ``data/fb_active_persona_override.json`` 里的 persona_key；无文件/损坏则 None。"""
    try:
        if not _ACTIVE_PERSONA_OVERRIDE.is_file():
            return None
        raw = json.loads(_ACTIVE_PERSONA_OVERRIDE.read_text(encoding="utf-8"))
        k = str((raw or {}).get("persona_key") or "").strip()
        return k or None
    except Exception as e:
        logger.debug("[fb_personas] override 读取失败: %s", e)
        return None


def set_active_persona_override(persona_key: str) -> Dict[str, Any]:
    """写入运行时默认客群 key（须存在于 YAML personas 且 active）。"""
    import datetime as _dt

    cfg = load_config()
    personas = cfg.get("personas") or {}
    pk = (persona_key or "").strip()
    if not pk or pk not in personas:
        raise ValueError(f"未知或未配置的 persona_key: {persona_key!r}")
    p = personas.get(pk) or {}
    if isinstance(p, dict) and p.get("active") is False:
        raise ValueError(f"persona 已禁用(active=false): {pk}")
    _ACTIVE_PERSONA_OVERRIDE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "persona_key": pk,
        "updated_at": _dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _ACTIVE_PERSONA_OVERRIDE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def clear_active_persona_override() -> bool:
    """删除运行时覆盖，恢复仅由 YAML ``default_persona`` 决定。"""
    try:
        if _ACTIVE_PERSONA_OVERRIDE.is_file():
            _ACTIVE_PERSONA_OVERRIDE.unlink()
            return True
    except Exception as e:
        logger.warning("[fb_personas] 删除 override 失败: %s", e)
    return False


def get_yaml_default_persona_key() -> str:
    """YAML 文件里的 ``default_persona``（不含运行时覆盖）。"""
    cfg = load_config()
    return str(cfg.get("default_persona") or "jp_female_midlife")


def get_persona(persona_key: Optional[str] = None) -> Dict[str, Any]:
    """按 key 取 persona；key 为空或不存在时返回 default_persona；
    default 也缺失时退化为 _FALLBACK 第一个。"""
    cfg = load_config()
    personas = cfg.get("personas") or {}
    key = persona_key or get_default_persona_key() or ""
    if key not in personas:
        if personas:
            key = next(iter(personas.keys()))
        else:
            return copy.deepcopy(_FALLBACK["personas"]["jp_female_midlife"])
    p = copy.deepcopy(personas[key])
    p["persona_key"] = key
    return p


def get_default_persona_key() -> str:
    """当前**生效**的默认客群：运行时 override（若合法）优先，否则 YAML ``default_persona``。"""
    cfg = load_config()
    yaml_def = str(cfg.get("default_persona") or "jp_female_midlife")
    personas = cfg.get("personas") or {}
    ov = read_active_persona_override()
    if ov and ov in personas:
        p = personas.get(ov) or {}
        if isinstance(p, dict) and p.get("active", True):
            return ov
    return yaml_def


def get_quotas() -> Dict[str, Any]:
    return dict(load_config().get("quotas") or {})


def get_vlm_config() -> Dict[str, Any]:
    return dict(load_config().get("vlm") or {})


def get_risk_guard() -> Dict[str, Any]:
    return dict(load_config().get("risk_guard") or {})


def get_dedup_window_hours() -> int:
    return int(load_config().get("dedup_window_hours") or 168)


# ── P2-UI Sprint 新增 ─────────────────────────────────────────────────
# 把 persona 的展示元数据抽成 helpers，让 router/前端不再硬编码国旗、
# 国家名、引流优先级等散点字段。

def get_referral_priority(persona_key: Optional[str] = None) -> List[str]:
    """返回指定 persona 的引流渠道优先级列表。

    persona 未声明时回退到全局默认 ``_DEFAULT_REFERRAL_PRIORITY``。
    结果会去重 + 补齐缺漏渠道（保证 line/instagram/whatsapp/telegram 都在列）。
    """
    p = get_persona(persona_key)
    raw = p.get("referral_priority") or []
    if not isinstance(raw, list) or not raw:
        raw = list(_DEFAULT_REFERRAL_PRIORITY)
    seen = set()
    out = []
    for ch in raw:
        ch = str(ch).lower().strip()
        if ch and ch not in seen:
            seen.add(ch)
            out.append(ch)
    # 把默认渠道里缺的按原顺序补到末尾
    for ch in _DEFAULT_REFERRAL_PRIORITY:
        if ch not in seen:
            out.append(ch)
    return out


def get_persona_display(persona_key: Optional[str] = None) -> Dict[str, Any]:
    """返回给前端下拉/弹窗用的 persona 展示包（稳定字段契约）。

    字段均保证存在（从 persona 读取，缺失时填合理默认值），供 /active-persona
    接口直接序列化。
    """
    p = get_persona(persona_key)
    key = p.get("persona_key") or get_default_persona_key()
    # country_code 兜底：persona 未声明时从 locale 取前两位（ja-JP → JP）
    cc = str(p.get("country_code") or "").upper()
    if not cc:
        loc = str(p.get("locale") or "")
        if "-" in loc:
            cc = loc.split("-", 1)[1].upper()
    return {
        "persona_key": key,
        "name": p.get("name") or key,
        "display_flag": p.get("display_flag") or "🌐",
        "display_label": p.get("display_label") or p.get("name") or key,
        "short_label": p.get("short_label") or p.get("name") or key,
        "country_code": cc or "",
        "country_zh": p.get("country_zh") or "",
        "language": p.get("language") or (p.get("locale") or "").split("-", 1)[0],
        "age_min": int(p.get("age_min") or 0) or None,
        "age_max": int(p.get("age_max") or 0) or None,
        "gender": p.get("gender") or "",
        "locale": p.get("locale") or "",
        "interest_topics": list(p.get("interest_topics") or []),
        "seed_group_keywords": list(p.get("seed_group_keywords") or []),
        "referral_priority": get_referral_priority(key),
    }


def list_persona_displays() -> List[Dict[str, Any]]:
    """给前端"目标客群"下拉的精简列表（所有 active persona）。"""
    out = []
    for p in list_personas(only_active=True):
        out.append(get_persona_display(p.get("persona_key")))
    return out
