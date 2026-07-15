# -*- coding: utf-8 -*-
"""
engine_registry.py — Phase 5 引擎适配器层 + 注册表

把 TTS / 变声(VC) / 口型(LipSync) 三类引擎抽象为统一的可插拔后端：
  - 「换模型」从此变成「注册一个 adapter + 改 Profile 配置」，而非重写服务。
  - 老引擎(XTTS / CosyVoice / GPT-SoVITS / RVC / MuseTalk)作为内置后端预注册并保留兜底。
  - 记录每个引擎的可用性与滚动延迟，供 /api/engines 与延迟看板使用。

本模块 **不依赖 avatar_hub**，可独立导入与单测；网络探测由调用方触发。
"""
from __future__ import annotations

import time
import threading
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

# 引擎类别
KIND_TTS = "tts"
KIND_VC = "vc"
KIND_LIPSYNC = "lipsync"
KIND_FACESWAP = "faceswap"
KIND_S2S = "s2s"                 # P0-S2S: 语音到语音同传后端(云端可插拔)
VALID_KINDS = {KIND_TTS, KIND_VC, KIND_LIPSYNC, KIND_FACESWAP, KIND_S2S}

_LATENCY_WINDOW = 200  # 每引擎保留的滚动延迟样本数


@dataclass
class EngineDescriptor:
    """单个引擎的元数据 + 运行时状态。"""
    name: str                       # 唯一标识，如 "xtts" / "rvc" / "seedvc"
    kind: str                       # KIND_TTS / KIND_VC / KIND_LIPSYNC
    backend: str = ""               # 对应 SERVICES 中的服务键（内置引擎用），新引擎可留空
    base_url: str = ""              # 服务地址
    description: str = ""
    capabilities: dict = field(default_factory=dict)
    builtin: bool = True            # 内置(老引擎) or 新接入
    # ── 运行时（非序列化为配置）──
    available: bool = False
    last_latency_ms: Optional[int] = None
    _samples: deque = field(default_factory=lambda: deque(maxlen=_LATENCY_WINDOW), repr=False)

    def record_latency(self, ms: int) -> None:
        if ms is None or ms < 0:
            return
        self.last_latency_ms = int(ms)
        self._samples.append(int(ms))

    def latency_stats(self) -> dict:
        s = sorted(self._samples)
        n = len(s)
        if n == 0:
            return {"count": 0, "avg_ms": None, "p50_ms": None, "p95_ms": None}
        avg = sum(s) / n
        p50 = s[int(n * 0.50)] if n > 1 else s[0]
        p95 = s[min(n - 1, int(n * 0.95))]
        return {"count": n, "avg_ms": round(avg, 1), "p50_ms": p50, "p95_ms": p95}

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "backend": self.backend,
            "base_url": self.base_url,
            "description": self.description,
            "capabilities": self.capabilities,
            "builtin": self.builtin,
            "available": self.available,
            "last_latency_ms": self.last_latency_ms,
            "latency": self.latency_stats(),
        }


# ── 适配器接口（供 Phase 6+ 接入新模型时实现）────────────────────────
class BaseAdapter(ABC):
    """所有引擎适配器的基类。新模型 = 继承对应子类 + register()。"""
    descriptor: EngineDescriptor

    @abstractmethod
    def capabilities(self) -> dict:
        ...


class TTSAdapter(BaseAdapter):
    @abstractmethod
    async def synthesize(self, text: str, *, ref_audio_b64: str = "",
                         voice_name: str = "", language: str = "zh-cn",
                         emotion: str = "neutral", instruct: str = "",
                         **opts) -> bytes:
        """返回合成音频字节(wav)。"""
        ...


class VCAdapter(BaseAdapter):
    @abstractmethod
    async def convert(self, audio_b64: str, *, target: str = "",
                      settings: Optional[dict] = None, **opts) -> str:
        """输入音频(base64)，返回转换后的音频(base64)。target 为目标音色/模型标识。"""
        ...


class LipSyncAdapter(BaseAdapter):
    @abstractmethod
    async def generate(self, audio_bytes: bytes, face_bytes: bytes,
                       *, fps: int = 25, **opts) -> bytes:
        """返回口型同步 MP4 字节。"""
        ...


class FaceSwapAdapter(BaseAdapter):
    @abstractmethod
    async def swap(self, src_bytes: bytes, *, face_bytes: bytes = b"",
                   **opts) -> bytes:
        """输入源图/帧，返回换脸后图像字节。face_bytes 为目标人脸。"""
        ...


