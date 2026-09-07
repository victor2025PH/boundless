"""账号级通道门禁（M-2，2026-09-06：D-M1 登录默认半自动 / #232 通道真相 / #233 退避锁）。

背景（1.0.75 首日 Messenger 实录，UE7VM3 / K9CY6R / ZGKVQB）：账号 15:40 清数据重登，
15:42–15:43 AutoDraft 30 秒内对 7 个会话批量起草并**立即自动投递**——边车会话还没
准备好 → 首条 500 → 边车 ``send_backoff`` 熔断 → 自动重试 13s/23s 改期重投续锁 →
坐席手动发送也吃 429。**通道没准备好就开火，是所有假成功 / 错语言事故的放大器。**

本模块是「按账号」的四件事的单一事实源（会话级档位仍在 conversation_settings，
本模块**不写会话行**——老会话的显式档位不动，D-M1 老板原话）：

1. **登录冷静期**（D-M1 ③）：任何账号登录 / 重登 / 清数据重登 → ``cooldown_sec``
   （默认 10 分钟）内该账号**全部会话**只起草不投递（经 ``effective_automation``
   封顶层 ``login_cooldown`` → review，草稿 ``level=L1 reason=cooldown``），账号栏
   列出「积压待过目 N 条」。判「真实登录」的依据是边车 ``login_id`` 变了或从不健康
   态恢复——边车重启 / 后端重启 / WA 自动重连重推同一 login_id 的 authorized **不算**
   登录（否则 WhatsApp 号永远在冷静期）。
2. **登录后账号默认半自动**（D-M1 ①②，UE7VM3「不看历史模式」）：登录时刻晚于
   用户上一次按账号显式开全自动的时刻 → 账号层默认 ``review``，直到用户在账号菜单
   再次显式选「此账号全部会话 → 全自动」并确认。会话行里已有的显式 ``auto_ai``
   **不受影响**（会话显式 > 账号层，老会话不动）。
3. **连续失败降级**（D-M1 ⑦ / UE7VM3 ⑦）：同账号连续 ``fail_streak_limit``（默认 3）
   次真实发送失败 → 降半自动 + 账号红标「通道异常，已暂停自动发送」；边车退避 429
   （``send_backoff`` / ``account_blocked``）**不计**连续失败——那是通道自保不是新的
   失败；平台消息窗口 / 配额政策拒发（``window_expired``，QQ 机器人被动窗口 / WA·FB
   24h / Zalo 7d）同样**不计**——通道是通的，只是这条按规则此刻不能发。恢复后
   **需用户确认**（``clear_degraded``）才重新放开，不自动解除。
4. **退避锁手动优先**（#233）：边车报 ``send_backoff`` → 记账号级 ``backoff_until``，
   autosend 在此之前不再改期重投（不续命）；手动发送独立于该锁（由 Node 侧 manual
   probe 放行一次）；登录成功 / 健康探测通过 → ``reset_backoff``。

持久化：``config_dir()/account_channel_gate.json``（与 account_mode_onboarding 同目录
同风格；mtime 缓存 + 锁）。任何 IO / 解析异常 → 空态（fail-open：门禁自身故障绝不
成为回复链的新故障点；但冷静期这种「少发」方向的失败面本就安全）。

配置 ``inbox.auto_draft.login_gate``（缺省全开）::

    inbox:
      auto_draft:
        login_gate:
          enabled: true
          cooldown_sec: 600        # 登录后只起草的冷静期
          fail_streak_limit: 3     # 连续真实失败 N 次 → 降半自动 + 红标
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ai_chat_assistant.account_channel_gate")

COOLDOWN_SEC_DEFAULT = 600.0
FAIL_STREAK_LIMIT_DEFAULT = 3
#: 边车「通道自保」类败因：不计连续失败、只记退避水位
BACKOFF_ERROR_KINDS = frozenset({"send_backoff", "account_blocked"})
#: 平台「消息窗口 / 配额」政策拒发（``official_send_error`` 的 ``window_expired``：WA 24h /
#: IG·FB 24h / Zalo cs 7d / QQ 机器人被动回复 60min·4 条）：通道本身是通的，只是这条
#: 消息按平台规则此刻不能发——**不计**连续失败、不记退避（2026-09-07 QQ 官方轨接入时
#: 收口：否则一条主动关怀被窗口拦 3 次就把好端端的机器人降成半自动 + 红标「通道异常」）。
POLICY_ERROR_KINDS = frozenset({"window_expired"})
#: 退避水位夹取（秒）：边车 retry_after_ms 缺失时按下限；超长冻结按上限（超过就该人来看）
_BACKOFF_MIN_SEC = 5.0
_BACKOFF_MAX_SEC = 15 * 60.0

#: 降级红标的机器可读原因
DEGRADED_REASON_FAIL_STREAK = "fail_streak"

_lock = threading.RLock()
_cache: Dict[str, Any] = {"path": None, "mtime": None, "data": None}


# ── 配置 ─────────────────────────────────────────────────────────────────

def gate_cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """解析 ``inbox.auto_draft.login_gate``；缺项取安全默认，绝不抛。"""
    out = {"enabled": True, "cooldown_sec": COOLDOWN_SEC_DEFAULT,
           "fail_streak_limit": FAIL_STREAK_LIMIT_DEFAULT}
    try:
        node = (((config or {}).get("inbox") or {}).get("auto_draft") or {}).get(
            "login_gate")
        if isinstance(node, dict):
            if "enabled" in node:
                out["enabled"] = bool(node.get("enabled"))
            if node.get("cooldown_sec") is not None:
                out["cooldown_sec"] = max(0.0, float(node.get("cooldown_sec")))
            if node.get("fail_streak_limit") is not None:
                out["fail_streak_limit"] = max(1, int(node.get("fail_streak_limit")))
    except Exception:
        return {"enabled": True, "cooldown_sec": COOLDOWN_SEC_DEFAULT,
                "fail_streak_limit": FAIL_STREAK_LIMIT_DEFAULT}
    return out


def account_key(platform: str, account_id: str) -> str:
    return f"{str(platform or '').strip().lower()}:{str(account_id or '').strip()}"


# ── 持久化 ───────────────────────────────────────────────────────────────

def _state_path() -> Optional[Path]:
    try:
        from src.licensing.data_paths import config_dir
        return Path(config_dir()) / "account_channel_gate.json"
    except Exception:
        return None


def _empty_state() -> Dict[str, Any]:
    return {"accounts": {}}


def _load_state() -> Dict[str, Any]:
    path = _state_path()
    with _lock:
        if path is None:
            return _cache["data"] if _cache["data"] is not None else _empty_state()
        try:
            mtime = path.stat().st_mtime_ns if path.exists() else None
        except OSError:
            mtime = None
        if (_cache["data"] is not None and _cache["path"] == str(path)
                and _cache["mtime"] == mtime):
            return _cache["data"]
        data = _empty_state()
        if mtime is not None:
            try:
                raw = json.loads(path.read_text("utf-8")) or {}
                accts = raw.get("accounts") if isinstance(raw, dict) else None
                if isinstance(accts, dict):
                    for k, v in accts.items():
                        if isinstance(v, dict):
                            data["accounts"][str(k)] = dict(v)
            except Exception:
                logger.debug("account_channel_gate 状态装载失败（按空态）", exc_info=True)
        _cache.update(path=str(path), mtime=mtime, data=data)
        return data


def _persist_state(data: Dict[str, Any]) -> bool:
    path = _state_path()
    with _lock:
        _cache.update(path=str(path) if path else None, data=data)
        if path is None:
            _cache["mtime"] = None
            return False
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")
            _cache["mtime"] = path.stat().st_mtime_ns
            return True
        except Exception:
            logger.debug("account_channel_gate 落盘失败（保留内存态）", exc_info=True)
            _cache["mtime"] = None
            return False


def _acct(data: Dict[str, Any], key: str) -> Dict[str, Any]:
    accts = data.setdefault("accounts", {})
    rec = accts.get(key)
    if not isinstance(rec, dict):
        rec = {}
        accts[key] = rec
    return rec


def _f(rec: Dict[str, Any], k: str) -> float:
    try:
        return float(rec.get(k) or 0.0)
    except (TypeError, ValueError):
        return 0.0


# ── 1 + 2：登录事件 / 冷静期 / 登录后账号默认 ─────────────────────────────

def note_login(platform: str, account_id: str, *, login_id: str = "",
               now: Optional[float] = None,
               config: Optional[Dict[str, Any]] = None,
               force: bool = False) -> bool:
    """登记一次**真实**登录 / 重登；返回是否开启了冷静期。

    判据：``login_id`` 非空且与上次记录不同（边车换了登录档案＝清数据 / 重新登录），
    或 ``force=True``（调用方已判出「从不健康恢复」这类确定性重登）。同一 login_id
    重推（边车 / 后端重启、WA 自动重连）→ False 不动——否则 WA 号永远在冷静期。
    登录同时：清连续失败 / 退避（新会话从头判），降级红标**保留**（那是要人确认的）。
    """
    key = account_key(platform, account_id)
    if not str(account_id or "").strip():
        return False
    cfg = gate_cfg(config)
    ts = time.time() if now is None else float(now)
    lid = str(login_id or "").strip()
    with _lock:
        data = _load_state()
        rec = _acct(data, key)
        prev_lid = str(rec.get("login_id") or "")
        is_new = force or (bool(lid) and lid != prev_lid)
        if lid:
            rec["login_id"] = lid
        if not is_new:
            _persist_state(data)
            return False
        rec["login_ts"] = ts
        rec["fail_streak"] = 0
        rec["backoff_until"] = 0.0
        rec["backoff_kind"] = ""
        if cfg["enabled"] and cfg["cooldown_sec"] > 0:
            rec["cooldown_until"] = ts + float(cfg["cooldown_sec"])
        else:
            rec["cooldown_until"] = 0.0
        _persist_state(data)
    logger.info(
        "[channel_gate] 账号登录/重登 %s login_id=%s → 冷静期 %.0fs 只起草不投递；"
        "账号默认半自动，全自动需在账号菜单显式开启（D-M1）",
        key, lid or "-", float(cfg["cooldown_sec"]) if cfg["enabled"] else 0.0)
    return True


def cooldown_remaining(platform: str, account_id: str,
                       now: Optional[float] = None) -> float:
    """冷静期剩余秒数（0＝不在冷静期）。"""
    ts = time.time() if now is None else float(now)
    rec = _load_state().get("accounts", {}).get(account_key(platform, account_id)) or {}
    until = _f(rec, "cooldown_until")
    return max(0.0, until - ts) if until > 0 else 0.0


def end_cooldown(platform: str, account_id: str) -> bool:
    """显式结束冷静期（预留给「我已过目积压」类操作）。"""
    key = account_key(platform, account_id)
    with _lock:
        data = _load_state()
        rec = data.get("accounts", {}).get(key)
        if not rec or not _f(rec, "cooldown_until"):
            return False
        rec["cooldown_until"] = 0.0
        _persist_state(data)
    return True


def confirm_account_mode(platform: str, account_id: str, mode: str, *,
                         actor: str = "", now: Optional[float] = None) -> None:
    """用户在账号菜单按账号显式选档（D-M1 ②）——记时刻，登录后默认半自动到此为止。

    与 ``account_mode_onboarding.decide_account_mode`` 配合：那边持久化「选了什么」，
    这边持久化「什么时候选的」（要和 ``login_ts`` 比大小）。
    """
    key = account_key(platform, account_id)
    ts = time.time() if now is None else float(now)
    with _lock:
        data = _load_state()
        rec = _acct(data, key)
        rec["confirmed_ts"] = ts
        rec["confirmed_mode"] = str(mode or "")
        rec["confirmed_by"] = str(actor or "")[:48]
        _persist_state(data)


def login_default_mode(platform: str, account_id: str) -> Optional[str]:
    """登录后账号层默认档；``None``＝本层不表态（回落 onboarding / 全局）。

    有过登录记录且登录晚于用户最近一次按账号确认 → ``review``（半自动）。
    """
    rec = _load_state().get("accounts", {}).get(account_key(platform, account_id)) or {}
    login_ts = _f(rec, "login_ts")
    if login_ts <= 0:
        return None
    if _f(rec, "confirmed_ts") >= login_ts:
        return None
    return "review"


def login_pending_confirm(platform: str, account_id: str) -> bool:
    """账号是否处于「登录后默认半自动、等用户按账号确认」态（UI 提示用）。"""
    return login_default_mode(platform, account_id) == "review"


# ── 3：连续失败降级 ────────────────────────────────────────────────────────

def note_send_ok(platform: str, account_id: str, *, manual: bool = False) -> None:
    """一次真实送达成功：清连续失败 + 清退避（通道显然通了）。降级红标不自动清。"""
    key = account_key(platform, account_id)
    if not str(account_id or "").strip():
        return
    with _lock:
        data = _load_state()
        rec = data.get("accounts", {}).get(key)
        if not rec:
            return
        changed = False
        if int(rec.get("fail_streak") or 0):
            rec["fail_streak"] = 0
            changed = True
        if _f(rec, "backoff_until"):
            rec["backoff_until"] = 0.0
            rec["backoff_kind"] = ""
            changed = True
        if changed:
            rec["last_ok_ts"] = time.time()
            rec["last_ok_manual"] = bool(manual)
            _persist_state(data)


def note_send_fail(platform: str, account_id: str, *, error_kind: str = "",
                   retry_after_ms: int = 0, now: Optional[float] = None,
                   config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """一次发送失败。返回 ``{streak, degraded_now, degraded, backoff_until}``。

    - 边车退避类（``send_backoff`` / ``account_blocked``）→ 只记 ``backoff_until``，
      **不计**连续失败（#233：退避期重试不计失败）；
    - 平台政策拒发（``POLICY_ERROR_KINDS``，如 ``window_expired``）→ 只记
      ``last_policy_block_ts/kind``，**不计**连续失败、不记退避（通道是通的）；
    - 其余 → ``fail_streak += 1``，达阈值首次进入降级（``degraded_now=True``）。
    """
    key = account_key(platform, account_id)
    out = {"streak": 0, "degraded_now": False, "degraded": False, "backoff_until": 0.0}
    if not str(account_id or "").strip():
        return out
    cfg = gate_cfg(config)
    ts = time.time() if now is None else float(now)
    kind = str(error_kind or "").strip().lower()
    with _lock:
        data = _load_state()
        rec = _acct(data, key)
        if kind in POLICY_ERROR_KINDS:
            rec["last_policy_block_ts"] = ts
            rec["last_policy_block_kind"] = kind[:48]
            out["streak"] = int(rec.get("fail_streak") or 0)
            out["degraded"] = bool(_f(rec, "degraded_since"))
            out["backoff_until"] = _f(rec, "backoff_until")
            out["policy_block"] = True
            _persist_state(data)
            return out
        if kind in BACKOFF_ERROR_KINDS:
            wait = float(retry_after_ms or 0) / 1000.0
            wait = min(_BACKOFF_MAX_SEC, max(_BACKOFF_MIN_SEC, wait))
            rec["backoff_until"] = max(_f(rec, "backoff_until"), ts + wait)
            rec["backoff_kind"] = kind
            out["backoff_until"] = rec["backoff_until"]
            out["streak"] = int(rec.get("fail_streak") or 0)
            out["degraded"] = bool(_f(rec, "degraded_since"))
            _persist_state(data)
            return out
        streak = int(rec.get("fail_streak") or 0) + 1
        rec["fail_streak"] = streak
        rec["last_fail_ts"] = ts
        rec["last_fail_kind"] = kind[:48]
        out["streak"] = streak
        already = bool(_f(rec, "degraded_since"))
        if (cfg["enabled"] and streak >= int(cfg["fail_streak_limit"])
                and not already):
            rec["degraded_since"] = ts
            rec["degraded_reason"] = DEGRADED_REASON_FAIL_STREAK
            rec["degraded_streak"] = streak
            out["degraded_now"] = True
        out["degraded"] = bool(_f(rec, "degraded_since"))
        _persist_state(data)
    if out["degraded_now"]:
        logger.warning(
            "[channel_gate] 账号 %s 连续 %d 次发送失败（最近：%s）→ 自动降半自动，"
            "已暂停自动发送；恢复后需用户在账号菜单确认再开（D-M1 ⑦）",
            key, streak, kind or "?")
    return out


def degraded_state(platform: str, account_id: str) -> Optional[Dict[str, Any]]:
    """降级态：``None``＝正常；否则 ``{since, reason, streak}``。"""
    rec = _load_state().get("accounts", {}).get(account_key(platform, account_id)) or {}
    since = _f(rec, "degraded_since")
    if since <= 0:
        return None
    return {"since": since, "reason": str(rec.get("degraded_reason") or ""),
            "streak": int(rec.get("degraded_streak") or 0)}


def clear_degraded(platform: str, account_id: str, *, actor: str = "") -> bool:
    """用户确认通道已恢复 → 解除降级红标（连续失败计数一并归零）。"""
    key = account_key(platform, account_id)
    with _lock:
        data = _load_state()
        rec = data.get("accounts", {}).get(key)
        if not rec or not _f(rec, "degraded_since"):
            return False
        rec["degraded_since"] = 0.0
        rec["degraded_reason"] = ""
        rec["fail_streak"] = 0
        rec["degraded_cleared_by"] = str(actor or "")[:48]
        rec["degraded_cleared_ts"] = time.time()
        _persist_state(data)
    logger.info("[channel_gate] 账号 %s 降级已由 %s 确认解除", key, actor or "?")
    return True


# ── 4：退避锁 ─────────────────────────────────────────────────────────────

def backoff_remaining(platform: str, account_id: str,
                      now: Optional[float] = None) -> float:
    """账号级自动投递退避剩余秒数（0＝无退避）。"""
    ts = time.time() if now is None else float(now)
    rec = _load_state().get("accounts", {}).get(account_key(platform, account_id)) or {}
    until = _f(rec, "backoff_until")
    return max(0.0, until - ts) if until > 0 else 0.0


def reset_backoff(platform: str, account_id: str, *, why: str = "") -> bool:
    """登录成功 / 通道健康探测通过 → 清退避（#233「登录成功即解锁」）。"""
    key = account_key(platform, account_id)
    with _lock:
        data = _load_state()
        rec = data.get("accounts", {}).get(key)
        if not rec or not _f(rec, "backoff_until"):
            return False
        rec["backoff_until"] = 0.0
        rec["backoff_kind"] = ""
        _persist_state(data)
    logger.info("[channel_gate] 账号 %s 自动投递退避已重置（%s）", key, why or "-")
    return True


