#!/usr/bin/env python3
"""教学集拼装（老板 09-16 03:03 格式）：开头唱（jingle 副歌）→ 说唱教学（ACE rap take）+ 逐字卡拉OK 动态字幕
+ 讲到哪录到哪（curriculum footage clip 按段落切换）→ 尾卡。16:9 1920×1080。

  python make_episode.py E1 --take 426
  python make_episode.py E1 --take 426 --intro-s 8 --map a_overview,b_filters,c_thread,d_copilot

时间轴：
  [0, intro)            标题卡叠在首段录屏上 + jingle 前 intro 秒（唱段从 0 s 起）
  [intro, intro+rapdur) 说唱 take 从首个有词分段起，每 4 行一个段落 → 对应一支录屏 clip；ASS 卡拉OK 按 ASR 分段时间轴
  尾卡                  说唱尾奏 + 下一集/二维码
产物：out/<EP>/<EP>_h<take>_episode.mp4 + 抽帧；ffprobe 双流验收。
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
BG = ROOT.parent / "huanyan-brand-bg" / "cosmos-wide.jpg"
MARK = ROOT.parent / "huanyan-brand-bg" / "boundless-mark-512.png"
FONT_B, FONT_R = "C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc"
W, H = 1920, 1080


def run(cmd):
    print("  $", " ".join(str(c) for c in cmd)[:200])
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")


def ffprobe(p: Path) -> dict:
    r = run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(p)])
    if r.returncode != 0:
        raise SystemExit(f"[FAIL] ffprobe {p.name}: {r.stderr[:200]}")
    return json.loads(r.stdout)


def dur(p: Path) -> float:
    return float(ffprobe(p)["format"]["duration"])


# ── 卡片 ─────────────────────────────────────────────────────────────────────

def _bg(dim=0.35):
    im = Image.open(BG).convert("RGB")
    s = max(W / im.width, H / im.height)
    im = im.resize((round(im.width * s), round(im.height * s)), Image.LANCZOS)
    x0, y0 = (im.width - W) // 2, (im.height - H) // 2
    return Image.blend(im.crop((x0, y0, x0 + W, y0 + H)), Image.new("RGB", (W, H), (5, 6, 15)), dim)


def _mark(im, xy, hgt):
    mk = Image.open(MARK).convert("RGBA")
    mk = mk.resize((round(mk.width * hgt / mk.height), hgt), Image.LANCZOS)
    im.paste(mk, xy, mk)


def _center(d, y, s, font, fill=(255, 255, 255)):
    box = d.textbbox((0, 0), s, font=font)
    d.text(((W - (box[2] - box[0])) // 2 - box[0], y), s, font=font, fill=fill)


def title_overlay(ep: dict, dst: Path) -> None:
    """半透明标题层（RGBA），叠在首段录屏上：集号 + 标题 + 「唱」提示。"""
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, W, H), fill=(5, 6, 15, 150))
    _mark(im, ((W - 341) // 2, 230), 200)
    _center(d, 470, ep["title"], ImageFont.truetype(FONT_B, 96))
    _center(d, 610, f"智聊 ChatX 教程 · {ep['id']} · {ep.get('feature_names', ' / '.join(ep['features']))}", ImageFont.truetype(FONT_R, 44), (200, 210, 230))
    im.save(dst)


def end_card(ep: dict, nxt: str, dst: Path) -> None:
    import qrcode
    im = _bg(0.3)
    d = ImageDraw.Draw(im)
    _mark(im, (300, 200), 160)
    d.text((620, 200), f"下一集：{nxt}", font=ImageFont.truetype(FONT_B, 64), fill="white")
    d.text((620, 300), "有问题点小球问小智 · 官网 bd2026.cc", font=ImageFont.truetype(FONT_R, 44), fill=(200, 210, 230))
    qr = qrcode.QRCode(box_size=10, border=2)
    qr.add_data(f"https://bd2026.cc/download/chatx?utm_source=video&utm_campaign={ep['id'].lower()}-zh")
    qr.make(fit=True)
    im.paste(qr.make_image(fill_color="black", back_color="white").convert("RGB").resize((380, 380)), (1380, 560))
    d.text((300, 620), "智聊 ChatX · 免费开始 · 标准翻译永久免费", font=ImageFont.truetype(FONT_B, 48), fill="white")
    d.text((300, 720), "歌声与说唱由 幻声 VoiceX 生成 · 无界科技 BOUNDLESS", font=ImageFont.truetype(FONT_R, 34), fill=(170, 180, 200))
    im.save(dst)


# ── 卡拉OK 字幕 ──────────────────────────────────────────────────────────────

def _ts(t: float) -> str:
    cs = int(round(t * 100))
    return f"{cs//360000}:{cs%360000//6000:02d}:{cs%6000//100:02d}.{cs%100:02d}"


def lyric_lines(text: str) -> list[str]:
    return [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("[")]


def align_lines(lines: list[str], segs: list[dict]) -> list[tuple[float, float, str]]:
    """取「有词」的 ASR 分段（去掉 Zither Harp / Yeah 之类），按序贴歌词行；分段数≠行数时按时长均分兜底。"""
    cjk = re.compile(r"[\u4e00-\u9fff]")
    voc = [s for s in segs if len(cjk.findall(s["text"])) >= 3]
    if len(voc) == len(lines):
        return [(voc[i]["start"], voc[i]["end"], lines[i]) for i in range(len(lines))]
    if not voc:
        raise SystemExit("[FAIL] ASR 无有词分段，无法对齐")
    t0, t1 = voc[0]["start"], voc[-1]["end"]
    step = (t1 - t0) / len(lines)
    print(f"  ~ ASR 分段 {len(voc)} ≠ 行数 {len(lines)}，按均分对齐")
    return [(t0 + i * step, t0 + (i + 1) * step, lines[i]) for i in range(len(lines))]


def karaoke_ass(aligned: list[tuple[float, float, str]], offset: float, dst: Path) -> None:
    ev = []
    for t0, t1, line in aligned:
        chars = [c for c in line]
        span = max(0.3, t1 - t0)
        # 拉丁词整体一个 \k；CJK 逐字
        toks = re.findall(r"[A-Za-z0-9@/]+|[^A-Za-z0-9@/\s]|\s", line)
        toks = [t for t in toks if t.strip()]
        k_each = span * 100 / max(1, len(toks))
        body = "".join(f"{{\\k{int(round(k_each))}}}{t}" for t in toks)
        ev.append(f"Dialogue: 0,{_ts(t0+offset)},{_ts(t1+offset)},Rap,,0,0,0,,{body}")
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\nWrapStyle: 2\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        # Primary=已唱到（亮黄），Secondary=未唱（白）
        # Alignment=2 底中；MarginV=300 把卡拉OK 抬到指针 caption（bottom≈42px 高条）之上，
        # 避免大红圈/得点文案与唱词叠成一团（2026-09-16 目检）。
        "Style: Rap,Microsoft YaHei,60,&H0000E5FF,&H00FFFFFF,&H96000000,&H64000000,-1,0,0,0,100,100,3,0,1,4,2,2,80,80,300,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    dst.write_text(head + "\n".join(ev) + "\n", encoding="utf-8")


# ── 拼装 ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("episode")
    ap.add_argument("--take", type=int, required=True)
    ap.add_argument("--intro-s", type=float, default=8.0)
    ap.add_argument("--map", help="逗号分隔的 clip 名，按 4 行一段依次对应；缺省用 curriculum 顺序")
    ap.add_argument("--next", default="用客户的母语聊，像老乡一样")
    a = ap.parse_args()
    cur = json.loads((ROOT / "curriculum.json").read_text(encoding="utf-8"))
    ep = next(e for e in cur["episodes"] if e["id"] == a.episode)
    ep["feature_names"] = " / ".join(next((f["name"] for f in cur["features"] if f["id"] == fid), fid) for fid in ep["features"])
    epdir = OUT / ep["id"]
    take_json = next(p for p in OUT.glob(f"{ep['id']}_*_s*.json") if json.loads(p.read_text(encoding="utf-8")).get("history_id") == a.take)
    take = json.loads(take_json.read_text(encoding="utf-8"))
    rap_wav = take_json.with_suffix(".wav")
    lines = lyric_lines(Path(ROOT / take_json.with_suffix(".lyrics.txt").name).read_text(encoding="utf-8")
                        if (ROOT / take_json.with_suffix(".lyrics.txt").name).exists()
                        else take_json.with_suffix(".lyrics.txt").read_text(encoding="utf-8"))
    aligned = align_lines(lines, take["asr"])
    rap_t0 = max(0.0, aligned[0][0] - 1.2)             # 说唱起点（留 1.2 s 起拍）
    rap_t1 = aligned[-1][1] + 0.6
    # 开场唱：优先本集 intro take，禁止再用「老乡聊天」共用 jingle
    intro_wav = None
    # 多抽时按 ASR 逐句命中均值挑最清楚的一把（同分取最新），而不是盲取最新
    def _intro_score(j: Path) -> tuple[float, float]:
        try:
            hits = json.loads(j.read_text(encoding="utf-8")).get("line_hits") or []
            return (sum(hits) / len(hits) if hits else 0.0, j.stat().st_mtime)
        except Exception:
            return (0.0, j.stat().st_mtime)
    intro_cands = sorted(OUT.glob(f"{ep['id']}_intro*.json"), key=_intro_score, reverse=True)
    for j in intro_cands:
        w = j.with_suffix(".wav")
        if w.exists() and w.stat().st_size > 8000:
            intro_wav = w
            print(f"  intro 选 {j.name} mean_hits={_intro_score(j)[0]:.2f}")
            break
    if intro_wav is None:
        raise SystemExit(f"[FAIL] 缺本集开场唱 {ep['id']}_intro*.wav —— 先跑 make_song.py {ep['id']}_intro")
    # 下一集标题（尾卡）
    eps_all = [e for e in cur["episodes"] if re.match(r"^[ERC]\d+", e.get("id", ""))]
    try:
        ix = next(i for i, e in enumerate(eps_all) if e["id"] == ep["id"])
        next_title = eps_all[ix + 1]["title"] if ix + 1 < len(eps_all) else "bd2026.cc 免费开始"
    except StopIteration:
        next_title = a.next
    intro = a.intro_s
    # 段落 → clip
    clips = a.map.split(",") if a.map else [c["clip"] for c in ep["footage_actions"]]
    n_par = (len(aligned) + 3) // 4
    while len(clips) < n_par:
        clips.append(clips[-1])
    paragraphs = []
    for i in range(n_par):
        seg = aligned[i * 4:(i + 1) * 4]
        p0 = seg[0][0] if i > 0 else rap_t0
        p1 = seg[-1][1] if i < n_par - 1 else rap_t1
        paragraphs.append((p0, p1, epdir / f"footage_{clips[i]}.webm"))
    rap_len = rap_t1 - rap_t0
    end_d = 7.0
    total = intro + rap_len + end_d
    print(f"  intro({intro_wav.name}) {intro}s | rap {rap_t0:.1f}→{rap_t1:.1f} ({rap_len:.1f}s, {n_par} 段) | end 下一集「{next_title}」 | total {total:.1f}s")

    cards = epdir / "cards"
    cards.mkdir(exist_ok=True)
    title_overlay(ep, cards / "title.png")
    end_card(ep, next_title, cards / "end.png")
    ass = cards / "rap.ass"
    karaoke_ass(aligned, intro - rap_t0, ass)
    ass_esc = str(ass).replace("\\", "/").replace(":", "\\:")

    # 视频链：intro = 首段 clip 的导航铺垫（ready→content）叠标题层；段落 = clip 从 content_offset 起截 (p1-p0) 秒；
    # 同一 clip 连续用于多个段落时接着放而不是从头重播（2026-09-17 复盘：从头重播＝观众只看到空工作台）；end = 卡
    def _meta(clip: Path) -> dict:
        js = clip.with_suffix(".json")
        return json.loads(js.read_text(encoding="utf-8")) if js.exists() else {}

    def _ready(clip: Path) -> float:
        return float(_meta(clip).get("ready_offset") or 0.0)

    def _content(clip: Path) -> float:
        m = _meta(clip)
        return float(m.get("content_offset") or m.get("ready_offset") or 0.0)

    def _dur(clip: Path) -> float:
        m = _meta(clip)
        return float(m.get("duration") or dur(clip))

    inputs = ["-ss", f"{_ready(paragraphs[0][2]):.2f}", "-i", str(paragraphs[0][2]),
              "-loop", "1", "-t", f"{intro}", "-i", str(cards / "title.png")]
    fc = [f"[0:v]trim=duration={intro},setpts=PTS-STARTPTS,scale={W}:{H},setsar=1,fps=30,format=yuv420p[i0];",
          f"[1:v]scale={W}:{H},setsar=1,fps=30,format=rgba[t0];",
          f"[i0][t0]overlay=shortest=1,format=yuv420p[intro];"]
    idx = 2
    labels = ["[intro]"]
    prev_clip, prev_off, prev_len = None, 0.0, 0.0
    for k, (p0, p1, clip) in enumerate(paragraphs):
        ln = p1 - p0
        off = _content(clip)
        if clip == prev_clip:
            cont = prev_off + prev_len
            if _dur(clip) - cont >= min(ln, 6.0):
                off = cont            # 接着上一段放
        if _dur(clip) - off < ln:
            print(f"  ~ {clip.name} 从 {off:.1f}s 起仅 {_dur(clip) - off:.1f}s，段落 {ln:.1f}s 将循环补足")
        prev_clip, prev_off, prev_len = clip, off, ln
        print(f"  段{k+1} {clip.name} -ss {off:.1f}s × {ln:.1f}s")
        inputs += ["-ss", f"{off:.2f}", "-stream_loop", "-1", "-i", str(clip)]
        fc.append(f"[{idx}:v]trim=duration={ln:.3f},setpts=PTS-STARTPTS,scale={W}:{H},setsar=1,fps=30,format=yuv420p[p{k}];")
        labels.append(f"[p{k}]")
        idx += 1
    inputs += ["-loop", "1", "-t", f"{end_d}", "-i", str(cards / "end.png")]
    fc.append(f"[{idx}:v]scale={W}:{H},setsar=1,fps=30,format=yuv420p,trim=duration={end_d},setpts=PTS-STARTPTS[end];")
    labels.append("[end]")
    idx += 1
    fc.append("".join(labels) + f"concat=n={len(labels)}:v=1:a=0[vcat];")
    feat_name = next((f["name"] for f in cur["features"] if f["id"] == ep["features"][0]), ep["features"][0])
    fc.append(f"[vcat]subtitles='{ass_esc}',"
              # 章节标签放顶栏中部空档（搜索框右侧、急需处理左侧），别压住软件自己的标题/头像
              f"drawtext=text='{ep['id']} · {feat_name}':fontfile='C\\:/Windows/Fonts/msyhbd.ttc':fontsize=28:"
              f"fontcolor=white@0.85:box=1:boxcolor=0x1a2a5a@0.75:boxborderw=10:x=1120:y=12:"
              f"enable='between(t,{intro},{intro+rap_len})',"
              f"drawtext=text='bd2026.cc':fontfile='C\\:/Windows/Fonts/arial.ttf':fontsize=26:fontcolor=white@0.5:x=70:y=h-th-14,"
              f"fade=t=in:st=0:d=0.5,fade=t=out:st={total-0.8:.2f}:d=0.8[v];")
    # 音频链：本集开场唱前 intro 秒（淡出）+ rap [rap_t0, rap_t1+end) 淡出
    inputs += ["-i", str(intro_wav), "-i", str(rap_wav)]
    fc.append(f"[{idx}:a]atrim=duration={intro},asetpts=PTS-STARTPTS,afade=t=out:st={intro-1.0:.2f}:d=1.0[aj];")
    fc.append(f"[{idx+1}:a]atrim=start={rap_t0:.3f}:duration={rap_len+end_d:.3f},asetpts=PTS-STARTPTS,"
              f"afade=t=out:st={rap_len+end_d-2.0:.2f}:d=2.0[ar];")
    fc.append("[aj][ar]concat=n=2:v=0:a=1[a]")
    out = epdir / f"{ep['id']}_h{a.take}_episode.mp4"
    r = run(["ffmpeg", "-y", *inputs, "-filter_complex", "".join(fc), "-map", "[v]", "-map", "[a]",
             "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "192k", "-t", f"{total:.2f}", "-movflags", "+faststart", str(out)])
    if r.returncode != 0:
        raise SystemExit(f"[FAIL] ffmpeg:\n{r.stderr[-1500:]}")
    info = ffprobe(out)
    kinds = {s["codec_type"] for s in info["streams"]}
    d = float(info["format"]["duration"])
    if out.read_bytes()[4:8] != b"ftyp" or kinds != {"video", "audio"} or abs(d - total) > 1.0:
        raise SystemExit(f"[FAIL] 验收不过 streams={kinds} dur={d:.1f}/{total:.1f}")
    print(f"  [OK] {out.name}: {d:.1f}s 1920x1080 双流 {out.stat().st_size//1024}KB")
    for t in (3.0, intro + 3.0, intro + rap_len * 0.4, intro + rap_len * 0.8, total - 3.0):
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(out), "-frames:v", "1", str(epdir / f"{out.stem}_t{int(t)}.png")])
    print("  抽帧 5 张")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    sys.exit(main())
