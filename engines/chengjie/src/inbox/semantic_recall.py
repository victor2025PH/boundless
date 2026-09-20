"""inbox 语义召回（2026-09-18 N2）：记忆探针的「同义改写」兜底。

事故链：客户问「还记得我上次说的那个涮肉店吗」，inbox 里原话是「火锅」——
:mod:`src.inbox.memory_probe` 的关键词子串命中为零 → 注「坦白说记不清」→ 客户
明明说过却被告知「没印象」，这是记忆探针上线后可预见的第一类漏召回。

做法（不动 store.py / 不改 messages 表 / 不引入外部服务）：
- 侧库 ``inbox_semantic.db``（与 ``inbox.db`` 同目录，account_blocklist.db 同手法；无路径 →
  进程内 :memory:），表 ``msg_vec`` 按 (conversation_id, message_id) 存 float32 BLOB，
  带 ``model`` 标签——换嵌入模型后旧向量自然失效，不会拿两个向量空间算假相似度；
- 向量来自现役 ``AIClient.embed()``（批量、熔断、双端点 failover 都是它的）；
  ``embedding_status().state != "ready"`` → 本轮静默跳过；
- **懒索引**：只在探针命中（recall 类）时对**本会话**最近 ``index_cap`` 行补向量
  （一次批量调用，通常 <1s），之后增量；探针是稀有事件，草稿链本身就是多秒 LLM 调用，
  这点延迟可接受，且整体套 ``timeout_sec`` 超时即回落关键词证据；
- 只做召回不做裁决：返回候选**行**给 memory_probe 合并进证据块，「只可引用原文 /
  查不到就坦白」的硬规则不变。

配置 ``inbox.semantic_recall``（默认**开**——嵌入未配置时本模块等价于关）：
    {enabled: true, min_cosine: 0.50, top_k: 4, index_cap: 300, timeout_sec: 8, batch: 64}

观测走 inbox_draft 指标同一根管道（``semantic_recall:hit|miss|skip_unready|timeout|error``），
零路由改动即在 ``draft_pipeline.total`` 里可见；进程内 :func:`snapshot` 供测试 / 诊断。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.utils.episodic_vector import blob_to_vec, cosine_similarity, vec_to_blob

logger = logging.getLogger(__name__)

DB_NAME = "inbox_semantic.db"

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "min_cosine": 0.50,
    "top_k": 4,
    "index_cap": 300,
    "timeout_sec": 8.0,
    "batch": 64,
    "min_chars": 4,        # 太短的行（「嗯」「好」）不进索引：没语义、白花向量
}

_DDL = f"""
CREATE TABLE IF NOT EXISTS msg_vec (
    conversation_id TEXT NOT NULL,
    message_id      TEXT NOT NULL,
    ts              REAL NOT NULL DEFAULT 0,
    role            TEXT NOT NULL DEFAULT '',
    text_hash       TEXT NOT NULL DEFAULT '',
    model           TEXT NOT NULL DEFAULT '',
    dim             INTEGER NOT NULL DEFAULT 0,
    vec             BLOB NOT NULL,
    created_at      REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (conversation_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_msgvec_conv ON msg_vec(conversation_id);
"""

_LOCK = threading.Lock()
_STATS: Dict[str, Any] = {
    "checked": 0, "hit": 0, "miss": 0, "skip_unready": 0, "timeout": 0, "error": 0,
    "embedded_rows": 0, "last_ts": 0.0, "last_hit": None,
}


# ── 配置 ─────────────────────────────────────────────────────────────────────

def resolve_semantic_recall_cfg(config: Any) -> Dict[str, Any]:
    """``inbox.semantic_recall`` → 归一化配置（默认开；容错任意脏值）。"""
    out = dict(DEFAULTS)
    c = config if isinstance(config, dict) else {}
    raw = (c.get("inbox") or {}).get("semantic_recall") if isinstance(c.get("inbox"), dict) else None
    if isinstance(raw, bool):
        out["enabled"] = raw
        return out
    if not isinstance(raw, dict):
        return out
    out["enabled"] = bool(raw.get("enabled", True))
    try:
        out["min_cosine"] = min(0.99, max(0.1, float(raw.get("min_cosine", out["min_cosine"]))))
    except (TypeError, ValueError):
        pass
    for k, lo, hi in (("top_k", 1, 12), ("index_cap", 20, 2000), ("batch", 8, 256),
                      ("min_chars", 1, 40)):
        try:
            out[k] = max(lo, min(hi, int(raw.get(k, out[k]))))
        except (TypeError, ValueError):
            pass
    try:
        out["timeout_sec"] = max(1.0, min(60.0, float(raw.get("timeout_sec", out["timeout_sec"]))))
    except (TypeError, ValueError):
        pass
    return out


# ── 侧库 ─────────────────────────────────────────────────────────────────────

class SemanticIndex:
    """会话消息向量侧库（线程安全；按 (conversation_id, message_id) 幂等写）。"""

    def __init__(self, db_path: Optional[Path]) -> None:
        self._path = Path(db_path) if db_path else None
        self._lock = threading.RLock()
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self._path), check_same_thread=False, timeout=10)
            try:
                self._conn.execute("PRAGMA journal_mode=WAL")
            except Exception:
                pass
        else:
            self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_DDL)
            self._conn.commit()

    @property
    def path(self) -> Optional[Path]:
        return self._path

    def have(self, conversation_id: str) -> Dict[str, Tuple[str, str]]:
        """已索引 message_id → (text_hash, model)。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT message_id, text_hash, model FROM msg_vec WHERE conversation_id=?",
                (str(conversation_id),)).fetchall()
        return {str(r["message_id"]): (str(r["text_hash"]), str(r["model"])) for r in rows}

    def put_many(self, conversation_id: str,
                 items: Sequence[Tuple[str, float, str, str, str, List[float]]]) -> int:
        """items: (message_id, ts, role, text_hash, model, vec)。空向量跳过。返回写入条数。"""
        now = time.time()
        n = 0
        with self._lock:
            for mid, ts, role, h, model, vec in items:
                if not vec:
                    continue
                self._conn.execute(
                    "INSERT OR REPLACE INTO msg_vec(conversation_id, message_id, ts, role, text_hash,"
                    " model, dim, vec, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (str(conversation_id), str(mid), float(ts or 0), str(role or ""), str(h or ""),
                     str(model or ""), len(vec), vec_to_blob(list(vec)), now))
                n += 1
            self._conn.commit()
        return n

    def vectors(self, conversation_id: str, model: str = "") -> List[Tuple[str, float, str, List[float]]]:
        """(message_id, ts, role, vec)；``model`` 非空时只取同模型向量。"""
        with self._lock:
            if model:
                rows = self._conn.execute(
                    "SELECT message_id, ts, role, vec FROM msg_vec WHERE conversation_id=? AND model=?",
                    (str(conversation_id), str(model))).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT message_id, ts, role, vec FROM msg_vec WHERE conversation_id=?",
                    (str(conversation_id),)).fetchall()
        out = []
        for r in rows:
            v = blob_to_vec(r["vec"])
            if v:
                out.append((str(r["message_id"]), float(r["ts"] or 0), str(r["role"] or ""), v))
        return out

    def count(self, conversation_id: str = "") -> int:
        with self._lock:
            if conversation_id:
                row = self._conn.execute(
                    "SELECT COUNT(*) FROM msg_vec WHERE conversation_id=?",
                    (str(conversation_id),)).fetchone()
            else:
                row = self._conn.execute("SELECT COUNT(*) FROM msg_vec").fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


