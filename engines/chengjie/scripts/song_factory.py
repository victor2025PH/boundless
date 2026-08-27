# -*- coding: utf-8 -*-
"""歌房工厂——人设唱歌供给的生产级 CLI（实施58 P1；替代 song_prerender 的 SVC 路）。

供给模型（2026-08-23 定型）：每把「唱歌嗓」以一条**老板人耳认可的锚 take**
（`config/song_templates/anchors/<voice>.wav`）+ 提示词 + 画布常数注册在
`config/song_templates/voices.json`；任何模板曲的供给＝ACE audio2audio
以锚为参考换词重唱（voice-carry 实测 campplus 0.81~0.89），成品字节复制到
该嗓分组内全部人设——**GPU 成本只随声库数不随人设数**。

五道全自动质检（单次合格率 3~5 成，≤N 抽累积 >90%）：
  ① RIFF magic；② 逐句 ASR ≥0.5（缺句即弃）；③ 收尾裁切（verbose ASR 末句
  词尾 → 3s 内最深能量谷收刀——「画布填充污染」根治）；④ campplus vs 锚
  ≥0.75（同嗓不变量；评分器缺席则跳过并在 meta 注明）；⑤ 时长带 10s~画布。

176 显存编排（2026-08-23 夜实锤三坑机制化）：任务前卸 ollama 常驻模型 +
park singing/sbv2/ace；**ACE 任务后必 park**（torch 缓存 ~10.9G 不回落且
mem_get_info 自查=自阻塞）；再拉 singing 做分离。

用法：
  python -m scripts.song_factory --template origin_wind            # 全声库
  python -m scripts.song_factory --template origin_wind --voice warm_f
  python -m scripts.song_factory --template origin_wind --enable-template
  python -m scripts.song_factory --status                          # 只读盘点
凭证：env SONG_TOKEN，缺省读 D:/faceX/mfys/secrets/service_token.txt（本机
即 AvatarHub 节点的运维约定；均缺则直连口若开着 service_auth 会 401）。
失败语义：单 (模板×嗓) 失败继续批，任何失败 exit 1；hub 不可达 exit 2。
"""
from __future__ import annotations

