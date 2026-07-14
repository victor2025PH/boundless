# -*- coding: utf-8 -*-
"""Phase 6 第1步测试：VC 适配器分发接缝 (T21-T25)

验证「新变声引擎接入 = 实现并 attach 一个 VCAdapter」的可插拔性，
且 rvc 默认路径行为不变。离线用 monkeypatch + 模拟未来适配器，无需 RVC 服务。
"""
import sys, os, asyncio
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
print(" Phase 6-1 测试：VC 适配器分发接缝")
print("=" * 55)

_AH = None
try:
    import avatar_hub as _AH
    import engine_registry as _ER
except Exception as e:
    print(f"  (import 失败，离线测试跳过: {e})")

# ── T21: RVC 已被包成 VCAdapter 并注册 ────────────
print("\n--- T21: RvcAdapter 注册 ---")
if _AH:
    try:
        ad = _ER.registry.get_adapter("rvc")
        check("get_adapter('rvc') 非空", ad is not None)
        check("是 VCAdapter 实例", isinstance(ad, _ER.VCAdapter))
        check("capabilities 含 engine=rvc", ad.capabilities().get("engine") == "rvc")
        lst = {e["name"]: e for e in _ER.registry.list("vc")}
        check("/list 标注 rvc has_adapter", lst.get("rvc", {}).get("has_adapter") is True)
    except Exception as e:
        check("RvcAdapter 注册", False, str(e))
else:
    skip("RvcAdapter 注册", "(import 失败)")

# ── T22: rvc 分发委托 _call_rvc（行为不变）─────────
print("\n--- T22: rvc 分发委托 ---")
if _AH:
    try:
        class _FakeResp:
            status_code = 200
            def json(self): return {"audio_base64": "FAKE_RVC_AUDIO"}
        async def _fake_call_rvc(audio_b64, model, settings):
            return _FakeResp()
        _orig = _AH._call_rvc
        _AH._call_rvc = _fake_call_rvc
        try:
            r = asyncio.run(_AH._vc_dispatch("rvc", "in", "model.pth", {}))
            check("rvc 返回 200", r.status_code == 200)
            check("rvc 取到委托音频", r.audio_b64 == "FAKE_RVC_AUDIO")
        finally:
            _AH._call_rvc = _orig
    except Exception as e:
        check("rvc 分发委托", False, str(e))
else:
    skip("rvc 分发委托", "(import 失败)")

# ── T23: 可插拔 — 模拟未来 Seed-VC 适配器 ──────────
print("\n--- T23: 可插拔新引擎(模拟 Seed-VC) ---")
if _AH:
    try:
        class _FakeSeedVC(_ER.VCAdapter):
            def capabilities(self): return {"engine": "seedvc_test", "zero_shot": True}
            async def convert(self, audio_b64, *, target="", settings=None, **o):
                return _AH._VCResp(200, "SEEDVC_OUT")
        _ER.registry.register(_ER.EngineDescriptor(name="seedvc_test", kind="vc",
                              description="模拟测试引擎"))
        _ER.registry.attach_adapter("seedvc_test", _FakeSeedVC())
        r = asyncio.run(_AH._vc_dispatch("seedvc_test", "in", "", {}))
        check("路由到新适配器", r.status_code == 200 and r.audio_b64 == "SEEDVC_OUT",
              "→ 新引擎接入仅需 attach 一个 VCAdapter")
    except Exception as e:
        check("可插拔新引擎", False, str(e))
else:
    skip("可插拔新引擎", "(import 失败)")

# ── T24: 缺适配器的未知引擎明确报错 ────────────────
print("\n--- T24: 未知引擎报错 ---")
if _AH:
    try:
        _ER.registry.register(_ER.EngineDescriptor(name="ghost_vc", kind="vc"))
        raised = False
        try:
            asyncio.run(_AH._vc_dispatch("ghost_vc", "in", "", {}))
        except RuntimeError:
            raised = True
        check("无适配器引擎抛 RuntimeError", raised)
    except Exception as e:
        check("未知引擎报错", False, str(e))
else:
    skip("未知引擎报错", "(import 失败)")

# ── T26: 分发已集中化（6-6 源码守卫）──────────────
print("\n--- T26: RVC 路由集中化守卫 ---")
try:
    with open("avatar_hub.py", encoding="utf-8") as f:
        src = f.read()
    n_call = src.count("await _call_rvc(")
    n_disp = src.count("await _vc_dispatch(")
    # 仅 RvcAdapter + _vc_dispatch 回退两处可直呼 _call_rvc
    check("直呼 _call_rvc 仅剩 2 处(适配器+回退)", n_call == 2, f"count={n_call}")
    # 6 个端点经分发：speak/stream/batch/batch_stream/ab_compare/sse
    check("经 _vc_dispatch 分发 ≥6 处", n_disp >= 6, f"count={n_disp}")
    check("stream_sse 支持 body.vc_engine 覆盖", 'body.get("vc_engine")' in src)
except Exception as e:
    check("RVC 路由集中化守卫", False, str(e))

# ── T25: 默认 speak 回归（在线，需 TTS+RVC）────────
print("\n--- T25: 默认 speak 回归 ---")
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
        h = requests.get(f"{HUB}/health", timeout=8).json()
        if not h.get("services", {}).get("tts"):
            skip("默认 speak 回归", "(TTS 子服务未运行)")
        else:
            r = requests.post(f"{HUB}/avatar/speak",
                json={"text": "Phase6 适配器回归测试"}, timeout=120)
            check("默认 speak 返回音频", r.status_code == 200 and bool(r.json().get("audio_base64")),
                  f"status={r.status_code}")
    except Exception as e:
        check("默认 speak 回归", False, str(e))
else:
    skip("默认 speak 回归", "(Hub 未运行)")

print("\n" + "=" * 55)
print(f" 结果: PASS={PASS}  FAIL={FAIL}  SKIP={SKIP}")
print("=" * 55)
sys.exit(1 if FAIL else 0)
