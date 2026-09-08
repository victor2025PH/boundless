# -*- coding: utf-8 -*-
"""停联 / 自伤硬停（O-1 A · #252 #253 · D-O1，2026-09-08）。

事故（XAM4KV，WhatsApp 09-07 21:00–21:04）：客户三次要求停联（please stop →
stop writing to me → Never write me again），系统在 ``shadow=stop_contact risk=high``
下仍连发两条「I hear you, and I'll stop here… Take care.」——09-04「L3/L4 全放行
只记录」（#160 v2）没有豁免项，而放行的初衷是敏感词误伤，不是这个。

本模块＝硬停的**状态与文案**（决策本身在 ``autosend_policy.decide``，那是唯一入口）：

- ``frozen_reason(store, cid)``：会话是否已冻结（``stop_contact`` / ``self_harm`` / 空）。
  冻结＝列表标签「客户要求停联」在场，或「需人工」标的 ``handoff_meta.reason`` 属
  :data:`FREEZE_REASONS`。**只读**，绝不抛。
- ``freeze_conversation(...)``：一次冻结＝① ``tag_needs_human``（与 R8 危机桥同一入口，
  reason=stop_contact/self_harm，非自动摘除类）② 列表标签「客户要求停联」③ 会话档位
  按 ``manual``（source ``guard:<reason>_from:<原档>``——B 线拟稿 / A 线直发 / 主动触达 /
  目标引擎全部认这一个闸，无需各处再查标志）④ 案例跟进落一条 + 通知中心一条
  ⑤ 日志 ``[stop-contact] conv=… action=frozen``。幂等：重复冻结只补缺的那几件。
- ``unfreeze_conversation(...)``：人工解冻＝摘标签 + 摘「需人工」+ 档位还原到冻结前
  原档（只认自己写的 source；坐席期间自己改过档位一律不动）。
- ``farewell_text(lang)``：唯一允许出站的那一条告别——人设口吻、一句话、不解释是
  AI、不承诺「助理会联系」、无客服腔（Take care / I hear you 皆禁）。按会话语言查表，
  查不到回英文。
- ``FAREWELL_MARK`` / ``HARD_STOP_PASS_MARK`` / ``is_hard_stop_pass_draft(row)``：「最多
  一条」放行稿在草稿行 ``risk_reasons`` 里的标记（告别稿两者都带；self_harm 那一句陪伴只带
  后者）——AutosendWorker 对冻结会话只放行带标记的 L2 稿，其余一律取消。

⛔ 词表不在这里：停联判定词表是 ``src/ai/chat_assistant_service._STOP_CONTACT_*``
（``quick_analyze`` → ``risk_reasons`` 含 ``stop_contact``），本模块只消费结果。
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

#: 冻结原因（＝``autosend_policy.HARD_STOP_REASONS``，两处必须同值；测试钉住）
FREEZE_REASONS = ("stop_contact", "self_harm")
#: 会话列表标签（数据值；与「需人工」HANDOFF_TAG 并列，坐席在标签面板可见可摘）
STOP_CONTACT_TAG = "客户要求停联"
#: 告别稿标记（写进草稿行 risk_reasons；worker 据此放行「唯一一条告别」）
FAREWELL_MARK = "farewell:stop_contact"
#: 硬停「最多一条」放行标记（risk_reasons）：冻结当刻产出的那一条稿——stop_contact 是
#: 告别模板（同时带 FAREWELL_MARK），self_harm 是 AI 那一句陪伴。worker / enrich 对冻结
#: 会话只放行带此标记的稿，其余一律取消 / 人审。
HARD_STOP_PASS_MARK = "hard_stop_pass"
#: 档位来源前缀：``guard:stop_contact_from:auto_ai``——``guard:`` 家族让 snooze /
#: takeover 的还原逻辑对它视而不见（那两处只认自己写的 source），``_from:`` 编码原档
#: 供 :func:`unfreeze_conversation` 还原（与 snooze_hold 同一编码术，零 schema）。
_MODE_SOURCE_PREFIX = "guard:"
_MODE_SOURCE_FROM = "_from:"

# 告别文案（一句、第一人称、口语；禁：AI / 助理 / Take care / I hear you / 条件句收尾）。
# 键＝normalize 后的语言码；查不到 → en。
_FAREWELL: Dict[str, str] = {
    "en": "Okay, I won't message you again.",
    "zh": "好，我不会再打扰你了。",
    "zh-tw": "好，我不會再打擾你了。",
    "ja": "わかった、もう連絡しないね。",
    "ko": "알겠어, 더는 연락 안 할게.",
    "th": "โอเค เราจะไม่ทักไปอีกแล้วนะ",
    "vi": "Được rồi, mình sẽ không nhắn nữa.",
    "id": "Oke, aku nggak akan chat kamu lagi.",
    "ms": "Okay, saya tak akan mesej awak lagi.",
    "es": "Vale, no te vuelvo a escribir.",
    "pt": "Tá bom, não te escrevo mais.",
    "fr": "D'accord, je ne t'écrirai plus.",
    "de": "Okay, ich schreibe dir nicht mehr.",
    "it": "Va bene, non ti scrivo più.",
    "ru": "Хорошо, больше не буду писать.",
    "ar": "حسنًا، لن أراسلك مرة أخرى.",
    "tr": "Tamam, bir daha yazmayacağım.",
}
_ZH_TRAD = ("zh-tw", "zh-hk", "zh-mo", "zh-hant", "yue", "zh_tw", "zh_hk", "zh_hant")


def _norm_lang(lang: Any) -> str:
    s = str(lang or "").strip().lower().replace("_", "-")
    if not s or s == "unknown":
        return "en"
    if s in _ZH_TRAD or s.startswith("zh-hant"):
        return "zh-tw"
    if s.startswith("zh"):
        return "zh"
    return s.split("-", 1)[0]


def farewell_text(lang: Any) -> str:
    """会话语言 → 唯一那条告别（查不到回英文；纯函数）。"""
    return _FAREWELL.get(_norm_lang(lang)) or _FAREWELL["en"]


def farewell_languages() -> List[str]:
    return sorted(_FAREWELL.keys())


def _reasons_of(row: Any) -> List[str]:
    """草稿行 risk_reasons（list / JSON 串 / 逗号串）→ list[str]；绝不抛。"""
    v = (row or {}).get("risk_reasons") if isinstance(row, dict) else None
    if not v:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v if str(x)]
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("["):
            try:
                arr = json.loads(s)
                return [str(x) for x in arr if str(x)] if isinstance(arr, list) else []
            except Exception:
                return []
        return [p.strip() for p in s.split(",") if p.strip()]
    return []


def is_farewell_draft(row: Any) -> bool:
    """草稿行是否「停联告别稿」（risk_reasons 含 :data:`FAREWELL_MARK`）。"""
    try:
        return FAREWELL_MARK in _reasons_of(row)
    except Exception:
        return False


def is_hard_stop_pass_draft(row: Any) -> bool:
    """草稿行是否硬停「最多一条」放行稿（告别稿 或 自伤那一句陪伴）。"""
    try:
        rs = _reasons_of(row)
        return FAREWELL_MARK in rs or HARD_STOP_PASS_MARK in rs
    except Exception:
        return False


def _tags_of(store: Any, cid: str) -> List[str]:
    try:
        return [str(t) for t in (store.get_conv_tags(cid) or [])]
    except Exception:
        return []


def _handoff_meta(store: Any, cid: str) -> Dict[str, Any]:
    if not hasattr(store, "get_handoff_meta"):
        return {}
    try:
        m = store.get_handoff_meta(cid)
        return dict(m) if isinstance(m, dict) else {}
    except Exception:
        return {}


def frozen_reason(store: Any, conversation_id: str) -> str:
    """会话冻结原因：``stop_contact`` / ``self_harm`` / ``""``（未冻结）。只读，绝不抛。

    判据：列表标签「客户要求停联」在场 → stop_contact；否则「需人工」标在场且其
    ``handoff_meta.reason`` ∈ FREEZE_REASONS → 该 reason。坐席摘掉标签＝解冻（见
    :func:`unfreeze_conversation`），AI 恢复与否另看档位（冻结时已按 manual）。
    """
    cid = str(conversation_id or "").strip()
    if store is None or not cid:
        return ""
    try:
        tags = _tags_of(store, cid)
        if STOP_CONTACT_TAG in tags:
            return "stop_contact"
        from src.integrations.protocol_autoreply import HANDOFF_TAG
        if HANDOFF_TAG in tags:
            r = str(_handoff_meta(store, cid).get("reason") or "")
            if r in FREEZE_REASONS:
                return r
    except Exception:
        logger.debug("[stop-contact] frozen_reason 判定异常（按未冻结）", exc_info=True)
    return ""


def _mode_source(reason: str, prev_mode: str) -> str:
    src = f"{_MODE_SOURCE_PREFIX}{reason}"
    if prev_mode and prev_mode != "manual":
        src += f"{_MODE_SOURCE_FROM}{prev_mode}"
    return src[:80]


def is_freeze_mode_source(source: Any) -> str:
    """该档位 source 是否本模块写的；是 → 返回 reason，否 → 空串。"""
    s = str(source or "")
    if not s.startswith(_MODE_SOURCE_PREFIX):
        return ""
    body = s[len(_MODE_SOURCE_PREFIX):]
    reason = body.split(_MODE_SOURCE_FROM, 1)[0]
    return reason if reason in FREEZE_REASONS else ""


def freeze_prev_mode(source: Any) -> str:
    """从本模块写的 source 解出冻结前原档；无 → 空串。"""
    s = str(source or "")
    if not is_freeze_mode_source(s) or _MODE_SOURCE_FROM not in s:
        return ""
    prev = s.split(_MODE_SOURCE_FROM, 1)[1].strip().lower()
    try:
        from src.inbox.store import AUTOMATION_MODES
        return prev if prev in AUTOMATION_MODES else ""
    except Exception:
        return prev


def log_action(action: str, *, conversation_id: str, reason: str = "",
               draft_id: str = "", hits: Iterable[str] = (), extra: str = "") -> None:
    """统一日志行 ``[stop-contact] conv=… action=farewell|frozen|skipped|review …``。"""
    try:
        hs = "|".join(str(h) for h in list(hits or [])[:4]) or "-"
        logger.info(
            "[stop-contact] conv=%s action=%s reason=%s draft=%s hits=%s%s",
            conversation_id or "-", action, reason or "-", draft_id or "-", hs,
            (" " + extra) if extra else "")
    except Exception:
        pass


def freeze_conversation(
    store: Any,
    *,
    platform: str,
    account_id: str,
    chat_key: str,
    conversation_id: str = "",
    reason: str = "stop_contact",
    hits: Iterable[str] = (),
    chat_name: str = "",
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """冻结会话（幂等、best-effort、绝不抛）。返回各落点结果供日志/测试。

    落点：需人工（tag_needs_human 同一入口）→ 列表标签 → 档位 manual → 案例跟进 +
    通知中心。任一落点失败不影响其余（安全动作宁多做不少做）。
    """
    reason = reason if reason in FREEZE_REASONS else "stop_contact"
    ts = float(now if now is not None else time.time())
    hits_l = [str(h)[:40] for h in list(hits or []) if str(h)][:6]
    cid = str(conversation_id or "").strip()
    if not cid:
        try:
            from src.inbox.normalizer import conv_id as _conv_id
            cid = _conv_id(str(platform or ""), str(account_id or ""), str(chat_key or ""))
        except Exception:
            cid = ""
    out: Dict[str, Any] = {
        "conversation_id": cid, "reason": reason, "tagged": False, "labelled": False,
        "mode_set": False, "prev_mode": "", "case": False, "notified": False,
        "already_frozen": False,
    }
    if store is None or not cid:
        out["error"] = "no_store_or_cid"
        return out
    out["already_frozen"] = frozen_reason(store, cid) == reason
    try:
        from src.integrations.protocol_autoreply import HANDOFF_TAG as _handoff_tag
    except Exception:
        _handoff_tag = "需人工"

    # ① 需人工（与 R8 危机桥同一入口）。已带别的标 / 调用方传入的 cid 与 normalizer 算出的
    #    不一致（测试假件）→ 再按给定 cid 强制把 handoff_meta.reason 写成冻结原因。
    try:
        from src.integrations.protocol_autoreply import tag_needs_human
        payload = {"platform": platform, "account_id": account_id, "chat_key": chat_key}
        tagged = bool(tag_needs_human(store, payload, reason=reason, source="system", now=ts))
        if hasattr(store, "set_handoff_meta"):
            meta = _handoff_meta(store, cid)
            if str(meta.get("reason") or "") not in FREEZE_REASONS:
                store.set_handoff_meta(cid, {"reason": reason, "ts": ts, "source": "system",
                                             "hits": hits_l})
                tagged = True
        out["tagged"] = tagged
    except Exception:
        logger.debug("[stop-contact] tag_needs_human 失败", exc_info=True)

    # ② 列表标签「客户要求停联」（自伤不打这个标：语义不对，需人工 + 危机链已有）
    try:
        tags = _tags_of(store, cid)
        want = list(tags)
        if _handoff_tag not in want:
            want.append(_handoff_tag)
        if reason == "stop_contact" and STOP_CONTACT_TAG not in want:
            want.append(STOP_CONTACT_TAG)
        if want != tags:
            store.set_conv_tags(cid, want)
            out["labelled"] = True
    except Exception:
        logger.debug("[stop-contact] 列表标签写入失败", exc_info=True)

    # ③ 档位 manual（全链共认的闸）；原档编码进 source 供解冻还原
    try:
        prev = ""
        if hasattr(store, "get_automation_mode_if_set"):
            prev = str(store.get_automation_mode_if_set(cid) or "")
        out["prev_mode"] = prev
        if prev != "manual":
            src = _mode_source(reason, prev)
            try:
                store.set_automation_mode(cid, "manual", source=src)
            except TypeError:
                store.set_automation_mode(cid, "manual")
            out["mode_set"] = True
    except Exception:
        logger.debug("[stop-contact] 档位写入失败", exc_info=True)

    # ④ 案例跟进一条 + 通知中心一条（首次冻结才发，重复冻结不刷屏）
    if not out["already_frozen"]:
        try:
            if hasattr(store, "record_escalation"):
                out["case"] = bool(store.record_escalation(
                    cid, reason=reason, agent_id="", agent_name="system",
                    wait_sec=0, dedup_sec=3600.0, ts=ts))
        except Exception:
            logger.debug("[stop-contact] record_escalation 失败", exc_info=True)
        try:
            from src.integrations.shared.event_bus import get_event_bus
            name = str(chat_name or chat_key or "")
            get_event_bus().publish("escalation", {
                "conversation_id": cid, "platform": str(platform or ""),
                "account_id": str(account_id or ""), "chat_key": str(chat_key or ""),
                "name": name, "chat_name": name, "display_name": name,
                "reason": reason, "risk_hits": hits_l,
                "agent_id": "", "agent_name": "system", "wait_sec": 0,
                "assigned_to": "", "ts": ts,
            })
            out["notified"] = True
        except Exception:
            logger.debug("[stop-contact] 通知发布失败", exc_info=True)

    # ⑥ P-2 E（D-P4）：账号级停联名单——会话级标记随重装 / 删会话即丢，名单只增不删、
    #    随账号迁移包导出导入、登录同步后扫描复用。只记 stop_contact（自伤不是「别联系我」）。
    out["blocklisted"] = False
    if reason == "stop_contact":
        try:
            from src.inbox.account_blocklist import get_blocklist
            _plat, _acct, _peer = _split_cid(cid, platform, account_id, chat_key)
            if _plat and _acct and _peer:
                get_blocklist(store).add(
                    _plat, _acct, _peer, reason=reason,
                    hit_text="|".join(hits_l), ts=ts, source="freeze")
                out["blocklisted"] = True
        except Exception:
            logger.debug("[stop-contact] 名单写入失败", exc_info=True)

    log_action(
        "frozen" if not out["already_frozen"] else "frozen_again",
        conversation_id=cid, reason=reason, hits=hits_l,
        extra=f"prev_mode={out['prev_mode'] or '-'} mode_set={out['mode_set']} "
              f"tagged={out['tagged']} labelled={out['labelled']} "
              f"blocklisted={out['blocklisted']}")
    return out


def _split_cid(cid: str, platform: str = "", account_id: str = "",
               chat_key: str = "") -> tuple:
    """(platform, account_id, peer)：显式参数优先，缺的从 ``platform:account:peer`` 解。"""
    plat, acct, peer = str(platform or ""), str(account_id or ""), str(chat_key or "")
    if (not plat or not acct or not peer) and cid:
        parts = str(cid).split(":", 2)
        if len(parts) == 3:
            plat, acct, peer = plat or parts[0], acct or parts[1], peer or parts[2]
    return plat.lower(), acct, peer


def unfreeze_conversation(
    store: Any, conversation_id: str, *, actor: str = "human",
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """人工解冻：摘「客户要求停联」+ 摘「需人工」（仅当其 reason 是冻结原因）+ 档位还原。

    档位只在「当前显式 manual 且 source 是本模块写的」时还原（原档 > 全局默认）；坐席
    期间自己改过档位 / 被别的守卫降档 / 接管态 → 不动（更新的意图优先，与 snooze_hold
    同一纪律）。绝不抛。
    """
    cid = str(conversation_id or "").strip()
    out: Dict[str, Any] = {"conversation_id": cid, "was": "", "unlabelled": False,
                           "untagged": False, "mode_restored": ""}
    if store is None or not cid:
        return out
    out["was"] = frozen_reason(store, cid)
    try:
        tags = _tags_of(store, cid)
        if STOP_CONTACT_TAG in tags:
            store.set_conv_tags(cid, [t for t in tags if t != STOP_CONTACT_TAG])
            out["unlabelled"] = True
    except Exception:
        logger.debug("[stop-contact] 摘标签失败", exc_info=True)
    try:
        if str(_handoff_meta(store, cid).get("reason") or "") in FREEZE_REASONS:
            from src.integrations.protocol_autoreply import clear_needs_human
            out["untagged"] = bool(clear_needs_human(store, cid, actor=actor))
    except Exception:
        logger.debug("[stop-contact] 摘需人工失败", exc_info=True)
    try:
        meta: Dict[str, Any] = {}
        if hasattr(store, "get_automation_mode_meta"):
            meta = dict(store.get_automation_mode_meta(cid) or {})
        src = meta.get("source")
        if (is_freeze_mode_source(src)
                and str(meta.get("mode") or "").lower() == "manual"):
            target = freeze_prev_mode(src)
            if not target:
                try:
                    from src.inbox.automation_mode import global_automation_mode_from_config
                    target = global_automation_mode_from_config(config)
                except Exception:
                    target = ""
            if target and target != "manual":
                try:
                    store.set_automation_mode(cid, target, source=f"unfreeze:{actor}"[:80])
                except TypeError:
                    store.set_automation_mode(cid, target)
                out["mode_restored"] = target
    except Exception:
        logger.debug("[stop-contact] 档位还原失败", exc_info=True)
    # P-2 E：名单行写 unfrozen_ts（**不删行**）——人工解冻是「解冻」标记，历史可查
    out["unfrozen_listed"] = False
    try:
        from src.inbox.account_blocklist import get_blocklist
        _plat, _acct, _peer = _split_cid(cid)
        if _plat and _acct and _peer:
            out["unfrozen_listed"] = bool(get_blocklist(store).mark_unfrozen(
                _plat, _acct, _peer, by=actor))
    except Exception:
        logger.debug("[stop-contact] 名单解冻标记失败", exc_info=True)
    log_action("unfrozen", conversation_id=cid, reason=out["was"],
               extra=f"by={actor} mode_restored={out['mode_restored'] or '-'} "
                     f"listed={out['unfrozen_listed']}")
    return out


def stop_contact_hits(text: str) -> List[str]:
    """文本是否命中停联词表（复用 O-1 A 双语词表：``quick_analyze`` → risk_reasons 含
    stop_contact）。返回命中词（空 = 未命中）。绝不抛。"""
    t = str(text or "").strip()
    if not t:
        return []
    try:
        from src.ai.chat_assistant_service import quick_analyze
        a = quick_analyze(t) or {}
        if "stop_contact" in list(a.get("risk_reasons") or []):
            hits = [str(h) for h in list(a.get("risk_hits") or []) if str(h)]
            return hits or [t[:40]]
    except Exception:
        logger.debug("[stop-contact] 词表判定异常", exc_info=True)
    return []


def login_scan_backfill(
    store: Any, platform: str, account_id: str,
    items: Iterable[Dict[str, Any]], *, now: Optional[float] = None,
) -> Dict[str, Any]:
    """P-2 E（D-P4）：登录同步完成后对回填历史跑一遍停联扫描 + 名单复冻。

    ``items``：本次登录回填的**客户入站**行 ``{conversation_id, chat_key, text, ts, name}``
    （dormant_review 的 settle 收集）。两路：① 文本命中双语词表 → 冻结 + 标签「客户要求停联」
    + 进名单；② 名单里未解冻的 peer 其会话在场但未冻结（重装丢了会话标记）→ 重新冻结。
    日志一行 ``[stop-contact] login_scan account=… hits=N relisted=M``。绝不抛。
    """
    plat = str(platform or "").lower()
    acct = str(account_id or "")
    out: Dict[str, Any] = {"platform": plat, "account_id": acct, "scanned": 0,
                           "hits": 0, "relisted": 0, "frozen": []}
    if store is None or not plat or not acct:
        return out
    try:
        from src.inbox.account_blocklist import get_blocklist
        bl = get_blocklist(store)
    except Exception:
        bl = None
    seen_peers: Dict[str, str] = {}
    for it in items or []:
        try:
            cid = str(it.get("conversation_id") or "")
            peer = str(it.get("chat_key") or "")
            if not peer and cid:
                peer = _split_cid(cid)[2]
            if not cid:
                from src.inbox.normalizer import conv_id as _conv_id
                cid = _conv_id(plat, acct, peer)
            seen_peers.setdefault(peer, cid)
            out["scanned"] += 1
            hits = stop_contact_hits(str(it.get("text") or ""))
            if not hits:
                continue
            out["hits"] += 1
            if frozen_reason(store, cid) != "stop_contact":
                freeze_conversation(
                    store, platform=plat, account_id=acct, chat_key=peer,
                    conversation_id=cid, reason="stop_contact", hits=hits,
                    chat_name=str(it.get("name") or ""), now=now)
                out["frozen"].append(cid)
            elif bl is not None:
                bl.add(plat, acct, peer, reason="stop_contact",
                       hit_text="|".join(hits), ts=now, source="login_scan")
        except Exception:
            logger.debug("[stop-contact] login_scan 单条失败", exc_info=True)
    # ② 名单复冻：名单在场（未解冻）但会话没冻结标记
    if bl is not None:
        try:
            for peer in bl.blocked_peers(plat, acct):
                cid = seen_peers.get(peer)
                if not cid:
                    from src.inbox.normalizer import conv_id as _conv_id
                    cid = _conv_id(plat, acct, peer)
                    try:
                        if hasattr(store, "get_conversation") and not store.get_conversation(cid):
                            continue   # 会话不在本机 → 无需冻结（下次它来消息时 opener 谓词仍挡）
                    except Exception:
                        continue
                if frozen_reason(store, cid) == "stop_contact":
                    continue
                freeze_conversation(
                    store, platform=plat, account_id=acct, chat_key=peer,
                    conversation_id=cid, reason="stop_contact",
                    hits=["blocklist"], now=now)
                out["relisted"] += 1
                if cid not in out["frozen"]:
                    out["frozen"].append(cid)
        except Exception:
            logger.debug("[stop-contact] 名单复冻失败", exc_info=True)
    logger.info("[stop-contact] login_scan account=%s:%s scanned=%d hits=%d relisted=%d",
                plat, acct, out["scanned"], out["hits"], out["relisted"])
    return out


__all__ = [
    "FREEZE_REASONS", "STOP_CONTACT_TAG", "FAREWELL_MARK", "HARD_STOP_PASS_MARK",
    "farewell_text", "farewell_languages", "is_farewell_draft", "is_hard_stop_pass_draft",
    "frozen_reason", "freeze_conversation", "unfreeze_conversation",
    "is_freeze_mode_source", "freeze_prev_mode", "log_action",
    "stop_contact_hits", "login_scan_backfill",
]