# ── 注册表 ───────────────────────────────────────────────────────────
class EngineRegistry:
    def __init__(self) -> None:
        self._engines: dict[str, EngineDescriptor] = {}
        self._adapters: dict[str, BaseAdapter] = {}
        self._lock = threading.Lock()

    def register(self, desc: EngineDescriptor, adapter: Optional[BaseAdapter] = None) -> None:
        if desc.kind not in VALID_KINDS:
            raise ValueError(f"非法引擎类别: {desc.kind}（应为 {sorted(VALID_KINDS)}）")
        with self._lock:
            self._engines[desc.name] = desc
            if adapter is not None:
                adapter.descriptor = desc
                self._adapters[desc.name] = adapter

    def get(self, name: str) -> Optional[EngineDescriptor]:
        return self._engines.get(name)

    def get_adapter(self, name: str) -> Optional[BaseAdapter]:
        return self._adapters.get(name)

    def attach_adapter(self, name: str, adapter: BaseAdapter) -> bool:
        """给已注册的引擎挂上具体适配器（保留其描述符与延迟样本）。"""
        d = self._engines.get(name)
        if d is None:
            return False
        adapter.descriptor = d
        self._adapters[name] = adapter
        return True

    def exists(self, name: str) -> bool:
        return name in self._engines

    def names(self, kind: Optional[str] = None) -> list[str]:
        return [n for n, d in self._engines.items() if kind is None or d.kind == kind]

    def list(self, kind: Optional[str] = None) -> list[dict]:
        items = [d for d in self._engines.values() if kind is None or d.kind == kind]
        items.sort(key=lambda d: (d.kind, not d.builtin, d.name))
        out = []
        for d in items:
            dd = d.to_dict()
            dd["has_adapter"] = d.name in self._adapters
            out.append(dd)
        return out

    def record_latency(self, name: str, ms: int) -> None:
        d = self._engines.get(name)
        if d is not None:
            d.record_latency(ms)

    def set_available(self, name: str, available: bool) -> None:
        d = self._engines.get(name)
        if d is not None:
            d.available = bool(available)

    def update_availability(self, health: dict) -> None:
        """传入 {service_key: bool} 的健康表，按 backend 同步各引擎可用性。"""
        for d in self._engines.values():
            if d.backend and d.backend in health:
                d.available = bool(health[d.backend])

    def probe(self, name: str, *, timeout: float = 1.5) -> bool:
        """主动探测单个引擎健康并测量延迟（需要 requests）。"""
        d = self._engines.get(name)
        if d is None or not d.base_url:
            return False
        try:
            import requests
            endpoint = "/inputDevices" if d.backend == "rvc" else "/health"
            t0 = time.time()
            r = requests.get(f"{d.base_url}{endpoint}", timeout=timeout)
            ok = r.status_code == 200
            d.available = ok
            if ok:
                d.record_latency(int((time.time() - t0) * 1000))
            return ok
        except Exception:
            d.available = False
            return False


# 全局单例
registry = EngineRegistry()


