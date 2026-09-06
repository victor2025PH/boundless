"""工具箱音频链的「收件核查 + 错误分类」纯函数（M-5 D / #225，2026-09-06）。

5NXHUW 实录：工具箱→翻译工具→语音 上传后报「处理失败: 转录服务暂不可用」，日志
``audio_pipeline transcribe … ok=False dur=0.0s len=0 latency=16602ms err=APITimeoutError``，
而同一分钟工作台 ``voice_transcriber`` 客户语音 1 秒转录成功——服务可用，只有工具箱这条
链超时；提示又把超时一律译成「服务不可用」。两件事在这里收口：

1. :func:`probe_audio_file` —— 上传落盘后**核查**：文件在不在 / 有没有字节 / 容器头
   认不认得 / 能算出多长（WAV 读头；OGG Opus/Vorbis 读末页 granule；其余有 ffprobe
   就问 ffprobe，没有就只报字节数）。UI 据此回显「已收到 x 秒音频」，字节为零或头
   不对直接报「文件未正确接收」，不再等 ASR 超时后猜。
   注：``audio_pipeline`` 日志里的 ``dur=0.0s`` 对 openai 后端是**恒 0**（只有
   faster_whisper 回 duration），不是「文件未接收」的证据——这也是要在收件侧
   自己量一次的原因。
2. :func:`classify_asr_error` —— 转写失败原因 → 机器码 ``timeout / unavailable /
   rejected / format / upload_failed / no_speech / unknown``，路由按
   ``err.asr.<code>`` 取人话（超时说超时，拒绝说拒绝）。

零第三方依赖、零 IO 之外的副作用（ffprobe 是可选外呼，找不到就跳过）。
"""

from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
from typing import Any, Dict, Optional

# 收件核查结果 reason 码
INTAKE_MISSING = "missing"
INTAKE_EMPTY = "empty"
INTAKE_BAD_HEADER = "bad_header"

# 转写失败机器码（i18n 键 = err.asr.<code>）
ASR_TIMEOUT = "timeout"
ASR_UNAVAILABLE = "unavailable"
ASR_REJECTED = "rejected"
ASR_FORMAT = "format"
ASR_UPLOAD_FAILED = "upload_failed"
ASR_NO_SPEECH = "no_speech"
ASR_UNKNOWN = "unknown"
ASR_CODES = (ASR_TIMEOUT, ASR_UNAVAILABLE, ASR_REJECTED, ASR_FORMAT,
             ASR_UPLOAD_FAILED, ASR_NO_SPEECH, ASR_UNKNOWN)


def asr_error_i18n_key(code: str) -> str:
    c = str(code or "").strip().lower()
    return f"err.asr.{c if c in ASR_CODES else ASR_UNKNOWN}"


# ── 容器识别 ─────────────────────────────────────────────────────────────────

def _sniff_container(head: bytes) -> str:
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"OggS":
        return "ogg"
    if head[:3] == b"ID3" or (len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0):
        return "mp3"
    if head[4:8] == b"ftyp":
        return "m4a"
    if head[:4] == b"\x1a\x45\xdf\xa3":
        return "webm"
    if head[:6] == b"#!AMR\n" or head[:9] == b"#!AMR-WB\n":
        return "amr"
    if head[:4] == b"fLaC":
        return "flac"
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xF6) == 0xF0:
        return "aac"
    return "unknown"


def _wav_duration(path: str) -> Optional[float]:
    """RIFF/WAVE：fmt 块的 byte_rate + data 块长度 → 秒。坏头 → None。"""
    try:
        with open(path, "rb") as f:
            riff = f.read(12)
            if riff[:4] != b"RIFF" or riff[8:12] != b"WAVE":
                return None
            byte_rate = 0
            data_len = 0
            while True:
                hdr = f.read(8)
                if len(hdr) < 8:
                    break
                cid, clen = hdr[:4], struct.unpack("<I", hdr[4:8])[0]
                if cid == b"fmt ":
                    fmt = f.read(clen)
                    if len(fmt) >= 16:
                        byte_rate = struct.unpack("<I", fmt[8:12])[0]
                elif cid == b"data":
                    data_len = clen
                    break
                else:
                    f.seek(clen + (clen & 1), os.SEEK_CUR)
            if byte_rate > 0 and data_len > 0:
                return round(data_len / float(byte_rate), 2)
    except Exception:
        return None
    return None


