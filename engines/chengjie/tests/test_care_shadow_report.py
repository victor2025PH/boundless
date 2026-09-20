"""P4：care 影子对照周审 CLI 门禁（JSONL 聚合 / 书写系统分类 / 判词渲染）。"""
from __future__ import annotations

import json
import time

from scripts.care_shadow_report import (
    classify_script, load_records, render, summarize,
)

NOW = time.time()


# ── 书写系统近似分类 ─────────────────────────────────────────────────────
def test_classify_script():
    assert classify_script("明天去面试") == "cjk"
    assert classify_script("tomorrow interview") == "latin"
    assert classify_script("พรุ่งนี้ไปสัมภาษณ์") == "thai"
    assert classify_script("明日、面接があります") == "kana"   # 日文夹汉字按假名归
    assert classify_script("내일 병원 가요") == "hangul"
    assert classify_script("ngày mai đi khám") == "latin"      # 越南语=拉丁系
    assert classify_script("123 !!") == "other"
    assert classify_script("") == "other"


def _rec(ts, text, *, llm_found=None, regex=False, captured=False, error=False):
    llm = {"error": True} if error else (
        {"found": True, "topic": "面试", "date": "2099-01-05", "confidence": 0.9}
        if llm_found else {"found": False})
    return {
        "ts": ts, "cid": "c1", "platform": "telegram", "text": text,
        "regex": ([{"topic": "面试", "due_at": ts + 3600, "confidence": 0.85}]
                  if regex else []),
        "llm": llm, "agree": bool(llm_found) == bool(regex), "captured": captured,
    }


def _write(dirp, name, recs):
    dirp.mkdir(parents=True, exist_ok=True)
    (dirp / name).write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
        encoding="utf-8")


# ── 读取：按天窗过滤 + 坏行跳过 ─────────────────────────────────────────
def test_load_records_window_and_bad_lines(tmp_path):
    d = tmp_path / "care_shadow"
    _write(d, "shadow-20990101.jsonl", [
        _rec(NOW - 3600, "明天面试", llm_found=True, regex=True),
        _rec(NOW - 10 * 86400, "旧记录", llm_found=True),
    ])
    (d / "shadow-20990101.jsonl").open("a", encoding="utf-8").write("not json\n")
    recs = load_records(d, days=7, now=NOW)
    assert len(recs) == 1
    assert load_records(tmp_path / "nope", days=7) == []


# ── 聚合 ─────────────────────────────────────────────────────────────────
def test_summarize_buckets_and_samples(tmp_path):
    recs = [
        _rec(NOW, "明天面试", llm_found=True, regex=True),              # both
        _rec(NOW, "พรุ่งนี้สัมภาษณ์", llm_found=True, captured=True),   # llm_only + captured
        _rec(NOW, "ngày mai đi khám", llm_found=True),                  # llm_only
        _rec(NOW, "周五出差", regex=True),                              # regex_only
        _rec(NOW, "weekend vibes"),                                     # none
        _rec(NOW, "明天见", error=True),                                # error → none 桶
    ]
    s = summarize(recs, samples=5)
    assert s["total"] == 6
    assert s["both"] == 1 and s["llm_only"] == 2 and s["regex_only"] == 1
    assert s["none"] == 2
    assert s["llm_err"] == 1
    assert s["agree"] == 3 and abs(s["agree_rate"] - 0.5) < 1e-9
    assert s["captured"] == 1
    assert len(s["llm_only_samples"]) == 2
    assert {x["script"] for x in s["llm_only_samples"]} == {"thai", "latin"}
    assert s["by_script"]["thai"]["llm_only"] == 1
    assert s["regex_only_samples"][0]["topic"] == "面试"


def test_summarize_empty():
    s = summarize([])
    assert s["total"] == 0 and s["agree_rate"] is None


# ── 渲染 ─────────────────────────────────────────────────────────────────
def test_render_empty_and_nonempty(tmp_path):
    empty = render(summarize([]), root="R", days=7)
    assert "无对照记录" in empty
    s = summarize([_rec(NOW, "พรุ่งนี้", llm_found=True, captured=True)], samples=3)
    out = render(s, root="R", days=7)
    assert "llm_only=1" in out and "captured=1" in out
    assert "切主" in out          # 判词指引在场
