"""通话音频适配 —— PCM16 重采样 / 声道折叠（纯数学核心 + numpy 加速，可离线单测）。

为什么需要：通话传输层(ntgcalls/SIP)用 **48kHz** PCM16，MiniCPM-o 实时大脑上行要 **16kHz**、
下行吐 16k/24k；两侧采样率不一致 → 必须重采样。本模块把**长度数学**（确定性、可精确断言）
与**实际重采样**（numpy 优先、纯 Python 线性兜底）分开：

  - ``resampled_len``        —— 纯函数：给定源样本数/源率/目标率，算目标样本数（测试锚点）
  - ``resample_pcm16``       —— 48k↔16k↔24k 线性重采样（numpy 有则用，无则纯 Python）
  - ``downmix_to_mono``      —— 多声道 → 单声道（均值折叠）
  - ``pcm16_duration_ms``    —— PCM16 时长（容量/节奏估算）

设计：线性插值足够通话可懂（生产要更高保真可换 soxr，接口不变）；任何异常安全退化返回原样，
绝不抛进实时音频热路（卡死通话比降级更糟）。
"""
from __future__ import annotations

import array
from typing import List


def resampled_len(n_samples: int, src_rate: int, dst_rate: int) -> int:
    """重采样后的样本数（纯整数数学，重采样实现必须与之一致）。"""
    if n_samples <= 0 or src_rate <= 0 or dst_rate <= 0:
        return 0
    if src_rate == dst_rate:
        return int(n_samples)
    return max(1, int(n_samples * dst_rate // src_rate))


def pcm16_duration_ms(pcm: bytes, sample_rate: int, channels: int = 1) -> float:
    """PCM16 字节流时长（毫秒）。"""
    if not pcm or sample_rate <= 0 or channels <= 0:
        return 0.0
    n_samples = len(pcm) // 2 // channels
    return n_samples * 1000.0 / sample_rate


def downmix_to_mono(pcm: bytes, channels: int) -> bytes:
    """交织多声道 PCM16 → 单声道（各声道均值）。channels<=1 原样返回。"""
    if channels <= 1 or not pcm:
        return pcm
    try:
        samples = array.array("h")
        samples.frombytes(pcm[: (len(pcm) // (2 * channels)) * 2 * channels])
        out = array.array("h", [0] * (len(samples) // channels))
        for i in range(len(out)):
            acc = 0
            base = i * channels
            for c in range(channels):
                acc += samples[base + c]
            out[i] = int(acc / channels)
        return out.tobytes()
    except Exception:
        return pcm


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """PCM16 mono 线性重采样。src==dst 或空输入原样返回。异常安全退化返回原样。

    numpy 可用走向量化线性插值（热路快）；否则纯 Python 线性插值（离线/CI 无 numpy 也能测）。
    """
    if not pcm or src_rate <= 0 or dst_rate <= 0 or src_rate == dst_rate:
        return pcm
    try:
        n_in = len(pcm) // 2
        if n_in == 0:
            return b""
        n_out = resampled_len(n_in, src_rate, dst_rate)
        if n_out <= 0:
            return b""
        try:
            import numpy as np
            src = np.frombuffer(pcm[: n_in * 2], dtype="<i2").astype("float32")
            # 目标样本在源时间轴上的位置（端点对齐，避免整体音高偏移）
            idx = np.linspace(0.0, n_in - 1, num=n_out, dtype="float32")
            lo = np.floor(idx).astype("int32")
            hi = np.minimum(lo + 1, n_in - 1)
            frac = idx - lo
            out = (src[lo] * (1.0 - frac) + src[hi] * frac)
            out = np.clip(np.round(out), -32768, 32767).astype("<i2")
            return out.tobytes()
        except Exception:
            return _resample_pure(pcm, n_in, n_out)
    except Exception:
        return pcm


def _resample_pure(pcm: bytes, n_in: int, n_out: int) -> bytes:
    """纯 Python 线性插值兜底（numpy 缺失时）。"""
    src = array.array("h")
    src.frombytes(pcm[: n_in * 2])
    out = array.array("h", [0] * n_out)
    if n_out == 1:
        out[0] = src[0]
        return out.tobytes()
    ratio = (n_in - 1) / (n_out - 1)
    for i in range(n_out):
        pos = i * ratio
        lo = int(pos)
        hi = min(lo + 1, n_in - 1)
        frac = pos - lo
        val = src[lo] * (1.0 - frac) + src[hi] * frac
        out[i] = max(-32768, min(32767, int(round(val))))
    return out.tobytes()


__all__ = [
    "resampled_len",
    "pcm16_duration_ms",
    "downmix_to_mono",
    "resample_pcm16",
]
