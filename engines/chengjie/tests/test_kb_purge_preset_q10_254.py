# -*- coding: utf-8 -*-
"""Q-10 #254（22KVXF ⑤，2026-09-10）：帮助语料不进用户 KB + KB 页「一键清除预置条目」两步走。

钉住：
- 小智帮助语料只存独立库 ``assistant_help.db``（``help_kb.help_corpus_status()`` 只读口；
  ``seed_help_corpus_bg`` 日志点明落点），``kb_entries`` 里一条不写；
- ``preset_entries`` 只列 vendor / system / help（help = source LIKE 'help%' / 'seed:%' 残留），
  user / import / learner 永不入选；非法来源 fail-closed 返回空；
- ``purge_preset_entries`` 只删 ids ∩ 当前清单，空 ids 不删；dry-run 到点删之间被改成 user
  的行自动豁免；清光 system 行才打 ``KB_SYSTEM_SEEDS_PURGED_KEY``；
- 路由 ``POST /api/kb/entries/purge-preset`` 默认 dry_run 只列清单；dry_run=false 无 ids → 400；
  来源非法 → 400；自检 / 启动路径没有任何自动调用；
- KB 页有按钮 + 弹窗（先预览再删），厂商横幅「一键清空」也改走弹窗；i18n zh/en 齐。
"""
from __future__ import annotations

import inspect
import logging
from pathlib import Path

import pytest

from src.utils.kb_store import (
    KB_SYSTEM_SEEDS_PURGED_KEY,
    PRESET_PURGE_SOURCES,
    KnowledgeBaseStore,
    preset_entries,
    purge_preset_entries,
)

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def store(tmp_path):
    return KnowledgeBaseStore(tmp_path / "kb.db")


def _add(store, title, source, **kw):
    e = {"title": title, "category": "日常话题", "triggers": [title], "example_reply_zh": "r",
         "source": source}
    e.update(kw)
    return store.add_entry(e)


def _set_raw_source(store, entry_id, raw):
    # normalize_kb_source 会把未知来源折成 user，历史残留只能直写 raw 值模拟
    with store._conn() as c:
        c.execute("UPDATE kb_entries SET source=? WHERE id=?", (raw, entry_id))


def _count(store, sql, *params):
    with store._conn() as c:
        return c.execute(sql, params).fetchone()[0]


def _mixed_box(store):
    ids = {
        "user": _add(store, "我的知识", "user"),
        "import": _add(store, "导入的", "import"),
        "vendor": _add(store, "厂商说明", "vendor"),
        "system": _add(store, "系统兜底", "system"),
        "help1": _add(store, "怎么接微信", "user"),
        "help2": _add(store, "怎么开夜间模式", "user"),
    }
    _set_raw_source(store, ids["help1"], "help")
    _set_raw_source(store, ids["help2"], "seed:help_terms")
    return ids


# ── 帮助语料落点 ───────────────────────────────────────────────────────────────

def test_help_corpus_lives_only_in_assistant_help_db(tmp_path, monkeypatch):
    from src.assistant import help_kb as hk
    from src.assistant.seed_corpus import build_all_entries

    kb = hk.HelpKB(tmp_path / "assistant_help.db")
    n = kb.upsert_entries(build_all_entries())
    assert n >= 200                                      # ~295 条随代码生成
    monkeypatch.setattr(hk, "_KB", kb)
    st = hk.help_corpus_status()
    assert st["store"] == "assistant_help.db" and st["count"] == n and st["in_user_kb"] is False
    assert st["path"].endswith("assistant_help.db")
    # 用户 KB 同目录一条不写
    ukb = KnowledgeBaseStore(tmp_path / "knowledge_base.db")
    assert ukb.stats()["total_entries"] == 0
    assert preset_entries(ukb, ["help"]) == []


def test_help_corpus_status_readonly_and_never_raises(tmp_path, monkeypatch):
    from src.assistant import help_kb as hk

    class _Boom:
        _path = "x"

        def count(self):
            raise RuntimeError("db gone")

    monkeypatch.setattr(hk, "_KB", _Boom())
    st = hk.help_corpus_status()
    assert st["count"] == -1 and st["in_user_kb"] is False
    assert hk._KB.__class__ is _Boom                     # 不重建单例、不改状态


