"""共享入站媒体识别层 src/inbox/media_enrich 单测（无网络/无真实 VLM）。"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.inbox.media_enrich import (
    enrich_inbound_media_text,
    is_placeholder_only,
    lazy_voice_transcriber,
    media_placeholder,
)


def test_is_placeholder_only():
    assert is_placeholder_only("")
    assert is_placeholder_only("[图片]")
    assert is_placeholder_only("[语音]")
    assert is_placeholder_only("[媒体]")
    # 识别结果（带描述）不算占位
    assert not is_placeholder_only("[图片内容] 一只橘猫在晒太阳")
    assert not is_placeholder_only("你好在吗")
    assert not is_placeholder_only("[视频内容] 画面：海边")


def test_wechat_voice_duration_is_placeholder():
    """电脑微信入站气泡「语音N秒」不是转写正文（P3-1，2026-09-19）。"""
    from src.inbox.media_enrich import is_voice_duration_placeholder
    for t in ("语音", "语音11秒", "语音 11秒", "语音11\"", "[语音] 11 秒",
              "Voice 11s", "Voice 11 sec", "Voice message 8s"):
        assert is_voice_duration_placeholder(t), t
        assert is_placeholder_only(t), t
    # 英文裸词 / 真转写 / 普通聊天不能误杀
    for t in ("Voice", "hello", "来聊一聊今天的新闻", "语音发我一下",
              "[语音转录] 明天三点见"):
        assert not is_voice_duration_placeholder(t), t


def test_media_placeholder():
    assert media_placeholder("image") == "[图片]"
    assert media_placeholder("voice") == "[语音]"
    assert media_placeholder("video") == "[视频]"
    assert media_placeholder("unknown_kind") == "[媒体]"


@pytest.mark.asyncio
async def test_remote_ref_not_downloaded_returns_placeholder():
    """远程 http URL 本层不下载 → 回落 caption/占位，描述空。"""
    text, desc = await enrich_inbound_media_text(
        media_type="image", media_ref="https://example.com/x.jpg",
        caption="看这个", config={"vision": {"enabled": True}},
    )
    assert text == "看这个"
    assert desc == ""


@pytest.mark.asyncio
async def test_no_media_returns_caption():
    text, desc = await enrich_inbound_media_text(
        media_type="", media_ref="", caption="纯文本", config={},
    )
    assert text == "纯文本"
    assert desc == ""


@pytest.mark.asyncio
async def test_image_enrich_with_vision(tmp_path):
    """本地图片 + Vision 可用 → 组装 [图片内容] 描述。"""
    img = tmp_path / "a.jpg"
    img.write_bytes(b"fakejpg")
    with patch(
        "src.inbox.media_enrich._resolve_local_path", return_value=str(img),
    ), patch(
        "src.vision_client.has_any_vision_backend", return_value=True,
    ), patch(
        "src.vision_client.VisionClient.describe_image_with_ollama_zhipu_fallback",
        new=AsyncMock(return_value=("一只橘猫在窗边晒太阳", "ollama_ok")),
    ):
        text, desc = await enrich_inbound_media_text(
            media_type="image", media_ref="/static/protocol_media/whatsapp/a.jpg",
            caption="", config={"vision": {"enabled": True}},
        )
    assert desc == "一只橘猫在窗边晒太阳"
    assert text == "[图片内容] 一只橘猫在窗边晒太阳"


@pytest.mark.asyncio
async def test_image_enrich_with_caption(tmp_path):
    img = tmp_path / "a.jpg"
    img.write_bytes(b"fakejpg")
    with patch(
        "src.inbox.media_enrich._resolve_local_path", return_value=str(img),
    ), patch(
        "src.vision_client.has_any_vision_backend", return_value=True,
    ), patch(
        "src.vision_client.VisionClient.describe_image_with_ollama_zhipu_fallback",
        new=AsyncMock(return_value=("发票金额 199 元", "zhipu_only")),
    ):
        text, desc = await enrich_inbound_media_text(
            media_type="image", media_ref="/static/protocol_media/whatsapp/a.jpg",
            caption="帮我看下", config={"vision": {"enabled": True}},
        )
    assert "帮我看下" in text
    assert "[图片内容] 发票金额 199 元" in text


@pytest.mark.asyncio
async def test_sticker_enrich_uses_sticker_marker(tmp_path):
    """#143（0902）：贴纸识别产物标 [贴纸内容]，不再冒充 [图片内容]。

    前缀是贴纸性在正文/历史行里的唯一载体（inbound_enrich._match_media_prefix
    回解析为 sticker）——丢了它，后续轮次把表情包当真实照片评论（「贴纸里的猫」
    实锤），且中文描述被当对方话语参与语言判定。
    """
    img = tmp_path / "s.webp"
    img.write_bytes(b"fakewebp")
    with patch(
        "src.inbox.media_enrich._resolve_local_path", return_value=str(img),
    ), patch(
        "src.vision_client.has_any_vision_backend", return_value=True,
    ), patch(
        "src.vision_client.VisionClient.describe_image_with_ollama_zhipu_fallback",
        new=AsyncMock(return_value=("一只卡通猫举着爱心", "ollama_ok")),
    ):
        text, desc = await enrich_inbound_media_text(
            media_type="sticker",
            media_ref="/static/protocol_media/whatsapp/s.webp",
            caption="", config={"vision": {"enabled": True}},
        )
    assert desc == "一只卡通猫举着爱心"
    assert text == "[贴纸内容] 一只卡通猫举着爱心"
    # 回解析必须还原贴纸性（历史行/回扫链同口径）
    from src.inbox.inbound_enrich import _match_media_prefix
    kind, pdesc = _match_media_prefix(text)
    assert kind == "sticker" and pdesc == "一只卡通猫举着爱心"
    # 系统标注不构成语言证据（#74 剥离口径覆盖新前缀）
    from src.ai.lang_policy import strip_system_injected
    assert strip_system_injected(text) == ""
    # strip_media_desc 剥得掉（翻译/记忆抽取卫生）
    from src.inbox.media_enrich import strip_media_desc
    assert strip_media_desc("看这个\n" + text) == "看这个"
    assert strip_media_desc(text) == ""


@pytest.mark.asyncio
async def test_vision_disabled_falls_back(tmp_path):
    """vision.enabled=false → 不识别，回落占位。"""
    img = tmp_path / "a.jpg"
    img.write_bytes(b"fakejpg")
    with patch("src.inbox.media_enrich._resolve_local_path", return_value=str(img)):
        text, desc = await enrich_inbound_media_text(
            media_type="image", media_ref="/static/protocol_media/whatsapp/a.jpg",
            caption="", config={"vision": {"enabled": False}},
        )
    assert desc == ""
    assert text == "[图片]"


@pytest.mark.asyncio
async def test_voice_transcribe(tmp_path):
    """语音 → 转写文本直接作为待回复正文。"""
    ogg = tmp_path / "v.ogg"
    ogg.write_bytes(b"fakeogg")
    vtr = AsyncMock()
    vtr.transcribe_voice_message = AsyncMock(return_value="明天下午三点可以吗")
    with patch("src.inbox.media_enrich._resolve_local_path", return_value=str(ogg)):
        text, desc = await enrich_inbound_media_text(
            media_type="voice", media_ref="/static/protocol_media/whatsapp/v.ogg",
            caption="", config={"voice_recognition": {"language": "auto"}},
            voice_transcriber=vtr,
        )
    assert text == "明天下午三点可以吗"
    assert desc == "明天下午三点可以吗"


@pytest.mark.asyncio
async def test_video_understand(tmp_path):
    mp4 = tmp_path / "c.mp4"
    mp4.write_bytes(b"fakemp4")
    with patch(
        "src.inbox.media_enrich._resolve_local_path", return_value=str(mp4),
    ), patch(
        "src.ai.inbound_video.understand_video_file",
        new=AsyncMock(return_value="画面：一群人聚餐 语音：生日快乐"),
    ):
        text, desc = await enrich_inbound_media_text(
            media_type="video", media_ref="/static/protocol_media/whatsapp/c.mp4",
            caption="", config={"vision": {"enabled": True}},
        )
    assert "[视频内容]" in text
    assert "生日快乐" in desc


def test_lazy_voice_transcriber_disabled_returns_none():
    """未启用 ASR → 不懒建。"""
    assert lazy_voice_transcriber({}) is None
    assert lazy_voice_transcriber({"voice_recognition": {"enabled": False}}) is None


def test_lazy_voice_transcriber_builds_and_caches():
    """启用 ASR + 宿主没建 → 按配置懒建并缓存；配置变更后重建。

    背景：voice_transcriber 只在进程启动时初始化，运行中热开
    voice_recognition.enabled 后它仍是 None——懒建让"一键开启"即时生效。
    """
    import src.inbox.media_enrich as me
    calls = []

    class _FakeVtr:
        def __init__(self, cfg):
            self.cfg = cfg

    def _fake_create(cfg):
        calls.append(cfg)
        return _FakeVtr(cfg)

    old = (me._LAZY_VTR, me._LAZY_VTR_KEY)
    me._LAZY_VTR, me._LAZY_VTR_KEY = None, None
    try:
        with patch(
            "src.voice_transcriber.VoiceTranscriberFactory.create_transcriber",
            side_effect=_fake_create,
        ):
            cfg1 = {"voice_recognition": {"enabled": True, "provider": "faster_whisper"}}
            v1 = lazy_voice_transcriber(cfg1)
            v2 = lazy_voice_transcriber(cfg1)
            assert v1 is not None and v1 is v2, "同配置应命中缓存"
            assert len(calls) == 1
            cfg2 = {"voice_recognition": {"enabled": True, "provider": "openai"}}
            v3 = lazy_voice_transcriber(cfg2)
            assert v3 is not v1, "配置指纹变更应重建"
            assert len(calls) == 2
    finally:
        me._LAZY_VTR, me._LAZY_VTR_KEY = old


@pytest.mark.asyncio
async def test_voice_without_transcriber_uses_lazy(tmp_path):
    """宿主未传 transcriber 但配置已启用 → enrich 内部走懒建。"""
    ogg = tmp_path / "v.ogg"
    ogg.write_bytes(b"fakeogg")
    vtr = AsyncMock()
    vtr.transcribe_voice_message = AsyncMock(return_value="下午三点见")
    with patch(
        "src.inbox.media_enrich._resolve_local_path", return_value=str(ogg),
    ), patch(
        "src.inbox.media_enrich.lazy_voice_transcriber", return_value=vtr,
    ):
        text, desc = await enrich_inbound_media_text(
            media_type="voice", media_ref="/static/protocol_media/whatsapp/v.ogg",
            caption="",
            config={"voice_recognition": {"enabled": True, "language": "auto"}},
            voice_transcriber=None,
        )
    assert text == "下午三点见"
    vtr.transcribe_voice_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_recognition_exception_soft_fallback(tmp_path):
    """识别抛异常 → 软降级占位，绝不外泄异常。"""
    img = tmp_path / "a.jpg"
    img.write_bytes(b"fakejpg")
    with patch(
        "src.inbox.media_enrich._resolve_local_path", return_value=str(img),
    ), patch(
        "src.vision_client.has_any_vision_backend", return_value=True,
    ), patch(
        "src.vision_client.VisionClient.describe_image_with_ollama_zhipu_fallback",
        new=AsyncMock(side_effect=RuntimeError("vlm down")),
    ):
        text, desc = await enrich_inbound_media_text(
            media_type="image", media_ref="/static/protocol_media/whatsapp/a.jpg",
            caption="急", config={"vision": {"enabled": True}},
        )
    assert desc == ""
    assert text == "急"
