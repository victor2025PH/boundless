# -*- coding: utf-8 -*-
"""升级式提醒的持久化状态账本（2026-09-10 运维群降噪 P0.2/P0.3）。

背景：``HealthWatchdog`` 二十多个「首提 → 每 N 小时重提 → 恢复通知」的巡检，状态
（``_*_alerted`` / ``_*_last_remind`` / ``*_down_since``）全是实例属性——**进程一重启
全部清零**。09-10 实测：15:10 与 17:31 两次重启各把「待审草稿 / 客户在等 / 案例积压 /
语音掉线 / LAN GPU」五张卡整轮重发一遍，且「已 30 分钟」这类时长从重启时刻重新计
（实际已 16 小时）。同时这些慢性积压内容一个月没变，每 4h 一遍同样的数字。

本模块给巡检提供一份**跨重启**的、带**内容指纹**的状态：

- ``first_seen``：条件首次成立的时刻（时长口径，重启不重置）；
- ``alerted`` / ``last_remind``：是否已首提、上次外发时刻（重启后接着算间隔，不重发）；
- ``fingerprint``：上次外发时的内容指纹。指纹**未变**（同样那 5 条稿、同样那 3 个案例）
  时，重提间隔从 ``interval_sec`` 放宽到 ``unchanged_interval_sec``（默认 24h）——
  「情况有变化」才值得实时打断人，没变化的一天提一次足够；
- 恢复：``resolve()`` 返回「此前是否告过警」，调用方据此决定发不发恢复通知。

文件形态：单个 JSON（``{key: {alerted, first_seen, last_remind, fingerprint}}``），
原子写（临时文件 + replace）。路径为 None 时纯内存（单测 / 无配置目录场景）。
任何读写异常都吞掉——账本坏了退化成「重启前行为」，绝不让巡检本身挂掉。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

STATE_FILENAME = "health_remind_state.json"

# 账本里超过这个年龄没被 touch 过的条目视为陈旧丢弃（防长期堆积 / 改名遗留）。
_STALE_ENTRY_SEC = 30 * 86400.0

# 首提 / 重提 / 静默 的判定结果
FIRST = "first"
REMIND = "remind"
HOLD = "hold"


def fingerprint(*parts: Any) -> str:
    """把若干可 JSON 化的片段压成稳定短指纹（顺序敏感，dict 键排序）。"""
    try:
        raw = json.dumps(list(parts), ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        raw = repr(parts)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


def state_path(config_dir: Optional[Path]) -> Optional[Path]:
    if config_dir is None:
        return None
    try:
        return Path(config_dir) / STATE_FILENAME
    except Exception:
        return None


class RemindLedger:
    def __init__(self, path: Optional[Path] = None, *, now: Optional[float] = None) -> None:
        self._path = Path(path) if path else None
        self._data: Dict[str, Dict[str, Any]] = {}
        self._load(now=now)

    # ── 持久化 ────────────────────────────────────────────────────────────
    def _load(self, *, now: Optional[float] = None) -> None:
        if self._path is None or not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("提醒状态账本损坏，按空账本启动：%s", self._path)
            return
        if not isinstance(raw, dict):
            return
        ts = float(now if now is not None else time.time())
        kept = 0
        for key, st in raw.items():
            if not isinstance(st, dict):
                continue
            touched = float(st.get("touched") or st.get("last_remind") or st.get("first_seen") or 0.0)
            if touched and ts - touched > _STALE_ENTRY_SEC:
                continue
            meta = st.get("meta")
            self._data[str(key)] = {
                "alerted": bool(st.get("alerted")),
                "first_seen": float(st.get("first_seen") or 0.0),
                "last_remind": float(st.get("last_remind") or 0.0),
                "fingerprint": str(st.get("fingerprint") or ""),
                "meta": dict(meta) if isinstance(meta, dict) else {},
                "touched": touched,
            }
            kept += 1
        if kept:
            logger.info("提醒状态账本已恢复 %d 条（%s）", kept, self._path)

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(self._path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self._path)
        except Exception:
            logger.debug("提醒状态账本落盘失败（忽略）", exc_info=True)

    # ── 读 ────────────────────────────────────────────────────────────────
    def get(self, key: str) -> Dict[str, Any]:
        st = self._data.get(key)
        if st is None:
            st = {"alerted": False, "first_seen": 0.0, "last_remind": 0.0,
                  "fingerprint": "", "meta": {}, "touched": 0.0}
            self._data[key] = st
        return st

    def last_remind(self, key: str) -> float:
        return float(self.get(key)["last_remind"] or 0.0)

    def meta(self, key: str, field: str, default: Any = None) -> Any:
        return (self.get(key).get("meta") or {}).get(field, default)

    def alerted(self, key: str) -> bool:
        return bool(self.get(key)["alerted"])

    def first_seen(self, key: str) -> float:
        return float(self.get(key)["first_seen"] or 0.0)

    def active_seconds(self, key: str, now: Optional[float] = None) -> float:
        """条件持续了多久（first_seen 为 0 → 0）。"""
        fs = self.first_seen(key)
        if fs <= 0:
            return 0.0
        return max(0.0, float(now if now is not None else time.time()) - fs)

    def keys(self) -> list:
        return list(self._data.keys())

    # ── 写 ────────────────────────────────────────────────────────────────
    def observe(self, key: str, *, now: Optional[float] = None,
                since: Optional[float] = None) -> float:
        """条件成立：登记 first_seen（若尚未记），返回 first_seen。

        ``since``：调用方算得出的**条件真实成立时刻**（例如「第 3 条稿超过 24h 的那一刻」）。
        账本首次见到某条件往往晚于它成立（账本 09-10 才上线，积压 8 月就有了），不给
        ``since`` 时「已开 X」只能从账本首见起算、偏短；给了就以它为 first_seen（不晚于 now）。
        """
        ts = float(now if now is not None else time.time())
        st = self.get(key)
        try:
            since_f = float(since) if since is not None else 0.0
        except (TypeError, ValueError):
            since_f = 0.0
        if not (0 < since_f < ts):
            since_f = 0.0
        if st["first_seen"] <= 0:
            st["first_seen"] = since_f or ts
            st["touched"] = ts
            self._save()
        elif since_f and since_f < st["first_seen"]:
            # 只允许往早修正（账本上线前就成立的条件，首见时刻偏晚）；绝不往晚改
            st["first_seen"] = since_f
            self._save()
        return float(st["first_seen"])

    def decide(self, key: str, *, now: Optional[float] = None,
               after_sec: float = 0.0, interval_sec: float,
               fp: Optional[str] = None,
               unchanged_interval_sec: Optional[float] = None,
               since: Optional[float] = None) -> str:
        """条件成立时问「这一轮要不要外发」。

        - 尚未首提：持续 ≥ ``after_sec`` → ``FIRST``，否则 ``HOLD``；
        - 已首提：距上次 ≥ 有效间隔 → ``REMIND``，否则 ``HOLD``。
          有效间隔：给了 ``fp`` 且与上次外发指纹**相同** → ``unchanged_interval_sec``
          （缺省 = max(interval_sec, 24h)）；指纹变了 → ``interval_sec``。
        ``since`` 透传 :meth:`observe`（条件真实成立时刻，只在首次登记时生效）。
        只判定，不改状态；外发成功后调 ``mark_sent()``。
        """
        ts = float(now if now is not None else time.time())
        st = self.get(key)
        self.observe(key, now=ts, since=since)
        # 人工闭嘴（运维群卡片「静音 / 已处理」，P1.3）：静音期内一律不外发；
        # 「已处理」= 内容指纹不变就不再提（情况变了才重新打断人）。恢复通知不受影响。
        if self.is_muted(key, now=ts):
            return HOLD
        acked_fp = str((st.get("meta") or {}).get("acked_fp") or "")
        if acked_fp and fp is not None and acked_fp == fp:
            return HOLD
        if not st["alerted"]:
            return FIRST if (ts - st["first_seen"]) >= float(after_sec) else HOLD
        eff = float(interval_sec)
        if fp is not None and st.get("fingerprint") and st["fingerprint"] == fp:
            eff = float(unchanged_interval_sec if unchanged_interval_sec is not None
                        else max(interval_sec, 86400.0))
        return REMIND if (ts - float(st["last_remind"] or 0.0)) >= eff else HOLD

    def mark_sent(self, key: str, *, now: Optional[float] = None,
                  fp: Optional[str] = None, summary: Optional[str] = None) -> None:
        """外发成功后登记。``summary`` 是一行人话（「待审草稿 5 条，最久 30 天」），
        供每日摘要直接引用——摘要不必再各自重算一遍巡检。"""
        ts = float(now if now is not None else time.time())
        st = self.get(key)
        st["alerted"] = True
        st["last_remind"] = ts
        st["touched"] = ts
        if st["first_seen"] <= 0:
            st["first_seen"] = ts
        if fp is not None:
            st["fingerprint"] = fp
        if summary is not None:
            st.setdefault("meta", {})["summary"] = str(summary)[:200]
        self._save()

    def open_items(self, *, now: Optional[float] = None) -> list:
        """仍在告警中的条目（已首提、尚未恢复），按首次成立时刻升序。
        每项 ``{key, first_seen, last_remind, summary, meta, muted_until, acked}``——每日摘要的数据面。"""
        ts = float(now if now is not None else time.time())
        out = []
        for key, st in self._data.items():
            if not st.get("alerted"):
                continue
            meta = dict(st.get("meta") or {})
            out.append({
                "key": key,
                "first_seen": float(st.get("first_seen") or 0.0),
                "last_remind": float(st.get("last_remind") or 0.0),
                "summary": str(meta.get("summary") or ""),
                "meta": meta,
                "muted_until": float(meta.get("muted_until") or 0.0) if self.is_muted(key, now=ts) else 0.0,
                "acked": bool(meta.get("acked_fp")) and str(meta.get("acked_fp")) == self._fp_of(st),
                "by": str(meta.get("muted_by") or meta.get("acked_by") or ""),
            })
        out.sort(key=lambda x: (x["first_seen"] or float("inf"), x["key"]))
        return out

    # ── 人工闭嘴（P1.3 认领 / 静音）────────────────────────────────────────
    @staticmethod
    def _fp_of(st: Dict[str, Any]) -> str:
        """条目当前内容指纹：decide/mark_sent 走 ``fingerprint``；LAN GPU 巡检沿用旧直写口
        把形态指纹放在 ``meta.fp``——两处都认。"""
        return str(st.get("fingerprint") or (st.get("meta") or {}).get("fp") or "")

    def is_muted(self, key: str, *, now: Optional[float] = None) -> bool:
        st = self._data.get(key)
        if not st:
            return False
        until = float((st.get("meta") or {}).get("muted_until") or 0.0)
        return until > float(now if now is not None else time.time())

    def mute(self, key: str, hours: float, *, by: str = "", now: Optional[float] = None) -> float:
        """静音 ``hours`` 小时（上限 7 天）：期内 decide() 一律 HOLD。返回截止时刻。"""
        ts = float(now if now is not None else time.time())
        h = min(max(float(hours or 0.0), 0.0), 7 * 24.0)
        until = ts + h * 3600.0
        st = self.get(key)
        meta = st.setdefault("meta", {})
        meta["muted_until"] = until
        meta["muted_by"] = str(by or "")[:60]
        meta["muted_at"] = ts
        st["touched"] = ts
        self._save()
        return until

    def ack(self, key: str, *, by: str = "", now: Optional[float] = None) -> str:
        """认领 / 已处理：记下当前内容指纹，指纹不变就不再重提（情况变了才重新打断人）。
        尚无指纹（从未按指纹外发）的条目退化为静音 24h。返回 ``acked`` 或 ``muted``。"""
        ts = float(now if now is not None else time.time())
        st = self.get(key)
        fp = self._fp_of(st)
        if not fp:
            self.mute(key, 24.0, by=by, now=ts)
            return "muted"
        meta = st.setdefault("meta", {})
        meta["acked_fp"] = fp
        meta["acked_by"] = str(by or "")[:60]
        meta["acked_at"] = ts
        st["touched"] = ts
        self._save()
        return "acked"

    def unmute(self, key: str, *, now: Optional[float] = None) -> None:
        st = self._data.get(key)
        if not st:
            return
        meta = st.setdefault("meta", {})
        for k in ("muted_until", "muted_by", "muted_at", "acked_fp", "acked_by", "acked_at"):
            meta.pop(k, None)
        self._save()

    def resolve(self, key: str, *, now: Optional[float] = None) -> bool:
        """条件不再成立：清状态，返回「此前是否已首提」（True → 该发恢复通知）。"""
        st = self._data.get(key)
        was = bool(st and st.get("alerted"))
        had = bool(st and (st.get("alerted") or st.get("first_seen")))
        if had:
            self._data.pop(key, None)
            self._save()
        return was

    def touch(self, key: str, *, now: Optional[float] = None) -> None:
        st = self.get(key)
        st["touched"] = float(now if now is not None else time.time())

    # ── 直写口（给沿用旧属性写法的巡检 / 单测用；语义与旧内存字段一一对应）──
    def _drop_if_blank(self, key: str) -> None:
        st = self._data.get(key)
        if st and not st["alerted"] and st["first_seen"] <= 0 and st["last_remind"] <= 0 \
                and not st.get("meta"):
            self._data.pop(key, None)

    def set_alerted(self, key: str, value: bool) -> None:
        st = self.get(key)
        st["alerted"] = bool(value)
        self._drop_if_blank(key)
        self._save()

    def set_first_seen(self, key: str, ts: float) -> None:
        st = self.get(key)
        st["first_seen"] = float(ts or 0.0)
        self._drop_if_blank(key)
        self._save()

    def set_last_remind(self, key: str, ts: float) -> None:
        st = self.get(key)
        st["last_remind"] = float(ts or 0.0)
        self._drop_if_blank(key)
        self._save()

    def set_meta(self, key: str, field: str, value: Any) -> None:
        st = self.get(key)
        meta = st.setdefault("meta", {})
        if value in (None, ""):
            meta.pop(field, None)
        else:
            meta[field] = value
        self._drop_if_blank(key)
        self._save()
