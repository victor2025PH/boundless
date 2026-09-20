# -*- coding: utf-8 -*-
"""语音「慢速拟人」编排层（voice pacing，2026-08-19 tmp_voice_eval v3 投产）。

老板对评测页「慢速拟人 v3」的听感拍板后，把评测脚本的装配层平移进生产 hub 路径
（``tts_pipeline._try_hub_fish`` 的 pacing 分支）。整段一口气合成的引擎不会留
气口、语速复制参考音——节奏权收回编排层：

  分句（polish **前**的口语稿——polish 会把句中句号改省略号，先切后 polish）
  → 逐段合成（每段独立情绪标签＝分段合成独有的表现力）
  → 修边留残料 → 响度对齐 → ffmpeg atempo 变速不变调（普通 0.93 / 思考段 0.90）
  → 15ms 余弦淡边（拼接爆点的根治，2026-08-19 上午「电流刺啦声」事故）
  → 句间真实停顿（句 0.5-0.65s / 问句 0.62s / 思考词前 +0.34s，crc 确定性抖动）
  → 真呼吸（hub 参考音四道闸提取，**宁缺毋滥**——合成吸气=滤波噪声会被人耳
    听成电流声，提不出就不放，绝不合成噪声凑数）
  → 底床（合成残料拼「同一房间的安静」；残料近零回落 -56dB 低通暖褐噪）
  → QC（25ms 帧扫描：无绝对静音帧 <-70dBFS、无削顶；不过=整体作废）。

失败语义：任何一步失败/QC 不过/预算不足 → 抛 ``PacingSkip``，调用方回落整段
单发旧路径（同档同引擎，只是没有编排——**宁快勿哑，绝不阻塞出站**，也不构成
换声：音色事实源仍是同一 hub 档）。

软依赖：numpy 缺席=分支整体不可用（``numpy_available()`` 闸）；ffmpeg 缺席=
不降速只停顿（``atempo_wav`` 原样返回）。两者都不入 requirements 硬依赖。
"""
from __future__ import annotations

import io
import logging
import re
import subprocess
import urllib.request
import wave
import zlib
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

logger = logging.getLogger("ai_chat_assistant.voice_pacing")

_THINK_RE = re.compile(r"^(嗯+…*|呃+|唔+|这个嘛|怎么说呢|让我想想)|……")
_HAPPY_RE = re.compile(r"哈哈|嘿嘿|哇|太好|好开心|开心|！|棒|好玩|逗")
_LOW_RE = re.compile(r"唉|累|疼|难受|辛苦|叹")
# 沉稳型人设全程钉基调；其余（俏皮/撒娇/温柔/warm 默认）允许分段跳 happy
_RESTRAINED_STYLES = ("沉稳",)
_EXPRESSIVE_DEFAULT_EMOTIONS = ("warm", "playful", "happy")


class PacingSkip(Exception):
    """编排不可用/不合格——调用方应回落整段单发，不算错误。"""


def numpy_available() -> bool:
    try:
        import numpy  # noqa: F401
        return True
    except Exception:
        return False


