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
from typing import Any, Callable, List, Optional

from src.integrations.wechat_pc.backend import Bubble, WeChatPcBackend
from src.integrations.wechat_pc.desktop_input import desktop_input
from src.integrations.wechat_pc.identity import is_group_title, normalize_display_name

STAGES = ("open", "title", "fill", "send", "echo")
#: 语音五步（2026-09-19）：open / title 同文本；``record``＝点「发语音」并确认录音态；``play``＝把合成音灌进
#: 麦克风（虚拟声卡）；``send``＝点「发送语音」；``echo``＝新增己方语音气泡（秒数与音频时长相符）。
#: 任一步在进入录音态之后失败 → **必点「取消」**退出录音态；取消也失败 → stage ``cancel``（录音态卡住会让
#: 之后所有文字都发不出去，service 据此冻结并通知主人）。
VOICE_STAGES = ("open", "title", "record", "play", "send", "echo", "cancel")


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


#: 语音气泡 Name 里的秒数：「语音11秒」「语音 11"」「11″」「Voice 11s」「[语音] 11 秒」
_VOICE_SECONDS_RE = re.compile(r"(\d{1,3})\s*(?:秒|\"|″|''|s\b|sec\b)", re.IGNORECASE)


def parse_voice_seconds(name: str) -> Optional[int]:
    """从语音气泡的可见文本解析秒数；解析不出 → None（不同版本/语言的占位文案不一定带秒数）。"""
    m = _VOICE_SECONDS_RE.search(str(name or ""))
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def _is_voice_bubble(b: Bubble) -> bool:
    if not b.is_self:
        return False
    if b.kind == "voice":
        return True
    # 类名认不出时按占位文案兜底：「语音11秒」
    return "语音" in str(b.text) and parse_voice_seconds(b.text) is not None


def _tail_voice_run(bubbles: List[Bubble]) -> int:
    """列表尾部连续的己方语音气泡数（时间分隔/空占位不打断）。"""
    n = 0
    for b in reversed(bubbles):
        if _is_voice_bubble(b):
            n += 1
        elif b.kind == "system" or not str(b.text or "").strip():
            continue
        else:
            break
    return n


def find_voice_echo(before: List[Bubble], after: List[Bubble], expected_sec: Optional[int] = None,
                    *, tol_low: int = 2, tol_high: int = 5) -> Optional[Bubble]:
    """发送后新增的己方**语音**气泡（送达证据）。

    「新增」认两种证据之一（RuntimeId 会被 RecyclerListView 复用，不能当证据）：
    ① 可见己方语音气泡**总数**比发送前多；② 列表**尾部连续**己方语音气泡数比发送前多。
    ②是给「分条连发」的：新气泡冒出来时列表顶部最老的一条语音会滚出可视区，总数不变——2026-09-19 真机
    第二条明明发出去了却 echo_not_found → 误判失败进人审（已送达的稿子被人再发一次＝最坏结果）。
    尾部连续数只看底部，顶部滚出不影响。
    ``expected_sec`` 给定且气泡带秒数时还要秒数落在 ``[expected-tol_low, expected+tol_high]``——录音比音频长
    1–3 秒是正常的（点按/UI 切换延迟），但差得离谱说明抓到的是别的语音。解析不出秒数则只按数量判。
    """
    before_n = sum(1 for b in before if _is_voice_bubble(b))
    after_v = [b for b in after if _is_voice_bubble(b)]
    if not after_v:
        return None
    if len(after_v) <= before_n and _tail_voice_run(after) <= _tail_voice_run(before):
        return None
    hit = after_v[-1]
    if expected_sec is not None:
        got = parse_voice_seconds(hit.text)
        if got is not None and not (expected_sec - tol_low <= got <= expected_sec + tol_high):
            return None
    return hit


