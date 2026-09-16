# -*- coding: utf-8 -*-
"""视觉记忆库（#333 EREM2H · 2026-09-17）：客户会话里「图中是谁」的持久记录。

与 ``image_observation``（24h 文字便签）和 episodic（刻意排除识图 caption）都不同——这里存
**人脸向量 + 身份归属 + 置信 + 来源**，让「昨天发过自拍的是客户本人」「上周那张是 TA 妹妹」
跨轮、跨天、跨重启都在。

两张表（SQLite ``<config_dir>/visual_memory.db``，WAL，线程锁）：

- ``visual_observations``：每张带脸/带主体的入站图一行——``conv_key / message_id / ts / sha1 /
  subject / summary / label（persona|customer_self|known|unknown|no_face）/ matched / score /
  embedding(json) / source（ai_inferred|user_confirmed）/ confirmed``。
- ``visual_entities``：客户的视觉实体——``self``（本人）与 ``relation``（妹妹/朋友/狗…），
  ``embedding`` 是该实体全部已确认观察的均值（L2 归一）。**只有 user_confirmed 观察进实体**；
  ``ai_inferred`` 只在「≥2 张互相一致（余弦 ≥ MATCH）的自拍」时给一个**临时**本人原型
  （``source=ai_inferred``，注入措辞按「推断」说，永不写成事实）。

纠正/删除：``retire_entity`` / ``delete_conv``；``user_confirmed`` 永远压过 ``ai_inferred``
（REA733：用户纠正必须能覆盖 AI 标签）。所有 I/O 软失败：任何异常 → None/[]/False，
绝不阻断拟稿。门禁 ``tests/test_face_identity.py``。
"""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_DDL = """
CREATE TABLE IF NOT EXISTS visual_observations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conv_key TEXT NOT NULL,
  message_id TEXT NOT NULL DEFAULT '',
  ts REAL NOT NULL,
  sha1 TEXT NOT NULL DEFAULT '',
  subject TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  label TEXT NOT NULL DEFAULT '',
  matched TEXT NOT NULL DEFAULT '',
  score REAL NOT NULL DEFAULT 0,
  embedding TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'ai_inferred',
  confirmed INTEGER NOT NULL DEFAULT 0,
  entity_id INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_vobs_conv_ts ON visual_observations(conv_key, ts DESC);
CREATE TABLE IF NOT EXISTS visual_entities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  conv_key TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  label TEXT NOT NULL DEFAULT '',
  relation TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'user_confirmed',
  confidence REAL NOT NULL DEFAULT 1.0,
  embedding TEXT NOT NULL DEFAULT '',
  n_samples INTEGER NOT NULL DEFAULT 0,
  created_ts REAL NOT NULL,
  updated_ts REAL NOT NULL,
  retired INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_vent_conv ON visual_entities(conv_key, retired);
"""

SELF_TYPE = "self"
RELATION_TYPE = "relation"
MATCH = 0.50   # 与 visual_identity.MATCH_THRESHOLD 同刻度（这里避免循环 import，门禁钉同值）


# ── 向量小工具（stdlib）──────────────────────────────────────────────────────

def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    try:
        va = [float(x) for x in a]
        vb = [float(x) for x in b]
    except (TypeError, ValueError):
        return 0.0
    if not va or len(va) != len(vb):
        return 0.0
    dot = sum(x * y for x, y in zip(va, vb))
    na = math.sqrt(sum(x * x for x in va)) or 1.0
    nb = math.sqrt(sum(y * y for y in vb)) or 1.0
    return dot / (na * nb)


def mean_vector(vecs: Sequence[Sequence[float]]) -> Optional[List[float]]:
    rows = [[float(x) for x in v] for v in (vecs or []) if v]
    if not rows:
        return None
    n = len(rows[0])
    rows = [r for r in rows if len(r) == n]
    if not rows:
        return None
    acc = [0.0] * n
    for r in rows:
        for i, x in enumerate(r):
            acc[i] += x
    norm = math.sqrt(sum(x * x for x in acc)) or 1.0
    return [x / norm for x in acc]