_INSTANCES: Dict[str, SemanticIndex] = {}
_INST_LOCK = threading.Lock()


def semantic_db_path(store: Any) -> Optional[Path]:
    """与 inbox.db 同目录的侧库路径；store 无 ``_db_path`` → None（内存档）。"""
    p = getattr(store, "_db_path", None)
    if p is None:
        return None
    try:
        return Path(str(p)).parent / DB_NAME
    except Exception:
        return None


def get_index(store: Any = None, *, db_path: Any = None) -> SemanticIndex:
    """按路径取单例（同一 inbox.db 目录一份；无路径 → 进程内 :memory: 一份）。绝不抛。"""
    path = Path(str(db_path)) if db_path else semantic_db_path(store)
    key = str(path) if path else ":memory:"
    with _INST_LOCK:
        inst = _INSTANCES.get(key)
        if inst is None:
            try:
                inst = SemanticIndex(path)
            except Exception:
                logger.debug("[semantic_recall] 打开 %s 失败，回落内存", key, exc_info=True)
                inst = SemanticIndex(None)
            _INSTANCES[key] = inst
    return inst


def reset_for_tests() -> None:
    with _INST_LOCK:
        for inst in _INSTANCES.values():
            inst.close()
        _INSTANCES.clear()
    with _LOCK:
        for k in ("checked", "hit", "miss", "skip_unready", "timeout", "error", "embedded_rows"):
            _STATS[k] = 0
        _STATS["last_ts"] = 0.0
        _STATS["last_hit"] = None


