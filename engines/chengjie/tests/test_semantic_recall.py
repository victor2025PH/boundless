# -*- coding: utf-8 -*-
"""inbox 语义召回（2026-09-18 N2）：记忆探针的同义改写兜底。

钉住：
- 配置解析（默认开、脏值容错、bool 短写）；
- 侧库 SemanticIndex：幂等写 / have / 按模型取向量 / 内存档；
- 纯函数：indexable_rows（剔占位 / 短行 / 当前问题）、pick_rows_to_embed（缺向量 / 文本变 / 模型换，最近优先，cap）、rank（阈值 + 客户侧同分优先）；
- ensure_indexed 增量：第二次零嵌入；批量数量不齐即停不写脏；
- recall_rows_async 端到端：假 store + 假 ai_client（同义词映射到同向量）→ 「涮肉」召回「火锅」行；
  与 probe_detail_from_store 合并后证据块含原文；
- 降级：嵌入未就绪 → [] + skip_unready；embed 回空 → []；超时 → [] + timeout；enabled=false → 零调用；
- 侧库路径随 inbox.db 目录；无 _db_path → 内存。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List

import pytest

from src.inbox import semantic_recall as sr
from src.inbox.semantic_recall import (
    SemanticIndex, ensure_indexed, indexable_rows, pick_rows_to_embed, rank,
    recall_rows_async, resolve_semantic_recall_cfg, text_hash,
)


@pytest.fixture(autouse=True)
def _reset():
    sr.reset_for_tests()
    yield
    sr.reset_for_tests()


def _r(mid, ts, direction, text):
    return {"message_id": mid, "ts": ts, "direction": direction, "text": text}


# 同义词 → 同一向量空间：火锅/涮肉 同向；猫/狗 另一向；其余正交
_VOCAB = {
    "火锅": [1.0, 0.0, 0.0, 0.0], "涮肉": [0.95, 0.05, 0.0, 0.0],
    "猫": [0.0, 1.0, 0.0, 0.0], "狗": [0.0, 0.9, 0.1, 0.0],
    "设计": [0.0, 0.0, 1.0, 0.0],
}


def _embed_text(t: str) -> List[float]:
    for k, v in _VOCAB.items():
        if k in t:
            return list(v)
    return [0.0, 0.0, 0.0, 1.0]


class _AI:
    def __init__(self, state="ready", fail=False, delay=0.0):
        self._embedding_model = "bge-m3"
        self._state = state
        self._fail = fail
        self._delay = delay
        self.calls: List[List[str]] = []

    def embedding_status(self):
        return {"state": self._state}

    async def embed(self, texts):
        self.calls.append(list(texts))
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fail:
            return []
        return [_embed_text(t) for t in texts]


class _Store:
    def __init__(self, rows, db_path=None):
        self.rows = rows
        if db_path is not None:
            self._db_path = db_path

    def list_recent_messages(self, cid, *, limit=50, include_deleted=True):
        return list(self.rows)[-limit:]

    def list_messages(self, cid, *, limit=50):
        return list(self.rows)[:limit]

    def count_messages(self, cid=""):
        return len(self.rows)


ROWS = [
    _r("m1", 1000, "in", "我叫阿明，在深圳做设计"),
    _r("m2", 1010, "out", "阿明你好呀"),
    _r("m3", 2000, "in", "上周吃火锅吃到撑，太爽了"),
    _r("m4", 2010, "out", "哈哈注意肠胃"),
    _r("m5", 2500, "in", "嗯"),                                  # 短行不索引
    _r("m6", 2600, "in", "[图片] 一张自拍"),                      # 占位不索引
    _r("m7", 3000, "in", "还记得我说的那个涮肉店吗"),             # 当前问题
]
Q = "还记得我说的那个涮肉店吗"


# ── 配置 ─────────────────────────────────────────────────────────────────────

def test_cfg_defaults_and_overrides():
    d = resolve_semantic_recall_cfg({})
    assert d["enabled"] is True and d["min_cosine"] == 0.5 and d["top_k"] == 4
    c = resolve_semantic_recall_cfg({"inbox": {"semantic_recall": {
        "enabled": False, "min_cosine": 5, "top_k": 0, "index_cap": "x", "timeout_sec": 0.1}}})
    assert c["enabled"] is False and c["min_cosine"] == 0.99 and c["top_k"] == 1
    assert c["index_cap"] == 300 and c["timeout_sec"] == 1.0
    assert resolve_semantic_recall_cfg({"inbox": {"semantic_recall": False}})["enabled"] is False
    assert resolve_semantic_recall_cfg(None)["enabled"] is True


# ── 侧库 ─────────────────────────────────────────────────────────────────────

def test_index_roundtrip_idempotent_and_model_scoped():
    idx = SemanticIndex(None)
    n = idx.put_many("c1", [("m1", 1.0, "user", "h1", "bge", [1.0, 0.0]),
                            ("m2", 2.0, "assistant", "h2", "bge", [0.0, 1.0]),
                            ("m3", 3.0, "user", "h3", "bge", [])])          # 空向量跳过
    assert n == 2 and idx.count("c1") == 2
    idx.put_many("c1", [("m1", 1.0, "user", "h1b", "bge", [0.5, 0.5])])    # 覆盖
    assert idx.count("c1") == 2
    assert idx.have("c1")["m1"] == ("h1b", "bge")
    assert len(idx.vectors("c1", "bge")) == 2
    assert idx.vectors("c1", "other") == []
    assert idx.vectors("c1")[0][3] in ([0.5, 0.5], [0.0, 1.0])


def test_index_path_follows_inbox_db_dir(tmp_path):
    st = _Store([], db_path=tmp_path / "inbox.db")
    assert sr.semantic_db_path(st) == tmp_path / sr.DB_NAME
    idx = sr.get_index(st)
    assert idx.path == tmp_path / sr.DB_NAME and idx.path.exists()
    assert sr.get_index(st) is idx                       # 单例
    assert sr.get_index(_Store([])).path is None         # 无 _db_path → 内存


# ── 纯函数 ───────────────────────────────────────────────────────────────────

def test_indexable_rows_filters():
    rows = indexable_rows(ROWS, min_chars=4, current_text=Q)
    ids = [r["message_id"] for r in rows]
    assert ids == ["m1", "m2", "m3", "m4"]


def test_pick_rows_to_embed_incremental_and_capped():
    rows = indexable_rows(ROWS, current_text=Q)
    have = {"m3": (text_hash("上周吃火锅吃到撑，太爽了"), "bge-m3"),
            "m4": ("stale-hash", "bge-m3"),                   # 文本变了
            "m1": (text_hash("我叫阿明，在深圳做设计"), "old-model")}   # 模型换了
    todo = [r["message_id"] for r in pick_rows_to_embed(rows, have, "bge-m3", cap=10)]
    assert set(todo) == {"m1", "m2", "m4"}
    assert todo[0] == "m4"                                # 最近优先
    assert len(pick_rows_to_embed(rows, {}, "bge-m3", cap=2)) == 2


def test_rank_threshold_and_user_priority():
    cands = [("a", 1.0, "assistant", [1.0, 0.0, 0.0, 0.0]),
             ("u", 2.0, "user", [1.0, 0.0, 0.0, 0.0]),
             ("far", 3.0, "user", [0.0, 0.0, 0.0, 1.0])]
    out = rank([1.0, 0.0, 0.0, 0.0], cands, top_k=5, min_cosine=0.5)
    assert [m for m, _ in out] == ["u", "a"]
    assert rank([1.0, 0.0, 0.0, 0.0], cands, top_k=1, min_cosine=0.5) == [("u", 1.0)]


# ── 增量索引 ─────────────────────────────────────────────────────────────────

async def test_ensure_indexed_incremental_and_partial_failure():
    idx = SemanticIndex(None)
    ai = _AI()
    rows = indexable_rows(ROWS, current_text=Q)
    n1 = await ensure_indexed(idx, "c1", rows, ai.embed, "bge-m3", cap=300, batch=2)
    assert n1 == 4 and idx.count("c1") == 4 and len(ai.calls) == 2
    n2 = await ensure_indexed(idx, "c1", rows, ai.embed, "bge-m3", cap=300, batch=2)
    assert n2 == 0 and len(ai.calls) == 2                 # 稳态零嵌入
    bad = _AI(fail=True)
    idx2 = SemanticIndex(None)
    n3 = await ensure_indexed(idx2, "c1", rows, bad.embed, "bge-m3", cap=300, batch=2)
    assert n3 == 0 and idx2.count("c1") == 0 and len(bad.calls) == 1   # 首批失败即停


# ── 端到端 ───────────────────────────────────────────────────────────────────

async def test_recall_finds_synonym_row_and_merges_into_probe_block():
    from src.inbox.memory_probe import probe_detail_from_store
    st = _Store(ROWS)
    ai = _AI()
    found = await recall_rows_async(st, "c1", Q, ai_client=ai, config={})
    assert [r["message_id"] for r in found] == ["m3"]
    assert found[0]["_cosine"] >= 0.9
    block, kind, n = probe_detail_from_store(st, "c1", Q, semantic_rows=found)
    assert kind == "recall" and n >= 1 and "火锅" in block and "记不太清" not in block
    snap = sr.snapshot()
    assert snap["checked"] == 1 and snap["hit"] == 1 and snap["embedded_rows"] == 4
    # 第二次探针：索引已在，只嵌查询一次
    calls_before = len(ai.calls)
    found2 = await recall_rows_async(st, "c1", "还记得那家涮肉吗", ai_client=ai, config={})
    assert "m3" in [r["message_id"] for r in found2]
    # 至少一次查询嵌入；问句换了可能再补索引 1 批（原问题行不再被 current_text 剔除）
    assert calls_before + 1 <= len(ai.calls) <= calls_before + 2


async def test_recall_miss_when_nothing_similar():
    st = _Store(ROWS)
    ai = _AI()
    found = await recall_rows_async(st, "c1", "还记得我养的那只猫吗", ai_client=ai, config={})
    assert found == []
    assert sr.snapshot()["miss"] == 1


async def test_recall_degrades_silently():
    st = _Store(ROWS)
    # 嵌入未就绪
    assert await recall_rows_async(st, "c1", Q, ai_client=_AI(state="circuit_open"), config={}) == []
    assert sr.snapshot()["skip_unready"] == 1
    # 没有 ai_client
    assert await recall_rows_async(st, "c1", Q, ai_client=None, config={}) == []
    # embed 回空（熔断中）
    assert await recall_rows_async(st, "c1", Q, ai_client=_AI(fail=True), config={}) == []
    # 关掉 → 零调用
    ai = _AI()
    assert await recall_rows_async(st, "c1", Q, ai_client=ai,
                                   config={"inbox": {"semantic_recall": {"enabled": False}}}) == []
    assert ai.calls == []
    # 超时
    slow = _AI(delay=0.5)
    out = await recall_rows_async(st, "c1", Q, ai_client=slow,
                                  config={"inbox": {"semantic_recall": {"timeout_sec": 1}}},
                                  index=SemanticIndex(None))
    # timeout_sec 地板 1s，两次 0.5s 嵌入可能刚好卡线；只断言不抛且结果合法
    assert isinstance(out, list)
    st2 = _Store(ROWS)
    very_slow = _AI(delay=3.0)
    out2 = await asyncio.wait_for(
        recall_rows_async(st2, "c2", Q, ai_client=very_slow,
                          config={"inbox": {"semantic_recall": {"timeout_sec": 1}}},
                          index=SemanticIndex(None)),
        timeout=5)
    assert out2 == [] and sr.snapshot()["timeout"] >= 1


async def test_recall_excludes_current_question_and_stale_model_vectors():
    st = _Store(ROWS)
    idx = SemanticIndex(None)
    # 预埋旧模型向量：不能被当作候选
    idx.put_many("c1", [("m1", 1000, "user", text_hash("我叫阿明，在深圳做设计"), "old", [0.95, 0.05, 0, 0])])
    ai = _AI()
    found = await recall_rows_async(st, "c1", Q, ai_client=ai, config={}, index=idx)
    assert [r["message_id"] for r in found] == ["m3"]     # m1 旧向量不参赛、当前问题 m7 不在
    assert idx.have("c1")["m1"][1] == "bge-m3"            # 旧模型行被重嵌