class GuardedSender:
    """五步守卫编排器。``sleep`` 可注入（单测传 no-op）。"""

    def __init__(self, backend: WeChatPcBackend, *, sleep: Callable[[float], None] = time.sleep,
                 read_pause_sec: float = 0.8, type_time_sec: Callable[[str], float] = lambda t: 0.0,
                 echo_wait_sec: float = 1.2, echo_retries: int = 3, voice_echo_retries: int = 8,
                 typing_wait_max_sec: float = 8.0, typing_poll_sec: float = 1.0) -> None:
        self.backend = backend
        self._sleep = sleep
        self.read_pause_sec = max(0.0, float(read_pause_sec))
        self._type_time = type_time_sec
        self.echo_wait_sec = max(0.0, float(echo_wait_sec))
        self.echo_retries = max(1, int(echo_retries))
        # 语音气泡要等本地录音落盘+上传占位切换，连发第二条常比文字慢；错判「没发出」的代价（重发）远高于多等几秒
        self.voice_echo_retries = max(self.echo_retries, int(voice_echo_retries))
        #: 播放期间提前定位「发送语音」按钮的重试间隔
        self.prime_retry_sec = 0.25
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
        """``expected_wxid`` 非空＝微信号级身份：open 步逐格核对资料卡（同名不同号防发错人）。
        五步整体持桌面输入锁：双开时另一个驾驶不能在填字→回车之间抓前台。"""
        with desktop_input():
            return self._send(target_name, text, expected_wxid=expected_wxid)

    def _send(self, target_name: str, text: str, *, expected_wxid: str = "") -> SendOutcome:
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
            self.backend.set_composer("")   # 没发出去的稿子不能留在框里等人手一回车
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

    # ── 语音 ──
    def _abort_recording(self, trace: List[str]) -> bool:
        """录音态之后任何失败都走这里：点「取消」并确认退出。返回是否确认退出（False＝录音态卡住）。"""
        cancel = getattr(self.backend, "cancel_voice_record", None)
        ok = False
        if callable(cancel):
            try:
                ok = bool(cancel())
            except Exception:
                ok = False
        trace.append("cancel ok" if ok else "cancel FAIL recording_stuck")
        return ok

    def send_voice(self, target_name: str, play: Callable[[], float], *, expected_wxid: str = "",
                   expected_sec: Optional[int] = None) -> SendOutcome:
        """发一条语音：open → title → record → play → send → echo。

        ``play()`` 由调用方提供：把音频**阻塞地**灌进微信当前麦克风（虚拟声卡），返回实际播放秒数；抛异常＝播放失败。
        ``expected_sec`` 用于 echo 步核对气泡秒数（None＝只认「新增己方语音气泡」）。
        录音态进入之后的任何失败都先「取消」再返回；取消不成 → ``stage="cancel"``。
        """
        with desktop_input():
            return self._send_voice(target_name, play, expected_wxid=expected_wxid, expected_sec=expected_sec)

    def _send_voice(self, target_name: str, play: Callable[[], float], *, expected_wxid: str = "",
                    expected_sec: Optional[int] = None) -> SendOutcome:
        t0 = time.monotonic()
        trace: List[str] = []
        ready = getattr(self.backend, "voice_ready", None)
        if not callable(ready) or not ready():
            return self._fail("record", "voice_not_ready", t0, trace)
        # 1. open（带身份核对）
        try:
            opened = self.backend.open_session(target_name, expected_wxid=expected_wxid)
        except TypeError:
            opened = self.backend.open_session(target_name)
        if not opened:
            return self._fail("open", "identity_mismatch" if expected_wxid else "open_session_failed", t0, trace)
        # 语音 trace 每步带 ``@累计ms``：真机上一条 5s 语音端到端 17s，不带时间轴根本说不清哪步在吃时间
        trace.append("open ok" + (f" wxid={expected_wxid}" if expected_wxid else "") + self._at(t0))
        self._sleep(self.read_pause_sec)
        # 2. title
        if not verify_title(self.backend.current_title(), target_name):
            return self._fail("title", f"title_mismatch:{_norm_text(self.backend.current_title())[:40]}", t0, trace)
        trace.append("title ok" + self._at(t0))
        self._wait_peer_typing(trace)
        before = self.backend.read_visible_messages()
        try:
            return self._record_play_send(target_name, play, before, expected_sec, t0, trace)
        finally:
            close = getattr(play, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def _record_play_send(self, target_name: str, play: Callable[..., float], before: List[Bubble],
                          expected_sec: Optional[int], t0: float, trace: List[str]) -> SendOutcome:
        """record → play → send → echo。首尾静音收窄（2026-09-19 P2）：

        - 进录音态**之前**先 ``play.warmup()`` 把输出流开好——不然录音一开始的 100–300ms 全是开流静音；
        - 播放**期间**让后端 ``prime_voice_send()`` 提前定位「发送语音」按钮（UIA 遍历几百毫秒藏进播放窗口）；
        - 播完只做一次标题核对就点发送，尾部静音目标 ≤200ms；trace 记 ``tail=…ms`` 给真机验收看。
        """
        warm = getattr(play, "warmup", None)
        if callable(warm):
            try:
                warm()
            except Exception:
                pass
        # 3. record：进入录音态
        if not self.backend.start_voice_record():
            # 可能点了按钮但没确认到录音态：保险起见尝试取消
            if getattr(self.backend, "voice_recording", lambda: False)():
                self._abort_recording(trace)
            return self._fail("record", "start_record_failed", t0, trace)
        trace.append("record ok" + self._at(t0))
        # 4. play：灌音频（阻塞）。录音期间标题被顶走（来消息/焦点抢占）→ 取消，不能发到别人会话
        prime = getattr(self.backend, "prime_voice_send", None)
        during = None
        if callable(prime):
            # 「发送语音」按钮在录音态出现后还要晚 100–300ms 才挂上名字（真机），首次定位不到就隔 0.25s 再试，
            # 但只在播放窗口内试：deadline 卡在音频结束前 0.4s，绝不让定位拖长 play() 返回（那就是尾部静音）
            dur = float(getattr(play, "duration", 0.0) or 0.0) or float(expected_sec or 0)
            budget = max(0.0, min(dur - 0.4, 3.0))
            deadline = time.monotonic() + budget

            def during() -> None:
                while not prime():
                    if time.monotonic() + self.prime_retry_sec > deadline:
                        return
                    self._sleep(self.prime_retry_sec)
        try:
            played = float(self._play(play, during) or 0.0)
        except Exception as exc:  # noqa: BLE001
            self._abort_recording(trace)
            return self._fail("play", f"play_failed:{type(exc).__name__}", t0, trace)
        t_play_end = time.monotonic()
        trace.append(f"play ok {played:.1f}s" + self._at(t0))
        if not verify_title(self.backend.current_title(), target_name):
            stuck = not self._abort_recording(trace)
            return self._fail("cancel" if stuck else "send", "title_changed_before_send", t0, trace)
        # 5. send：点「发送语音」
        if not self.backend.finish_voice_record():
            stuck = not self._abort_recording(trace)
            return self._fail("cancel" if stuck else "send", "finish_record_failed", t0, trace)
        trace.append(f"send ok tail={int((time.monotonic() - t_play_end) * 1000)}ms" + self._at(t0))
        # 6. echo：新增己方语音气泡
        for i in range(self.voice_echo_retries):
            self._sleep(self.echo_wait_sec)
            after = self.backend.read_visible_messages()
            hit = find_voice_echo(before, after, expected_sec)
            if hit is not None:
                trace.append(f"echo ok@{i + 1} {hit.text[:20]}" + self._at(t0))
                return SendOutcome(True, "echo", "", echo_text=hit.text,
                                   elapsed_ms=int((time.monotonic() - t0) * 1000), trace=trace)
        return self._fail("echo", "echo_not_found", t0, trace)

    @staticmethod
    def _play(play: Callable[..., float], during: Optional[Callable[[], Any]]) -> float:
        """``play(during=…)`` 若被支持就把提前定位塞进播放窗口；老式无参闭包照旧 ``play()``。"""
        if during is not None:
            try:
                import inspect
                params = inspect.signature(play).parameters
                accepts = "during" in params or any(p.kind == p.VAR_KEYWORD for p in params.values())
            except (TypeError, ValueError):
                accepts = False
            if accepts:
                return play(during=during)
        return play()

    @staticmethod
    def _at(t0: float) -> str:
        return f" @{int((time.monotonic() - t0) * 1000)}ms"

    @staticmethod
    def _fail(stage: str, reason: str, t0: float, trace: List[str]) -> SendOutcome:
        trace.append(f"{stage} FAIL {reason}")
        return SendOutcome(False, stage, reason, elapsed_ms=int((time.monotonic() - t0) * 1000),
                           trace=trace)


__all__ = ["STAGES", "VOICE_STAGES", "SendOutcome", "verify_title", "verify_composer", "texts_match", "find_echo",
           "parse_voice_seconds", "find_voice_echo", "GuardedSender"]
