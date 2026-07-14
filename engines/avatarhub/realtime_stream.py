# -*- coding: utf-8 -*-
"""
实时直播流 —— 视频换脸 + 虚拟摄像头输出
==============================================
架构：
  物理摄像头 / scrcpy手机画面
      ↓ 30fps 捕获
  [换脸队列] → FaceSwap API (8000) → 换脸结果缓存
      ↓ 30fps 平滑输出
  pyvirtualcam → OBS Virtual Camera
      ↓
  微信/抖音/Zoom 等APP看到"OBS Virtual Camera"

前置条件（一次性安装）：
  1. 安装 OBS Studio：https://obsproject.com/
     安装后"工具 → 虚拟摄像头 → 启动"（或开机自动启动OBS Virtual Camera）
  2. pip install pyvirtualcam opencv-python requests numpy
"""
import sys, io, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

import cv2
# 抑制 DirectShow(VideoIO) 后端在"摄像头暂时打不开"时刷屏的原生警告：
#   cap.cpp:459 raised unknown C++ exception! / cap.cpp:480 can't be used to capture by index
# 打开失败已由下方 capture_worker 的重试+自动重选源逻辑与我们自己的日志接管，
# 不需要底层每 3s 重试都往控制台糊一屏。ERROR 及以上级别不受影响。
try:
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
except Exception:
    pass
import numpy as np
import base64
import requests
import time
import threading
import argparse
import socket
import subprocess
import json
from urllib.parse import urlparse, parse_qs
from queue import Queue, Empty, Full
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

# ── 配置 ───────────────────────────────────────────────────────────
FACESWAP_API   = "http://127.0.0.1:8000/faceswap"
AVATARHUB_API  = "http://127.0.0.1:9000"
# 换脸请求目标：默认走 AvatarHub 代理（注入激活角色脸）；设 SWAP_API_URL 可直连
# faceswap_api(8000)，无需启动整个数字人 Hub（轻量「真人换脸+变声」链路用）。
SWAP_ENDPOINT  = os.environ.get("SWAP_API_URL", f"{AVATARHUB_API}/faceswap")
# 2026-07-09 二进制直连(可选提速)：SWAP_API_RAW=<引擎根地址,如 http://192.168.0.104:8000>
# → 帧走引擎 /faceswap_raw(原始 JPEG 进出,免 base64 膨胀33%/编解码 CPU/Hub 代理跳)。
# 源脸=引擎侧激活角色(Hub 激活时已推 /faces/switch + register_avg)。引擎未升级(404)自动
# 永久回退 JSON 通道；网络异常单帧回退——不牺牲任何健壮性。默认空=行为不变。
SWAP_RAW_BASE  = os.environ.get("SWAP_API_RAW", "").strip().rstrip("/")
import app_config
_BASE = str(app_config.BASE)

# ── 虚拟背景（Phase 12 C-1）：换脸后·vcam 前的背景替换层（none=零开销直通）──
try:
    from bg_replace import BackgroundReplacer
    _bg = BackgroundReplacer()
except Exception as _bg_e:
    _bg = None
    print(f"[BG] 虚拟背景模块不可用(不影响换脸): {_bg_e}", flush=True)
_vcam_out_frame = None   # vcam 实际推出的最终帧(含 crossfade+背景)，供预览同源显示
STATUS_FILE    = rf"{_BASE}\realtime_status.json"    # FPS 共享状态文件
CAMERA_IDX_FILE  = rf"{_BASE}\camera_index.txt"         # 持久化摄像头索引
CAMERA_STATUS_FILE = rf"{_BASE}\camera_status.json"       # 热重载状态反馈

DEFAULT_WIDTH  = 1280

# 热重载摄像头索引（-1表示无变化请求）
_desired_cam_idx: int = -1

def _get_saved_camera_index() -> int:
    try:
        if os.path.exists(CAMERA_IDX_FILE):
            with open(CAMERA_IDX_FILE, 'r') as f:
                v = f.read().strip()
                return int(v) if v.isdigit() else 0
    except Exception:
        pass
    return 0

def camera_watcher():
    """后台线程：每5秒检查camera_index.txt，变化时更新_desired_cam_idx"""
    global _desired_cam_idx
    last_idx = _get_saved_camera_index()
    _desired_cam_idx = last_idx  # 初始值
    # 初始化状态文件
    _write_camera_status(last_idx, True, "running")
    while _running:
        time.sleep(5)
        try:
            idx = _get_saved_camera_index()
            if idx != last_idx:
                print(f"[Watcher] Camera index changed: {last_idx} → {idx}")
                _desired_cam_idx = idx
                last_idx = idx
                _write_camera_status(idx, True, "reloading")
        except Exception:
            pass

def _write_camera_status(idx: int, ok: bool, state: str):
    """写入摄像头状态到共享文件，供Hub读取反馈给UI"""
    try:
        import json as _json
        with open(CAMERA_STATUS_FILE, 'w', encoding='utf-8') as f:
            _json.dump({"index": idx, "ok": ok, "state": state, "ts": time.time()}, f)
    except Exception:
        pass
DEFAULT_HEIGHT = 720
DEFAULT_FPS    = 30

# 输出比例预设（OBS 虚拟摄像头 / Telegram 画面比例）。运行时可经 /output/aspect 热切。
_ASPECT_PRESETS = {
    "portrait916":    {"label": "手机竖屏 9:16",   "width": 720,  "height": 1280},
    "portrait34":     {"label": "手机竖屏 3:4",    "width": 720,  "height": 960},
    "landscape169":   {"label": "横屏 16:9",       "width": 1280, "height": 720},
    "landscape169hd": {"label": "横屏 16:9 高清",  "width": 1920, "height": 1080},
    "square11":       {"label": "方形 1:1",        "width": 720,  "height": 720},
}
_out_preset = "landscape169"
_out_width  = DEFAULT_WIDTH
_out_height = DEFAULT_HEIGHT
_out_dim_lock = threading.Lock()


def _output_status() -> dict:
    with _out_dim_lock:
        p = _ASPECT_PRESETS.get(_out_preset, {})
        return {
            "preset": _out_preset,
            "width": _out_width,
            "height": _out_height,
            "label": p.get("label", f"{_out_width}x{_out_height}"),
            "presets": {k: v["label"] for k, v in _ASPECT_PRESETS.items()},
        }


def apply_output_aspect(name: str = "", width: int = 0, height: int = 0) -> dict:
    """切换虚拟摄像头输出比例。name=预设键；或直接给 width×height(自定义)。"""
    global _out_preset, _out_width, _out_height, DEFAULT_WIDTH, DEFAULT_HEIGHT
    key = (name or "").strip().lower()
    if key:
        p = _ASPECT_PRESETS.get(key)
        if not p:
            return {"ok": False, "error": f"未知比例:{name!r}",
                    "presets": list(_ASPECT_PRESETS)}
        w, h, label = p["width"], p["height"], p["label"]
        preset = key
    elif width > 0 and height > 0:
        w, h, label, preset = int(width), int(height), f"{width}x{height}", "custom"
    else:
        return {"ok": False, "error": "需指定 name=预设 或 width+height"}
    with _out_dim_lock:
        _out_preset, _out_width, _out_height = preset, w, h
        DEFAULT_WIDTH, DEFAULT_HEIGHT = w, h
    print(f"[Output] 比例 → {label} ({w}x{h})", flush=True)
    return {"ok": True, "output": _output_status()}
# ── 换脸预设档：一键切换「观感/性能」取向，隐藏底层 SWAP_* 细节 ──────────────
# SWAP_PRESET=natural(自然·默认) | beauty(美颜) | hd(高清) | eco(省电)。
# 预设仅提供“默认值”；任何显式设置的 SWAP_* 环境变量仍然优先(可在预设上微调单项)。
# 未设 SWAP_PRESET 时行为与旧版逐字节一致(预设表取空 → 回退到原硬编码默认)。
_SWAP_PRESETS = {
    "natural": {"SWAP_PROC_W": 384, "SWAP_ENHANCE": "none",   "SWAP_SMOOTH": 1.0, "SWAP_SHARPEN": 0.6,  "SWAP_FPS": 15, "SWAP_JPEG_Q": 55},
    "beauty":  {"SWAP_PROC_W": 448, "SWAP_ENHANCE": "gfpgan", "SWAP_SMOOTH": 1.2, "SWAP_SHARPEN": 0.35, "SWAP_FPS": 14, "SWAP_JPEG_Q": 60},
    # hd 档精修 gfpgan→gpen(2026-07-09)：GPEN-256 ONNX 单前向 ~10ms 级 vs GFPGAN ~170-220ms，
    # 清晰度/身份实测持平或更好(id 0.8538 vs 0.8434)。引擎未升级时收到 gpen=静默不增强(有锐化兜底)。
    "hd":      {"SWAP_PROC_W": 512, "SWAP_ENHANCE": "gpen",   "SWAP_SMOOTH": 1.0, "SWAP_SHARPEN": 0.7,  "SWAP_FPS": 12, "SWAP_JPEG_Q": 72},
    "eco":     {"SWAP_PROC_W": 288, "SWAP_ENHANCE": "none",   "SWAP_SMOOTH": 0.9, "SWAP_SHARPEN": 0.5,  "SWAP_FPS": 10, "SWAP_JPEG_Q": 45},
}
SWAP_PRESET   = os.environ.get("SWAP_PRESET", "").strip().lower()
_PS           = _SWAP_PRESETS.get(SWAP_PRESET, {})


def _ps(key, fallback):
    """取参数：显式环境变量 > 预设档 > 硬编码默认。"""
    return os.environ.get(key, str(_PS.get(key, fallback)))


# [2026-07-06 P1] 把「显式 SWAP_* 环境变量仍然优先」的承诺延伸到运行期：
# 启动时记下用户显式钉住的单项，切档(手动/自适应)后重贴——此前第一次换档就会把 env 微调静默冲掉。
# 注意 SWAP_FPS 不在此列(用上限语义 _USER_SWAP_FPS_CAP 单独处理)；钉住 SWAP_ENHANCE/PROC_W 意味着
# 自适应只能调其余旋钮(用户显式接管该项，文档语义如此)。
_USER_PIN = {k: os.environ[k].strip() for k in
             ("SWAP_PROC_W", "SWAP_ENHANCE", "SWAP_SMOOTH", "SWAP_SHARPEN", "SWAP_JPEG_Q")
             if os.environ.get(k, "").strip()}


