# -*- coding: utf-8 -*-
"""克隆音实时同传 showcase(修复版):音轨=同一 Fish 参考音克隆的 中文→英文,每句各一次、完整。
每句一个双语面板:🎤 我说中文 → 🔊 对方听英文;先播中文(我的声音)再播英文(同一把声音)=同人证明。
用法: facefusion python demo_record/compose_interp_demo.py
"""
import json
import os
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "interp2")
OUT = os.path.join(HERE, "out")
TMP = tempfile.gettempdir()
FONT = "C\\:/Windows/Fonts/msyh.ttc"
BG = "0x0b0e14"
GAP = 0.35   # 中英之间停顿


def run(cmd):
    subprocess.run(cmd, check=True)


def esc(t):
    return t.replace("'", "\u2019").replace(":", "\uff1a").replace(",", "\uff0c")


def panel(zh, en, dur, active, out):
    """双语面板:上=🎤中文, 下=🔊英文; active='zh'|'en' 高亮当前在说的一侧。"""
    zh_col = "0x22d3ee" if active == "zh" else "0x64748b"
    en_col = "0x34d399" if active == "en" else "0x64748b"
    tag = "▶ 我说中文（我的声音）" if active == "zh" else "▶ 对方听到英文（同一把声音）"
    tag_col = zh_col if active == "zh" else en_col
    vf = (
        f"drawtext=fontfile='{FONT}':text='通译 LingoX · 实时同传':fontsize=40:fontcolor=white:"
        f"x=(w-text_w)/2:y=70,"
        f"drawtext=fontfile='{FONT}':text='{esc(tag)}':fontsize=44:fontcolor={tag_col}:"
        f"borderw=2:bordercolor=black@0.5:x=(w-text_w)/2:y=200,"
        f"drawtext=fontfile='{FONT}':text='{esc(zh)}':fontsize=58:fontcolor={zh_col}:"
        f"borderw=2:bordercolor=black@0.6:x=(w-text_w)/2:y=430,"
        f"drawtext=fontfile='{FONT}':text='{esc(en)}':fontsize=46:fontcolor={en_col}:"
        f"borderw=2:bordercolor=black@0.6:x=(w-text_w)/2:y=560"
    )
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"color=c={BG}:s=1920x1080:d={dur}:r=30", "-vf", vf,
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "20", "-preset", "medium", out])


def sil(dur, out):
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"anullsrc=r=44100:cl=mono:d={dur}", "-c:a", "pcm_s16le", out])


def title_card(text, sub, out, dur):
    vf = (f"drawtext=fontfile='{FONT}':text='{esc(text)}':fontsize=92:fontcolor=white:"
          f"x=(w-text_w)/2:y=(h-text_h)/2-40:alpha='min(t/0.4,1)',"
          f"drawtext=fontfile='{FONT}':text='{esc(sub)}':fontsize=40:fontcolor=0x22d3ee:"
          f"x=(w-text_w)/2:y=(h-text_h)/2+70:alpha='min(t/0.6,1)'")
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"color=c={BG}:s=1920x1080:d={dur}:r=30", "-vf", vf,
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "20", "-preset", "medium", out])


def main():
    meta = json.load(open(os.path.join(SRC, "lines.json"), encoding="utf-8"))
    v_parts, a_parts = [], []

    def add_v(p):
        v_parts.append(p)

    def add_a(p):
        a_parts.append(p)

    # 片头
    tc = os.path.join(TMP, "ip_title.mp4")
    title_card("克隆音实时同传", "我说中文 · 对方听英文 · 还是我自己的声音", tc, 2.2)
    add_v(tc)
    s = os.path.join(TMP, "ip_title.wav"); sil(2.2, s); add_a(s)

    for m in meta:
        i = m["i"]
        # 中文段:播我说的中文(我的声音),高亮中文
        pz = os.path.join(TMP, f"ip_{i}_zh.mp4")
        panel(m["zh"], m["en"], m["zh_dur"] + GAP, "zh", pz); add_v(pz)
        add_a(m["zh_wav"])
        sg = os.path.join(TMP, f"ip_{i}_g1.wav"); sil(GAP, sg); add_a(sg)
        # 英文段:播克隆英文(同一把声音),高亮英文
        pe = os.path.join(TMP, f"ip_{i}_en.mp4")
        panel(m["zh"], m["en"], m["en_dur"] + GAP, "en", pe); add_v(pe)
        add_a(m["en_wav"])
        sg2 = os.path.join(TMP, f"ip_{i}_g2.wav"); sil(GAP, sg2); add_a(sg2)

    # 尾卡
    ec = os.path.join(TMP, "ip_end.mp4")
    title_card("无界 BOUNDLESS", "usdt2026.cc", ec, 2.5); add_v(ec)
    se = os.path.join(TMP, "ip_end.wav"); sil(2.5, se); add_a(se)

    # 拼视频
    vlst = os.path.join(TMP, "ip_v.txt")
    with open(vlst, "w", encoding="utf-8") as f:
        for p in v_parts:
            f.write("file '%s'\n" % p.replace("\\", "/"))
    vcat = os.path.join(TMP, "ip_vcat.mp4")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", vlst, "-c", "copy", vcat])
    # 拼音频(wav 统一 44100 mono)
    alst = os.path.join(TMP, "ip_a.txt")
    with open(alst, "w", encoding="utf-8") as f:
        for p in a_parts:
            f.write("file '%s'\n" % p.replace("\\", "/"))
    acat = os.path.join(TMP, "ip_acat.wav")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", alst,
         "-ar", "44100", "-ac", "1", acat])
    # 合并(视频为准,音频跟随;末尾静音补齐由 -shortest 处理)
    final = os.path.join(OUT, "interp_demo_v2.mp4")
    run(["ffmpeg", "-y", "-v", "error", "-i", vcat, "-i", acat,
         "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", final])
    print("成片:", final)


if __name__ == "__main__":
    main()
