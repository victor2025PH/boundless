# -*- coding: utf-8 -*-
"""克隆音实时同传 v3(专业版·全视频):
  会说话的数字人(Ditto,嘴型跟音频)分屏——左「我·中文」右「对方·英文」,
  同一张脸同一把声音;下三分之一双语字幕;交叉淡化;降噪+统一响度+轻 BGM。
"""
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compose_lib as L

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
DIT = os.path.join(HERE, "ditto")
SRC = os.path.join(HERE, "interp2")
TMP = L.TMP
BG = L.BG

# 英文版:标题/头标查表翻译,下三分之一主副行对调(英文为主行)
EN = os.environ.get("DEMO_LANG", "").lower() == "en"
TXT = {
    "克隆音实时同传": "Live Interpreting · In Your Own Voice",
    "我说中文 · 对方听英文 · 还是我的声音": "I speak Mandarin · They hear English · Still my voice",
    "我说 · 中文": "I speak · Mandarin",
    "对方听 · 英文": "They hear · English",
}


def T(s):
    return TXT.get(s, s) if EN else s


def split_talk(zh_mp4, en_mp4, side_active, out, seg_dur, sub_zh, sub_en):
    """左中文/右英文数字人分屏;side_active='zh'|'en' 决定哪侧亮起(播哪侧)。"""
    zc = L.CYAN if side_active == "zh" else "0x475569"
    ec = L.GREEN if side_active == "en" else "0x475569"
    # 卡片 440x740,含 8px 描边 → 456x756;左 x=250,右 x=1214(对称,中缝留箭头)
    fc = (
        f"[0:v]trim=0:{seg_dur},setpts=PTS-STARTPTS,scale=440:740:force_original_aspect_ratio=increase,"
        f"crop=440:740,pad=456:756:8:8:color={zc}@0.6,fps=30,settb=AVTB[l];"
        f"[1:v]trim=0:{seg_dur},setpts=PTS-STARTPTS,scale=440:740:force_original_aspect_ratio=increase,"
        f"crop=440:740,pad=456:756:8:8:color={ec}@0.6,fps=30,settb=AVTB[r];"
        f"color=c={BG}:s=1920x1080:d={seg_dur}[bgc];"
        f"[bgc][l]overlay=x=250:y=96[o1];[o1][r]overlay=x=1214:y=96[o2];"
        f"[o2]"
        + L._dt(T("我说 · 中文"), "478-tw/2", "48", 38, zc, L.FONT_BD) + ","
        + L._dt(T("对方听 · 英文"), "1442-tw/2", "48", 38, ec, L.FONT_BD) + ","
        + L._dt("→", "(w-tw)/2", "440", 96, "0x94a3b8", L.FONT_BD) + ","
        + L.lower_third(sub_en if EN else sub_zh, sub_zh if EN else sub_en,
                        accent=(L.CYAN if side_active == "zh" else L.GREEN))
        + "," + L.brand_corner() + "[v]"
    )
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", zh_mp4, "-i", en_mp4,
                    "-filter_complex", fc, "-map", "[v]", "-t", str(seg_dur),
                    "-an", "-r", "30", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "20",
                    "-preset", "medium", out], check=True)


def main():
    meta = json.load(open(os.path.join(SRC, "lines.json"), encoding="utf-8"))
    clips, a_parts = [], []

    tc = os.path.join(TMP, "iv_title.mp4")
    L.title_card(T("克隆音实时同传"), T("我说中文 · 对方听英文 · 还是我的声音"), tc, 2.4)
    clips.append(tc)
    st = os.path.join(TMP, "iv_t.wav"); L.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
              "-i", "anullsrc=r=44100:cl=mono:d=2.4", "-c:a", "pcm_s16le", st]); a_parts.append(st)

    GAP = 0.3
    for m in meta:
        i = m["i"]
        zt = os.path.join(DIT, f"line{i}_zh.mp4")
        et = os.path.join(DIT, f"line{i}_en.mp4")
        # 中文段:左亮,时长=中文音频
        c1 = os.path.join(TMP, f"iv_{i}_zh.mp4")
        split_talk(zt, et, "zh", c1, m["zh_dur"] + GAP, m["zh"], m["en"]); clips.append(c1)
        dz = os.path.join(TMP, f"iv_{i}_zh.wav"); L.denoise_wav(m["zh_wav"], dz)
        gz = os.path.join(TMP, f"iv_{i}_zg.wav"); L.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                  "-i", f"anullsrc=r=44100:cl=mono:d={GAP}", "-c:a", "pcm_s16le", gz])
        a_parts += [dz, gz]
        # 英文段:右亮,时长=英文音频
        c2 = os.path.join(TMP, f"iv_{i}_en.mp4")
        split_talk(zt, et, "en", c2, m["en_dur"] + GAP, m["zh"], m["en"]); clips.append(c2)
        de = os.path.join(TMP, f"iv_{i}_en.wav"); L.denoise_wav(m["en_wav"], de)
        ge = os.path.join(TMP, f"iv_{i}_eg.wav"); L.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                  "-i", f"anullsrc=r=44100:cl=mono:d={GAP}", "-c:a", "pcm_s16le", ge])
        a_parts += [de, ge]

    ec = os.path.join(TMP, "iv_end.mp4"); L.end_card(ec, 2.6); clips.append(ec)
    se = os.path.join(TMP, "iv_e.wav"); L.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
              "-i", "anullsrc=r=44100:cl=mono:d=2.6", "-c:a", "pcm_s16le", se]); a_parts.append(se)

    # 视频交叉淡化拼接
    vcat = os.path.join(TMP, "iv_vcat.mp4")
    L.xfade_concat(clips, vcat, dur_x=0.35)
    # 音频顺接(concat)
    alst = os.path.join(TMP, "iv_a.txt")
    with open(alst, "w", encoding="utf-8") as f:
        for p in a_parts:
            f.write("file '%s'\n" % p.replace("\\", "/"))
    acat = os.path.join(TMP, "iv_acat.wav")
    L.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", alst,
           "-ar", "44100", "-ac", "1", acat])
    # BGM 垫乐(按视频总长)
    vd = L.dur(vcat)
    bgm = os.path.join(TMP, "iv_bgm.wav"); L.music_bed(vd, bgm, mood="interp")
    final = os.path.join(OUT, "interp_demo_v3_en.mp4" if EN else "interp_demo_v3.mp4")
    L.mux(vcat, acat, final, bgm=bgm, voice_gain="1.0", bgm_gain="0.22")
    print("成片:", final)


if __name__ == "__main__":
    main()
