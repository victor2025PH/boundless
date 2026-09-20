# -*- coding: utf-8 -*-
"""spoken_style 灰度复盘 CLI 纯函数门禁（tools/spoken_style_obs.py）。

守的窄不变量：
  · 语言三分类（zh*/foreign/unknown）与 bridge 的 zh_only 语义对齐；
  · 污染判定＝语气词命中且只看外语会话，样本按 cap 截断、按 cutoff 分桶；
  · 出站镜像占位（[图片]…）绝不进任何读数（进了=把发图量算成话术长度）；
  · 中文形态统计的分位数/语气词占比口径稳定（复盘周环比要可比）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.spoken_style_obs import (  # noqa: E402
    _percentile,
    build_report,
    classify_lang,
)

CUTOFF = 1000.0


def _row(text, ts, language, **kw):
    base = {"text": text, "ts": ts, "language": language,
            "platform": "telegram", "display_name": "tester",
            "conversation_id": "c1", "chat_type": "private"}
    base.update(kw)
    return base


def test_classify_lang_three_way():
    assert classify_lang("zh") == "zh"
    assert classify_lang("zh-CN") == "zh"
    assert classify_lang("en") == "foreign"
    assert classify_lang("ja") == "foreign"
    assert classify_lang("unknown") == "unknown"
    assert classify_lang("") == "unknown"
    assert classify_lang(None) == "unknown"  # type: ignore[arg-type]


def test_percentile_nearest_rank():
    assert _percentile([], 0.5) == 0
    assert _percentile([7], 0.9) == 7
    assert _percentile([1, 2, 3, 4, 5], 0.5) == 3
    assert _percentile([1, 2, 3, 4, 5], 0.0) == 1
    assert _percentile([1, 2, 3, 4, 5], 1.0) == 5


def test_pollution_modal_hit_split_by_cutoff_and_sampled():
    rows = [
        _row("ok sure 啦", CUTOFF - 10, "en"),          # 分界前命中
        _row("got it 嘛", CUTOFF + 10, "en"),           # 分界后命中
        _row("hello there", CUTOFF + 20, "en"),         # 干净外语
    ]
    rep = build_report(rows, cutoff_ts=CUTOFF)
    p = rep["pollution"]
    assert p["n_out_foreign"] == 3
    assert p["modal_hits_before"] == 1
    assert p["modal_hits_after"] == 1
    whens = {s["when"] for s in rep["pollution_samples"]}
    assert whens == {"before", "after"}


def test_pollution_sample_cap_respected():
    rows = [_row(f"see you 啦 {i}", CUTOFF + i, "en") for i in range(6)]
    rep = build_report(rows, cutoff_ts=CUTOFF, samples_cap=2)
    assert rep["pollution"]["modal_hits_after"] == 6
    assert len(rep["pollution_samples"]) == 2


def test_pollution_han_wide_counted_but_not_sampled():
    # han 占比高但无语气词 → 宽口径计数，不进样本（品牌名类误报只看量级）
    rows = [_row("智聊自动回复演示 demo", CUTOFF + 5, "en")]
    rep = build_report(rows, cutoff_ts=CUTOFF)
    assert rep["pollution"]["han_wide_after"] == 1
    assert rep["pollution"]["modal_hits_after"] == 0
    assert rep["pollution_samples"] == []


def test_placeholder_and_empty_excluded_everywhere():
    rows = [
        _row("[图片] 给你看看", CUTOFF + 1, "zh"),
        _row("[语音]×2", CUTOFF + 2, "en"),
        _row("   ", CUTOFF + 3, "zh"),
    ]
    rep = build_report(rows, cutoff_ts=CUTOFF)
    assert rep["pollution"]["n_out_foreign"] == 0
    assert rep["tone_zh"]["after"]["n"] == 0


def test_zh_tone_stats_split_and_particle_rate():
    rows = [
        _row("好的收到，明天给您答复", CUTOFF - 5, "zh"),      # 基线，无语气词
        _row("好嘛", CUTOFF + 1, "zh"),                        # 灰度，命中
        _row("这个我确认一下哈，晚点回您", CUTOFF + 2, "zh"),  # 灰度，「哈」不在词表→不命中
        _row("好哦", CUTOFF + 3, "zh"),                        # 句尾哦命中
    ]
    rep = build_report(rows, cutoff_ts=CUTOFF)
    t = rep["tone_zh"]
    assert t["before"]["n"] == 1 and t["before"]["particle_rate"] == 0.0
    assert t["after"]["n"] == 3
    assert abs(t["after"]["particle_rate"] - 2 / 3) < 5e-4  # 快照 round(4)
    assert t["after"]["p50_len"] >= 2


def test_modal_tail_only_for_o_particle():
    # 「哦」句中不算（避免音译词误伤），句尾/标点前算
    rows = [
        _row("哦我知道 ok", CUTOFF + 1, "en"),
        _row("fine 好哦!", CUTOFF + 2, "en"),
    ]
    rep = build_report(rows, cutoff_ts=CUTOFF)
    assert rep["pollution"]["modal_hits_after"] == 1


def test_unknown_language_skipped_and_counted():
    rows = [_row("whatever 啦", CUTOFF + 1, "unknown")]
    rep = build_report(rows, cutoff_ts=CUTOFF)
    assert rep["pollution"]["n_out_foreign"] == 0
    assert rep["tone_zh"]["after"]["n"] == 0
    assert rep["unknown_out_skipped"] == 1