def _dump(v: Optional[Sequence[float]]) -> str:
    if not v:
        return ""
    return json.dumps([round(float(x), 6) for x in v], separators=(",", ":"))


def _load(s: Any) -> Optional[List[float]]:
    try:
        v = json.loads(str(s or "") or "null")
        return [float(x) for x in v] if isinstance(v, list) and v else None
    except Exception:
        return None


# ── 客户确认 / 关系陈述（纯函数）────────────────────────────────────────────

_YES_SELF = re.compile(
    r"(?:^|[\s,，.。!！~～]|\b)(?:yes+,?\s*)?(?:that(?:'s|’s|\s+is)|it(?:'s|’s|\s+is)|this\s+is|its)\s+me\b"
    r"|\b(?:yep|yeah|yes|yup|ya|sure)[,!\s]*(?:it(?:'s|’s)|that(?:'s|’s))\s+me\b"
    r"|\bthat(?:'s|’s)\s+(?:me|myself)\s+(?:in|at|on|lol|haha|😂|😉)"
    r"|\bme\s+(?:lol|haha|😂|at\s+|in\s+|after\s+|this\s+morning|yesterday|today)"
    r"|^\s*(?:it'?s\s+)?me[!.~\s]*$"
    r"|(?:是|就是|当然是|對|对)\s*我\s*(?:呀|啊|啦|哦|哟|呗|呢|自己)?(?:[，。！!~\s]|$)"
    r"|这(?:张|个|是)?\s*(?:就是|是)\s*我(?:本人|自己)?"
    r"|(?:^|[，。\s])我(?:本人|自己)(?:呀|啊|啦)?(?:[，。！!~\s]|$)",
    re.IGNORECASE,
)
_NO_SELF = re.compile(
    r"\b(?:that(?:'s|’s|\s+is)|it(?:'s|’s|\s+is)|this\s+is)\s+not\s+me\b|\bnot\s+me\b|\bisn'?t\s+me\b"
    r"|不是\s*我|并不是我|哪是我|不是本人",
    re.IGNORECASE,
)
_RELATIONS: Tuple[Tuple[str, str], ...] = (
    (r"\b(?:my\s+)?(?:little\s+|younger\s+|older\s+|big\s+|baby\s+)?sis(?:ter)?\b|我?(?:的)?(?:妹妹|姐姐|妹|姐)", "sister"),
    (r"\b(?:my\s+)?(?:little\s+|younger\s+|older\s+|big\s+)?bro(?:ther)?\b|我?(?:的)?(?:哥哥|弟弟|哥|弟)", "brother"),
    (r"\bmy\s+(?:mom|mother|mum|mama)\b|我?(?:的)?(?:妈妈|母亲|老妈|我妈)", "mother"),
    (r"\bmy\s+(?:dad|father|papa)\b|我?(?:的)?(?:爸爸|父亲|老爸|我爸)", "father"),
    (r"\bmy\s+(?:son|boy)\b|我?(?:的)?(?:儿子|大儿子|小儿子)", "son"),
    (r"\bmy\s+(?:daughter|girl)\b|我?(?:的)?(?:女儿|闺女)", "daughter"),
    (r"\bmy\s+(?:kids?|children|child)\b|我?(?:的)?(?:孩子|小孩|娃)", "child"),
    (r"\bmy\s+(?:wife|husband|hubby|spouse)\b|我?(?:的)?(?:老婆|老公|太太|先生|妻子|丈夫)", "spouse"),
    (r"\bmy\s+(?:gf|girlfriend|bf|boyfriend|partner|fianc[eé]e?)\b|我?(?:的)?(?:女朋友|男朋友|对象|女友|男友)", "partner"),
    (r"\bmy\s+(?:best\s+)?(?:friend|buddy|mate|homie|pal)s?\b|我?(?:的)?(?:朋友|闺蜜|哥们|兄弟|好友|同事)", "friend"),
    (r"\bmy\s+(?:dog|puppy|cat|kitten|pet)\b|我?(?:的|家)?(?:狗|狗狗|猫|猫猫|猫咪|宠物)", "pet"),
    (r"\bmy\s+(?:grand(?:ma|mother|pa|father)|nan(?:a)?)\b|我?(?:的)?(?:奶奶|外婆|爷爷|外公|姥姥|姥爷)", "grandparent"),
)


