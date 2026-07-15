# -*- coding: utf-8 -*-
"""
配音引擎首包延迟(TTFA)压测：对比 fish / qwen3 / cosyvoice 的克隆 TTS 延迟。

测两条链路(与 live_interpreter 实际用法一致)：
  • clone(非流式)  /v1/tts/clone            → 拿到整段音频的总耗时(≈该引擎“说第一个字”前你要等的时间)
  • clone/stream    /v1/tts/clone/stream      → 首个 PCM 帧到达耗时(TTFA，真实“多久开始出声”) + 总耗时
    仅 fish/qwen3 支持该端点；cosyvoice 无“克隆+流式”单端点(其 /v1/tts/stream 用内置参考音不克隆)，故只测非流式。

用法(在服务已启动的实机)：
  python bench_tts.py                         # 测三引擎，各 5 次，用内置正弦参考音(仅测延迟)
  python bench_tts.py --ref myvoice.wav       # 用真实参考音(更贴近线上数字/音色)
  python bench_tts.py --engines fish qwen3 --n 8 --text "Hello, this is a latency test."
  python bench_tts.py --json                  # 机器可读输出

参考音不影响延迟结论：内置正弦仅用于满足接口(cosyvoice 要求非空参考)，音质不代表真实克隆。
"""
import argparse
import base64
import io
import math
import statistics
import struct
import sys
import time
import wave

import requests

DEFAULT_TEXT = ("This is a real-time interpretation latency benchmark, "
                "measuring how fast each engine starts speaking.")

# 引擎 → base URL(优先 app_config，其次内置默认端口)。与 live_interpreter._tts_url_for 对齐。
_DEFAULT_PORTS = {"fish": 7855, "qwen3": 7858, "cosyvoice": 7852}
_SVC_KEY = {"fish": "fish_tts", "qwen3": "qwen3_tts", "cosyvoice": "emotion_tts"}
# 是否支持 Fish 式“克隆+流式”单端点(与 live_interpreter._CLONE_STREAM_ENGINES 对齐)
_CLONE_STREAM = {"fish", "qwen3"}
# 与 live_interpreter._enqueue_synth_stream 一致：跳过 <0.05s@44.1k 的 priming/碎帧
_MIN_PCM_BYTES = 2205


def _resolve_url(engine: str) -> str:
    try:
        import app_config as ac
        return ac.svc_url(_SVC_KEY[engine])
    except Exception:
        return f"http://127.0.0.1:{_DEFAULT_PORTS[engine]}"


def _gen_sine_wav(seconds: float = 3.0, sr: int = 16000, freq: float = 220.0) -> bytes:
    """生成一段单声道正弦 WAV(仅用于满足克隆接口的参考音要求；不代表真实音色)。"""
    buf = io.BytesIO()
    w = wave.open(buf, "wb")
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr)
    frames = bytearray()
    for i in range(int(sr * seconds)):
        frames += struct.pack("<h", int(0.2 * 32767 * math.sin(2 * math.pi * freq * i / sr)))
    w.writeframes(bytes(frames)); w.close()
    return buf.getvalue()


def _load_ref(path: str):
    if path:
        with open(path, "rb") as f:
            data = f.read()
        return base64.b64encode(data).decode(), "This is a reference voice sample for cloning."
    return base64.b64encode(_gen_sine_wav()).decode(), "This is a reference voice sample for cloning."


def _payload(text: str, ref_b64: str, ref_text: str) -> dict:
    p = {"text": text, "language": "en", "return_base64": True,
         "temperature": 0.7, "top_p": 0.7, "repetition_penalty": 1.2, "seed": 42}
    if ref_b64:
        p["reference_audio_b64"] = ref_b64
        p["reference_text"] = ref_text
    return p


def _healthy(url: str) -> bool:
    try:
        return requests.get(url + "/health", timeout=3).ok
    except Exception:
        return False


def _clone_once(url: str, text: str, ref_b64: str, ref_text: str) -> float:
    t0 = time.time()
    r = requests.post(url + "/v1/tts/clone", json=_payload(text, ref_b64, ref_text), timeout=180)
    r.raise_for_status()
    if not r.json().get("audio_base64"):
        raise RuntimeError("空 audio_base64")
    return (time.time() - t0) * 1000.0


