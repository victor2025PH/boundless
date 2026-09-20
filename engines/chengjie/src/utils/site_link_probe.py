# -*- coding: utf-8 -*-
"""官网连通性探针（实施86 域A-2②，#17/#51 沉淀）。

事故背景：托管桌面机的多条链都依赖官网（bd2026.cc）——AI 网关 / 克隆语音中继 /
报障上报 / 更新检查，但此前**没有集中连通性自诊断**：断链时各功能各自静默回落
（克隆回落通用音、上报报错、AI 降级），用户看到的是一堆互不相干的怪现象，
值守拿到的是一堆互不相干的报障单（#41/#42/#26/#51 同根不同单的实录）。

本模块＝单一探针 + 进程缓存，经 ``/api/workspace/ai-runtime-status.site_link``
随既有 60s 轮询捎带（零新增轮询），工作台右下胶囊给一句人话。

判定语义（宁稳勿噪）：
- 只在**托管态**探（自建部署不依赖官网，恒 None=前端隐藏）；
- ``reachable`` 语义＝网络路径通：任何 HTTP 响应（含 4xx/5xx）都算通——
  应用层坏是另一类问题（有各自的告警面），别混进「连不上」；
- 连续 ≥2 次探测失败才报不可达（单次抖动不闪横幅）；恢复即刻转绿。
"""
from __future__ import annotations

import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: 探测间隔下限（秒）——ai-runtime-status 是 60s 轮询，探针自己再限一层，
#: 多标签页/多坐席同时轮询也不会放大到官网。
PROBE_TTL_SEC = 120
PROBE_TIMEOUT_SEC = 6
#: 连续失败达到该次数才对外报「不可达」（防单次抖动闪横幅）
FAIL_STREAK_THRESHOLD = 2

_lock = threading.Lock()
_state: Dict[str, Any] = {
    "ts": 0.0,           # 上次真探时刻
    "fail_streak": 0,
    "down_since": 0.0,   # 首次连续失败起点（报时长用）
    "reachable": True,
}


def probe_site_once(site: str, *, timeout: float = PROBE_TIMEOUT_SEC) -> bool:
    """单次探测：GET ``{site}/api/ai/hub/health``（轻量、无鉴权、恒 200）。

    HTTPError（4xx/5xx）＝连上了；URLError/超时＝没连上。绝不抛。
    """
    url = f"{str(site or '').rstrip('/')}/api/ai/hub/health"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout):
            return True
    except urllib.error.HTTPError:
        return True          # 有响应＝网络路径通
    except Exception:
        return False


def site_link_snapshot(config_manager, *, probe=None,
                       now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """当前官网连通性快照；非托管态返回 None（前端据此整块隐藏）。

    返回 ``{"reachable": bool, "site": str, "down_min": int}``。
    同步函数（调用方 to_thread）；探测结果进程级缓存 ``PROBE_TTL_SEC``。
    """
    cfg = (getattr(config_manager, "config", None)
           if config_manager is not None else None) or {}
    try:
        from src.ai.hosted_gateway import _site_url, _wants_hosted
        if not _wants_hosted(cfg):
            return None
        site = _site_url(cfg)
    except Exception:
        return None
    if not site:
        return None

    t = float(now if now is not None else time.time())
    do_probe = False
    with _lock:
        if t - float(_state["ts"]) >= PROBE_TTL_SEC:
            _state["ts"] = t
            do_probe = True
    if do_probe:
        ok = (probe or probe_site_once)(site)
        with _lock:
            if ok:
                _state["fail_streak"] = 0
                _state["down_since"] = 0.0
                _state["reachable"] = True
            else:
                _state["fail_streak"] = int(_state["fail_streak"]) + 1
                if _state["fail_streak"] == 1:
                    _state["down_since"] = t
                if _state["fail_streak"] >= FAIL_STREAK_THRESHOLD:
                    if _state["reachable"]:
                        logger.warning(
                            "[site-link] 官网连续 %d 次探测不可达（%s）",
                            _state["fail_streak"], site)
                    _state["reachable"] = False
    with _lock:
        reachable = bool(_state["reachable"])
        down_since = float(_state["down_since"]) if not reachable else 0.0
    down_min = int(max(0.0, t - down_since) // 60) if down_since else 0
    return {"reachable": reachable, "site": site, "down_min": down_min}


def _reset_for_tests() -> None:
    with _lock:
        _state.update({"ts": 0.0, "fail_streak": 0,
                       "down_since": 0.0, "reachable": True})
