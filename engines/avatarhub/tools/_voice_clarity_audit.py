# -*- coding: utf-8 -*-
"""角色库参考音清晰度体检：逐个拉取 profile 的 voice_b64，量四个维度并打总分。
维度：信噪比(说话帧p90 - 底噪帧p10)、有效频宽(高频细节=清晰度上限)、削波率、时长。
用途：挑最干净的参考音换掉当前克隆音色(参考音脏 → 克隆必然含混)。
"""
import base64, io, json, sys, urllib.parse, urllib.request
import numpy as np

try:
    import soundfile as sf
except ImportError:
    sf = None
import wave

HUB = "http://127.0.0.1:9000"


def fetch(name):
    url = f"{HUB}/profiles/{urllib.parse.quote(name)}?include_face=true"
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.load(r)


def decode_audio(raw: bytes):
    """尽量解出 (mono float32, sr)。优先 soundfile；退回 stdlib wave。"""
    if sf is not None:
        try:
            data, sr = sf.read(io.BytesIO(raw), dtype="float32", always_2d=True)
            return data.mean(axis=1), sr
        except Exception:
            pass
    with wave.open(io.BytesIO(raw)) as w:
        sr = w.getframerate()
        n = w.getnframes()
        sw = w.getsampwidth()
        pcm = w.readframes(n)
        dt = {1: np.int8, 2: np.int16, 4: np.int32}[sw]
        x = np.frombuffer(pcm, dtype=dt).astype(np.float32) / float(2 ** (8 * sw - 1))
        if w.getnchannels() > 1:
            x = x.reshape(-1, w.getnchannels()).mean(axis=1)
        return x, sr


def frame_rms_db(x, sr, win=0.03, hop=0.01):
    w = max(1, int(sr * win)); h = max(1, int(sr * hop))
    if x.size < w:
        return np.array([20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9)])
    n = 1 + (x.size - w) // h
    idx = np.arange(w)[None, :] + h * np.arange(n)[:, None]
    fr = x[idx]
    return 20 * np.log10(np.sqrt(np.mean(fr ** 2, axis=1)) + 1e-9)


def bandwidth_khz(x, sr):
    """平均谱上,相对峰值 -50dB 仍有能量的最高频率(kHz)——8k 上采样/闷录音会现形。"""
    nfft = 4096
    if x.size < nfft:
        x = np.pad(x, (0, nfft - x.size))
    n = x.size // nfft
    spec = np.abs(np.fft.rfft(x[: n * nfft].reshape(n, nfft) * np.hanning(nfft), axis=1)).mean(axis=0)
    db = 20 * np.log10(spec + 1e-9)
    db -= db.max()
    freqs = np.fft.rfftfreq(nfft, 1 / sr)
    keep = np.where(db > -50)[0]
    return float(freqs[keep[-1]] / 1000) if keep.size else 0.0


def audit(name):
    j = fetch(name)
    vb = j.get("voice_b64") or ""
    if not vb:
        return None
    raw = base64.b64decode(vb)
    x, sr = decode_audio(raw)
    if x.size < sr * 0.5:
        return None
    dur = x.size / sr
    db = frame_rms_db(x, sr)
    voiced = db[db > db.max() - 35]                      # 有声帧(排除长静音)
    speech = float(np.percentile(voiced, 90))
    floor = float(np.percentile(db, 10))
    snr = speech - floor
    clip = float(np.mean(np.abs(x) > 0.985)) * 100
    bw = bandwidth_khz(x, sr)
    # 总分：SNR 主导(封顶45)，频宽次之(封顶16k)，削波重罚，时长 6~30s 最佳
    s_snr = min(snr, 45.0) / 45.0 * 50
    s_bw = min(bw, 16.0) / 16.0 * 30
    s_clip = -min(clip * 20, 20)
    s_dur = 20 if 6 <= dur <= 40 else (10 if 3 <= dur < 6 or 40 < dur <= 90 else 0)
    score = s_snr + s_bw + s_clip + s_dur
    return dict(name=name, dur=round(dur, 1), sr=sr, kb=len(raw) // 1024,
                speech_db=round(speech, 1), floor_db=round(floor, 1),
                snr=round(snr, 1), clip=round(clip, 3), bw=round(bw, 1),
                score=round(score, 1))


def main():
    with urllib.request.urlopen(f"{HUB}/profiles", timeout=15) as r:
        plist = json.load(r)["profiles"]
    rows = []
    for p in plist:
        if not p.get("has_voice"):
            continue
        try:
            row = audit(p["name"])
            if row:
                qa = p.get("quality_axes") or {}
                row["nat"] = qa.get("naturalness")
                row["cos"] = qa.get("cosine")
                rows.append(row)
            else:
                print(f"SKIP {p['name']}: 无参考音或太短", flush=True)
        except Exception as e:
            print(f"FAIL {p['name']}: {repr(e)[:80]}", flush=True)
    rows.sort(key=lambda r: -r["score"])
    hdr = f"{'音色':<8} {'总分':>5} {'信噪比':>6} {'底噪dB':>7} {'频宽kHz':>7} {'削波%':>6} {'时长s':>6} {'采样率':>6} {'自然度':>6} {'相似度':>6}"
    print(hdr, flush=True)
    for r in rows:
        print(f"{r['name']:<8} {r['score']:>5} {r['snr']:>6} {r['floor_db']:>7} {r['bw']:>7} "
              f"{r['clip']:>6} {r['dur']:>6} {r['sr']:>6} "
              f"{(r['nat'] if r['nat'] is not None else '-'):>6} {(r['cos'] if r['cos'] is not None else '-'):>6}", flush=True)
    print("BEST=" + (rows[0]["name"] if rows else "NONE"), flush=True)


if __name__ == "__main__":
    main()
