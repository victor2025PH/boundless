"""AvatarHub 语音服务客户端 — 本机 CosyVoice3(7852) / Qwen3-TTS(7858) + 远端 Whisper STT(7854)。

背景：本机(192.168.0.117) 是 AvatarHub 集群的 TTS 节点，D:/faceX/mfys 下常驻两个
语音 HTTP 服务（计划任务开机自启）。本模块**只做 HTTP 调用**——严禁在本项目内
加载任何 TTS/GPU 模型（3060 显存预算已满，自行加载会挤爆在线服务，有过 OOM 事故）。

服务契约（已核对源码）：
  A. CosyVoice3 情感克隆 — http://127.0.0.1:7852 （在线回复主力，2~4s/句）
     GET  /health                → {"ok":true,"models_loaded":true,...}
     POST /v1/tts/clone          → {"text","reference_audio_b64","reference_text",
                                    "emotion","speed","return_base64":true}
                                  ← {"audio_base64":"<WAV b64>","sample_rate":24000,...}
     POST /v1/tts/instruct       → {"text","instruct","reference_audio_b64","return_base64":true}
     POST /v1/tts/register_spk   → {"reference_audio_b64":"..."}（预热，显著降首句延迟）
     emotion ∈ neutral/happy/sad/angry/fearful/surprised/disgusted/gentle/excited/calm/serious
  B. Qwen3-TTS 克隆 — http://127.0.0.1:7858 （音色最像但慢 RTF≈2.8，只用于离线/批量预渲染）
     GET  /health                → {"status":"ok","model_loaded":true}
     POST /v1/tts/clone/batch    → {"texts":[...],"reference_audio_b64","reference_text","language"}
                                  ← {"ok":true,"sample_rate":...,"results":[{"audio_base64","seconds"},...]}
     注意 7858 不支持 instruct（返 501），情感只能走 7852。
  C. Whisper STT — http://192.168.0.140:7854 （跨机，需请求头 X-AH-Svc，
     令牌运行时读 D:/faceX/mfys/secrets/service_token.txt，**绝不写进代码库/日志**）
     POST /transcribe_b64        → {"audio_base64","language"} ← {"ok":true,"text",...,"no_speech_prob"}

并发纪律：GPU 单卡（3060），**全局单 worker 串行**调 TTS——模块级锁保证任意时刻
只有一个合成请求在打 GPU（7852/7858 共享一把锁：同一张卡）。单请求超时 90s、
失败重试 1 次。长文本调用方先按句切块（复用 voice_clone_client.split_text_for_clone）。

服务没起时的自愈：ensure_ready() 可经计划任务拉起（schtasks /Run /TN EmotionTTS_Boot /
Qwen3TTS_Boot）后轮询 health 直至就绪（best-effort，仅本机服务有意义）。

可单测纯函数（无网络/IO）：build_clone_payload / build_instruct_payload /
build_batch_payload / build_stt_payload / parse_audio_response / parse_batch_response /
parse_stt_response / normalize_avatar_emotion / find_reference_text
"""
from __future__ import annotations

import base64
import json
import logging
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# CosyVoice3(7852) 支持的情感标签词表
AVATAR_EMOTIONS = (
    "neutral", "happy", "sad", "angry", "fearful", "surprised",
    "disgusted", "gentle", "excited", "calm", "serious",
)

# ⚠ 默认 neutral＝音色保真路径（服务端 zero_shot+逐字稿，音色最像参考音）。
# 非 neutral 标签会让 7852 切 instruct2 情感路径——忽略逐字稿、音色漂移
# （2026-07-13 事故："没用克隆声，像豆包 AI"）。情感标签只该由强情绪显式传入。
DEFAULT_EMOTION = "neutral"

# ── 进程级共享状态 ────────────────────────────────────────────────────────────
# GPU 串行锁：**按主机分锁**（B1 多端点，2026-07-15）。同一主机上的 7852/7858
# 共享一张卡 → 共用一把锁；远端主机（如 176 的 5090）各自一把——本地串行纪律
# 保留，远端合成不被本地队列拖累。
_GPU_LOCKS: Dict[str, threading.Lock] = {}
_GPU_LOCKS_GUARD = threading.Lock()
# 健康缓存：base_url -> (expires_monotonic, ok)
_HEALTH_CACHE: Dict[str, Tuple[float, bool]] = {}
_HEALTH_LOCK = threading.Lock()
# B1 多端点路由（2026-07-15）：合成失败的端点进冷却，期间路由自动落到下一优先级。
# base_url -> monotonic 解禁时刻
_ENDPOINT_BAD_UNTIL: Dict[str, float] = {}
_ENDPOINT_LOCK = threading.Lock()


def _gpu_lock_for(url: str) -> threading.Lock:
    """按 URL 主机取 GPU 串行锁（127.0.0.1/localhost 归并为同一把「本机」锁）。"""
    host = ""
    try:
        host = str(url or "").split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    except Exception:
        host = ""
    if host in ("", "127.0.0.1", "localhost", "0.0.0.0"):
        host = "local"
    with _GPU_LOCKS_GUARD:
        lk = _GPU_LOCKS.get(host)
        if lk is None:
            lk = threading.Lock()
            _GPU_LOCKS[host] = lk
        return lk
