# -*- coding: utf-8 -*-
"""视频换脸 showcase v2(修 v1:抖动/口型/换声)：
  段1 表情跟随：vlog(真人走动说话)→刘亦菲,高动态证明口型表情完全跟随、脸稳不抖。
  段2 换脸+变声：0002→刘德华,原声过 RVC 变成另一把声音(脸和声音都换了)。
  段3 扫描线前后对比：0002→杰森·斯坦森(欧美脸,反差最大)。
  段4 四宫格同步：原/刘德华/彭于晏/杰森 逐帧同步(AI 生成做不到)。
所有换脸片=HyperSwap-256 + 关键点 EMA 平滑(faceswap_video2.py)。
"""
import os
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
STAR = r"C:\Users\user\Desktop\明星"
FONT = "C\\:/Windows/Fonts/msyh.ttc"
TMP = tempfile.gettempdir()

VLOG = os.path.join(OUT, "_vlog_clip.mp4")           # 12s 真人走动说话
VLOG_LYF = os.path.join(OUT, "v2_vlog_lyf.mp4")      # 换脸刘亦菲
SRC2 = os.path.join(STAR, "0002.MOV")
LIU_V = os.path.join(OUT, "v2_0002_liu_voiced.mp4")  # 刘德华+变声
PENG = os.path.join(OUT, "v2_0002_peng.mp4")
JASON = os.path.join(OUT, "v2_0002_jason.mp4")


def run(cmd):
    subprocess.run(cmd, check=True)


def dur(p):
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "csv=p=0", p], capture_output=True, text=True).stdout.strip())


def lab(text, x="(w-text_w)/2", y="h-110", size=44, st=None, en=None):
    f = (f"drawtext=fontfile='{FONT}':text='{text}':fontsize={size}:fontcolor=white:"
         f"borderw=3:bordercolor=black@0.8:x={x}:y={y}")
    if st is not None:
        f += f":enable='between(t,{st},{en})'"
    return f


def norm(inp, out, t, extra_vf="", trim0=0.0):
    """归一到 1920x1080@30 + 主标签,可选附加 vf。"""
    vf = f"trim={trim0}:{trim0+t},setpts=PTS-STARTPTS,fps=30,scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0x0b0e14,format=yuv420p"
    if extra_vf:
        vf += "," + extra_vf
    run(["ffmpeg", "-y", "-v", "error", "-i", inp, "-vf", vf, "-t", str(t),
         "-an", "-c:v", "libx264", "-crf", "20", "-preset", "medium", out])


def seg_with_audio(vin, ain, out, t, extra_vf="", vol=1.0):
    """带音轨段(用于变声段):vin 视频 + ain 音频源。"""
    vf = f"fps=30,scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0x0b0e14,format=yuv420p"
    if extra_vf:
        vf += "," + extra_vf
    run(["ffmpeg", "-y", "-v", "error", "-i", vin, "-i", ain, "-map", "0:v", "-map", "1:a",
         "-vf", vf, "-t", str(t), "-c:v", "libx264", "-crf", "20", "-preset", "medium",
         "-c:a", "aac", "-b:a", "160k", "-ar", "48000", out])


def title(text, sub, out, d=1.6):
    vf = (f"drawtext=fontfile='{FONT}':text='{text}':fontsize=90:fontcolor=white:"
          f"x=(w-text_w)/2:y=(h-text_h)/2-40:alpha='min(t/0.4,1)',"
          f"drawtext=fontfile='{FONT}':text='{sub}':fontsize=40:fontcolor=0x22d3ee:"
          f"x=(w-text_w)/2:y=(h-text_h)/2+70:alpha='min(t/0.6,1)'")
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"color=c=0x0b0e14:s=1920x1080:d={d}:r=30", "-vf", vf,
         "-an", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "20", "-preset", "medium", out])


