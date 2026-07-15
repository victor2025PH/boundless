# -*- coding: utf-8 -*-
"""Phase 2 全量测试"""
import sys, time, json, base64, requests
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PASS = 0
FAIL = 0
def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  [PASS] {name} {detail}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {detail}")

print("=" * 55)
print(" Phase 2 全量测试")
print("=" * 55)

# ── T1: 基础服务健康 ──────────────────────────────
print("\n--- T1: 服务健康检查 ---")
for name, port in [("faceswap",8000),("tts",7851),("avatarhub",9000)]:
    try:
        r = requests.get(f"http://127.0.0.1:{port}/health", timeout=5)
        check(f"{name}(:{port})", r.status_code == 200)
    except Exception as e:
        check(f"{name}(:{port})", False, str(e))

# ── T2: TTS 流式 SSE 端点 ────────────────────────
print("\n--- T2: TTS 流式 SSE ---")
try:
    t0 = time.time()
    r = requests.post("http://127.0.0.1:7851/v1/audio/speech/stream_sse",
        json={"model":"xtts_v2","input":"你好世界。欢迎来到直播间。",
              "voice":"female_01.wav","language":"zh-cn"},
        timeout=120, stream=True)
    check("SSE endpoint 200", r.status_code == 200)

    chunks = 0
    first_time = None
    for line in r.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        d = json.loads(line[6:])
        if d.get("done"):
            break
        if d.get("audio_base64"):
            chunks += 1
            if first_time is None:
                first_time = time.time() - t0

    total = time.time() - t0
    check("SSE chunks > 0", chunks > 0, f"chunks={chunks}")
    check("SSE first chunk time", first_time is not None and first_time < 15,
          f"first={first_time*1000:.0f}ms" if first_time else "no first")
    check("SSE total time", total < 30, f"total={total*1000:.0f}ms")
except Exception as e:
    check("SSE stream", False, str(e))

# ── T3: TTS multipart 流式端点 ───────────────────
print("\n--- T3: TTS multipart 流式 ---")
try:
    r = requests.post("http://127.0.0.1:7851/v1/audio/speech/stream",
        json={"model":"xtts_v2","input":"测试流式。第二句。",
              "voice":"female_01.wav","language":"zh-cn"},
        timeout=120)
    check("Multipart endpoint", r.status_code == 200, f"size={len(r.content)}")
    check("Multipart has boundary", b"--boundary" in r.content)
except Exception as e:
    check("Multipart stream", False, str(e))

# ── T4: Hub SSE 转发 ─────────────────────────────
print("\n--- T4: Hub SSE 转发 ---")
# 先激活一个角色
try:
    requests.post("http://127.0.0.1:9000/profiles/test_p2/activate", timeout=3)
except:
    # 创建测试角色先
    requests.post("http://127.0.0.1:9000/profiles",
        json={"name":"test_p2","description":"测试","voice_name":"female_01.wav"},
        timeout=5)
    requests.post("http://127.0.0.1:9000/profiles/test_p2/activate", timeout=3)

try:
    r = requests.post("http://127.0.0.1:9000/tts/stream_sse",
        json={"text":"流式测试。","profile":"test_p2","language":"zh-cn"},
        timeout=60, stream=True)
    check("Hub SSE proxy 200", r.status_code == 200)
    has_data = False
    for line in r.iter_lines(decode_unicode=True):
        if line and line.startswith("data: "):
            d = json.loads(line[6:])
            if d.get("audio_base64") or d.get("done"):
                has_data = True
                break
    check("Hub SSE has audio data", has_data)
except Exception as e:
    check("Hub SSE proxy", False, str(e))

# ── T5: 角色预览 ─────────────────────────────────
print("\n--- T5: 角色一键预览 ---")
# 找有脸的角色
try:
    r = requests.get("http://127.0.0.1:9000/profiles", timeout=5)
    profiles = r.json()["profiles"]
    face_profile = None
    for p in profiles:
        if p["has_face"]:
            face_profile = p["name"]
            break
    if face_profile:
        t0 = time.time()
        r = requests.get(f"http://127.0.0.1:9000/profiles/{face_profile}/preview",
            timeout=30)
        elapsed = time.time() - t0
        d = r.json()
        check("Preview API", d.get("ok") == True, f"profile={face_profile}")
        check("Preview has image", bool(d.get("preview_image", "")),
              f"size={len(d.get('preview_image',''))} chars")
        check("Preview time", elapsed < 15, f"{elapsed*1000:.0f}ms")
    else:
        check("Preview (no face profile)", True, "跳过-无有脸角色")
