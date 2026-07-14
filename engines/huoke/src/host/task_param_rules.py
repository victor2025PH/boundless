# -*- coding: utf-8 -*-
"""
任务参数归一化 — 按 config/task_param_schemas.yaml 合并默认值、类型与边界。
可选与 task_execution_policy.yaml 中 normalize_task_params 联动。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from src.host._yaml_cache import YamlCache
from src.host.audience_preset import merge_audience_preset
from src.host.device_registry import config_file

logger = logging.getLogger(__name__)

_SCHEMA_PATH = config_file("task_param_schemas.yaml")


def _post_process(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    return {"version": 1, "options": {}, "apps": {}}


_CACHE = YamlCache(
    path=_SCHEMA_PATH,
    defaults={"version": 1, "options": {}, "apps": {}},
    post_process=_post_process,
    log_label="task_param_schemas.yaml",
    logger=logger,
)


def load_schemas(force_reload: bool = False) -> Dict[str, Any]:
    return _CACHE.get(force_reload=force_reload)


def policy_normalize_enabled() -> bool:
    try:
        from src.host.task_policy import load_task_execution_policy

        p = load_task_execution_policy()
        return bool(p.get("normalize_task_params", True))
    except Exception:
        return True


def _allow_unknown_keys() -> bool:
    """strict_task_params=true 时禁止未知键（与 schema options 求交集）。"""
    data = load_schemas()
    schema_allow = bool((data.get("options") or {}).get("allow_unknown_keys", True))
    try:
        from src.host.task_policy import load_task_execution_policy

        if load_task_execution_policy().get("strict_task_params"):
            return False
    except Exception:
        pass
    return schema_allow


def _find_app_for_task(task_type: str) -> Optional[str]:
    if not task_type:
        return None
    for app in ("tiktok", "telegram", "whatsapp", "linkedin", "facebook", "instagram"):
        if task_type.startswith(app + "_"):
            return app
    return None


def resolve_schema_entry(task_type: str) -> Tuple[str, Dict[str, Any]]:
    """
    返回 (匹配的 prefix 键, schema 块)。
    无匹配时返回 ("", {})。
    """
    app = _find_app_for_task(task_type)
    if not app:
        return "", {}
    data = load_schemas()
    apps = data.get("apps") or {}
    block = apps.get(app) or {}
    tasks = block.get("tasks_by_prefix") or {}
    if not tasks:
        return "", {}
    if task_type in tasks:
        return task_type, tasks[task_type]
    best_p = ""
    best_spec: Dict[str, Any] = {}
    best_len = -1
    for prefix, spec in tasks.items():
        if task_type.startswith(prefix) and len(prefix) > best_len:
            best_len = len(prefix)
            best_p = prefix
            best_spec = spec if isinstance(spec, dict) else {}
    return best_p, best_spec


def _coerce_scalar(val: Any, spec: Dict[str, Any]) -> Any:
    t = (spec.get("type") or "string").lower()
    if val is None:
        return None
    try:
        if t == "int":
            x = int(float(val))
        elif t == "float":
            x = float(val)
        elif t == "bool":
            if isinstance(val, bool):
                x = val
            else:
                s = str(val).strip().lower()
                x = s in ("1", "true", "yes", "on", "是")
        else:
            x = str(val).strip()
    except (TypeError, ValueError):
        return spec.get("default")

    if t == "int":
        mn, mx = spec.get("min"), spec.get("max")
        if mn is not None:
            x = max(int(mn), x)
        if mx is not None:
            x = min(int(mx), x)
    if t == "float":
        mn, mx = spec.get("min"), spec.get("max")
        if mn is not None:
            x = max(float(mn), x)
        if mx is not None:
            x = min(float(mx), x)

    enum = spec.get("enum")
    if isinstance(enum, list) and enum:
        if t == "string":
            xs = str(x).lower()
            el = [str(e).lower() for e in enum]
            if xs not in el:
                return spec.get("default", enum[0])
            # 还原为 yaml 中大小写（取第一个匹配）
            for e in enum:
                if str(e).lower() == xs:
                    return e
        elif x not in enum:
            return spec.get("default", enum[0])

    return x


def normalize_params(task_type: str, params: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[str]]:
    """
    返回 (归一化后的 params, warnings)。
    未知键默认保留（allow_unknown_keys）。
    """
    raw = dict(params or {})
    allow_unknown = _allow_unknown_keys()

    prefix, spec_block = resolve_schema_entry(task_type)
    fields = (spec_block.get("fields") or {}) if spec_block else {}
    if not fields:
        return raw, []

    out = dict(raw)
    warnings: List[str] = []

    for key, fspec in fields.items():
        if not isinstance(fspec, dict):
            continue
        if key not in out and "default" in fspec:
            out[key] = fspec["default"]
        elif key in out:
            old = out[key]
            new = _coerce_scalar(old, fspec)
            if old != new and old not in (None, ""):
                warnings.append(f"{key}: 已调整 {old!r} → {new!r}")
            out[key] = new

    if not allow_unknown:
        allowed = set(fields.keys())
        extra = [k for k in list(out.keys()) if k not in allowed and not str(k).startswith("_")]
        for k in extra:
            warnings.append(f"移除未知参数: {k}")
            del out[k]

    if prefix:
        logger.debug("task params schema matched prefix=%s task_type=%s", prefix, task_type)

    return out, warnings


def prepare_task_params(task_type: str, params: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[str]]:
    """人群预设合并 + 归一化；返回 (params, 全部提示语)。"""
    merged, preset_notes = merge_audience_preset(task_type, dict(params or {}))
    norm, warnings = normalize_params(task_type, merged)
    return norm, preset_notes + warnings


def maybe_normalize_for_task(task_type: str, params: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """创建任务前调用：策略关闭时原样返回（仍展开 audience_preset）。"""
    if not policy_normalize_enabled():
        merged, _ = merge_audience_preset(task_type, dict(params or {}))
        return merged
    out, warns = prepare_task_params(task_type, params)
    for w in warns:
        logger.info("[task_params] %s: %s", task_type, w)
    return out


def schema_prompt_fragment(task_type: str) -> str:
    """供 AI 精炼时注入 system 片段。"""
    _, spec_block = resolve_schema_entry(task_type)
    if not spec_block:
        return f"任务类型: {task_type}\n输出 JSON 对象，键为任务参数字段。"
    lines = [f"任务: {spec_block.get('label', task_type)}", "字段:"]
    fields = spec_block.get("fields") or {}
    for k, fspec in fields.items():
        if not isinstance(fspec, dict):
            continue
        desc = fspec.get("description", "")
        lines.append(f"  - {k}: type={fspec.get('type','string')} default={fspec.get('default')} {desc}".strip())
    return "\n".join(lines)


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    """从 LLM 输出中抠 JSON 对象。"""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            return None
    return None


def ai_refine_params(task_type: str, natural_language: str, base_params: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], List[str], str]:
    """
    使用 LLM 将自然语言与 base 合并为参数，再归一化。
    先展开 audience_preset，再与 LLM 输出合并（显式键优先）。
    返回 (params, warnings, raw_llm_snippet)。
    """
    base = dict(base_params or {})
    base_merged, preset_notes = merge_audience_preset(task_type, base)
    fragment = schema_prompt_fragment(task_type)
    user = (
        f"用户说明:\n{natural_language}\n\n"
        f"已有参数(JSON):\n{json.dumps(base_merged, ensure_ascii=False)}\n\n"
        "请只输出一个 JSON 对象，合并用户意图到参数中；不要解释；不要 markdown。"
    )
    raw_snippet = ""
    try:
        from src.ai.llm_client import get_llm_client

        client = get_llm_client()
        messages = [
            {"role": "system", "content": "你是 OpenClaw 任务参数提取器。\n" + fragment},
            {"role": "user", "content": user},
        ]
        raw = client.chat_messages(messages, temperature=0.05, max_tokens=800)
        raw_snippet = (raw or "")[:2000]
        merged = extract_json_object(raw)
        if not isinstance(merged, dict):
            merged = {}
        combined = {**base_merged, **merged}
        norm, warns = normalize_params(task_type, combined)
        return norm, preset_notes + warns, raw_snippet
    except Exception as e:
        logger.warning("ai_refine_params 失败: %s", e)
        norm, warns = normalize_params(task_type, base_merged)
        return norm, preset_notes + warns + [f"ai_refine_failed: {e}"], raw_snippet
