# -*- coding: utf-8 -*-
"""画像槽位统一写口 + 「记忆事实 × 画像槽位」两链合一抽取（Q-5，#263）。

0909 现场「画像永远空」的三段真因（落点表 §〇）：① 两个开关都不在基线；② 记忆抽取链与
画像 LLM 轨各自调一次模型、各自接地、互不知晓；③ 昵称里自报的「名/年龄/城市/职业」
从来没人读。本模块把三件事收成一处：

- **单元 schema（接口约定①，Q-5 写 / Q-1 读）**：``{value, source, status, ts}``，
  ``source ∈ user|nickname|ai_inferred|confirmed``，``status ∈ unknown|mentioned|confirmed``。
  写入时**同时**带旧读者键 ``v`` / ``src``（``profile_slots.slot_value`` / ``facts_line`` /
  ``goal_routes._profile_view`` 仍读 ``v``/``src``）——旧读者零改动照常工作；``cell_view`` 读
  ``value/source/status``。旧形 ``{v, src, ts}`` 与裸字符串读时兼容（裸字符串 = 人录 confirmed）。
- **``apply``**：所有自动写口（AI 推断 / 昵称）都走这里：只填空槽或替换更弱来源；**绝不覆盖
  confirmed**；与已确认值冲突 → 通知中心一条（不打断对话）；每次写入一行 ``[profile]`` 日志。
  AI 推断值**不自动升 confirmed**——升级只在坐席 ✓（``confirm``）。
- **``run_extraction``**：skill_manager 抽取链的**唯一** LLM 调用——一次提示词同时产出记忆事实
  （带引文，走 ``memory_grounding`` 同一护栏）与画像槽位候选（走 ``profile_llm.ground_extracted``
  同一护栏），两路分发：事实回给调用方按既有路径 ``add_fact``；槽位 → ``apply(source=ai_inferred)``。
  ``companion.goals.profile_llm.enabled`` 与 ``memory.extract.use_llm`` **任一开即走 LLM**。
  日志 ``[extract] conv=… facts=n slots=n llm=1|0``。
- **停滞计数（D）**：连续 :data:`STALL_THRESHOLD` 次抽取 ``facts=0 slots=0`` → ``stall_status()``
  给页面（记忆页 / 目标报表 / 目标卡黄条）出「抽取可能未生效」告警行。进程内计数，重启归零。
任何异常零阻断主链。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from src.companion.goals.profile_slots import cell_view, get_slot, slot_label

logger = logging.getLogger("src.companion.goals.profile_fill")

SCHEMA_VERSION = 2

SRC_USER = "user"
SRC_NICKNAME = "nickname"
SRC_AI = "ai_inferred"
SRC_CONFIRMED = "confirmed"
SOURCES = (SRC_USER, SRC_NICKNAME, SRC_AI, SRC_CONFIRMED)

ST_UNKNOWN = "unknown"
ST_MENTIONED = "mentioned"
ST_CONFIRMED = "confirmed"
STATUSES = (ST_UNKNOWN, ST_MENTIONED, ST_CONFIRMED)

# 新 source → 旧读者 ``src``（slot_src / facts_line「(待确认)」/ 目标卡 src-agent 边框）
_LEGACY_SRC = {SRC_USER: "agent", SRC_CONFIRMED: "agent",
               SRC_NICKNAME: "llm_pending", SRC_AI: "llm_pending"}
# 旧 src → 新 source（读旧升新）
_UPGRADE_SRC = {"agent": SRC_USER, "auto": SRC_USER, "": SRC_USER,
                "llm": SRC_AI, "llm_pending": SRC_AI}
_MENTIONED_RANK = {SRC_NICKNAME: 1, SRC_AI: 2}   # mentioned 内部强弱：聊天证据 > 昵称

_MAX_VALUE = 80
_MAX_SLOT_VALUE = 40
_LLM_TIMEOUT = 14.0
_MAX_FACTS = 4

# ── D：停滞计数 ────────────────────────────────────────────────────────────────
STALL_THRESHOLD = 50
_STALL_LOCK = threading.Lock()
_STALL: Dict[str, Any] = {"zero_streak": 0, "runs": 0, "last_ts": 0.0,
                          "last_nonzero_ts": 0.0, "last_llm": None}

# 通知中心落点（health_watchdog 同一 ``app.state.notif_queue`` sys_status 语义）
_APP: Any = None
_NICK_MEMO: Dict[str, str] = {}
_NICK_MEMO_MAX = 5000


# ── 单元 schema ───────────────────────────────────────────────────────────────

def _now() -> float:
    return time.time()


def _norm_val(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip()[:_MAX_VALUE]


def _same(a: str, b: str) -> bool:
    return _norm_val(a).casefold() == _norm_val(b).casefold()


def make_cell(value: Any, source: str, status: Optional[str] = None, *,
              ts: Optional[float] = None, evidence: str = "") -> Dict[str, Any]:
    """组一个接口约定①单元（含旧读者兼容键）。``status`` 缺省：user/confirmed → confirmed，
    nickname/ai_inferred → mentioned。"""
    src = str(source or "").strip().lower()
    if src not in SOURCES:
        src = SRC_AI
    st = str(status or "").strip().lower()
    if st not in STATUSES:
        st = ST_CONFIRMED if src in (SRC_USER, SRC_CONFIRMED) else ST_MENTIONED
    v = _norm_val(value)
    n = float(ts if ts is not None else _now())
    cell: Dict[str, Any] = {
        "value": v, "source": src, "status": st, "ts": n,
        # 旧读者键（profile_slots.slot_value / slot_src / facts_line / _profile_view）
        "v": v, "src": _LEGACY_SRC.get(src, "llm_pending"),
    }
    ev = _norm_val(evidence)
    if ev:
        cell["evidence"] = ev
    return cell


def normalize_cell(cell: Any, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """读旧升新：裸字符串 → ``{value, source:user, status:confirmed}``；旧形 ``{v, src, ts}`` →
    按 ``cell_view`` 语义映射（agent/auto → user·confirmed；llm/llm_pending → ai_inferred·mentioned）；
    已是新形原样（补齐缺的兼容键）。空值 → None。绝不抛。"""
    try:
        if cell is None:
            return None
        if isinstance(cell, dict):
            val, src, st = cell_view(cell)
            if not val:
                return None
            if cell.get("source") in SOURCES and cell.get("status") in STATUSES:
                out = dict(cell)
                out["value"] = val
                out.setdefault("v", val)
                out.setdefault("src", _LEGACY_SRC.get(str(cell.get("source")), "llm_pending"))
                out.setdefault("ts", float(now if now is not None else _now()))
                return out
            new_src = _UPGRADE_SRC.get(str(src or "").lower(), SRC_AI if st == ST_MENTIONED else SRC_USER)
            new_st = ST_CONFIRMED if st == ST_CONFIRMED else ST_MENTIONED
            try:
                ts = float(cell.get("ts") or 0) or float(now if now is not None else _now())
            except (TypeError, ValueError):
                ts = float(now if now is not None else _now())
            return make_cell(val, new_src, new_st, ts=ts)
        if isinstance(cell, bool):
            return None
        if isinstance(cell, (str, int, float)):
            val = _norm_val(cell)
            return make_cell(val, SRC_USER, ST_CONFIRMED, ts=now) if val else None
    except Exception:
        logger.debug("normalize_cell failed", exc_info=True)
    return None


def is_new_schema(cell: Any) -> bool:
    return (isinstance(cell, dict) and cell.get("source") in SOURCES
            and cell.get("status") in STATUSES and "value" in cell)


def upgrade_fields(fields: Any, *, now: Optional[float] = None) -> Tuple[Dict[str, Any], int]:
    """整行 fields → 新 schema；返回 ``(fields, 升级了几格)``。未知键原样保留。"""
    out: Dict[str, Any] = {}
    n = 0
    for k, cell in (fields or {}).items() if isinstance(fields, dict) else []:
        if get_slot(str(k)) is None:
            out[k] = cell
            continue
        nc = normalize_cell(cell, now=now)
        if nc is None:
            continue
        if not is_new_schema(cell):
            n += 1
        out[str(k)] = nc
    return out, n


def export_profile(fields: Any, *, now: Optional[float] = None) -> Dict[str, Any]:
    """导出 / 备份形状：``{"schema_version": 2, "fields": {...新形...}}``。"""
    up, _n = upgrade_fields(fields, now=now)
    return {"schema_version": SCHEMA_VERSION, "fields": up}


def import_profile(payload: Any, *, now: Optional[float] = None) -> Dict[str, Any]:
    """导入：认 v2 信封 ``{schema_version, fields}``、裸 fields dict（v1 旧形 / 裸字符串）。
    一律升到新形。坏输入 → {}。"""
    try:
        if isinstance(payload, dict) and "fields" in payload and isinstance(payload.get("fields"), dict) \
                and ("schema_version" in payload or len(payload) <= 3):
            fields = payload["fields"]
        elif isinstance(payload, dict):
            fields = payload
        else:
            return {}
        up, _n = upgrade_fields(fields, now=now)
        return up
    except Exception:
        return {}


# ── 通知中心（冲突只通知不打断）────────────────────────────────────────────────

def bind_app(app: Any) -> None:
    """路由装配时把 FastAPI app 交进来（通知中心队列挂 ``app.state.notif_queue``）。"""
    global _APP
    _APP = app


def _t(key: str, lang: str = "zh", **fmt: Any) -> str:
    try:
        from src.web.web_i18n import t as _tt
        s = _tt(key, lang)
    except Exception:
        s = key
    if fmt:
        try:
            s = s.format(**fmt)
        except Exception:
            pass
    return s


def notify_conflict(platform: str, chat_key: str, slot: str, confirmed: str, candidate: str,
                    *, source: str = SRC_AI, lang: str = "zh", app: Any = None) -> bool:
    """「画像冲突：职业 已确认 X，AI 推断 Y」→ 工作台通知中心（sys_status，按 id 合并）。
    无 app / 队列 → False。任何异常吞掉。"""
    a = app if app is not None else _APP
    if a is None:
        return False
    try:
        state = getattr(a, "state", a)
        nq = getattr(state, "notif_queue", None)
        if nq is None:
            nq = []
            state.notif_queue = nq
        sid = f"profile_conflict:{platform}:{chat_key}:{slot}"[:64]
        text = _t("inbox.goal.profile.conflict_notice", lang,
                  label=slot_label(slot, lang), confirmed=str(confirmed or "")[:30],
                  candidate=str(candidate or "")[:30])
        if source == SRC_NICKNAME:
            text = _t("inbox.goal.profile.conflict_notice_nick", lang,
                      label=slot_label(slot, lang), confirmed=str(confirmed or "")[:30],
                      candidate=str(candidate or "")[:30])
        nq[:] = [n for n in nq
                 if not ((n or {}).get("type") == "sys_status"
                         and str(((n or {}).get("data") or {}).get("id") or "") == sid)]
        nq.append({"type": "sys_status",
                   "data": {"id": sid, "text": text[:300], "source": "profile_fill",
                            "conversation_id": f"{platform}::{chat_key}"},
                   "_notif_ts": int(time.time() * 1000)})
        if len(nq) > 200:
            del nq[:-200]
        return True
    except Exception:
        logger.debug("notify_conflict failed", exc_info=True)
        return False


# ── 写口 ──────────────────────────────────────────────────────────────────────

def _log_profile(conv: str, slot: str, value: str, source: str, status: str) -> None:
    logger.info("[profile] conv=%s slot=%s value=%s source=%s status=%s",
                conv or "-", slot, str(value or "")[:40], source, status)


def _conv_label(platform: str, chat_key: str, conversation_id: str = "") -> str:
    return str(conversation_id or "").strip() or f"{platform}:{chat_key}"


def _cand_pair(cand: Any) -> Tuple[str, str]:
    if isinstance(cand, dict):
        return _norm_val(cand.get("value")), _norm_val(cand.get("evidence"))
    return _norm_val(cand), ""


def apply(store: Any, platform: str, chat_key: str, candidates: Any, *,
          source: str, status: str = ST_MENTIONED, conversation_id: str = "",
          now: Optional[float] = None, lang: str = "zh", notify: bool = True) -> Dict[str, Any]:
    """候选 ``{slot: value | {"value", "evidence"}}`` → 画像。返回
    ``{"written": [slot], "skipped": {slot: reason}, "conflicts": [{slot, confirmed, candidate}]}``。

    规则：
    - ``source`` = nickname / ai_inferred（自动来源）：**只填空槽**或替换更弱的 mentioned
      （昵称 < AI 推断 < 已确认）；同来源值变了则刷新（昵称改了 / 新证据）；已确认槽
      **永不覆盖**，值不同 → conflicts + 通知中心一条；
    - ``source`` = user / confirmed（坐席动作）：覆盖一切，status 强制 confirmed。
    绝不抛（写失败按 skipped 记 write_failed）。"""
    out: Dict[str, Any] = {"written": [], "skipped": {}, "conflicts": []}
    pf, ck = str(platform or "").strip(), str(chat_key or "").strip()
    src = str(source or "").strip().lower()
    if not pf or not ck or not isinstance(candidates, dict) or not candidates or src not in SOURCES:
        return out
    conv = _conv_label(pf, ck, conversation_id)
    n = float(now if now is not None else _now())
    try:
        prof = store.get_customer_profile(pf, ck) or {}
    except Exception:
        prof = {}
    fields = dict(prof.get("fields") or {})
    cells: Dict[str, Any] = {}
    manual = src in (SRC_USER, SRC_CONFIRMED)
    st = ST_CONFIRMED if manual else (status if status in STATUSES else ST_MENTIONED)
    if st == ST_CONFIRMED and not manual:
        st = ST_MENTIONED      # 红线：AI / 昵称值不自动升 confirmed
    for key, cand in candidates.items():
        k = str(key or "").strip().lower()
        if get_slot(k) is None:
            out["skipped"][k] = "unknown_slot"
            continue
        val, ev = _cand_pair(cand)
        if not val:
            out["skipped"][k] = "empty"
            continue
        val = val[:_MAX_SLOT_VALUE] if not manual else val
        old_v, old_src, old_st = cell_view(fields.get(k))
        old_src_new = _UPGRADE_SRC.get(old_src, old_src) if old_src not in SOURCES else old_src
        if not manual:
            if old_st == ST_CONFIRMED and old_v:
                if _same(old_v, val):
                    out["skipped"][k] = "already_confirmed"
                else:
                    out["skipped"][k] = "conflict"
                    out["conflicts"].append({"slot": k, "confirmed": old_v, "candidate": val})
                    logger.info("[profile] conv=%s slot=%s conflict confirmed=%s candidate=%s source=%s",
                                conv, k, old_v[:40], val[:40], src)
                    if notify:
                        notify_conflict(pf, ck, k, old_v, val, source=src, lang=lang)
                continue
            if old_st == ST_MENTIONED and old_v:
                new_rank, old_rank = _MENTIONED_RANK.get(src, 0), _MENTIONED_RANK.get(old_src_new, 2)
                if _same(old_v, val) and new_rank <= old_rank:
                    out["skipped"][k] = "same"
                    continue
                if new_rank < old_rank:
                    out["skipped"][k] = "weaker_source"
                    continue
                # 同值但来源更强（昵称 → 聊天证据）：升来源、带 evidence，仍是 mentioned
        cells[k] = make_cell(val, src, st, ts=n, evidence=ev)
    if not cells:
        return out
    try:
        res = store.upsert_profile_cells(pf, ck, cells, now=n)
    except Exception:
        logger.debug("profile apply write failed", exc_info=True)
        res = None
    after = dict((res or {}).get("fields") or {}) if isinstance(res, dict) else {}
    for k, cell in cells.items():
        if after and after.get(k) != cell:
            out["skipped"][k] = "write_failed"
            continue
        out["written"].append(k)
        _log_profile(conv, k, cell["value"], cell["source"], cell["status"])
    return out


def confirm(store: Any, platform: str, chat_key: str, slot: str, *, value: str = "",
            conversation_id: str = "", now: Optional[float] = None) -> Dict[str, Any]:
    """坐席 ✓ / ✎：写 ``status=confirmed source=confirmed``（值缺省用现值；无值 → no_value）。"""
    k = str(slot or "").strip().lower()
    if get_slot(k) is None:
        return {"ok": False, "reason": "unknown_slot"}
    pf, ck = str(platform or "").strip(), str(chat_key or "").strip()
    prof = store.get_customer_profile(pf, ck) or {}
    fields = dict(prof.get("fields") or {})
    cur_v, _s, _st = cell_view(fields.get(k))
    v = _norm_val(value) or cur_v
    if not v:
        return {"ok": False, "reason": "no_value"}
    n = float(now if now is not None else _now())
    cell = make_cell(v, SRC_CONFIRMED, ST_CONFIRMED, ts=n)
    try:
        res = store.upsert_profile_cells(pf, ck, {k: cell}, now=n)
    except Exception:
        logger.debug("profile confirm write failed", exc_info=True)
        return {"ok": False, "reason": "write_failed"}
    after = dict((res or {}).get("fields") or {}) if isinstance(res, dict) else {}
    _v, _src, st = cell_view(after.get(k))
    _log_profile(_conv_label(pf, ck, conversation_id), k, v, SRC_CONFIRMED, st)
    return {"ok": st == ST_CONFIRMED, "slot": k, "value": v, "state": st, "source": SRC_CONFIRMED}


def reject(store: Any, platform: str, chat_key: str, slot: str, *,
           conversation_id: str = "", now: Optional[float] = None) -> Dict[str, Any]:
    """坐席 ✕：否掉 AI 推断 / 昵称值——只删 **mentioned** 单元（已确认值不动，返回 confirmed）。"""
    k = str(slot or "").strip().lower()
    if get_slot(k) is None:
        return {"ok": False, "reason": "unknown_slot"}
    pf, ck = str(platform or "").strip(), str(chat_key or "").strip()
    prof = store.get_customer_profile(pf, ck) or {}
    fields = dict(prof.get("fields") or {})
    cur_v, cur_src, cur_st = cell_view(fields.get(k))
    if cur_st == ST_CONFIRMED:
        return {"ok": False, "reason": "confirmed", "slot": k, "value": cur_v, "state": cur_st}
    if not cur_v:
        return {"ok": True, "slot": k, "value": "", "state": ST_UNKNOWN}
    n = float(now if now is not None else _now())
    try:
        store.upsert_profile_cells(pf, ck, {k: None}, now=n)
    except Exception:
        logger.debug("profile reject write failed", exc_info=True)
        return {"ok": False, "reason": "write_failed"}
    logger.info("[profile] conv=%s slot=%s value=%s source=%s status=rejected",
                _conv_label(pf, ck, conversation_id), k, cur_v[:40], cur_src or "-")
    return {"ok": True, "slot": k, "value": "", "state": ST_UNKNOWN, "rejected": cur_v}


# ── B：昵称预填 ────────────────────────────────────────────────────────────────

def nickname_prefill(store: Any, platform: str, chat_key: str, nickname: Any, *,
                     conversation_id: str = "", now: Optional[float] = None,
                     force: bool = False, lang: str = "zh") -> Dict[str, Any]:
    """昵称 → 槽位候选 → ``apply(source=nickname, status=mentioned)``（「来自昵称 · 待确认」）。
    同会话同昵称进程内只跑一次（昵称变了再跑；``force`` 忽略记忆）。无候选 → 空结果。"""
    nick = re.sub(r"\s+", " ", str(nickname or "")).strip()
    key = f"{platform}:{chat_key}"
    if not nick or nick == str(chat_key or ""):
        return {"written": [], "skipped": {}, "conflicts": [], "nickname": nick}
    if not force and _NICK_MEMO.get(key) == nick:
        return {"written": [], "skipped": {"*": "memo"}, "conflicts": [], "nickname": nick}
    try:
        from src.contacts.nickname_parse import nickname_candidates
        cands = nickname_candidates(nick)
    except Exception:
        cands = {}
    if len(_NICK_MEMO) >= _NICK_MEMO_MAX:
        _NICK_MEMO.clear()
    _NICK_MEMO[key] = nick
    if not cands:
        return {"written": [], "skipped": {}, "conflicts": [], "nickname": nick}
    res = apply(store, platform, chat_key, cands, source=SRC_NICKNAME, status=ST_MENTIONED,
                conversation_id=conversation_id, now=now, lang=lang)
    res["nickname"] = nick
    res["candidates"] = {k: v["value"] for k, v in cands.items()}
    return res


def conversation_nickname(inbox_store: Any, conversation_id: str) -> str:
    """会话 display_name（平台推送昵称）；无 / 等于 chat_key → ""。绝不抛。"""
    conv = str(conversation_id or "").strip()
    if inbox_store is None or not conv or not hasattr(inbox_store, "get_conversation"):
        return ""
    try:
        row = inbox_store.get_conversation(conv) or {}
        name = str(row.get("display_name") or "").strip()
        if name and name != str(row.get("chat_key") or ""):
            return name
    except Exception:
        pass
    return ""


# ── A：两链合一抽取 ───────────────────────────────────────────────────────────

def llm_extract_enabled(cfg_root: Any, memory_cfg: Any = None) -> Tuple[bool, str]:
    """``companion.goals.profile_llm.enabled`` 与 ``memory.extract.use_llm`` 任一开 → (True, 哪个开)。
    两键语义合并：页面只留一个「AI 抽取」开关，另一键跟随。"""
    root = cfg_root if isinstance(cfg_root, dict) else {}
    try:
        goals = ((root.get("companion") or {}).get("goals") or {}) if isinstance(root.get("companion"), dict) else {}
        pl = goals.get("profile_llm") if isinstance(goals, dict) else None
        if isinstance(pl, dict) and pl.get("enabled"):
            return True, "profile_llm"
    except Exception:
        pass
    try:
        mem = memory_cfg if isinstance(memory_cfg, dict) else (root.get("memory") if isinstance(root.get("memory"), dict) else {})
        ex = mem.get("extract") if isinstance(mem.get("extract"), dict) else {}
        # 缺键按开（与 skill_manager 旧判据 ``ex.get("use_llm", True)`` 一致，不回退）
        if ex.get("use_llm", True):
            return True, "memory_use_llm"
    except Exception:
        pass
    return False, ""


def slot_table(cfg_root: Any = None) -> List[Dict[str, Any]]:
    """按业务域取槽位表（陪伴 = 称呼/坐标/职业/年龄/兴趣/家庭/婚恋/收入/居住/资产）。"""
    try:
        from src.companion.goals.profile_slots import slots_for_domain
        from src.utils.business_domain import resolve_business_domain
        bd = resolve_business_domain(cfg_root) if cfg_root is not None else None
        return [s for s in slots_for_domain(bd, include_lifecycle=False) if not s.get("custom")]
    except Exception:
        from src.companion.goals.profile_slots import ALL_SLOTS
        return [dict(s) for s in ALL_SLOTS if s.get("track") in ("relation", "personal")]


def build_merged_prompt(user_msg: str, reply: str, slots: List[Dict[str, Any]]) -> str:
    """一次调用同时抽「记忆事实」与「画像槽位」。事实规则与 ``ai_client.extract_memory_facts``
    同口径（接地 / 引文 / 持久性 / 称呼方向 / 主语 / 置信）；槽位规则与 ``profile_llm`` 同口径
    （摘录式、禁推断、原语言、≤30 字、带引文）。"""
    slot_lines = "\n".join(
        f"- {s['key']}: {s.get('label_zh') or s['key']}（{s.get('ask_zh') or ''}）" for s in slots[:12])
    return (
        "你是对话记忆与客户画像抽取器。根据本轮 USER（客户）消息与 ASSISTANT 回复，一次输出两部分：\n"
        "【一、facts 记忆事实】抽取 0～4 条值得后续聊天记住的客观信息（客户自称的名字、偏好、"
        "刚透露的重要事实、简单约定）。\n"
        "- 接地铁律：事实只能来自 USER 消息里客户明确说出的内容；ASSISTANT 里的猜测/提议/问句"
        "一律不得作为事实。客户没明确说的宁可不抽。\n"
        "- 引文铁律：每条事实附 evidence——从 USER 消息里**逐字复制**的一段原话（原语言、不翻译、"
        "不改写、≤80 字）；找不到可逐字引用的原话就不要输出这条。\n"
        "- 持久性：只抽有持续意义的（身份/称呼/偏好/关系/经历/约定/计划）；此刻天气、正在做的动作、"
        "当下情绪不抽。\n"
        "- 称呼方向：「hi X」「你好 X」是客户在称呼助手，不是客户名字；只有「我是X / my name is X / "
        "call me X」才算。绝不抽助手/AI 自己的名字身份。\n"
        "- 主语：每条事实主语显式写「客户」（如「客户自称Michael」），禁止「用户称呼自己为X」式歧义句。\n"
        "- confidence（0～1）：原话直接陈述 ≥0.85；结合语境推断 0.5～0.7；更低不输出。fact 用中文短句。\n"
        "【二、slots 画像槽位】从 USER 消息里**摘录**下列字段（只许原文片段或原文数字，禁止推断、"
        "翻译、补全、编造；消息里没提到的字段不要输出）。每个值 ≤30 字、保持原语言，并附 evidence"
        "（USER 原话逐字片段）。「毕业于2000年」不能推出年龄；「hi Michael」不是客户的 name。\n"
        f"候选字段（key: 含义）：\n{slot_lines}\n"
        "【输出】严格一行 JSON，不要 markdown：\n"
        '{"facts":[{"fact":"客户有一个女儿","evidence":"I have a daughter","confidence":0.95}],'
        '"slots":{"occupation":{"value":"union carpenter","evidence":"I\'m a union carpenter"}}}\n'
        "facts 无则 []，slots 无则 {}。\n\n"
        f"USER:\n{str(user_msg or '')[:2000]}\n\nASSISTANT:\n{str(reply or '')[:1500]}"
    )


def parse_merged(raw: Any) -> Tuple[List[Dict[str, Any]], Dict[str, Dict[str, str]]]:
    """LLM 输出 → ``(facts[{fact, evidence, confidence?}], slots{key: {value, evidence}})``。
    剥代码栅栏、取首尾大括号；坏输出 → ([], {})。"""
    s = str(raw or "").strip()
    if not s:
        return [], {}
    s = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.IGNORECASE).strip()
    i, j = s.find("{"), s.rfind("}")
    if i < 0 or j <= i:
        return [], {}
    try:
        data = json.loads(s[i:j + 1])
    except Exception:
        return [], {}
    if not isinstance(data, dict):
        return [], {}
    facts: List[Dict[str, Any]] = []
    for it in (data.get("facts") or []) if isinstance(data.get("facts"), list) else []:
        if isinstance(it, str):
            it = {"fact": it}
        if not isinstance(it, dict):
            continue
        fact = _norm_val(it.get("fact") or it.get("text"))
        if not fact:
            continue
        row: Dict[str, Any] = {"fact": fact[:120], "evidence": _norm_val(it.get("evidence") or it.get("quote"))}
        c = it.get("confidence")
        if isinstance(c, (int, float)) and not isinstance(c, bool):
            row["confidence"] = max(0.0, min(1.0, float(c)))
        facts.append(row)
        if len(facts) >= _MAX_FACTS:
            break
    slots: Dict[str, Dict[str, str]] = {}
    raw_slots = data.get("slots")
    if isinstance(raw_slots, dict):
        for k, v in raw_slots.items():
            key = str(k or "").strip().lower()
            if get_slot(key) is None:
                continue
            if isinstance(v, dict):
                val, ev = _norm_val(v.get("value")), _norm_val(v.get("evidence"))
            elif isinstance(v, (str, int, float)) and not isinstance(v, bool):
                val, ev = _norm_val(v), ""
            else:
                continue
            if val:
                slots[key] = {"value": val[:_MAX_SLOT_VALUE], "evidence": ev[:120]}
    return facts, slots


_NORM_STRIP_RE = re.compile(r"[\s，。,.!！?？、;；:：'\"“”‘’()（）]+")


def _lit(s: str) -> str:
    return _NORM_STRIP_RE.sub("", str(s or "")).casefold()


def ground_slots(user_msg: str, slots: Dict[str, Dict[str, str]]) -> Tuple[Dict[str, Dict[str, str]], Dict[str, str]]:
    """槽位候选接地：值过 ``profile_llm.ground_extracted``（memory_grounding 金标 + 超短值字面锚定）；
    给了 evidence 的，引文必须真出自客户原话；``slot_value_suspect`` 命中即丢。
    返回 ``(kept, dropped{key: reason})``。"""
    kept: Dict[str, Dict[str, str]] = {}
    dropped: Dict[str, str] = {}
    msg = str(user_msg or "")
    if not slots:
        return kept, dropped
    try:
        from src.companion.goals.profile_llm import ground_extracted
        ok_vals = ground_extracted(msg, {k: v.get("value", "") for k, v in slots.items()})
    except Exception:
        logger.debug("ground_extracted failed", exc_info=True)
        return kept, {k: "grounding_error" for k in slots}
    try:
        from src.companion.goals.profile_slots import slot_value_suspect
    except Exception:
        slot_value_suspect = None   # type: ignore[assignment]
    lit_msg = _lit(msg)
    for k, v in slots.items():
        val, ev = v.get("value", ""), v.get("evidence", "")
        if k not in ok_vals:
            dropped[k] = "fact_unanchored"
            continue
        if ev and _lit(ev) and _lit(ev) not in lit_msg:
            dropped[k] = "evidence_mismatch"
            continue
        if slot_value_suspect is not None:
            try:
                why = slot_value_suspect(k, val)
            except Exception:
                why = ""
            if why:
                dropped[k] = f"suspect:{why}"
                continue
        kept[k] = {"value": val, "evidence": ev}
    return kept, dropped


async def extract_facts_and_slots(ai_client: Any, user_msg: str, reply: str, *,
                                  slots: Optional[List[Dict[str, Any]]] = None,
                                  timeout: float = _LLM_TIMEOUT) -> Dict[str, Any]:
    """一次 LLM 调用 → ``{"facts": [{fact, evidence, confidence?}], "dropped": [...], "slots": {k: {value, evidence}},
    "slots_dropped": {k: reason}, "candidates": n, "llm": 1|0}``。事实过 ``memory_grounding.ground_fact_items``
    （与 ``ai_client.extract_memory_facts`` 同一护栏），槽位过 :func:`ground_slots`。任何失败 → 全空、llm=0。"""
    empty: Dict[str, Any] = {"facts": [], "dropped": [], "slots": {}, "slots_dropped": {},
                             "candidates": 0, "llm": 0}
    try:
        from src.inbox.media_enrich import strip_media_desc
        u = strip_media_desc(str(user_msg or ""))
    except Exception:
        u = str(user_msg or "")
    u = u.strip()
    a = str(reply or "").strip()
    if len(u) < 2 or ai_client is None or not callable(getattr(ai_client, "chat", None)):
        return empty
    try:
        cb_until = float(getattr(ai_client, "_cb_open_until", 0) or 0)
        if getattr(ai_client, "_cb_enabled", False) and cb_until > 0 and time.time() < cb_until:
            logger.debug("[extract] skipped: circuit open")
            return empty
    except Exception:
        pass
    prompt = build_merged_prompt(u, a, slots if slots is not None else slot_table())
    raw = None
    try:
        try:
            from src.ai.llm_cost import purpose_scope
        except Exception:
            purpose_scope = None   # type: ignore[assignment]
        if purpose_scope is not None:
            with purpose_scope("memory_extract"):
                raw = await asyncio.wait_for(ai_client.chat(prompt), timeout=timeout)
        else:
            raw = await asyncio.wait_for(ai_client.chat(prompt), timeout=timeout)
    except asyncio.TimeoutError:
        logger.debug("[extract] merged llm timeout")
        return empty
    except Exception:
        logger.debug("[extract] merged llm failed", exc_info=True)
        return empty
    facts_raw, slots_raw = parse_merged(raw)
    out: Dict[str, Any] = dict(empty)
    out["llm"] = 1
    out["candidates"] = len(facts_raw)
    # 事实接地（引文级）——与 ai_client._ground_extracted_fact_items 同一判据
    try:
        from src.ai.memory_grounding import ground_fact_items
        kept, dropped = ground_fact_items(facts_raw, u)
        conf = {str(it.get("fact")): it["confidence"] for it in facts_raw if it.get("confidence") is not None}
        facts: List[Dict[str, Any]] = []
        for k in kept:
            txt = str(k.get("text") or "")
            if not txt:
                continue
            row: Dict[str, Any] = {"fact": txt, "evidence": str(k.get("evidence") or "")}
            if txt in conf:
                row["confidence"] = conf[txt]
            facts.append(row)
        out["facts"] = facts
        out["dropped"] = [{"fact": str(d.get("text") or ""), "evidence": str(d.get("evidence") or ""),
                           "reason": str(d.get("reason") or "")} for d in dropped if d.get("text")]
        if out["dropped"]:
            logger.warning("记忆抽取接地护栏丢弃 %d 条未锚定用户原话的事实: %s", len(out["dropped"]),
                           "; ".join(f"[{d['reason']}] {d['fact'][:40]} ⇐ {d['evidence'][:40]!r}" for d in out["dropped"]))
    except Exception:
        logger.debug("[extract] fact grounding failed; dropping all", exc_info=True)
        out["facts"] = []
    kept_slots, sd = ground_slots(u, slots_raw)
    out["slots"], out["slots_dropped"] = kept_slots, sd
    if sd:
        logger.info("[profile] slot candidates dropped: %s",
                    "; ".join(f"{k}:{r}" for k, r in sd.items()))
    return out


# ── D：停滞计数 ────────────────────────────────────────────────────────────────

def record_extract_result(n_facts: int, n_slots: int, *, llm: Optional[int] = None,
                          now: Optional[float] = None) -> Dict[str, Any]:
    n = float(now if now is not None else _now())
    with _STALL_LOCK:
        _STALL["runs"] = int(_STALL.get("runs") or 0) + 1
        _STALL["last_ts"] = n
        if llm is not None:
            _STALL["last_llm"] = int(bool(llm))
        if int(n_facts or 0) <= 0 and int(n_slots or 0) <= 0:
            _STALL["zero_streak"] = int(_STALL.get("zero_streak") or 0) + 1
        else:
            _STALL["zero_streak"] = 0
            _STALL["last_nonzero_ts"] = n
        return dict(_STALL)


def stall_status(*, threshold: int = STALL_THRESHOLD) -> Dict[str, Any]:
    """``{zero_streak, threshold, stalled, runs, last_ts, last_llm}``——页面告警行数据源。"""
    with _STALL_LOCK:
        z = int(_STALL.get("zero_streak") or 0)
        return {"zero_streak": z, "threshold": int(threshold), "stalled": z >= int(threshold),
                "runs": int(_STALL.get("runs") or 0), "last_ts": float(_STALL.get("last_ts") or 0),
                "last_nonzero_ts": float(_STALL.get("last_nonzero_ts") or 0),
                "last_llm": _STALL.get("last_llm")}


def reset_stall() -> None:
    with _STALL_LOCK:
        _STALL.update({"zero_streak": 0, "runs": 0, "last_ts": 0.0, "last_nonzero_ts": 0.0, "last_llm": None})
    _NICK_MEMO.clear()


# ── skill_manager 入口 ────────────────────────────────────────────────────────

async def run_extraction(ai_client: Any, cfg_root: Any, config_path: Any, *, user_msg: str, reply: str,
                         platform: str, chat_key: str, account_id: str = "", conversation_id: str = "",
                         memory_cfg: Any = None, inbox_store: Any = None, nickname: str = "",
                         goal_store: Any = None, lang: str = "zh", now: Optional[float] = None,
                         slots: Optional[List[Dict[str, Any]]] = None,
                         heuristic_facts: int = 0) -> Dict[str, Any]:
    """skill_manager 抽取链调用的**唯一**入口：一次 LLM（任一开关开）→ 事实回传 + 槽位分发。

    返回 ``{"facts": [(fact, evidence, confidence|None)], "dropped": [...], "slots_written": n,
    "slots": {…}, "llm": 0|1, "enabled_by": ""|"profile_llm"|"memory_use_llm", "nickname": {...}}``。
    事实的 ``add_fact`` 由调用方按既有路径做（本函数不碰 episodic store）。绝不抛。"""
    res: Dict[str, Any] = {"facts": [], "dropped": [], "slots_written": 0, "slots": {}, "llm": 0,
                           "enabled_by": "", "nickname": None, "conflicts": []}
    pf = str(platform or "").strip()
    ck = str(chat_key or "").strip()
    conv = str(conversation_id or "").strip() or (f"{pf}:{account_id or ''}:{ck}" if pf and ck else "")
    try:
        on, by = llm_extract_enabled(cfg_root, memory_cfg)
        res["enabled_by"] = by
        store = goal_store
        if store is None and pf and ck:
            try:
                from src.companion.goals.service import get_configured_store, goals_enabled
                if goals_enabled(cfg_root):
                    store = get_configured_store(cfg_root, config_path)
            except Exception:
                store = None
        # B：昵称预填（会话首条入站即触发；昵称变更再触发）
        if store is not None and pf and ck:
            nick = str(nickname or "").strip() or conversation_nickname(inbox_store, conv)
            if nick:
                try:
                    res["nickname"] = nickname_prefill(store, pf, ck, nick, conversation_id=conv, now=now, lang=lang)
                except Exception:
                    logger.debug("nickname prefill failed", exc_info=True)
        if on and ai_client is not None:
            ext = await extract_facts_and_slots(
                ai_client, user_msg, reply, slots=slots if slots is not None else slot_table(cfg_root))
            res["llm"] = int(ext.get("llm") or 0)
            res["facts"] = [(str(it.get("fact") or ""), str(it.get("evidence") or ""),
                             (float(it["confidence"]) if it.get("confidence") is not None else None))
                            for it in (ext.get("facts") or []) if it.get("fact")]
            res["dropped"] = list(ext.get("dropped") or [])
            res["slots"] = dict(ext.get("slots") or {})
            if res["slots"] and store is not None and pf and ck:
                ap = apply(store, pf, ck, res["slots"], source=SRC_AI, status=ST_MENTIONED,
                           conversation_id=conv, now=now, lang=lang)
                res["slots_written"] = len(ap.get("written") or [])
                res["conflicts"] = list(ap.get("conflicts") or [])
    except Exception:
        logger.debug("[extract] run_extraction failed", exc_info=True)
    # facts 口径 = 启发式（调用方已落库）+ 本次 LLM 过接地的；停滞计数同口径
    n_facts = len(res["facts"]) + max(0, int(heuristic_facts or 0))
    record_extract_result(n_facts, int(res["slots_written"]), llm=res["llm"], now=now)
    logger.info("[extract] conv=%s facts=%d slots=%d llm=%d", conv or "-",
                n_facts, int(res["slots_written"]), int(res["llm"]))
    return res


__all__ = [
    "SCHEMA_VERSION", "SOURCES", "STATUSES", "STALL_THRESHOLD",
    "apply", "bind_app", "build_merged_prompt", "confirm", "conversation_nickname",
    "export_profile", "extract_facts_and_slots", "ground_slots", "import_profile",
    "is_new_schema", "llm_extract_enabled", "make_cell", "nickname_prefill", "normalize_cell",
    "notify_conflict", "parse_merged", "record_extract_result", "reject", "reset_stall",
    "run_extraction", "slot_table", "stall_status", "upgrade_fields",
]
