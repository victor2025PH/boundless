# -*- coding: utf-8 -*-
"""
Telegram 视频通话一体模式 · Python 编排器
─────────────────────────────────────────────────────────────────────────
视频：摄像头 -> faceswap_api(8000) -> realtime_stream -> OBS 虚拟摄像头
音频：你说中文 -> 同传方向A(译英+克隆音) -> VB-Cable 虚拟麦 -> 对方听到
字幕：对方说话 -> 同传方向B(译中) -> 桌面置顶悬浮字幕窗

为什么用 Python 而非 .bat：cmd 批处理对 UTF-8 中文多字节解析脆弱，易错位丢字符；
Python 处理 UTF-8 稳定、可写日志、健康探测/清场逻辑更可靠。环境路径由 env_config.bat
经 start_telegram_mode.bat 注入（os.environ）。

用法（一般经 start_telegram_mode.bat 启动）：
    python start_telegram_mode.py [摄像头序号]   默认 0
    python start_telegram_mode.py --source scrcpy   用手机(scrcpy)画面
"""
import os
import sys
import time
import json
import socket
import argparse
import subprocess
import webbrowser
from urllib.parse import quote

try:                                   # 控制台默认 GBK，无法输出 ✓ 等字符 → 统一切 UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import requests
except Exception:
    requests = None

BASE = os.path.dirname(os.path.abspath(__file__))
LOGDIR = os.path.join(BASE, "logs")
os.makedirs(LOGDIR, exist_ok=True)

PY = os.environ.get("FACEFUSION_PY") or sys.executable
FISH_PY = os.environ.get("FISHSPEECH_PY", PY)
COSY_PY = os.environ.get("COSYTTS_PY", PY)

SVC_FISH = os.environ.get("SVC_FISH_TTS")
SVC_STT = os.environ.get("SVC_STT")
SVC_FACESWAP = os.environ.get("SVC_FACESWAP")

DETACHED = 0x00000008 | 0x00000200          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


# ── 工具 ──────────────────────────────────────────────────────────────────
def info(msg):
    print(msg, flush=True)


def http_ok(url, timeout=2.0):
    try:
        if requests is not None:
            return requests.get(url, timeout=timeout).status_code == 200
        import urllib.request
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def port_listening(port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def kill_pids_on_ports(ports):
    """按端口找 PID 并连子进程一起结束（清场，确保重复运行干净）。"""
    pids = set()
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             timeout=10).stdout
        for line in out.splitlines():
            if "LISTENING" not in line:
                continue
            parts = line.split()
            if len(parts) < 5:
                continue
            local = parts[1]
            try:
                p = int(local.rsplit(":", 1)[1])
            except Exception:
                continue
            if p in ports:
                pids.add(parts[-1])
    except Exception:
        pass
    for pid in pids:
        if pid and pid != "0":
            subprocess.run(["taskkill", "/F", "/T", "/PID", pid],
                           capture_output=True, text=True)
    return pids


def kill_by_script(names):
    """按命令行匹配脚本名结束 python 进程（覆盖不占端口的 realtime_stream/overlay）。"""
    pat = "|".join(names)
    ps = (
        "Get-CimInstance Win32_Process -Filter \"name='python.exe'\" | "
        "Where-Object { $_.CommandLine -and ($_.CommandLine -match '%s') } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        % pat
    )
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                       capture_output=True, text=True, timeout=20)
    except Exception:
        pass


def launch(name, pyexe, script, args=None):
    """后台启动一个服务，输出重定向到 logs/tg_<name>.log，进程独立存活。"""
    args = args or []
    logpath = os.path.join(LOGDIR, f"tg_{name}.log")
    logf = open(logpath, "ab")
    cmd = [pyexe, script] + args
    p = subprocess.Popen(cmd, cwd=BASE, stdout=logf, stderr=subprocess.STDOUT,
                         creationflags=DETACHED)
    info(f"    启动 {name}  (PID {p.pid}, 日志 logs/tg_{name}.log)")
    return p


