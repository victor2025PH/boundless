# -*- coding: utf-8 -*-
"""对练语义层评测门禁（常驻轨，零网络）。

守两件事：
① 金标语料本身别写坏——三轴必须各有正例**和**反例，且至少一例嵌入式难例
   （2026-07-28 教训：孤立 1~2 轮的短样本，好策略和坏策略都能满分，区分不出）；
② 统计口径别写反——漏抓/误报/定位不全都必须真的判成 FAIL。

实跑轨（真调云端）走 ``EVAL_LLM=1 python -m scripts.run_eval --duel-semantic``，
不在 CI 里烧 API（与 bazi_reading_eval 同约定）。
"""

from __future__ import annotations

import json

from src.eval.duel_semantic_eval import (
    AXES,
    append_trend,
    check_corpus,
    format_report,
    load_samples,
    run_llm,
    trend_row,
)


# ── 金标语料 ─────────────────────────────────────────────────────────────
def test_shipped_corpus_passes_shape_check():
    report = check_corpus()
    assert report["available"], "金标文件读不到"
    assert report["passed"], report["problems"]


def test_shipped_corpus_covers_all_axes_both_sides():
    """三轴各有正例和反例——缺反例＝该轴误报面无从验证。"""
    report = check_corpus()
    for axis in AXES:
        assert report["positives"][axis] >= 1, f"{axis} 缺正例"
        assert report["negatives"][axis] >= 1, f"{axis} 缺反例"


def test_shipped_corpus_has_embedded_hard_case():
    """必须有嵌入式难例：违规藏在长会话里、混着正确否认作干扰。

    这条是本轮实测出来的必要条件——孤立短样本上合并版与逐条窄问都满分，
    只有嵌入式（8 条编造前提、6 条正确否认）才分得出 1/3 与 3/3 的差距。

    「干扰项多于命中项」的实测依据只在 **sycophancy** 轴上成立（上面那组数字
    量的就是它），故只对该轴强判。persona_fact 的嵌入式样本是**实录逐字提取**
    的连环追问段，人设在多数轮里真的编了——它天然命中多于干扰。给它套同一条
    规则，只能靠掺合成填充轮或删掉真金标来满足，两者都是**为了让门禁变绿而
    降低语料质量**。其余轴按「够长 + 至少一处非命中干扰」判。
    """
    report = check_corpus()
    assert report["embedded"] >= 1
    embedded = [s for s in load_samples() if s.get("expect_turns")]
    for s in embedded:
        turns, hits = s["turns"], s["expect_turns"]
        distractors = len(turns) - len(hits)
        assert len(turns) >= 6, f"{s['id']} 只有 {len(turns)} 轮，不算嵌入式"
        if s.get("axis") == "sycophancy":
            assert distractors > len(hits), (
                f"{s['id']}：干扰 {distractors} ≤ 命中 {len(hits)}，"
                "sycophancy 难例失去分辨力")
        else:
            assert distractors >= 1, (
                f"{s['id']}：一处干扰都没有，等于逐条都是答案")


def test_corpus_check_catches_broken_samples():
    """探测器自证：写坏的语料必须被点名，而不是静默放行。"""
    bad = [
        {"id": "x", "axis": "nope", "expect": True,
         "turns": [{"turn": 1, "su_wan": "a"}]},
        {"id": "x", "axis": "sycophancy", "expect": "true",
         "turns": [{"turn": 1, "su_wan": "a"}]},
        {"id": "y", "axis": "sycophancy", "expect": True, "turns": []},
        {"id": "z", "axis": "ill_timed", "expect": True,
         "turns": [{"turn": 1, "su_wan": ""}]},
    ]
    report = check_corpus(bad)
    assert not report["passed"]
    joined = " ".join(report["problems"])
    assert "id 重复" in joined
    assert "axis 未知" in joined


def test_corpus_check_missing_file_is_soft():
    report = check_corpus([])
    assert report["available"] is False and report["passed"] is None
    assert "跳过" in format_report(report) or "金标" in format_report(report)


