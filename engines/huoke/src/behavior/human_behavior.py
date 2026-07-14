"""
Human Behavior Simulation Engine.

Replaces naive random.sleep with realistic interaction patterns:
- Bezier curve swiping (natural arc trajectories)
- Gaussian typing (variable cadence with occasional pauses/typos)
- Poisson-distributed waits (bursty-then-idle, like real users)
- Reading time simulation (proportional to content length)
- Session pacing (active/rest cycles to avoid detection)
- Warm-up ramp (gradual activity increase at session start)
"""

from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple, List

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _type_text_unicode_safe(d, text: str) -> bool:
    """Unicode-safe text input — 2 级 fallback, 修 2026-04-24 真机 blocker。

    原 ``d.send_keys(text)`` 依赖 ADBKeyboard IME APK, MIUI/HyperOS 拒装
    (``INSTALL_FAILED_USER_RESTRICTED``); 且即便装成功, ASCII fallback 路
    径 (``adb shell input text``) 对中日韩等非 ASCII 字符**乱码或丢失**,
    导致 zh/ja 客群 B bot 完全不可用。

    **2 级 fallback**:

    1. ``d(focused=True).set_text(text)`` — uiautomator2 的 AccessibilityNode
       接口直接写 focused widget 的 text 属性。Unicode safe (zh/ja/en 全支持),
       不依赖 IME / APK 安装 / 剪贴板权限 / Clipper 广播。**主路径**。
       要求 widget 当前是 focused (调用方应在 ``type_text`` 前 tap 确保)。

    2. ``d.shell(['input', 'text', escaped])`` — 老的 ASCII-only fallback。
       仅当 Path 1 miss **且** ``text`` 全 ASCII 时尝试。非 ASCII 跳过该
       fallback 并返回 False (而不是字符乱码/丢失)。

    Returns:
        ``True`` — 任一路径写入成功
        ``False`` — Path 1 miss/异常 + (Path 2 跳过 [非 ASCII] 或 shell 异常)

    Note:
        不抛异常, 失败静默返 False, 调用方自行决定降级策略 (e.g. smart_tap
        Send 之前 read UI 验证 composer 是否有内容, 空则不发)。
    """
    # Path 1: AccessibilityNode set_text on focused widget (Unicode-safe)
    try:
        focused = d(focused=True)
        if focused.exists(timeout=0.8):
            focused.set_text(text)
            return True
    except Exception as e:
        log.debug(
            "[_type_text_unicode_safe] focused.set_text failed: %s", e)

    # Path 2: adb shell input text — ASCII 专用, 非 ASCII 直接跳过避免乱码
    if any(ord(c) > 127 for c in text):
        log.warning(
            "[_type_text_unicode_safe] Path 1 miss 且 text 含非 ASCII "
            "(zh/ja/ko), 拒绝 adb input text 避免字符丢失: %r",
            text[:30])
        return False

    try:
        # adb input 对空格/&/引号需转义
        escaped = text.replace(' ', '%s').replace('&', r'\&')
        if hasattr(d, 'shell'):
            d.shell(['input', 'text', escaped])
            return True
        # u2 老版/非 u2 Device: 走 subprocess adb 兜底
        import subprocess
        serial = getattr(d, 'serial', None)
        cmd = ['adb'] + (['-s', serial] if serial else []) + \
              ['shell', 'input', 'text', escaped]
        subprocess.run(cmd, capture_output=True, timeout=5)
        return True
    except Exception as e:
        log.debug(
            "[_type_text_unicode_safe] adb input text failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class TypingProfile:
    mean_interval_ms: float = 110
    sigma_ms: float = 35
    space_factor: float = 0.65
    punctuation_factor: float = 1.7
    think_pause_prob: float = 0.05
    think_pause_range_ms: Tuple[int, int] = (500, 1500)
    typo_prob: float = 0.0
    min_interval_ms: float = 30

@dataclass
class SwipeProfile:
    bezier_steps: int = 15
    step_interval_ms: Tuple[int, int] = (8, 25)
    control_offset_px: Tuple[int, int] = (25, 70)
    end_linger_ms: Tuple[int, int] = (30, 100)

@dataclass
class TapProfile:
    offset_px: Tuple[int, int] = (0, 4)
    pre_tap_ms: Tuple[int, int] = (40, 180)
    double_tap_gap_ms: Tuple[int, int] = (80, 160)

@dataclass
class ReadingProfile:
    words_per_minute_mean: float = 230
    words_per_minute_sigma: float = 45
    scroll_during_read: bool = True
    min_sec: float = 0.8
    max_sec: float = 60.0

@dataclass
class SessionProfile:
    active_mean_min: float = 28
    active_sigma_min: float = 7
    rest_mean_min: float = 10
    rest_sigma_min: float = 3
    warmup_duration_min: float = 5.0
    warmup_rate_factor: float = 0.35

@dataclass
class BehaviorProfile:
    """Aggregate profile — one per platform or use-case."""
    name: str = "default"
    typing: TypingProfile = field(default_factory=TypingProfile)
    swipe: SwipeProfile = field(default_factory=SwipeProfile)
    tap: TapProfile = field(default_factory=TapProfile)
    reading: ReadingProfile = field(default_factory=ReadingProfile)
    session: SessionProfile = field(default_factory=SessionProfile)
    action_delay_mean: float = 2.5
    action_delay_sigma: float = 0.8
    action_delay_min: float = 0.4
    action_delay_max: float = 8.0


# ---------------------------------------------------------------------------
# Pre-built profiles
# ---------------------------------------------------------------------------

PROFILES = {
    "telegram": BehaviorProfile(
        name="telegram",
        typing=TypingProfile(mean_interval_ms=95, sigma_ms=30),
        session=SessionProfile(active_mean_min=35, rest_mean_min=8),
        action_delay_mean=2.0,
    ),
    "linkedin": BehaviorProfile(
        name="linkedin",
        typing=TypingProfile(mean_interval_ms=130, sigma_ms=45, think_pause_prob=0.08),
        reading=ReadingProfile(words_per_minute_mean=200, words_per_minute_sigma=40),
        session=SessionProfile(
            active_mean_min=22, active_sigma_min=6,
            rest_mean_min=15, rest_sigma_min=5,
            warmup_duration_min=8.0, warmup_rate_factor=0.25,
        ),
        action_delay_mean=4.0,
        action_delay_sigma=1.5,
        action_delay_min=1.5,
        action_delay_max=15.0,
    ),
    "whatsapp": BehaviorProfile(
        name="whatsapp",
        typing=TypingProfile(mean_interval_ms=105, sigma_ms=32),
        session=SessionProfile(active_mean_min=25, rest_mean_min=12),
        action_delay_mean=2.5,
    ),
    # ── New platforms ──────────────────────────────────────────────────
    "facebook": BehaviorProfile(
        name="facebook",
        typing=TypingProfile(mean_interval_ms=108, sigma_ms=34, think_pause_prob=0.06),
        swipe=SwipeProfile(bezier_steps=18, step_interval_ms=(10, 28)),
        reading=ReadingProfile(words_per_minute_mean=220, words_per_minute_sigma=50),
        session=SessionProfile(
            active_mean_min=30, active_sigma_min=8,
            rest_mean_min=12, rest_sigma_min=4,
            warmup_duration_min=6.0, warmup_rate_factor=0.30,
        ),
        action_delay_mean=3.0,
        action_delay_sigma=1.2,
        action_delay_min=0.8,
        action_delay_max=12.0,
    ),
    "instagram": BehaviorProfile(
        name="instagram",
        typing=TypingProfile(mean_interval_ms=100, sigma_ms=30, think_pause_prob=0.04),
        tap=TapProfile(offset_px=(0, 5), pre_tap_ms=(30, 150), double_tap_gap_ms=(70, 140)),
        swipe=SwipeProfile(bezier_steps=12, step_interval_ms=(6, 20)),
        reading=ReadingProfile(
            words_per_minute_mean=250, words_per_minute_sigma=60,
            min_sec=0.5, max_sec=30.0,
        ),
        session=SessionProfile(
            active_mean_min=20, active_sigma_min=6,
            rest_mean_min=10, rest_sigma_min=4,
            warmup_duration_min=4.0, warmup_rate_factor=0.40,
        ),
        action_delay_mean=2.0,
        action_delay_sigma=0.8,
        action_delay_min=0.5,
        action_delay_max=8.0,
    ),
    "tiktok": BehaviorProfile(
        name="tiktok",
        typing=TypingProfile(mean_interval_ms=95, sigma_ms=28),
        swipe=SwipeProfile(bezier_steps=10, step_interval_ms=(5, 15)),
        reading=ReadingProfile(
            words_per_minute_mean=280, words_per_minute_sigma=70,
            min_sec=0.3, max_sec=15.0,
        ),
        session=SessionProfile(
            active_mean_min=25, active_sigma_min=8,
            rest_mean_min=8, rest_sigma_min=3,
            warmup_duration_min=3.0, warmup_rate_factor=0.45,
        ),
        action_delay_mean=1.5,
        action_delay_sigma=0.6,
        action_delay_min=0.3,
        action_delay_max=6.0,
    ),
    "twitter": BehaviorProfile(
        name="twitter",
        typing=TypingProfile(mean_interval_ms=90, sigma_ms=25, think_pause_prob=0.07),
        reading=ReadingProfile(
            words_per_minute_mean=260, words_per_minute_sigma=55,
            min_sec=0.4, max_sec=20.0,
        ),
        session=SessionProfile(
            active_mean_min=25, active_sigma_min=7,
            rest_mean_min=10, rest_sigma_min=4,
            warmup_duration_min=5.0, warmup_rate_factor=0.35,
        ),
        action_delay_mean=2.0,
        action_delay_sigma=0.7,
        action_delay_min=0.4,
        action_delay_max=8.0,
    ),
}


def get_profile(name: str) -> BehaviorProfile:
    return PROFILES.get(name, BehaviorProfile(name=name))


# ---------------------------------------------------------------------------
# Bezier math
# ---------------------------------------------------------------------------

def _cubic_bezier(t: float, p0: float, p1: float, p2: float, p3: float) -> float:
    u = 1.0 - t
    return u*u*u*p0 + 3*u*u*t*p1 + 3*u*t*t*p2 + t*t*t*p3


def _bezier_curve(
    start: Tuple[float, float],
    end: Tuple[float, float],
    offset_range: Tuple[int, int],
    steps: int,
) -> List[Tuple[int, int]]:
    ox = random.randint(*offset_range) * random.choice([-1, 1])
    oy = random.randint(*offset_range) * random.choice([-1, 1])
    mx, my = (start[0] + end[0]) / 2, (start[1] + end[1]) / 2
    cp1 = (mx + ox * 0.6, my + oy * 0.6)
    cp2 = (mx - ox * 0.4, my - oy * 0.4)

    points = []
    for i in range(steps + 1):
        t = i / steps
        x = _cubic_bezier(t, start[0], cp1[0], cp2[0], end[0])
        y = _cubic_bezier(t, start[1], cp1[1], cp2[1], end[1])
        points.append((int(round(x)), int(round(y))))
    return points


# ---------------------------------------------------------------------------
# HumanBehavior Engine
# ---------------------------------------------------------------------------

class HumanBehavior:
    """
    Wraps uiautomator2 device operations with human-like timing.

    Usage:
        hb = HumanBehavior(profile=get_profile("linkedin"))
        hb.session_start()
        hb.tap(d, x, y)
        hb.type_text(d, element, "Hello world")
        hb.wait_between_actions()
        if hb.should_rest():
            hb.rest()
    """

    def __init__(self, profile: Optional[BehaviorProfile] = None):
        self.profile = profile or BehaviorProfile()
        self._session_start: Optional[float] = None
        self._session_actions: int = 0
        self._current_active_limit: float = 0
        self._resting: bool = False

    # -- Session lifecycle --------------------------------------------------

    def session_start(self):
        self._session_start = time.time()
        self._session_actions = 0
        p = self.profile.session
        self._current_active_limit = max(5, random.gauss(p.active_mean_min, p.active_sigma_min)) * 60
        log.info("HumanBehavior session started (profile=%s, active_limit=%.0fs)",
                 self.profile.name, self._current_active_limit)

    @property
    def session_elapsed(self) -> float:
        if self._session_start is None:
            return 0.0
        return time.time() - self._session_start

    @property
    def _warmup_multiplier(self) -> float:
        """During warm-up, slow everything down."""
        p = self.profile.session
        if p.warmup_duration_min <= 0:
            return 1.0
        elapsed_min = self.session_elapsed / 60
        if elapsed_min >= p.warmup_duration_min:
            return 1.0
        progress = elapsed_min / p.warmup_duration_min
        return p.warmup_rate_factor + (1.0 - p.warmup_rate_factor) * progress

    def should_rest(self) -> bool:
        if self._session_start is None:
            return False
        return self.session_elapsed >= self._current_active_limit

    def rest(self):
        p = self.profile.session
        rest_sec = max(60, random.gauss(p.rest_mean_min, p.rest_sigma_min) * 60)
        log.info("HumanBehavior resting for %.0fs (actions this session: %d)",
                 rest_sec, self._session_actions)
        self._resting = True
        time.sleep(rest_sec)
        self._resting = False
        self.session_start()

    def session_stats(self) -> dict:
        return {
            "elapsed_sec": round(self.session_elapsed, 1),
            "actions": self._session_actions,
            "resting": self._resting,
            "warmup_mult": round(self._warmup_multiplier, 2),
        }

    # -- Tap ----------------------------------------------------------------

    def tap(self, d, x: int, y: int):
        """Human-like tap with slight positional jitter and pre-tap hesitation.

        MIUI 真机注意:`d.click()` 走 UiAutomation 注入,需要 INJECT_EVENTS 权限,
        在 MIUI/HyperOS 上常被拒绝。失败时 fallback 到 `adb shell input tap`
        (shell input 不需要 INJECT_EVENTS,稳定可用)。
        """
        tp = self.profile.tap
        ox = random.randint(-tp.offset_px[1], tp.offset_px[1])
        oy = random.randint(-tp.offset_px[1], tp.offset_px[1])
        pre_delay = random.randint(*tp.pre_tap_ms) / 1000.0
        pre_delay /= self._warmup_multiplier
        time.sleep(pre_delay)
        tx, ty = x + ox, y + oy
        try:
            d.click(tx, ty)
        except Exception as e:
            err = str(e)
            if "INJECT_EVENTS" in err or "SecurityException" in err:
                try:
                    d.shell(f"input tap {tx} {ty}")
                except Exception:
                    raise
            else:
                raise
        self._session_actions += 1

    # -- Swipe --------------------------------------------------------------

    def swipe(self, d, x1: int, y1: int, x2: int, y2: int):
        """Bezier-curve swipe that mimics a real finger arc.

        MIUI 上若 d.click 被 INJECT_EVENTS 拒绝,改走 `adb shell input swipe`
        (一次性轨迹,损失曲线感但保证可用)。
        """
        sp = self.profile.swipe
        points = _bezier_curve((x1, y1), (x2, y2), sp.control_offset_px, sp.bezier_steps)

        try:
            for i in range(1, len(points)):
                px, py = points[i]
                d.click(px, py)
                interval = random.randint(*sp.step_interval_ms) / 1000.0
                time.sleep(interval)
        except Exception as e:
            err = str(e)
            if "INJECT_EVENTS" in err or "SecurityException" in err:
                duration = random.randint(280, 480)
                d.shell(f"input swipe {x1} {y1} {x2} {y2} {duration}")
            else:
                raise

        linger = random.randint(*sp.end_linger_ms) / 1000.0
        time.sleep(linger)
        self._session_actions += 1

    def swipe_native(self, d, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300):
        """Use u2's native swipe but with human-like pre/post delays.

        真机修复 (P1): 之前把 `duration_ms + jitter`(单位毫秒,最大可到 800)
        直接传给 d.swipe(),而 u2 的 d.swipe(duration) 单位是**秒**,等于让
        手机滑 800 秒;在 ADB fallback 上 `swipe(duration=0.3)` 又被乘 1000
        变成 800_000ms,被 MIUI 误判为长按底部上滑手势,弹出最近任务面板。
        现在统一传秒给 d.swipe();ADB fallback 那侧也兼容毫秒输入。
        """
        tp = self.profile.tap
        pre = random.randint(*tp.pre_tap_ms) / 1000.0
        time.sleep(pre)
        jitter_ms = random.randint(200, 500)
        total_ms = max(150, min(900, duration_ms + jitter_ms))
        duration_sec = total_ms / 1000.0
        try:
            d.swipe(x1, y1, x2, y2, duration=duration_sec)
        except Exception as e:
            err = str(e)
            if "INJECT_EVENTS" in err or "SecurityException" in err:
                d.shell(f"input swipe {x1} {y1} {x2} {y2} {total_ms}")
            else:
                raise
        linger = random.randint(*self.profile.swipe.end_linger_ms) / 1000.0
        time.sleep(linger)
        self._session_actions += 1

    # -- Typing -------------------------------------------------------------

    def type_text(self, d, text: str, clear_first: bool = True):
        """
        Type text character-by-character with Gaussian-distributed intervals.

        2026-04-24: 核心打字调用从 `d.send_keys()` 改为
        `_type_text_unicode_safe(d, text)`。

        **原因**: `d.send_keys()` 依赖 ADBKeyboard IME APK, MIUI/HyperOS 常拒
        装 (``INSTALL_FAILED_USER_RESTRICTED``), 整条 type_text 挂。真机实测
        2026-04-24 Redmi 13C (``IJ8HZLORS485PJWW``) 撞此问题, 导致
        ``FacebookAutomation.send_message`` 对 zh/ja 客群完全不可用。

        新实现 2 级 fallback (见 ``_type_text_unicode_safe``):
          1. ``d(focused=True).set_text(text)`` — AccessibilityNode 直接写
             widget text 属性, Unicode safe (zh/ja/en 全支持), 不依赖
             IME/APK/剪贴板
          2. ``d.shell(['input', 'text', escaped])`` — 老 ASCII-only fallback

        Signature / behavior 保持不变 (think time + clear_first + 模拟输入
        时长 sleep), 只换核心打字调用, 老 caller 零改动。
        """
        tp = self.profile.typing
        think_time = self._reading_time_for_length(len(text) * 2) * 0.3
        time.sleep(max(0.3, think_time))

        if clear_first:
            try:
                d.clear_text()
            except Exception as e:
                # 某些 widget 未 focused / 不支持 clear_text → 不阻塞, 继续
                log.debug("[type_text] clear_text failed (continue): %s", e)
            time.sleep(random.uniform(0.1, 0.3))

        total_type_time = 0.0
        for ch in text:
            base = random.gauss(tp.mean_interval_ms, tp.sigma_ms)
            base = max(tp.min_interval_ms, base)
            if ch == ' ':
                base *= tp.space_factor
            elif ch in '.,;:!?':
                base *= tp.punctuation_factor
            if random.random() < tp.think_pause_prob:
                base += random.randint(*tp.think_pause_range_ms)
            total_type_time += base

        simulated_sec = total_type_time / 1000.0 / self._warmup_multiplier
        _type_text_unicode_safe(d, text)
        remaining = simulated_sec - 0.05
        if remaining > 0:
            time.sleep(min(remaining, 8.0))

        self._session_actions += 1

    def type_text_slow(self, d, element, text: str):
        """
        True character-by-character input via ADB shell input.
        Much slower but more realistic for short texts (connection notes, etc).
        """
        tp = self.profile.typing
        think_time = self._reading_time_for_length(len(text)) * 0.3
        time.sleep(max(0.3, think_time))

        element.click()
        time.sleep(random.uniform(0.15, 0.35))

        import subprocess
        device_serial = getattr(d, 'serial', None)
        for ch in text:
            interval_ms = random.gauss(tp.mean_interval_ms, tp.sigma_ms)
            interval_ms = max(tp.min_interval_ms, interval_ms)
            if ch == ' ':
                interval_ms *= tp.space_factor
            elif ch in '.,;:!?':
                interval_ms *= tp.punctuation_factor
            if random.random() < tp.think_pause_prob:
                interval_ms += random.randint(*tp.think_pause_range_ms)

            interval_ms /= self._warmup_multiplier
            time.sleep(interval_ms / 1000.0)

            if ch == ' ':
                cmd = ['adb'] + (['-s', device_serial] if device_serial else []) + \
                      ['shell', 'input', 'keyevent', '62']
            else:
                cmd = ['adb'] + (['-s', device_serial] if device_serial else []) + \
                      ['shell', 'input', 'text', ch]
            try:
                subprocess.run(cmd, capture_output=True, timeout=5)
            except Exception:
                pass

        self._session_actions += 1

    # -- Reading simulation -------------------------------------------------

    def wait_read(self, text_or_length):
        """Simulate reading time proportional to content length."""
        length = text_or_length if isinstance(text_or_length, (int, float)) else len(str(text_or_length))
        sec = self._reading_time_for_length(length)
        time.sleep(sec)

    def _reading_time_for_length(self, char_count: int) -> float:
        rp = self.profile.reading
        wpm = max(80, random.gauss(rp.words_per_minute_mean, rp.words_per_minute_sigma))
        words = char_count / 5.0
        sec = (words / wpm) * 60.0
        sec /= self._warmup_multiplier
        return max(rp.min_sec, min(rp.max_sec, sec))

    # -- Action delays ------------------------------------------------------

    def wait_between_actions(self, context_weight: float = 1.0):
        """
        Poisson-inspired wait between discrete actions.
        context_weight > 1.0 for heavier actions (e.g. after sending a long message).
        """
        p = self.profile
        base = max(p.action_delay_min,
                   random.gauss(p.action_delay_mean, p.action_delay_sigma))
        delay = base * context_weight / self._warmup_multiplier
        delay = min(delay, p.action_delay_max * context_weight)
        if random.random() < 0.08:
            delay *= random.uniform(2.0, 4.0)
        time.sleep(delay)

    def wait_think(self, complexity: float = 1.0):
        """Simulate thinking pause (before composing a message, making a decision)."""
        base = random.gauss(1.5, 0.5) * complexity
        base = max(0.3, base) / self._warmup_multiplier
        time.sleep(min(base, 10.0))

    # -- Scroll (natural) ---------------------------------------------------

    def scroll_down(self, d, screen_height: int = 1600, fraction: float = 0.4):
        """Natural scroll down using u2 swipe with variable distance."""
        vary = random.uniform(0.7, 1.3)
        dist = int(screen_height * fraction * vary)
        cx = random.randint(300, 420)
        y_start = int(screen_height * 0.7) + random.randint(-30, 30)
        y_end = y_start - dist
        self.swipe_native(d, cx, y_start, cx, max(100, y_end))

    def scroll_up(self, d, screen_height: int = 1600, fraction: float = 0.3):
        vary = random.uniform(0.7, 1.3)
        dist = int(screen_height * fraction * vary)
        cx = random.randint(300, 420)
        y_start = int(screen_height * 0.3) + random.randint(-30, 30)
        y_end = y_start + dist
        self.swipe_native(d, cx, y_start, cx, min(screen_height - 100, y_end))
