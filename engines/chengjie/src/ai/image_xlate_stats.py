"""P1-OBS（2026-08-19）：图片翻译链路观测（进程级单例，风格对齐 frontend_error_stats）。

回答运营三问：
1. 坐席在用吗——识别翻译/译文图量、目标语分布（lbxl 前端 beacon 是动作口径，
   这里是**链路成功口径**，两边对照可见「点了没成」）；
2. OCR 走的哪条链——ppocr 微服务 / VLM / 各自缓存的分布，是 imgsvc176 灰度
   切换决策的直接读数（A/B 结论：截图类 ppocr 碾压、实拍照片类 VLM 略胜，
   切换后看这里的分布与覆盖率变化）；
3. 译得全吗——块级覆盖率（识别块/已译/失败），「很多地方没翻译」的量化位。

进程内累计、重启清零；零流量 ``active=False``（ops 卡按站内惯例整卡隐藏）。
record_* 由调用方 try 包住 best-effort，绝不影响翻译链路本身。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

_LANG_CAP = 24          # 目标语种类有限（选择器 ~16 语），cap 防未来枚举失控
_PROVIDER_CAP = 12


def provider_bucket(tag: str, cached: bool = False) -> str:
    """OCR 来源 tag → 观测桶（受控枚举，前端可直接拼 HTML）。

    ppocr* 前缀=专用微服务（含其缓存）；其余按 cached 分 vlm_cache/vlm——
    vision 级联 tag（ollama/zhipu/fallback…）细节不进桶，引擎级观测另有
    provider_stats/vision 卡。
    """
    t = str(tag or "").strip().lower()
    if t.startswith("ppocr_cache"):
        return "ppocr_cache"
    if t.startswith("ppocr"):
        return "ppocr"
    if cached or t == "cache":
        return "vlm_cache"
    return "vlm"


class ImageXlateStats:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.runs = 0
        self.patches = 0
        self.by_provider: Dict[str, int] = {}
        self.by_lang: Dict[str, int] = {}
        self.blocks = 0
        self.blocks_ok = 0
        self.blocks_failed = 0
        self.ms_sum = 0
        self.ms_n = 0
        self.last_ts = 0.0

    def _bump(self, d: Dict[str, int], key: str, cap: int) -> None:
        k = str(key or "").strip().lower() or "unknown"
        if k in d or len(d) < cap:
            d[k] = int(d.get(k, 0)) + 1

    def _record(self, *, provider_tag: str, cached: bool, lang: str,
                stats: Optional[Dict[str, Any]], ms: int, patch: bool) -> None:
        with self._lock:
            if patch:
                self.patches += 1
            else:
                self.runs += 1
            self._bump(self.by_provider, provider_bucket(provider_tag, cached),
                       _PROVIDER_CAP)
            if lang:
                self._bump(self.by_lang, lang, _LANG_CAP)
            st = stats or {}
            try:
                b = int(st.get("blocks") or 0)
                ok = int(st.get("patched") or 0) + int(st.get("identity") or 0)
                fl = int(st.get("failed") or 0)
            except Exception:
                b = ok = fl = 0
            self.blocks += b
            self.blocks_ok += ok
            self.blocks_failed += fl
            if ms and ms > 0:
                self.ms_sum += int(ms)
                self.ms_n += 1
            self.last_ts = time.time()

    def record_run(self, *, provider_tag: str = "", cached: bool = False,
                   lang: str = "", stats: Optional[Dict[str, Any]] = None,
                   ms: int = 0) -> None:
        """识别翻译成功（unified 文本模式带 stats；旧纯文本路径 stats=None）。"""
        self._record(provider_tag=provider_tag, cached=cached, lang=lang,
                     stats=stats, ms=ms, patch=False)

    def record_patch(self, *, provider_tag: str = "", cached: bool = False,
                     lang: str = "", stats: Optional[Dict[str, Any]] = None,
                     ms: int = 0) -> None:
        """译文图生成成功。"""
        self._record(provider_tag=provider_tag, cached=cached, lang=lang,
                     stats=stats, ms=ms, patch=True)

    def dump(self) -> Dict[str, Any]:
        """观测快照（无敏感字段；provider/lang 键为受控枚举/语种码）。"""
        with self._lock:
            total = self.runs + self.patches
            return {
                "active": total > 0,
                "runs": self.runs,
                "patches": self.patches,
                "providers": dict(self.by_provider),
                "langs": dict(self.by_lang),
                "coverage": {
                    "blocks": self.blocks,
                    "ok": self.blocks_ok,
                    "failed": self.blocks_failed,
                    "ratio": (round(self.blocks_ok / self.blocks, 4)
                              if self.blocks else None),
                },
                "avg_ms": (int(self.ms_sum / self.ms_n) if self.ms_n else 0),
                "last_ts": self.last_ts,
            }

    def reset(self) -> None:
        with self._lock:
            self.runs = 0
            self.patches = 0
            self.by_provider.clear()
            self.by_lang.clear()
            self.blocks = self.blocks_ok = self.blocks_failed = 0
            self.ms_sum = self.ms_n = 0
            self.last_ts = 0.0


_SINGLETON: Optional[ImageXlateStats] = None
_LOCK = threading.Lock()


def get_image_xlate_stats() -> ImageXlateStats:
    global _SINGLETON
    if _SINGLETON is None:
        with _LOCK:
            if _SINGLETON is None:
                _SINGLETON = ImageXlateStats()
    return _SINGLETON


__all__ = ["ImageXlateStats", "get_image_xlate_stats", "provider_bucket"]
