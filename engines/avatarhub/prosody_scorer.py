# -*- coding: utf-8 -*-
"""自然度（韵律）评分：与 clone_scorer 的音色相似度互补。

cosine 量「音色像不像同一人」，本模块量「说得像不像活人」——
基于 F0 音高动态(半音 std)、能量起伏(dB std)、浊音比，对照真人参考音的韵律丰富度。
仅依赖 numpy + 标准库（F0 用自相关法），无 librosa/torch。
"""
import io
import wave
import base64

import numpy as np


def _b64_to_bytes(s: str) -> bytes:
    if "," in s and s.strip().startswith("data:"):
        s = s.split(",", 1)[1]
    return base64.b64decode(s)


def _decode_wav(raw: bytes):
    with wave.open(io.BytesIO(raw), "rb") as w:
        nch, sw, sr = w.getnchannels(), w.getsampwidth(), w.getframerate()
        fr = w.readframes(w.getnframes())
    if sw == 2:
        a = np.frombuffer(fr, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        a = np.frombuffer(fr, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif sw == 1:
        a = (np.frombuffer(fr, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"unsupported width {sw}")
    if nch > 1:
        a = a.reshape(-1, nch).mean(axis=1)
    return a, sr


def _f0_energy(a, sr, fmin=75, fmax=400, frame=0.04, hop=0.01):
    fl = int(frame * sr)
    hp = int(hop * sr)
    lo = max(1, int(sr / fmax))
    hi = int(sr / fmin)
    f0, en = [], []
    for i in range(0, max(1, len(a) - fl), hp):
        x = a[i:i + fl].astype(np.float64)
        e = np.sqrt(np.mean(x ** 2) + 1e-9)
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


def extract_prosody(wav_b64: str) -> dict:
    """音频 → 韵律特征。"""
    a, sr = _decode_wav(_b64_to_bytes(wav_b64))
    if len(a) < sr * 0.2:
        return {"ok": False, "detail": "音频过短"}
    f0, en = _f0_energy(a, sr)
    voiced = f0[f0 > 0]
    voiced_ratio = len(voiced) / max(1, len(f0))
    if len(voiced) > 3:
        semi = 12 * np.log2(voiced / np.median(voiced) + 1e-9)
        f0_std = float(np.std(semi))
        f0_range = float(np.percentile(semi, 95) - np.percentile(semi, 5))
    else:
        f0_std = 0.0
        f0_range = 0.0
    edb = 20 * np.log10(en + 1e-6)
    en_std = float(np.std(edb))
    return {"ok": True, "f0_semi_std": round(f0_std, 3), "f0_range": round(f0_range, 3),
            "energy_db_std": round(en_std, 3), "voiced_ratio": round(voiced_ratio, 3)}


# 真人参考韵律基准（由刘德华母带+窗口实测均值；可被 set_reference 覆盖）
_HUMAN_REF = {"f0_semi_std": 5.93, "energy_db_std": 11.85, "voiced_ratio": 0.671}


def set_reference(stats: dict):
    """用某角色真人参考音的实测韵律替换默认基准。"""
    global _HUMAN_REF
    for k in ("f0_semi_std", "energy_db_std", "voiced_ratio"):
        if k in stats and stats[k]:
            _HUMAN_REF[k] = stats[k]


def naturalness_score(synth_b64: str, reference_b64: str = "") -> dict:
    """自然度 0~1：合成音韵律丰富度对照真人基准（达到/超过真人计满分）。

    若给 reference_b64，则以该参考音的实测韵律为目标，否则用内置真人基准。
    """
    try:
        ref = _HUMAN_REF
        if reference_b64:
            rp = extract_prosody(reference_b64)
            if rp.get("ok"):
                ref = {k: rp[k] for k in ("f0_semi_std", "energy_db_std", "voiced_ratio")}
        pr = extract_prosody(synth_b64)
        if not pr.get("ok"):
            return {"ok": False, "detail": pr.get("detail", "韵律提取失败")}
        # 各项：达到参考即满分，过低按比例扣分（封顶 1）
        s_f0 = min(1.0, pr["f0_semi_std"] / max(1e-6, ref["f0_semi_std"]))
        s_en = min(1.0, pr["energy_db_std"] / max(1e-6, ref["energy_db_std"]))
        # 浊音比偏离参考越大越扣（双向）
        s_vr = max(0.0, 1.0 - abs(pr["voiced_ratio"] - ref["voiced_ratio"]) / 0.4)
        score = 0.5 * s_f0 + 0.35 * s_en + 0.15 * s_vr
        score = round(float(score), 4)
        if score >= 0.85:
            label = "自然"
        elif score >= 0.70:
            label = "较自然"
        elif score >= 0.55:
            label = "略平"
        else:
            label = "偏平淡"
        return {"ok": True, "naturalness": score, "label": label,
                "prosody": pr, "reference": ref,
                "parts": {"f0": round(s_f0, 3), "energy": round(s_en, 3), "voiced": round(s_vr, 3)}}
    except Exception as e:
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}