import argparse
import base64
import difflib
import io
import json
import math
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.companion.song_stock import (  # noqa: E402
    load_song_manifest, prepare_dry_vocal, stock_root, templates_dir,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:
    pass

GATEWAY = os.environ.get("SONG_HUB", "http://192.168.0.176:9000").rstrip("/")
DIRECT_ACE = os.environ.get("SONG_ACE", "http://192.168.0.176:7859").rstrip("/")
DIRECT_SONG = os.environ.get("SONG_STUDIO",
                             "http://192.168.0.176:7853").rstrip("/")
ASR_BASE = os.environ.get("SONG_ASR",
                          "http://192.168.0.176:8765/v1").rstrip("/")
OLLAMA = "http://192.168.0.176:11434"
_TOKEN_FILE = Path("D:/faceX/mfys/secrets/service_token.txt")

SIM_FLOOR = 0.75          # campplus vs 锚（同嗓不变量）
LINE_FLOOR = 0.5          # 逐句 ASR 局部相似度
MIN_DUR = 10.0
VOICES_NAME = "voices.json"


def log(m: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def _token() -> str:
    t = os.environ.get("SONG_TOKEN", "").strip()
    if t:
        return t
    try:
        return _TOKEN_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return ""


# ── 纯函数（单测面：tests/test_song_factory.py） ─────────────────────────────

def load_voices(tdir: Path) -> List[Dict[str, Any]]:
    """voices.json → 合法声库条目（缺锚/缺 prompt/坏行跳过并告警）。"""
    f = Path(tdir) / VOICES_NAME
    out: List[Dict[str, Any]] = []
    try:
        rows = (json.loads(f.read_text(encoding="utf-8")) or {}).get("voices")
    except Exception:
        return out
    for row in rows or []:
        try:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "").strip()
            anchor = str(row.get("anchor") or "").strip()
            prompt = str(row.get("prompt") or "").strip()
            if not key or not anchor or not prompt:
                log(f"voices.json 跳过缺字段行 key={key!r}")
                continue
            if ".." in anchor.replace("\\", "/").split("/"):
                log(f"voices.json 跳过路径穿越 key={key!r}")
                continue
            out.append({
                "key": key,
                "label": str(row.get("label") or key),
                "anchor": anchor,
                "prompt": prompt,
                "canvas_s": float(row.get("canvas_s") or 28.0),
                "strength": float(row.get("strength") or 0.5),
                "personas": [str(p) for p in (row.get("personas") or [])],
            })
        except Exception as e:  # noqa: BLE001
            log(f"voices.json 坏行跳过: {e}")
    return out


def line_hits(lyrics: str, transcript: Optional[str]) -> Optional[List[float]]:
    """逐句在转写里的最佳局部相似度；转写缺席回 None。纯函数。"""
    if transcript is None:
        return None
    tr = "".join(c for c in transcript if "\u4e00" <= c <= "\u9fff")
    hits: List[float] = []
    for ln in [x.strip() for x in (lyrics or "").splitlines() if x.strip()]:
        ln_n = "".join(c for c in ln if "\u4e00" <= c <= "\u9fff")
        best = 0.0
        if ln_n and tr:
            for i in range(0, max(1, len(tr) - len(ln_n) + 1)):
                best = max(best, difflib.SequenceMatcher(
                    None, ln_n, tr[i:i + len(ln_n) + 2]).ratio())
        hits.append(round(best, 2))
    return hits


def qa_verdict(*, hits: Optional[List[float]], sim: Optional[float],
               dur: float, canvas_s: float,
               line_floor: float = LINE_FLOOR,
               sim_floor: float = SIM_FLOOR) -> Tuple[bool, str]:
    """五闸合议（②④⑤；①③在管线内完成）。ASR/评分器缺席＝该闸弃权不拦
    （软基建缺席不该锁死供给），但 meta 会如实记录弃权。纯函数。"""
    if hits is not None and any(h < line_floor for h in hits):
        return False, f"line_miss:{hits}"
    if sim is not None and sim < sim_floor:
        return False, f"sim_low:{sim}"
    if dur < MIN_DUR:
        return False, f"too_short:{dur}"
    if dur > canvas_s + 2.0:
        return False, f"too_long:{dur}"
    return True, "ok"


def stock_is_fresh(sidecar: Path, anchor_sha1: str, lyrics: str) -> bool:
    """货新鲜度：sidecar 在 + 锚指纹一致 + 词一致（换锚/换词自动判旧）。"""
    try:
        if not sidecar.is_file():
            return False
        meta = json.loads(sidecar.read_text(encoding="utf-8")) or {}
        return (str(meta.get("anchor_sha1") or "") == anchor_sha1
                and str(meta.get("lyrics") or "") == (lyrics or ""))
    except Exception:
        return False


# ── wav / HTTP 工具 ──────────────────────────────────────────────────────────

def read_wav(b: bytes):
    with wave.open(io.BytesIO(b), "rb") as w:
        ch, fr, n = w.getnchannels(), w.getframerate(), w.getnframes()
        vals = list(struct.unpack(f"<{n * ch}h", w.readframes(n)))
    return vals, ch, fr


def write_wav(vals, ch, fr) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(fr)
        w.writeframes(struct.pack(f"<{len(vals)}h", *vals))
    return buf.getvalue()


def sec_db(vals, ch, fr, win=0.5):
    step = int(fr * win) * ch
    out = []
    for k in range(math.ceil(len(vals) / max(1, step))):
        seg = vals[k * step:(k + 1) * step]
        if not seg:
            continue
        rms = math.sqrt(sum(v * v for v in seg) / len(seg))
        out.append((k * win, 20 * math.log10(max(rms, 1e-9) / 32768.0)))
    return out


def sha1_bytes(b: bytes) -> str:
    import hashlib
    return hashlib.sha1(b).hexdigest()


def _req(base: str, path: str, payload=None, timeout=150, raw=False):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    tok = _token()
    if tok and base in (DIRECT_ACE, DIRECT_SONG):
        headers["X-AH-Svc"] = tok
    req = urllib.request.Request(base + path, data=data,
                                 method="POST" if data is not None else "GET",
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return (body, str(r.headers.get("Content-Type") or "")) if raw \
            else json.loads(body.decode("utf-8"))


def poll(base: str, tid: str, budget=900):
    t0 = time.monotonic()
    last = ""
    while time.monotonic() - t0 < budget:
        time.sleep(6)
        t = _req(base, f"/v1/task/{tid}")
        st = str(t.get("status"))
        if st in ("done", "error", "failed", "cancelled"):
            if st != "done":
                raise RuntimeError(f"{tid} {st}: {t.get('detail')}")
            return t
        cur = f"{st}{t.get('progress')}"
        if cur != last:
            log(f"    {tid} {st} {t.get('progress')}%")
            last = cur
    raise TimeoutError(tid)


def asr_json(wav_bytes: bytes, verbose: bool):
    boundary = uuid.uuid4().hex
    parts = []
    fields = (("model", "large-v3-turbo"), ("language", "zh"),
              ("response_format", "verbose_json" if verbose else "json"))
    for k, v in fields:
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; "
                      f'name="{k}"\r\n\r\n{v}\r\n').encode())
    parts.append((f"--{boundary}\r\nContent-Disposition: form-data; "
                  f'name="file"; filename="a.wav"\r\n'
                  f"Content-Type: audio/wav\r\n\r\n").encode())
    body = b"".join(parts) + wav_bytes + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        ASR_BASE + "/audio/transcriptions", data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        log(f"    ASR 软失败: {e}")
        return None