# ── 文本分段（与 tmp_voice_eval v2/v3 同口径）────────────────────────────────
def split_chunks(text: str) -> List[Dict[str, Any]]:
    """句级切段；>44 字长句在中部逗号再切；<5 字碎尾并入前段。"""
    text = str(text or "").strip()
    sents = [s for s in re.split(r"(?<=[。！？!?])", text) if s.strip()]
    out: List[Dict[str, Any]] = []
    for s in sents:
        s = s.strip()
        parts = [s]
        if len(s) > 44 and "，" in s[10:-6]:
            cut = min(range(10, len(s) - 6),
                      key=lambda i: abs(i - len(s) // 2) if s[i] == "，" else 9999)
            if s[cut] == "，":
                parts = [s[:cut + 1], s[cut + 1:]]
        for j, p in enumerate(parts):
            if out and len(p) < 5:
                out[-1]["text"] += p
                continue
            out.append({
                "text": p,
                "think": bool(_THINK_RE.search(p)),
                "question": p.rstrip().endswith(("？", "?")),
                "comma_tail": j < len(parts) - 1,
            })
    return out


def merge_to_max(chunks: List[Dict[str, Any]], max_chunks: int) -> List[Dict[str, Any]]:
    """段数超上限时从尾部向前合并（保住句首的节奏，尾部长一点可接受）。"""
    max_chunks = max(1, int(max_chunks))
    out = [dict(c) for c in chunks]
    while len(out) > max_chunks:
        tail = out.pop()
        out[-1]["text"] += tail["text"]
        out[-1]["question"] = tail["question"]
        out[-1]["comma_tail"] = tail["comma_tail"]
        if "gap_class" in tail:
            # 剧本段合并：段后停顿档随尾段走（呼吸标记丢弃——吸气只能在段首）
            out[-1]["gap_class"] = tail.get("gap_class")
    return out


# ── 语音剧本 Speech Script v1（实施65）：解析与档位 ──────────────────────────
# 剧本由 voice_colloquial_llm.build_speech_script_prompt 的 LLM 产出，格式
# 「段‖档 段‖档 段」（‖短/‖中/‖长），段首可带 [breath]。本模块只做**执行**：
# 停顿档位→毫秒、[breath]→真呼吸插槽。语义决策全部在 LLM 侧（实施65 的核心）。
_SCRIPT_MARK_RE = re.compile(r"‖\s*(短|中|长)")
_GAP_CLASS_MS = {"短": 300, "中": 560, "长": 850}


def parse_speech_script(text: str) -> Optional[List[Dict[str, Any]]]:
    """剧本文本 → chunks（含 gap_class/breath）；无标记或段数出界（2..10）→ None。

    None＝调用方回落 split_chunks 旧路（几何切分+crc 编排），行为不劣于旧链。
    """
    t = str(text or "").strip()
    if "‖" not in t:
        return None
    parts = _SCRIPT_MARK_RE.split(t)
    out: List[Dict[str, Any]] = []
    for i in range(0, len(parts), 2):
        seg = str(parts[i] or "").strip()
        cls = str(parts[i + 1]) if i + 1 < len(parts) else ""
        breath = False
        while seg.startswith("[breath]"):
            breath = True
            seg = seg[len("[breath]"):].strip()
        if len(seg) < 2:
            if out and seg:
                out[-1]["text"] += seg
            continue
        out.append({
            "text": seg,
            "think": bool(_THINK_RE.search(seg)),
            "question": seg.rstrip().endswith(("？", "?")),
            "comma_tail": False,
            "gap_class": cls,
            "breath": breath,
        })
    if not (2 <= len(out) <= 10):
        return None
    return out


def gap_ms_for_class(cls: str, seed: int) -> Optional[int]:
    """停顿档 → 毫秒（±10% 确定性抖动）；未知档 → None（回落 gap_ms_after）。"""
    base = _GAP_CLASS_MS.get(str(cls or "").strip())
    if not base:
        return None
    jitter = ((seed % 21) - 10) / 100.0
    return int(base * (1.0 + jitter))


def ensure_think(chunks: List[Dict[str, Any]], seed: int) -> List[Dict[str, Any]]:
    """整段没有思考词 → 第 2/3 段前补「嗯……」（确定性，至多一处）。"""
    if any(c["think"] for c in chunks) or len(chunks) < 2:
        return chunks
    i = 1 + (seed % max(1, min(2, len(chunks) - 1)))
    i = min(i, len(chunks) - 1)
    chunks[i]["text"] = "嗯……" + chunks[i]["text"]
    chunks[i]["think"] = True
    return chunks


def gap_ms_after(cur: Dict[str, Any], nxt: Optional[Dict[str, Any]],
                 seed: int) -> int:
    """段后停顿（毫秒）：逗号切 0.3s / 句 0.56s / 问句 0.62s；思考词前 +0.34s；±15% 抖动。"""
    if nxt is None:
        return 0
    base = 300 if cur.get("comma_tail") else (620 if cur.get("question") else 560)
    if nxt.get("think"):
        base += 340
    jitter = ((seed % 31) - 15) / 100.0
    return int(base * (1.0 + jitter))


def is_expressive(instruct_style: str, default_emotion: str) -> bool:
    """人设是否允许分段情绪外放（评测集校准：沉稳型钉基调，warm 默认放开）。"""
    style = str(instruct_style or "").strip()
    if style and style not in _RESTRAINED_STYLES:
        return True
    return str(default_emotion or "").strip().lower() in _EXPRESSIVE_DEFAULT_EMOTIONS


def chunk_emotion(text: str, base: str, *, expressive: bool, think: bool) -> str:
    if not expressive or think:
        return base
    if _HAPPY_RE.search(text):
        return "happy"
    if _LOW_RE.search(text) and base == "happy":
        return "gentle"
    return base


# ── 音频原语（wave/stdlib + numpy）───────────────────────────────────────────
def wav_to_float(data: bytes):
    import numpy as np
    with wave.open(io.BytesIO(data), "rb") as w:
        sr = w.getframerate()
        ch = w.getnchannels()
        raw = w.readframes(w.getnframes())
    x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1)
    return x, sr


def float_to_wav(x, sr: int) -> bytes:
    import numpy as np
    pcm = (np.clip(x, -1.0, 1.0) * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _rms(x) -> float:
    import numpy as np
    return float(np.sqrt(np.mean(x * x) + 1e-12)) if getattr(x, "size", 0) else 0.0


def trim_keep_residue(x, sr: int, thresh_db: float = -42.0, pad_ms: int = 40):
    """修边并返回裁下的「空气声」残料（底床原料；同一段声音的噪底=零假声）。"""
    import numpy as np
    if x.size == 0:
        return x, []
    win = max(1, sr // 100)
    env = np.sqrt(np.convolve(x * x, np.ones(win) / win, mode="same"))
    thr = 10 ** (thresh_db / 20.0)
    idx = np.nonzero(env > thr)[0]
    if idx.size == 0:
        return x, []
    pad = int(sr * pad_ms / 1000)
    a = max(0, int(idx[0]) - pad)
    b = min(x.size, int(idx[-1]) + pad)
    residues = []
    if a >= int(0.03 * sr):
        residues.append(x[:a])
    if x.size - b >= int(0.03 * sr):
        residues.append(x[b:])
    return x[a:b], residues


def fade_edges(x, sr: int, ms: float = 15.0):
    """两端余弦淡入淡出——拼接爆点（波形阶跃）的根治。"""
    import numpy as np
    n = min(int(sr * ms / 1000.0), max(1, x.size // 4))
    if n <= 1:
        return x
    y = x.copy()
    ramp = 0.5 - 0.5 * np.cos(np.linspace(0.0, np.pi, n, dtype=np.float32))
    y[:n] *= ramp
    y[-n:] *= ramp[::-1]
    return y


def atempo_wav(wav_bytes: bytes, factor: float, sr: int) -> bytes:
    """ffmpeg 变速不变调；ffmpeg 缺席/失败原样返回（不降速仍有停顿收益）。"""
    if abs(float(factor) - 1.0) < 0.01:
        return wav_bytes
    try:
        p = subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-i", "pipe:0",
             "-filter:a", f"atempo={factor}", "-ar", str(int(sr)), "-ac", "1",
             "-f", "wav", "pipe:1"],
            input=wav_bytes, capture_output=True, timeout=60)
        if p.returncode == 0 and p.stdout[:4] == b"RIFF":
            return p.stdout
    except Exception:
        pass
    return wav_bytes


def _crossfade_concat(parts: list, sr: int, fade_ms: int = 50):
    import numpy as np
    if not parts:
        return np.zeros(0, dtype=np.float32)
    n_f = int(sr * fade_ms / 1000)
    out = parts[0].astype(np.float32)
    for p in parts[1:]:
        p = p.astype(np.float32)
        k = min(n_f, out.size, p.size)
        if k > 8:
            ramp = np.linspace(0.0, 1.0, k, dtype=np.float32)
            mixed = out[-k:] * np.sqrt(1 - ramp) + p[:k] * np.sqrt(ramp)
            out = np.concatenate([out[:-k], mixed, p[k:]])
        else:
            out = np.concatenate([out, p])
    return out


def _bed_stabilizer(total: int, sr: int, seed: int):
    """-66dB 低通白噪稳定层：褐噪/残料床的帧能量在低谷会跌破 -70dBFS（QC 死寂线），
    白噪帧方差仅 ±1.5dB，垫底保证每帧过线；比主床低 10dB，听感不可闻。"""
    import numpy as np
    rng = np.random.default_rng(seed ^ 0x5F5F)
    w = rng.standard_normal(total).astype(np.float32)
    W = np.fft.rfft(w)
    freqs = np.fft.rfftfreq(total, 1 / sr)
    W[freqs > 4000] = 0.0
    w = np.fft.irfft(W, total).astype(np.float32)
    return w * (10 ** (-66 / 20) / (_rms(w) + 1e-12))


def build_bed(residues: list, total: int, sr: int, seed: int):
    """底床：优先残料拼（同一房间的安静）；残料近零 → -56dB 低通暖褐噪。

    两条路径都叠 ``_bed_stabilizer``——低频为主的床在 25ms 帧尺度有深谷，
    单靠主床会被 QC 判死寂帧（首版实测 -78dB）。"""
    import numpy as np
    stab = _bed_stabilizer(total, sr, seed)
    pool = [r for r in residues if r.size >= int(0.03 * sr)]
    pool_med = float(np.median([_rms(r) for r in pool])) if pool else 0.0
    if pool and pool_med >= 10 ** (-64 / 20):
        rng = np.random.default_rng(seed)
        order: list = []
        # 终止条件按**原始长度**累计 + 轮数护栏（首版「预留淡接损耗」的动态门槛
        # 会被碎料的增速反超 → 死循环，2026-08-19 pytest 卡死实锤）。淡接损耗
        # 两层兜：① 床拼接用 8ms 短淡接（修边残料多为 30-40ms 碎片，50ms 淡接
        # 对它们是净零增长）；② 仍不足额 wrap 补齐——极端下床是短循环重复，
        # 作为 -5xdB 底噪仍然合法。
        guard = 0
        while sum(p.size for p in order) < total + sr and guard < 200:
            order.extend(pool[i] for i in rng.permutation(len(pool)))
            guard += 1
        bed = _crossfade_concat(order, sr, fade_ms=8)
        if bed.size < total:
            bed = np.pad(bed, (0, total - bed.size), mode="wrap")
        bed = bed[:total]
        target = float(np.clip(pool_med, 10 ** (-58 / 20), 10 ** (-50 / 20)))
        bed = bed * (target / (_rms(bed) + 1e-12))
        return (bed + stab).astype(np.float32), "residue"
    rng = np.random.default_rng(seed)
    x = np.cumsum(rng.standard_normal(total).astype(np.float32))
    x -= np.linspace(x[0], x[-1], total, dtype=np.float32)
    X = np.fft.rfft(x)
    freqs = np.fft.rfftfreq(total, 1 / sr)
    mask = np.ones_like(freqs)
    mask[freqs > 1500] = 0.0
    soft = (freqs > 900) & (freqs <= 1500)
    mask[soft] = np.linspace(1.0, 0.0, int(soft.sum()) or 1)
    x = np.fft.irfft(X * mask, total).astype(np.float32)
    x = x * (10 ** (-56 / 20) / (_rms(x) + 1e-12))
    return (x + stab).astype(np.float32), "noise"


def extract_breath(x, sr: int):
    """参考音里提真吸气：能量带 + 前置静音 + 过零率 + 低频占比 四道闸，宁缺毋滥。"""
    import numpy as np
    if x is None or getattr(x, "size", 0) < sr:
        return None
    hop = int(0.010 * sr)
    win = int(0.020 * sr)
    n = (x.size - win) // hop
    if n < 50:
        return None
    fr = np.lib.stride_tricks.sliding_window_view(x, win)[::hop][:n]
    rms = np.sqrt(np.mean(fr * fr, axis=1) + 1e-12)
    zcr = np.mean(np.abs(np.diff(np.sign(fr), axis=1)) > 0, axis=1)
    floor = float(np.percentile(rms, 10))
    peak = float(np.percentile(rms, 95))
    voiced = rms > peak * 0.3
    voiced_zcr = float(np.median(zcr[voiced])) if voiced.any() else 0.05
    lo, hi = max(floor * 2.5, 1e-5), peak * 0.12
    cand = (rms > lo) & (rms < hi)
    best, best_score = None, -1.0
    i = 0
    while i < n:
        if not cand[i]:
            i += 1
            continue
        j = i
        while j < n and cand[j]:
            j += 1
        dur = (j - i) * hop / sr
        if 0.12 <= dur <= 0.50:
            pre_a, pre_b = max(0, i - 10), i
            pre_quiet = (rms[pre_a:pre_b] < lo).mean() if pre_b > pre_a else 0.0
            seg = x[i * hop: j * hop + win]
            z = float(np.mean(zcr[i:j]))
            spec = np.abs(np.fft.rfft(seg)) ** 2
            fq = np.fft.rfftfreq(seg.size, 1 / sr)
            low_ratio = float(spec[fq < 250].sum() / (spec.sum() + 1e-12))
            if pre_quiet >= 0.7 and z > 1.5 * voiced_zcr and low_ratio < 0.40:
                score = z * (1.0 - abs(dur - 0.28))
                if score > best_score:
                    best, best_score = seg.copy(), score
        i = j
    if best is None:
        return None
    N = best.size
    B = np.fft.rfft(best)
    fq = np.fft.rfftfreq(N, 1 / sr)
    m = np.ones_like(fq)
    m[fq < 150] = 0.0
    m[fq > 6000] = 0.0
    best = np.fft.irfft(B * m, N).astype(np.float32)
    k = min(int(0.04 * sr), N // 3)
    ramp = np.linspace(0, 1, k, dtype=np.float32)
    best[:k] *= ramp
    best[-k:] *= ramp[::-1]
    return best


def load_breath_for_profile(base_url: str, profile: str, cache_dir: Path,
                            sr: int):
    """拉 hub 档参考音（磁盘缓存）→ 提真吸气。任何失败 → None（不放呼吸）。

    缓存落 ``cache_dir``（生产传实例数据根下的 config/voice_pacing_refs——
    服务进程 CWD=数据根，属 C 类数据落点）。
    """
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache = cache_dir / (
            f"{zlib.crc32(profile.encode('utf-8')) & 0xffffffff}_{int(sr)}.wav")
        if not cache.exists():
            url = f"{base_url.rstrip('/')}/profiles/{quote(profile)}/reference_audio"
            with urllib.request.urlopen(url, timeout=30) as r:  # nosec B310
                raw = r.read()
            p = subprocess.run(
                ["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-ar", str(sr),
                 "-ac", "1", "-f", "wav", "pipe:1"],
                input=raw, capture_output=True, timeout=60)
            if p.returncode != 0 or p.stdout[:4] != b"RIFF":
                return None
            cache.write_bytes(p.stdout)
        x, _sr = wav_to_float(cache.read_bytes())
        return extract_breath(x, sr)
    except Exception:
        return None


def qc_frames(final, sr: int) -> Tuple[float, bool]:
    """25ms 帧扫描：最低帧能量（dBFS）与是否合格（无死寂帧、无削顶）。"""
    import numpy as np
    hop = int(0.025 * sr)
    n = final.size // hop
    if not n:
        return -99.0, False
    fr = final[:n * hop].reshape(n, hop)
    frdb = 20 * np.log10(np.sqrt(np.mean(fr * fr, axis=1)) + 1e-9)
    min_db = float(frdb.min())
    ok = (min_db > -70.0) and (float(np.max(np.abs(final))) < 0.99)
    return min_db, ok


# ── 总编排 ───────────────────────────────────────────────────────────────────
def paced_synthesize(
    spoken_text: str,
    *,
    synth_chunk: Callable[[str, str], bytes],
    base_emotion: str = "",
    expressive: bool = False,
    polish: Optional[Callable[[str], str]] = None,
    tempo: float = 0.93,
    think_tempo: float = 0.90,
    min_chars: int = 24,
    max_chunks: int = 8,
    breath_loader: Optional[Callable[[int], Any]] = None,
    bed: bool = True,
    inject_think: bool = True,
    seed_key: str = "",
) -> Tuple[bytes, Dict[str, Any]]:
    """分句→逐段合成→装配。返回 (wav bytes, meta)。不合格抛 ``PacingSkip``。

    ``synth_chunk(text, emotion) -> wav bytes``：每段的合成回调（hub 往返在
    调用方，本函数纯装配）。``polish``：每段送合成前的清洗（hub 送稿 polish
    必须在**分句之后**做——它会把句中句号改成省略号，先 polish 会毁掉分句）。
    ``breath_loader(sr) -> ndarray|None``：按实际合成采样率惰性取真吸气
    （None/返 None=不放呼吸——绝不合成噪声凑数）。
    """
    import numpy as np

    text = str(spoken_text or "").strip()
    seed = zlib.crc32((seed_key + text).encode("utf-8"))
    # 实施65：文本带剧本标记 → 按 LLM 的语义决策执行（跳过几何切分与 crc 思考词）；
    # 无标记/解析失败 → 原路（split+ensure_think）。
    script_chunks = parse_speech_script(text)
    if script_chunks is not None:
        plain_len = sum(len(c["text"]) for c in script_chunks)
        if plain_len < max(1, int(min_chars)):
            raise PacingSkip(f"text too short ({plain_len} < {min_chars})")
        chunks = merge_to_max(script_chunks, max_chunks)
    else:
        if len(text) < max(1, int(min_chars)):
            raise PacingSkip(f"text too short ({len(text)} < {min_chars})")
        chunks = split_chunks(text)
        if inject_think:
            chunks = ensure_think(chunks, seed)
        chunks = merge_to_max(chunks, max_chunks)
    if len(chunks) < 2:
        raise PacingSkip("single chunk — 编排无增益")

    pieces, residues, emos = [], [], []
    sr = 0
    for c in chunks:
        emo = chunk_emotion(c["text"], base_emotion,
                            expressive=expressive, think=c["think"])
        emos.append(emo)
        send = polish(c["text"]) if polish else c["text"]
        if not str(send or "").strip():
            raise PacingSkip("polish emptied a chunk")
        audio = synth_chunk(str(send), emo)
        x, c_sr = wav_to_float(audio)
        if sr == 0:
            sr = int(c_sr)
        elif int(c_sr) != sr:
            raise PacingSkip(f"chunk sr mismatch {c_sr} != {sr}")
        xt, res = trim_keep_residue(x, sr)
        if xt.size < int(0.15 * sr):
            raise PacingSkip("chunk audio too short after trim")
        pieces.append(xt)
        residues.extend(res)

    med = float(np.median([_rms(p) for p in pieces]))
    fixed = []
    tempo_applied = False
    for c, p in zip(chunks, pieces):
        gain = float(np.clip(med / (_rms(p) + 1e-12), 0.75, 1.35))
        f = think_tempo if c["think"] else tempo
        wav = atempo_wav(float_to_wav(p * gain, sr), f, sr)
        y, _ = wav_to_float(wav)
        if abs(f - 1.0) >= 0.01 and y.size > p.size * 1.02:
            tempo_applied = True
        fixed.append(fade_edges(y, sr))

    gaps = []
    for i in range(len(chunks)):
        nxt = chunks[i + 1] if i + 1 < len(chunks) else None
        gseed = zlib.crc32(f"{seed_key}#{i}".encode())
        if nxt is None:
            gaps.append(0)
            continue
        g = gap_ms_for_class(chunks[i].get("gap_class") or "", gseed)
        if g is None:
            g = gap_ms_after(chunks[i], nxt, gseed)
        if script_chunks is not None and nxt.get("breath"):
            g = max(g, 620)   # 吸气要占位：呼吸段前的停顿至少给足一口气的时长
        gaps.append(g)
    breath = None
    if breath_loader is not None:
        try:
            breath = breath_loader(sr)
        except Exception:
            breath = None
    breath_slots: List[int] = []
    if breath is not None and getattr(breath, "size", 0):
        breath = breath * (med * 0.07 / (_rms(breath) + 1e-12))
        if script_chunks is not None:
            # 剧本模式：呼吸位置由 LLM 的 [breath] 标记决定（吸气在该段之前的停顿里）
            breath_slots = [i for i in range(len(chunks) - 1)
                            if chunks[i + 1].get("breath")][:2]
        else:
            breath_slots = sorted(
                [i for i in range(len(gaps) - 1) if gaps[i] >= 520],
                key=lambda i: -gaps[i])[:2]
    else:
        breath = None

    out: List[Any] = []
    for i, y in enumerate(fixed):
        out.append(y)
        g = gaps[i]
        if not g:
            continue
        if i in breath_slots and breath is not None:
            pre = np.zeros(int(sr * 0.12), dtype=np.float32)
            rest = max(g / 1000.0 - 0.12 - breath.size / sr, 0.08)
            out.extend([pre, breath, np.zeros(int(sr * rest), dtype=np.float32)])
        else:
            out.append(np.zeros(int(sr * g / 1000.0), dtype=np.float32))
    track = np.concatenate(out)

    bed_src = "off"
    final = track
    if bed:
        bed_x, bed_src = build_bed(residues, track.size, sr, seed)
        final = track + bed_x

    min_db, ok = qc_frames(final, sr)
    if bed and not ok:
        raise PacingSkip(f"QC failed (min_frame={min_db:.0f}dB)")

    meta = {
        "chunks": len(chunks), "chunk_emotions": emos,
        "breaths": len(breath_slots), "bed": bed_src,
        "min_frame_db": round(min_db, 1), "tempo": tempo,
        "tempo_applied": tempo_applied, "sr": sr,
        "dur_sec": round(final.size / sr, 2),
        "script": script_chunks is not None,
    }
    return float_to_wav(final, sr), meta