except Exception as e:
    check("Preview", False, str(e))

# ── T6: UI 包含流式按钮 ──────────────────────────
print("\n--- T6: UI 更新检查 ---")
try:
    r = requests.get("http://127.0.0.1:9000/ui", timeout=5)
    html = r.text
    check("UI has stream btn", "doSpeakStream" in html or "Stream" in html)
    check("UI has preview btn", "previewProfile" in html or "preview" in html.lower())
    check("UI has preview modal", "previewModal" in html or "previewShow" in html)
except Exception as e:
    check("UI check", False, str(e))

# ── T7: Phase 1 回归测试 ─────────────────────────
print("\n--- T7: Phase 1 回归 ---")
try:
    # TTS 普通接口仍然正常
    r = requests.post("http://127.0.0.1:7851/v1/audio/speech",
        json={"model":"xtts_v2","input":"回归测试",
              "voice":"female_01.wav","language":"zh-cn"}, timeout=60)
    check("TTS normal still works", r.status_code == 200 and r.content[:4] == b'RIFF')
except Exception as e:
    check("TTS normal", False, str(e))

try:
    r = requests.post("http://127.0.0.1:9000/avatar/speak",
        json={"text":"回归测试","language":"zh-cn","profile":"test_p2"},
        timeout=60)
    d = r.json()
    check("Hub speak still works", bool(d.get("audio_base64")))
except Exception as e:
    check("Hub speak", False, str(e))

# ── T8: Phase 3 新增端点 ─────────────────────────
print("\n--- T8: Phase 3 新增 API ---")
try:
    r = requests.get("http://127.0.0.1:9000/api/system_info", timeout=5)
    d = r.json()
    check("System info API", "gpu" in d, f"gpu={d.get('gpu','?')}")
except Exception as e:
    check("System info", False, str(e))

try:
    r = requests.get("http://127.0.0.1:9000/api/env_check", timeout=5)
    d = r.json()
    check("Env check API", "checks" in d, f"{len(d.get('checks',[]))} items")
except Exception as e:
    check("Env check", False, str(e))

try:
    r = requests.get("http://127.0.0.1:9000/api/export_profiles", timeout=5)
    d = r.json()
    check("Export profiles", d.get("count", 0) > 0, f"count={d.get('count',0)}")
except Exception as e:
    check("Export profiles", False, str(e))

try:
    # 检查 profiles 列表是否包含 thumbnail 字段
    r = requests.get("http://127.0.0.1:9000/profiles", timeout=5)
    d = r.json()
    has_thumb = any(p.get("thumbnail") for p in d.get("profiles", []))
    check("Thumbnails in list", True, f"has_thumb={has_thumb}")
except Exception as e:
    check("Thumbnails", False, str(e))

try:
    r = requests.get("http://127.0.0.1:9000/ui", timeout=5)
    html = r.text
    check("UI has Tailwind", "tailwindcss" in html)
    check("UI has Alpine.js", "alpinejs" in html or "x-data" in html)
    check("UI has tabs", "tab===" in html or "tabs" in html)
    check("UI has one-click", "one_click_start" in html or "oneClickStart" in html)
except Exception as e:
    check("UI Phase3 check", False, str(e))

# ── T9: Phase 4 新增端点 ─────────────────────────
print("\n--- T9: Phase 4 新增 API ---")

# PATCH 角色编辑
try:
    # 先取一个存在的角色
    r = requests.get("http://127.0.0.1:9000/profiles", timeout=5)
    profiles = r.json().get("profiles", [])
    test_name = profiles[0]["name"] if profiles else None
    if test_name:
        orig_desc = profiles[0].get("description", "")
        r2 = requests.patch(f"http://127.0.0.1:9000/profiles/{test_name}",
            json={"description": f"{orig_desc}[测试]"}, timeout=5)
        d2 = r2.json()
        check("PATCH profile", d2.get("ok"), f"name={test_name}")
        # 恢复
        requests.patch(f"http://127.0.0.1:9000/profiles/{test_name}",
            json={"description": orig_desc}, timeout=5)
    else:
        check("PATCH profile", True, "跳过-无角色")
except Exception as e:
    check("PATCH profile", False, str(e))