_scorer = None
_scorer_tried = False


def sim_vs(ref_b64: str, wav_bytes: bytes) -> Optional[float]:
    """campplus 相似度（本机 mfys 评分器；缺席回 None=该闸弃权）。"""
    global _scorer, _scorer_tried
    if not _scorer_tried:
        _scorer_tried = True
        mfys = Path("D:/faceX/mfys")
        if mfys.is_dir():
            sys.path.insert(0, str(mfys))
            try:
                from clone_scorer import score_similarity as _s  # type: ignore
                _scorer = _s
            except Exception as e:  # noqa: BLE001
                log(f"clone_scorer 不可用（{e}），音色闸弃权")
    if _scorer is None:
        return None
    try:
        rv = _scorer(ref_b64, base64.b64encode(wav_bytes).decode())
        return round(float(rv.get("similarity") or 0), 4) if rv.get("ok") \
            else None
    except Exception as e:  # noqa: BLE001
        log(f"    sim 软失败: {e}")
        return None


# ── voice_match（实施58 P3-1：vs 本人说话声的音色贴合度仪表） ────────────────
# 五闸原有 sim 只量「vs 选角锚」（同嗓一致性），从未量「vs 人设说话声」——
# 音色不符没被仪表盘看见的根因。此仪表 WARN 不拦（拦=当前全库下架），
# 落 sidecar/take_json 供点唱台三档灯；A1（RVC 主修）验收线 ≥0.70。
_SPEAK_REFS: Optional[Dict[str, str]] = None


def _speaking_refs() -> Dict[str, str]:
    """pid → 说话参考音 b64（懒加载 + 进程内缓存；缺失回空表零阻断）。"""
    global _SPEAK_REFS
    if _SPEAK_REFS is not None:
        return _SPEAK_REFS
    refs: Dict[str, str] = {}
    try:
        from scripts._data_root import load_merged_config, resolve_data_roots
        from scripts.avatar_prerender import _collect_avatar_personas
        root = resolve_data_roots()[0]
        cfg = load_merged_config(root)
        for pid, ref_path in _collect_avatar_personas(cfg, root=root):
            try:
                refs[pid] = base64.b64encode(
                    Path(ref_path).read_bytes()).decode()
            except Exception:
                continue
    except Exception as e:  # noqa: BLE001
        log(f"说话参考音收集软失败（voice_match 弃权）: {e}")
    _SPEAK_REFS = refs
    return refs


def voice_match(pid: str, wav_bytes: bytes) -> Optional[float]:
    """campplus(人设说话声, 唱段)——「像不像本人」的量化口径；缺席回 None。"""
    ref = _speaking_refs().get(str(pid))
    if not ref:
        return None
    return sim_vs(ref, wav_bytes)


