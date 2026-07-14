# -*- coding: utf-8 -*-
"""统一专业设计系统(所有演示片共用):品牌色板 + 下三分之一字幕条 + 片头/转场/尾卡 +
设备取景框 + 音频降噪/统一响度 + 合成 BGM。让四条片版式/字体/剪辑一致、达专业水准。

设计规范:
  画布 1920x1080; 背景 #0a0d13; 主强调 青 #22d3ee, 次强调 品红 #d946ef, 成功 绿 #34d399。
  字体: 微软雅黑(msyhbd 粗/msyh 常规)。标题 84, 段标 46, 下条主 40, 下条副 28。
  字幕=左下角"下三分之一"半透明条(非居中漂浮),带左侧强调竖条。
  转场=0.4s 交叉淡化。片头 kinetic 标题, 尾卡品牌。全片 -14 LUFS 归一 + 轻 BGM。
"""
import os
import subprocess
import tempfile

BG = "0x0a0d13"
CYAN = "0x22d3ee"
MAGENTA = "0xd946ef"
GREEN = "0x34d399"
WHITE = "0xffffff"
FONT = "C\\:/Windows/Fonts/msyh.ttc"
FONT_BD = "C\\:/Windows/Fonts/msyhbd.ttc"
TMP = tempfile.gettempdir()
ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
BRAND = "无界 BOUNDLESS"
SITE = "usdt2026.cc"


def run(cmd):
    subprocess.run(cmd, check=True)


def dur(p):
    return float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                 "-of", "csv=p=0", p], capture_output=True, text=True).stdout.strip())


def esc(t):
    return t.replace("\\", "").replace("'", "\u2019").replace(":", "\uff1a").replace(",", "\uff0c")


def _dt(text, x, y, size, color=WHITE, font=FONT, border=3, bcol="black@0.6",
        st=None, en=None, alpha=None):
    f = (f"drawtext=fontfile='{font}':text='{esc(text)}':fontsize={size}:fontcolor={color}:"
         f"borderw={border}:bordercolor={bcol}:x={x}:y={y}")
    if alpha is not None:
        f += f":alpha='{alpha}'"
    if st is not None:
        f += f":enable='between(t,{st},{en})'"
    return f


def lower_third(title, sub="", accent=CYAN, st=None, en=None):
    """左下角下三分之一字幕:强调竖条 + 半透明底 + 主/副标题。返回 vf 片段列表(逗号连接)。"""
    y0 = 880
    parts = [
        # 半透明底条
        f"drawbox=x=80:y={y0}:w=1760:h=140:color=black@0.42:t=fill",
        # 左侧强调竖条
        f"drawbox=x=80:y={y0}:w=10:h=140:color={accent}:t=fill",
        _dt(title, "130", str(y0 + 22), 46, WHITE, FONT_BD, st=st, en=en),
    ]
    if sub:
        parts.append(_dt(sub, "132", str(y0 + 86), 28, accent, FONT, st=st, en=en))
    base = ",".join(parts)
    if st is not None:
        # 整条随时间显隐:用 enable 已在各 drawtext/box... drawbox 不支持 enable? 支持 enable。
        base = ",".join(p + f":enable='between(t,{st},{en})'" if p.startswith("drawbox") else p
                        for p in parts)
    return base


def seg_label(text, accent=CYAN):
    """右上角段落标签(章节感)。"""
    return (f"drawbox=x=1380:y=70:w=470:h=64:color={accent}@0.16:t=fill,"
            f"drawbox=x=1380:y=70:w=8:h=64:color={accent}:t=fill,"
            + _dt(text, "1410", "86", 34, accent, FONT_BD))


def brand_corner():
    """右上角常驻品牌小字(全片一致的水印级标识)。"""
    return _dt(BRAND, "w-tw-40", "40", 26, WHITE, FONT, border=2)


# ── 归一化:把任意片源套进 1920x1080 舞台(留边、居中、深底) ──
def stage_vf(inner_scale="scale=1920:1080:force_original_aspect_ratio=decrease"):
    return (f"{inner_scale},pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color={BG},"
            f"format=yuv420p,fps=30,settb=AVTB")


def title_card(main, sub, out, d=2.4, accent=CYAN):
    """片头/章节卡:kinetic 淡入 + 强调线扫入。"""
    vf = (
        f"drawbox=x=(w-560)/2:y=560:w=560:h=6:color={accent}:t=fill:"
        f"enable='gte(t,0.5)',"
        + _dt(main, "(w-tw)/2", "(h-th)/2-70", 84, WHITE, FONT_BD, alpha="min(t/0.5,1)") + ","
        + _dt(sub, "(w-tw)/2", "(h-th)/2+40", 40, accent, FONT, alpha="min(max((t-0.3)/0.5,0),1)")
    )
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"color=c={BG}:s=1920x1080:d={d}:r=30", "-vf", vf,
         "-an", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "20", "-preset", "medium", out])


