# -*- coding: utf-8 -*-
"""视频换脸 v3(专业版·统一设计·单一主体):
  全程同一条真人视频(0002 你的正脸),不再混入无关素材/食物空镜。
  段1 换脸+变声：0002→刘德华 + 原声变另一把音色。
  段2 扫描线前后对比：0002→杰森·斯坦森(欧美脸反差最大)。
  段3 四宫格同步：原/刘德华/彭于晏/杰森 逐帧同步(AI 生成做不到)。
  统一片头/下三分之一字幕/段标/交叉淡化/尾卡/轻 BGM。
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compose_lib as L

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
STAR = r"C:\Users\user\Desktop\明星"
TMP = L.TMP
BG = L.BG
SRC2 = os.path.join(STAR, "0002.MOV")
LIU_V = os.path.join(OUT, "v2_0002_liu_voiced.mp4")
LIU = os.path.join(OUT, "v2_0002_liu.mp4")
PENG = os.path.join(OUT, "v2_0002_peng.mp4")
JASON = os.path.join(OUT, "v2_0002_jason.mp4")

EN = os.environ.get("DEMO_LANG", "").lower() == "en"
TXT = {
    "视频换脸 · 前后对比": "Video Face Swap · Before / After",
    "一段真实视频 · 想换谁换谁": "One real video · Swap to anyone",
    "01 / 换脸 + 变声": "01 / Face + Voice swap",
    "换脸 + 变声：脸和声音一起换": "Face and voice swapped together",
    "同一段真人视频 · 只换脸与音色": "Same source video · Only face & voice change",
    "02 / 扫描线揭示": "02 / Scanline reveal",
    "一键换脸 · 只有脸变了": "One-click swap · Only the face changes",
    "扫描线左侧=已换脸,右侧=真实原脸": "Left of the line = swapped · Right = original",
    "03 / 四张脸同步": "03 / Four faces in sync",
    "同一段视频 · 四张脸逐帧同步": "One video · Four faces frame-locked",
    "动作口型完全一致 —— AI 生成做不到": "Identical motion & lips — generative AI can't",
    "原始": "Original", "刘德华": "Andy Lau", "彭于晏": "Eddie Peng",
    "杰森·斯坦森": "Jason Statham",
}


def T(s):
    return TXT.get(s, s) if EN else s


def stage_clip(inp, out, t, seg, sub_title, sub_desc, accent=L.CYAN, with_audio=None, trim0=0.0):
    """把一条竖屏真人片放进 16:9 舞台(左侧人、右侧留白无所谓,居中),加段标+下三分之一。"""
    vf = (f"[0:v]trim={trim0}:{trim0+t},setpts=PTS-STARTPTS,{L.stage_vf()}[bg];"
          f"[bg]" + L.seg_label(seg, accent) + "," +
          L.lower_third(sub_title, sub_desc, accent) + "," + L.brand_corner() + "[v]")
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", inp]
    if with_audio:
        cmd += ["-i", with_audio]
    cmd += ["-filter_complex", vf, "-map", "[v]", "-t", str(t)]
    if with_audio:
        cmd += ["-map", "1:a", "-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
    else:
        cmd += ["-an"]
    cmd += ["-r", "30", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "20", "-preset", "medium", out]
    subprocess.run(cmd, check=True)


def main():
    clips = []

    tc = os.path.join(TMP, "fv_title.mp4"); L.title_card(T("视频换脸 · 前后对比"), T("一段真实视频 · 想换谁换谁"), tc); clips.append(tc)

    # 段1 换脸+变声(带音轨:变声后的音频)
    s1 = os.path.join(TMP, "fv_s1.mp4")
    stage_clip(LIU_V, s1, min(9.0, L.dur(LIU_V)), T("01 / 换脸 + 变声"),
               T("换脸 + 变声：脸和声音一起换"), T("同一段真人视频 · 只换脸与音色"), accent=L.CYAN, with_audio=LIU_V)
    clips.append(s1)

    # 段2 扫描线前后对比(0002 原 → 杰森)
    s2 = os.path.join(TMP, "fv_s2.mp4")
    Ln = 8.0
    fc = (
        f"[0:v]trim=0:{Ln},setpts=PTS-STARTPTS,{L.stage_vf()}[org];"
        f"[1:v]trim=0:{Ln},setpts=PTS-STARTPTS,{L.stage_vf()}[swp];"
        f"[org][swp]xfade=transition=wiperight:duration=4.5:offset=2[wp];"
        f"color=c={L.CYAN}@0.95:s=6x1080:d={Ln}[bar];"
        f"[wp][bar]overlay=x='-6+1926*min(1,max(0,(t-2)/4.5))':y=0:enable='between(t,2,6.5)'[ln];"
        f"[ln]" + L.seg_label(T("02 / 扫描线揭示"), L.MAGENTA) + "," +
        L.lower_third(T("一键换脸 · 只有脸变了"), T("扫描线左侧=已换脸,右侧=真实原脸"), L.MAGENTA) + "," +
        L.brand_corner() + "[v]"
    )
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", SRC2, "-i", JASON, "-filter_complex", fc,
                    "-map", "[v]", "-t", str(Ln), "-an", "-r", "30", "-pix_fmt", "yuv420p",
                    "-c:v", "libx264", "-crf", "20", "-preset", "medium", s2], check=True)
    clips.append(s2)

    # 段3 四宫格
    s3 = os.path.join(TMP, "fv_s3.mp4")
    L2 = 8.0
    q = [f"[{i}:v]trim=0:{L2},setpts=PTS-STARTPTS,fps=30,settb=AVTB,"
         f"scale=960:540:force_original_aspect_ratio=decrease,pad=960:540:(ow-iw)/2:(oh-ih)/2:color={BG}[q{i}]"
         for i in range(4)]
    fc3 = (";".join(q) + ";[q0][q1][q2][q3]xstack=inputs=4:layout=0_0|960_0|0_540|960_540[g];"
           f"[g]" + L._dt(T("原始"), "40", "36", 34, L.WHITE, L.FONT_BD) + "," +
           L._dt(T("刘德华"), "1000", "36", 34, L.CYAN, L.FONT_BD) + "," +
           L._dt(T("彭于晏"), "40", "576", 34, L.GREEN, L.FONT_BD) + "," +
           L._dt(T("杰森·斯坦森"), "1000", "576", 34, L.MAGENTA, L.FONT_BD) + "," +
           L.seg_label(T("03 / 四张脸同步"), L.CYAN) + "," +
           L.lower_third(T("同一段视频 · 四张脸逐帧同步"), T("动作口型完全一致 —— AI 生成做不到"), L.CYAN) + "[v]")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", SRC2, "-i", LIU, "-i", PENG, "-i", JASON,
                    "-filter_complex", fc3, "-map", "[v]", "-t", str(L2), "-an", "-r", "30",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "20", "-preset", "medium", s3], check=True)
    clips.append(s3)

    ec = os.path.join(TMP, "fv_end.mp4"); L.end_card(ec); clips.append(ec)

    # 交叉淡化拼接(无声视频层)
    vcat = os.path.join(TMP, "fv_vcat.mp4")
    L.xfade_concat(clips, vcat, dur_x=0.4)

    # 音频:段1 的变声音轨在其时间窗放,其余段用 BGM。做法:整片 BGM + 段1 时间窗叠变声。
    vd = L.dur(vcat)
    bgm = os.path.join(TMP, "fv_bgm.wav"); L.music_bed(vd, bgm, mood="faceswap")
    # 段1 在片头卡之后:offset = title(2.4) - xfade(0.4) = 2.0
    s1_off = L.dur(clips[0]) - 0.4
    voiced = os.path.join(TMP, "fv_voice.wav")
    L.denoise_wav(LIU_V, voiced)   # 变声音轨也降噪+统一响度
    # 用 adelay 把变声放到 s1 时间窗,与 BGM 混
    final = os.path.join(OUT, "faceswap_demo_v3_en.mp4" if EN else "faceswap_demo_v3.mp4")
    fc_a = (f"[1:a]volume=0.28[b];"
            f"[2:a]adelay={int(s1_off*1000)}|{int(s1_off*1000)},volume=1.0[v];"
            f"[b][v]amix=inputs=2:duration=first:dropout_transition=0,volume=1.4[a]")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", vcat, "-i", bgm, "-i", voiced,
                    "-filter_complex", fc_a, "-map", "0:v", "-map", "[a]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                    "-shortest", final], check=True)
    print("成片:", final)


if __name__ == "__main__":
    main()
