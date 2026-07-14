"""
数字人画面广播中枢 —— 一份 25fps 帧源，同时喂给：
  (1) OBS Virtual Camera（系统摄像头，供 OBS/会议/直播软件选用）
  (2) WebRTC 对端（手机/浏览器，真·低延迟连续音视频，像真摄像头一样丝滑）

端口: 7870  (运行环境: facefusion；pyvirtualcam + opencv + aiortc + av)
画布: 1280x720 @25fps，竖图头像居中(两侧黑边)

API:
  GET  /                       - 自带 WebRTC 直播预览页（手机/浏览器直接打开）
  GET  /health
  GET  /status                 - 是否在播、排队片段数、WebRTC 连接数
  POST /set_idle  (multipart face)  - 设置空闲时显示的头像
  POST /play      (multipart video[,audio]) - 入队一段 MP4，按帧推流；播完自动回空闲
  POST /clear                  - 清空播放队列，立即回空闲
  POST /webrtc/offer  (json sdp/type) - WebRTC 信令，返回 answer

设计:
- 单独相机线程独占 pyvirtualcam，按 25fps 节拍发帧；每帧同时更新全局 `_latest_rgb`。
- 片段开播时把整段音频解码成 48k 单声道 PCM，喂进每个 WebRTC 对端的音频缓冲（各自游标，多端互不抢样本）。
- WebRTC 视频轨读共享 `_latest_rgb`（广播）；音频轨读各自缓冲（空则静音）。视频 25fps、音频 48k 均按真实时间节拍 → 自然同步。
"""
import os, sys, time, asyncio, tempfile, threading, logging, shutil, subprocess, random
from pathlib import Path
from collections import deque
from fractions import Fraction
import numpy as np
import cv2
import uvicorn
from fastapi import FastAPI, File, UploadFile, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

import av
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamTrack

logging.basicConfig(level=logging.INFO, format="%(asctime)s [VCAM] %(message)s")
logger = logging.getLogger("vcam")

# 画布尺寸/帧率可配：默认 1280x720 横屏(兼容旧部署)；竖屏直播设 VCAM_WIDTH=720 VCAM_HEIGHT=1280。
W = int(os.environ.get("VCAM_WIDTH", "1280"))
H = int(os.environ.get("VCAM_HEIGHT", "720"))
FPS = int(os.environ.get("VCAM_FPS", "25"))
# 拟合模式：contain=等比缩放+黑边(默认)；cover=等比放大填满+居中裁切(零黑边，适合真人半身竖屏直播)。
_CANVAS_FIT = os.environ.get("VCAM_FIT", "contain").strip().lower()
AUDIO_SR, AUDIO_SPF = 48000, 960   # 48kHz，每帧 20ms = 960 样本
STREAM_GRACE = 0.5                 # 流式段间欠载冻结上一帧的最长秒数（超则回空闲）
# 音频 pre-roll:挂 seg0 音频时延迟 N 毫秒起播声音,让口型(立即出画)先建立缓冲领跑,
#   吸收段间抖动→把"句末画面落后声音"的全程漂移压向 0。实测全程漂移≈段间停顿累计(~120ms)。
AUDIO_PREROLL_MS = int(os.environ.get("VCAM_AUDIO_PREROLL_MS", "60"))
# 自适应 pre-roll:据近窗"段间停顿"估计动态设 pre-roll=停顿/2(峰值 desync 最小点),
#   长句/争用加剧→停顿变大→自动多缓冲;空闲→收敛回小值。固定值用 VCAM_PREROLL_ADAPT=0。
# pre-roll 按本句音频时长线性缩放(确定性,不用 EMA→不被争用尖峰污染):
#   preroll = clamp(时长 × PER_SEC, MIN, MAX)。停顿≈正比于句长(段越多抖动累计越多),
#   故长句多缓冲、短句少缓冲,使两端 peak|desync| 都最小。PER_SEC≈实测停顿率/2。
# 默认关:三种自适应(全局EMA / 时长×EMA / 时长×定率)实测 peak|desync| 均劣于固定 60ms
#   (99 / 113 / 100 vs 70)。原因:本场景句长~5-8s,时长缩放把 pre-roll 抬到 65-110ms,而多数句
#   仅轻微卡顿,过大 pre-roll 增加的偏移大于其省下的漂移。固定 60ms 是本负载下的最优点。
#   置 1 可在"停顿随句长强相关"的环境启用时长线性 pre-roll。
PREROLL_ADAPT = os.environ.get("VCAM_PREROLL_ADAPT", "0") == "1"
PREROLL_PER_SEC = float(os.environ.get("VCAM_PREROLL_PER_SEC", "13"))   # ms / 每秒音频
PREROLL_MIN_MS = int(os.environ.get("VCAM_PREROLL_MIN_MS", "30"))
PREROLL_MAX_MS = int(os.environ.get("VCAM_PREROLL_MAX_MS", "110"))
# 缓冲感知 pre-roll:起播音频前,若后续段已在队列里(说明生成已领跑),则趁机多等极短一会拿到
#   1 段视频垫底→吸收下游 seg-gap 尖峰;但等待窗很紧(≤PREBUF_MAX),拿不到就照常起播,
#   避免把"没卡顿的句子"的偏移拉得过负。段为 1s 粒度,故只在"几乎免费"时取垫底。
PREBUF = os.environ.get("VCAM_PREBUF", "1") == "1"
PREBUF_SEGS = int(os.environ.get("VCAM_PREBUF_SEGS", "1"))
PREBUF_MAX_MS = int(os.environ.get("VCAM_PREBUF_MAX_MS", "110"))
app = FastAPI(title="数字人广播中枢 (OBS + WebRTC)", version="2.0")
import service_auth                                  # GPU 服务面加固：鉴权 + CORS 收敛
service_auth.secure(app, name="vcam")

_lock = threading.Lock()
_clip_queue: deque = deque()      # 待播片段: {"video": mp4路径, "audio": wav路径|None}
_idle_frame = None                # RGB 1280x720 空闲帧（静态兜底）
_idle_src = None                  # 空闲原始人脸 BGR（用于活体待机的呼吸变换）
_idle_loop_canvas = None          # list[RGB 1280x720]：预 letterbox 的 LivePortrait 活体循环帧
_IDLE_LOOP_FPS = int(os.environ.get("VCAM_IDLE_LOOP_FPS", "18"))   # 活体循环播放帧率(眨眼自然不拖沓)
# 待机循环 boomerang 往返播放：消除硬 %n 循环在首/末帧之间的跳切(实测接缝可达相邻帧差的 8x，
# 即每个循环周期一次肉眼可见的"视频重置")。平稳待机窗口里"前进→倒放"的细微呼吸/摇摆几乎不可
# 察觉，却彻底抹平这一破绽。设 0 可退回硬循环。
_IDLE_PINGPONG = os.environ.get("VCAM_IDLE_PINGPONG", "1") == "1"
# 视频纹理(Video Textures)非重复待机：从单段循环里学"相似帧之间的可跳转点"，运行时做
# "永远只在相似帧间跳转"的随机游走 → 无限不重复、且始终平滑的待机。这是直播长时间待机时
# 最持续可见的"假"破绽(每 ~13s 原样重复)的根治方案，且只在 vcam 内闭环、可保证平滑、可开关。
# 设 0 退回 ping-pong。
_IDLE_TEXTURE   = os.environ.get("VCAM_IDLE_TEXTURE", "1") == "1"
_IDLE_TEXTURE_P = float(os.environ.get("VCAM_IDLE_TEXTURE_P", "0.04"))  # 每帧发生"跳转"的概率(约每1.7s一次)
_idle_trans = None                # list[list[int]]：每帧的相似远帧集合(视频纹理转移表)
_IDLE_FADE_SEC = float(os.environ.get("VCAM_IDLE_FADE", "0.45"))   # 说话→待机的交叉淡入时长(消除接缝跳变)
_START_FADE_SEC = float(os.environ.get("VCAM_START_FADE", "0.3"))  # 待机→说话的起播淡入时长(开口不跳变)
_latest_rgb = None                # 最新一帧(RGB)，WebRTC 视频轨广播读取
_cam_thread = None
_running = True
_playing = False
_force_idle = False               # 置位后相机线程立即丢弃当前段+队列回待机(降级中途用)
_av_audio_ts = 0.0                # 最近一次带音频段(seg0)启动音频的时刻
_av_await_frame = False           # 等待该段首帧出画以测 A/V 偏移
_av_offset_ms = None              # 最近一次音画偏移(首帧出画 - 音频启动),ms;>0=画面晚于声音
_av_audio_dur = 0.0               # 当前句音频时长(s,从 wav 头解出),用于全程漂移
_av_speaking = False              # 当前是否在播带音频的句子(用于句末结算漂移)
_av_frames = 0                    # 自音频挂载以来已出画的"说话帧"数(待机帧不计)
_av_drift_done = False            # 本句漂移是否已结算(音频时长走完即结算,避免重复)
_av_drift_ms = None               # 最近一句音画漂移(音频时长 - 已出画画面时长),ms;>0=画面落后于声音
_av_first_frame_ts = 0.0          # 本句首帧出画时刻(pre-roll 后于音频起播时算偏移)
_av_preroll_used = AUDIO_PREROLL_MS  # 本句实际采用的 pre-roll(ms),用于观测


