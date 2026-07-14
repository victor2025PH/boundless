# -*- coding: utf-8 -*-
"""换发型·定妆·试衣 v3(专业版·全动态视频):
  段1 视频换妆：真人动态视频 左原/右妆(红唇+桃花)。
  段2 视频试衣：真人视频逐帧换装 左原/右换装(CatV2TON,2 件,循环填充)。
  段3 换发型：Ditto 动态化的发型(呼吸/眨眼级微动),原→3 款,不再是静图/贴图。
  统一片头/段标/下三分之一/交叉淡化/尾卡/轻 BGM。
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

VMK1 = os.path.join(OUT, "vmakeup_lyf.mp4")
VMK2 = os.path.join(OUT, "vmakeup_lyf_taohua.mp4")
SRC2 = os.path.join(STAR, "0002.MOV")
# 试衣升级:6.1s 连续长镜头(两段 96 帧同种子拼接),不再是 2s 循环
TRYON_LONG = [(os.path.join(OUT, "vtryon_long_003.mp4"), "玫粉上衣"),
              (os.path.join(OUT, "vtryon_long_022.mp4"), "FILA 拼色")]
HAIR_IDLES = [(os.path.join(OUT, "idle_lyf_src.mp4"), "原始造型"),
              (os.path.join(OUT, "idle_hair2_wave.mp4"), "波浪长发"),
              (os.path.join(OUT, "idle_hair2_ponytail.mp4"), "棕色长发"),
              (os.path.join(OUT, "idle_hair2_short.mp4"), "层次短发")]

EN = os.environ.get("DEMO_LANG", "").lower() == "en"
TXT = {
    "定妆 · 换发 · 试衣": "Makeup · Hair · Try-on",
    "真人视频 · 引擎实时输出": "Real video · Real engine output",
    "01 / 视频换妆": "01 / Video makeup",
    "视频换妆 · 复古红唇": "Video makeup · Classic red lips",
    "真人动态视频 · 左原图 / 右上妆": "Live video · Left original / Right made-up",
    "视频换妆 · 元气桃花": "Video makeup · Peach glow",
    "同一个人 · 一键换整套妆": "Same person · One-click full look",
    "02 / 视频试衣": "02 / Video try-on",
    "视频试衣 · 玫粉上衣": "Video try-on · Rose tee",
    "视频试衣 · FILA 拼色": "Video try-on · FILA colorblock",
    "连续长镜头 · 真人视频逐帧换装": "Continuous shot · Frame-by-frame garment swap",
    "原始": "Original", "一键换装": "One-click outfit",
    "03 / 换发型": "03 / Hairstyles",
    "换发型 · 换发不换人": "New hair · Same person",
    "动态微动预览 · 一键切换整套造型": "Live micro-motion preview · Switch in one click",
    "原始造型": "Original", "波浪长发": "Wavy long", "棕色长发": "Brown long",
    "层次短发": "Layered short",
}


def T(s):
    return TXT.get(s, s) if EN else s


def stage_full(inp, out, t, seg, title, sub, accent=L.CYAN, trim0=0.0):
    vf = (f"[0:v]trim={trim0}:{trim0+t},setpts=PTS-STARTPTS,{L.stage_vf()}[bg];"
          f"[bg]" + L.seg_label(seg, accent) + "," + L.lower_third(title, sub, accent)
          + "," + L.brand_corner() + "[v]")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", inp, "-filter_complex", vf,
                    "-map", "[v]", "-t", str(t), "-an", "-r", "30", "-pix_fmt", "yuv420p",
                    "-c:v", "libx264", "-crf", "20", "-preset", "medium", out], check=True)


def tryon_split(before, after, out, t, seg, title, sub, src_off=0.0):
    """连续长镜头版:before 取原片同一时间窗(src_off 起),after 为整段试衣结果,不循环。"""
    fc = (
        f"[0:v]trim={src_off}:{src_off+t},setpts=PTS-STARTPTS,scale=420:560:force_original_aspect_ratio=increase,"
        f"crop=420:560,pad=436:576:8:8:color=0x475569@0.6,fps=30,settb=AVTB[a];"
        f"[1:v]trim=0:{t},setpts=PTS-STARTPTS,scale=420:560:force_original_aspect_ratio=increase,"
        f"crop=420:560,pad=436:576:8:8:color={L.GREEN}@0.7,fps=30,settb=AVTB[b];"
        f"color=c={BG}:s=1920x1080:d={t}[bgc];"
        f"[bgc][a]overlay=x=290:y=210[o1];[o1][b]overlay=x=1194:y=210[o2];"
        f"[o2]" + L._dt(T("原始"), "508-tw/2", "150", 36, L.WHITE, L.FONT_BD) + "," +
        L._dt(T("一键换装"), "1412-tw/2", "150", 36, L.GREEN, L.FONT_BD) + "," +
        L._dt("→", "(w-tw)/2", "440", 90, "0x94a3b8", L.FONT_BD) + "," +
        L.seg_label(seg, L.GREEN) + "," + L.lower_third(title, sub, L.GREEN) + "," +
        L.brand_corner() + "[v]"
    )
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", before, "-i", after,
                    "-filter_complex", fc, "-map", "[v]", "-t", str(t), "-an", "-r", "30",
                    "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "20", "-preset", "medium", out], check=True)


def hair_grid(out, t=4.0):
    """四宫格动态发型(原+3 款),全部是 Ditto 微动视频。"""
    ins = []
    for p, _ in HAIR_IDLES:
        ins += ["-i", p]
    q = [f"[{i}:v]trim=0:{t},setpts=PTS-STARTPTS,fps=30,settb=AVTB,scale=440:440:force_original_aspect_ratio=increase,"
         f"crop=440:440[q{i}]" for i in range(4)]
    fc = (";".join(q) + ";[q0][q1][q2][q3]xstack=inputs=4:layout=0_0|440_0|0_440|440_440,"
          f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color={BG}[grid];"
          f"[grid]" + L._dt(T(HAIR_IDLES[0][1]), "745", "300", 30, L.WHITE, L.FONT_BD) + "," +
          L._dt(T(HAIR_IDLES[1][1]), "1185", "300", 30, L.CYAN, L.FONT_BD) + "," +
          L._dt(T(HAIR_IDLES[2][1]), "745", "740", 30, L.MAGENTA, L.FONT_BD) + "," +
          L._dt(T(HAIR_IDLES[3][1]), "1185", "740", 30, L.GREEN, L.FONT_BD) + "," +
          L.seg_label(T("03 / 换发型"), L.CYAN) + "," +
          L.lower_third(T("换发型 · 换发不换人"), T("动态微动预览 · 一键切换整套造型"), L.CYAN) + "," +
          L.brand_corner() + "[v]")
    subprocess.run(["ffmpeg", "-y", "-v", "error", *ins, "-filter_complex", fc, "-map", "[v]",
                    "-t", str(t), "-an", "-r", "30", "-pix_fmt", "yuv420p", "-c:v", "libx264",
                    "-crf", "20", "-preset", "medium", out], check=True)


def main():
    clips = []
    tc = os.path.join(TMP, "sv_title.mp4"); L.title_card(T("定妆 · 换发 · 试衣"), T("真人视频 · 引擎实时输出"), tc); clips.append(tc)

    t1 = os.path.join(TMP, "sv_mk1.mp4")
    stage_full(VMK1, t1, min(4.6, L.dur(VMK1)), T("01 / 视频换妆"), T("视频换妆 · 复古红唇"), T("真人动态视频 · 左原图 / 右上妆"), L.MAGENTA); clips.append(t1)
    t2 = os.path.join(TMP, "sv_mk2.mp4")
    stage_full(VMK2, t2, min(4.6, L.dur(VMK2)), T("01 / 视频换妆"), T("视频换妆 · 元气桃花"), T("同一个人 · 一键换整套妆"), L.MAGENTA); clips.append(t2)

    for i, (v, cn) in enumerate(TRYON_LONG):
        if not os.path.exists(v):
            continue
        tt = os.path.join(TMP, f"sv_ty{i}.mp4")
        tryon_split(SRC2, v, tt, min(6.1, L.dur(v)), T("02 / 视频试衣"), T(f"视频试衣 · {cn}"),
                    T("连续长镜头 · 真人视频逐帧换装")); clips.append(tt)

    hg = os.path.join(TMP, "sv_hair.mp4"); hair_grid(hg, 4.0); clips.append(hg)

    ec = os.path.join(TMP, "sv_end.mp4"); L.end_card(ec); clips.append(ec)

    vcat = os.path.join(TMP, "sv_vcat.mp4")
    L.xfade_concat(clips, vcat, dur_x=0.4)
    vd = L.dur(vcat)
    bgm = os.path.join(TMP, "sv_bgm.wav"); L.music_bed(vd, bgm, mood="studio")
    final = os.path.join(OUT, "studio_demo_v3_en.mp4" if EN else "studio_demo_v3.mp4")
    # 纯音乐驱动的片子,BGM 是主音轨:loudnorm 后的 -26 LUFS 提到 ~-18(音乐前置)
    L.mux(vcat, None, final, bgm=bgm, bgm_gain="2.5")
    print("成片:", final)


if __name__ == "__main__":
    main()
