# -*- coding: utf-8 -*-
"""合成「视频换脸·前后对比」成片:
段1 扫描线揭示 — 0001 原片播 2.5s → 青色扫描线 5s 从左到右扫过,扫过处变刘德华脸,再放 5s。
段2 四宫格同步 — 0002 原片 + 三个换脸版(刘德华/彭于晏/杰森斯坦森) 2x2 同步播放。
真实拍摄素材逐帧同步,只有脸不同——这是 AI 生成片做不到的证据感。
用法: facefusion python demo_record/compose_faceswap_demo.py
"""
import os
import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
SRC1 = r"C:\Users\user\Desktop\明星\0001.MOV"
SRC2 = r"C:\Users\user\Desktop\明星\0002.MOV"
FS1 = os.path.join(OUT, "fs_0001_liu.mp4")
FS2_LIU = os.path.join(OUT, "fs_0002_liu.mp4")
FS2_PENG = os.path.join(OUT, "fs_0002_peng.mp4")
FS2_JASON = os.path.join(OUT, "fs_0002_jason.mp4")
FONT = "C\\:/Windows/Fonts/msyh.ttc"

WIPE_START = 2.5   # 扫描线开始
WIPE_DUR = 5.0
SEG1_LEN = 13.0    # 段1总长(原片 14.7s,留尾)
SEG2_LEN = 11.0    # 四宫格长度


def run(cmd):
    print(">", " ".join(cmd)[:220])
    subprocess.run(cmd, check=True)


def label(text, x="(w-text_w)/2", y="h-96", size=40, start=None, end=None):
    f = (f"drawtext=fontfile='{FONT}':text='{text}':fontsize={size}:fontcolor=white:"
         f"borderw=3:bordercolor=black@0.8:x={x}:y={y}")
    if start is not None:
        f += f":enable='between(t,{start},{end})'"
    return f


