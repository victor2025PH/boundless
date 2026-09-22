#!/usr/bin/env python3
"""P 系列官网发布版出厂 + 上传 + 上架（2026-09-17）。

  python publish_persona_web.py                 # 出厂 P1 P3 P4 → out/_web_persona/
  python publish_persona_web.py P1              # 只做一集
  python publish_persona_web.py --upload        # 出厂后 scp 到 /var/www/media/scenarios/chatx/ 并上架 /videos
  python publish_persona_web.py --upload-only   # 用已出厂产物直接上传上架

出厂五件事（任一步不过即非零退出，遵守媒体产物验证纪律）：
  1. 脱敏：按成片 meta 的 `zoom_timeline` **确定性**定位会话列表在画面里的位置（不靠模板匹配）——
     全景段列表在 (66,268)-(366,1060)；列表推近段（crop 40,100 ×1.5）列表在 (39,252)-(489,1080)；
     聊天栏/工具箱推近段列表已被裁出画面，无需处理。真实联系人昵称/头像/预览一律高斯模糊。
  2. 裁尾卡：网页端播放列表负责「下一条」，二维码对着自己屏幕没意义 → 去掉 6 s 尾卡，补视频 0.6 s / 音频 1.5 s 淡出。
  3. 转码：H.264 High@L4.1 + yuv420p + faststart + AAC 48k（iOS/移动端硬解上限）。
  4. 海报：1280×720「谁在用智聊」模板（人物 + 岗位 + 标题 + 该集 UI 截图），JPEG ≤ 160 KB。
  5. 双验：magic bytes + moov 在 mdat 前 + ffprobe（profile/level/画幅/双流/时长）+ QA 抽帧。

上传：视频与海报都进 nginx 直出目录 `/var/www/media/scenarios/chatx/`（部署不覆盖），
上架走 `/api/admin/feed`（ai=false 真实引擎输出；默认 broadcast=false 不发 TG 频道），
ADMIN_KEY 只在服务器侧从 /home/ubuntu/yuntech/.env.local 读取，不落本地日志。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shlex
import struct
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
WEB = OUT / "_web_persona"
QA = WEB / "qa"
FONT_B, FONT_R = "C:/Windows/Fonts/msyhbd.ttc", "C:/Windows/Fonts/msyh.ttc"
W, H = 1920, 1080
PW, PH = 1280, 720
END_D = 6.0          # 成片尾卡时长（make_persona_episode.END_D）
HOOK_D = 3.2         # 人物卡时长

# 会话列表在输出画面里的矩形：景别 → (x0,y0,x1,y1)；None = 列表已被裁出画面
LIST_RECT: dict[str, tuple[int, int, int, int] | None] = {
    "": (66, 262, 372, 1080),                 # 全景（1:1）
    "zoom_list": (30, 246, 495, 1080),         # crop(40,100) ×1.5
    "list": (30, 246, 495, 1080),
    "list_clean": None,                        # 列表已 list_filter 过滤到只剩舞台联系人，不打码
    "zoom": None, "chat": None, "zoom_low": None, "low": None, "zoom_panel": None, "panel": None,
    "zoom_guide": None,                        # F2 接入引导页：整页没有会话列表
}

SITE = "https://bd2026.cc"
SSH_HOST = "ubuntu@165.154.233.121"
SSH_KEY = str(Path.home() / ".ssh" / "hualing_deploy")
REMOTE_DIR = "/var/www/media/scenarios/chatx"
MEDIA_URL = "/media/scenarios/chatx"
ENV_FILE = "/home/ubuntu/yuntech/.env.local"


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise SystemExit(f"[FAIL] {cmd[0]}: {(r.stderr or r.stdout)[-1200:]}")
    return r


def ffprobe(p: Path) -> dict:
    return json.loads(run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(p)]).stdout)


def frame_at(p: Path, t: float) -> np.ndarray:
    cap = cv2.VideoCapture(str(p))
    cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
    ok, fr = cap.read()
    cap.release()
    if not ok:
        raise SystemExit(f"[FAIL] 抽帧失败 {p.name} @ {t}s")
    return fr


# ── 脱敏区间 ─────────────────────────────────────────────────────────────────

def blur_ranges(zoom_timeline: list[list], cut_at: float) -> dict[tuple[int, int, int, int], list[tuple[float, float]]]:
    """把 zoom_timeline 折成 {矩形: [(t0,t1)…]}，相邻同矩形区间合并；超过 cut_at 的部分丢掉。"""
    out: dict[tuple[int, int, int, int], list[tuple[float, float]]] = {}
    for a, b, z in zoom_timeline:
        rect = LIST_RECT.get(z or "", LIST_RECT[""])
        if rect is None:
            continue
        a, b = float(a), min(float(b), cut_at)
        if b - a <= 0.05:
            continue
        rs = out.setdefault(rect, [])
        if rs and a - rs[-1][1] < 0.12:
            rs[-1] = (rs[-1][0], b)
        else:
            rs.append((a, b))
    return out


def encode(src: Path, dst: Path, total: float, blurs: dict) -> None:
    fade_v, fade_a = 0.6, 1.5
    layers = [(rect, rng) for rect, rng in blurs.items() if rng]
    if layers:
        n = len(layers)
        vf = f"[0:v]split={n + 1}[base]" + "".join(f"[t{k}]" for k in range(n)) + ";"
        cur = "base"
        for k, (rect, rng) in enumerate(layers):
            x0, y0, x1, y1 = rect
            vf += (f"[t{k}]crop={x1 - x0}:{y1 - y0}:{x0}:{y0},"
                   f"boxblur=luma_radius=18:luma_power=2:chroma_radius=9:chroma_power=2[bl{k}];")
            enable = "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in rng)
            vf += f"[{cur}][bl{k}]overlay={x0}:{y0}:enable='{enable}'[o{k}];"
            cur = f"o{k}"
        vf += f"[{cur}]fade=t=out:st={total - fade_v:.2f}:d={fade_v}[v]"
    else:
        vf = f"[0:v]fade=t=out:st={total - fade_v:.2f}:d={fade_v}[v]"
    af = (f"[0:a]atrim=0:{total:.3f},asetpts=PTS-STARTPTS,"
          f"afade=t=out:st={total - fade_a:.2f}:d={fade_a}[a]")
    run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-filter_complex", vf + ";" + af,
         "-map", "[v]", "-map", "[a]", "-t", f"{total:.3f}",
         "-c:v", "libx264", "-profile:v", "high", "-level", "4.1", "-pix_fmt", "yuv420p",
         "-crf", "21", "-preset", "slow", "-maxrate", "3M", "-bufsize", "6M",
         "-c:a", "aac", "-b:a", "128k", "-ar", "48000", "-ac", "2",
         "-movflags", "+faststart", str(dst)])
def verify_mp4(p: Path, expect_dur: float, size: tuple[int, int]) -> dict:
    data = p.read_bytes()
    if data[4:8] != b"ftyp":
        raise SystemExit(f"[FAIL] {p.name} 不是 MP4（magic {data[4:8]!r}）")
    pos, order = 0, []
    while pos + 8 <= len(data) and len(order) < 6:
        n, = struct.unpack(">I", data[pos:pos + 4])
        typ = data[pos + 4:pos + 8].decode("latin1")
        order.append(typ)
        if n == 0 or typ == "mdat":
            break
        pos += n
    if "moov" not in order or ("mdat" in order and order.index("moov") > order.index("mdat")):
        raise SystemExit(f"[FAIL] {p.name} moov 不在前：{order}")
    info = ffprobe(p)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    if not any(s["codec_type"] == "audio" for s in info["streams"]):
        raise SystemExit(f"[FAIL] {p.name} 无音轨")
    if v["codec_name"] != "h264" or v.get("profile") != "High" or int(v.get("level", 0)) > 41:
        raise SystemExit(f"[FAIL] {p.name} 编码不合规 {v['codec_name']}/{v.get('profile')}/L{v.get('level')}")
    if (v["width"], v["height"]) != size or v.get("pix_fmt") != "yuv420p":
        raise SystemExit(f"[FAIL] {p.name} 画幅 {v['width']}x{v['height']} {v.get('pix_fmt')}")
    d = float(info["format"]["duration"])
    if abs(d - expect_dur) > 0.6:
        raise SystemExit(f"[FAIL] {p.name} 时长 {d:.2f} ≠ {expect_dur:.2f}")
    return {"duration": round(d, 2), "size": p.stat().st_size, "level": int(v["level"]),
            "sha256": hashlib.sha256(data).hexdigest()[:16]}


# ── 海报 ─────────────────────────────────────────────────────────────────────

def _glow(im: Image.Image, cx: int, cy: int, r: int, rgb: tuple[int, int, int], alpha: int) -> None:
    layer = Image.new("RGBA", im.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse((cx - r, cy - r, cx + r, cy + r), fill=rgb + (alpha,))
    im.alpha_composite(layer.filter(ImageFilter.GaussianBlur(r * 0.6)))


def _gradient_text(im: Image.Image, xy: tuple[int, int], text: str, font: ImageFont.FreeTypeFont,
                   c0=(34, 211, 238), c1=(139, 92, 246)) -> None:
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).text(xy, text, font=font, fill=255)
    box = mask.getbbox() or (xy[0], xy[1], xy[0] + 1, xy[1] + 1)
    grad = Image.new("RGBA", im.size, (0, 0, 0, 0))
    gp = grad.load()
    x0, x1 = box[0], max(box[2], box[0] + 1)
    for x in range(x0, x1):
        k = (x - x0) / (x1 - x0)
        col = tuple(round(c0[i] + (c1[i] - c0[i]) * k) for i in range(3)) + (255,)
        for y in range(box[1], box[3]):
            gp[x, y] = col
    im.paste(grad, (0, 0), mask)


def _rounded(im: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.width - 1, im.height - 1), radius=radius, fill=255)
    out = im.convert("RGBA")
    out.putalpha(mask)
    return out
def poster(ep: dict, dur_s: float, shot_bgr: np.ndarray, dst: Path) -> None:
    im = Image.new("RGBA", (PW, PH), (5, 6, 15, 255))
    _glow(im, int(PW * 0.18), int(PH * 0.08), 360, (139, 92, 246), 70)
    _glow(im, int(PW * 0.85), 0, 320, (34, 211, 238), 60)
    grid = Image.new("RGBA", im.size, (0, 0, 0, 0))
    gd = ImageDraw.Draw(grid)
    for x in range(0, PW, 64):
        gd.line((x, 0, x, PH), fill=(255, 255, 255, 10))
    for y in range(0, PH, 64):
        gd.line((0, y, PW, y), fill=(255, 255, 255, 10))
    im.alpha_composite(grid)

    shot = Image.fromarray(cv2.cvtColor(shot_bgr, cv2.COLOR_BGR2RGB))
    crop = shot.crop((360, 60, 1880, 1060))
    card_w = 560
    card = crop.resize((card_w, round(card_w * crop.height / crop.width)), Image.LANCZOS)
    card = _rounded(card, 16)
    cx, cy = PW - card_w - 44, (PH - card.height) // 2 + 6
    shadow = Image.new("RGBA", im.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((cx + 8, cy + 18, cx + card_w + 8, cy + card.height + 18), radius=16, fill=(0, 0, 0, 160))
    im.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(22)))
    im.alpha_composite(card, (cx, cy))
    ImageDraw.Draw(im).rounded_rectangle((cx, cy, cx + card_w - 1, cy + card.height - 1), radius=16, outline=(255, 255, 255, 40), width=1)

    d = ImageDraw.Draw(im)
    left, maxw = 64, PW - card_w - 44 - 64 - 36
    # 教学片（F 系列）海报眉题跟成片人物卡一致（ep.card_label），不再写「谁在用」
    d.text((left, 60), ep.get("card_label") or "谁在用智聊 ChatX", font=ImageFont.truetype(FONT_R, 24), fill=(148, 163, 184, 255))
    _gradient_text(im, (left - 4, 104), ep["persona"], ImageFont.truetype(FONT_B, 92))
    d.text((left, 226), f"{ep['job']} · {ep['company']}", font=ImageFont.truetype(FONT_R, 26), fill=(203, 213, 225, 255))
    tf = ImageFont.truetype(FONT_B, 44)
    lines, cur = [], ""
    for ch in ep["title"]:
        if d.textlength(cur + ch, font=tf) > maxw and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)
    y = 316
    for ln in lines[:2]:
        d.text((left, y), ln, font=tf, fill=(255, 255, 255, 255))
        y += 58
    m, s = divmod(int(round(dur_s)), 60)
    plat = ep["platform"].replace("+", " + ").title()
    d.text((left, y + 16), f"{plat} · {ep['lang_pair']} · {m}:{s:02d}", font=ImageFont.truetype(FONT_R, 22), fill=(148, 163, 184, 255))
    pill = Image.new("RGBA", im.size, (0, 0, 0, 0))
    ImageDraw.Draw(pill).rounded_rectangle((left, PH - 92, left + 104, PH - 62), radius=15,
                                          fill=(34, 211, 238, 36), outline=(34, 211, 238, 90))
    im.alpha_composite(pill)
    d = ImageDraw.Draw(im)
    d.text((left + 16, PH - 88), "真机录屏", font=ImageFont.truetype(FONT_B, 17), fill=(34, 211, 238, 255))
    d.text((left + 118, PH - 88), "歌声与念白由幻声 VoiceX 生成 · bd2026.cc", font=ImageFont.truetype(FONT_R, 17), fill=(100, 116, 139, 255))

    rgb = im.convert("RGB")
    q = 86
    while True:
        rgb.save(dst, "JPEG", quality=q, optimize=True, progressive=True)
        if dst.stat().st_size <= 160 * 1024 or q <= 60:
            break
        q -= 6
    if dst.read_bytes()[:2] != b"\xff\xd8":
        raise SystemExit(f"[FAIL] {dst.name} 不是 JPEG")


# ── 上传 / 上架 ──────────────────────────────────────────────────────────────

def ssh(cmd: str, timeout: int = 180) -> str:
    r = run(["ssh", "-o", "StrictHostKeyChecking=accept-new", "-i", SSH_KEY, SSH_HOST, cmd])
    return (r.stdout or "").strip()

def scp(local: Path, remote_name: str) -> None:
    run(["scp", "-o", "StrictHostKeyChecking=accept-new", "-i", SSH_KEY, str(local),
         f"{SSH_HOST}:{REMOTE_DIR}/{remote_name}"])


def upload(entry: dict, media: bool = True) -> dict:
    """scp 视频+海报 → 远端 sha256 复核 → /api/admin/feed 上架（key 在服务器侧读，不出现在本地命令行）。
    media=False：只改条目元数据/触发广播，不重传媒体（远端 sha 仍复核，防止条目指向不存在的文件）。"""
    mp4, jpg = WEB / entry["file"], WEB / entry["poster"]
    ssh(f"mkdir -p {REMOTE_DIR}")
    if media:
        for p in (mp4, jpg):
            scp(p, p.name)
    remote_sha = (ssh(f"sha256sum {REMOTE_DIR}/{mp4.name} 2>/dev/null | cut -c1-16").split() or ["-"])[0]
    if remote_sha != entry["sha256_16"]:
        raise SystemExit(f"[FAIL] {mp4.name} 远端 sha {remote_sha} ≠ 本地 {entry['sha256_16']}"
                         + ("" if media else "（--meta-only 要求媒体已在远端）"))
    # 同名覆盖 + nginx `cache-control: max-age=604800`（7 天）：不带版本号，重录换片后一周内老观众
    # 看到的还是旧片（2026-09-18 首批重录实锤）。用内容 sha 作 ?v=，文件名不变、条目 id 不变，
    # 频道旧帖链接仍可达；服务端 /api/admin/feed 只校验 ^/media/ 前缀，query 不影响。
    _v = entry["sha256_16"]
    payload = {
        "id": entry["feed_id"],
        "date": entry["date"],
        "title_zh": entry["title_zh"], "title_en": entry["title_en"],
        "desc_zh": entry["desc_zh"], "desc_en": entry["desc_en"],
        "src": f"{MEDIA_URL}/{mp4.name}?v={_v}",
        "poster": f"{MEDIA_URL}/{jpg.name}?v={_v}",
        "ai": False,
        "broadcast": entry.get("broadcast", False),
    }
    # payload 走文件（scp → curl -d @file）：中文 + 全角标点经 shell 引号转义易被折断（2026-09-17 上架 500 实锤）
    body_local = WEB / f"_payload_{entry['feed_id']}.json"
    body_local.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    remote_body = f"/tmp/{body_local.name}"
    run(["scp", "-o", "StrictHostKeyChecking=accept-new", "-i", SSH_KEY, str(body_local), f"{SSH_HOST}:{remote_body}"])
    out = ssh(
        "set -a; . " + ENV_FILE + " >/dev/null 2>&1; set +a; "
        # 走 https://bd2026.cc（经 nginx）：直连 127.0.0.1:3000 缺代理头会被 Next 拒成 400（2026-09-17 实测）
        f"curl -s -X POST {SITE}/api/admin/feed "
        f"-H 'Content-Type: application/json; charset=utf-8' "
        f"-H \"x-setup-key: ${{ADMIN_KEY:-$TELEGRAM_SETUP_KEY}}\" --data-binary @{remote_body}; "
        f"rm -f {remote_body}"
    )
    try:
        res = json.loads(out)
    except Exception:
        raise SystemExit(f"[FAIL] 上架回包不是 JSON：{out[:300]}")
    if not res.get("ok"):
        raise SystemExit(f"[FAIL] 上架失败：{out[:300]}")
    live = ssh(f"curl -s -o /dev/null -w '%{{http_code}}' -r 0-999 '{SITE}{payload['src']}'")   # 带 ?v= 须引号防 glob
    live_p = ssh(f"curl -s -o /dev/null -w '%{{http_code}}' '{SITE}{payload['poster']}'")
    return {"feed": res, "http_mp4": live, "http_poster": live_p, "src": payload["src"]}


def feed_text(ep: dict) -> dict:
    """上架文案：中文取剧本，英文取 ep.en（2026-09-18 线上 EN 视图曾直接露中文 job/hook，官网英文访客看不懂）。"""
    en = ep.get("en") or {}
    if not all(en.get(k) for k in ("job", "title", "hook", "trigger")):
        raise SystemExit(f"[FAIL] {ep['id']} 缺 en.job/title/hook/trigger —— 英文上架文案不得回退成中文")
    return {
        "title_zh": f"{ep['title']}｜{ep['persona']} · {ep['job']}",
        "title_en": f"{en['title']} | {ep['persona']} · {en['job']}",
        "desc_zh": f"{ep['trigger']} —— {ep['hook']}。真机录屏，念白与歌声由幻声 VoiceX 生成。",
        "desc_en": f"{en['trigger']} — {en['hook']}. Real screen recording; narration and vocals generated by VoiceX.",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("eps", nargs="*", help="P1 P3 P4；缺省=首批全部")
    ap.add_argument("--upload", action="store_true", help="出厂后上传并上架")
    ap.add_argument("--upload-only", action="store_true", help="跳过出厂，直接上传已出厂产物")
    ap.add_argument("--broadcast", action="store_true", help="同时发 Telegram 频道（默认不发；服务端幂等，已发过不重发）")
    ap.add_argument("--meta-only", action="store_true", help="只重传条目元数据（标题/文案/广播），不重传媒体")
    a = ap.parse_args()
    sc = json.loads((ROOT / "scenarios_persona.json").read_text(encoding="utf-8"))
    eps = {e["id"]: e for e in sc["episodes"]}
    ids = a.eps or [e["id"] for e in sc["episodes"] if e.get("batch") == 1]
    WEB.mkdir(parents=True, exist_ok=True)
    QA.mkdir(exist_ok=True)
    man_path = WEB / "manifest.json"
    manifest = json.loads(man_path.read_text(encoding="utf-8")) if man_path.exists() else {}

    for pid in ids:
        ep = eps[pid]
        epdir = OUT / pid
        src = epdir / f"{pid}_persona.mp4"
        meta_p = epdir / f"{pid}_persona.meta.json"
        name = f"chatx-{ep['utm'].split('persona-')[-1]}"   # utm 尾巴已含 p1/p3/p4
        dst, jpg = WEB / f"{name}.mp4", WEB / f"{name}.jpg"
        if a.meta_only:
            if pid not in manifest:
                raise SystemExit(f"[FAIL] {pid} 未出厂，--meta-only 无从更新")
            manifest[pid].update(feed_text(ep))
            continue
        if not a.upload_only:
            if not (src.exists() and meta_p.exists()):
                raise SystemExit(f"[FAIL] 缺 {src.name} / meta —— 先 make_persona_episode.py {pid}")
            meta = json.loads(meta_p.read_text(encoding="utf-8"))
            src_dur = float(ffprobe(src)["format"]["duration"])
            total = src_dur - END_D
            blurs = blur_ranges(meta.get("zoom_timeline") or [], total)
            print(f"[{pid}] {src.name} {src_dur:.1f}s → 裁尾卡 {END_D}s = {total:.1f}s")
            for rect, rng in blurs.items():
                print(f"  脱敏 {rect} {sum(b - a for a, b in rng):.1f}s：" +
                      ", ".join(f"{x:.1f}-{y:.1f}" for x, y in rng))
            if not blurs:
                print("  脱敏 —（全程推近，会话列表不在画面内）")
            encode(src, dst, total, blurs)
            info = verify_mp4(dst, total, (W, H))
            print(f"  [OK] {dst.name} {info['duration']}s L{info['level']} {info['size'] // 1024}KB sha={info['sha256']}")
            shot = frame_at(dst, HOOK_D + (total - HOOK_D) * 0.55)
            poster(ep, total, shot, jpg)
            print(f"  [OK] {jpg.name} {jpg.stat().st_size // 1024}KB")
            for tag, t in (("a", HOOK_D + 2.0), ("b", HOOK_D + (total - HOOK_D) * 0.5), ("c", total - 1.5)):
                cv2.imwrite(str(QA / f"{name}_{tag}_t{int(t)}.jpg"),
                            cv2.resize(frame_at(dst, t), (960, 540)), [cv2.IMWRITE_JPEG_QUALITY, 82])
            manifest[pid] = {
                "id": pid, "feed_id": f"persona-{pid.lower()}", "file": dst.name, "poster": jpg.name,
                "persona": ep["persona"], "job": ep["job"], "company": ep["company"],
                **feed_text(ep),
                # 首批 P 系列钉 09-17；后续集（F1 教学片）各自带 feed_date，官网按日期倒序时排在首批前面
                "date": ep.get("feed_date") or "2026-09-17T12:00:00.000Z",
                "durationSec": info["duration"], "size": info["size"], "sha256_16": info["sha256"],
                "source": str(src.relative_to(OUT)), "utm": ep["utm"],
                "blur": {str(k): v for k, v in blurs.items()},
            }
            man_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    if a.upload or a.upload_only or a.meta_only:
        for pid in ids:
            entry = dict(manifest[pid])
            entry["broadcast"] = a.broadcast
            res = upload(entry, media=not a.meta_only)
            tg = (res["feed"].get("broadcast") or {})
            if a.broadcast:
                print(f"[{pid}] 频道广播 ok={tg.get('ok')} msg={tg.get('messageId')} {tg.get('error') or ''}".rstrip())
                if tg.get("ok") and tg.get("messageId"):
                    manifest[pid]["tg_message_id"] = tg["messageId"]
            print(f"[{pid}] 上架 {entry['feed_id']} created={res['feed'].get('created')} "
                  f"mp4 HTTP {res['http_mp4']} poster HTTP {res['http_poster']} → {SITE}/videos")
            manifest[pid]["published"] = {"src": res["src"], "http": res["http_mp4"], "at": entry["date"]}
        man_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[DONE] manifest → {man_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    sys.exit(main())
