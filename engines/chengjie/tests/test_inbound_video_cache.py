"""入站视频跨链路理解缓存（C2, 2026-07-22）单测。

背景：同一条视频被直发线与全自动草稿链各理解一遍（双倍 VLM/ASR），
按「路径+mtime+size」缓存成品描述，第二链路直接复用。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

import src.ai.inbound_video as iv


@pytest.fixture(autouse=True)
def _clean_cache():
    iv._UNDERSTAND_CACHE.clear()
    yield
    iv._UNDERSTAND_CACHE.clear()


def _mk_video(tmp_path, name="v.mp4", data=b"x" * 1000):
    p = tmp_path / name
    p.write_bytes(data)
    return str(p)


@pytest.mark.asyncio
async def test_second_call_hits_cache(tmp_path):
    """同文件第二次理解直接回缓存，不再跑抽帧/VLM。"""
    vp = _mk_video(tmp_path)
    calls = {"visual": 0}

    async def _fake_visual(path, loop, cfg):
        calls["visual"] += 1
        return "桌面上有键盘"

    with patch.object(iv, "_video_visual_desc", side_effect=_fake_visual), \
         patch.object(iv, "_video_audio_understand",
                      new=AsyncMock(return_value=("你好", ""))), \
         patch.object(iv, "vision_usable", return_value=True):
        r1 = await iv.understand_video_file(vp, vision_config={"enabled": True})
        r2 = await iv.understand_video_file(vp, vision_config={"enabled": True})

    assert r1 == r2
    assert "画面：桌面上有键盘" in r1 and "语音：你好" in r1
    assert calls["visual"] == 1, "第二次必须走缓存，不得重跑 VLM"


@pytest.mark.asyncio
async def test_modified_file_invalidates_cache(tmp_path):
    """文件内容变更（mtime/size 变）→ 缓存失效重新理解。"""
    vp = _mk_video(tmp_path, data=b"a" * 500)
    calls = {"n": 0}

    async def _fake_visual(path, loop, cfg):
        calls["n"] += 1
        return f"第{calls['n']}次"

    with patch.object(iv, "_video_visual_desc", side_effect=_fake_visual), \
         patch.object(iv, "_video_audio_understand",
                      new=AsyncMock(return_value=("", ""))), \
         patch.object(iv, "vision_usable", return_value=True):
        await iv.understand_video_file(vp, vision_config={"enabled": True})
        # 改写文件（size 变化 → key 变化）
        _mk_video(tmp_path, data=b"b" * 900)
        await iv.understand_video_file(vp, vision_config={"enabled": True})

    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_empty_result_not_cached(tmp_path):
    """空结果（后端瞬时不可用）不缓存——下一链路应有机会重试成功。"""
    vp = _mk_video(tmp_path)
    calls = {"n": 0}

    async def _fake_visual(path, loop, cfg):
        calls["n"] += 1
        return "" if calls["n"] == 1 else "第二次成功"

    with patch.object(iv, "_video_visual_desc", side_effect=_fake_visual), \
         patch.object(iv, "_video_audio_understand",
                      new=AsyncMock(return_value=("", ""))), \
         patch.object(iv, "vision_usable", return_value=True):
        r1 = await iv.understand_video_file(vp, vision_config={"enabled": True})
        r2 = await iv.understand_video_file(vp, vision_config={"enabled": True})

    assert r1 is None
    assert r2 and "第二次成功" in r2
    assert calls["n"] == 2
