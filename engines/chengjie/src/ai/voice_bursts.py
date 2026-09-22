"""English vocal bursts — splice real laugh / breath into cloned speech.

IndexTTS-2 on the LAN does not consume CosyVoice ``[laughter]``/``[breath]``
(those are spoken as “PLAS”). ``emo_text`` also does nothing unless the
server loaded QwenEmotion. The ear-test path is: keep the clone for words,
then insert short same-timbre burst stems at **pauses**, never mid-word.

A hush fragment peak-normalized into the line sounds like a glitch, not a
laugh — reject stems that need more than ~4× gain.
"""
from __future__ import annotations

import io
import wave
from pathlib import Path
from typing import Optional, Tuple

import numpy as np


def _read_wav(data: bytes) -> Tuple[np.ndarray, int]:
    with wave.open(io.BytesIO(data), "rb") as w:
        sr = int(w.getframerate())
        nch = int(w.getnchannels())
        sw = int(w.getsampwidth())
        raw = w.readframes(w.getnframes())
    if sw == 2:
        x = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    else:
        raise ValueError(f"unsupported sampwidth {sw}")
    if nch > 1:
        x = x.reshape(-1, nch).mean(axis=1)
    return x, sr


def _write_wav(x: np.ndarray, sr: int) -> bytes:
    y = np.clip(x, -0.98, 0.98)
    pcm = (y * 32767.0).astype(np.int16).tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm)
    return buf.getvalue()


