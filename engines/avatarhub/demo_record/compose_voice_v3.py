# -*- coding: utf-8 -*-
"""声音克隆·情感 TTS v3:把真实录屏(voice_take3_raw)统一成 v3 设计系统——
片头卡 + 下三分之一字幕 + 品牌角标 + 尾卡 + 轻 BGM;沿用 v2 的精剪窗口与文案节拍。
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compose_lib as L

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
RAW = os.path.join(OUT, "voice_take3_raw.mp4")
DELOGO = "delogo=x=776:y=2:w=344:h=38"
TMP = L.TMP

# 双语:DEMO_LANG=en 输出英文版(voice_demo_v3_en.mp4),文案查表、管线共用
EN = os.environ.get("DEMO_LANG", "").lower() == "en"
TXT = {
    "声音克隆 · 情感 TTS": "Voice Cloning · Emotional TTS",
    "全程真实界面实录 · 真实引擎输出": "Real UI capture · Real engine output",
    "打一句话 · 选一种情感": "Type a line · Pick an emotion",
    "同一把克隆声音 · 多种情绪": "One cloned voice · Many emotions",
    "选「开心」· 点 开口说话": "Choose Happy · Hit Speak",
    "开心版 · 30 秒样本克隆的音色": "Happy — cloned from a 30s sample",
    "情绪、语气由引擎实时渲染": "Emotion & tone rendered live",
    "同一句话 · 换成「悲伤」": "Same line · Switched to Sad",
    "悲伤版 · 情绪张力完全不同": "Sad — a totally different feel",
    "同一句话 · 两种人生": "One line · Two moods",
    "切英文 · 还是同一把克隆声音": "Switch to English · Same cloned voice",
    "多语种 · 音色一致": "Multilingual · Consistent timbre",
    "English · 音色保持一致": "English · The very same voice",
    "30 秒样本克隆 · 多语种带情感": "Clone from 30s · Multilingual & emotional",
}


def T(s):
    return TXT.get(s, s) if EN else s

# 精剪 v3.1:在静音边界剪(30.5-39.4 开心播放 / 59.5-66.9 悲伤播放 / 88.0-92.4 英文),
# 打字等待段加速——总长 65.8s → ~53s,信息密度更高。
# (原片起, 原片止, 速度, [(局部起,局部止,主标题,副标题)...])
SEGS = [
    (3.4, 13.0, 1.6, [(0.3, 2.9, "声音克隆 · 情感 TTS", "全程真实界面实录 · 真实引擎输出"),
                      (3.1, 5.8, "打一句话 · 选一种情感", "")]),
    (13.0, 19.5, 1.0, [(0.4, 5.6, "选「开心」· 点 开口说话", "同一把克隆声音 · 多种情绪")]),
    (29.8, 39.7, 1.0, [(0.5, 8.9, "开心版 · 30 秒样本克隆的音色", "情绪、语气由引擎实时渲染")]),
    (43.0, 46.5, 1.0, [(0.2, 3.3, "同一句话 · 换成「悲伤」", "")]),
    (58.6, 67.2, 1.0, [(0.5, 8.0, "悲伤版 · 情绪张力完全不同", "同一句话 · 两种人生")]),
    (67.2, 80.5, 1.8, [(0.5, 7.0, "切英文 · 还是同一把克隆声音", "多语种 · 音色一致")]),
    (87.4, 93.5, 1.0, [(0.5, 5.2, "English · 音色保持一致", "One voice · Any language")]),
]
# 原片 ~4.4s 起顶部就有红色运维横幅(直播停更提醒),几乎全程在——统一用页面底色盖掉
COVER_BANNER = "drawbox=x=0:y=46:w=1920:h=52:color=0x141923:t=fill"


def seg_clip(a, b, speed, caps, out):
    t = (b - a) / speed
    vf_parts = [f"trim={a}:{b}", "setpts=PTS-STARTPTS", DELOGO, COVER_BANNER]
    if speed != 1.0:
        vf_parts.append(f"setpts=PTS/{speed}")
    vf_parts += ["fps=30", "settb=AVTB", "format=yuv420p"]
    vf = ",".join(vf_parts)
    for (st, en, main, sub) in caps:
        vf += "," + L.lower_third(T(main), T(sub), L.CYAN, st=st, en=en)
    vf += "," + L.brand_corner()
    af = f"atrim={a}:{b},asetpts=PTS-STARTPTS"
    if speed != 1.0:
        af += f",atempo={speed}"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", RAW,
                    "-filter_complex",
                    f"[0:v]{vf}[v];[0:a]{af}[a]",
                    "-map", "[v]", "-map", "[a]", "-t", str(t),
                    "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", out], check=True)


def silent(video, out):
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", video, "-f", "lavfi",
                    "-i", "anullsrc=r=48000:cl=stereo", "-map", "0:v", "-map", "1:a",
                    "-c:v", "copy", "-c:a", "aac", "-shortest", out], check=True)


def main():
    parts = []
    tc = os.path.join(TMP, "vv_t.mp4"); L.title_card(T("声音克隆 · 情感 TTS"), T("30 秒样本克隆 · 多语种带情感"), tc)
    tca = os.path.join(TMP, "vv_t_a.mp4"); silent(tc, tca); parts.append(tca)
    for i, (a, b, sp, caps) in enumerate(SEGS):
        p = os.path.join(TMP, f"vv_s{i}.mp4"); seg_clip(a, b, sp, caps, p); parts.append(p)
    ec = os.path.join(TMP, "vv_e.mp4"); L.end_card(ec)
    eca = os.path.join(TMP, "vv_e_a.mp4"); silent(ec, eca); parts.append(eca)

    lst = os.path.join(TMP, "vv_c.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in parts:
            f.write("file '%s'\n" % p.replace("\\", "/"))
    cat = os.path.join(TMP, "vv_cat.mp4")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst,
                    "-c:v", "libx264", "-crf", "20", "-preset", "medium",
                    "-c:a", "aac", "-b:a", "192k", cat], check=True)
    vd = L.dur(cat)
    bgm = os.path.join(TMP, "vv_bgm.wav"); L.music_bed(vd, bgm)
    final = os.path.join(OUT, "voice_demo_v3_en.mp4" if EN else "voice_demo_v3.mp4")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", cat, "-i", bgm,
                    "-filter_complex",
                    "[0:a]volume=1.0[v];[1:a]volume=0.30[b];[v][b]amix=inputs=2:duration=first:dropout_transition=0[a]",
                    "-map", "0:v", "-map", "[a]", "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", final], check=True)
    print("成片:", final)


if __name__ == "__main__":
    main()