SWAP_FPS       = int(_ps("SWAP_FPS", "15"))                   # 换脸每秒处理帧数上限
JPEG_QUALITY   = int(_ps("SWAP_JPEG_Q", "55"))               # 送去换脸的JPEG质量(越低越快)
SWAP_MIN_INTERVAL = 1.0 / max(1, SWAP_FPS)
# 换脸处理分辨率(宽,px)：真正决定远端推理+传输耗时的旋钮，与显示分辨率无关
SWAP_PROC_W    = int(_ps("SWAP_PROC_W", "384"))
# 实时增强：none=关(默认，省~90ms/帧)；gfpgan/codeformer=开(更清晰但更慢)
SWAP_ENHANCE   = _ps("SWAP_ENHANCE", "none")
# 服务端时域平滑系数：1.0=关闭混帧(默认，消除晃动幻影)。每帧随请求下发，
# 免疫 faceswap 服务被 hub 自愈重启后参数回默认(0.6)导致幻影复现。
SWAP_SMOOTH    = float(_ps("SWAP_SMOOTH", "1.0"))
# 换脸输出锐化强度(unsharp mask)：0=关。0.4~0.8 提升观感清晰度，几乎不耗时。
SWAP_SHARPEN   = float(_ps("SWAP_SHARPEN", "0.6"))
# 贴回模式(2026-07-09 随帧下发)：feather=羽化掩码贴回(默认；~2ms，无 Poisson 亮度渗漏——绿幕
# 强光"发白"的根因之一)；poisson=引擎旧行为。旧引擎忽略未知字段，零风险。
SWAP_BLEND_MODE = os.environ.get("SWAP_BLEND_MODE", "feather").strip().lower()
# XSeg 遮挡掩码(话筒/手/刘海挡脸时不糊贴)。默认开(2026-07-10 四轮终版)：引擎侧已改
# 「CPU 后台 3Hz 异步刷新」——帧路径只读缓存永不等待，GPU 零占用，生产实测遮挡开着
# 单帧 56ms(与关时无差)。掩码陈旧度 ≤350ms，对在位遮挡物观感无差；首 1-2 帧无掩码
# (等效关)属预期。SWAP_OCCLUSION=0 可关。
SWAP_OCCLUSION  = os.environ.get("SWAP_OCCLUSION", "1").strip() not in ("0", "false", "False")
# 口型区保护(2026-07-10)：贴回时保留目标真实嘴区(106点唇部+羽化)——说话口型/牙齿/舌
# 100% 真实，口播/带货观感质变；代价是唇色不随源脸(妆容层可补)。默认开；SWAP_MOUTH_MASK=0 关。
SWAP_MOUTH_MASK = os.environ.get("SWAP_MOUTH_MASK", "1").strip() not in ("0", "false", "False")
# 小脸不换门槛(2026-07-10)：脸最长边 < 此像素(送检图坐标)→不换保原画(远景走动时换脸只会糊)。
# 全帧路径送 SWAP_PROC_W(512)宽,44px≈占画面 8.6%;裁剪通道脸占满不受影响。0=不设限。
SWAP_MIN_FACE_PX = int(os.environ.get("SWAP_MIN_FACE_PX", "44"))
# 贴回掩码内缩(2026-07-10 光头主播修缮)："上,右,下,左" 百分比(如 "8,12,0,12")。本人光头/
# 短发而源脸有头发时，对齐框边缘的源发会糊上头皮=头两侧深色发带；内缩把掩码边扣掉。
# 默认空=不随帧下发(用引擎 /params 侧默认，那里已支持热调+落盘)；设了则逐帧显式覆盖。
def _parse_mask_padding(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        v = [max(0.0, min(40.0, float(x))) for x in raw.split(",")[:4]]
        v = (v + [0.0] * 4)[:4]
        return v if any(x > 0 for x in v) else None
    except Exception:
        print(f"[Swap] SWAP_MASK_PADDING 格式无效(需'上,右,下,左'): {raw!r} → 忽略", flush=True)
        return None
SWAP_MASK_PADDING = _parse_mask_padding(os.environ.get("SWAP_MASK_PADDING", ""))
# 全帧路径脸区贴回(2026-07-10)：只把换好的脸区贴回原生帧,背景保持原生像素(不整幅 512→720 放大)。
SWAP_FULLFRAME_PASTE = os.environ.get("SWAP_FULLFRAME_PASTE", "1").strip() not in ("0", "false", "False")
MJPEG_FPS      = 12       # MJPEG推流帧率降低到12fps减少卡顿
# 输出(预览/手机端)JPEG质量：与「送检压缩」解耦(2026-07-05 P0)。送检低q是为省传输+推理耗时(按画质档走)，
# 输出高q几乎零代价(本机编码+局域网)。OBS 虚拟摄像头走 pyvirtualcam 原始帧，不经此参数。
OUT_JPEG_QUALITY = int(os.environ.get("SWAP_OUT_JPEG_Q", "85"))
PREVIEW        = True     # 同时弹出本地预览窗口

# ── 全局状态 ────────────────────────────────────────────────────────
_raw_frame    = None      # 最新原始帧
_swapped_frame = None     # 最新换脸帧
_swap_time    = 0.0
_frame_lock   = threading.Lock()
SWAP_WORKERS  = max(1, int(os.environ.get("SWAP_WORKERS", "3")))   # 并发换脸线程数
_swap_queue   = Queue(maxsize=SWAP_WORKERS + 1)
_swap_seq     = 0          # 生产者帧序号（递增）
_applied_seq  = -1         # 已应用到输出的最大帧序号（防止乱序回退）
_running      = True
_swap_ok      = 0         # 成功换脸计数
_swap_fail    = 0
_fs_degraded  = False     # S8-2: Hub 容灾副本接管中（无 GFPGAN）→ 提高 unsharp 补偿 + 状态透出
_fs_degraded_ts = 0.0
_last_noface_log = 0.0    # 无脸日志节流时间戳（避免逐帧刷屏）
_fps_actual   = 0.0
_swap_latency_last = 0.0
_swap_latency_avg  = 0.0
_swap_latency_wmax = 0.0   # 快照窗口内(~30s)单帧最差时延；每次快照读取后清零。抓 EWMA 抹平的偶发卡顿
_swap_meta    = {}
_last_face_boxes = []      # 最近一次成功换脸的脸区 bbox（显示分辨率坐标），供 /swap/quality 画质体检定位脸区
_last_face_boxes_ts = 0.0

# ── 脸区原生分辨率通道（2026-07-06 P2）────────────────────────────────────
# 旧路径把整帧缩到 SWAP_PROC_W 再送检：脸区像素被下采样稀释（720p 上 300px 的脸到 512 帧里只剩
# ~210px，还要再放大回去），背景也跟着糊一轮。新路径按「上一帧回传的脸框」裁原图送检：
# 脸按原生像素进引擎（检测/增强都在大脸上做），背景完全不动（原生像素直出）。
#   · 滞回稳框：脸框(留半边距)仍在旧裁剪窗内 → 沿用旧窗，避免逐帧重取景抖动引擎输入；
#   · 周期全帧探测(默认3s)：只刷新脸框/发现第二张脸，不上屏——防背景清晰度脉冲；
#   · 失脸回退：裁剪窗内检不到脸 → 清框+冷却1.5s → 自动回全帧路径，绝不卡死在错误窗口；
#   · 多脸(≠1)自动走全帧（裁剪只服务单主播场景）；裁剪长边上限随画质档 PROC_W 缩放，
#     自适应降档对裁剪路径同样有效。运行时可经 /swap/crop?on=0|1 热切。
SWAP_CROP         = os.environ.get("SWAP_CROP", "1") == "1"
SWAP_CROP_MARGIN  = float(os.environ.get("SWAP_CROP_MARGIN", "0.45"))   # 脸框每侧外扩比例
SWAP_CROP_MAX     = int(os.environ.get("SWAP_CROP_MAX", "560"))         # 裁剪送检长边上限(hd档)
SWAP_CROP_STALE_S = float(os.environ.get("SWAP_CROP_STALE_S", "2.0"))   # 脸框超龄→回全帧
SWAP_CROP_PROBE_S = float(os.environ.get("SWAP_CROP_PROBE_S", "3.0"))   # 全帧探测周期(只刷框不上屏)
# 仅换主脸(2026-07-06p 直播实测：第二张脸入镜→裁剪退全帧+每脸都换+增强翻倍,490ms vs 单脸 250-330ms)：
#   默认开——只换最大脸(主播)，海报/路人/手机屏不被误换；引擎只回主脸框→裁剪通道在多脸场景保持咬合。
#   双人同框节目请 /swap/main_face?on=0 关闭(或用 face_map 双人档)。运行时热切,重启回默认。
SWAP_MAIN_FACE    = os.environ.get("SWAP_MAIN_FACE", "1") == "1"
# 离席兜底(同场实测：主播离席 6 分钟,观众看冻结帧以为直播卡死)：最后成功换脸帧超龄→渐进模糊+角标,
#   恢复出脸即刻回正常。真脸从不外泄(模糊的是最后一张换好的脸)。off=保留旧冻结行为。
# 06u 自定义：SWAP_AWAY_TEXT 改角标文案(空=不出角标)；style=image 时用 SWAP_AWAY_IMAGE 品牌图
#   (cover 填充,图坏/缺→自动退 blur)。/swap/away 在播热切,重启回环境变量默认。
SWAP_AWAY_AFTER   = float(os.environ.get("SWAP_AWAY_AFTER", "10"))      # 换脸帧超龄多少秒进入离席画面
SWAP_AWAY_STYLE   = os.environ.get("SWAP_AWAY_STYLE", "blur")           # blur/image/off
SWAP_AWAY_TEXT    = os.environ.get("SWAP_AWAY_TEXT", "")   # 默认不出角标文字(空=不叠角标)
SWAP_AWAY_IMAGE   = os.environ.get("SWAP_AWAY_IMAGE", "")               # 品牌图路径(style=image 用)
_away_badge       = None     # 预渲染角标(BGRA)，首次用时生成；文案热改后重置重渲染
_away_img_cache   = {"path": None, "img": None}   # 品牌图解码缓存(路径变更即重载)
_crop_rect        = None     # 当前裁剪窗(显示坐标,16px对齐);None=全帧
_crop_active      = False    # 最近上屏的一帧是否走裁剪通道
_crop_hits        = 0
_crop_miss        = 0
_crop_block_until = 0.0      # 失脸冷却截止,期间不进裁剪(防裁剪↔全帧打摆)
_crop_probe_ts    = 0.0
_feather_masks    = {}       # 羽化掩码缓存 {(w,h):mask}；16px 对齐+滞回让尺寸种类很少
# 近窗滚动样本 (ts, swap_ok, swap_fail)：在源头算"最近 ~5s 的成功/失败增量与每秒速率"，
# 比前端按轮询间隔算更准、更稳（不受前端 poll 抖动/丢包影响）。每 2s 写状态时各 append 一帧。
_swap_hist    = deque(maxlen=16)

# 请求级换脸参数（由 CLI 设置）
face_params = {}

# ── 运行时预设切换 + 状态（供 /control 控制页与 /swap/* 端点用）─────────────────
# 换脸的画质/性能旋钮(PROC_W/ENHANCE/SMOOTH/SHARPEN/FPS/JPEG_Q)都是 swap_worker 逐帧
# 读取的模块全局量，故可在运行时直接改写即时生效——无需重启。仅 SWAP_WORKERS(线程数)
# 在启动时固化，改它需重启。模型(inswapper/hyperswap)与 TRT 由 faceswap_api 端决定，
# 不在此切换，仅经 /swap/status 代理其 /health 暴露真相(GPU/CPU/TRT)。
# ── 换脸画质·负载自适应（实时优先·绝不掉帧；opt-in 默认关，零回归）──────────────
#   信号 = 实测每帧换脸时延 _swap_latency_avg(EWMA·faceswap_api 往返)。持续偏高→自动降一档
#   (PROC_W↓ + 关 GFPGAN，直接砍时延)；持续宽裕→自动升回一档，但绝不超过用户选定的"目标档"。
#   CPU 兜底无需显式判：引擎掉 CPU→时延必然飙高→被降档逻辑自然压到最低档 eco。带滞回(降/升不同
#   阈值)+驻留(连续 N 个 ~2s 采样才动)防抖。与实时口型"负载门控"同源理念(满载自动回落·绝不掉帧)。
SWAP_AUTO         = os.environ.get("SWAP_AUTO_QUALITY", "0") == "1"    # 总开关(默认关，零回归)
_SWAP_ORDER       = ["eco", "natural", "beauty", "hd"]                 # 画质低→高(PROC_W 288/384/448/512)
SWAP_AUTO_DOWN_MS = float(os.environ.get("SWAP_AUTO_DOWN_MS", "90"))   # 每帧时延>此(持续驻留)→降一档
SWAP_AUTO_UP_MS   = float(os.environ.get("SWAP_AUTO_UP_MS", "45"))     # 每帧时延<此(持续驻留)且未达目标→升一档
SWAP_AUTO_DWELL   = int(os.environ.get("SWAP_AUTO_DWELL", "3"))        # 同向连续 N 次(每次~2s)才动，防抖
# 升档退避(2026-07-05 实弹演习补强)：轻档时延在过载下依旧很低(不能预测重档扛不扛得住)，
# 只靠滞回+驻留会陷入「爬升→压垮→跌落」试探震荡(75s 注压内转档 12 次)。对策：刚被降档后
# 先蹲 HOLDOFF 秒才允许爬；若爬上去 STABLE 秒内又被打下来，退避时间翻倍(封顶 8×)；
# 持续 RESET 秒无降档则退避归零。持续过载收敛为「低档稳态+偶发探测」，负载解除仍能爬回目标。
SWAP_AUTO_CLIMB_HOLDOFF_S = max(0.0, float(os.environ.get("SWAP_AUTO_CLIMB_HOLDOFF_S", "20")))
_SWAP_CLIMB_STABLE_S      = 45.0    # 爬升后多久内被降档视为"爬早了"(退避翻倍)
_SWAP_BACKOFF_RESET_S     = 120.0   # 连续无降档多久后退避归零
_SWAP_BACKOFF_CAP         = 4      # 退避倍数封顶(20s→最长 80s,保证负载解除后 ~1.5min 内必有试探)
_swap_target_preset = (SWAP_PRESET or "natural")   # 用户选定的目标档(自动只在 [eco..target] 内浮动)
_swap_auto_reason   = ""                            # 最近一次自适应决策原因(供 /ops 展示)
_swap_auto_dwell    = 0                             # 同向驻留计数(正=想升/负=想降；反向清零)
_swap_last_down_t   = 0.0                           # 最近一次自动降档时刻
_swap_last_climb_t  = 0.0                           # 最近一次自动升档时刻
_swap_climb_backoff = 1                             # 当前退避倍数(1=无退避)

# ── 换脸性能趋势留存 + 自适应反哺（把口译侧「跨会话统计」方法论迁到换脸）─────────────
#   连续流无会话边界→按时间滚动快照(每 SWAP_STATS_EVERY 秒)一份 {fps/延迟/窗口成功失败/生效档/引擎}
#   落 logs/swap_stats.json(趋势观测·跨重启回灌连续)。SWAP_AUTO_REMEMBER=1(opt-in)时依近期趋势的生效档
#   众数为「本机可持续画质档」，下次启动直接从该档起——避开每次从高档起、被自适应降档前的开场掉帧。
#   关(默认 REMEMBER=0)→零行为变化；SWAP_STATS=0→不快照不落盘(零开销)。
SWAP_STATS_ON      = os.environ.get("SWAP_STATS", "1") == "1"
SWAP_STATS_EVERY   = max(5.0, float(os.environ.get("SWAP_STATS_EVERY", "30")))   # 快照间隔秒
SWAP_STATS_MAX     = max(10, int(os.environ.get("SWAP_STATS_MAX", "240")))       # 趋势保留快照数(240×30s≈2h)
SWAP_AUTO_REMEMBER = os.environ.get("SWAP_AUTO_REMEMBER", "0") == "1"            # 记忆可持续档→下次从此起(opt-in)
SWAP_STATS_PATH    = os.path.join(_BASE, "logs", "swap_stats.json")
_swap_trend        = deque(maxlen=SWAP_STATS_MAX)   # 内存滚动趋势(启动时从盘回灌)
_swap_stats_state  = {"last": 0.0}                  # 上次快照时间(容器存放，免在循环函数内声明 global)


_USER_SWAP_FPS_CAP = 0   # 用户显式 --swap-fps(>0)=帧率上限；换档只在上限内取值，不再无声覆盖(2026-07-05 P0)


def _apply_preset_values(key: str):
    """把某预设参数写进运行时全局(逐帧读取，即时生效)。不改'目标档'——供用户切换与自适应共用。
    注意：SWAP_FPS 尊重用户显式上限(_USER_SWAP_FPS_CAP)；输出画质(OUT_JPEG_QUALITY)与档位无关，永不触碰。"""
    global SWAP_PRESET, SWAP_FPS, JPEG_QUALITY, SWAP_MIN_INTERVAL
    global SWAP_PROC_W, SWAP_ENHANCE, SWAP_SMOOTH, SWAP_SHARPEN
    ps = _SWAP_PRESETS[key]
    SWAP_PRESET  = key
    SWAP_PROC_W  = int(_USER_PIN.get("SWAP_PROC_W", ps["SWAP_PROC_W"]))
    SWAP_ENHANCE = str(_USER_PIN.get("SWAP_ENHANCE", ps["SWAP_ENHANCE"]))
    SWAP_SMOOTH  = float(_USER_PIN.get("SWAP_SMOOTH", ps["SWAP_SMOOTH"]))
    SWAP_SHARPEN = float(_USER_PIN.get("SWAP_SHARPEN", ps["SWAP_SHARPEN"]))
    SWAP_FPS     = int(ps["SWAP_FPS"])
    if _USER_SWAP_FPS_CAP > 0:
        SWAP_FPS = min(SWAP_FPS, _USER_SWAP_FPS_CAP)
    JPEG_QUALITY = int(_USER_PIN.get("SWAP_JPEG_Q", ps["SWAP_JPEG_Q"]))
    SWAP_MIN_INTERVAL = 1.0 / max(1, SWAP_FPS)


def apply_swap_preset(name: str) -> dict:
    """用户切换换脸预设 = 设定'目标档'并即时应用。开了自适应(SWAP_AUTO)后，后续会在 [eco..目标档]
    内按实测时延自动升降(但绝不超过目标)。返回 {ok, preset, target, auto} 或 {ok:False, error, presets}。"""
    global _swap_target_preset, _swap_auto_dwell, _swap_auto_reason
    key = (name or "").strip().lower()
    if key not in _SWAP_PRESETS:
        return {"ok": False, "error": f"未知预设:{name!r}", "presets": list(_SWAP_PRESETS)}
    _swap_target_preset = key
    _swap_auto_dwell = 0
    _swap_auto_reason = ""
    _apply_preset_values(key)
    print(f"[Swap] 目标预设 → {key}  PROC_W={SWAP_PROC_W} FPS={SWAP_FPS} "
          f"ENHANCE={SWAP_ENHANCE} SHARPEN={SWAP_SHARPEN} JPEG_Q={JPEG_QUALITY}"
          + ("  (自适应开:将按时延在≤此档内浮动)" if SWAP_AUTO else ""), flush=True)
    return {"ok": True, "preset": key, "target": key, "auto": SWAP_AUTO}


def _swap_autotune():
    """每 ~2s 调一次：按实测每帧换脸时延，在 [eco..目标档] 内自动升/降档，绝不掉帧。opt-in。
    降/升用不同阈值(滞回)+同向连续驻留 N 次(防抖)。降档按超标倍数一次跳多档(先稳后快：突发重载
    ≥3×阈值直跳最低档，避免逐档 3×驻留才到底)，升档永远单档保守恢复，且带**指数退避**——
    轻档的低时延不能证明重档扛得住(过载下 eco 也只有 150ms)，滞回阻不断「爬升试探→被压垮」
    的震荡循环，退避把试探间隔从 ~8s 拉长到 20s→40s→…(封顶)，持续过载即收敛低档稳态。
    纯算术、无阻塞调用，安全放热路径。"""
    global _swap_auto_dwell, _swap_auto_reason
    global _swap_last_down_t, _swap_last_climb_t, _swap_climb_backoff
    if not SWAP_AUTO:
        return
    order = _SWAP_ORDER
    target = _swap_target_preset if _swap_target_preset in order else "natural"
    t_idx = order.index(target)
    cur = SWAP_PRESET if SWAP_PRESET in order else "natural"
    c_idx = order.index(cur)
    lat = _swap_latency_avg
    if lat <= 0:                       # 尚无样本/未在换脸 → 不动
        return
    now = time.time()
    if _swap_last_down_t and (now - _swap_last_down_t) > _SWAP_BACKOFF_RESET_S:
        _swap_climb_backoff = 1        # 长时间没被降档 → 环境已宽裕，清退避
    want = 0                           # -1 想降档, +1 想升档, 0 稳
    if lat > SWAP_AUTO_DOWN_MS and c_idx > 0:
        want = -1
    elif lat < SWAP_AUTO_UP_MS and c_idx < t_idx:
        holdoff = SWAP_AUTO_CLIMB_HOLDOFF_S * _swap_climb_backoff
        if (now - _swap_last_down_t) < holdoff:      # 刚被打下来，蹲够退避期再试探
            _swap_auto_dwell = 0
            _swap_auto_reason = ("维持 %s · 时延 %.0fms(爬升退避 %.0fs/%.0fs)"
                                 % (cur, lat, now - _swap_last_down_t, holdoff))
            return
        want = +1
    if want == 0:
        _swap_auto_dwell = 0
        _swap_auto_reason = (("已达目标 %s · 时延 %.0fms" % (cur, lat)) if c_idx == t_idx
                             else ("维持 %s · 时延 %.0fms" % (cur, lat)))
        return
    _swap_auto_dwell = (_swap_auto_dwell + want) if (_swap_auto_dwell * want) >= 0 else want
    if abs(_swap_auto_dwell) < max(1, SWAP_AUTO_DWELL):
        return
    _swap_auto_dwell = 0
    if want < 0:                       # 降档：按超标倍数一次跳多档(突发重载快速保实时；EWMA+驻留已滤掉瞬时毛刺)
        over = lat / SWAP_AUTO_DOWN_MS
        steps = 1 if over < 2 else (2 if over < 3 else 3)
        nxt_idx = max(0, c_idx - steps)
        _swap_last_down_t = now
        if (now - _swap_last_climb_t) < _SWAP_CLIMB_STABLE_S:   # 刚爬上来就被打下 → 爬早了，退避翻倍
            _swap_climb_backoff = min(_SWAP_BACKOFF_CAP, _swap_climb_backoff * 2)
    else:                              # 升档：永远单档、保守恢复(防升太急又立刻过载来回抖档)
        nxt_idx = min(t_idx, c_idx + 1)
        _swap_last_climb_t = now
    nxt = order[nxt_idx]
    if nxt == cur:
        return
    _apply_preset_values(nxt)
    _swap_auto_reason = (("时延 %.0fms>%.0f → 降到 %s 保实时" % (lat, SWAP_AUTO_DOWN_MS, nxt)) if want < 0
                         else ("时延 %.0fms<%.0f → 升回 %s" % (lat, SWAP_AUTO_UP_MS, nxt)))
    print("[Swap/Auto] %s → %s  (lat=%.0fms, target=%s, steps=%d, backoff=%dx)"
          % (cur, nxt, lat, target, abs(nxt_idx - c_idx), _swap_climb_backoff), flush=True)


_ENGINE_HEALTH_URL = os.environ.get("FACESWAP_HEALTH_URL", FACESWAP_API.replace("/faceswap", "/health"))
_eng_cache = {"ts": 0.0, "data": None}


def _engine_health() -> dict:
    """代理 faceswap_api 的 /health，抽取「实际生效」的 GPU/CPU/TRT 真相。带 3s 缓存，
    避免控制页轮询频繁打探换脸服务。best-effort：探测失败返回 ok=False（前端显示引擎离线）。"""
    now = time.time()
    if _eng_cache["data"] is not None and now - _eng_cache["ts"] < 3.0:
        return _eng_cache["data"]
    try:
        j = requests.get(_ENGINE_HEALTH_URL, timeout=1.5).json()
        active = j.get("swap_providers_active") or []
        d = {"ok": True, "model": j.get("swap_model", ""), "backend": j.get("execution_backend", ""),
             "providers_active": active, "cpu_only": bool(j.get("swap_cpu_only")),
             "trt": bool(j.get("trt_enabled")),
             "gpu": any(("CUDA" in p or "Tensorrt" in p) for p in active),
             "enh_concurrency": j.get("enh_concurrency"),
             "enh_pool": j.get("enh_pool") or {}}
    except Exception as e:
        d = {"ok": False, "error": str(e)[:80]}
    _eng_cache["ts"] = now
    _eng_cache["data"] = d
    return d


def _swap_status() -> dict:
    """当前预设 + 逐帧参数 + 实时统计 + 换脸引擎(GPU/CPU/TRT)真相。"""
    return {
        "preset": SWAP_PRESET or "natural",
        "presets": list(_SWAP_PRESETS),
        "params": {"proc_w": SWAP_PROC_W, "enhance": SWAP_ENHANCE, "smooth": round(SWAP_SMOOTH, 2),
                   "sharpen": round(SWAP_SHARPEN, 2), "swap_fps": SWAP_FPS, "jpeg_q": JPEG_QUALITY,
                   "out_jpeg_q": OUT_JPEG_QUALITY, "fps_cap": _USER_SWAP_FPS_CAP,
                   "pinned": sorted(_USER_PIN), "workers": SWAP_WORKERS,
                   "main_face": SWAP_MAIN_FACE, "away_style": SWAP_AWAY_STYLE,
                   "away_text": SWAP_AWAY_TEXT, "away_image": SWAP_AWAY_IMAGE},
        "crop": {"on": SWAP_CROP, "active": _crop_active, "rect": _crop_rect,
                 "hits": _crop_hits, "miss": _crop_miss},
        "stats": {"ok": _swap_ok, "fail": _swap_fail, "fps": round(_fps_actual, 1),
                  "latency_ms": round(_swap_latency_avg, 1)},
        "auto": {"enabled": SWAP_AUTO, "target": _swap_target_preset,
                 "effective": SWAP_PRESET or "natural", "reason": _swap_auto_reason,
                 "down_ms": SWAP_AUTO_DOWN_MS, "up_ms": SWAP_AUTO_UP_MS},
        "engine": _engine_health(),
        "failover": {"degraded": _fs_degraded,
                     "degrade_reason": ("replica_no_enhance" if _fs_degraded else ""),
                     "since_ts": round(_fs_degraded_ts, 1) if _fs_degraded else 0},
        "bg": (_bg.status() if _bg else {"enabled": False, "mode": "none",
                                         "error": "bg_replace 模块不可用"}),
        "output": _output_status(),
    }


def _swap_quality_check() -> dict:
    """画质体检(2026-07-06 P1)：取最近成功换脸的脸区 bbox，对比 换脸后 vs 原始 同区清晰度
    (Laplacian 方差)与亮度，产出一句「可执行」建议。只在用户点按钮时算一次(两个 Laplacian ~ms 级)。"""
    with _frame_lock:
        swapped = None if _swapped_frame is None else _swapped_frame.copy()
        raw     = None if _raw_frame is None else _raw_frame.copy()
        boxes   = list(_last_face_boxes)
        box_ts  = _last_face_boxes_ts
    if swapped is None or raw is None:
        return {"ok": False, "advice": "画面未就绪：请先开播并确认摄像头有画面"}
    if not boxes or time.time() - box_ts > 10:
        return {"ok": False, "advice": "最近 10 秒没有成功换脸：把脸对准镜头、拉近距离、确保光线充足后再体检"}
    x1, y1, x2, y2 = boxes[0]
    h, w = swapped.shape[:2]
    x1, y1 = max(0, min(x1, w - 1)), max(0, min(y1, h - 1))
    x2, y2 = max(x1 + 1, min(x2, w)), max(y1 + 1, min(y2, h))
    if x2 - x1 < 16 or y2 - y1 < 16:
        return {"ok": False, "advice": "脸区过小无法评估：请拉近镜头再试"}
    crop_s = cv2.cvtColor(swapped[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    var_s = float(cv2.Laplacian(crop_s, cv2.CV_64F).var())
    rh, rw = raw.shape[:2]
    if (rw, rh) == (w, h):
        crop_r = cv2.cvtColor(raw[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    else:   # 竖屏切换瞬间等分辨率不一致：按比例映射同一脸区
        sx, sy = rw / float(w), rh / float(h)
        crop_r = cv2.cvtColor(raw[int(y1 * sy):int(y2 * sy), int(x1 * sx):int(x2 * sx)], cv2.COLOR_BGR2GRAY)
    var_r = float(cv2.Laplacian(crop_r, cv2.CV_64F).var()) if crop_r.size else 0.0
    brightness = float(np.mean(crop_r)) if crop_r.size else 0.0
    retention = int(round(var_s / var_r * 100)) if var_r > 1.0 else None
    # 嘴部子评分(P2)：脸框下 1/3 带(口型区)。口型糊是观感第一短板，单独量化
    mouth_ret = None
    try:
        my1 = y1 + (y2 - y1) * 2 // 3
        if y2 - my1 >= 12 and crop_r.shape[:2] == crop_s.shape[:2]:
            m_s = float(cv2.Laplacian(crop_s[my1 - y1:, :], cv2.CV_64F).var())
            m_r = float(cv2.Laplacian(crop_r[my1 - y1:, :], cv2.CV_64F).var())
            if m_r > 1.0:
                mouth_ret = int(round(m_s / m_r * 100))
    except Exception:
        pass
    eff = SWAP_PRESET or "natural"
    tgt = _swap_target_preset if SWAP_AUTO else eff
    _lb = {"eco": "省电", "natural": "自然", "beauty": "美颜", "hd": "高清"}
    # 建议规则：按「对画质影响从大到小」出第一条命中的可执行建议
    if brightness < 55:
        advice, level = "光线偏暗（脸区亮度 %d/255）：补光后检测更稳、脸部细节明显更好" % int(brightness), "warn"
    elif (x2 - x1) < 140:
        advice, level = "脸太小（约 %dpx 宽）：拉近镜头/靠近手机，脸区像素翻倍清晰度立涨" % (x2 - x1), "warn"
    elif tgt != "hd":
        advice, level = "当前目标档「%s」：切「高清脸」预设(512px+精修)，脸部清晰度预计 +40~60%%" % _lb.get(tgt, tgt), "warn"
    elif eff != tgt:
        advice, level = "目标高清但暂被自适应压到「%s」保流畅：负载回落会自动升回，无需操作" % _lb.get(eff, eff), "warn"
    elif mouth_ret is not None and retention is not None and mouth_ret < 55 and mouth_ret < retention - 20:
        # 嘴部显著差于整脸时先给精确诊断，别被整体均值淹没(口型区是观感第一短板)
        advice, level = "嘴部清晰度偏低（%d%%，明显低于整脸）：口型区是观感短板，确认高清档+精修生效；说话时属正常波动" % mouth_ret, "warn"
    elif retention is not None and retention < 70:
        advice, level = "换脸引擎损失偏大（保留率 %d%%）：确认精修在生效（看引擎状态条），或把画布维持 720p 减少放大稀释" % retention, "warn"
    else:
        advice, level = ("画质健康：脸部清晰度保留率 %s，当前已是较优配置" %
                         (("%d%%" % retention) if retention is not None else "正常")), "good"
    return {"ok": True, "retention": retention, "mouth_retention": mouth_ret,
            "sharp_swapped": round(var_s, 1), "sharp_raw": round(var_r, 1),
            "brightness": int(brightness), "face_w": x2 - x1, "face_h": y2 - y1,
            "frame_w": w, "frame_h": h,   # D-1 设备体检需按帧高算脸占比(720/1080 口径统一)
            "effective": eff, "target": tgt, "enhance": SWAP_ENHANCE,
            "crop_active": _crop_active,
            "advice": advice, "level": level}


def _swap_stats_snapshot(now, fps, real_fps, win_ok, win_fail, win_secs) -> dict:
    """一份时间点快照：实时 fps/真新帧 fps/换脸时延(均值 EWMA + 本窗峰值)/近窗成功失败/生效档/目标档/
    引擎是否掉 CPU。引擎真相取 _engine_health 缓存(不新发探测)。读取后清零本窗峰值(下窗重新累积)。"""
    global _swap_latency_wmax
    eng = _eng_cache.get("data") or {}
    lat_max = _swap_latency_wmax or _swap_latency_avg   # 本窗无新样本→退回 EWMA，避免 0 干扰分位
    _swap_latency_wmax = 0.0
    return {"ts": round(now, 1), "fps": round(fps, 1), "real_fps": round(real_fps, 1),
            "latency_ms": round(_swap_latency_avg, 1), "lat_max": round(lat_max, 1),
            "win_ok": int(win_ok), "win_fail": int(win_fail), "win_secs": round(win_secs, 2),
            "effective": SWAP_PRESET or "natural", "target": _swap_target_preset, "auto": SWAP_AUTO,
            "cpu_only": (bool(eng.get("cpu_only")) if eng.get("ok") else None)}


def _pctl(vals, q: float):
    """近似分位数(nearest-rank，无 numpy 依赖)：q∈[0,1]。空→None。用于时延 p95 抓尾部卡顿。"""
    xs = sorted(v for v in vals if isinstance(v, (int, float)))
    if not xs:
        return None
    import math
    k = min(len(xs) - 1, max(0, int(math.ceil(q * len(xs)) - 1)))
    return xs[k]


def _swap_stats_summary(trend) -> dict:
    """从趋势窗算汇总：出帧均值/最低(抓吞吐塌陷) + 时延均值/p95/峰值(抓偶发卡顿) + 降档运行占比
    (自适应下生效档低于目标档的时间比，反映硬件吃紧程度)。时延分位优先用每窗峰值 lat_max(退回
    latency_ms)，故 p95/峰值反映真实单帧尾时延而非被 EWMA 抹平。降档占比只在开自适应的快照上算。"""
    if not trend:
        return {"n": 0}
    fps = [s.get("fps") for s in trend if isinstance(s.get("fps"), (int, float))]
    lat_avg = [s.get("latency_ms") for s in trend if isinstance(s.get("latency_ms"), (int, float))]
    lat_tail = [(s.get("lat_max") if isinstance(s.get("lat_max"), (int, float)) else s.get("latency_ms"))
                for s in trend]
    lat_tail = [v for v in lat_tail if isinstance(v, (int, float))]
    def _avg(a):
        return round(sum(a) / len(a), 1) if a else None
    p95 = _pctl(lat_tail, 0.95)
    # 降档运行占比：仅在开了自适应且生效/目标档均已知的快照上算(自适应关→档恒等于目标→该指标无意义)
    cons = [s for s in trend if s.get("auto") and s.get("effective") in _SWAP_ORDER and s.get("target") in _SWAP_ORDER]
    below_pct = None
    if cons:
        b = sum(1 for s in cons if _SWAP_ORDER.index(s["effective"]) < _SWAP_ORDER.index(s["target"]))
        below_pct = round(100.0 * b / len(cons))
    return {"n": len(trend), "fps_avg": _avg(fps), "fps_min": (round(min(fps), 1) if fps else None),
            "lat_avg": _avg(lat_avg), "lat_p95": (round(p95, 1) if p95 is not None else None),
            "lat_max": (round(max(lat_tail), 1) if lat_tail else None), "below_target_pct": below_pct}


def _swap_recommended_start():
    """从近期趋势推荐启动档 = 生效档众数(自适应稳定停留最多的档)。无趋势/无自适应样本→None。
    供 SWAP_AUTO_REMEMBER 启动时避开开场掉帧(直接从本机可持续档起)。"""
    from collections import Counter
    cnt = Counter(s.get("effective") for s in _swap_trend if s.get("effective") in _SWAP_ORDER)
    return cnt.most_common(1)[0][0] if cnt else None


def _swap_stats_load() -> dict:
    """读 logs/swap_stats.json：{trend:[...], recommended_start, updated_at}。缺失/坏→空骨架。"""
    try:
        with open(SWAP_STATS_PATH, "r", encoding="utf-8") as f:
            j = json.load(f)
        if isinstance(j, dict):
            j.setdefault("trend", [])
            return j
    except Exception:
        pass
    return {"trend": [], "recommended_start": None}


def _swap_stats_persist():
    """把内存趋势 + 推荐启动档原子落盘(每快照一次，30s 级低频、体积小)。best-effort。"""
    try:
        data = {"updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "count": len(_swap_trend),
                "recommended_start": _swap_recommended_start(), "trend": list(_swap_trend)}
        os.makedirs(os.path.dirname(SWAP_STATS_PATH), exist_ok=True)
        tmp = SWAP_STATS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SWAP_STATS_PATH)
    except Exception:
        pass


def _swap_stats_payload(n: int = 60) -> dict:
    """/swap/stats 载荷：趋势(最近 n) + 汇总(均值/最低fps·均值/p95/峰值时延) + 最新快照 + 推荐启动档 +
    近窗掉 CPU 次数。汇总在返回窗上算(与前端 sparkline 同一窗)，一处真相供 /ops 与将来告警共用。"""
    trend = list(_swap_trend)
    win = trend[-max(1, n):]
    cpu_cnt = sum(1 for s in win if s.get("cpu_only") is True)
    return {"ok": True, "on": SWAP_STATS_ON, "count": len(trend),
            "recommended_start": _swap_recommended_start(), "remember_on": SWAP_AUTO_REMEMBER,
            "cpu_only_snaps": cpu_cnt, "recent": (trend[-1] if trend else {}),
            "summary": _swap_stats_summary(win), "trend": win}


def _swap_apply_remembered_start():
    """SWAP_AUTO_REMEMBER 开：若持久化推荐档在 [eco..target] 内且 != 当前，则从该档起(避开开场掉帧)。"""
    if not (SWAP_AUTO and SWAP_AUTO_REMEMBER):
        return
    rec = _swap_stats_load().get("recommended_start")
    if rec in _SWAP_ORDER and rec in _SWAP_PRESETS:
        t = _swap_target_preset if _swap_target_preset in _SWAP_ORDER else _SWAP_ORDER[-1]
        if _SWAP_ORDER.index(rec) <= _SWAP_ORDER.index(t) and rec != (SWAP_PRESET or "natural"):
            _apply_preset_values(rec)
            print(f"[Swap/Remember] 依上次趋势从 {rec} 档起 (target={_swap_target_preset})", flush=True)


def _swap_stats_init():
    """启动：从盘回灌趋势(跨重启连续) + (opt-in)依推荐档起。SWAP_STATS 关→跳过。"""
    if not SWAP_STATS_ON:
        return
    for s in _swap_stats_load().get("trend", []):
        if isinstance(s, dict):
            _swap_trend.append(s)
    _swap_apply_remembered_start()


# 换脸实时控制页：预览流 + 一键切预设 + 引擎(GPU/CPU/TRT)/参数/性能可视。手机浏览器可用。
_CTRL_PAGE = r"""<!DOCTYPE html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>FaceSwap Live · 控制台</title>
<style>
 body{margin:0;background:#000;overflow:hidden;font-family:"Microsoft YaHei UI","Segoe UI",sans-serif}
 #v{position:fixed;inset:0;display:flex;justify-content:center;align-items:center}
 #v img{max-height:100vh;max-width:100vw;object-fit:contain}
 #ctl{position:fixed;left:10px;top:10px;z-index:9;background:rgba(14,18,26,.82);
   border:1px solid rgba(255,255,255,.14);border-radius:12px;padding:10px 12px;color:#e8ecf6;
   -webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);max-width:min(92vw,340px)}
 #ctl.mini{padding:7px 10px}
 #ctl.mini .body{display:none}
 .hdr{display:flex;align-items:center;gap:8px;justify-content:space-between}
 .hdr b{font-size:13px}
 .presets{display:flex;gap:6px;margin:10px 0 8px;flex-wrap:wrap}
 .presets button{flex:1;min-width:60px;background:#232a3a;color:#cdd6ea;border:1px solid transparent;
   border-radius:8px;padding:8px 6px;font-size:12px;font-weight:700;cursor:pointer}
 .presets button.on{background:linear-gradient(135deg,#4f7aff,#a855f7);color:#fff}
 .ratios{display:flex;gap:5px;margin:6px 0 4px;flex-wrap:wrap}
 .ratios button{flex:1;min-width:72px;background:#1a2233;color:#b8c4dc;border:1px solid rgba(255,255,255,.1);
   border-radius:8px;padding:7px 4px;font-size:11px;font-weight:600;cursor:pointer}
 .ratios button.on{background:linear-gradient(135deg,#22c55e,#16a34a);color:#fff}
 .kv{color:#9fb0cc;font-size:11.5px;line-height:1.75}
 .chip{display:inline-block;padding:1px 8px;border-radius:999px;font-weight:700;font-size:11px}
 .chip.gpu{background:rgba(52,211,153,.18);color:#34d399}
 .chip.cpu{background:rgba(248,113,113,.2);color:#f87171}
 .chip.trt{background:rgba(96,165,250,.2);color:#60a5fa;margin-left:4px}
 .chip.off{background:rgba(148,163,184,.2);color:#94a3b8}
 #tgl{cursor:pointer;background:none;border:none;color:#9fb0cc;font-size:15px;padding:0 2px}
</style></head><body>
<div id="v"><img src="/stream" alt="live"/></div>
<div id="ctl">
  <div class="hdr"><b>换脸预设</b><span id="eng"><span class="chip off">…</span></span><button id="tgl">▾</button></div>
  <div class="body"><div style="font-size:11px;color:#8b9ab8;margin-bottom:4px">输出比例</div>
   <div class="ratios" id="ar"></div>
   <div style="font-size:11px;color:#8b9ab8;margin:8px 0 4px">换脸预设</div>
   <div class="presets" id="ps"></div><div class="kv" id="kv"></div></div>
</div>
<script>
 var LABEL={natural:'自然',beauty:'美颜',hd:'高清',eco:'省电'};
 var AR={portrait916:'9:16竖屏',portrait34:'3:4竖屏',landscape169:'16:9',landscape169hd:'16:9高清',square11:'1:1'};
 var $=function(s){return document.querySelector(s)}, cur=null, arCur=null;
 function chipEng(e){
   if(!e||!e.ok) return '<span class="chip cpu">引擎离线</span>';
   var g=e.cpu_only?'<span class="chip cpu">CPU</span>':(e.gpu?'<span class="chip gpu">GPU</span>':'<span class="chip off">?</span>');
   return g+(e.trt?'<span class="chip trt">TRT</span>':'');
 }
 function render(st){
   if(!st) return; cur=st.preset;
   var o=st.output||{};
   arCur=o.preset;
   var ar=$('#ar'); ar.innerHTML='';
   Object.keys(o.presets||AR).forEach(function(k){
     var b=document.createElement('button'); b.textContent=AR[k]||(o.presets[k]||k);
     b.className=(k==arCur?'on':''); b.onclick=function(){ setAspect(k); }; ar.appendChild(b);
   });
   var ps=$('#ps'); ps.innerHTML='';
   (st.presets||[]).forEach(function(n){
     var b=document.createElement('button'); b.textContent=LABEL[n]||n; b.className=(n==cur?'on':'');
     b.onclick=function(){ setPreset(n); }; ps.appendChild(b);
   });
   var p=st.params||{}, s=st.stats||{};
   var au=st.auto||{};
   $('#kv').innerHTML='输出 '+(o.width||'?')+'×'+(o.height||'?')+' '+(o.label||'')
     +'<br>宽 '+p.proc_w+'px · 上限 '+p.swap_fps+'fps · 增强 '+(p.enhance||'none')+' · 锐化 '+p.sharpen+' · JPEG '+p.jpeg_q
     +'<br>实时 '+(s.fps||0)+'fps · 换脸延迟 '+(s.latency_ms||0)+'ms · OK '+(s.ok||0)+' / Fail '+(s.fail||0)
     + (st.engine&&st.engine.model?(' · 模型 '+st.engine.model):'')
     + (au.enabled?('<br>负载自适应 · 目标 '+(au.target||'—')+' → 生效 '+(au.effective||'—')+(au.reason?(' · '+au.reason):'')):'');
   $('#eng').innerHTML=chipEng(st.engine);
 }
 function poll(){ fetch('/swap/status').then(function(r){return r.json()}).then(render).catch(function(){}); }
 function setPreset(n){ fetch('/swap/preset?name='+encodeURIComponent(n)).then(function(r){return r.json()})
   .then(function(j){ if(j&&j.status)render(j.status); else poll(); }).catch(function(){}); }
 function setAspect(n){ fetch('/output/aspect?name='+encodeURIComponent(n)).then(function(r){return r.json()})
   .then(function(j){ if(j&&j.ok)poll(); }).catch(function(){}); }
 $('#tgl').onclick=function(){ var c=$('#ctl'); c.classList.toggle('mini'); this.textContent=c.classList.contains('mini')?'▸':'▾'; };
 poll(); setInterval(poll,2500);
</script></body></html>"""


def _sharpen(img, amount):
    """轻量 unsharp mask 锐化：放大高频细节，提升换脸脸部观感清晰度。"""
    if amount <= 0 or img is None:
        return img
    blur = cv2.GaussianBlur(img, (0, 0), 2.0)
    return cv2.addWeighted(img, 1.0 + amount, blur, -amount, 0)


def _sharpen_for_swap(d: dict) -> float:
    """S8-2 容灾画质对齐：副本无 GFPGAN 时在 stream 侧抬高 unsharp（~1ms/帧，不占 GPU 显存）。
    beauty/hd 预设平时靠 gfpgan+0.35 锐化；副本期把锐化拉到 ~0.85 补偿大半清晰度落差。"""
    global _fs_degraded, _fs_degraded_ts
    if d.get("failover"):
        _fs_degraded = True
        _fs_degraded_ts = time.time()
        base = max(SWAP_SHARPEN, 0.45)
        return min(1.15, base * 1.55)
    if _fs_degraded:
        _fs_degraded = False
    return SWAP_SHARPEN


def _enqueue_swap(frame):
    """把最新帧塞入换脸队列；队列满则丢最旧帧，始终保留最新（带递增序号防乱序）。"""
    global _swap_seq
    _swap_seq += 1
    item = (_swap_seq, frame)
    try:
        _swap_queue.put_nowait(item)
    except Full:
        try:
            _swap_queue.get_nowait()
        except Empty:
            pass
        try:
            _swap_queue.put_nowait(item)
        except Full:
            pass

# 双缓冲 + crossfade
_prev_swapped = None      # 上一帧换脸结果
_curr_swapped = None      # 当前换脸结果
_swap_arrive_time = 0.0   # 当前帧到达时间
_swap_motion  = 0.0       # 新旧换脸帧的变化幅度(运动检测，用于自适应过渡)
_swap_gap_ema = 0.0       # 换脸帧到达间隔 EMA(秒)——动态钳制 crossfade 的依据(2026-07-09)
CROSSFADE_DURATION = float(os.environ.get("SWAP_CROSSFADE", "0.10"))  # crossfade过渡时长(秒)，短→不拖影
# 帧间变化超过此阈值视为"在晃动"，直接切到新帧而不做混合，消除运动幻影
# (口径=脸区平均像素差，见 _face_region_motion；2026-07-09 前为整帧口径)
MOTION_SNAP_THRESH = float(os.environ.get("SWAP_MOTION_SNAP", "10"))
# crossfade 参数护栏(2026-07-09 根治「过渡永不完成」类白斑)：
#   有效过渡时长 = min(CROSSFADE_DURATION, 钳制系数×换脸帧实际到达间隔)。
#   历史事故：--crossfade 0.4 + swap-fps 5(200ms 间隔) → alpha 永远到不了 1，
#   画面恒为新旧两帧 50/50 叠影，任何头动/光变都会显成"白斑/重影"。钳制后过渡
#   必在下一帧到达前完成；断供(间隔>SNAP 上限)直接切新帧，绝不拿陈旧帧混。
CROSSFADE_GAP_RATIO = float(os.environ.get("SWAP_CROSSFADE_GAP_RATIO", "0.6"))
CROSSFADE_STALE_GAP = float(os.environ.get("SWAP_CROSSFADE_STALE_GAP", "0.8"))  # 供帧间隔超此秒数=断供,直切


def _face_region_motion(prev, curr) -> float:
    """相邻两张换脸帧的运动幅度（crossfade 直切判定用）。
    白斑修复(2026-07-09)：只量脸框区域(最近回传框外扩30%)。旧口径整帧缩 64px 求均值，
    头部运动被大面积静止背景稀释成 2~8、永远低于 SWAP_MOTION_SNAP(10) → 头动时 crossfade
    照混，上一帧亮背景/额头透过深色眉眼 → 脸上一团白斑拖影（启动期换脸帧间隔最大最明显）。
    脸区口径下头动 15~50，直切生效；静止脸仍 ≪10，保留静态混帧的顺滑。
    无框/两帧尺寸不一 → 回退整帧口径(旧行为)。调用方须持 _frame_lock(读 _last_face_boxes)。"""
    try:
        box = _last_face_boxes[0] if _last_face_boxes else None
        if box is not None and prev.shape == curr.shape:
            h, w = curr.shape[:2]
            bw, bh = box[2] - box[0], box[3] - box[1]
            px, py = int(bw * 0.3), int(bh * 0.3)
            x1 = max(0, int(box[0]) - px); y1 = max(0, int(box[1]) - py)
            x2 = min(w, int(box[2]) + px); y2 = min(h, int(box[3]) + py)
            if x2 - x1 >= 16 and y2 - y1 >= 16:
                prev = prev[y1:y2, x1:x2]
                curr = curr[y1:y2, x1:x2]
        a = cv2.resize(prev, (64, 64))
        b = cv2.resize(curr, (64, 64))
        return float(np.mean(cv2.absdiff(a, b)))
    except Exception:
        return 0.0


def _away_badge_img():
    """预渲染离席角标(BGRA)：PIL+雅黑渲染 SWAP_AWAY_TEXT(06u 可自定义)；PIL/字体不可用回退
    英文 putText。缓存至文案热改(/swap/away 置 _away_badge=None 触发重渲染)；空文案=无角标。"""
    global _away_badge
    if _away_badge is not None:
        return _away_badge
    text = (SWAP_AWAY_TEXT or "").strip()
    if not text:
        _away_badge = np.zeros((0, 0, 4), np.uint8)      # 空角标=叠加环节自动跳过
        return _away_badge
    try:
        from PIL import Image, ImageDraw, ImageFont
        font = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 30)
        _m = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
        box = _m.textbbox((0, 0), text, font=font)
        tw, th = box[2] - box[0], box[3] - box[1]
        pad = 18
        img = Image.new("RGBA", (tw + pad * 2, th + pad * 2), (15, 18, 26, 205))
        ImageDraw.Draw(img).text((pad - box[0], pad - box[1]), text, font=font, fill=(235, 240, 248, 255))
        _away_badge = cv2.cvtColor(np.array(img), cv2.COLOR_RGBA2BGRA)
    except Exception:
        badge = np.zeros((64, 400, 4), np.uint8)
        badge[:, :, :3] = (26, 18, 15)
        badge[:, :, 3] = 205
        cv2.putText(badge, "Be right back", (24, 42), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (248, 240, 235, 255), 2)
        _away_badge = badge
    return _away_badge


def _away_brand_img(w, h):
    """品牌离席图(style=image)：SWAP_AWAY_IMAGE 按 cover 裁切填满 w×h(中文路径用 fromfile)。
    路径缓存,变更即重载；读不到/解码坏→None(调用方退 blur,绝不黑屏)。"""
    path = (SWAP_AWAY_IMAGE or "").strip()
    if not path:
        return None
    if _away_img_cache["path"] != path:
        _away_img_cache["path"] = path
        _away_img_cache["img"] = None
        try:
            raw = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
            _away_img_cache["img"] = raw
        except Exception:
            _away_img_cache["img"] = None
    src = _away_img_cache["img"]
    if src is None:
        return None
    sh, sw = src.shape[:2]
    scale = max(w / sw, h / sh)
    rw, rh = max(2, int(sw * scale)), max(2, int(sh * scale))
    img = cv2.resize(src, (rw, rh))
    x0, y0 = (rw - w) // 2, (rh - h) // 2
    return img[y0:y0 + h, x0:x0 + w]


def _away_frame(out, stale_s):
    """离席兜底画面(2026-07-06p 实测：主播离席 6 分钟观众看冻结帧以为卡死)：
    最后换好的一帧 → 渐进模糊(降采样重放大,廉价强模糊) + 轻压暗 + 底部角标。
    style=image(06u)→品牌图渐入替底(图坏自动退 blur)。
    真脸从不外泄(模糊的是换好的脸)；出脸恢复即刻回正常画面。不改写入参数组。"""
    ramp = min(1.0, max(0.0, (stale_s - SWAP_AWAY_AFTER) / 5.0))      # 超龄后 5s 推满
    h, w = out.shape[:2]
    brand = _away_brand_img(w, h) if SWAP_AWAY_STYLE == "image" else None
    if brand is not None:
        blur = brand if ramp >= 1.0 else cv2.addWeighted(out, 1.0 - ramp, brand, ramp, 0)
    else:
        k = 2 + int(ramp * 8)                                         # 模糊力度 2→10 倍降采样
        small = cv2.resize(out, (max(2, w // k), max(2, h // k)), interpolation=cv2.INTER_AREA)
        blur = cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)
        if ramp < 1.0:
            blur = cv2.addWeighted(out, 1.0 - ramp, blur, ramp, 0)    # 渐入
        blur = cv2.convertScaleAbs(blur, alpha=1.0 - 0.22 * ramp)     # 轻压暗
    b = _away_badge_img()
    bh, bw = b.shape[:2]
    if 0 < bw < w and 0 < bh < h:
        x, y = (w - bw) // 2, min(h - bh - 8, int(h * 0.78))
        roi = blur[y:y + bh, x:x + bw].astype(np.float32)
        a = (b[:, :, 3:4].astype(np.float32) / 255.0) * ramp
        blur[y:y + bh, x:x + bw] = (b[:, :, :3].astype(np.float32) * a + roi * (1.0 - a)).astype(np.uint8)
    return blur


def get_active_face_b64() -> str:
    """从 AvatarHub 获取当前激活角色的人脸 base64"""
    try:
        r = requests.get(f"{AVATARHUB_API}/profiles", timeout=2)
        d = r.json()
        active = d.get("active", "")
        if active:
            for p in d.get("profiles", []):
                pass
        # 直接从 profiles 详情获取
        if active:
            r2 = requests.get(f"{AVATARHUB_API}/profiles", timeout=2)
            raw = r2.json()
            active_name = raw.get("active", "")
            # 通过换脸 API 自动注入 —— 无需在这里获取
    except Exception:
        pass
    return ""


# ── 线程1：摄像头捕获 ────────────────────────────────────────────────
def _reselect_live_source(current_source):
    """数字摄像头索引连续打不开(手机虚拟摄像头未推流/被别的程序占用/索引失效)时，
    用统一选源逻辑(device_enum)挑一个"能出活帧"的源，避免死盯坏索引每 3s 刷 DSHOW 报错。
    返回新的 source(数字索引字符串或 URL)；无更优选择时返回 None。
    'scrcpy' 兜底不在此处处理(属另一条采集链路)，返回 None 让外层继续静默重试。"""
    try:
        import device_enum
        pick = device_enum.pick_camera_source(adb_has_device=False, probe=True)
        src = str(pick.get("source") or "")
        if src and src != "scrcpy" and src != str(current_source):
            print(f"[Capture] 自动重选源 → {src}  ({pick.get('reason')})", flush=True)
            return src
    except Exception as e:
        print(f"[Capture] 自动重选源失败: {e}", flush=True)
    return None


def capture_worker(source: int | str, width: int, height: int):
    """
    source: 初始摄像头索引或URL。支持热重载：检测到 _desired_cam_idx 变化时自动切换。
    """
    global _raw_frame, _running, _desired_cam_idx

    if source == "scrcpy":
        _capture_scrcpy(width, height)
        return

    is_url = not str(source).isdigit()
    current_source = source
    open_fail_streak = 0                # 连续打开失败次数：达阈值后自动重选"能出活帧"的源

    # 外层循环：支持摄像头热重载
    while _running:
        # 检查是否需要切换摄像头
        if not is_url and _desired_cam_idx != -1 and _desired_cam_idx != int(current_source):
            current_source = str(_desired_cam_idx)
            open_fail_streak = 0
            print(f"[Capture] Hot-reload: switching to camera {current_source}")

        # 初始化摄像头
        if is_url:
            cap = cv2.VideoCapture(current_source)
            print(f"[Capture] HTTP stream isOpened={cap.isOpened()}  url={current_source}")
        else:
            cap = cv2.VideoCapture(int(current_source), cv2.CAP_DSHOW)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH,  width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            cap.set(cv2.CAP_PROP_FPS, DEFAULT_FPS)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            print(f"[Capture] Local cam #{current_source}  isOpened={cap.isOpened()}")

        if not cap.isOpened():
            cap.release()
            open_fail_streak += 1
            # 数字索引连续打不开(手机虚拟摄像头未推流/被占用/索引失效)：改用"能出活帧"的源，
            # 不再每 3s 死盯同一个坏索引刷 DSHOW 报错。同步把热重载目标设为新索引，
            # 避免外层"热重载检查"又把它切回坏索引造成来回抖动。
            if not is_url and open_fail_streak >= 2:
                new_src = _reselect_live_source(current_source)
                if new_src is not None:
                    current_source = new_src
                    is_url = not str(current_source).isdigit()
                    if not is_url:
                        _desired_cam_idx = int(current_source)
                    open_fail_streak = 0
                    continue
            print(f"[Capture] ERROR: cannot open {current_source}, retrying in 3s...", flush=True)
            time.sleep(3)
            continue

        open_fail_streak = 0
        print(f"[Capture] Ready  {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))}")
        try:
            _cam_status_idx = int(current_source)
        except (ValueError, TypeError):
            _cam_status_idx = -1                 # URL 源(手机 WiFi MJPEG)无整数索引，避免 int() 崩溃采集线程
        _write_camera_status(_cam_status_idx, True, "running")  # 通知Hub切换成功
        fail_streak = 0
        last_swap_t = 0.0

        # 内层循环：读取帧，同时检查摄像头切换请求
        while _running:
            # 热重载检查（仅限本地摄像头，非URL）
            if not is_url and _desired_cam_idx != -1 and _desired_cam_idx != int(current_source):
                print(f"[Capture] Camera switch requested: {current_source} → {_desired_cam_idx}")
                break  # 跳出内层循环，重新初始化

            ret, frame = cap.read()
            if not ret or frame is None:
                fail_streak += 1
                if fail_streak % 60 == 1:
                    print(f"[Capture] read fail #{fail_streak}, reconnecting...")
                    break  # 重连时也跳出内层循环
                time.sleep(0.05)
                continue
            fail_streak = 0
            with _frame_lock:
                _raw_frame = frame
            now = time.time()
            if now - last_swap_t >= SWAP_MIN_INTERVAL:
                _enqueue_swap(frame.copy())
                last_swap_t = now
            time.sleep(1.0 / DEFAULT_FPS)

        cap.release()
        time.sleep(0.5)  # 释放后短暂停顿再重试/切换


def _hwnd_capture(hwnd) -> np.ndarray | None:
    """截取窗口画面 — 用 PrintWindow(PW_CLIENTONLY|PW_RENDERFULLCONTENT) 直接取窗口内容。
    不依赖窗口可见/置顶/不被遮挡/不最小化（scrcpy 以 --render-driver software 运行，PrintWindow 有效）。
    旧实现用"屏幕坐标 BitBlt"：窗口被别的窗口盖住、挪到屏外或最小化时会截成黑帧，
    继而退回 _adb_screencap() 抓到手机桌面（而非摄像头）。改 PrintWindow 后彻底规避。"""
    import ctypes, ctypes.wintypes
    user32 = ctypes.windll.user32
    gdi32  = ctypes.windll.gdi32

    r = ctypes.wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    w, h = r.right, r.bottom
    if w < 10 or h < 10:
        return None

    hdc_win = user32.GetDC(hwnd)
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    hbmp    = gdi32.CreateCompatibleBitmap(hdc_win, w, h)
    gdi32.SelectObject(hdc_mem, hbmp)
    # 3 = PW_CLIENTONLY(1) | PW_RENDERFULLCONTENT(2)：只取客户区(去标题栏)+支持现代渲染
    ok = user32.PrintWindow(hwnd, hdc_mem, 3)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize",         ctypes.c_uint32),
                    ("biWidth",        ctypes.c_int32),
                    ("biHeight",       ctypes.c_int32),
                    ("biPlanes",       ctypes.c_uint16),
                    ("biBitCount",     ctypes.c_uint16),
                    ("biCompression",  ctypes.c_uint32),
                    ("biSizeImage",    ctypes.c_uint32),
                    ("biXPelsPerMeter",ctypes.c_int32),
                    ("biYPelsPerMeter",ctypes.c_int32),
                    ("biClrUsed",      ctypes.c_uint32),
                    ("biClrImportant", ctypes.c_uint32)]
    bih = BITMAPINFOHEADER(40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = (ctypes.c_byte * (w * h * 4))()
    gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bih), 0)

    gdi32.DeleteObject(hbmp)
    gdi32.DeleteDC(hdc_mem)
    user32.ReleaseDC(hwnd, hdc_win)

    if not ok:
        return None
    arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, w, 4)
    return cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)


def _adb_screencap() -> np.ndarray | None:
    adb = rf"{_BASE}\scrcpy\scrcpy-win64-v3.1\adb.exe"
    try:
        r = subprocess.run([adb, "exec-out", "screencap", "-p"],
                           capture_output=True, timeout=2,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0 or not r.stdout:
            return None
        arr = np.frombuffer(r.stdout, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return frame
    except Exception:
        return None


def _fit_frame(frame: np.ndarray, width: int, height: int) -> np.ndarray:
    h, w = frame.shape[:2]
    if w < 2 or h < 2:
        return np.zeros((height, width, 3), dtype=np.uint8)
    # cover 模式：填满目标画布，裁切多余部分，不留黑边
    scale = max(width / w, height / h)
    nw, nh = max(int(w * scale), width), max(int(h * scale), height)
    frame = cv2.resize(frame, (nw, nh))
    # 居中裁切
    x = max((nw - width) // 2, 0)
    y = max((nh - height) // 2, 0)
    return frame[y:y + height, x:x + width].copy()


def _crop_scrcpy_content(frame: np.ndarray) -> np.ndarray:
    mask_white = np.all(frame > 245, axis=2)
    mask_black = np.all(frame < 8, axis=2)
    mask = ~(mask_white | mask_black)
    ys, xs = np.where(mask)
    if xs.size < 1000 or ys.size < 1000:
        return frame
    x1, x2 = int(xs.min()), int(xs.max()) + 1
    y1, y2 = int(ys.min()), int(ys.max()) + 1
    if (x2 - x1) < 20 or (y2 - y1) < 20:
        return frame
    return frame[y1:y2, x1:x2]


def _crop_black_border(frame: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    mask = gray > 8
    ys, xs = np.where(mask)
    if xs.size < 1000 or ys.size < 1000:
        return frame
    x1, x2 = int(xs.min()), int(xs.max()) + 1
    y1, y2 = int(ys.min()), int(ys.max()) + 1
    if (x2 - x1) < 20 or (y2 - y1) < 20:
        return frame
    return frame[y1:y2, x1:x2]


def _capture_scrcpy(width: int, height: int):
    """捕获 scrcpy 窗口画面（使用 PrintWindow，不受窗口遮挡影响）"""
    import ctypes
    global _raw_frame, _running
    user32 = ctypes.windll.user32
    print("[Capture] Waiting for scrcpy window 'PhoneCam'...")

    last_swap_t = 0
    hwnd = 0
    # 手机摄像头流刚连上时会有多镜头切换/多画面过渡帧，跳过这段时间不取流
    SCRCPY_WARMUP = 3.0
    warmup_until = 0.0
    while _running:
        if not hwnd:
            hwnd = user32.FindWindowW(None, "PhoneCam") or user32.FindWindowW(None, "scrcpy")
            if not hwnd:
                time.sleep(0.5)
                continue
            print(f"[Capture] Found window hwnd={hwnd}")
            warmup_until = time.time() + SCRCPY_WARMUP

        try:
            frame = _hwnd_capture(hwnd)
            if frame is None or frame.mean() < 3:
                frame = _adb_screencap()
            if frame is None:
                hwnd = 0
                time.sleep(0.3)
                continue
            # 摄像头预热期：仅显示占位提示，不把过渡帧推入取流/换脸
            if time.time() < warmup_until:
                blank = np.zeros((height, width, 3), dtype=np.uint8)
                cv2.putText(blank, "Camera warming up...", (8, height // 2 - 10),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 255), 1, cv2.LINE_AA)
                with _frame_lock:
                    _raw_frame = blank
                time.sleep(0.1)
                continue
            if frame.mean() < 3:
                frame = np.zeros((height, width, 3), dtype=np.uint8)
                cv2.putText(frame, "SCRCPY/ADB FRAME IS BLACK", (8, height // 2 - 20),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.42, (0, 0, 255), 1, cv2.LINE_AA)
                cv2.putText(frame, "Unlock phone / close camera app / allow capture", (8, height // 2 + 5),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.34, (0, 255, 255), 1, cv2.LINE_AA)
                with _frame_lock:
                    _raw_frame = frame
                time.sleep(0.2)
                continue

            frame = _crop_scrcpy_content(frame)
            frame = _fit_frame(frame, width, height)
            with _frame_lock:
                _raw_frame = frame
            now = time.time()
            if now - last_swap_t >= SWAP_MIN_INTERVAL:
                _enqueue_swap(frame.copy())
                last_swap_t = now
        except Exception as e:
            print(f"[Capture] {e}")
            hwnd = 0
        time.sleep(1.0 / DEFAULT_FPS)


# ── 脸区裁剪通道：几何/羽化/回退 helpers ────────────────────────────────
def _crop_expand_box(box, w, h):
    """脸框外扩 SWAP_CROP_MARGIN + 16px 对齐 + 夹取画面边界 → 裁剪窗。过小(<96px)返回 None。"""
    x1, y1, x2, y2 = box
    bw, bh = x2 - x1, y2 - y1
    if bw <= 0 or bh <= 0:
        return None
    mx, my = bw * SWAP_CROP_MARGIN, bh * SWAP_CROP_MARGIN
    x1 = max(0, int(x1 - mx) // 16 * 16)
    y1 = max(0, int(y1 - my) // 16 * 16)
    x2 = min(w, -(-int(x2 + mx) // 16) * 16)
    y2 = min(h, -(-int(y2 + my) // 16) * 16)
    if x2 - x1 < 96 or y2 - y1 < 96:
        return None
    return (x1, y1, x2, y2)


def _crop_pick_rect(now, w, h):
    """决定本帧是否走裁剪通道。返回裁剪窗或 None(走全帧)。
    滞回：脸框(留半边距余量)仍在旧窗内且脸没显著变小 → 沿用旧窗，稳住引擎输入取景。"""
    global _crop_rect
    if not SWAP_CROP or now < _crop_block_until:
        return None
    with _frame_lock:
        boxes = list(_last_face_boxes)
        ts = _last_face_boxes_ts
    if len(boxes) != 1 or now - ts > SWAP_CROP_STALE_S:
        return None
    box = boxes[0]
    r = _crop_rect
    if r and r[2] <= w and r[3] <= h:
        bw, bh = box[2] - box[0], box[3] - box[1]
        gx, gy = bw * SWAP_CROP_MARGIN * 0.5, bh * SWAP_CROP_MARGIN * 0.5
        inside = (box[0] - gx >= r[0] and box[1] - gy >= r[1] and
                  box[2] + gx <= r[2] and box[3] + gy <= r[3])
        # 脸显著变小(远离镜头)→窗口过大浪费送检像素，重取景
        shrunk = bw < (r[2] - r[0]) * 0.30
        if inside and not shrunk:
            return r
    _crop_rect = _crop_expand_box(box, w, h)
    return _crop_rect


def _crop_face_lost(now):
    """裁剪窗内检不到脸：清框+冷却，回全帧重新发现。只在裁剪路径 miss 时调用
    (全帧/探测帧的偶发漏检不清框——裁剪里的大脸比全帧小脸更好检，别让弱信号杀稳定通道)。"""
    global _crop_rect, _crop_block_until, _crop_miss, _last_face_boxes, _last_face_boxes_ts, _crop_active
    with _frame_lock:
        _last_face_boxes = []
        _last_face_boxes_ts = 0.0
    _crop_rect = None
    _crop_active = False
    _crop_block_until = now + 1.5
    _crop_miss += 1


def _feather_mask(cw, ch, f=12):
    """裁剪回贴的边缘羽化掩码(线性坡)，按尺寸缓存；16px 对齐+滞回让尺寸种类很少。"""
    m = _feather_masks.get((cw, ch))
    if m is None:
        f = min(f, cw // 4, ch // 4)
        m = np.ones((ch, cw), np.float32)
        if f > 0:
            ramp = np.linspace(0.0, 1.0, f, endpoint=False, dtype=np.float32)
            m[:, :f] *= ramp[None, :]
            m[:, cw - f:] *= ramp[::-1][None, :]
            m[:f, :] *= ramp[:, None]
            m[ch - f:, :] *= ramp[::-1][:, None]
        m = m[:, :, None]
        if len(_feather_masks) > 8:
            _feather_masks.clear()
        _feather_masks[(cw, ch)] = m
    return m


def _crop_composite(frame, result_crop, rect):
    """换好的裁剪块羽化回贴到原生帧：背景原生像素直出，贴缝无痕。"""
    x1, y1, x2, y2 = rect
    out = frame.copy()
    m = _feather_mask(x2 - x1, y2 - y1)
    base = out[y1:y2, x1:x2].astype(np.float32)
    out[y1:y2, x1:x2] = (result_crop.astype(np.float32) * m + base * (1.0 - m)).astype(np.uint8)
    return out


# ── 二进制直连通道（2026-07-09 传输提速；SWAP_API_RAW 非空才启用）────────────
_raw_off = [False]        # 引擎 404(未升级) → 本进程内永久回退 JSON，不再逐帧试探


def _swap_via_raw(payload: dict, jpg_bytes: bytes):
    """帧经引擎 /faceswap_raw 直连换脸：原始 JPEG 进出+元数据响应头。
    成功 → 返回与 JSON 通道同形的 d 字典(_raw_bytes 携带图字节)；
    引擎未升级(404) → 置 _raw_off 永久回退；其它失败 → None(本帧走 JSON 兜底)。"""
    try:
        params = {}
        for k in ("blend", "threshold", "smooth_alpha", "enhance", "source_key", "blend_mode", "min_face_px"):
            v = payload.get(k)
            if v not in (None, ""):
                params[k] = v
        if payload.get("occlusion") is not None:
            params["occlusion"] = "1" if payload["occlusion"] else "0"
        if payload.get("mouth_mask") is not None:
            params["mouth_mask"] = "1" if payload["mouth_mask"] else "0"
        if payload.get("mask_padding"):
            params["mask_padding"] = ",".join(str(x) for x in payload["mask_padding"])
        if payload.get("main_face_only") is not None:
            params["main_face_only"] = "1" if payload["main_face_only"] else "0"
        _h = payload.get("main_face_hint")
        if _h:
            params["main_face_hint"] = f"{_h[0]:.1f},{_h[1]:.1f}"
        try:
            _hdrs = app_config.service_headers({"Content-Type": "image/jpeg"})
        except Exception:
            _hdrs = {"Content-Type": "image/jpeg"}
        r = requests.post(f"{SWAP_RAW_BASE}/faceswap_raw", params=params,
                          data=jpg_bytes, headers=_hdrs, timeout=15)
        if r.status_code == 404:
            _raw_off[0] = True
            print("[Swap] 引擎无 /faceswap_raw(未升级) → 回退 JSON 通道", flush=True)
            return None
        ct = (r.headers.get("content-type") or "").lower()
        if r.status_code == 200 and ct.startswith("image/"):
            h = r.headers
            d = {"result_image": True, "_raw_bytes": r.content}
            for hk, dk in (("X-Elapsed-Ms", "elapsed_ms"), ("X-Faces-Tgt", "faces_tgt"),
                           ("X-Faces-Used", "faces_used"), ("X-Detect-Ms", "detect_ms"),
                           ("X-Swap-Ms", "swap_ms"), ("X-Enhance-Ms", "enhance_ms"),
                           ("X-Smooth-Ms", "smooth_ms")):
                try:
                    d[dk] = int(h.get(hk)) if h.get(hk) not in (None, "None", "") else None
                except Exception:
                    d[dk] = None
            try:
                d["faces_boxes"] = json.loads(h["X-Faces-Boxes"]) if h.get("X-Faces-Boxes") else None
            except Exception:
                d["faces_boxes"] = None
            return d
        if 400 <= r.status_code < 500:
            try:
                return r.json()       # 业务 4xx(如目标无脸)：同形透传,上层按"无脸"处理
            except Exception:
                return None
        return None                    # 5xx/异常形态 → 本帧回退 JSON 通道
    except Exception:
        return None                    # 网络异常 → 本帧回退 JSON 通道(其自带异常处理)


# ── 线程2：换脸处理 ──────────────────────────────────────────────────
def swap_worker(wid: int = 0):
    global _swapped_frame, _swap_time, _swap_ok, _swap_fail, _running, _prev_swapped, _curr_swapped, _swap_arrive_time, _applied_seq, _last_noface_log, _last_face_boxes, _last_face_boxes_ts
    print(f"[Swap] Swap thread #{wid} started")

    while _running:
        try:
            seq, frame = _swap_queue.get(timeout=1.0)
        except Empty:
            continue

        # 不在 worker 内再限速：供给端(capture)已按 SWAP_FPS 控速，
        # 多 worker 并发处理以抵消单帧往返延迟。

        h, w = frame.shape[:2]
        now = time.time()
        global _crop_probe_ts
        # 路径选择：裁剪(脸区原生像素) / 全帧(发现·回退) / 探测(裁剪跑久了发现新脸,不上屏)
        rect = _crop_pick_rect(now, w, h)
        probe = False
        if rect is not None and now - _crop_probe_ts > SWAP_CROP_PROBE_S:
            probe, rect = True, None
            _crop_probe_ts = now          # 决策即占位，防多 worker 同时探测
        crop_scale = 1.0
        if rect is not None:
            x1, y1, x2, y2 = rect
            crop = frame[y1:y2, x1:x2]
            cw0, ch0 = x2 - x1, y2 - y1
            # 送检长边上限随画质档 PROC_W 缩放：自适应降档对裁剪路径同样有效
            cap = max(256, min(SWAP_CROP_MAX, int(SWAP_CROP_MAX * SWAP_PROC_W / 512)))
            if max(cw0, ch0) > cap:
                crop_scale = cap / float(max(cw0, ch0))
                crop = cv2.resize(crop, (max(2, int(cw0 * crop_scale)), max(2, int(ch0 * crop_scale))))
            # 裁剪块小,抬高送检 q 保护贴缝与脸部细节,传输代价可忽略
            _, buf = cv2.imencode('.jpg', crop, [cv2.IMWRITE_JPEG_QUALITY, max(JPEG_QUALITY, 80)])
        else:
            # 全帧路径：限制为 SWAP_PROC_W 宽，显著减少 API 处理/传输时间
            proc_w = min(w, SWAP_PROC_W)
            proc_h = int(h * proc_w / w)
            small = cv2.resize(frame, (proc_w, proc_h))
            _, buf = cv2.imencode('.jpg', small, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        target_b64 = base64.b64encode(buf).decode()

        try:
            # 走 AvatarHub 代理 → 自动注入激活角色的脸
            payload = {"target_image": target_b64}
            # 可选换脸参数透传（后端/FaceSwap API 有默认值）
            if face_params.get("blend", 0) > 0:
                payload["blend"] = face_params["blend"]
            if face_params.get("threshold", 0) > 0:
                payload["threshold"] = face_params["threshold"]
            # 始终下发时域平滑系数(默认1.0=关混帧)，确保服务端重启后也不回退到幻影状态
            payload["smooth_alpha"] = face_params["smooth"] if face_params.get("smooth", 0) > 0 else SWAP_SMOOTH
            # 探测帧只为刷新脸框，不上屏：增强关掉省 ~200ms 引擎时间
            payload["enhance"] = "none" if probe else (face_params.get("enhance") or SWAP_ENHANCE)
            # 贴回/遮挡(2026-07-09)：feather 羽化贴回(消 Poisson 亮度渗漏)+XSeg 遮挡掩码。
            # 旧引擎 pydantic 忽略未知字段=零风险；探测帧不上屏,遮挡/口型掩码省掉。
            payload["blend_mode"] = SWAP_BLEND_MODE
            payload["occlusion"] = SWAP_OCCLUSION and not probe
            payload["mouth_mask"] = SWAP_MOUTH_MASK and not probe
            # 掩码内缩(光头修缮)：设了才随帧下发；不设=尊重引擎侧 /params 默认
            if SWAP_MASK_PADDING:
                payload["mask_padding"] = SWAP_MASK_PADDING
            # 小脸不换：仅全帧路径下发(裁剪通道脸占满,发了会误伤主脸)
            if SWAP_MIN_FACE_PX > 0 and rect is None:
                payload["min_face_px"] = SWAP_MIN_FACE_PX
            # 仅换主脸：显式下发(开/关都发)，引擎侧无默认歧义
            payload["main_face_only"] = SWAP_MAIN_FACE
            # 主脸滞回提示(06s)：上一帧主脸中心映射到本次送检图坐标系随请求下发，引擎按
            # "在位者优先、挑战者面积≥1.3×才换主"选脸——两人近等大时主脸身份不再逐帧闪切。
            # 引擎无状态(多客户端天然隔离)；无框/框超龄(首帧、丢脸5s)=不带提示，回退最大脸发现语义。
            if SWAP_MAIN_FACE:
                with _frame_lock:
                    _hb = list(_last_face_boxes[0]) if _last_face_boxes else None
                    _hts = _last_face_boxes_ts
                if _hb is not None and now - _hts <= 5.0:
                    _hcx, _hcy = (_hb[0] + _hb[2]) / 2.0, (_hb[1] + _hb[3]) / 2.0
                    if rect is not None:
                        payload["main_face_hint"] = [(_hcx - rect[0]) * crop_scale,
                                                     (_hcy - rect[1]) * crop_scale]
                    else:
                        payload["main_face_hint"] = [_hcx * proc_w / w, _hcy * proc_w / w]
            t_api0 = time.time()
            d = None
            _status = 200
            if SWAP_RAW_BASE and not _raw_off[0]:
                d = _swap_via_raw(payload, buf.tobytes())   # 二进制直连；不可用自动回 JSON
            if d is None:
                r = requests.post(SWAP_ENDPOINT,
                    json=payload,
                    timeout=15)
                _status = r.status_code
                if _status == 200:
                    d = r.json()
            if d is not None:
                # 目标帧未检测到人脸时，远端引擎返回 400 {detail:...}，被 Hub 代理透传为 200，
                # 此时没有 result_image。按"无脸"处理：保持上一张好脸、节流日志，避免逐帧刷屏崩解析。
                if not d.get("result_image"):
                    _swap_fail += 1
                    _now = time.time()
                    if rect is not None:
                        _crop_face_lost(_now)     # 脸离开裁剪窗→回全帧重新发现
                    if _now - _last_noface_log > 5:
                        _last_noface_log = _now
                        print(f"[Swap] 目标帧未检测到人脸，保持上一帧（{d.get('detail', 'no face')}）")
                    continue
                # 二进制直连帧带 _raw_bytes(免 b64 解码)；JSON 通道帧照旧解 b64
                img_bytes = d.get("_raw_bytes") or base64.b64decode(d["result_image"])
                arr = np.frombuffer(img_bytes, np.uint8)
                result = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                latency_ms = d.get("elapsed_ms") or int((time.time() - t_api0) * 1000)
                # 是否真的换到了脸：检测不到人脸时不能把"未换的原始画面"当成新结果，
                # 否则会"掉脸"(闪回真人脸)。faces_used/faces_tgt 缺省(None)时按成功处理。
                _fu = d.get("faces_used")
                _ft = d.get("faces_tgt")
                no_face = (_fu == 0) or (_ft == 0)
                if no_face and rect is not None:
                    _crop_face_lost(time.time())
                if result is not None:
                    global _crop_hits, _crop_active
                    display = None
                    if not no_face:
                        if rect is not None:
                            # 裁剪路径：结果块缩回原生裁剪尺寸→(仅块内)锐化→羽化回贴原生帧
                            cw0, ch0 = rect[2] - rect[0], rect[3] - rect[1]
                            if result.shape[1] != cw0 or result.shape[0] != ch0:
                                result = cv2.resize(result, (cw0, ch0))
                            _sh = _sharpen_for_swap(d)
                            if _sh > 0:
                                result = _sharpen(result, _sh)
                            display = _crop_composite(frame, result, rect)
                        else:
                            # 全帧路径(2026-07-10 脸区贴回)：旧法把 proc_w(512) 结果整幅放大到
                            # 720p → 背景跟着变软(用户报"清晰度差"的全局元凶之一)。改为只把脸区从
                            # 放大结果贴回原生帧、背景保持原生像素。无脸框/关闭 → 回退整幅放大(旧行为)。
                            _sh = _sharpen_for_swap(d)
                            _bxf = d.get("faces_boxes")
                            if SWAP_FULLFRAME_PASTE and _bxf:
                                up = cv2.resize(result, (w, h))
                                if _sh > 0:
                                    up = _sharpen(up, _sh)
                                _scf = w / float(proc_w)
                                display = frame
                                for b in _bxf:
                                    bx1, by1, bx2, by2 = (int(b[0]*_scf), int(b[1]*_scf),
                                                          int(b[2]*_scf), int(b[3]*_scf))
                                    pxx, pyy = int((bx2-bx1)*0.35), int((by2-by1)*0.35)
                                    cx1 = max(0, bx1-pxx); cy1 = max(0, by1-pyy)
                                    cx2 = min(w, bx2+pxx); cy2 = min(h, by2+pyy)
                                    if cx2-cx1 >= 8 and cy2-cy1 >= 8:
                                        display = _crop_composite(display, up[cy1:cy2, cx1:cx2],
                                                                  (cx1, cy1, cx2, cy2))
                                if display is frame:      # 框都太小没贴 → 回退整幅
                                    display = _sharpen(cv2.resize(result, (w, h)), _sh) if _sh > 0 else cv2.resize(result, (w, h))
                            else:
                                display = cv2.resize(result, (w, h))
                                if _sh > 0:
                                    display = _sharpen(display, _sh)
                    with _frame_lock:
                        global _swap_latency_last, _swap_latency_avg, _swap_latency_wmax
                        if not probe:   # 探测帧(增强关)不计入时延统计，防自适应被周期性低值扰动
                            _swap_latency_last = latency_ms
                            _swap_latency_avg = latency_ms if _swap_latency_avg <= 0 else (_swap_latency_avg * 0.7 + latency_ms * 0.3)
                            if latency_ms > _swap_latency_wmax:      # 累积本快照窗口的峰值单帧时延(供 p95/峰值分位)
                                _swap_latency_wmax = latency_ms
                            _swap_meta.update({
                                "faces_src": d.get("faces_src"),
                                "faces_tgt": _ft,
                                "faces_used": _fu,
                                "faces_filtered": d.get("faces_filtered"),
                                "detect_ms": d.get("detect_ms"),
                                "swap_ms": d.get("swap_ms"),
                                "enhance_ms": d.get("enhance_ms"),
                                "smooth_ms": d.get("smooth_ms"),
                            })
                        # 脸框刷新：三条路径都回传(裁剪坐标系→显示坐标需加窗口原点)
                        _bx = d.get("faces_boxes")
                        if _bx and not no_face:
                            try:
                                if rect is not None:
                                    _last_face_boxes = [[rect[0] + int(b[0] / crop_scale), rect[1] + int(b[1] / crop_scale),
                                                         rect[0] + int(b[2] / crop_scale), rect[1] + int(b[3] / crop_scale)]
                                                        for b in _bx]
                                    _last_face_boxes_ts = time.time()
                                else:
                                    _sc = w / float(proc_w)
                                    _nb = [[int(v * _sc) for v in b] for b in _bx]
                                    # 锁主脸+裁剪健康咬合时，探测帧只许确认"主窗内"的脸，不把窗口让给
                                    # 窗外更大的脸(路人凑近≠主播换人)。窗内无框→本次不更新：裁剪路径的
                                    # 成功响应会持续刷新主脸框；主播真离窗由裁剪 miss→face_lost 清窗兜底。
                                    if probe and SWAP_MAIN_FACE and _crop_active and _crop_rect is not None:
                                        _r = _crop_rect
                                        _nb = [b for b in _nb
                                               if _r[0] <= (b[0] + b[2]) // 2 <= _r[2]
                                               and _r[1] <= (b[1] + b[3]) // 2 <= _r[3]]
                                    if _nb:
                                        _last_face_boxes = _nb
                                        _last_face_boxes_ts = time.time()
                                        _crop_probe_ts = _last_face_boxes_ts   # 全帧成功=一次有效探测
                            except Exception:
                                pass
                        # 并发乱序保护：只接受比已应用更新的帧；探测帧只刷框不上屏
                        if seq >= _applied_seq and display is not None and not probe:
                            global _swap_motion, _swap_gap_ema
                            # 运动幅度：脸区平均像素差(见 _face_region_motion——整帧口径会被
                            # 静止背景稀释，头动时 crossfade 照混出"白斑"拖影；已持 _frame_lock)
                            if _curr_swapped is not None:
                                _swap_motion = _face_region_motion(_curr_swapped, display)
                            _applied_seq = seq
                            _prev_swapped = _curr_swapped
                            _curr_swapped = display
                            _swapped_frame = display
                            _swap_time = time.time()
                            # 换脸帧到达间隔 EMA：供 vcam 侧把 crossfade 钳到间隔以内(过渡必完成)
                            _gap = _swap_time - _swap_arrive_time
                            if 0.0 < _gap < 3.0:
                                _swap_gap_ema = _gap if _swap_gap_ema <= 0 else (_swap_gap_ema * 0.7 + _gap * 0.3)
                            _swap_arrive_time = _swap_time
                            _crop_active = rect is not None
                            if rect is not None:
                                _crop_hits += 1
                    if no_face:
                        _swap_fail += 1
                    else:
                        _swap_ok += 1
                else:
                    _swap_fail += 1
            else:
                _swap_fail += 1
                print(f"[Swap] API {_status}")
        except Exception as e:
            _swap_fail += 1
            print(f"[Swap] 请求失败: {e}")


# ── 线程3：虚拟摄像头输出 ────────────────────────────────────────────
def vcam_worker(width: int, height: int):
    global _running, _fps_actual, _vcam_out_frame

    cam = None
    cur_w, cur_h = 0, 0
    try:
        import pyvirtualcam
        _rt_backend = os.environ.get("RT_VCAM_BACKEND", "obs").strip().lower() or "obs"
    except ImportError:
        pyvirtualcam = None
        print("[VCam] ⚠️  pyvirtualcam 未安装，进入降级模式(仅预览/统计，无OBS输出)")

    t_prev = time.time()
    frame_count = 0
    real_seen = 0; real_prev = 0; _last_raw_seen = None
    _real_fps = 0.0
    _last_good_frame = None
    frame_dt = 1.0 / max(1, DEFAULT_FPS)

    def _open_cam(tw, th):
        nonlocal cam, cur_w, cur_h
        if pyvirtualcam is None:
            return
        if cam is not None:
            try:
                cam.close()
            except Exception:
                pass
            cam = None
        try:
            _rt_backend = os.environ.get("RT_VCAM_BACKEND", "obs").strip().lower() or "obs"
            cam = pyvirtualcam.Camera(width=tw, height=th, fps=DEFAULT_FPS,
                                      backend=_rt_backend, fmt=pyvirtualcam.PixelFormat.BGR)
            cur_w, cur_h = tw, th
            print(f"[VCam] ✅ 虚拟摄像头就绪: {cam.device} {tw}x{th} [backend={_rt_backend}]")
        except Exception as e:
            print(f"[VCam] ⚠️  打开OBS虚拟摄像头失败({e})，进入降级模式(OBS可能已被其它程序占用)")

    try:
        while _running:
            with _out_dim_lock:
                tw, th = _out_width, _out_height
            if cam is None or (tw, th) != (cur_w, cur_h):
                _open_cam(tw, th)

            with _frame_lock:
                if _curr_swapped is not None:
                    _last_good_frame = _curr_swapped
                raw = _raw_frame
                prev_s = _prev_swapped
                curr_s = _curr_swapped
                arrive_t = _swap_arrive_time
                motion = _swap_motion
                gap_ema = _swap_gap_ema

            # P1-E 真新帧探针：采集线程每读到新帧会把 _raw_frame 换成新对象；此处身份变化即"真有新帧"。
            #   摄像头冻结/断线时 _raw_frame 不再更新(同一对象)→ real_seen 不增 → real_fps→0，即便 vcam 仍在按帧率推旧帧。
            if raw is not None and raw is not _last_raw_seen:
                real_seen += 1
                _last_raw_seen = raw

            # 运动自适应 crossfade：晃动(帧间变化大)时直接切到新帧，避免双影幻影；
            # 仅在小幅变化且仍在过渡窗内时做混合，让静态时更顺滑。
            # 2026-07-09 参数护栏：有效时长钳到换脸帧实际到达间隔以内(CROSSFADE_GAP_RATIO)，
            # 过渡必在下一帧前完成——配置多大都不再出"永久 50/50 叠影"；供帧断档
            # (间隔 EMA 超 CROSSFADE_STALE_GAP)直接切新帧，绝不拿陈旧帧混出鬼影。
            if curr_s is not None and prev_s is not None:
                elapsed = time.time() - arrive_t
                eff_cf = CROSSFADE_DURATION
                if gap_ema > 0:
                    eff_cf = 0.0 if gap_ema > CROSSFADE_STALE_GAP else min(eff_cf, CROSSFADE_GAP_RATIO * gap_ema)
                # 仅当 prev/curr 尺寸完全一致才混帧：手机旋转/分辨率变化(如 webrtc 重协商)会让两帧尺寸不同，
                # cv2.addWeighted 要求同尺寸否则抛异常 → 整个 vcam 推流线程崩溃、OBS 无画面。尺寸不一时直接切新帧。
                if (eff_cf > 0 and elapsed < eff_cf and motion <= MOTION_SNAP_THRESH
                        and prev_s.shape == curr_s.shape):
                    alpha = elapsed / eff_cf
                    out = cv2.addWeighted(prev_s, 1.0 - alpha, curr_s, alpha, 0)
                else:
                    out = curr_s
            elif _last_good_frame is not None:
                out = _last_good_frame
            else:
                out = raw
            if out is None:
                blank = np.zeros((th, tw, 3), np.uint8)
                cv2.putText(blank, "Waiting for camera...", (tw//6, th//2),
                            cv2.FONT_HERSHEY_SIMPLEX, 1.2, (80, 200, 255), 2)
                out = blank

            # 确保尺寸正确。宽高比不同(如横屏摄像头→竖屏输出)先居中裁剪(cover)再缩放——
            if out.shape[:2] != (th, tw):
                sh, sw = out.shape[:2]
                ta, sa = tw / float(th), sw / float(sh)
                if abs(sa - ta) > 0.01:
                    if sa > ta:                      # 源更宽 → 裁两侧
                        cw = max(2, int(sh * ta))
                        x0 = max(0, (sw - cw) // 2)
                        out = out[:, x0:x0 + cw]
                    else:                            # 源更高 → 裁上下
                        ch = max(2, int(sw / ta))
                        y0 = max(0, (sh - ch) // 2)
                        out = out[y0:y0 + ch, :]
                out = cv2.resize(out, (tw, th))

            # Phase 12 C-1 虚拟背景：换脸之后·推流之前（none=直通零开销）。
            # 异常绝不打断推流线程（虚拟背景挂了宁可出原画面也不能黑屏）。
            if _bg is not None and _bg.mode != "none":
                try:
                    out = _bg.process(out)
                except Exception as _e:
                    print(f"[BG] 处理失败,本帧直通: {_e}", flush=True)
            # 离席兜底：换脸帧超龄(主播离席/引擎断供)→渐进模糊+角标；出脸恢复即刻回正常。
            # 异常绝不打断推流(宁可出冻结帧也不能黑屏)。
            if SWAP_AWAY_STYLE != "off" and curr_s is not None:
                _stale_s = time.time() - _swap_time
                if _stale_s > SWAP_AWAY_AFTER:
                    try:
                        out = _away_frame(out, _stale_s)
                    except Exception:
                        pass
            _vcam_out_frame = out          # 最终输出帧(供 /swapped 预览与 vcam 同源)

            if cam is not None:
                try:
                    cam.send(out)
                    cam.sleep_until_next_frame()
                except Exception:
                    time.sleep(frame_dt)
            else:
                time.sleep(frame_dt)

            frame_count += 1
            now = time.time()
            if now - t_prev >= 2.0:
                _fps_actual = frame_count / (now - t_prev)
                _real_fps = (real_seen - real_prev) / (now - t_prev)   # P1-E 真新帧速率(0=画面停更，即便 vcam 仍在推旧帧)
                real_prev = real_seen
                frame_count = 0
                t_prev = now
                # 近 ~5s 滚动增量 + 每秒速率（源头计算；识别"中途丢脸"比累计计数更灵敏、更抗抖动）
                _swap_hist.append((now, _swap_ok, _swap_fail))
                _win_ok = _win_fail = 0
                _win_secs = 0.0
                if len(_swap_hist) >= 2:
                    _base = _swap_hist[0]
                    for _s in _swap_hist:          # 取最早一帧 age<=5s 作窗口起点（不足 5s 用最早可得帧）
                        if now - _s[0] <= 5.0:
                            _base = _s; break
                    _win_secs = now - _base[0]
                    _win_ok   = max(0, _swap_ok   - _base[1])   # clamp：进程内计数只增，负值仅防御异常
                    _win_fail = max(0, _swap_fail - _base[2])
                _swap_recent = {"ok": _win_ok, "fail": _win_fail, "secs": round(_win_secs, 2)}
                try:                     # 换脸画质·负载自适应(opt-in)：按实测时延在≤目标档内自动升降，绝不掉帧
                    _swap_autotune()
                except Exception:
                    pass
                if SWAP_STATS_ON and (now - _swap_stats_state["last"]) >= SWAP_STATS_EVERY:
                    try:                 # 性能趋势快照(每 ~30s)：滚动留存 + 原子落盘，供 /ops 看跨时间健康与推荐启动档
                        _swap_stats_state["last"] = now
                        _swap_trend.append(_swap_stats_snapshot(now, _fps_actual, _real_fps, _win_ok, _win_fail, _win_secs))
                        _swap_stats_persist()
                    except Exception:
                        pass
                _ps_div = _win_secs if _win_secs > 0.05 else 1.0
                # 写入 FPS 状态文件（IPC）
                try:
                    import json as _json
                    with open(STATUS_FILE, 'w', encoding='utf-8') as _f:
                        _json.dump({"fps": round(_fps_actual, 1),
                                    "real_fps": round(_real_fps, 1),   # P1-E 真新帧速率(供后端判"画面停更")
                                    "swap_ok": _swap_ok, "swap_fail": _swap_fail,
                                    "swap_recent": _swap_recent,
                                    "swap_ok_ps": round(_win_ok / _ps_div, 2),
                                    "swap_fail_ps": round(_win_fail / _ps_div, 2),
                                    "swap_latency_last": round(_swap_latency_last, 1),
                                    "swap_latency_avg": round(_swap_latency_avg, 1),
                                    "main_face": SWAP_MAIN_FACE,       # 锁主脸开关(UI 多脸提示用)
                                    **{k:v for k,v in _swap_meta.items() if v is not None},
                                    "ts": now}, _f)
                except Exception:
                    pass
    finally:
        if cam is not None:
            try:
                cam.close()
            except Exception:
                pass


# ── 主预览窗口（可选）─────────────────────────────────────────────────
def preview_loop(width: int, height: int):
    """本地预览：左=原始，右=换脸"""
    global _running
    SWAP_EXPIRE = 3.0
    t_prev = time.time()
    frame_count = 0

    while _running:
        with _frame_lock:
            raw = _raw_frame.copy()     if _raw_frame     is not None else None
            ok  = time.time() - _swap_time < SWAP_EXPIRE
            swp = _swapped_frame.copy() if (_swapped_frame is not None and ok) else None
            # 06u 同源原则(本地窗版)：预览优先取 vcam 实际推给观众的最终帧(含 虚拟背景/
            # 交叉淡化/离席画面)。旧逻辑拿背景替换前的换脸帧 → 开虚拟背景后本地窗仍是
            # 真实房间背景，主播据此误判「背景没换」(2026-07-07 实例)。
            fin = _vcam_out_frame.copy() if _vcam_out_frame is not None else None

        now = time.time()
        frame_count += 1
        if now - t_prev >= 2.0:
            fps = frame_count / (now - t_prev)
            frame_count = 0
            t_prev = now
        else:
            fps = _fps_actual

        if raw is None:
            blank = np.zeros((360, 640, 3), np.uint8)
            cv2.putText(blank, "Waiting for camera input...", (80, 180),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 200, 255), 2)
            cv2.imshow("RealTime Avatar  [Q=Quit]", blank)
        else:
            display = _crop_black_border(fin if fin is not None else (swp if swp is not None else raw))
            dh, dw = display.shape[:2]
            max_h = 900
            if dh > max_h:
                scale = max_h / dh
                display = cv2.resize(display, (int(dw * scale), max_h))
            status = f"OK:{_swap_ok} Fail:{_swap_fail}"
            cam_label = "VCam: FaceSwap" if swp is not None else "VCam: Original"
            cv2.putText(display, cam_label, (8, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,255,0), 2)
            cv2.putText(display, status, (8, 54), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200,200,200), 1)
            cv2.putText(display, f"{fps:.1f}fps vcam", (8, 76), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (180,180,180), 1)
            cv2.imshow("RealTime Avatar  [Q=Quit]", display)

        key = cv2.waitKey(33) & 0xFF
        if key == ord('q') or key == ord('Q') or key == 27:
            _running = False
            break

    cv2.destroyAllWindows()


# ── 内置 MJPEG HTTP 服务器 ───────────────────────────────────────────
def _get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def mjpeg_server_worker(port: int):
    """内置MJPEG服务器 — 直接从共享内存读帧，推给手机浏览器"""
    BOUNDARY = b"--mjpegframe"
    interval  = 1.0 / MJPEG_FPS

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/" or self.path == "/control":
                body = _CTRL_PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            elif self.path.startswith("/swap/status"):
                self._json(_swap_status())

            elif self.path.startswith("/swap/stats"):
                q = parse_qs(urlparse(self.path).query)
                try:
                    _n = int((q.get("n") or ["60"])[0])
                except Exception:
                    _n = 60
                self._json(_swap_stats_payload(_n))

            elif self.path.startswith("/swap/quality"):
                self._json(_swap_quality_check())

            elif self.path.startswith("/swap/crop"):
                # 运行时热切脸区裁剪通道：/swap/crop?on=0|1（默认开；关闭即回全帧旧路径）
                global SWAP_CROP, _crop_rect, _crop_active
                q = parse_qs(urlparse(self.path).query)
                v = (q.get("on") or [""])[0].strip()
                if v in ("0", "1"):
                    SWAP_CROP = v == "1"
                    if not SWAP_CROP:
                        _crop_rect = None
                        _crop_active = False
                    print(f"[Swap] 裁剪通道 → {'开' if SWAP_CROP else '关'}", flush=True)
                self._json({"ok": True, "on": SWAP_CROP, "active": _crop_active,
                            "hits": _crop_hits, "miss": _crop_miss})

            elif self.path.startswith("/swap/main_face"):
                # 运行时热切"仅换主脸"：/swap/main_face?on=0|1（默认开；双人同框节目关掉=全换旧行为）
                global SWAP_MAIN_FACE
                q = parse_qs(urlparse(self.path).query)
                v = (q.get("on") or [""])[0].strip()
                if v in ("0", "1"):
                    SWAP_MAIN_FACE = v == "1"
                    print(f"[Swap] 仅换主脸 → {'开' if SWAP_MAIN_FACE else '关(全换)'}", flush=True)
                self._json({"ok": True, "on": SWAP_MAIN_FACE})

            elif self.path.startswith("/swap/away"):
                # 06u 离席画面热切：/swap/away?style=blur|image|off&text=...&image=路径
                # 只改显式给出的键；text 改动即重渲染角标(text= 空串=撤掉角标)；重启回环境变量默认。
                global SWAP_AWAY_STYLE, SWAP_AWAY_TEXT, SWAP_AWAY_IMAGE, _away_badge
                q = parse_qs(urlparse(self.path).query, keep_blank_values=True)
                st_ = (q.get("style") or [""])[0].strip().lower()
                if st_ in ("blur", "image", "off"):
                    SWAP_AWAY_STYLE = st_
                if "text" in q:
                    SWAP_AWAY_TEXT = (q.get("text") or [""])[0].strip()
                    _away_badge = None                      # 重渲染
                if "image" in q:
                    SWAP_AWAY_IMAGE = (q.get("image") or [""])[0].strip()
                print(f"[Swap] 离席画面 → style={SWAP_AWAY_STYLE} text={SWAP_AWAY_TEXT!r} "
                      f"image={SWAP_AWAY_IMAGE!r}", flush=True)
                self._json({"ok": True, "style": SWAP_AWAY_STYLE, "text": SWAP_AWAY_TEXT,
                            "image": SWAP_AWAY_IMAGE})

            elif self.path.startswith("/swap/params"):
                # P9 参数热更(2026-07-06)：融合/阈值/平滑/精修/交叉淡化/输出画质 即调即生效——
                # 告别「下次开播生效」。face_params 是 swap_worker 逐帧读取的请求级覆盖(0/空=回引擎默认)，
                # CROSSFADE_DURATION/OUT_JPEG_QUALITY 同为逐帧读取的模块全局，直接改写立即体现。
                # 只动 URL 里出现的键；非法值整单拒绝(400)不部分生效，避免"半套参数"的中间态。
                global CROSSFADE_DURATION, OUT_JPEG_QUALITY, SWAP_FPS, SWAP_MIN_INTERVAL, _USER_SWAP_FPS_CAP
                q = parse_qs(urlparse(self.path).query)
                _g = lambda k: (q.get(k) or [None])[0]
                upd, err = {}, ""
                try:
                    if _g("blend") is not None:
                        f = float(_g("blend"))
                        if not 0.0 <= f <= 1.0: raise ValueError("blend 需在 0~1")
                        upd["blend"] = f
                    if _g("threshold") is not None:
                        f = float(_g("threshold"))
                        if not 0.0 <= f <= 0.95: raise ValueError("threshold 需在 0~0.95")
                        upd["threshold"] = f
                    if _g("smooth") is not None:
                        f = float(_g("smooth"))
                        if not 0.0 <= f <= 0.95: raise ValueError("smooth 需在 0~0.95")
                        upd["smooth"] = f
                    if _g("enhance") is not None:
                        v = _g("enhance").strip().lower()
                        # gpen(2026-07-09)：引擎侧 ONNX 轻精修；旧引擎收到未知值=静默不增强，安全
                        if v not in ("", "none", "gfpgan", "codeformer", "gpen"):
                            raise ValueError("enhance 需为 空/none/gfpgan/codeformer/gpen")
                        upd["enhance"] = v
                    if _g("crossfade") is not None:
                        f = float(_g("crossfade"))
                        if not 0.0 <= f <= 1.5: raise ValueError("crossfade 需在 0~1.5")
                        upd["crossfade"] = f
                    if _g("swap_fps") is not None:
                        # 2026-07-09 送检帧率热更：latency 优化后无需重启即可提到 10-15fps
                        n = int(float(_g("swap_fps")))
                        if not 1 <= n <= 30: raise ValueError("swap_fps 需在 1~30")
                        upd["swap_fps"] = n
                    if _g("out_q") is not None:
                        n = int(float(_g("out_q")))
                        if not 5 <= n <= 100: raise ValueError("out_q 需在 5~100")
                        upd["out_q"] = n
                    if _g("blend_mode") is not None:
                        v = _g("blend_mode").strip().lower()
                        if v not in ("feather", "poisson"): raise ValueError("blend_mode 需为 feather/poisson")
                        upd["blend_mode"] = v
                    if _g("occlusion") is not None:
                        upd["occlusion"] = _g("occlusion").strip() in ("1", "true", "True", "on")
                    if _g("mouth_mask") is not None:
                        upd["mouth_mask"] = _g("mouth_mask").strip() in ("1", "true", "True", "on")
                    if _g("mask_padding") is not None:
                        # "上,右,下,左"% 或 "0"/空=清除(回引擎默认)。畸形→ValueError 走 400。
                        _raw = _g("mask_padding").strip()
                        if _raw in ("", "0", "off", "none"):
                            upd["mask_padding"] = None
                        else:
                            _v = [max(0.0, min(40.0, float(x))) for x in _raw.split(",")[:4]]
                            upd["mask_padding"] = ((_v + [0.0] * 4)[:4]
                                                   if any(x > 0 for x in _v) else None)
                except Exception as e:
                    err = str(e)
                if err:
                    self._json({"ok": False, "detail": err}, 400)
                else:
                    global SWAP_BLEND_MODE, SWAP_OCCLUSION, SWAP_MOUTH_MASK, SWAP_MASK_PADDING
                    for k in ("blend", "threshold", "smooth", "enhance"):
                        if k in upd:
                            face_params[k] = upd[k]
                    if "blend_mode" in upd:
                        SWAP_BLEND_MODE = upd["blend_mode"]
                    if "occlusion" in upd:
                        SWAP_OCCLUSION = upd["occlusion"]
                    if "mouth_mask" in upd:
                        SWAP_MOUTH_MASK = upd["mouth_mask"]
                    if "mask_padding" in upd:
                        SWAP_MASK_PADDING = upd["mask_padding"]
                    if "crossfade" in upd:
                        CROSSFADE_DURATION = upd["crossfade"]
                    if "swap_fps" in upd:
                        SWAP_FPS = upd["swap_fps"]
                        _USER_SWAP_FPS_CAP = upd["swap_fps"]   # 热更=用户显式意志,同步上限(换档不回落)
                        SWAP_MIN_INTERVAL = 1.0 / max(1, SWAP_FPS)
                    if "out_q" in upd:
                        OUT_JPEG_QUALITY = upd["out_q"]
                    if upd:
                        print(f"[Swap] 参数热更 → {upd}", flush=True)
                    self._json({"ok": True, "applied": upd, "params": {
                        "blend": face_params.get("blend", 0), "threshold": face_params.get("threshold", 0),
                        "smooth": face_params.get("smooth", 0), "enhance": face_params.get("enhance", ""),
                        "crossfade": round(CROSSFADE_DURATION, 2), "swap_fps": SWAP_FPS,
                        "out_q": OUT_JPEG_QUALITY, "blend_mode": SWAP_BLEND_MODE,
                        "occlusion": SWAP_OCCLUSION, "mouth_mask": SWAP_MOUTH_MASK,
                        "mask_padding": SWAP_MASK_PADDING}})

            elif self.path.startswith("/swap/preset"):
                q = parse_qs(urlparse(self.path).query)
                res = apply_swap_preset((q.get("name") or [""])[0])
                if res.get("ok"):
                    res["status"] = _swap_status()
                self._json(res, 200 if res.get("ok") else 400)

            elif self.path.startswith("/output/aspect"):
                q = parse_qs(urlparse(self.path).query)
                name = (q.get("name") or [""])[0]
                try:
                    w = int((q.get("width") or ["0"])[0])
                    h = int((q.get("height") or ["0"])[0])
                except Exception:
                    w, h = 0, 0
                res = apply_output_aspect(name=name, width=w, height=h)
                if res.get("ok"):
                    res["status"] = _swap_status()
                self._json(res, 200 if res.get("ok") else 400)

            elif self.path.startswith("/output/status"):
                self._json({"ok": True, "output": _output_status()})

            elif self.path.startswith("/bg/status"):
                # Phase 12 C-1 虚拟背景状态（模式/背景图列表/耗时）
                self._json(_bg.status() if _bg else
                           {"enabled": False, "mode": "none", "error": "bg_replace 模块不可用"})

            elif self.path.startswith("/bg/set"):
                # 运行时热切虚拟背景：/bg/set?mode=none|blur|image|green&image=xx.jpg&blur=17&every=1
                if _bg is None:
                    self._json({"ok": False, "detail": "bg_replace 模块不可用"}, 400)
                else:
                    q = parse_qs(urlparse(self.path).query)
                    _get1 = lambda k: (q.get(k) or [None])[0]
                    res = _bg.set_config(mode=_get1("mode"), image=_get1("image"),
                                         blur=_get1("blur"), every=_get1("every"))
                    if res.get("ok"):
                        print(f"[BG] 虚拟背景 → {_bg.mode}"
                              + (f"({_bg.image_name})" if _bg.mode == "image" else ""), flush=True)
                    self._json(res, 200 if res.get("ok") else 400)

            elif self.path in ("/stream", "/raw", "/swapped"):
                self.send_response(200)
                self.send_header("Content-Type",
                    "multipart/x-mixed-replace; boundary=mjpegframe")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                t_next = time.time()
                try:
                    while _running:
                        with _frame_lock:
                            if self.path == "/swapped":
                                # 预览恒与观众同源：vcam 最终帧含 背景/交叉淡化/离席画面(06u 修)。
                                # 旧逻辑仅背景开启才用最终帧→离席期预览显示冻结换脸帧、观众却在看
                                # 模糊/品牌图,主播据预览误判"画面正常"。
                                if _vcam_out_frame is not None:
                                    frame = _vcam_out_frame
                                else:
                                    frame = _swapped_frame if _swapped_frame is not None else _raw_frame
                            else:
                                frame = _raw_frame
                        if frame is not None:
                            _, jpeg = cv2.imencode(".jpg", frame,
                                [cv2.IMWRITE_JPEG_QUALITY, OUT_JPEG_QUALITY])
                            data = jpeg.tobytes()
                            self.wfile.write(BOUNDARY + b"\r\n")
                            self.wfile.write(b"Content-Type: image/jpeg\r\n")
                            self.wfile.write(f"Content-Length: {len(data)}\r\n\r\n".encode())
                            self.wfile.write(data + b"\r\n")
                            self.wfile.flush()
                        t_next += interval
                        sleep_t = t_next - time.time()
                        if sleep_t > 0:
                            time.sleep(sleep_t)
                        else:
                            t_next = time.time()
                except Exception:
                    pass
            else:
                self.send_response(404); self.end_headers()

    class _ExclusiveHTTPServer(ThreadingHTTPServer):
        # Windows 的 SO_REUSEADDR 语义=允许第二个进程绑上"正被监听"的端口(2026-07-06 三通道 soak 实锤：
        # 双 realtime_stream 同绑 8080，/swap/status 与 /bg/set 在两实例间串线)。Windows 上 listen 重绑
        # 本就不被 TIME_WAIT 拦，关掉 REUSEADDR 无重启代价；再加 SO_EXCLUSIVEADDRUSE 防别人反过来劫持。
        allow_reuse_address = (os.name != "nt")

        def server_bind(self):
            if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                try:
                    self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                except Exception:
                    pass
            ThreadingHTTPServer.server_bind(self)

    try:
        srv = _ExclusiveHTTPServer(("0.0.0.0", port), Handler)
    except OSError as e:
        # 端口被占=已有另一路实例(或他人应用)。没有控制面的实例对 Hub 不可见也停不掉，
        # 还会双倍抢摄像头/GPU——僵尸比崩溃更糟，fail-fast 整进程退出。
        global _running
        _running = False
        print(f"[MJPEG] 端口 {port} 已被占用(另一路 realtime_stream 在跑？): {e}", flush=True)
        print("[MJPEG] 本实例退出以防状态/控制串线。确需并行请用 --mjpeg-port 换端口。", flush=True)
        time.sleep(0.5)
        os._exit(2)
    try:
        srv.daemon_threads = True   # 每连接一线程：/stream 长连接不再阻塞 /swap/* 控制端点；退出不悬挂
        ip = _get_local_ip()
        print(f"[MJPEG] 手机浏览器打开: http://{ip}:{port}  (控制台: / 或 /control  流: /stream)", flush=True)
        srv.serve_forever()
    except Exception as e:
        print(f"[MJPEG] 启动失败: {e}", flush=True)


# ── 入口 ──────────────────────────────────────────────────────────────
def main():
    global _running
    global DEFAULT_WIDTH, DEFAULT_HEIGHT, DEFAULT_FPS, SWAP_FPS, JPEG_QUALITY, SWAP_MIN_INTERVAL, MJPEG_FPS, CROSSFADE_DURATION, face_params
    global OUT_JPEG_QUALITY, _USER_SWAP_FPS_CAP

    parser = argparse.ArgumentParser(description="实时换脸虚拟摄像头")
    parser.add_argument("--source",  default="0",
                        help="摄像头索引(0,1...)、'scrcpy'(手机)、或 RTSP URL")
    parser.add_argument("--width",   type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height",  type=int, default=DEFAULT_HEIGHT)
    parser.add_argument("--fps",     type=int, default=DEFAULT_FPS,
                        help="摄像头采集帧率")
    parser.add_argument("--swap-fps", type=int, default=SWAP_FPS,
                        help="换脸处理帧率上限")
    parser.add_argument("--mjpeg-fps", type=int, default=MJPEG_FPS,
                        help="MJPEG 推流帧率")
    parser.add_argument("--jpeg-quality", type=int, default=0,
                        help="输出(MJPEG预览/手机端)JPEG质量 1-100，0=默认(SWAP_OUT_JPEG_Q,85)。"
                             "与「送检压缩」解耦：送检质量按画质档走(hd=72/natural=55)")
    parser.add_argument("--swap-preset", type=str, default="",
                        help="换脸画质目标档 eco/natural/beauty/hd，空=用 SWAP_PRESET 环境变量")
    parser.add_argument("--crossfade", type=float, default=CROSSFADE_DURATION,
                        help="换脸帧之间 crossfade 时长(秒)")
    parser.add_argument("--face-blend", type=float, default=0.0,
                        help="换脸融合强度(0-1)，0使用服务器默认")
    parser.add_argument("--face-threshold", type=float, default=0.0,
                        help="人脸检测阈值，0使用服务器默认")
    parser.add_argument("--face-smooth", type=float, default=0.0,
                        help="时序平滑 alpha，0使用服务器默认")
    parser.add_argument("--face-enhance", type=str, default="",
                        help="增强方式: none/gfpgan/codeformer，空使用服务器默认")
    parser.add_argument("--aspect", type=str, default="",
                        help="输出比例预设: portrait916/portrait34/landscape169/landscape169hd/square11")
    parser.add_argument("--no-preview", action="store_true",
                        help="不显示本地预览窗口（节省性能）")
    parser.add_argument("--mjpeg-port", type=int, default=8080,
                        help="内置MJPEG服务器端口，0=禁用（默认8080）")
    args = parser.parse_args()

    # 覆盖全局运行参数（global 声明已上移至 main() 顶部，避免「先使用后声明」语法错误）
    DEFAULT_WIDTH  = args.width if args.width > 0 else DEFAULT_WIDTH
    DEFAULT_HEIGHT = args.height if args.height > 0 else DEFAULT_HEIGHT
    if args.aspect and args.aspect.strip():
        apply_output_aspect(name=args.aspect.strip())
    else:
        apply_output_aspect(width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT)
    DEFAULT_FPS    = args.fps if args.fps and args.fps > 0 else DEFAULT_FPS
    SWAP_FPS       = args.swap_fps if args.swap_fps and args.swap_fps > 0 else SWAP_FPS
    _USER_SWAP_FPS_CAP = args.swap_fps if args.swap_fps and args.swap_fps > 0 else 0
    MJPEG_FPS      = args.mjpeg_fps if args.mjpeg_fps and args.mjpeg_fps > 0 else MJPEG_FPS
    # --jpeg-quality 语义=输出(预览/手机端)质量(帮助文案历来如此)；送检质量由画质档管，不再被它踩(2026-07-05 P0 解耦)
    if args.jpeg_quality and args.jpeg_quality > 0:
        OUT_JPEG_QUALITY = min(100, max(5, args.jpeg_quality))
    CROSSFADE_DURATION = max(0.0, args.crossfade if args.crossfade is not None else CROSSFADE_DURATION)
    SWAP_MIN_INTERVAL = 1.0 / max(1, SWAP_FPS)
    # 目标档参数化：--swap-preset 优先于 SWAP_PRESET 环境变量(供 one_click_start 按 UI 档位透传)
    if args.swap_preset and args.swap_preset.strip().lower() in _SWAP_PRESETS:
        apply_swap_preset(args.swap_preset.strip().lower())
    face_params = {
        "blend": args.face_blend,
        "threshold": args.face_threshold,
        "smooth": args.face_smooth,
        "enhance": args.face_enhance,
    }

    # 若 source 是默认值且 camera_index.txt 存在，则使用持久化的摄像头索引。
    # 根因修复：只有当保存的索引"现在真能出活帧"时才覆盖；否则保持传入源(hub 已校验/自动选源的结果)，
    # 避免把启动钉死在一个未推流的手机虚拟摄像头(如 DroidCam 未连手机)上→死循环打不开。
    if args.source == "0" and os.path.exists(CAMERA_IDX_FILE):
        try:
            saved = open(CAMERA_IDX_FILE).read().strip()
            if saved.isdigit() and saved != "0":
                try:
                    import device_enum
                    _saved_live = device_enum._probe_live(int(saved))
                except Exception:
                    _saved_live = True          # 探测不可用时退回旧行为(直接采用保存索引)
                if _saved_live:
                    args.source = saved
                    print(f"[Main] 使用保存的摄像头索引: {args.source}")
                else:
                    print(f"[Main] 保存的摄像头索引 {saved} 当前无活帧，忽略并用传入源 {args.source}")
        except Exception:
            pass

    print("=" * 60)
    print("  实时换脸虚拟摄像头")
    print(f"  输入源: {args.source}  输出: {_out_width}x{_out_height} ({_output_status()['label']})")
    print(f"  换脸频率: {SWAP_FPS}fps  换脸目标: {SWAP_ENDPOINT}")
    print("  在 AvatarHub 选好角色后换脸会自动生效")
    print("  Q / ESC 退出")
    print("=" * 60)

    # 检查换脸 API
    try:
        r = requests.get("http://127.0.0.1:8000/health", timeout=3)
        print(f"[OK] FaceSwap API: {r.json().get('model_loaded','?')}")
    except Exception:
        print("[WARN] FaceSwap API 未运行，将显示原始画面")

    # 检查 AvatarHub
    try:
        r = requests.get("http://127.0.0.1:9000/health", timeout=2)
        active = r.json().get("active_profile", "")
        print(f"[OK] AvatarHub 激活角色: {active or '(未选择)'}")
    except Exception:
        print("[WARN] AvatarHub 未运行")

    # 对 HTTP 源，主线程预热 VideoCapture（避免子线程初始化失败）
    if not str(args.source).isdigit() and args.source != "scrcpy":
        print(f"[Main] Pre-warming HTTP VideoCapture for {args.source}...")
        _pre = cv2.VideoCapture(args.source)
        print(f"[Main] Pre-warm isOpened={_pre.isOpened()}")
        _pre.release()
        del _pre
        time.sleep(0.5)

    # 换脸性能趋势：从盘回灌历史趋势(跨重启连续) + (opt-in)依上次可持续档起，避开开场掉帧
    try:
        _swap_stats_init()
    except Exception:
        pass

    # 启动线程（增加摄像头热重载监控）
    threads = [
        threading.Thread(target=capture_worker,
                         args=(args.source, args.width, args.height), daemon=True),
        threading.Thread(target=vcam_worker, args=(args.width, args.height), daemon=True),
        threading.Thread(target=camera_watcher, daemon=True),  # 热重载监控
    ]
    # 并发换脸线程：抵消单帧往返延迟，提高脸的实际更新率
    for _i in range(SWAP_WORKERS):
        threads.append(threading.Thread(target=swap_worker, args=(_i,), daemon=True))
    print(f"[Main] 预设档={SWAP_PRESET or 'natural(默认)'}  SWAP_WORKERS={SWAP_WORKERS}  SWAP_FPS={SWAP_FPS}  "
          f"SWAP_PROC_W={SWAP_PROC_W}  JPEG_Q={JPEG_QUALITY}  ENHANCE={SWAP_ENHANCE}  SHARPEN={SWAP_SHARPEN}")
    if args.mjpeg_port > 0:
        threads.append(threading.Thread(
            target=mjpeg_server_worker, args=(args.mjpeg_port,), daemon=True))
    for t in threads:
        t.start()

    if not args.no_preview:
        preview_loop(args.width, args.height)
    else:
        print("无预览模式，Ctrl+C 退出")
        try:
            while _running:
                time.sleep(1)
                print(f"\r换脸成功:{_swap_ok} 失败:{_swap_fail}", end="")
        except KeyboardInterrupt:
            pass

    _running = False
    print("\n已退出")


if __name__ == "__main__":
    main()