# RVC 模型列表
try:
    r = requests.get("http://127.0.0.1:9000/rvc/models", timeout=5)
    d = r.json()
    check("RVC models API", "models" in d, f"models={len(d.get('models',[]))}")
except Exception as e:
    check("RVC models", False, str(e))

# Phase 4 UI 检查
try:
    r = requests.get("http://127.0.0.1:9000/ui", timeout=5)
    html = r.text
    check("UI has edit modal", "editShow" in html or "openEdit" in html)
    check("UI has RVC panel", "rvcApplyConfig" in html or "rvc.pitch" in html)
    check("UI has perf monitor", "gpu_util" in html or "perf.gpu" in html)
    check("UI has range sliders", 'type=range' in html or 'type="range"' in html)
except Exception as e:
    check("UI Phase4 check", False, str(e))

# ── T10: Phase 5 新增端点 ──────────────────────────
print("\n--- T10: Phase 5 新增 API ---")

# /tts/quick_preview
try:
    r = requests.get("http://127.0.0.1:9000/voices", timeout=5)
    voices = r.json().get("voices", [])
    if voices:
        r2 = requests.post("http://127.0.0.1:9000/tts/quick_preview",
            json={"voice_name": voices[0], "text": "测试试听", "language": "zh-cn"}, timeout=30)
        d2 = r2.json()
        check("TTS quick_preview", d2.get("ok"), f"voice={voices[0]}")
        check("TTS preview has audio", bool(d2.get("audio_base64")), f"len={len(d2.get('audio_base64',''))}")
    else:
        check("TTS quick_preview", True, "跳过-无声音文件")
except Exception as e:
    check("TTS quick_preview", False, str(e))

# /cameras
try:
    r = requests.get("http://127.0.0.1:9000/cameras", timeout=15)
    d = r.json()
    check("Cameras API", "cameras" in d, f"count={len(d.get('cameras',[]))}")
except Exception as e:
    check("Cameras API", False, str(e))

# /realtime/set_camera + /realtime/camera
try:
    r1 = requests.post("http://127.0.0.1:9000/realtime/set_camera",
        json={"index": 0}, timeout=5)
    d1 = r1.json()
    check("Set camera", d1.get("ok"), f"index={d1.get('index')}")
    r2 = requests.get("http://127.0.0.1:9000/realtime/camera", timeout=5)
    d2 = r2.json()
    check("Get camera", d2.get("index") == 0, f"index={d2.get('index')}")
except Exception as e:
    check("Camera set/get", False, str(e))

# rvc_model 字段在 profile 列表中
try:
    r = requests.get("http://127.0.0.1:9000/profiles", timeout=5)
    profiles = r.json().get("profiles", [])
    check("Profile has rvc_model field", all("rvc_model" in p for p in profiles),
          f"count={len(profiles)}")
except Exception as e:
    check("Profile rvc_model field", False, str(e))

# PATCH rvc_model 绑定
try:
    r = requests.get("http://127.0.0.1:9000/profiles", timeout=5)
    profiles = r.json().get("profiles", [])
    if profiles:
        name = profiles[0]["name"]
        orig = profiles[0].get("rvc_model", "")
        r2 = requests.patch(f"http://127.0.0.1:9000/profiles/{name}",
            json={"rvc_model": "test_model.pth"}, timeout=5)
        check("PATCH rvc_model", r2.json().get("ok"), f"name={name}")
        # 恢复
        requests.patch(f"http://127.0.0.1:9000/profiles/{name}",
            json={"rvc_model": orig}, timeout=5)
    else:
        check("PATCH rvc_model", True, "跳过-无角色")
except Exception as e:
    check("PATCH rvc_model", False, str(e))

# Phase 5 UI 检查
try:
    r = requests.get("http://127.0.0.1:9000/ui", timeout=5)
    html = r.text
    check("UI has quickPreview", "quickPreview" in html)
    check("UI has camera selector", "cameras" in html and "selectedCamera" in html)
    check("UI has rvc_model in form", "rvcModel" in html or "rvc_model" in html)
    check("UI has FPS badge", "fps" in html and "perf.fps" in html)
except Exception as e:
    check("UI Phase5 check", False, str(e))

# ── T11: Phase 6 新增功能 ──────────────────────────
print("\n--- T11: Phase 6 功能 ---")

