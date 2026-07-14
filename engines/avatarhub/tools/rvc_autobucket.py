# -*- coding: utf-8 -*-
"""P2-RA: RVC 编号男声自动预分桶探针。

改进自「纯人工试听命名会」：9 个 CN_男声-XX号 逐一用**同一段**真人样本过
/api/rvc_assets/preview 转换（引擎冷加载 10-30s/个，命中缓存秒回），对输出做频谱量化：
质心/高频占比/倾斜/85% 滚降。RVC 保源 f0（pitch=0），音高不可分桶——**音色亮暗/厚薄**
全在谱包络里，这四个指标恰好卡住它。产出 _rvc_probe_report.json：特征 + z 分 + 建议桶 +
边界款标记（相邻距离过近 → 值得人工试听裁决的就这几对，不用 9 个全听）。
命名仍由人工按报告落 rvc_alias_map.json——机器只负责把「听感排序」变成可复核的数字。
"""
import io
import json
import math
import re
import struct
import sys
import time
import urllib.request
import wave
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
HUB = "http://127.0.0.1:9000"
OUT = Path(r"C:\模仿音色\tools\_rvc_probe_report.json")


def api(path, body=None, timeout=240):
    req = urllib.request.Request(HUB + path)
    if body is not None:
        req.data = json.dumps(body).encode("utf-8")
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def wav_features(wav_bytes: bytes) -> dict:
    """无 numpy 依赖的谱特征（Hub 同机跑，别赌探针环境有科学栈）。
    2048 窗/1024 步 Hann + 迭代 FFT；只统计有声帧（RMS 过阈值）。"""
    with wave.open(io.BytesIO(wav_bytes)) as wf:
        sr, nch, sw = wf.getframerate(), wf.getnchannels(), wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    assert sw == 2, f"只支持 16-bit wav，得到 {sw*8}-bit"
    n = len(raw) // 2
    samples = struct.unpack(f"<{n}h", raw)
    if nch > 1:                     # 多声道取平均
        samples = [sum(samples[i:i + nch]) / nch for i in range(0, n, nch)]
    x = [s / 32768.0 for s in samples]

    N, hop = 2048, 1024
    win = [0.5 - 0.5 * math.cos(2 * math.pi * i / (N - 1)) for i in range(N)]

    def fft(re_, im_):
        nn = len(re_)
        j = 0
        for i in range(1, nn):
            bit = nn >> 1
            while j & bit:
                j ^= bit; bit >>= 1
            j |= bit
            if i < j:
                re_[i], re_[j] = re_[j], re_[i]
                im_[i], im_[j] = im_[j], im_[i]
        ln = 2
        while ln <= nn:
            ang = -2 * math.pi / ln
            wr, wi = math.cos(ang), math.sin(ang)
            for i in range(0, nn, ln):
                cr, ci = 1.0, 0.0
                for k in range(ln // 2):
                    a, b = i + k, i + k + ln // 2
                    tr = re_[b] * cr - im_[b] * ci
                    ti = re_[b] * ci + im_[b] * cr
                    re_[b], im_[b] = re_[a] - tr, im_[a] - ti
                    re_[a], im_[a] = re_[a] + tr, im_[a] + ti
                    cr, ci = cr * wr - ci * wi, cr * wi + ci * wr
            ln <<= 1

    cents, rolls, hfr, airr, tilts = [], [], [], [], []
    rms_all = [math.sqrt(sum(v * v for v in x[o:o + N]) / N) for o in range(0, len(x) - N, hop)]
    gate = (sorted(rms_all)[len(rms_all) // 2] if rms_all else 0) * 0.5   # 半中位阈：静音/呼吸帧不进统计
    for fi, o in enumerate(range(0, len(x) - N, hop)):
        if rms_all[fi] < gate or rms_all[fi] < 1e-4:
            continue
        re_ = [x[o + i] * win[i] for i in range(N)]
        im_ = [0.0] * N
        fft(re_, im_)
        half = N // 2
        mag = [math.hypot(re_[i], im_[i]) for i in range(half)]
        hz = sr / N
        tot = sum(mag) + 1e-12
        cents.append(sum(mag[i] * i * hz for i in range(half)) / tot)
        acc, r85 = 0.0, 0.0
        for i in range(half):
            acc += mag[i]
            if acc >= tot * 0.85:
                r85 = i * hz
                break
        rolls.append(r85)
        e = [m * m for m in mag]
        te = sum(e) + 1e-12
        hfr.append(sum(e[int(2000 / hz):int(5000 / hz)]) / te)     # presence 2-5k：亮/糊分水岭
        airr.append(sum(e[int(5000 / hz):int(8000 / hz)]) / te)    # air 5-8k：齿音/空气感
        lo = sum(e[int(80 / hz):int(1000 / hz)]) + 1e-12
        hi = sum(e[int(1000 / hz):int(4000 / hz)]) + 1e-12
        tilts.append(10 * math.log10(hi / lo))                     # 正=亮薄，负=暗厚

    med = lambda a: sorted(a)[len(a) // 2] if a else 0.0
    return {"sr": sr, "voiced_frames": len(cents),
            "centroid_hz": round(med(cents), 1), "rolloff85_hz": round(med(rolls), 1),
            "presence_2_5k": round(med(hfr), 4), "air_5_8k": round(med(airr), 4),
            "tilt_db": round(med(tilts), 2)}


def main():
    models = api("/rvc/models")["models"]
    targets = sorted(m for m in models if re.search(r"CN_男声-\d+号\.pth$", m))
    print(f"编号男声 {len(targets)} 个：{targets}")
    rows = {}
    for m in targets:
        t0 = time.time()
        try:
            r = api("/api/rvc_assets/preview", {"id": m})
        except Exception as e:
            print(f"  ✗ {m}: 转换失败 {e}")
            continue
        import base64
        feats = wav_features(base64.b64decode(r["audio_base64"]))
        feats["convert_s"] = round(time.time() - t0, 1)
        feats["cached"] = r.get("cached", False)
        rows[m] = feats
        print(f"  ✓ {m}: 质心={feats['centroid_hz']}Hz presence={feats['presence_2_5k']} "
              f"tilt={feats['tilt_db']}dB ({feats['convert_s']}s{'·缓存' if feats['cached'] else ''})")

    if len(rows) >= 3:
        cs = [v["centroid_hz"] for v in rows.values()]
        mean = sum(cs) / len(cs)
        std = (sum((c - mean) ** 2 for c in cs) / len(cs)) ** 0.5 or 1.0
        order = sorted(rows.items(), key=lambda kv: kv[1]["centroid_hz"])
        third = max(1, len(order) // 3)
        for i, (m, v) in enumerate(order):
            v["centroid_z"] = round((v["centroid_hz"] - mean) / std, 2)
            v["bucket"] = "暗厚" if i < third else ("明亮" if i >= len(order) - third else "中性")
            v["rank_dark_to_bright"] = i + 1
        # 边界款：与相邻名次质心差 < 0.25σ → 机器分不出，值得人工试听
        for i in range(1, len(order)):
            if order[i][1]["centroid_hz"] - order[i - 1][1]["centroid_hz"] < 0.25 * std:
                order[i][1].setdefault("borderline_with", []).append(order[i - 1][0])
                order[i - 1][1].setdefault("borderline_with", []).append(order[i][0])
    OUT.write_text(json.dumps({"ts": int(time.time()), "n": len(rows), "rows": rows},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"报告 → {OUT}")


if __name__ == "__main__":
    main()