# ── 汇总：封顶层 / worker 闸 / UI 快照 ────────────────────────────────────

def hold_reason(platform: str, account_id: str, *, now: Optional[float] = None,
                config: Optional[Dict[str, Any]] = None) -> str:
    """autosend 自动投递此刻是否该按账号扣住：``cooldown`` / ``degraded`` / ``backoff`` / ``""``。

    只判本模块自己的三种状态；「通道未连接」由 ``platform_session_health.
    channel_connection_state`` 另判（各自单一事实源，调用方两个都问）。
    """
    if not gate_cfg(config)["enabled"]:
        return ""
    if cooldown_remaining(platform, account_id, now) > 0:
        return "cooldown"
    if degraded_state(platform, account_id) is not None:
        return "degraded"
    if backoff_remaining(platform, account_id, now) > 0:
        return "backoff"
    return ""


def gate_caps(platform: str, account_id: str, *, config: Optional[Dict[str, Any]] = None,
              now: Optional[float] = None) -> List[Any]:
    """供 ``effective_automation.compute_mode_caps`` 追加的封顶层（review）。

    - ``login_cooldown``（带 until_ts 可倒计时）
    - ``channel_degraded``（无 until，需用户确认）
    - ``channel_disconnected``（通道未连接：needs_login / worker 重连中放弃等）
    """
    from src.inbox.effective_automation import ModeCap
    caps: List[Any] = []
    if not gate_cfg(config)["enabled"]:
        return caps
    ts = time.time() if now is None else float(now)
    try:
        rem = cooldown_remaining(platform, account_id, ts)
        if rem > 0:
            caps.append(ModeCap("login_cooldown", "review",
                                detail=f"remaining_s={rem:.0f}", until_ts=ts + rem))
    except Exception:
        pass
    try:
        deg = degraded_state(platform, account_id)
        if deg is not None:
            caps.append(ModeCap("channel_degraded", "review",
                                detail=f"{deg.get('reason') or 'fail'}:{deg.get('streak') or 0}"))
    except Exception:
        pass
    try:
        from src.integrations.platform_session_health import channel_connection_state
        cs = channel_connection_state(platform, account_id, now=ts)
        if cs.get("state") == "disconnected":
            caps.append(ModeCap("channel_disconnected", "review",
                                detail=str(cs.get("reason") or "disconnected")))
    except Exception:
        pass
    return caps


