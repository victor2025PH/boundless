# -*- coding: utf-8 -*-
"""
OpenClaw Agent — mobile-auto 接入 Core (openclaw-voice) 的客户端。

基于 OpenClawAdapterBase SDK，提供标准注册/心跳/事件推送/AI路由。

使用方式：
  在 api.py lifespan 中调用 start_openclaw_agent() / stop_openclaw_agent()

环境变量：
  OPENCLAW_CORE_URL    — Core地址，如 http://192.168.0.100:9765（不设则不启用）
  OPENCLAW_CORE_TOKEN  — Core API Token（对应 Core 的 OPENCLAW_API_KEY）
  OPENCLAW_SYSTEM_ID   — 本系统标识符（默认 "mobile-auto"）
  OPENCLAW_SDK_PATH    — 含 ``openclaw_adapter_base.py`` 的目录，可多路径，用 `os.pathsep` 分隔（**优先于**项目根 ``shared/sdk`` 与历史 parents 探测）
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.host.device_registry import PROJECT_ROOT

logger = logging.getLogger("openclaw.agent")

# ─── SDK 导入（支持相对路径和 shared/sdk 两种部署方式）──────────────────────

def _load_sdk():
    """尝试从 shared/sdk 加载 OpenClawAdapterBase"""
    candidates: list[Path] = []
    _raw = (os.environ.get("OPENCLAW_SDK_PATH") or "").strip()
    if _raw:
        for p in _raw.split(os.pathsep):
            s = p.strip()
            if s:
                candidates.append(Path(s).expanduser())
    _here = Path(__file__).resolve()
    _pt = _here.parents
    # 深路径多仓库布局；浅路径（如单测/临时）可能没有 parents[5]，须避免 IndexError
    _legacy: list[Path] = [PROJECT_ROOT / "shared" / "sdk"]
    for idx, _suffix in ((5, "xlx2026/shared/sdk"), (4, "shared/sdk"), (3, "shared/sdk")):
        if idx < len(_pt):
            p = _pt[idx]
            for part in _suffix.split("/"):
                p = p / part
            _legacy.append(p)
    candidates += _legacy
    for sdk_path in candidates:
        if (sdk_path / "openclaw_adapter_base.py").exists():
            if str(sdk_path) not in sys.path:
                sys.path.insert(0, str(sdk_path))
            break

_load_sdk()

try:
    from openclaw_adapter_base import OpenClawAdapterBase, AdapterConfig
    _SDK_AVAILABLE = True
except ImportError:
    _SDK_AVAILABLE = False
    logger.warning("OpenClawAdapterBase SDK 未找到，使用内置简化版本")


# ─── Adapter 实现 ──────────────────────────────────────────────────────────

if _SDK_AVAILABLE:
    class MobileAutoAdapter(OpenClawAdapterBase):
        """Mobile-Auto 子系统 Adapter，继承标准基类"""

        SYSTEM_ID = os.environ.get("OPENCLAW_SYSTEM_ID", "mobile-auto")
        VERSION = "2.0.0"
        CAPABILITIES = ["tiktok", "telegram", "whatsapp", "phone_cluster"]

        def _get_port(self) -> int:
            from src.openclaw_env import openclaw_port

            return openclaw_port()

        async def on_message(
            self,
            device_id: str,
            session_id: str,
            message: str,
            context: Optional[List[Dict]] = None,
        ) -> Optional[str]:
            """通过 Core AI 处理入站消息"""
            return await self.call_core_ai(
                device_id=device_id,
                session_id=session_id,
                message=message,
                context=context,
            )

        async def on_start(self):
            logger.info("Mobile-Auto Adapter 就绪，Core: %s", self.config.core_url)

else:
    # ─── Fallback: 不依赖 SDK 的内置简化实现 ─────────────────────────────

    import httpx
    import socket

    _CORE_URL = os.environ.get("OPENCLAW_CORE_URL", "").rstrip("/")
    _CORE_TOKEN = os.environ.get("OPENCLAW_CORE_TOKEN", "")
    _SYSTEM_ID = os.environ.get("OPENCLAW_SYSTEM_ID", "mobile-auto")
    _HEARTBEAT_INTERVAL = 300

    def _headers():
        h = {"Content-Type": "application/json"}
        if _CORE_TOKEN:
            h["Authorization"] = f"Bearer {_CORE_TOKEN}"
        return h

    def _local_ip():
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                return s.getsockname()[0]
        except Exception:
            return ""

    class MobileAutoAdapter:
        SYSTEM_ID = _SYSTEM_ID

        def __init__(self):
            self._enabled = bool(_CORE_URL)
            self._task: Optional[asyncio.Task] = None

        async def push_event(self, event_type: str, data: Dict[str, Any]) -> bool:
            if not _CORE_URL:
                return False
            try:
                async with httpx.AsyncClient(timeout=8.0) as client:
                    resp = await client.post(
                        f"{_CORE_URL}/api/platform/event",
                        json={"source": _SYSTEM_ID, "type": event_type, "data": data},
                        headers=_headers(),
                    )
                    return resp.status_code < 400
            except Exception as e:
                logger.debug("Core 事件推送异常: %s", e)
                return False

        async def _register(self) -> bool:
            if not _CORE_URL:
                return False
            try:
                from src.openclaw_env import openclaw_port

                _port = openclaw_port()
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.post(
                        f"{_CORE_URL}/api/platform/register",
                        json={
                            "system_id": _SYSTEM_ID,
                            "name": "OpenClaw Mobile Auto",
                            "url": f"http://{_local_ip()}:{_port}",
                            "version": "2.0.0",
                            "capabilities": ["tiktok", "telegram", "whatsapp", "phone_cluster"],
                        },
                        headers=_headers(),
                    )
                    ok = resp.status_code < 400
                    if ok:
                        logger.info("已注册到 Core: %s", _CORE_URL)
                    return ok
            except Exception as e:
                logger.debug("Core 注册异常: %s", e)
                return False

        async def _loop(self):
            for attempt in range(3):
                if await self._register():
                    break
                await asyncio.sleep(5 * (attempt + 1))
            while True:
                await asyncio.sleep(_HEARTBEAT_INTERVAL)
                try:
                    async with httpx.AsyncClient(timeout=8.0) as client:
                        await client.post(
                            f"{_CORE_URL}/api/platform/heartbeat",
                            json={"system_id": _SYSTEM_ID},
                            headers=_headers(),
                        )
                except Exception:
                    pass

        async def start(self) -> bool:
            if not self._enabled:
                return False
            self._task = asyncio.create_task(self._loop())
            return True

        async def stop(self):
            if self._task and not self._task.done():
                self._task.cancel()
                self._task = None


# ─── 模块级单例 & 向后兼容接口 ─────────────────────────────────────────────

_adapter: Optional[MobileAutoAdapter] = None


async def push_event(event_type: str, data: Dict[str, Any]) -> bool:
    """
    向 Core 推送业务事件（模块级接口，向后兼容）。

    示例：
      await push_event("lead_acquired", {"platform": "tiktok", "account": "xxx"})
    """
    global _adapter
    if _adapter is None:
        return False
    return await _adapter.push_event(event_type, data)


def start_openclaw_agent():
    """在 lifespan 中调用，启动后台 agent loop（仅当 OPENCLAW_CORE_URL 设置时）"""
    global _adapter
    core_url = os.environ.get("OPENCLAW_CORE_URL", "")
    if not core_url:
        logger.debug("OPENCLAW_CORE_URL 未设置，Core 接入已跳过")
        return

    if _SDK_AVAILABLE:
        _adapter = MobileAutoAdapter()
    else:
        _adapter = MobileAutoAdapter()  # fallback 版本

    asyncio.create_task(_adapter.start())
    logger.info("OpenClaw Agent 已启动，Core: %s", core_url)


def stop_openclaw_agent():
    """在 lifespan shutdown 中调用"""
    global _adapter
    if _adapter is not None:
        asyncio.create_task(_adapter.stop())
        _adapter = None
