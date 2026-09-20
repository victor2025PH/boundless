"""履约端存活监控（厂商机侧）。

试用签发链上唯一的单点是厂商机：它不跑，用户点了「免费领取」就永远停在
「正在签发」——客户端不报错、官网台账只是多一条 pending，**整条链静默失效**。
客服控制台已有心跳卡，但那是被动的，得有人去看。本模块把它升级为主动告警。

判定放在 :func:`evaluate`（纯函数，可单测），HTTP 与缓存放在 :func:`probe`，
接线在 ``HealthWatchdog._check_trial_fulfiller``。

三种不健康，分开报是因为处置动作完全不同：

* ``stale``      —— 心跳过期：履约端没来取待办（任务被删/python 路径变了/机器关了）。
* ``stuck``      —— 心跳新鲜但待办积压：它在来，却清不掉活（回填失败/私钥签不出）。
* ``unreachable``—— 连台账都取不到：本机网络或官网出问题，签发链状态未知。

**必须用 ``?peek=1`` 读台账**：普通路径会记心跳，看门狗自己的轮询就会把心跳刷新，
于是心跳永远新鲜、永远发现不了履约端已死——监控自我失效。这条不变量有门禁守着。
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: 探针 TTL。看门狗 tick 通常 300s，60s 缓存足够，且避免 recheck 连打官网。
PROBE_TTL_SEC = 60.0
HTTP_TIMEOUT = 12

_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None, "url": ""}


def watch_target(config: Optional[dict] = None) -> Optional[Dict[str, Any]]:
    """本机是否该监控履约端；不该则返回 ``None``（天然静默，零误报）。

    默认关：只有厂商机（持私钥、装了 ChatXTrialFulfill 任务的那台）该开。
    ``admin_key_file`` 只给**路径**——密钥内容不入配置、不进日志、不上命令行。
    """
    cfg = ((config or {}).get("licensing") or {}).get("trial") or {}
    fw = cfg.get("fulfiller_watch") or {}
    if not fw.get("enabled", False):
        return None
    site = str(fw.get("site_url") or cfg.get("site_url") or "").strip().rstrip("/")
    key_file = str(fw.get("admin_key_file") or "").strip()
    if not site or not key_file:
        return None
    return {
        "site": site,
        "key_file": key_file,
        "stale_min": float(fw.get("stale_min", 15) or 15),
        "backlog_min": float(fw.get("backlog_min", 20) or 20),
        "after_min": float(fw.get("after_min", 15) or 15),
        "interval_min": float(fw.get("interval_min", 240) or 240),
    }


def _parse_iso(s: str) -> Optional[float]:
    t = str(s or "").strip()
    if not t:
        return None
    try:
        dt = _dt.datetime.fromisoformat(t.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def evaluate(stats: Optional[dict], *, now: Optional[float] = None,
             stale_min: float = 15.0, backlog_min: float = 20.0) -> Dict[str, Any]:
    """纯判定：台账 stats → 履约端健康快照。

    返回 ``{healthy, kind, heartbeat_min, pending, backlog_min, never}``。
    ``kind`` 为 ``""``（健康）/ ``stale`` / ``stuck`` / ``unreachable``。
    """
    ts = float(now if now is not None else time.time())
    if not isinstance(stats, dict):
        return {"healthy": False, "kind": "unreachable", "heartbeat_min": -1,
                "pending": -1, "backlog_min": -1, "never": False}

    pending = int(stats.get("pending") or 0)
    backlog = float(stats.get("oldest_pending_min") or 0)
    beat = _parse_iso(str(stats.get("fulfiller_last_seen") or ""))
    if beat is None:
        # 从未来过。既然开了监控就说明它应该在跑，这与「过期」同样需要处置。
        return {"healthy": False, "kind": "stale", "heartbeat_min": -1,
                "pending": pending, "backlog_min": int(backlog), "never": True}

    # 时钟差/官网时间略超前时钳到 0，避免负龄被当成「刚刚」以外的怪值
    age_min = max(0.0, (ts - beat) / 60.0)
    if age_min >= stale_min:
        return {"healthy": False, "kind": "stale", "heartbeat_min": int(age_min),
                "pending": pending, "backlog_min": int(backlog), "never": False}
    # 心跳新鲜却清不掉活 —— 它在来，但签不出/回填不上
    if pending > 0 and backlog >= backlog_min:
        return {"healthy": False, "kind": "stuck", "heartbeat_min": int(age_min),
                "pending": pending, "backlog_min": int(backlog), "never": False}
    return {"healthy": True, "kind": "", "heartbeat_min": int(age_min),
            "pending": pending, "backlog_min": int(backlog), "never": False}


def _read_key(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except Exception:
        logger.debug("[trial-watch] 读 admin key 失败", exc_info=True)
        return ""


def fetch_stats(target: Dict[str, Any]) -> Optional[dict]:
    """取台账 stats（``?peek=1`` 只读不打卡）。失败返回 ``None``。

    服务端必须回声 ``peeked: true``——这是能力握手。旧版本官网不认 peek，我们这一读
    就会把心跳刷新，于是心跳永远新鲜、履约端死了也永远不报。宁可**不监控**（返回
    ``{"unsupported": True}`` 让上层静默）也不要闭眼监控给人虚假的安全感。
    """
    key = _read_key(str(target.get("key_file") or ""))
    if not key:
        return None
    url = f"{target['site']}/api/admin/trial-claims?status=pending&limit=1&peek=1"
    req = urllib.request.Request(url, method="GET")
    req.add_header("accept", "application/json")
    req.add_header("x-setup-key", key)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
        if body.get("peeked") is not True:
            logger.warning(
                "[trial-watch] 官网未回声 peeked —— 该版本不认只读读法，"
                "本机监控保持静默（否则会把心跳刷新、监控自我失效）。先部署官网再开监控。")
            return {"unsupported": True}
        st = body.get("stats")
        return st if isinstance(st, dict) else None
    except urllib.error.HTTPError as e:
        # 401 = 密钥不对，这本身就是「签发链废了」——照 unreachable 处置并留痕
        logger.warning("[trial-watch] 取台账 HTTP %s", e.code)
        return None
    except Exception:
        logger.debug("[trial-watch] 取台账失败", exc_info=True)
        return None


def probe(config: Optional[dict] = None, *,
          now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """探针（带 TTL 缓存）。未启用 → ``None``；否则返回 :func:`evaluate` 的快照。"""
    target = watch_target(config)
    if target is None:
        return None
    ts = float(now if now is not None else time.time())
    if (_CACHE["data"] is not None and _CACHE["url"] == target["site"]
            and ts - float(_CACHE["ts"]) < PROBE_TTL_SEC):
        return dict(_CACHE["data"])
    raw = fetch_stats(target)
    if isinstance(raw, dict) and raw.get("unsupported"):
        # 官网还没上 peek：保持静默（不缓存，好让部署后下一 tick 立刻恢复监控）
        return None
    snap = evaluate(raw, now=ts,
                    stale_min=target["stale_min"], backlog_min=target["backlog_min"])
    snap["site"] = target["site"]
    _CACHE.update({"ts": ts, "data": dict(snap), "url": target["site"]})
    return dict(snap)


def reset_probe_cache() -> None:
    """测试钩子。"""
    _CACHE.update({"ts": 0.0, "data": None, "url": ""})
