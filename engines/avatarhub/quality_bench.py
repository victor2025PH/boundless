# -*- coding: utf-8 -*-
"""quality_bench.py — 产品效果基线度量（数据驱动优化的起点）

把三条产品线的核心效果指标量化成可重复的基线，便于「每次优化测得出提升」，
也可直接作为对客户的效果承诺数据。纯度量、只读、不改任何服务、不做门禁。

测什么：
  VoiceX  音色相似度(campplus cosine) / 自然度 / 韵律 + 合成延迟 + 实时率 xRT
          （合成走 /avatar/speak，打分走现成的 /api/clone_score）
  LiveX   流式首音延迟 TTFA（/tts/stream_sse 到第一段 audio_base64 的时间）
  FaceX   换脸吞吐 fps / 是否真正换脸（faces_used）；人脸相似度可选(--face-sim, 需 insightface)

软降级：某服务/能力不可用 → 该项标 skip 并给原因，绝不中断其余度量。

用法：
  python quality_bench.py                         # 默认活动角色，各项 5 次
  python quality_bench.py --runs 10 --profile 刘德华
  python quality_bench.py --face faces\刘德华.jpg --face-sim
  python quality_bench.py --json                  # 仅输出 JSON（CI/留痕）
报告落盘：logs/quality_bench.json
"""
import os
import io
import sys
import json
import time
import wave
import base64
import argparse
import statistics
import urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
except Exception:
    pass

