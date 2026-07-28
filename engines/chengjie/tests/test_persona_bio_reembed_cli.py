# -*- coding: utf-8 -*-
"""``scripts/persona_bio_reembed`` 门禁（I5 线收尾）。

CLI 的价值在「老库句向量静默缺失」这件事上——它是唯一手柄，所以选人/汇总/退出码都得钉住。
真嵌入不进门禁（跑 CLI 主流程时用假 store），纯函数直接测。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.persona_bio_reembed import main, select_personas, summarize


def test_select_personas_defaults_to_all_and_keeps_order():
    assert select_personas(["b", "a"], None) == ["b", "a"]
    assert select_personas(["b", "a"], []) == ["b", "a"]


def test_select_personas_filters_and_drops_unknown():
    assert select_personas(["a", "b", "c"], ["c", "a"]) == ["a", "c"]
    assert select_personas(["a"], ["nope"]) == []       # 未知 id 不炸


def test_summarize_counts_and_names_failures():
    out = summarize([
        {"persona_id": "a", "status": "ok", "updated": 2, "sents_added": 9},
        {"persona_id": "b", "status": "ok", "updated": 0, "sents_added": 4},
        {"persona_id": "c", "status": "failed"},
    ])
    assert out["total"] == 3 and out["ok"] == 2 and out["failed"] == 1
    assert out["failed_personas"] == ["c"]
    assert out["chunks_embedded"] == 2
    assert out["sentences_embedded"] == 13


def _install_fake_store(monkeypatch, *, personas, result):
    import src.companion.persona_bio_store as pbs
    monkeypatch.setattr(pbs, "list_bio_personas", lambda: list(personas))
    monkeypatch.setattr(pbs, "get_bio_meta", lambda p: {"chars": 100, "chunks": 2})
    monkeypatch.setattr(pbs, "reembed_bio_doc", lambda p: result)


def test_cli_exit_zero_when_no_bio_docs(monkeypatch, capsys):
    _install_fake_store(monkeypatch, personas=[], result=None)
    assert main(["--json"]) == 0
    assert "no_bio_docs" in capsys.readouterr().out


def test_cli_dry_run_does_not_embed(monkeypatch, capsys):
    calls = []
    import src.companion.persona_bio_store as pbs
    monkeypatch.setattr(pbs, "list_bio_personas", lambda: ["mizuki"])
    monkeypatch.setattr(pbs, "get_bio_meta", lambda p: {"chars": 33000, "chunks": 140})
    monkeypatch.setattr(pbs, "reembed_bio_doc", lambda p: calls.append(p))
    assert main(["--dry-run"]) == 0
    assert calls == []                                  # 一次嵌入都没打
    assert "mizuki" in capsys.readouterr().out


def test_cli_reports_sentence_backfill(monkeypatch, capsys):
    _install_fake_store(monkeypatch, personas=["mizuki"],
                        result={"chunks": 140, "updated": 0, "sents_added": 412})
    assert main([]) == 0
    assert "sents+412" in capsys.readouterr().out       # 块齐但句缺＝老库典型态


def test_cli_exit_one_when_embedding_unavailable(monkeypatch, capsys):
    """嵌入端点挂了 → reembed 返 None → 如实记 failed，别装成功。"""
    _install_fake_store(monkeypatch, personas=["mizuki"], result=None)
    assert main([]) == 1
    assert "ERR mizuki" in capsys.readouterr().out
