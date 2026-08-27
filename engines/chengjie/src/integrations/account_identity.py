# -*- coding: utf-8 -*-
"""账号「登录身份决议」层（实施72 P1-1/P1-2，2026-08-27 身份错乱事故根治）。

事故一句话：登录位 ``msg_nxl3iqxn`` 原属生产号 Calixa Lopez，人工重登时被改登成
另一个 FB 账号 Wisley Gho——系统零校验零提示地接受了「换人」：静默新建账号行、
自动补挂默认人设、会话进全自动，AI 差点以新身份开口（被防封额度 auto_cap=0
误打误撞拦下）。「登录成功」≠「身份合法且延续」，中间缺一次**决议**。

本模块提供决议所需的三件事（全部 best-effort，绝不成为登录链故障点）：

1. **登录位↔账号映射全路径落库**（``record_login_identity``）——此前只有连接流
   轮询写 ``messenger_login_id``，直打 worker 的 relogin / session-status 回报路径
   不写 → 想校验连原料都没有。现在任何登录路径都可调本函数补写映射并**检测换人**
   （同 login_id 此前挂在别的账号上）。
2. **隔离态（identity_pending）**（``quarantine_account``）——检测到换人时给新账号
   打 ``identity_pending`` 标：
   - ``persona_voice.ensure_account_default_persona`` 对 pending 账号跳过自动补挂
     （错误身份场景下"便利功能"是放大器）；
   - ``effective_automation.compute_mode_caps`` 对 pending 账号封顶 ``review``
     （AI 只拟稿人审后发，手动发送不受影响）；
   - ops_alert + ops_events 审计（谁/何时/从谁换到谁）。
3. **显式转正**（``confirm_account_identity``）——人确认后清 pending、显式绑定人设
   （或此时才补默认人设）、落审计。「同一登录位换人」必须是显式决策，不是静默事实。

刻意不做（改动前先读）：
- **不自动登出/踢掉新身份**：登录是人在服务器窗口里完成的真实动作，系统的职责是
  「不让 AI 以未确认身份说话」，不是替人反悔登录。
- **不动存储命名空间**：conversation_id 本就按账号隔离（本次事故零数据串写），
  决议层只管工作流。
- pending 判定走 ``peek_account``（只读现有注册表单例，绝不隐式建库）+ 60s TTL
  进程缓存——与 ``cached_business_line`` 同一纪律：热路径便宜、判不出＝不拦。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("ai_chat_assistant.account_identity")

# 平台 → 登录位映射的 meta 键（与各 login provider 既有落库键逐字一致；
# 未收录平台回落 "<platform>_login_id"）。LINE/Telegram 无浏览器登录位概念，不参与。
_LOGIN_ID_META_KEYS: Dict[str, str] = {
    "messenger": "messenger_login_id",
    "whatsapp": "baileys_login_id",
    "instagram": "instagram_login_id",
    "zalo": "zca_login_id",
}

# identity_pending 判定的进程缓存（热路径 compute_mode_caps 每条消息都会问）
_PENDING_CACHE_TTL = 60.0
_pending_cache: Dict[str, Any] = {}
_pending_lock = threading.Lock()

# 「疑似自有号未登记」候选的进程缓存（ops 卡轮询消费；扫会话表按 TTL 摊薄）
_FLEET_CAND_TTL = 300.0
_fleet_cand_cache: Dict[str, Any] = {}
_fleet_cand_lock = threading.Lock()


def login_meta_key(platform: str) -> str:
    """该平台登录位映射所用的 meta 键名。"""
    p = str(platform or "").strip().lower()
    return _LOGIN_ID_META_KEYS.get(p, f"{p}_login_id" if p else "")


def _get_registry(registry: Any = None) -> Any:
    if registry is not None:
        return registry
    try:
        from src.integrations.account_registry import get_account_registry
        return get_account_registry()
    except Exception:
        return None


def invalidate_pending_cache(platform: str = "", account_id: str = "") -> None:
    """清 pending 判定缓存（写路径 / 测试用）。空参 = 全清。"""
    with _pending_lock:
        if not platform and not account_id:
            _pending_cache.clear()
        else:
            _pending_cache.pop(f"{str(platform).lower()}:{account_id}", None)


def is_identity_pending_row(row: Optional[Dict[str, Any]]) -> bool:
    """账号行是否处于「身份待确认」隔离态（纯函数）。"""
    try:
        return bool(((row or {}).get("meta") or {}).get("identity_pending"))
    except Exception:
        return False


def cached_account_meta(platform: str, account_id: str) -> Dict[str, Any]:
    """账号 meta 的热路径快照（60s TTL；判不出＝空 dict，fail-open）。

    只读现有注册表单例（``peek_account``，无隐式建库副作用）。供每条消息都会
    问的封顶层共用（identity_pending / reconnect_backlog），一次取数两处消费。
    """
    key = f"{str(platform or '').lower()}:{str(account_id or '')}"
    now = time.time()
    with _pending_lock:
        hit = _pending_cache.get(key)
        if hit is not None and (now - hit[1]) < _PENDING_CACHE_TTL:
            return dict(hit[0])
    meta: Dict[str, Any] = {}
    try:
        from src.integrations.account_registry import peek_account
        row = peek_account(platform, account_id)
        meta = dict((row or {}).get("meta") or {})
    except Exception:
        meta = {}
    with _pending_lock:
        _pending_cache[key] = (meta, now)
    return dict(meta)


def identity_pending(platform: str, account_id: str) -> bool:
    """热路径判定：该账号是否身份待确认。判不出＝False（fail-open 不拦）。"""
    try:
        return bool(cached_account_meta(platform, account_id).get(
            "identity_pending"))
    except Exception:
        return False


def record_login_identity(
    platform: str, account_id: str, login_id: str, *, registry: Any = None,
) -> Dict[str, Any]:
    """登录成功后登记「登录位↔账号」映射，并返回换人检测结果。

    返回 ``{"changed": bool, "prev_accounts": [...]}``：``prev_accounts``＝同平台
    此前把同一 login_id 记在自己名下的**其他**账号（含 removed——信息面，调用方
    自行决定处置）。任何异常 → ``{"changed": False, "prev_accounts": []}``。
    """
    out: Dict[str, Any] = {"changed": False, "prev_accounts": []}
    plat = str(platform or "").strip().lower()
    acct = str(account_id or "").strip()
    lid = str(login_id or "").strip()
    mkey = login_meta_key(plat)
    if not plat or not acct or not lid or not mkey:
        return out
    reg = _get_registry(registry)
    if reg is None:
        return out
    try:
        prev: List[str] = []
        for row in (reg.list(plat, include_removed=True) or []):
            aid = str(row.get("account_id") or "")
            if not aid or aid == acct:
                continue
            if str(((row.get("meta") or {}).get(mkey)) or "") == lid:
                prev.append(aid)
        cur = reg.get(plat, acct) or {}
        if str(((cur.get("meta") or {}).get(mkey)) or "") != lid:
            reg.upsert(plat, acct, meta={mkey: lid}, merge_meta=True)
        out["prev_accounts"] = prev
        out["changed"] = bool(prev)
        return out
    except Exception:
        logger.debug("[account_identity] 登录映射登记失败（忽略）", exc_info=True)
        return out


def quarantine_account(
    platform: str, account_id: str, prev_accounts: List[str], *,
    registry: Any = None, login_id: str = "",
) -> bool:
    """检测到换人 → 给新账号打「身份待确认」标（幂等），并告警+审计。

    幂等语义：已 pending → 不重复告警；此前已被人对**同一批前任**显式转正
    （``identity_confirmed_prev``）→ 不再打回隔离（尊重人的决定）。
    返回「本次是否新打标」。绝不抛。
    """
    plat = str(platform or "").strip().lower()
    acct = str(account_id or "").strip()
    prev = sorted({str(p) for p in (prev_accounts or []) if str(p) and str(p) != acct})
    if not plat or not acct or not prev:
        return False
    reg = _get_registry(registry)
    if reg is None:
        return False
    try:
        row = reg.get(plat, acct) or {}
        meta = row.get("meta") or {}
        if bool(meta.get("identity_pending")):
            return False
        confirmed_prev = sorted(
            str(p) for p in (meta.get("identity_confirmed_prev") or []))
        if confirmed_prev and set(prev) <= set(confirmed_prev):
            return False
        now = time.time()
        reg.upsert(plat, acct, meta={
            "identity_pending": True,
            "identity_prev_accounts": prev,
            "identity_changed_at": now,
        }, merge_meta=True)
        invalidate_pending_cache(plat, acct)
        logger.warning(
            "[account_identity] 登录身份变更：%s:%s 接替了 %s（login=%s）→ "
            "进入隔离态（不补人设 / 自动化封顶 review），待人工确认",
            plat, acct, ",".join(prev), login_id or "?")
        try:
            from src.ops.ops_events import get_ops_event_store
            st = get_ops_event_store()
            if st is not None:
                st.record(
                    "identity_change", account_id=acct, platform=plat,
                    reason="quarantined",
                    detail=f"prev={','.join(prev)};login={login_id or ''}"[:300],
                )
        except Exception:
            logger.debug("[account_identity] ops 事件审计失败（忽略）",
                         exc_info=True)
        try:
            from src.ops.ops_alert import notify
            notify(
                "account_identity_changed",
                f"🪪 {plat}:{acct} 用原属 {','.join(prev)} 的登录位完成了登录"
                "（账号身份变更）。已进入隔离态：AI 只拟稿不自发、不自动挂人设；"
                "请在确认身份与人设后转正（/api/admin/account-identity/confirm）。",
                account_id=f"{plat}:{acct}", reason="identity_changed")
        except Exception:
            logger.debug("[account_identity] ops 告警失败（忽略）", exc_info=True)
        return True
    except Exception:
        logger.debug("[account_identity] 隔离打标失败（忽略）", exc_info=True)
        return False


def resolve_login_identity(
    platform: str, account_id: str, login_id: str, *,
    registry: Any = None, extra_prev: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """登录路径唯一汇入点：登记映射 → 合并换人证据 → 需要时进隔离态。

    ``extra_prev``：调用方另行掌握的前任证据（如 session_health 的 superseded
    清单——注册表映射漏写的存量场景全靠它兜底）。返回
    ``{"changed": bool, "prev_accounts": [...], "quarantined": bool}``。
    """
    ident = record_login_identity(
        platform, account_id, login_id, registry=registry)
    prev = list(dict.fromkeys(
        [*(ident.get("prev_accounts") or []),
         *(str(p) for p in (extra_prev or []) if str(p or "").strip())]))
    prev = [p for p in prev if p and p != str(account_id or "")]
    quarantined = False
    if prev:
        quarantined = quarantine_account(
            platform, account_id, prev, registry=registry, login_id=login_id)
    return {"changed": bool(prev), "prev_accounts": prev,
            "quarantined": quarantined}


def confirm_account_identity(
    platform: str, account_id: str, *,
    persona_id: str = "", registry: Any = None,
    config: Optional[Dict[str, Any]] = None, actor: str = "",
) -> Dict[str, Any]:
    """显式转正：清 pending、显式绑人设（或此时才补默认）、落审计。

    ``persona_id`` 非空 → 覆盖绑定该人设；空 → 若账号无绑定则按默认人设补挂
    （此刻是人做的决定，auto-attach 的禁令不再适用）。
    返回 ``{"ok": bool, "persona_id": str}``。
    """
    plat = str(platform or "").strip().lower()
    acct = str(account_id or "").strip()
    reg = _get_registry(registry)
    if not plat or not acct or reg is None:
        return {"ok": False, "persona_id": ""}
    try:
        row = reg.get(plat, acct)
        if not row:
            return {"ok": False, "persona_id": ""}
        meta = row.get("meta") or {}
        prev = [str(p) for p in (meta.get("identity_prev_accounts") or [])]
        patch: Dict[str, Any] = {
            "identity_pending": False,
            "identity_confirmed_at": time.time(),
            "identity_confirmed_by": str(actor or "")[:80],
            "identity_confirmed_prev": prev,
        }
        pid = str(persona_id or "").strip()
        if pid:
            patch["persona_id"] = pid
            patch["persona_ids"] = [pid]
        reg.upsert(plat, acct, meta=patch, merge_meta=True)
        invalidate_pending_cache(plat, acct)
        eff = pid
        if not pid:
            try:
                from src.ai.persona_voice import ensure_account_default_persona
                eff = ensure_account_default_persona(reg, plat, acct, config)
            except Exception:
                eff = ""
        try:
            from src.ops.ops_events import get_ops_event_store
            st = get_ops_event_store()
            if st is not None:
                st.record(
                    "identity_change", account_id=acct, platform=plat,
                    reason="confirmed",
                    detail=f"persona={eff or '(none)'};by={actor or ''}"[:300],
                )
        except Exception:
            logger.debug("[account_identity] 转正审计失败（忽略）", exc_info=True)
        logger.info("[account_identity] %s:%s 身份已转正 persona=%s by=%s",
                    plat, acct, eff or "(none)", actor or "?")
        return {"ok": True, "persona_id": eff}
    except Exception:
        logger.debug("[account_identity] 转正失败", exc_info=True)
        return {"ok": False, "persona_id": ""}


def pending_accounts(registry: Any = None) -> List[Dict[str, Any]]:
    """全平台「身份待确认」账号清单（管理 API / 看板用），绝不抛。"""
    reg = _get_registry(registry)
    if reg is None:
        return []
    out: List[Dict[str, Any]] = []
    try:
        for row in (reg.list(include_removed=False) or []):
            if not is_identity_pending_row(row):
                continue
            meta = row.get("meta") or {}
            out.append({
                "platform": str(row.get("platform") or ""),
                "account_id": str(row.get("account_id") or ""),
                "status": str(row.get("status") or ""),
                "self_name": str(meta.get("self_name") or ""),
                "prev_accounts": [
                    str(p) for p in (meta.get("identity_prev_accounts") or [])],
                "changed_at": float(meta.get("identity_changed_at") or 0.0),
                "persona_id": str(meta.get("persona_id") or ""),
            })
    except Exception:
        logger.debug("[account_identity] pending 清单读取失败", exc_info=True)
    return out


def invalidate_fleet_candidates_cache() -> None:
    """清「疑似自有号未登记」候选缓存（配置写路径 / 测试用）。"""
    with _fleet_cand_lock:
        _fleet_cand_cache.clear()


def handover_context(
    platform: str, account_id: str, *,
    registry: Any = None, inbox_store: Any = None,
    followup_limit: int = 30,
) -> Dict[str, Any]:
    """换号顶岗向导上下文（实施72 P2-1；只读一次取齐，绝不抛）。

    给 ops 向导弹层供料：目标账号现状 + 每个前任账号的注册表行 + 前任账号
    **最近私聊会话**（＝「跟进中联系人」建议清单的原料——旧号换走后这些客户
    正在等一个再也不会回话的号）。会话只列不迁：交接是人勾选进报告的决定，
    不是数据搬运（conversation 归属改写刻意不做，见 §5.4g）。

    返回 ``{"ok": False}``（账号不存在/注册表不可用）或
    ``{"ok": True, platform, account_id, pending, self_name, persona_id,
       prev: [{account_id, exists, status, self_name, persona_id,
               followups: [{chat_key, display_name, last_ts, unread, last_text}]}]}``。
    """
    plat = str(platform or "").strip().lower()
    acct = str(account_id or "").strip()
    reg = _get_registry(registry)
    if not plat or not acct or reg is None:
        return {"ok": False}
    try:
        row = reg.get(plat, acct)
        if not row:
            return {"ok": False}
        meta = row.get("meta") or {}
        prev_ids = [str(p) for p in (meta.get("identity_prev_accounts") or [])
                    if str(p or "").strip() and str(p) != acct]
        prev: List[Dict[str, Any]] = []
        for pid_ in prev_ids:
            entry: Dict[str, Any] = {"account_id": pid_, "exists": False,
                                     "status": "", "self_name": "",
                                     "persona_id": "", "followups": []}
            try:
                prow = reg.get(plat, pid_)
                if prow:
                    pmeta = prow.get("meta") or {}
                    entry.update({
                        "exists": True,
                        "status": str(prow.get("status") or ""),
                        "self_name": str(pmeta.get("self_name") or ""),
                        "persona_id": str(pmeta.get("persona_id") or ""),
                    })
            except Exception:
                pass
            # 跟进建议＝旧号最近私聊（会话在 inbox 库里与注册表行生死无关，
            # 行被硬删也照样列得出来）
            try:
                if inbox_store is not None:
                    for c in (inbox_store.list_conversations(
                            limit=max(1, int(followup_limit)),
                            platform=plat, account_id=pid_) or []):
                        if str(c.get("chat_type") or "private") not in (
                                "private", ""):
                            continue
                        ck = str(c.get("chat_key") or "").strip()
                        if not ck or ck in ("me", pid_):
                            continue
                        entry["followups"].append({
                            "chat_key": ck,
                            "display_name": str(c.get("display_name") or ""),
                            "last_ts": float(c.get("last_ts") or 0.0),
                            "unread": int(c.get("unread") or 0),
                            "last_text": str(c.get("last_text") or "")[:80],
                        })
            except Exception:
                logger.debug("[account_identity] 跟进清单读取失败（忽略）",
                             exc_info=True)
            prev.append(entry)
        return {
            "ok": True, "platform": plat, "account_id": acct,
            "pending": bool(meta.get("identity_pending")),
            "self_name": str(meta.get("self_name") or ""),
            "persona_id": str(meta.get("persona_id") or ""),
            "prev": prev,
        }
    except Exception:
        logger.debug("[account_identity] 向导上下文读取失败", exc_info=True)
        return {"ok": False}


def _archive_account_row(reg: Any, platform: str, account_id: str) -> bool:
    """归档一行（软删）：优先走 registry.remove（含头像回收/凭据池归还），
    假件/旧接口回落 upsert(status=removed)。返回是否执行了归档。"""
    try:
        if hasattr(reg, "remove"):
            reg.remove(platform, account_id)
        else:
            reg.upsert(platform, account_id, status="removed")
        return True
    except Exception:
        logger.debug("[account_identity] 归档 %s:%s 失败（忽略）",
                     platform, account_id, exc_info=True)
        return False


def execute_handover(
    platform: str, account_id: str, *,
    persona_id: str = "", archive: Optional[List[str]] = None,
    followups: Optional[List[Dict[str, Any]]] = None,
    note: str = "", actor: str = "",
    registry: Any = None, config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """换号顶岗向导终步（实施72 P2-1 唯一写入口）。

    组合语义＝「转正 + 归档旧号 + 交接报告」，其中**转正复用**
    ``confirm_account_identity``（单写入口纪律：清 pending / 绑人设 / 落
    confirmed 审计一处不漏）；转正失败＝整体失败，后续不执行。

    - ``archive``：要归档的旧号（**必须 ⊆ 该行 identity_prev_accounts**——
      本端点不是通用删号口，越界 id 记 ``archive_skipped``；已 removed 的
      跳过）；归档＝软删，历史查档照常。
    - ``followups``：人勾选的「需新号跟进」联系人（只进报告，**不迁移任何
      会话数据**）；每条消毒为 {chat_key, display_name, last_ts, note}，
      上限 100。
    - 报告：全文落 ``<数据根>/logs/handover/*.json``（best-effort）+
      ops_events ``identity_change/handover`` 摘要行（重启不清零）；全文
      同时随响应返回（前端触发下载）。
    """
    plat = str(platform or "").strip().lower()
    acct = str(account_id or "").strip()
    reg = _get_registry(registry)
    if not plat or not acct or reg is None:
        return {"ok": False, "error": "bad_args"}
    try:
        row = reg.get(plat, acct)
        if not row:
            return {"ok": False, "error": "not_found"}
        prev_ids = [str(p) for p in ((row.get("meta") or {})
                                     .get("identity_prev_accounts") or [])]
        new_name = str(((row.get("meta") or {}).get("self_name")) or "")

        res = confirm_account_identity(
            plat, acct, persona_id=persona_id, registry=reg,
            config=config, actor=actor)
        if not res.get("ok"):
            return {"ok": False, "error": "confirm_failed"}
        eff_persona = str(res.get("persona_id") or "")

        archived: List[str] = []
        archive_skipped: List[Dict[str, str]] = []
        for aid in (archive or []):
            aid = str(aid or "").strip()
            if not aid or aid == acct:
                continue
            if aid not in prev_ids:
                archive_skipped.append({"account_id": aid, "reason": "not_prev"})
                continue
            try:
                arow = reg.get(plat, aid)
            except Exception:
                arow = None
            if not arow:
                archive_skipped.append({"account_id": aid, "reason": "missing"})
                continue
            if str(arow.get("status") or "") == "removed":
                archive_skipped.append({"account_id": aid,
                                        "reason": "already_removed"})
                continue
            if _archive_account_row(reg, plat, aid):
                archived.append(aid)
            else:
                archive_skipped.append({"account_id": aid, "reason": "error"})

        fu_clean: List[Dict[str, Any]] = []
        for f in (followups or [])[:100]:
            try:
                ck = str((f or {}).get("chat_key") or "").strip()
                if not ck:
                    continue
                fu_clean.append({
                    "chat_key": ck,
                    "display_name": str((f or {}).get("display_name") or "")[:80],
                    "last_ts": float((f or {}).get("last_ts") or 0.0),
                    "note": str((f or {}).get("note") or "")[:200],
                })
            except Exception:
                continue

        now = time.time()
        report: Dict[str, Any] = {
            "kind": "account_handover",
            "ts": now,
            "platform": plat,
            "new_account": {"account_id": acct, "self_name": new_name,
                            "persona_id": eff_persona},
            "prev_accounts": prev_ids,
            "archived": archived,
            "archive_skipped": archive_skipped,
            "followups": fu_clean,
            "note": str(note or "")[:1000],
            "actor": str(actor or "")[:80],
        }

        report_file = ""
        try:
            import json as _json
            from src.licensing.data_paths import config_dir
            safe = "".join(ch for ch in acct if ch.isalnum() or ch in "-_")[:40]
            d = config_dir().parent / "logs" / "handover"
            d.mkdir(parents=True, exist_ok=True)
            p = d / (f"handover_{plat}_{safe or 'acct'}_"
                     f"{time.strftime('%Y%m%d_%H%M%S', time.localtime(now))}.json")
            p.write_text(_json.dumps(report, ensure_ascii=False, indent=2),
                         encoding="utf-8")
            report_file = p.name
        except Exception:
            logger.debug("[account_identity] 交接报告落盘失败（忽略）",
                         exc_info=True)
        try:
            from src.ops.ops_events import get_ops_event_store
            st = get_ops_event_store()
            if st is not None:
                st.record(
                    "identity_change", account_id=acct, platform=plat,
                    reason="handover",
                    detail=(f"persona={eff_persona or '(none)'};"
                            f"archived={','.join(archived) or '-'};"
                            f"followups={len(fu_clean)};"
                            f"file={report_file or '-'};by={actor or ''}")[:300],
                )
        except Exception:
            logger.debug("[account_identity] 交接审计失败（忽略）", exc_info=True)
        logger.info(
            "[account_identity] %s:%s 换号交接完成 persona=%s archived=%s "
            "followups=%d by=%s", plat, acct, eff_persona or "(none)",
            ",".join(archived) or "-", len(fu_clean), actor or "?")
        return {"ok": True, "persona_id": eff_persona, "archived": archived,
                "archive_skipped": archive_skipped, "report": report,
                "report_file": report_file}
    except Exception:
        logger.debug("[account_identity] 换号交接失败", exc_info=True)
        return {"ok": False, "error": "internal"}


def fleet_candidates(
    config: Optional[Dict[str, Any]] = None, *,
    registry: Any = None, inbox_store: Any = None,
) -> List[Dict[str, Any]]:
    """「疑似自有号未登记」候选清单（实施72 下一阶段 P1；身份卡观测面）。

    对端命中本机注册表 **removed 归档账号**（账密迁去别机的痕迹，Calixa 原型）
    却没登记 ``companion.own_fleet.extra`` → 提示行。判定纯函数在
    ``proactive_peer_hygiene.own_fleet_candidates``（刻意只匹 removed——现役
    同库对子是「老板扮客户测全自动」常态流，提示登记反而诱导误封）。

    只读 + 300s TTL 进程缓存（ops 轮询不反复扫会话表）；快路径＝注册表里
    **没有** removed 行时零会话扫描。任何异常回空表，绝不影响 pending 主体。
    """
    now = time.time()
    with _fleet_cand_lock:
        hit = _fleet_cand_cache.get("v")
        if hit is not None and (now - hit[1]) < _FLEET_CAND_TTL:
            return list(hit[0])
    out: List[Dict[str, Any]] = []
    try:
        reg = _get_registry(registry)
        rows = list(reg.list(include_removed=True) or []) if reg is not None else []
        removed_plats = sorted({
            str(r.get("platform") or "").strip().lower()
            for r in rows
            if str(r.get("status") or "").strip().lower() == "removed"
            and str(r.get("platform") or "").strip()
        })
        if removed_plats and inbox_store is not None:
            convs: List[Dict[str, Any]] = []
            for plat in removed_plats:
                try:
                    convs.extend(inbox_store.list_conversations(
                        limit=500, platform=plat) or [])
                except Exception:
                    continue
            from src.companion.proactive_peer_hygiene import (
                own_fleet_candidates,
            )
            out = own_fleet_candidates(rows, convs, config)
    except Exception:
        logger.debug("[account_identity] 自有号候选检测失败（忽略）",
                     exc_info=True)
        out = []
    with _fleet_cand_lock:
        _fleet_cand_cache["v"] = (list(out), now)
    return out


__all__ = [
    "cached_account_meta",
    "confirm_account_identity",
    "execute_handover",
    "fleet_candidates",
    "handover_context",
    "identity_pending",
    "invalidate_fleet_candidates_cache",
    "invalidate_pending_cache",
    "is_identity_pending_row",
    "login_meta_key",
    "pending_accounts",
    "quarantine_account",
    "record_login_identity",
    "resolve_login_identity",
]