def launch_gui(name, pyexe, script, args=None):
    """启动 GUI（悬浮窗），不重定向，让 Tk 窗口正常显示。"""
    args = args or []
    p = subprocess.Popen([pyexe, script] + args, cwd=BASE, creationflags=DETACHED)
    info(f"    启动 {name}  (PID {p.pid})")
    return p


def wait_health(url, label, tries, interval):
    for i in range(tries):
        if http_ok(url):
            info(f"    {label} 就绪 ✓")
            return True
        time.sleep(interval)
    info(f"    [警告] {label} 未在 {int(tries*interval)}s 内就绪（见对应 tg_*.log）")
    return False


def _scrcpy_window_present():
    """是否存在手机镜像窗口(PhoneCam/scrcpy)。"""
    try:
        import ctypes
        u32 = ctypes.windll.user32
        return bool(u32.FindWindowW(None, "PhoneCam") or u32.FindWindowW(None, "scrcpy"))
    except Exception:
        return False


def _saved_cam_index():
    """读 realtime_stream 持久化的 camera_index.txt（若有），用于显式带出，避免隐式覆盖。"""
    f = os.path.join(BASE, "camera_index.txt")
    try:
        if os.path.exists(f):
            s = open(f, encoding="utf-8").read().strip()
            if s:
                return s
    except Exception:
        pass
    return None


def _detect_video_source(requested, wait_scrcpy=4.0):
    """决定画面源并【显式】返回，避免把 '0' 丢给 realtime 后被陈旧 camera_index.txt 静默劫持。
    - 用户显式给了非 0 值(摄像头序号/scrcpy/URL) → 尊重。
    - 默认('0'/未指定)：本项目真实画面在手机里(本机摄像头多为空白占位)，故优先手机镜像——
      短暂轮询等 scrcpy/PhoneCam 窗口(可能刚启动还没出来)；仍无则回退到已保存的摄像头索引
      (camera_index.txt)并显式带出(透明可见)，最后才用 0。
    返回 (source, reason)。"""
    req = "" if requested is None else str(requested)
    if req not in ("0", "", "auto", "default"):
        return req, "用户指定"
    deadline = time.time() + max(0.0, wait_scrcpy)
    while True:
        if _scrcpy_window_present():
            return "scrcpy", "检测到手机镜像窗口 PhoneCam/scrcpy"
        if time.time() >= deadline:
            break
        time.sleep(0.5)
    saved = _saved_cam_index()
    if saved is not None:
        return saved, f"未见手机镜像；用已保存摄像头索引 camera_index.txt={saved}（如要用手机请先开 scrcpy）"
    return "0", "未见手机镜像且无保存索引；用摄像头 0"


def _get_profiles():
    if requests is None:
        return None
    try:
        return requests.get("http://127.0.0.1:9000/profiles", timeout=6).json()
    except Exception:
        return None


def ensure_active_face(wait_s=12):
    """守护"换脸有源脸"。
    Hub 自身会在启动后自动重激活上次角色（读 active_profile.txt，约 sleep 3s+等子服务），
    所以这里先轮询给它留出时间：窗口内一旦出现"激活且带脸"的角色就采纳（多为上次角色，
    尊重 Hub 的选择，最小干预）；若窗口结束仍没有，则兜底自动激活一个带脸角色
    （优先沿用 active_profile.txt 记录的上次角色，否则取第一个带脸角色）。
    返回 (status, name)，status ∈ {ok, activated, noface, err}。"""
    if requests is None:
        return "err", None
    faced = []
    deadline = time.time() + wait_s
    while time.time() < deadline:
        j = _get_profiles()
        if j is None:
            time.sleep(1.0)
            continue
        act = j.get("active") or ""
        faced = [p.get("name") for p in j.get("profiles", []) if p.get("has_face")]
        for p in j.get("profiles", []):
            if p.get("name") == act and p.get("has_face"):
                return "ok", act          # Hub 已激活且带脸 → 直接采纳
        time.sleep(1.0)
    if not faced:
        return "noface", None             # 一个带脸角色都没有，无法兜底
    target = None
    try:                                  # 优先沿用上次激活的角色（若它带脸）
        f = os.path.join(BASE, "active_profile.txt")
        if os.path.exists(f):
            last = open(f, encoding="utf-8").read().strip()
            if last in faced:
                target = last
    except Exception:
        target = None
    if not target:
        target = faced[0]
    try:
        requests.post(f"http://127.0.0.1:9000/profiles/{quote(target)}/activate",
                      timeout=20)
        return "activated", target
    except Exception:
        return "err", target