def _start_clip_audio(path: str, delay_s: float):
    """延迟 delay_s 后起播该段音频(OBS 桌面声 + WebRTC),并据实际起播时刻结算 A/V 偏移。
    画面在挂载即出画、音频晚 delay_s → 口型先建立缓冲领跑,吸收段间抖动,把全程漂移压向 0。"""
    def _go():
        global _av_audio_ts, _av_offset_ms
        _play_audio_async(path)                          # OBS 桌面声
        threading.Thread(target=_feed_clip_audio, args=(path,), daemon=True).start()
        _av_audio_ts = time.time()                       # 实际声音起点(偏移/漂移均以此为基准)
        if _av_first_frame_ts:                           # 首帧已出画 → 偏移=首帧-声音(pre-roll 下为负=画面领先)
            _av_offset_ms = round((_av_first_frame_ts - _av_audio_ts) * 1000, 1)
    if PREBUF:                                           # 缓冲感知:在 [delay_s, PREBUF_MAX] 内等到 1 段垫底就起播
        def _go_when_buffered():
            t0 = time.time(); floor = max(0.0, delay_s); cap_s = PREBUF_MAX_MS / 1000.0
            while _running:
                el = time.time() - t0
                if el >= cap_s:
                    break
                if el >= floor and len(_clip_queue) >= PREBUF_SEGS:   # 已攒到垫底段→可起播
                    break
                time.sleep(0.008)
            _go()
        threading.Thread(target=_go_when_buffered, daemon=True).start()
    elif delay_s > 0:
        threading.Timer(delay_s, _go).start()
    else:
        _go()


def _wav_dur(path) -> float:
    """读 wav 头取时长(s);失败返回 0。用于音画全程漂移结算。"""
    try:
        import wave
        with wave.open(path, "rb") as w:
            fr = w.getframerate() or 0
            return (w.getnframes() / fr) if fr else 0.0
    except Exception:
        return 0.0


def _av_finalize_sentence(_tag=""):
    """结算本句音画漂移 = 音频时长 - 已出画画面时长(说话帧/FPS)。
    >0=画面落后于声音(欠载累计),≈0=全程贴合。按"说话帧计数"而非时间戳,对段间瞬时
    冻结鲁棒(冻结期不增帧,恢复后继续累加,真实反映画面是否跟上连续播放的音频)。
    不解除说话态:换块/句末由下一次音频挂载重置,瞬时回待机不影响计数。"""
    global _av_drift_ms, _av_drift_done
    if _av_speaking and not _av_drift_done and _av_audio_dur > 0:
        video_s = _av_frames / float(FPS)
        _av_drift_ms = round((_av_audio_dur - video_s) * 1000, 1)
        _av_drift_done = True


def _next_preroll_ms(audio_dur_s: float) -> int:
    """本句 pre-roll(ms)=clamp(本句时长×PER_SEC, MIN, MAX):长句多缓冲、短句少缓冲,
    两端 peak|desync| 都最小。确定性(不用 EMA),不被争用尖峰污染。"""
    if not PREROLL_ADAPT:
        return AUDIO_PREROLL_MS
    return int(max(float(PREROLL_MIN_MS), min(float(PREROLL_MAX_MS), audio_dur_s * PREROLL_PER_SEC)))
_cam_ready = False                # 虚拟摄像头是否成功开启(OBS 后端就绪)
_cam_error = ""                   # 开启失败原因(OBS 未装/被占用等)，供 UI 明确提示
import app_config
_default_face = str(app_config.BASE / "_ldh720.jpg")

# 活体待机：不说话时对静态人脸做极轻的呼吸/微动变换，免 GPU 即有"活人感"
_IDLE_BREATH = os.environ.get("VCAM_IDLE_BREATH", "1") == "1"
import math as _math
def _breathing_canvas(src_bgr, t: float):
    """对原始人脸做缓慢呼吸缩放(±1.5%)+ 微幅上下浮动，再 letterbox 到画布。纯 CPU，约 1 次 resize/帧。"""
    h, w = src_bgr.shape[:2]
    breath = 1.0 + 0.015 * _math.sin(t * 0.85)       # ~7s 一个呼吸周期
    drift  = 0.006 * _math.sin(t * 0.55)             # 极慢的横向微移
    base = min(W / w, H / h)
    scale = base * breath
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    resized = cv2.resize(src_bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)
    canvas = np.zeros((H, W, 3), np.uint8)
    dy = int(6 * _math.sin(t * 0.85))                # 轻微上下起伏
    dx = int(W * drift)
    x = (W - nw) // 2 + dx
    y = (H - nh) // 2 + dy
    # 计算源/目标裁剪区间，越界安全
    x0, y0 = max(0, x), max(0, y)
    sx0, sy0 = max(0, -x), max(0, -y)
    ex, ey = min(W, x + nw), min(H, y + nh)
    sw, sh = ex - x0, ey - y0
    if sw > 0 and sh > 0:
        canvas[y0:y0 + sh, x0:x0 + sw] = resized[sy0:sy0 + sh, sx0:sx0 + sw]
    return cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)


def _smooth(a: float) -> float:
    """smoothstep 缓动：把线性进度 a∈[0,1] 变成 S 曲线，更快越过 50/50 最重影区，
    交叉淡入的"双姿态重影"段更短、转场更干净(实测起播重影最坏达正常帧差 11x)。"""
    a = 0.0 if a < 0.0 else (1.0 if a > 1.0 else a)
    return a * a * (3.0 - 2.0 * a)


# 句间欠载(流式段间)时，对冻结帧叠加极轻微呼吸/漂移，让停顿像真人换气而非"视频卡死"。
# 实测一段对话每~3s 出现一次 ~0.4s 冻结；纯 CPU 单次 warpAffine，仅段间(每次~0.4s)调用。
_GRACE_MICRO_LIVE = os.environ.get("VCAM_GRACE_MICRO_LIVE", "1") == "1"
def _micro_live(rgb, t: float):
    s  = 1.004 + 0.003 * _math.sin(t * 2.2)      # +0.1%~+0.7% 始终轻微放大，避免缩出黑边
    dx = 1.3 * _math.sin(t * 1.3)
    dy = 1.7 * _math.sin(t * 1.9)
    M = cv2.getRotationMatrix2D((W / 2.0, H / 2.0), 0.0, s)
    M[0, 2] += dx; M[1, 2] += dy
    return cv2.warpAffine(rgb, M, (W, H), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REPLICATE)

# ── WebRTC 对端 / 音频轨注册表 ────────────────────────────────
_pcs = set()
_audio_tracks = set()
_audio_tracks_lock = threading.Lock()