def ffmpeg_ogg(wav_bytes: bytes) -> Optional[bytes]:
    exe = shutil.which("ffmpeg")
    if not exe:
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="songfac_") as td:
            src, dst = Path(td) / "in.wav", Path(td) / "out.ogg"
            src.write_bytes(wav_bytes)
            r = subprocess.run(
                [exe, "-y", "-loglevel", "error", "-i", str(src),
                 "-c:a", "libopus", "-b:a", "48k", "-ar", "48000", "-ac", "1",
                 str(dst)], capture_output=True, timeout=120)
            if r.returncode != 0 or not dst.is_file():
                return None
            out = dst.read_bytes()
            return out if out[:4] == b"OggS" else None
    except Exception:
        return None


# ── 176 显存编排 ─────────────────────────────────────────────────────────────

def park(name: str) -> None:
    try:
        _req(GATEWAY, f"/api/gpu/park?name={name}", payload={}, timeout=90)
    except Exception as e:  # noqa: BLE001
        log(f"park {name} 软失败: {e}")


def ensure(name: str) -> None:
    try:
        r = _req(GATEWAY, f"/api/services/ensure?name={name}&wait_s=90",
                 payload={}, timeout=150)
        if not r.get("up"):
            log(f"ensure {name} 未上线: {str(r)[:120]}")
    except Exception as e:  # noqa: BLE001
        log(f"ensure {name} 软失败: {e}")


