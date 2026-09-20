# -*- coding: utf-8 -*-
"""话题分类审阅 CLI 门禁（P2 2026-08-04）。

不变量：
- ``report_for_cache``：正常缓存出 total/passed/rows（含 veto/light 命中词，
  与 ``smalltalk_verdict`` 同一事实源）；坏文件/缺文件 → ``{"ok": False}``
  绝不抛；
- ``collect_reports``：显式 data-root 只扫那一处；缓存缺失的根静默跳过；
- ``render_text``：PASS/DROP 双态可见 + 空数据有人话提示。只读零写入。
"""
import json

from scripts.topic_gate_report import (
    collect_reports,
    render_text,
    report_for_cache,
)

NOW = 1785600000.0

_HORMUZ = {
    "title": "美伊官员称伊朗与阿曼接近达成霍尔木兹海峡航行协议 - 同花顺",
    "summary": "伊朗称与阿曼谈判接近完成 美军向外界征集军事行动方案",
    "link": "", "published_ts": NOW - 3600,
}
_WTA = {
    "title": "WTA1000多伦多站：张帅终结对普丁塞娃七连败",
    "summary": "网球｜加拿大国家银行公开赛：张帅晋级女单第二轮",
    "link": "", "published_ts": NOW - 7200,
}


def _write_cache(root, topics):
    cfg = root / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    p = cfg / "daily_topics_cache.json"
    p.write_text(json.dumps({"fetched_ts": NOW, "topics": topics},
                            ensure_ascii=False), encoding="utf-8")
    return p


def test_report_classifies_with_hits(tmp_path):
    p = _write_cache(tmp_path, [_HORMUZ, _WTA])
    rep = report_for_cache(p)
    assert rep["ok"] is True
    assert rep["total"] == 2 and rep["passed"] == 1
    hz = next(r for r in rep["rows"] if "霍尔木兹" in r["title"])
    wta = next(r for r in rep["rows"] if "WTA" in r["title"])
    assert hz["ok"] is False and hz["veto_hits"]
    assert wta["ok"] is True and "网球" in wta["light_hits"]


def test_report_bad_or_missing_cache(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json at all", encoding="utf-8")
    assert report_for_cache(bad)["ok"] is False
    assert report_for_cache(tmp_path / "nope.json")["ok"] is False


def test_collect_reports_explicit_root_and_skip_missing(tmp_path):
    # 有缓存的根收进来
    _write_cache(tmp_path, [_WTA])
    reps = collect_reports(str(tmp_path))
    assert len(reps) == 1
    assert reps[0]["data_root"] == str(tmp_path)
    assert reps[0]["passed"] == 1
    # 无缓存的根静默跳过（不是每台机都开话题包）
    empty = tmp_path / "empty_root"
    empty.mkdir()
    assert collect_reports(str(empty)) == []


def test_render_text_shows_pass_drop_and_empty_hint(tmp_path):
    p = _write_cache(tmp_path, [_HORMUZ, _WTA])
    rep = report_for_cache(p)
    rep["data_root"] = str(tmp_path)
    text = render_text([rep])
    assert "PASS" in text and "DROP" in text
    assert "veto:" in text and "light:" in text
    assert "没有任何数据根有话题缓存" in render_text([])
