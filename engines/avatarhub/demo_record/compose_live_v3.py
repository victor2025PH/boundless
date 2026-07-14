# -*- coding: utf-8 -*-
"""直播实时换脸换声 v3:直播工作台版式——
主画面 = 换脸+变声后的直播输出(v2_0002_liu_voiced),右下画中画 = 摄像头原始输入(0002),
左上 LIVE 红点(闪烁)+时长,延迟标签,统一片头/下三分之一/尾卡/BGM。
DEMO_LANG=en 输出英文版。
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

CAM = os.path.join(STAR, "0002.MOV")                       # 摄像头原始输入
LIVE = os.path.join(OUT, "v2_0002_liu_voiced.mp4")         # 换脸+变声输出

EN = os.environ.get("DEMO_LANG", "").lower() == "en"
TXT = {
    "直播实时换脸换声": "Live Face & Voice Swap",
    "摄像头进 · 换脸变声出 · 直播级延迟": "Camera in · Swapped face & voice out · Stream-ready",
    "直播输出 · 已换脸 + 已变声": "Live output · Face & voice swapped",
    "摄像头原始输入 → 引擎实时处理 → OBS 虚拟摄像头": "Camera input → Real-time engine → OBS virtual camera",
    "摄像头原始输入": "Camera input",
    "端到端延迟 ~120ms": "End-to-end latency ~120ms",
}


def T(s):
    return TXT.get(s, s) if EN else s


def main():
    t = 15.5
    clips = []
    tc = os.path.join(TMP, "lv_title.mp4")
    L.title_card(T("直播实时换脸换声"), T("摄像头进 · 换脸变声出 · 直播级延迟"), tc)
    clips.append(tc)

    # 主台:输出画面铺满舞台;PiP 380x214 右下,灰边+标签;LIVE 红点闪烁;延迟标签
    s1 = os.path.join(TMP, "lv_main.mp4")
    fc = (
        f"[0:v]trim=0:{t},setpts=PTS-STARTPTS,{L.stage_vf()}[main];"
        f"[1:v]trim=0:{t},setpts=PTS-STARTPTS,scale=380:214:force_original_aspect_ratio=increase,"
        f"crop=380:214,pad=388:222:4:4:color=0x94a3b8@0.8,fps=30,settb=AVTB[pip];"
        f"[main][pip]overlay=x=1492:y=700[o1];"
        # LIVE 徽章:红底圆点(闪烁)+文字
        f"[o1]drawbox=x=64:y=64:w=150:h=54:color=black@0.55:t=fill,"
        f"drawbox=x=64:y=64:w=6:h=54:color=0xef4444:t=fill,"
        f"drawbox=x=86:y=85:w=12:h=12:color=0xef4444:t=fill:enable='lt(mod(t,1.2),0.7)',"
        + L._dt("LIVE", "112", "78", 30, "0xef4444", L.FONT_BD, border=0) + ","
        # 延迟标签(贴 PiP 上方)
        + L._dt(T("端到端延迟 ~120ms"), "1492", "666", 24, L.CYAN, L.FONT) + ","
        + L._dt(T("摄像头原始输入"), "1500", "930", 24, L.WHITE, L.FONT_BD) + ","
        + L.seg_label(T("直播输出 · 已换脸 + 已变声"), L.CYAN).replace("w=470", "w=560").replace("x=1380", "x=1290") + ","
        + L.lower_third(T("直播实时换脸换声"), T("摄像头原始输入 → 引擎实时处理 → OBS 虚拟摄像头"), L.CYAN)
        + "," + L.brand_corner() + "[v]"
    )
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", LIVE, "-i", CAM,
                    "-filter_complex", fc, "-map", "[v]", "-map", "0:a",
                    "-t", str(t), "-r", "30", "-pix_fmt", "yuv420p",
                    "-c:v", "libx264", "-crf", "20", "-preset", "medium",
                    "-c:a", "aac", "-b:a", "192k", "-ar", "48000", s1], check=True)
    clips.append(s1)

    ec = os.path.join(TMP, "lv_end.mp4"); L.end_card(ec); clips.append(ec)

    # 拼接:片头/尾卡无声,主段带变声人声 → 用 concat(硬切),BGM 全程铺底
    parts = []
    for i, c in enumerate(clips):
        p = os.path.join(TMP, f"lv_p{i}.mp4")
        has_a = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a",
                                "-show_entries", "stream=codec_type", "-of", "csv=p=0", c],
                               capture_output=True, text=True).stdout.strip()
        if has_a:
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", c, "-c:v", "copy",
                            "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", p], check=True)
        else:
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", c, "-f", "lavfi",
                            "-i", "anullsrc=r=48000:cl=stereo", "-map", "0:v", "-map", "1:a",
                            "-c:v", "copy", "-c:a", "aac", "-shortest", p], check=True)
        parts.append(p)
    lst = os.path.join(TMP, "lv_c.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for p in parts:
            f.write("file '%s'\n" % p.replace("\\", "/"))
    cat = os.path.join(TMP, "lv_cat.mp4")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst,
                    "-c:v", "libx264", "-crf", "20", "-preset", "medium",
                    "-c:a", "aac", "-b:a", "192k", cat], check=True)

    vd = L.dur(cat)
    bgm = os.path.join(TMP, "lv_bgm.wav"); L.music_bed(vd, bgm)
    final = os.path.join(OUT, "live_demo_v3_en.mp4" if EN else "live_demo_v3.mp4")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", cat, "-i", bgm,
                    "-filter_complex",
                    "[0:a]volume=1.0[v];[1:a]volume=0.30[b];[v][b]amix=inputs=2:duration=first:dropout_transition=0[a]",
                    "-map", "0:v", "-map", "[a]", "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", final], check=True)
    print("成片:", final)


if __name__ == "__main__":
    main()