# 参考音 b64 缓存：path -> (size, mtime, b64)（参考音几百 KB~几 MB，避免每次读盘+编码）
_REF_B64_CACHE: Dict[str, Tuple[int, int, str]] = {}
_REF_LOCK = threading.Lock()
# 已预热 speaker 指纹（register_spk 幂等防重复）：sha1(ref_b64 前 4KB) 集合
_REGISTERED_SPK: set = set()
# 服务令牌缓存：path -> (mtime, token)
_TOKEN_CACHE: Dict[str, Tuple[float, str]] = {}
_TOKEN_LOCK = threading.Lock()
# 计划任务拉起冷却：task_name -> last_trigger_monotonic
_BOOT_TRIGGER: Dict[str, float] = {}


# ── 纯函数：请求体构建 ────────────────────────────────────────────────────────
def normalize_avatar_emotion(emotion: Optional[str], default: str = DEFAULT_EMOTION) -> str:
    """把任意情绪字符串规整到 CosyVoice3 词表；未知/空 → default。纯函数。"""
    e = str(emotion or "").strip().lower()
    if e in AVATAR_EMOTIONS:
        return e
    d = str(default or DEFAULT_EMOTION).strip().lower()
    return d if d in AVATAR_EMOTIONS else DEFAULT_EMOTION


def build_clone_payload(
    *, text: str, reference_audio_b64: str, reference_text: str = "",
    emotion: str = DEFAULT_EMOTION, speed: float = 1.0,
    flow_temperature: float = 0.0, llm_top_k: int = 0,
    prosody_variation: bool = True,
) -> bytes:
    """7852 /v1/tts/clone 请求体（JSON bytes）。"""
    body: Dict[str, Any] = {
        "text": str(text or ""),
        "reference_audio_b64": reference_audio_b64,
        "emotion": normalize_avatar_emotion(emotion),
        "speed": float(speed or 1.0),
        "return_base64": True,
        "prosody_variation": bool(prosody_variation),
    }
    if reference_text:
        body["reference_text"] = reference_text
    ft = float(flow_temperature or 0)
    if ft > 0:
        body["flow_temperature"] = max(1.0, min(1.18, ft))
    tk = int(llm_top_k or 0)
    if tk > 0:
        body["llm_top_k"] = max(16, min(80, tk))
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def build_instruct_payload(
    *, text: str, instruct: str, reference_audio_b64: str,
) -> bytes:
    """7852 /v1/tts/instruct 请求体——自由语气指令（比 emotion 标签更细腻）。"""
    return json.dumps({
        "text": str(text or ""),
        "instruct": str(instruct or ""),
        "reference_audio_b64": reference_audio_b64,
        "return_base64": True,
    }, ensure_ascii=False).encode("utf-8")


def build_batch_payload(
    *, texts: List[str], reference_audio_b64: str, reference_text: str = "",
    language: str = "zh",
) -> bytes:
    """7858 /v1/tts/clone/batch 请求体（离线/批量预渲染）。"""
    body: Dict[str, Any] = {
        "texts": [str(t or "") for t in texts],
        "reference_audio_b64": reference_audio_b64,
        "language": str(language or "zh"),
    }
    if reference_text:
        body["reference_text"] = reference_text
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def build_stt_payload(audio_bytes: bytes, *, language: str = "zh") -> bytes:
    """7854 /transcribe_b64 请求体（音频字节 → b64）。

    ``language`` 语义（2026-07-13 实测契约）：
      - 具体语种码（"zh"/"en"...）→ Whisper **强制该语言**——对不匹配的音频会
        产出「翻译」而非转写（英文音频 + zh → 中文译文！）；
      - **空串 ""** → 服务端自动检测（多语聊天场景的正确档）；
      - "auto"/null → 服务端 500/422，**必须**在客户端归一化为空串。
    """
    lang = str(language or "").strip().lower()
    if lang == "auto":
        lang = ""
    return json.dumps({
        "audio_base64": base64.b64encode(audio_bytes or b"").decode("ascii"),
        "language": lang,
    }).encode("utf-8")


# ── 纯函数：响应解析 ─────────────────────────────────────────────────────────
def parse_audio_response(body: bytes) -> bytes:
    """解析 7852 clone/instruct 响应 → WAV 字节。失败抛 RuntimeError。"""
    if not body:
        raise RuntimeError("avatar_voice: empty response")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body  # 裸音频字节兜底
    if not isinstance(data, dict):
        raise RuntimeError("avatar_voice: unexpected response shape")
    if data.get("ok") is False:
        msg = data.get("error") or data.get("message") or "tts failed"
        raise RuntimeError(f"avatar_voice: {str(msg)[:200]}")
    b64 = data.get("audio_base64") or data.get("audio")
    if not b64:
        raise RuntimeError(
            f"avatar_voice: no audio in response keys={list(data.keys())}")
    return base64.b64decode(b64)


def parse_batch_response(body: bytes) -> List[bytes]:
    """解析 7858 batch 响应 → WAV 字节列表（与 texts 等长同序）。失败抛。"""
    if not body:
        raise RuntimeError("avatar_voice: empty batch response")
    data = json.loads(body.decode("utf-8"))
    if not isinstance(data, dict) or data.get("ok") is not True:
        msg = (data or {}).get("error") if isinstance(data, dict) else None
        raise RuntimeError(f"avatar_voice: batch failed: {str(msg or body[:120])}")
    out: List[bytes] = []
    for i, item in enumerate(data.get("results") or []):
        b64 = (item or {}).get("audio_base64")
        if not b64:
            raise RuntimeError(f"avatar_voice: batch item {i} has no audio")
        out.append(base64.b64decode(b64))
    return out