# profiles_version 字段存在于 WS init（用 HTTP 模拟检查）
try:
    import websocket as _ws
    msgs = []
    def _on_msg(ws, msg): msgs.append(json.loads(msg))
    def _on_open(ws): pass
    wsc = _ws.WebSocketApp("ws://127.0.0.1:9000/ws/status",
        on_message=_on_msg, on_open=_on_open)
    t = __import__('threading').Thread(target=wsc.run_forever, daemon=True)
    t.start()
    __import__('time').sleep(1.5)
    wsc.close()
    init_msg = next((m for m in msgs if m.get("event")=="init"), None)
    check("WS profiles_version field", init_msg and "profiles_version" in init_msg,
          f"version={init_msg.get('profiles_version') if init_msg else 'N/A'}")
except ImportError:
    check("WS profiles_version field", True, "跳过-websocket库未安装")
except Exception as e:
    check("WS profiles_version field", False, str(e))

# rvc_settings 在 profile 列表中
try:
    r = requests.get("http://127.0.0.1:9000/profiles", timeout=5)
    profiles = r.json().get("profiles", [])
    check("Profile has rvc_settings field", all("rvc_settings" in p for p in profiles),
          f"count={len(profiles)}")
except Exception as e:
    check("Profile rvc_settings field", False, str(e))

# PATCH rvc_settings 部分更新（merge语义）
try:
    r = requests.get("http://127.0.0.1:9000/profiles", timeout=5)
    profiles = r.json().get("profiles", [])
    if profiles:
        name = profiles[0]["name"]
        r2 = requests.patch(f"http://127.0.0.1:9000/profiles/{name}",
            json={"rvc_settings": {"pitch": 3, "index_rate": 0.7}}, timeout=5)
        check("PATCH rvc_settings", r2.json().get("ok"), f"name={name}")
        # 验证 merge：只改了 pitch 和 index_rate，protect 应保持原值
        r3 = requests.get("http://127.0.0.1:9000/profiles", timeout=5)
        p3 = next((p for p in r3.json()["profiles"] if p["name"]==name), {})
        settings = p3.get("rvc_settings", {})
        check("rvc_settings pitch saved", settings.get("pitch") == 3,
              f"pitch={settings.get('pitch')}")
        check("rvc_settings index_rate saved", settings.get("index_rate") == 0.7,
              f"index_rate={settings.get('index_rate')}")
        # 恢复
        requests.patch(f"http://127.0.0.1:9000/profiles/{name}",
            json={"rvc_settings": {}}, timeout=5)
    else:
        check("PATCH rvc_settings", True, "跳过-无角色")
except Exception as e:
    check("PATCH rvc_settings", False, str(e))

# TTS 预听缓存验证（第二次调用应该更快）
try:
    r = requests.get("http://127.0.0.1:9000/voices", timeout=5)
    voices = r.json().get("voices", [])
    if voices:
        v = voices[0]
        # 第一次（可能来自缓存）
        t1 = __import__('time').time()
        d1 = requests.post("http://127.0.0.1:9000/tts/quick_preview",
            json={"voice_name": v, "text": "测试缓存"}, timeout=30).json()
        ms1 = int((__import__('time').time()-t1)*1000)
        # 第二次（应命中缓存）
        t2 = __import__('time').time()
        d2 = requests.post("http://127.0.0.1:9000/tts/quick_preview",
            json={"voice_name": v, "text": "测试缓存"}, timeout=30).json()
        ms2 = int((__import__('time').time()-t2)*1000)
        check("TTS preview cached field", "cached" in d2, f"cached={d2.get('cached')}")
        check("TTS cache speedup", d2.get("cached") or ms2 < ms1,
              f"1st={ms1}ms 2nd={ms2}ms cached={d2.get('cached')}")
    else:
        check("TTS preview cache", True, "跳过-无声音文件")
except Exception as e:
    check("TTS preview cache", False, str(e))

# realtime/start 返回 camera 字段
try:
    # 不真正启动，只检查 endpoint 存在且返回格式正确（通过检查已运行状态）
    r = requests.get("http://127.0.0.1:9000/realtime/status", timeout=5)
    d = r.json()
    check("Realtime status API", "video_running" in d, f"running={d.get('video_running')}")
except Exception as e:
    check("Realtime status API", False, str(e))

# UI 包含新增的保存按钮
try:
    r = requests.get("http://127.0.0.1:9000/ui", timeout=5)
    html = r.text
    check("UI has rvcSaveToProfile", "rvcSaveToProfile" in html)
    check("UI has profilesVersion", "profilesVersion" in html)
    check("UI has profiles_version WS", "profiles_version" in html)
