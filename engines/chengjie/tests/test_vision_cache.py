"""P0-1：入站识图描述语义缓存门禁。

覆盖 ``VisionClient.describe_image_with_ollama_zhipu_fallback`` 外层缓存壳：
命中跳过 VLM / prompt 变不误命中 / model 变不误命中 / 失败不缓存 /
开关关闭 / 缺文件优雅降级 / cache_hit debug tag / stats 快照 / 独立于 OCR-ASR 缓存。

不依赖真实 VLM/网络：monkeypatch ``_describe_fallback_chain`` 计数 + 返回固定结果。
"""
from __future__ import annotations

import os
import tempfile

import pytest

from src.ai.media_text_cache import get_media_text_cache, get_vision_desc_cache
from src.vision_client import VisionClient, vision_cache_stats


def _mk_img(data: bytes = b"stable-img-bytes-for-sha1") -> str:
    fd, path = tempfile.mkstemp(prefix="vc_test_", suffix=".jpg")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


def _patch_chain(monkeypatch, result: dict, counter: dict) -> None:
    async def fake_chain(cls, merged, gv, image_path, prompt=None):
        counter["n"] += 1
        return result["value"]

    monkeypatch.setattr(VisionClient, "_describe_fallback_chain", classmethod(fake_chain))


@pytest.fixture(autouse=True)
def _reset_cache():
    get_vision_desc_cache().reset()
    yield
    get_vision_desc_cache().reset()


@pytest.mark.asyncio
async def test_cache_hit_skips_second_vlm_call(monkeypatch):
    counter = {"n": 0}
    _patch_chain(monkeypatch, {"value": ("一张猫的照片", "ollama_ok")}, counter)
    path = _mk_img()
    try:
        merged = {"model": "qwen2.5vl"}
        t1, tag1 = await VisionClient.describe_image_with_ollama_zhipu_fallback(merged, {}, path, "描述")
        t2, tag2 = await VisionClient.describe_image_with_ollama_zhipu_fallback(merged, {}, path, "描述")
        assert (t1, tag1) == ("一张猫的照片", "ollama_ok")
        assert t2 == "一张猫的照片" and tag2 == "cache_hit"
        assert counter["n"] == 1  # 第二次命中缓存，未再调 VLM
    finally:
        os.remove(path)


@pytest.mark.asyncio
async def test_prompt_change_no_false_hit(monkeypatch):
    counter = {"n": 0}
    _patch_chain(monkeypatch, {"value": ("desc", "ollama_ok")}, counter)
    path = _mk_img()
    try:
        merged = {"model": "m"}
        await VisionClient.describe_image_with_ollama_zhipu_fallback(merged, {}, path, "prompt-A")
        _, tag2 = await VisionClient.describe_image_with_ollama_zhipu_fallback(merged, {}, path, "prompt-B")
        assert tag2 == "ollama_ok"  # 不同 prompt 不命中
        assert counter["n"] == 2
    finally:
        os.remove(path)


@pytest.mark.asyncio
async def test_model_change_no_false_hit(monkeypatch):
    """同图同 prompt 但 model 热切换 → 不复用旧 model 的描述。"""
    counter = {"n": 0}
    _patch_chain(monkeypatch, {"value": ("desc", "ollama_ok")}, counter)
    path = _mk_img()
    try:
        await VisionClient.describe_image_with_ollama_zhipu_fallback({"model": "A"}, {}, path, "p")
        _, tag2 = await VisionClient.describe_image_with_ollama_zhipu_fallback({"model": "B"}, {}, path, "p")
        assert tag2 == "ollama_ok"
        assert counter["n"] == 2
    finally:
        os.remove(path)


@pytest.mark.asyncio
async def test_failure_not_cached(monkeypatch):
    """失败/空结果不写缓存——端点恢复后应可重试。"""
    counter = {"n": 0}
    _patch_chain(monkeypatch, {"value": (None, "ollama_empty_no_zhipu_key")}, counter)
    path = _mk_img()
    try:
        merged = {"model": "m"}
        await VisionClient.describe_image_with_ollama_zhipu_fallback(merged, {}, path, "p")
        _, tag2 = await VisionClient.describe_image_with_ollama_zhipu_fallback(merged, {}, path, "p")
        assert tag2 == "ollama_empty_no_zhipu_key"  # 未命中缓存
        assert counter["n"] == 2
    finally:
        os.remove(path)


@pytest.mark.asyncio
async def test_cache_disabled_flag(monkeypatch):
    counter = {"n": 0}
    _patch_chain(monkeypatch, {"value": ("desc", "ollama_ok")}, counter)
    path = _mk_img()
    try:
        merged = {"model": "m", "cache": {"enabled": False}}
        await VisionClient.describe_image_with_ollama_zhipu_fallback(merged, {}, path, "p")
        _, tag2 = await VisionClient.describe_image_with_ollama_zhipu_fallback(merged, {}, path, "p")
        assert tag2 == "ollama_ok"
        assert counter["n"] == 2  # 关闭后每次都真调
    finally:
        os.remove(path)


@pytest.mark.asyncio
async def test_missing_file_degrades_gracefully(monkeypatch):
    """图片文件不可 hash（不存在）→ 不缓存、不崩溃，正常走 chain。"""
    counter = {"n": 0}
    _patch_chain(monkeypatch, {"value": ("desc", "ollama_ok")}, counter)
    missing = os.path.join(tempfile.gettempdir(), "no_such_vc_img_zzz.jpg")
    merged = {"model": "m"}
    t1, _ = await VisionClient.describe_image_with_ollama_zhipu_fallback(merged, {}, missing, "p")
    t2, _ = await VisionClient.describe_image_with_ollama_zhipu_fallback(merged, {}, missing, "p")
    assert t1 == "desc" and t2 == "desc"
    assert counter["n"] == 2  # 无法 hash → 不缓存


@pytest.mark.asyncio
async def test_stats_snapshot(monkeypatch):
    counter = {"n": 0}
    _patch_chain(monkeypatch, {"value": ("desc", "ollama_ok")}, counter)
    path = _mk_img()
    try:
        merged = {"model": "m"}
        await VisionClient.describe_image_with_ollama_zhipu_fallback(merged, {}, path, "p")  # miss + put
        await VisionClient.describe_image_with_ollama_zhipu_fallback(merged, {}, path, "p")  # hit
        st = vision_cache_stats()
        assert st["hits"] >= 1 and st["misses"] >= 1
        assert st["size"] >= 1 and st["max"] == 512
        assert 0.0 <= st["hit_rate"] <= 1.0
    finally:
        os.remove(path)


@pytest.mark.asyncio
async def test_vision_cache_independent_from_ocr_asr_cache(monkeypatch):
    """入站识图缓存与坐席 OCR/ASR 缓存互不干扰（独立单例）。"""
    counter = {"n": 0}
    _patch_chain(monkeypatch, {"value": ("desc", "ollama_ok")}, counter)
    get_media_text_cache().reset()
    path = _mk_img()
    try:
        merged = {"model": "m"}
        await VisionClient.describe_image_with_ollama_zhipu_fallback(merged, {}, path, "p")
        await VisionClient.describe_image_with_ollama_zhipu_fallback(merged, {}, path, "p")
        # 识图缓存有活动，OCR/ASR 缓存零活动
        assert get_vision_desc_cache().stats()["size"] >= 1
        assert get_media_text_cache().stats()["size"] == 0
    finally:
        os.remove(path)
