# -*- coding: utf-8 -*-
"""assistant 产品帮助语料库（独立 db + 进程内 BM25，绝不触碰客户话术 KB）。

⚠ 为什么不按原设计「复用 KnowledgeBaseStore 另开 db 文件」：该类的
BM25/向量索引是**类属性共享态**（`KnowledgeBaseStore._index` /
`_index_dirty`，见 kb_store.py:354-358、566），第二个不同 db 的实例会
与客户话术库实例互相清洗索引——客户聊天 RAG 可能检索到后台操作手册，
或帮助检索命中客服话术。故这里只复用它的**无共享态零件**：
`_BM25Index`（实例态）与 `_tokenize`（纯函数），库表自建。

语料规模 ~200-300 条（help_terms + nav_schema + docs 精选），BM25 +
CJK bigram 已足；向量融合留 P1（需要时走 ai_client.embed 存列即可）。
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

logger = logging.getLogger("ai_chat_assistant.assistant.help_kb")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS help_entries (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    title_en TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    content_en TEXT NOT NULL DEFAULT '',
    keywords TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    anchor TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT ''
);
"""

# 幂等迁移（house 约定：集中列表逐条 try，已存在即跳过）。anchor=「带我去」
# 聚光灯的目标选择器（P3 2026-08-21）：跳页后高亮该元素；空=只跳页不聚光。
_MIGRATIONS = (
    "ALTER TABLE help_entries ADD COLUMN anchor TEXT NOT NULL DEFAULT ''",
)


class HelpKB:
    """独立帮助库：sqlite 持久 + 进程内 BM25 实例索引（零共享态）。"""

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            from src.licensing.data_paths import config_dir

            db_path = config_dir() / "assistant_help.db"
        self._path = Path(db_path)
        self._lock = threading.Lock()
        self._index = None  # 懒建：_BM25Index 实例（本实例私有）
        self._index_dirty = True
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self._path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self) -> None:
        try:
            with self._lock, self._connect() as conn:
                conn.executescript(_SCHEMA)
                for stmt in _MIGRATIONS:
                    try:
                        conn.execute(stmt)
                    except sqlite3.OperationalError:
                        pass  # 列已存在（新建库走 _SCHEMA 全量）
        except Exception:
            logger.warning("assistant help_kb schema init 失败", exc_info=True)

    # ------------------------------------------------------------------ 写
    def upsert_entries(self, entries: list[dict]) -> int:
        """按 id 幂等 upsert；返回写入条数。entry 键：
        id/title/title_en/content/content_en/keywords/source/path。"""
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        n = 0
        with self._lock, self._connect() as conn:
            for e in entries:
                eid = str(e.get("id") or "").strip()
                title = str(e.get("title") or "").strip()
                if not eid or not title:
                    continue
                conn.execute(
                    "INSERT OR REPLACE INTO help_entries"
                    "(id,title,title_en,content,content_en,keywords,source,path,"
                    "anchor,enabled,updated_at) VALUES(?,?,?,?,?,?,?,?,?,1,?)",
                    (
                        eid[:80],
                        title[:200],
                        str(e.get("title_en") or "")[:200],
                        str(e.get("content") or "")[:4000],
                        str(e.get("content_en") or "")[:4000],
                        str(e.get("keywords") or "")[:400],
                        str(e.get("source") or "")[:60],
                        str(e.get("path") or "")[:160],
                        str(e.get("anchor") or "")[:120],
                        now,
                    ),
                )
                n += 1
            self._index_dirty = True
        return n

    # ------------------------------------------------------------------ 读
    def count(self) -> int:
        try:
            with self._lock, self._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM help_entries WHERE enabled=1"
                ).fetchone()
                return int(row["n"] or 0)
        except Exception:
            return 0

    def _rebuild_index(self) -> None:
        from src.utils.kb_store import _BM25Index

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id,title,title_en,content,content_en,keywords,path "
                "FROM help_entries WHERE enabled=1"
            ).fetchall()
        docs = []
        for r in rows:
            # 中英文一起进索引：查询任一语言都可命中（title 权重最高，
            # keywords 进 triggers 权重位，正文进 scenario 权重位）
            docs.append(
                {
                    "id": str(r["id"]),
                    "triggers": str(r["keywords"] or ""),
                    "title": " ".join(
                        x for x in (r["title"], r["title_en"]) if x
                    ),
                    "scenario": " ".join(
                        x for x in (r["content"], r["content_en"]) if x
                    ),
                    "steps": str(r["path"] or ""),
                    "principles": "",
                }
            )
        idx = _BM25Index()
        idx.build(docs)
        self._index = idx
        self._index_dirty = False

    def search(self, query: str, top_k: int = 3, lang: str = "zh") -> list[dict]:
        """检索：返回 [{id,title,content,path,score}]（按 lang 取语言字段，
        英文缺失回落中文）。空库/异常返回空表，绝不抛。"""
        q = str(query or "").strip()
        if not q:
            return []
        try:
            with self._lock:
                if self._index_dirty or self._index is None:
                    self._rebuild_index()
                ranked = self._index.search(q, top_k=max(1, top_k))
                if not ranked:
                    return []
                ids = [doc_id for doc_id, _ in ranked]
                score_map = {doc_id: s for doc_id, s in ranked}
                with self._connect() as conn:
                    ph = ",".join("?" * len(ids))
                    rows = conn.execute(
                        f"SELECT * FROM help_entries WHERE id IN ({ph}) AND enabled=1",
                        ids,
                    ).fetchall()
                row_map = {str(r["id"]): r for r in rows}
        except Exception:
            logger.warning("assistant help_kb search 失败", exc_info=True)
            return []
        out: list[dict] = []
        for doc_id in ids:
            r = row_map.get(doc_id)
            if r is None:
                continue
            if lang == "en":
                title = str(r["title_en"] or r["title"])
                content = str(r["content_en"] or r["content"])
            else:
                title = str(r["title"])
                content = str(r["content"])
            try:
                anchor = str(r["anchor"] or "")
            except (IndexError, KeyError):
                anchor = ""  # 旧行/迁移前快照兜底
            out.append(
                {
                    "id": doc_id,
                    "title": title,
                    "content": content,
                    "path": str(r["path"] or ""),
                    "anchor": anchor,
                    "score": round(float(score_map.get(doc_id, 0.0)), 4),
                }
            )
        return out


_KB: HelpKB | None = None
_KB_LOCK = threading.Lock()


def get_help_kb() -> HelpKB:
    global _KB
    if _KB is None:
        with _KB_LOCK:
            if _KB is None:
                _KB = HelpKB()
    return _KB


def help_corpus_status() -> dict:
    """只读口（Q-10 #254 / 22KVXF ⑤）：给 KB 页 / 自检说明「帮助语料在哪」。

    帮助语料（~295 条）自始只存这份独立库 ``assistant_help.db``，**从不进用户
    知识库 kb_entries**；KB 页「一键清除预置条目」的 help 档清的是历史残留、
    不会碰这里。不建索引、不改任何状态：单例已建就复用，未建则单独开一次连接数数。
    """
    kb = _KB
    try:
        if kb is None:
            kb = HelpKB()
        return {"store": "assistant_help.db", "path": str(kb._path), "count": int(kb.count()),
                "in_user_kb": False}
    except Exception:  # noqa: BLE001
        logger.debug("help_corpus_status 读取失败", exc_info=True)
        return {"store": "assistant_help.db", "path": "", "count": -1, "in_user_kb": False}


__all__ = ["HelpKB", "get_help_kb", "help_corpus_status"]
