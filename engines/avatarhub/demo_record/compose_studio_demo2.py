# -*- coding: utf-8 -*-
"""换发型·定妆·试衣 showcase v2(修 v1:静图看不出/发型假/换装拼图)：
  段1 视频换妆：真人走动视频 左原/右妆 分屏(明显妆效,动态真人)。
  段2 视频试衣：真人视频换装(CatV2TON 逐帧),左原/右换装分屏。
  段3 换发型：整头替换(paste_back=False,不露原发),原→3 款发型。
均为引擎真实输出,动态为主。
"""
import os
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
ST = os.path.join(HERE, "studio")
STAR = r"C:\Users\user\Desktop\明星"
FONT = "C\\:/Windows/Fonts/msyh.ttc"
TMP = tempfile.gettempdir()
BG = "0x0b0e14"

VMK1 = os.path.join(OUT, "vmakeup_lyf.mp4")            # 视频换妆·复古红唇(已分屏)
VMK2 = os.path.join(OUT, "vmakeup_lyf_taohua.mp4")     # 视频换妆·桃花
VTRYON = os.path.join(OUT, "vtryon_0002_top003.mp4")   # 视频试衣结果
SRC_TRYON = os.path.join(STAR, "0002.MOV")


def run(cmd):
    subprocess.run(cmd, check=True)


def dur(p):
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "csv=p=0", p], capture_output=True, text=True).stdout.strip())


def lab(text, x="(w-text_w)/2", y="60", size=44, st=None, en=None):
    f = (f"drawtext=fontfile='{FONT}':text='{text}':fontsize={size}:fontcolor=white:"
         f"borderw=3:bordercolor=black@0.8:x={x}:y={y}")
    if st is not None:
        f += f":enable='between(t,{st},{en})'"
    return f


def clip(inp, out, t, label_top, label_sub, trim0=0.0):
    vf = (f"trim={trim0}:{trim0+t},setpts=PTS-STARTPTS,fps=30,"
          f"scale=1920:1080:force_original_aspect_ratio=decrease,"
          f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color={BG},format=yuv420p,"
          + lab(label_top, y="52") + "," + lab(label_sub, y="132", size=32))
    run(["ffmpeg", "-y", "-v", "error", "-i", inp, "-vf", vf, "-t", str(t),
         "-an", "-c:v", "libx264", "-crf", "20", "-preset", "medium", out])


def tryon_split(before, after, out, t, label_top, label_sub, loops=2):
    """真人试衣左原/右换装分屏(两视频等长同步);短片 loop 拉长到可看。"""
    fc = (
        f"[0:v]trim=0:{t},setpts=PTS-STARTPTS,fps=30,settb=AVTB,scale=540:960:force_original_aspect_ratio=decrease,"
        f"pad=540:960:(ow-iw)/2:(oh-ih)/2:color={BG}[a];"
        f"[1:v]trim=0:{t},setpts=PTS-STARTPTS,fps=30,settb=AVTB,scale=540:960:force_original_aspect_ratio=decrease,"
        f"pad=540:960:(ow-iw)/2:(oh-ih)/2:color={BG}[b];"
        f"[a][b]hstack=2,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color={BG},format=yuv420p,"
        f"drawtext=fontfile='{FONT}':text='原始':fontsize=34:fontcolor=white:borderw=2:bordercolor=black@0.7:x=(w/2-text_w)/2+120:y=80,"
        f"drawtext=fontfile='{FONT}':text='一键换装':fontsize=34:fontcolor=0x34d399:borderw=2:bordercolor=black@0.7:x=w/2+(w/2-text_w)/2-120:y=80,"
        f"{lab(label_top, y='h-150', size=42)},{lab(label_sub, y='h-90', size=30)}[v]"
    )
    base = os.path.join(TMP, "st_ty_base.mp4")
    run(["ffmpeg", "-y", "-v", "error", "-i", before, "-i", after, "-filter_complex", fc,
         "-map", "[v]", "-t", str(t), "-an", "-c:v", "libx264", "-crf", "20", "-preset", "medium", base])
    # loop 拉长(短 tryon 片 ~2s → loops 次)
    run(["ffmpeg", "-y", "-v", "error", "-stream_loop", str(loops - 1), "-i", base,
         "-c", "copy", out])


