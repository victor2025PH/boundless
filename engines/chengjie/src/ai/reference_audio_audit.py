"""参考音质量审计 — Phase E「换带情绪起伏的参考音」的前置工具。

为什么需要：克隆音色的上限由参考音决定——参考音本身**韵律平淡**（念稿式录音），
CosyVoice3 zero_shot 会忠实复刻这份平淡，Phase C 的采样方差救不回来（方差是
「同一份韵律基准附近的抖动」，基准平则怎么抖都平）。运营要换参考音，先得知道
**哪条差、差在哪**：本模块对每条参考音出确定性体检报告（时长/采样率/削波/静音/
能量动态/音高动态/逐字稿 sidecar），把「凭耳朵猜」变成「按报告换」。

设计（与 prosody_scorer 同族但**本仓独立实现**——CI 无 D:/faceX/mfys 也可单测）：
  - 纯函数 + numpy（faster-whisper 传递依赖，生产必有）；任何脏输入 → ok=False。
  - 指标刻度对齐集群 prosody_scorer 的真人基准（f0_semi_std≈5.9 / energy_db_std≈11.9），
    但审计阈值取宽松档（参考音短、内容单一，天然低于长对话基准）。
  - 只审 WAV（7852 契约就是 WAV 参考音）；非 WAV/坏文件如实报错不猜。

可单测纯函数：analyze_wav_bytes / classify_reference / audit_reference_file。
CLI 壳：scripts/reference_audio_audit.py（收集人设 → 报告 + JSON 产物）。
"""
from __future__ import annotations

import io
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ── 判级阈值（宽松档：参考音 3~10s 短素材，别用长对话刻度硬卡）────────────────
DUR_MIN_SEC = 3.0            # 过短：说话人特征不够稳
DUR_MAX_SEC = 12.0           # 过长：7852 只吃前段，特征被稀释
SR_MIN = 16000               # 采样率下限（再低音色细节丢失）
CLIP_RATIO_WARN = 0.001      # 削波样本占比 >0.1% = 录音爆音
SILENCE_EDGE_WARN = 1.5      # 首/尾静音 > 1.5s = 浪费有效时长
ENERGY_STD_FLAT = 6.0        # 能量动态 dB std < 此值 = 念稿感（真人基准 ~11.9）
F0_STD_FLAT = 2.5            # 音高半音 std < 此值 = 单调（真人基准 ~5.9）
VOICED_RATIO_LOW = 0.35      # 浊音比过低 = 静音/气声过多，有效语音少


def _decode_wav(raw: bytes) -> Tuple[Any, int, int, int]:
    """WAV bytes → (float32 mono ndarray, sample_rate, channels, sampwidth)。失败抛。"""
    import numpy as np

    with wave.open(io.BytesIO(raw), "rb") as w:
        nch, sw, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
        frames = w.readframes(w.getnframes())
    if sw == 2:
        a = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        a = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif sw == 1:
        a = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"unsupported sample width {sw}")
    if nch > 1:
        a = a.reshape(-1, nch).mean(axis=1)
    return a, sr, nch, sw


def _f0_energy(a: Any, sr: int, *, fmin: int = 75, fmax: int = 400,
               frame: float = 0.04, hop: float = 0.01) -> Tuple[Any, Any]:
    """帧级 F0（自相关法）与 RMS 能量（与集群 prosody_scorer 同法，独立实现）。"""
    import numpy as np

    fl = int(frame * sr)
    hp = int(hop * sr)
    lo = max(1, int(sr / fmax))
    hi = int(sr / fmin)
    f0: List[float] = []
    en: List[float] = []
    for i in range(0, max(1, len(a) - fl), hp):
        x = a[i:i + fl].astype(np.float64)
        e = float(np.sqrt(np.mean(x ** 2) + 1e-9))
        en.append(e)
        if e < 0.01:
            f0.append(0.0)
            continue
        x = x - x.mean()
        ac = np.correlate(x, x, mode="full")[len(x) - 1:]
        if ac[0] <= 0 or len(ac) <= hi:
            f0.append(0.0)
            continue
        seg = ac[lo:hi]
        if len(seg) < 2:
            f0.append(0.0)
            continue
        k = int(np.argmax(seg)) + lo
        r = ac[k] / ac[0]
        f0.append(sr / k if r > 0.3 else 0.0)
    return np.array(f0), np.array(en)