def parse_stt_response(body: bytes, *, max_no_speech_prob: float = 0.85) -> Optional[str]:
    """解析 7854 STT 响应 → 文本；ok=false / 静音置信过高 / 空文本 → None。

    ``no_speech_prob`` 是 Whisper 的「本段无人声」概率——超阈值时文本大概率是
    幻觉（尾字幕套话），返 None 交上层按「听不清」处理。
    """
    if not body:
        return None
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("ok") is not True:
        return None
    try:
        nsp = float(data.get("no_speech_prob") or 0.0)
    except (TypeError, ValueError):
        nsp = 0.0
    if nsp >= max_no_speech_prob:
        return None
    text = str(data.get("text") or "").strip()
    return text or None


def find_reference_text(reference_audio_path: str) -> str:
    """参考音逐字稿自动发现：``ref.wav`` 旁的同名 ``.txt``（如 ``ref.txt``）。

    提供逐字稿可显著提升克隆音色相似度。文件不存在/读失败 → ""（不阻塞）。
    """
    try:
        p = Path(str(reference_audio_path or ""))
        if not p.name:
            return ""
        sidecar = p.with_suffix(".txt")
        if sidecar.is_file():
            return sidecar.read_text(encoding="utf-8", errors="replace").strip()
    except Exception:
        pass
    return ""


# ── 令牌 / 参考音缓存 ─────────────────────────────────────────────────────────
def read_service_token(token_file: str) -> str:
    """运行时读跨机服务令牌（X-AH-Svc）。带 mtime 缓存；**绝不写日志/绝不硬编码**。

    文件缺失/读失败 → ""（调用方按 STT 不可用降级，不抛）。
    """
    path = str(token_file or "").strip()
    if not path:
        return ""
    try:
        mtime = Path(path).stat().st_mtime
    except OSError:
        return ""
    with _TOKEN_LOCK:
        hit = _TOKEN_CACHE.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
    try:
        token = Path(path).read_text(encoding="utf-8", errors="strict").strip()
    except Exception:
        return ""
    with _TOKEN_LOCK:
        _TOKEN_CACHE[path] = (mtime, token)
    return token


def load_reference_b64(reference_audio_path: str) -> str:
    """参考音 → base64（进程级缓存，按 size+mtime 指纹自动失效）。失败抛。"""
    p = Path(str(reference_audio_path or ""))
    if not p.is_file():
        raise RuntimeError(f"avatar_voice: reference_audio_missing:{reference_audio_path}")
    st = p.stat()
    key = str(p)
    with _REF_LOCK:
        hit = _REF_B64_CACHE.get(key)
        if hit and hit[0] == st.st_size and hit[1] == int(st.st_mtime):
            return hit[2]
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    with _REF_LOCK:
        _REF_B64_CACHE[key] = (st.st_size, int(st.st_mtime), b64)
    return b64


