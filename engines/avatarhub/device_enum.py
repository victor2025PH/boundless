# -*- coding: utf-8 -*-
"""
统一设备枚举与"手机/虚拟设备"识别（摄像头 + 麦克风）。

用途：让系统在不同手机接入方式下自动选对输入源——
  - 安卓手机：USB + ADB → 走 scrcpy（画质最好、延迟最低，现有路径）
  - 苹果/其它：装 iVCam / Camo / DroidCam(iOS) / EpocCam 等 app
    → 它们在 Windows 上注册成"虚拟摄像头 + 虚拟麦克风" → 自动按名识别并选用

设计要点：
  1. 摄像头用 pygrabber 取 DirectShow 设备名，其顺序与 OpenCV CAP_DSHOW 索引一致，
     因此能把"名字 ↔ 索引"一一对应（OpenCV 本身拿不到名字）。
  2. 必须排除我们自己的输出回环设备（OBS Virtual Camera / VB-Cable），
     否则把输出当输入会造成自反馈死循环。
  3. 活帧探测：候选摄像头先开一下读帧，非黑才采用，避免误选"空闲占位"的虚拟源。
  4. 决策完全只读、无副作用，可被 hub 安全调用。

依赖：pygrabber（pip install pygrabber）。缺失时摄像头按名枚举退化为空列表，
      决策自动回退到 scrcpy，不影响安卓现有路径。
"""
from __future__ import annotations

import json
import os
import time
import urllib.request

# monitor_relay 手机摄像头 MJPEG 源（WiFi 浏览器推流，无需 DroidCam/scrcpy）
MONITOR_BASE = os.environ.get("MONITOR_URL", "http://127.0.0.1:7878").rstrip("/")
MONITOR_CAM_URL = os.environ.get("MONITOR_CAM_URL", f"{MONITOR_BASE}/cam.mjpeg")

# ── 名称特征库 ────────────────────────────────────────────────────────
# "手机/虚拟摄像头" app（把手机摄像头变成 Windows 摄像头）
PHONE_CAM_PATTERNS = [
    "droidcam", "ivcam", "e2esoft", "camo", "reincubate", "epoccam",
    "kinoni", "iriun", "iphone", "ios", "phone", "mobile",
    "ip camera", "ip webcam",
]
# 绝不能当输入的"输出/回环"摄像头（我们自己的 vcam 输出）
CAM_EXCLUDE_PATTERNS = [
    "obs virtual camera", "obs-camera", "obs virtualcam", "unity video capture",
]

# "手机/虚拟麦克风" app
PHONE_MIC_PATTERNS = [
    "droidcam", "ivcam", "e2esoft", "camo", "epoccam", "wo mic", "womic",
    "iphone", "ios", "phone",
]
# 绝不能当输入的"输出/回环/混音"音频设备
MIC_EXCLUDE_PATTERNS = [
    "cable", "vb-audio", "vb audio", "voicemeeter", "obs", "stereo mix",
    "立体声混音", "线路", "loopback", "what u hear",
]


def _match(name: str, patterns: list[str]) -> bool:
    n = (name or "").lower()
    return any(p in n for p in patterns)


# ── 摄像头 ────────────────────────────────────────────────────────────
def list_named_cameras() -> list[dict]:
    """返回 [{index, name}]，索引与 OpenCV CAP_DSHOW 一致。无 pygrabber 时返回 []。"""
    try:
        from pygrabber.dshow_graph import FilterGraph
        devs = FilterGraph().get_input_devices()
        return [{"index": i, "name": n} for i, n in enumerate(devs)]
    except Exception:
        return []


def classify_camera(name: str) -> str:
    """output=我们的输出回环(排除) / phone=手机虚拟摄像头 / other=普通摄像头"""
    if _match(name, CAM_EXCLUDE_PATTERNS):
        return "output"
    if _match(name, PHONE_CAM_PATTERNS):
        return "phone"
    return "other"


