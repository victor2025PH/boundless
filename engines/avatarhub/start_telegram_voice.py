# -*- coding: utf-8 -*-
"""
一键启动 Telegram 实时翻译克隆声（出向：手机麦→翻译→克隆声→Telegram麦克风）。

幂等：可重复运行。按设备名解析索引(避免 PortAudio 索引漂移)。

链路:
  手机麦 → scrcpy(--audio-source=mic) → Windows默认播放=Voicemeeter Input
        → VoiceMeeter VAIO条→B1 → live_interpreter采集(Voicemeeter Out B1)
        → STT中 → 翻译英 → 克隆声合成 → WASAPI CABLE Input(48000)
        → CABLE Output = Telegram 麦克风

用法: python start_telegram_voice.py [角色名]   (默认 阿龙)
"""
import sys, os, time, ctypes, subprocess
import requests, numpy as np, sounddevice as sd
# 控制台默认 GBK 编码,直接 print emoji/特殊符号会 UnicodeEncodeError(导致退出码非0误导)。
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass

BASE = r"C:\模仿音色"
PY_FF = r"C:\Users\user\Miniconda3\envs\facefusion\python.exe"
PY_STT = r"C:\Users\user\Miniconda3\envs\cosytts\python.exe"
SCRCPY = r"C:\模仿音色\scrcpy\scrcpy-win64-v3.1\scrcpy.exe"
VM_DLL = r"C:\Program Files (x86)\VB\Voicemeeter\VoicemeeterRemote64.dll"
NW = subprocess.CREATE_NO_WINDOW

PY_NEMO = r"C:\Users\user\Miniconda3\envs\nemoasr\python.exe"
PROFILE = sys.argv[1] if len(sys.argv) > 1 else "阿龙"
INTERP = "http://127.0.0.1:7900"
STT = "http://127.0.0.1:7854"
NEMO = "http://127.0.0.1:7857"          # Nemotron 流式 STT(逐词字幕)
STREAM = os.environ.get("TG_STREAM", "1") == "1"   # 默认开启逐词流式;TG_STREAM=0 关闭


def log(m): print(f"[启动] {m}", flush=True)


# ── 1. VoiceMeeter: 启引擎 + VAIO条(Strip[2])只送B1 ──────────────
def setup_voicemeeter():
    vm = ctypes.cdll.LoadLibrary(VM_DLL)
    vm.VBVMR_Login.restype = ctypes.c_long
    vm.VBVMR_RunVoicemeeter.argtypes = [ctypes.c_long]
    vm.VBVMR_SetParameterFloat.argtypes = [ctypes.c_char_p, ctypes.c_float]
    vm.VBVMR_GetParameterFloat.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_float)]
    if vm.VBVMR_Login() == 1:
        log("VoiceMeeter 引擎未运行，启动中…")
        vm.VBVMR_RunVoicemeeter(1)
        for _ in range(20):
            time.sleep(0.5)
            if vm.VBVMR_Login() == 0:
                break
    time.sleep(0.8)
    for k, v in {"Strip[2].B1": 1.0, "Strip[2].Mute": 0.0, "Strip[2].Gain": 0.0}.items():
        for _ in range(4):
            vm.VBVMR_SetParameterFloat(k.encode(), ctypes.c_float(v)); time.sleep(0.3)
    val = ctypes.c_float(0); vm.VBVMR_GetParameterFloat(b"Strip[2].B1", ctypes.byref(val))
    log(f"VoiceMeeter VAIO→B1 = {val.value} (应为1.0)")


# ── 2. 默认播放设备 = Voicemeeter Input ──────────────────────────
def set_default_playback():
    r = subprocess.run(["powershell", "-ExecutionPolicy", "Bypass", "-File",
                        rf"{BASE}\_set_default_audio.ps1", "-NameMatch", "Voicemeeter Input",
                        "-Kind", "Render"], capture_output=True, text=True, creationflags=NW)
    log("默认播放设备 → Voicemeeter Input: " + ("OK" if "rc=0" in r.stdout else r.stdout.strip()[-80:]))


# ── 3. 按名解析设备索引 ─────────────────────────────────────────
def find_dev(name_sub, host_sub, want_input):
    h = sd.query_hostapis()
    for i, d in enumerate(sd.query_devices()):
        if name_sub in d["name"] and host_sub in h[d["hostapi"]]["name"]:
            if want_input and d["max_input_channels"] > 0: return i
            if (not want_input) and d["max_output_channels"] > 0: return i
    return None


def resolve_devices():
    mic = find_dev("Voicemeeter Out B1", "MME", True)            # 手机麦(经B1)
    cable = find_dev("CABLE Input", "WASAPI", False)             # 克隆声出口(必须WASAPI 48000)
    # 对方声(direction B, 出字幕): 用 soundcard 环回 Realtek 扬声器(Telegram 输出到这里)。
    # 传输出设备 + is_output=True；interpreter 内部用 soundcard 按设备名做 WASAPI 环回。
    loop = find_dev("扬声器", "WASAPI", False) or find_dev("Speaker", "WASAPI", False) \
        or find_dev("扬声器", "MME", False)
    loop_is_out = loop is not None
    if loop is None:  # 兜底立体声混音
        loop = find_dev("立体声混音", "MME", True) or find_dev("Stereo Mix", "MME", True)
    log(f"设备解析: mic(B1)={mic}  cable(WASAPI)={cable}  loopback(Realtek扬声器环回)={loop} is_output={loop_is_out}")
    if mic is None or cable is None:
        raise RuntimeError("找不到 Voicemeeter Out B1 或 CABLE Input(WASAPI)，请检查 VoiceMeeter/VB-CABLE")
    return mic, cable, (loop if loop is not None else -1), loop_is_out