# ── 客户端 ───────────────────────────────────────────────────────────────────
class AvatarVoiceClient:
    """AvatarHub 语音服务薄 HTTP 客户端（合成走全局 GPU 串行锁）。"""

    def __init__(self, cfg: Optional[Dict[str, Any]] = None) -> None:
        cfg = cfg or {}
        self.enabled: bool = bool(cfg.get("enabled", False))
        # A：CosyVoice3 情感克隆（在线主力）
        self.base_url: str = str(
            cfg.get("base_url") or "http://127.0.0.1:7852").rstrip("/")
        # B1 多端点（2026-07-15）：``base_urls`` 按优先级排列（如 5090 主、3060 备）。
        # 合成失败的端点进冷却（endpoint_cooldown_sec），路由自动落到下一优先级；
        # 未配置 → 单端点=旧行为。``base_url``（单数）与列表并存时并入尾部兼容。
        _raw_bases = cfg.get("base_urls")
        _bases = ([str(b).rstrip("/") for b in _raw_bases if str(b).strip()]
                  if isinstance(_raw_bases, (list, tuple)) else [])
        if not _bases:
            _bases = [self.base_url]
        elif cfg.get("base_url") and self.base_url not in _bases:
            _bases.append(self.base_url)
        self.base_urls: List[str] = _bases
        self.base_url = _bases[0]   # 主端点=首个（watchdog/看板探测口径不变）
        self.endpoint_cooldown_sec: float = float(
            cfg.get("endpoint_cooldown_sec") or 120.0)
        # B：Qwen3-TTS（离线/批量预渲染专用）
        self.qwen_base_url: str = str(
            cfg.get("qwen_base_url") or "http://127.0.0.1:7858").rstrip("/")
        self.health_timeout_sec: float = float(cfg.get("health_timeout_sec") or 3.0)
        self.health_cache_sec: float = float(cfg.get("health_cache_sec") or 30.0)
        # 并发纪律：单请求 90s 超时、失败重试 1 次
        self.synth_timeout_sec: float = float(cfg.get("synth_timeout_sec") or 90.0)
        self.retries: int = int(cfg.get("retries", 1) or 0)
        self.default_emotion: str = normalize_avatar_emotion(
            cfg.get("default_emotion"), DEFAULT_EMOTION)
        self.speed: float = float(cfg.get("speed") or 1.0)
        # 长文本切块（复用 voice_clone_client 的切句器；7852 建议单次 ≤80 字）
        self.chunk_max_chars: int = int(cfg.get("chunk_max_chars", 80) or 0)
        self.chunk_gap_ms: int = int(cfg.get("chunk_gap_ms", 120) or 0)
        # 服务自愈：health 不通时可经计划任务拉起（仅本机 127.0.0.1 服务有意义）
        boot = cfg.get("boot_tasks") if isinstance(cfg.get("boot_tasks"), dict) else {}
        self.boot_task_7852: str = str(boot.get("emotion_tts") or "EmotionTTS_Boot")
        self.boot_task_7858: str = str(boot.get("qwen3_tts") or "Qwen3TTS_Boot")
        self.boot_cooldown_sec: float = float(cfg.get("boot_cooldown_sec") or 120.0)
        # C：远端 Whisper STT
        stt = cfg.get("stt") if isinstance(cfg.get("stt"), dict) else {}
        self.stt_base_url: str = str(
            stt.get("base_url") or "http://192.168.0.140:7854").rstrip("/")
        self.stt_token_file: str = str(
            stt.get("token_file") or "D:/faceX/mfys/secrets/service_token.txt")
        self.stt_timeout_sec: float = float(stt.get("timeout_sec") or 30.0)
        self.stt_language: str = str(stt.get("language") or "zh")
        self.stt_max_no_speech_prob: float = float(
            stt.get("max_no_speech_prob") or 0.85)
        pros = cfg.get("prosody") if isinstance(cfg.get("prosody"), dict) else {}
        self.prosody_enabled: bool = pros.get("enabled", True) is not False
        self.flow_temperature: float = float(pros.get("flow_temperature") or 0)
        self.llm_top_k: int = int(pros.get("llm_top_k") or 0)

    @classmethod
    def from_config(cls, full_config: Dict[str, Any]) -> "AvatarVoiceClient":
        return cls((full_config or {}).get("avatar_voice") or {})

    # ── HTTP 基础 ────────────────────────────────────────────────────────────
    def _post(
        self, url: str, payload: bytes, *, timeout: float,
        headers: Optional[Dict[str, str]] = None,
    ) -> bytes:
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=payload, headers=hdrs, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()

    def _post_with_retry(
        self, url: str, payload: bytes, *, timeout: float,
        headers: Optional[Dict[str, str]] = None, serialize_gpu: bool = True,
    ) -> bytes:
        """合成请求：GPU 串行锁内执行 + 失败重试（共 1+retries 次）。

        队列水位经 AvatarVoiceStats 观测（enter 在等锁前 → depth=排队+执行中）。
        """
        stats = None
        if serialize_gpu:
            try:
                from src.ai.avatar_voice_stats import get_avatar_voice_stats
                stats = get_avatar_voice_stats()
                stats.queue_enter()
            except Exception:
                stats = None
        try:
            last_exc: Optional[Exception] = None
            for attempt in range(1 + max(0, self.retries)):
                try:
                    if serialize_gpu:
                        wait_t0 = time.monotonic()
                        with _gpu_lock_for(url):
                            # 排队等待分段观测（容量规划：等待 vs 合成各占多少）
                            if stats is not None:
                                try:
                                    stats.record_queue_wait(
                                        int((time.monotonic() - wait_t0) * 1000))
                                except Exception:
                                    pass
                            return self._post(url, payload, timeout=timeout, headers=headers)
                    return self._post(url, payload, timeout=timeout, headers=headers)
                except Exception as exc:
                    last_exc = exc
                    if attempt < self.retries:
                        logger.warning(
                            "[avatar_voice] 请求失败(第%d次): %s → 重试", attempt + 1, exc)
                        time.sleep(0.5)
            assert last_exc is not None
            raise last_exc
        finally:
            if stats is not None:
                try:
                    stats.queue_exit()
                except Exception:
                    pass

    # ── 健康 / 自愈 ──────────────────────────────────────────────────────────
    def health(self) -> Dict[str, Any]:
        """7852 健康明细 {reachable, models_loaded}。best-effort，绝不抛。"""
        return self._probe(f"{self.base_url}/health", ok_keys=("ok", "models_loaded"))

    def qwen_health(self) -> Dict[str, Any]:
        """7858 健康明细 {reachable, models_loaded}。"""
        return self._probe(
            f"{self.qwen_base_url}/health", ok_keys=("status", "model_loaded"))

    def stt_health(self) -> Dict[str, Any]:
        """远端 STT(7854) 健康明细 {reachable, models_loaded}（/health 无需令牌）。"""
        return self._probe(
            f"{self.stt_base_url}/health", ok_keys=("ok", "loaded"))

    def _probe(self, url: str, *, ok_keys: Tuple[str, str]) -> Dict[str, Any]:
        detail: Dict[str, Any] = {"reachable": False, "models_loaded": False}
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=self.health_timeout_sec) as r:
                body = r.read()
            detail["reachable"] = True
            data = json.loads(body.decode("utf-8"))
            if isinstance(data, dict):
                flag, loaded_key = ok_keys
                flag_ok = data.get(flag) in (True, "ok")
                detail["models_loaded"] = bool(flag_ok and data.get(loaded_key, True))
        except Exception as exc:
            logger.debug("[avatar_voice] health probe failed %s: %s", url, exc)
        return detail

    def _health_ok_base(self, base: str, *, use_cache: bool = True) -> bool:
        """单端点就绪判定（进程级 30s 缓存，避免每条消息都探）。"""
        now = time.monotonic()
        if use_cache:
            with _HEALTH_LOCK:
                hit = _HEALTH_CACHE.get(base)
            if hit and hit[0] > now:
                return hit[1]
        d = self._probe(f"{base}/health", ok_keys=("ok", "models_loaded"))
        ok = bool(d["reachable"] and d["models_loaded"])
        with _HEALTH_LOCK:
            _HEALTH_CACHE[base] = (now + self.health_cache_sec, ok)
        return ok

    def health_ok(self, *, use_cache: bool = True) -> bool:
        """克隆链是否可用＝**任一**端点就绪（B1 多端点；单端点=旧语义）。"""
        return any(self._health_ok_base(b, use_cache=use_cache)
                   for b in self.base_urls)

    # ── B1 端点路由：失败冷却 + 优先级挑选 ───────────────────────────────────
    def _endpoint_cooling(self, base: str) -> bool:
        with _ENDPOINT_LOCK:
            return time.monotonic() < _ENDPOINT_BAD_UNTIL.get(base, 0.0)

    def _note_endpoint_bad(self, base: str) -> None:
        with _ENDPOINT_LOCK:
            _ENDPOINT_BAD_UNTIL[base] = (
                time.monotonic() + max(10.0, self.endpoint_cooldown_sec))

    def _note_endpoint_ok(self, base: str) -> None:
        with _ENDPOINT_LOCK:
            _ENDPOINT_BAD_UNTIL.pop(base, None)

    def _endpoint_candidates(self) -> List[str]:
        """按优先级出可用端点：未冷却且健康 → 未冷却 → 全冷却时回退主端点。

        健康预检带 30s 缓存（跨主机死端点最多每 30s 花一次 3s 探测，绝不让
        90s 合成超时去当探针——那是今天 218s 响应的元凶之一）。
        """
        alive = [b for b in self.base_urls if not self._endpoint_cooling(b)]
        healthy = [b for b in alive if self._health_ok_base(b)]
        return healthy or alive or [self.base_urls[0]]

    def _post_any(self, path: str, payload: bytes, *, timeout: float) -> bytes:
        """按优先级在候选端点执行合成 POST：失败标记冷却并顺移下一端点。

        忙碌感知（2026-07-21 三端点扩容）：本进程内 GPU 锁被占的主机往后排——
        并发请求派给空闲主机真正并行合成。原实现恒按优先级 → 多端点只当备份，
        并发全部在主端点排队（吞吐不随机器数增长）。空闲组内仍保持配置优先级
        （主力机优先），全忙时回落原优先级顺序排队。"""
        cands = self._endpoint_candidates()
        if len(cands) > 1:
            free = [b for b in cands if not _gpu_lock_for(b).locked()]
            if free and len(free) < len(cands):
                cands = free + [b for b in cands if b not in free]
        last_exc: Optional[Exception] = None
        for base in cands:
            try:
                body = self._post_with_retry(
                    f"{base}{path}", payload, timeout=timeout)
                self._note_endpoint_ok(base)
                if base != self.base_urls[0]:
                    logger.info("[avatar_voice] 经备用端点合成成功 %s", base)
                return body
            except Exception as exc:
                last_exc = exc
                self._note_endpoint_bad(base)
                if len(cands) > 1:
                    logger.warning(
                        "[avatar_voice] 端点 %s 合成失败(%s) → 冷却 %.0fs 切下一端点",
                        base, exc, self.endpoint_cooldown_sec)
        assert last_exc is not None
        raise last_exc

    def mark_health_ok(self) -> None:
        with _HEALTH_LOCK:
            _HEALTH_CACHE[self.base_url] = (
                time.monotonic() + self.health_cache_sec, True)

    def _trigger_boot_task(self, task_name: str) -> bool:
        """schtasks /Run /TN <task> 拉起服务（冷却防重复触发）。best-effort。"""
        if not task_name:
            return False
        now = time.monotonic()
        last = _BOOT_TRIGGER.get(task_name, 0.0)
        if now - last < self.boot_cooldown_sec:
            return False
        _BOOT_TRIGGER[task_name] = now
        try:
            r = subprocess.run(
                ["schtasks", "/Run", "/TN", task_name],
                capture_output=True, text=True, timeout=15,
            )
            ok = r.returncode == 0
            (logger.info if ok else logger.warning)(
                "[avatar_voice] 计划任务拉起 %s → rc=%d", task_name, r.returncode)
            return ok
        except Exception as exc:
            logger.warning("[avatar_voice] 计划任务拉起 %s 失败: %s", task_name, exc)
            return False

    def ensure_ready(
        self, *, wait_sec: float = 90.0, poll_sec: float = 3.0, service: str = "7852",
    ) -> bool:
        """确保服务就绪：health 不通 → 计划任务拉起 → 轮询 health 直至就绪/超时。

        阻塞式（供启动预热/预渲染脚本用）；在线消息路径**不要**调本函数
        （不通直接回落文字，绝不阻塞聊天主流程）。
        """
        probe = self.health if service == "7852" else self.qwen_health
        task = self.boot_task_7852 if service == "7852" else self.boot_task_7858
        d = probe()
        if d["reachable"] and d["models_loaded"]:
            return True
        self._trigger_boot_task(task)
        deadline = time.monotonic() + max(0.0, wait_sec)
        while time.monotonic() < deadline:
            time.sleep(max(0.5, poll_sec))
            d = probe()
            if d["reachable"] and d["models_loaded"]:
                logger.info("[avatar_voice] 服务 %s 已就绪", service)
                if service == "7852":
                    self.mark_health_ok()
                return True
        logger.warning("[avatar_voice] 服务 %s 等待就绪超时(%ss)", service, wait_sec)
        return False

    # ── TTS（7852 在线主力）─────────────────────────────────────────────────
    def tts(
        self, text: str, *, reference_audio_b64: str, reference_text: str = "",
        emotion: Optional[str] = None, speed: Optional[float] = None,
        prosody_variation: Optional[bool] = None,
        flow_temperature: Optional[float] = None,
        llm_top_k: Optional[int] = None,
    ) -> bytes:
        """情感克隆合成 → WAV 字节。长文本自动按句切块逐块合成再拼接。失败抛。

        ``prosody_variation``/``flow_temperature``/``llm_top_k``：per-call 覆盖
        （None=实例配置）。探针 A/B（固定噪声 vs fresh noise 对照）用。
        """
        emo = normalize_avatar_emotion(emotion, self.default_emotion)
        spd = float(speed if speed is not None else self.speed)
        pv = self.prosody_enabled if prosody_variation is None else bool(prosody_variation)
        ft = self.flow_temperature if flow_temperature is None else float(flow_temperature)
        tk = self.llm_top_k if llm_top_k is None else int(llm_top_k)
        chunks = self._split(text)
        parts: List[bytes] = []
        for ch in chunks:
            payload = build_clone_payload(
                text=ch, reference_audio_b64=reference_audio_b64,
                reference_text=reference_text, emotion=emo, speed=spd,
                flow_temperature=ft, llm_top_k=tk, prosody_variation=pv)
            body = self._post_any(
                "/v1/tts/clone", payload, timeout=self.synth_timeout_sec)
            audio = parse_audio_response(body)
            if not audio:
                raise RuntimeError("avatar_voice: decoded empty audio")
            parts.append(audio)
        return self._merge(parts, text, lambda t: self.tts(
            t, reference_audio_b64=reference_audio_b64,
            reference_text=reference_text, emotion=emo, speed=spd,
            prosody_variation=pv, flow_temperature=ft, llm_top_k=tk))

    def tts_instruct(
        self, text: str, *, reference_audio_b64: str, instruct: str,
    ) -> bytes:
        """自由语气合成（如「用撒娇黏人的语气说」）→ WAV 字节。失败抛。"""
        chunks = self._split(text)
        parts: List[bytes] = []
        for ch in chunks:
            payload = build_instruct_payload(
                text=ch, instruct=instruct, reference_audio_b64=reference_audio_b64)
            body = self._post_any(
                "/v1/tts/instruct", payload, timeout=self.synth_timeout_sec)
            audio = parse_audio_response(body)
            if not audio:
                raise RuntimeError("avatar_voice: decoded empty audio")
            parts.append(audio)
        return self._merge(parts, text, lambda t: self.tts_instruct(
            t, reference_audio_b64=reference_audio_b64, instruct=instruct))

    def _split(self, text: str) -> List[str]:
        from src.ai.voice_clone_client import split_text_for_clone
        t = str(text or "")
        if self.chunk_max_chars > 0:
            chunks = split_text_for_clone(t, self.chunk_max_chars)
            return chunks if chunks else [t]
        return [t]

    def _merge(self, parts: List[bytes], text: str, retry_whole) -> bytes:
        """多块 WAV 拼接；拼接异常（格式不一致，极少见）→ 回退整段单次合成。"""
        if len(parts) == 1:
            return parts[0]
        from src.ai.voice_clone_client import concat_wav_bytes
        try:
            return concat_wav_bytes(parts, gap_ms=self.chunk_gap_ms)
        except Exception as ex:
            logger.warning("[avatar_voice] 分块拼接失败(%s)，回退整段合成", ex)
            old = self.chunk_max_chars
            self.chunk_max_chars = 0
            try:
                return retry_whole(text)
            finally:
                self.chunk_max_chars = old

    def register_spk(self, reference_audio_b64: str) -> bool:
        """预热 speaker（bot 启动时对每个人设调一次，显著降首句延迟）。幂等、best-effort。"""
        import hashlib
        fp = hashlib.sha1(reference_audio_b64[:4096].encode("ascii")).hexdigest()
        if fp in _REGISTERED_SPK:
            return True
        try:
            payload = json.dumps(
                {"reference_audio_b64": reference_audio_b64}).encode("utf-8")
            # 预热也过 GPU 锁：避开在线合成高峰互相拖垮
            body = self._post_with_retry(
                f"{self.base_url}/v1/tts/register_spk", payload,
                timeout=self.synth_timeout_sec)
            try:
                data = json.loads(body.decode("utf-8"))
                ok = not (isinstance(data, dict) and data.get("ok") is False)
            except Exception:
                ok = True
            if ok:
                _REGISTERED_SPK.add(fp)
            return ok
        except Exception as exc:
            logger.warning("[avatar_voice] register_spk 失败: %s", exc)
            return False

    # ── 批量预渲染（7858，夜间/离线专用）────────────────────────────────────
    def batch_clone(
        self, texts: List[str], *, reference_audio_b64: str,
        reference_text: str = "", language: str = "zh",
        timeout_sec: Optional[float] = None,
    ) -> List[bytes]:
        """Qwen3-TTS 批量克隆 → WAV 字节列表（与 texts 等长同序）。

        RTF≈2.8 很慢：超时按文本量放大（每条给足 60s，下限 synth_timeout_sec）。
        仅离线/批量预渲染用，**在线回复禁用**（走 tts()/7852）。
        """
        if not texts:
            return []
        timeout = float(timeout_sec or max(self.synth_timeout_sec, 60.0 * len(texts)))
        payload = build_batch_payload(
            texts=texts, reference_audio_b64=reference_audio_b64,
            reference_text=reference_text, language=language)
        body = self._post_with_retry(
            f"{self.qwen_base_url}/v1/tts/clone/batch", payload, timeout=timeout)
        return parse_batch_response(body)

    # ── STT（远端 7854）──────────────────────────────────────────────────────
    def stt(
        self, audio_bytes: bytes, *, language: Optional[str] = None,
    ) -> Optional[str]:
        """语音 → 文本。令牌运行时读取；失败/静音/令牌缺失 → None（调用方降级）。

        入参可为 WAV 或原始 ogg/opus 字节（服务端均可解）；上游建议先经 ffmpeg
        转 16k 单声道 WAV（见 transcribe_file_via_avatar），识别更稳。
        """
        if not audio_bytes:
            return None
        token = read_service_token(self.stt_token_file)
        if not token:
            logger.warning("[avatar_voice] STT 令牌不可用（token_file 缺失/为空）")
            return None
        payload = build_stt_payload(
            audio_bytes, language=str(language or self.stt_language))
        try:
            body = self._post(
                f"{self.stt_base_url}/transcribe_b64", payload,
                timeout=self.stt_timeout_sec, headers={"X-AH-Svc": token})
        except Exception as exc:
            logger.warning("[avatar_voice] STT 请求失败: %s", exc)
            self._record_stt(ok=False)
            return None
        text = parse_stt_response(
            body, max_no_speech_prob=self.stt_max_no_speech_prob)
        self._record_stt(ok=text is not None)
        return text

    @staticmethod
    def _record_stt(*, ok: bool) -> None:
        try:
            from src.ai.avatar_voice_stats import get_avatar_voice_stats
            get_avatar_voice_stats().record_stt(ok=ok)
        except Exception:
            pass

    def translate(
        self, text: str, *, src: str = "zh", dest: str = "en",
    ) -> Optional[str]:
        """7854 NLLB 文本翻译（POST /translate {text,src,dest}）。失败 → None。

        实测 ~70ms/句（zh→en）。**刻意不接入** ``translation.engines`` 引擎栈——
        NLLB-600M 质量弱于在栈的 hy-mt2-7b/DeepSeek，且现有栈已双活+云兜底；
        本方法仅作跨机工具能力保留（如未来集群侧协作需要）。
        """
        t = str(text or "").strip()
        if not t:
            return None
        token = read_service_token(self.stt_token_file)
        if not token:
            return None
        try:
            payload = json.dumps({
                "text": t, "src": str(src or "zh"), "dest": str(dest or "en"),
            }, ensure_ascii=False).encode("utf-8")
            body = self._post(
                f"{self.stt_base_url}/translate", payload,
                timeout=self.stt_timeout_sec, headers={"X-AH-Svc": token})
            data = json.loads(body.decode("utf-8"))
            if isinstance(data, dict) and data.get("ok") is True:
                out = str(data.get("text") or "").strip()
                return out or None
        except Exception as exc:
            logger.warning("[avatar_voice] translate 请求失败: %s", exc)
        return None


