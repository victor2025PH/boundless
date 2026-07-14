# -*- coding: utf-8 -*-
"""任务来源标记：统一为 params 注入 _created_via（setdefault，不覆盖已有）。"""

from __future__ import annotations

from typing import Any, Dict, Optional


def with_origin(params: Optional[Dict[str, Any]], origin: str) -> Dict[str, Any]:
    """合并 params，仅在未设置 _created_via 时写入。"""
    p = dict(params or {})
    p.setdefault("_created_via", origin)
    return p