def clear_for_ace() -> None:
    try:
        ps = json.loads(urllib.request.urlopen(
            OLLAMA + "/api/ps", timeout=10).read().decode())
        for m in ps.get("models", []):
            req = urllib.request.Request(
                OLLAMA + "/api/generate",
                data=json.dumps({"model": m.get("name"),
                                 "keep_alive": 0}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=30).read()
    except Exception as e:  # noqa: BLE001
        log(f"ollama unload 软失败: {e}")
    for svc in ("singing", "sbv2_tts", "ace_studio"):
        park(svc)


# ── 生产管线 ─────────────────────────────────────────────────────────────────

def finish_take(dry: bytes, lyrics: str):
    """闸③：ASR 词尾 → 3s 内最深谷 +0.4s 收刀 + 淡出 + prepare。"""
    vals, ch, fr = read_wav(dry)
    dur = len(vals) / ch / fr
    t_end = 0.0
    d = asr_json(dry, verbose=True)
    if d:
        for s in d.get("segments") or []:
            if str(s.get("text") or "").strip():
                t_end = max(t_end, float(s.get("end") or 0))
    if t_end <= 0:
        t_end = max(0.0, dur - 6.0)
    rows = sec_db(vals, ch, fr)
    cands = [(db, t) for t, db in rows if t_end <= t <= min(t_end + 3.0, dur)]
    cut_t = (min(cands)[1] + 0.4) if cands else min(t_end + 0.8, dur)
    cut_t = min(cut_t, dur)
    out = list(vals[:int(cut_t * fr) * ch])
    nfade = min(int(0.3 * fr) * ch, len(out))
    for i in range(nfade):
        idx = len(out) - nfade + i
        out[idx] = int(out[idx] * (1.0 - i / nfade))
    final, meta = prepare_dry_vocal(write_wav(out, ch, fr), lyrics=lyrics)
    return (final if meta.get("ok") else None), round(cut_t, 2), meta


def render_voice_song(voice: Dict[str, Any], lyrics: str, tdir: Path,
                      tries: int) -> Optional[Dict[str, Any]]:
    """锚 remix 抽签 ≤tries 次，首个过闸 take 胜出。"""
    anchor_path = tdir / voice["anchor"]
    anchor = anchor_path.read_bytes()
    anchor_b64 = base64.b64encode(anchor).decode()
    avals, ch, fr = read_wav(anchor)
    canvas = float(voice["canvas_s"])
    need = int(canvas * fr) * ch - len(avals)
    padded = avals + [0] * max(0, need)
    ref_b64 = base64.b64encode(write_wav(padded, ch, fr)).decode()
    for attempt in range(1, tries + 1):
        seed = random.randint(1, 2 ** 31 - 1)
        log(f"  [{voice['key']}] 抽 {attempt}/{tries} seed={seed} "
            f"画布{canvas}s strength={voice['strength']}")
        try:
            clear_for_ace()
            ensure("ace_studio")
            sub = _req(DIRECT_ACE, "/v1/create", {
                "prompt": voice["prompt"], "lyrics": lyrics,
                "duration_s": canvas, "steps": 60, "seed": seed,
                "ref_b64": ref_b64, "ref_strength": voice["strength"],
                "song_name": f"factory_{voice['key']}"}, timeout=300)
            poll(DIRECT_ACE, sub["task_id"])
            mix, ctype = _req(DIRECT_ACE,
                              f"/v1/task/{sub['task_id']}/audio",
                              raw=True, timeout=180)
            if not mix.startswith(b"RIFF"):                       # 闸①
                log(f"    产物非 wav ctype={ctype}")
                continue
            park("ace_studio")
            ensure("singing")
            sub2 = _req(DIRECT_SONG, "/v1/separate", {
                "song_b64": base64.b64encode(mix).decode(),
                "song_name": f"factory_sep_{voice['key']}",
                "sep_overlap": 4, "sep_model": "mel"})
            poll(DIRECT_SONG, sub2["task_id"])
            dry, _ = _req(DIRECT_SONG,
                          f"/v1/task/{sub2['task_id']}/audio?stem=vocals",
                          raw=True, timeout=180)
            if not dry.startswith(b"RIFF"):
                log("    分离产物非 wav")
                continue
            final, cut_t, meta = finish_take(dry, lyrics)          # 闸③
            if final is None:
                log(f"    收尾/prepare 失败 {str(meta)[:80]}")
                continue
            tr = (asr_json(final, verbose=False) or {}).get("text")
            hits = line_hits(lyrics, tr)                           # 闸②
            sim = sim_vs(anchor_b64, final)                        # 闸④
            fvals, fch, ffr = read_wav(final)
            dur = len(fvals) / fch / ffr                           # 闸⑤
            ok, why = qa_verdict(hits=hits, sim=sim, dur=dur,
                                 canvas_s=canvas)
            log(f"    四句={hits} sim={sim} dur={dur:.1f}s -> "
                f"{'PASS' if ok else why}")
            if ok:
                return {"final": final, "seed": seed, "cut_t": cut_t,
                        "hits": hits, "sim": sim, "dur": round(dur, 1),
                        "asr": (tr or "")[:80], "attempt": attempt}
        except Exception as e:  # noqa: BLE001
            log(f"    抽签异常: {e}")
    return None


def stock_take(take: Dict[str, Any], voice: Dict[str, Any], tmpl,
               sroot: Path, anchor_sha1: str) -> int:
    ogg = ffmpeg_ogg(take["final"])
    body, ext = (ogg, ".ogg") if ogg else (take["final"], ".wav")
    if ogg is None:
        log("  ⚠ 无 ffmpeg/转码失败，保留 wav（语音消息形态欠佳）")
    n = 0
    for pid in voice["personas"]:
        sdir = Path(sroot) / pid / "songs"
        sdir.mkdir(parents=True, exist_ok=True)
        for old_ext in (".ogg", ".wav", ".mp3"):
            if old_ext != ext:
                (sdir / f"{tmpl.id}{old_ext}").unlink(missing_ok=True)
        (sdir / f"{tmpl.id}{ext}").write_bytes(body)
        vm = voice_match(pid, take["final"])       # vs 本人说话声（WARN 仪表）
        (sdir / f"{tmpl.id}.json").write_text(json.dumps({
            "template_id": tmpl.id, "title": tmpl.title,
            "lyrics": tmpl.lyrics, "source": tmpl.source,
            "supply": "casting_direct", "casting_voice": voice["key"],
            "anchor_sha1": anchor_sha1, "take_seed": take["seed"],
            "line_hits": take["hits"], "sim_vs_anchor": take["sim"],
            "voice_match": vm,
            "format": ext.lstrip("."), "sample_rate": 48000,
            "duration_sec": take["dur"],
            "rendered_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "note": "声库锚 remix 直供（song_factory）；勿用 SVC 分支覆盖",
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        n += 1
    return n


def cmd_status(tdir: Path, sroot: Path) -> int:
    voices = load_voices(tdir)
    tpls = load_song_manifest(tdir)
    print(f"声库 {len(voices)} 把 / 模板 {len(tpls)} 首"
          f"（enabled {sum(1 for t in tpls if t.enabled)}）")
    for v in voices:
        anchor_ok = (tdir / v["anchor"]).is_file()
        print(f"  [{v['key']}] {v['label']} 锚={'✓' if anchor_ok else '✗'} "
              f"画布{v['canvas_s']}s 人设×{len(v['personas'])}")
        for t in tpls:
            got = sum(1 for pid in v["personas"]
                      if any((Path(sroot) / pid / 'songs' /
                              f'{t.id}{ext}').is_file()
                             for ext in ('.ogg', '.wav')))
            flag = "enabled" if t.enabled else "disabled"
            print(f"      {t.id}({flag}): 备货 {got}/{len(v['personas'])}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="歌房工厂：声库锚 remix 批量供给")
    ap.add_argument("--template", default="", help="模板 id（见 manifest）")
    ap.add_argument("--voice", default="all",
                    help="声库 key 逗号分隔 / all（只跑带人设分组的嗓）")
    ap.add_argument("--tries", type=int, default=5)
    ap.add_argument("--data-root", default="", help="实例数据根（缺省自动发现）")
    ap.add_argument("--force", action="store_true", help="无视新鲜度重渲")
    ap.add_argument("--enable-template", action="store_true",
                    help="供给成功后把模板置 enabled")
    ap.add_argument("--status", action="store_true", help="只读盘点")
    args = ap.parse_args(argv)

    from scripts._data_root import resolve_data_roots
    root = Path(args.data_root) if args.data_root else resolve_data_roots()[0]
    tdir = templates_dir(root=root)
    sroot = stock_root(root=root)
    log(f"数据根 {root}")

    if args.status:
        return cmd_status(tdir, sroot)
    if not args.template:
        ap.error("--template 必填（或用 --status）")

    tpls = {t.id: t for t in load_song_manifest(tdir)}
    tmpl = tpls.get(args.template)
    if tmpl is None:
        log(f"模板 {args.template} 不在 manifest")
        return 1
    voices = [v for v in load_voices(tdir) if v["personas"]]
    if args.voice != "all":
        want = {x.strip() for x in args.voice.split(",") if x.strip()}
        voices = [v for v in voices if v["key"] in want]
    if not voices:
        log("无目标声库（检查 voices.json / --voice）")
        return 1

    try:
        _req(GATEWAY, "/api/gpu/status", timeout=15)
    except Exception as e:  # noqa: BLE001
        log(f"hub 不可达: {e}")
        return 2

    outdir = Path(root) / "logs" / "song_factory"
    outdir.mkdir(parents=True, exist_ok=True)
    failures = 0
    for voice in voices:
        anchor_path = tdir / voice["anchor"]
        if not anchor_path.is_file():
            log(f"[{voice['key']}] 锚缺失 {anchor_path}，跳过")
            failures += 1
            continue
        anchor_sha1 = sha1_bytes(anchor_path.read_bytes())
        if not args.force:
            fresh = all(stock_is_fresh(
                Path(sroot) / pid / "songs" / f"{tmpl.id}.json",
                anchor_sha1, tmpl.lyrics) for pid in voice["personas"])
            if fresh:
                log(f"[{voice['key']}] {tmpl.id} 全员新鲜，跳过（--force 重渲）")
                continue
        log(f"== {voice['label']}（{voice['key']}）× 《{tmpl.title}》 ==")
        take = render_voice_song(voice, tmpl.lyrics, tdir, args.tries)
        row = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
               "template": tmpl.id, "voice": voice["key"],
               "ok": take is not None}
        if take is None:
            log(f"  !! {voice['key']} {args.tries} 抽全败")
            failures += 1
        else:
            n = stock_take(take, voice, tmpl, sroot, anchor_sha1)
            row.update(seed=take["seed"], attempt=take["attempt"],
                       hits=take["hits"], sim=take["sim"], dur=take["dur"],
                       stocked=n)
            log(f"  >> 过闸（第{take['attempt']}抽）→ 铺 {n} 人设")
        with (outdir / "factory.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    if args.enable_template and not failures:
        mf = tdir / "manifest.json"
        data = json.loads(mf.read_text(encoding="utf-8"))
        for r in data.get("templates") or []:
            if r.get("id") == tmpl.id:
                r["enabled"] = True
        mf.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                      encoding="utf-8")
        log(f"manifest：{tmpl.id} → enabled")
    print("FACTORY DONE" if not failures else f"FACTORY PARTIAL fail={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