# ── StreamOut 扇出（RTMP / 本地录制，共享 _latest_rgb 帧源）────────
RECORD_DIR = os.environ.get("STREAMOUT_RECORD_DIR", str(app_config.BASE / "recordings"))
_rtmp_proc = None
_rtmp_url = ""
_record_writer = None
_record_path = ""
_fanout_lock = threading.Lock()


def _ffmpeg_exe() -> str | None:
    try:
        import imageio_ffmpeg as iff
        return iff.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def _fanout_push(rgb: np.ndarray):
    """相机线程每帧调用：并行喂 RTMP pipe + 本地 MP4。"""
    with _fanout_lock:
        if _rtmp_proc is not None and _rtmp_proc.poll() is None:
            try:
                _rtmp_proc.stdin.write(np.ascontiguousarray(rgb).tobytes())
            except Exception as ex:
                logger.debug(f"RTMP pipe write: {ex}")
        if _record_writer is not None:
            try:
                bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
                _record_writer.write(bgr)
            except Exception as ex:
                logger.debug(f"record write: {ex}")


def _rtmp_start(url: str) -> dict:
    global _rtmp_proc, _rtmp_url
    ff = _ffmpeg_exe()
    if not ff:
        return {"ok": False, "detail": "未找到 ffmpeg（需 imageio-ffmpeg 或 PATH 中的 ffmpeg）"}
    if not url:
        return {"ok": False, "detail": "未设置 RTMP URL"}
    _rtmp_stop()
    cmd = [
        ff, "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS),
        "-i", "pipe:0",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
        "-pix_fmt", "yuv420p", "-g", str(FPS * 2), "-f", "flv", url,
    ]
    try:
        _rtmp_proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        _rtmp_url = url
        logger.info(f"RTMP 推流启动 → {url[:80]}")
        return {"ok": True, "url": url}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


def _rtmp_stop() -> dict:
    global _rtmp_proc, _rtmp_url
    url = _rtmp_url
    if _rtmp_proc is not None:
        try:
            _rtmp_proc.stdin.close()
        except Exception:
            pass
        try:
            _rtmp_proc.terminate()
            _rtmp_proc.wait(timeout=3)
        except Exception:
            try:
                _rtmp_proc.kill()
            except Exception:
                pass
        _rtmp_proc = None
    _rtmp_url = ""
    return {"ok": True, "was_url": url}


def _record_start(profile: str = "") -> dict:
    global _record_writer, _record_path
    _record_stop()
    Path(RECORD_DIR).mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in (profile or "stream"))[:24]
    path = str(Path(RECORD_DIR) / f"{safe}_{ts}.mp4")
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    wr = cv2.VideoWriter(path, fourcc, FPS, (W, H))
    if not wr.isOpened():
        return {"ok": False, "detail": "VideoWriter 无法打开"}
    _record_writer = wr
    _record_path = path
    logger.info(f"本地录制开始 → {path}")
    return {"ok": True, "path": path}


def _record_stop() -> dict:
    global _record_writer, _record_path
    path = _record_path
    if _record_writer is not None:
        _record_writer.release()
        _record_writer = None
    _record_path = ""
    if path and os.path.isfile(path):
        sz = os.path.getsize(path)
        logger.info(f"本地录制结束 → {path} ({sz // 1024}KB)")
        return {"ok": True, "path": path, "size_kb": sz // 1024}
    return {"ok": True, "path": path or ""}


def _stream_out_status() -> dict:
    rtmp_alive = _rtmp_proc is not None and _rtmp_proc.poll() is None
    return {
        "rtmp_active": rtmp_alive,
        "rtmp_url": _rtmp_url if rtmp_alive else "",
        "recording": _record_writer is not None,
        "record_path": _record_path if _record_writer is not None else "",
        "record_dir": RECORD_DIR,
        "ffmpeg": bool(_ffmpeg_exe()),
    }


# ── 音频解码 + 分发到各 WebRTC 音频轨 ─────────────────────────
def _decode_audio_48k_mono_i16(path: str) -> np.ndarray:
    """任意 wav/mp3 → 48kHz 单声道 int16 PCM（用 PyAV，鲁棒）。"""
    try:
        container = av.open(path)
        resampler = av.AudioResampler(format="s16", layout="mono", rate=AUDIO_SR)
        chunks = []
        for frame in container.decode(audio=0):
            for rf in resampler.resample(frame):
                chunks.append(rf.to_ndarray().reshape(-1))
        container.close()
        if chunks:
            return np.concatenate(chunks).astype(np.int16)
    except Exception as e:
        logger.debug(f"音频解码失败: {e}")
    return np.zeros(0, np.int16)


def _feed_clip_audio(path: str):
    """后台线程：解码片段音频并推给所有活跃 WebRTC 音频轨。"""
    if not path or not os.path.exists(path):
        return
    pcm = _decode_audio_48k_mono_i16(path)
    if pcm.size == 0:
        return
    with _audio_tracks_lock:
        for t in list(_audio_tracks):
            t.feed(pcm)


def _play_audio_async(path):
    """片段开播时同步播声音(供 OBS 桌面音频捕获)。无音频设备则静默跳过。"""
    if not path or not os.path.exists(path):
        return
    try:
        import winsound
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception as e:
        logger.debug(f"音频播放跳过: {e}")


def _letterbox_to_canvas(bgr: np.ndarray) -> np.ndarray:
    """任意尺寸 BGR → 贴入 W×H 画布，返回 RGB。
    contain: 等比缩放 + 居中黑边(默认)；cover: 等比放大填满 + 居中裁切(零黑边)。
    内容尺寸恰为画布尺寸时两种模式皆为直通(无缩放/无裁切)。"""
    h, w = bgr.shape[:2]
    if _CANVAS_FIT == "cover":
        scale = max(W / w, H / h)
        nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
        interp = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_CUBIC
        resized = cv2.resize(bgr, (nw, nh), interpolation=interp) if (nw, nh) != (w, h) else bgr
        x0, y0 = (nw - W) // 2, (nh - H) // 2
        crop = resized[y0:y0 + H, x0:x0 + W]
        return cv2.cvtColor(np.ascontiguousarray(crop), cv2.COLOR_BGR2RGB)
    scale = min(W / w, H / h)
    nw, nh = int(w * scale), int(h * scale)
    resized = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_AREA)
    canvas = np.zeros((H, W, 3), np.uint8)
    x, y = (W - nw) // 2, (H - nh) // 2
    canvas[y:y + nh, x:x + nw] = resized
    return cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)


def _set_idle_from_path(path: str):
    global _idle_frame, _idle_src, _idle_loop_canvas, _idle_trans
    img = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR) if os.path.exists(path) else None
    if img is None:
        _idle_frame = np.zeros((H, W, 3), np.uint8); _idle_src = None
    else:
        _idle_frame = _letterbox_to_canvas(img); _idle_src = img
    _idle_loop_canvas = None; _idle_trans = None   # 换脸→旧活体循环失效，等后台重新载入


def _build_idle_transitions(frames):
    """视频纹理转移表：对每帧 i 找出与之"高度相似的远帧"集合 jump[i]
    (|j-i|≥12 且 dist[i,j] < 1.6×相邻均值，每帧最多 8 个最优)。
    运行时基础轨迹仍是全片段 ping-pong(真实 footage、永不环绕、必平滑)，
    再以小概率跳到 jump[i] 里的相似远帧——跳转因 dist<阈值故肉眼无跳感，
    只改变后续手势走向，从而把"每 ~13s 原样重复"打断成无限不重复。"""
    try:
        n = len(frames)
        if n < 24:
            return None
        # 下采样特征 + (a-b)^2=a²+b²-2ab 成对距离(避免 NxNxD 大张量)
        F = np.stack([cv2.resize(f, (64, 112)).astype(np.float32).ravel() for f in frames])
        sq = (F * F).sum(1)
        d2 = sq[:, None] + sq[None, :] - 2.0 * (F @ F.T)
        np.maximum(d2, 0, out=d2)
        dist = np.sqrt(d2 / F.shape[1])
        adj = float(np.mean([dist[i, i + 1] for i in range(n - 1)]))
        thr = 1.6 * adj
        jump = []
        for i in range(n):
            row = dist[i]
            cand = sorted((row[j], j) for j in range(n)
                          if abs(j - i) >= 12 and row[j] < thr)
            jump.append([j for _, j in cand[:8]])
        edges = sum(len(r) for r in jump)
        logger.info(f"视频纹理转移表已建: 相邻均值={adj:.2f} 阈值={thr:.2f} 跳转边={edges} "
                    f"可跳帧={sum(1 for r in jump if r)}/{n}")
        return jump if edges >= n // 4 else None   # 跳转点太少则退回纯 ping-pong
    except Exception:
        logger.exception("视频纹理转移表构建失败，退回 ping-pong")
        return None


