"""WhatsApp Cloud 产品化 + 官方 media_ref 识图接线门禁（P2，2026-08-05）。"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.integrations.platform_readiness import _IMPLEMENTED_MODES
from src.integrations.whatsapp_cloud import extract_wa_media_id
from src.utils.channel_setup import get_channel


def test_whatsapp_cloud_channel_declares_official_bridge():
    ch = get_channel("whatsapp")
    assert ch is not None
    assert ch.official_platform == "whatsapp"
    assert ch.enable_key == "whatsapp_cloud.enabled"
    assert "platform_login.orchestrator_enabled" in ch.enable_on_ready
    assert ("whatsapp", "official") in _IMPLEMENTED_MODES


def test_extract_wa_media_id():
    assert extract_wa_media_id({"type": "text", "text": {"body": "hi"}}) == ("", "")
    assert extract_wa_media_id({
        "type": "image", "image": {"id": "MID1", "mime_type": "image/jpeg"},
    }) == ("image", "MID1")
    assert extract_wa_media_id({"type": "audio", "audio": {"id": "A1"}}) == ("audio", "A1")


@pytest.mark.asyncio
async def test_enrich_fetches_remote_https_when_enabled(tmp_path):
    """media.remote_fetch 开 + https media_ref → 下载后识图（激活 IG/Messenger CDN）。"""
    from src.inbox.media_enrich import enrich_inbound_media_text

    img = tmp_path / "x.jpg"
    img.write_bytes(b"fake-jpg")
    cfg = {
        "media": {"remote_fetch": {"enabled": True, "max_mb": 1}},
        "vision": {"enabled": True},
    }

    async def _fake_fetch(url, **kw):
        assert url.startswith("https://")
        return str(img), "ok"

    with patch("src.inbox.media_fetch.fetch_remote_media", _fake_fetch), \
         patch("src.inbox.media_enrich._describe_image",
               AsyncMock(return_value="一只猫")):
        text, desc = await enrich_inbound_media_text(
            media_type="image",
            media_ref="https://cdn.example.com/a.jpg",
            config=cfg,
        )
    assert "猫" in desc
    assert "图片内容" in text
    # 临时下载文件应已被清理（我们返回的是已有 path，假 fetch 没建新文件——
    # 这里只钉「走了远程分支且识别成功」）


@pytest.mark.asyncio
async def test_enrich_skips_remote_when_fetch_disabled():
    from src.inbox.media_enrich import enrich_inbound_media_text

    text, desc = await enrich_inbound_media_text(
        media_type="image",
        media_ref="https://cdn.example.com/a.jpg",
        config={"media": {"remote_fetch": {"enabled": False}}},
    )
    assert desc == ""
    assert text == "[图片]"


@pytest.mark.asyncio
async def test_wa_non_text_mirrors_downloaded_media_ref(monkeypatch):
    from src.integrations import whatsapp_cloud as wa

    mirrored = []
    monkeypatch.setattr(
        "src.integrations.shared.official_inbound.mirror_inbound_media",
        lambda **kw: mirrored.append(kw) or True)
    monkeypatch.setattr(wa, "wa_download_media_file",
                        AsyncMock(return_value=r"C:\tmp\wa_in_x.jpg"))
    monkeypatch.setattr(wa, "wa_send_text", AsyncMock(return_value={"ok": True}))

    await wa._handle_one_message(
        msg={"from": "15551234567", "type": "image", "id": "wamid.1",
             "image": {"id": "MEDIA99"}},
        sm=None, phone_number_id="1099", access_token="tok",
        unsupported="unsupported",
    )
    assert mirrored and mirrored[0]["media_ref"].endswith("wa_in_x.jpg")
    assert mirrored[0]["media_type"] == "image"