# ── Telegram 语音条格式 ──────────────────────────────────────────────────────
def to_voice_note(wav_bytes: bytes, out_dir: Optional[str] = None) -> Tuple[str, int]:
    """WAV 字节 → Telegram 语音条 OGG/Opus 文件。返回 (ogg_path, duration_sec)。

    复用 voice_sender 的 ffmpeg 转换（48k 单声道 voip 档）。失败抛 RuntimeError。
    """
    import tempfile
    import uuid as _uuid

    from src.ai.tts_pipeline import compute_audio_duration_sec
    from src.client.voice_sender import convert_to_ogg_opus

    if not wav_bytes:
        raise RuntimeError("avatar_voice: empty wav bytes")
    base = Path(out_dir) if out_dir else Path(tempfile.gettempdir())
    base.mkdir(parents=True, exist_ok=True)
    wav_path = base / f"avatar-{time.strftime('%Y%m%d-%H%M%S')}-{_uuid.uuid4().hex[:8]}.wav"
    wav_path.write_bytes(wav_bytes)
    dur, _src = compute_audio_duration_sec(str(wav_path), "wav")
    ogg = convert_to_ogg_opus(str(wav_path), delete_src=True)
    if not ogg:
        try:
            wav_path.unlink(missing_ok=True)
        except Exception:
            pass
        raise RuntimeError("avatar_voice: ffmpeg ogg/opus conversion failed")
    return ogg, (int(round(dur)) if dur and dur > 0 else 0)