def _load_idle_loop_bytes(data: bytes) -> dict:
    """把 LivePortrait 活体循环 MP4 解码并预 letterbox 成 RGB 画布帧列表。
    一次性解码，之后待机播放仅按索引取帧（零 resize / 零 GPU），不会重新引入卡顿。"""
    global _idle_loop_canvas, _idle_trans
    if not data:
        return {"ok": False, "detail": "空数据"}
    tmp = tempfile.mktemp(suffix=".mp4")
    frames = []
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        cap = cv2.VideoCapture(tmp)
        while True:
            ok, bgr = cap.read()
            if not ok:
                break
            frames.append(_letterbox_to_canvas(bgr))
            if len(frames) >= 240:   # 安全上限(~10s@24fps)，防异常大文件吃内存
                break
        cap.release()
    except Exception as e:
        return {"ok": False, "detail": str(e)}
    finally:
        try: os.unlink(tmp)
        except Exception: pass
    if len(frames) < 2:
        return {"ok": False, "detail": f"帧数不足({len(frames)})"}
    _idle_loop_canvas = frames
    _idle_trans = _build_idle_transitions(frames) if _IDLE_TEXTURE else None
    logger.info(f"真·活体待机循环已载入: {len(frames)} 帧 @ {_IDLE_LOOP_FPS}fps "
                f"(视频纹理={'on' if _idle_trans else 'off'})")
    return {"ok": True, "frames": len(frames)}


# ── 字幕叠加（直播同传用；无字幕时零开销，对现有直播无影响） ──────────────
_sub_lock = threading.Lock()
_SUB_FADE_SEC = float(os.environ.get("VCAM_SUB_FADE", "0.35"))   # ttl 到点后渐隐时长
_sub_slots = {                                                   # top=对方(上) bottom=我(下)
    "bottom": {"l1": "", "l2": "", "until": 0.0, "ts": 0.0},
    "top":    {"l1": "", "l2": "", "until": 0.0, "ts": 0.0},
}
_sub_cache = {"bottom": (None, None), "top": (None, None)}
_notice = ""                                # 持久角标文字(降级提示等);空=不显示
_notice_cache = (None, None)                # (text -> (rgb, alpha)) 缓存,避免逐帧重渲染
_SUB_FONT_CACHE = {}
_SUB_BAND_H = int(H * 0.22)                  # 底部字幕带(我说)
_SUB_TOP_H  = int(H * 0.16)                  # 顶部字幕带(对方说,略矮少挡脸)


def _sub_font(px: int):
    f = _SUB_FONT_CACHE.get(px)
    if f is None:
        from PIL import ImageFont
        for fp in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"):
            try:
                f = ImageFont.truetype(fp, px); break
            except Exception:
                f = None
        _SUB_FONT_CACHE[px] = f
    return f


def _fit_font(draw, text, base_px, min_px, max_w):
    """自动缩小字号使整行不溢出(优先保全文字，而非截断)。极长仍超宽才末尾省略。"""
    px = base_px
    while px > min_px and draw.textlength(text, font=_sub_font(px)) > max_w:
        px -= 2
    font = _sub_font(px)
    if draw.textlength(text, font=font) > max_w:              # 已到最小仍超宽 → 省略
        while text and draw.textlength(text + "…", font=font) > max_w:
            text = text[:-1]
        text += "…"
    return text, font