HERE = Path(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_HUB = os.environ.get("ACCEPT_HUB", "http://127.0.0.1:9000")
TEXT_SHORT = "你好，今天天气不错，我们一起出去走走吧。"
TEXT_STREAM = "第一句话用来测量首音延迟。第二句话让流式继续推进。第三句收尾。"


def _post(base, path, body, timeout=120):
    """POST JSON → (ok, data|errstr, wall_ms)。"""
    t0 = time.time()
    try:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(base + path, data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", errors="replace"))
        return True, d, (time.time() - t0) * 1000.0
    except Exception as e:
        return False, str(e), (time.time() - t0) * 1000.0


def _get(base, path, timeout=10):
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            return True, json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        return False, str(e)


def wav_duration_s(b64):
    try:
        with wave.open(io.BytesIO(base64.b64decode(b64)), "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return None


def agg(xs):
    xs = [float(x) for x in xs if x is not None]
    if not xs:
        return None
    s = sorted(xs)

    def p(q):
        return s[min(len(s) - 1, int(round((len(s) - 1) * q)))]
    return {"n": len(xs), "mean": round(statistics.mean(xs), 3),
            "p50": round(p(0.5), 3), "p95": round(p(0.95), 3),
            "min": round(min(xs), 3), "max": round(max(xs), 3)}


def stream_probe(base, body, timeout=90):
    """/tts/stream_sse：返回 (ttfa_ms|None, info)。info 含 sentences/chunks/done/elapsed_ms/err，
    便于在拿不到首音时给出诊断（如"产出 N 句却 0 段音频"）。"""
    t0 = time.time()
    info = {"sentences": None, "chunks": 0, "done": False, "err": None}
    try:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(base + "/tts/stream_sse", data=data, method="POST",
                                     headers={"Content-Type": "application/json"})
        resp = urllib.request.urlopen(req, timeout=timeout)
    except Exception as e:
        info["err"] = str(e)
        return None, info
    try:
        for raw in resp:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            try:
                obj = json.loads(line[5:].strip())
            except Exception:
                continue
            if "total_sentences" in obj:
                info["sentences"] = obj["total_sentences"]
            if obj.get("audio_base64"):
                info["chunks"] += 1
                if info["chunks"] == 1:
                    ms = (time.time() - t0) * 1000.0
                    return ms, info
            if obj.get("error"):
                info["err"] = obj["error"]
                break
            if obj.get("done"):
                info["done"] = True
                break
        info["elapsed_ms"] = round((time.time() - t0) * 1000.0)
        return None, info
    finally:
        try:
            resp.close()
        except Exception:
            pass


def discover_profile(base, want):
    ok, d = _get(base, "/profiles")
    if not ok or not isinstance(d, dict):
        return want or "", []
    profs = d.get("profiles", []) or []
    names = [p.get("name", "") for p in profs]
    if want:
        return want, names
    for p in profs:
        if p.get("active"):
            return p.get("name", ""), names
    return (names[0] if names else ""), names


# ── 各产品线度量 ───────────────────────────────────────────────────
def bench_voicex(base, profile, runs, text):
    """VoiceX：合成 → 相似度/自然度 + 延迟/实时率。"""
    out = {"name": "VoiceX 音色克隆"}
    cos, sim, nat, synth_ms, xrt, labels = [], [], [], [], [], []
    for _ in range(runs):
        ok, d, wall = _post(base, "/avatar/speak",
                            {"text": text, "profile": profile, "language": "zh-cn"})
        if not ok or not d.get("audio_base64"):
            out.setdefault("errors", []).append(d if isinstance(d, str) else d.get("warning", "无音频"))
            continue
        ab = d["audio_base64"]
        el = d.get("elapsed_ms") or wall
        dur = wav_duration_s(ab)
        synth_ms.append(el)
        if dur and el:
            xrt.append(dur / (el / 1000.0))   # >1 = 快于实时
        ok2, sc, _ = _post(base, "/api/clone_score", {"profile": profile, "audio_base64": ab})
        if ok2 and isinstance(sc, dict):
            if isinstance(sc.get("cosine"), (int, float)):
                cos.append(sc["cosine"])
            if isinstance(sc.get("similarity"), (int, float)):
                sim.append(sc["similarity"])
            if isinstance(sc.get("naturalness"), (int, float)):
                nat.append(sc["naturalness"])
            if sc.get("label"):
                labels.append(sc["label"])
    if not synth_ms and not cos:
        out["status"] = "skip"
        out["note"] = "未取得合成音频（TTS 服务未就绪？）"
        return out
    out["status"] = "ok"
    out["similarity_score"] = agg(sim)        # 0~1 友好相似度
    out["similarity_cosine_raw"] = agg(cos)   # campplus 原始 cosine
    out["similarity_label"] = labels[-1] if labels else ""
    out["naturalness"] = agg(nat)
    out["synth_latency_ms"] = agg(synth_ms)
    out["realtime_xRT"] = agg(xrt)
    return out


def bench_livex(base, profile, runs):
    """LiveX：流式首音延迟 TTFA。"""
    out = {"name": "LiveX 流式首音"}
    ttfa, last = [], None
    for _ in range(runs):
        ms, info = stream_probe(base, {"text": TEXT_STREAM, "profile": profile,
                                       "language": "zh-cn", "low_latency": True})
        last = info
        if ms is not None:
            ttfa.append(ms)
    if not ttfa:
        out["status"] = "skip"
        if last and last.get("err"):
            out["note"] = "流式报错：" + str(last["err"])
        elif last and last.get("sentences") and last.get("chunks") == 0:
            out["note"] = ("流式产出 0 段音频（共 %s 句、%sms 后 done）——"
                           "疑似流式 TTS 未适配该角色引擎(如 fish_speech)，每句合成被静默跳过"
                           % (last.get("sentences"), last.get("elapsed_ms", "?")))
        else:
            out["note"] = "未取得流式首音（流式 TTS 未就绪）"
        out["diagnostic"] = last
        return out
    out["status"] = "ok"
    out["ttfa_ms"] = agg(ttfa)
    out["diagnostic"] = last
    return out


def _load_face_b64(face_arg):
    cand = []
    if face_arg:
        cand.append(Path(face_arg) if os.path.isabs(face_arg) else HERE / face_arg)
    fd = HERE / "faces"
    if fd.is_dir():
        cand += sorted(fd.glob("*.jpg")) + sorted(fd.glob("*.png"))
    for p in cand:
        try:
            if p.is_file():
                return base64.b64encode(p.read_bytes()).decode("ascii"), p.name
        except Exception:
            continue
    return None, None


def face_cosine(src_b64, dst_b64):
    """可选：用 insightface 计算源脸与换脸结果的人脸相似度（cosine）。"""
    try:
        import numpy as np
        import cv2
        from insightface.app import FaceAnalysis
        global _FA
        try:
            _FA
        except NameError:
            _FA = FaceAnalysis(name="buffalo_l")
            _FA.prepare(ctx_id=0, det_size=(640, 640))

        def emb(b64):
            arr = np.frombuffer(base64.b64decode(b64), np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            faces = _FA.get(img)
            if not faces:
                return None
            f = max(faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
            return f.normed_embedding
        a, b = emb(src_b64), emb(dst_b64)
        if a is None or b is None:
            return None, "未检出人脸"
        import numpy as np2
        return float(np2.dot(a, b)), None
    except Exception as e:
        return None, str(e)


def bench_facex(base, runs, face_arg, do_sim):
    """FaceX：换脸吞吐/成功；可选人脸相似度。"""
    out = {"name": "FaceX 图片换脸"}
    tgt_b64, tgt_name = _load_face_b64(face_arg)
    if not tgt_b64:
        out["status"] = "skip"
        out["note"] = "未找到可用人脸图（faces/*.jpg 或 --face）"
        return out
    swap_ms, used, sims = [], [], []
    last_src = last_res = None
    for _ in range(runs):
        ok, d, wall = _post(base, "/faceswap", {"source_image": tgt_b64, "target_image": tgt_b64})
        if not ok or not isinstance(d, dict) or not d.get("result_image"):
            out.setdefault("errors", []).append(d if isinstance(d, str) else "无 result_image")
            continue
        swap_ms.append(d.get("elapsed_ms") or wall)
        used.append(d.get("faces_used", 0) or 0)
        last_src, last_res = tgt_b64, d["result_image"]
    if not swap_ms:
        out["status"] = "skip"
        out["note"] = "换脸未产出（faceswap 服务未就绪？）"
        return out
    out["status"] = "ok"
    out["target_face"] = tgt_name
    out["swap_latency_ms"] = agg(swap_ms)
    fps = [1000.0 / m for m in swap_ms if m]
    out["throughput_fps"] = agg(fps)
    out["faces_used_avg"] = round(statistics.mean(used), 2) if used else 0
    if do_sim and last_res:
        sim, err = face_cosine(last_src, last_res)
        out["face_cosine"] = sim if sim is not None else None
        if err:
            out["face_sim_note"] = err
    elif not do_sim:
        out["face_sim_note"] = "未测人脸相似度（加 --face-sim 启用，需 insightface）"
    return out


def slo_targets(base):
    ok, d = _get(base, "/api/latency_dashboard")
    if ok and isinstance(d, dict):
        t = (d.get("slo") or {}).get("targets") or {}
        return {"speak_p95_ms": t.get("speak_p95_ms"),
                "stream_ttfa_p95_ms": t.get("stream_ttfa_p95_ms")}
    return {}


def _fmt(a):
    return "—" if not a else "mean %.2f · p50 %.2f · p95 %.2f (n=%d)" % (
        a["mean"], a["p50"], a["p95"], a["n"])


def main():
    ap = argparse.ArgumentParser(description="产品效果基线度量 quality_bench")
    ap.add_argument("--hub", default=DEFAULT_HUB)
    ap.add_argument("--profile", default="", help="角色名；空=自动取活动角色")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--text", default=TEXT_SHORT)
    ap.add_argument("--face", default="", help="换脸用人脸图路径；空=自动取 faces/*")
    ap.add_argument("--face-sim", dest="face_sim", action="store_true",
                    help="额外测人脸相似度（需 insightface，较慢）")
    ap.add_argument("--only", default="", help="只跑 voicex,livex,facex 之子集（逗号分隔）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    base = args.hub.rstrip("/")

    if not args.json:
        print("=" * 64)
        print("  产品效果基线度量  quality_bench   %s" % time.strftime("%Y-%m-%d %H:%M:%S"))

    profile, names = discover_profile(base, args.profile)
    want = set(s.strip() for s in args.only.split(",") if s.strip()) or {"voicex", "livex", "facex"}
    slo = slo_targets(base)

    report = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "hub": base, "profile": profile,
              "runs": args.runs, "slo": slo, "sections": {}}

    if not args.json:
        print("  Hub: %s   角色: %s   每项 %d 次" % (base, profile or "(未指定)", args.runs))
        print("=" * 64)

    if "voicex" in want:
        if not args.json:
            print("\n▶ VoiceX 合成 + 相似度/延迟 …", flush=True)
        report["sections"]["voicex"] = bench_voicex(base, profile, args.runs, args.text)
    if "livex" in want:
        if not args.json:
            print("▶ LiveX 流式首音 TTFA …", flush=True)
        report["sections"]["livex"] = bench_livex(base, profile, args.runs)
    if "facex" in want:
        if not args.json:
            print("▶ FaceX 换脸吞吐%s …" % ("（含人脸相似度）" if args.face_sim else ""), flush=True)
        report["sections"]["facex"] = bench_facex(base, args.runs, args.face, args.face_sim)

    try:
        (HERE / "logs").mkdir(exist_ok=True)
        (HERE / "logs" / "quality_bench.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    v = report["sections"].get("voicex")
    if v and v.get("status") == "ok":
        print("\n── VoiceX 音色克隆 ──")
        sm = v.get("similarity_score")
        print("  音色相似度 (0~1)  : " + _fmt(sm) + (
            ("  → %s" % v.get("similarity_label")) if v.get("similarity_label") else ""))
        print("  原始 cosine       : " + _fmt(v.get("similarity_cosine_raw")))
        print("  自然度            : " + _fmt(v.get("naturalness")))
        print("  合成延迟 ms       : " + _fmt(v.get("synth_latency_ms"))
              + (("   [SLO p95≤%sms]" % slo["speak_p95_ms"]) if slo.get("speak_p95_ms") else ""))
        print("  实时率 xRT        : " + _fmt(v.get("realtime_xRT")) + "   (>1 快于实时)")
    elif v:
        print("\n── VoiceX ── 跳过：" + v.get("note", ""))

    lv = report["sections"].get("livex")
    if lv and lv.get("status") == "ok":
        print("\n── LiveX 流式首音 ──")
        print("  首音延迟 TTFA ms  : " + _fmt(lv.get("ttfa_ms"))
              + (("   [SLO p95≤%sms]" % slo["stream_ttfa_p95_ms"]) if slo.get("stream_ttfa_p95_ms") else ""))
    elif lv:
        print("\n── LiveX ── 跳过：" + lv.get("note", ""))

    fx = report["sections"].get("facex")
    if fx and fx.get("status") == "ok":
        print("\n── FaceX 图片换脸 ──")
        print("  换脸延迟 ms       : " + _fmt(fx.get("swap_latency_ms")))
        print("  吞吐 fps          : " + _fmt(fx.get("throughput_fps")))
        print("  实际换脸数/次     : %s" % fx.get("faces_used_avg"))
        if fx.get("face_cosine") is not None:
            print("  人脸相似度 cosine : %.4f" % fx["face_cosine"])
        elif fx.get("face_sim_note"):
            print("  人脸相似度        : %s" % fx["face_sim_note"])
    elif fx:
        print("\n── FaceX ── 跳过：" + fx.get("note", ""))

    print("\n报告：logs/quality_bench.json")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