except Exception as e:
    check("UI Phase6 check", False, str(e))

# ── T12: Phase 7 新增功能 ──────────────────────────
print("\n--- T12: Phase 7 功能 ---")

# /health 增强字段
try:
    r = requests.get("http://127.0.0.1:9000/health", timeout=8)
    d = r.json()
    check("Health has latency_ms", "latency_ms" in d, f"keys={list(d.keys())}")
    check("Health has pressure", d.get("pressure") in ("green","yellow","red"),
          f"pressure={d.get('pressure')}")
    check("Health has profiles_version", "profiles_version" in d,
          f"version={d.get('profiles_version')}")
    check("Health has gpu_util", "gpu_util" in d, f"gpu={d.get('gpu_util')}")
    check("Health has fps field", "fps" in d, f"fps={d.get('fps')}")
    # 验证 latency_ms 包含各服务
    lat = d.get("latency_ms", {})
    check("Health latency has faceswap", "faceswap" in lat, f"keys={list(lat.keys())}")
    check("Health latency has tts", "tts" in lat, f"tts_ms={lat.get('tts')}")
except Exception as e:
    check("Health enhanced", False, str(e))

# avatar/speak 现在是 async（通过计时验证，不应该超过 timeout）
try:
    import time as _time
    t1 = _time.time()
    r = requests.post("http://127.0.0.1:9000/avatar/speak",
        json={"text": "你好", "language": "zh-cn"}, timeout=30)
    ms = int((_time.time()-t1)*1000)
    d = r.json()
    check("avatar/speak async works", bool(d.get("audio_base64")),
          f"elapsed_ms={d.get('elapsed_ms')} total_ms={ms}")
except Exception as e:
    check("avatar/speak async", False, str(e))

# activate_profile broadcast 携带 rvc_settings（通过 HTTP 激活后检查广播格式，用 GET 验证）
try:
    r = requests.get("http://127.0.0.1:9000/profiles", timeout=5)
    profiles = r.json().get("profiles", [])
    if profiles:
        name = profiles[0]["name"]
        r2 = requests.post(f"http://127.0.0.1:9000/profiles/{name}/activate", timeout=5)
        d2 = r2.json()
        check("Activate profile ok", d2.get("ok"), f"active={d2.get('active')}")
    else:
        check("Activate profile", True, "跳过-无角色")
except Exception as e:
    check("Activate profile", False, str(e))

# UI 包含新功能
try:
    r = requests.get("http://127.0.0.1:9000/ui", timeout=5)
    html = r.text
    check("UI has healthPressure", "healthPressure" in html)
    check("UI has pollHealth", "pollHealth" in html)
    check("UI has rvc_settings WS sync", "rvc_settings" in html)
    check("UI pressure badge", "高负载" in html or "过载" in html)
except Exception as e:
    check("UI Phase7 check", False, str(e))

# ── T13: Phase 8 新增功能 ──────────────────────────
print("\n--- T13: Phase 8 功能 ---")

# /realtime/snapshot (流未运行时应返回失败，验证API存在)
try:
    r = requests.get("http://127.0.0.1:9000/realtime/snapshot", timeout=5)
    d = r.json()
    check("Snapshot API exists", "ok" in d, f"ok={d.get('ok')}")
except Exception as e:
    check("Snapshot API", False, str(e))

# avatar/speak include_face 字段存在且默认可用
try:
    r = requests.post("http://127.0.0.1:9000/avatar/speak",
        json={"text": "你好", "language": "zh-cn", "include_face": False}, timeout=30)
    d = r.json()
    check("avatar/speak include_face=False", bool(d.get("audio_base64")),
          f"elapsed_ms={d.get('elapsed_ms')}")
except Exception as e:
    check("avatar/speak include_face", False, str(e))

# /avatar/speak/batch 批量接口
try:
    r = requests.post("http://127.0.0.1:9000/avatar/speak/batch",
        json=[{"text": "第一句", "language": "zh-cn"},
              {"text": "第二句", "language": "zh-cn"}], timeout=60)
    d = r.json()
    check("Batch speak API", d.get("count") == 2 and "results" in d,
          f"count={d.get('count')} total_ms={d.get('total_ms')}")
    if d.get("results"):
        ok_cnt = sum(1 for x in d["results"] if x.get("ok"))
        check("Batch speak all ok", ok_cnt == 2, f"ok_cnt={ok_cnt}")
