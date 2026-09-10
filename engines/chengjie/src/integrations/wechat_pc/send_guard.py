# -*- coding: utf-8 -*-
"""个人微信 PC 副驾 · 五步发送守卫（实施97 线 B）。

读屏路线最坏的失败不是「发不出」，而是**发错人**（会话切换没生效 / 列表滚动把目标顶走 /
输入框残留半句）。每次发送必须走满五步，任一步不符即 :class:`SendOutcome` 失败并给出机器可读
``stage``，service 据此**冻结该账号自动发送**、绝不盲重试：

1. ``open``      切到目标会话
2. ``title``     校验聊天头部标题 == 目标显示名（归一后比较）
3. ``fill``      填入文本后回读输入框 == 文本（防残留/截断/输入法吞字）
4. ``send``      回车（或点发送）
5. ``echo``      回读可见气泡：最后一条己方气泡文本 == 发送文本（送达证据，ack 的依据）

步骤间的拟人节奏（读停顿 / 逐字时长）由调用方按 ``PacingConfig`` 注入 ``sleep``；本模块只做
判定与编排，纯函数部分（``verify_*``）可离线单测。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from src.integrations.wechat_pc.backend import Bubble, WeChatPcBackend
from src.integrations.wechat_pc.identity import is_group_title, normalize_display_name

STAGES = ("open", "title", "fill", "send", "echo")


@dataclass
class SendOutcome:
    ok: bool
    stage: str = ""            # 失败在哪一步（成功为 "echo"）
    reason: str = ""
    echo_text: str = ""
    elapsed_ms: int = 0
    trace: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "stage": self.stage, "reason": self.reason,
                "echo_text": self.echo_text[:120], "elapsed_ms": self.elapsed_ms,
                "trace": list(self.trace)}


def _norm_text(s: str) -> str:
    return " ".join(str(s or "").replace("\r", "").split())


def verify_title(actual_title: str, expected_name: str) -> bool:
    """标题匹配：归一后相等，或标题以名字开头再跟人数「(12)」/「（12）」（群聊头部形态）。"""
    a = normalize_display_name(actual_title)
    e = normalize_display_name(expected_name)
    if not a or not e:
        return False
    if a == e:
        return True
    return is_group_title(a, e)


#: 微信输入框回读（ValuePattern）对表情的三种失真（4.1.12.55 真机实录）：
#: ① 微信原生表情（😊 等）被换成表情对象 → 回读成「?」；② 「[微笑]」类表情码转成表情对象 → 回读 U+FFFC；
#: ③ 非原生 emoji（😀）保留，但**每个非 BMP 字符让回读少 1 个尾字符**（UTF-16 计数缺陷）。
_EMOJI_LIKE_RE = re.compile(r"[\U00010000-\U0010FFFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u200D\uFFFC]")
_EMOTICON_CODE_RE = re.compile(r"\[[^\[\]\s]{1,12}\]")
_NON_BMP_RE = re.compile(r"[\U00010000-\U0010FFFF]")


def _norm_for_compare(s: str) -> str:
    s = _EMOTICON_CODE_RE.sub("", str(s or ""))
    s = _EMOJI_LIKE_RE.sub("", s)
    s = s.replace("?", "").replace("？", "")
    return _norm_text(s)


def texts_match(actual: str, expected: str) -> bool:
    """屏幕回读文本（输入框 / 气泡）是否就是要发的这条：去掉表情类字符后相等；含非 BMP 表情时容忍
    「每个表情丢 1 个尾字符」的回读缺陷（回读必须仍是期望文本的前缀且不短于 80%）。期望全是表情 → 回读非空即可。"""
    exp_raw = _norm_text(expected)
    if not exp_raw:
        return False
    e, a = _norm_for_compare(expected), _norm_for_compare(actual)
    if not e:
        return bool(_norm_text(actual))
    if a == e:
        return True
    lost_budget = len(_NON_BMP_RE.findall(expected))
    if lost_budget and a and e.startswith(a):
        missing = len(e) - len(a)
        return missing <= lost_budget and len(a) >= int(len(e) * 0.8)
    return False


def verify_composer(actual: str, expected: str) -> bool:
    return texts_match(actual, expected)


def find_echo(before: List[Bubble], after: List[Bubble], text: str) -> Optional[Bubble]:
    """在发送后的可见气泡里找「新增的、己方的、文本一致的」那条（防把历史同文误判为送达）。"""
    if not _norm_text(text):
        return None
    # 气泡回读与输入框同样有表情失真（😊→?、[微笑]→U+FFFC、非 BMP 表情丢尾字符）→ 同一套容忍比对；
    # RecyclerListView 会复用 RuntimeId（不能当「新增」证据），「新增」只认**同文己方气泡数量比发送前多**
    before_count = sum(1 for b in before if b.is_self and texts_match(b.text, text))
    matches_after = [b for b in after if b.is_self and texts_match(b.text, text)]
    if len(matches_after) > before_count:
        return matches_after[-1]
    return None


class GuardedSender:
    """五步守卫编排器。``sleep`` 可注入（单测传 no-op）。"""

    def __init__(self, backend: WeChatPcBackend, *, sleep: Callable[[float], None] = time.sleep,
                 read_pause_sec: float = 0.8, type_time_sec: Callable[[str], float] = lambda t: 0.0,
                 echo_wait_sec: float = 1.2, echo_retries: int = 3,
                 typing_wait_max_sec: float = 8.0, typing_poll_sec: float = 1.0) -> None:
        self.backend = backend
        self._sleep = sleep
        self.read_pause_sec = max(0.0, float(read_pause_sec))
        self._type_time = type_time_sec
        self.echo_wait_sec = max(0.0, float(echo_wait_sec))
        self.echo_retries = max(1, int(echo_retries))
        self.typing_wait_max_sec = max(0.0, float(typing_wait_max_sec))
        self.typing_poll_sec = max(0.1, float(typing_poll_sec))

    def _wait_peer_typing(self, trace: List[str]) -> None:
        """对方「正在输入」时不抢话：轮询等其停下（上限 ``typing_wait_max_sec``），后端没这能力就跳过。"""
        probe = getattr(self.backend, "peer_typing", None)
        if not callable(probe) or self.typing_wait_max_sec <= 0:
            return
        waited = 0.0
        while waited < self.typing_wait_max_sec:
            try:
                if not probe():
                    break
            except Exception:
                break
            self._sleep(self.typing_poll_sec)
            waited += self.typing_poll_sec
        if waited > 0:
            trace.append(f"waited typing {waited:.0f}s")

    def send(self, target_name: str, text: str, *, expected_wxid: str = "") -> SendOutcome:
        """``expected_wxid`` 非空＝微信号级身份：open 步逐格核对资料卡（同名不同号防发错人）。"""
        t0 = time.monotonic()
        trace: List[str] = []
        text = str(text or "")
        if not _norm_text(text):
            return SendOutcome(False, "fill", "empty_text", trace=trace)
        # 1. open（带身份核对）
        try:
            opened = self.backend.open_session(target_name, expected_wxid=expected_wxid)
        except TypeError:  # 旧后端签名
            opened = self.backend.open_session(target_name)
        if not opened:
            return self._fail("open", "identity_mismatch" if expected_wxid else "open_session_failed",
                              t0, trace)
        trace.append("open ok" + (f" wxid={expected_wxid}" if expected_wxid else ""))
        self._sleep(self.read_pause_sec)
        # 2. title
        title = self.backend.current_title()
        if not verify_title(title, target_name):
            return self._fail("title", f"title_mismatch:{_norm_text(title)[:40]}", t0, trace)
        trace.append("title ok")
        self._wait_peer_typing(trace)
        before = self.backend.read_visible_messages()
        # 3. fill
        if not self.backend.set_composer(text):
            return self._fail("fill", "set_composer_failed", t0, trace)
        self._sleep(max(0.0, float(self._type_time(text) or 0.0)))
        composer = self.backend.read_composer()
        if not verify_composer(composer, text):
            self.backend.set_composer("")   # 清残留，防下一条把半句一起发出去
            return self._fail("fill", "composer_mismatch", t0, trace)
        trace.append("fill ok")
        # 4. send —— 回车前再校一次标题：填字期间焦点被抢/来消息把会话顶走的窗口就在这里
        if not verify_title(self.backend.current_title(), target_name):
            self.backend.set_composer("")
            return self._fail("send", "title_changed_before_send", t0, trace)
        if not self.backend.press_send():
            return self._fail("send", "press_send_failed", t0, trace)
        trace.append("send ok")
        # 5. echo
        for i in range(self.echo_retries):
            self._sleep(self.echo_wait_sec)
            after = self.backend.read_visible_messages()
            hit = find_echo(before, after, text)
            if hit is not None:
                trace.append(f"echo ok@{i + 1}")
                return SendOutcome(True, "echo", "", echo_text=hit.text,
                                   elapsed_ms=int((time.monotonic() - t0) * 1000), trace=trace)
        return self._fail("echo", "echo_not_found", t0, trace)

    @staticmethod
    def _fail(stage: str, reason: str, t0: float, trace: List[str]) -> SendOutcome:
        trace.append(f"{stage} FAIL {reason}")
        return SendOutcome(False, stage, reason, elapsed_ms=int((time.monotonic() - t0) * 1000),
                           trace=trace)


__all__ = ["STAGES", "SendOutcome", "verify_title", "verify_composer", "texts_match", "find_echo", "GuardedSender"]