def detect_self_confirmation(text: str) -> str:
    """客户文本是否在确认/否认「图里是我」→ ``"yes"`` / ``"no"`` / ``""``。宁缺勿滥。"""
    t = str(text or "").strip()
    if not t or len(t) > 200:
        return ""
    if _NO_SELF.search(t):
        return "no"
    if _YES_SELF.search(t):
        return "yes"
    return ""


def detect_relation_statement(text: str) -> str:
    """客户文本里「这是我妹妹 / that's my sister / my dog」→ 关系标签（sister/brother/…）；无 → ''。

    只在句子像在介绍图中人/物时才认（含 this/that/it's/是/这是 或整句很短），防「我妹妹昨天来了」
    这类叙事误绑。"""
    t = str(text or "").strip()
    if not t or len(t) > 200:
        return ""
    intro = re.search(r"\b(?:this|that|it|these|those|here)(?:'s|’s|\s+is|\s+are)?\b|这(?:是|个|张|位)|那(?:是|个|张|位)|是我", t, re.I)
    short = len(t) <= 24
    if not (intro or short):
        return ""
    for rx, rel in _RELATIONS:
        if re.search(rx, t, re.IGNORECASE):
            return rel
    return ""


# ── 存储 ────────────────────────────────────────────────────────────────────