def main():
    parts = []   # (path, has_audio)

    # 段1 表情跟随(vlog→刘亦菲),配原声(vlog 有环境声)
    s1 = os.path.join(TMP, "fs_s1.mp4")
    seg_with_audio(VLOG_LYF, VLOG, s1, min(9.0, dur(VLOG_LYF)),
                   extra_vf=lab("换脸 · 表情口型完全跟随", st=0.3, en=9.0) + "," +
                            lab("真人走动说话 → 换成刘亦菲", y="150", size=34, st=0.3, en=9.0))
    parts.append(s1)

    # 段2 换脸+变声(0002→刘德华 + 男神声)
    s2 = os.path.join(TMP, "fs_s2.mp4")
    seg_with_audio(LIU_V, LIU_V, s2, min(9.0, dur(LIU_V)),
                   extra_vf=lab("换脸 + 变声：脸和声音都换了", st=0.3, en=9.0) + "," +
                            lab("原声已换成另一把音色", y="150", size=34, st=0.3, en=9.0))
    parts.append(s2)

    # 段3 扫描线前后对比(0002 原 → 杰森)
    s3 = os.path.join(TMP, "fs_s3.mp4")
    L = 8.0
    fc = (
        f"[0:v]trim=0:{L},setpts=PTS-STARTPTS,fps=30,settb=AVTB,scale=1920:1080:force_original_aspect_ratio=decrease,"
        f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0x0b0e14,format=yuv420p[org];"
        f"[1:v]trim=0:{L},setpts=PTS-STARTPTS,fps=30,settb=AVTB,scale=1920:1080:force_original_aspect_ratio=decrease,"
        f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0x0b0e14,format=yuv420p[swp];"
        f"[org][swp]xfade=transition=wiperight:duration=4.5:offset=2[wp];"
        f"color=c=0x22d3ee@0.9:s=8x1080:d={L}[bar];"
        f"[wp][bar]overlay=x='-10+1930*min(1,max(0,(t-2)/4.5))':y=0:enable='between(t,2,6.5)'[ln];"
        f"[ln]{lab('真实原脸', st=0.3, en=2)},{lab('一键换脸,只有脸变了', st=6.5, en=L)}[v]"
    )
    run(["ffmpeg", "-y", "-v", "error", "-i", SRC2, "-i", JASON, "-filter_complex", fc,
         "-map", "[v]", "-t", str(L), "-an", "-c:v", "libx264", "-crf", "20", "-preset", "medium", s3])
    parts.append(s3)

    # 段4 四宫格
    s4 = os.path.join(TMP, "fs_s4.mp4")
    L2 = 8.0
    q = [f"[{i}:v]trim=0:{L2},setpts=PTS-STARTPTS,fps=30,settb=AVTB,scale=960:540,format=yuv420p[q{i}]"
         for i in range(4)]
    fc4 = (";".join(q) + ";[q0][q1][q2][q3]xstack=inputs=4:layout=0_0|960_0|0_540|960_540[g];"
           f"[g]{lab('原始', x='30', y='30', size=34)},{lab('刘德华', x='990', y='30', size=34)},"
           f"{lab('彭于晏', x='30', y='570', size=34)},{lab('杰森·斯坦森', x='990', y='570', size=34)},"
           f"{lab('同一段视频 · 四张脸逐帧同步', st=0.5, en=L2)}[v]")
    run(["ffmpeg", "-y", "-v", "error", "-i", SRC2, "-i", os.path.join(OUT, "v2_0002_liu.mp4"),
         "-i", PENG, "-i", JASON, "-filter_complex", fc4, "-map", "[v]", "-t", str(L2),
         "-an", "-c:v", "libx264", "-crf", "20", "-preset", "medium", s4])
    parts.append(s4)

    # 尾卡
    ec = os.path.join(TMP, "fs_end.mp4")
    title("无界 BOUNDLESS", "usdt2026.cc", ec, 2.5)
    parts.append(ec)

    # 给无声段补静音轨,统一后 concat(视频流参数已一致)
    normed = []
    for i, p in enumerate(parts):
        has_a = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
                                "stream=codec_type", "-of", "csv=p=0", p],
                               capture_output=True, text=True).stdout.strip()
        if has_a:
            normed.append(p)
        else:
            q = os.path.join(TMP, f"fs_a{i}.mp4")
            run(["ffmpeg", "-y", "-v", "error", "-i", p, "-f", "lavfi",
                 "-i", "anullsrc=r=48000:cl=stereo", "-map", "0:v", "-map", "1:a",
                 "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-shortest", q])
            normed.append(q)

    lst = os.path.join(TMP, "fs_concat.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in normed:
            f.write("file '%s'\n" % p.replace("\\", "/"))
    final = os.path.join(OUT, "faceswap_demo_v2.mp4")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst,
         "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-c:a", "aac", "-b:a", "160k",
         "-movflags", "+faststart", final])
    print("成片:", final)


if __name__ == "__main__":
    main()