def end_card(out, d=2.6):
    vf = (
        _dt(BRAND, "(w-tw)/2", "(h-th)/2-60", 92, WHITE, FONT_BD, alpha="min(t/0.5,1)") + ","
        + _dt(SITE, "(w-tw)/2", "(h-th)/2+64", 46, CYAN, FONT, alpha="min(max((t-0.3)/0.5,0),1)") + ","
        + f"drawbox=x=(w-460)/2:y=(h/2+40):w=460:h=5:color={CYAN}:t=fill:enable='gte(t,0.4)'"
    )
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"color=c={BG}:s=1920x1080:d={d}:r=30", "-vf", vf,
         "-an", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "20", "-preset", "medium", out])


def xfade_concat(clips, out, dur_x=0.4, trans="fade"):
    """等分辨率片段 xfade 链接(专业交叉淡化);单片直接拷。"""
    if len(clips) == 1:
        run(["ffmpeg", "-y", "-v", "error", "-i", clips[0], "-c", "copy", out])
        return
    inp = []
    for c in clips:
        inp += ["-i", c]
    ds = [dur(c) for c in clips]
    fc = ""
    prev = "0:v"
    off = ds[0] - dur_x
    for i in range(1, len(clips)):
        lbl = f"x{i}"
        fc += f"[{prev}][{i}:v]xfade=transition={trans}:duration={dur_x}:offset={off:.3f}[{lbl}];"
        prev = lbl
        off += ds[i] - dur_x
    fc = fc.rstrip(";")
    run(["ffmpeg", "-y", "-v", "error", *inp, "-filter_complex", fc, "-map", f"[{prev}]",
         "-an", "-r", "30", "-pix_fmt", "yuv420p", "-c:v", "libx264", "-crf", "20",
         "-preset", "medium", out])


def denoise_wav(inp, out):
    """人声降噪 + 统一响度(去杂音):高通 70Hz + 双 afftdn + loudnorm -16 LUFS。"""
    run(["ffmpeg", "-y", "-v", "error", "-i", inp,
         "-af", "highpass=f=70,afftdn=nf=-28,afftdn=nf=-28,loudnorm=I=-16:TP=-1.5:LRA=11",
         "-ar", "44100", "-ac", "1", "-c:a", "pcm_s16le", out])


def music_bed(total, out, key=("146.83", "220.00", "293.66", "369.99"), gain="-13dB",
              mood=""):
    """垫乐:优先 ACE-Step 原创器乐(整曲文本成曲、零版权),循环/截断到片长 +
    响度对齐 + 两端淡入淡出;权重未生成时回退四音合成铺底。
    mood 指定情绪曲(assets/bgm_<mood>.wav),缺失则回退通用 bgm_ace.wav。"""
    fo = max(0.1, total - 2)
    ace = os.path.join(ASSETS, f"bgm_{mood}.wav") if mood else ""
    if not (ace and os.path.exists(ace)):
        ace = os.path.join(ASSETS, "bgm_ace.wav")
    if os.path.exists(ace):
        run(["ffmpeg", "-y", "-v", "error", "-stream_loop", "-1", "-i", ace,
             "-t", str(total), "-af",
             f"loudnorm=I=-26:TP=-8:LRA=9,afade=t=in:st=0:d=1.5,afade=t=out:st={fo}:d=2",
             "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", out])
        return
    fc = (";".join(f"sine=frequency={f}:duration={total}[t{i}]" for i, f in enumerate(key)) + ";"
          + "".join(f"[t{i}]volume={v}[a{i}];" for i, v in enumerate((0.5, 0.34, 0.30, 0.24)))
          + "".join(f"[a{i}]" for i in range(4))
          + f"amix=inputs=4:normalize=0,tremolo=f=0.12:d=0.4,lowpass=f=1900,"
          + f"aecho=0.8:0.85:400|850:0.35|0.25,volume={gain},"
          + f"afade=t=in:st=0:d=2,afade=t=out:st={fo}:d=2[m]")
    run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={total}",
         "-filter_complex", fc, "-map", "[m]", "-ar", "44100", "-ac", "2",
         "-c:a", "pcm_s16le", out])


def mux(video, audio, out, bgm=None, voice_gain="1.0", bgm_gain="0.5"):
    """视频 + (人声[+BGM]) 合并成片。audio 为空→只用 bgm;都空→静音。"""
    if audio and bgm:
        fc = (f"[1:a]volume={voice_gain}[v];[2:a]volume={bgm_gain}[b];"
              f"[v][b]amix=inputs=2:duration=first:dropout_transition=0[a]")
        run(["ffmpeg", "-y", "-v", "error", "-i", video, "-i", audio, "-i", bgm,
             "-filter_complex", fc, "-map", "0:v", "-map", "[a]",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
             "-shortest", out])
    elif audio:
        run(["ffmpeg", "-y", "-v", "error", "-i", video, "-i", audio, "-map", "0:v", "-map", "1:a",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", out])
    elif bgm:
        run(["ffmpeg", "-y", "-v", "error", "-i", video, "-i", bgm, "-map", "0:v", "-map", "1:a",
             "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", "-shortest", out])
    else:
        run(["ffmpeg", "-y", "-v", "error", "-i", video, "-an",
             "-c:v", "copy", "-movflags", "+faststart", out])
