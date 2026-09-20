# -*- coding: utf-8 -*-
"""桥接驱动进程（个人微信 PC 副驾等 ``mode=desktop`` 账号的外部驱动）存活心跳——纯函数。

- :func:`heartbeat_meta`：请求体 → 写进 registry ``meta.bridge_heartbeat`` 的精简字典（只留展示要用的字段，
  不把 stats 全量塞进注册表）。
- :func:`bridge_presence`：registry meta → accounts_summary 行上的 ``bridge`` 对象
  ``{kind, tier, readonly, alive, age_sec, ts}``；``alive`` = 心跳距今 ≤ :data:`ALIVE_WITHIN_SEC`。
  驱动每 3–4 秒一轮，一次资料卡核对约 4–10 秒，90 秒足够容忍长轮次又能及时发现进程挂掉。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

ALIVE_WITHIN_SEC = 90.0
TIERS = ("copilot", "semi", "auto_reply")
_STAT_KEYS = ("ticks", "inbound", "sent", "denied", "send_failed", "unknown_direction",
              "frozen_until", "last_disposition", "offline", "last_readable",
              "voice_ready", "voice_sent", "voice_failed",
              # P2（2026-09-19）：麦克风被坐席占用（开会/通话）/ 今日语音配额用量 / 比默认宽松的配额项
              "voice_mic_busy", "voice_mic_busy_by", "voice_today", "voice_daily_cap", "caps_relaxed")


def heartbeat_meta(body: Dict[str, Any], now: Optional[float] = None) -> Dict[str, Any]:
    tier = str(body.get("tier") or "copilot").strip().lower()
    if tier not in TIERS:
        tier = "copilot"
    stats_in = body.get("stats") if isinstance(body.get("stats"), dict) else {}
    stats: Dict[str, Any] = {}
    for k in _STAT_KEYS:
        if k in stats_in:
            v = stats_in[k]
            stats[k] = v if isinstance(v, (int, float, bool, str)) else str(v)
    return {
        "ts": float(now if now is not None else time.time()),
        "kind": str(body.get("bridge") or "desktop")[:24],
        "tier": tier,
        "readonly": bool(body.get("readonly", False)),
        "stats": stats,
    }


def bridge_presence(meta: Optional[Dict[str, Any]], now: Optional[float] = None,
                    alive_within_sec: float = ALIVE_WITHIN_SEC) -> Optional[Dict[str, Any]]:
    """``meta.bridge_heartbeat`` → 展示用 presence；没有心跳记录 → None（普通桌面壳镜像账号）。"""
    hb = (meta or {}).get("bridge_heartbeat") if isinstance(meta, dict) else None
    if not isinstance(hb, dict):
        return None
    try:
        ts = float(hb.get("ts") or 0.0)
    except Exception:
        ts = 0.0
    if ts <= 0:
        return None
    t = float(now if now is not None else time.time())
    age = max(0.0, t - ts)
    stats = hb.get("stats") if isinstance(hb.get("stats"), dict) else {}
    # 进程活着 ≠ 能干活：微信最小化到托盘 / 退出登录时驱动仍在心跳，但读不到屏——单独给一位
    readable = stats.get("last_readable")
    voice_ready = stats.get("voice_ready")
    mic_busy = stats.get("voice_mic_busy")
    return {
        "kind": str(hb.get("kind") or "desktop"),
        "tier": str(hb.get("tier") or "copilot"),
        "readonly": bool(hb.get("readonly", False)),
        "alive": age <= float(alive_within_sec),
        "age_sec": int(age),
        "ts": ts,
        "readable": (bool(readable) if isinstance(readable, bool) else None),
        # 这台机能不能发语音（「发语音」锚点 + 虚拟声卡通路）；老驱动没这项 → None
        "voice_ready": (bool(voice_ready) if isinstance(voice_ready, bool) else None),
        # 坐席此刻正用麦（开会/通话）：能力在、但这一刻不该排语音；老驱动没这项 → False
        "voice_mic_busy": bool(mic_busy) if isinstance(mic_busy, bool) else False,
        "voice_mic_busy_by": str(stats.get("voice_mic_busy_by") or ""),
        "stats": stats,
    }


def bridge_voice_ready(meta: Optional[Dict[str, Any]], now: Optional[float] = None,
                       alive_within_sec: float = ALIVE_WITHIN_SEC) -> bool:
    """后端排语音前问一句：该桌面账号的驱动**活着、报了 voice_ready、且坐席此刻没在用麦**。
    没心跳/过期/老驱动一律 False（改发文字）；麦克风被占用也 False（省一次白合成，驱动侧同样会拒）。"""
    pres = bridge_presence(meta, now=now, alive_within_sec=alive_within_sec)
    return bool(pres and pres["alive"] and pres.get("voice_ready") is True and not pres.get("voice_mic_busy"))


#: 心跳过期多久算「真实掉线」（fail）；之前算 warn（可能在重启/长轮次）
FAIL_AFTER_SEC = 600.0
_TIER_LABEL = {"copilot": "只读建议", "semi": "半自动", "auto_reply": "全自动"}


def bridge_driver_problems(rows: List[Dict[str, Any]], now: Optional[float] = None,
                           *, fail_after_sec: float = FAIL_AFTER_SEC,
                           alive_within_sec: float = ALIVE_WITHIN_SEC) -> List[Dict[str, Any]]:
    """注册表行 → 心跳过期的桥接驱动「问题项」（与 ``ops_overview.orchestrator_worker_problems`` 同形，
    供 HealthWatchdog 并入 ``orchestrator_worker_alert``：铃铛/SSE/webhook 三面复用、事件表落账、恢复补发）。

    只看 ``mode=desktop`` 且有过心跳、状态不是 offline/removed 的账号（运营登出/移除的不报）；
    从未心跳的普通桌面壳镜像账号不在此列。纯函数。
    """
    t = float(now if now is not None else time.time())
    out: List[Dict[str, Any]] = []
    for a in rows or []:
        if not isinstance(a, dict) or str(a.get("mode") or "") != "desktop":
            continue
        if str(a.get("status") or "") in ("offline", "removed"):
            continue
        pres = bridge_presence(a.get("meta") if isinstance(a.get("meta"), dict) else None, now=t,
                               alive_within_sec=alive_within_sec)
        if not pres or pres["alive"]:
            continue
        platform = str(a.get("platform") or "?")
        account_id = str(a.get("account_id") or "?")
        label = str(a.get("label") or "").strip() or account_id
        age = int(pres["age_sec"])
        tier_key = str(pres.get("tier") or "")
        # 半自动/全自动档的副驾**本来就该在收发**，断了就是业务中断 → 直接 fail（只配红灯 webhook 的部署也能立刻收到）；
        # 只读建议档断了坐席只是少了建议 → 先 warn，超过 fail_after_sec 再升级
        expected_sending = tier_key in ("semi", "auto_reply")
        severity = "fail" if (expected_sending or age >= float(fail_after_sec)) else "warn"
        tier = _TIER_LABEL.get(tier_key, tier_key)
        out.append({
            "id": f"{platform}:{account_id}",
            "name": f"{label}（{platform}/{account_id}）",
            "status": severity,
            "platform": platform,
            "account_id": account_id,
            "restarts": 0,
            "kind": "bridge_offline",
            "age_sec": age,
            "detail": (f"副驾驱动 {max(1, age // 60)} 分钟无心跳（{tier}档）：新消息不会进来、回复不会发出，"
                       f"请到「账号管理 → 个人微信 · PC 副驾 → 接入流程」第 ③ 步点「启动副驾」"),
        })
    return out


__all__ = ["ALIVE_WITHIN_SEC", "FAIL_AFTER_SEC", "TIERS", "heartbeat_meta", "bridge_presence",
           "bridge_voice_ready", "bridge_driver_problems"]
