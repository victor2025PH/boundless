#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
lipsync_ab_bench.py — Ditto(512 全脸) vs MuseTalk(256 + GFPGAN) 实时口型 A/B 基准

为「默认全量 Ditto vs 分场景(直播 Ditto/手机 MuseTalk)」这个决策产出客观数据 + 主观盲评素材。

三轴度量：
  1) 吞吐/实时性 (RTF)：同音频、同脸，直调各引擎 /generate，读服务端 X-Processing-Time →
     RTF = render_time / audio_seconds。RTF<1 = 快于实时(可实时)。多句多长度取分布。
  2) 句首帧延迟 (TTFV)：各引擎 /generate_stream 推到本脚本起的本地 sink，统一口径记「首段到达」
     耗时（两引擎同一测法，可比）。MuseTalk 另读其自报 ttfv_ms 交叉校验。
  3) 单卡满载稳定性：后台用 fish TTS 持续合成施压（模拟直播答句时 fish+ditto 抢同一张卡），
     测 ditto 的 RTF idle vs loaded + 显存水位，看实时引擎在满载下是否仍 RTF≲1。

另产出「双盲评测」素材：同一句的 ditto / musetalk 成片随机命名（隐藏映射）+ 自包含
  blind_review.html —— 人工只看画质盲评（看不到引擎名），评完点「揭晓」得主观胜率。

设计要点：
  * 直调后端隔离 TTS 干扰；参数对齐 Hub 部署（ditto sampling_timesteps=25、stream=15；
    musetalk fps=25/batch=8、enhance=gfpgan）。
  * 每引擎先预热一次（注册脸 + cudnn/GFPGAN 自整定），不计入计时，测稳态逐句成本。
  * 每个 pass 独立 try/except，单臂失败不拖垮整轮。

用法：
  python lipsync_ab_bench.py                      # 默认全跑(吞吐+首帧+盲评素材)，不含 load
  python lipsync_ab_bench.py --load               # 加单卡满载稳定性
  python lipsync_ab_bench.py --sentences 3 --profile 阿龙
  python lipsync_ab_bench.py --no-firstframe --no-clips   # 只测吞吐
