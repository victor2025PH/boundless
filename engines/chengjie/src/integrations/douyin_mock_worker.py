# -*- coding: utf-8 -*-
"""抖音 echo / mock worker（实施96 P0-3）：在**没有**抖音资质、边车、真机的环境里把整条链跑通。

抖音企业版的官方通道要等企业主体小程序上线 + 能力实验室审核（4–8 周），个人号托管要等边车
落地；而收件箱登记 / 拟稿 / 出站策略拦截 / 出站镜像 / 能力矩阵 / 日报 / UI 美化这些工作
**不该等资质**。本 worker 就是那条「假传输、真链路」：

- 注册为 ``(douyin, web)`` 工厂，**仅当** ``platform_login.douyin.mock_enabled: true``
  （默认关 → 生产零痕迹；桌面种子与 example 都不写这个键）。
- ``send`` / ``send_media``：只记录并回 ``delivered=True``；可选延迟后用
  ``protocol_bridge.emit_incoming`` 造一条「客户回声」入站，让拟稿链、SSE、未读角标、
  客户面板都有真实数据流（``echo_delay_sec < 0`` 关闭回声）。
- ``mark_read`` / ``send_chat_action``：记录并返回 True——能力矩阵按 ``hasattr`` 判定，
  这样 UI 侧「已读 / 正在输入」的拟人链在演示里也能走通。
- ``simulate_inbound()``：演示或测试注入一条进线（如「多少钱」），不经任何平台。

**它不是抖音接入**：能力矩阵（``platform_capabilities.WORKERS``）刻意不登记它，
``platform_readiness._IMPLEMENTED_MODES`` 也不加 ``douyin``——注册表里抖音仍是
``implemented=False``，事实卡仍答「暂不支持」。真实 worker 落地时替换工厂名即可，
本模块整体删除不影响任何其它代码。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PLATFORM = "douyin"
MODE = "web"
DEFAULT_ECHO_DELAY_SEC = 1.5
DEFAULT_DEMO_ACCOUNT = "demo"


def mock_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """``platform_login.douyin`` 块 → 归一（缺省全关）。"""
    try:
        blk = dict((((config or {}).get("platform_login") or {}).get(PLATFORM)) or {})
    except Exception:
        blk = {}
    try:
        delay = float(blk.get("mock_echo_delay_sec", DEFAULT_ECHO_DELAY_SEC))
    except (TypeError, ValueError):
        delay = DEFAULT_ECHO_DELAY_SEC
    return {
        "mock_enabled": bool(blk.get("mock_enabled", False)),
        "mock_echo_delay_sec": delay,
        "mock_auto_account": bool(blk.get("mock_auto_account", True)),
        "mock_account_id": str(blk.get("mock_account_id") or DEFAULT_DEMO_ACCOUNT),
    }


def mock_enabled(config: Optional[Dict[str, Any]]) -> bool:
    return mock_cfg(config)["mock_enabled"]


class DouyinMockWorker:
    """纯内存 worker；契约见 ``account_orchestrator`` 模块头（start/stop/healthy/status/send…）。"""

    def __init__(self, account: Dict[str, Any], config: Optional[Dict[str, Any]] = None) -> None:
        self.account_id = str((account or {}).get("account_id") or DEFAULT_DEMO_ACCOUNT)
        self.meta: Dict[str, Any] = dict((account or {}).get("meta") or {})
        self.config = config or {}
        self._cfg = mock_cfg(self.config)
        self.running = False
        self.started_at = 0.0
        self.sent: List[Tuple[str, str]] = []
        self.media: List[Tuple[str, str, str]] = []
        self.read_marks: List[str] = []
        self.actions: List[Tuple[str, str]] = []
        self._seq = 0
        self._echo_tasks: List[Any] = []

    # ── 生命周期 ──
    async def start(self) -> None:
        self.running = True
        self.started_at = time.time()
        logger.info("[douyin-mock] worker 启动 account=%s（演示用假传输）", self.account_id)

    async def stop(self) -> None:
        self.running = False
        for t in self._echo_tasks:
            try:
                t.cancel()
            except Exception:
                pass
        self._echo_tasks.clear()

    async def healthy(self) -> bool:
        return self.running

    def status(self) -> Dict[str, Any]:
        return {
            "type": "douyin_mock", "running": self.running, "mock": True,
            "sent": len(self.sent), "media": len(self.media),
            "echo_delay_sec": self._cfg["mock_echo_delay_sec"],
            "started_at": self.started_at,
        }

    # ── 出站契约 ──
    def _next_id(self) -> str:
        self._seq += 1
        return f"mock-{self.account_id}-{self._seq}"

    async def send(self, chat_key: str, text: str, *,
                   reply_to: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.sent.append((str(chat_key), str(text)))
        mid = self._next_id()
        self._schedule_echo(chat_key, f"[抖音演示] 收到：{str(text)[:80]}")
        return {"delivered": True, "message_id": mid, "mock": True}

    async def send_media(self, chat_key: str, *, media_path: str, media_type: str,
                         caption: str = "") -> Dict[str, Any]:
        self.media.append((str(chat_key), str(media_type), str(media_path)))
        mid = self._next_id()
        self._schedule_echo(chat_key, f"[抖音演示] 收到一条{media_type or '媒体'}")
        return {"delivered": True, "message_id": mid, "mock": True}

    async def mark_read(self, chat_key: str) -> bool:
        self.read_marks.append(str(chat_key))
        return True

    async def send_chat_action(self, chat_key: str, action: str = "typing") -> bool:
        self.actions.append((str(chat_key), str(action)))
        return True

    # ── 演示注入 ──
    def simulate_inbound(self, text: str, chat_key: str = "douyin:user:demo",
                         name: str = "抖音演示客户", ts: Optional[float] = None) -> Dict[str, Any]:
        """造一条客户进线（同步；返回已 emit 的消息 dict 便于断言）。"""
        from src.integrations.protocol_bridge import emit_incoming, make_message
        msg = make_message(
            platform=PLATFORM, account_id=self.account_id, chat_key=str(chat_key),
            text=str(text), name=name, ts=float(ts or time.time()),
            msg_id=f"mock-in-{int((ts or time.time()) * 1000)}", direction="in",
            source={"mock": True, "conversation_id": str(chat_key),
                    "server_message_id": f"mock-in-{int((ts or time.time()) * 1000)}"},
        )
        emit_incoming(msg)
        return msg

    def _schedule_echo(self, chat_key: str, text: str) -> None:
        delay = float(self._cfg["mock_echo_delay_sec"])
        if delay < 0:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _later() -> None:
            try:
                if delay > 0:
                    await asyncio.sleep(delay)
                if self.running:
                    self.simulate_inbound(text, chat_key=chat_key)
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.debug("[douyin-mock] 回声失败", exc_info=True)

        self._echo_tasks.append(loop.create_task(_later()))


def register_douyin_mock_worker(config: Optional[Dict[str, Any]], *, registry: Any = None) -> bool:
    """按开关注册 ``(douyin, web)`` 工厂（幂等）；开着且 ``mock_auto_account`` 时确保演示账号在线。

    返回是否注册了工厂。**默认关**：``platform_login.douyin.mock_enabled`` 缺省 False。
    """
    cfg = mock_cfg(config)
    if not cfg["mock_enabled"]:
        return False
    from src.integrations.account_orchestrator import get_worker_factory, register_worker
    if get_worker_factory(PLATFORM, MODE) is None:
        register_worker(PLATFORM, MODE, lambda acc, c: DouyinMockWorker(acc, c))
        logger.warning("[douyin-mock] 已注册抖音演示 worker（platform_login.douyin.mock_enabled=true，"
                       "仅供演示/测试；不是抖音接入）")
    if cfg["mock_auto_account"]:
        try:
            reg = registry
            if reg is None:
                from src.integrations.account_registry import get_account_registry
                reg = get_account_registry()
            reg.upsert(PLATFORM, cfg["mock_account_id"], mode=MODE, label="抖音演示（mock）",
                       status="online", meta={"mock": True}, merge_meta=True)
        except Exception:
            logger.debug("[douyin-mock] 演示账号登记失败（不影响工厂注册）", exc_info=True)
    return True


__all__ = ["PLATFORM", "MODE", "DouyinMockWorker", "mock_cfg", "mock_enabled",
           "register_douyin_mock_worker"]