def convert_to_wav_16k_mono(src_path: str) -> Optional[str]:
    """入站语音条（ogg/opus 等）→ 16k 单声道 WAV（STT 前处理）。

    ffmpeg 缺失/失败 → None（调用方直接送原始字节，服务端也能解）。
    """
    import shutil

    if shutil.which("ffmpeg") is None:
        return None
    src = Path(src_path)
    if not src.is_file():
        return None
    dst = src.parent / (src.stem + "_16k.wav")
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-ar", "16000", "-ac", "1",
             "-f", "wav", str(dst)],
            capture_output=True, text=True, timeout=60,
        )
        if r.returncode != 0 or not dst.is_file() or dst.stat().st_size == 0:
            return None
        return str(dst)
    except Exception:
        return None


# ── 启动预热 ─────────────────────────────────────────────────────────────────
def warmup_personas(full_config: Dict[str, Any]) -> int:
    """对所有配置了 avatar_clone 后端参考音的人设调 register_spk 预热。

    返回成功预热数。阻塞式（调用方放后台线程）；服务不通时先 ensure_ready
    （经计划任务拉起）。任何异常都吞掉——预热失败只影响首句延迟，不影响功能。
    """
    cfg = full_config or {}
    av = AvatarVoiceClient.from_config(cfg)
    if not av.enabled:
        return 0
    refs: List[str] = []
    seen: set = set()

    def _collect_emotion_refs(vp: Dict[str, Any]) -> None:
        """情绪分库参考音（①）也纳入预热：首次强情绪轮次不吃特征抽取冷启。"""
        lib = (vp or {}).get("reference_audio_by_emotion")
        if isinstance(lib, dict):
            for p in lib.values():
                p = str(p or "").strip()
                if p and p not in seen and Path(p).is_file():
                    seen.add(p)
                    refs.append(p)

    def _collect(vp: Any) -> None:
        if not isinstance(vp, dict):
            return
        ref = str(vp.get("reference_audio_path") or "").strip()
        if ref and ref not in seen and Path(ref).is_file():
            seen.add(ref)
            refs.append(ref)
        _collect_emotion_refs(vp)

    # telegram.voice_reply / 全局 voice_profile
    _collect(((cfg.get("telegram") or {}).get("voice_reply") or {}).get("voice_profile"))
    # config.yaml personas.profiles
    for p in (cfg.get("personas") or {}).get("profiles") or []:
        if isinstance(p, dict):
            _collect(p.get("voice_profile"))
    # 运行时 PersonaManager（web 登记/profiles_runtime.yaml 的人设；空 tag=全部）
    try:
        from src.utils.persona_manager import PersonaManager
        for p in PersonaManager.get_instance().get_profiles_by_tag("") or []:
            if isinstance(p, dict):
                _collect(p.get("voice_profile"))
    except Exception:
        pass

    if not refs:
        return 0
    if not av.ensure_ready(wait_sec=120.0):
        logger.warning("[avatar_voice] 预热跳过：7852 未就绪")
        return 0
    n = 0
    for ref in refs:
        try:
            if av.register_spk(load_reference_b64(ref)):
                n += 1
                logger.info("[avatar_voice] 预热完成: %s", Path(ref).name)
        except Exception as exc:
            logger.warning("[avatar_voice] 预热失败 %s: %s", ref, exc)
    return n


