# -*- coding: utf-8 -*-
"""本机 OpenClaw API 监听端口（单一默认值，减少与常见 8000 端口冲突）。"""
from __future__ import annotations

import os

# 默认监听端口；可通过环境变量 OPENCLAW_PORT 覆盖
DEFAULT_OPENCLAW_PORT: int = 18080


def openclaw_port() -> int:
    v = os.environ.get("OPENCLAW_PORT", "").strip()
    if v:
        return int(v)
    return DEFAULT_OPENCLAW_PORT


def local_api_base(host: str = "127.0.0.1") -> str:
    """本机 OpenClaw HTTP 根地址（无尾斜杠）。"""
    return f"http://{host}:{openclaw_port()}"
