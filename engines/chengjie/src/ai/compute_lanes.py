# -*- coding: utf-8 -*-
"""三路算力互相顶班 + 欠费/故障登记（2026-09-18）。

三档（与 ``ai_primary_summary.chain_roles`` 同名）：``local``＝173 vLLM，
``cloud``＝``ai.base_url``（现网 DeepSeek 官方），``pool``＝``ai.key_pool`` 首键
（现网硅基流动）。出话顺序仍按主链档位，但**已登记欠费/失效的档直接跳过**，
让还能用的那一档立刻顶上——不再出现「DeepSeek 没钱、本地还在答、硅基闲置」。

本模块是进程内登记表 + 无 token 探活。告警外发在 HealthWatchdog；出话链只问
``should_skip`` / ``failover_order``。永不抛、零密钥入日志。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

LANES = ("local", "cloud", "pool")
LANE_LABELS = {
    "local": "173 本地 vLLM",
    "cloud": "DeepSeek 官方",
    "pool": "硅基流动备用",
}
NAG_INTERVAL_SEC = 180.0
# 欠费/鉴权：跳过这一档，直到探活或出话成功清掉。
SKIP_QUOTA_SEC = 600.0
# timeout / 5xx / 连续连接失败：短跳过，避免每句先撞死端点。
SKIP_FAIL_SEC = 120.0
# 173 keep-alive 毛刺（2026-09-18）：单次 Connection error 不是宕机。
# 旧口径一次 connect 就冻 120s → 机子还在答、本进程已经改走云。
# 第 1 次不跳过（调用方当场重试）；第 2 次短冻；第 3 次才按真故障冻 120s。
SKIP_CONNECT_STREAK_SEC = 15.0
CONNECT_STREAK_WINDOW_SEC = 45.0
REMIND_KEY_PREFIX = "compute_lane:"

_lock = threading.Lock()
_state: Dict[str, Dict[str, Any]] = {}


def reset_lanes() -> None:
    """单测隔离：清空登记表。"""
    with _lock:
        _state.clear()


def _blank(lane: str) -> Dict[str, Any]:
    return {
        "lane": lane,
        "ok": True,
        "kind": "",
        "detail": "",
        "skip_until": 0.0,
        "last_ok_ts": 0.0,
        "last_fail_ts": 0.0,
        "fail_streak": 0,
    }


def skip_hold_sec(lane: str, kind: str, *, streak: int = 1) -> float:
    """某次失败该跳过多久。纯函数，供 note_fail 与单测共用。"""
    k = str(kind or "other").strip().lower() or "other"
    if k in ("quota", "auth", "low"):
        return SKIP_QUOTA_SEC
    if str(lane) == "local" and k == "connect":
        n = max(1, int(streak or 1))
        if n <= 1:
            return 0.0
        if n == 2:
            return SKIP_CONNECT_STREAK_SEC
        return SKIP_FAIL_SEC
    return SKIP_FAIL_SEC


def snapshot(*, now: Optional[float] = None) -> Dict[str, Dict[str, Any]]:
    ts = float(now if now is not None else time.time())
    with _lock:
        out = {}
        for lane in LANES:
            row = dict(_state.get(lane) or _blank(lane))
            row["skipped"] = float(row.get("skip_until") or 0.0) > ts
            out[lane] = row
        return out


def should_skip(lane: str, *, now: Optional[float] = None) -> bool:
    ts = float(now if now is not None else time.time())
    with _lock:
        until = float((_state.get(str(lane)) or {}).get("skip_until") or 0.0)
    return until > ts


def note_ok(lane: str, *, now: Optional[float] = None) -> None:
    if str(lane) not in LANES:
        return
    ts = float(now if now is not None else time.time())
    with _lock:
        st = _state.setdefault(str(lane), _blank(str(lane)))
        st["ok"] = True
        st["kind"] = ""
        st["detail"] = ""
        st["skip_until"] = 0.0
        st["last_ok_ts"] = ts
        st["fail_streak"] = 0


def note_fail(lane: str, kind: str, detail: str = "", *, now: Optional[float] = None,
              skip: bool = True) -> None:
    """登记一档失败。``quota``/``auth`` 长跳过，其余短跳过。``skip=False`` 只记账供催办。"""
    if str(lane) not in LANES:
        return
    ts = float(now if now is not None else time.time())
    k = str(kind or "other").strip().lower() or "other"
    with _lock:
        st = _state.setdefault(str(lane), _blank(str(lane)))
        prev_kind = str(st.get("kind") or "")
        prev_ts = float(st.get("last_fail_ts") or 0.0)
        streak = 1
        if k == "connect" and prev_kind == "connect" and prev_ts > 0:
            if (ts - prev_ts) <= CONNECT_STREAK_WINDOW_SEC:
                streak = int(st.get("fail_streak") or 0) + 1
        hold = skip_hold_sec(str(lane), k, streak=streak)
        st["ok"] = False
        st["kind"] = k
        st["detail"] = str(detail or "")[:200]
        st["fail_streak"] = streak
        if skip and hold > 0:
            st["skip_until"] = ts + hold
        elif skip and hold <= 0:
            st["skip_until"] = 0.0
        st["last_fail_ts"] = ts


def failover_order(mode: str, *, now: Optional[float] = None) -> List[str]:
    """按档位给出话顺序，把当前应跳过的档排到队尾（仍保留，充值后本轮还能试）。"""
    try:
        from src.ai.ai_primary_summary import chain_roles
        preferred = list(chain_roles(str(mode or "cloud")).get("order") or list(LANES))
    except Exception:
        preferred = list(LANES)
    ts = float(now if now is not None else time.time())
    if str(mode or "").strip().lower() == "local_only":
        preferred = [x for x in preferred if x == "local"]
    live, skipped = [], []
    for lane in preferred:
        if lane not in LANES:
            continue
        (skipped if should_skip(lane, now=ts) else live).append(lane)
    return live + skipped


def remind_enabled(config: Optional[Dict[str, Any]]) -> bool:
    """服务器默认开；桌面包默认关；单测默认关（避免 _tick 真打三路网）。显式配置优先。"""
    import os
    block = (((config or {}).get("health_watchdog") or {}).get("compute_lane_remind")
             or {}) if isinstance(config, dict) else {}
    if isinstance(block, dict) and "enabled" in block:
        return bool(block.get("enabled"))
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    try:
        from src.utils.desktop_mode import is_desktop_client
        return not is_desktop_client(config if isinstance(config, dict) else {})
    except Exception:
        return True


def nag_interval_sec(config: Optional[Dict[str, Any]] = None) -> float:
    block = (((config or {}).get("health_watchdog") or {}).get("compute_lane_remind")
             or {}) if isinstance(config, dict) else {}
    try:
        raw = float((block or {}).get("interval_sec", NAG_INTERVAL_SEC) or NAG_INTERVAL_SEC)
    except (TypeError, ValueError):
        raw = NAG_INTERVAL_SEC
    return max(60.0, raw)


def remind_key(lane: str) -> str:
    return f"{REMIND_KEY_PREFIX}{lane}"


def kind_zh(kind: str) -> str:
    return {
        "quota": "没有费用 / 余额不可用",
        "low": "余额偏低，快见底",
        "auth": "Key 失效或被拒",
        "connect": "连不上",
        "timeout": "超时",
        "gateway_5xx": "上游 5xx",
        "empty": "有响应但没模型",
        "other": "出错",
    }.get(str(kind or "other"), str(kind or "出错"))


def _endpoints(config: Dict[str, Any]) -> Dict[str, Dict[str, str]]:
    ai = (config or {}).get("ai") or {}
    if not isinstance(ai, dict):
        ai = {}
    fb = ai.get("fallback") if isinstance(ai.get("fallback"), dict) else {}
    pool_item: Dict[str, Any] = {}
    kp = ai.get("key_pool") if isinstance(ai.get("key_pool"), dict) else {}
    if kp.get("enabled", True):
        for item in (kp.get("keys") or []):
            if isinstance(item, dict) and str(item.get("api_key") or "").strip():
                pool_item = item
                break
    return {
        "local": {
            "base_url": str(fb.get("base_url") or "").strip().rstrip("/"),
            "model": str(fb.get("model") or "").strip(),
            "api_key": str(fb.get("api_key") or "vllm").strip(),
            "enabled": "1" if fb.get("enabled", False) else "",
        },
        "cloud": {
            "base_url": str(ai.get("base_url") or "").strip().rstrip("/"),
            "model": str(ai.get("model") or "").strip(),
            "api_key": str(ai.get("api_key") or "").strip(),
        },
        "pool": {
            "base_url": str(pool_item.get("base_url") or ai.get("base_url") or "").strip().rstrip("/"),
            "model": str(pool_item.get("model") or ai.get("model") or "").strip(),
            "api_key": str(pool_item.get("api_key") or "").strip(),
        },
    }


def _http_json(url: str, *, headers: Optional[Dict[str, str]] = None,
               timeout: float = 8.0) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": False, "status": 0, "body": None, "error": ""}
    try:
        req = urllib.request.Request(url, headers=headers or {})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(8000)
            out["status"] = int(getattr(resp, "status", 200) or 200)
            try:
                out["body"] = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                out["body"] = None
            out["ok"] = 200 <= out["status"] < 300
            return out
    except urllib.error.HTTPError as e:
        out["status"] = int(e.code or 0)
        out["error"] = f"HTTP {e.code}"
        return out
    except Exception as e:
        out["error"] = type(e).__name__
        return out


def _models_ok(base: str, api_key: str, model: str = "") -> Dict[str, Any]:
    if not base:
        return {"ok": False, "kind": "other", "detail": "未配置端点"}
    url = base if base.endswith("/models") else (base + "/models")
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    hit = _http_json(url, headers=headers, timeout=8.0)
    if hit["status"] in (401, 402, 403):
        kind = "quota" if hit["status"] == 402 else "auth"
        return {"ok": False, "kind": kind, "detail": hit.get("error") or f"HTTP {hit['status']}"}
    if not hit.get("ok"):
        kind = "connect" if not hit.get("status") else "other"
        return {"ok": False, "kind": kind, "detail": str(hit.get("error") or "探测失败")[:120]}
    ids: List[str] = []
    body = hit.get("body")
    if isinstance(body, dict):
        for row in (body.get("data") or []):
            if isinstance(row, dict) and row.get("id"):
                ids.append(str(row["id"]))
    if model and ids and not any(i == model or i.endswith("/" + model) for i in ids):
        return {"ok": False, "kind": "empty", "detail": f"/v1/models 无 {model}"}
    return {"ok": True, "kind": "", "detail": ""}


def _probe_deepseek_cloud(config: Dict[str, Any], api_key: str) -> Dict[str, Any]:
    try:
        from src.utils.cloud_credentials import (
            classify_balance, credentials_config, probe_deepseek_balance,
        )
        cc = credentials_config(config)
        url = "https://api.deepseek.com/user/balance"
        probe = probe_deepseek_balance(url, api_key, timeout_sec=8.0)
        if int(probe.get("http_status") or 0) == 402:
            return {"ok": False, "kind": "quota", "detail": "HTTP 402"}
        if probe.get("available") is False:
            return {"ok": False, "kind": "quota", "detail": "账户不可用"}
        summary = classify_balance(probe, float(cc.get("balance_warn_cny") or 20))
        status = str(summary.get("status") or "")
        bal = summary.get("balance")
        try:
            bal_f = float(bal) if bal is not None else None
        except (TypeError, ValueError):
            bal_f = None
        if bal_f is not None and bal_f <= 0:
            return {"ok": False, "kind": "quota", "detail": "余额为 0"}
        if status == "ok":
            return {"ok": True, "kind": "", "detail": ""}
        if status == "low":
            return {"ok": False, "kind": "low",
                    "detail": f"余额 {bal} {summary.get('currency') or 'CNY'}"}
        if status == "auth_failed":
            return {"ok": False, "kind": "auth", "detail": str(summary.get("error") or "鉴权失败")}
        if status == "unreachable":
            return {"ok": False, "kind": "connect", "detail": str(summary.get("error") or "不可达")}
        return {"ok": False, "kind": "other", "detail": status or "余额未知"}
    except Exception as e:
        return {"ok": False, "kind": "other", "detail": type(e).__name__}


def probe_lane(lane: str, config: Dict[str, Any]) -> Dict[str, Any]:
    """无 token 探活一档。返回 ``{ok, kind, detail}``，不含密钥。"""
    eps = _endpoints(config)
    ep = eps.get(str(lane)) or {}
    base = str(ep.get("base_url") or "")
    key = str(ep.get("api_key") or "")
    model = str(ep.get("model") or "")
    if lane == "local":
        if not ep.get("enabled"):
            return {"ok": False, "kind": "other", "detail": "ai.fallback 未启用"}
        return _models_ok(base, key, model)
    if not key:
        return {"ok": False, "kind": "auth", "detail": "未配置 api_key"}
    if "deepseek" in base.lower():
        hit = _probe_deepseek_cloud(config, key)
        # 余额接口通了才信；若探不到余额，再试 /models（避免把网络抖动当成欠费长跳过）
        if hit.get("kind") == "connect":
            models = _models_ok(base, key, "")
            if models.get("ok"):
                return models
        return hit
    return _models_ok(base, key, "")


def apply_probe(lane: str, result: Dict[str, Any], *, now: Optional[float] = None) -> Dict[str, Any]:
    if result.get("ok"):
        note_ok(lane, now=now)
    else:
        kind = str(result.get("kind") or "other")
        # 本地探活偶发抖动不该让客服句句跳过 173；欠费/鉴权才登记跳过。
        skip = kind in ("quota", "auth", "low") or str(lane) != "local"
        note_fail(lane, kind, str(result.get("detail") or ""), now=now, skip=skip)
    snap = snapshot(now=now).get(lane) or _blank(lane)
    snap = dict(snap)
    snap["probe"] = {k: result.get(k) for k in ("ok", "kind", "detail")}
    return snap


def healthy_labels(*, now: Optional[float] = None) -> List[str]:
    ts = float(now if now is not None else time.time())
    out = []
    for lane, row in snapshot(now=ts).items():
        if row.get("ok") and not row.get("skipped"):
            out.append(LANE_LABELS.get(lane, lane))
    return out


__all__ = [
    "LANES", "LANE_LABELS", "NAG_INTERVAL_SEC", "REMIND_KEY_PREFIX",
    "SKIP_QUOTA_SEC", "SKIP_FAIL_SEC", "SKIP_CONNECT_STREAK_SEC",
    "reset_lanes", "snapshot", "should_skip", "note_ok", "note_fail",
    "skip_hold_sec", "failover_order", "remind_enabled", "nag_interval_sec",
    "remind_key", "kind_zh", "probe_lane", "apply_probe", "healthy_labels",
]