def _clone_stream_once(url: str, text: str, ref_b64: str, ref_text: str):
    """返回 (TTFA_ms, total_ms)：首个有效 PCM 帧到达耗时 + 全流结束耗时。"""
    t0 = time.time()
    r = requests.post(url + "/v1/tts/clone/stream", json=_payload(text, ref_b64, ref_text),
                      stream=True, timeout=180)
    r.raise_for_status()
    buf = b""; ttfa = None
    try:
        for raw in r.iter_content(chunk_size=4096):
            buf += raw
            while len(buf) >= 4:
                ln = struct.unpack("<I", buf[:4])[0]
                if ln == 0:
                    buf = b""; break
                if len(buf) < 4 + ln:
                    break
                pcm = buf[4:4 + ln]; buf = buf[4 + ln:]
                if len(pcm) < _MIN_PCM_BYTES:
                    continue
                if ttfa is None:
                    ttfa = (time.time() - t0) * 1000.0
    finally:
        r.close()
    return ttfa, (time.time() - t0) * 1000.0


def _stat(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return {"n": len(vals), "min": round(min(vals), 1),
            "median": round(statistics.median(vals), 1),
            "mean": round(statistics.mean(vals), 1),
            "max": round(max(vals), 1)}


def bench(engines, text, ref_b64, ref_text, n):
    out = {}
    for eng in engines:
        url = _resolve_url(eng)
        rec = {"url": url, "healthy": _healthy(url)}
        if not rec["healthy"]:
            rec["skipped"] = "服务未就绪(/health 不可达)"
            out[eng] = rec; continue
        try:                                       # 预热一次(不计入)：冷启动/参考编码不污染均值
            _clone_once(url, text, ref_b64, ref_text)
        except Exception as e:
            rec["warmup_error"] = str(e)[:120]
        clone = []
        for _ in range(n):
            try: clone.append(_clone_once(url, text, ref_b64, ref_text))
            except Exception as e: rec.setdefault("clone_errors", []).append(str(e)[:120])
        rec["clone_ms"] = _stat(clone)
        if eng in _CLONE_STREAM:
            try: _clone_stream_once(url, text, ref_b64, ref_text)     # 预热
            except Exception: pass
            ttfas, totals = [], []
            for _ in range(n):
                try:
                    ttfa, total = _clone_stream_once(url, text, ref_b64, ref_text)
                    ttfas.append(ttfa); totals.append(total)
                except Exception as e:
                    rec.setdefault("stream_errors", []).append(str(e)[:120])
            rec["stream_ttfa_ms"] = _stat(ttfas)
            rec["stream_total_ms"] = _stat(totals)
        out[eng] = rec
    return out


def _print_human(res: dict):
    print("=" * 68)
    print("配音引擎延迟压测 (数值越小越好；TTFA=首包出声延迟)")
    print("=" * 68)
    for eng, rec in res.items():
        print(f"\n[{eng}]  {rec['url']}")
        if not rec.get("healthy"):
            print(f"  跳过：{rec.get('skipped', '不可达')}"); continue
        c = rec.get("clone_ms")
        if c:
            print(f"  clone(非流式)    n={c['n']}  中位 {c['median']}ms  (min {c['min']} / mean {c['mean']} / max {c['max']})")
        if rec.get("clone_errors"):
            print(f"    clone 失败 {len(rec['clone_errors'])} 次，例：{rec['clone_errors'][0]}")
        t = rec.get("stream_ttfa_ms"); tt = rec.get("stream_total_ms")
        if t:
            print(f"  clone/stream     n={t['n']}  TTFA 中位 {t['median']}ms  (min {t['min']} / max {t['max']})"
                  + (f"  |  总耗时中位 {tt['median']}ms" if tt else ""))
        elif eng in _CLONE_STREAM and rec.get("stream_errors"):
            print(f"    stream 失败 {len(rec['stream_errors'])} 次，例：{rec['stream_errors'][0]}")
        elif eng not in _CLONE_STREAM:
            print("  clone/stream     —（该引擎无克隆+流式端点，线上走分块非流式）")
    print("\n提示：线上 live_interpreter 流式默认走 clone/stream(fish/qwen3)；cosyvoice 走分块非流式克隆。")


def main():
    ap = argparse.ArgumentParser(description="配音引擎首包延迟(TTFA)压测")
    ap.add_argument("--engines", nargs="+", default=["fish", "qwen3", "cosyvoice"],
                    help="要测的引擎(默认三个都测)")
    ap.add_argument("--text", default=DEFAULT_TEXT, help="合成文本(英文)")
    ap.add_argument("--ref", default="", help="参考音 WAV 路径(默认用内置正弦，仅测延迟)")
    ap.add_argument("--n", type=int, default=5, help="每项测量次数(默认 5)")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ref_b64, ref_text = _load_ref(args.ref)
    res = bench(args.engines, args.text, ref_b64, ref_text, args.n)
    if args.json:
        import json
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        _print_human(res)


if __name__ == "__main__":
    main()
