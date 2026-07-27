"""首启匿名体验档（本地额度，不签名、不写 license.key）。

为什么它**不**走签名授权
========================
签名授权只能由厂商私钥签发。要在客户端本地签发一张"体验档 license"，就得把私钥塞进
安装包——那等于把授权体系整个交出去。所以体验档刻意**不进** `LicenseManager` 的语义：
它是「首启赠送的一点用量」，不是权益凭证。

这条边界带来一个清晰的规则：**凡是签名的都来自厂商，凡是本地的都很小且尽力而为。**
体验档因此天然可被绕过（删状态文件即重置），这不是缺陷而是取舍——真正的防滥用在
签发侧的机器码台账上做（服务端判断"这台机器领过没有"），本地只负责不打扰地计量。

产品意图（运营已定）
====================
装完直接能用（1 万字符 / 48 小时），不要求注册；想要完整 7 天再注册。先给价值、再要
信息，把「还没看到东西就要我填表」这个最大流失点挪走。

计量复用
========
用量记在与正式授权同一个 `LicenseQuotaStore`，lic_id 形如 ``local:ab12cd34``（机器短码）。
不另建表：会员页/ops/告警读的是同一份口径。

状态文件
========
``<config>/local_trial.json``：``{first_seen, last_seen, machine, chars, window_hours, closed}``
- ``last_seen`` 取单调最大值 → 把系统时间往回拨不能延长窗口；
- ``closed`` 一旦置真就永久失效（注册换正式试用后置真），删 license.key 也复活不了。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

STATE_FILENAME = "local_trial.json"

#: 默认赠量（运营口径：够真跑通一条客户消息，不够跑完一天活）
DEFAULT_CHARS = 10_000
DEFAULT_WINDOW_HOURS = 48.0

#: 时钟回拨容忍：小于它按正常时间抖动处理（NTP 校正、休眠唤醒）
CLOCK_SLACK_SEC = 300.0

#: last_seen 落盘节流。额度检查/记账在**每条消息**的热路上，不节流就是每翻译一句
#: 写一次 JSON。5 分钟粒度的水位对时钟回拨防护完全够用（回拨要有意义得拨小时级）。
TOUCH_MIN_INTERVAL_SEC = 300.0

#: 状态读缓存 TTL。本进程是唯一写者，故内存缓存安全；没有它每条消息都要读一次文件。
STATE_CACHE_TTL_SEC = 5.0

LIC_ID_PREFIX = "local:"


@dataclass
class LocalTrialState:
    first_seen: float = 0.0
    last_seen: float = 0.0
    machine: str = ""
    chars: int = DEFAULT_CHARS
    window_hours: float = DEFAULT_WINDOW_HOURS
    closed: bool = False

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: Any) -> "LocalTrialState":
        d = d if isinstance(d, dict) else {}

        def _f(k: str, dv: float) -> float:
            try:
                return float(d.get(k) or dv)
            except Exception:
                return dv

        return cls(
            first_seen=_f("first_seen", 0.0),
            last_seen=_f("last_seen", 0.0),
            machine=str(d.get("machine") or ""),
            chars=max(0, int(_f("chars", DEFAULT_CHARS))),
            window_hours=max(0.0, _f("window_hours", DEFAULT_WINDOW_HOURS)),
            closed=bool(d.get("closed", False)),
        )


def lic_id_for(machine_short: str) -> str:
    """体验档在额度表里的 lic_id。与正式授权的 lic_id 空间天然隔离（有前缀）。"""
    return LIC_ID_PREFIX + (str(machine_short or "unknown").strip() or "unknown")


def is_local_lic_id(lic_id: str) -> bool:
    return str(lic_id or "").startswith(LIC_ID_PREFIX)


def evaluate(
    state: LocalTrialState, *, now: float, used_chars: int = 0,
) -> Dict[str, Any]:
    """纯函数：算出体验档此刻的状态。

    返回 ``{active, closed, expired, exhausted, tampered, effective_now,
    seconds_left, chars_left, included, used}``。
    ``active`` = 还能用（未关闭、未过窗、未用尽）。
    """
    # 时钟回拨保护：以「见过的最晚时刻」为准，往回拨系统时间不能换来更长的窗口。
    tampered = bool(state.last_seen and now < state.last_seen - CLOCK_SLACK_SEC)
    eff = max(float(now), float(state.last_seen or 0.0))

    started = float(state.first_seen or 0.0)
    window = float(state.window_hours or 0.0) * 3600.0
    if not started:
        expired = False
        seconds_left = window
    else:
        elapsed = max(0.0, eff - started)
        seconds_left = max(0.0, window - elapsed) if window > 0 else float("inf")
        expired = window > 0 and elapsed >= window

    included = int(state.chars or 0)
    used = max(0, int(used_chars or 0))
    chars_left = max(0, included - used) if included > 0 else -1   # -1 = 不限
    exhausted = included > 0 and used >= included

    active = bool(started) and not state.closed and not expired and not exhausted
    return {
        "active": active,
        "closed": bool(state.closed),
        "expired": expired,
        "exhausted": exhausted,
        "tampered": tampered,
        "effective_now": eff,
        "seconds_left": (None if seconds_left == float("inf") else round(seconds_left)),
        "hours_left": (None if seconds_left == float("inf") else round(seconds_left / 3600.0, 1)),
        "included": included,
        "used": used,
        "chars_left": chars_left,
    }


class LocalTrial:
    """体验档状态的读写（文件 IO 集中在此，纯判定在 `evaluate`）。"""

    def __init__(self, path: str, *, chars: int = DEFAULT_CHARS,
                 window_hours: float = DEFAULT_WINDOW_HOURS,
                 machine_short: str = "") -> None:
        self.path = str(path)
        self._chars = max(0, int(chars))
        self._window = max(0.0, float(window_hours))
        self._machine = str(machine_short or "")
        self._lock = threading.Lock()
        self._cache: Optional[LocalTrialState] = None
        self._cache_ts = 0.0

    # ── 读写 ────────────────────────────────────────────────────────────
    def _read(self, *, use_cache: bool = True) -> LocalTrialState:
        """读状态。带短 TTL 内存缓存——额度检查在每条消息的热路上，不缓存就是每句一次读盘。"""
        if use_cache and self._cache is not None and \
                (time.time() - self._cache_ts) < STATE_CACHE_TTL_SEC:
            return self._cache
        st: Optional[LocalTrialState] = None
        try:
            if os.path.exists(self.path):
                with open(self.path, "r", encoding="utf-8") as f:
                    st = LocalTrialState.from_json(json.load(f))
        except Exception:
            logger.debug("[local_trial] 状态文件损坏，按未初始化处理", exc_info=True)
        if st is None:
            st = LocalTrialState(chars=self._chars, window_hours=self._window)
        self._cache, self._cache_ts = st, time.time()
        return st

    def _write(self, st: LocalTrialState) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(st.to_json(), f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            logger.debug("[local_trial] 状态写入失败（已忽略）", exc_info=True)
        finally:
            # 无论落盘成败，内存视图都以刚写的为准（失败时下次 TTL 到期会再读盘纠正）
            self._cache, self._cache_ts = st, time.time()

    # ── 对外 ────────────────────────────────────────────────────────────
    def state(self) -> LocalTrialState:
        return self._read()

    def lic_id(self) -> str:
        st = self._read()
        return lic_id_for(st.machine or self._machine)

    def begin(self, *, now: Optional[float] = None) -> LocalTrialState:
        """确保锚点已落地（首次调用即开始计时）。已关闭/已开始则原样返回。"""
        n = float(now if now is not None else time.time())
        with self._lock:
            st = self._read()
            if st.closed:
                return st
            changed = False
            if not st.first_seen:
                st.first_seen = n
                st.machine = st.machine or self._machine
                st.chars = st.chars or self._chars
                st.window_hours = st.window_hours or self._window
                changed = True
            if n > st.last_seen:
                st.last_seen = n
                changed = True
            if changed:
                self._write(st)
            return st

    def touch(self, *, now: Optional[float] = None,
              min_interval: float = TOUCH_MIN_INTERVAL_SEC) -> None:
        """推进 last_seen 单调水位（防时钟回拨；不新建锚点）。

        **节流落盘**：本方法挂在额度记账的热路上（每条翻译/合成一次），不节流就是
        每句话写一次 JSON。只有水位前进超过 ``min_interval`` 才落盘——回拨要有意义
        得拨小时级，5 分钟粒度的水位完全够用。
        """
        n = float(now if now is not None else time.time())
        with self._lock:
            st = self._read()
            if not st.first_seen or n <= st.last_seen:
                return
            if (n - st.last_seen) < max(0.0, float(min_interval)):
                return
            st.last_seen = n
            self._write(st)

    def close(self, reason: str = "") -> None:
        """永久关闭体验档（注册换正式试用后调用）。"""
        with self._lock:
            st = self._read()
            if st.closed:
                return
            st.closed = True
            if not st.first_seen:
                st.first_seen = time.time()
            self._write(st)
        logger.info("[local_trial] 体验档已关闭（%s）", reason or "registered")

    def snapshot(self, *, used_chars: int = 0,
                 now: Optional[float] = None) -> Dict[str, Any]:
        st = self._read()
        n = float(now if now is not None else time.time())
        out = evaluate(st, now=n, used_chars=used_chars)
        out["lic_id"] = lic_id_for(st.machine or self._machine)
        out["machine"] = st.machine or self._machine
        out["started"] = bool(st.first_seen)
        out["window_hours"] = st.window_hours
        return out


# ── 配置 + 单例 ─────────────────────────────────────────────────────────
_TRIAL: Optional[LocalTrial] = None
_ENABLED = False
_ENFORCE = False
_CFG_LOCK = threading.Lock()


def _cfg(config: Dict[str, Any]) -> Dict[str, Any]:
    lic = (config or {}).get("licensing", {}) or {}
    return lic.get("trial", {}) or {}


def configure_local_trial(
    config: Dict[str, Any], *, config_dir: str = "", trial: Any = None,
) -> Optional[LocalTrial]:
    """启动期装配。``licensing.trial.enabled`` 默认关（新子系统惯例）。

    桌面版在随包种子 ``config.desktop.min.yaml`` 里开启——服务器部署不受影响，
    而体验档本来就是给桌面首装用户的。

    **但种子只在配置文件不存在时播种**，所以升级安装（config.yaml 是旧的、
    压根没有 licensing 段）永远拿不到体验档——173 实机升级到 0.2.3 后实测
    `source=license / included=0`，装完还是什么都没有。改用户的 config.yaml 去补
    这一段太脏（会动他们的注释和自定义），故改为：**桌面模式下、且配置里完全没写过
    `licensing.trial` 时，按桌面默认开启**。配置里一旦写了就完全以配置为准
    （包括显式 `enabled: false`），服务器部署没有 AITR_DESKTOP_MODE 也不受影响。
    """
    global _TRIAL, _ENABLED, _ENFORCE
    c = _cfg(config)
    # 「没写过」与「写了 false」必须分开：后者是明确的关闭意愿，不能被默认值顶掉。
    unset = "enabled" not in c
    desktop = str(os.environ.get("AITR_DESKTOP_MODE") or "") == "1"
    with _CFG_LOCK:
        _ENABLED = desktop if unset else bool(c.get("enabled", False))
        if unset and desktop:
            logger.info("[local_trial] 配置未写 licensing.trial，桌面模式按默认开启体验档")
        _ENFORCE = bool(c.get("enforce", False))
        if trial is not None:
            _TRIAL = trial
            return _TRIAL
        if not _ENABLED:
            _TRIAL = None
            return None
        base = config_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__)))), "config")
        from src.licensing.machine_bridge import machine_short
        _TRIAL = LocalTrial(
            os.path.join(base, STATE_FILENAME),
            chars=int(c.get("chars", DEFAULT_CHARS) or DEFAULT_CHARS),
            window_hours=float(c.get("window_hours", DEFAULT_WINDOW_HOURS)
                               or DEFAULT_WINDOW_HOURS),
            machine_short=machine_short(),
        )
        return _TRIAL


def get_local_trial() -> Optional[LocalTrial]:
    """当前体验档（未启用 → None）。"""
    return _TRIAL


def local_trial_enforced() -> bool:
    """用尽/过期后是否真的拦。默认关——注册换正式试用的链路上线前开它会把新装用户锁死。"""
    return bool(_ENABLED and _ENFORCE)


def reset_local_trial() -> None:
    """测试钩子。"""
    global _TRIAL, _ENABLED, _ENFORCE
    with _CFG_LOCK:
        _TRIAL = None
        _ENABLED = False
        _ENFORCE = False