def main():
    seg1 = os.path.join(OUT, "_seg1.mp4")
    seg2 = os.path.join(OUT, "_seg2.mp4")

    # ── 段1: 扫描线揭示(xfade wipeleft + 移动亮线) ──
    # 用 0002 + 杰森·斯坦森(欧美光头白人):与华人原脸差异最大,扫描线揭示瞬间对比最强。
    fc = (
        f"[0:v]trim=0:{SEG1_LEN},setpts=PTS-STARTPTS,fps=30,settb=AVTB,scale=1920:1080,format=yuv420p[org];"
        f"[1:v]trim=0:{SEG1_LEN},setpts=PTS-STARTPTS,fps=30,settb=AVTB,scale=1920:1080,format=yuv420p[swp];"
        f"[org][swp]xfade=transition=wiperight:duration={WIPE_DUR}:offset={WIPE_START}[wiped];"
        # 扫描线亮条(6px 青色),随 wipe 从左到右移动,扫完消失
        f"color=c=0x22d3ee@0.9:s=8x1080:d={SEG1_LEN}[bar];"
        f"[wiped][bar]overlay=x='-10+1930*min(1,max(0,(t-{WIPE_START})/{WIPE_DUR}))':y=0:"
        f"enable='between(t,{WIPE_START},{WIPE_START + WIPE_DUR})'[lined];"
        f"[lined]{label('真实拍摄 · 原始视频', start=0.3, end=WIPE_START)},"
        f"{label('一键换脸 · 逐帧同步,只有脸变了', start=WIPE_START + WIPE_DUR, end=SEG1_LEN)},"
        f"{label('AI 换脸中', x='w-text_w-60', y='60', size=34, start=WIPE_START, end=WIPE_START + WIPE_DUR)}[v]"
    )
    run(["ffmpeg", "-y", "-v", "error", "-i", SRC2, "-i", FS2_JASON,
         "-filter_complex", fc, "-map", "[v]", "-map", "0:a",
         "-t", str(SEG1_LEN),
         "-c:v", "libx264", "-crf", "20", "-preset", "slow",
         "-c:a", "aac", "-b:a", "160k", "-ar", "48000", seg1])

    # ── 段1.5: 大画面换脸 + 右上角真人小窗(证明两窗逐帧同步——直播换脸核心卖点) ──
    segc = os.path.join(OUT, "_segc.mp4")
    CORNER_LEN = 8.0
    fcc = (
        f"[1:v]trim=0:{CORNER_LEN},setpts=PTS-STARTPTS,fps=30,settb=AVTB,scale=1920:1080,format=yuv420p[big];"
        f"[0:v]trim=0:{CORNER_LEN},setpts=PTS-STARTPTS,fps=30,settb=AVTB,scale=520:-2,"
        f"drawbox=x=0:y=0:w=iw:h=ih:color=0x22d3ee:t=4[pip];"
        f"[big][pip]overlay=x=W-w-40:y=40[ov];"
        f"[ov]{label('大画面：已换脸', x='60', y='60', size=40)},"
        f"{label('右上：真人原画 · 逐帧同步', x='W-560', y='h-140', size=32)}[v]"
    )
    run(["ffmpeg", "-y", "-v", "error", "-i", SRC2, "-i", FS2_LIU,
         "-filter_complex", fcc, "-map", "[v]", "-map", "1:a",
         "-t", str(CORNER_LEN),
         "-c:v", "libx264", "-crf", "20", "-preset", "slow",
         "-c:a", "aac", "-b:a", "160k", "-ar", "48000", segc])

    # ── 段2: 2x2 四宫格(原/刘德华/彭于晏/杰森斯坦森)同步播放 ──
    q = []
    for i in range(4):
        q.append(f"[{i}:v]trim=0:{SEG2_LEN},setpts=PTS-STARTPTS,fps=30,settb=AVTB,scale=960:540,format=yuv420p[q{i}]")
    fc2 = (
        ";".join(q) + ";"
        "[q0][q1][q2][q3]xstack=inputs=4:layout=0_0|960_0|0_540|960_540[grid];"
        f"[grid]{label('原始', x='30', y='30', size=36)},"
        f"{label('刘德华', x='990', y='30', size=36)},"
        f"{label('彭于晏', x='30', y='570', size=36)},"
        f"{label('杰森·斯坦森', x='990', y='570', size=36)},"
        f"{label('同一段视频 · 换谁都行 · 动作口型逐帧同步', start=0.5, end=SEG2_LEN)}[v]"
    )
    run(["ffmpeg", "-y", "-v", "error", "-i", SRC2, "-i", FS2_LIU, "-i", FS2_PENG, "-i", FS2_JASON,
         "-filter_complex", fc2, "-map", "[v]", "-map", "0:a",
         "-t", str(SEG2_LEN),
         "-c:v", "libx264", "-crf", "20", "-preset", "slow",
         "-c:a", "aac", "-b:a", "160k", "-ar", "48000", seg2])

    # ── 尾卡 + 拼接(复用 postprod 的尾卡样式) ──
    card = os.path.join(OUT, "_card.mp4")
    run(["ffmpeg", "-y", "-v", "error",
         "-f", "lavfi", "-i", "color=c=0x080b10:s=1920x1080:d=2.5:r=30",
         "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=2.5",
         "-vf",
         f"drawtext=fontfile='{FONT}':text='无界 BOUNDLESS':fontsize=88:fontcolor=white:"
         f"x=(w-text_w)/2:y=(h-text_h)/2-60:alpha='min(t/0.6,1)',"
         f"drawtext=fontfile='{FONT}':text='usdt2026.cc':fontsize=44:fontcolor=0x22d3ee:"
         f"x=(w-text_w)/2:y=(h-text_h)/2+60:alpha='min(t/0.9,1)'",
         "-c:v", "libx264", "-crf", "21", "-preset", "slow",
         "-c:a", "aac", "-b:a", "160k", "-shortest", card])
    lst = os.path.join(OUT, "_concat.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in (seg1, segc, seg2, card):
            f.write("file '%s'\n" % p.replace("\\", "/"))
    final = os.path.join(OUT, "faceswap_demo_v1.mp4")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", final])
    print("成片:", final)


if __name__ == "__main__":
    main()
