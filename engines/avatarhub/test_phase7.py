# -*- coding: utf-8 -*-
"""Phase 7 测试：流式 TTFA 滚动分布 + SLO 看板 + 低延迟首块 + 流式水印一致性 (T26-T30)

说明：本阶段是「在已有句级流式 + UI 指标看板之上」的加强，不重复造轮子。
离线测纯函数（首块切分 / 分位 / 超标率）；在线测 /api/latency_dashboard 结构。
"""
import sys, os
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PASS = 0; FAIL = 0; SKIP = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  [PASS] {name} {detail}")
    else:    FAIL += 1; print(f"  [FAIL] {name} {detail}")
def skip(name, why=""):
    global SKIP; SKIP += 1; print(f"  [SKIP] {name} {why}")

print("=" * 55)
print(" Phase 7 测试：流式 TTFA / SLO 看板 / 低延迟首块")
print("=" * 55)

# ── 离线：导入 avatar_hub 测纯函数 ────────────────
_AH = None
try:
    import avatar_hub as _AH
except Exception as e:
    print(f"  (import avatar_hub 失败，离线纯函数测试跳过: {e})")

# ── T26: 低延迟首块切分降低 TTFA ──────────────────
print("\n--- T26: 低延迟首块切分 ---")
if _AH:
    try:
        long_first = ["今天天气非常好，我们一起去公园散步吧。", "然后再去吃饭。"]
        out = _AH._split_first_for_ttfa(long_first, max_head_chars=16)
        check("首句被切为更短首块", len(out[0]) < len(long_first[0]), f"first={out[0]!r}")
        check("后续内容完整保留", out[-1] == long_first[-1])
        # 短首句不切
        short = ["你好。", "再见。"]
        check("短首句保持不变", _AH._split_first_for_ttfa(short) == short)
        # 无子句标点不切
        nopunct = ["这是一段没有任何子句标点的很长的文本内容需要被合成"]
        check("无切点时原样返回", _AH._split_first_for_ttfa(nopunct) == nopunct)
    except Exception as e:
        check("低延迟首块", False, str(e))
else:
    skip("低延迟首块", "(import 失败)")

# ── T27: 分位/超标率纯函数 ────────────────────────
print("\n--- T27: 分位与 SLO 超标率 ---")
if _AH:
    try:
        s = sorted([100, 200, 300, 400, 500, 600, 700, 800, 900, 1000])
        check("p50 正确", _AH._pct(s, 0.50) in (500, 600), f"p50={_AH._pct(s,0.50)}")
        check("p95 接近尾部", _AH._pct(s, 0.95) >= 900)
        check("空列表返回 0", _AH._pct([], 0.5) == 0)
        check("超标率 50%", _AH._breach_pct(s, 500) == 50.0, f"={_AH._breach_pct(s,500)}")
        check("无样本超标率 0", _AH._breach_pct([], 100) == 0.0)
    except Exception as e:
        check("分位/超标率", False, str(e))
else:
    skip("分位/超标率", "(import 失败)")

# ── T28: /api/latency_dashboard 结构（在线）───────
print("\n--- T28: /api/latency_dashboard 结构 ---")
HUB = os.environ.get("HUB_URL", "http://127.0.0.1:9000")
_online = False
try:
    import requests
    _online = requests.get(f"{HUB}/health", timeout=8).status_code == 200
except Exception:
    _online = False
if _online:
    try:
        import requests
        d = requests.get(f"{HUB}/api/latency_dashboard", timeout=10).json()
        check("含 speak 分位块", all(k in d.get("speak", {}) for k in ("p50_ms","p95_ms","p99_ms","histogram")))
        check("含 streaming_ttfa 滚动分位", all(k in d.get("streaming_ttfa", {}) for k in ("window_p50_ms","window_p95_ms","cumulative_avg_ms")))
        check("含 engines 列表", isinstance(d.get("engines"), list) and len(d["engines"]) >= 5)
        check("含 SLO 预算与超标率", all(k in d.get("slo", {}) for k in ("targets","speak_p95_breach_pct","stream_ttfa_breach_pct")))
    except Exception as e:
        check("/api/latency_dashboard", False, str(e))
else:
    skip("/api/latency_dashboard", "(Hub 未运行)")

# ── T29: dashboard 与 /metrics 不冲突（在线回归）──
print("\n--- T29: /metrics 回归 ---")
if _online:
    try:
        import requests
        m = requests.get(f"{HUB}/metrics", timeout=10).json()
        check("/metrics 仍含 p50/p95/p99", all(k in m for k in ("speak_latency_p50ms","speak_latency_p95ms","speak_latency_p99ms")))
        check("/metrics 仍含 stream_sse_avg_first_ms", "stream_sse_avg_first_ms" in m)
    except Exception as e:
        check("/metrics 回归", False, str(e))
else:
    skip("/metrics 回归", "(Hub 未运行)")

# ── T30: 流式水印一致性（需 TTS 子服务，否则跳过）─
print("\n--- T30: 流式输出水印一致性 ---")
if _online:
    try:
        import requests, json, base64
        h = requests.get(f"{HUB}/health", timeout=8).json()
        tts_up = bool(h.get("services", {}).get("tts"))
        if not tts_up:
            skip("流式水印", "(TTS 子服务未运行)")
        else:
            r = requests.post(f"{HUB}/avatar/speak/stream",
                json={"text": "你好，流式水印测试。", "language": "zh-cn"},
                timeout=120, stream=True)
            audio_b64 = ""
            for line in r.iter_lines(decode_unicode=True):
                if line and line.startswith("data: "):
                    d = json.loads(line[6:])
                    if d.get("phase") == "done":
                        audio_b64 = d.get("audio_base64", "")
            import provenance as P
            v = P.verify_credentials(base64.b64decode(audio_b64)) if audio_b64 else {"has_watermark": False}
            check("流式 done 输出带水印", v.get("has_watermark") is True)
    except Exception as e:
        check("流式水印", False, str(e))
else:
    skip("流式水印", "(Hub 未运行)")

print("\n" + "=" * 55)
print(f" 结果: PASS={PASS}  FAIL={FAIL}  SKIP={SKIP}")
print("=" * 55)
sys.exit(1 if FAIL else 0)
