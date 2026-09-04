# -*- coding: utf-8 -*-
"""风险放行影子台账（#160 I-1-C，2026-09-04）。

老板拍板 v2：风险扣稿一条不留、全部放行，**触发只写台账**；攒满一个月真实
流量再定新拦截规则。本模块就是那份台账。

两层口径，别混：
  · **JSONL 落盘**＝持久口径、一个月数据的权威来源。落点
    ``logs/autosend_shadow/shadow_YYYYMMDD.jsonl``（相对 CWD——生产进程 CWD 是实例
    数据根，相对路径在这里是**正确**的，见 tools/audit_relative_paths 的 C 类；
    测试/CLI 用 ``AITR_AUTOSEND_SHADOW_DIR`` 覆写落点）。与 care.llm_extract 影子档
    同款：进程计数器重启就清零，**JSONL 才是持久口径**。
  · **进程内计数器**＝ops 看板/metrics 的实时读数（按 hold_reason 分桶、
    stop_contact / self_harm 单列——那两个是要人看的）。

⛔ **不记出站/入站原文**：``conv_key`` + ``draft_id`` + ``ts`` 已足够回溯到
protocol_media 里的原始会话做抽样复核；落原文只是白担隐私风险。文本只留
``text_fp``（sha1 前 8 位）与 ``text_len``。

``risk_hits``（命中词）**必须有**——没有它一个月后分不清「真该拦」还是「正则误伤」。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_DIR = "logs/autosend_shadow"
ENV_DIR = "AITR_AUTOSEND_SHADOW_DIR"
FILE_PREFIX = "shadow_"

# 要人看的两个 reason：命中即时推值守群（不拦，只报）
ALERT_REASONS = ("stop_contact", "self_harm")

# 台账字段契约（CLI 与门禁按它读；新增字段只许追加，不许改名/删除——
# 一个月后补不回来的字段现在不加就永久缺失）
RECORD_FIELDS = (
    "ts", "platform", "account_id", "conv_key", "draft_id",
    "would_hold_level", "hold_reason", "peer_risk", "peer_reasons",
    "reply_risk", "reply_reasons", "risk_hits", "text_fp", "text_len",
    # 追加字段（v1 即有，供分析分桶）
    "stage",            # peer=入站定级时 / reply=AI 稿收尾时 / analysis=LLM 分析 overlay
    "automation_mode",  # 会话档位（应恒为 auto_ai——review 不进台账）
    "policy_mode",      # shadow（enforce 档不写本台账）
)


_WRITE_LOCK = threading.Lock()


def _day_key(ts: float) -> str:
    return time.strftime("%Y%m%d", time.localtime(ts))


class ShadowStats:
    """进程级计数器（重启清零；持久口径看 JSONL）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self.total = 0
            self.by_reason: Dict[str, int] = {}
            self.by_level: Dict[str, int] = {}
            self.by_stage: Dict[str, int] = {}
            self.by_hit: Dict[str, int] = {}
            self.alerts = 0
            self.write_errors = 0
            self.last_ts = 0.0
            self.last_reason = ""
            self._day = _day_key(time.time())
            self.today = 0

    def bump(self, rec: Dict[str, Any]) -> None:
        with self._lock:
            now = float(rec.get("ts") or time.time())
            day = _day_key(now)
            if day != self._day:
                self._day, self.today = day, 0
            self.total += 1
            self.today += 1
            r = str(rec.get("hold_reason") or "unknown")
            self.by_reason[r] = self.by_reason.get(r, 0) + 1
            lv = str(rec.get("would_hold_level") or "?")
            self.by_level[lv] = self.by_level.get(lv, 0) + 1
            st = str(rec.get("stage") or "?")
            self.by_stage[st] = self.by_stage.get(st, 0) + 1
            for h in (rec.get("risk_hits") or [])[:8]:
                hk = str(h)[:40]
                if len(self.by_hit) < 200 or hk in self.by_hit:
                    self.by_hit[hk] = self.by_hit.get(hk, 0) + 1
            self.last_ts = now
            self.last_reason = r

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            top_hits = sorted(self.by_hit.items(), key=lambda kv: -kv[1])[:10]
            return {
                "total": self.total,
                "today": self.today if self._day == _day_key(time.time()) else 0,
                "by_reason": dict(sorted(self.by_reason.items(), key=lambda kv: -kv[1])),
                "by_level": dict(self.by_level),
                "by_stage": dict(self.by_stage),
                "top_hits": [{"hit": k, "n": v} for k, v in top_hits],
                "stop_contact": self.by_reason.get("stop_contact", 0),
                "self_harm": self.by_reason.get("self_harm", 0),
                "alerts": self.alerts,
                "write_errors": self.write_errors,
                "last_ts": self.last_ts,
                "last_reason": self.last_reason,
                "dir": str(shadow_dir()),
            }


_STATS = ShadowStats()


def get_stats() -> ShadowStats:
    return _STATS


def stats_snapshot() -> Dict[str, Any]:
    """metrics / ops 卡消费口。零命中时 total=0（卡片按此隐藏）。"""
    return _STATS.snapshot()


def shadow_dir() -> Path:
    return Path(os.environ.get(ENV_DIR) or DEFAULT_DIR)