# ── 纯函数 ───────────────────────────────────────────────────────────────────

def _row_text(r: Dict[str, Any]) -> str:
    return str(r.get("text") or r.get("content") or "").strip()


def _row_role(r: Dict[str, Any]) -> str:
    role = str(r.get("role") or "")
    if role:
        return "user" if role == "user" else "assistant"
    return "user" if str(r.get("direction") or "") in ("in", "inbound") else "assistant"


def _row_ts(r: Dict[str, Any]) -> float:
    try:
        return float(r.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _row_id(r: Dict[str, Any]) -> str:
    mid = str(r.get("message_id") or r.get("id") or "").strip()
    if mid:
        return mid
    # 无 id 的行（测试假件 / 客户端归一化行）：ts+文本 hash 兜底成稳定键
    return "h:" + hashlib.sha1(f"{_row_ts(r)}|{_row_text(r)[:200]}".encode("utf-8")).hexdigest()[:20]


def text_hash(s: str) -> str:
    return hashlib.sha1(str(s or "").encode("utf-8")).hexdigest()[:24]


def indexable_rows(rows: Sequence[Dict[str, Any]], *, min_chars: int = 4,
                   current_text: str = "") -> List[Dict[str, Any]]:
    """可进索引的行：有正文、非系统占位（``[…]`` 开头）、≥min_chars、不是当前这条问题。纯函数。"""
    cur = str(current_text or "").strip()
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        t = _row_text(r)
        if not t or t.startswith("[") or len(t) < int(min_chars):
            continue
        if cur and t == cur and _row_role(r) == "user":
            continue
        out.append(r)
    return out


def pick_rows_to_embed(rows: Sequence[Dict[str, Any]], have: Dict[str, Tuple[str, str]],
                       model: str, *, cap: int) -> List[Dict[str, Any]]:
    """缺向量 / 文本变了 / 模型换了的行，**最近的优先**，最多 cap 条。纯函数。"""
    todo: List[Dict[str, Any]] = []
    for r in sorted(rows, key=_row_ts, reverse=True):
        mid = _row_id(r)
        prev = have.get(mid)
        if prev is not None and prev[0] == text_hash(_row_text(r)) and prev[1] == model:
            continue
        todo.append(r)
        if len(todo) >= int(cap):
            break
    return todo


def rank(query_vec: Sequence[float], cands: Sequence[Tuple[str, float, str, List[float]]],
         *, top_k: int, min_cosine: float) -> List[Tuple[str, float]]:
    """(message_id, cosine) 按分数降序；客户侧行同分优先。纯函数。"""
    scored: List[Tuple[float, int, str]] = []
    for mid, _ts, role, vec in cands:
        c = cosine_similarity(list(query_vec), vec)
        if c >= float(min_cosine):
            scored.append((c, 1 if role == "user" else 0, mid))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [(mid, round(c, 4)) for c, _u, mid in scored[: max(1, int(top_k))]]


# ── 嵌入能力探测 ──────────────────────────────────────────────────────────────

def embedding_ready(ai_client: Any) -> bool:
    """有可用嵌入路径且未熔断。无 ``embedding_status`` 的旧客户端 → 只看有没有 ``embed``。"""
    if ai_client is None or not hasattr(ai_client, "embed"):
        return False
    st = getattr(ai_client, "embedding_status", None)
    if callable(st):
        try:
            return str((st() or {}).get("state") or "") == "ready"
        except Exception:
            return False
    return True


def embedding_model_tag(ai_client: Any) -> str:
    try:
        return str(getattr(ai_client, "_embedding_model", "") or "").strip() or "default"
    except Exception:
        return "default"


def _bump(key: str, hit: Optional[Dict[str, Any]] = None) -> None:
    with _LOCK:
        _STATS[key] = int(_STATS.get(key) or 0) + 1
        _STATS["last_ts"] = time.time()
        if hit is not None:
            _STATS["last_hit"] = hit
    try:
        from src.monitoring.metrics_store import get_metrics_store
        get_metrics_store().record_inbox_draft_event(f"semantic_recall:{key}")
    except Exception:
        pass


def snapshot() -> Dict[str, Any]:
    with _LOCK:
        out = dict(_STATS)
    out["active"] = bool(out.get("checked"))
    return out


# ── 主流程 ───────────────────────────────────────────────────────────────────

EmbedFn = Callable[[List[str]], Any]   # async: List[str] -> List[List[float]]


async def ensure_indexed(index: SemanticIndex, conversation_id: str,
                         rows: Sequence[Dict[str, Any]], embed_fn: EmbedFn, model: str,
                         *, cap: int = 300, batch: int = 64) -> int:
    """为会话补向量（懒索引，最近优先，最多 cap 条）。返回新写入条数；嵌入失败的位置不写。"""
    have = index.have(conversation_id)
    todo = pick_rows_to_embed(rows, have, model, cap=cap)
    if not todo:
        return 0
    written = 0
    for i in range(0, len(todo), max(1, int(batch))):
        chunk = todo[i:i + max(1, int(batch))]
        texts = [_row_text(r)[:500] for r in chunk]
        vecs = await embed_fn(texts)
        if not vecs or len(vecs) != len(chunk):
            # 批量数量不齐＝端点异常（熔断中返回 []）：本批放弃，后续批也别再打
            break
        items = [(_row_id(r), _row_ts(r), _row_role(r), text_hash(_row_text(r)), model, v)
                 for r, v in zip(chunk, vecs)]
        written += index.put_many(conversation_id, items)
    if written:
        with _LOCK:
            _STATS["embedded_rows"] = int(_STATS.get("embedded_rows") or 0) + written
    return written


async def recall_rows_async(
    store: Any, conversation_id: str, text: str, *,
    ai_client: Any, config: Any = None, index: Optional[SemanticIndex] = None,
    now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """探针（recall 类）命中后的语义召回 → 候选行（ts 升序）；不可用 / 超时 / 异常 → []。

    调用方（persona_reply）把结果作为 ``semantic_rows`` 交给
    :func:`src.inbox.memory_probe.probe_detail_from_store` 合并；本函数**不**判定探针。
    """
    cfg = resolve_semantic_recall_cfg(config)
    cid = str(conversation_id or "").strip()
    q = str(text or "").strip()
    if not cfg.get("enabled") or store is None or not cid or len(q) < 2:
        return []
    if not embedding_ready(ai_client):
        _bump("skip_unready")
        return []
    _bump("checked")
    model = embedding_model_tag(ai_client)
    idx = index if index is not None else get_index(store)

    async def _run() -> List[Dict[str, Any]]:
        from src.inbox.memory_probe import load_probe_rows
        rows = indexable_rows(load_probe_rows(store, cid, scan_limit=int(cfg["index_cap"])),
                              min_chars=int(cfg["min_chars"]), current_text=q)
        if not rows:
            return []
        by_id = {_row_id(r): r for r in rows}
        # 补索引（已索引的会话这里是零调用）→ 再嵌查询；稳态每次探针只多一次单条嵌入
        await ensure_indexed(idx, cid, rows, ai_client.embed, model,
                             cap=int(cfg["index_cap"]), batch=int(cfg["batch"]))
        qv = await ai_client.embed([q[:500]])
        if not qv or not qv[0]:
            return []
        hits = rank(qv[0], idx.vectors(cid, model), top_k=int(cfg["top_k"]),
                    min_cosine=float(cfg["min_cosine"]))
        out = [dict(by_id[mid], _cosine=c) for mid, c in hits if mid in by_id]
        out.sort(key=_row_ts)
        return out

    try:
        found = await asyncio.wait_for(_run(), timeout=float(cfg["timeout_sec"]))
    except asyncio.TimeoutError:
        _bump("timeout")
        return []
    except Exception:
        logger.debug("[semantic_recall] 召回异常（回落关键词）", exc_info=True)
        _bump("error")
        return []
    if found:
        _bump("hit", {"ts": float(now or time.time()), "conv": cid, "q": q[:60],
                      "n": len(found),
                      "top": max(float(r.get("_cosine") or 0) for r in found)})
    else:
        _bump("miss")
    return found


__all__ = [
    "DEFAULTS", "DB_NAME", "SemanticIndex", "resolve_semantic_recall_cfg",
    "semantic_db_path", "get_index", "reset_for_tests", "indexable_rows",
    "pick_rows_to_embed", "rank", "text_hash", "embedding_ready", "embedding_model_tag",
    "ensure_indexed", "recall_rows_async", "snapshot",
]
