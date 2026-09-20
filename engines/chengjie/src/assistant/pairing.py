# -*- coding: utf-8 -*-
"""小智「手机扫码操控」配对核心（实施58 P4，2026-08-23）。

安全模型（docs/实施58 §5-4；改前先读）：
1. **一次性配对 token**：桌面登录态签发，TTL 120s、单次核销、绑定签发人
   （uid/uname/role 随 token 传递到手机会话——手机就是「这个人」在操作，
   不是新账号）。扫码 URL 里的 token 用完即废，截屏泄露也只有 2 分钟窗口。
2. **手机会话注册表**（进程内）：配对成功登记 msid，写进手机端 session；
   小智接口每次调用校验 `mobile_ok(msid)`（触摸续活，12h 闲置过期）；
   桌面「已连接的手机」列表可随时 `revoke` ——踢下线对小智接口**立即**
   生效。实例重启注册表清空＝所有手机自然掉线（重扫即可，符合直觉）。
3. **审计标记**：手机会话发起的动作在审计行 actor 带 `@mobile` 后缀
   （assistant_action_routes 接线），谁在哪端改的一目了然。

进程内存储与 actions.py 同哲学（有界 + TTL + 惰性 GC + 可 monkeypatch
时钟）。门禁 ``tests/test_assistant_pairing.py``。
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any, Dict, List, Optional

PAIR_TTL_SEC = 120.0
# 相机/微信扫码会先预取 URL 把一次性 token 核销，用户点开时再打一次。
# 短窗回放同一 token（身份不变），否则预取 = 废码、手机页永远过期。
PAIR_REPLAY_SEC = 60.0
MOBILE_IDLE_TTL_SEC = 12 * 3600.0
_MAX_ENTRIES = 50

_time = time.time  # 测试可 monkeypatch

_LOCK = threading.Lock()
_PAIR_TOKENS: Dict[str, Dict[str, Any]] = {}
_PAIR_USED: Dict[str, Dict[str, Any]] = {}
_MOBILE_SESSIONS: Dict[str, Dict[str, Any]] = {}


def _gc() -> None:
    now = _time()
    for k in [k for k, v in _PAIR_TOKENS.items()
              if now - v.get("ts", 0) > PAIR_TTL_SEC]:
        _PAIR_TOKENS.pop(k, None)
    for k in [k for k, v in _PAIR_USED.items()
              if now - v.get("used_ts", 0) > PAIR_REPLAY_SEC]:
        _PAIR_USED.pop(k, None)
    for k in [k for k, v in _MOBILE_SESSIONS.items()
              if now - v.get("last_seen", 0) > MOBILE_IDLE_TTL_SEC]:
        _MOBILE_SESSIONS.pop(k, None)


def _evict_oldest(store: Dict[str, Dict[str, Any]], key: str) -> None:
    if len(store) >= _MAX_ENTRIES:
        oldest = min(store, key=lambda k: store[k].get(key, 0))
        store.pop(oldest, None)


def issue_pair_token(uid: str, uname: str, role: str) -> str:
    token = "xzp-" + secrets.token_urlsafe(18)
    with _LOCK:
        _gc()
        _evict_oldest(_PAIR_TOKENS, "ts")
        _PAIR_TOKENS[token] = {"uid": str(uid), "uname": str(uname),
                               "role": str(role), "ts": _time()}
    return token


def peek_pair_token(token: str) -> bool:
    """token 仍可核销或仍在回放窗内（预取探测用，不取走）。"""
    tok = str(token or "")
    now = _time()
    with _LOCK:
        _gc()
        if tok in _PAIR_TOKENS:
            return True
        used = _PAIR_USED.get(tok)
        return bool(used and now - used.get("used_ts", 0) <= PAIR_REPLAY_SEC)


def consume_pair_token(token: str) -> Optional[Dict[str, Any]]:
    """取走即删（单次核销）；过期=None。

    短窗内同一 token 再来一次（扫码预取 + 用户点开）回放同一身份，
    已登记的 msid 一并带回，避免预取废码、避免双开会话。
    """
    tok = str(token or "")
    now = _time()
    with _LOCK:
        _gc()
        ent = _PAIR_TOKENS.pop(tok, None)
        if ent:
            used = dict(ent)
            used["used_ts"] = now
            _evict_oldest(_PAIR_USED, "used_ts")
            _PAIR_USED[tok] = used
            return dict(used)
        used = _PAIR_USED.get(tok)
        if used and now - used.get("used_ts", 0) <= PAIR_REPLAY_SEC:
            return dict(used)
    return None


def attach_msid(token: str, msid: str) -> None:
    """首登登记后把 msid 钉回回放窗，二次打开复用同一手机会话。"""
    tok = str(token or "")
    with _LOCK:
        if tok in _PAIR_USED:
            _PAIR_USED[tok]["msid"] = str(msid or "")


def register_mobile(uid: str, uname: str, role: str, ua: str = "") -> str:
    msid = "xzm-" + secrets.token_urlsafe(14)
    now = _time()
    with _LOCK:
        _gc()
        _evict_oldest(_MOBILE_SESSIONS, "last_seen")
        _MOBILE_SESSIONS[msid] = {
            "uid": str(uid), "uname": str(uname), "role": str(role),
            "ua": str(ua or "")[:120], "ts": now, "last_seen": now,
        }
    return msid


def mobile_ok(msid: str) -> bool:
    """有效即触摸续活；被踢/过期/重启后=False。"""
    with _LOCK:
        _gc()
        ent = _MOBILE_SESSIONS.get(str(msid or ""))
        if not ent:
            return False
        ent["last_seen"] = _time()
        return True


def list_mobile(uid: Optional[str] = None) -> List[Dict[str, Any]]:
    with _LOCK:
        _gc()
        out = []
        for msid, ent in _MOBILE_SESSIONS.items():
            if uid is not None and ent.get("uid") != str(uid):
                continue
            row = dict(ent)
            row["msid"] = msid
            out.append(row)
    out.sort(key=lambda r: r.get("ts", 0), reverse=True)
    return out


def revoke_mobile(msid: str) -> bool:
    with _LOCK:
        return _MOBILE_SESSIONS.pop(str(msid or ""), None) is not None
