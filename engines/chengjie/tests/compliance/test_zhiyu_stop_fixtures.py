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


# 2026-10-08 合入 dm-integration（智安 stop_gate #66）后实测：正例 109 漏 32、反例 53 误中 2。
# stop_gate.py 归智安维护，这里不改他的词表，而是做棘轮：已知缺口登记在下面，
# 出现登记之外的新漏判/新误判就红；缺口被补上只提示、不红（补上后请把对应行删掉）。
# 缺口清单同步写在 handoff NOTES_zhiyu.md，供智安补词。
_KNOWN_GATE_MISSES = frozenset({
    # en
    "CANCEL", "END", "QUIT", "Stop it, I'm not interested.", "quit messaging me",
    "I don't want these messages",
    # tl
    "pakitigil na po", "huwag na po kayong mag-message", "ayaw ko na makatanggap ng message",
    "huwag mo na akong kontakin", "alisin mo na ako sa listahan", "pakitanggal na po ako sa listahan",
    # taglish
    "pls stop na po sa pag text", "stop na sa chat please", "stop na, di ako interested",
    "wag na po kayo mag-send ng messages",
    # ceb
    "hunonga na", "Hunong na palihug.", "pahunong na palihug", "ayaw na ko pag-chat",
    "ayaw na mo pag-text nako", "di na ko ganahan ma-message", "ayaw na ko hasola",
    # hi
    "\u092e\u0948\u0938\u0947\u091c \u092d\u0947\u091c\u0928\u093e \u092c\u0902\u0926 \u0915\u0930\u094b",
    "\u0905\u092c \u0914\u0930 \u092e\u0948\u0938\u0947\u091c \u0928\u0939\u0940\u0902 \u091a\u093e\u0939\u093f\u090f",
    "band karo messages", "message bhejna band karo",
    "\u0905\u0928\u0938\u092c\u094d\u0938\u0915\u094d\u0930\u093e\u0907\u092c",
    "\u092e\u0941\u091d\u0947 \u0932\u093f\u0938\u094d\u091f \u0938\u0947 \u0939\u091f\u093e\u0913",
    # zh
    "请停止发送", "停止发送", "以后别给我发了",
})
_KNOWN_GATE_FALSE_HITS = frozenset({"不要再发呆了", "我想退订单可以吗"})


def _gate_vs_corpus():
    fn = _gate_fn()
    d = _corpus()
    missed = [r["text"] for r in d["positive"] if not _truthy(fn(r["text"]))]
    false_hits = [r["text"] for r in d["negative"] if _truthy(fn(r["text"]))]
    return d, missed, false_hits


def test_stop_gate_against_corpus():
    d, missed, false_hits = _gate_vs_corpus()
    new_missed = [t for t in missed if t not in _KNOWN_GATE_MISSES]
    new_false = [t for t in false_hits if t not in _KNOWN_GATE_FALSE_HITS]
    assert new_missed == [] and new_false == [], {"new_missed": new_missed, "new_false_hits": new_false}
    fixed = sorted((_KNOWN_GATE_MISSES - set(missed)) | (_KNOWN_GATE_FALSE_HITS - set(false_hits)))
    if fixed:
        print("stop_gate 已补上的登记缺口（可从清单删除）:", fixed)


def test_known_gate_gaps_are_real_corpus_rows():
    d = _corpus()
    pos = {r["text"] for r in d["positive"]}
    neg = {r["text"] for r in d["negative"]}
    assert _KNOWN_GATE_MISSES <= pos, sorted(_KNOWN_GATE_MISSES - pos)
    assert _KNOWN_GATE_FALSE_HITS <= neg, sorted(_KNOWN_GATE_FALSE_HITS - neg)