def _ogg_duration(path: str) -> Optional[float]:
    """OGG：第一页识别 Opus（48k 固定）或 Vorbis（读采样率），末页 granule → 秒。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(min(size, 4096))
            rate = 0
            if b"OpusHead" in head:
                rate = 48000
            else:
                i = head.find(b"\x01vorbis")
                if i >= 0 and len(head) >= i + 16:
                    rate = struct.unpack("<I", head[i + 12:i + 16])[0]
            if rate <= 0:
                return None
            f.seek(max(0, size - 65536))
            tail = f.read()
        j = tail.rfind(b"OggS")
        if j < 0 or len(tail) < j + 14:
            return None
        granule = struct.unpack("<q", tail[j + 6:j + 14])[0]
        if granule <= 0:
            return None
        return round(granule / float(rate), 2)
    except Exception:
        return None


def _ffprobe_duration(path: str, timeout: float = 5.0) -> Optional[float]:
    exe = shutil.which("ffprobe")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=timeout)
        v = float(str(out.stdout or "").strip() or 0)
        return round(v, 2) if v > 0 else None
    except Exception:
        return None


def probe_audio_file(path: str, *, use_ffprobe: bool = True) -> Dict[str, Any]:
    """上传落盘核查 → ``{"ok", "bytes", "duration_sec", "container", "reason"}``。

    ``ok=False`` 的三种 reason：``missing``（路径不存在）/ ``empty``（0 字节）/
    ``bad_header``（前 12 字节不像任何音频容器＝解码或写盘出了问题）。
    ``duration_sec`` 算不出来 → None（调用方按字节数回显），**不**算失败。
    """
    p = str(path or "")
    if not p or not os.path.isfile(p):
        return {"ok": False, "bytes": 0, "duration_sec": None,
                "container": "unknown", "reason": INTAKE_MISSING}
    try:
        n = int(os.path.getsize(p))
    except OSError:
        n = 0
    if n <= 0:
        return {"ok": False, "bytes": 0, "duration_sec": None,
                "container": "unknown", "reason": INTAKE_EMPTY}
    try:
        with open(p, "rb") as f:
            head = f.read(12)
    except OSError:
        head = b""
    container = _sniff_container(head)
    if container == "unknown":
        return {"ok": False, "bytes": n, "duration_sec": None,
                "container": container, "reason": INTAKE_BAD_HEADER}
    dur: Optional[float] = None
    if container == "wav":
        dur = _wav_duration(p)
    elif container == "ogg":
        dur = _ogg_duration(p)
    if dur is None and use_ffprobe:
        dur = _ffprobe_duration(p)
    return {"ok": True, "bytes": n, "duration_sec": dur,
            "container": container, "reason": ""}


def received_summary(probe: Dict[str, Any]) -> Dict[str, Any]:
    """给前端回显的精简形状（不带 reason/container 之外的内部字段）。"""
    d = probe.get("duration_sec")
    return {
        "bytes": int(probe.get("bytes") or 0),
        "duration_sec": (round(float(d), 1) if isinstance(d, (int, float)) and d > 0 else None),
        "container": str(probe.get("container") or "unknown"),
    }


# ── 失败分类 ─────────────────────────────────────────────────────────────────

_TIMEOUT_RE = re.compile(
    r"timeout|timed\s*out|transcribe_timeout|APITimeoutError|ReadTimeout|"
    r"ConnectTimeout|deadline|超时", re.I)
_UNAVAILABLE_RE = re.compile(
    r"APIConnectionError|ConnectionRefused|ConnectionError|Connection\s+refused|"
    r"unreachable|Name or service not known|getaddrinfo|10061|10060|"
    r"cb_open|circuit|model_load_failed|missing api_key|missing dependency|"
    r"token.*(?:未就绪|empty)|AITR_HOSTED_AI_KEY|不可达|未配置|pipeline_disabled|"
    r"asr_unconfigured|Service Unavailable|\b50[234]\b|InternalServerError", re.I)
_REJECTED_RE = re.compile(
    r"AuthenticationError|PermissionDenied|RateLimit|Unauthorized|Forbidden|"
    r"invalid[_ ]api[_ ]key|incorrect api key|\b40[013]\b|\b404\b|\b429\b|"
    r"NotFoundError|BadRequestError|model[^\n]{0,40}not found|does not exist|"
    r"quota|令牌.*(?:过期|无效)|拒绝", re.I)
_UPLOAD_RE = re.compile(
    r"file_not_found|empty_after_decode|write_failed|bad_data_url|decode_failed|"
    r"文件不存在|未正确接收", re.I)
_FORMAT_RE = re.compile(
    r"unsupported|Invalid file format|invalid_file|could not decode|"
    r"Unrecognized|file_too_large|file too large|too_large|too large|"
    r"格式|无法解码|ffmpeg|codec", re.I)
_NO_SPEECH_RE = re.compile(r"no_speech|no speech|empty transcript|返回空结果|返空", re.I)


def classify_asr_error(error: Any) -> str:
    """转写失败原因（异常字符串 / TranscribeResult.error / 转写器 last_error）→ 机器码。

    判序刻意为：超时 > 未接收 > 格式 > 拒绝 > 不可用 > 无语音——「超时」最常被
    误报成「不可用」（5NXHUW 原话），先认它；「400 Invalid file format」是格式问题
    不是被拒（用户能自己换格式）；鉴权/配额类才是「被拒」，与网络不通是两码事。
    空串 / 认不出 → ``unknown``（路由回落通用「转录失败，请重试」文案）。
    """
    s = str(error or "").strip()
    if not s:
        return ASR_UNKNOWN
    if _TIMEOUT_RE.search(s):
        return ASR_TIMEOUT
    if _UPLOAD_RE.search(s):
        return ASR_UPLOAD_FAILED
    if _FORMAT_RE.search(s):
        return ASR_FORMAT
    if _REJECTED_RE.search(s):
        return ASR_REJECTED
    if _UNAVAILABLE_RE.search(s):
        return ASR_UNAVAILABLE
    if _NO_SPEECH_RE.search(s):
        return ASR_NO_SPEECH
    return ASR_UNKNOWN


__all__ = [
    "ASR_CODES",
    "ASR_FORMAT",
    "ASR_NO_SPEECH",
    "ASR_REJECTED",
    "ASR_TIMEOUT",
    "ASR_UNAVAILABLE",
    "ASR_UNKNOWN",
    "ASR_UPLOAD_FAILED",
    "INTAKE_BAD_HEADER",
    "INTAKE_EMPTY",
    "INTAKE_MISSING",
    "asr_error_i18n_key",
    "classify_asr_error",
    "probe_audio_file",
    "received_summary",
]
