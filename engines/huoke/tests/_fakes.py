# -*- coding: utf-8 -*-
"""Phase 20.3 (2026-04-25): A 侧 e2e 联调用的 fake B Messenger harness.

用途:
  * A 侧能在没真 B 实装的情况下 e2e 测试整个 referral 闭环
  * B 实装后, 可保留这套 fake 作为 CI 回归 (不需要真机)
  * B 侧反向参考: 看 A 怎么 mock B, 反推 B 真实接口契约

设计原则:
  * 接口签名严格匹配 docs/A_TO_B_PHASE20_INBOX.md §3 的 spec
  * 支持配置化对话池 (peer_name → 文本) 模拟不同 reply 场景
  * 记录所有 send_message / check_messenger_inbox 调用供 assertion
  * 不引入真 uiautomator / device 依赖, 纯进程内
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


class FakeBMessenger:
    """模拟 B 侧 FacebookAutomation 实装的 Messenger 部分.

    构造:
      conversations: dict {peer_name: last_inbound_text}
        — 配置某 peer 在 inbox 里的最新一条入站文本; 没配则不返
      send_should_fail: set of peer_names — 调 send_message 这些 peer 时 raise
      send_returns: dict {peer: dict(success/error)} — 自定单 peer send 结果

    属性:
      send_log: 所有 send_message 调用记录
      inbox_calls: 所有 check_messenger_inbox 调用记录
    """

    def __init__(self,
                  conversations: Optional[Dict[str, str]] = None,
                  send_should_fail: Optional[set] = None,
                  send_returns: Optional[Dict[str, Dict[str, Any]]] = None,
                  inbound_times: Optional[Dict[str, str]] = None):
        self._conversations = conversations or {}
        self._send_fail = send_should_fail or set()
        self._send_returns = send_returns or {}
        self._inbound_times = inbound_times or {}
        self.send_log: List[Dict[str, Any]] = []
        self.inbox_calls: List[Dict[str, Any]] = []

    # ─── send_message — _fb_send_referral_replies 用 ─────────────────

    def send_message(self, peer_name: str, message: str = "",
                       *, raise_on_error: bool = False, **kwargs):
        """模拟 facebook.send_message.

        kwargs 接收 device_id / chat_lookup_strategy / 其他 send_referral_replies
        会传的参数, 全部记入 send_log.
        """
        call = {
            "peer_name": peer_name,
            "message": message,
            "kwargs": dict(kwargs),
        }
        self.send_log.append(call)

        if peer_name in self._send_fail:
            from src.app_automation.facebook import MessengerError
            err = MessengerError("fake_failure", "fake send failed")
            if raise_on_error:
                raise err
            return {"success": False, "error": str(err), "code": "fake_failure"}

        if peer_name in self._send_returns:
            return self._send_returns[peer_name]

        return {"success": True, "peer_name": peer_name,
                "message_sent": message[:80]}

    # ─── check_messenger_inbox — _fb_check_referral_replies 用 ──────

    def check_messenger_inbox(self, *, auto_reply: bool = False,
                                 referral_mode: bool = False,
                                 peers_filter: Optional[List[str]] = None,
                                 max_messages_per_peer: int = 5,
                                 device_id: str = "",
                                 max_conversations: int = 20,
                                 **kwargs) -> Dict[str, Any]:
        """按 docs/A_TO_B_PHASE20_INBOX.md §3 spec 实现 referral_mode.

        referral_mode=False 时退化为 messenger_active 状态返回 (legacy 兼容).
        """
        call = {
            "auto_reply": auto_reply,
            "referral_mode": referral_mode,
            "peers_filter": list(peers_filter) if peers_filter else None,
            "max_messages_per_peer": max_messages_per_peer,
            "device_id": device_id,
            "extra": dict(kwargs),
        }
        self.inbox_calls.append(call)

        if not referral_mode:
            return {"messenger_active": True}

        convs: List[Dict[str, Any]] = []
        peers_to_check = peers_filter or list(self._conversations.keys())
        for peer in peers_to_check:
            if peer not in self._conversations:
                continue
            convs.append({
                "peer_name": peer,
                "last_inbound_text": self._conversations[peer],
                "last_inbound_time": self._inbound_times.get(peer, ""),
                "conv_id": f"fake-conv-{peer}",
            })
        return {"messenger_active": True, "conversations": convs}

    # ─── helpers for tests ────────────────────────────────────────────

    def add_inbound(self, peer_name: str, text: str,
                      inbound_time: str = "") -> None:
        """运行中追加一条 peer inbound 消息 (模拟用户后续才回复)."""
        self._conversations[peer_name] = text
        if inbound_time:
            self._inbound_times[peer_name] = inbound_time

    def clear_inbox(self) -> None:
        self._conversations.clear()
        self._inbound_times.clear()

    @property
    def send_count(self) -> int:
        return len(self.send_log)

    @property
    def inbox_call_count(self) -> int:
        return len(self.inbox_calls)