def warmup_personas_async(full_config: Dict[str, Any]) -> threading.Thread:
    """后台线程预热（fire-and-forget，绝不阻塞启动）。返回线程句柄（测试用）。"""
    t = threading.Thread(
        target=lambda: warmup_personas(full_config),
        name="avatar-voice-warmup", daemon=True)
    t.start()
    return t


# ── 测试辅助 ─────────────────────────────────────────────────────────────────
def reset_caches() -> None:
    """清空模块级缓存（测试用）。"""
    with _HEALTH_LOCK:
        _HEALTH_CACHE.clear()
    with _REF_LOCK:
        _REF_B64_CACHE.clear()
    with _TOKEN_LOCK:
        _TOKEN_CACHE.clear()
    with _ENDPOINT_LOCK:
        _ENDPOINT_BAD_UNTIL.clear()
    _REGISTERED_SPK.clear()
    _BOOT_TRIGGER.clear()


def nudge_emotion_tts_boot(config: Optional[Dict[str, Any]] = None) -> None:
    """非阻塞：7852 未就绪时后台 schtasks 拉起 EmotionTTS（best-effort）。

    在线消息路径专用——绝不阻塞聊天；与 ensure_ready 阻塞轮询互补。
    """
    cfg = config or {}

    def _run() -> None:
        try:
            av_cfg = cfg.get("avatar_voice") or {}
            if not av_cfg.get("enabled"):
                return
            client = AvatarVoiceClient(av_cfg)
            if client.health_ok(use_cache=True):
                return
            client._trigger_boot_task(client.boot_task_7852)
        except Exception:
            logger.debug("[avatar_voice] nudge boot 异常", exc_info=True)

    threading.Thread(target=_run, daemon=True, name="avatar-voice-nudge").start()
