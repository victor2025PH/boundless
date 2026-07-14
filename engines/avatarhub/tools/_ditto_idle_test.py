# -*- coding: utf-8 -*-
"""Ditto 静默驱动待机微动实测：静音 WAV + 人脸图 → mp4，量化微动幅度。

验证两件事：
  1) 纯静音输入下 Ditto 是否产生自然微动（呼吸/眨眼/头部微摆）——tryon_preset
     animate=auto 路径的核心假设；
  2) crop_scale 对构图的影响（试衣场景要尽量保留服装上身视野）。

指标：逐帧灰度 absdiff 均值（motion score）。经验阈值：
  <0.05 静态图级别（假设不成立）；0.1~1.5 自然微动；>3 动作过大（不适合待机）。
用法: python tools/_ditto_idle_test.py [face_path] [secs] [crop_scale]
"""
import sys, io, time, wave
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import requests
import numpy as np
import cv2

BASE = Path(r"c:\模仿音色")
DITTO = "http://127.0.0.1:8096"


def silence_wav(secs: float, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
    w.writeframes(b"\x00\x00" * int(rate * secs))
    w.close()
    return buf.getvalue()


def motion_stats(mp4_path: Path) -> dict:
    cap = cv2.VideoCapture(str(mp4_path))
    prev, diffs, n = None, [], 0
    w = h = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        n += 1
        h, w = frame.shape[:2]
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)
        if prev is not None:
            diffs.append(float(np.abs(g - prev).mean()))
        prev = g
    cap.release()
    if not diffs:
        return {"frames": n, "error": "no frames"}
    d = np.array(diffs)
    return {"frames": n, "size": f"{w}x{h}",
            "motion_mean": round(float(d.mean()), 3),
            "motion_max": round(float(d.max()), 3),
            "motion_p90": round(float(np.percentile(d, 90)), 3)}


def main():
    face_path = Path(sys.argv[1]) if len(sys.argv) > 1 else BASE / "faces" / "刘德华.jpg"
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 2.5
    crop = float(sys.argv[3]) if len(sys.argv) > 3 else 2.8

    face_bytes = face_path.read_bytes()
    print(f"[test] face={face_path.name} ({len(face_bytes)//1024}KB) secs={secs} crop_scale={crop}")

    t0 = time.time()
    r = requests.post(
        f"{DITTO}/ditto/generate",
        files={"audio": ("idle.wav", silence_wav(secs), "audio/wav"),
               "face": ("face.jpg", face_bytes, "image/jpeg")},
        data={"sampling_timesteps": 25, "crop_scale": crop},
        timeout=240)
    el = time.time() - t0
    if r.status_code != 200:
        print(f"[test] FAIL http={r.status_code} {r.text[:300]}")
        sys.exit(1)
    out = BASE / "logs" / f"ditto_idle_{face_path.stem}_c{crop}.mp4"
    out.write_bytes(r.content)
    print(f"[test] OK {len(r.content)//1024}KB in {el:.1f}s -> {out}")

    st = motion_stats(out)
    print(f"[test] motion: {st}")
    # 抽首帧存图供人工查看构图
    cap = cv2.VideoCapture(str(out))
    ok, frame = cap.read()
    cap.release()
    if ok:
        fp = BASE / "logs" / f"ditto_idle_{face_path.stem}_c{crop}_f0.jpg"
        ok2, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if ok2:
            fp.write_bytes(buf.tobytes())
            print(f"[test] first frame -> {fp}")


if __name__ == "__main__":
    main()
