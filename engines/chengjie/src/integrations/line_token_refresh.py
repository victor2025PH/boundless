# -*- coding: utf-8 -*-
"""LINE 协议号 access token 续期（2026-09-06，zhiliao 两个 LINE 号「7 天必掉线」根因修复）。

事故机制（本机 zhiliao 实例 vic001 / yunshan zan 两号取证）
------------------------------------------------------------
LINE 副设备（Chrome 扩展形态，``cmode=SECONDARY``）签发的 access token 是 JWT，
``exp - iat`` **恒等于 7 天**；refresh token 有效期 365 天、每次续期**轮换**。
vic001 08-29 06:56:05 登录 → 09-05 06:56:02 被标 ``worker:expired``（差 3 秒＝7 天整）。

续期本该由 okline 完成，但 ``okline.auth.refresh_access_token`` 对
``/api/auth/tokenRefresh`` 的请求 **``require_auth=False`` 不带 ``X-Line-Access`` 头**，
网关一律回 HTTP 401 ``REQUEST_NEED_LOGIN``(10004)。本机 09-06 实测：同一 refresh token、
同一请求体，**带上（已过期的）access token 头即成功**签发新 token。也就是说这套部署里
续期从来没成功过一次——两个号的 session 文件自登录起一个字节没改过。

下游把「续期失败」解释成「refresh token 也死了 → 必须重新扫码」：``line_pull_sync``
→ ``relogin_required`` → ``_report_relogin_required`` → 健康表 ``expired`` → 注册表翻
``offline``（``offline_reason=worker:expired``）→ 编排器不再拉起 → 界面「未接入」。
而 SSE 流拿着过期 token 照样 200 + ping（yunshan zan 过期后聋了 4 周才被兜底探出）。

本模块收口三件事（okline 是第三方包，不改它）：
1. ``refresh_line_tokens``：**带 X-Line-Access 头**的续期请求；成功后就地写回 session
   文件——只改 ``accessToken/refreshToken`` 两个字段。**绝不用 ``client.save_tokens()``**：
   它会重导出 E2EE 密钥，而从文件装回的密钥 wasm 拒绝再导出（``illegal operation``），
   结果是把 ``e2ee.keys`` 写成空——09-06 实测踩坑并手工恢复。
   同一 session 文件的多个 client（worker 主连接 / 拉取兜底 / 存量同步各建一个实例）
   共用一把进程锁，且**先看文件**：别人已续期过就直接采用文件里的新 token，不重复
   打网关——refresh token 会轮换，两个实例各自续期会互相作废。
2. ``ensure_fresh_token``：按 JWT ``exp`` **主动**续期（剩余 < 48h 即续；过期也照续，
   实测过期 1 天仍可续），供 worker 启动时与保活线程周期调用。
3. ``revive_expired_line_accounts``：对注册表里已被标 ``worker:expired`` 的 LINE 号
   （本次事故的存量伤员）用其 session 文件试续期，成功即上报 ``authorized`` → 注册表
   回 ``online`` → 编排器下一轮自动拉起。真死（网关明说 REQUEST_NEED_LOGIN）的号按
   指数退避低频重试，仍需人重新扫码。

纯函数（JWT 解码 / 到期判定）与 IO 分离，门禁 ``tests/test_line_token_refresh.py``。
"""
from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "REFRESH_LEAD_SEC",
    "ATTEMPT_COOLDOWN_SEC",
    "decode_jwt_claims",
    "access_token_expiry",
    "token_expired",
    "refresh_due",
    "is_token_stale_error",
    "is_relogin_error",
    "read_session_file",
    "write_session_tokens",
    "refresh_line_tokens",
    "ensure_fresh_token",
    "install_refresh_hook",
    "revive_expired_line_accounts",
]

#: 网关的续期端点（okline ``SPECIAL_ENDPOINTS["auth.tokenRefresh"]`` 同值，硬编码以免
#: 版本漂移时 import 失败把续期整条打死）
_REFRESH_PATH = "/api/auth/tokenRefresh"

#: 主动续期提前量：token 活 7 天，剩 48h 内即续——给网络故障留两天重试窗，
#: 又不至于把 7 天的 token 每天白刷一遍。
REFRESH_LEAD_SEC = 48 * 3600.0
#: 同一 client 两次**上网**续期尝试的最小间隔（防对着故障网关每分钟打一次）
ATTEMPT_COOLDOWN_SEC = 600.0

#: 复活扫描的退避：首败 30min，之后翻倍，封顶 12h（真死的号不该每小时刷一行 WARNING）
_REVIVE_BASE_SEC = 1800.0
_REVIVE_CAP_SEC = 12 * 3600.0
#: account_id → {"ts": 上次尝试时刻, "fails": 连续失败次数}
_REVIVE_LEDGER: Dict[str, Dict[str, float]] = {}