class VisualMemoryStore:
    def __init__(self, db_path: Any = ":memory:") -> None:
        self._is_mem = str(db_path) == ":memory:"
        if not self._is_mem:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            if not self._is_mem:
                try:
                    self._conn.execute("PRAGMA journal_mode=WAL")
                except Exception:
                    pass
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_DDL)
            self._conn.commit()

    # ── 观察 ──
    def record_observation(
        self, conv_key: str, *, message_id: str = "", ts: Optional[float] = None, sha1: str = "",
        subject: str = "", summary: str = "", label: str = "", matched: str = "", score: float = 0.0,
        embedding: Optional[Sequence[float]] = None, source: str = "ai_inferred",
        confirmed: bool = False, entity_id: int = 0,
    ) -> int:
        ck = str(conv_key or "").strip()
        if not ck:
            return 0
        try:
            with self._lock:
                cur = self._conn.execute(
                    "INSERT INTO visual_observations(conv_key, message_id, ts, sha1, subject, summary, label,"
                    " matched, score, embedding, source, confirmed, entity_id)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (ck, str(message_id or ""), float(ts if ts is not None else time.time()), str(sha1 or ""),
                     str(subject or "")[:32], str(summary or "")[:240], str(label or ""), str(matched or "")[:64],
                     float(score or 0.0), _dump(embedding), str(source or "ai_inferred"),
                     1 if confirmed else 0, int(entity_id or 0)),
                )
                self._conn.commit()
                return int(cur.lastrowid or 0)
        except Exception:
            logger.debug("[visual_memory] record_observation failed", exc_info=True)
            return 0

    def list_observations(self, conv_key: str, *, limit: int = 20, with_face_only: bool = False) -> List[Dict[str, Any]]:
        ck = str(conv_key or "").strip()
        if not ck:
            return []
        try:
            sql = "SELECT * FROM visual_observations WHERE conv_key=?"
            if with_face_only:
                sql += " AND embedding<>''"
            sql += " ORDER BY ts DESC, id DESC LIMIT ?"
            with self._lock:
                rows = self._conn.execute(sql, (ck, int(limit))).fetchall()
            return [self._obs(r) for r in rows]
        except Exception:
            return []

    def last_face_observation(self, conv_key: str, *, within_sec: float = 0) -> Optional[Dict[str, Any]]:
        rows = self.list_observations(conv_key, limit=1, with_face_only=True)
        if not rows:
            return None
        if within_sec > 0 and time.time() - float(rows[0].get("ts") or 0) > within_sec:
            return None
        return rows[0]

    @staticmethod
    def _obs(r: sqlite3.Row) -> Dict[str, Any]:
        d = dict(r)
        d["embedding"] = _load(d.get("embedding"))
        d["confirmed"] = bool(d.get("confirmed"))
        return d

    # ── 实体 ──
    def entities(self, conv_key: str, *, include_retired: bool = False) -> List[Dict[str, Any]]:
        ck = str(conv_key or "").strip()
        if not ck:
            return []
        try:
            sql = "SELECT * FROM visual_entities WHERE conv_key=?"
            if not include_retired:
                sql += " AND retired=0"
            sql += " ORDER BY updated_ts DESC"
            with self._lock:
                rows = self._conn.execute(sql, (ck,)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["embedding"] = _load(d.get("embedding"))
                out.append(d)
            return out
        except Exception:
            return []

    def self_entity(self, conv_key: str) -> Optional[Dict[str, Any]]:
        for e in self.entities(conv_key):
            if e.get("entity_type") == SELF_TYPE and e.get("embedding"):
                return e
        return None

    def self_prototype(self, conv_key: str) -> Tuple[Optional[List[float]], str]:
        """客户本人原型 → ``(向量, source)``。已确认实体优先；否则 ≥2 张互相一致的推断自拍给临时原型。"""
        ent = self.self_entity(conv_key)
        if ent is not None:
            return ent.get("embedding"), str(ent.get("source") or "user_confirmed")
        obs = [o for o in self.list_observations(conv_key, limit=12, with_face_only=True)
               if o.get("label") == "customer_self" and o.get("embedding")]
        if len(obs) >= 2:
            vecs = [o["embedding"] for o in obs]
            # 互相一致才当同一个人（防两张不同人的「自拍」被平均成幽灵脸）
            base = vecs[0]
            agree = [v for v in vecs if cosine(base, v) >= MATCH]
            if len(agree) >= 2:
                return mean_vector(agree), "ai_inferred"
        return None, ""

    def _upsert_entity(self, conv_key: str, entity_type: str, *, relation: str, label: str,
                       embedding: Optional[Sequence[float]], source: str) -> int:
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM visual_entities WHERE conv_key=? AND entity_type=? AND relation=? AND retired=0",
                (conv_key, entity_type, relation)).fetchone()
            if row is None:
                cur = self._conn.execute(
                    "INSERT INTO visual_entities(conv_key, entity_type, label, relation, source, confidence,"
                    " embedding, n_samples, created_ts, updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (conv_key, entity_type, label, relation, source, 1.0 if source == "user_confirmed" else 0.6,
                     _dump(embedding), 1 if embedding else 0, now, now))
                self._conn.commit()
                return int(cur.lastrowid or 0)
            old = _load(row["embedding"])
            n = int(row["n_samples"] or 0)
            new = embedding
            if old and embedding:
                # 增量均值（保留归一）
                merged = [o * n + e for o, e in zip(old, embedding)]
                new = mean_vector([merged])
            self._conn.execute(
                "UPDATE visual_entities SET embedding=?, n_samples=?, updated_ts=?, source=?, label=? WHERE id=?",
                (_dump(new or old), n + (1 if embedding else 0), now,
                 "user_confirmed" if (source == "user_confirmed" or row["source"] == "user_confirmed") else source,
                 label or row["label"], int(row["id"])))
            self._conn.commit()
            return int(row["id"])

    def confirm_self(self, conv_key: str, *, observation_id: int = 0, within_sec: float = 0) -> bool:
        """客户确认「图里是我」：把（指定 / 最近一张带脸）观察升为 user_confirmed 本人，并入 self 实体。"""
        ck = str(conv_key or "").strip()
        if not ck:
            return False
        try:
            obs = None
            if observation_id:
                with self._lock:
                    r = self._conn.execute("SELECT * FROM visual_observations WHERE id=? AND conv_key=?",
                                           (int(observation_id), ck)).fetchone()
                obs = self._obs(r) if r else None
            else:
                obs = self.last_face_observation(ck, within_sec=within_sec)
            if not obs or not obs.get("embedding"):
                return False
            if obs.get("label") == "persona":
                return False   # 人设自己的照片，客户说「是我」不成立
            eid = self._upsert_entity(ck, SELF_TYPE, relation="", label="self",
                                      embedding=obs["embedding"], source="user_confirmed")
            with self._lock:
                self._conn.execute(
                    "UPDATE visual_observations SET label='customer_self', matched='customer_self', source='user_confirmed',"
                    " confirmed=1, entity_id=? WHERE id=?", (eid, int(obs["id"])))
                self._conn.commit()
            return True
        except Exception:
            logger.debug("[visual_memory] confirm_self failed", exc_info=True)
            return False

    def deny_self(self, conv_key: str, *, within_sec: float = 0) -> bool:
        """客户说「不是我」：最近一张带脸观察若被标 customer_self → 改 unknown（user_confirmed 否定）。"""
        try:
            obs = self.last_face_observation(str(conv_key or ""), within_sec=within_sec)
            if not obs:
                return False
            with self._lock:
                self._conn.execute(
                    "UPDATE visual_observations SET label='unknown', matched='', source='user_confirmed', confirmed=1"
                    " WHERE id=?", (int(obs["id"]),))
                self._conn.commit()
            return True
        except Exception:
            return False

    def confirm_relation(self, conv_key: str, relation: str, *, observation_id: int = 0,
                         within_sec: float = 0) -> bool:
        """客户说「这是我妹妹」：最近一张带脸观察 → 关系实体（user_confirmed）。"""
        ck = str(conv_key or "").strip()
        rel = str(relation or "").strip()
        if not ck or not rel:
            return False
        try:
            obs = None
            if observation_id:
                with self._lock:
                    r = self._conn.execute("SELECT * FROM visual_observations WHERE id=? AND conv_key=?",
                                           (int(observation_id), ck)).fetchone()
                obs = self._obs(r) if r else None
            else:
                obs = self.last_face_observation(ck, within_sec=within_sec)
            if not obs:
                return False
            eid = self._upsert_entity(ck, RELATION_TYPE, relation=rel, label=rel,
                                      embedding=obs.get("embedding"), source="user_confirmed")
            with self._lock:
                self._conn.execute(
                    "UPDATE visual_observations SET label='known', matched=?, source='user_confirmed', confirmed=1,"
                    " entity_id=? WHERE id=?", (rel, eid, int(obs["id"])))
                self._conn.commit()
            return True
        except Exception:
            logger.debug("[visual_memory] confirm_relation failed", exc_info=True)
            return False

    def known_relations(self, conv_key: str) -> List[Dict[str, Any]]:
        return [e for e in self.entities(conv_key) if e.get("entity_type") == RELATION_TYPE and e.get("embedding")]

    def retire_entity(self, conv_key: str, entity_id: int) -> bool:
        try:
            with self._lock:
                self._conn.execute("UPDATE visual_entities SET retired=1, updated_ts=? WHERE id=? AND conv_key=?",
                                   (time.time(), int(entity_id), str(conv_key or "")))
                self._conn.commit()
            return True
        except Exception:
            return False

    def delete_conv(self, conv_key: str) -> int:
        try:
            with self._lock:
                n = self._conn.execute("DELETE FROM visual_observations WHERE conv_key=?", (str(conv_key or ""),)).rowcount
                self._conn.execute("DELETE FROM visual_entities WHERE conv_key=?", (str(conv_key or ""),))
                self._conn.commit()
            return int(n or 0)
        except Exception:
            return 0

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


