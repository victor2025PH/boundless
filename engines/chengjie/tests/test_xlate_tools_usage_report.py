"""P4（2026-08-18）：翻译工具用量裁决 CLI 契约测试。

钉住：纪元日（改埋点分桶必须同步挪纪元）/ 三前缀分桶完整性 / 纪元前隔离 /
判词样本闸门（天数、总量）/ 达标后的四问判词边界。
"""
import time

from tools.xlate_tools_usage_report import (
    BUCKETS,
    EPOCH_DAY,
    quarantine_pre_epoch,
    summarize,
    verdicts,
)


def _now_after(days: int) -> float:
    return time.mktime(time.strptime(EPOCH_DAY, "%Y-%m-%d")) + days * 86400 + 3600


def test_epoch_and_buckets_pinned():
    assert EPOCH_DAY == "2026-08-19"
    assert set(BUCKETS) == {"ctx_entry", "compare", "lightbox", "doc_av"}
    assert "xlctx_video" in BUCKETS["ctx_entry"]
    assert "lbxl_patch" in BUCKETS["lightbox"]
    assert "docxl_srt_dl" in BUCKETS["doc_av"]


def test_quarantine_pre_epoch():
    rows = [{"day": "2026-08-18", "action": "xlctx_text", "n": 99},
            {"day": "2026-08-19", "action": "xlctx_text", "n": 3}]
    kept, dropped = quarantine_pre_epoch(rows)
    assert dropped == 1 and kept[0]["n"] == 3
    assert summarize(kept) == {"xlctx_text": 3}


def test_verdicts_sample_gate_days():
    vs = verdicts({"xlctx_text": 100}, min_days=14, now=_now_after(3))
    assert all("样本不足" in v["verdict"] for v in vs)


def test_verdicts_sample_gate_total():
    vs = verdicts({"xlctx_text": 2}, min_days=14, min_total=20, now=_now_after(20))
    assert all("样本不足" in v["verdict"] for v in vs)


def test_verdicts_after_gate():
    totals = {"xlctx_text": 40, "xlctx_img": 10, "xlctx_voice": 0, "xlctx_video": 0,
              "xlctx_cmp": 20, "xlctx_cmp_lang": 8, "xlctx_cmp_copy": 15,
              "lbxl_run": 20, "lbxl_patch": 6, "lbxl_patch_view": 9,
              "docxl_audio": 35, "docxl_video": 4, "docxl_audio_done": 30,
              "docxl_srt_dl": 12}
    vs = {v["q"]: v["verdict"] for v in verdicts(totals, min_days=14, now=_now_after(20))}
    assert "xlctx_voice" in vs["右键翻译组入口活性"]          # 零点击入口被点名
    assert "40%" in vs["对照矩阵多语言价值"]                   # 8/20 → ≥30% 档
    assert "自动预生成" in vs["灯箱贴回价值"]                  # 6/20=30% ≥25%
    assert "分片管线" in vs["音视频加工用量"]                  # audio 35 ≥30
    assert "默认开" in vs["音视频加工用量"]                    # srt_dl 12 ≥10