def analyze_wav_bytes(raw: bytes) -> Dict[str, Any]:
    """参考音 WAV → 体检指标 dict。任何异常 → ``{"ok": False, "detail": ...}``。

    指标：duration_sec / sample_rate / channels / clip_ratio /
    lead_silence_sec / trail_silence_sec / energy_db_std / f0_semi_std /
    voiced_ratio / peak。
    """
    import numpy as np

    try:
        a, sr, nch, _sw = _decode_wav(raw or b"")
    except Exception as exc:
        return {"ok": False, "detail": f"decode failed: {exc}"}
    if len(a) < sr * 0.2:
        return {"ok": False, "detail": "audio too short (<0.2s)"}

    dur = len(a) / sr
    peak = float(np.max(np.abs(a))) if len(a) else 0.0
    # 削波：|sample| ≥ 0.999 视为触顶（int16 转 float 后的满幅）
    clip_ratio = float(np.mean(np.abs(a) >= 0.999)) if len(a) else 0.0

    # 首尾静音：10ms 窗 RMS < -40dBFS 连续段
    win = max(1, int(sr * 0.01))
    n_win = len(a) // win
    rms = np.sqrt(np.mean(
        a[: n_win * win].reshape(n_win, win) ** 2, axis=1) + 1e-12)
    silent = rms < 10 ** (-40 / 20)
    lead = 0
    for s in silent:
        if not s:
            break
        lead += 1
    trail = 0
    for s in silent[::-1]:
        if not s:
            break
        trail += 1

    f0, en = _f0_energy(a, sr)
    voiced = f0[f0 > 0]
    voiced_ratio = float(len(voiced) / max(1, len(f0)))
    if len(voiced) > 3:
        semi = 12 * np.log2(voiced / np.median(voiced) + 1e-9)
        f0_std = float(np.std(semi))
    else:
        f0_std = 0.0
    edb = 20 * np.log10(en + 1e-6)
    energy_std = float(np.std(edb))

    return {
        "ok": True,
        "duration_sec": round(dur, 2),
        "sample_rate": int(sr),
        "channels": int(nch),
        "peak": round(peak, 4),
        "clip_ratio": round(clip_ratio, 5),
        "lead_silence_sec": round(lead * win / sr, 2),
        "trail_silence_sec": round(trail * win / sr, 2),
        "energy_db_std": round(energy_std, 2),
        "f0_semi_std": round(f0_std, 2),
        "voiced_ratio": round(voiced_ratio, 3),
    }


def classify_reference(
    metrics: Dict[str, Any], *, has_sidecar: bool,
) -> Dict[str, Any]:
    """体检指标 → ``{"level": ok|warn|bad, "issues": [...], "tips": [...]}``。纯函数。

    bad＝分析失败/根本不可用；warn＝可用但有明确改进项；ok＝素材健康。
    tips 是给运营的动作句（换什么样的录音），不是指标复读。
    """
    if not metrics or not metrics.get("ok"):
        return {
            "level": "bad",
            "issues": [f"文件不可分析：{(metrics or {}).get('detail', 'unknown')}"],
            "tips": ["确认是 PCM WAV（16bit 单声道 3~10 秒），重新导出后再试"],
        }
    issues: List[str] = []
    tips: List[str] = []

    dur = float(metrics.get("duration_sec") or 0)
    if dur < DUR_MIN_SEC:
        issues.append(f"时长 {dur}s 过短（<{DUR_MIN_SEC}s）")
        tips.append("换 5~10 秒的完整连续说话片段，特征更稳")
    elif dur > DUR_MAX_SEC:
        issues.append(f"时长 {dur}s 过长（>{DUR_MAX_SEC}s）")
        tips.append("裁剪到最有表现力的 5~10 秒（引擎只吃前段）")

    if int(metrics.get("sample_rate") or 0) < SR_MIN:
        issues.append(f"采样率 {metrics.get('sample_rate')} 过低（<{SR_MIN}）")
        tips.append("用 ≥16k（推荐 24k/48k）采样率重新导出")

    if float(metrics.get("clip_ratio") or 0) > CLIP_RATIO_WARN:
        issues.append(f"削波 {float(metrics.get('clip_ratio')) * 100:.2f}%（录音爆音）")
        tips.append("降低录音增益重录：爆音会被克隆成破音底色")

    lead = float(metrics.get("lead_silence_sec") or 0)
    trail = float(metrics.get("trail_silence_sec") or 0)
    if lead > SILENCE_EDGE_WARN or trail > SILENCE_EDGE_WARN:
        issues.append(f"首尾静音过长（头 {lead}s / 尾 {trail}s）")
        tips.append("裁掉首尾静音，把有效时长留给真实语音")

    if float(metrics.get("voiced_ratio") or 0) < VOICED_RATIO_LOW:
        issues.append(f"浊音比 {metrics.get('voiced_ratio')} 过低（有效语音太少）")
        tips.append("换一段连续说话（少停顿/气声）的素材")

    # Phase E 核心：韵律平淡的参考音 = 克隆声天然「念稿」，采样方差救不回
    flat_energy = float(metrics.get("energy_db_std") or 0) < ENERGY_STD_FLAT
    flat_f0 = float(metrics.get("f0_semi_std") or 0) < F0_STD_FLAT
    if flat_energy and flat_f0:
        issues.append(
            f"韵律平淡（能量std {metrics.get('energy_db_std')}dB + "
            f"音高std {metrics.get('f0_semi_std')}semi 双低）")
        tips.append("换一段带情绪起伏的自然聊天录音（讲故事/惊喜/大笑），"
                    "别用念稿式朗读——克隆声的活人感上限就是参考音的活人感")
    elif flat_energy:
        issues.append(f"能量动态平（std {metrics.get('energy_db_std')}dB）")
        tips.append("选轻重缓急更明显的片段（有强调、有收尾）")
    elif flat_f0:
        issues.append(f"音高动态平（std {metrics.get('f0_semi_std')}semi）")
        tips.append("选语调起伏更大的片段（疑问/感叹句比陈述句好）")

    if not has_sidecar:
        issues.append("缺逐字稿 sidecar（同名 .txt）")
        tips.append("补上参考音的逐字稿：保真路径与混合情感路径都依赖它，"
                    "缺失时音色相似度显著下降")

    return {
        "level": "warn" if issues else "ok",
        "issues": issues,
        "tips": tips,
    }