def _register_builtins() -> None:
    """注册当前已有的内置引擎（与 avatar_hub.SERVICES 对齐）。"""
    builtins = [
        EngineDescriptor(
            name="xtts", kind=KIND_TTS, backend="tts",
            base_url="http://127.0.0.1:7851",
            description="Coqui XTTS-v2 多语言零样本克隆（默认 TTS）",
            capabilities={"zero_shot": True, "languages": 17, "emotion": False,
                          "streaming": True, "license": "CPML", "sample_rate": 24000},
        ),
        EngineDescriptor(
            name="cosyvoice", kind=KIND_TTS, backend="emotion_tts",
            base_url="http://127.0.0.1:7852",
            description="CosyVoice3-0.5B 情感 TTS（emotion/instruct）",
            capabilities={"zero_shot": True, "emotion": True, "instruct": True,
                          "license": "Apache-2.0", "sample_rate": 24000},
        ),
        EngineDescriptor(
            name="gptsovits", kind=KIND_TTS, backend="singing",
            base_url="http://127.0.0.1:7853",
            description="GPT-SoVITS v4（运行时已下线；7853 现由 Song Studio 翻唱服务占用）",
            capabilities={"few_shot": True, "singing": False, "deprecated": True,
                          "vocoder": "BigVGAN"},
        ),
        EngineDescriptor(
            name="yingmusic_svc", kind=KIND_VC, backend="singing",
            base_url="http://127.0.0.1:7853",
            description="YingMusic-SVC 零样本歌声转换（AI 翻唱：分离+换声+混音，MIT 可商用）",
            capabilities={"zero_shot": True, "singing_cover": True,
                          "vocal_separation": True, "auto_pitch": True,
                          "license": "MIT", "sample_rate": 44100},
        ),
        EngineDescriptor(
            name="fish_speech", kind=KIND_TTS, backend="fish_tts",
            base_url="http://127.0.0.1:7855",
            description="Fish-Speech 1.5 零样本克隆 TTS（业界最佳中文WER 0.54%，自然语言情感控制）",
            capabilities={"zero_shot": True, "emotion": True, "instruct": True,
                          "languages": 17, "license": "CC-BY-NC-SA-4.0",
                          "sample_rate": 44100, "vocoder": "FireflyGAN"},
        ),
        EngineDescriptor(
            name="voxcpm2", kind=KIND_TTS, backend="voxcpm",
            base_url="http://127.0.0.1:7856",
            description="VoxCPM2 无分词器扩散-AR TTS（Apache-2.0 可商用，30语+9方言，48kHz，Voice Design/风格可控克隆，vLLM-Omni 多租户）",
            capabilities={"zero_shot": True, "emotion": True, "instruct": True,
                          "voice_design": True, "languages": 30,
                          "license": "Apache-2.0", "sample_rate": 48000,
                          "streaming": True, "commercial": True,
                          "fish_compatible": True},
            builtin=False,
        ),
        EngineDescriptor(
            name="qwen3_tts", kind=KIND_TTS, backend="qwen3_tts",
            base_url="http://127.0.0.1:7858",
            description="Qwen3-TTS 克隆 TTS（阿里·Apache-2.0 可商用·3秒极速克隆·10语种·"
                        "音色相似度最优(campplus 0.76 vs fish 0.37)·批推理离线配音；"
                        "0.6B Base 不支持 instruct，非实时(3060 RTF>1)→定位离线/商用兜底）",
            capabilities={"zero_shot": True, "emotion": False, "instruct": False,
                          "voice_design": False, "languages": 10,
                          "streaming": True, "batch": True,
                          "license": "Apache-2.0", "commercial": True,
                          "fast_clone_sec": 3, "fish_compatible": True},
            builtin=False,
        ),
        EngineDescriptor(
            name="rvc", kind=KIND_VC, backend="rvc",
            base_url="http://127.0.0.1:6242",
            description="RVC v2 音色转换（实时+离线，需训练 index）",
            capabilities={"realtime": True, "zero_shot": False,
                          "requires_training": True, "f0": ["rmvpe", "fcpe"]},
        ),
        EngineDescriptor(
            name="musetalk", kind=KIND_LIPSYNC, backend="lipsync",
            base_url="http://127.0.0.1:8090",
            description="MuseTalk 1.5 音频驱动口型（实时 30fps+）",
            capabilities={"realtime": True, "resolution": "256x256"},
        ),
        EngineDescriptor(
            name="echomimic", kind=KIND_LIPSYNC, backend="echomimic",
            base_url="http://127.0.0.1:8095",
            description="EchoMimic 全脸音频驱动数字人（高清视频，整脸表情+头动，离线出片）",
            capabilities={"realtime": False, "full_face": True, "resolution": "512x512",
                          "license": "Apache-2.0", "accelerated_steps": 6,
                          "fps": 24, "commercial": True},
            builtin=False,
        ),
        EngineDescriptor(
            name="ditto", kind=KIND_LIPSYNC, backend="ditto",
            base_url="http://127.0.0.1:8096",
            description="Ditto 运动空间扩散·实时说话头（全脸:口型+头动+表情+眨眼，warm 后渲染 50-60fps 快于实时）",
            capabilities={"realtime": True, "full_face": True, "resolution": "512x512",
                          "license": "Apache-2.0", "streaming": True,
                          "fps": 25, "commercial": True},
            builtin=False,
        ),
        EngineDescriptor(
            name="seed_liveinterpret", kind=KIND_S2S, backend="",
            base_url="wss://openspeech.bytedance.com/api/v4/ast/v2/translate",
            description="Seed LiveInterpret 2.0 云端语音到语音同传（火山 AST v2·端到端 ~2.2s·"
                        "复刻说话人音色输出译文；INTERP_S2S_BACKEND=seed + 密钥启用，"
                        "默认关闭；断线自动回退本地级联，离线可用性不受影响）",
            capabilities={"s2s": True, "voice_clone": True, "streaming": True,
                          "languages": ["zh", "en"], "cloud": True, "optional": True,
                          "glossary": True, "fallback": "local_cascade"},
            builtin=False,
        ),
        EngineDescriptor(
            name="inswapper", kind=KIND_FACESWAP, backend="faceswap",
            base_url="http://127.0.0.1:8000",
            description="InsightFace + inswapper_128 换脸（默认）",
            capabilities={"realtime": True, "resolution": "128x128",
                          "enhancer": ["gfpgan", "codeformer"],
                          "execution": ["dml", "cuda", "tensorrt"],
                          "swap_compatible": True},
        ),
        EngineDescriptor(
            name="hyperswap_256", kind=KIND_FACESWAP, backend="faceswap",
            base_url="http://127.0.0.1:8000",
            description="高清换脸 256（HyperSwap/Ghost/SimSwap 同格式 ONNX·画质↑·光流时序平滑）",
            capabilities={"realtime": True, "resolution": "256x256",
                          "enhancer": ["gfpgan", "codeformer"],
                          "execution": ["dml", "cuda", "tensorrt"],
                          "temporal_flow": True, "swap_compatible": True},
            builtin=False,
        ),
    ]
    for d in builtins:
        registry.register(d)


_register_builtins()


def default_engine(kind: str) -> str:
    """各类别的默认引擎名。
    TTS 默认用 fish_speech：它是常驻核心、可商用主力(业界最佳中文 WER)，且始终在线；
    旧默认 xtts 是「可选扩展(缺权重默认不启动)」——当 profile 未显式指定引擎时回退到 xtts
    会落到不可用引擎导致合成失败。改回常驻主力，消除「默认引擎指向不可用」的清单不一致。"""
    return {KIND_TTS: "fish_speech", KIND_VC: "rvc", KIND_LIPSYNC: "musetalk",
            KIND_FACESWAP: "inswapper"}.get(kind, "")
