#!/usr/bin/env python3
"""营销歌产线 · 第一段：hub 原创作曲（ACE-Step）→ 验证 → 15 秒高光 MV → 验证。

  python make_song.py V1                 # 1 抽
  python make_song.py V1 --tries 3       # 3 抽（选角/挑 take 给老板人耳）
  python make_song.py V1 --mv 123        # 对 history_id=123 出 15 秒高光 MV
  python make_song.py V1 --mv 123 --profile 001 --seconds 15

176 显存舞步沿用 engines/chengjie/scripts/song_factory.py（2026-08-23 实锤）：
任务前卸 ollama 常驻 + park singing/sbv2/ace → ensure ace_studio → 出歌 → **必 park ace**。
全程尊重 hub 直播让路（409 即停，不 force）。

媒体产物验证纪律：先看 Content-Type，落盘后必验 magic bytes（RIFF/ID3/fLaC/OggS/ftyp）+ ffprobe；
HTTP 200 + 体积不构成验证。任何一步失败即非零退出。
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
HUB = "http://192.168.0.176:9000"
OLLAMA = "http://192.168.0.176:11434"
ASR = "http://192.168.0.176:8765/v1/audio/transcriptions"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def req(path: str, payload=None, *, method: str | None = None, timeout=120, raw=False):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(HUB + path, data=data,
                               method=method or ("POST" if data is not None else "GET"),
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        body = resp.read()
        ctype = str(resp.headers.get("Content-Type") or "")
        return (body, ctype) if raw else json.loads(body.decode("utf-8"))


# ── 显存舞步 ─────────────────────────────────────────────────────────────────

def park(name: str) -> None:
    try:
        req(f"/api/gpu/park?name={name}", {}, timeout=90)
    except Exception as e:  # noqa: BLE001
        log(f"park {name} 软失败: {e}")


def ensure(name: str, wait_s: int = 120) -> bool:
    r = req(f"/api/services/ensure?name={name}&wait_s={wait_s}", {}, timeout=wait_s + 30)
    up = bool(r.get("up") or r.get("ok"))
    log(f"ensure {name}: {'在线' if up else '未上线 ' + str(r)[:160]}")
    return up


def clear_for_ace() -> None:
    try:
        ps = json.loads(urllib.request.urlopen(OLLAMA + "/api/ps", timeout=10).read().decode())
        for m in ps.get("models", []):
            rq = urllib.request.Request(OLLAMA + "/api/generate",
                                        data=json.dumps({"model": m.get("name"), "keep_alive": 0}).encode(),
                                        headers={"Content-Type": "application/json"})
            urllib.request.urlopen(rq, timeout=30).read()
            log(f"ollama 卸载 {m.get('name')}")
    except Exception as e:  # noqa: BLE001
        log(f"ollama unload 软失败: {e}")
    for svc in ("singing", "sbv2_tts", "ace_studio"):
        park(svc)


def vram() -> str:
    try:
        v = req("/api/gpu/status", timeout=15).get("vram") or {}
        return f"free {v.get('free_mb')}MB / used {v.get('used_mb')}MB ({v.get('level')})"
    except Exception:
        return "?"


# ── 验证 ─────────────────────────────────────────────────────────────────────

def sniff(b: bytes) -> str:
    h = b[:12]
    if h.startswith(b"RIFF"):
        return "wav"
    if h.startswith(b"ID3") or h[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "mp3"
    if h.startswith(b"fLaC"):
        return "flac"
    if h.startswith(b"OggS"):
        return "ogg"
    if h[4:8] == b"ftyp":
        return "mp4"
    if h.startswith(b"{") or h.startswith(b"<"):
        return "TEXT(json/html 信封!)"
    return f"unknown({h.hex()})"


def ffprobe(path: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"[FAIL] ffprobe 读不了 {path.name}: {r.stderr[:200]}")
    return json.loads(r.stdout)


_whisper = None


def asr_segments(path: Path, lang: str = "zh") -> list[dict]:
    """本地 faster-whisper（small/int8/CPU，~1.5 倍实时）转写；176:8765 ASR 本机不可达（2026-09-16 实测）。
    返回 [{start,end,text}]，软失败回空表。"""
    global _whisper
    try:
        from faster_whisper import WhisperModel
        if _whisper is None:
            _whisper = WhisperModel("small", device="cpu", compute_type="int8")
        segs, _ = _whisper.transcribe(str(path), language=lang[:2], beam_size=3, vad_filter=False)
        return [{"start": round(s.start, 1), "end": round(s.end, 1), "text": s.text} for s in segs]
    except Exception as e:  # noqa: BLE001
        log(f"ASR 软失败: {e}")
        return []


def asr_text(path: Path, lang: str = "zh") -> str:
    return "".join(s["text"] for s in asr_segments(path, lang))


def line_hits(lyrics: str, transcript: str) -> list[float]:
    import difflib
    import re
    tr = re.sub(r"[\s，。！？、,.!?~♪]", "", transcript)
    out = []
    for l in lyrics.splitlines():
        l = l.strip()
        if not l or l.startswith("["):
            continue
        key = re.sub(r"\s", "", l)
        best = 0.0
        for i in range(0, max(1, len(tr) - len(key) + 1)):
            seg = tr[i:i + len(key) + 2]
            best = max(best, difflib.SequenceMatcher(None, key, seg).ratio())
        out.append(round(best, 2))
    return out


# ── 产线 ─────────────────────────────────────────────────────────────────────

def load_item(item_id: str) -> tuple[dict, dict]:
    reg = json.loads((ROOT / "registry.json").read_text(encoding="utf-8"))
    for it in reg["items"]:
        if it["id"] == item_id:
            return it, reg["styles"]
    raise SystemExit(f"registry 无 {item_id}")


def cut_lyrics(full: str, mode: str) -> str:
    """short＝首个 [verse] + 首个 [chorus] + [outro]（45s 竖屏容量实测：18 行只唱得下 ~10 行，
    2026-09-16 V1 首抽 ACE 把 verse 前两句唱了两遍、verse2/chorus2 全丢）。full＝原文。"""
    if mode != "short":
        return full
    blocks: list[tuple[str, list[str]]] = []
    tag = None
    for l in full.splitlines():
        s = l.strip()
        if s.startswith("[") and s.endswith("]"):
            tag = s.lower()
            blocks.append((tag, []))
        elif s and tag:
            blocks[-1][1].append(s)
    keep, seen = [], set()
    for t, ls in blocks:
        if t in ("[verse]", "[chorus]") and t not in seen:
            seen.add(t)
            keep.append((t, ls))
        elif t == "[outro]":
            keep.append((t, ls))
    return "\n\n".join(t + "\n" + "\n".join(ls) for t, ls in keep) + "\n"


def create_song(item: dict, styles: dict, *, seed: int | None, quality: str, profile: str,
                cut: str = "short") -> dict:
    lyrics = cut_lyrics((ROOT / item["lyrics"]).read_text(encoding="utf-8"), cut)
    style = styles[item["style"]]
    seed = seed or random.randint(1, 2**31 - 1)
    payload = {"style": style, "lyrics": lyrics, "duration_s": float(item["duration_s"]),
               "quality": quality, "seed": seed, "profile": profile, "svc_swap": False,
               "song_name": f"mk_{item['id']}_{seed}"}
    if item.get("lora"):  # 说唱教学段：hub 自带 ACE-Step-v1-chinese-rap-LoRA
        payload["lora"] = item["lora"]
        payload["lora_weight"] = float(item.get("lora_weight", 1.0))
    log(f"提交作曲 {item['id']} seed={seed} dur={item['duration_s']}s quality={quality} style={item['style']}")
    sub = req("/api/song/create", payload, timeout=120)
    tid = sub.get("task_id")
    if not tid:
        raise SystemExit(f"[FAIL] 提交无 task_id: {sub}")
    t0, last = time.monotonic(), ""
    while time.monotonic() - t0 < 1200:
        time.sleep(5)
        st = req(f"/api/song/create/{tid}", timeout=30)
        s = str(st.get("status"))
        cur = f"{s} {st.get('stage','')} {st.get('progress',0)}% {st.get('detail','')}"
        if cur != last:
            log(f"  {tid} {cur}")
            last = cur
        if s == "done":
            st["_seed"] = seed
            return st
        if s in ("error", "failed", "cancelled"):
            raise SystemExit(f"[FAIL] 作曲 {s}: {st.get('detail')}")
    raise SystemExit("[FAIL] 作曲超时")


def fetch_audio(st: dict, dst_stem: Path) -> Path:
    url = st.get("audio_url")
    if not url:
        raise SystemExit(f"[FAIL] done 但无 audio_url: {json.dumps(st, ensure_ascii=False)[:300]}")
    body, ctype = req(url, raw=True, timeout=180)
    kind = sniff(body)
    log(f"取音频 {url} ctype={ctype} magic={kind} {len(body)//1024}KB")
    if kind not in ("wav", "mp3", "flac", "ogg"):
        raise SystemExit(f"[FAIL] 产物不是音频（{kind}）——按纪律拒收")
    dst = dst_stem.with_suffix("." + kind)
    dst.write_bytes(body)
    info = ffprobe(dst)
    dur = float(info["format"]["duration"])
    astr = [s for s in info["streams"] if s["codec_type"] == "audio"]
    if not astr or dur < 8:
        raise SystemExit(f"[FAIL] ffprobe: streams={len(astr)} dur={dur}")
    log(f"  [OK] {dst.name}: {dur:.1f}s sr={astr[0].get('sample_rate')} codec={astr[0].get('codec_name')}")
    return dst


def make_mv(history_id: int, *, seconds: float, profile: str, dst_stem: Path) -> Path:
    log(f"出 MV history_id={history_id} seconds={seconds} profile={profile or '(active)'}")
    # 口型服务默认泊车（2026-09-16 实测 503「口型服务不可达」）→ 先按需拉起 hub 当前口型引擎
    try:
        eng = str(req("/health", timeout=15).get("active_lipsync_engine") or "lipsync")
    except Exception:
        eng = "lipsync"
    ensure("lipsync" if eng == "musetalk" else eng, wait_s=150)
    try:
        d = req("/api/song/mv", {"history_id": history_id, "seconds": seconds, "profile": profile,
                                  "hd": False, "force": False}, timeout=600)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:300]
        if e.code == 409:
            raise SystemExit(f"[STOP] hub 直播让路 409，不 force：{body}")
        raise SystemExit(f"[FAIL] MV HTTP {e.code}: {body}")
    if not d.get("ok") or not d.get("url"):
        raise SystemExit(f"[FAIL] MV 回包异常: {json.dumps(d, ensure_ascii=False)[:300]}")
    body, ctype = req(d["url"], raw=True, timeout=300)
    kind = sniff(body)
    log(f"取 MV {d['url']} ctype={ctype} magic={kind} {len(body)//1024}KB start_s={d.get('start_s')} sec={d.get('seconds')}")
    if kind != "mp4":
        raise SystemExit(f"[FAIL] MV 产物不是 mp4（{kind}）")
    dst = dst_stem.with_suffix(".mp4")
    dst.write_bytes(body)
    info = ffprobe(dst)
    kinds = {s["codec_type"] for s in info["streams"]}
    dur = float(info["format"]["duration"])
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    if kinds != {"video", "audio"} or dur < 5:
        raise SystemExit(f"[FAIL] MV 验收不过 streams={kinds} dur={dur}")
    log(f"  [OK] {dst.name}: {dur:.1f}s {v.get('width')}x{v.get('height')} 视频+音频")
    (dst_stem.with_suffix(".mv.json")).write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    return dst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("item")
    ap.add_argument("--tries", type=int, default=1)
    ap.add_argument("--seed", type=int)
    ap.add_argument("--quality", default="turbo")
    ap.add_argument("--profile", default="")
    ap.add_argument("--mv", type=int, help="仅对该 history_id 出 MV，不作曲")
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--no-dance", action="store_true", help="跳过显存舞步（引擎已在线时）")
    ap.add_argument("--cut", default="short", choices=("short", "full"),
                    help="short=首 verse+首 chorus+outro（45s 竖屏）；full=全词（配 duration 90s+）")
    ap.add_argument("--duration", type=float, help="覆盖 registry 的 duration_s")
    a = ap.parse_args()
    item, styles = load_item(a.item)
    OUT.mkdir(exist_ok=True)

    if a.mv:
        suffix = f"_{a.profile}" if a.profile else ""
        make_mv(a.mv, seconds=a.seconds, profile=a.profile,
                dst_stem=OUT / f"{item['id']}_h{a.mv}_mv{int(a.seconds)}s{suffix}")
        return 0

    log(f"VRAM 前: {vram()}")
    if not a.no_dance:
        clear_for_ace()
        log(f"VRAM 清场后: {vram()}")
    if not ensure("ace_studio"):
        raise SystemExit("[FAIL] ace_studio 拉不起来")
    h = req("/api/song/health", timeout=20)
    log(f"song/health create={h.get('create')}")
    if not (h.get("create") or {}).get("online"):
        raise SystemExit("[FAIL] hub 报 create 离线，不假唱")
    if a.duration:
        item["duration_s"] = a.duration
    takes = []
    try:
        for i in range(a.tries):
            st = create_song(item, styles, seed=a.seed if a.tries == 1 else None,
                             quality=a.quality, profile=a.profile, cut=a.cut)
            seed = st["_seed"]
            stem = OUT / f"{item['id']}_{a.cut}_s{seed}"
            audio = fetch_audio(st, stem)
            sent = cut_lyrics((ROOT / item["lyrics"]).read_text(encoding="utf-8"), a.cut)
            stem.with_suffix(".lyrics.txt").write_text(sent, encoding="utf-8")
            meta = {k: st.get(k) for k in ("history_id", "audio_url", "elapsed_ms", "stage", "detail")}
            meta.update({"seed": seed, "quality": a.quality, "style": item["style"], "cut": a.cut,
                         "duration_s": item["duration_s"]})
            segs = asr_segments(audio, item.get("lang", "zh"))
            if segs:
                tr = "".join(s["text"] for s in segs)
                hits = line_hits(sent, tr)
                meta["asr"] = segs
                meta["line_hits"] = hits
                low = [h for h in hits if h < 0.5]
                last_sung = max(s["end"] for s in segs)
                meta["last_sung_s"] = last_sung
                log(f"  逐句命中 {hits} → {'WARN 有 ' + str(len(low)) + ' 句 <0.5' if low else 'OK'}；"
                    f"末句唱到 {last_sung}s / {item['duration_s']}s")
            stem.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
            takes.append(meta)
    finally:
        park("ace_studio")
        log(f"VRAM 收尾(park ace): {vram()}")
    print("\n== takes ==")
    for t in takes:
        print(json.dumps({k: t.get(k) for k in ("seed", "history_id", "line_hits", "last_sung_s")},
                         ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
