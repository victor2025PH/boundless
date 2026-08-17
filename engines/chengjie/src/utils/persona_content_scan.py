# -*- coding: utf-8 -*-
"""人设内容排查器（2026-08-03「删除不干净」事故链）——「这个词还在哪」的单一入口。

背景：一个人设事实（如「养猫」）在创建/丰富期会被反规范化写进多个字段
（hobbies / background / tastes / selfie_scenes / life_arc / specific_memories …），
而 Studio 表单只暴露其中一部分；此外还有每客户的情景记忆、会话历史、长传记库、
近期出站消息等运行时残留。运营在 UI 删掉一处后 AI 照提不误，且无从知道「还在哪」。
本模块把「按关键词全层扫描」收成一个只读入口：

- ``scan_profile_fields(persona, q)``     — 纯函数：递归扫档案全字段（含 UI 未暴露的），
  每个命中标注 ① 到达通道（进 persona prompt / 场景块 / 生活线 / 惰性）
  ② 是否可在 Studio 表单里直接编辑（不可编辑 = 要走「高级字段」或后台）。
- ``scan_bio_chunks / scan_episodic / scan_history / scan_outbound``
  — 运行时层的只读 sqlite 扫描（``mode=ro`` URI，零写事务），全部软失败
  （库缺席/异常 → ``{"available": False}``，绝不让路由 500）。

通道判定复用 K1 prompt 数据链登记表（``persona_manager.PROMPT_CONSUMED_FIELDS``）
——那张表是「哪些字段进 prompt」的唯一事实源；豁免表里**并非惰性**的字段
（selfie_scenes/life_arc/location 由其它子系统拼块注入）在 ``_EXEMPT_ACTIVE``
里显式标注到达通道，防止「豁免」被误读成「删不删无所谓」。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 豁免于 persona prompt、但**经其它子系统到达 LLM/客户**的字段 → 通道代号。
# 与 persona_manager.PROMPT_EXEMPT_FIELDS 的注释一一对应；新增豁免字段时若它
# 会被别的子系统消费，必须同步登记这里，否则扫描会把它标成 inert 误导运营。
_EXEMPT_ACTIVE: Dict[str, str] = {
    "selfie_scenes": "scene_state",   # 场景状态块（被问「在干嘛」）+ 生图场景池
    "life_arc": "life_beat",          # 深度人设生活线（主动分享/状态叙事）
    "location": "local_state",        # 本地时钟/天气/场景块
}

# Studio 表单**有输入控件**的字段路径（与 personas.html 的表单覆盖面对应；
# 前端 _ADV_MANAGED_* 的语义是「表单管理」，其中 personality.quirks/humor/
# temperament 实为只读展示区，不算可编辑）。命中不在此集合 = 运营在表单里
# 删不掉，要走「高级字段」编辑或后台——这正是本次事故里最伤的一类。
UI_EDITABLE_PATHS = frozenset({
    "name", "role", "tags", "age", "gender", "background", "appearance",
    "personality.style",
    "speaking.reply_length", "speaking.emoji_level",
    "speaking.forbidden_phrases", "speaking.openers",
    "identity.deny_ai", "identity.claim_human",
    "voice_profile.backend", "voice_profile.voice",
    "names.full_western", "names.english", "names.nickname", "names.usage_notes",
    "context.hobbies", "context.specific_memories", "context.emotional_triggers",
    "tastes.likes", "tastes.dislikes", "tastes.opinions",
    "boundaries.topics_to_avoid",
    "selfie_scenes",
})

_SNIPPET_RADIUS = 30          # 命中片段左右各留多少字符
_MAX_HITS_PER_LAYER = 50      # 单层命中上限（防超长档案撑爆响应）

# ── 同义词建议表（P2 期，2026-08-04）────────────────────────────────────────
# 字面 LIKE 的两个实证盲区：① 语义同义（生产原文「小三花」不含「猫」字）；
# ② **跨语言**（selfie_scenes/appearance 是英文字段，「猫」永远扫不到
#   "cat cafe"——首期实施时恰好靠人工补 cat 才抓到）。
# 组内互为建议（双向）；只做**建议**不自动并入——扩词的召回收益与误报成本
# 由运营点选决定（与 persona_bio 的 _QUERY_ALIASES「宁可漏进不错进」同哲学）。
# 扩展点：按业务域往这里加组即可，勿引入需要分词器/网络的花活。
_SCAN_SYNONYM_GROUPS = (
    ("猫", "猫咪", "喵", "橘猫", "三花", "流浪猫", "猫咖", "cat", "kitten", "kitty"),
    ("狗", "狗狗", "汪", "柴犬", "金毛", "dog", "puppy"),
    ("咖啡", "拿铁", "美式", "coffee", "latte", "espresso"),
    ("奶茶", "珍珠奶茶", "波霸", "bubble tea", "boba", "milk tea"),
    ("抹茶", "matcha"),
    ("健身", "撸铁", "瑜伽", "gym", "workout", "yoga", "fitness"),
    ("旅行", "旅游", "travel", "trip"),
    ("红酒", "威士忌", "啤酒", "清酒", "wine", "whisky", "beer", "sake"),
)
_SCAN_TERMS_MAX = 5           # 单次扫描词数上限（5 词 × 4 库层 = 20 条 LIKE，够用且有界）
_SCAN_TERM_SPLIT = ("，", "、", ",", ";", "；")


def split_scan_terms(q: str) -> List[str]:
    """查询串 → 词列表（中英逗号/顿号/分号分隔，去重保序，上限 5，单词 ≤40 字）。"""
    s = str(q or "")
    for sep in _SCAN_TERM_SPLIT:
        s = s.replace(sep, "\x00")
    seen = set()
    out: List[str] = []
    for part in s.split("\x00"):
        t = part.strip()[:40]
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t)
        if len(out) >= _SCAN_TERMS_MAX:
            break
    return out


def suggest_related_terms(terms: Any, extra_groups: Any = None) -> List[str]:
    """给定词集 → 同组的其它候选（排除已含词；上限 10）。运营点选后并入再扫。

    ``extra_groups``（2026-08-04 P3）＝运营自维护的同义词组，来自 config
    ``personas.content_scan.synonym_groups``（列表的列表，overlay 热重载 ~30s
    生效）——业务黑话/人设专名（「毛豆」→猫）没法进内置表，给运营留扩展口。
    形状防御：非列表/组内非字符串一律跳过，绝不因配置手滑砸扫描。
    内置组在后、运营组在前（运营词优先出现在建议里）。
    """
    have = {str(t).strip().lower() for t in (terms or []) if str(t).strip()}
    if not have:
        return []
    groups: List[tuple] = []
    if isinstance(extra_groups, (list, tuple)):
        for g in extra_groups:
            if isinstance(g, (list, tuple)):
                clean = tuple(str(x).strip() for x in g
                              if isinstance(x, (str, int, float)) and str(x).strip())
                if len(clean) >= 2:
                    groups.append(clean)
    groups.extend(_SCAN_SYNONYM_GROUPS)
    out: List[str] = []
    seen = set(have)
    for group in groups:
        if not any(g.lower() in have for g in group):
            continue
        for g in group:
            if g.lower() not in seen:
                seen.add(g.lower())
                out.append(g)
            if len(out) >= 10:
                return out
    return out


def _strip_indexes(path: str) -> str:
    """``tastes.likes[3]`` → ``tastes.likes``（通道/可编辑判定按字段路径，不看下标）。"""
    out: List[str] = []
    for seg in str(path or "").split("."):
        i = seg.find("[")
        out.append(seg[:i] if i >= 0 else seg)
    return ".".join(s for s in out if s)


def _path_in_set(path: str, registry: Any) -> bool:
    """点路径与登记表的双向前缀匹配（登记 dict 路径 = 整棵子树同一表态）。"""
    p = _strip_indexes(path)
    if not p:
        return False
    for key in registry:
        k = str(key)
        if p == k or p.startswith(k + ".") or k.startswith(p + "."):
            return True
    return False


def classify_channel(path: str) -> str:
    """字段路径 → 到达通道代号。

    - ``persona_prompt``：K1 登记表消费，进人设 system prompt（每轮必达）
    - ``scene_state`` / ``life_beat`` / ``local_state``：豁免表中由其它子系统拼块
    - ``inert``：纯内部/运营元数据（id、tags、voice_profile…），不达 LLM
    - ``unknown``：两张表都没登记（新字段？按 K1 门禁本不该出现，如实上报）
    """
    try:
        from src.utils.persona_manager import (
            PROMPT_CONSUMED_FIELDS, PROMPT_EXEMPT_FIELDS,
        )
    except Exception:                       # 极端导入失败：全部标 unknown，不装懂
        return "unknown"
    p = _strip_indexes(path)
    top = p.split(".", 1)[0]
    if top in _EXEMPT_ACTIVE:
        return _EXEMPT_ACTIVE[top]
    if _path_in_set(p, PROMPT_CONSUMED_FIELDS):
        return "persona_prompt"
    if _path_in_set(p, PROMPT_EXEMPT_FIELDS):
        return "inert"
    return "unknown"


def is_ui_editable(path: str) -> bool:
    return _path_in_set(path, UI_EDITABLE_PATHS)


def _snippet(text: str, q: str) -> str:
    s = str(text)
    i = s.lower().find(q.lower())
    if i < 0:
        return s[: _SNIPPET_RADIUS * 2]
    a = max(0, i - _SNIPPET_RADIUS)
    b = min(len(s), i + len(q) + _SNIPPET_RADIUS)
    return ("…" if a > 0 else "") + s[a:b] + ("…" if b < len(s) else "")


def scan_profile_fields(persona: Any, q: str) -> List[Dict[str, Any]]:
    """递归扫描档案全字段 → 命中清单（纯函数，大小写不敏感子串匹配）。

    返回 ``[{path, text, channel, ui_editable}, …]``；q 空/档案非 dict → []。
    刻意扫**所有**键（含 ``_history`` 外的内部键）——排查工具漏报比多报更糟；
    通道栏会把 inert 命中如实标出，由运营自行忽略。
    """
    query = str(q or "").strip()
    if not query or not isinstance(persona, dict):
        return []
    ql = query.lower()
    hits: List[Dict[str, Any]] = []

    def _walk(node: Any, path: str) -> None:
        if len(hits) >= _MAX_HITS_PER_LAYER:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                _walk(v, f"{path}.{k}" if path else str(k))
        elif isinstance(node, (list, tuple)):
            for i, v in enumerate(node):
                _walk(v, f"{path}[{i}]")
        else:
            s = str(node if node is not None else "")
            if s and ql in s.lower():
                hits.append({
                    "path": path,
                    "text": _snippet(s, query),
                    "channel": classify_channel(path),
                    "ui_editable": is_ui_editable(path),
                })

    _walk(persona, "")
    return hits


# ── 运行时层（只读 sqlite；全部软失败）────────────────────────────────────────

def _ro_connect(db_path: Any) -> Optional[sqlite3.Connection]:
    p = Path(str(db_path))
    if not p.exists():
        return None
    return sqlite3.connect(f"file:{p.as_posix()}?mode=ro", uri=True, timeout=3)


def _unavailable() -> Dict[str, Any]:
    return {"available": False, "count": 0, "samples": []}


def scan_bio_chunks(db_path: Any, persona_id: str, q: str,
                    limit: int = 5) -> Dict[str, Any]:
    """长传记库该人设的含词分块（``persona_bio_chunks``）。"""
    try:
        conn = _ro_connect(db_path)
        if conn is None:
            return _unavailable()
        try:
            like = f"%{q}%"
            n = conn.execute(
                "SELECT COUNT(*) FROM persona_bio_chunks"
                " WHERE persona_id = ? AND text LIKE ?",
                (str(persona_id), like)).fetchone()[0]
            rows = conn.execute(
                "SELECT idx, text FROM persona_bio_chunks"
                " WHERE persona_id = ? AND text LIKE ? ORDER BY idx LIMIT ?",
                (str(persona_id), like, int(limit))).fetchall()
        finally:
            conn.close()
        return {"available": True, "count": int(n),
                "samples": [{"idx": int(r[0]), "text": _snippet(r[1], q)}
                            for r in rows]}
    except Exception:
        logger.debug("[persona_scan] bio 扫描失败", exc_info=True)
        return _unavailable()


def scan_episodic(db_path: Any, q: str, limit: int = 5) -> Dict[str, Any]:
    """情景记忆含词条目（bot.db::episodic_memory；跨全部客户——记忆按客户键存，
    无法按人设过滤，交给运营按 memory_key 去记忆页处置）。"""
    try:
        conn = _ro_connect(db_path)
        if conn is None:
            return _unavailable()
        try:
            like = f"%{q}%"
            n = conn.execute(
                "SELECT COUNT(*) FROM episodic_memory WHERE content LIKE ?",
                (like,)).fetchone()[0]
            rows = conn.execute(
                "SELECT id, user_id, content FROM episodic_memory"
                " WHERE content LIKE ? ORDER BY id DESC LIMIT ?",
                (like, int(limit))).fetchall()
        finally:
            conn.close()
        return {"available": True, "count": int(n),
                "samples": [{"row_id": int(r[0]), "memory_key": str(r[1]),
                             "text": _snippet(r[2], q)} for r in rows]}
    except Exception:
        logger.debug("[persona_scan] episodic 扫描失败", exc_info=True)
        return _unavailable()


def scan_history(db_path: Any, q: str, limit: int = 5) -> Dict[str, Any]:
    """会话历史含词条目（bot.db::user_context，_conversation_history 落这里）。

    只报 key 与计数不摘原文长段——上下文血包含大量客户隐私，排查场景给
    「哪些会话还在聊」已够用；深挖去收件箱按会话看。
    """
    try:
        conn = _ro_connect(db_path)
        if conn is None:
            return _unavailable()
        try:
            like = f"%{q}%"
            n = conn.execute(
                "SELECT COUNT(*) FROM user_context WHERE data LIKE ?",
                (like,)).fetchone()[0]
            rows = conn.execute(
                "SELECT user_id FROM user_context WHERE data LIKE ?"
                " ORDER BY updated_at DESC LIMIT ?",
                (like, int(limit))).fetchall()
        finally:
            conn.close()
        return {"available": True, "count": int(n),
                "samples": [{"memory_key": str(r[0])} for r in rows]}
    except Exception:
        logger.debug("[persona_scan] history 扫描失败", exc_info=True)
        return _unavailable()


def scan_outbound(db_path: Any, q: str, days: int = 14,
                  limit: int = 5) -> Dict[str, Any]:
    """近 N 天**出站**消息含词条数（inbox.db::messages，direction='out'）。

    口径注明：inbox 消息不带人设归属，这里是跨人设的症状面观测——
    「客户现在还在收到含 X 的消息吗」。
    """
    try:
        conn = _ro_connect(db_path)
        if conn is None:
            return _unavailable()
        try:
            like = f"%{q}%"
            since = time.time() - max(1, int(days)) * 86400
            n = conn.execute(
                "SELECT COUNT(*) FROM messages"
                " WHERE direction = 'out' AND ts >= ? AND text LIKE ?",
                (since, like)).fetchone()[0]
            rows = conn.execute(
                "SELECT conversation_id, ts, text FROM messages"
                " WHERE direction = 'out' AND ts >= ? AND text LIKE ?"
                " ORDER BY ts DESC LIMIT ?",
                (since, like, int(limit))).fetchall()
        finally:
            conn.close()
        return {"available": True, "count": int(n), "days": int(days),
                "samples": [{"conversation_id": str(r[0]), "ts": float(r[1]),
                             "text": _snippet(r[2], q)} for r in rows]}
    except Exception:
        logger.debug("[persona_scan] outbound 扫描失败", exc_info=True)
        return _unavailable()


__all__ = [
    "UI_EDITABLE_PATHS", "classify_channel", "is_ui_editable",
    "split_scan_terms", "suggest_related_terms",
    "scan_profile_fields", "scan_bio_chunks", "scan_episodic",
    "scan_history", "scan_outbound",
]