def _resample(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if src_sr == dst_sr or x.size == 0:
        return x
    n = max(1, int(round(x.size * dst_sr / src_sr)))
    t_src = np.linspace(0.0, 1.0, x.size, endpoint=False)
    t_dst = np.linspace(0.0, 1.0, n, endpoint=False)
    return np.interp(t_dst, t_src, x).astype(np.float32)


def _envelope(x: np.ndarray, sr: int, hop_sec: float = 0.01) -> Tuple[np.ndarray, int]:
    hop = max(1, int(hop_sec * sr))
    win = max(hop, int(0.03 * sr))
    env = np.array([
        float(np.sqrt(np.mean(x[i:i + win] ** 2)))
        for i in range(0, max(1, x.size - win), hop)
    ], dtype=np.float32)
    return env, hop


def trim_burst(x: np.ndarray, sr: int, *, min_sec: float, max_sec: float,
               kind: str = "laugh") -> Optional[np.ndarray]:
    """Keep the loudest voiced window. Reject hush that would become a whoop."""
    if x.size == 0:
        return None
    env, hop = _envelope(x, sr)
    if env.size == 0:
        return None
    thr = max(0.02, float(env.max()) * 0.18)
    on = np.where(env >= thr)[0]
    if on.size == 0:
        return None
    a = on[0] * hop
    b = min(x.size, on[-1] * hop + hop)
    seg = x[a:b]
    cap = int(max_sec * sr)
    floor = int(min_sec * sr)
    if seg.size > cap:
        peak = int(np.argmax(np.abs(seg)))
        lo = max(0, peak - cap // 3)
        seg = seg[lo:lo + cap]
    if seg.size < max(8, int(0.12 * sr)):
        return None
    orig_peak = float(np.max(np.abs(seg)) or 0.0)
    # Quiet residue (the old 2.2s giggle lead-in, peak ~0.12) must not be
    # gained up into a mid-line chirp.
    min_peak = 0.18 if kind == "laugh" else 0.05
    if orig_peak < min_peak:
        return None
    target = 0.40 if kind == "laugh" else 0.22
    if target / orig_peak > 4.0:
        return None
    k = min(int(0.02 * sr), max(1, seg.size // 6))
    ramp = np.linspace(0.0, 1.0, k, dtype=np.float32)
    seg = seg.copy()
    seg[:k] *= ramp
    seg[-k:] *= ramp[::-1]
    return (seg * (target / orig_peak)).astype(np.float32)


def load_burst(path: str | Path, sr: int, *, kind: str) -> Optional[np.ndarray]:
    p = Path(path)
    if not p.is_file():
        return None
    try:
        x, src = _read_wav(p.read_bytes())
    except Exception:
        return None
    x = _resample(x, src, sr)
    if kind == "laugh":
        return trim_burst(x, sr, min_sec=0.28, max_sec=0.85, kind="laugh")
    return trim_burst(x, sr, min_sec=0.16, max_sec=0.40, kind="breath")


def find_pause(speech: np.ndarray, sr: int, *, after_sec: float = 0.55,
               before_frac: float = 0.84) -> int:
    """First energy valley after the opening phrase. Else append at the end.

    Mid-word insert at a fixed 28% is what turned the last sample into a
    glitch at ~2s.
    """
    if speech.size < int(0.5 * sr):
        return int(speech.size)
    env, hop = _envelope(speech, sr, 0.015)
    if env.size < 8:
        return int(speech.size)
    lo = max(1, int(after_sec * sr / hop))
    hi = min(env.size - 2, int(speech.size * before_frac / hop))
    if hi <= lo:
        return int(speech.size)
    voiced = float(np.median(env[env > np.percentile(env, 55)])) if env.size else 0.1
    floor = max(0.012, voiced * 0.22)
    best_i, best_score = None, 1e9
    for i in range(lo, hi):
        if env[i] > floor:
            continue
        if env[i] <= env[i - 1] and env[i] <= env[i + 1]:
            # prefer an early phrase-end valley
            score = float(env[i]) + 0.15 * ((i - lo) / max(1, hi - lo))
            if score < best_score:
                best_score, best_i = score, i
    if best_i is None:
        return int(speech.size)
    return int(best_i * hop)


def speech_onset(x: np.ndarray, sr: int) -> int:
    """Sample index of the first word. Leading silence is not a pause."""
    env, hop = _envelope(x, sr, 0.015)
    if env.size == 0:
        return 0
    thr = max(0.012, float(env.max()) * 0.12)
    on = np.where(env >= thr)[0]
    return int(on[0] * hop) if on.size else 0


def _insert(speech: np.ndarray, burst: np.ndarray, at: int, sr: int) -> np.ndarray:
    if burst.size == 0:
        return speech
    fade = min(int(0.035 * sr), burst.size // 3, max(1, speech.size // 20))
    if at >= speech.size - fade:
        # append after the last word with a short air gap
        gap = np.zeros(int(0.07 * sr), dtype=np.float32)
        return np.concatenate([speech, gap, burst]).astype(np.float32)
    at = max(fade, min(at, max(0, speech.size - fade)))
    gap = np.zeros(int(0.05 * sr), dtype=np.float32)
    left, right = speech[:at], speech[at:]
    if fade and left.size >= fade and burst.size >= fade:
        left = left.copy()
        burst = burst.copy()
        ramp = np.linspace(1.0, 0.0, fade, dtype=np.float32)
        left[-fade:] *= ramp
        burst[:fade] *= ramp[::-1]
        left[-fade:] += burst[:fade]
        burst = burst[fade:]
    return np.concatenate([left, burst, gap, right]).astype(np.float32)


def apply_vocal_bursts(
    wav_bytes: bytes,
    *,
    laugh_path: str = "",
    breath_path: str = "",
    want_laugh: bool = False,
    want_breath: bool = False,
) -> bytes:
    """Return WAV with optional laugh (at a pause) and breath (opening rest).

    Missing files / hush stems / empty speech → original bytes.
    """
    if not wav_bytes or wav_bytes[:4] != b"RIFF":
        return wav_bytes
    if not want_laugh and not want_breath:
        return wav_bytes
    try:
        speech, sr = _read_wav(wav_bytes)
    except Exception:
        return wav_bytes
    if speech.size < int(0.4 * sr):
        return wav_bytes
    out = speech
    applied = False
    if want_breath and breath_path:
        br = load_burst(breath_path, sr, kind="breath")
        if br is not None:
            # Only inside the line, at a rest the speaker actually took. The
            # old code searched from 0.04s — the leading silence before the
            # first word scored as the best valley — and fell back to a fixed
            # 60ms when it found nothing. Either way every line opened with
            # 140ms of canned breath, a 50ms gap, then the words; detached
            # from the speech like that it reads as a glitch, not a breath.
            at = find_pause(out, sr,
                            after_sec=speech_onset(out, sr) / sr + 0.25,
                            before_frac=0.45)
            if at < out.size:
                out = _insert(out, br, at, sr)
                applied = True
    if want_laugh and laugh_path:
        lg = load_burst(laugh_path, sr, kind="laugh")
        if lg is not None:
            at = find_pause(out, sr, after_sec=0.70, before_frac=0.84)
            out = _insert(out, lg, at, sr)
            applied = True
    if not applied:
        return wav_bytes
    peak = float(np.max(np.abs(out)) or 1.0)
    if peak > 0.95:
        out = out * (0.92 / peak)
    return _write_wav(out, sr)


def resolve_burst_paths(
    voice_profile: Optional[dict] = None, *, stock: bool = True,
) -> Tuple[str, str]:
    """Persona ``burst_audio.laugh/breath`` or (``stock=True``) the stock CC stems."""
    vp = voice_profile if isinstance(voice_profile, dict) else {}
    lib = vp.get("burst_audio") if isinstance(vp.get("burst_audio"), dict) else {}
    laugh = str(lib.get("laugh") or (
        "config/voice_refs/burst_laugh_src.wav" if stock else "")).strip()
    breath = str(lib.get("breath") or (
        "config/voice_refs/burst_breath_src.wav" if stock else "")).strip()
    here = Path(__file__).resolve().parents[2]  # engines/chengjie
    out = []
    for p in (laugh, breath):
        if not p:
            out.append("")
            continue
        cand = Path(p)
        if not cand.is_file():
            alt = here / p
            if alt.is_file():
                cand = alt
        out.append(str(cand) if cand.is_file() else "")
    return out[0], out[1]


def decorate_clone_wav(
    wav_bytes: bytes, *, text: str = "", emotion: str = "",
    voice_profile: Optional[dict] = None, need_text_cue: bool = False,
    own_stems_only: bool = False,
) -> bytes:
    """``need_text_cue``: only splice when the words themselves carry the cue
    (a laugh in "haha", a breath in "mmm"); the emotion label alone is not
    enough. ``own_stems_only``: skip the stock CC stems — a canned stranger's
    laugh in a cloned voice is the loudest "not her" tell; only persona
    ``burst_audio`` stems qualify."""
    laugh, breath = resolve_burst_paths(voice_profile, stock=not own_stems_only)
    want_l, want_b = burst_cues_from_text(text, emotion, need_text_cue=need_text_cue)
    return apply_vocal_bursts(
        wav_bytes, laugh_path=laugh, breath_path=breath,
        want_laugh=want_l, want_breath=want_b,
    )


_LAUGH_CUES = (
    "haha", "hehe", "lol", "lmao", "giggle", "laugh", "funny",
    "come here", "mouth", "wine",
)
_BREATH_CUES = (
    "mm", "mmm", "ahh", "aww", "sofa", "breath", "warm", "come here",
    "mouth", "wine", "pajama",
)


def burst_cues_from_text(
    text: str, emotion: str = "", *, need_text_cue: bool = False,
) -> Tuple[bool, bool]:
    """When to splice. English companion: laugh on play/heat cues; breath on mm/heat.

    Text that already carries a CosyVoice ``[laughter]`` mark or a soft-laugh
    opener (嘿/哈哈) is laughing once already — never stack a canned laugh on it.
    """
    t = str(text or "").lower()
    emo = str(emotion or "").lower()
    laugh_cue = any(k in t for k in _LAUGH_CUES)
    breath_cue = any(k in t for k in _BREATH_CUES)
    if need_text_cue:
        laugh, breath = laugh_cue, breath_cue
    else:
        laugh = laugh_cue or emo in ("playful", "happy", "excited")
        breath = breath_cue or emo in ("playful", "warm", "empathetic", "sad")
    if "[laughter]" in t or t.lstrip().startswith(("嘿", "哈哈", "呵呵", "嘻嘻")):
        laugh = False
    return laugh, breath