"""
import os
import io
import sys
import json
import time
import wave
import random
import base64
import argparse
import threading
import contextlib
import subprocess
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import requests
except Exception:
    print("需要 requests（facefusion env 应已安装）：", sys.exc_info()[1])
    sys.exit(1)

try:
    import cv2
except Exception:
    cv2 = None

HUB = os.environ.get("AB_HUB", "http://127.0.0.1:9000")
DITTO = os.environ.get("AB_DITTO", "http://127.0.0.1:8096")
MUSE = os.environ.get("AB_MUSE", "http://127.0.0.1:8090")
FISH = os.environ.get("AB_FISH", "http://127.0.0.1:7855")
OUT_ROOT = os.path.join("logs", "lipsync_ab")
FFPROBE_CANDIDATES = [r"C:\echomimic\bin\ffprobe.exe", r"C:\ditto\bin\ffprobe.exe", "ffprobe"]

# 测试句（长度递增：开场短句→中句→长句；覆盖固定开销摊销对 RTF 的影响）
SENTENCES = [
    "大家好，欢迎来到直播间。",
    "今天给大家带来一款非常实用的好物，性价比很高。",
    "这款产品我自己也在用，质量真的没话说，回购了好几次了，强烈推荐给你们。",
    "如果你还在犹豫，那就听我一句劝，这个价格真的是全网最低了，错过今天就没有了，赶紧下单吧。",
    "感谢大家的支持，记得点点关注，下次开播不迷路，我们下期再见啦。",
]


# ─────────────────────────── 小工具 ───────────────────────────
def _get_json(url, timeout=10):
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _wav_seconds(b: bytes):
    """WAV 时长(秒)；非 WAV 返回 None。"""
    try:
        with contextlib.closing(wave.open(io.BytesIO(b), "rb")) as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return None


def _ffprobe_seconds(path: str):
    for exe in FFPROBE_CANDIDATES:
        try:
            r = subprocess.run([exe, "-v", "error", "-show_entries", "format=duration",
                                "-of", "default=nw=1:nk=1", path],
                               capture_output=True, text=True, timeout=20)
            if r.returncode == 0 and r.stdout.strip():
                return float(r.stdout.strip())
        except Exception:
            continue
    return None


def _probe_mp4(b: bytes):
    """解析 mp4：帧数 / fps / 宽 / 高 / 时长(秒)。优先 cv2，回退 ffprobe。"""
    info = {"frames": None, "fps": None, "w": None, "h": None, "dur": None, "kb": len(b) // 1024}
    tmp = os.path.join(OUT_ROOT, f"_probe_{int(time.time()*1000)%100000}.mp4")
    try:
        with open(tmp, "wb") as f:
            f.write(b)
        if cv2 is not None:
            cap = cv2.VideoCapture(tmp)
            if cap.isOpened():
                info["frames"] = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or None
                info["fps"] = round(cap.get(cv2.CAP_PROP_FPS), 2) or None
                info["w"] = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or None
                info["h"] = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or None
                cap.release()
                if info["frames"] and info["fps"]:
                    info["dur"] = round(info["frames"] / info["fps"], 3)
        if not info["dur"]:
            info["dur"] = _ffprobe_seconds(tmp)
    finally:
        try:
            os.remove(tmp)
        except Exception:
            pass
    return info


def _xproc_seconds(resp) -> float:
    h = resp.headers.get("X-Processing-Time", "")
    try:
        return float(h.replace("s", "").strip())
    except Exception:
        return None


def _pct(vals, p):
    if not vals:
        return None
    s = sorted(vals)
    k = max(0, min(len(s) - 1, int(round((p / 100.0) * (len(s) - 1)))))
    return s[k]


def _stat(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return {"n": 0}
    return {"n": len(vals), "avg": round(sum(vals) / len(vals), 3),
            "p50": round(_pct(vals, 50), 3), "p95": round(_pct(vals, 95), 3),
            "min": round(min(vals), 3), "max": round(max(vals), 3)}


# ─────────────────────────── 输入准备 ───────────────────────────
def resolve_profile_face(profile_arg: str):
    """选角色(优先有声+有脸) + 取其脸图 bytes。返回 (name, face_bytes)。"""
    data = _get_json(f"{HUB}/profiles")
    profs = data.get("profiles", [])
    name = profile_arg
    if not name:
        cand = [p for p in profs if p.get("has_voice") and p.get("has_face")]
        if cand:
            name = cand[0]["name"]
        else:
            name = data.get("active") or (profs[0]["name"] if profs else "")
    if not name:
        raise RuntimeError("无可用角色")
    full = _get_json(f"{HUB}/profiles/{name}?include_face=true")
    fb64 = full.get("face_b64") or ""
    if not fb64:
        raise RuntimeError(f"角色 {name} 无脸图")
    return name, base64.b64decode(fb64)


def synth_audio(profile: str, text: str):
    """/api/tts_only 合成克隆音 → (wav_bytes, seconds)。失败返回 (None, None)。"""
    try:
        r = requests.post(f"{HUB}/api/tts_only",
                          json={"profile": profile, "text": text, "language": "zh-cn"},
                          timeout=120)
        j = r.json()
        if not j.get("ok") and not j.get("audio_base64"):
            return None, None
        b = base64.b64decode(j["audio_base64"])
        return b, _wav_seconds(b)
    except Exception as e:
        print(f"   ! TTS 合成失败: {e}")
        return None, None


# ─────────────────────────── 引擎调用 ───────────────────────────
def ditto_register(face_bytes):
    try:
        requests.post(f"{DITTO}/ditto/register",
                      files={"face": ("face.jpg", face_bytes, "image/jpeg")}, timeout=120)
    except Exception as e:
        print(f"   ! ditto register: {e}")


def muse_precompute(face_bytes, face_id="ab_bench"):
    try:
        requests.post(f"{MUSE}/lipsync/precompute_face",
                      files={"face": ("face.jpg", face_bytes, "image/jpeg")},
                      data={"face_id": face_id}, timeout=120)
        return face_id
    except Exception as e:
        print(f"   ! muse precompute: {e}")
        return ""


def gen_ditto(audio_bytes, face_bytes, sampling_timesteps=25):
    return requests.post(f"{DITTO}/ditto/generate",
                         files={"audio": ("a.wav", audio_bytes, "audio/wav"),
                                "face": ("face.jpg", face_bytes, "image/jpeg")},
                         data={"sampling_timesteps": sampling_timesteps}, timeout=600)


def gen_muse(audio_bytes, face_bytes, face_id="", enhance=""):
    files = {"audio": ("a.wav", audio_bytes, "audio/wav")}
    data = {"fps": 25, "batch_size": 8}
    if enhance:
        data["enhance"] = enhance
    if face_id:
        data["face_id"] = face_id
    else:
        files["face"] = ("face.jpg", face_bytes, "image/jpeg")
    return requests.post(f"{MUSE}/lipsync/generate", files=files, data=data, timeout=600)


ARMS = {
    "ditto":           {"label": "Ditto 512 全脸", "engine": "ditto"},
    "musetalk":        {"label": "MuseTalk 256 原始", "engine": "musetalk"},
    "musetalk_gfpgan": {"label": "MuseTalk 256 + GFPGAN", "engine": "musetalk"},
}


def gen_arm(arm, audio_bytes, face_bytes, face_id):
    if arm == "ditto":
        return gen_ditto(audio_bytes, face_bytes, sampling_timesteps=25)
    if arm == "musetalk":
        return gen_muse(audio_bytes, face_bytes, face_id=face_id, enhance="")
    if arm == "musetalk_gfpgan":
        return gen_muse(audio_bytes, face_bytes, face_id=face_id, enhance="gfpgan")
    raise ValueError(arm)


# ─────────────────────────── 首帧 sink ───────────────────────────
class _Sink:
    """本地广播桩：接收 /play 段，记录每段到达时间（统一测两引擎 TTFV）。"""
    def __init__(self, port):
        self.port = port
        self.arrivals = []   # 绝对时间戳
        self._t0 = None
        h = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0) or 0)
                if n:
                    self.rfile.read(n)
                if h._t0 is not None:
                    h.arrivals.append(time.time())
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

        self._srv = ThreadingHTTPServer(("127.0.0.1", port), H)
        self._th = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def start(self):
        self._th.start()

    def begin(self):
        self.arrivals = []
        self._t0 = time.time()

    def first_ms(self):
        if self._t0 is None or not self.arrivals:
            return None
        return round((min(self.arrivals) - self._t0) * 1000, 1)

    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        try:
            self._srv.shutdown()
        except Exception:
            pass


def ttfv_ditto(sink, audio_bytes, face_bytes):
    sink.begin()
    requests.post(f"{DITTO}/ditto/generate_stream",
                  files={"audio": ("a.wav", audio_bytes, "audio/wav"),
                         "face": ("face.jpg", face_bytes, "image/jpeg")},
                  data={"vcam_url": sink.url(), "fps": 25, "first_seg_frames": 6,
                        "seg_frames": 25, "sampling_timesteps": 15, "stream_dit": True},
                  timeout=600)
    return sink.first_ms()


def ttfv_muse(sink, audio_bytes, face_bytes, face_id, enhance="gfpgan"):
    sink.begin()
    files = {"audio": ("a.wav", audio_bytes, "audio/wav")}
    data = {"vcam_url": sink.url(), "fps": 25, "first_seg_frames": 15, "seg_frames": 25,
            "enhance": enhance, "push_segs": True}
    if face_id:
        data["face_id"] = face_id
    else:
        files["face"] = ("face.jpg", face_bytes, "image/jpeg")
    r = requests.post(f"{MUSE}/lipsync/generate_stream", files=files, data=data, timeout=600)
    self_ttfv = None
    try:
        self_ttfv = r.json().get("ttfv_ms")
    except Exception:
        pass
    return sink.first_ms(), self_ttfv


# ─────────────────────────── 显存 ───────────────────────────
def vram_free_used():
    try:
        g = _get_json(f"{HUB}/api/gpu/status").get("vram", {})
        return g.get("free_mb"), g.get("used_mb"), g.get("total_mb")
    except Exception:
        return None, None, None


# ─────────────────────────── 各 pass ───────────────────────────
def pass_throughput(arms, audios, face_bytes, face_id, results):
    print("\n=== [1/3] 吞吐 / 实时性 (RTF) ===")
    clips = {}   # (arm, idx) -> mp4 bytes（供盲评素材）
    for arm in arms:
        lab = ARMS[arm]["label"]
        # 预热一次（注册脸 + 自整定），不计时
        try:
            print(f"  · 预热 {lab} …")
            gen_arm(arm, audios[0][0], face_bytes, face_id)
        except Exception as e:
            print(f"   ! {lab} 预热失败，跳过该臂: {e}")
            results["throughput"][arm] = {"error": str(e)}
            continue
        rows = []
        for idx, (ab, asec) in enumerate(audios):
            try:
                t0 = time.time()
                r = gen_arm(arm, ab, face_bytes, face_id)
                wall = time.time() - t0
                if r.status_code != 200:
                    print(f"   ! {lab} 句{idx} HTTP {r.status_code}")
                    continue
                srv = _xproc_seconds(r)
                mp4 = _probe_mp4(r.content)
                dur = asec or mp4["dur"]
                rtf = round(srv / dur, 3) if (srv and dur) else None
                rows.append({"idx": idx, "audio_s": round(dur, 2) if dur else None,
                             "render_s": srv, "wall_s": round(wall, 2), "rtf": rtf,
                             "frames": mp4["frames"], "res": f"{mp4['w']}x{mp4['h']}",
                             "fps": mp4["fps"], "kb": mp4["kb"]})
                clips[(arm, idx)] = r.content
                print(f"  {lab:22s} 句{idx} {dur:5.2f}s音频 render={srv}s RTF={rtf} "
                      f"{mp4['w']}x{mp4['h']} {mp4['frames']}帧 {mp4['kb']}KB")
            except Exception as e:
                print(f"   ! {lab} 句{idx} 异常: {e}")
        results["throughput"][arm] = {
            "label": lab, "rows": rows,
            "rtf": _stat([x["rtf"] for x in rows]),
            "render_s": _stat([x["render_s"] for x in rows]),
            "resolution": rows[0]["res"] if rows else None,
        }
    return clips


def pass_firstframe(audios, face_bytes, face_id, results, port):
    print("\n=== [2/3] 句首帧延迟 (TTFV，本地 sink 统一口径) ===")
    sink = _Sink(port)
    try:
        sink.start()
    except Exception as e:
        print(f"   ! 无法启动 sink: {e}")
        return
    try:
        d_rows, m_rows, m_self = [], [], []
        # 预热流式各一次
        try:
            ttfv_ditto(sink, audios[0][0], face_bytes)
            ttfv_muse(sink, audios[0][0], face_bytes, face_id)
        except Exception as e:
            print(f"   ! 流式预热: {e}")
        for idx, (ab, asec) in enumerate(audios):
            try:
                d = ttfv_ditto(sink, ab, face_bytes)
                d_rows.append(d)
                print(f"  Ditto            句{idx} TTFV={d}ms")
            except Exception as e:
                print(f"   ! ditto stream 句{idx}: {e}")
            try:
                m, ms = ttfv_muse(sink, ab, face_bytes, face_id)
                m_rows.append(m)
                m_self.append(ms)
                print(f"  MuseTalk+GFPGAN  句{idx} TTFV={m}ms (自报 {ms}ms)")
            except Exception as e:
                print(f"   ! muse stream 句{idx}: {e}")
        results["firstframe"] = {
            "ditto_ttfv_ms": _stat(d_rows),
            "musetalk_gfpgan_ttfv_ms": _stat(m_rows),
            "musetalk_self_reported_ttfv_ms": _stat(m_self),
        }
    finally:
        sink.stop()


def pass_load(audios, face_bytes, face_id, profile, results, secs):
    print(f"\n=== [3/3] 单卡满载稳定性（后台 fish 施压 {secs}s，测 ditto RTF idle vs loaded） ===")
    # 基线（idle）
    idle = []
    for ab, asec in audios:
        try:
            t0 = time.time()
            r = gen_ditto(ab, face_bytes)
            if r.status_code == 200:
                srv = _xproc_seconds(r)
                dur = asec or _probe_mp4(r.content)["dur"]
                if srv and dur:
                    idle.append(round(srv / dur, 3))
        except Exception:
            pass
    print(f"  idle ditto RTF: {_stat(idle)}")

    # 后台 fish 施压
    stop = threading.Event()
    fish_calls = [0]

    def _load_worker():
        txt = "这是一段用于压测的语音合成文本，持续不断地占用语音合成的算力。"
        while not stop.is_set():
            try:
                requests.post(f"{HUB}/api/tts_only",
                              json={"profile": profile, "text": txt, "language": "zh-cn"},
                              timeout=60)
                fish_calls[0] += 1
            except Exception:
                time.sleep(0.5)

    workers = [threading.Thread(target=_load_worker, daemon=True) for _ in range(2)]
    for w in workers:
        w.start()
    time.sleep(2)   # 让压力起来

    loaded, vram_used_peak = [], 0
    t_end = time.time() + secs
    i = 0
    while time.time() < t_end:
        ab, asec = audios[i % len(audios)]
        i += 1
        try:
            t0 = time.time()
            r = gen_ditto(ab, face_bytes)
            if r.status_code == 200:
                srv = _xproc_seconds(r)
                dur = asec or _probe_mp4(r.content)["dur"]
                if srv and dur:
                    loaded.append(round(srv / dur, 3))
            _f, used, _t = vram_free_used()
            if used:
                vram_used_peak = max(vram_used_peak, used)
        except Exception as e:
            print(f"   ! loaded ditto 异常: {e}")
    stop.set()
    for w in workers:
        w.join(timeout=5)

    print(f"  loaded ditto RTF: {_stat(loaded)}  (后台 fish 合成 {fish_calls[0]} 次, "
          f"显存峰值 used≈{round((vram_used_peak or 0)/1024,1)}GB)")
    results["load"] = {
        "ditto_rtf_idle": _stat(idle),
        "ditto_rtf_loaded": _stat(loaded),
        "fish_calls": fish_calls[0],
        "vram_used_peak_mb": vram_used_peak,
    }


# ─────────────────────────── 盲评素材 ───────────────────────────
def build_blind(clips, run_dir, sentences):
    """把 ditto vs musetalk_gfpgan 同句成片随机命名写盘 + 隐藏映射 + blind_review.html。"""
    pair_arms = ("ditto", "musetalk_gfpgan")
    cdir = os.path.join(run_dir, "clips")
    os.makedirs(cdir, exist_ok=True)
    manifest, pairs = {}, []
    idxs = sorted({k[1] for k in clips})
    for idx in idxs:
        if all((a, idx) in clips for a in pair_arms):
            entry = {"sentence_idx": idx,
                     "sentence": sentences[idx] if idx < len(sentences) else "",
                     "clips": {}}
            order = list(pair_arms)
            random.shuffle(order)   # A/B 顺序随机
            ab = {}
            for slot, arm in zip(("A", "B"), order):
                cid = f"c{idx}_{slot}_{random.randint(1000,9999)}.mp4"
                with open(os.path.join(cdir, cid), "wb") as f:
                    f.write(clips[(arm, idx)])
                manifest[cid] = {"engine": arm, "label": ARMS[arm]["label"], "slot": slot, "idx": idx}
                ab[slot] = cid
            entry["clips"] = ab
            pairs.append(entry)
    with open(os.path.join(run_dir, "blind_manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    _write_blind_html(run_dir, pairs, manifest)
    return len(pairs)


def _write_blind_html(run_dir, pairs, manifest):
    html = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"/>
<title>口型引擎 · 双盲评测</title>
<style>
 body{margin:0;background:#0b0f17;color:#e5edf7;font:15px/1.6 system-ui,"Microsoft YaHei",sans-serif;}
 header{padding:16px 22px;border-bottom:1px solid #1f2a3c;position:sticky;top:0;background:#0b0f17;}
 h1{font-size:18px;margin:0;} .mut{color:#7c8aa0;font-size:13px;}
 .wrap{max-width:1000px;margin:0 auto;padding:18px 22px;}
 .pair{background:#121826;border:1px solid #1f2a3c;border-radius:14px;padding:16px;margin-bottom:18px;}
 .vids{display:flex;gap:16px;flex-wrap:wrap;}
 .v{flex:1;min-width:320px;} video{width:100%;border-radius:10px;background:#000;}
 .v h3{margin:6px 0;font-size:15px;}
 .opts{margin-top:12px;display:flex;gap:10px;flex-wrap:wrap;}
 .opts label{padding:7px 14px;border:1px solid #2a3850;border-radius:999px;cursor:pointer;}
 .opts input{margin-right:6px;}
 .sent{color:#9fb0c7;margin-bottom:10px;}
 button{padding:10px 20px;border:0;border-radius:10px;font-weight:700;cursor:pointer;
   background:linear-gradient(90deg,#37b6ff,#6ee7b7);color:#04101f;font-size:15px;}
 #result{margin-top:16px;padding:14px;border-radius:12px;background:#0e1726;border:1px solid #24405f;display:none;}
 .win{color:#6ee7b7;font-weight:700;}
</style></head><body>
<header><h1>口型引擎 · 双盲评测</h1>
<div class="mut">每组两个视频（A / B，引擎已隐藏且左右随机）。只凭画质/口型/自然度选更好的一个，全部评完点「揭晓结果」。</div></header>
<div class="wrap" id="pairs"></div>
<div class="wrap"><button onclick="reveal()">揭晓结果</button><div id="result"></div></div>
<script>
const PAIRS = __PAIRS__;
const MANIFEST = __MANIFEST__;
const wrap = document.getElementById('pairs');
PAIRS.forEach((p,i)=>{
  const d=document.createElement('div'); d.className='pair';
  d.innerHTML = `<div class="sent">第 ${i+1} 组　句子：${p.sentence||''}</div>
   <div class="vids">
     <div class="v"><h3>A</h3><video src="clips/${p.clips.A}" controls loop muted playsinline></video></div>
     <div class="v"><h3>B</h3><video src="clips/${p.clips.B}" controls loop muted playsinline></video></div>
   </div>
   <div class="opts">
     <label><input type="radio" name="q${i}" value="A">A 更好</label>
     <label><input type="radio" name="q${i}" value="B">B 更好</label>
     <label><input type="radio" name="q${i}" value="tie">差不多</label>
   </div>`;
  wrap.appendChild(d);
});
function reveal(){
  const tally={}; let answered=0;
  PAIRS.forEach((p,i)=>{
    const sel=document.querySelector(`input[name=q${i}]:checked`);
    if(!sel) return; answered++;
    let eng;
    if(sel.value==='tie'){ eng='tie'; }
    else { eng = MANIFEST[p.clips[sel.value]].engine; }
    tally[eng]=(tally[eng]||0)+1;
  });
  const r=document.getElementById('result'); r.style.display='block';
  let html=`<div>已评 ${answered}/${PAIRS.length} 组。胜出统计：</div><ul>`;
  Object.entries(tally).forEach(([k,v])=>{
    const name = k==='tie'?'差不多':(k==='ditto'?'Ditto 512 全脸':'MuseTalk 256 + GFPGAN');
    html+=`<li><span class="win">${name}</span>：${v} 组</li>`;
  });
  html+='</ul><div class="mut">每组揭晓：</div><ul>';
  PAIRS.forEach((p,i)=>{
    html+=`<li>第 ${i+1} 组：A=${MANIFEST[p.clips.A].label}，B=${MANIFEST[p.clips.B].label}</li>`;
  });
  html+='</ul>';
  r.innerHTML=html;
}
</script></body></html>"""
    html = html.replace("__PAIRS__", json.dumps(pairs, ensure_ascii=False))
    html = html.replace("__MANIFEST__", json.dumps(manifest, ensure_ascii=False))
    with open(os.path.join(run_dir, "blind_review.html"), "w", encoding="utf-8") as f:
        f.write(html)