# ── 主动触达 / goal 消费：一句「TA 的照片记忆」（带外、无方括号标签）────────────

_REL_ZH = {"sister": "妹妹/姐姐", "brother": "哥哥/弟弟", "mother": "妈妈", "father": "爸爸", "son": "儿子",
           "daughter": "女儿", "child": "孩子", "spouse": "爱人", "partner": "对象", "friend": "朋友",
           "pet": "宠物", "grandparent": "长辈"}


def _age_label(age_sec: float) -> str:
    s = max(0.0, float(age_sec or 0.0))
    if s < 3600:
        return "刚刚"
    if s < 86400:
        return f"{int(s // 3600)}小时前"
    if s < 2 * 86400:
        return "昨天"
    return f"{int(s // 86400)}天前"


def memory_note(store: Optional["VisualMemoryStore"], conv_key: str, *, who: str = "TA",
                now: Optional[float] = None, max_chars: int = 220) -> str:
    """proactive / goal 注入用：「{who} 的照片记忆：本人自拍 N 张（最近 X 前，已确认）；确认过的
    关系人：妹妹 …；最近一张图：…（X 前）」。无记录 → ''。绝不抛。

    只说**已确认**的关系与「已确认 / 推断」标注清楚的本人；unknown 一律不点名（防「Marina」式
    把画面猜成事实）。规则句钉住：照片记忆只能用来接话，不能编造照片里没有的细节。"""
    try:
        st = store if store is not None else get_visual_memory_store()
        if st is None:
            return ""
        ck = str(conv_key or "").strip()
        if not ck:
            return ""
        n = float(now if now is not None else time.time())
        obs = st.list_observations(ck, limit=30)
        if not obs:
            return ""
        parts: List[str] = []
        selfs = [o for o in obs if o.get("label") == "customer_self"]
        if selfs:
            confirmed = any(o.get("confirmed") for o in selfs) or st.self_entity(ck) is not None
            tag = "已确认是本人" if confirmed else "推断是本人、未确认"
            parts.append(f"本人自拍 {len(selfs)} 张（最近 {_age_label(n - float(selfs[0].get('ts') or 0))}，{tag}）")
        rels = st.known_relations(ck)
        if rels:
            names = []
            for e in rels[:4]:
                rel = str(e.get("relation") or "")
                names.append(_REL_ZH.get(rel, rel))
            parts.append("确认过的关系人：" + "、".join(names))
        last = obs[0]
        summ = str(last.get("summary") or "").strip()
        if summ and last.get("label") in ("no_face", "unknown", "known", "customer_self", "persona"):
            lab = last.get("label")
            head = {"no_face": "最近一张图（无人物）", "unknown": "最近一张图（有人但未确认是谁）",
                    "persona": "最近一张图（是我方人设自己的照片）"}.get(lab, "最近一张图")
            parts.append(f"{head}：{summ[:60]}（{_age_label(n - float(last.get('ts') or 0))}）")
        if not parts:
            return ""
        s = f"{who}的照片记忆：" + "；".join(parts) + "。规则：只能拿来自然接话（如「上次那张…」），不编造照片里没有的细节，未确认是谁的人不要点名。"
        if len(s) > max_chars + 90:
            s = s[: max_chars + 89] + "…"
        return s
    except Exception:
        return ""


# ── 进程单例（config_dir/visual_memory.db）────────────────────────────────────
_STORE: Optional[VisualMemoryStore] = None
_STORE_LOCK = threading.Lock()


def get_visual_memory_store() -> Optional[VisualMemoryStore]:
    global _STORE
    if _STORE is not None:
        return _STORE
    with _STORE_LOCK:
        if _STORE is not None:
            return _STORE
        try:
            from src.licensing.data_paths import config_dir
            p = Path(config_dir()) / "visual_memory.db"
        except Exception:
            p = Path("config") / "visual_memory.db"
        try:
            _STORE = VisualMemoryStore(p)
        except Exception:
            logger.debug("[visual_memory] store open failed", exc_info=True)
            return None
        return _STORE


__all__ = [
    "VisualMemoryStore", "get_visual_memory_store", "cosine", "mean_vector", "memory_note",
    "detect_self_confirmation", "detect_relation_statement", "SELF_TYPE", "RELATION_TYPE", "MATCH",
]
