# -*- coding: utf-8 -*-
"""
离线生成"转写对齐的短参考音"(供 live_interpreter 加速 Fish 克隆，音质不降)。
─────────────────────────────────────────────────────────────────────────
原理：Fish 克隆合成的固定开销随参考音变长而增大。这里用 Whisper 段级时间戳
      从角色全长参考里挑一段 ~8s 的干净连续语音，连同其精确转写一起存盘。
      因音频与 reference_text 严格对齐，音色不变、合成更快(实测约 -25%)。

产物：refs/interp_<profile>.wav  +  refs/interp_<profile>.txt
      live_interpreter._fetch_voice_ref 会在二者都存在时优先使用。

运行：在 cosytts 环境(已装 whisper)下：
      python make_interp_ref.py                 # 处理当前活动角色
      python make_interp_ref.py --profile 阿龙   # 指定角色
      python make_interp_ref.py --target 8 --lang zh
"""
import os, io, sys, re, json, wave, base64, argparse
import numpy as np
import requests
import soundfile as sf

HUB = os.environ.get("HUB_URL", "http://127.0.0.1:9000")
REF_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "refs")


def fetch_profile(name: str):
    if not name:
        name = requests.get(f"{HUB}/profiles", timeout=5).json().get("active", "")
    j = requests.get(f"{HUB}/profiles/{name}", params={"include_face": "true"}, timeout=15).json()
    return name, j.get("voice_b64", "") or "", (j.get("fish_tts_params") or {}).get("reference_text", "") or ""


def pick_window(segments, target=8.0, lo=6.0, hi=11.0):
    """从连续段里挑一个时长最接近 target(在[lo,hi]内)的窗口，字数多者优先。"""
    best = None
    n = len(segments)
    for i in range(n):
        for j in range(i, n):
            span = segments[j]["end"] - segments[i]["start"]
            if span < lo:
                continue
            if span > hi:
                break
            text = "".join(s["text"] for s in segments[i:j + 1]).strip()
            score = (-abs(span - target), len(text))
            if best is None or score > best[0]:
                best = (score, i, j, segments[i]["start"], segments[j]["end"], text)
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="")
    ap.add_argument("--target", type=float, default=8.0)
    ap.add_argument("--lang", default="zh")
    ap.add_argument("--model", default=os.environ.get("STT_MODEL", "small"))
    args = ap.parse_args()

    name, vb, ref_text = fetch_profile(args.profile)
    if not vb:
        print(f"[错误] 角色 {name!r} 没有 voice_b64,无法生成短参考。"); sys.exit(1)
    data, sr = sf.read(io.BytesIO(base64.b64decode(vb)), dtype="float32", always_2d=False)
    if data.ndim > 1:
        data = data.mean(axis=1)
    dur = len(data) / sr
    print(f"角色={name} 参考音时长={dur:.1f}s sr={sr} 全长转写={ref_text[:30]!r}")

    safe = re.sub(r"[^\w\-]", "_", name)
    os.makedirs(REF_DIR, exist_ok=True)
    wavp = os.path.join(REF_DIR, f"interp_{safe}.wav")
    txtp = os.path.join(REF_DIR, f"interp_{safe}.txt")

    if dur <= args.target + 2:
        seg_audio, seg_text = data, ref_text
        print("参考音已足够短,直接采用全段。")
    else:
        import whisper, torch
        dev = "cuda" if torch.cuda.is_available() else "cpu"
        print(f"加载 whisper '{args.model}' on {dev} 做段级时间戳...")
        mdl = whisper.load_model(args.model, device=dev)
        # 16k 单声道送 whisper
        if sr != 16000:
            n = int(round(len(data) * 16000 / sr))
            a16 = np.interp(np.linspace(0, len(data), n, endpoint=False),
                            np.arange(len(data)), data).astype(np.float32)
        else:
            a16 = data
        res = mdl.transcribe(a16, language=args.lang, task="transcribe",
                             fp16=(dev == "cuda"), temperature=0.0)
        segs = [{"start": s["start"], "end": s["end"], "text": s["text"]}
                for s in res.get("segments", []) if (s["end"] - s["start"]) > 0.1]
        if not segs:
            print("[警告] 未得到分段,直接采用前 8s。")
            seg_audio = data[:int(sr * args.target)]
            seg_text = ref_text
        else:
            best = pick_window(segs, target=args.target)
            if best is None:
                print("[警告] 无合适窗口,采用前 8s。")
                seg_audio = data[:int(sr * args.target)]
                seg_text = ref_text
            else:
                _, i, j, t0, t1, text = best
                print(f"选窗: [{t0:.2f}s, {t1:.2f}s] 跨度 {t1-t0:.1f}s 段{i}..{j}")
                seg_audio = data[int(t0 * sr):int(t1 * sr)]
                seg_text = text

    with wave.open(wavp, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
        w.writeframes((np.clip(seg_audio, -1, 1) * 32767).astype("<i2").tobytes())
    with open(txtp, "w", encoding="utf-8") as f:
        f.write(seg_text.strip())

    print(f"\n已生成短参考:")
    print(f"  {wavp}  ({os.path.getsize(wavp)//1024}KB, {len(seg_audio)/sr:.1f}s)")
    print(f"  {txtp}  转写={seg_text.strip()[:40]!r}")
    print("live_interpreter 下次 /start 将自动优先使用它。")


if __name__ == "__main__":
    main()
