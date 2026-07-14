"""全自动数字人口播视频（video_autosend）单测。

锁定：请求检测 / 决策护栏（默认关/on_request/超长/危机/频率上限）/ payload 构造 /
响应解析 / stage 合成（mock HTTP）/ 人设白名单 / 每会话每日频率。全离线。
"""
from __future__ import annotations

import asyncio

import pytest

from src.inbox import video_autosend as V


@pytest.fixture(autouse=True)
def _reset_daily():
    V.reset_daily()
    yield
    V.reset_daily()


# ── 请求检测 ──────────────────────────────────────────────

def test_detect_video_request():
    assert V.detect_video_request("给我录个视频看看") is True
    assert V.detect_video_request("说句话给我看") is True
    assert V.detect_video_request("send me a video of you") is True
    assert V.detect_video_request("发张照片") is False       # 要图不是要视频
    assert V.detect_video_request("在吗") is False
    assert V.detect_video_request("") is False


# ── 决策护栏 ──────────────────────────────────────────────

def _vb(**kw):
    base = {"enabled": True, "trigger": "on_request", "max_chars": 120, "daily_cap": 3}
    base.update(kw)
    return base


def test_decide_disabled_default():
    ok, reason = V.decide_video({}, "你好", peer_text="录个视频")
    assert ok is False and reason == "disabled"


def test_decide_on_request_hits():
    ok, reason = V.decide_video(_vb(), "好呀我录给你看~", peer_text="给我录个视频")
    assert ok is True and reason == "requested"


def test_decide_on_request_no_request_falls_back():
    ok, reason = V.decide_video(_vb(), "今天天气不错", peer_text="今天忙吗")
    assert ok is False and reason == "no_request"


def test_decide_peer_video_reciprocal():
    ok, reason = V.decide_video(_vb(), "哈哈你也太可爱了", peer_text="", peer_sent_video=True)
    assert ok is True and reason == "peer_video"


def test_decide_too_long():
    ok, reason = V.decide_video(_vb(max_chars=10), "这段话明显超过十个字了啦啦啦", peer_text="录个视频")
    assert ok is False and reason == "too_long"


def test_decide_crisis_safe():
    ok, reason = V.decide_video(_vb(), "我在的别怕", peer_text="录个视频", crisis_block=True)
    assert ok is False and reason == "crisis_safe"


def test_decide_always():
    ok, reason = V.decide_video(_vb(trigger="always"), "你好呀", peer_text="")
    assert ok is True and reason == "trigger_always"


def test_decide_never():
    ok, reason = V.decide_video(_vb(trigger="never"), "你好呀", peer_text="录个视频")
    assert ok is False and reason == "trigger_never"


def test_decide_daily_cap():
    vb = _vb(daily_cap=2)
    ck = "telegram:a:c"
    V.bump_daily(ck)
    V.bump_daily(ck)
    ok, reason = V.decide_video(vb, "好呀", peer_text="录个视频", conv_key=ck)
    assert ok is False and reason == "daily_cap"


def test_daily_count_and_bump():
    ck = "telegram:a:c2"
    assert V.daily_count(ck) == 0
    V.bump_daily(ck)
    assert V.daily_count(ck) == 1


# ── 人设白名单 / profile ──────────────────────────────────

def test_persona_allowlist():
    assert V.persona_allowed_for_video({}, "lin_xiaoyu") is True         # 空=不限
    assert V.persona_allowed_for_video({"persona_allowlist": ["lin_xiaoyu"]}, "lin_xiaoyu") is True
    assert V.persona_allowed_for_video({"persona_allowlist": ["lin_xiaoyu"]}, "other") is False


def test_resolve_avatar_profile():
    assert V.resolve_avatar_profile({}, "lin_xiaoyu") == "lin_xiaoyu"    # 缺省=persona_id
    assert V.resolve_avatar_profile(
        {"persona_profiles": {"lin_xiaoyu": "XiaoyuAvatar"}}, "lin_xiaoyu") == "XiaoyuAvatar"


# ── payload / 响应解析 ────────────────────────────────────

def test_build_speak_payload():
    p = V.build_speak_payload("你好", profile="林小玲", emotion="happy")
    assert p["text"] == "你好" and p["profile"] == "林小玲"
    assert p["generate_lipsync"] is True and p["emotion"] == "happy"
    assert p["language"] == "zh-cn"           # 契约字段


def test_build_speak_payload_language_override():
    p = V.build_speak_payload("hi", profile="c1", language="en")
    assert p["language"] == "en"


def test_build_speak_payload_field_override():
    p = V.build_speak_payload("hi", profile="c1",
                              field_names={"profile": "character", "text": "content"})
    assert p["content"] == "hi" and p["character"] == "c1"


def test_parse_speak_video_b64():
    assert V.parse_speak_video_b64({"lipsync_video_b64": "AAAA"}) == "AAAA"
    assert V.parse_speak_video_b64({"video_base64": "BBBB"}) == "BBBB"
    assert V.parse_speak_video_b64({}) == ""
    assert V.parse_speak_video_b64("nope") == ""


# ── stage 合成（mock HTTP）───────────────────────────────

def test_stage_video_file_success(monkeypatch):
    import base64
    fake_mp4 = base64.b64encode(b"MP4DATA").decode()
    monkeypatch.setattr(V, "_post_json",
                        lambda url, payload, timeout: {"lipsync_video_b64": fake_mp4})
    saved = {}

    def _save(platform, account_id, filename, data):
        saved["data"] = data
        saved["filename"] = filename
        return ("/tmp/x.mp4", "/static/x.mp4", "video")

    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(pb, "save_outbound_media", _save)

    vb = {"enabled": True, "base_url": "http://svc:9000"}
    res = asyncio.run(V.stage_video_file(
        {}, "telegram", "a1", "lin_xiaoyu", "你好呀", video_block=vb))
    assert res == ("/tmp/x.mp4", "/static/x.mp4")
    assert saved["data"] == b"MP4DATA" and saved["filename"].endswith(".mp4")


def test_stage_video_file_service_down(monkeypatch):
    def _boom(url, payload, timeout):
        raise OSError("connection refused")
    monkeypatch.setattr(V, "_post_json", _boom)
    res = asyncio.run(V.stage_video_file(
        {}, "telegram", "a1", "lin_xiaoyu", "你好",
        video_block={"enabled": True, "base_url": "http://svc:9000"}))
    assert res is None       # 服务不可达 → None（调用方回落）


def test_stage_video_file_empty_response(monkeypatch):
    monkeypatch.setattr(V, "_post_json", lambda url, payload, timeout: {})
    res = asyncio.run(V.stage_video_file(
        {}, "telegram", "a1", "lin_xiaoyu", "你好",
        video_block={"enabled": True}))
    assert res is None
