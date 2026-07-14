# -*- coding: utf-8 -*-
"""Phase 5 测试：引擎适配器层 + 指标埋点 (T16-T20)

离线部分（导入 engine_registry）始终运行；
在线部分（HTTP 调用 :9000）仅在 AvatarHub 运行时执行，否则跳过。
"""
import sys, json
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PASS = 0; FAIL = 0; SKIP = 0
def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1; print(f"  [PASS] {name} {detail}")
    else:
        FAIL += 1; print(f"  [FAIL] {name} {detail}")
def skip(name, why=""):
    global SKIP
    SKIP += 1; print(f"  [SKIP] {name} {why}")

print("=" * 55)
print(" Phase 5 测试：引擎适配器层 + 指标埋点")
print("=" * 55)

import os
HUB = os.environ.get("HUB_URL", "http://127.0.0.1:9000")

# ── 在线检测 ──────────────────────────────────────
_online = False
try:
    import requests
    r = requests.get(f"{HUB}/health", timeout=8)  # /health 含并行子服务探测，给足超时
    _online = r.status_code == 200
except Exception:
    _online = False

# ── T16: 注册表列出内置引擎（离线）────────────────
print("\n--- T16: 注册表列出内置引擎 ---")
try:
    from engine_registry import registry, KIND_TTS, KIND_VC, KIND_LIPSYNC, default_engine
    tts = set(registry.names(KIND_TTS))
    vc  = set(registry.names(KIND_VC))
    ls  = set(registry.names(KIND_LIPSYNC))
    check("含 xtts/cosyvoice/gptsovits", {"xtts","cosyvoice","gptsovits"} <= tts, f"tts={sorted(tts)}")
    check("含 rvc 变声引擎", "rvc" in vc, f"vc={sorted(vc)}")
    check("含 musetalk 口型引擎", "musetalk" in ls, f"lipsync={sorted(ls)}")
    # 2026-07-06: 默认 TTS 已改 fish_speech(常驻核心主力;xtts 为可选扩展,缺权重默认不启动)
    check("默认 TTS=fish_speech", default_engine("tts") == "fish_speech")
    check("默认 VC=rvc", default_engine("vc") == "rvc")
except Exception as e:
    check("import engine_registry", False, str(e))

# ── T17: /avatar/speak 默认行为不变（回归）────────
print("\n--- T17: speak 默认行为回归 ---")
_tts_up = False
if _online:
    try:
        _h = requests.get(f"{HUB}/health", timeout=8).json()
        _svc = _h.get("services", {})
        _tts_up = bool(_svc.get("tts")) if isinstance(_svc, dict) else False
    except Exception:
        _tts_up = False
if _online and _tts_up:
    try:
        r = requests.post(f"{HUB}/avatar/speak",
            json={"text": "你好，这是Phase5回归测试", "language": "zh-cn"}, timeout=120)
        ok = r.status_code == 200 and bool(r.json().get("audio_base64"))
        check("默认 speak 返回音频", ok, f"status={r.status_code}")
    except Exception as e:
        check("默认 speak", False, str(e))
else:
    skip("speak 默认行为", "(Hub 或 TTS 子服务未运行)")

# ── T18: 指定不存在的 engine 返回 400 + available 列表 ──
print("\n--- T18: 未知引擎拒绝 ---")
if _online:
    try:
        r = requests.post(f"{HUB}/avatar/speak",
            json={"text": "测试", "tts_engine": "__no_such_engine__"}, timeout=15)
        body = r.json()
        detail = body.get("detail", body)
        has_avail = isinstance(detail, dict) and "available" in detail
        check("未知引擎返回 400", r.status_code == 400, f"status={r.status_code}")
        check("400 含 available 列表", has_avail, f"detail={detail}")
    except Exception as e:
        check("未知引擎拒绝", False, str(e))
else:
    skip("未知引擎拒绝", "(Hub 未运行)")

# ── T19: 延迟记录（离线可测）+ /api/engines 延迟字段 ──
print("\n--- T19: 引擎延迟记录 ---")
try:
    from engine_registry import registry as _reg
    _reg.record_latency("xtts", 123)
    _reg.record_latency("xtts", 321)
    st = _reg.get("xtts").latency_stats()
    check("xtts 延迟样本累计", st["count"] >= 2, f"stats={st}")
    check("延迟统计含 avg/p50/p95", all(k in st for k in ("avg_ms","p50_ms","p95_ms")))
except Exception as e:
    check("延迟记录", False, str(e))

# ── T20: /api/engines 标注 available/latency ──────
print("\n--- T20: /api/engines 端点 ---")
if _online:
    try:
        r = requests.get(f"{HUB}/api/engines", timeout=10)
        body = r.json()
        engines = body.get("engines", [])
        check("/api/engines 200", r.status_code == 200)
        check("返回引擎列表非空", len(engines) >= 5, f"count={len(engines)}")
        e0 = engines[0] if engines else {}
        check("每项含 available 字段", "available" in e0)
        check("每项含 latency 统计", "latency" in e0)
        check("含 defaults 默认引擎", "defaults" in body and body["defaults"].get("tts") == "fish_speech")
    except Exception as e:
        check("/api/engines", False, str(e))
else:
    skip("/api/engines 端点", "(Hub 未运行)")

print("\n" + "=" * 55)
print(f" 结果: PASS={PASS}  FAIL={FAIL}  SKIP={SKIP}")
print("=" * 55)
sys.exit(1 if FAIL else 0)
