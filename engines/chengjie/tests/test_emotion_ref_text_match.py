# -*- coding: utf-8 -*-
"""情绪参考音转写回验：繁简归一 + 句首叹词容错（2026-08-03 陈默 sad 假阳性）。"""
from tools.qc_emotion_refs import text_match_ratio


def test_traditional_stt_not_rejected():
    script = "唉……今天有点不想说话，心里闷闷的，你能陪我坐一会儿吗。"
    # Whisper 实锤繁体 + 吞句首「唉」
    stt = "今天有點不想說話心裡悶悶的你能陪我坐一會嗎"
    assert text_match_ratio(script, stt) >= 0.80


def test_true_garble_still_low():
    script = "诶你猜怎么着，今天特别顺利，我开心得不行，走路都带风呀！"
    stt = "参考音频里那句卡车报站继续往下念乱七八糟"
    assert text_match_ratio(script, stt) < 0.50


def test_exact_simplified_high():
    script = "嗯，我在呢。刚泡了杯茶坐下来，咱们慢慢聊，不急。"
    stt = "嗯我在呢刚泡了杯茶坐下来咱们慢慢聊不急"
    assert text_match_ratio(script, stt) >= 0.90
