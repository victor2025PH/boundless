"""Phase5：入站视频理解共享模块 + enrich 门禁。"""
from __future__ import annotations

import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.ai.inbound_video import (
    DEFAULT_INBOUND_VIDEO_MAX_BYTES,
    compose_video_inbound_text,
    enrich_tg_video_payload,
    resolve_inbound_video_max_bytes,
    tg_has_video_media,
    vision_usable,
)
from src.ai.inbound_video_stats import get_inbound_video_stats
from src.inbox.inbound_enrich import _match_media_prefix, peer_media_context


class _VidMsg:
    def __init__(self, *, caption: str = "", video=True) -> None:
        self.text = None
        self.caption = caption or None
        self.video = object() if video else None
        self.video_note = None
        self.animation = None


def test_resolve_inbound_video_max_bytes_default():
    assert resolve_inbound_video_max_bytes({}) == DEFAULT_INBOUND_VIDEO_MAX_BYTES


def test_resolve_inbound_video_max_bytes_config():
    cfg = {"telegram": {"inbound_video_max_bytes": 5_000_000}}
    assert resolve_inbound_video_max_bytes(cfg) == 5_000_000


def test_compose_video_inbound_text_cases():
    assert compose_video_inbound_text(video_desc="画面：猫") == "[视频内容] 画面：猫"
    assert compose_video_inbound_text(caption="看这个") == "看这个"
    t = compose_video_inbound_text(caption="看这个", video_desc="画面：猫")
    assert t.startswith("看这个\n[视频内容]")
    assert compose_video_inbound_text() == "[视频]"


def test_match_media_prefix_embedded_caption_video():
    t = "用户说明\n[视频内容] 画面：海边日落"
    kind, desc = _match_media_prefix(t)
    assert kind == "video"
    assert "海边" in desc
    ctx = peer_media_context(t)
    assert ctx.get("_peer_message_is_media") is True
    assert ctx.get("_media_kind") == "video"


def test_tg_has_video_media():
    assert tg_has_video_media(_VidMsg())
    assert not tg_has_video_media(_VidMsg(video=False))


@pytest.mark.asyncio
async def test_enrich_tg_video_payload_no_ref_oversize():
    payload = {"media_type": "video", "media_ref": "", "text": ""}
    out = await enrich_tg_video_payload(_VidMsg(), payload, config={})
    assert out["text"] == "[视频]"


@pytest.mark.asyncio
async def test_enrich_tg_video_payload_with_file(tmp_path):
    vid = tmp_path / "clip.mp4"
    vid.write_bytes(b"fake")
    static_root = tmp_path / "static" / "protocol_media" / "telegram"
    static_root.mkdir(parents=True)
    dest = static_root / "a_1.mp4"
    dest.write_bytes(b"fake")
    url = "/static/protocol_media/telegram/a_1.mp4"
    payload = {"media_type": "video", "media_ref": url, "text": "备注一下"}
    with patch(
        "src.integrations.protocol_bridge.static_media_ref_to_path",
        return_value=str(dest),
    ), patch(
        "src.ai.inbound_video.understand_video_file",
        new=AsyncMock(return_value="画面：测试"),
    ):
        out = await enrich_tg_video_payload(
            _VidMsg(caption="备注一下"), payload,
            config={"vision": {"provider": "zhipu", "api_key": "k"}},
        )
    assert "备注一下" in out["text"]
    assert "[视频内容]" in out["text"]
    assert "画面：测试" in out["text"]


def test_vision_usable_openai_compat():
    assert vision_usable({"base_urls": ["http://127.0.0.1:11434/v1"]})
    assert not vision_usable({"provider": "zhipu", "api_key": ""})
