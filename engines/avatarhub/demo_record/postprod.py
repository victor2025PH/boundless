# -*- coding: utf-8 -*-
"""录屏后期流水线:去 Bandicam 水印 → 选段拼接 → 字幕 → 品牌尾卡 → 1080p 成片。

用法(facefusion 环境 python):
  python demo_record/postprod.py --in out/voice_take3_raw.mp4 --out out/voice_clean.mp4
  # 精剪 + 字幕 + 尾卡:
  python demo_record/postprod.py --in raw.mp4 --out final.mp4 \
      --keep 0-12,18.5-40,61-90 --caption "0,6,同一把克隆声音 · 多种情感" --endcard

说明:
  - 水印框是 1920x1080 全屏录制下 Bandicam 未注册水印的实测坐标(x782 y2 w300 h38)。
  - --keep 用原片秒数(去水印前后不变);段间硬切。
  - --caption 可多次,格式 "start,end,文字"(秒,基于剪完后的时间轴)。
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DELOGO = "delogo=x=776:y=2:w=344:h=38"
FONT = "C\\:/Windows/Fonts/msyh.ttc"
BRAND = "无界 BOUNDLESS"
SITE = "usdt2026.cc"


def run(cmd):
    print(">", " ".join(cmd))
    subprocess.run(cmd, check=True)


def _ffprobe_dur(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", path], capture_output=True, text=True)
    return float(out.stdout.strip())


def auto_keep(path, noise_db=-40, min_sil=1.5, pad=0.35, drop_gap=2.0):
    """跑 silencedetect,把非静音区间合并成 keep 窗口(带 pad);仅当静音≥drop_gap 才切断。
    返回 [(a,b),...]。用于自动剔除录制里的轮询空档。"""
    import re
    out = subprocess.run(["ffmpeg", "-i", path, "-vn", "-af",
                          f"silencedetect=noise={noise_db}dB:d={min_sil}", "-f", "null", "NUL"],
                         capture_output=True, text=True)
    log = out.stderr
    starts = [float(m) for m in re.findall(r"silence_start:\s*([-\d.]+)", log)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*([\d.]+)", log)]
    dur = _ffprobe_dur(path)
    # 构造静音区间,只保留够长(≥drop_gap)的作为切割点
    sil = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else dur
        if e - s >= drop_gap:
            sil.append((max(0, s), min(dur, e)))
    # keep = 全片 - 长静音,首尾各留 pad
    keep = []
    cur = 0.0
    for s, e in sil:
        a, b = cur, s + pad
        if b - a > 0.5:
            keep.append((max(0, a - (pad if cur > 0 else 0)), b))
        cur = max(cur, e - pad)
    if dur - cur > 0.5:
        keep.append((cur, dur))
    # 合并相邻/重叠
    merged = []
    for a, b in keep:
        if merged and a <= merged[-1][1] + 0.1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", required=True)
    ap.add_argument("--out", dest="dst", required=True)
    ap.add_argument("--keep", default="", help="保留段列表 a-b,c-d(原片秒)")
    ap.add_argument("--caption", action="append", default=[],
                    help='字幕 "start,end,文字",可多次(成片时间轴)')
    ap.add_argument("--endcard", action="store_true", help="追加 2.5s 品牌尾卡")
    ap.add_argument("--no-delogo", action="store_true")
    ap.add_argument("--crf", type=int, default=21)
    args = ap.parse_args()

    src = os.path.abspath(args.src)
    dst = os.path.abspath(args.dst)
    os.makedirs(os.path.dirname(dst), exist_ok=True)

    vf_parts = [] if args.no_delogo else [DELOGO]

    # ── 选段 ──
    filter_complex = None
    if args.keep:
        segs = []
        for part in args.keep.split(","):
            a, b = part.split("-")
            segs.append((float(a), float(b)))
        chains = []
        for i, (a, b) in enumerate(segs):
            pre = ",".join(vf_parts) + "," if vf_parts else ""
            chains.append(
                f"[0:v]trim={a}:{b},setpts=PTS-STARTPTS,{pre}format=yuv420p[v{i}];"
                f"[0:a]atrim={a}:{b},asetpts=PTS-STARTPTS[a{i}]")
        concat_in = "".join(f"[v{i}][a{i}]" for i in range(len(segs)))
        chains.append(f"{concat_in}concat=n={len(segs)}:v=1:a=1[vc][ac]")
        filter_complex = ";".join(chains)
        v_label, a_label = "[vc]", "[ac]"

    # ── 字幕(叠在成片时间轴上)──
    cap_filters = []
    for cap in args.caption:
        st, en, text = cap.split(",", 2)
        text = text.replace("'", "\u2019").replace(":", "\\:")
        cap_filters.append(
            f"drawtext=fontfile='{FONT}':text='{text}':fontsize=42:fontcolor=white:"
            f"borderw=3:bordercolor=black@0.75:x=(w-text_w)/2:y=h-140:"
            f"enable='between(t,{st},{en})'")

    tmp_main = os.path.join(tempfile.gettempdir(), "pp_main.mp4")
    enc = ["-c:v", "libx264", "-crf", str(args.crf), "-preset", "slow",
           "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart"]

    if filter_complex:
        fc = filter_complex
        if cap_filters:
            fc += f";{v_label}" + ",".join(cap_filters) + "[vf]"
            v_label = "[vf]"
        run(["ffmpeg", "-y", "-v", "error", "-i", src, "-filter_complex", fc,
             "-map", v_label, "-map", a_label] + enc + [tmp_main])
    else:
        vf = ",".join(vf_parts + cap_filters) or "null"
        run(["ffmpeg", "-y", "-v", "error", "-i", src, "-vf", vf] + enc + [tmp_main])

    if not args.endcard:
        shutil.move(tmp_main, dst)  # 临时目录可能跨盘,os.replace 会报 WinError 17
        print("成片:", dst)
        return

    # ── 品牌尾卡(2.5s,黑底品牌名+域名,淡入)──
    tmp_card = os.path.join(tempfile.gettempdir(), "pp_card.mp4")
    run(["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "color=c=0x080b10:s=1920x1080:d=2.5:r=30",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=2.5",
         "-vf",
         f"drawtext=fontfile='{FONT}':text='{BRAND}':fontsize=88:fontcolor=white:"
         f"x=(w-text_w)/2:y=(h-text_h)/2-60:alpha='min(t/0.6,1)',"
         f"drawtext=fontfile='{FONT}':text='{SITE}':fontsize=44:fontcolor=0x22d3ee:"
         f"x=(w-text_w)/2:y=(h-text_h)/2+60:alpha='min(t/0.9,1)'",
         "-c:v", "libx264", "-crf", "21", "-preset", "slow",
         "-c:a", "aac", "-b:a", "160k", "-shortest", tmp_card])
    lst = os.path.join(tempfile.gettempdir(), "pp_concat.txt")
    with open(lst, "w", encoding="utf-8") as f:
        f.write(f"file '{tmp_main}'\nfile '{tmp_card}'\n")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst,
         "-c", "copy", dst])
    for t in (tmp_main, tmp_card, lst):
        try:
            os.remove(t)
        except OSError:
            pass
    print("成片:", dst)


if __name__ == "__main__":
    main()