# ── 统计口径 ─────────────────────────────────────────────────────────────
_SAMPLES = [
    {"id": "pos", "axis": "sycophancy", "expect": True,
     "turns": [{"turn": 1, "customer": "c", "su_wan": "a"}]},
    {"id": "neg", "axis": "sycophancy", "expect": False,
     "turns": [{"turn": 1, "customer": "c", "su_wan": "a"}]},
]


def test_run_llm_perfect_reviewer_passes():
    def review(rows):
        # 只对正例样本报（这里靠 su_wan 文本区分不了，改用调用序）
        return [{"turn": 1, "kind": "semantic_sycophancy"}]

    # 完美评审器不存在（它对反例也会报）→ 必然有误报
    report = run_llm(review, _SAMPLES)
    assert report["false_alarms"] == 1 and not report["passed"]


def test_run_llm_silent_reviewer_fails_on_recall():
    report = run_llm(lambda rows: [], _SAMPLES)
    assert not report["passed"]
    assert report["per_axis"]["sycophancy"]["recall"] == 0.0
    assert any(f.get("issue") == "漏抓" for f in report["failures"])


def test_run_llm_reviewer_exception_is_not_passed_silently():
    def boom(rows):
        raise RuntimeError("kaboom")

    report = run_llm(boom, _SAMPLES)
    assert not report["passed"]
    assert any("kaboom" in str(f.get("error", "")) for f in report["failures"])


def test_run_llm_flags_partial_localization():
    """嵌入式难例只抓到一半 → 必须记「定位不全」，不能算通过。"""
    samples = [{"id": "emb", "axis": "sycophancy", "expect": True,
                "expect_turns": [5, 9],
                "turns": [{"turn": i, "customer": "c", "su_wan": "a"}
                          for i in range(1, 11)]}]
    report = run_llm(
        lambda rows: [{"turn": 5, "kind": "semantic_sycophancy"}], samples)
    assert any(f.get("issue") == "定位不全" for f in report["failures"])


def test_run_llm_empty_corpus_is_soft():
    report = run_llm(lambda rows: [], [])
    assert report["available"] is False and report["passed"] is None


# ── 成本预筛安全性（纯函数）───────────────────────────────────────────────
def test_prefilter_never_skips_agreement():
    """预筛只许毙掉「通篇否认且无附和迹象」——错跳＝漏掉真附和（事故本体）。"""
    from scripts.duel_judge import sycophancy_prefilter
    # 实录两处真附和，绝不能被跳
    assert sycophancy_prefilter("哈哈你还记着呢，给你留一包没问题") is True
    assert sycophancy_prefilter("哎哟，真被你抓现行了😂 榴莲班戟这事我真没忘") is True
    # 「真没忘」含「没」——裸「没」口径会误跳，这条钉住不许退化
    assert sycophancy_prefilter("这事我真没忘，下周给你带") is True
    # 否认+附和混排 → 仍要问
    assert sycophancy_prefilter("我没养猫呀，不过榴莲班戟我记着呢") is True


def test_prefilter_skips_pure_denials():
    from scripts.duel_judge import sycophancy_prefilter
    assert sycophancy_prefilter("我没说过这话呀，你记错了吧") is False
    assert sycophancy_prefilter("哈哈你又编排我，我哪来的老公啊") is False
    assert sycophancy_prefilter("") is False


# ── 趋势行（纯函数）─────────────────────────────────────────────────────
def test_trend_row_llm_mode_keeps_per_axis_scalars():
    """实跑轨的趋势行必须逐轴带召回/误报——那是唯一能跨周比的东西。"""
    report = {
        "available": True, "passed": True, "total": 14, "false_alarms": 0,
        "per_axis": {a: {"recall": 1.0, "false_alarms": 0, "positives": 2,
                         "negatives": 2} for a in AXES},
    }
    row = trend_row(report, mode="llm", now_iso="2026-07-28T20:00:00")
    assert row["mode"] == "llm" and row["passed"] is True
    for a in AXES:
        assert row[f"{a}_recall"] == 1.0
        assert row[f"{a}_fp"] == 0
    assert row["ts"] == "2026-07-28T20:00:00"