# ─────────────────────────── 主流程 ───────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", default="")
    ap.add_argument("--sentences", type=int, default=len(SENTENCES))
    ap.add_argument("--engines", default="ditto,musetalk,musetalk_gfpgan")
    ap.add_argument("--no-firstframe", action="store_true")
    ap.add_argument("--no-clips", action="store_true")
    ap.add_argument("--load", action="store_true")
    ap.add_argument("--load-secs", type=int, default=60)
    ap.add_argument("--sink-port", type=int, default=9912)
    args = ap.parse_args()

    os.makedirs(OUT_ROOT, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = os.path.join(OUT_ROOT, f"run_{stamp}")
    os.makedirs(run_dir, exist_ok=True)
    arms = [a.strip() for a in args.engines.split(",") if a.strip() in ARMS]

    print(f"== Lipsync A/B 基准 == 输出: {run_dir}")
    # 健康检查
    health = {}
    for nm, url in (("ditto", DITTO), ("musetalk", MUSE)):
        try:
            health[nm] = _get_json(f"{url}/health")
        except Exception as e:
            health[nm] = {"error": str(e)}
    print(f"  ditto: {health['ditto'].get('models_loaded', health['ditto'])}  "
          f"musetalk: {health['musetalk'].get('models_loaded', health['musetalk'])}")
    if not health["ditto"].get("models_loaded"):
        print("  ! ditto 未就绪（端口 8096）。先确保 ditto 在线。")
    if not health["musetalk"].get("models_loaded"):
        print("  ! musetalk 未就绪（端口 8090）。可用 POST /api/engine/start?name=lipsync 预热后重试。")

    profile, face_bytes = resolve_profile_face(args.profile)
    print(f"  角色: {profile}  脸图: {len(face_bytes)//1024}KB")

    # 合成音频
    print("  合成测试音频（克隆音）…")
    audios = []
    for s in SENTENCES[:args.sentences]:
        ab, sec = synth_audio(profile, s)
        if ab and sec:
            audios.append((ab, sec))
            print(f"    · {sec:5.2f}s  «{s[:18]}…»")
    if not audios:
        print("  ! 无可用音频，退出。")
        return
    # 注册脸
    ditto_register(face_bytes)
    face_id = muse_precompute(face_bytes)

    f0, u0, tot = vram_free_used()
    results = {"meta": {"stamp": stamp, "profile": profile, "arms": arms,
                        "n_sentences": len(audios), "health": health,
                        "vram_total_mb": tot, "vram_free_start_mb": f0},
               "throughput": {}, "firstframe": {}, "load": {}}

    clips = pass_throughput(arms, audios, face_bytes, face_id, results)

    if not args.no_firstframe:
        try:
            pass_firstframe(audios, face_bytes, face_id, results, args.sink_port)
        except Exception as e:
            print(f"   ! firstframe pass 失败: {e}")

    if args.load:
        try:
            pass_load(audios, face_bytes, face_id, profile, results, args.load_secs)
        except Exception as e:
            print(f"   ! load pass 失败: {e}")

    n_pairs = 0
    if not args.no_clips and clips:
        try:
            n_pairs = build_blind(clips, run_dir, SENTENCES)
        except Exception as e:
            print(f"   ! 盲评素材失败: {e}")

    with open(os.path.join(run_dir, "results.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    # 汇总
    print("\n================ 汇总 ================")
    for arm in arms:
        t = results["throughput"].get(arm, {})
        if "rtf" in t and t["rtf"].get("n"):
            print(f"  {t['label']:24s} RTF avg={t['rtf']['avg']} p95={t['rtf']['p95']} "
                  f"render avg={t['render_s']['avg']}s  {t.get('resolution')}")
    if results.get("firstframe"):
        ff = results["firstframe"]
        d = ff.get("ditto_ttfv_ms", {})
        m = ff.get("musetalk_gfpgan_ttfv_ms", {})
        print(f"  首帧 TTFV  Ditto avg={d.get('avg')}ms p95={d.get('p95')}ms | "
              f"MuseTalk+GFPGAN avg={m.get('avg')}ms p95={m.get('p95')}ms")
    if results.get("load"):
        L = results["load"]
        print(f"  满载  ditto RTF idle avg={L['ditto_rtf_idle'].get('avg')} → "
              f"loaded avg={L['ditto_rtf_loaded'].get('avg')} p95={L['ditto_rtf_loaded'].get('p95')} "
              f"(显存峰值 {round((L.get('vram_used_peak_mb') or 0)/1024,1)}GB)")
    if n_pairs:
        print(f"  盲评素材: {n_pairs} 组 → 浏览器打开 {os.path.join(run_dir, 'blind_review.html')}")
    print(f"  结果 JSON: {os.path.join(run_dir, 'results.json')}")


if __name__ == "__main__":
    main()
