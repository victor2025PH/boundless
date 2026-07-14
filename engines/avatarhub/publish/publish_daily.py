# -*- coding: utf-8 -*-
"""publish_daily.py — 每日视频三端自动发布（官网 /videos + Telegram 频道 + YouTube）。

链路：publish/queue/ 取最早一条视频 →（可选 ffmpeg 压 1080p 网页版）→
      YouTube 上传原片（4K 母版，标 AI 合成内容）→ scp 网页版到服务器 /var/www/media/feed →
      POST /api/admin/feed 上架官网并由服务器自动广播 Telegram 频道 →
      挪进 publish/published/ 归档 → 私信管理员发布结果。

素材从哪来（二选一，可并存）：
  A. 手动投喂：在 Google Flow（labs.google/flow，Ultra 档）批量生成后，把 mp4 丢进 queue/。
     可附同名 .json 指定文案：{"title_zh":…,"title_en":…,"desc_zh":…,"desc_en":…,"ai":true}
     没有 .json 时按文件名关键词（live/faceswap/voice/interp/studio/avatar）套用内置文案库。
  B. API 全自动：配好 secrets/gemini_api_key.txt 后 generate_daily.py 每天生成一条进队列。

用法：
  python publish_daily.py            # 发布一条（计划任务 AvatarHubPublish 每天调用）
  python publish_daily.py --dry-run  # 只演练不动手
  python publish_daily.py --no-youtube  # 跳过 YouTube（未授权时自动跳过）

YouTube 前置（一次性）：GCP 建项目开 YouTube Data API v3 → OAuth 桌面端凭据 →
  下载 client_secret.json 放 secrets/youtube/ → python publish/yt_auth.py 完成授权。
  注意：未过 YouTube 合规审核的 API 项目，上传一律锁「私享」；提交审核表单通过后即正常公开。
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent          # publish/
ROOT = BASE.parent                               # 项目根
QUEUE = BASE / "queue"
PUBLISHED = BASE / "published"
WORK = BASE / "work"
DEPLOY_DIR = ROOT / "secrets" / "deploy"
YT_DIR = ROOT / "secrets" / "youtube"
LOG_FILE = ROOT / "secrets" / "publish.log"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(msg: str):
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    try:
        LOG_FILE.parent.mkdir(exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ── 配置 ──────────────────────────────────────────────────────────────
def load_conf() -> dict:
    import os
    cfg = json.loads((DEPLOY_DIR / "deploy.config.json").read_text(encoding="utf-8"))
    site = os.environ.get("AVH_SITE") or cfg["site"]["url"]
    key = os.environ.get("ADMIN_KEY", "")
    if not key:
        for line in (DEPLOY_DIR / "prod.env.local.bak").read_text(encoding="utf-8").splitlines():
            m = re.match(r"^ADMIN_KEY=(.+)$", line.strip())
            if m:
                key = m.group(1).strip()
                break
    srv = cfg["server"]
    return {
        "site": site.rstrip("/"),
        "key": key,
        "ssh": f"{srv['user']}@{srv['host']}",
        "ssh_key": srv["ssh_key"],
        "media_dir": "/var/www/media/feed",
    }


def http_json(url: str, payload: dict | None = None, key: str = "", method: str | None = None) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Content-Type": "application/json", "x-setup-key": key},
        method=method or ("POST" if payload is not None else "GET"),
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def notify_admin(conf: dict, text: str):
    try:
        http_json(f"{conf['site']}/api/admin/notify", {"text": text}, key=conf["key"])
    except Exception as e:
        log(f"[提醒] 管理员私信失败（不影响发布）：{e}")


# ── 文案库：文件名关键词 → 双语标题/简介（sidecar json 可覆盖） ──────────
META_BANK: list[tuple[re.Pattern, dict]] = [
    (re.compile(r"live|stream", re.I), {
        "slug": "live",
        "title_zh": "直播实时换脸换声", "title_en": "Live face & voice swap",
        "desc_zh": "一键开播：摄像头画面实时换脸，声音同步变声，延迟低至毫秒级。",
        "desc_en": "Go live with one click: real-time face swap on camera with synced voice conversion at millisecond latency.",
    }),
    (re.compile(r"faceswap|face", re.I), {
        "slug": "faceswap",
        "title_zh": "视频换脸 · 前后对比", "title_en": "Video face swap · before & after",
        "desc_zh": "同一段素材换脸前后对比：动作、光影、表情完整保留。",
        "desc_en": "Same footage, before and after: motion, lighting and expressions fully preserved.",
    }),
    (re.compile(r"voice|tts|clone", re.I), {
        "slug": "voice",
        "title_zh": "声音克隆 · 情感 TTS", "title_en": "Voice cloning · emotional TTS",
        "desc_zh": "30 秒样本克隆音色，喜怒哀乐随文本驱动，支持多语种。",
        "desc_en": "Clone a voice from a 30-second sample; emotions driven by text, multilingual output.",
    }),
    (re.compile(r"interp|translate", re.I), {
        "slug": "interp",
        "title_zh": "克隆音实时同传", "title_en": "Real-time interpreting in your own voice",
        "desc_zh": "开会说中文，对方听到的是你自己声音的外语，双向实时。",
        "desc_en": "Speak Chinese, they hear your own voice in their language — both directions, in real time.",
    }),
    (re.compile(r"studio|style|fitting", re.I), {
        "slug": "studio",
        "title_zh": "换发型 · 定妆 · 试衣", "title_en": "Hair, makeup & outfit try-on",
        "desc_zh": "一键切换发型、妆容与穿搭，动作姿态保持不变。",
        "desc_en": "Switch hair, makeup and outfits instantly while pose and motion stay intact.",
    }),
    (re.compile(r"avatar|digital", re.I), {
        "slug": "avatar",
        "title_zh": "数字人口播", "title_en": "Digital human presenter",
        "desc_zh": "一张照片 + 一段文案，数字人开口成片，口型精准同步。",
        "desc_en": "One photo plus a script: a digital presenter with precise lip-sync.",
    }),
]
GENERIC_META = {
    "slug": "demo",
    "title_zh": "AvatarHub 效果演示", "title_en": "AvatarHub demo",
    "desc_zh": "实时数字人引擎效果演示。", "desc_en": "Real-time digital human engine demo.",
}


def meta_for(video: Path) -> dict:
    meta = None
    for pat, m in META_BANK:
        if pat.search(video.stem):
            meta = dict(m)
            break
    meta = meta or dict(GENERIC_META)
    sidecar = video.with_suffix(".json")
    if sidecar.exists():
        try:
            meta.update(json.loads(sidecar.read_text(encoding="utf-8")))
        except Exception as e:
            log(f"[警告] 附带文案 {sidecar.name} 解析失败，用内置文案：{e}")
    meta.setdefault("ai", True)
    return meta


# ── ffmpeg：压 1080p 网页版 + 抽封面帧 ────────────────────────────────
def find_ffmpeg() -> str | None:
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    links = Path.home() / "AppData/Local/Microsoft/WinGet/Links/ffmpeg.exe"
    if links.exists():
        return str(links)
    pkgs = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
    hits = sorted(pkgs.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe")) if pkgs.exists() else []
    return str(hits[-1]) if hits else None


def make_web_version(ffmpeg: str | None, src: Path, out_mp4: Path, out_jpg: Path) -> Path:
    """>1080p 或 >19MB 时转码（Telegram URL 拉取上限 20MB），否则原样复制。总是尝试抽封面。"""
    need = src.stat().st_size > 19 * 1024 * 1024
    if ffmpeg and not need:
        try:
            probe = subprocess.run(
                [ffmpeg, "-i", str(src), "-hide_banner"], capture_output=True, text=True, errors="replace")
            m = re.search(r"(\d{3,5})x(\d{3,5})", probe.stderr or "")
            if m and int(m.group(1)) > 1920:
                need = True
        except Exception:
            pass
    if need and not ffmpeg:
        log("[警告] 无 ffmpeg 且文件超限，直接用原片（Telegram 可能降级为链接帖）")
    if need and ffmpeg:
        subprocess.run(
            [ffmpeg, "-y", "-i", str(src), "-c:v", "libx264", "-crf", "23", "-preset", "medium",
             "-vf", "scale='min(1920,iw)':-2", "-c:a", "aac", "-b:a", "128k",
             "-movflags", "+faststart", str(out_mp4)],
            check=True, capture_output=True)
    else:
        shutil.copy2(src, out_mp4)
    if ffmpeg:
        try:
            subprocess.run(
                [ffmpeg, "-y", "-ss", "1", "-i", str(out_mp4), "-frames:v", "1",
                 "-vf", "scale='min(1280,iw)':-2", str(out_jpg)],
                check=True, capture_output=True)
        except Exception as e:
            log(f"[警告] 封面抽帧失败（无封面上架）：{e}")
    return out_mp4


# ── YouTube：refresh token → resumable 上传 ───────────────────────────
def yt_token() -> dict | None:
    f = YT_DIR / "token.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def yt_access_token(tok: dict) -> str:
    data = urllib.parse.urlencode({
        "client_id": tok["client_id"],
        "client_secret": tok["client_secret"],
        "refresh_token": tok["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["access_token"]


def yt_upload(video: Path, meta: dict, date_str: str, site: str) -> str | None:
    """上传原片到 YouTube。未授权返回 None；已授权失败抛异常。返回 videoId。"""
    tok = yt_token()
    if not tok:
        return None
    access = yt_access_token(tok)
    title = f"{meta['title_en']} | {meta['title_zh']} · AvatarHub Demo {date_str}"
    desc = (
        f"{meta['desc_en']}\n{meta['desc_zh']}\n\n"
        f"AvatarHub — real-time digital human engine (face swap / voice cloning / interpreting).\n"
        f"Runs on YOUR OWN hardware. Unlimited local usage.\n\n"
        f"Website: {site}/en\nPlans: {site}/en/order\nAll demos: {site}/en/videos\n\n"
        + ("This video is an AI-generated concept demo.\n" if meta.get("ai", True) else "Real engine output.\n")
        + "#AvatarHub #AI #FaceSwap #VoiceCloning #DigitalHuman"
    )
    body = {
        "snippet": {
            "title": title[:95],
            "description": desc[:4900],
            "tags": ["AvatarHub", "AI", "face swap", "voice cloning", "digital human", "real-time"],
            "categoryId": "28",
        },
        "status": {
            "privacyStatus": "public",           # 未过合规审核的项目会被 YouTube 强制转私享
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,      # AI 合成内容平台披露（必须如实声明）
        },
    }
    size = video.stat().st_size
    init = urllib.request.Request(
        "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {access}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": "video/mp4",
            "X-Upload-Content-Length": str(size),
        },
        method="POST",
    )
    with urllib.request.urlopen(init, timeout=60) as r:
        upload_url = r.headers["Location"]
    put = urllib.request.Request(
        upload_url, data=video.read_bytes(),
        headers={"Authorization": f"Bearer {access}", "Content-Type": "video/mp4"},
        method="PUT",
    )
    with urllib.request.urlopen(put, timeout=1800) as r:
        resp = json.loads(r.read())
    return resp.get("id")


# ── 主流程 ────────────────────────────────────────────────────────────
def pick_from_queue() -> Path | None:
    QUEUE.mkdir(parents=True, exist_ok=True)
    vids = sorted(QUEUE.glob("*.mp4"), key=lambda p: p.stat().st_mtime)
    return vids[0] if vids else None


def scp(conf: dict, local: Path, remote_name: str):
    subprocess.run(
        ["scp", "-i", conf["ssh_key"], "-o", "StrictHostKeyChecking=accept-new",
         str(local), f"{conf['ssh']}:{conf['media_dir']}/{remote_name}"],
        check=True, capture_output=True)


def run(dry: bool, use_youtube: bool) -> int:
    conf = load_conf()
    src = pick_from_queue()
    if not src:
        log("[发布] 队列为空，今天没有可发的视频。")
        notify_admin(conf, "📭 <b>视频队列已空</b>\n今天没有可发布的演示视频。请去 Google Flow 批量生成后丢进 publish\\queue\\（或配置 Gemini API 自动生成）。")
        return 0
    meta = meta_for(src)
    date_str = datetime.now().strftime("%Y-%m-%d")
    vid_id = f"{datetime.now():%Y%m%d}-{meta['slug']}"
    remote_mp4 = f"{vid_id}.mp4"
    remote_jpg = f"{vid_id}.jpg"
    log(f"[发布] 选中 {src.name} → {vid_id}（{src.stat().st_size / 1e6:.1f}MB）")
    if dry:
        log(f"[演练] 将上架：{meta['title_zh']} / {meta['title_en']}")
        return 0

    WORK.mkdir(exist_ok=True)
    web_mp4, poster = WORK / remote_mp4, WORK / remote_jpg
    make_web_version(find_ffmpeg(), src, web_mp4, poster)

    yt = None
    yt_err = ""
    if use_youtube:
        try:
            yt = yt_upload(src, meta, date_str, conf["site"])
            log(f"[YouTube] {'✓ videoId=' + yt if yt else '未授权，跳过（运行 publish/yt_auth.py 可开启）'}")
        except Exception as e:
            yt_err = str(e)[:200]
            log(f"[YouTube] 上传失败（不阻塞其它端）：{yt_err}")

    scp(conf, web_mp4, remote_mp4)
    if poster.exists():
        scp(conf, poster, remote_jpg)
    log("[官网] 视频与封面已上传服务器")

    payload = {
        "id": vid_id,
        "title_zh": meta["title_zh"], "title_en": meta["title_en"],
        "desc_zh": meta["desc_zh"], "desc_en": meta["desc_en"],
        "src": f"/media/feed/{remote_mp4}",
        "poster": f"/media/feed/{remote_jpg}" if poster.exists() else None,
        "youtube": yt,
        "ai": bool(meta.get("ai", True)),
    }
    r = http_json(f"{conf['site']}/api/admin/feed", payload, key=conf["key"])
    tg_ok = bool((r.get("broadcast") or {}).get("ok"))
    log(f"[官网] 上架 {'✓' if r.get('ok') else '✗ ' + str(r)}；[频道] 广播 {'✓' if tg_ok else '✗ ' + str(r.get('broadcast'))}")

    PUBLISHED.mkdir(exist_ok=True)
    dest = PUBLISHED / src.name
    if dest.exists():
        dest = PUBLISHED / f"{src.stem}-{int(time.time())}{src.suffix}"
    shutil.move(str(src), dest)
    sidecar = src.with_suffix(".json")
    if sidecar.exists():
        shutil.move(str(sidecar), PUBLISHED / sidecar.name)
    for tmp in (web_mp4, poster):
        tmp.unlink(missing_ok=True)

    left = len(list(QUEUE.glob("*.mp4")))
    summary = (
        f"🎬 <b>今日视频已发布</b> · {meta['title_zh']}\n"
        f"官网：{conf['site']}/videos\n"
        f"频道：{'✓ 已发' if tg_ok else '✗ 失败'}\n"
        f"YouTube：{('✓ https://youtu.be/' + yt) if yt else ('✗ ' + yt_err if yt_err else '未授权跳过')}\n"
        f"队列剩余：{left} 条" + ("（⚠️ 告急，尽快补充素材）" if left <= 2 else "")
    )
    notify_admin(conf, summary)
    return 1


def main():
    ap = argparse.ArgumentParser(description="每日视频三端自动发布")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-youtube", action="store_true")
    args = ap.parse_args()
    try:
        run(args.dry_run, not args.no_youtube)
    except Exception as e:
        log(f"[错误] 发布失败：{e}")
        try:
            notify_admin(load_conf(), f"❌ <b>今日视频发布失败</b>\n{str(e)[:500]}\n请查 secrets\\publish.log")
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