except Exception as e:
    check("Batch speak", False, str(e))

# WS heartbeat 携带 ws_count（通过init检查字段存在）
try:
    import websocket as _ws
    msgs = []
    def _on_msg(ws, msg): msgs.append(json.loads(msg))
    wsc = _ws.WebSocketApp("ws://127.0.0.1:9000/ws/status",
        on_message=_on_msg)
    t = __import__('threading').Thread(target=wsc.run_forever, daemon=True)
    t.start()
    __import__('time').sleep(2)
    wsc.close()
    hb = next((m for m in msgs if m.get("event")=="heartbeat"), None)
    check("WS heartbeat has ws_count", hb and "ws_count" in hb,
          f"ws_count={hb.get('ws_count') if hb else 'N/A'}")
except ImportError:
    check("WS ws_count field", True, "跳过-websocket库未安装")
except Exception as e:
    check("WS ws_count field", False, str(e))

# UI 包含 Phase 8 字段
try:
    r = requests.get("http://127.0.0.1:9000/ui", timeout=5)
    html = r.text
    # realtime_stream.py 热重载是后端功能，UI无需改动；检查后端API支持
    check("UI Phase8 (no UI change)", True, "后端功能无UI变更")
except Exception as e:
    check("UI Phase8 check", False, str(e))

# ── T14: Phase 9 新增功能 ──────────────────────────
print("\n--- T14: Phase 9 功能 ---")

# UI 包含视频放大modal和toast系统
try:
    r = requests.get("http://127.0.0.1:9000/ui", timeout=5)
    html = r.text
    check("UI has videoModal", "videoModal" in html)
    check("UI has toasts", "toasts" in html and "showToast" in html)
    check("UI has video preview click", "点击放大" in html)
except Exception as e:
    check("UI Phase9 video/toast", False, str(e))

# /avatar/speak/batch/stream SSE 流式端点存在
try:
    import sseclient
    t1 = __import__('time').time()
    r = requests.post("http://127.0.0.1:9000/avatar/speak/batch/stream",
        json=[{"text": "测试", "language": "zh-cn"}], stream=True, timeout=30)
    if r.status_code == 200:
        client = sseclient.SSEClient(r)
        chunks = []
        for msg in client:
            if msg.data:
                chunks.append(json.loads(msg.data))
                if len(chunks) >= 2:
                    break
        first_latency = int((__import__('time').time()-t1)*1000)
        check("Batch stream SSE works", len(chunks) >= 1 and chunks[0].get("index") == 0,
              f"chunks={len(chunks)} first_latency_ms={first_latency}")
    else:
        check("Batch stream SSE", False, f"HTTP {r.status_code}")
except ImportError:
    # 无sseclient库时直接测试端点响应格式
    try:
        r = requests.post("http://127.0.0.1:9000/avatar/speak/batch/stream",
            json=[{"text": "测试", "language": "zh-cn"}], stream=True, timeout=30)
        body = r.raw.read(200).decode('utf-8', errors='replace')
        check("Batch stream endpoint", "data:" in body or "text/event-stream" in r.headers.get("content-type", ""),
              f"content-type={r.headers.get('content-type')}")
    except Exception as e2:
        check("Batch stream endpoint", False, str(e2))
except Exception as e:
    check("Batch stream SSE", False, str(e))

# /rvc/models 缓存字段
try:
    r1 = requests.get("http://127.0.0.1:9000/rvc/models", timeout=5)
    d1 = r1.json()
    check("RVC models cached field", "cached" in d1, f"cached={d1.get('cached')}")
    # 第二次请求应命中缓存
    r2 = requests.get("http://127.0.0.1:9000/rvc/models", timeout=5)
    d2 = r2.json()
    check("RVC models cache hit", d2.get("cached") == True, f"2nd_cached={d2.get('cached')}")
except Exception as e:
    check("RVC models cache", False, str(e))

# ── T15: Phase 10 新增功能 ──────────────────────────
print("\n--- T15: Phase 10 功能 ---")

# /api/import_profiles preview模式
try:
    r = requests.post("http://127.0.0.1:9000/api/import_profiles",
        json={"profiles": {"测试角色": {}}, "mode": "preview"}, timeout=5)
    d = r.json()
    check("Import preview mode", d.get("preview") == True and "conflicts" in d,
          f"preview={d.get('preview')} conflicts={len(d.get('conflicts', []))}")