_FILE_LOCKS: Dict[str, threading.Lock] = {}
_FILE_LOCKS_GUARD = threading.Lock()


# ── 纯函数 ──────────────────────────────────────────────────────────────────

def decode_jwt_claims(token: Any) -> Dict[str, Any]:
    """解 JWT payload（不验签——只为读 ``exp``）；不是 JWT / 解不开 → ``{}``。"""
    try:
        parts = str(token or "").split(".")
        if len(parts) != 3:
            return {}
        seg = parts[1]
        seg += "=" * (-len(seg) % 4)
        data = json.loads(base64.urlsafe_b64decode(seg.encode("ascii")).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def access_token_expiry(token: Any) -> float:
    """access token 的 ``exp``（epoch 秒）；读不到 → 0.0（调用方按「未知」处理）。"""
    try:
        return float(decode_jwt_claims(token).get("exp") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def token_expired(token: Any, *, now: Optional[float] = None) -> bool:
    """``exp`` 已过 → True；``exp`` 未知 → False（不武断判死）。"""
    exp = access_token_expiry(token)
    if exp <= 0:
        return False
    return (time.time() if now is None else float(now)) >= exp


def refresh_due(token: Any, *, now: Optional[float] = None,
                lead_sec: float = REFRESH_LEAD_SEC) -> bool:
    """是否该主动续期：``exp`` 已知且剩余不足 ``lead_sec``（含已过期）。"""
    exp = access_token_expiry(token)
    if exp <= 0:
        return False
    ts = time.time() if now is None else float(now)
    return (exp - ts) < float(lead_sec)


def is_token_stale_error(exc: BaseException) -> bool:
    """RPC 报「access token 需刷新」（LINE Thrift code=119，非 HTTP 401）。"""
    if getattr(exc, "code", None) == 119:
        return True
    msg = str(exc).lower()
    return "token refresh required" in msg or "access token refresh" in msg


def is_relogin_error(exc: BaseException) -> bool:
    """refresh token 也失效＝只能重新扫码（LINE code=10004 REQUEST_NEED_LOGIN）。"""
    if getattr(exc, "code", None) == 10004:
        return True
    return "request_need_login" in str(exc).lower()


# ── session 文件 ────────────────────────────────────────────────────────────

def read_session_file(path: str) -> Dict[str, Any]:
    """读 okline session JSON；缺文件/坏文件 → ``{}``。"""
    p = str(path or "").strip()
    if not p:
        return {}
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def write_session_tokens(path: str, access_token: str, refresh_token: Optional[str]) -> bool:
    """只改 session 文件的 ``accessToken/refreshToken``，其余字段（certificate/e2ee/mid）原样保留。

    原子写（tmp + replace）。文件不存在时不凭空创建：没有 e2ee/certificate 的半个
    session 比没有更糟（会让 worker 以为能用）。
    """
    p = str(path or "").strip()
    if not p or not os.path.isfile(p):
        return False
    try:
        with open(p, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return False
        data["accessToken"] = access_token
        if refresh_token:
            data["refreshToken"] = refresh_token
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
        return True
    except Exception:  # noqa: BLE001
        logger.warning("[line-token] session 文件回写失败 path=%s", p, exc_info=True)
        return False


def _lock_for(key: str) -> threading.Lock:
    with _FILE_LOCKS_GUARD:
        lock = _FILE_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _FILE_LOCKS[key] = lock
        return lock


def _session_path_of(client: Any, tokens_file: Optional[str]) -> str:
    if tokens_file:
        return str(tokens_file)
    # okline ``from_tokens_file`` 记下的路径（私有属性，缺失时按「无文件」处理）
    return str(getattr(client, "_session_path", "") or "")


def _err_brief(exc: BaseException) -> str:
    bits = [type(exc).__name__, str(exc)[:160]]
    st = getattr(exc, "status", None)
    code = getattr(exc, "code", None)
    if st is not None or code is not None:
        bits.append(f"status={st} code={code}")
    return " ".join(b for b in bits if b)


# ── 续期 ─────────────────────────────────────────────────────────────────────

def refresh_line_tokens(client: Any, tokens_file: Optional[str] = None, *,
                        reason: str = "", now: Optional[float] = None) -> Dict[str, Any]:
    """给一个 okline client 续期 access token（带 ``X-Line-Access`` 头），并回写 session 文件。

    返回 ``{"ok", "source", "exp", "rotated", "relogin", "error"}``：
    - ``source="file"``：别的实例已续过，直接采用文件里的新 token（零网络）；
    - ``source="network"``：本次真打了网关；
    - ``relogin=True``：网关明说 ``REQUEST_NEED_LOGIN`` / 没有 refresh token——只能重新扫码。
    绝不抛。
    """
    transport = getattr(client, "transport", None)
    tokens = getattr(transport, "tokens", None)
    if transport is None or tokens is None:
        return {"ok": False, "source": "", "exp": 0.0, "rotated": False,
                "relogin": False, "error": "client has no transport/tokens"}
    path = _session_path_of(client, tokens_file)
    with _lock_for(path or f"client:{id(client)}"):
        # 1) 文件先行：同一 session 的其它实例可能刚续过（refresh token 会轮换，
        #    拿旧 refresh token 再去续只会被拒 → 白白把号判死）
        if path:
            disk = read_session_file(path)
            disk_access = str(disk.get("accessToken") or "")
            if (disk_access and disk_access != str(tokens.access_token or "")
                    and not token_expired(disk_access, now=now)):
                tokens.access_token = disk_access
                if disk.get("refreshToken"):
                    tokens.refresh_token = disk["refreshToken"]
                logger.info("[line-token] 采用 session 文件里已续期的 token（%s）exp=%s",
                            reason or "-", _fmt_ts(access_token_expiry(disk_access)))
                return {"ok": True, "source": "file", "exp": access_token_expiry(disk_access),
                        "rotated": False, "relogin": False, "error": ""}
        rt = str(tokens.refresh_token or "")
        if not rt:
            return {"ok": False, "source": "", "exp": 0.0, "rotated": False,
                    "relogin": True, "error": "no refresh token"}
        # 2) 上网续期。摘掉 okline 的 401 钩子：它的续期路径本身会 401，装着会递归。
        hook = getattr(transport, "_refresh_hook", None)
        try:
            transport._refresh_hook = None
            # ⚠ require_auth=True 就是整个修复：网关要靠 X-Line-Access（哪怕已过期）
            # 认出这条会话；okline 原版不带此头 → 一律 REQUEST_NEED_LOGIN。
            data = transport.post_json(_REFRESH_PATH, {"refreshToken": rt},
                                       require_auth=True, allow_refresh=False)
        except Exception as exc:  # noqa: BLE001
            relogin = is_relogin_error(exc)
            logger.warning("[line-token] tokenRefresh 失败（%s）%s%s", reason or "-",
                           _err_brief(exc), " → refresh token 已失效，需重新扫码" if relogin else "")
            return {"ok": False, "source": "network", "exp": 0.0, "rotated": False,
                    "relogin": relogin, "error": _err_brief(exc)}
        finally:
            transport._refresh_hook = hook
        tok = data.get("tokenV3IssueResult", data) if isinstance(data, dict) else {}
        if not isinstance(tok, dict):
            tok = {}
        access = str(tok.get("accessToken") or (data.get("accessToken") if isinstance(data, dict) else "") or "")
        if not access:
            return {"ok": False, "source": "network", "exp": 0.0, "rotated": False,
                    "relogin": False, "error": "tokenRefresh returned no accessToken"}
        new_rt = str(tok.get("refreshToken") or "")
        tokens.access_token = access
        if new_rt:
            tokens.refresh_token = new_rt
        exp = access_token_expiry(access)
        saved = write_session_tokens(path, access, new_rt or rt) if path else False
        logger.info("[line-token] access token 已续期（%s）exp=%s rotated=%s saved=%s",
                    reason or "-", _fmt_ts(exp), bool(new_rt and new_rt != rt), saved)
        return {"ok": True, "source": "network", "exp": exp,
                "rotated": bool(new_rt and new_rt != rt), "relogin": False, "error": ""}


def ensure_fresh_token(client: Any, tokens_file: Optional[str] = None, *,
                       reason: str = "", now: Optional[float] = None,
                       lead_sec: float = REFRESH_LEAD_SEC,
                       cooldown_sec: float = ATTEMPT_COOLDOWN_SEC) -> Dict[str, Any]:
    """按 JWT ``exp`` 判断是否该续期，该续则续（带冷却）。绝不抛。

    返回 ``{"due", "refreshed", "skipped", "relogin", "exp", ...}``；``due=False`` 表示
    token 还新鲜（或 exp 未知）——什么都没做。
    """
    tokens = getattr(getattr(client, "transport", None), "tokens", None)
    if tokens is None:
        return {"due": False, "refreshed": False, "skipped": False, "relogin": False, "exp": 0.0}
    ts = time.time() if now is None else float(now)
    exp = access_token_expiry(tokens.access_token)
    if not refresh_due(tokens.access_token, now=ts, lead_sec=lead_sec):
        return {"due": False, "refreshed": False, "skipped": False, "relogin": False, "exp": exp}
    last = float(getattr(client, "_ltr_attempt_ts", 0.0) or 0.0)
    if last and (ts - last) < float(cooldown_sec):
        return {"due": True, "refreshed": False, "skipped": True, "relogin": False, "exp": exp}
    try:
        client._ltr_attempt_ts = ts  # noqa: SLF001 —— 按 client 记冷却戳（duck-typed）
    except Exception:  # noqa: BLE001
        pass
    res = refresh_line_tokens(client, tokens_file, reason=reason or "due", now=ts)
    out = dict(res)
    out.update({"due": True, "refreshed": bool(res.get("ok")), "skipped": False})
    if not res.get("ok"):
        out["exp"] = exp
    return out


def install_refresh_hook(client: Any, tokens_file: Optional[str] = None) -> bool:
    """把 okline 的 401 自动刷新钩子换成本模块的（带头、写文件不碰 e2ee）。"""
    transport = getattr(client, "transport", None)
    if transport is None:
        return False

    def _hook() -> bool:
        return bool(refresh_line_tokens(client, tokens_file, reason="http401").get("ok"))

    try:
        transport._refresh_hook = _hook
        return True
    except Exception:  # noqa: BLE001
        return False


# ── 存量伤员复活 ─────────────────────────────────────────────────────────────

def _revive_wait_sec(fails: int) -> float:
    if fails <= 0:
        return 0.0
    return float(min(_REVIVE_CAP_SEC, _REVIVE_BASE_SEC * (2 ** (fails - 1))))


def revive_expired_line_accounts(registry: Any, config: Optional[Dict[str, Any]] = None, *,
                                 now: Optional[float] = None,
                                 client_factory: Any = None,
                                 report: Any = None) -> Dict[str, Any]:
    """对注册表里 ``offline + offline_reason=worker:expired`` 的 LINE 协议号试续期。

    成功 → ``report_session_transition(authorized)``（注册表回 online，编排器下轮拉起）；
    失败 → 按指数退避记账（真死的号最多 12h 一次 WARNING），等人重新扫码。
    ``client_factory(tokens_file) -> client`` / ``report(platform, acct, status, detail=)``
    可注入（测试用）。返回 ``{"checked", "revived", "failed", "skipped"}``。绝不抛。
    """
    out = {"checked": 0, "revived": 0, "failed": 0, "skipped": 0}
    ts = time.time() if now is None else float(now)
    try:
        rows = list(registry.list() or [])
    except Exception:  # noqa: BLE001
        return out
    for row in rows:
        try:
            if not isinstance(row, dict):
                continue
            if (str(row.get("platform") or "").lower() != "line"
                    or str(row.get("mode") or "") != "protocol"
                    or str(row.get("status") or "") != "offline"):
                continue
            meta = row.get("meta") or {}
            if not str(meta.get("offline_reason") or "").startswith("worker:expired"):
                continue
            mid = str(row.get("account_id") or "")
            if not mid:
                continue
            path = str(meta.get("tokens_path") or "")
            if not path:
                from src.integrations.line_protocol_login import tokens_path as _tp
                path = _tp(config or {}, mid)
            if not path or not os.path.isfile(path):
                continue
            out["checked"] += 1
            rec = _REVIVE_LEDGER.get(mid) or {"ts": 0.0, "fails": 0}
            if rec["ts"] and (ts - rec["ts"]) < _revive_wait_sec(int(rec["fails"])):
                out["skipped"] += 1
                continue
            rec["ts"] = ts
            _REVIVE_LEDGER[mid] = rec
            if client_factory is not None:
                client = client_factory(path)
            else:
                from okline import OkLine
                client = OkLine.from_tokens_file(path)
            try:
                res = refresh_line_tokens(client, path, reason="revive")
            finally:
                try:
                    client.close()
                except Exception:  # noqa: BLE001
                    pass
            if res.get("ok"):
                rec["fails"] = 0
                _REVIVE_LEDGER.pop(mid, None)
                out["revived"] += 1
                detail = "access token 续期成功，自动恢复上线"
                logger.info("[line-token] 已复活被标 expired 的 LINE 号 %s（exp=%s）",
                            mid, _fmt_ts(float(res.get("exp") or 0.0)))
                if report is not None:
                    report("line", mid, "authorized", detail=detail)
                else:
                    from src.integrations.platform_session_health import report_session_transition
                    report_session_transition("line", mid, "authorized", detail=detail)
            else:
                rec["fails"] = int(rec["fails"]) + 1
                out["failed"] += 1
                logger.warning(
                    "[line-token] LINE 号 %s 复活失败 #%d（%s）%s；下次 %.0f 分钟后再试",
                    mid, rec["fails"], res.get("error") or "-",
                    "→ 需重新扫码" if res.get("relogin") else "",
                    _revive_wait_sec(int(rec["fails"])) / 60.0)
        except Exception:  # noqa: BLE001
            logger.debug("[line-token] 复活扫描单行异常（忽略）", exc_info=True)
    return out


def _fmt_ts(ts: float) -> str:
    try:
        if not ts:
            return "-"
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(ts)))
    except Exception:  # noqa: BLE001
        return str(ts)