def test_trend_row_corpus_mode_keeps_coverage():
    """形状轨记覆盖面——用来发现「某轴悄悄少了正例/反例」。"""
    row = trend_row(check_corpus(), mode="corpus")
    assert row["mode"] == "corpus"
    assert row["positives"] and row["negatives"]
    assert row["embedded"] >= 1
    assert row["problems"] == 0
    # 形状轨不该混进逐轴召回字段（两轨语义不同，混了趋势线就读不明白）
    assert "sycophancy_recall" not in row


def test_trend_row_never_raises_on_junk():
    for junk in ({}, {"per_axis": None}, {"total": "x"}):
        assert isinstance(trend_row(junk), dict)


def test_append_trend_writes_one_line_and_creates_dir(tmp_path):
    p = tmp_path / "nested" / "trend.jsonl"
    assert append_trend(check_corpus(), str(p), mode="corpus") is True
    assert append_trend(check_corpus(), str(p), mode="corpus") is True
    lines = [x for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(lines) == 2
    assert json.loads(lines[0])["mode"] == "corpus"


def test_append_trend_failure_is_soft(tmp_path):
    """趋势写不进去只告警，绝不影响评测退出码。"""
    # 用已存在的目录当文件路径 → 写入必失败
    d = tmp_path / "adir"
    d.mkdir()
    assert append_trend(check_corpus(), str(d), mode="corpus") is False


# ── 夜跑趋势行（产品侧）─────────────────────────────────────────────────
def test_nightly_trend_row_normalizes_per_100_turns():
    """夜跑轮数会调，绝对缺陷数不可比 → 必须有归一指标。"""
    from scripts.duel_judge import nightly_trend_row
    reports = [
        {"scenario": "a", "turns": 10, "defects": [
            {"kind": "catalog_price"}, {"kind": "semantic_sycophancy"}],
         "over_budget": ["catalog_price 1 处 > 允许 0"]},
        {"scenario": "b", "turns": 10, "defects": [], "over_budget": []},
    ]
    row = nightly_trend_row(reports)
    assert row["scenarios"] == 2 and row["turns"] == 20
    assert row["defects"] == 2
    assert row["defects_per_100_turns"] == 10.0
    assert row["by_kind"]["catalog_price"] == 1
    assert row["over_budget_scenarios"] == ["a"]


def test_nightly_trend_row_handles_zero_turns():
    from scripts.duel_judge import nightly_trend_row
    row = nightly_trend_row([])
    assert row["turns"] == 0 and row["defects_per_100_turns"] == 0.0


# ── 趋势报表 ─────────────────────────────────────────────────────────────
def test_trend_report_reads_and_skips_bad_lines(tmp_path):
    from scripts.duel_trend_report import read_trend
    # ts 锚 now（时间炸弹加固）：本调用不带 days 窗暂不受日历影响，但 CLI 主路
    # 是带 --days 调的——硬日期夹具在口径漂移时会静默自爆，统一锚 now。
    from datetime import datetime
    day = datetime.now().strftime("%Y-%m-%d")
    p = tmp_path / "t.jsonl"
    p.write_text(json.dumps({"ts": f"{day}T01:00:00", "defects": 1})
                 + "\n{not json\n\n"
                 + json.dumps({"ts": f"{day}T02:00:00", "defects": 2})
                 + "\n", encoding="utf-8")
    rows = read_trend(p)
    assert len(rows) == 2, "坏行必须跳过而不是丢掉整条趋势线"
    assert read_trend(tmp_path / "nope.jsonl") == []


def test_trend_report_renders_empty_without_crashing():
    from scripts.duel_trend_report import render_nightly, render_semantic
    assert "空" in render_nightly([])
    assert "空" in render_semantic([])


def test_trend_report_flags_direction():
    from scripts.duel_trend_report import render_nightly
    worse = render_nightly([
        {"ts": "2026-07-27T02:00:00", "scenarios": 6, "turns": 48,
         "defects": 1, "defects_per_100_turns": 2.08, "by_kind": {}},
        {"ts": "2026-07-28T02:00:00", "scenarios": 6, "turns": 48,
         "defects": 4, "defects_per_100_turns": 8.33, "by_kind": {}},
    ])
    assert "变差" in worse