def _pick_loopbacks():
    """选「立体声混音/Stereo Mix」输入设备；host api 偏好 MME>WASAPI>DirectSound，
    排除 WDM-KS（回调式采集在 WDM-KS 上会 -9999 打不开）。返回 [(idx, hostapi, name), ...]。"""
    try:
        import sounddevice as sd
    except Exception:
        return []
    pref = {"MME": 0, "Windows WASAPI": 1, "Windows DirectSound": 2}
    try:
        ha = sd.query_hostapis()
        cands = []
        for i, d in enumerate(sd.query_devices()):
            nm = d["name"]
            if d["max_input_channels"] > 0 and ("立体声混音" in nm or "stereo mix" in nm.lower()):
                hn = ha[d["hostapi"]]["name"]
                if hn in pref:
                    cands.append((pref[hn], i, hn, nm))
        cands.sort()
        return [(i, hn, nm) for _, i, hn, nm in cands]
    except Exception:
        return []


def drive_call_start(base="http://127.0.0.1:7900"):
    """以通话模式启动同传：译英换声→VB-Cable + 对方声→字幕（经 /call_mode/start 自检+绑设备）。"""
    if requests is None:
        return False
    try:
        r = requests.post(base + "/call_mode/start",
                          json={"profile": "", "mode": "local"}, timeout=90)
        report = r.json()
        for step in report.get("steps", []):
            icon = "✓" if step.get("ok") else "✗"
            detail = (step.get("detail") or "")[:80]
            info(f"    {icon} {step.get('name')}" + (f" — {detail}" if detail else ""))
        if report.get("ready"):
            info("    通话模式就绪 ✓（你说中文→对方听英文克隆音；对方说话→字幕）")
            return True
        info("    [警告] 通话模式有未通过项，见 http://127.0.0.1:7900/call_mode/status")
        return False
    except Exception as e:
        info(f"    启动通话模式失败：{e}")
        return False


def _has_vbcable():
    """检测 VB-CABLE 虚拟声卡（需同时有输出端 CABLE Input 与输入端 CABLE Output）。"""
    try:
        import sounddevice as sd
    except Exception:
        return None, "sounddevice 不可用"
    outs = ins = 0
    for d in sd.query_devices():
        up = d["name"].upper()
        if "CABLE" in up or "VB-AUDIO" in up:
            if d["max_output_channels"] > 0:
                outs += 1
            if d["max_input_channels"] > 0:
                ins += 1
    return (outs > 0 and ins > 0), f"输出端={outs} 输入端={ins}"


def _has_obs_vcam():
    """查注册表确认 OBS Virtual Camera 的 DirectShow 滤镜 CLSID 是否注册。"""
    try:
        import winreg
        sub = r"SOFTWARE\Classes\CLSID\{A3FCE0F5-3493-419F-958A-ABA1250EC20B}"
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                winreg.OpenKey(hive, sub).Close()
                return True
            except OSError:
                continue
        return False
    except Exception:
        return None


