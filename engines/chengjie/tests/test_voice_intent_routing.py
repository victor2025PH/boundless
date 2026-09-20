# -*- coding: utf-8 -*-
"""客户点名要语音/唱歌 → 强制语音路由门禁（P0-5，2026-07-29 对练实证）。

实录：客户「发条语音呗/你唱首歌」时 when_peer_voice 只认「对方发了语音」→ 打字
要语音永远得不到语音，AI 打字冒充唱歌。decide_voice 的 peer_requested_voice 强制路由。
"""
from __future__ import annotations

from src.inbox.voice_autosend import decide_voice


_VB = {"enabled": True, "trigger": "when_peer_voice", "max_chars": 110}


def test_peer_requested_forces_voice_when_peer_voice_off():
    # 对方没发语音（is_peer_voice=False），但点名要语音 → 强制走语音
    d = decide_voice(_VB, "好呀我跟你说～", peer_sent_voice=False,
                     peer_requested_voice=True)
    assert d.send_voice and d.reason == "peer_requested"


def test_without_request_when_peer_voice_still_text():
    # 没点名 + 对方没发语音 → 维持 when_peer_voice 旧行为（文字）
    d = decide_voice(_VB, "好呀我跟你说～", peer_sent_voice=False,
                     peer_requested_voice=False)
    assert not d.send_voice


def test_request_relaxes_max_chars_but_hard_cap():
    long_ok = "字" * 200      # > max_chars(110) 但 < 硬帽(300)
    long_no = "字" * 400      # > 硬帽
    assert decide_voice(_VB, long_ok, peer_requested_voice=True).send_voice
    assert not decide_voice(_VB, long_no, peer_requested_voice=True).send_voice


def test_never_still_wins_over_request():
    vb = {"enabled": True, "trigger": "never", "max_chars": 110}
    assert not decide_voice(vb, "唱两句", peer_requested_voice=True).send_voice


def test_disabled_ignores_request():
    vb = {"enabled": False, "trigger": "always"}
    assert not decide_voice(vb, "唱两句", peer_requested_voice=True).send_voice


def test_wants_media_voice_signal_covers_sing():
    # 路由信号源：wants_media 必须把「唱歌」也识别为要语音
    from src.ai.outbound_promise_guard import wants_media
    assert wants_media("你唱首歌给我听") == "voice"
    assert wants_media("发条语音呗想听你声音") == "voice"
    assert wants_media("说句话我听听") == "voice"
