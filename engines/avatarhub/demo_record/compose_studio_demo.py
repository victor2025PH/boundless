# -*- coding: utf-8 -*-
"""换发型·定妆·试衣 showcase:把离线引擎出的前后静图做成"智能镜"风格换装片。
三段:定妆(刘亦菲 4 妆) → 换发型(刘亦菲 3 发) → 试衣(林志玲 3 衣),段间光扫过渡 + 标签 + 品牌尾卡。
用法: facefusion python demo_record/compose_studio_demo.py
"""
import os
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
ST = os.path.join(HERE, "studio")
OUT = os.path.join(HERE, "out")
STAR = r"C:\Users\user\Desktop\明星"
FONT = "C\\:/Windows/Fonts/msyh.ttc"
TMP = tempfile.gettempdir()

HOLD = 2.0     # 每张停留
XF = 0.6       # 过渡时长
BG = "0x0b0e14"


def run(cmd):
    subprocess.run(cmd, check=True)


def _ffprobe_dur(path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "csv=p=0", path], capture_output=True, text=True)
    return float(r.stdout.strip())


def clip_from_image(img, label, sub, out, hold=HOLD):
    """单张图 → hold 秒竖屏卡:居中大图 + 顶部主标签 + 副标签,深色影棚底。"""
    # 图缩放到高 820 居中,画布 1920x1080
    vf = (
        f"scale=-2:820,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color={BG},"
        f"drawbox=x=0:y=0:w=1920:h=1080:color={BG}@0:t=fill,"
        f"drawtext=fontfile='{FONT}':text='{label}':fontsize=58:fontcolor=white:"
        f"borderw=4:bordercolor=black@0.7:x=(w-text_w)/2:y=52,"
        f"drawtext=fontfile='{FONT}':text='{sub}':fontsize=32:fontcolor=0x22d3ee:"
        f"borderw=2:bordercolor=black@0.6:x=(w-text_w)/2:y=132"
    )
    run(["ffmpeg", "-y", "-v", "error", "-loop", "1", "-t", str(hold), "-i", img,
         "-vf", vf, "-r", "30", "-pix_fmt", "yuv420p",
         "-c:v", "libx264", "-crf", "20", "-preset", "medium", out])


def title_card(text, sub, out, dur=1.4):
    vf = (
        f"drawtext=fontfile='{FONT}':text='{text}':fontsize=92:fontcolor=white:"
        f"x=(w-text_w)/2:y=(h-text_h)/2-40:alpha='min(t/0.4,1)',"
        f"drawtext=fontfile='{FONT}':text='{sub}':fontsize=40:fontcolor=0x22d3ee:"
        f"x=(w-text_w)/2:y=(h-text_h)/2+70:alpha='min(t/0.6,1)'"
    )
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"color=c={BG}:s=1920x1080:d={dur}:r=30", "-vf", vf,
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "20", "-preset", "medium", out])


def xfade_chain(clips, out, trans="wipedown", dur=XF):
    """把多个等分辨率 clip 用 xfade 链接(光扫过渡)。"""
    if len(clips) == 1:
        run(["ffmpeg", "-y", "-v", "error", "-i", clips[0], "-c", "copy", out])
        return
    inputs = []
    for c in clips:
        inputs += ["-i", c]
    # 逐段累积 offset
    durs = []
    for c in clips:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "csv=p=0", c], capture_output=True, text=True)
        durs.append(float(r.stdout.strip()))
    fc = ""
    prev = "0:v"
    offset = durs[0] - dur
    for i in range(1, len(clips)):
        lbl = f"x{i}"
        fc += (f"[{prev}][{i}:v]xfade=transition={trans}:duration={dur}:"
               f"offset={offset:.3f}[{lbl}];")
        prev = lbl
        offset += durs[i] - dur
    fc = fc.rstrip(";")
    run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", fc,
         "-map", f"[{prev}]", "-r", "30", "-pix_fmt", "yuv420p",
         "-c:v", "libx264", "-crf", "20", "-preset", "medium", out])


def main():
    clips = []

    def add(img, label, sub, hold=HOLD):
        p = os.path.join(TMP, f"st_{len(clips):02d}.mp4")
        clip_from_image(img, label, sub, p, hold)
        clips.append(p)

    def add_title(text, sub):
        p = os.path.join(TMP, f"st_{len(clips):02d}.mp4")
        title_card(text, sub, p)
        clips.append(p)

    # 段1 定妆
    add_title("定妆 · 一键上妆", "同一张脸 · 秒切妆容")
    add(os.path.join(STAR, "刘亦菲.jpg"), "原始素颜", "BOUNDLESS 智能镜", 1.6)
    for style in ["复古红唇", "元气桃花", "烟熏", "女团紫"]:
        f = os.path.join(ST, f"makeup_刘亦菲_{style}.jpg")
        if os.path.exists(f):
            add(f, style, "AI 定妆")
    # 段2 换发型
    add_title("换发型 · 一键换发", "同一张脸 · 换发型不换人")
    add(os.path.join(STAR, "刘亦菲.jpg"), "原始发型", "BOUNDLESS 智能镜", 1.6)
    for st, cn in [("演示发型005", "波浪长发"), ("演示发型012", "棕色马尾"), ("演示发型020", "层次短发")]:
        f = os.path.join(ST, f"hair_刘亦菲_{st}.jpg")
        if os.path.exists(f):
            add(f, cn, "AI 换发")
    # 段3 试衣
    add_title("试衣 · 虚拟试穿", "上传一张照片 · 想穿什么穿什么")
    add(os.path.join(STAR, "林志玲.jpeg"), "试衣前", "BOUNDLESS 智能镜", 1.6)
    for c, cn in [("上衣010", "运动上衣"), ("连衣裙001", "白色长裙"), ("上衣003", "休闲款")]:
        f = os.path.join(ST, f"tryon_林志玲_演示{c}.jpg")
        if os.path.exists(f):
            add(f, cn, "AI 试衣")
    # 尾卡
    p = os.path.join(TMP, f"st_{len(clips):02d}.mp4")
    title_card("无界 BOUNDLESS", "usdt2026.cc", p, dur=2.5)
    clips.append(p)

    silent = os.path.join(OUT, "studio_demo_v1_silent.mp4")
    xfade_chain(clips, silent, trans="wipedown", dur=XF)
    dur = _ffprobe_dur(silent)
    # 原创合成环境垫乐(C 大调铺底四音 + 慢颤音 + 低通 + 混响,零版权风险),两端淡入淡出
    fo = max(0.1, dur - 2.0)
    music = ("sine=frequency=130.81:duration=%s[t1];"
             "sine=frequency=261.63:duration=%s[t2];"
             "sine=frequency=329.63:duration=%s[t3];"
             "sine=frequency=392.00:duration=%s[t4];"
             "[t1]volume=0.5[a];[t2]volume=0.34[b];[t3]volume=0.30[c];[t4]volume=0.24[d];"
             "[a][b][c][d]amix=inputs=4:normalize=0,tremolo=f=0.12:d=0.4,"
             "lowpass=f=2000,aecho=0.8:0.85:400|850:0.35|0.25,volume=-10dB,"
             "afade=t=in:st=0:d=2,afade=t=out:st=%s:d=2[music]"
             % (dur, dur, dur, dur, fo))
    final = os.path.join(OUT, "studio_demo_v1.mp4")
    run(["ffmpeg", "-y", "-v", "error", "-i", silent,
         "-filter_complex", music, "-map", "0:v", "-map", "[music]",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest",
         "-movflags", "+faststart", final])
    os.remove(silent)
    print("成片:", final)


if __name__ == "__main__":
    main()