def tg_preflight():
    """Telegram 模式预检：上摄像头前一键确认所有可自动检查的就绪项。只读，不启动任何服务。"""
    rows = []

    def add(state, name, detail=""):
        rows.append((state, name, detail))

    cfg = {}
    if requests is not None:
        try:
            cfg = (requests.get("http://127.0.0.1:9000/api/config", timeout=5)
                   .json().get("services") or {})
        except Exception:
            cfg = {}

    # 1 OBS 虚拟摄像头(视频输出口)
    v = _has_obs_vcam()
    add("ok" if v else ("warn" if v is None else "fail"), "OBS Virtual Camera 已安装",
        "" if v else "未注册；装 OBS 并手动点一次 Start Virtual Camera 以注册该设备")
    # 2 VB-CABLE 虚拟声卡(配音输出/麦克风)
    vb, vbd = _has_vbcable()
    add("ok" if vb else ("warn" if vb is None else "fail"), "VB-CABLE 虚拟声卡", vbd)
    # 3 立体声混音(对方声采集)
    lps = _pick_loopbacks()
    add("ok" if lps else "fail", "立体声混音(对方声采集)",
        (f"{lps[0][1]} {lps[0][2]}" if lps else "未启用：声音设置→录制→启用「立体声混音」"))
    # 4 各服务健康（远端地址以 Hub 实解析为准，避免环境差异误判）
    for nm, url in [("Hub", "http://127.0.0.1:9000/health"),
                    ("Fish克隆音", (cfg.get("fish_tts") or SVC_FISH or "http://127.0.0.1:7855") + "/health"),
                    ("同传", "http://127.0.0.1:7900/health"),
                    ("远端STT", (cfg.get("stt") or SVC_STT or "http://127.0.0.1:7854") + "/health"),
                    ("远端换脸", (cfg.get("faceswap") or SVC_FACESWAP or "http://127.0.0.1:8000") + "/health")]:
        add("ok" if http_ok(url, 4) else "fail", f"服务 {nm}", url)
    # 5 换脸代理上游可达（防 hub_config 把 faceswap 钉死 localhost 的回归）
    fs = cfg.get("faceswap")
    if fs:
        ok = http_ok(fs + "/health", 4)
        add("ok" if ok else "fail", "Hub 换脸代理上游", f"{fs} {'可达' if ok else '不可达(会 502)'}")
    else:
        add("warn", "Hub 换脸代理上游", "无法读取 /api/config")
    # 6 激活角色带脸（换脸源脸）
    j = _get_profiles()
    if j is None:
        add("warn", "激活角色带脸", "Hub 未就绪，无法确认")
    else:
        act = j.get("active") or ""
        hasf = any(p.get("name") == act and p.get("has_face") for p in j.get("profiles", []))
        add("ok" if (act and hasf) else "fail", "激活角色带脸",
            f"当前=「{act}」" + ("" if hasf else "  无脸/未激活 → 去 9000 激活一个带脸角色"))
    # 7 同传对方声采集无错
    if requests is not None:
        try:
            st = requests.get("http://127.0.0.1:7900/status", timeout=5).json()
            cb = st.get("cap_b_err")
            add("ok" if (st.get("running") and not cb) else "warn", "同传对方声采集",
                ("运行中" if st.get("running") else "未在通话") + (f"  错误:{cb}" if cb else ""))
        except Exception:
            add("warn", "同传对方声采集", "无法读取 /status")
    # 8 画面源（提醒先开 scrcpy）
    src, reason = _detect_video_source("0", wait_scrcpy=0.5)
    add("ok" if src == "scrcpy" else "warn", "画面源", f"{src}（{reason}）")

    icon = {"ok": "[ OK ]", "warn": "[警告]", "fail": "[缺失]"}
    nfail = sum(1 for r in rows if r[0] == "fail")
    nwarn = sum(1 for r in rows if r[0] == "warn")
    info("=" * 54)
    info(" Telegram 模式 · 上摄像头前预检（只读，不启动服务）")
    info("=" * 54)
    for state, name, detail in rows:
        info(f"  {icon[state]} {name}" + (f" — {detail}" if detail else ""))
    info("=" * 54)
    if nfail == 0:
        info(f" 结论：核心就绪 ✓（{nwarn} 项提示）。开 scrcpy 对着脸，Telegram 预览即见换脸画面。")
    else:
        info(f" 结论：{nfail} 项缺失需先处理（见上 [缺失]），{nwarn} 项提示。")
    return nfail == 0