def account_snapshot(platform: str, account_id: str,
                     now: Optional[float] = None,
                     config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """UI 快照（账号栏 / 账号菜单 / 会话头部共用）。"""
    ts = time.time() if now is None else float(now)
    rec = _load_state().get("accounts", {}).get(account_key(platform, account_id)) or {}
    deg = degraded_state(platform, account_id)
    return {
        "cooldown_remaining_s": round(cooldown_remaining(platform, account_id, ts)),
        "cooldown_until": _f(rec, "cooldown_until"),
        "login_ts": _f(rec, "login_ts"),
        "login_pending_confirm": login_pending_confirm(platform, account_id),
        "degraded": deg is not None,
        "degraded_since": (deg or {}).get("since", 0.0),
        "degraded_reason": (deg or {}).get("reason", ""),
        "fail_streak": int(rec.get("fail_streak") or 0),
        "backoff_remaining_s": round(backoff_remaining(platform, account_id, ts)),
        "hold_reason": hold_reason(platform, account_id, now=ts, config=config),
    }


def _reset_for_tests() -> None:
    with _lock:
        _cache.update(path=None, mtime=None, data=None)


__all__ = [
    "COOLDOWN_SEC_DEFAULT", "FAIL_STREAK_LIMIT_DEFAULT", "BACKOFF_ERROR_KINDS",
    "POLICY_ERROR_KINDS", "DEGRADED_REASON_FAIL_STREAK",
    "gate_cfg", "account_key",
    "note_login", "cooldown_remaining", "end_cooldown",
    "confirm_account_mode", "login_default_mode", "login_pending_confirm",
    "note_send_ok", "note_send_fail", "degraded_state", "clear_degraded",
    "backoff_remaining", "reset_backoff",
    "hold_reason", "gate_caps", "account_snapshot",
]