def still(img, out, t, label_top, label_sub):
    vf = (f"scale=-2:840,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color={BG},format=yuv420p,"
          + lab(label_top, y="52") + "," + lab(label_sub, y="132", size=32, ))
    run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-t", str(t), "-i", img,
         "-vf", vf, "-r", "30", "-an", "-c:v", "libx264", "-crf", "20", "-preset", "medium", out])


def title(text, sub, out, d=1.6):
    vf = (f"drawtext=fontfile='{FONT}':text='{text}':fontsize=88:fontcolor=white:"
          f"x=(w-text_w)/2:y=(h-text_h)/2-40:alpha='min(t/0.4,1)',"
          f"drawtext=fontfile='{FONT}':text='{sub}':fontsize=38:fontcolor=0x22d3ee:"
          f"x=(w-text_w)/2:y=(h-text_h)/2+66:alpha='min(t/0.6,1)'")
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"color=c={BG}:s=1920x1080:d={d}:r=30", "-vf", vf,
         "-an", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "20", "-preset", "medium", out])


def main():
    P = []

    def add(fn):
        P.append(fn)

    t = os.path.join(TMP, "st_t0.mp4"); title("定妆 · 换发 · 试衣", "真人视频 · 引擎实时输出", t); add(t)

    # 段1 视频换妆
    t = os.path.join(TMP, "st_mk1.mp4")
    clip(VMK1, t, min(6.5, dur(VMK1)), "视频换妆 · 复古红唇", "真人动态视频 · 左原图 / 右上妆"); add(t)
    t = os.path.join(TMP, "st_mk2.mp4")
    clip(VMK2, t, min(6.0, dur(VMK2)), "视频换妆 · 元气桃花", "同一个人 · 一键换整套妆"); add(t)

    # 段2 视频试衣(真人换装)
    t = os.path.join(TMP, "st_ty.mp4")
    ty_len = min(dur(VTRYON), dur(SRC_TRYON), 8.0)
    tt = os.path.join(TMP, "st_t2.mp4"); title("虚拟试衣 · 真人换装", "上传真人视频 · 逐帧换装", tt); add(tt)
    tryon_split(SRC_TRYON, VTRYON, t, ty_len, "视频试衣 · 真人逐帧换装", "不是拼图 · 每一帧都换", loops=3); add(t)

    # 段3 换发型(整头替换,不露原发)
    tt = os.path.join(TMP, "st_t3.mp4"); title("换发型 · 整头替换", "换发不换人 · 不露原发", tt); add(tt)
    for img, cn in [("hair2_wave.jpg", "波浪长发"), ("hair2_ponytail.jpg", "棕色长发"), ("hair2_short.jpg", "层次感")]:
        p = os.path.join(ST, img)
        if os.path.exists(p):
            t = os.path.join(TMP, f"st_h_{cn}.mp4")
            still(p, t, 2.0, cn, "AI 换发 · 整头生成"); add(t)

    ec = os.path.join(TMP, "st_end.mp4"); title("无界 BOUNDLESS", "usdt2026.cc", ec, 2.5); add(ec)

    silent = os.path.join(OUT, "studio_v2_silent.mp4")
    lst = os.path.join(TMP, "st_concat.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in P:
            f.write("file '%s'\n" % p.replace("\\", "/"))
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst,
         "-c:v", "libx264", "-crf", "20", "-preset", "medium", silent])
    d = dur(silent); fo = max(0.1, d - 2)
    music = ("sine=frequency=146.83:duration=%s[t1];sine=frequency=293.66:duration=%s[t2];"
             "sine=frequency=369.99:duration=%s[t3];sine=frequency=440:duration=%s[t4];"
             "[t1]volume=0.5[a];[t2]volume=0.34[b];[t3]volume=0.30[c];[t4]volume=0.24[d];"
             "[a][b][c][d]amix=inputs=4:normalize=0,tremolo=f=0.13:d=0.4,lowpass=f=2000,"
             "aecho=0.8:0.85:400|850:0.35|0.25,volume=-11dB,afade=t=in:st=0:d=2,afade=t=out:st=%s:d=2[m]"
             % (d, d, d, d, fo))
    final = os.path.join(OUT, "studio_demo_v2.mp4")
    run(["ffmpeg", "-y", "-v", "error", "-i", silent, "-filter_complex", music,
         "-map", "0:v", "-map", "[m]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
         "-shortest", "-movflags", "+faststart", final])
    os.remove(silent)
    print("成片:", final)


if __name__ == "__main__":
    main()
