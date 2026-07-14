"""P58-2：多模态识别结果缓存（OCR/ASR 复用）。

OCR/ASR 都是高延迟外部调用。同一张图/同一段语音被重复识别（坐席手滑点两次、
或同一图片转发多次）时，按**媒体内容 hash** 命中缓存可直接跳过 provider 调用。

进程级、有界（FIFO 淘汰）、线程安全；只缓存识别出的**文本**（非字节），TTL 可选。
键约定：``f"{kind}:{sha1}"``，kind ∈ {ocr, asr}。
"""

from __future__ import annotations

import hashlib
import threading
import time
from collections import OrderedDict
from typing import Optional, Tuple


class MediaTextCache:
    def __init__(self, max_entries: int = 256, ttl_sec: float = 3600.0) -> None:
        self._lock = threading.RLock()
        self._d: "OrderedDict[str, Tuple[str, float]]" = OrderedDict()
        self._max = int(max_entries)
        self._ttl = float(ttl_sec)
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> Optional[str]:
        if not key:
            return None
        with self._lock:
            item = self._d.get(key)
            if item is None:
                self.misses += 1
                return None
            text, ts = item
            if self._ttl > 0 and (time.time() - ts) > self._ttl:
                self._d.pop(key, None)
                self.misses += 1
                return None
            self._d.move_to_end(key)  # LRU 触达
            self.hits += 1
            return text

    def put(self, key: str, text: str) -> None:
        if not key or not text:
            return
        with self._lock:
            self._d[key] = (text, time.time())
            self._d.move_to_end(key)
            while len(self._d) > self._max:
                self._d.popitem(last=False)

    def reset(self) -> None:
        with self._lock:
            self._d.clear()
            self.hits = 0
            self.misses = 0

    def stats(self) -> dict:
        """命中观测快照：{hits, misses, size, max}。size=当前条目数。"""
        with self._lock:
            total = self.hits + self.misses
            return {
                "hits": self.hits,
                "misses": self.misses,
                "size": len(self._d),
                "max": self._max,
                "hit_rate": round(self.hits / total, 4) if total else 0.0,
            }


def hash_file(path: str) -> Optional[str]:
    """读文件算 sha1；失败（文件不存在/不可读）返回 None（调用方跳过缓存）。"""
    try:
        h = hashlib.sha1()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except Exception:
        return None


_SINGLETON: Optional[MediaTextCache] = None
_LOCK = threading.Lock()


def get_media_text_cache() -> MediaTextCache:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = MediaTextCache()
    return _SINGLETON


# 入站图片/视频帧 VLM 描述结果缓存：独立于 OCR/ASR 缓存（后者服务坐席「图片翻译/语音翻译」
# API），避免自动回复主链的高频识图把坐席翻译缓存挤淘汰；命中率/容量互不干扰、观测独立。
# 容量取 512（识图路径调用量大于坐席翻译）；TTL 1h（同字节图描述本恒定，TTL 仅为容量周转
# 与「model 升级后旧描述陈旧」兜底）。
_VISION_DESC_SINGLETON: Optional[MediaTextCache] = None
_VISION_DESC_LOCK = threading.Lock()


def get_vision_desc_cache() -> MediaTextCache:
    global _VISION_DESC_SINGLETON
    if _VISION_DESC_SINGLETON is None:
        with _VISION_DESC_LOCK:
            if _VISION_DESC_SINGLETON is None:
                _VISION_DESC_SINGLETON = MediaTextCache(max_entries=512, ttl_sec=3600.0)
    return _VISION_DESC_SINGLETON


__all__ = [
    "MediaTextCache",
    "get_media_text_cache",
    "get_vision_desc_cache",
    "hash_file",
]
