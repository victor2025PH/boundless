# -*- coding: utf-8 -*-
"""song_factory 纯函数门禁：声库注册表解析 / 逐句命中 / 五闸合议 / 新鲜度。

工厂是唱歌供给唯一入口（实施58），闸的语义漂移=假货直达客户，故钉死：
- 逐句缺一句即拒（「没唱完」事故的机制化，2026-08-22 人耳三轮的根因）；
- ASR/评分器缺席＝弃权不拦（软基建挂了不锁死供给，但绝不冒充「过闸」语义）；
- 换锚/换词必须判旧（换嗓后旧货=错声，与 voice_prerender stock_is_stale 同族）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.song_factory import (  # noqa: E402
    line_hits, load_voices, qa_verdict, stock_is_fresh,
)


def _write_voices(tmp_path: Path, rows) -> Path:
    (tmp_path / "voices.json").write_text(
        json.dumps({"voices": rows}, ensure_ascii=False), encoding="utf-8")
    return tmp_path


def test_load_voices_valid_and_defaults(tmp_path):
    tdir = _write_voices(tmp_path, [{
        "key": "warm_f", "label": "暖女声", "anchor": "anchors/warm_f.wav",
        "prompt": "a cappella female", "personas": ["a", "b"]}])
    vs = load_voices(tdir)
    assert len(vs) == 1
    v = vs[0]
    assert v["key"] == "warm_f"
    assert v["canvas_s"] == 28.0        # 缺省画布
    assert v["strength"] == 0.5         # 缺省 remix 强度
    assert v["personas"] == ["a", "b"]


def test_load_voices_skips_bad_rows(tmp_path):
    tdir = _write_voices(tmp_path, [
        {"key": "", "anchor": "x.wav", "prompt": "p"},          # 缺 key
        {"key": "k1", "anchor": "", "prompt": "p"},             # 缺锚
        {"key": "k2", "anchor": "a.wav", "prompt": ""},         # 缺 prompt
        {"key": "k3", "anchor": "../escape.wav", "prompt": "p"},  # 穿越
        "not-a-dict",
        {"key": "ok", "anchor": "anchors/ok.wav", "prompt": "p",
         "canvas_s": 34, "strength": 0.45},
    ])
    vs = load_voices(tdir)
    assert [v["key"] for v in vs] == ["ok"]
    assert vs[0]["canvas_s"] == 34.0
    assert vs[0]["strength"] == 0.45


def test_load_voices_missing_file(tmp_path):
    assert load_voices(tmp_path) == []


LYRICS = "月亮爬上了窗台\n晚风把心事吹开\n我把想说的话\n慢慢唱给你猜"


def test_line_hits_full_transcript():
    hits = line_hits(LYRICS, "月亮爬上了窗台晚风把心事吹开我把想说的话慢慢唱给你猜")
    assert hits is not None and len(hits) == 4
    assert all(h >= 0.85 for h in hits)


def test_line_hits_missing_last_line():
    hits = line_hits(LYRICS, "月亮爬上了窗台晚风把心事吹开我把想说的话")
    assert hits[0] >= 0.85 and hits[3] < 0.5


def test_line_hits_none_transcript_abstains():
    assert line_hits(LYRICS, None) is None


def test_qa_verdict_pass():
    ok, why = qa_verdict(hits=[0.9, 0.8, 0.9, 0.7], sim=0.83, dur=19.0,
                         canvas_s=28.0)
    assert ok and why == "ok"


def test_qa_verdict_line_miss_rejects():
    ok, why = qa_verdict(hits=[0.9, 0.9, 0.9, 0.3], sim=0.9, dur=19.0,
                         canvas_s=28.0)
    assert not ok and why.startswith("line_miss")


def test_qa_verdict_sim_floor_rejects():
    ok, why = qa_verdict(hits=[0.9, 0.9, 0.9, 0.9], sim=0.6, dur=19.0,
                         canvas_s=28.0)
    assert not ok and why.startswith("sim_low")


def test_qa_verdict_duration_band():
    ok, why = qa_verdict(hits=None, sim=None, dur=5.0, canvas_s=28.0)
    assert not ok and why.startswith("too_short")
    ok, why = qa_verdict(hits=None, sim=None, dur=31.0, canvas_s=28.0)
    assert not ok and why.startswith("too_long")


def test_qa_verdict_abstain_gates_do_not_block():
    ok, why = qa_verdict(hits=None, sim=None, dur=19.0, canvas_s=28.0)
    assert ok and why == "ok"


def test_stock_is_fresh_all_paths(tmp_path):
    sidecar = tmp_path / "origin_moon.json"
    assert not stock_is_fresh(sidecar, "abc", "词")          # 无 sidecar
    sidecar.write_text(json.dumps(
        {"anchor_sha1": "abc", "lyrics": "词"}, ensure_ascii=False),
        encoding="utf-8")
    assert stock_is_fresh(sidecar, "abc", "词")              # 新鲜
    assert not stock_is_fresh(sidecar, "OTHER", "词")        # 换锚判旧
    assert not stock_is_fresh(sidecar, "abc", "新词")        # 换词判旧
    sidecar.write_text("{broken", encoding="utf-8")
    assert not stock_is_fresh(sidecar, "abc", "词")          # 坏文件按旧
