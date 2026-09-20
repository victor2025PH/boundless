# -*- coding: utf-8 -*-
"""回落原因枚举门禁（P0 2026-08-31 提示风暴复盘）。

事故：语种能力闸把日语刻意改道 edge 标准声后，前端只拿到 fallback_from 布尔级
事实，把「刻意的语言路由」播报成「克隆音色暂不可用/通道中断→重试/报障」——
归因错误制造无效工单（实测当时 7852 与 hub 通道全绿）。原因在管线 extras 里
本来就有（clone_lang_blocked / primary_error），只是路由层没转发。

本文件钉两层：① fallback_reason_from_extra 纯函数语义（优先级/兜底/绝不抛）；
② tts-test 与 send-voice 两路由的 voice_meta 都转发 fallback_reason/fallback_lang
（写了没挂线）。前端分支行为由 tests/test_cp_voice_ui_revamp.py 钉。
"""

from __future__ import annotations

import pathlib

from src.ai.lang_voice_route import fallback_reason_from_extra

REPO = pathlib.Path(__file__).resolve().parents[1]


def test_lang_unsupported_takes_priority():
    # 语种闸路径：clone_lang_blocked + fallback_from 同在 → 语种原因优先
    reason, lang = fallback_reason_from_extra({
        "clone_lang_blocked": "ja",
        "fallback_from": "avatar_clone",
        "primary_error": "clone_lang_unsupported:ja",
    })
    assert reason == "lang_unsupported"
    assert lang == "ja"


def test_lang_from_primary_error_when_blocked_key_missing():
    # 兜底：老/异构调用点只带 primary_error 也能归纳出语种
    reason, lang = fallback_reason_from_extra({
        "primary_error": "clone_lang_unsupported:th",
        "fallback_from": "minicpm_clone",
    })
    assert reason == "lang_unsupported"
    assert lang == "th"


def test_quota_before_channel():
    reason, lang = fallback_reason_from_extra({
        "primary_error": "token_wallet_exhausted",
        "fallback_from": "avatar_clone",
    })
    assert reason == "quota"
    assert lang == ""


def test_channel_down_generic_unavailability():
    reason, lang = fallback_reason_from_extra({
        "primary_error": "avatar_clone_unreachable",
        "fallback_from": "avatar_clone",
    })
    assert reason == "channel_down"
    assert lang == ""


def test_voice_name_mapped_without_fallback_chain():
    # B62 场景：没有回落链，只有音色名映射
    reason, lang = fallback_reason_from_extra({"voice_mapped_from": "steven"})
    assert reason == "voice_name_mapped"
    assert lang == ""


def test_unknown_returns_empty_and_never_raises():
    # 判不出=空串（前端走旧通用文案，不猜）；非法输入绝不抛
    assert fallback_reason_from_extra({}) == ("", "")
    assert fallback_reason_from_extra(None) == ("", "")
    assert fallback_reason_from_extra({"provider": "edge_tts"}) == ("", "")


def test_routes_forward_reason_fields():
    """写了没挂线：两条语音路由的 voice_meta 必须转发 reason/lang 两字段。"""
    vr = (REPO / "src" / "web" / "routes" / "voice_routes.py").read_text(
        encoding="utf-8")
    sr = (REPO / "src" / "web" / "routes" / "unified_inbox_send_routes.py"
          ).read_text(encoding="utf-8")
    for src_text, where in ((vr, "voice_routes"), (sr, "send_routes")):
        assert '"fallback_reason"' in src_text, where
        assert '"fallback_lang"' in src_text, where
        assert "fallback_reason_from_extra" in src_text, where
