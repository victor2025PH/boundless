# -*- coding: utf-8 -*-
"""_dual_lipsync_bench.py — 双副本口型并发实测（临时脚本）。
生成 3s 测试音频（正弦扫频，MuseTalk 只看音频特征不管内容），用同一张人脸图
分别做：A) 单卡串行两次（基线）；B) 双卡并行各一次。
若 B 的墙钟时间 ≈ 单次耗时（而非 2 倍），即证明第二张卡真实分担渲染。
结果追加到 logs/optimize_20260705/dual_lipsync.json
"""
import concurrent.futures as cf
import io
import json
import struct
import time
import wave

import requests

LOCAL = "http://127.0.0.1:8090"
REMOTE = "http://192.168.0.198:8090"
FACE = "_ldh720.jpg"
OUT = "logs/optimize_20260705/dual_lipsync.json"


def make_wav(seconds=3.0, sr=16000):
    import math
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        n = int(seconds * sr)
        frames = bytearray()
        for i in range(n):
            t = i / sr
            # 200→800Hz 扫频 + 4Hz 包络，近似语音能量起伏
            f = 200 + 600 * (i / n)
            env = 0.5 * (1 + math.sin(2 * math.pi * 4 * t))
            v = int(12000 * env * math.sin(2 * math.pi * f * t))
            frames += struct.pack("<h", v)
        w.writeframes(bytes(frames))
    return buf.getvalue()


def gen(base, wav, face_bytes, tag):
    t0 = time.time()
    r = requests.post(f"{base}/lipsync/generate",
                      files={"audio": ("t.wav", wav, "audio/wav"),
                             "face": ("f.jpg", face_bytes, "image/jpeg")},
                      data={"fps": 25, "batch_size": 8}, timeout=300)
    dt = time.time() - t0
    ok = r.status_code == 200 and len(r.content) > 10000
    print(f"[{tag}] {base} -> {r.status_code} {len(r.content)}B in {dt:.1f}s", flush=True)
    return {"base": base, "tag": tag, "ok": ok, "s": round(dt, 1), "bytes": len(r.content)}


def main():
    wav = make_wav()
    face = open(FACE, "rb").read()
    print("== warm both replicas (first call includes face detect) ==", flush=True)
    w1 = gen(LOCAL, wav, face, "warm-local")
    w2 = gen(REMOTE, wav, face, "warm-remote")

    print("== A) serial x2 on local (baseline) ==", flush=True)
    t0 = time.time()
    a1 = gen(LOCAL, wav, face, "serial-1")
    a2 = gen(LOCAL, wav, face, "serial-2")
    serial_wall = time.time() - t0

    print("== B) parallel: local + remote ==", flush=True)
    t0 = time.time()
    with cf.ThreadPoolExecutor(2) as ex:
        f1 = ex.submit(gen, LOCAL, wav, face, "par-local")
        f2 = ex.submit(gen, REMOTE, wav, face, "par-remote")
        b1, b2 = f1.result(), f2.result()
    par_wall = time.time() - t0

    res = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
           "warm": [w1, w2], "serial": [a1, a2, {"wall_s": round(serial_wall, 1)}],
           "parallel": [b1, b2, {"wall_s": round(par_wall, 1)}],
           "speedup": round(serial_wall / par_wall, 2) if par_wall else None}
    print(f"== serial wall {serial_wall:.1f}s vs parallel wall {par_wall:.1f}s "
          f"(speedup x{res['speedup']}) ==", flush=True)
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(res, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
