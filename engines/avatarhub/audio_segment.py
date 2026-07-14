"""
长录音自动分段：能量 VAD 在静音处切成「非重叠、去首尾静音、响度归一」的干净片段，
专为多段参考融合准备素材。零额外依赖（numpy + 标准库 wave）。

核心思路（与 Phase C 结论对齐：非重叠 + 多样 = 更稳更像）：
  1. 解码 → 单声道 float
  2. 30ms 帧 RMS → 自适应阈值判定 speech/silence
  3. 在 ≥min_silence 的静音处寻找切点，贪心累积到 target 秒就切
  4. 每段去首尾静音 + RMS 归一到 -20dBFS
"""
import io
import wave
import base64
import numpy as np


def _decode_wav(data: bytes):
    """bytes(WAV PCM) → (float32 ndarray[T] in [-1,1], sample_rate)。"""
    with wave.open(io.BytesIO(data), "rb") as w:
        nch, width, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
        frames = w.readframes(w.getnframes())
    if width == 2:
        arr = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 4:
        arr = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif width == 1:
        arr = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"不支持的位深: {width*8}bit")
    if nch > 1:
        arr = arr.reshape(-1, nch).mean(axis=1)
    return arr, sr


def _encode_wav(arr: np.ndarray, sr: int) -> bytes:
    """float32[-1,1] → 16-bit PCM WAV bytes。"""
    pcm = np.clip(arr, -1.0, 1.0)
    pcm = (pcm * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _frame_rms(arr: np.ndarray, sr: int, frame_ms: int = 30):
    """逐帧 RMS。返回 (rms[N], frame_len_samples)。"""
    flen = max(1, int(sr * frame_ms / 1000))
    n = len(arr) // flen
    if n == 0:
        return np.array([np.sqrt(np.mean(arr**2) + 1e-12)]), len(arr)
    trimmed = arr[: n * flen].reshape(n, flen)
    rms = np.sqrt(np.mean(trimmed**2, axis=1) + 1e-12)
    return rms, flen


def _normalize_rms(arr: np.ndarray, target_dbfs: float = -20.0) -> np.ndarray:
    """把片段 RMS 归一到目标 dBFS，避免多段音量不一致干扰克隆。"""
    rms = np.sqrt(np.mean(arr**2) + 1e-12)
    if rms < 1e-6:
        return arr
    target = 10 ** (target_dbfs / 20.0)
    gain = target / rms
    out = arr * gain
    peak = np.max(np.abs(out)) + 1e-9
    if peak > 0.99:                 # 防削顶
        out = out * (0.99 / peak)
    return out


def auto_segment(wav_bytes: bytes, *, target_sec: float = 18.0,
                 min_sec: float = 8.0, max_sec: float = 28.0,
                 min_silence_ms: int = 280, max_segments: int = 4,
                 frame_ms: int = 30) -> dict:
    """
    长录音 → 干净片段（base64 WAV）列表。
    返回 {ok, sample_rate, total_sec, segments:[{b64, dur}], n}
    """
    try:
        arr, sr = _decode_wav(wav_bytes)
    except Exception as e:
        return {"ok": False, "detail": f"解码失败: {e}"}

    total_sec = len(arr) / sr
    if total_sec < min_sec:
        return {"ok": False, "detail": f"录音过短({total_sec:.1f}s)，至少 {min_sec:.0f}s"}

    rms, flen = _frame_rms(arr, sr, frame_ms)
    # 自适应阈值：噪声底(20分位) 与 整体能量 的折中
    noise = np.percentile(rms, 20)
    med = np.median(rms)
    thr = max(noise * 2.2, med * 0.18)
    is_speech = rms > thr

    fps = sr / flen                                  # 帧/秒
    min_sil_frames = max(1, int(min_silence_ms / 1000 * fps))
    target_f = int(target_sec * fps)
    min_f = int(min_sec * fps)
    max_f = int(max_sec * fps)
    N = len(is_speech)

    # 找静音区间（连续 non-speech ≥ min_sil_frames），作为候选切点（取其中点）
    cut_points = []
    i = 0
    while i < N:
        if not is_speech[i]:
            j = i
            while j < N and not is_speech[j]:
                j += 1
            if (j - i) >= min_sil_frames:
                cut_points.append((i + j) // 2)
            i = j
        else:
            i += 1

    # 贪心切段：从当前起点出发，累积到 ~target 处，挑最近的静音切点落刀
    segments = []
    start = 0
    # 跳过开头静音
    fs = np.argmax(is_speech) if is_speech.any() else 0
    start = int(fs)
    while start < N and len(segments) < max_segments:
        ideal = start + target_f
        # 在 [start+min_f, start+max_f] 范围内挑最接近 ideal 的切点
        cand = [c for c in cut_points if start + min_f <= c <= start + max_f]
        if cand:
            cut = min(cand, key=lambda c: abs(c - ideal))
        else:
            cut = min(start + max_f, N)               # 无静音可切，硬切到 max
        seg_frames = is_speech[start:cut]
        # 该段若几乎无语音则跳过
        if seg_frames.sum() < min_f * 0.3:
            start = cut
            continue
        # 段内去首尾静音
        sp = np.where(seg_frames)[0]
        if len(sp) == 0:
            start = cut
            continue
        s_f = start + sp[0]
        e_f = start + sp[-1] + 1
        s_smp = s_f * flen
        e_smp = min(len(arr), e_f * flen)
        seg = arr[s_smp:e_smp]
        dur = len(seg) / sr
        if dur >= min_sec * 0.7:                       # 容忍轻微不足
            seg = _normalize_rms(seg)
            segments.append({"b64": base64.b64encode(_encode_wav(seg, sr)).decode(),
                             "dur": round(dur, 1)})
        start = cut

    if not segments:
        return {"ok": False, "detail": "未能切出有效语音片段，请确认录音清晰"}
    return {"ok": True, "sample_rate": sr, "total_sec": round(total_sec, 1),
            "n": len(segments), "segments": segments}
