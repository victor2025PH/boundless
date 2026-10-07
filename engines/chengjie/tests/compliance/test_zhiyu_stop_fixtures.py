"""P1-4（2026-10-08 智语）：多语 STOP/退订语料与确认模板的结构守卫。

语料供智安 ``src/compliance/stop_gate.py`` 使用；gate 落地后本文件最后一个用例会自动
拿整份语料跑它（正例必须判 STOP、反例必须不判），没落地前跳过。
"""

import json
import re
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[2]
_CORPUS = Path(__file__).resolve().parent / "fixtures" / "stop_optout_corpus.json"
_TEMPLATES = _ROOT / "config" / "presets" / "snippets" / "stop_confirm_templates.yaml"
_LANGS = {"en", "tl", "taglish", "ceb", "hi", "zh"}


def _corpus():
    return json.loads(_CORPUS.read_text(encoding="utf-8"))


def test_corpus_schema_and_counts():
    d = _corpus()
    assert d["schema"] == "zhiliao.stop_corpus.v1"
    for k in ("positive", "negative", "ambiguous"):
        assert d["counts"][k] == len(d[k])
    assert len(d["positive"]) >= 60 and len(d["negative"]) >= 40
    for row in d["positive"]:
        assert row["text"].strip() and row["lang"] in _LANGS
        assert row["kind"] in {"keyword", "stop_request", "unsubscribe"}
    for row in d["negative"]:
        assert row["text"].strip() and row["lang"] in _LANGS and row["why"]
    for row in d["ambiguous"]:
        assert row["expect"] in {"not_stop", "not_stop+rg_flag", "stop_if_alone"}


def test_every_language_covered_both_ways():
    d = _corpus()
    pos_langs = {r["lang"] for r in d["positive"]}
    neg_langs = {r["lang"] for r in d["negative"]}
    assert _LANGS <= pos_langs
    assert _LANGS <= neg_langs


def test_no_overlap_and_no_duplicates():
    d = _corpus()
    norm = lambda s: re.sub(r"\s+", " ", s.strip().lower())  # noqa: E731
    pos = [norm(r["text"]) for r in d["positive"]]
    neg = [norm(r["text"]) for r in d["negative"]]
    amb = [norm(r["text"]) for r in d["ambiguous"]]
    raw_pos = [r["text"] for r in d["positive"]]   # 大小写/空白变体是刻意的，按原文去重
    raw_neg = [r["text"] for r in d["negative"]]
    assert len(set(raw_pos)) == len(raw_pos)
    assert len(set(raw_neg)) == len(raw_neg)
    assert not (set(pos) & set(neg)) and not (set(pos) & set(amb)) and not (set(neg) & set(amb))


@pytest.mark.parametrize("must", [
    "STOP", "Stop po", "tigil na", "ayoko na", "huwag mo na akong i-chat",
    "wag mo na ako i-message", "unsubscribe me", "stop messaging me", "别再发了",
    "अब मैसेज मत करो",
])
def test_required_positive_present(must):
    assert must in {r["text"] for r in _corpus()["positive"]}


@pytest.mark.parametrize("must", [
    "don't stop", "stop by later", "bus stop", "hindi ako titigil sa paglalaro",
])
def test_required_negative_present(must):
    assert must in {r["text"] for r in _corpus()["negative"]}


# ── 确认模板（FD-16 式：只确认，不挑逗、不挽留）────────────────────────────────

_RETENTION = re.compile(
    r"change\s+your\s+mind|anytime|any\s+time|miss\s+you|come\s+back|start|"
    r"balik|kapag\s+gusto\s+mo|sayang|miss\s+na|想你|随时|隨時|回来|回來|再聊|"
    r"फिर\s+से\s+बात|http|www\.|\?|？", re.I)
_EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")


def _templates():
    return yaml.safe_load(_TEMPLATES.read_text(encoding="utf-8"))


def test_templates_cover_corpus_languages():
    t = _templates()
    assert t["schema"] == "zhiliao.stop_confirm.v1"
    assert t["default_lang"] in t["templates"]
    assert _LANGS | {"zh_hant"} <= set(t["templates"])
    for lang, v in t["templates"].items():
        assert set(v) == {"persona", "brand"}, lang


def test_templates_confirm_only():
    for lang, v in _templates()["templates"].items():
        for kind, text in v.items():
            assert text.strip(), (lang, kind)
            assert not _RETENTION.search(text), (lang, kind, text)
            assert not _EMOJI.search(text), (lang, kind, text)
            assert len(text) <= 90, (lang, kind, text)


# ── gate 落地后自动接上 ─────────────────────────────────────────────────────────

def _gate_fn():
    mod = pytest.importorskip("src.compliance.stop_gate")
    for name in ("is_stop_message", "is_stop", "detect_stop"):
        fn = getattr(mod, name, None)
        if callable(fn):
            return fn
    pytest.skip("stop_gate 尚无 is_stop_message/is_stop/detect_stop 入口")


def _truthy(res):
    if isinstance(res, dict):
        return bool(res.get("stop") or res.get("hit") or res.get("matched"))
    if isinstance(res, tuple):
        return bool(res[0])
    return bool(res)


def test_stop_gate_against_corpus():
    fn = _gate_fn()
    d = _corpus()
    missed = [r["text"] for r in d["positive"] if not _truthy(fn(r["text"]))]
    false_hits = [r["text"] for r in d["negative"] if _truthy(fn(r["text"]))]
    assert missed == [] and false_hits == [], {"missed": missed, "false_hits": false_hits}
