# -*- coding: utf-8 -*-
"""Phase 8 纯代码子集测试：LipSync 适配器分发 + FaceSwap 注册脚手架 (T31-T37)

验证「新口型/换脸引擎接入 = 实现并 attach 一个 Adapter」，且 musetalk 默认路径不变。
离线用 monkeypatch + 模拟未来适配器(LatentSync)，无需 lipsync 服务。
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
print(" Phase 8 测试：LipSync 分发 + FaceSwap 脚手架")
print("=" * 55)

_AH = None
try:
    import avatar_hub as _AH
    import engine_registry as _ER
except Exception as e:
    print(f"  (import 失败: {e})")

# ── T31: MuseTalk 已包成 LipSyncAdapter ────────────
print("\n--- T31: MuseTalkAdapter 注册 ---")
if _AH:
    try:
        ad = _ER.registry.get_adapter("musetalk")
        check("get_adapter('musetalk') 非空", ad is not None)
        check("是 LipSyncAdapter 实例", isinstance(ad, _ER.LipSyncAdapter))
        lst = {e["name"]: e for e in _ER.registry.list("lipsync")}
        check("/list 标注 musetalk has_adapter", lst.get("musetalk", {}).get("has_adapter") is True)
    except Exception as e:
        check("MuseTalkAdapter 注册", False, str(e))
else:
    skip("MuseTalkAdapter 注册", "(import 失败)")

# ── T32: lipsync 分发委托内置调用 ──────────────────
print("\n--- T32: lipsync 分发委托 ---")
if _AH:
    try:
        # 签名随 _lipsync_call_service 演进：Phase 后续加了 enhance 位(实时 256 画质增强)
        async def _fake_call(audio_bytes, face_bytes, fps=25, batch_size=8, smooth=None, enhance=""):
            return _AH._LSResp(200, b"FAKE_MP4")
        _orig = _AH._lipsync_call_service
        _AH._lipsync_call_service = _fake_call
        try:
            r = asyncio.run(_AH._lipsync_dispatch("musetalk", b"a", b"f"))
            check("musetalk 返回 200", r.status_code == 200)
            check("取到委托视频", r.video_bytes == b"FAKE_MP4")
        finally:
            _AH._lipsync_call_service = _orig
    except Exception as e:
        check("lipsync 分发委托", False, str(e))
else:
    skip("lipsync 分发委托", "(import 失败)")

# ── T33: 可插拔 — 模拟未来 LatentSync 适配器 ───────
print("\n--- T33: 可插拔新口型引擎(模拟 LatentSync) ---")
if _AH:
    try:
        class _FakeLatentSync(_ER.LipSyncAdapter):
            def capabilities(self): return {"engine": "latentsync", "resolution": "512x512"}
            async def generate(self, audio_bytes, face_bytes, *, fps=25, batch_size=8, smooth=None, **o):
                return _AH._LSResp(200, b"LATENTSYNC_HD")
        _ER.registry.register(_ER.EngineDescriptor(name="latentsync_test",
                              kind=_ER.KIND_LIPSYNC, description="模拟测试"))
        _ER.registry.attach_adapter("latentsync_test", _FakeLatentSync())
        # 桩掉健康门控：dispatch 会先探引擎服务存活(离线→回落 musetalk)；本测只验适配器路由
        async def _ready_true(engine):
            return True
        _orig_ready = _AH._lipsync_engine_ready
        _AH._lipsync_engine_ready = _ready_true
        try:
            r = asyncio.run(_AH._lipsync_dispatch("latentsync_test", b"a", b"f"))
        finally:
            _AH._lipsync_engine_ready = _orig_ready
        check("路由到新适配器", r.status_code == 200 and r.video_bytes == b"LATENTSYNC_HD",
              "→ 新口型引擎接入仅需 attach 一个 LipSyncAdapter")
    except Exception as e:
        check("可插拔新口型引擎", False, str(e))
else:
    skip("可插拔新口型引擎", "(import 失败)")

# ── T34: 未知口型引擎报错 ──────────────────────────
print("\n--- T34: 未知口型引擎报错 ---")
if _AH:
    try:
        _ER.registry.register(_ER.EngineDescriptor(name="ghost_ls", kind=_ER.KIND_LIPSYNC))
        raised = False
        async def _ready_true2(engine):
            return True
        _orig_ready2 = _AH._lipsync_engine_ready
        _AH._lipsync_engine_ready = _ready_true2      # 桩掉健康门控,让分发走到"无适配器"分支
        try:
            asyncio.run(_AH._lipsync_dispatch("ghost_ls", b"a", b"f"))
        except RuntimeError:
            raised = True
        finally:
            _AH._lipsync_engine_ready = _orig_ready2
        check("无适配器口型引擎抛 RuntimeError", raised)
    except Exception as e:
        check("未知口型引擎报错", False, str(e))
else:
    skip("未知口型引擎报错", "(import 失败)")

# ── T35: FaceSwap 注册脚手架 ───────────────────────
print("\n--- T35: FaceSwap 注册脚手架 ---")
if _AH:
    try:
        check("KIND_FACESWAP 合法", _ER.KIND_FACESWAP in _ER.VALID_KINDS)
        check("FaceSwapAdapter 基类存在", hasattr(_ER, "FaceSwapAdapter"))
        check("默认换脸引擎=inswapper", _ER.default_engine("faceswap") == "inswapper")
        fs = {e["name"]: e for e in _ER.registry.list("faceswap")}
        check("inswapper 已注册", "inswapper" in fs)
        check("Hub 降级 _default_engine 含 faceswap",
              _AH._default_engine("faceswap") == "inswapper")
    except Exception as e:
        check("FaceSwap 注册脚手架", False, str(e))
else:
    skip("FaceSwap 注册脚手架", "(import 失败)")

# ── T36: lipsync 路由集中化守卫 ────────────────────
print("\n--- T36: lipsync 路由集中化守卫 ---")
try:
    with open("avatar_hub.py", encoding="utf-8") as f:
        src = f.read()
    # 非流式 lipsync POST 调用点（精确计数：带右引号，排除 /lipsync/generate_stream
    # 与文档串里的 (/lipsync/generate)）。后期演进后集中在 2 处：
    #   ① 中央 helper _lipsync_call_service  ② 适配器基类 _LSAdapterBase。
    # 流式口型走独立端点 /lipsync/generate_stream（Phase 7-E 新增），单独计数守卫。
    n_post = src.count('/lipsync/generate"')
    n_stream = src.count("/lipsync/generate_stream")
    n_disp = src.count("await _lipsync_dispatch(")
    check("非流式 lipsync POST 集中(中央helper+适配器基类=2处)", n_post == 2, f"count={n_post}")
    check("流式口型走独立 /lipsync/generate_stream(=1)", n_stream == 1, f"count={n_stream}")
    check("经 _lipsync_dispatch 分发 ≥2 处", n_disp >= 2, f"count={n_disp}")
    check("SpeakRequest 含 lipsync_engine", "lipsync_engine:" in src)
except Exception as e:
    check("lipsync 路由集中化守卫", False, str(e))

# ── T37: /api/engines 含 faceswap（在线）───────────
print("\n--- T37: /api/engines 含 faceswap ---")
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
        j = requests.get(f"{HUB}/api/engines", timeout=8).json()
        names = [e["name"] for e in j.get("engines", [])]
        check("/api/engines 含 inswapper", "inswapper" in names, f"names={names}")
        check("defaults 含 faceswap", j.get("defaults", {}).get("faceswap") == "inswapper")
    except Exception as e:
        check("/api/engines 含 faceswap", False, str(e))
else:
    skip("/api/engines 含 faceswap", "(Hub 未运行)")

print("\n" + "=" * 55)
print(f" 结果: PASS={PASS}  FAIL={FAIL}  SKIP={SKIP}")
print("=" * 55)
sys.exit(1 if FAIL else 0)
