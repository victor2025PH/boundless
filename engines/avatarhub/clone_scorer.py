"""
克隆质量评分：用 CosyVoice 自带的 campplus 说话人嵌入模型，
计算「合成音」与「参考音」的说话人余弦相似度，量化"像不像"。

复用 token2wav.py 的特征提取流程：
  16k mono → kaldi.fbank(80, dither=0) → 减均值 → campplus.onnx → 嵌入向量
相似度 = 两个嵌入的余弦值，范围 ~[-1,1]，越接近 1 越像同一人。
"""
import io
import base64
import threading

import numpy as np

import app_config
_CAMPPLUS_PATH = str(app_config.BASE / "CosyVoice" / "pretrained_models" / "Fun-CosyVoice3-0.5B" / "campplus.onnx")

_sess = None
_sess_lock = threading.Lock()


def _get_session():
    """懒加载 onnx 会话（CPU 推理，~28MB，进程内单例）。"""
    global _sess
    if _sess is None:
        with _sess_lock:
            if _sess is None:
                import onnxruntime
                opt = onnxruntime.SessionOptions()
                opt.intra_op_num_threads = 2
                opt.log_severity_level = 3
                _sess = onnxruntime.InferenceSession(
                    _CAMPPLUS_PATH, sess_options=opt,
                    providers=["CPUExecutionProvider"])
    return _sess


def _decode_wav(data: bytes):
    """bytes(WAV PCM) → (float32 ndarray[T] in [-1,1], sample_rate)。仅用标准库，避免 torchcodec/soundfile 依赖。"""
    import wave
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            nch   = w.getnchannels()
            width = w.getsampwidth()
            sr    = w.getframerate()
            frames = w.readframes(w.getnframes())
    except wave.Error:
        # Song-P1: 标准库 wave 不认 IEEE float WAV（fmt=3，录音棚/DAW 导出常见）→ 手动解析
        return _decode_wav_manual(data)
    if width == 2:
        arr = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    elif width == 4:
        arr = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif width == 1:
        arr = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"不支持的位深: {width*8}bit")
    if nch > 1:                                  # 交错多声道 → 单声道均值
        arr = arr.reshape(-1, nch).mean(axis=1)
    return arr, sr


def _decode_wav_manual(data: bytes):
    """兜底 RIFF 解析：支持 PCM(1) / IEEE float(3) / extensible(0xFFFE)。"""
    import struct
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("不是有效的 WAV 文件")
    fmt_tag = nch = sr = bits = None
    payload = None
    pos = 12
    while pos + 8 <= len(data):
        cid, csz = data[pos:pos + 4], struct.unpack("<I", data[pos + 4:pos + 8])[0]
        body = data[pos + 8:pos + 8 + csz]
        if cid == b"fmt ":
            fmt_tag, nch, sr = struct.unpack("<HHI", body[:8])
            bits = struct.unpack("<H", body[14:16])[0]
            if fmt_tag == 0xFFFE and len(body) >= 26:    # extensible → 取 SubFormat 前 2 字节
                fmt_tag = struct.unpack("<H", body[24:26])[0]
        elif cid == b"data":
            payload = body
        pos += 8 + csz + (csz & 1)                       # 块按 2 字节对齐
    if fmt_tag is None or payload is None:
        raise ValueError("WAV 缺少 fmt/data 块")
    if fmt_tag == 3 and bits == 32:
        arr = np.frombuffer(payload, dtype=np.float32).astype(np.float32)
    elif fmt_tag == 3 and bits == 64:
        arr = np.frombuffer(payload, dtype=np.float64).astype(np.float32)
    elif fmt_tag == 1 and bits == 16:
        arr = np.frombuffer(payload, dtype=np.int16).astype(np.float32) / 32768.0
    elif fmt_tag == 1 and bits == 32:
        arr = np.frombuffer(payload, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif fmt_tag == 1 and bits == 8:
        arr = (np.frombuffer(payload, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"不支持的 WAV 格式: fmt={fmt_tag} bits={bits}")
    if nch and nch > 1:
        arr = arr[: len(arr) // nch * nch].reshape(-1, nch).mean(axis=1)
    return np.clip(arr, -1.0, 1.0), sr


def _load_wav_16k(data: bytes):
    """bytes(WAV) → torch.FloatTensor[T]，单声道 16kHz。"""
    import torch
    arr, sr = _decode_wav(data)
    if sr != 16000:
        from scipy.signal import resample_poly
        from math import gcd
        g = gcd(int(sr), 16000)
        arr = resample_poly(arr, 16000 // g, int(sr) // g).astype(np.float32)
    return torch.from_numpy(np.ascontiguousarray(arr)).float()


def _embed(wav) -> np.ndarray:
    """单段音频 → campplus 说话人嵌入向量（已 L2 归一化）。"""
    import torch
    import torchaudio.compliance.kaldi as kaldi
    feat = kaldi.fbank(wav.unsqueeze(0), num_mel_bins=80,
                       dither=0, sample_frequency=16000)
    feat = feat - feat.mean(dim=0, keepdim=True)
    sess = _get_session()
    inp = feat.unsqueeze(0).cpu().numpy().astype(np.float32)  # [1, T, 80]
    emb = sess.run(None, {sess.get_inputs()[0].name: inp})[0].flatten()
    n = np.linalg.norm(emb)
    return emb / n if n > 0 else emb


def _b64_to_bytes(s: str) -> bytes:
    if "," in s and s.strip().startswith("data:"):
        s = s.split(",", 1)[1]
    return base64.b64decode(s)


def score_similarity(reference_b64: str, synth_b64: str) -> dict:
    """
    返回 {ok, similarity(0~1), label, detail}
    similarity 已把余弦的 [-1,1] 线性映射到 [0,1] 便于展示。
    """
    try:
        ref = _load_wav_16k(_b64_to_bytes(reference_b64))
        syn = _load_wav_16k(_b64_to_bytes(synth_b64))
        if ref.numel() < 1600 or syn.numel() < 1600:   # < 0.1s 视为无效
            return {"ok": False, "detail": "音频过短，无法评分"}
        e1 = _embed(ref)
        e2 = _embed(syn)
        cos = float(np.dot(e1, e2))                      # 已归一化，点积=余弦
        sim01 = round((cos + 1) / 2, 4)                  # 映射到 0~1
        cos_r = round(cos, 4)
        if   cos >= 0.75: label = "极佳"
        elif cos >= 0.60: label = "优秀"
        elif cos >= 0.45: label = "良好"
        elif cos >= 0.30: label = "一般"
        else:             label = "偏差较大"
        return {"ok": True, "cosine": cos_r, "similarity": sim01,
                "label": label}
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}


def warmup():
    """预加载会话，避免首次调用卡顿。"""
    try:
        _get_session()
        return True
    except Exception:
        return False