def test_seed_log_names_destination(caplog, monkeypatch, tmp_path):
    from src.assistant import help_kb as hk
    from src.assistant import seed_corpus as sc

    kb = hk.HelpKB(tmp_path / "assistant_help.db")
    monkeypatch.setattr(hk, "get_help_kb", lambda: kb)
    monkeypatch.setattr(sc, "build_all_entries", lambda: [
        {"id": "t1", "title": "测试", "content": "c", "keywords": "k", "source": "test", "path": "/"}])
    import threading

    class _Sync:
        def __init__(self, target=None, **kw):
            self._t = target

        def start(self):
            self._t()

    monkeypatch.setattr(threading, "Thread", _Sync)
    with caplog.at_level(logging.INFO, logger="ai_chat_assistant.assistant.seed"):
        sc.seed_help_corpus_bg()
    msgs = [r.getMessage() for r in caplog.records]
    assert any("小智独立库" in m and "不入用户 KB" in m and "assistant_help.db" in m for m in msgs), msgs


# ── 清单 / 删除 ──────────────────────────────────────────────────────────────

def test_preset_entries_lists_only_vendor_system_help(store):
    ids = _mixed_box(store)
    items = preset_entries(store)
    got = {i["id"]: i for i in items}
    assert set(got) == {ids["vendor"], ids["system"], ids["help1"], ids["help2"]}
    assert got[ids["help1"]]["source"] == "help" and got[ids["help1"]]["source_raw"] == "help"
    assert got[ids["help2"]]["source"] == "help" and got[ids["help2"]]["source_raw"] == "seed:help_terms"
    assert got[ids["vendor"]]["source"] == "vendor"
    for k in ("title", "category", "enabled", "use_count", "template_key"):
        assert k in got[ids["system"]]
    # 按来源筛
    assert {i["id"] for i in preset_entries(store, ["vendor"])} == {ids["vendor"]}
    assert {i["id"] for i in preset_entries(store, ["help"])} == {ids["help1"], ids["help2"]}
    assert {i["id"] for i in preset_entries(store, ["system", "vendor"])} == {ids["system"], ids["vendor"]}
    # fail-closed：user / import / 未知来源永不入选
    assert preset_entries(store, ["user"]) == []
    assert preset_entries(store, ["import", "learner", "whatever"]) == []
    assert preset_entries(store, []) == preset_entries(store, None)   # 空列表 = 全部三档
    assert PRESET_PURGE_SOURCES == ("vendor", "system", "help")


def test_dry_run_deletes_nothing_and_purge_intersects_reviewed_ids(store):
    ids = _mixed_box(store)
    before = store.stats()["total_entries"]
    items = preset_entries(store)
    assert store.stats()["total_entries"] == before      # dry-run 无副作用
    # 空 ids → 什么都不删
    assert purge_preset_entries(store, [], None) == {"count": 0, "by_source": {}, "ids": []}
    assert store.stats()["total_entries"] == before
    # 清单外的 id（用户条目）混进来 → 被过滤
    res = purge_preset_entries(store, [ids["user"], ids["import"], "nope"], None)
    assert res["count"] == 0
    assert store.stats()["total_entries"] == before
    # dry-run 到点删之间有人把 vendor 那条改成了自己的 → 自动豁免
    _set_raw_source(store, ids["vendor"], "user")
    reviewed = [i["id"] for i in items]
    res = purge_preset_entries(store, reviewed, None)
    assert res["count"] == 3 and res["by_source"] == {"system": 1, "help": 2}
    assert set(res["ids"]) == {ids["system"], ids["help1"], ids["help2"]}
    assert store.stats()["total_entries"] == before - 3
    assert _count(store, "SELECT COUNT(*) FROM kb_entries WHERE id IN (?,?,?)",
                  ids["user"], ids["import"], ids["vendor"]) == 3
    # 清光 system 行 → 打标防灌回
    assert store.get_meta(KB_SYSTEM_SEEDS_PURGED_KEY)


def test_sources_filter_bounds_deletion_and_system_flag_only_when_cleared(store):
    ids = _mixed_box(store)
    sys2 = _add(store, "系统兜底 2", "system")
    all_ids = [i["id"] for i in preset_entries(store)]
    # 只勾了 help → 即便 ids 里带 system / vendor 也只删 help
    res = purge_preset_entries(store, all_ids, ["help"])
    assert res["by_source"] == {"help": 2} and res["count"] == 2
    assert _count(store, "SELECT COUNT(*) FROM kb_entries WHERE id IN (?,?)", ids["system"], sys2) == 2
    assert not store.get_meta(KB_SYSTEM_SEEDS_PURGED_KEY)
    # 删一条 system，还剩一条 → 不打标
    res = purge_preset_entries(store, [ids["system"]], ["system"])
    assert res["count"] == 1 and not store.get_meta(KB_SYSTEM_SEEDS_PURGED_KEY)
    res = purge_preset_entries(store, [sys2], ["system"])
    assert res["count"] == 1 and store.get_meta(KB_SYSTEM_SEEDS_PURGED_KEY)
    # 用户 / 导入 / 没勾的 vendor 都还在
    assert _count(store, "SELECT COUNT(*) FROM kb_entries WHERE id IN (?,?,?)",
                  ids["user"], ids["import"], ids["vendor"]) == 3
    assert _count(store, "SELECT COUNT(*) FROM kb_entries") == 3


