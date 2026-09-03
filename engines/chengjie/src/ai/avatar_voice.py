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
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

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
# 上游引擎形状缓存（#58 2026-08-30）：base_url -> "cosyvoice"|"indextts2"。
# 由健康探测顺带判定（7852 与 7865 的 /health 响应键不同，见 health_shape_of），
# 不设 TTL——每次探测都会覆写，30s 健康节律天然刷新；副语言标记只对实证
# CosyVoice 形状的端点放行（IndexTTS-2 会把 [breath] 按英文念出＝「PLAS」事故）。
_SHAPE_CACHE: Dict[str, str] = {}
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
# 「令牌不可用」已告警过的用途集合（#161：缺令牌是**稳态配置事实**不是偶发故障，
# 每条语音回验都刷一行 WARNING 只会把真故障淹掉。进程内每个用途只说一次）
_TOKEN_WARNED: set = set()
# 计划任务拉起冷却：task_name -> last_trigger_monotonic
_BOOT_TRIGGER: Dict[str, float] = {}


# ── 纯函数：请求体构建 ────────────────────────────────────────────────────────
def health_shape_of(data: Any) -> str:
    """克隆端点 /health 响应 → 上游引擎形状（纯函数，#58 消费侧判据）。

    - mfys CosyVoice(7852) 家族＝``{ok, models_loaded}`` → "cosyvoice"；
    - IndexTTS-2(7865) 家族＝``{status, model_loaded}`` → "indextts2"；
    - 其他/非 dict → ""（未知）。
    托管网关中继会把上游 /health 原样透传，所以经网关也能判出真实引擎。
    副语言标记（[breath]/[sigh]…）只许送 "cosyvoice"——未知按不安全算。
    """
    if not isinstance(data, dict):
        return ""
    if "ok" in data or "models_loaded" in data:
        return "cosyvoice"
    if "status" in data or "model_loaded" in data:
        return "indextts2"
    return ""


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


# ── 幻声 hub Fish-Speech 高保真克隆（Phase B, 2026-07-24）────────────────────────
# 幻声机(.176:9000) 的 hub 单一 LAN 网关暴露 ``POST /api/tts_only``：按声纹档名
# (profile) 合成一句，内部走 Fish-Speech-1.5 优先的高保真链（44.1kHz 零样本克隆）。
# 参考音在 hub 侧按 profile 已注册——本地**无需**参考音文件；chengjie 人设 id 与 hub
# 档名同名 1:1（lin_jiaxin/zhao_laoshi/...）。失败一律由调用方回落本地 CosyVoice3。
def build_tts_only_payload(
    profile: str, text: str, *, language: str = "", emotion: str = "",
    best_of: int = 1, audio_format: str = "", tts_engine: str = "",
) -> bytes:
    """hub ``/api/tts_only`` 请求体（JSON bytes）。纯函数、可单测。

    - ``emotion`` 为空或 ``neutral`` 不下发（走服务端保真默认路径）。
    - ``best_of>1`` 才下发（多 seed 择优，牺牲耗时换音质，仅非实时）。
    - ``audio_format`` 为空或 ``wav`` 不下发 ``format`` 键（Hub 缺省回 wav=旧行为）；
      如 ``ogg`` 才下发（hub 侧 opus 48k 直出，省本机发送前转码）。
    - ``tts_engine``（2026-07-27 音色一致性）：为空不下发＝沿用 hub 侧档上配置；
      显式给值（如 ``moss_ttsd``）让同一 hub 上各产品用同一引擎——智聊的档被
      ``avatar_profile_sync`` 钉成 fish_speech 而幻影对话走 moss_ttsd，正是「同人设
      两种音色」的根因之一。hub 若忽略此键，行为与不下发完全一致（无害）。
    """
    body: Dict[str, Any] = {"profile": str(profile), "text": str(text)}
    lang = str(language or "").strip()
    if lang:
        body["language"] = lang
    emo = str(emotion or "").strip().lower()
    if emo and emo != "neutral":
        body["emotion"] = emo
    try:
        n = int(best_of or 1)
    except (TypeError, ValueError):
        n = 1
    if n > 1:
        body["best_of"] = n
    fmt = str(audio_format or "").strip().lower()
    if fmt and fmt != "wav":
        body["format"] = fmt
    eng = str(tts_engine or "").strip()
    if eng:
        body["tts_engine"] = eng
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def sniff_audio_format(audio: bytes, default: str = "wav") -> str:
    """按音频魔数判格式：前 4 字节 ``OggS``→ogg / ``RIFF``→wav；识别不出回落 default。

    Hub 侧 ffmpeg 异常时会静默回退 wav（响应 format 字段可能与实际字节不符）——
    魔数为准，绝不允许 .ogg 后缀文件装 wav 字节（Telegram 播不出）。
    """
    head = bytes(audio[:4] if audio else b"")
    if head == b"OggS":
        return "ogg"
    if head == b"RIFF":
        return "wav"
    return default


# ── 引擎归属指纹（2026-08-22「fish 冒充 IndexTTS-2」事故）────────────────────
# hub `/api/tts_only` 的信封只有 ``{ok, audio_base64, format, profile, elapsed_ms}``
# ——**没有引擎字段**（当晚实测）。而 hub 的引擎解析是 prefer 语义：目标引擎在目录里
# 标 unavailable 就静默回落 fish，于是「合成成功」全绿、客户听到的却是另一个人的声音。
# 唯一不依赖 hub 改造的机器判据是**采样率指纹**（各引擎原生出采样率互不相同）。
ENGINE_SAMPLE_RATES: Dict[str, int] = {
    "index_tts": 22050,
    "moss_ttsd": 24000,
    "fish_speech": 44100,
}
# 别名表的键是**去掉所有分隔符**后的小写形（见 normalize_engine_name）——同一个引擎
# 在配置/hub 目录/日志里写法五花八门（index_tts / IndexTTS-2 / indextts2），漏一种
# 写法就等于该次合成不做校验，静默漏判比不做校验更危险。
_ENGINE_ALIASES: Dict[str, str] = {
    "index": "index_tts", "indextts": "index_tts",
    "indextts2": "index_tts", "indexttsv2": "index_tts",
    "moss": "moss_ttsd", "mossttsd": "moss_ttsd", "mosstts": "moss_ttsd",
    "fish": "fish_speech", "fishspeech": "fish_speech", "fishtts": "fish_speech",
}


class HubEngineMismatch(RuntimeError):
    """hub 换了引擎合成（采样率指纹不符）＝音色身份已变，按合成失败处理。"""


def normalize_engine_name(name: Any) -> str:
    """引擎名归一（大小写/分隔符/别名）；认不出的返回下划线形（判定层当「未登记」）。"""
    raw = str(name or "").strip().lower()
    squashed = "".join(ch for ch in raw if ch.isalnum())
    if not squashed:
        return ""
    hit = _ENGINE_ALIASES.get(squashed)
    if hit:
        return hit
    cleaned = "".join(ch if ch.isalnum() else " " for ch in raw)
    return "_".join(cleaned.split())


def wav_sample_rate(audio: bytes) -> Optional[int]:
    """从 RIFF/WAVE 头取采样率。非 wav / 头损坏 / 离谱值一律 None＝**判不了**。

    判不了绝不等于判失败——上层对 None 一律放行（见 ``engine_attribution``）。
    """
    b = bytes(audio or b"")
    if len(b) < 44 or b[:4] != b"RIFF" or b[8:12] != b"WAVE":
        return None
    pos = 12
    while pos + 8 <= len(b):
        cid = b[pos:pos + 4]
        size = int.from_bytes(b[pos + 4:pos + 8], "little")
        body = pos + 8
        if cid == b"fmt " and size >= 16 and body + 16 <= len(b):
            sr = int.from_bytes(b[body + 4:body + 8], "little")
            return sr if 4000 <= sr <= 192000 else None
        if size <= 0:
            return None
        pos = body + size + (size & 1)  # chunk 按偶数字节对齐
    return None


