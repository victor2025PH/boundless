"""新账号「AI 接管方式」确认层（账号级档位，P0 2026-08-30）。

需求（老板原话）：以后登入每个 app 的新账号都要有一个提醒——这个账号的 AI
接管方式选「全自动 / 拟稿人审 / 关闭」，默认拟稿人审。

语义
====
- **新账号**＝注册表 ``created_at``（各平台登录链统一 upsert、首插即冻结，见
  ``account_connection.resolve_account_connected_at`` 的取数注释）晚于本功能的
  **启用基线时刻** ``baseline_ts`` 的账号。存量账号全部豁免（零打扰零行为变化）。
- **未确认默认 review**：新账号在运营选档前，其下会话生效档位＝拟稿人审——
  AI 只写稿不发送，这就是「默认是拟稿人审」的行为落点（不是只在 UI 预选）。
- **三档映射**（与全局三档/收件箱会话档同一词汇表）：
  全自动=``auto_ai`` / 拟稿人审=``review`` / 关闭=``manual``（B 线对 manual
  不拟稿、A 线只对 auto_ai 直发，语义由既有消费方天然成立）。
- **档位解析层级**（``automation_mode.resolve_automation_mode`` 消费）：
  会话显式设置 > **账号级（本模块）** > 全局 ``auto_draft.automation_mode``。
  账号层生效期间 bootstrap **不落盘**（账号层自身持久，会话跟随账号决策——
  确认「全自动」后该账号存量未显式设置的会话立即自动化，零对齐成本）。

失败方向（热路每条消息都会问，绝不能成为故障点）
============================================
任何 IO / 解析 / 注册表异常 → ``None``（回落全局旧行为）。``connected_at``
判不出（=0.0）按**老账号**处理——宁可不打扰、不改行为，也不把老客户的自动
回复静默降级成人审（与 ``resolve_account_connected_at`` 的失败方向一致）。

持久化
======
``config_dir()/account_mode_onboarding.json``（与 ``account_connection.json``
同目录同风格）：``{"baseline_ts": float, "decisions": {"<platform>:<acct>":
{"mode","ts","actor"}}}``。进程内 mtime 缓存（热路零重复读盘）+ 锁保护写。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.inbox.account_connection import (
    resolve_account_connected_at as _resolve_connected_at,
)

logger = logging.getLogger("ai_chat_assistant.account_mode_onboarding")

#: 提醒卡三选项（UI 顺序）；与会话档位共用同一词汇表。
DECISION_MODES = ("auto_ai", "review", "manual")

#: 新账号未确认前的生效档位（安全默认＝拟稿人审）。
PENDING_DEFAULT_MODE = "review"

#: 会话行对齐时的写入来源（standby 的 _ALIGN_SOURCES 刻意不含它：账号级决策
#: 是人按账号做的显式决定，不被全局一键批量覆盖——与 human 行同待遇）。
ALIGN_SOURCE = "account_mode"

_lock = threading.Lock()
_cache: Dict[str, Any] = {"path": None, "mtime": None, "data": None}


def _cfg(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    node = (((config or {}).get("inbox") or {}).get("auto_draft") or {})
    sub = node.get("account_mode_onboarding")
    return sub if isinstance(sub, dict) else {}


def onboarding_enabled(config: Optional[Dict[str, Any]]) -> bool:
    """总开关 ``inbox.auto_draft.account_mode_onboarding.enabled``（默认关）。"""
    return bool(_cfg(config).get("enabled"))


def account_key(platform: str, account_id: str) -> str:
    return f"{str(platform or '').strip().lower()}:{str(account_id or '').strip()}"


def _state_path() -> Optional[Path]:
    try:
        from src.licensing.data_paths import config_dir
        return Path(config_dir()) / "account_mode_onboarding.json"
    except Exception:
        return None


def _empty_state() -> Dict[str, Any]:
    return {"baseline_ts": 0.0, "decisions": {}}


def _load_state() -> Dict[str, Any]:
    """读状态（mtime 缓存）；任何异常返回内存已有值或空态，绝不抛。"""
    path = _state_path()
    with _lock:
        if path is None:
            return dict(_cache["data"]) if _cache["data"] else _empty_state()
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
                if isinstance(raw, dict):
                    try:
                        data["baseline_ts"] = float(raw.get("baseline_ts") or 0.0)
                    except (TypeError, ValueError):
                        data["baseline_ts"] = 0.0
                    dec = raw.get("decisions")
                    if isinstance(dec, dict):
                        for k, v in dec.items():
                            if (isinstance(v, dict)
                                    and str(v.get("mode")) in DECISION_MODES):
                                data["decisions"][str(k)] = {
                                    "mode": str(v["mode"]),
                                    "ts": float(v.get("ts") or 0.0),
                                    "actor": str(v.get("actor") or ""),
                                }
            except Exception:
                logger.debug("account_mode_onboarding 状态装载失败（按空态）",
                             exc_info=True)
        _cache.update(path=str(path), mtime=mtime, data=data)
        return data


def _persist_state(data: Dict[str, Any]) -> bool:
    """落盘并刷新缓存；失败保留内存值（下轮解析仍按内存生效）。"""
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
            logger.debug("account_mode_onboarding 落盘失败（保留内存态）",
                         exc_info=True)
            _cache["mtime"] = None
            return False


def ensure_baseline(now: Optional[float] = None) -> float:
    """首次调用写入启用基线时刻（observe-once）；已有即返回既有值。

    只应在 ``onboarding_enabled`` 为真后被调（各入口自判）——功能关着时不建档，
    「装了很久才开」的部署以**开启后首次流量/首次打开设置页**为基线。
    """
    data = _load_state()
    if data.get("baseline_ts"):
        return float(data["baseline_ts"])
    ts = time.time() if now is None else float(now)
    data = {"baseline_ts": ts, "decisions": dict(data.get("decisions") or {})}
    _persist_state(data)
    return ts


def decided_mode(platform: str, account_id: str) -> Optional[str]:
    """运营已确认的账号档位；未确认 → None。"""
    rec = _load_state().get("decisions", {}).get(account_key(platform, account_id))
    return str(rec["mode"]) if rec else None


def is_new_account(
    platform: str, account_id: str, *, now: Optional[float] = None,
) -> bool:
    """账号接入时刻晚于基线 → 新账号。接入时刻判不出（0.0）→ False（按存量）。"""
    baseline = float(_load_state().get("baseline_ts") or 0.0)
    if baseline <= 0:
        return False
    try:
        connected = float(_resolve_connected_at(platform, account_id, now=now) or 0.0)
    except Exception:
        return False
    return connected > baseline


def account_mode_for(
    platform: str, account_id: str, config: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """账号级生效档位；``None``＝本层不表态（回落全局）。

    已确认 → 所选档；新账号未确认 → ``review``（安全默认）；存量账号 → None。
    """
    try:
        if not onboarding_enabled(config):
            return None
        if not str(account_id or "").strip():
            return None
        ensure_baseline()
        decided = decided_mode(platform, account_id)
        if decided is not None:
            return decided
        if is_new_account(platform, account_id):
            return PENDING_DEFAULT_MODE
        return None
    except Exception:
        logger.debug("account_mode_for 异常（回落全局）", exc_info=True)
        return None


def account_mode_from_cid(
    conversation_id: str, config: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """按会话 id（``platform:account_id:chat_key``）取账号级档位。"""
    parts = str(conversation_id or "").split(":", 2)
    if len(parts) < 3:
        return None
    return account_mode_for(parts[0], parts[1], config)


def decide_account_mode(
    platform: str, account_id: str, mode: str, *, actor: str = "",
) -> bool:
    """写入运营决策（三档白名单）；非法档位/无账号 id → False 不写。"""
    mode = str(mode or "").strip().lower()
    if mode not in DECISION_MODES:
        return False
    if not str(account_id or "").strip() or not str(platform or "").strip():
        return False
    data = _load_state()
    decisions = dict(data.get("decisions") or {})
    decisions[account_key(platform, account_id)] = {
        "mode": mode, "ts": round(time.time(), 3),
        "actor": str(actor or "")[:80],
    }
    _persist_state({"baseline_ts": float(data.get("baseline_ts") or 0.0),
                    "decisions": decisions})
    return True


def pending_accounts(
    config: Optional[Dict[str, Any]] = None, *, registry: Any = None,
) -> List[Dict[str, Any]]:
    """待确认清单：注册表里 ``created_at`` 晚于基线且未决策的账号（新→旧排序）。

    ``registry`` 可注入（测试）；缺省取进程单例。注册表异常 → 空表（提醒缺席
    好过报错——本函数只服务提醒 UI，不参与档位解析）。
    """
    if not onboarding_enabled(config):
        return []
    baseline = ensure_baseline()
    data = _load_state()
    decisions = data.get("decisions") or {}
    try:
        if registry is None:
            from src.integrations.account_registry import get_account_registry
            registry = get_account_registry()
        rows = registry.list() or []
    except Exception:
        logger.debug("pending_accounts 注册表读取失败（返回空）", exc_info=True)
        return []
    out: List[Dict[str, Any]] = []
    for r in rows:
        try:
            plat = str(r.get("platform") or "")
            aid = str(r.get("account_id") or "")
            created = float(r.get("created_at") or 0.0)
            if not plat or not aid or created <= baseline:
                continue
            if account_key(plat, aid) in decisions:
                continue
            out.append({
                "platform": plat, "account_id": aid,
                "display_name": str(r.get("display_name") or r.get("name")
                                    or aid),
                "status": str(r.get("status") or ""),
                "connected_at": created,
                "default_mode": PENDING_DEFAULT_MODE,
            })
        except Exception:
            continue
    out.sort(key=lambda x: -x["connected_at"])
    return out


def decisions_snapshot() -> List[Dict[str, Any]]:
    """已决策清单（设置页展示/改档用），按决策时间新→旧。"""
    dec = _load_state().get("decisions") or {}
    out: List[Dict[str, Any]] = []
    for key, rec in dec.items():
        plat, _, aid = str(key).partition(":")
        out.append({"platform": plat, "account_id": aid,
                    "mode": str(rec.get("mode") or ""),
                    "ts": float(rec.get("ts") or 0.0),
                    "actor": str(rec.get("actor") or "")})
    out.sort(key=lambda x: -x["ts"])
    return out


def align_account_conversations(
    store: Any, platform: str, account_id: str, target_mode: str,
) -> int:
    """把该账号下**系统落档**的存量会话行对齐到 ``target_mode``。

    正常情况下账号层生效期 bootstrap 不落盘，本函数只清「功能启用前/注册表
    短暂读不到期间」被全局档固化的残留行。范围＝source ∈ {bootstrap, standby,
    account_mode}（上次账号决策写的行换档要跟着改）；human/takeover/guard/
    snooze 等人的决定绝不覆盖。升 auto_ai 时群会话跳过（与 standby 对齐同一
    铁律）。返回实际改动行数；任何异常尽量跳过单行不中断。
    """
    if store is None or target_mode not in DECISION_MODES:
        return 0
    prefix = f"{str(platform or '').strip().lower()}:{str(account_id or '').strip()}:"
    try:
        rows = store.list_automation_mode_rows()
    except Exception:
        return 0
    changed = 0
    for r in rows or []:
        try:
            cid = str(r.get("conversation_id") or "")
            cur = str(r.get("automation_mode") or "")
            src = str(r.get("source") or "")
            if (not cid.startswith(prefix) or cur == target_mode
                    or src not in ("bootstrap", "standby", ALIGN_SOURCE)):
                continue
            if target_mode == "auto_ai":
                from src.inbox.automation_mode import conversation_is_group
                from src.inbox.ingest import is_group_conversation
                if (conversation_is_group(store, cid)
                        or is_group_conversation({
                            "conversation_id": cid,
                            "platform": cid.split(":", 1)[0]})):
                    continue
            store.set_automation_mode(cid, target_mode, source=ALIGN_SOURCE)
            changed += 1
        except Exception:
            continue
    return changed


def _reset_for_tests() -> None:
    """清进程内缓存（测试隔离用；配合 AITR_DATA_DIR 指向 tmp）。"""
    with _lock:
        _cache.update(path=None, mtime=None, data=None)


__all__ = [
    "DECISION_MODES", "PENDING_DEFAULT_MODE", "ALIGN_SOURCE",
    "onboarding_enabled", "ensure_baseline", "account_key",
    "decided_mode", "is_new_account", "account_mode_for",
    "account_mode_from_cid", "decide_account_mode",
    "pending_accounts", "decisions_snapshot",
    "align_account_conversations",
]