# ── 路由 ─────────────────────────────────────────────────────────────────────

def test_route_dry_run_default_then_delete_by_ids(auth_client, app):
    kb = app.state.kb_store
    ids = _mixed_box(kb)
    before = kb.stats()["total_entries"]
    r = auth_client.post("/api/kb/entries/purge-preset", json={})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] and d["dry_run"] is True
    assert sorted(d["sources"]) == ["help", "system", "vendor"]
    listed = {i["id"] for i in d["items"]}
    assert {ids["vendor"], ids["system"], ids["help1"], ids["help2"]} <= listed
    assert ids["user"] not in listed and ids["import"] not in listed
    assert "help_corpus" in d and d["help_corpus"].get("in_user_kb") is False
    assert kb.stats()["total_entries"] == before          # dry-run 没删
    # dry_run=false 不给 ids → 400
    r = auth_client.post("/api/kb/entries/purge-preset", json={"dry_run": False})
    assert r.status_code == 400
    # 非法来源 → 400
    r = auth_client.post("/api/kb/entries/purge-preset", json={"sources": ["user"]})
    assert r.status_code == 400
    r = auth_client.post("/api/kb/entries/purge-preset", json={"sources": "vendor"})
    assert r.status_code == 400
    # 只删 help 两条
    r = auth_client.post("/api/kb/entries/purge-preset",
                         json={"dry_run": False, "sources": ["help"],
                               "ids": [ids["help1"], ids["help2"], ids["vendor"], ids["user"]]})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["ok"] and d["dry_run"] is False and d["count"] == 2 and d["by_source"] == {"help": 2}
    assert kb.stats()["total_entries"] == before - 2
    assert _count(kb, "SELECT COUNT(*) FROM kb_entries WHERE id IN (?,?)", ids["vendor"], ids["user"]) == 2


def test_route_never_autoruns_and_ui_wired():
    from src.web.routes import kb_routes
    src = inspect.getsource(kb_routes)
    assert '"/api/kb/entries/purge-preset"' in src
    assert 'data.get("dry_run", True) is not False' in src
    # purge_preset_entries 只在显式 ids 路径调用一次；自检 / 启动段没有任何自动调用
    assert src.count("purge_preset_entries(") == 1      # 唯一调用点
    assert "purge_preset_entries(_kb_store, [str(i) for i in ids], sources)" in src
    for mod in ("src/bootstrap/lifecycle.py", "src/web/admin.py", "src/utils/kb_registry.py", "main.py"):
        p = REPO / mod
        if p.exists():
            assert "purge_preset_entries" not in p.read_text(encoding="utf-8"), mod
    html = (REPO / "src" / "web" / "templates" / "knowledge.html").read_text(encoding="utf-8")
    for needle in ('id="btn-purge-preset"', 'id="preset-modal"', "openPresetPurgeModal(['vendor'])",
                   "{dry_run:true,sources:srcs}", "{dry_run:false,sources:srcs,ids}",
                   "confirm(window.Tf('kb2_preset_confirm'", "help_corpus"):
        assert needle in html, needle
    # 厂商横幅「一键清空」不再直打无 dry-run 的 purge-source
    assert "api('POST','/api/kb/entries/purge-source'" not in html
    from src.web.i18n_packs.kb_page import EN, ZH
    for key in ("kb2_preset_btn", "kb2_preset_title", "kb2_preset_lead", "kb2_src_help", "kb2_preset_preview",
                "kb2_preset_delete", "kb2_preset_delete_n", "kb2_preset_pick_source", "kb2_preset_none",
                "kb2_preset_summary", "kb2_preset_flag_enabled", "kb2_preset_flag_used",
                "kb2_preset_help_line", "kb2_preset_confirm", "kb2_preset_purged"):
        assert key in ZH and key in EN, key
    from src.web.i18n_packs.errors import EN as EEN, ZH as EZH
    assert "err.kb.purge_preset_sources_invalid" in EZH and "err.kb.purge_preset_sources_invalid" in EEN