def pick_best_segment(
    a: Any, sr: int, *, target_sec: float = 8.0, step_sec: float = 0.5,
) -> Tuple[float, float]:
    """在整段音频里选「韵律最丰富」的 target_sec 窗口。返回 (start_sec, end_sec)。

    评分 = 音高动态/F0_STD_FLAT + 能量动态/ENERGY_STD_FLAT（双指标归一等权）——
    与审计判「平淡」的刻度同源，选出来的段落天然过审。步骤：
      ① 去首尾静音（audit 抓的头部静音直接消掉）；
      ② 有效长度 ≤ target → 整段返回；
      ③ 滑窗打分取最优，窗沿向邻近静音帧吸附（≤0.4s）避免切在字中间。
    纯函数（numpy in / 秒 out），供 CLI 与门禁单测。
    """
    import numpy as np

    n = len(a)
    if n <= 0 or sr <= 0:
        return 0.0, 0.0
    win = max(1, int(sr * 0.01))
    n_win = n // win
    if n_win < 2:
        return 0.0, n / sr
    rms = np.sqrt(np.mean(
        a[: n_win * win].reshape(n_win, win) ** 2, axis=1) + 1e-12)
    silent = rms < 10 ** (-40 / 20)
    first = 0
    while first < n_win and silent[first]:
        first += 1
    last = n_win
    while last > first and silent[last - 1]:
        last -= 1
    if first >= last:
        return 0.0, min(n / sr, float(target_sec))
    eff_start = first * win / sr
    eff_end = last * win / sr
    if eff_end - eff_start <= target_sec:
        return round(eff_start, 2), round(eff_end, 2)

    def _score(seg: Any) -> float:
        f0, en = _f0_energy(seg, sr)
        voiced = f0[f0 > 0]
        if len(voiced) > 3:
            semi = 12 * np.log2(voiced / np.median(voiced) + 1e-9)
            f0_std = float(np.std(semi))
        else:
            f0_std = 0.0
        edb = 20 * np.log10(en + 1e-6)
        return (f0_std / F0_STD_FLAT) + (float(np.std(edb)) / ENERGY_STD_FLAT)

    best_start, best_score = eff_start, -1.0
    t = eff_start
    while t + target_sec <= eff_end + 1e-6:
        seg = a[int(t * sr): int((t + target_sec) * sr)]
        s = _score(seg)
        if s > best_score:
            best_score, best_start = s, t
        t += max(0.1, float(step_sec))

    def _snap(sec: float, *, direction: int) -> float:
        """把边沿吸附到 ≤0.4s 内最近的静音帧（direction=-1 向前 / +1 向后）。"""
        idx = int(sec * sr / win)
        for off in range(int(0.4 * sr / win)):
            j = idx + direction * off
            if 0 <= j < n_win and silent[j]:
                return j * win / sr
        return sec

    s0 = _snap(best_start, direction=-1)
    s1 = _snap(best_start + target_sec, direction=1)
    s1 = min(s1, eff_end)
    return round(max(eff_start, s0), 2), round(s1, 2)


def write_wav_mono(a: Any, sr: int, out_path: str) -> None:
    """float32 [-1,1] → 16bit 单声道 PCM WAV（裁剪产物写盘）。"""
    import numpy as np

    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(a, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())


def audit_reference_file(ref_path: str) -> Dict[str, Any]:
    """单文件端到端审计：读盘 → analyze → classify。绝不抛。"""
    p = Path(str(ref_path or ""))
    if not p.is_file():
        return {
            "ref": str(ref_path), "level": "bad",
            "issues": ["参考音文件不存在"], "tips": ["检查 voice_profile.reference_audio_path"],
            "metrics": {},
        }
    try:
        raw = p.read_bytes()
    except Exception as exc:
        return {
            "ref": str(ref_path), "level": "bad",
            "issues": [f"读文件失败：{exc}"], "tips": [], "metrics": {},
        }
    metrics = analyze_wav_bytes(raw)
    has_sidecar = False
    try:
        from src.ai.avatar_voice import find_reference_text
        has_sidecar = bool(find_reference_text(str(p)))
    except Exception:
        has_sidecar = p.with_suffix(".txt").is_file()
    verdict = classify_reference(metrics, has_sidecar=has_sidecar)
    return {
        "ref": str(ref_path),
        "level": verdict["level"],
        "issues": verdict["issues"],
        "tips": verdict["tips"],
        "metrics": metrics if metrics.get("ok") else {},
        "has_sidecar": has_sidecar,
    }
