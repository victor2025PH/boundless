# -*- coding: utf-8 -*-
"""任务参数：模式查询、归一化校验、AI 精炼。"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request, Security
from fastapi.security import APIKeyHeader

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/task-params", tags=["task-params"])


async def _verify_api_key(request: Request,
                          key: Optional[str] = Security(
                              APIKeyHeader(name="X-API-Key", auto_error=False))):
    from ..api import verify_api_key
    await verify_api_key(request, key)


_auth = [Depends(_verify_api_key)]


@router.get("/schema/{task_type}", dependencies=_auth)
def get_param_schema(task_type: str):
    """返回某任务类型匹配到的 YAML 模式（字段说明）。"""
    from src.host.task_param_rules import load_schemas, resolve_schema_entry

    prefix, spec = resolve_schema_entry(task_type)
    return {
        "ok": True,
        "task_type": task_type,
        "matched_prefix": prefix,
        "label": spec.get("label", ""),
        "fields": spec.get("fields", {}),
        "schema_version": load_schemas().get("version", 1),
    }


@router.post("/normalize", dependencies=_auth)
def post_normalize(body: dict):
    """人群预设合并 + 归一化 + 警告（与创建任务路径一致），不写库。"""
    from src.host.task_param_rules import prepare_task_params

    task_type = (body.get("task_type") or body.get("type") or "").strip()
    params = body.get("params") or {}
    if not task_type:
        return {"ok": False, "error": "task_type 必填"}
    norm, warnings = prepare_task_params(task_type, params)
    return {"ok": True, "task_type": task_type, "params": norm, "warnings": warnings}


@router.get("/audience-presets", dependencies=_auth)
def get_audience_presets(if_etag: Optional[str] = Query(None, description="上次响应的 etag，未变则返回 unchanged=true")):
    """
    列出 config/audience_presets.yaml 中的人群预设摘要。
    响应含 etag / version / mtime；若传入 if_etag 且与当前一致，则 presets 为 null 且 unchanged=true。
    """
    from src.host.audience_preset import audience_presets_etag, list_presets, load_presets

    etag, version, mtime = audience_presets_etag()
    if if_etag and if_etag.strip() == etag:
        return {
            "ok": True,
            "unchanged": True,
            "etag": etag,
            "version": version,
            "mtime": mtime,
            "presets": None,
        }
    # 完整响应前强制从磁盘重载 YAML，避免进程内缓存与文件不一致
    load_presets(force_reload=True)
    etag, version, mtime = audience_presets_etag()
    return {
        "ok": True,
        "etag": etag,
        "version": version,
        "mtime": mtime,
        "presets": list_presets(),
    }


@router.post("/reload-audience-presets", dependencies=_auth)
def post_reload_audience_presets():
    """
    强制从磁盘重载 config/audience_presets.yaml（含 merge_audience_preset 所用进程内缓存）。
    供运维/控制台「刷新预设」与脚本调用；返回最新 etag 与摘要列表。
    """
    from src.host.audience_preset import audience_presets_etag, list_presets, load_presets

    load_presets(force_reload=True)
    etag, version, mtime = audience_presets_etag()
    return {
        "ok": True,
        "etag": etag,
        "version": version,
        "mtime": mtime,
        "presets": list_presets(),
    }


@router.post("/ai-refine", dependencies=_auth)
def post_ai_refine(body: dict):
    """
    自然语言 + 可选已有参数 → LLM 提取 JSON → 归一化。
    Body: { "task_type": "tiktok_follow", "text": "...", "params": {} }
    """
    from src.host.task_param_rules import ai_refine_params

    task_type = (body.get("task_type") or "").strip()
    text = (body.get("text") or body.get("message") or "").strip()
    base = body.get("params") or {}
    if not task_type or not text:
        return {"ok": False, "error": "task_type 与 text 必填"}
    norm, warnings, raw = ai_refine_params(task_type, text, base)
    return {
        "ok": True,
        "task_type": task_type,
        "params": norm,
        "warnings": warnings,
        "llm_preview": raw[:500] if raw else "",
    }