def _probe_live(index: int, tries: int = 5, min_mean: float = 5.0) -> bool:
    """打开摄像头读几帧，确认能产生非黑画面（区分"在streaming"和"空闲占位"）。"""
    try:
        import cv2
        try:                       # 探测打不开的索引会刷 DSHOW C++ 警告(cap.cpp:459/480)，这里静默(保留 ERROR)
            cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
        except Exception:
            pass
        cap = cv2.VideoCapture(int(index), cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap.release()
            return False
        ok = False
        for _ in range(tries):
            ret, frame = cap.read()
            if ret and frame is not None and float(frame.mean()) >= min_mean:
                ok = True
                break
            time.sleep(0.05)
        cap.release()
        return ok
    except Exception:
        return False


def annotate_cameras(probe: bool = False) -> list[dict]:
    """返回带分类(+可选活帧)的摄像头列表，供 UI/诊断展示。"""
    out = []
    for c in list_named_cameras():
        kind = classify_camera(c["name"])
        item = {"index": c["index"], "name": c["name"], "kind": kind}
        if probe and kind != "output":
            item["live"] = _probe_live(c["index"])
        out.append(item)
    return out


def check_monitor_phone_cam(timeout: float = 1.2) -> dict:
    """查询 monitor_relay 手机摄像头是否在线(浏览器 WebRTC/WS 推流)。"""
    out = {"live": False, "url": MONITOR_CAM_URL, "status": {}}
    try:
        req = urllib.request.Request(f"{MONITOR_BASE}/cam/status")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
        st = d.get("status") or {}
        out["status"] = st
        out["url"] = (d.get("url_local") or MONITOR_CAM_URL).strip()
        out["live"] = bool(st.get("connected")) or (
            int(st.get("peers") or 0) > 0 and int(st.get("frames") or 0) > 0)
        if not out["live"] and float(st.get("fps") or 0) > 0:
            out["live"] = True
    except Exception as e:
        out["err"] = str(e)[:80]
    return out


def pick_camera_source(adb_has_device: bool = False, probe: bool = True) -> dict:
    """
    选出最佳视频输入源。返回 {source, reason, cameras}。
    优先级（WiFi 手机终端优先，再有线/虚拟摄像头）：
      1) monitor_relay 手机摄像头在线 → cam.mjpeg URL（浏览器 WiFi 推流）
      2) 安卓已通过 ADB 授权  → scrcpy（画质最好/延迟最低）
      3) 存在"手机虚拟摄像头"且能出活帧 → 用其索引（DroidCam/iVCam 等）
      4) 存在普通可用摄像头且能出活帧 → 用其索引
      5) 兜底 → scrcpy（继续等待设备）
    source: URL、"scrcpy" 或数字索引字符串。
    """
    cams = list_named_cameras()

    relay = check_monitor_phone_cam()
    if relay.get("live"):
        return {"source": relay["url"], "reason": "phone-wifi→cam.mjpeg",
                "cameras": cams, "relay": relay}

    if adb_has_device:
        return {"source": "scrcpy", "reason": "android-adb→scrcpy", "cameras": cams}

    phones = [c for c in cams if classify_camera(c["name"]) == "phone"]
    for c in phones:
        if (not probe) or _probe_live(c["index"]):
            return {"source": str(c["index"]),
                    "reason": f"phone-cam:{c['name']}", "cameras": cams}

    others = [c for c in cams if classify_camera(c["name"]) == "other"]
    for c in others:
        if (not probe) or _probe_live(c["index"]):
            return {"source": str(c["index"]),
                    "reason": f"generic-cam:{c['name']}", "cameras": cams}

    return {"source": "scrcpy", "reason": "fallback→scrcpy", "cameras": cams}


# ── 麦克风 ────────────────────────────────────────────────────────────
def classify_mic(name: str) -> str:
    """output=回环/混音(排除) / phone=手机虚拟麦克风 / other=普通麦克风"""
    if _match(name, MIC_EXCLUDE_PATTERNS):
        return "output"
    if _match(name, PHONE_MIC_PATTERNS):
        return "phone"
    return "other"


def pick_mic(input_devices: list) -> dict:
    """
    从 RVC /inputDevices 返回的设备名列表中选最佳手机麦克风。
    input_devices: 字符串列表（设备名，可能含 "(MME)" 等后缀）。
    优先 MME（采样率更稳，避免 RVC "Invalid sample rate"）。返回 {device, reason}。
    """
    names = [d for d in (input_devices or []) if isinstance(d, str)]
    phones = [d for d in names if classify_mic(d) == "phone"]
    if not phones:
        return {"device": None, "reason": "no-phone-mic"}
    mme = [d for d in phones if "mme" in d.lower()]
    chosen = mme[0] if mme else phones[0]
    return {"device": chosen, "reason": "mme" if mme else "first"}


# ══════════════════════════════════════════════════════════════════════
#  音频设备人话化（P0 呈现层单一真相）：解析 / 去重 / 翻译 / 分组 / 推荐
#
#  背景：RVC /inputDevices 返回 sounddevice 原始枚举（同一物理设备在 MME /
#  DirectSound / WASAPI / WDM-KS 各出现一次，MME 名字还被截到 31 字符），
#  91 个"输入"里真实设备只有约 8 个。本模块把原始名收敛成结构化条目：
#    {value(提交用原始名，不可改), label(人话), group, note, danger, hidden, ...}
#  约束：RVC 按名字字符串精确匹配设备 → value 必须原样保留；显示层任意加工。
#  这里同时是全站设备关键词的单一真相（替代此前散落 4 处的各自小表）。
# ══════════════════════════════════════════════════════════════════════
import re as _re

_AUDIO_HOSTAPIS = {"MME", "Windows DirectSound", "Windows WASAPI", "Windows WDM-KS", "ASIO"}
_HOSTAPI_PREF = {"MME": 0, "Windows DirectSound": 1, "Windows WASAPI": 2, "Windows WDM-KS": 3, "ASIO": 4}
_MME_TRUNC_LEN = 25      # MME 设备名 31 UTF-16 字符截断；基名≥此长度且仅存在于 MME → 尝试并入全名桶

AUDIO_GROUPS = {
    "in": [
        ("phone",   "📱 手机麦克风"),
        ("usb",     "🎙️ 独立麦克风"),
        ("bt",      "🎧 蓝牙耳机"),
        ("cam",     "📷 摄像头麦克风"),
        ("board",   "🖥️ 电脑声卡"),
        ("other",   "🎤 其他输入"),
        ("virtual", "⚙️ 虚拟/系统设备（高级）"),
    ],
    "out": [
        ("live",    "✅ 直播声卡（观众听这一路）"),
        ("monitor", "🎧 耳机/音箱（自己监听）"),
        ("hdmi",    "🖥️ 显示器/HDMI 音频"),
        ("other",   "🔈 其他输出"),
        ("virtual", "⚙️ 虚拟/系统设备（高级）"),
    ],
}

# ── 品牌/关键词词典（P2-3 外置）：内置表为兜底真相，audio_brands.json 可增量扩充──
#    新设备"认不出"时运营只需在 JSON 里加一行关键词，不用改代码。
#    合并规则：JSON 条目在前（先匹配→可覆盖内置的排序/译名），按小写键去重；坏文件/缺文件=纯内置，绝不炸。
_BRANDS_BUILTIN = {
    "phone_keywords": ["droidcam", "droid cam", "ivcam", "e2esoft", "camo", "reincubate",
                       "epoccam", "kinoni", "iriun", "wo mic", "womic", "iphone", "ipad"],
    "phone_pretty": [["droid", "DroidCam"], ["ivcam", "iVCam"], ["e2esoft", "iVCam"],
                     ["camo", "Camo"], ["epoccam", "EpocCam"], ["iriun", "Iriun"],
                     ["wo mic", "WO Mic"], ["womic", "WO Mic"], ["iphone", "iPhone"], ["ipad", "iPad"]],
    "bt_keywords": ["hands-free", "handsfree", "蓝牙", "bluetooth", "airpods", " ag audio", "免提"],
    "cam_keywords": ["brio", "c920", "c922", "c925", "c930", "c270", "c310", "c505", "c615",
                     "streamcam", "webcam", "lifecam", "kiyo", "facecam", "eos webcam", "摄像头"],
    "usbmic_keywords": ["podcast", "pd100", "pd200", "yeti", "seiren", "rode", "røde", "nt-usb",
                        "samson", "fifine", "maono", "hyperx", "quadcast", "at2020", "audio-technica",
                        "shure", "mv7", "usb audio device", "usb microphone", "usb mic", "condenser"],
    "board_keywords": ["realtek", "high definition audio", "conexant", "via hd", "smartaudio",
                       "cirrus", "cx audio", "主板"],
    "hdmi_keywords": ["nvidia", "amd high definition", "intel(r) display", "displayport", "hdmi", "显示器"],
    # 手机麦品牌内推荐排序：与旧 _pick_audio_devices 关键词链同序（droid 最优先）
    "phone_pick_priority": ["droid", "ivcam", "e2esoft", "camo", "epoccam", "iriun",
                            "wo mic", "womic", "iphone", "ipad"],
}
_BRANDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio_brands.json")


def _load_brands() -> dict:
    """内置词典 + audio_brands.json 增量合并（JSON 在前=优先命中）。任何异常回退纯内置。"""
    merged = {k: list(v) for k, v in _BRANDS_BUILTIN.items()}
    try:
        with open(_BRANDS_FILE, encoding="utf-8") as f:
            ext = json.load(f)
        if isinstance(ext, dict):
            for k, builtin in _BRANDS_BUILTIN.items():
                add = ext.get(k)
                if not isinstance(add, list):
                    continue
                out, seen = [], set()
                for item in list(add) + list(builtin):
                    if isinstance(item, str):
                        key = item.strip().lower()
                        if key and key not in seen:
                            seen.add(key)
                            out.append(item.strip())
                    elif isinstance(item, (list, tuple)) and len(item) == 2:   # phone_pretty 的 [关键词, 译名] 对
                        key = str(item[0]).strip().lower()
                        if key and key not in seen:
                            seen.add(key)
                            out.append([str(item[0]).strip(), str(item[1]).strip()])
                if out:
                    merged[k] = out
    except Exception:
        pass
    return merged


_BRANDS = _load_brands()
_KW_PHONE = _BRANDS["phone_keywords"]
_PHONE_PRETTY = [tuple(p) for p in _BRANDS["phone_pretty"]]
_KW_BT = _BRANDS["bt_keywords"]
_KW_CAM = _BRANDS["cam_keywords"]
_KW_USBMIC = _BRANDS["usbmic_keywords"]
_KW_BOARD = _BRANDS["board_keywords"]
_KW_HDMI = _BRANDS["hdmi_keywords"]


def parse_audio_name(raw: str):
    """'CABLE Input (VB-Audio Virtual Cable) (MME)' → ('CABLE Input (VB-Audio Virtual Cable)', 'MME')。
    没有已知 hostapi 后缀则 api 为 ''。兼容 MME 截断导致的括号不闭合。"""
    s = (raw or "").strip()
    if s.endswith(")") and " (" in s:
        base, tail = s.rsplit(" (", 1)
        api = tail[:-1].strip()
        if api in _AUDIO_HOSTAPIS:
            return base.strip(), api
    return s, ""


def _norm(s: str) -> str:
    return _re.sub(r"\s+", " ", (s or "").strip().lower())


def _product_of(base: str) -> str:
    """'麦克风 (PD100X Podcast Microphone' → 'PD100X Podcast Microphone'（去掉'2- '序号前缀）。"""
    m = _re.search(r"\(([^()]*)\)?\s*$", base)
    prod = (m.group(1) if m else base).strip().rstrip(")")
    return _re.sub(r"^\d+-\s*", "", prod) or base.strip()


def _phone_brand(bl: str) -> str:
    for k, pretty in _PHONE_PRETTY:
        if k in bl:
            return pretty
    return "手机 App"


def _classify_audio(base: str, kind: str) -> dict:
    """按'完整基名'分类；返回 {group,label,note,danger,hidden}。顺序即优先级，先特殊后一般。"""
    bl = _norm(base)
    prod = _product_of(base)
    V = lambda **kw: {"group": "virtual", "hidden": True, "danger": "", "note": "", **kw}

    if bl.startswith("loopback:"):
        nm = base.split(":", 1)[1].strip()
        return V(label=f"内放采集（{nm}）",
                 note="录电脑里正在播放的声音（采伴奏用，高级）",
                 danger="当麦克风用会把电脑声音混进直播，观众会听到回声" if kind == "in" else "")
    if "声音映射器" in base or "sound mapper" in bl:
        return V(label="系统默认路由（声音映射器）", note="等同于顶部的「跟随系统默认」")
    # 主声卡/主声音(捕获)驱动程序 = DirectSound 的 Primary Sound (Capture) Driver 各种中文译名
    if "primary sound" in bl or (("主声卡" in base or "主声音" in base) and "驱动" in base):
        return V(label="系统主声卡（旧接口）", note="等同于「跟随系统默认」")
    short = _re.sub(r"\s*\(\s*vb[^)]*\)?\s*$", "", base, flags=_re.I).strip()   # 去 "(VB-Audio…" 尾巴(容截断)
    if "cable" in bl and ("vb-audio" in bl or "virtual" in bl or bl.startswith("cable")):
        if kind == "in":
            return V(label=f"虚拟声卡回收口（{short or 'CABLE Output'}）",
                     danger="这是直播声卡的「回收口」（给 OBS 采声用）——当麦克风用会自激回环/无声")
        if "cable input" in bl and "16ch" not in bl:
            return {"group": "live", "hidden": False, "danger": "",
                    "label": "直播声卡（CABLE Input）",
                    "note": "变声后的声音从这里进 OBS——观众听到的就是这一路"}
        if "16ch" in bl:
            return V(label="直播声卡 16 声道版（不推荐）", note="多声道特殊用途，直播请用普通 CABLE Input")
        return V(label=f"虚拟声卡（{short or base}）")
    if "voicemeeter" in bl:
        return V(label=f"调音台通道（{short or base}）",
                 note="Voicemeeter 高级路由——普通开播用不到" if kind == "in"
                 else "经调音台转发：需自行确认 OBS 采集对应通道（高级）")
    if "splitcam" in bl:
        return V(label="SplitCam 虚拟混音", note="SplitCam 软件的虚拟通道")
    if "立体声混音" in bl or "stereo mix" in bl or "what u hear" in bl or "wave out mix" in bl:
        return V(label="电脑内放（立体声混音）",
                 danger="会把电脑里播放的声音录进直播，观众会听到回声/伴奏" if kind == "in" else "")
    if "obs" in bl and ("virtual" in bl or "monitor" in bl):
        return V(label=f"OBS 虚拟音频（{base}）")
    if _match(bl, _KW_PHONE):
        brand = _phone_brand(bl)
        if kind == "in":
            return {"group": "phone", "hidden": False, "danger": "",
                    "label": f"手机麦克风（{brand}）", "note": "手机 App 传来的声音，收音近、延迟低"}
        return {"group": "other", "hidden": False, "danger": "",
                "label": f"手机音频输出（{brand}）", "note": "回放到手机 App（一般用不到）"}
    if _match(bl, _KW_BT):
        if kind == "in":
            return {"group": "bt", "hidden": False, "danger": "",
                    "label": f"蓝牙耳机麦（{prod}）", "note": "蓝牙通话模式：音质一般、延迟略高，应急可用"}
        return {"group": "monitor", "hidden": False, "danger": "",
                "label": f"蓝牙耳机（{prod}）", "note": "无线监听：自己听用，观众听不到这里的声音"}
    if kind == "in" and _match(bl, _KW_USBMIC):
        if "podcast" in bl:
            nm = _re.sub(r"\s*podcast\s*microphone\s*", "", prod, flags=_re.I).strip() or prod
            return {"group": "usb", "hidden": False, "danger": "",
                    "label": f"播客麦克风（{nm}）", "note": "USB 独立麦克风，音质好"}
        return {"group": "usb", "hidden": False, "danger": "",
                "label": f"独立麦克风（{prod}）", "note": "USB 直连独立麦克风，音质好"}
    if kind == "out" and _match(bl, _KW_USBMIC):
        return {"group": "monitor", "hidden": False, "danger": "",
                "label": f"麦克风自带监听口（{prod}）", "note": "接在 USB 麦克风上的耳机口，自己监听用"}
    if _match(bl, _KW_CAM):
        if kind == "in":
            return {"group": "cam", "hidden": False, "danger": "",
                    "label": f"摄像头麦克风（{prod}）", "note": "摄像头自带收音：距离远、音质一般，有独立麦优先用独立麦"}
        return {"group": "other", "hidden": False, "danger": "", "label": f"{prod}", "note": ""}
    if kind == "out" and _match(bl, _KW_HDMI):
        # '24G11ZE (NVIDIA High Definition Audio)' → 用显示器型号 24G11ZE 当名字
        head = base.split(" (")[0].strip()
        gpuish = _norm(head).startswith(("nvidia", "amd", "intel", "displayport", "hdmi"))
        return {"group": "hdmi", "hidden": False, "danger": "",
                "label": f"显示器音频（{head if head and not gpuish else prod}）",
                "note": "声音走显示器/电视自带喇叭"}
    if _match(bl, _KW_BOARD):
        if kind == "in":
            if "线路" in bl or "line in" in bl:
                return {"group": "board", "hidden": False, "danger": "",
                        "label": "线路输入（主板蓝色孔）", "note": "接播放器/调音台的线路口，不是麦克风孔"}
            return {"group": "board", "hidden": False, "danger": "",
                    "label": "电脑麦克风（主板声卡）", "note": "主机粉色孔接的麦克风（机箱前/后面板）"}
        if "耳机" in bl or "headphone" in bl or "2nd output" in bl:
            return {"group": "monitor", "hidden": False, "danger": "",
                    "label": "耳机（主板声卡 · 机箱前面板）", "note": "自己监听用；观众听不到这里的声音"}
        if "digital" in bl or "spdif" in bl or "光纤" in bl:
            return V(label="光纤/同轴数字输出", note="接功放的数字口，直播用不到")
        return {"group": "monitor", "hidden": False, "danger": "",
                "label": "电脑音箱（主板声卡）", "note": "自己监听用；选这个观众听不到变声"}
    # ── 泛化兜底：按通用前缀翻译 ──
    if kind == "in":
        if bl.startswith("麦克风") or bl.startswith("microphone") or bl.startswith("mic "):
            return {"group": "other", "hidden": False, "danger": "",
                    "label": f"麦克风（{prod}）", "note": ""}
        if "线路" in bl or "line in" in bl:
            return {"group": "other", "hidden": False, "danger": "",
                    "label": f"线路输入（{prod}）", "note": "线路口，不是麦克风"}
        return {"group": "other", "hidden": False, "danger": "", "label": base, "note": ""}
    if bl.startswith("耳机") or "headphone" in bl:
        return {"group": "monitor", "hidden": False, "danger": "",
                "label": f"耳机（{prod}）", "note": "自己监听用"}
    if bl.startswith("扬声器") or bl.startswith("speaker"):
        return {"group": "monitor", "hidden": False, "danger": "",
                "label": f"音箱/扬声器（{prod}）", "note": "自己监听用；观众听不到这里的声音"}
    return {"group": "other", "hidden": False, "danger": "", "label": base, "note": ""}


def humanize_audio_devices(names: list, kind: str) -> dict:
    """原始设备名列表 → 结构化人话条目（去重合并 + 分类翻译）。
    kind: 'in' | 'out'。value 保留原始字符串（RVC 按名精确匹配，不可改）。"""
    items = []
    for raw in names or []:
        if not isinstance(raw, str) or not raw.strip():
            continue
        base, api = parse_audio_name(raw)
        items.append({"raw": raw, "base": base, "api": api})

    # 1) 完全同基名分桶（不同 hostapi 合并）
    buckets, order = {}, []
    for it in items:
        k = _norm(it["base"])
        if k not in buckets:
            buckets[k] = []
            order.append(k)
        buckets[k].append(it)

    # 2) MME 截断名并入唯一全名桶：仅当该桶只存在于 MME（截断只发生在 MME）、
    #    名字够长（≥25，31 字符截断的保守下界）、且恰好一个更长前缀候选（歧义保守不动）
    merged_away = set()
    for k in list(order):
        if len(k) < _MME_TRUNC_LEN or k in merged_away:
            continue
        if not all((it["api"] == "MME" or not it["api"]) for it in buckets[k]):
            continue
        cands = [k2 for k2 in order
                 if k2 != k and k2 not in merged_away and k2.startswith(k) and len(k2) > len(k)]
        if len(cands) == 1:
            buckets[cands[0]].extend(buckets[k])
            merged_away.add(k)

    entries = []
    for k in order:
        if k in merged_away:
            continue
        grp = buckets[k]
        grp.sort(key=lambda it: _HOSTAPI_PREF.get(it["api"], 9))
        full_base = max((it["base"] for it in grp), key=len)
        apis = []
        for it in grp:
            if it["api"] and it["api"] not in apis:
                apis.append(it["api"])
        cls = _classify_audio(full_base, kind)
        hidden = bool(cls.get("hidden"))
        note = cls.get("note", "")
        # 仅剩 WDM-KS 内核接口的孤儿条目：默认折叠（独占模式易冲突，专家再用）
        if not hidden and apis and all(a == "Windows WDM-KS" for a in apis):
            hidden = True
            note = (note + " · " if note else "") + "内核流接口（高级）"
        entries.append({
            "value": grp[0]["raw"], "raw": grp[0]["raw"], "base": full_base,
            "label": cls["label"], "group": cls["group"], "danger": cls.get("danger", ""),
            "note": note, "hidden": hidden, "hostapis": apis,
            "variants": [it["raw"] for it in grp],
        })

    glabels = dict(AUDIO_GROUPS[kind])
    for e in entries:
        e["group_label"] = glabels.get(e["group"], e["group"])
    return {"devices": entries, "raw_count": len(items), "merged_count": len(entries),
            "groups": [{"key": g, "label": lb} for g, lb in AUDIO_GROUPS[kind]]}


_PICK_REASONS = {
    "phone": "手机就在嘴边：收音近、延迟低",
    "usb":   "独立麦克风，音质最好",
    "bt":    "蓝牙耳机麦（通话音质，应急可用）",
    "cam":   "摄像头自带麦：距离远，先用着，建议接独立麦",
    "board": "主板声卡的麦克风口",
    "other": "当前可用的输入设备",
    "live":  "变声后的声音从这里进 OBS——观众听到的就是这一路",
}
_PICK_ORDER = {"in": ["phone", "usb", "bt", "cam", "board", "other"], "out": ["live"]}
# 手机麦品牌内排序（词典键 phone_pick_priority，可经 audio_brands.json 扩充）
_PHONE_PICK_PRI = [p if isinstance(p, str) else str(p[0]) for p in _BRANDS["phone_pick_priority"]]


def label_for(raw: str, kind: str) -> str:
    """单个原始设备名 → 人话标签（设备已不在线时也能给出可读名字，供丢失提示用）。"""
    base, _api = parse_audio_name(raw or "")
    try:
        return _classify_audio(base, kind).get("label") or base or (raw or "")
    except Exception:
        return base or (raw or "")


def pick_best(entries: list, kind: str) -> dict:
    """从结构化条目中选推荐设备。输入按 手机>独立麦>蓝牙>摄像头麦>主板 排序；输出只认直播声卡。"""
    for g in _PICK_ORDER.get(kind, []):
        cand = [e for e in entries or [] if e.get("group") == g and not e.get("danger")]
        if not cand:
            continue
        if g == "phone":
            def _pri(e):
                bl = _norm(e.get("base") or e.get("value") or "")
                for i, kw in enumerate(_PHONE_PICK_PRI):
                    if kw in bl:
                        return i
                return 99
            cand.sort(key=_pri)
        e = cand[0]
        return {"value": e["value"], "label": e["label"], "group": g,
                "reason": _PICK_REASONS.get(g, "")}
    return {"value": "", "label": "", "group": "",
            "reason": ("未检测到可用麦克风" if kind == "in"
                       else "未检测到直播虚拟声卡（VB-Cable）——观众会听不到声音")}


if __name__ == "__main__":
    print("== 摄像头(带分类/活帧探测) ==")
    for c in annotate_cameras(probe=True):
        print(f"  [{c['index']}] {c['name']!r}  kind={c['kind']}  live={c.get('live')}")
    print("\n== 自动选源(假设无ADB) ==")
    print(" ", pick_camera_source(adb_has_device=False, probe=True))
