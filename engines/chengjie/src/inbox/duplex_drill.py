# -*- coding: utf-8 -*-
"""舰队互聊演练模式（duplex_drill，P1 2026-08-07）。

背景（.104/.198「双向全自动只有一边回」排障 §98 续）：两个自家账号互发消息
验证「双向全自动」是产品的**第一演示场景**（销售 PoC / 内测验收都这么做），
但系统的安全体系在设计上就会把 AI↔AI 互聊单边掐停——peer_bot_guard 把对面
判成机器人（复读/秒回/疑似）、冷启动预热把新号压进人审、日预算 40 轮触顶。
每一层单独都对，叠加起来「演示必翻车」。

本模块提供**受控放行**：显式白名单账号对在演练窗内豁免上述闸门，但换上
演练自己的硬顶（单会话日轮次 ``max_rounds``，靠既有 peer_reply_ledger 台账
计数）——到点自动停（封顶 review：AI 继续拟稿人审、不再自动发），跨日自动
复位。即「能演示、刹得住、不用改守卫配置」。

语义边界（改动前先读）：

- **默认关**（新子系统纪律）；开了也只对 ``pairs`` 白名单里的账号对生效，
  其他会话零行为变化。
- 豁免面 = peer_bot_guard 判定（Tier0/复读/秒回/预算）+ 档位封顶（预热/
  业务线/平台）；**不豁免**坐席显式档位（下拉切 manual 仍是 manual——人的
  明示决定永远最高）与工作班表（演练该在班上做）。
- 硬顶**不可豁免**：max_rounds<=0 视为配置错误按默认 20 处理，绝不允许
  「无限互聊」形态存在（那正是守卫要防的 token 无底洞）。
- 计数复用 ``peer_reply_ledger``（A 线获准直回 +1 / B 线将自动投递拟稿 +1，
  日界自动清零）——演练轮次与预算轮次同一口径，观测面（收件箱预算横幅 /
  why_no_reply）零改动可见。

配置（``inbox.duplex_drill``）::

    duplex_drill:
      enabled: false          # 总开关（默认关）
      pairs:                  # 白名单账号对（无序匹配；telegram 数字 id 字符串）
        - ["8041810715", "8755679833"]
      max_rounds: 20          # 单会话/日 自动回复轮次硬顶（到点封顶 review）
      exempt_guard: true      # 豁免 peer_bot_guard（Tier0/复读/秒回/预算）
      exempt_caps: true       # 豁免档位封顶（预热/业务线/平台）
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

_DEFAULTS: Dict[str, Any] = {
    "enabled": False,
    "pairs": [],
    "max_rounds": 20,
    "exempt_guard": True,
    "exempt_caps": True,
}

# 硬顶的绝对上限：就算运营写 max_rounds: 99999，单会话/日也封在这——演练是
# 「看它聊起来」，不是「让它聊一天」；更大的量属于压测，该去隔离环境做。
_MAX_ROUNDS_HARD_LIMIT = 200


def parse_cfg(root_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """读 ``inbox.duplex_drill``，缺键回默认；坏值按默认（绝不抛）。"""
    node = (((root_config or {}).get("inbox") or {}).get("duplex_drill") or {})
    out = dict(_DEFAULTS)
    if not isinstance(node, dict):
        return out
    out["enabled"] = bool(node.get("enabled", out["enabled"]))
    pairs: List[Tuple[str, str]] = []
    raw_pairs = node.get("pairs")
    if isinstance(raw_pairs, (list, tuple)):
        for item in raw_pairs:
            try:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    a, b = str(item[0]).strip(), str(item[1]).strip()
                    if a and b and a != b:
                        pairs.append((a, b))
            except Exception:
                continue
    out["pairs"] = pairs
    try:
        mr = int(node.get("max_rounds", out["max_rounds"]))
    except (TypeError, ValueError):
        mr = int(out["max_rounds"])
    if mr <= 0:
        mr = int(_DEFAULTS["max_rounds"])
    out["max_rounds"] = min(mr, _MAX_ROUNDS_HARD_LIMIT)
    out["exempt_guard"] = bool(node.get("exempt_guard", out["exempt_guard"]))
    out["exempt_caps"] = bool(node.get("exempt_caps", out["exempt_caps"]))
    return out


def is_drill_pair(cfg: Dict[str, Any], account_id: str, chat_key: str) -> bool:
    """本会话是否在演练白名单（无序对匹配：本方账号 × 对方 chat_key）。"""
    if not cfg.get("enabled"):
        return False
    a = str(account_id or "").strip()
    c = str(chat_key or "").strip()
    if not a or not c:
        return False
    for x, y in cfg.get("pairs") or []:
        if (a == x and c == y) or (a == y and c == x):
            return True
    return False


def guard_exempt(
    root_config: Optional[Dict[str, Any]], *, account_id: str, chat_key: str,
) -> bool:
    """peer_bot_guard 是否应对本会话豁免（演练对 + exempt_guard）。"""
    cfg = parse_cfg(root_config)
    return bool(cfg.get("exempt_guard")) and is_drill_pair(
        cfg, account_id, chat_key)


def rounds_used_today(
    store: Any, conversation_id: str, *, now: Optional[float] = None,
) -> int:
    """今日自动链轮次（peer_reply_ledger 口径；台账不可用 → 0=不拦）。"""
    if store is None or not conversation_id \
            or not hasattr(store, "get_auto_reply_ledger"):
        return 0
    try:
        from src.inbox.peer_bot_guard import today_key
        led = store.get_auto_reply_ledger(conversation_id) or {}
        if str(led.get("day") or "") != today_key(now):
            return 0
        return max(0, int(led.get("auto_replies") or 0))
    except Exception:
        return 0


def next_midnight_ts(now: Optional[float] = None) -> float:
    """本地下一个零点（drill_limit 封顶的自动恢复时刻，前端倒计时用）。"""
    now = time.time() if now is None else float(now)
    lt = time.localtime(now)
    midnight = now - (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec)
    return midnight + 86400.0


__all__ = [
    "parse_cfg",
    "is_drill_pair",
    "guard_exempt",
    "rounds_used_today",
    "next_midnight_ts",
]