# ── 4. 确保 scrcpy 手机麦转发在跑 ───────────────────────────────
def _scrcpy_running():
    try:
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq scrcpy.exe"],
                             capture_output=True, text=True, creationflags=NW).stdout
        return "scrcpy.exe" in out
    except Exception:
        return False


def ensure_scrcpy_mic(mic_idx):
    if _scrcpy_running():
        log("scrcpy 进程已存在(供麦/供画面)，不重复启动"); return
    # 测 B1 是否有底噪流(说明 scrcpy 在供麦)。静默则启动音频专用 scrcpy。
    try:
        with sd.InputStream(device=mic_idx, channels=2, samplerate=44100) as st:
            d, _ = st.read(8000)
        if float(np.max(np.abs(d))) > 1e-4:
            log("检测到手机麦音频流(scrcpy已在供麦)"); return
    except Exception:
        pass
    log("未检测到手机麦流，启动音频专用 scrcpy(--audio-source=mic)…")
    subprocess.Popen([SCRCPY, "--no-video", "--audio-source=mic", "--audio-buffer=50"],
                     creationflags=subprocess.CREATE_NEW_CONSOLE)
    time.sleep(4)


# ── 5. 确保 STT + interpreter 服务在线 ──────────────────────────
def ensure_service(url, py, script, env_label, wait_s):
    try:
        if requests.get(url + "/health", timeout=2).ok:
            log(f"{env_label} 已在线"); return
    except Exception:
        pass
    log(f"启动 {env_label} …")
    logf = open(rf"{BASE}\logs\{script}.log", "a", encoding="utf-8", errors="replace")
    subprocess.Popen([py, rf"{BASE}\{script}"], cwd=BASE,
                     stdout=logf, stderr=logf,
                     creationflags=subprocess.CREATE_NO_WINDOW)
    for _ in range(wait_s):
        time.sleep(1)
        try:
            if requests.get(url + "/health", timeout=2).ok:
                log(f"{env_label} 就绪"); return
        except Exception:
            continue
    log(f"⚠ {env_label} 启动超时，请查日志")


def ensure_nemotron(wait_s=100):
    """启动 Nemotron 流式 STT(逐词字幕)，等待模型 loaded=True。失败仅告警(自动回退分段同传)。"""
    def _loaded():
        try:
            return bool(requests.get(NEMO + "/health", timeout=2).json().get("loaded"))
        except Exception:
            return None
    st = _loaded()
    if st is True:
        log("Nemotron 流式STT(7857) 已就绪"); return True
    if st is None:
        log("启动 Nemotron 流式STT(7857) …(首次加载约 60s)")
        logf = open(rf"{BASE}\logs\nemotron_stt.log", "a", encoding="utf-8", errors="replace")
        subprocess.Popen([PY_NEMO, rf"{BASE}\nemotron_stt_server.py"], cwd=BASE,
                         stdout=logf, stderr=logf, creationflags=subprocess.CREATE_NO_WINDOW)
    for _ in range(wait_s):
        time.sleep(1)
        if _loaded() is True:
            log("Nemotron 流式STT 就绪(逐词字幕可用)"); return True
    log("⚠ Nemotron 未就绪，将回退「整句分段」同传(不影响通话，仅非逐词)")
    return False


# ── 6. 启动同传会话 ─────────────────────────────────────────────
def start_session(mic, cable, loop, loop_is_out):
    try:
        requests.post(INTERP + "/stop", timeout=10)
    except Exception:
        pass
    body = {"mic_index": mic, "cable_index": cable, "loopback_index": loop,
            "loopback_is_output": loop_is_out, "profile": PROFILE, "mode": "local",
            "live_mode": False, "stream": STREAM}
    r = requests.post(INTERP + "/start", json=body, timeout=30)
    log(f"会话启动: {r.status_code} {r.json()}")


def main():
    log(f"角色={PROFILE}")
    setup_voicemeeter()
    set_default_playback()
    ensure_service(STT, PY_STT, "stt_server.py", "STT(7854)", 40)
    ensure_service(INTERP, PY_FF, "live_interpreter.py", "interpreter(7900)", 20)
    if STREAM:
        ensure_nemotron()       # 逐词流式;未就绪自动回退分段(非致命)
    mic, cable, loop, loop_is_out = resolve_devices()
    ensure_scrcpy_mic(mic)
    start_session(mic, cable, loop, loop_is_out)
    print("\n" + "=" * 56)
    print("✅ 出向克隆声链路已就绪")
    print("   Telegram 设置 → 高级 → 通话:")
    print("     麦克风 = CABLE Output (VB-Audio Virtual Cable)")
    print("     扬声器 = 你的耳机/音箱")
    print("=" * 56)


if __name__ == "__main__":
    main()