def _render_subtitle(l1: str, l2: str, band_h: int = None, accent: str = "bottom"):
    """渲染字幕带为 (band_h, W, 3) RGB + (band_h, W) alpha。l1 英文(大)、l2 中文(次)。"""
    from PIL import Image, ImageDraw
    bh = band_h or _SUB_BAND_H
    img = Image.new("RGBA", (W, bh), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = (20, 40, 70, 140) if accent == "top" else (0, 0, 0, 150)   # 顶部略偏蓝区分「对方」
    d.rectangle([0, 0, W, bh], fill=bg)
    maxw = W - 2 * int(W * 0.05)
    l1, f1 = _fit_font(d, l1 or "", max(22, H // 24), max(15, H // 40), maxw) if l1 else ("", None)
    l2, f2 = _fit_font(d, l2 or "", max(18, H // 32), max(13, H // 48), maxw) if l2 else ("", None)
    h1 = int(f1.size * 1.35) if l1 else 0
    h2 = int(f2.size * 1.25) if l2 else 0
    y = max(int(bh * 0.12), (bh - h1 - h2) // 2)              # 文字块垂直居中
    if l1:
        d.text(((W - d.textlength(l1, font=f1)) / 2, y), l1, font=f1, fill=(255, 255, 255, 255)); y += h1
    if l2:
        c2 = (200, 220, 255, 255) if accent == "top" else (180, 200, 230, 255)
        d.text(((W - d.textlength(l2, font=f2)) / 2, y), l2, font=f2, fill=c2)
    arr = np.array(img)                                       # bh x W x 4 (RGBA)
    return arr[:, :, :3], arr[:, :, 3].astype(np.float32) / 255.0


def _slot_fade_mult(until: float, now: float) -> float:
    """ttl 到点后线性渐隐；返回 0 表示该 slot 已完全消失。"""
    if now <= until:
        return 1.0
    return max(0.0, 1.0 - (now - until) / _SUB_FADE_SEC)


def _composite_band(frame, rgb, alpha, y0: int, fade: float):
    """把字幕带 alpha 合成到 frame[y0:y0+bh]。"""
    if fade <= 0:
        return frame
    bh = rgb.shape[0]
    out = frame if fade >= 0.999 else frame.copy()
    band = out[y0:y0 + bh].astype(np.float32)
    a = alpha[:, :, None] * fade
    out[y0:y0 + bh] = (band * (1 - a) + rgb.astype(np.float32) * a).astype(np.uint8)
    return out


def _apply_subtitle(frame):
    """在 RGB 帧上/下叠加当前字幕。无/过期且渐隐完毕→原样返回(零开销)。"""
    global _sub_cache
    now = time.time()
    with _sub_lock:
        jobs = []
        for slot, y0, bh, accent in (
            ("top", 0, _SUB_TOP_H, "top"),
            ("bottom", H - _SUB_BAND_H, _SUB_BAND_H, "bottom"),
        ):
            s = _sub_slots[slot]
            if not (s["l1"] or s["l2"]):
                continue
            fade = _slot_fade_mult(s["until"], now)
            if fade <= 0:
                continue
            jobs.append((slot, s["l1"], s["l2"], y0, bh, accent, fade))
    if not jobs:
        return frame
    out = frame
    for slot, l1, l2, y0, bh, accent, fade in jobs:
        key = (l1, l2, bh, accent)
        cached_key, cached = _sub_cache[slot]
        if cached_key != key:
            try:
                cached = _render_subtitle(l1, l2, bh, accent)
            except Exception:
                logger.exception("字幕渲染失败"); continue
            _sub_cache[slot] = (key, cached)
        rgb, alpha = cached
        if fade < 0.999:
            out = out.copy()
        out = _composite_band(out, rgb, alpha, y0, fade)
    out = _apply_notice(out)
    return out


def _render_notice(text: str):
    """渲染左上角降级角标(圆角底 + 文字),返回 (rgb_patch, alpha_patch, x0, y0)。"""
    from PIL import Image, ImageDraw
    px = max(18, H // 36)
    font = _sub_font(px)
    d0 = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    tw = int(d0.textlength(text, font=font)) if font else len(text) * px
    pad = int(px * 0.5)
    pw, ph = tw + 2 * pad, px + 2 * pad
    img = Image.new("RGBA", (pw, ph), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, pw - 1, ph - 1], radius=int(ph * 0.3), fill=(160, 40, 40, 180))
    d.text((pad, pad - 2), text, font=font, fill=(255, 240, 200, 255))
    arr = np.array(img)
    return arr[:, :, :3], arr[:, :, 3].astype(np.float32) / 255.0


def _apply_notice(frame):
    """左上角持久角标(降级提示)。无 notice→零开销原样返回。"""
    global _notice_cache
    txt = _notice
    if not txt:
        return frame
    key, cached = _notice_cache
    if key != txt:
        try:
            cached = _render_notice(txt)
        except Exception:
            logger.exception("角标渲染失败"); return frame
        _notice_cache = (txt, cached)
    rgb, alpha = cached
    ph, pw = rgb.shape[0], rgb.shape[1]
    x0 = int(W * 0.03); y0 = int(H * 0.04)
    if y0 + ph > H or x0 + pw > W:
        return frame
    out = frame.copy()
    band = out[y0:y0 + ph, x0:x0 + pw].astype(np.float32)
    a = alpha[:, :, None]
    out[y0:y0 + ph, x0:x0 + pw] = (band * (1 - a) + rgb.astype(np.float32) * a).astype(np.uint8)
    return out


# ── 授权可见水印（watermark_free 杠杆：强制模式且授权不含「去水印」→ 打；pro/未强制→不打）──
# vcam 自评授权（同仓 license.py + 同一 license.key），比"等 hub 下发"更抗绕过；判定缓存
# _WM_TTL 秒避免逐帧调 license；渲染按文字缓存；异常/未强制/pro → 一律零开销原样返回。
_wm_cache = (None, None)                     # (text -> (rgb, alpha)) 渲染缓存
_wm_state = {"on": False, "text": "", "checked": 0.0}
_WM_TTL = 15.0                               # 授权判定缓存(s)：运行时激活/续费/换档 ~15s 内生效


def _watermark_refresh():
    """周期评估「是否需水印 + 水印文字」。策略集中于 watermark.resolve()——与离线视频导出
    （video_queue）共用同一真相，保证「直播/录制/快照/导出」判定一致。"""
    now = time.time()
    if now - _wm_state["checked"] < _WM_TTL:
        return
    _wm_state["checked"] = now
    try:
        import watermark as _wm
        on, text = _wm.resolve(force_reload=True)   # 强制重读授权→运行时激活/续费/换档 ~TTL 内生效
    except Exception:
        on, text = False, ""                        # 授权不可评估 → 软降级不打
    _wm_state["on"] = on
    _wm_state["text"] = text


def _render_watermark(text: str):
    """右下角半透明水印，渲染集中于 watermark.render_rgba()（直播/导出同款观感）。返回 (rgb, alpha)。"""
    import watermark as _wm
    return _wm.render_rgba(text, H)


def _apply_watermark(frame):
    """右下角授权水印。未强制 / pro / 无文字 / 异常 → 零开销原样返回。"""
    global _wm_cache
    _watermark_refresh()
    if not _wm_state["on"] or not _wm_state["text"]:
        return frame
    txt = _wm_state["text"]
    key, cached = _wm_cache
    if key != txt:
        try:
            cached = _render_watermark(txt)
        except Exception:
            logger.exception("水印渲染失败"); return frame
        _wm_cache = (txt, cached)
    rgb, alpha = cached
    ph, pw = rgb.shape[0], rgb.shape[1]
    x0 = W - pw - int(W * 0.02); y0 = H - ph - int(H * 0.03)   # 右下角
    if x0 < 0 or y0 < 0 or ph > H or pw > W:
        return frame
    out = frame.copy()
    band = out[y0:y0 + ph, x0:x0 + pw].astype(np.float32)
    a = alpha[:, :, None]
    out[y0:y0 + ph, x0:x0 + pw] = (band * (1 - a) + rgb.astype(np.float32) * a).astype(np.uint8)
    return out


class _NullCam:
    """Headless 广播 sink：不写 OBS 虚拟摄像头(免 OBS 依赖、免与 realtime_stream 抢设备)，
    仅经 _latest_rgb + WebRTC/fanout 对外。VCAM_NO_OBS=1 启用(云端/纯 WebRTC 部署或首帧取真)。
    与 pyvirtualcam.Camera 同接口：上下文管理 + send()(空操作) + sleep_until_next_frame()(按 fps 节拍)。"""
    def __init__(self, fps):
        self._spf = 1.0 / max(1, int(fps))
        self._next = None
        self.device = "headless (no OBS)"

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def send(self, frame):
        pass

    def sleep_until_next_frame(self):
        now = time.time()
        if self._next is None:
            self._next = now
        self._next += self._spf
        d = self._next - now
        if d > 0:
            time.sleep(d)
        else:
            self._next = now                     # 落后则重锚,避免追帧风暴


def _camera_loop():
    """独占相机：逐帧推流。播片段时从磁盘读帧，否则推空闲帧。每帧同时更新 _latest_rgb。
    VCAM_NO_OBS=1 时走 headless sink(不占 OBS 设备,仅 WebRTC/fanout 对外)。"""
    global _playing, _latest_rgb, _cam_ready, _cam_error, _force_idle
    global _av_audio_ts, _av_await_frame, _av_offset_ms
    global _av_audio_dur, _av_speaking, _av_frames, _av_drift_done, _av_first_frame_ts
    global _av_preroll_used
    # 广播后端选择（双链路 OBS 拆分）：
    #   VCAM_BACKEND = obs        → OBS Virtual Camera（默认；数字人独占 OBS 时用）
    #                = unitycapture→ Unity Video Capture（与 realtime_stream 的 OBS 双路同开时用，互不抢设备）
    #                = headless    → 不写任何虚拟摄像头(仅 WebRTC/fanout)
    #   兼容旧开关 VCAM_NO_OBS=1 → 等价 headless。
    _backend = os.environ.get("VCAM_BACKEND", "").strip().lower()
    if os.environ.get("VCAM_NO_OBS", "0") == "1":
        _backend = "headless"
    if not _backend:
        _backend = "obs"
    _headless = (_backend == "headless")
    try:
        if _headless:
            _cam_cm = _NullCam(FPS)
        else:
            import pyvirtualcam
            _cam_cm = pyvirtualcam.Camera(width=W, height=H, fps=FPS,
                                          backend=_backend, fmt=pyvirtualcam.PixelFormat.RGB)
        with _cam_cm as cam:
            _cam_ready = True; _cam_error = ""
            _be_label = {"headless": "Headless 广播(无 OBS,仅 WebRTC/fanout)",
                         "obs": "OBS 虚拟摄像头", "unitycapture": "Unity 虚拟摄像头"}.get(_backend, _backend)
            logger.info(f"{_be_label} 已开启: {cam.device} {W}x{H}@{FPS} [backend={_backend}]")
            cap = None
            cur = None
            last_clip_frame = None      # 流式段间欠载时冻结的上一帧
            grace_deadline = 0.0        # 段播完后在此时刻前保持冻结而非回空闲
            fade_from = None            # 说话→待机交叉淡入的起始帧(最后一帧说话画面)
            fade_until = 0.0            # 淡入结束时刻
            idle_anchor = time.time()   # 待机循环锚点：每次回到待机都从中性姿态(第0帧)起播，再叠淡入，接缝最小
            start_fade_from = None      # 待机→说话起播淡入的起始帧(最后一帧待机画面)
            start_fade_until = 0.0
            last_was_idle = True        # 上一帧是否为待机帧(用于仅在"待机→说话"时淡入，段间不淡)
            tex_idx = 0                 # 视频纹理随机游走当前帧索引
            tex_dir = 1                 # 当前播放方向(+1/-1)，遇片段两端折返
            tex_step_t = time.time()    # 上次按 idle fps 步进的时刻
            while _running:
                frame = None
                _drew_source = False                            # 本帧是否为新解码的源帧(非冻结/待机重复)
                if _force_idle:                                 # 降级中途:立即丢弃当前段+队列,直接回待机
                    _force_idle = False
                    if cap is not None:
                        cap.release(); cap = None
                    with _lock:
                        while _clip_queue:
                            _it = _clip_queue.popleft()
                            for _p in (_it.get("video"), _it.get("audio")):
                                try:
                                    if _p and _p.startswith(tempfile.gettempdir()):
                                        os.unlink(_p)
                                except Exception:
                                    pass
                        _playing = False
                    cur = None; last_clip_frame = None; grace_deadline = 0.0
                if cap is None:
                    with _lock:
                        cur = _clip_queue.popleft() if _clip_queue else None
                    if cur:
                        cap = cv2.VideoCapture(cur["video"])
                        _playing = True
                        grace_deadline = 0.0                        # 新段到达，取消冻结
                        fade_from = None                            # 开始说话→取消待机淡入
                        # 仅当从待机起播(非段间续播)时，记录最后待机帧做起播淡入
                        if last_was_idle and _START_FADE_SEC > 0 and _latest_rgb is not None:
                            start_fade_from = _latest_rgb
                            start_fade_until = time.time() + _START_FADE_SEC
                        else:
                            start_fade_from = None
                        _ap = cur.get("audio")
                        if not _ap:                                 # 无音频段(续播 seg)→ 立即按原逻辑
                            _play_audio_async(None)
                        else:
                            if _av_speaking:                        # 上一块未回待机就来新音频(分句流水线)→先结算
                                _av_finalize_sentence("chunk")
                            _av_audio_ts = 0.0; _av_await_frame = True   # 实际起播在 pre-roll 后置位
                            _av_first_frame_ts = 0.0
                            _av_audio_dur = _wav_dur(_ap)           # 音频时长→句末算全程漂移
                            _av_speaking = True; _av_frames = 0; _av_drift_done = False
                            _av_preroll_used = _next_preroll_ms(_av_audio_dur)  # 按本句时长×停顿速率定 pre-roll
                            _start_clip_audio(_ap, _av_preroll_used / 1000.0)  # 延迟起播,口型先领跑
                if cap is not None:
                    ok, bgr = cap.read()
                    if ok:
                        frame = _letterbox_to_canvas(bgr)
                        # 起播淡入：最后待机帧 → 说话帧，抹平开口瞬间的口型/头位跳变
                        if start_fade_from is not None:
                            _nows = time.time()
                            if _nows < start_fade_until:
                                a = _smooth(1.0 - (start_fade_until - _nows) / _START_FADE_SEC)  # 0→1 缓动
                                frame = cv2.addWeighted(start_fade_from, 1.0 - a, frame, a, 0)
                            else:
                                start_fade_from = None
                        last_clip_frame = frame
                        last_was_idle = False
                        _drew_source = True                     # 新源帧出画(口型真实推进)
                    else:
                        cap.release(); cap = None
                        for _k in ("video", "audio"):
                            try:
                                _p = cur.get(_k) if cur else None
                                if _p and _p.startswith(tempfile.gettempdir()):
                                    os.unlink(_p)
                            except Exception:
                                pass
                        cur = None
                        with _lock:
                            _qempty = not _clip_queue
                            _playing = not _qempty
                        if _qempty and last_clip_frame is not None:
                            grace_deadline = time.time() + STREAM_GRACE  # 进入冻结宽限
                if frame is None:
                    _now = time.time()
                    # 段间短暂欠载(流式)→保持上一帧，等待下一段，避免闪回待机；
                    # 叠极轻微呼吸/漂移，把"冻结死帧"变成"真人换气微停"，消除卡顿观感。
                    if last_clip_frame is not None and _now < grace_deadline:
                        frame = _micro_live(last_clip_frame, _now) if _GRACE_MICRO_LIVE else last_clip_frame
                    else:
                        # 刚从说话切回待机：记下最后说话帧做淡入起点，并把待机循环锚到第0帧(中性姿态)
                        if last_clip_frame is not None:
                            if _IDLE_FADE_SEC > 0:
                                fade_from = last_clip_frame
                                fade_until = _now + _IDLE_FADE_SEC
                            idle_anchor = _now
                            tex_idx = 0; tex_dir = 1; tex_step_t = _now   # 回到待机：从中性姿态(第0帧)重新起步
                            last_clip_frame = None
                        # 计算当前待机目标帧（真·活体循环 > 呼吸 > 静态）
                        if _idle_loop_canvas:
                            _n = len(_idle_loop_canvas)
                            if _idle_trans is not None and _n > 2:
                                # 视频纹理游走：全片段 ping-pong(真实 footage、永不环绕、必平滑)作基础轨迹，
                                # 偶发跳到相似远帧(dist<阈值，肉眼无跳感)打断"每 ~13s 原样重复"的周期性。
                                _steps = int((_now - tex_step_t) * _IDLE_LOOP_FPS)
                                if _steps > 0:
                                    tex_step_t += _steps / _IDLE_LOOP_FPS
                                    for _ in range(min(_steps, 6)):   # 防长时间欠载后一次性补太多步
                                        _nx = _idle_trans[tex_idx]
                                        if _nx and random.random() < _IDLE_TEXTURE_P:
                                            tex_idx = random.choice(_nx)          # 跳到相似远帧
                                        else:
                                            _ni = tex_idx + tex_dir
                                            if _ni < 0 or _ni >= _n:              # 片段两端→折返(不环绕)
                                                tex_dir = -tex_dir; _ni = tex_idx + tex_dir
                                            tex_idx = _ni
                                idx = tex_idx
                            elif _IDLE_PINGPONG and _n > 2:
                                _tick = int((_now - idle_anchor) * _IDLE_LOOP_FPS)
                                _period = 2 * (_n - 1)          # 0..n-1..1 三角波：末帧处平滑折返，无跳切
                                _p = _tick % _period
                                idx = _p if _p < _n else _period - _p
                            else:
                                idx = int((_now - idle_anchor) * _IDLE_LOOP_FPS) % _n
                            idle_frame = _idle_loop_canvas[idx]
                        elif _IDLE_BREATH and _idle_src is not None:
                            idle_frame = _breathing_canvas(_idle_src, _now)
                        else:
                            idle_frame = _idle_frame
                        # 交叉淡入：最后说话帧 → 待机帧，抹平口型/头位跳变
                        if fade_from is not None and _now < fade_until and idle_frame is not None:
                            a = _smooth(1.0 - (fade_until - _now) / _IDLE_FADE_SEC)   # 0→1 缓动
                            frame = cv2.addWeighted(fade_from, 1.0 - a, idle_frame, a, 0)
                        else:
                            frame = idle_frame
                            fade_from = None
                        last_was_idle = True
                frame = _apply_subtitle(frame)                      # 直播字幕叠加(无字幕时原样)
                frame = _apply_watermark(frame)                     # 授权可见水印(未强制/pro→零开销原样)
                if not last_was_idle:                               # 正在出画说话帧
                    if _av_speaking and _drew_source:               # 只计"新源帧"→冻结/欠载不算推进(诚实漂移)
                        _av_frames += 1
                    if _av_await_frame and _drew_source:            # 该段首"源帧"出画 → 记首帧时刻
                        _av_first_frame_ts = time.time()
                        _av_await_frame = False
                        if _av_audio_ts > 0:                        # 无 pre-roll:声音已起→即时算偏移
                            _av_offset_ms = round((_av_first_frame_ts - _av_audio_ts) * 1000, 1)
                    if _av_speaking and not _av_drift_done and _av_audio_ts > 0 \
                            and (time.time() - _av_audio_ts) >= _av_audio_dur > 0:
                        _av_finalize_sentence("dur")                # 音频时长走完即结算(不解除说话态)
                _latest_rgb = frame                                 # 原子赋值，供 WebRTC 广播
                _fanout_push(frame)
                cam.send(frame)
                cam.sleep_until_next_frame()
    except Exception as e:
        _cam_ready = False
        msg = str(e) or e.__class__.__name__
        # 归一化常见根因，给 UI 直接可读的中文提示
        low = msg.lower()
        if "could not be started" in low or "obs" in low:
            _cam_error = "OBS 虚拟摄像头无法启动：请先安装 OBS Studio 并点过一次「启动虚拟摄像头」，或确认未被其它程序独占。"
        else:
            _cam_error = f"虚拟摄像头异常：{msg[:160]}"
        logger.exception("相机线程异常退出")


# ── WebRTC 媒体轨 ────────────────────────────────────────────
class VideoBroadcastTrack(MediaStreamTrack):
    """读全局 _latest_rgb，按 25fps 节拍发帧（广播：所有对端共享同一帧源）。"""
    kind = "video"

    def __init__(self):
        super().__init__()
        self._start = None
        self._n = 0

    async def recv(self):
        if self._start is None:
            self._start = time.time()
        target = self._start + self._n / FPS
        delay = target - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        self._n += 1
        rgb = _latest_rgb
        if rgb is None:
            rgb = np.zeros((H, W, 3), np.uint8)
        frame = av.VideoFrame.from_ndarray(np.ascontiguousarray(rgb), format="rgb24")
        frame.pts = self._n
        frame.time_base = Fraction(1, FPS)
        return frame


class AudioBroadcastTrack(MediaStreamTrack):
    """每对端独立音频缓冲(48k 单声道)。有片段则播其声，空则静音。"""
    kind = "audio"

    def __init__(self):
        super().__init__()
        self._start = None
        self._n = 0
        self._buf = deque()      # list of np.int16 chunks
        self._cur = None
        self._pos = 0
        self._buf_lock = threading.Lock()

    def feed(self, pcm: np.ndarray):
        with self._buf_lock:
            self._buf.append(pcm)

    def flush(self):
        """Song-P2: 清空待播缓冲(切歌/停止即刻静音)。"""
        with self._buf_lock:
            self._buf.clear()
            self._cur = None
            self._pos = 0

    def _pull(self, n: int) -> np.ndarray:
        out = np.zeros(n, np.int16)
        filled = 0
        with self._buf_lock:
            while filled < n:
                if self._cur is None or self._pos >= len(self._cur):
                    if not self._buf:
                        break
                    self._cur = self._buf.popleft()
                    self._pos = 0
                take = min(n - filled, len(self._cur) - self._pos)
                out[filled:filled + take] = self._cur[self._pos:self._pos + take]
                self._pos += take
                filled += take
        return out

    async def recv(self):
        if self._start is None:
            self._start = time.time()
        target = self._start + self._n * (AUDIO_SPF / AUDIO_SR)
        delay = target - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        self._n += 1
        pcm = self._pull(AUDIO_SPF)
        frame = av.AudioFrame.from_ndarray(pcm.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = AUDIO_SR
        frame.pts = self._n * AUDIO_SPF
        frame.time_base = Fraction(1, AUDIO_SR)
        return frame

    def stop(self):
        super().stop()
        with _audio_tracks_lock:
            _audio_tracks.discard(self)


@app.get("/health")
def health():
    so = _stream_out_status()
    return {"ok": True, "service": "vcam", "playing": _playing,
            "queued": len(_clip_queue), "webrtc_peers": len(_pcs),
            "device": "OBS Virtual Camera", "res": f"{W}x{H}@{FPS}",
            "cam_ready": _cam_ready, "cam_error": _cam_error,
            "stream_out": so}


@app.get("/status")
def status():
    so = _stream_out_status()
    return {"playing": _playing, "queued": len(_clip_queue), "webrtc_peers": len(_pcs),
            "cam_ready": _cam_ready, "cam_error": _cam_error,
            "av_offset_ms": _av_offset_ms, "av_drift_ms": _av_drift_ms,
            "preroll_ms": _av_preroll_used, "prebuf": PREBUF, **so}


@app.get("/snapshot")
def snapshot():
    """把当前广播帧(_latest_rgb)编码成 JPEG 返回：用于可视化校验数字人正在出画，
    无需开 WebRTC 客户端。无帧(未初始化)则 503。"""
    from fastapi.responses import Response
    frm = _latest_rgb
    if frm is None:
        raise HTTPException(503, "暂无画面帧")
    bgr = cv2.cvtColor(frm, cv2.COLOR_RGB2BGR)
    ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        raise HTTPException(500, "编码失败")
    return Response(content=buf.tobytes(), media_type="image/jpeg")


@app.get("/preview.mjpeg")
async def preview_mjpeg(fps: float = 10.0, q: int = 78):
    """D-2 MJPEG 兜底预览：WebRTC 不通(严格 NAT/老浏览器/证书问题)时手机页自动降级到此。
    仅按需编码：无客户端不产生任何开销；限 1-15fps 防手机流量爆表。无帧时先发占位说明帧。"""
    fps = max(1.0, min(15.0, fps))
    q = max(40, min(92, int(q)))
    boundary = b"--vcamframe"

    async def gen():
        interval = 1.0 / fps
        placeholder_sent = False
        while True:
            t0 = time.time()
            frm = _latest_rgb
            jpg = None
            if frm is not None:
                ok, buf = cv2.imencode(".jpg", cv2.cvtColor(frm, cv2.COLOR_RGB2BGR),
                                       [int(cv2.IMWRITE_JPEG_QUALITY), q])
                if ok:
                    jpg = buf.tobytes()
            elif not placeholder_sent:
                canvas = np.zeros((H, W, 3), np.uint8)
                cv2.putText(canvas, "waiting for frames...", (W // 2 - 160, H // 2),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.9, (160, 160, 160), 2)
                ok, buf = cv2.imencode(".jpg", canvas, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
                if ok:
                    jpg = buf.tobytes()
                    placeholder_sent = True
            if jpg:
                yield (boundary + b"\r\nContent-Type: image/jpeg\r\nContent-Length: "
                       + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
            await asyncio.sleep(max(0.02, interval - (time.time() - t0)))

    return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=vcamframe",
                             headers={"Cache-Control": "no-store"})


@app.get("/stream_out/status")
def stream_out_status():
    return {"ok": True, **_stream_out_status()}


@app.post("/stream_out/rtmp/start")
async def stream_out_rtmp_start(request: Request):
    body = await request.json()
    url = (body.get("url") or os.environ.get("STREAMOUT_RTMP_URL", "")).strip()
    r = _rtmp_start(url)
    if not r.get("ok"):
        raise HTTPException(400, r.get("detail", "RTMP 启动失败"))
    return r


@app.post("/stream_out/rtmp/stop")
def stream_out_rtmp_stop():
    return _rtmp_stop()


@app.post("/stream_out/record/start")
async def stream_out_record_start(request: Request):
    body = await request.json()
    r = _record_start(body.get("profile", ""))
    if not r.get("ok"):
        raise HTTPException(400, r.get("detail", "录制启动失败"))
    return r


@app.post("/stream_out/record/stop")
def stream_out_record_stop():
    return _record_stop()


@app.post("/set_idle")
async def set_idle(face: UploadFile = File(...)):
    raw = await face.read()
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        raise HTTPException(400, "无法解码图片")
    global _idle_frame, _idle_src, _idle_loop_canvas, _idle_trans
    _idle_frame = _letterbox_to_canvas(img); _idle_src = img
    _idle_loop_canvas = None; _idle_trans = None   # 新头像→旧活体循环失效，待 /set_idle_loop 重新载入
    return {"ok": True}


@app.post("/set_idle_loop")
async def set_idle_loop(loop: UploadFile = File(...)):
    """载入真·活体待机循环（LivePortrait 眨眼/转头基底 MP4）。
    解码一次后循环播放，待机即有真实生命感，且不再吃 GPU。"""
    raw = await loop.read()
    r = _load_idle_loop_bytes(raw)
    if not r.get("ok"):
        raise HTTPException(400, r.get("detail", "活体循环载入失败"))
    return r


@app.post("/clear_idle_loop")
async def clear_idle_loop():
    """清除活体循环，回退到呼吸/静态待机。"""
    global _idle_loop_canvas, _idle_trans
    _idle_loop_canvas = None; _idle_trans = None
    return {"ok": True}


@app.post("/play")
async def play(video: UploadFile = File(...), audio: UploadFile = File(None)):
    raw = await video.read()
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(raw); vtmp = f.name
    atmp = None
    if audio is not None:
        araw = await audio.read()
        if araw:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as af:
                af.write(araw); atmp = af.name
    with _lock:
        _clip_queue.append({"video": vtmp, "audio": atmp})
        qn = len(_clip_queue)
    return {"ok": True, "queued": qn}


@app.post("/play_audio")
async def play_audio(audio: UploadFile = File(...)):
    """纯音频播放(不入视频队列)：保持待机脸不变，仅播声音(OBS 桌面声 + WebRTC 音轨)。
    用于直播自愈降级——口型(lipsync)异常时，数字人维持待机表情 + 克隆语音继续，画面不中断。
    (Song-P2 点歌台整曲播放也走这里——歌曲以分钟计，cleanup 需覆盖整曲时长)"""
    araw = await audio.read()
    if not araw:
        return {"ok": False, "detail": "empty audio"}
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as af:
        af.write(araw); atmp = af.name
    _play_audio_async(atmp)                                    # OBS 桌面声(同步起播)
    threading.Thread(target=_feed_clip_audio, args=(atmp,), daemon=True).start()  # WebRTC 音轨
    dur = _wav_dur(atmp)
    def _cleanup():
        time.sleep(max(30.0, dur + 10.0))    # 整曲播完再删，winsound 按文件路径流式读
        try: os.unlink(atmp)
        except Exception: pass
    threading.Thread(target=_cleanup, daemon=True).start()
    return {"ok": True, "duration_s": round(dur, 1)}


@app.post("/stop_audio")
def stop_audio():
    """Song-P2: 停掉 /play_audio 正在播的纯音频(点歌台切歌/停止用)。
    winsound SND_PURGE 停桌面声；WebRTC 音轨清缓冲立即静音。视频队列不受影响。"""
    try:
        import winsound
        winsound.PlaySound(None, winsound.SND_PURGE)
    except Exception as e:
        logger.debug(f"停音跳过: {e}")
    with _audio_tracks_lock:
        for t in list(_audio_tracks):
            try:
                t.flush()
            except Exception:
                pass
    return {"ok": True}


@app.post("/clear")
def clear():
    global _playing
    with _lock:
        while _clip_queue:
            it = _clip_queue.popleft()
            for _p in (it.get("video"), it.get("audio")):
                try:
                    if _p: os.unlink(_p)
                except Exception: pass
    return {"ok": True}


@app.post("/return_idle")
def return_idle():
    """立即丢弃当前播放段+队列,回待机帧(直播降级中途用:避免停在半张嘴的死帧)。"""
    global _force_idle
    _force_idle = True
    return {"ok": True}


@app.post("/subtitle")
async def set_subtitle(request: Request):
    """设置/清除直播字幕。body: {line1, line2, ttl, slot}。
    slot=bottom(默认,我说) | top(对方说)。ttl<=0 或空文本=立即清除该 slot。"""
    try:
        body = await request.json()
    except Exception:
        body = {}
    slot = str(body.get("slot", "bottom") or "bottom").strip().lower()
    if slot not in _sub_slots:
        slot = "bottom"
    l1 = str(body.get("line1", "") or "").strip()
    l2 = str(body.get("line2", "") or "").strip()
    ttl = float(body.get("ttl", 6.0) or 0.0)
    now = time.time()
    with _sub_lock:
        s = _sub_slots[slot]
        if not (l1 or l2) or ttl <= 0:
            s["l1"] = s["l2"] = ""; s["until"] = 0.0; s["ts"] = 0.0
            _sub_cache[slot] = (None, None)
        else:
            # 短间隔内同 slot 文本扩展 → 合并+续 ttl，减少子句闪烁
            if s["l1"] and now - s["ts"] < 0.45:
                if l1.startswith(s["l1"]) or s["l1"].startswith(l1):
                    l1 = l1 if len(l1) >= len(s["l1"]) else s["l1"]
                if l2.startswith(s["l2"]) or s["l2"].startswith(l2):
                    l2 = l2 if len(l2) >= len(s["l2"]) else s["l2"]
                ttl = max(ttl, max(0.0, s["until"] - now))
            s["l1"] = l1; s["l2"] = l2; s["until"] = now + ttl; s["ts"] = now
    return {"ok": True, "slot": slot}


@app.post("/notice")
async def set_notice(request: Request):
    """设置/清除左上角持久角标(直播降级时显示「配音模式」让观众侧明确感知)。body: {text}。"""
    global _notice
    try:
        body = await request.json()
    except Exception:
        body = {}
    _notice = str(body.get("text", "") or "").strip()
    return {"ok": True, "notice": _notice}


@app.post("/webrtc/offer")
async def webrtc_offer(request: Request):
    params = await request.json()
    offer = RTCSessionDescription(sdp=params["sdp"], type=params["type"])
    pc = RTCPeerConnection()
    _pcs.add(pc)

    @pc.on("connectionstatechange")
    async def on_state():
        logger.info(f"WebRTC 连接状态: {pc.connectionState}")
        if pc.connectionState in ("failed", "closed", "disconnected"):
            await pc.close()
            _pcs.discard(pc)

    vtrack = VideoBroadcastTrack()
    atrack = AudioBroadcastTrack()
    with _audio_tracks_lock:
        _audio_tracks.add(atrack)
    pc.addTrack(vtrack)
    pc.addTrack(atrack)

    await pc.setRemoteDescription(offer)
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    logger.info(f"WebRTC 新对端接入，总连接 {len(_pcs)}")
    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}


_VIEWER_HTML = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>数字人直播</title>
<style>html,body{margin:0;height:100%;background:#000;display:flex;flex-direction:column;
align-items:center;justify-content:center;font-family:system-ui;color:#ccc}
video{max-width:100%;max-height:80vh;background:#111;border-radius:12px}
button{margin-top:16px;padding:12px 28px;font-size:16px;border:0;border-radius:24px;
background:#2d6cdf;color:#fff}#s{margin-top:8px;font-size:13px;opacity:.7}</style></head>
<body><video id=v autoplay playsinline></video>
<button id=b>▶ 连接直播</button><div id=s>未连接</div>
<script>
const v=document.getElementById('v'),s=document.getElementById('s'),b=document.getElementById('b');
b.onclick=async()=>{b.disabled=true;s.textContent='连接中…';
 const pc=new RTCPeerConnection();
 pc.addTransceiver('video',{direction:'recvonly'});
 pc.addTransceiver('audio',{direction:'recvonly'});
 const ms=new MediaStream();v.srcObject=ms;
 pc.ontrack=e=>{ms.addTrack(e.track);};
 pc.onconnectionstatechange=()=>{s.textContent='状态：'+pc.connectionState;};
 const off=await pc.createOffer();await pc.setLocalDescription(off);
 const r=await fetch('/webrtc/offer',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({sdp:pc.localDescription.sdp,type:pc.localDescription.type})});
 const ans=await r.json();await pc.setRemoteDescription(ans);
 v.muted=false;v.play().catch(()=>{v.muted=true;v.play();});
};
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
def viewer():
    return _VIEWER_HTML


def _startup():
    global _cam_thread
    _set_idle_from_path(_default_face)
    _cam_thread = threading.Thread(target=_camera_loop, daemon=True)
    _cam_thread.start()


if __name__ == "__main__":
    # 双开预检(06o)：vcam 双实例除了 7870 串线,还会互抢 OBS 虚拟摄像头(必崩)。守在 _startup()
    # 之前——相机线程一旦起来就开始摸 OBS,必须先验明正身再放行。工具 import 本模块不受影响。
    import port_guard
    port_guard.ensure_port_free(int(os.environ.get("VCAM_PORT", "7870")), "vcam_server")

_startup()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("VCAM_PORT", "7870")), log_level="warning")