# ── 主流程 ─────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Telegram 视频通话一体模式编排器")
    ap.add_argument("source", nargs="?", default="0",
                    help="摄像头序号（默认 0）或 scrcpy")
    ap.add_argument("--source", dest="source_opt", default=None,
                    help="同上，--source 0 / --source scrcpy")
    ap.add_argument("--width", default=None)
    ap.add_argument("--height", default=None)
    ap.add_argument("--aspect", default="portrait916",
                    help="输出比例: portrait916(手机9:16,默认)/portrait34/landscape169/landscape169hd/square11")
    ap.add_argument("--no-overlay", action="store_true", help="不启动悬浮字幕窗")
    ap.add_argument("--check", action="store_true",
                    help="只做上摄像头前预检（红绿清单），不启动任何服务")
    args = ap.parse_args()
    if args.check:
        ok = tg_preflight()
        sys.exit(0 if ok else 1)
    source, src_reason = _detect_video_source(args.source_opt or args.source)
    # 默认 portrait916(720×1280)：手机/Telegram 竖屏全屏比例；可在控制台 http://IP:8080 热切。
    aspect = (args.aspect or "portrait916").strip()
    _aspect_map = {
        "portrait916": (720, 1280), "portrait34": (720, 960),
        "landscape169": (1280, 720), "landscape169hd": (1920, 1080), "square11": (720, 720),
    }
    if args.width and args.height:
        width, height = str(args.width), str(args.height)
        aspect_arg = ""
    elif aspect in _aspect_map:
        w, h = _aspect_map[aspect]
        width, height = str(w), str(h)
        aspect_arg = aspect
    else:
        width, height = "720", "1280"
        aspect_arg = "portrait916"

    info("=" * 48)
    info(" Telegram 视频通话模式：换脸 + 翻译换声 + 对方字幕")
    info("=" * 48)
    info(f"  python(facefusion) = {PY}")
    if source == "scrcpy":
        info(f"  画面源 = scrcpy 手机镜像（{src_reason}）   输出 = {width}x{height} ({aspect_arg or '自定义'})")
    else:
        info(f"  画面源 = 摄像头 {source}（{src_reason}）   输出 = {width}x{height} ({aspect_arg or '自定义'})")

    info("\n[清理] 停止旧的相关服务（端口 + 脚本匹配）...")
    kill_pids_on_ports({7855, 7854, 9000, 8000, 7900})
    kill_by_script(["faceswap_api", "stt_server", "fish_speech_server",
                    "avatar_hub", "live_interpreter", "realtime_stream",
                    "subtitle_overlay"])
    time.sleep(1.5)

    info("\n前置条件：OBS Virtual Camera 已装(勿在OBS里自行启动虚拟摄像头) · "
         "VB-CABLE 已装 · 录音设备已启用「立体声混音」 · Hub 有带克隆音色的激活角色。")

    # 1) 核心：Fish / STT / Hub
    info("\n[1/5] 核心服务 Fish(克隆音TTS) / STT / Hub...")
    if SVC_FISH:
        info(f"    克隆音 Fish 走远端 {SVC_FISH}（跳过本地）")
    else:
        launch("fish", FISH_PY, os.path.join(BASE, "fish_speech_server.py"))
    if SVC_STT:
        info(f"    STT 走远端 {SVC_STT}（跳过本地）")
    else:
        launch("stt", COSY_PY, os.path.join(BASE, "stt_server.py"))
    launch("hub", PY, os.path.join(BASE, "avatar_hub.py"))

    # 2) 换脸引擎
    info("\n[2/5] 换脸引擎 FaceSwap-API（端口 8000，CUDA）...")
    if SVC_FACESWAP:
        info(f"    换脸走远端 {SVC_FACESWAP}（经 Hub 代理自动注入激活角色脸）")
    else:
        launch("faceswap", PY, os.path.join(BASE, "faceswap_api.py"))
        info("    等待换脸引擎加载模型（最多 ~120 秒）...")
        wait_health("http://127.0.0.1:8000/health", "换脸引擎", tries=60, interval=2)
    # realtime 默认走 Hub 9000/faceswap 代理：每帧自动注入"激活角色"的源脸，再转发到引擎(本地/远端)。
    # 不要设 SWAP_API_URL 直连引擎——那样会绕过注脸、且依赖引擎已被推过脸(脆弱)。
    os.environ.pop("SWAP_API_URL", None)

    # 等 Hub 就绪 → 守护"激活角色带脸"：等 Hub 自动重激活落定，没带脸则兜底激活一个带脸角色，
    # 避免编排器抢跑误报、也避免画面只透传原图、不换脸。
    if wait_health("http://127.0.0.1:9000/health", "Hub", tries=30, interval=2):
        info("    等待 Hub 自动重激活上次角色并校验源脸...")
        st, name = ensure_active_face(wait_s=12)
        if st == "ok":
            info(f"    换脸源脸 = 激活角色「{name}」✓")
        elif st == "activated":
            info(f"    已自动激活带脸角色「{name}」作为换脸源脸 ✓")
        elif st == "noface":
            info("    [提示] Hub 所有角色都没有人脸——请在 9000 页面给角色上传一张脸，否则不会换脸。")
        else:
            info("    [提示] 暂时无法确认激活角色（Hub 未就绪/网络）——换脸可能只透传原图。")

    # 3) 实时换脸推流：摄像头/手机 -> OBS 虚拟摄像头
    info(f"\n[3/5] 实时换脸推流（源 {source} → OBS 虚拟摄像头）...")
    rt_args = ["--source", source, "--width", width, "--height", height]
    if aspect_arg:
        rt_args += ["--aspect", aspect_arg]
    launch("realtime", PY, os.path.join(BASE, "realtime_stream.py"), rt_args)

    # 4) 实时同传（方向A 译音 + 方向B 字幕）
    info("\n[4/5] 实时同传 LingoX（端口 7900）...")
    launch("interp", PY, os.path.join(BASE, "live_interpreter.py"))
    if wait_health("http://127.0.0.1:7900/health", "同传服务", tries=30, interval=2):
        # 通话模式(仅字幕)：显式选稳定环回设备启动，避免 ?go=1 误选 WDM-KS 导致对方声采集失败
        info("    以通话模式启动同传（我方译英换声 + 对方译中字幕）...")
        drive_call_start()
    try:
        webbrowser.open("http://127.0.0.1:7900/")          # 仅用于监看(页面轮询 /status)
    except Exception:
        pass

    # 5) 悬浮字幕窗
    if not args.no_overlay:
        info("\n[5/5] 桌面置顶悬浮字幕窗（对方中文翻译）...")
        launch_gui("overlay", PY, os.path.join(BASE, "subtitle_overlay.py"))

    info("\n" + "=" * 48)
    info(" 全部已拉起 —— Telegram Desktop 里这样设：")
    info("=" * 48)
    info("  摄像头：选 OBS Virtual Camera（官网桌面版 Telegram 走 DShow，可直接选到；换脸画面从这里出）")
    info("          若列表里没有：确认用的是官网桌面版(非应用商店沙盒版)，且 realtime_stream 正在喂 OBS。")
    info("  麦克风：选 CABLE Output (VB-Audio Virtual Cable)  ← 对方听到你的英文克隆音")
    info("  扬声器：保持真实耳机/音箱（这样才能抓到对方声做字幕）")
    info("  画面比例：Telegram 里若仍横屏，开 http://本机IP:8080 点「9:16竖屏」可热切")
    info("  悬浮窗：拖动移动 · Ctrl+滚轮缩放 · 双击我方回显 · F8 穿透 · Esc 关闭")
    info("  换脸的脸 = Hub(9000) 当前【激活角色】的脸；换脸请在 9000 页面切换角色")
    info(f"  画面源：本次用【{source}】。默认优先手机镜像(先开 scrcpy/PhoneCam 再启动)；")
    info("          指定本机摄像头则 start_telegram_mode.bat <索引>（如 1）。")
    info("  各服务日志在 logs/tg_*.log（换脸看 tg_realtime.log，对方声看 tg_interp.log）")
    info("=" * 48)
    info("\n各服务已作为独立进程在后台运行；本窗口可关闭。")


if __name__ == "__main__":
    main()
