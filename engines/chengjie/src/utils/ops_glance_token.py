# -*- coding: utf-8 -*-
"""告警卡片里的「一眼看」链接令牌（Q-14 #262 E，2026-09-10）。

背景：Telegram 告警里的 ``/workspace?conv=…`` 链接在手机上点开先撞登录页，值班的人
看一眼「这条告警说的是哪个会话、AI 现在什么档」都要先登录一次。本模块给
``webhook_notifier`` 铸一个 **十分钟、一次性、只读** 的令牌，``/ops/glance`` 路由验它。

令牌形状 ``base64url(exp:nonce:sig)``，``sig = HMAC-SHA256(secret, f"{to}|{exp}|{nonce}")[:20]``：
- ``to`` 是相对路径（``/workspace?conv=xxx``），绑进签名——改路径即 bad_sig；
- ``exp`` 秒级 epoch，TTL 默认 600s；
- ``nonce`` 16 字节随机；验过一次即进 ``_used``（进程内存，重启即空——重启后旧令牌
  仍受 exp 约束，最坏多看一次十分钟内的只读页，不值得为此落盘）。

secret 来自 ``web_admin.secret_key``（``admin.py`` 建 app 时 ``configure()`` 注入）；
未注入则 ``mint()`` 返回 ``None``，notifier 回落到 ``links=login`` 的普通绝对链接——
链接永远可点，只是要登录。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from typing import Dict, Optional, Tuple

DEFAULT_TTL_SEC = 600
_SIG_BYTES = 20

_lock = threading.Lock()
_secret: str = ""
_used: Dict[str, float] = {}   # nonce -> exp（过期即可清）
_MAX_USED = 5000


def configure(secret: str) -> None:
    """注入签名密钥；默认占位符 ``change-me-in-production`` 视为未配置（不给可伪造的令牌）。"""
    global _secret
    s = str(secret or "")
    if not s or s == "change-me-in-production":
        s = ""
    with _lock:
        _secret = s


def is_configured() -> bool:
    return bool(_secret)


def _sig(to: str, exp: int, nonce: str) -> bytes:
    msg = f"{to}|{exp}|{nonce}".encode("utf-8")
    return hmac.new(_secret.encode("utf-8"), msg, hashlib.sha256).digest()[:_SIG_BYTES]


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def mint(to: str, *, ttl_sec: int = DEFAULT_TTL_SEC, now: Optional[float] = None) -> Optional[str]:
    """给相对路径 ``to`` 铸令牌；未配置 secret 或 ``to`` 不是站内相对路径 → ``None``。"""
    if not _secret:
        return None
    to = str(to or "")
    if not to.startswith("/") or to.startswith("//"):
        return None
    exp = int((now if now is not None else time.time()) + max(30, int(ttl_sec)))
    nonce = _b64e(secrets.token_bytes(16))
    raw = f"{exp}:{nonce}:".encode("ascii") + _sig(to, exp, nonce)
    return _b64e(raw)


def verify(token: str, to: str, *, now: Optional[float] = None) -> Tuple[bool, str]:
    """验令牌并**消耗**它。返回 ``(ok, reason)``，reason ∈ ``ok|expired|bad_sig|used|unconfigured``。

    顺序：先验签再看过期——伪造令牌永远报 bad_sig，不泄漏「这个 nonce 存在过」。
    """
    if not _secret:
        return False, "unconfigured"
    to = str(to or "")
    try:
        raw = _b64d(str(token or ""))
        head, nonce, sig = raw.split(b":", 2)
        exp = int(head.decode("ascii"))
        nonce_s = nonce.decode("ascii")
    except Exception:
        return False, "bad_sig"
    if len(sig) != _SIG_BYTES or not hmac.compare_digest(sig, _sig(to, exp, nonce_s)):
        return False, "bad_sig"
    t = now if now is not None else time.time()
    if t >= exp:
        return False, "expired"
    with _lock:
        _prune(t)
        if nonce_s in _used:
            return False, "used"
        _used[nonce_s] = float(exp)
    return True, "ok"


def _prune(now: float) -> None:
    if len(_used) < _MAX_USED and (len(_used) % 64):
        return
    for k in [k for k, e in _used.items() if e <= now]:
        _used.pop(k, None)
    if len(_used) >= _MAX_USED:
        # 极端情况（十分钟内五千次点开）按最早过期淘汰，宁可让一个旧令牌多用一次也不无限长
        for k in sorted(_used, key=_used.get)[: len(_used) - _MAX_USED // 2]:
            _used.pop(k, None)


def glance_url(base_url: str, to: str, *, ttl_sec: int = DEFAULT_TTL_SEC) -> Optional[str]:
    """拼 ``https://<base>/ops/glance?t=<token>&to=<quoted path>``；铸不出令牌 → ``None``。"""
    from urllib.parse import quote
    tok = mint(to, ttl_sec=ttl_sec)
    if not tok:
        return None
    return f"{str(base_url or '').rstrip('/')}/ops/glance?t={tok}&to={quote(to, safe='')}"


def _reset_for_tests() -> None:
    global _secret
    with _lock:
        _secret = ""
        _used.clear()


__all__ = ["configure", "is_configured", "mint", "verify", "glance_url", "DEFAULT_TTL_SEC"]
