# -*- coding: utf-8 -*-
"""独立复刻 hub._tryon_animate_ditto 二段策略做实测：
时装全身照 → ①原图 Ditto ②失败裁上半身×2 → 动画贴回 → 待机微动视频。"""
import io, sys, time, wave, subprocess
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import numpy as np
import requests
import cv2

BASE = Path(r"c:\模仿音色")
DITTO = "http://127.0.0.1:8096"


def silence_wav(secs: float, rate: int = 16000) -> bytes:
    n = int(rate * secs)
    noise = (np.random.default_rng(7).standard_normal(n) * 1e-4 * 32767).astype(np.int16)
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
    w.writeframes(noise.tobytes())
    w.close()
    return buf.getvalue()


def ditto_gen(img_bytes: bytes, secs: float, out: Path) -> bool:
    r = requests.post(f"{DITTO}/ditto/generate",
                      files={"audio": ("idle.wav", silence_wav(secs), "audio/wav"),
                             "face": ("t.jpg", img_bytes, "image/jpeg")},
                      data={"sampling_timesteps": 25, "crop_scale": 2.8}, timeout=240)
    if r.status_code == 200 and r.content[:100].find(b"ftyp") >= 0:
        out.write_bytes(r.content)
        return True
    print(f"    ditto {r.status_code}: {r.text[:120]}")
    return False


def main():
    src = Path(sys.argv[1])
    secs = float(sys.argv[2]) if len(sys.argv) > 2 else 4.0
    img_bytes = src.read_bytes()
    out = BASE / "logs" / f"_tryon_anim_{src.stem}.mp4"

    t0 = time.time()
    print(f"[1] 原图直跑 {src.name}")
    if ditto_gen(img_bytes, secs, out):
        print(f"[OK-1段] {out} ({time.time()-t0:.1f}s)")
        return

    print("[2] 上半身裁剪×2 重试")
    img = cv2.imdecode(np.frombuffer(img_bytes, np.uint8), cv2.IMREAD_COLOR)
    h, w = img.shape[:2]
    ch, cw = int(h * 0.45), int(w * 0.70)
    x0 = (w - cw) // 2
    crop = img[0:ch, x0:x0 + cw]
    crop2 = cv2.resize(crop, (cw * 2, ch * 2), interpolation=cv2.INTER_CUBIC)
    okb, buf = cv2.imencode(".jpg", crop2, [cv2.IMWRITE_JPEG_QUALITY, 92])
    anim_crop = out.with_name(out.stem + "_crop.mp4")
    if not ditto_gen(buf.tobytes(), secs, anim_crop):
        print("[FAIL] 二段也失败（脸被画幅裁断？）")
        return

    okj, jbuf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 95])
    full_jpg = out.with_name(out.stem + "_full.jpg")
    full_jpg.write_bytes(jbuf.tobytes())
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    vf = (f"[1:v]scale={cw}:{ch}[a];[0:v][a]overlay={x0}:0:shortest=1,"
          f"scale=trunc(iw/2)*2:trunc(ih/2)*2")
    proc = subprocess.run(
        [ffmpeg, "-y", "-loop", "1", "-i", str(full_jpg), "-i", str(anim_crop),
         "-filter_complex", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-r", "25", str(out)], capture_output=True, timeout=120)
    if proc.returncode != 0:
        print("[FAIL] ffmpeg:", (proc.stderr or b"")[-300:])
        return
    print(f"[OK-2段] {out} ({time.time()-t0:.1f}s)")

    # 动作分数 + 中帧留档
    cap = cv2.VideoCapture(str(out))
    prev, diffs, n = None, [], 0
    mid = None
    while True:
        ok, f = cap.read()
        if not ok:
            break
        n += 1
        if n == 50:
            mid = f.copy()
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32)
        if prev is not None:
            diffs.append(float(np.abs(g - prev).mean()))
        prev = g
    cap.release()
    print(f"[motion] frames={n} mean={np.mean(diffs):.3f} max={np.max(diffs):.3f}")
    if mid is not None:
        okm, mbuf = cv2.imencode(".jpg", mid, [cv2.IMWRITE_JPEG_QUALITY, 90])
        (out.with_name(out.stem + "_mid.jpg")).write_bytes(mbuf.tobytes())
        print(f"[mid] -> {out.with_name(out.stem + '_mid.jpg')}")


if __name__ == "__main__":
    main()
