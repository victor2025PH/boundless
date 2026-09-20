# -*- coding: utf-8 -*-
"""Phase 1 全量测试"""
import sys, time, base64, json, requests
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
print(" Phase 1 全量测试")
print("=" * 55)

# ── T1: 健康检查 ──────────────────────────────────
print("\n--- T1: 服务健康检查 ---")
for name, port in [("faceswap",8000),("tts",7851),("avatarhub",9000)]:
    try:
        r = requests.get(f"http://127.0.0.1:{port}/health", timeout=5)
        check(f"{name}(:{ port})", r.status_code == 200, f"status={r.status_code}")
    except Exception as e:
        check(f"{name}(:{port})", False, str(e))

# ── T2: TTS 直接调用 ─────────────────────────────
print("\n--- T2: TTS 直接调用(XTTS-v2) ---")
try:
    r = requests.get("http://127.0.0.1:7851/health", timeout=5)
    d = r.json()
    check("TTS engine=xtts_v2", d.get("engine") == "xtts_v2_local",
          f"engine={d.get('engine')}")
    check("TTS model_loaded", d.get("model_loaded") == True)
except Exception as e:
    check("TTS health", False, str(e))

try:
    r = requests.post("http://127.0.0.1:7851/v1/audio/speech",
        json={"model":"xtts_v2","input":"测试语音","voice":"female_01.wav",
              "language":"zh-cn"}, timeout=60)
    check("TTS synthesize", r.status_code == 200,
          f"status={r.status_code} size={len(r.content)}bytes")
    check("TTS returns WAV", r.content[:4] == b'RIFF', "WAV header OK")
except Exception as e:
    check("TTS synthesize", False, str(e))

# ── T3: Voices 列表 ──────────────────────────────
print("\n--- T3: 声音列表 ---")
try:
    r = requests.get("http://127.0.0.1:7851/voices", timeout=5)
    voices = r.json().get("voices", [])
    check("Voices list", len(voices) > 5, f"count={len(voices)}")
    check("female_01.wav in voices", "female_01.wav" in voices)
except Exception as e:
    check("Voices", False, str(e))

# ── T4: AvatarHub UI (前后端分离) ─────────────────
print("\n--- T4: AvatarHub UI ---")
try:
    r = requests.get("http://127.0.0.1:9000/ui", timeout=5)
    check("UI loads", r.status_code == 200, f"size={len(r.text)}chars")
    check("UI from file", "window.location.origin" in r.text,
          "动态HUB URL")
    check("UI has profiles", "loadProfiles" in r.text)
except Exception as e:
    check("UI", False, str(e))

# ── T5: 角色管理 ──────────────────────────────────
print("\n--- T5: 角色管理 ---")
try:
    r = requests.get("http://127.0.0.1:9000/profiles", timeout=5)
    profiles = r.json().get("profiles", [])
    check("Profiles loaded", len(profiles) >= 2, f"count={len(profiles)}")
except Exception as e:
    check("Profiles", False, str(e))

# 创建测试角色
try:
    r = requests.post("http://127.0.0.1:9000/profiles",
        json={"name":"test_phase1","description":"测试角色","voice_name":"female_01.wav"},
        timeout=5)
    check("Create profile", r.json().get("ok") == True)
except Exception as e:
    check("Create profile", False, str(e))

# 激活角色
try:
    r = requests.post("http://127.0.0.1:9000/profiles/test_phase1/activate",
        timeout=5)
    check("Activate profile", r.json().get("ok") == True)
except Exception as e:
    check("Activate profile", False, str(e))

# ── T6: 声脸联动 ─────────────────────────────────
print("\n--- T6: 声脸联动(avatar/speak) ---")
try:
    t0 = time.time()
    r = requests.post("http://127.0.0.1:9000/avatar/speak",
        json={"text":"你好，我是AI助手，Phase1测试","language":"zh-cn",
              "profile":"test_phase1"}, timeout=60)
    elapsed = int((time.time() - t0) * 1000)
    d = r.json()
    has_audio = bool(d.get("audio_base64", ""))
    check("Speak API 200", r.status_code == 200)
    check("Has audio", has_audio, f"audio_size={len(d.get('audio_base64',''))}chars")
    check("Elapsed reasonable", elapsed < 30000, f"{elapsed}ms")
    # 验证音频是 WAV
    if has_audio:
        audio_bytes = base64.b64decode(d["audio_base64"])
        check("Audio is WAV", audio_bytes[:4] == b'RIFF',
              f"size={len(audio_bytes)}bytes")
except Exception as e:
    check("Speak API", False, str(e))

# ── T7: 清理测试角色 ──────────────────────────────
print("\n--- T7: 清理 ---")
try:
    r = requests.delete("http://127.0.0.1:9000/profiles/test_phase1", timeout=5)
    check("Delete test profile", r.json().get("ok") == True)
except Exception as e:
    check("Delete", False, str(e))

# ── T8: service_manager status ────────────────────
print("\n--- T8: env_config.bat 存在 ---")
from pathlib import Path
check("env_config.bat exists", Path(r"C:\模仿音色\env_config.bat").exists())
check("static/ui.html exists", Path(r"C:\模仿音色\static\ui.html").exists())
check("service_manager.py exists", Path(r"C:\模仿音色\service_manager.py").exists())

# ── 总结 ──────────────────────────────────────────
print("\n" + "=" * 55)
print(f" 结果: {PASS} PASS / {FAIL} FAIL / {PASS+FAIL} TOTAL")
if FAIL == 0:
    print(" Phase 1 全量测试通过!")
else:
    print(f" 有 {FAIL} 项失败，需要修复")
print("=" * 55)
