# -*- coding: utf-8 -*-
"""分条语音按文字系统折算预算（2026-09-12 实锤）。

英文回复按 CJK 口径 36 字/条切成 "Yeah, it's super calm here at the" / "dock this morning.
Just me and the" / "coffee, watching the seagulls." 三条各两秒碎句；合并余量时拉丁词
之间无空格粘成 "dockat"。修法：拉丁主体 ×3 折算 part_max/min_tail/min_total，拼接补空格。
中文行为一字不变（既有 test_voice_clone_chunking / test_voice_quality_gate 钉住）。
"""
from __future__ import annotations

from src.ai import voice_clone_client as vcc

_EN = ("Yeah, it's super calm here at the dock this morning. "
       "Just me and the coffee, watching the seagulls.")
_EN2 = ("Um... anyway, it's just that, on my end it's a little past five in the morning, "
        "I'm standing on the dock at Granville Island with a coffee, and the seagulls "
        "are being ridiculous again. What about you, did you eat yet?")


def test_latin_budget_scale():
    assert vcc.latin_budget_scale(_EN) == vcc.LATIN_BUDGET_SCALE == 3.0
    assert vcc.latin_budget_scale("我这边是早上五点半多呢。刚下夜班。") == 1.0
    assert vcc.latin_budget_scale("这边大家都爱说 nice 和 sorry 呢") == 1.0     # 中英混排按中文
    assert vcc.latin_budget_scale("") == 1.0 and vcc.latin_budget_scale("123 ...") == 1.0
    assert vcc.effective_min_total(_EN, 40) == 120
    assert vcc.effective_min_total("中文", 40) == 40


def test_incident_english_reply_is_no_longer_chopped_into_fragments():
    # 事故参数：part_max_chars=36 / max_parts=3 / min_tail_chars=8（B 线默认）
    parts = vcc.pack_voice_parts(_EN, part_max_chars=36, max_parts=3, min_tail_chars=8)
    assert parts == [_EN]                       # 96 字符 < 108 → 一条到底，不切
    parts2 = vcc.pack_voice_parts(_EN2, part_max_chars=36, max_parts=3, min_tail_chars=8)
    assert 2 <= len(parts2) <= 3
    for p in parts2[:-1]:
        assert len(p) <= 36 * 3                 # 末条按既有语义可并入余量而超预算
    for p in parts2:
        assert len(p.split()) >= 6, p           # 没有两秒碎句
    # 每条都在句/子句边界结束，不在词中间
    assert all(p[-1] in ".?!,…" for p in parts2), parts2
    # 关掉 script_aware = 旧行为（碎句），证明差异来自折算
    legacy = vcc.pack_voice_parts(_EN, part_max_chars=36, max_parts=3, min_tail_chars=8,
                                  script_aware=False)
    assert len(legacy) >= 2


def test_latin_pieces_join_with_space_never_glue_words():
    # 余量合并 / 尾条合并 / 贪心打包三处都不得把 "dock"+"at" 粘成 "dockat"
    text = "on the dock at Granville Island with coffee and the seagulls are loud today ok"
    parts = vcc.pack_voice_parts(text, part_max_chars=10, max_parts=2, min_tail_chars=0,
                                 script_aware=False)
    assert len(parts) == 2
    joined = " ".join(parts)
    for w in text.split():
        assert w in joined.split(), (w, parts)
    assert "dockat" not in joined and "Islandwith" not in joined
    chunks = vcc.split_text_for_clone("hello world. foo bar. baz qux.", 24)
    assert all(" " in c for c in chunks) and "worldfoo" not in "".join(chunks)
    # 中文拼接不加空格（旧行为）
    assert vcc._join_pieces("好呀。", "嗯。") == "好呀。嗯。"
    assert vcc._join_pieces("dock", "at") == "dock at"
    assert vcc._join_pieces("dock,", "at") == "dock,at" or vcc._join_pieces("dock", " at") == "dock at"


def test_chinese_behavior_unchanged():
    long = "".join(f"这是第{i}句话内容还挺长的呢。" for i in range(6))
    a = vcc.pack_voice_parts(long, part_max_chars=20, max_parts=3)
    b = vcc.pack_voice_parts(long, part_max_chars=20, max_parts=3, script_aware=False)
    assert a == b and len(a) == 3
