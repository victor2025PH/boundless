# -*- coding: utf-8 -*-
"""个人微信 PC 副驾 · 驱动协议与假后端（实施97 线 B）。

``WeChatPcBackend`` 是 service / send_guard 唯一依赖的面：真机实现在 ``uia_backend.UiaBackend``，
单测/离线开发用 :class:`FakeBackend`。协议刻意**同步、无 UIA 类型**——UIA 调用在驱动进程内本就
串行（一个鼠标、一个前台窗口），异步只会带来伪并发；service 用线程池包一层即可。

所有读方法失败返回空/None、写方法返回 False，**绝不抛**——一次读屏失败不该让整个循环崩掉。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Protocol, runtime_checkable


@dataclass
class SessionRow:
    display_name: str
    unread: int = 0
    preview: str = ""
    is_group: bool = False
    raw_name: str = ""          # UIA 原始 Name（含时间/预览拼接时保留以便排错）
    index: int = -1             # 在会话列表中的位置（同名多格时用于定位；-1=未知）


@dataclass
class Bubble:
    text: str
    is_self: bool = False
    sender: str = ""            # 群聊发言人；私聊为空
    kind: str = "text"          # text | image | voice | file | video | sticker | link | card | location | system | unknown
    ts_hint: float = 0.0        # 屏幕上的时间戳（微信只间隔显示，多数为 0）
    runtime_id: str = ""        # UIA RuntimeId（元素存活期内稳定，用作屏内去重）
    index: int = 0              # 在可见列表中的位置
    direction_known: bool = True  # 己方/对方是否判定成功（4.x 无属性，靠像素；截不到图＝False → 不入站）
    ts_is_upper_bound: bool = False  # ts_hint 只是上界（可见区顶部无时间条，取下方时间条−1s）：去重按「时间未知」处理


@dataclass
class ScreenState:
    window_present: bool
    window_class: str = ""
    logged_in: bool = False
    dialog_texts: List[str] = field(default_factory=list)
    current_title: str = ""


@runtime_checkable
class WeChatPcBackend(Protocol):
    def screen_state(self) -> ScreenState: ...
    def list_sessions(self, limit: int = 50) -> List[SessionRow]: ...
    def open_session(self, display_name: str, *, expected_wxid: str = "", index: int = -1) -> bool: ...
    def current_title(self) -> str: ...
    def read_visible_messages(self) -> List[Bubble]: ...
    def read_composer(self) -> str: ...
    def set_composer(self, text: str) -> bool: ...
    def press_send(self) -> bool: ...
    def read_profile_wxid(self, display_name: str = "") -> str: ...
    def peer_typing(self) -> bool: ...


class FakeBackend:
    """脚本化假后端：``sessions`` / ``messages[name]`` 可随时改；记录所有动作。"""

    def __init__(self) -> None:
        self.present = True
        self.window_class = "mmui::MainWindow"
        self.logged_in = True
        self.dialogs: List[str] = []
        self.sessions: List[SessionRow] = []
        self.messages: Dict[str, List[Bubble]] = {}
        self.wxids: Dict[str, str] = {}
        self.wxids_by_index: Dict[int, str] = {}
        self.current: str = ""
        self.current_index: int = -1
        self.typing_polls_left: int = 0
        self.titles: Dict[str, str] = {}
        self.composer: str = ""
        self.actions: List[tuple] = []
        self.fail_open: set = set()
        self.fail_set_composer = False
        self.fail_send = False
        self.echo_on_send = True   # 回车后把输入框内容作为己方气泡追加（模拟真实客户端）

    def screen_state(self) -> ScreenState:
        return ScreenState(self.present, self.window_class, self.logged_in, list(self.dialogs),
                           self.current)

    def list_sessions(self, limit: int = 50) -> List[SessionRow]:
        self.actions.append(("list_sessions",))
        rows = list(self.sessions[:limit])
        for i, r in enumerate(rows):
            if r.index < 0:
                r.index = i
        return rows

    def open_session(self, display_name: str, *, expected_wxid: str = "", index: int = -1) -> bool:
        """同名多格：``wxids_by_index`` 给了则按格核对微信号（模拟真机逐格看资料卡）。"""
        self.actions.append(("open_session", display_name, expected_wxid, index))
        if display_name in self.fail_open:
            return False
        candidates = [i for i, s in enumerate(self.sessions) if s.display_name == display_name]
        if 0 <= index < len(self.sessions) and self.sessions[index].display_name == display_name:
            candidates = [index] + [i for i in candidates if i != index]
        if not candidates:
            return False
        by_idx = getattr(self, "wxids_by_index", {})
        from src.integrations.wechat_pc.identity import parse_wxid
        for i in candidates:
            wx = parse_wxid(by_idx.get(i, self.wxids.get(display_name, "")))
            if expected_wxid and wx != expected_wxid:
                continue
            self.current = display_name
            self.current_index = i
            self.sessions[i].unread = 0
            return True
        return False

    def current_title(self) -> str:
        """``titles`` 可为某会话指定标题（群聊「群名 (12)」形态）。"""
        return getattr(self, "titles", {}).get(self.current, self.current)

    def read_visible_messages(self) -> List[Bubble]:
        self.actions.append(("read_visible_messages", self.current))
        return list(self.messages.get(self.current, []))

    def read_composer(self) -> str:
        return self.composer

    def set_composer(self, text: str) -> bool:
        self.actions.append(("set_composer", text))
        if self.fail_set_composer:
            return False
        self.composer = text
        return True

    def press_send(self) -> bool:
        self.actions.append(("press_send", self.current))
        if self.fail_send:
            return False
        if self.echo_on_send and self.composer:
            lst = self.messages.setdefault(self.current, [])
            lst.append(Bubble(self.composer, is_self=True, ts_hint=time.time(),
                              runtime_id=f"rt{len(lst) + 1}", index=len(lst)))
        self.composer = ""
        return True

    def peer_typing(self) -> bool:
        """``typing_polls_left`` > 0 期间报「对方正在输入」，每问一次减一（模拟对方打字若干秒后停下）。"""
        self.actions.append(("peer_typing", self.current))
        if self.typing_polls_left > 0:
            self.typing_polls_left -= 1
            return True
        return False

    def read_profile_wxid(self, display_name: str = "") -> str:
        """当前打开会话的微信号（同名多格按 ``current_index`` 查 ``wxids_by_index``）。"""
        self.actions.append(("read_profile_wxid", display_name or self.current))
        by_idx = getattr(self, "wxids_by_index", {})
        idx = getattr(self, "current_index", -1)
        if idx in by_idx:
            return by_idx[idx]
        return self.wxids.get(display_name or self.current, "")


__all__ = ["SessionRow", "Bubble", "ScreenState", "WeChatPcBackend", "FakeBackend"]