def shadow_file_for(ts: float) -> Path:
    return shadow_dir() / f"{FILE_PREFIX}{_day_key(ts)}.jsonl"


def text_fingerprint(text: str) -> str:
    """出站文本 sha1 前 8 位——只用于「同一稿」去重/回溯，不可逆。"""
    return hashlib.sha1(str(text or "").encode("utf-8", "ignore")).hexdigest()[:8]


def build_record(
    *,
    platform: str,
    account_id: str,
    conv_key: str,
    draft_id: str,
    would_hold_level: str,
    hold_reason: str,
    peer_risk: str,
    peer_reasons: Iterable[str],
    reply_risk: str,
    reply_reasons: Iterable[str],
    risk_hits: Iterable[str],
    text: str = "",
    stage: str = "peer",
    automation_mode: str = "auto_ai",
    policy_mode: str = "shadow",
    ts: Optional[float] = None,
) -> Dict[str, Any]:
    """按 RECORD_FIELDS 契约组装一行（不落盘）。``text`` 只取指纹与长度，绝不入账。"""
    return {
        "ts": float(ts if ts is not None else time.time()),
        "platform": str(platform or ""),
        "account_id": str(account_id or ""),
        "conv_key": str(conv_key or ""),
        "draft_id": str(draft_id or ""),
        "would_hold_level": str(would_hold_level or ""),
        "hold_reason": str(hold_reason or ""),
        "peer_risk": str(peer_risk or "low"),
        "peer_reasons": [str(r) for r in (peer_reasons or [])],
        "reply_risk": str(reply_risk or "low"),
        "reply_reasons": [str(r) for r in (reply_reasons or [])],
        # 命中词＝正则匹配到的**短语片段**（截 40 字），不是整条消息
        "risk_hits": [str(h)[:40] for h in (risk_hits or [])][:16],
        "text_fp": text_fingerprint(text) if text else "",
        "text_len": len(str(text or "")),
        "stage": str(stage or "peer"),
        "automation_mode": str(automation_mode or ""),
        "policy_mode": str(policy_mode or "shadow"),
    }


def record(rec: Dict[str, Any]) -> bool:
    """落一行 JSONL + 计数。任何异常只记日志不抛——台账绝不阻塞发送。"""
    ok = True
    try:
        p = shadow_file_for(float(rec.get("ts") or time.time()))
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
        with _WRITE_LOCK:
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        ok = False
        _STATS.write_errors += 1
        logger.warning("autosend_shadow 台账落盘失败（已忽略）", exc_info=True)
    _STATS.bump(rec)
    return ok


def maybe_alert(rec: Dict[str, Any]) -> bool:
    """``stop_contact`` / ``self_harm`` 命中 → 即时 EventBus 事件（不拦，只报）。

    事件 ``autosend_shadow_alert``，webhook 订阅别名 ``autosend_shadow``；
    ``rate_key`` 按 ``account:conv`` 防同会话刷屏。文案带会话 id + 命中词 +
    「已放行，如需止损请人工介入」。返回是否已发布。
    """
    reason = str(rec.get("hold_reason") or "")
    if reason not in ALERT_REASONS:
        return False
    try:
        from src.integrations.shared.event_bus import get_event_bus
        acct = str(rec.get("account_id") or "")
        conv = str(rec.get("conv_key") or "")
        get_event_bus().publish("autosend_shadow_alert", {
            "reason": reason,
            "platform": str(rec.get("platform") or ""),
            "account_id": acct,
            "conv_key": conv,
            "draft_id": str(rec.get("draft_id") or ""),
            "would_hold_level": str(rec.get("would_hold_level") or ""),
            "risk_hits": list(rec.get("risk_hits") or [])[:6],
            "stage": str(rec.get("stage") or ""),
            "rate_key": f"{acct}:{conv}",
        })
        _STATS.alerts += 1
        return True
    except Exception:
        logger.debug("autosend_shadow_alert 发布失败（已忽略）", exc_info=True)
        return False


def iter_records(days: int = 30, *, base: Optional[Path] = None,
                 now: Optional[float] = None) -> Iterable[Dict[str, Any]]:
    """只读遍历近 N 天 JSONL（CLI 用）。坏行跳过不抛。"""
    base = Path(base) if base is not None else shadow_dir()
    if not base.exists():
        return
    now = float(now if now is not None else time.time())
    keep = {_day_key(now - 86400 * i) for i in range(max(1, int(days)))}
    for p in sorted(base.glob(f"{FILE_PREFIX}*.jsonl")):
        day = p.stem[len(FILE_PREFIX):]
        if day not in keep:
            continue
        try:
            with open(p, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except Exception:
                        continue
                    if isinstance(obj, dict):
                        yield obj
        except Exception:
            logger.debug("读取 %s 失败（跳过）", p, exc_info=True)


__all__ = [
    "ALERT_REASONS", "RECORD_FIELDS", "DEFAULT_DIR", "ENV_DIR",
    "build_record", "record", "maybe_alert", "iter_records",
    "stats_snapshot", "get_stats", "shadow_dir", "shadow_file_for", "text_fingerprint",
]