#: 静音判定的峰值地板（int16 满幅的比例）。B61（2026-08-23 `_309`/`_311`）：
#: 钧机克隆 TTS 全链 200、真字节回传（~184KB/10s），播放却无声——引擎在
#: 显存高压下产出「合法容器、零能量」的哑音，尺寸/时长/HTTP 状态全部无鉴别力。
_SILENCE_PEAK_FLOOR = 0.004  # ≈131/32767，远低于任何真实语音（底噪都在其上）
_SILENCE_SCAN_CAP_BYTES = 4 * 1024 * 1024  # 扫描上限（16bit mono 24k ≈ 87s，够）


def _pcm16_peak(pcm: bytes) -> Optional[int]:
    """int16 LE PCM 的绝对峰值（步进抽样防长音频拖热路）。空/奇数长度容错。"""
    n = len(pcm) // 2
    if n <= 0:
        return None
    import array
    try:
        arr = array.array("h")
        arr.frombytes(pcm[: n * 2])
    except Exception:
        return None
    step = max(1, len(arr) // 48000)  # 最多抽 ~48k 个样本点
    peak = 0
    for i in range(0, len(arr), step):
        v = arr[i]
        a = -v if v < 0 else v
        if a > peak:
            peak = a
    return peak


def detect_silent_audio(
    audio: bytes, audio_format: str, *, peak_floor: float = _SILENCE_PEAK_FLOOR,
) -> Optional[bool]:
    """哑音检测：True=确定无声 / False=有能量 / None=**判不了**（一律放行）。

    wav＝stdlib 解 PCM（零依赖，热路安全）；ogg/opus/mp3＝ffmpeg 解码前 30s
    到 s16le 再测峰值（ffmpeg 缺席/失败 → None）。判不了绝不当判失败——
    与 ``wav_sample_rate`` 同一「宁放行不误拦」方针；真正的拦截语义
    （重试/回落）由调用方对 **True** 实施。
    """
    b = bytes(audio or b"")
    if not b:
        return None
    fmt = str(audio_format or "").strip().lower()
    floor = max(1, int(32767 * float(peak_floor)))
    if fmt == "wav" or b[:4] == b"RIFF":
        try:
            import io as _io
            import wave as _wave
            with _wave.open(_io.BytesIO(b)) as w:
                if w.getsampwidth() != 2:
                    return None  # 非 16bit：判不了
                frames = w.readframes(
                    min(w.getnframes(),
                        _SILENCE_SCAN_CAP_BYTES // max(1, w.getnchannels() * 2)))
            peak = _pcm16_peak(frames)
        except Exception:
            return None
        if peak is None:
            return None
        return peak < floor
    # 压缩容器：ffmpeg 解码（客户机随包 ffmpeg 缺席时返 None＝不拦）
    try:
        import subprocess
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", "pipe:0",
             "-t", "30", "-f", "s16le", "-ac", "1", "pipe:1"],
            input=b, capture_output=True, timeout=15)
        if proc.returncode != 0 or not proc.stdout:
            return None
        peak = _pcm16_peak(proc.stdout[:_SILENCE_SCAN_CAP_BYTES])
    except Exception:
        return None
    if peak is None:
        return None
    return peak < floor


def engines_of_sample_rate(
    sample_rate: Optional[int], rate_table: Optional[Dict[str, int]] = None,
) -> List[str]:
    """采样率 → **所有**声明该采样率的引擎名（升序）；不在表内回空列表。

    刻意返回列表而非单值：指纹表一旦从 hub 目录学习就会变大，同一采样率撞名
    （多个引擎都是 44100）是常态。「说不清是谁」不妨碍「肯定不是点名那个」——
    判定层据此仍可给出 mismatch，只是 detail 里列出全部候选。
    """
    if not sample_rate:
        return []
    table = rate_table if rate_table is not None else ENGINE_SAMPLE_RATES
    return sorted(e for e, sr in table.items() if int(sample_rate) == int(sr))


def engine_of_sample_rate(
    sample_rate: Optional[int], rate_table: Optional[Dict[str, int]] = None,
) -> str:
    """采样率 → 引擎名（唯一命中才给名字）；撞名/未登记一律空串。"""
    hits = engines_of_sample_rate(sample_rate, rate_table)
    return hits[0] if len(hits) == 1 else ""


def engine_attribution(
    audio: bytes, audio_format: str, expected_engine: Any,
    *, rate_table: Optional[Dict[str, int]] = None,
) -> Tuple[str, str]:
    """判「这段音频是不是点名的那个引擎出的」→ ``(verdict, detail)``。

    verdict ∈ ``ok`` / ``mismatch`` / ``unknown``。**只有拿到正面反证才判
    mismatch**（采样率落在指纹表里、且不属于点名的引擎）；判不了的一律 unknown
    放行：没钉引擎、引擎不在指纹表、非 wav（hub 转 opus 恒 48k 会抹掉指纹）、
    wav 头不可解析、采样率不在表内。宁可漏判，绝不误杀正常语音。

    ``rate_table`` 缺省＝内置三引擎的实测指纹；传入时为「内置 + hub 目录自报」的
    合并表（见 ``merged_rate_table``），让内置表没覆盖到的引擎也能受保护。
    """
    table = rate_table if rate_table is not None else ENGINE_SAMPLE_RATES
    exp = normalize_engine_name(expected_engine)
    if not exp:
        return "unknown", "未钉引擎"
    exp_sr = table.get(exp)
    if not exp_sr:
        return "unknown", f"引擎 {exp} 未登记指纹"
    fmt = str(audio_format or "").strip().lower()
    if fmt != "wav":
        return "unknown", f"format={fmt or '?'} 无指纹（opus 恒 48k）"
    sr = wav_sample_rate(audio)
    if not sr:
        return "unknown", "wav 头不可解析"
    if sr == exp_sr:
        return "ok", f"{exp}@{sr}Hz"
    got = engines_of_sample_rate(sr, table)
    if not got:
        # 不在表内的采样率可能是 hub 侧重采样产物，不足以指认换引擎
        return "unknown", f"{sr}Hz 不在指纹表（期望 {exp}@{exp_sr}Hz）"
    if exp in got:
        # 撞名表里恰好含点名引擎（同采样率多引擎）→ 无法反证，放行
        return "unknown", f"{sr}Hz 撞名（{'/'.join(got)}），无法指认"
    return "mismatch", f"期望 {exp}@{exp_sr}Hz，实得 {'/'.join(got)}@{sr}Hz"


# ── hub 引擎目录（比采样率指纹更早、更准的同一事故信号）──────────────────────
# `GET /api/engines` 逐引擎带 ``available``——2026-08-22 事故当时实测
# ``index_tts2: available=false`` 而 ``fish_speech: available=true``，正是「点名
# 谁、实际谁」的**直接**证据。相对采样率指纹的三点优势：① 合成前就知道（不必先
# 烧一次 GPU 再拒发）；② 不依赖 wav（ogg 直出同样有效）；③ 给的是**根因**
# （目标引擎离线）而不是「听起来像另一个引擎」的推断。
# 它不取代指纹校验：目录是 hub 的自述，可能陈旧或与实际路由不一致；指纹是产物的
# 客观证据。两者互为补充——目录管「提前拦 + 报根因」，指纹管「最后一道正面反证」。
_ENGINE_DIR_TTL_SEC = 60.0


class EngineEntry(NamedTuple):
    """hub 引擎目录里的一条。

    ``service`` 是**服务名字空间**（目录的 ``backend`` 字段），与引擎展示名不同：
    展示名 ``index_tts2`` 的服务名是 ``index_tts``。唤醒/泊车类端点
    （``/api/services/ensure``、``/api/gpu/park``）一律吃服务名，拿展示名去调会
    静默无效——这正是本模块要替调用方记住的那件事。
    """

    available: bool
    sample_rate: int
    service: str


# {base_url: (取数时刻, {引擎名: EngineEntry})}
_engine_dir_cache: Dict[str, Tuple[float, Dict[str, EngineEntry]]] = {}
_engine_dir_lock = threading.Lock()


def _fetch_engine_directory(
    base_url: str, timeout: float,
) -> Dict[str, EngineEntry]:
    """拉 hub 引擎目录 → ``{归一化引擎名: EngineEntry}``。

    顺带收 ``capabilities.sample_rate``：内置指纹表只有实测过的三个引擎，其余
    （gptsovits/qwen3_tts/voxcpm2/cosyvoice/xtts…）此前**完全不受校验**——谁被
    顶包都查不出来。目录自报采样率让指纹表随 hub 一起长，新引擎零改代码即受保护。
    异常抛给调用方兜底（fail-open）。
    """
    url = str(base_url or "").rstrip("/") + "/api/engines"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8", "replace"))
    out: Dict[str, EngineEntry] = {}
    for item in (payload.get("engines") or []):
        if not isinstance(item, dict):
            continue
        # 目录的 name 是展示名（index_tts2），backend 才是配置里钉的那个名
        # （index_tts）——两者都登记，钉哪个写法都能命中。
        service = normalize_engine_name(item.get("backend"))
        names = {normalize_engine_name(item.get("name")), service} - {""}
        if not names:
            continue
        caps = item.get("capabilities")
        try:
            sr = int((caps or {}).get("sample_rate") or 0)
        except (TypeError, ValueError):
            sr = 0
        sr = sr if 4000 <= sr <= 192000 else 0
        avail = bool(item.get("available"))
        for name in names:
            prev = out.get(name)
            if prev is None:
                out[name] = EngineEntry(avail, sr, service)
            else:
                # 同一归一名多条 → 任一可用即算可用；采样率/服务名取先到的非空值
                out[name] = EngineEntry(prev.available or avail,
                                        prev.sample_rate or sr,
                                        prev.service or service)
    return out


def _engine_directory(
    base_url: Any, timeout: float, now: Optional[float] = None,
    *, cached_only: bool = False,
) -> Optional[Dict[str, EngineEntry]]:
    """带 60s TTL 进程缓存的目录读取；拉不到回 None（调用方一律 fail-open）。

    ``cached_only``＝只读缓存不发网络：给「在 event loop 里同步调用」的决策点用
    （合成前的格式选择），宁可少一次保护也不阻塞事件循环。
    """
    base = str(base_url or "").strip()
    if not base:
        return None
    ts = float(now if now is not None else time.time())
    with _engine_dir_lock:
        hit = _engine_dir_cache.get(base)
        if hit and ts - hit[0] < _ENGINE_DIR_TTL_SEC:
            return hit[1]
    if cached_only:
        return None
    try:
        table = _fetch_engine_directory(base, timeout)
    except Exception:
        return None  # 目录不可达 ≠ 引擎不可用，绝不据此拒发
    with _engine_dir_lock:
        _engine_dir_cache[base] = (ts, table)
    return table


def merged_rate_table(
    base_url: Any = None, *, timeout: float = 3.0, cached_only: bool = False,
) -> Dict[str, int]:
    """指纹表＝内置实测值 + hub 目录自报值（**内置优先**）。

    内置的三个是我们真机量过的；目录是 hub 自述，可能与实际产物有出入，故只用来
    **填补空缺**，不覆盖已实测的键。目录拉不到就退化成内置表（行为不变）。
    """
    table = dict(ENGINE_SAMPLE_RATES)
    directory = _engine_directory(base_url, timeout, cached_only=cached_only) or {}
    for name, entry in directory.items():
        if entry.sample_rate and name not in table:
            table[name] = entry.sample_rate
    return table


def hub_engine_available(
    base_url: Any, engine: Any, *, timeout: float = 3.0,
    now: Optional[float] = None,
) -> Optional[bool]:
    """点名的引擎在 hub 目录里是否可用：``True/False/None``（None＝判不了）。

    **三态是刻意的**：目录拉不到 / 该引擎压根不在目录 / 没钉引擎 → None，调用方
    一律放行（fail-open）。只有目录明确说 ``available:false`` 才返 False——那是
    「hub 会拿别人顶包」的确定性前兆。60s TTL 进程缓存，热路每分钟至多一次 GET。
    """
    eng = normalize_engine_name(engine)
    if not eng:
        return None
    table = _engine_directory(base_url, timeout, now)
    if table is None or eng not in table:
        return None  # 拉不到 / 未登记的引擎名（hub 版本差异）→ 不构成指认
    return bool(table[eng].available)


def hub_engine_directory_status(
    base_url: Any, engine: Any, *, timeout: float = 3.0,
) -> Dict[str, Any]:
    """给看板/自检用的目录快照：点名引擎的可用性 + 全表可用数。

    ``state`` ∈ ``ok``（目录说可用）/ ``offline``（目录说不可用＝会被顶包）/
    ``unlisted``（目录里没这个名字）/ ``unknown``（目录拉不到，判不了）。
    """
    eng = normalize_engine_name(engine)
    table = _engine_directory(base_url, timeout)
    if table is None:
        return {"engine": eng, "state": "unknown"}
    entry = table.get(eng)
    if entry is None:
        state = "unlisted" if eng else "unknown"
    else:
        state = "ok" if entry.available else "offline"
    out = {
        "engine": eng,
        "state": state,
        "service": entry.service if entry else "",
        "sample_rate": int(entry.sample_rate) if entry else 0,
        "available_engines": sorted(n for n, e in table.items() if e.available),
        "offline_engines": sorted(n for n, e in table.items() if not e.available),
    }
    # 「在岗」不等于「答得上来」（2026-08-22 实弹）：index_tts 目录 available:true、
    # 进程 /health 200，但 176 显存 97% 满 + util 100%（singing 5.7G + 10G 无主进程
    # + 两个 ollama 模型同卡），短句实测 **36~69s**，而生产预算 timeout_sec=30 ⇒
    # 每一发都超时回落，客户照样听到别人的声音。只亮绿灯不带这行，看板会**如实地
    # 说谎**：所有读数都对，结论全错。故把宿主压力钉在同一行——绿灯旁边写着
    # 「宿主吃紧」才是可行动的真相。
    out.update(_engine_host_pressure(base_url, out["service"] or eng))
    return out


_overview_warming: set = set()


def _warm_gpu_overview_bg(base: str) -> None:
    """后台预热 GPU 概览缓存。同一 base 只允许一个在飞（看板轮询不该叠出线程堆）。"""
    if not base:
        return
    with _engine_dir_lock:
        if base in _overview_warming:
            return
        _overview_warming.add(base)

    def _run() -> None:
        try:
            payload = _fetch_gpu_overview(base, 8.0)
            with _engine_dir_lock:
                _gpu_overview_cache[base] = (time.time(), payload)
        except Exception:
            logger.debug("GPU 概览预热失败（已忽略）", exc_info=True)
        finally:
            with _engine_dir_lock:
                _overview_warming.discard(base)

    try:
        threading.Thread(target=_run, name="hub-gpu-warm", daemon=True).start()
    except Exception:
        with _engine_dir_lock:
            _overview_warming.discard(base)


def _engine_host_pressure(base_url: Any, service: Any) -> Dict[str, Any]:
    """点名服务所在主机的显存/算力压力。拉不到一律回空壳（看板绝不因此变慢或报错）。"""
    name = str(service or "").strip()
    blank: Dict[str, Any] = {"host": "", "pressure": "", "host_free_mb": 0}
    if not name:
        return blank
    base = str(base_url or "").strip()
    ts = time.time()
    with _engine_dir_lock:
        hit = _gpu_overview_cache.get(base)
    # **陈旧缓存照用**（容忍到 10min）而不是重新拉：显存压力是慢变量，「176 吃紧」
    # 五分钟前成立现在几乎一定还成立，而一次同步拉取要 1.5~4s（端点现场采样 GPU +
    # 轮询各 remote）——为一个提示行让看板卡几秒是坏交易。
    # ⚠ 这里刻意**不用 3s 短超时**：`hub_vram_blockers` 的注释已经写明 3s 实测偶发
    # 超时，我第一版还是照写了 3s，结果 host/pressure 恒空——「怕慢」把整个字段做
    # 没了，比慢一次糟得多。只有缓存全冷时才付一次 8s，此后永远走缓存。
    payload = hit[1] if hit and ts - hit[0] < _PRESSURE_STALE_OK_SEC else None
    age = int(ts - hit[0]) if hit else 0
    if payload is None:
        # 缓存全冷 → **后台预热、本次不等**：本函数挂在 avatar-status 上（ops 看板轮询
        # 的端点），同步拉一次实测 8.2s ⇒ 进程起来后第一次开看板要卡 8 秒。轮询页面
        # 「下一轮出现」完全够用，而「页面卡住」是会被记恨的那种慢。
        _warm_gpu_overview_bg(base)
        return blank
    if not isinstance(payload, dict):
        return blank
    nodes = [payload.get("local")] + list(payload.get("remotes") or [])
    for node in nodes:
        if not isinstance(node, dict):
            continue
        if not any(isinstance(s, dict) and str(s.get("name") or "") == name
                   for s in (node.get("services") or [])):
            continue
        try:
            used = int(node.get("mem_used_mb") or 0)
            total = int(node.get("mem_total_mb") or 0)
        except (TypeError, ValueError):
            used = total = 0
        return {
            "host": str(node.get("ip") or ""),
            "pressure": str(node.get("pressure") or ""),
            "host_free_mb": max(0, total - used) if total else 0,
            # 读数多老要如实说：陈旧缓存是刻意的取舍，但消费方有权知道自己在看几分钟
            # 前的快照（否则「刚才还好好的」这类争论没法收）
            "pressure_age_s": max(0, age),
        }
    return blank


def hub_engine_service_name(
    base_url: Any, engine: Any, *, timeout: float = 3.0,
) -> str:
    """点名引擎 → hub **服务名**（唤醒/泊车端点吃的那个名字）。查不到回原名。"""
    eng = normalize_engine_name(engine)
    if not eng:
        return ""
    table = _engine_directory(base_url, timeout) or {}
    entry = table.get(eng)
    return (entry.service if entry and entry.service else eng)


# ── 显存占用归因 & 按需唤醒 ────────────────────────────────────────────────────
# 「引擎为什么离线」几乎总是同一个答案：**显存被别人占着**。hub 的 idle_park 会把
# 闲置引擎泊车省显存，于是「唱歌工作室在跑 → 质量轨 TTS 被挤下线 → 客户听到别人
# 的声音」这条因果链在告警里必须**指名道姓**，否则运维只知道「index_tts 离线了」，
# 还得自己去翻 GPU 面板才知道该让谁让路。
_GPU_OVERVIEW_TTL_SEC = 30.0
# 看板「宿主压力」提示行容忍多陈旧的读数（见 `_engine_host_pressure`）
_PRESSURE_STALE_OK_SEC = 600.0
_gpu_overview_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}