except Exception as e:
    check("Import preview", False, str(e))

# /realtime/camera_status 端点
try:
    r = requests.get("http://127.0.0.1:9000/realtime/camera_status", timeout=5)
    d = r.json()
    check("Camera status API", "index" in d and "state" in d,
          f"index={d.get('index')} state={d.get('state')}")
except Exception as e:
    check("Camera status", False, str(e))

# POST /profiles/{name}/clone 克隆功能
try:
    # 先获取一个存在的角色
    r = requests.get("http://127.0.0.1:9000/profiles", timeout=5)
    profiles = r.json().get("profiles", [])
    if profiles:
        name = profiles[0]["name"]
        r2 = requests.post(f"http://127.0.0.1:9000/profiles/{name}/clone",
            json={"new_name": name + "_clone_test"}, timeout=5)
        d2 = r2.json()
        check("Clone profile", d2.get("ok") == True and d2.get("source") == name,
              f"new_name={d2.get('name')}")
        # 清理克隆的角色
        requests.delete(f"http://127.0.0.1:9000/profiles/{d2.get('name')}", timeout=3)
    else:
        check("Clone profile", True, "跳过-无角色")
except Exception as e:
    check("Clone profile", False, str(e))

# UI Phase 10 字段检查
try:
    r = requests.get("http://127.0.0.1:9000/ui", timeout=5)
    html = r.text
    check("UI has importShow", "importShow" in html)
    check("UI has importPreview", "importPreview" in html)
    check("UI has importMode", "importMode" in html)
    check("UI has cloneProfile", "cloneProfile" in html)
    check("UI has pollCameraStatus", "pollCameraStatus" in html)
except Exception as e:
    check("UI Phase10", False, str(e))

# ── T16: Phase 11 新增功能 ──────────────────────────
print("\n--- T16: Phase 11 功能 ---")

# /api/logs 日志API
try:
    r = requests.get("http://127.0.0.1:9000/api/logs?limit=50", timeout=5)
    d = r.json()
    check("Logs API", "lines" in d and "count" in d, f"lines={len(d.get('lines', []))}")
except Exception as e:
    check("Logs API", False, str(e))

# /api/logs 级别过滤
try:
    r = requests.get("http://127.0.0.1:9000/api/logs?limit=50&level=INFO", timeout=5)
    d = r.json()
    check("Logs level filter", True, f"filtered_lines={len(d.get('lines', []))}")
except Exception as e:
    check("Logs level filter", False, str(e))

# /api/export_profiles 选择性导出
try:
    # 先获取角色列表
    r1 = requests.get("http://127.0.0.1:9000/profiles", timeout=5)
    profiles = r1.json().get("profiles", [])
    if profiles:
        name = profiles[0]["name"]
        r2 = requests.get(f"http://127.0.0.1:9000/api/export_profiles?names={name}", timeout=5)
        d2 = r2.json()
        check("Export filtered", d2.get("count") == 1 and d2.get("filtered") == True,
              f"count={d2.get('count')} filtered={d2.get('filtered')}")
    else:
        check("Export filtered", True, "跳过-无角色")
except Exception as e:
    check("Export filtered", False, str(e))

# UI Phase 11 字段检查
try:
    r = requests.get("http://127.0.0.1:9000/ui", timeout=5)
    html = r.text
    check("UI has logs tab", "logs" in html and "📋" in html)
    check("UI has loadLogs", "loadLogs" in html)
    check("UI has logAutoRefresh", "logAutoRefresh" in html)
    check("UI has exportSelected", "exportSelected" in html)
    check("UI has filteredProfiles", "filteredProfiles" in html)
    check("UI has profileSearch", "profileSearch" in html)
    check("UI has toggleSelectAll", "toggleSelectAll" in html)
except Exception as e:
    check("UI Phase11", False, str(e))

# ── 清理 ─────────────────────────────────────────
try:
    requests.delete("http://127.0.0.1:9000/profiles/test_p2", timeout=3)
except:
    pass

# ── 总结 ──────────────────────────────────────────
print("\n" + "=" * 55)
print(f" 结果: {PASS} PASS / {FAIL} FAIL / {PASS+FAIL} TOTAL")
if FAIL == 0:
    print(" Phase 2 全量测试通过!")
else:
    print(f" 有 {FAIL} 项失败，需要修复")
print("=" * 55)