def _fetch_gpu_overview(base_url: str, timeout: float) -> Dict[str, Any]:
    url = str(base_url or "").rstrip("/") + "/api/gpu/overview"
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def hub_vram_blockers(
    base_url: Any, *, timeout: float = 8.0, top: int = 3,
    now: Optional[float] = None, exclude: Any = None,
) -> Dict[str, Any]:
    """「谁占着显存、谁能让路」——给引擎离线告警补根因。

    ``exclude``＝**我们正要救的那个服务名**，绝不列为让路候选。它不是洁癖：
    2026-08-22 修掉「历史峰值冒充当前占用」后，176 的候选表只剩 ``index_tts``
    自己（8.5G）——而它就是要救的质量轨引擎，「泊掉它腾显存」正是我们要消灭的
    那次故障。此前这条自指建议被 ``singing`` 的 5.7G 幻影盖住了，幻影一除就露出来。

    每项候选带三档证据强度（消费方据此选限定词，别把它们混成裸数字）：
    实测驻留 ⇒ ``est=False``；remote 的标称预算 ⇒ ``est=True``；
    「在线但 hub 归不到进程」⇒ ``unattributed=True``（额度是同台无主池里估的
    上限，值得先试泊车但不是承诺）。

    ⚠ 超时刻意给到 8s（不是别处那个 3s）：本端点**现场采样** GPU 并轮询各 remote
    主机（实测 ``sample_ms≈1500`` 且随机器数增长），3s 下已实测偶发超时——而
    fail-open 会让告警**恰好在显存最紧张时**丢掉根因行，正是最需要它的时候。
    调用点是告警路径（至多 4h 一次、看门狗线程内），不在热路，慢一点无所谓。

    只列 ``parkable`` 且在线的服务（``core`` 服务不该被要求让路，列出来只会误导
    运维去停关键链路）。跨机聚合：hub 本机 + 各 remote 一并看，因为质量轨引擎可能
    落在任一台。任何异常/字段缺失一律返回空壳——**告警绝不能因为补充信息拉不到
    而发不出去**。

    每台主机三个口径分开报，因为**处置手段完全不同**，混一起等于没说：
    ``parkable``（泊车即让路，一条 HTTP）、``llm_mb``＝常驻大模型（ollama
    keep_alive 驻留，hub 自己会驱逐让路）、``extras_mb``＝无主进程（谁也管不了，
    只能上机排查）。176 实测的分布正是「可让路 0 / 大模型 9.8G / 无主 8.6G」——
    少了中间那笔，账面对不上已用量，运维只会怀疑读数坏了；而它恰恰是唯一还能
    自动腾出来的一笔。
    """
    base = str(base_url or "").strip()
    out: Dict[str, Any] = {"hosts": []}
    if not base:
        return out
    skip = str(exclude or "").strip()
    ts = float(now if now is not None else time.time())
    with _engine_dir_lock:
        hit = _gpu_overview_cache.get(base)
        payload = hit[1] if hit and ts - hit[0] < _GPU_OVERVIEW_TTL_SEC else None
    if payload is None:
        try:
            payload = _fetch_gpu_overview(base, timeout)
        except Exception:
            return out
        with _engine_dir_lock:
            _gpu_overview_cache[base] = (ts, payload)

    def _mb(svc: Dict[str, Any]) -> Tuple[int, bool]:
        """统一成 MB，并回「这个数是实测当前占用还是估算」。

        ⚠ 字段优先级是本函数的**全部要点**，写反了会让告警点名无辜服务（2026-08-22
        实弹踩到，首版就是写反的）。三个字段语义完全不同：

        - ``mem_mb``＝**当前实测**驻留（hub 现场采样 GPU 得来，本机节点才有）
        - ``vram_seen_mb``＝**历史峰值**水位（该服务曾经吃到多少，本机节点才有）
        - ``vram_gb``＝**标称**预算（remote 节点只给这个，非实测）

        首版按 ``vram_seen_mb`` 优先，于是 176 上 ``singing``（当时 ``mem_mb=0``，
        ``vram_seen_mb=5763``）被报成「泊掉可让出 5.7G」，把峰值当现况承诺出去。
        故：实测优先；只有实测缺席（remote 节点）才退标称，且**如实标记 est**，
        让消费方能说「约」而不是把估算当实测报。

        ⚠ 但「实测 0 ⇒ 让不出东西」这个反向推论**同样是错的**，判它要额外的证据，
        见 ``_host`` 里 ``unattributed`` 那一段（hub 归不到进程时 0 只代表「不知道」）。
        """
        try:
            cur = svc.get("mem_mb")
            if cur is not None:
                return max(0, int(cur)), True
        except (TypeError, ValueError):
            pass
        # 无实测：remote 只给标称 GB；本机异常态退历史峰值（两者都是估算）
        for key, scale in (("vram_gb", 1024.0), ("vram_seen_mb", 1.0)):
            try:
                val = int(float(svc.get(key) or 0) * scale)
            except (TypeError, ValueError):
                continue
            if val > 0:
                return val, False
        return 0, False

    def _host(node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not isinstance(node, dict):
            return None
        # 无主显存（``extras`` 里非 desktop 的进程）：**不可泊车、账上也看不见**，
        # 但 2026-08-22 实弹里它就是最大一块——176 两个孤儿 python.exe 合计 8.6G，
        # 而 parkable 排掉自指后一个都不剩。少了这一行，运维会以为这台没救；
        # 有了它，下一步是「去 176 找那两个进程」。先算它是因为下面判「实测 0 的
        # 服务算不算候选」要用到它（见 `orphan` 的用法）。
        extras = 0
        for ex in (node.get("extras") or []):
            if not isinstance(ex, dict) or ex.get("service") == "desktop":
                continue  # 桌面/系统合计是常驻开销，不是谁忘了关的脚本
            try:
                extras += max(0, int(ex.get("mem_mb") or 0))
            except (TypeError, ValueError):
                continue
        # 其中**归不到任何服务**的那部分（hub 自己标 ``service: null`` /
        # ``off_roster``）。它存在就意味着「本机的 服务→进程 映射这一轮不可信」，
        # 下面据此给实测 0 的在线服务留一条疑似通道。
        orphan = 0
        for ex in (node.get("extras") or []):
            if not isinstance(ex, dict) or ex.get("service") == "desktop":
                continue
            if ex.get("service") or not ex.get("off_roster"):
                continue
            try:
                orphan += max(0, int(ex.get("mem_mb") or 0))
            except (TypeError, ValueError):
                continue

        parkable: List[Dict[str, Any]] = []
        for svc in (node.get("services") or []):
            if not isinstance(svc, dict) or not svc.get("parkable"):
                continue
            if not svc.get("online") or svc.get("parked"):
                continue
            if skip and str(svc.get("name") or "") == skip:
                continue    # 要救的就是它，别建议泊掉（见 docstring）
            mem, measured = _mb(svc)
            unattributed = False
            if measured and mem <= 0:
                # 实测 0 有**两种**完全不同的成因，混成一句就必然骗人（本函数两版
                # 都栽在这里，方向相反）：
                #   a) 真的没驻留（idle_park 卸了权重、进程空挂）→ 泊掉让出 0，
                #      列出来运维白跑一趟且把账算到无辜服务头上；
                #   b) hub **没能把进程映射到这个服务**（开发期手工起的进程不在
                #      roster 里，健康探针通所以 online=true，但采样归不到它）
                #      → 泊掉很可能真让出东西，说「无可让路」是自信的假否定。
                # 判据就是同台有没有无主显存：有 ⇒ 映射本轮已被证明不可靠，把它当
                # **疑似**候选留下（额度取 峰值 与 无主池 的较小者——不能声称多于
                # 池子里实际有的），并标 est/unattributed 让文案说「疑似」。
                # 176 实测正是 b：singing/fish_tts 各报 0，而两个孤儿 python.exe
                # 合计 8.6G、峰值账本里正好是这两位（5763/4534）。
                peak = 0
                try:
                    peak = max(0, int(svc.get("vram_seen_mb") or 0))
                except (TypeError, ValueError):
                    peak = 0
                if orphan <= 0 or peak <= 0:
                    continue
                mem, measured, unattributed = min(peak, orphan), False, True
            parkable.append({
                "name": str(svc.get("name") or ""),
                "label": str(svc.get("label") or ""),
                "mem_mb": mem,
                "busy": bool(svc.get("busy")),
                "est": not measured,
                "unattributed": unattributed,
            })
        parkable.sort(key=lambda s: s["mem_mb"], reverse=True)
        try:
            used = int(node.get("mem_used_mb") or 0)
            total = int(node.get("mem_total_mb") or 0)
        except (TypeError, ValueError):
            used = total = 0
        # 常驻大模型（ollama keep_alive 驻留的那些）：既不在 ``services`` 里、也不在
        # ``extras`` 里，于是**整块从账上消失**。176 实测这是最大一笔——9.8G（翻译
        # hy-mt2 4.7G + 视觉 qwen3-vl 5.1G），而同台可让路的服务排掉自己后是 0。
        # 少了这一行，账面「引擎 8.5G + 无主 8.6G」对不上「已用 30.9G / 32.6G」，
        # 运维只能怀疑读数有问题；有了它，缺口闭合且指向一个**真能动**的杠杆：
        # hub 自己就会驱逐 LAN 大模型让路（park_stats 里 remote_llm_evict /
        # lan_keepwarm_yield 都是既有计数），不必上机杀进程。
        llm = 0
        for m in ((node.get("ollama") or {}).get("models") or []):
            if not isinstance(m, dict):
                continue
            try:
                llm += max(0, int(float(m.get("vram_gb") or 0) * 1024))
            except (TypeError, ValueError):
                continue
        return {
            "ip": str(node.get("ip") or ""),
            "free_mb": max(0, total - used) if total else 0,
            "pressure": str(node.get("pressure") or ""),
            "parkable": parkable[:max(1, top)],
            "extras_mb": extras,
            "llm_mb": llm,
        }

    hosts = [_host(payload.get("local"))]
    hosts += [_host(r) for r in (payload.get("remotes") or [])]
    # 留「有可让路服务」**或**「有无主显存/常驻大模型」的主机：后两者没有可泊项却
    # 正是根因所在，按旧口径（只看 parkable）会被整台滤掉——排掉自指候选后 176 恰好
    # 就是这种形态，漏了它等于在最需要归因的那台上什么都不说。
    out["hosts"] = [h for h in hosts
                    if h and (h["parkable"] or h["extras_mb"] or h["llm_mb"])]
    return out


def hub_engine_wake(
    base_url: Any, engine: Any, *, timeout: float = 12.0,
) -> Tuple[bool, str]:
    """请 hub 把点名的引擎拉起来（``POST /api/services/ensure``）。

    只请求「把我钉的这个拉起来」，**绝不代运维决定谁让路**（泊车别人属跨条线的
    显存策略，且 hub 自有 lease/idle_park 机制）。显存不够时 hub 自己会拒。

    ⚠ **超时＝已派发，不是失败**（2026-08-22 实弹踩到）：``wait_s=0`` 并不保证
    立刻返回——实测 5s 超时下 POST 抛 ``TimeoutError``，而引擎在 **35s 后真的上岗了**
    （176 空闲显存 5.9G→1.0G＝模型确实载入）。照「失败」上报的话，告警会把人从床上
    叫起来修一个**已经修好**的问题，一两次之后这个字段就没人信了。
    异常分两类刚好对应两种事实：裸 ``TimeoutError`` 是 http.client 在**等响应**时抛的
    ⇒ TCP 已连上、请求已送达、hub 正在载 ⇒ 已派发；``URLError`` 族是连都没连上
    ⇒ 真失败。就绪与否**不由本函数裁决**，由下一轮目录巡检出恢复通知（那是确定读数）。

    返回 ``(是否已派发, 说明)``。
    """
    base = str(base_url or "").strip()
    service = hub_engine_service_name(base, engine, timeout=min(timeout, 3.0))
    if not base or not service:
        return False, "no_target"
    url = (base.rstrip("/") + "/api/services/ensure?"
           + urllib.parse.urlencode({"name": service, "wait_s": 0}))
    try:
        req = urllib.request.Request(url, data=b"", method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except TimeoutError:
        # 读超时：请求已经进了 hub，它正在把引擎载起来（见上方 docstring）
        return True, "dispatched"
    except Exception as exc:  # 唤醒是尽力而为，失败绝不外溢
        return False, f"{type(exc).__name__}"
    if not isinstance(body, dict):
        return False, "bad_response"
    if body.get("ok") is False:
        return False, str(body.get("error") or body.get("detail") or "refused")
    return True, str(body.get("state") or body.get("status") or "accepted")


_WAKE_COOLDOWN_SEC = 90.0
_wake_last: Dict[str, float] = {}


def hub_engine_wake_bg(
    base_url: Any, engine: Any, *, cooldown_sec: float = _WAKE_COOLDOWN_SEC,
    now: Optional[float] = None,
) -> bool:
    """**需求驱动**唤醒：合成路径撞见引擎离线时，后台捎一句「把它拉起来」。

    为什么要有它而不只靠看门狗：看门狗按告警节奏动手（宽限 20min + 重提 4h），
    而 hub 的 ``idle_park`` 泊车是**按需换回**的设计——真正的需求信号就是「有人
    现在要合成」。只靠看门狗意味着客户侧降级窗口最坏 20 分钟；挂在这里，降级窗口
    收缩到一次冷载（IndexTTS-2 实测 ~18s），也就是**下一条消息**。

    三条硬约束（缺一条就会从修复变成事故源）：
      1. **绝不阻塞本条**：起 daemon 线程即走。本条仍按既有策略处置（strict 拒发 /
         lenient 顶包并计数）——拿当前这条消息去换下一条的音色是坏交易。
      2. **进程级节流**（默认 90s/引擎）：爆发式来消息时不能变成 POST 洪水；冷载
         本就 ~18s，节流窗盖住它即可。
      3. **静默**：唤醒失败与本条的成败无关，绝不外溢异常。

    返回是否**真的派发了**一次（被节流/无目标 → False），便于日志与计数。
    """
    base = str(base_url or "").strip()
    eng = normalize_engine_name(engine)
    if not base or not eng:
        return False
    key = base + "|" + eng
    ts = float(now if now is not None else time.time())
    with _engine_dir_lock:
        if ts - _wake_last.get(key, 0.0) < max(1.0, cooldown_sec):
            return False
        _wake_last[key] = ts

    def _run() -> None:
        try:
            ok, detail = hub_engine_wake(base, eng)
            logger.info("[tts] 引擎离线 → 已后台请求唤醒 %s 结果=%s/%s",
                        eng, "ok" if ok else "fail", detail)
        except Exception:
            logger.debug("引擎后台唤醒异常（已忽略）", exc_info=True)

    try:
        threading.Thread(target=_run, name="hub-engine-wake", daemon=True).start()
    except Exception:
        logger.debug("引擎后台唤醒线程创建失败（已忽略）", exc_info=True)
        return False
    return True


# ── hub 合成「超时熔断」（2026-08-22 宿主吃紧实测）──────────────────────────
# 目录说在岗、引擎 /health 200 且 model_loaded:true，但同卡显存 97% 满 → 8 字短句
# 实测 36~69s，而生产预算 timeout_sec=30 ⇒ **每一发都超时**。此时既有的两道闸都
# 不响：目录预检看的是 available（真的 true），指纹闸要先拿到音频（永远拿不到）。
# 结果就是每条消息白等 30 秒、然后 strict 拒发 / lenient 顶包——客户先等半分钟，
# 再听到别人的声音，或者干脆没有语音。
#
# 熔断只认**实测证据**，不做「显存少所以大概会慢」这类推断（推断会在引擎其实还能
# 答的时候误杀语音）：连续 N 次超时 → 开路一段时间，开路期直接跳过 hub（strict 的
# 结果与超时后完全相同，只是**立刻**给出文字而非让客户干等 30s）；窗口到点放一发
# 探路，成了就闭合。任何一次成功清零。
_BREAKER_FAILS_TO_OPEN = 2
_BREAKER_OPEN_SEC = 300.0
# (streak, opened_ts, half_open_inflight)
_breaker: Dict[str, Tuple[int, float, bool]] = {}


def _breaker_key(base_url: Any, engine: Any) -> str:
    base = str(base_url or "").strip()
    eng = normalize_engine_name(engine)
    return (base + "|" + eng) if base else ""


def _starvation_confirmed(base_url: Any, engine: Any) -> bool:
    """该引擎所在宿主**此刻已被实测为显存吃紧**（``pressure == "high"``）。

    用途只有一个：把「首次超时」从疑点升格为定案，让熔断少等一个客户（见
    ``note_hub_synth_outcome``）。这不违反「只认实测证据」——压力读数与超时都是
    量出来的，两者合起来正是事故当天的完整因果（同卡 97% 满 → 8 字要 36~69s →
    预算 30s 必超时），而不是「显存少所以大概会慢」那种推断。

    **全程零阻塞、零网络**：目录走 ``cached_only``，压力只在缓存里**已经有**这台
    hub 的快照时才读（连后台预热都不触发——那会让一条已经失败的合成路径顺手发起
    一次 HTTP）。缓存全冷就照旧走两振规则：ops 看板本就在轮询 avatar-status 持续
    预热这份缓存，真正吃紧的时段它一定是热的，为冷启动那几分钟去抢网络不值得。
    """
    base = str(base_url or "").strip()
    if not base:
        return False
    with _engine_dir_lock:
        if base not in _gpu_overview_cache:
            return False
    try:
        eng = normalize_engine_name(engine)
        table = _engine_directory(base, 0.0, cached_only=True) or {}
        entry = table.get(eng)
        service = (entry.service if entry and entry.service else eng)
        return _engine_host_pressure(base, service).get("pressure") == "high"
    except Exception:
        # 快速开路是**优化**不是保护：判断本身出问题就退回两振规则，绝不让它
        # 反过来影响熔断的正确性。
        logger.debug("显存吃紧判定失败（退回两振规则）", exc_info=True)
        return False


def note_hub_synth_outcome(
    base_url: Any, engine: Any, *, timed_out: bool, now: Optional[float] = None,
) -> None:
    """登记一次 hub 合成的**时间**结局。``timed_out=False`` 即成功/快败，一律清零。

    刻意只喂「超时」：HTTP 4xx/5xx 是快败（几百毫秒，没有延迟税可省），引擎冒名
    另有指纹闸处置。把它们混进来会让熔断在「hub 其实答得很快只是拒绝了」时也开路。
    """
    key = _breaker_key(base_url, engine)
    if not key:
        return
    ts = float(now if now is not None else time.time())
    # 显存判定必须在**取锁之前**：`_engine_host_pressure` 自己要拿同一把
    # `_engine_dir_lock`，而那是普通 Lock 不是 RLock ⇒ 放进临界区就是死锁。
    fast = _starvation_confirmed(base_url, engine) if timed_out else False
    with _engine_dir_lock:
        streak, opened, _ = _breaker.get(key, (0, 0.0, False))
        if not timed_out:
            if streak or opened:
                logger.info("[tts] hub 合成恢复正常 → 熔断闭合 %s", key)
            _breaker.pop(key, None)
            return
        streak += 1
        # 宿主已实测吃紧 → 首次超时即开路：两振规则要让**两个**客户各白等一轮
        # 预算（实测 30s）才开始保护，而在「同卡 97% 满」这种已确诊的场景里，
        # 第二发的结局没有任何不确定性，等它只是多赔一个客户。
        if streak >= _BREAKER_FAILS_TO_OPEN or fast:
            _breaker[key] = (streak, ts, False)
            logger.warning(
                "[tts] hub 合成超时 %d 次%s → 熔断开路 %.0fs（%s）：开路期直接"
                "跳过 hub，客户立刻拿到文字而不是白等一轮", streak,
                "（宿主显存已实测吃紧，不等第二振）" if fast and
                streak < _BREAKER_FAILS_TO_OPEN else "",
                _BREAKER_OPEN_SEC, key)
        else:
            _breaker[key] = (streak, 0.0, False)


def hub_synth_breaker_open(
    base_url: Any, engine: Any, *, now: Optional[float] = None,
) -> bool:
    """是否应跳过本次 hub 合成。半开时**放一发探路**（返回 False）。

    半开期只放一发：并发消息不该同时去撞一个正在恢复的引擎（那样窗口一到就是
    N 发一起超时，白付 N×30s）。探路那一发的结局经 ``note_hub_synth_outcome``
    回来后自然闭合或重新开路。
    """
    key = _breaker_key(base_url, engine)
    if not key:
        return False
    ts = float(now if now is not None else time.time())
    with _engine_dir_lock:
        entry = _breaker.get(key)
        if not entry:
            return False
        streak, opened, inflight = entry
        if not opened:
            return False
        if ts - opened < _BREAKER_OPEN_SEC:
            return True
        # 窗口到点：首个到达者拿走探路名额，其余继续被挡
        if inflight:
            return True
        _breaker[key] = (streak, opened, True)
        logger.info("[tts] hub 合成熔断半开 → 放一发探路（%s）", key)
        return False


def hub_synth_breaker_state(
    base_url: Any, engine: Any, *, now: Optional[float] = None,
) -> Dict[str, Any]:
    """只读快照（看板/告警用）：closed | tripping | open | half_open。

    键名刻意叫 ``breaker`` 而不是 ``state``——消费方（avatar-status 的 engine_dir）
    已经有一个 ``state``（在岗/离线），撞名会让「熔断闭合」把「引擎在岗」覆盖掉，
    整行渲染直接哑掉。两个正交事实各占一个键。
    """
    key = _breaker_key(base_url, engine)
    blank = {"breaker": "closed", "timeout_streak": 0, "reopen_in_s": 0}
    if not key:
        return blank
    ts = float(now if now is not None else time.time())
    with _engine_dir_lock:
        entry = _breaker.get(key)
    if not entry:
        return blank
    streak, opened, inflight = entry
    if not opened:
        return {"breaker": "tripping", "timeout_streak": streak,
                "reopen_in_s": 0}
    left = _BREAKER_OPEN_SEC - (ts - opened)
    if left > 0:
        return {"breaker": "open", "timeout_streak": streak,
                "reopen_in_s": int(left)}
    return {"breaker": "half_open", "timeout_streak": streak, "reopen_in_s": 0,
            "probe_inflight": bool(inflight)}


def parse_tts_only_response(body: bytes) -> Tuple[bytes, str]:
    """解析 hub ``/api/tts_only`` 响应 → (音频字节, 实际格式)。失败/空/ok=false → 抛 RuntimeError。

    格式判定：先取响应 JSON 的 ``format`` 字段（缺省 wav），再按魔数双校验
    （``sniff_audio_format``）——字段与魔数不符时以魔数为准（兼容 Hub ffmpeg
    异常静默回退 wav 的场景）。
    """
    if not body:
        raise RuntimeError("hub_fish: empty response")
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as ex:
        raise RuntimeError(f"hub_fish: bad json ({ex})")
    if not isinstance(data, dict):
        raise RuntimeError("hub_fish: unexpected response shape")
    if not data.get("ok"):
        raise RuntimeError(f"hub_fish: {str(data.get('detail') or 'synthesis failed')[:200]}")
    b64 = data.get("audio_base64") or ""
    if not b64:
        raise RuntimeError("hub_fish: no audio in response")
    audio = base64.b64decode(b64)
    claimed = str(data.get("format") or "wav").strip().lower() or "wav"
    return audio, sniff_audio_format(audio, default=claimed)


def hub_fish_synthesize(
    base_url: str, profile: str, text: str, *, language: str = "",
    emotion: str = "", best_of: int = 1, timeout_sec: float = 30.0,
    audio_format: str = "", tts_engine: str = "",
) -> Tuple[bytes, str]:
    """调用幻声 hub ``/api/tts_only`` 合成一句 → (音频字节, 实际格式)（同步；供 to_thread 包裹）。

    仅 HTTP，不加载任何本地模型（GPU 在 .176）。异常直抛，调用方回落本地克隆。
    ``audio_format="ogg"`` 请求 hub 直出 opus 48k；实际格式以响应为准（可能回退 wav）。
    """
    url = str(base_url or "").rstrip("/") + "/api/tts_only"
    payload = build_tts_only_payload(
        profile, text, language=language, emotion=emotion, best_of=best_of,
        audio_format=audio_format, tts_engine=tts_engine)
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:  # nosec B310
        return parse_tts_only_response(resp.read())


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
#: STT 令牌的历史内置默认路径（117 开发机的私有路径）。#161（2026-09-03 钧机
#: 报告 22:54）：客户机上这个路径根本不存在 → AvatarHub STT 二级回落因令牌缺失
#: 静默失败，「转录判幻觉丢弃」之后就没有第二次机会。它现在只作为**本机兜底**
#: 参与解析，且排在网关令牌/显式配置之后——客户机走网关设备令牌，不再依赖它。
DEV_STT_TOKEN_FILE = "D:/faceX/mfys/secrets/service_token.txt"


def resolve_service_token(token_file: str = "") -> str:
    """解析跨机服务令牌（X-AH-Svc）：显式配置 → 网关设备令牌 → 开发机兜底路径。

    #161：托管客户机没有集群令牌文件，此前 STT 二级回落必然缺令牌而失败——
    而设备令牌（``AITR_HOSTED_AI_KEY``，hosted_gateway 注入，官网网关同头验它）
    在同一进程里明明是有的，只是 STT 腿没接。三段优先级：

    1. ``token_file`` 显式配置且读得到（LAN 算力机部署的既有行为，零变化）；
    2. 环境里的设备令牌 ``cx.*``（托管桌面版；调用时取值＝换新不失效）；
    3. 开发机内置路径——**仅当未显式配置时**。显式配了却读不到＝运营指定的
       令牌源出了问题，此时偷偷改用另一台机器的令牌只会把配置错误掩盖成
       「时好时坏」；那种情况如实降级并告警（旧行为逐字保留）。

    全部落空 → ""（调用方按 STT 不可用降级，不抛）。
    """
    configured = str(token_file or "").strip()
    tok = read_service_token(configured)
    if tok:
        return tok
    hosted = str(os.environ.get("AITR_HOSTED_AI_KEY") or "").strip()
    if hosted.startswith("cx."):
        return hosted
    if not configured:
        return read_service_token(DEV_STT_TOKEN_FILE)
    return ""


def warn_token_missing_once(scope: str, token_file: str = "") -> None:
    """「令牌不可用」按用途只告警一次（#161：缺令牌是稳态，不该每条语音刷屏）。

    首次 WARNING 带上下一步动作（网关下发/配置 token_file），后续同用途降 DEBUG。
    **绝不打印令牌本身或其片段**。
    """
    if scope in _TOKEN_WARNED:
        logger.debug("[avatar_voice] %s 令牌仍不可用（已告警过，不重复）", scope)
        return
    _TOKEN_WARNED.add(scope)
    logger.warning(
        "[avatar_voice] %s 令牌不可用（配置 token_file=%s 读不到，且环境无网关设备"
        "令牌）→ 该能力本次降级；托管部署请确认官网网关已下发设备令牌，自建部署请"
        "配置 avatar_voice.stt.token_file。本进程同类告警只出这一次。",
        scope, str(token_file or "") or "-")


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
        # #161：不再把开发机路径当内置默认——客户机上它恒不存在，却让所有令牌
        # 解析看起来「配过了」。空串＝未配置，由 resolve_service_token 走网关
        # 设备令牌 → 开发机兜底路径两级回落（本机部署行为不变）。
        self.stt_token_file: str = str(stt.get("token_file") or "")
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
        """克隆端点健康明细 {reachable, models_loaded}。best-effort，绝不抛。

        两种上游健康形状都认（2026-08-29 智聊克隆主力迁 104:7865 IndexTTS-2）：
        mfys CosyVoice(7852)＝``{ok, models_loaded}``；IndexTTS-2(7865)＝
        ``{status:"ok", model_loaded}``。只认前者时 base_urls 直指 7865 会
        永远预检不过 → 全量静默回落 edge，与「端点其实活着」矛盾。
        """
        d = self._probe(f"{self.base_url}/health",
                        ok_keys=("ok", "models_loaded"),
                        alt_ok_keys=("status", "model_loaded"))
        if d.get("shape"):
            with _HEALTH_LOCK:
                _SHAPE_CACHE[self.base_url] = str(d["shape"])
        return d

    def qwen_health(self) -> Dict[str, Any]:
        """7858 健康明细 {reachable, models_loaded}。"""
        return self._probe(
            f"{self.qwen_base_url}/health", ok_keys=("status", "model_loaded"))

    def stt_health(self) -> Dict[str, Any]:
        """远端 STT(7854) 健康明细 {reachable, models_loaded}（/health 无需令牌）。"""
        return self._probe(
            f"{self.stt_base_url}/health", ok_keys=("ok", "loaded"))

    def _probe(
        self, url: str, *, ok_keys: Tuple[str, str],
        alt_ok_keys: Optional[Tuple[str, str]] = None,
    ) -> Dict[str, Any]:
        """``alt_ok_keys``：主形状的 flag 键不在响应里时改用的备选键对——
        克隆端点两家服务健康形状不同（7852 vs 7865），按响应实际带哪个键判。"""
        detail: Dict[str, Any] = {"reachable": False, "models_loaded": False}
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=self.health_timeout_sec) as r:
                body = r.read()
            detail["reachable"] = True
            data = json.loads(body.decode("utf-8"))
            if isinstance(data, dict):
                # 顺带记引擎形状（#58）：克隆探测消费；qwen/stt 探测带出来无害不消费
                detail["shape"] = health_shape_of(data)
                flag, loaded_key = ok_keys
                if alt_ok_keys and flag not in data and alt_ok_keys[0] in data:
                    flag, loaded_key = alt_ok_keys
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
        d = self._probe(f"{base}/health", ok_keys=("ok", "models_loaded"),
                        alt_ok_keys=("status", "model_loaded"))
        ok = bool(d["reachable"] and d["models_loaded"])
        with _HEALTH_LOCK:
            _HEALTH_CACHE[base] = (now + self.health_cache_sec, ok)
            if d.get("shape"):
                _SHAPE_CACHE[base] = str(d["shape"])
        return ok

    def health_ok(self, *, use_cache: bool = True) -> bool:
        """克隆链是否可用＝**任一**端点就绪（B1 多端点；单端点=旧语义）。"""
        return any(self._health_ok_base(b, use_cache=use_cache)
                   for b in self.base_urls)

    def marks_safe(self) -> bool:
        """副语言标记可否随文送出＝**全部**候选端点实证 CosyVoice 形状（#58）。

        IndexTTS-2 会把 ``[breath]`` 等标记当英文念出（104 实锤「PLAS」）；
        形状未知（还没探过/上游不带特征键）按不安全算——错剥只少一口气声，
        错送是当着客户念英文。只读缓存零 HTTP：形状由健康探测顺带落下
        （合成前必过 health_ok / _endpoint_candidates，节律 30s）。
        """
        if not self.base_urls:
            return False
        with _HEALTH_LOCK:
            return all(
                _SHAPE_CACHE.get(b, "") == "cosyvoice" for b in self.base_urls)

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

    def _svc_headers(self, base_url: str) -> Optional[Dict[str, str]]:
        """跨机 GPU 服务鉴权头（service_auth 语义：回环免鉴、LAN 需 X-AH-Svc）。

        2026-08-02 实锤：base_urls 里的 140/173 CosyVoice 节点一直 401——TTS 调用
        没带集群令牌（STT 早就带了），「多端点回落」从未对跨机端点真正生效。
        令牌文件复用 stt.token_file（集群通用令牌，140 实测同牌可合成）。
        令牌缺失时不带头：回环端点照常，LAN 端点继续 401→冷却切换＝旧行为零回退。
        """
        host = ""
        try:
            host = urllib.parse.urlsplit(base_url).hostname or ""
        except Exception:
            host = ""
        if host in ("127.0.0.1", "localhost", "::1"):
            return None
        # 托管桌面版（2026-08-03）：客户机没有集群令牌文件 → 改带设备令牌
        # （cx.…，hosted_gateway 注入的 env，调用时取值＝换新不失效）。官网网关
        # 验它放行（同一 X-AH-Svc 头）；LAN 节点令牌不匹配照旧 401→冷却切换，
        # 与「无令牌 401」等价，零回退。三级回落收在 resolve_service_token
        # （#161：STT 腿此前只读文件，同一份设备令牌它用不上）。
        token = resolve_service_token(self.stt_token_file)
        return {"X-AH-Svc": token} if token else None

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
                    f"{base}{path}", payload, timeout=timeout,
                    headers=self._svc_headers(base))
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
        # #58 兜底剥除：候选端点不全是 CosyVoice → 副语言标记进不了保真消费层，
        # 只会被当英文念出。注入层已按 marks_safe 不注，这里兜住 LLM 剧本残留/
        # 运营手工标注/混合端点池等一切来源（幂等，干净文本零开销）。
        if not self.marks_safe():
            from src.ai.voice_emotion import strip_paralinguistic_marks
            text = strip_paralinguistic_marks(text)
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
        if not self.marks_safe():
            from src.ai.voice_emotion import strip_paralinguistic_marks
            text = strip_paralinguistic_marks(text)
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
                timeout=self.synth_timeout_sec,
                headers=self._svc_headers(self.base_url))
            try:
                data = json.loads(body.decode("utf-8"))
                ok = not (isinstance(data, dict) and data.get("ok") is False)
            except Exception:
                ok = True
            if ok:
                _REGISTERED_SPK.add(fp)
            return ok
        except Exception as exc:
            code = getattr(exc, "code", None)
            if code in (401, 403) or "401" in str(exc) or "Unauthorized" in str(exc):
                # 该节点未配置 AvatarHub 服务令牌（D:/faceX/mfys/secrets/service_token.txt
                # 通常仅算力机有）→ 预热被拒是**预期态**，不是故障；在线合成/回落链不受影响。
                # 降级为 INFO，避免桌面/坐席机每次启动刷 WARNING（2026-08-07 日志监控实锤）。
                logger.info(
                    "[avatar_voice] register_spk 需鉴权令牌，本节点未配置，预热跳过（不影响合成）")
            elif code == 404 or "404" in str(exc):
                # 契约残留（2026-08-30 网关实锤 404×2）：/v1/tts/register_spk 是
                # CosyVoice(7852) 专属端点，IndexTTS-2(7865) 没有——克隆走 zero-shot
                # 本就无需预热，404 是**预期态**不是故障。记指纹止住本进程内重试，
                # INFO 一次即静默；绝不把它包装成「登记失败」冒给用户。
                _REGISTERED_SPK.add(fp)
                logger.info(
                    "[avatar_voice] 上游无 register_spk 端点（IndexTTS-2 零样本无需预热），跳过")
            else:
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
        token = resolve_service_token(self.stt_token_file)
        if not token:
            warn_token_missing_once("STT", self.stt_token_file)
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
        token = resolve_service_token(self.stt_token_file)
        if not token:
            warn_token_missing_once("translate", self.stt_token_file)
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


def convert_to_wav_mono(src_path: str) -> Optional[str]:
    """任意音频（ogg/opus/m4a/mp3…）→ 单声道 16bit WAV，**保留原采样率**。

    参考音登记用：与 STT 的 16k 版刻意分开——克隆参考音的高频细节直接决定
    音色相似度上限，不做降采样（Telegram 48k opus 语音条转出仍是 48k）。
    ffmpeg 缺失/失败 → None（调用方按「无法转换」降级，不抛）。
    """
    import shutil

    if shutil.which("ffmpeg") is None:
        return None
    src = Path(src_path)
    if not src.is_file():
        return None
    dst = src.parent / (src.stem + "_mono.wav")
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-ac", "1",
             "-acodec", "pcm_s16le", "-f", "wav", str(dst)],
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
