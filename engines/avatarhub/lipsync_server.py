"""
LipSync Server - MuseTalk 1.5 驱动的口型同步服务
端口: 8090
API:
  GET  /health              - 健康检查
  POST /lipsync/generate    - 音频+人脸图 → 口型同步视频(MP4字节)
  POST /lipsync/preload     - 预加载模型
  GET  /lipsync/status      - 模型加载状态
"""
import os, sys, io, copy, glob, time, tempfile, logging, threading, pickle, asyncio, functools, gc, subprocess
# face_alignment 1.5+ 内部用 torch.compile → Windows 无 Triton 会 TritonMissing 崩溃；
# 关掉 TorchDynamo 让 torch.compile 退化为 eager（功能不变，仅不编译）。
os.environ.setdefault("TORCHDYNAMO_DISABLE", "1")
# 减少显存碎片/保留增长（须在 import torch 前设置）
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")
from concurrent.futures import ThreadPoolExecutor
import numpy as np
import cv2
import requests
import torch
# torch cu128 后：mem-efficient 注意力(CUTLASS)在 5090(sm_120) 原生可用，比 math 快。
# flash 在 Windows torch 未编译(永远不可用)→ 关掉避免徒劳尝试与告警；mem-efficient+math 兜底。
try:
    torch.backends.cuda.enable_flash_sdp(False)
    torch.backends.cuda.enable_mem_efficient_sdp(True)
    torch.backends.cuda.enable_math_sdp(True)
except Exception:
    pass
import uvicorn
from fastapi import FastAPI, File, UploadFile, Form, HTTPException, BackgroundTasks
from fastapi.responses import Response, JSONResponse

# ── 路径设置 ─────────────────────────────────────────────────────────────
MUSETALK_DIR = os.path.join(os.path.dirname(__file__), "MuseTalk")
sys.path.insert(0, MUSETALK_DIR)
os.chdir(MUSETALK_DIR)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [LipSync] %(message)s")
logger = logging.getLogger("lipsync")


def _resolve_ffmpeg():
    """优先用 imageio-ffmpeg 自带二进制，回退到 PATH 上的 ffmpeg。"""
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    import shutil
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    logger.warning("ffmpeg 未找到：合音轨将跳过(仅无声视频)。请 pip install imageio-ffmpeg")
    return ""


FFMPEG = _resolve_ffmpeg()


def _mux_audio_video(vid_path: str, audio_path: str, out_path: str) -> bool:
    """ffmpeg 合音轨。用 subprocess 列表参数，避免 os.system 在 Windows 下
    因中文路径/引号解析触发「文件名、目录名或卷标语法不正确」。"""
    if not FFMPEG:
        return False
    try:
        r = subprocess.run(
            [FFMPEG, "-y", "-i", vid_path, "-i", audio_path,
             "-c:v", "copy", "-c:a", "aac", "-shortest", out_path,
             "-loglevel", "error"],
            capture_output=True, timeout=120)
        return r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 1000
    except Exception as e:
        logger.warning(f"ffmpeg mux failed: {e}")
        return False


def _clear_stale_face_alignment_cache():
    """face_alignment torch.compile 缓存在 torch 升级后易 checksum 损坏 → 启动时清掉。"""
    cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "face_alignment", "compile")
    if not os.path.isdir(cache_dir):
        return
    for p in glob.glob(os.path.join(cache_dir, "*.bin")):
        try:
            os.unlink(p)
            logger.info(f"cleared stale fa cache: {os.path.basename(p)}")
        except Exception:
            pass


# 人脸增强服务（facefusion 环境，GFPGAN）——把 256 口型输出复原到更清晰。可选、失败回退原视频。
ENHANCE_URL = os.environ.get("ENHANCE_URL", "http://127.0.0.1:8092")
VCAM_URL = os.environ.get("VCAM_URL", "http://127.0.0.1:7870")
# 流式 HD 增强混合系数：1.0=全 GFPGAN(可能略塑料)，<1=混回原图保留真实皮肤纹理。
_ENH_BLEND = float(os.environ.get("LIPSYNC_ENH_BLEND", "0.85"))


def _enhance_video_bytes(silent_mp4_path: str, blend: float = 1.0, timeout: float = 120.0) -> str:
    """把无声 MP4 送到增强服务，成功则写回增强结果到新临时文件并返回其路径；失败返回原路径。"""
    try:
        import requests
        with open(silent_mp4_path, "rb") as f:
            r = requests.post(f"{ENHANCE_URL}/enhance_video",
                              files={"video": ("clip.mp4", f, "video/mp4")},
                              data={"blend": str(blend), "detect_once": "1"},
                              timeout=timeout)
        if r.status_code == 200 and r.content and len(r.content) > 1000:
            out = silent_mp4_path.replace(".mp4", "_enh.mp4")
            with open(out, "wb") as f:
                f.write(r.content)
            logger.info(f"face-enhance ok ({len(r.content)//1024}KB, {r.headers.get('X-Processing-Time','?')})")
            return out
        logger.warning(f"face-enhance 返回 {r.status_code}，用原视频")
    except Exception as e:
        logger.warning(f"face-enhance 跳过(用原视频): {e}")
    return silent_mp4_path

app = FastAPI(title="LipSync Server", version="1.0")
import service_auth                                  # GPU 服务面加固：鉴权 + CORS 收敛
service_auth.secure(app, name="lipsync")


def _vcam_push_headers() -> dict:
    """段直推 vcam 的请求头：带共享服务令牌。VCAM_URL 为局域网地址时源 IP 非回环，
    vcam 的 service_auth 会拦（2026-07-05 实锤：/play 401 静默丢段 → 直播只剩待机画面）。
    令牌随每次推送热读（service_auth._token_direct 内已含 env/文件双源），轮换零重启。"""
    try:
        tok = service_auth._token_direct()
        return {"X-AH-Svc": tok} if tok else {}
    except Exception:
        return {}


# ── P-Harden3(2026-07-07 事故): vcam 推流断路器 ─────────────────────────────
# vcam(7870) 半途死掉后，每段推送连接被拒，但新的流式句照常进 GPU 渲染注定推不出去的
# 视频，队列越积越长(实测 gpu_wait 29s→110s)把整卡拖死、殃及无关任务(人脸预计算等)。
# 断路器：连续「连接级」推送失败 ≥N 段 → 打开；打开期间新流式句先花 ~1.5s 探一次
# vcam /health——通了就闭合放行(自动恢复，零人工)，不通直接 503 快速失败(不上 GPU)。
# 只拦「新句」，不打断正在渲染的句子；HTTP 4xx/5xx(如 401 鉴权)不算连接失败，保持原告警语义。
_PUSH_BREAKER_N       = int(os.environ.get("LIPSYNC_PUSH_BREAKER_N", "6"))       # 连续失败段数阈值；0=禁用
_PUSH_BREAKER_PROBE_S = float(os.environ.get("LIPSYNC_PUSH_BREAKER_PROBE_S", "1.5"))
_push_fail_streak = 0
_push_breaker_open = False
_push_breaker_lock = threading.Lock()


def _push_note(ok: bool, target: str = ""):
    """每段推送结果登记：成功清零并闭合；连续失败达阈值 → 打开断路器（跳变各记一次日志）。"""
    global _push_fail_streak, _push_breaker_open
    if _PUSH_BREAKER_N <= 0:
        return
    with _push_breaker_lock:
        if ok:
            if _push_breaker_open:
                logger.info("[stream] vcam 推流恢复 → 断路器闭合")
            _push_fail_streak = 0
            _push_breaker_open = False
        else:
            _push_fail_streak += 1
            if _push_fail_streak >= _PUSH_BREAKER_N and not _push_breaker_open:
                _push_breaker_open = True
                logger.warning(f"[stream] vcam({target}) 连续 {_push_fail_streak} 段推送失败 → 断路器打开："
                               f"新流式句先探活，不通即 503 快速失败，不再空烧 GPU")


def _push_breaker_gate(target: str) -> bool:
    """断路器闭合恒放行；打开时探一次 vcam /health：通 → 闭合放行(True)，不通 → False(调用方 503)。"""
    if not _push_breaker_open:
        return True
    try:
        r = requests.get(f"{target}/health", timeout=_PUSH_BREAKER_PROBE_S)
        alive = r.status_code == 200
    except Exception:
        alive = False
    if alive:
        _push_note(True)
    return alive

# ── 全局模型状态 ──────────────────────────────────────────────────────────
_lock = threading.Lock()
_models_loaded = False
_load_error = ""
_vae = _unet = _pe = _audio_processor = _whisper = None
_fp = None  # FaceParsing 单例（避免每次请求重载 bisenet）
# 所有 GPU 推理固定在同一个工作线程执行：sm_120(5090) 上各网络首前向的 kernel
# 编译/handle 初始化是「按线程」付费的，固定单线程 + 启动预热 → 之后每个请求全程热速。
# 单线程 + 优先级队列：直播流式生成(0) 永远排在 后台人脸预计算/活体导出(5) 之前 →
#   直播进行中后台预载某角色冷脸(~7-28s)不再卡住实时口型(只要直播句一入队即下一个执行)。
import queue as _queue_std
import itertools as _itertools
GPU_PRIO_LIVE = 0          # 直播流式/整句生成(最高优先)
GPU_PRIO_WARM = 2          # 引擎预热
GPU_PRIO_BG = 5            # 后台人脸预计算 / 活体导出(最低优先)
_gpu_q: "_queue_std.PriorityQueue" = _queue_std.PriorityQueue()
_gpu_seq = _itertools.count()
_device = "cuda" if torch.cuda.is_available() else "cpu"


def _gpu_worker():
    """单 GPU 工作线程:按优先级取任务执行(同优先级 FIFO),保证全程热态 kernel。"""
    while True:
        _prio, _seq, fn, args, kwargs, done = _gpu_q.get()
        if fn is None:
            break
        try:
            done(("ok", fn(*args, **kwargs)))
        except Exception as e:                          # 异常回传给等待方,不杀工作线程
            done(("err", e))


_gpu_thread = threading.Thread(target=_gpu_worker, daemon=True, name="lipsync-gpu")
_gpu_thread.start()


def _gpu_submit(fn, args, kwargs, priority, done):
    _gpu_q.put((priority, next(_gpu_seq), fn, args, kwargs, done))


def _gpu_run_sync(fn, *args, priority=GPU_PRIO_WARM, **kwargs):
    """同步在 GPU 线程执行并阻塞取结果(供启动/预热等非 async 路径)。"""
    ev = threading.Event(); box = {}
    def _done(r): box["r"] = r; ev.set()
    _gpu_submit(fn, args, kwargs, priority, _done)
    ev.wait()
    kind, val = box["r"]
    if kind == "err":
        raise val
    return val


async def _run_in_gpu(fn, *args, priority=GPU_PRIO_BG, **kwargs):
    """异步在 GPU 线程执行(优先级队列)。priority 越小越先。"""
    loop = asyncio.get_event_loop()
    fut = loop.create_future()
    def _done(r):
        kind, val = r
        if kind == "err":
            loop.call_soon_threadsafe(fut.set_exception, val)
        else:
            loop.call_soon_threadsafe(fut.set_result, val)
    _gpu_submit(fn, args, kwargs, priority, _done)
    return await fut


# ── 直播优先·防 GPU 队头阻塞 ─────────────────────────────────────────────────
#   单 GPU worker 逐任务跑完、不可抢占：一个后台重任务(人脸预计算/活体导出，含 LivePortrait
#   ~7-28s)正在执行时，直播句到达只能干等它跑完 → 首帧偶发数秒尖峰(实测 gpu_wait 可达数千 ms)。
#   两层解法(均可经环境变量关掉，回到原「纯优先级队列」行为，零回归开关)：
#     ① 准入控制：直播活跃期(最近 _LIVE_GUARD_SEC 秒内有直播句)不放行后台 GPU 任务入队，
#        从源头避免其占住 worker；封顶 _BG_DEFER_MAX 秒后仍放行，避免后台任务被永久饿死。
#     ② 循环让路：后台任务的逐帧循环每帧探队列，有更高优先(直播)任务在等就地跑完再续 →
#        把「已在跑的后台任务」对直播的阻塞从「整任务时长」收敛到「约一帧」。
_BG_DEFER_ON    = os.environ.get("LIPSYNC_BG_DEFER", "1") == "1"
_BG_YIELD_ON    = os.environ.get("LIPSYNC_BG_YIELD", "1") == "1"
_LIVE_GUARD_SEC = float(os.environ.get("LIPSYNC_LIVE_GUARD_SEC", "2.0"))    # 直播句后视为「活跃」的窗口
_BG_DEFER_MAX   = float(os.environ.get("LIPSYNC_BG_DEFER_MAX_SEC", "20"))   # 后台任务最长让路(封顶防饿死)
_live_active_until = 0.0


def _note_live():
    """标记「此刻有直播句」：后台 GPU 任务在 _LIVE_GUARD_SEC 内让路。直播端点入口/收尾调用。
    用 monotonic 时钟做区间判断，免受系统时钟跳变影响。"""
    global _live_active_until
    _live_active_until = time.monotonic() + _LIVE_GUARD_SEC


async def _await_live_idle(tag=""):
    """后台 GPU 任务提交前调用：直播活跃期异步等待(不占 worker，故不阻塞直播)，封顶 _BG_DEFER_MAX 秒。"""
    if not _BG_DEFER_ON:
        return 0.0
    t0 = time.monotonic(); deadline = t0 + _BG_DEFER_MAX
    while time.monotonic() < _live_active_until and time.monotonic() < deadline:
        await asyncio.sleep(0.05)
    waited = time.monotonic() - t0
    if waited > 0.1:
        logger.info(f"[sched] 后台任务 {tag} 让路直播 {waited*1000:.0f}ms")
    return waited


def _gpu_yield_to_live(my_prio=GPU_PRIO_BG):
    """在后台任务的耗时循环中调用：把队列里所有更高优先(直播)任务就地跑完再返回，
    使「已在执行的后台任务」对直播首帧的阻塞收敛到≈一次循环迭代。仅在 GPU worker 线程内调用。
    重入安全：_run_lipsync 为 @torch.no_grad 自包含、模型权重只读、后台累加器(latents)极小。"""
    if not _BG_YIELD_ON:
        return 0
    drained = 0
    while True:
        with _gpu_q.mutex:                       # 单消费者(仅本 worker)，探到即可安全取
            top = _gpu_q.queue[0][0] if _gpu_q.queue else None
        if top is None or top >= my_prio:
            break
        try:
            item = _gpu_q.get_nowait()
        except _queue_std.Empty:
            break
        _p, _s, fn, args, kwargs, done = item
        if fn is None:                           # 关停哨兵：放回，停止让路
            _gpu_q.put(item); break
        try:
            done(("ok", fn(*args, **kwargs)))
        except Exception as e:
            done(("err", e))
        drained += 1
    return drained


_weight_dtype = torch.float32  # UNet 默认 float32 权重，统一用 float32 避免 dtype 不匹配
_timesteps = torch.tensor([0], device=_device)
# face_id -> {coord_list, frame_list, latent_list}；LRU 上限，避免无限增长吃内存
from collections import OrderedDict
_FACE_CACHE_MAX = int(os.environ.get("LIPSYNC_FACE_CACHE_MAX", "8"))
_face_cache: "OrderedDict" = OrderedDict()

# 每次推理后的 GC 节流：实测整轮 finally 里的 gc.collect() 全量收集 ~188ms/次，是流式小块
# 的最大固定开销(占 ~66%)。大帧列表(res_frames/out_frames/seg_buf)本就靠引用计数置 None 即时
# 释放，gc.collect() 仅为清循环引用——无需每块都跑。改为「按累计渲染帧数节流」：每 ~阈值帧
# (默认 500≈20s 视频)才全量 gc 一次，把 188ms 摊薄到每轮一次而非每块一次。RSS 另有 mem_watchdog
# 调 /gc 兜底，故降频安全。设 0 可恢复「每次都 gc」的旧行为。
_GC_FRAME_THRESHOLD = int(os.environ.get("LIPSYNC_GC_FRAME_THRESHOLD", "500"))
_frames_since_gc = 0

# ── 活体基底(LivePortrait) ────────────────────────────────────────────────
# 把静态照片先用 LivePortrait 驱动成「会摆头/眨眼/有表情」的多帧基底，再让 MuseTalk
# 在动态基底上贴口型 → 真人感(而非一张死图对口型)。预计算多帧缓存即可，运行逻辑不变
# (frame_cycle = frame_list + frame_list[::-1] 天然把多帧基底乒乓循环)。
_ALIVE_ON = os.environ.get("LIPSYNC_ALIVE", "1") == "1"
_ALIVE_FRAMES = int(os.environ.get("LIPSYNC_ALIVE_FRAMES", "78"))  # 基底帧数(越多越不易看出循环，越吃内存)
# 活体基底磁盘持久化：face_id 是人脸内容哈希 → 同一照片只需算一次(landmark/LP ~17s)，
#   之后激活直接秒加载;并让「首句即活体」(除每个角色第一次外)。
_ALIVE_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_alive_cache")
_ALIVE_DISK = os.environ.get("LIPSYNC_ALIVE_DISK", "1") == "1"


def _alive_cache_path(face_id: str) -> str:
    import re as _re
    safe = _re.sub(r"[^A-Za-z0-9_.-]", "_", str(face_id))[:64]
    return os.path.join(_ALIVE_CACHE_DIR, f"{safe}_{_ALIVE_FRAMES}.pt")


def _save_alive_cache(path: str, data: dict):
    os.makedirs(_ALIVE_CACHE_DIR, exist_ok=True)
    payload = {
        "coord_list": data["coord_list"],
        "frame_list": data["frame_list"],
        "latent_list": [l.detach().cpu() for l in data["latent_list"]],
        "mask_list": data.get("mask_list"),
        "mask_margin": data.get("mask_margin"),
    }
    tmp = path + ".tmp"
    torch.save(payload, tmp)
    os.replace(tmp, path)


def _load_alive_cache(path: str) -> dict:
    payload = torch.load(path, map_location="cpu", weights_only=False)
    payload["latent_list"] = [l.to(_device) for l in payload["latent_list"]]
    if not payload.get("mask_list"):
        payload.pop("mask_list", None)
        payload.pop("mask_margin", None)
    return payload


def _face_cache_put(face_id: str, data: dict):
    _face_cache[face_id] = data
    _face_cache.move_to_end(face_id)
    while len(_face_cache) > _FACE_CACHE_MAX:
        old_id, _ = _face_cache.popitem(last=False)
        logger.info(f"face_cache 超过上限({_FACE_CACHE_MAX})，淘汰最旧: {old_id}")


def _face_cache_get(face_id: str):
    d = _face_cache.get(face_id)
    if d is not None:
        _face_cache.move_to_end(face_id)   # 命中即刷新为最近使用
    return d

# ── 懒加载模型 ────────────────────────────────────────────────────────────
def _load_models():
    global _models_loaded, _load_error, _vae, _unet, _pe, _audio_processor, _whisper
    with _lock:
        if _models_loaded:
            return True
        try:
            logger.info("Loading MuseTalk models...")
            _clear_stale_face_alignment_cache()
            from musetalk.utils.utils import load_all_model
            from musetalk.utils.audio_processor import AudioProcessor
            from transformers import WhisperModel

            _vae, _unet, _pe = load_all_model(device=_device)
            _vae.vae = _vae.vae.to(_device)
            _unet.model = _unet.model.to(_device)
            _pe = _pe.to(_device)

            _audio_processor = AudioProcessor(feature_extractor_path="models/whisper")
            _whisper = WhisperModel.from_pretrained("models/whisper").to(_device).eval()

            # 预热：1) 人脸检测 + 解析模型；2) 跑一次 UNet+VAE 假前向，
            #   触发 sm_120(5090) 的 CUDA kernel 编译/autotune，避免首句对话 ~80s 冷启动
            try:
                # 必须在 GPU 工作线程内预热，使该线程的 kernel/handle 全部就绪
                _gpu_run_sync(_warmup_inference, priority=GPU_PRIO_WARM)
                logger.info("Face detector + parsing + UNet/VAE warmed up (gpu thread)")
            except Exception as we:
                logger.warning(f"warmup skipped: {we}")

            # HD 自预热（opt-in）：实时高清(GFPGAN)首句要现场付「模型载入 + grid_sample/批量
            #   autotune」一次性开销(~数秒)。默认已开(LIPSYNC_HD_PREWARM=1)：启动即在 GPU 线程内付清 →
            #   首条 HD 内容句即满速。STD-only / 小显存部署可设 0 省 ~1GB 显存(HD 首用时懒加载)。
            if _HD_PREWARM:
                try:
                    _gpu_run_sync(_warmup_hd, priority=GPU_PRIO_WARM)
                    logger.info("HD enhancer (GFPGAN bf16) warmed up (gpu thread)")
                except Exception as we:
                    logger.warning(f"HD warmup skipped: {we}")

            # 流式分支预热：补齐「合并单循环 + 首段 flush + 收尾 partial batch (+HD 逐批增强)」的
            #   一次性开销，消除首条流式句的首帧冷启尖峰(9s 专查的低风险预防项)。
            try:
                _gpu_run_sync(_warmup_stream, priority=GPU_PRIO_WARM)
                logger.info("Streaming path warmed up (gpu thread)")
            except Exception as we:
                logger.warning(f"stream warmup skipped: {we}")

            _models_loaded = True
            logger.info(f"MuseTalk models loaded on {_device}")
            return True
        except Exception as e:
            _load_error = str(e)
            logger.error(f"Model load failed: {e}")
            return False


def _warmup_inference():
    """跑一次真实端到端生成（人脸检测→Whisper→UNet batch8→VAE→贴回），
    预编译 sm_120(5090) 上各网络首前向的 CUDA kernel，使后续请求全程热速。
    用内置人脸 + 1s 合成音频，无外部依赖；失败静默跳过。"""
    import soundfile as sf
    face_path = os.path.join(MUSETALK_DIR, "assets", "_warmup_face.png")
    if not os.path.exists(face_path):
        return
    face_img = cv2.imdecode(np.fromfile(face_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if face_img is None:
        return
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = f.name
    try:
        sr = 16000
        sig = (np.random.randn(sr * 6) * 0.01).astype(np.float32)  # 6s 低幅噪声，覆盖常见句长
        sf.write(wav_path, sig, sr)
        _run_lipsync(face_img, wav_path, fps=25, batch_size=8)
    finally:
        try: os.unlink(wav_path)
        except: pass


# 默认开启：本服务即实时口型服务，启动时把 GFPGAN 载入 + prepare/批量 autotune 一次性付清，
#   消除「首条 HD 句现场冷启 ~数秒」的流式首帧尖峰。STD-only / 小显存部署可设 0 省 ~1GB 显存。
_HD_PREWARM = os.environ.get("LIPSYNC_HD_PREWARM", "1") == "1"


def _warmup_hd():
    """HD 实时增强自预热：在 GPU 线程内跑一次 GFPGAN 载入 + prepare(grid_sample autotune)
    + 整批 enhance_batch，把首条 HD 句的一次性 autotune/显存分配开销在启动时付清。
    用内置人脸帧，无外部依赖；失败静默(降级到首句现场预热,行为不变)。"""
    face_path = os.path.join(MUSETALK_DIR, "assets", "_warmup_face.png")
    if not os.path.exists(face_path):
        return
    face_img = cv2.imdecode(np.fromfile(face_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if face_img is None:
        return
    import face_enhance as _fe
    if not _fe.ensure_loaded():
        return
    frames = [face_img.copy() for _ in range(8)]   # 凑满固定 batch，预热整批形状
    prep = _fe.prepare(frames[0])
    if prep is not None:
        _fe.enhance_batch(frames, prep, _ENH_BLEND)


def _warmup_stream():
    """预热「流式分支」的真实代码路径与批形状：合并单循环 + 首段 flush + 收尾 partial batch
    +（若 HD）同进程 GFPGAN 逐批增强。非流式预热(_warmup_inference)走的是另一条两段循环，
    首条流式句仍会现场付这条路径的一次性开销 → 这里在启动补齐，消除流式首帧冷启尖峰。
    用内置人脸 + 2.5s 合成音频(必产≥2 段 + partial batch)，noop sink 不推 vcam；失败静默跳过。"""
    import soundfile as sf
    face_path = os.path.join(MUSETALK_DIR, "assets", "_warmup_face.png")
    if not os.path.exists(face_path):
        return
    face_img = cv2.imdecode(np.fromfile(face_path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if face_img is None:
        return
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav_path = f.name
    try:
        sr = 16000
        sig = (np.random.randn(int(sr * 2.5)) * 0.01).astype(np.float32)  # 2.5s→多段 + 收尾 partial batch
        sf.write(wav_path, sig, sr)
        _run_lipsync(face_img, wav_path, fps=25, batch_size=8,
                     seg_sink=lambda _b, _i: None,            # noop：仅预热生成/编码路径，不推 vcam
                     seg_frames=25, first_seg_frames=15,
                     enhance=("gfpgan" if _HD_PREWARM else ""))
    finally:
        try: os.unlink(wav_path)
        except: pass


def _get_fp():
    """FaceParsing 单例。"""
    global _fp
    if _fp is None:
        from musetalk.utils.face_parsing import FaceParsing
        _fp = FaceParsing()
    return _fp


def _ensure_models():
    _touch()
    if not _models_loaded:
        ok = _load_models()
        if not ok:
            raise HTTPException(503, f"模型未就绪: {_load_error}")


# ── 空闲自动卸载模型（默认关闭；设环境变量 LIPSYNC_IDLE_UNLOAD=秒 开启，如 1800=空闲30分钟卸载）──
#   卸载后下次请求会自动重载(含预热)，首句会慢一次；适合长时间不用时省内存。
_IDLE_UNLOAD = float(os.environ.get("LIPSYNC_IDLE_UNLOAD", "0"))
_last_used = time.time()
_inflight = 0
_inflight_lock = threading.Lock()


def _touch():
    global _last_used
    _last_used = time.time()


def _infer_enter():
    global _inflight
    with _inflight_lock:
        _inflight += 1
    _touch()


def _infer_leave():
    global _inflight
    with _inflight_lock:
        if _inflight > 0:
            _inflight -= 1
    _touch()


def _unload_models():
    global _models_loaded, _vae, _unet, _pe, _audio_processor, _whisper, _fp
    with _lock:
        if not _models_loaded:
            return
        _vae = _unet = _pe = _audio_processor = _whisper = _fp = None
        _models_loaded = False
    try:
        gc.collect(); torch.cuda.empty_cache(); torch.cuda.ipc_collect()
    except Exception:
        pass
    logger.info("空闲卸载: MuseTalk 模型已释放，下次请求将自动重载")


def _idle_watch():
    while True:
        time.sleep(30)
        try:
            if _IDLE_UNLOAD <= 0:
                continue
            with _inflight_lock:
                busy = _inflight > 0
            if _models_loaded and not busy and (time.time() - _last_used) > _IDLE_UNLOAD:
                _unload_models()
        except Exception:
            pass


# ── 核心推理函数 ───────────────────────────────────────────────────────────
@torch.no_grad()
def _make_base_frames(face_img):
    """构造 MuseTalk 的基底帧序列：
    - 活体模式(默认): 用 LivePortrait 把静态照片驱动成 N 帧(摆头/眨眼/表情)。
    - 失败或关闭时: 退回单帧静态照片(原行为)。
    返回 BGR 帧列表。"""
    if _ALIVE_ON:
        try:
            import sys as _sys
            _wd = os.path.dirname(os.path.abspath(__file__))
            if _wd not in _sys.path:
                _sys.path.insert(0, _wd)
            import live_base
            t0 = time.time()
            frames = live_base.generate_alive_frames(face_img, max_frames=_ALIVE_FRAMES)
            if frames:
                logger.info(f"[alive] LivePortrait 基底 {len(frames)} 帧, {time.time()-t0:.1f}s")
                return frames
            logger.warning("[alive] 未检测到人脸/模板缺失，退回静态单帧")
        except Exception as e:
            logger.warning(f"[alive] LivePortrait 基底失败，退回静态单帧: {e}")
    return [face_img]


def _precompute_face_sync(face_img, face_id, bbox_shift, extra_margin, base_frames=None):
    """在 GPU 工作线程内预计算人脸 latents 并缓存。返回 latent 数量(0=未检测到人脸)。
    支持多帧活体基底：coord/frame/latent 三组列表严格对齐(剔除检测失败帧)。
    base_frames 非空时(路线A 视频底)：直接用真人视频帧做基底，跳过 LivePortrait 伪活体。"""
    from musetalk.utils.preprocessing import get_landmark_and_bbox, coord_placeholder
    from musetalk.utils.blending import get_image_prepare_material
    # 磁盘快路径：同一人脸(内容哈希)只算一次,之后秒加载
    if _ALIVE_ON and _ALIVE_DISK and face_id:
        _cp = _alive_cache_path(face_id)
        if os.path.exists(_cp):
            try:
                data = _load_alive_cache(_cp)
                _face_cache_put(face_id, data)
                logger.info(f"[alive] 命中磁盘缓存 {face_id}: {len(data['latent_list'])}帧(秒加载)")
                return len(data["latent_list"])
            except Exception as e:
                logger.warning(f"[alive] 磁盘缓存读取失败,重算: {e}")
    _tpc = {}; _ta = time.time()
    if base_frames is None:
        base_frames = _make_base_frames(face_img)
    _tpc["liveportrait"] = time.time() - _ta; _ta = time.time()
    _multi = len(base_frames) > 1
    fp = _get_fp() if _multi else None  # 多帧活体：预解析每帧 mask，运行时零额外 parsing
    tmp_files = []
    try:
        for fr in base_frames:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                tmp_files.append(f.name)
            cv2.imwrite(tmp_files[-1], fr)
        coord_list, frame_list = get_landmark_and_bbox(tmp_files, bbox_shift)
        _tpc["landmark"] = time.time() - _ta; _ta = time.time()
        if not coord_list or all(c == coord_placeholder for c in coord_list):
            return 0
        # 三组列表对齐：只保留检测成功的帧；多帧时一并预解析融合 mask
        coords2, frames2, latents2, masks2 = [], [], [], []
        for bbox, frame in zip(coord_list, frame_list):
            _gpu_yield_to_live()                 # 每帧让路：直播句到达时≤一帧即可插入生成，不必等整轮预计算
            if bbox == coord_placeholder:
                continue
            x1, y1, x2, y2 = bbox
            y2 = min(y2 + extra_margin, frame.shape[0])
            crop = cv2.resize(frame[y1:y2, x1:x2], (256, 256), interpolation=cv2.INTER_LANCZOS4)
            latents2.append(_vae.get_latents_for_unet(crop))
            coords2.append(bbox)
            frames2.append(frame)
            if _multi:
                try:
                    masks2.append(get_image_prepare_material(frame, [x1, y1, x2, y2], mode="jaw", fp=fp))
                except Exception:
                    masks2.append(None)
        if not latents2:
            return 0
        _tpc["latent_parse"] = time.time() - _ta
        data = {"coord_list": coords2, "frame_list": frames2, "latent_list": latents2}
        if _multi and len(masks2) == len(frames2) and all(m is not None for m in masks2):
            data["mask_list"] = masks2  # 预解析 mask(extra_margin=该值, mode=jaw)
            data["mask_margin"] = extra_margin
        _face_cache_put(face_id, data)
        if _multi:
            logger.info(f"[alive] precompute 分解: LP={_tpc.get('liveportrait',0):.1f}s "
                        f"landmark={_tpc.get('landmark',0):.1f}s latent+parse={_tpc.get('latent_parse',0):.1f}s "
                        f"({len(latents2)}帧)")
            if _ALIVE_DISK and face_id and "mask_list" in data:
                try:
                    _save_alive_cache(_alive_cache_path(face_id), data)
                    logger.info(f"[alive] 已写磁盘缓存 {face_id}")
                except Exception as e:
                    logger.warning(f"[alive] 磁盘缓存写入失败: {e}")
        return len(latents2)
    finally:
        for t in tmp_files:
            try: os.unlink(t)
            except: pass


# ── 接缝精修：视频底口型的"偏色补丁 + 偏软"修复 ───────────────────────────
# MuseTalk 256 解码出的嘴部 patch 与周围真人 720p 皮肤存在(1)白平衡/亮度漂移→偏色补丁，
# (2)清晰度失配→偏软。下面在贴回前对小 patch 做两步轻量修复(仅作用于嘴部框，实时安全)：
#   ① 色调对齐：用 patch 边缘环(必为皮肤)估计与底帧同区域的色差，做钳制 DC 校正(不碰嘴内牙齿/舌头)；
#   ② 轻 unsharp：补偿 256 解码的柔化。两者均可经 env 关闭/调强度。
_SEAM_COLOR   = os.environ.get("LIPSYNC_SEAM_COLOR", "1") == "1"
_SEAM_COLOR_MAX = float(os.environ.get("LIPSYNC_SEAM_COLOR_MAX", "10"))  # 单通道最大校正幅度(级)
_SEAM_SHARPEN = float(os.environ.get("LIPSYNC_SEAM_SHARPEN", "0.5"))      # unsharp 强度(0=关)


def _seam_refine(face, ref):
    """face: 生成嘴部 patch(BGR uint8)；ref: 底帧同区域(真人皮肤参考，同尺寸)。
    返回色调对齐 + 轻锐化后的 patch。失败/尺寸不符则原样返回。"""
    try:
        if (not _SEAM_COLOR and _SEAM_SHARPEN <= 0) or face.size == 0:
            return face
        fh, fw = face.shape[:2]
        if fh < 6 or fw < 6:
            return face
        f = face.astype(np.float32)
        if _SEAM_COLOR and ref is not None and ref.shape[:2] == (fh, fw):
            k = max(2, min(fh, fw) // 8)               # 边缘环宽度
            r = ref.astype(np.float32)
            # 上/下/左/右四条皮肤带的均值(规避中央嘴腔)
            def ring(a):
                return (a[:k].reshape(-1, 3).sum(0) + a[-k:].reshape(-1, 3).sum(0)
                        + a[:, :k].reshape(-1, 3).sum(0) + a[:, -k:].reshape(-1, 3).sum(0))
            cnt = 2 * k * fw + 2 * k * fh
            shift = (ring(r) - ring(f)) / max(1, cnt)   # 每通道色差
            shift = np.clip(shift, -_SEAM_COLOR_MAX, _SEAM_COLOR_MAX)
            f += shift[None, None, :]
        if _SEAM_SHARPEN > 0:
            blur = cv2.GaussianBlur(f, (0, 0), sigmaX=1.1)
            hf = f - blur                                   # 高频细节
            # 边缘自适应:按局部高频能量调锐化强度——牙齿/唇线等边缘满锐，
            #   平坦皮肤仅轻锐(保底 0.2)，避免把 256 解码在平坦区的噪点也放大。
            emag = cv2.GaussianBlur(np.abs(hf).mean(axis=2), (0, 0), sigmaX=2.0)
            m = float(emag.max())
            if m > 1e-3:
                w = np.clip(emag / m * 1.6, 0.2, 1.0)[:, :, None]
                f = f + (_SEAM_SHARPEN * w) * hf
            else:
                f = f + _SEAM_SHARPEN * hf
        return np.clip(f, 0, 255).astype(face.dtype)
    except Exception:
        return face


def _blend_np(image, face, face_box, mask_array, crop_box):
    """纯 numpy 版 get_image_blending：等价 PIL crop/paste(mask) 的 alpha 融合，
    但免去整图 1242×1802 的两次 PIL 往返(~21ms→个位 ms)。
    说明：原版对 image/face 各做一次 [:,:,::-1] 再于结尾反回 → 两次反转抵消，
    故可直接在 BGR 上运算，逐通道一致，结果与 PIL 版逐像素等价(仅四舍五入级差)。
    crop_box 越界部分按 PIL 语义补黑/裁剪。"""
    H, W = image.shape[:2]
    x, y, x1, y1 = face_box
    # 接缝精修：以底帧同区域真人皮肤为参考，对生成嘴部 patch 做色调对齐 + 轻锐化
    if _SEAM_COLOR or _SEAM_SHARPEN > 0:
        rx0, ry0, rx1, ry1 = max(0, x), max(0, y), min(W, x1), min(H, y1)
        if rx1 > rx0 and ry1 > ry0 and (rx1 - rx0, ry1 - ry0) == (face.shape[1], face.shape[0]):
            face = _seam_refine(face, image[ry0:ry1, rx0:rx1])
    x_s, y_s, x_e, y_e = crop_box
    cw, ch = x_e - x_s, y_e - y_s
    # face_large：从原图裁出 crop_box（越界补黑），再把嘴部 face 贴进去
    face_large = np.zeros((ch, cw, 3), dtype=image.dtype)
    sx0, sy0 = max(0, x_s), max(0, y_s)
    sx1, sy1 = min(W, x_e), min(H, y_e)
    if sx1 <= sx0 or sy1 <= sy0:
        return image
    dx0, dy0 = sx0 - x_s, sy0 - y_s
    face_large[dy0:dy0 + (sy1 - sy0), dx0:dx0 + (sx1 - sx0)] = image[sy0:sy1, sx0:sx1]
    fx, fy = x - x_s, y - y_s
    fh, fw = face.shape[:2]
    # 嘴部贴入(裁剪到 face_large 边界内，防越界)
    cy0, cx0 = max(0, fy), max(0, fx)
    cy1, cx1 = min(ch, fy + fh), min(cw, fx + fw)
    if cy1 > cy0 and cx1 > cx0:
        face_large[cy0:cy1, cx0:cx1] = face[cy0 - fy:cy1 - fy, cx0 - fx:cx1 - fx]
    # alpha 融合 face_large 回原图(mask 0→保留原图,255→用 face_large)
    alpha = (mask_array.astype(np.float32) / 255.0)[:, :, None]
    out = image.copy()
    reg = image[sy0:sy1, sx0:sx1].astype(np.float32)
    a = alpha[dy0:dy0 + (sy1 - sy0), dx0:dx0 + (sx1 - sx0)]
    fl = face_large[dy0:dy0 + (sy1 - sy0), dx0:dx0 + (sx1 - sx0)].astype(np.float32)
    out[sy0:sy1, sx0:sx1] = (fl * a + reg * (1.0 - a)).astype(image.dtype)
    return out


@torch.no_grad()
def _run_lipsync(
    face_img: np.ndarray,       # BGR, 任意尺寸（face_data 为 None 时必须提供）
    audio_path: str,            # WAV 文件路径
    fps: int = 25,
    bbox_shift: int = 0,
    batch_size: int = 8,
    extra_margin: int = 10,
    parsing_mode: str = "jaw",
    face_data: dict = None,     # 预计算的 {coord_list, frame_list, latent_list}，传入则跳过 DWPose
    enhance: str = "",          # "gfpgan"→送人脸增强服务复原(更清晰)；空=不增强
    seg_sink=None,              # 流式：callable(seg_mp4_bytes, seg_index)；设置则边生成边吐无声小片段
    seg_frames: int = 25,       # 流式每段帧数（默认 1s）
    first_seg_frames: int = 15, # 流式首段更短(~0.6s)→首帧更快；生成≥实时故后续段仍追得上
    prof: dict = None,          # 诊断(9s尖峰专查)：传 dict 则填入分段计时(GPU进入/whisper/首批/各批max/GFPGAN/编码/首段)
) -> bytes:
    """
    给定人脸图和音频, 返回口型同步的 MP4 视频字节流
    face_data 不为 None 时跳过耗时的人脸检测步骤（提速 15-20s）
    """
    from musetalk.utils.preprocessing import get_landmark_and_bbox, read_imgs, coord_placeholder
    from musetalk.utils.blending import get_image, get_image_prepare_material, get_image_blending
    from musetalk.utils.face_parsing import FaceParsing
    from musetalk.utils.utils import datagen

    if prof is not None:
        prof["t_enter"] = time.time()   # GPU 线程真正开始执行本任务的时刻(减调用方 submit=GPU队列等待，抓「排在后台预计算后」型尖峰)
    _infer_enter()
    tmp_face = None
    _nframes = 0     # 本次渲染帧数（供 finally 的 GC 节流计数；两分支各自赋值）
    try:
        # 1. 提取人脸关键点和 bbox（若有缓存则跳过）
        if face_data:
            coord_list       = face_data["coord_list"]
            frame_list       = face_data["frame_list"]
            input_latent_list = face_data["latent_list"]
            logger.info("Using precomputed face latents (skipped DWPose)")
        else:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                tmp_face = f.name
            cv2.imwrite(tmp_face, face_img)
            coord_list, frame_list = get_landmark_and_bbox([tmp_face], bbox_shift)
            if not coord_list or coord_list[0] == coord_placeholder:
                raise ValueError("未检测到人脸，请换一张清晰的正面人脸图")

            # 3. 编码人脸 latents
            input_latent_list = []
            for bbox, frame in zip(coord_list, frame_list):
                if bbox == coord_placeholder:
                    continue
                x1, y1, x2, y2 = bbox
                y2 = min(y2 + extra_margin, frame.shape[0])
                crop = cv2.resize(frame[y1:y2, x1:x2], (256, 256), interpolation=cv2.INTER_LANCZOS4)
                latents = _vae.get_latents_for_unet(crop)
                input_latent_list.append(latents)

        if not input_latent_list:
            raise ValueError("人脸编码失败")

        _tp = {}; _t0 = time.time()
        # 2. 提取音频特征 (Whisper)
        whisper_feats, librosa_len = _audio_processor.get_audio_feature(audio_path)
        whisper_chunks = _audio_processor.get_whisper_chunk(
            whisper_feats, _device, _weight_dtype, _whisper, librosa_len,
            fps=fps,
            audio_padding_length_left=2,
            audio_padding_length_right=2,
        )

        # 循环帧（让单张图产生足够帧数）
        frame_cycle   = frame_list + frame_list[::-1]
        coord_cycle   = coord_list + coord_list[::-1]
        latent_cycle  = input_latent_list + input_latent_list[::-1]

        # 活体多帧基底：用预解析 mask 预填融合缓存 → 运行时零额外 face parsing(保持实时)
        _mask_seed = {}
        if face_data and face_data.get("mask_list") and parsing_mode == "jaw" \
                and face_data.get("mask_margin") == extra_margin:
            try:
                for _frm, _bb, _mk in zip(frame_list, coord_list, face_data["mask_list"]):
                    _x1, _y1, _x2, _y2 = _bb
                    _y2 = min(_y2 + extra_margin, _frm.shape[0])
                    _mask_seed[(id(_frm), _x1, _y1, _x2, _y2)] = _mk
            except Exception:
                _mask_seed = {}

        _tp["whisper"] = time.time() - _t0; _t1 = time.time()
        if prof is not None:
            prof["whisper_ms"] = round(_tp["whisper"] * 1000, 1)

        # ── 流式分支：合并 UNet→VAE→贴回 单循环，每 seg_frames 帧吐一个无声小片段 ──
        #   前提条件已达成（cu128 + blend mask 缓存 → 生成≥实时），可边生成边播，
        #   首帧从「整句」降到「首段(~1s)」。段内不合音轨(避免每段 ffmpeg 固定开销)，
        #   整句音频由调用方(sink)挂到第 0 段，因生成≥实时视频追得上 → 全程 A/V 同步。
        if seg_sink is not None:
            import queue as _queue
            from musetalk.utils.blending import get_image_prepare_material, get_image_blending
            fp = _get_fp()
            gen = datagen(whisper_chunks, latent_cycle, batch_size=batch_size,
                          delay_frame=0, device=_device)
            seg_buf, seg_idx, fi = [], 0, 0
            _mask_cache = dict(_mask_seed)

            # 流式 HD(同进程 GFPGAN)：在本 GPU 线程内逐帧复原,与生成同一 CUDA 上下文 →
            #   免 mp4/HTTP/跨进程上下文切换。生成20ms+增强(bf16)~9ms 串行 ~32ms/帧 < 40ms,实时可达。
            _enh_on = bool(enhance)
            _face_enh = None
            _enh_prep = None
            if _enh_on:
                try:
                    import face_enhance as _face_enh
                    _face_enh.ensure_loaded()
                    logger.info("[stream] 同进程 GFPGAN(bf16) 已就绪,流式 HD 逐帧复原")
                except Exception as _le:
                    logger.warning(f"[stream] 同进程增强加载失败,降级为 STD: {_le}")
                    _enh_on = False

            # 关键：cv2 编码 + HTTP 推送放到后台线程，不阻塞 GPU 生成线程。
            #   GPU 线程纯生成 ~37ms/帧(<实时40ms)→段段及时到达，播放连续无卡顿。
            #   编码(CPU)与下一段的 GPU 生成天然重叠。
            _pushq: "_queue.Queue" = _queue.Queue()

            def _pusher():
                while True:
                    item = _pushq.get()
                    if item is None:
                        break
                    buf, idx = item
                    try:
                        _enc_t = time.time()
                        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as _sf:
                            _sp = _sf.name
                        h, w = buf[0].shape[:2]
                        wr = cv2.VideoWriter(_sp, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
                        for _fr in buf:
                            wr.write(_fr)
                        wr.release()
                        with open(_sp, "rb") as _rf:
                            _seg_bytes = _rf.read()
                        if prof is not None:
                            prof.setdefault("seg_enc_ms", []).append(round((time.time() - _enc_t) * 1000, 1))
                        seg_sink(_seg_bytes, idx)
                        try: os.unlink(_sp)
                        except Exception: pass
                    except Exception as _pe:
                        logger.warning(f"[stream] 段{idx} 编码/推送失败: {_pe}")
            _pt = threading.Thread(target=_pusher, daemon=True)
            _pt.start()

            _t_loop = time.time()
            _bidx = 0; _first_batch_ms = None; _max_batch_ms = 0.0
            for whisper_batch, latent_batch in gen:
                _bt = time.time()
                audio_feat = _pe(whisper_batch)
                latent_batch = latent_batch.to(dtype=torch.float32)
                pred = _unet.model(latent_batch, _timesteps,
                                   encoder_hidden_states=audio_feat).sample
                recon = _vae.decode_latents(pred)
                batch_out = []
                for res_frame in recon:
                    bbox = coord_cycle[fi % len(coord_cycle)]
                    src = frame_cycle[fi % len(frame_cycle)]
                    fi += 1
                    if bbox == coord_placeholder:
                        batch_out.append(copy.deepcopy(src))
                    else:
                        x1, y1, x2, y2 = bbox
                        y2 = min(y2 + extra_margin, src.shape[0])
                        fb = [x1, y1, x2, y2]
                        try:
                            res_resized = cv2.resize(res_frame.astype(np.uint8), (x2 - x1, y2 - y1))
                            key = (id(src), x1, y1, x2, y2)
                            cached = _mask_cache.get(key)
                            if cached is None:
                                cached = get_image_prepare_material(src, fb, mode=parsing_mode, fp=fp)
                                _mask_cache[key] = cached
                            mask_array, crop_box = cached
                            batch_out.append(_blend_np(src, res_resized, fb, mask_array, crop_box))
                        except Exception:
                            batch_out.append(copy.deepcopy(src))
                # 整批 GFPGAN 复原(detect-once 共享仿射,摊薄逐帧同步/启动开销 → ~25fps HD)
                if _enh_on and batch_out:
                    try:
                        if _enh_prep is None:            # detect-once：首帧定仿射/羽化 mask,后续复用
                            _pp_t = time.time()
                            _enh_prep = _face_enh.prepare(batch_out[0])
                            if prof is not None:
                                prof["gfpgan_prep_ms"] = round((time.time() - _pp_t) * 1000, 1)
                        if _enh_prep is not None:
                            batch_out = _face_enh.enhance_batch(batch_out, _enh_prep, _ENH_BLEND)
                    except Exception as _ee:
                        logger.warning(f"[stream] 同进程批量增强失败,用原帧: {_ee}")
                for frame_out in batch_out:
                    seg_buf.append(frame_out)
                    _target = first_seg_frames if seg_idx == 0 else seg_frames
                    if len(seg_buf) >= _target:
                        if seg_idx == 0 and prof is not None:   # 首段生成就绪(未含编码/推送)时刻
                            prof["seg0_gen_ms"] = round((time.time() - prof.get("t_enter", _t_loop)) * 1000, 1)
                        _pushq.put((seg_buf, seg_idx)); seg_idx += 1; seg_buf = []
                _b_ms = (time.time() - _bt) * 1000
                if _first_batch_ms is None: _first_batch_ms = _b_ms
                if _b_ms > _max_batch_ms: _max_batch_ms = _b_ms
                _bidx += 1
            if seg_buf:
                _pushq.put((seg_buf, seg_idx)); seg_idx += 1
            _tp["gen"] = time.time() - _t_loop
            if prof is not None:
                prof["gen_ms"] = round(_tp["gen"] * 1000, 1)
                prof["first_batch_ms"] = round(_first_batch_ms or 0.0, 1)
                prof["max_batch_ms"] = round(_max_batch_ms, 1)
                prof["n_batches"] = _bidx
            _pushq.put(None); _pt.join(timeout=30)
            _nframes = fi
            logger.info(f"[stream] bs={batch_size} {fi} frames in {seg_idx} segs, "
                        f"{(time.time()-_t0)/max(1,fi)*1000:.1f}ms/frame (gen)")
            return b""

        # 4. UNet 批推理
        video_num = len(whisper_chunks)
        gen = datagen(whisper_chunks, latent_cycle, batch_size=batch_size,
                      delay_frame=0, device=_device)
        res_frames = []
        for whisper_batch, latent_batch in gen:
            audio_feat = _pe(whisper_batch)
            latent_batch = latent_batch.to(dtype=torch.float32)
            pred = _unet.model(latent_batch, _timesteps,
                               encoder_hidden_states=audio_feat).sample
            recon = _vae.decode_latents(pred)
            res_frames.extend(recon)
        try: torch.cuda.synchronize()
        except Exception: pass
        _tp["unet_vae"] = time.time() - _t1; _t2 = time.time()

        # 5. 将生成嘴型贴回原图
        #   关键提速：FaceParsing 解析的是「原图人脸裁剪」——静态形象每帧完全相同，
        #   故每个唯一(原帧, bbox) 只解析一次缓存 mask，后续帧复用 get_image_blending
        #   (跳过解析)。静态头像 = 150 次解析降到 1 次，blend 由 ~46ms/帧 → ~个位数 ms/帧。
        fp = _get_fp()
        out_frames = []
        _mask_cache: dict = dict(_mask_seed)
        for i, res_frame in enumerate(res_frames):
            bbox = coord_cycle[i % len(coord_cycle)]
            src  = frame_cycle[i % len(frame_cycle)]
            if bbox == coord_placeholder:
                out_frames.append(copy.deepcopy(src))
                continue
            x1, y1, x2, y2 = bbox
            y2 = min(y2 + extra_margin, src.shape[0])
            fb = [x1, y1, x2, y2]
            try:
                res_resized = cv2.resize(res_frame.astype(np.uint8), (x2 - x1, y2 - y1))
            except Exception:
                out_frames.append(copy.deepcopy(src))
                continue
            key = (id(src), x1, y1, x2, y2)
            cached = _mask_cache.get(key)
            if cached is None:
                cached = get_image_prepare_material(src, fb, mode=parsing_mode, fp=fp)
                _mask_cache[key] = cached
            mask_array, crop_box = cached
            combined = _blend_np(src, res_resized, fb, mask_array, crop_box)
            out_frames.append(combined)

        if not out_frames:
            raise ValueError("推理输出为空")
        _tp["blend"] = time.time() - _t2; _t3 = time.time()

        # 6. 编码为 MP4
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            tmp_vid = f.name

        h, w = out_frames[0].shape[:2]
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(tmp_vid, fourcc, fps, (w, h))
        for frame in out_frames:
            # get_image() 返回的已是 BGR（专为 cv2.VideoWriter 设计），直接写入。
            # 此前多做一次 RGB2BGR 会把 R/B 调换 → 整张图偏蓝。
            writer.write(frame)
        writer.release()

        # 6.5 人脸增强（可选）：在合并音频前，把无声视频送 GFPGAN 服务复原，提清晰度
        if enhance:
            enh_vid = _enhance_video_bytes(tmp_vid)
            if enh_vid != tmp_vid:
                try:
                    if tmp_vid.startswith(tempfile.gettempdir()): os.unlink(tmp_vid)
                except Exception: pass
                tmp_vid = enh_vid

        # 7. 合并音频（ffmpeg）
        with tempfile.NamedTemporaryFile(suffix="_final.mp4", delete=False) as f:
            tmp_final = f.name
        if not _mux_audio_video(tmp_vid, audio_path, tmp_final):
            logger.warning("ffmpeg mux skipped, returning video-only")
        output_path = tmp_final if os.path.exists(tmp_final) and os.path.getsize(tmp_final) > 1000 else tmp_vid

        with open(output_path, "rb") as f:
            video_bytes = f.read()

        _tp["encode_mux"] = time.time() - _t3
        _nframes = len(out_frames)
        logger.info(f"[profile] frames={len(out_frames)} "
                    + " ".join(f"{k}={v:.2f}s" for k, v in _tp.items())
                    + f" | per-frame={(time.time()-_t0)/max(1,len(out_frames))*1000:.1f}ms")
        return video_bytes

    finally:
        if tmp_face:
            try: os.unlink(tmp_face)
            except: pass
        # 释放本轮累积的大块帧列表（res_frames/out_frames 可达数百帧）
        try:
            res_frames = None; out_frames = None
        except Exception:
            pass
        try:
            # GC 节流：累计渲染帧数达阈值才全量 gc.collect()(~188ms)，把它从「每块」摊薄到
            # 「每 ~阈值帧一次」。流式小块由此省下大头固定开销 → 单卡口型实时余量大增。
            global _frames_since_gc
            _frames_since_gc += _nframes
            if _GC_FRAME_THRESHOLD <= 0 or _frames_since_gc >= _GC_FRAME_THRESHOLD:
                gc.collect()
                _frames_since_gc = 0
            # 注意：默认不调 torch.cuda.empty_cache()。实测每轮生成后清空显存池，会把
            #   UNet/VAE 工作区还给驱动；下轮首句需在显存吃紧(LLM 占 ~17.8GB)下重新申请大块
            #   + 重建 cudnn 工作区 → 首句 UNet/VAE 冷启 ~32s。PyTorch 缓存分配器本就复用，
            #   保留显存池可让口型稳态常驻(首句 32s → ~2s)。真要省显存用 LIPSYNC_IDLE_UNLOAD
            #   (空闲整体卸载)，而非每轮 empty_cache。LIPSYNC_EMPTY_CACHE=1 可恢复旧行为。
            if os.environ.get("LIPSYNC_EMPTY_CACHE", "0") == "1":
                torch.cuda.empty_cache()
                torch.cuda.ipc_collect()
        except Exception:
            pass
        _infer_leave()


# ── FastAPI 端点 ───────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {
        "ok": True,
        "models_loaded": _models_loaded,
        "device": _device,
        "service": "lipsync"
    }


@app.get("/meminfo")
def meminfo():
    info = {"service": "lipsync"}
    try:
        import psutil, os as _os
        mi = psutil.Process(_os.getpid()).memory_info()
        info["rss_mb"] = round(mi.rss / 1048576, 1)
        info["vms_mb"] = round(getattr(mi, "vms", 0) / 1048576, 1)
    except Exception:
        pass
    try:
        if torch.cuda.is_available():
            info["gpu_alloc_mb"] = round(torch.cuda.memory_allocated() / 1048576, 1)
            info["gpu_reserved_mb"] = round(torch.cuda.memory_reserved() / 1048576, 1)
    except Exception:
        pass
    return info


@app.post("/gc")
def gc_endpoint(hard: bool = False):
    """非侵入式回收，不卸载模型。
    默认「软回收」：只 gc.collect()(释放 Python 帧缓存→降进程提交内存)，
      不动 torch 显存池——因为看门狗多按「提交内存(RAM)」阈值调用，而 empty_cache
      只把 VRAM 缓存还给驱动、对 RAM 几乎无效，却会让下次口型 UNet/VAE 在显存吃紧下
      重新申请大块 → 实测首句冷启 ~25-35s。保住显存池可使实时口型常驻热态。
    hard=1：真有 VRAM 紧急(别的进程要显存)时才 empty_cache。"""
    before = None
    try:
        if hard and torch.cuda.is_available():
            before = torch.cuda.memory_reserved()
    except Exception:
        before = None
    n = gc.collect()
    freed_mb = None
    try:
        if hard and torch.cuda.is_available():
            torch.cuda.empty_cache()
            torch.cuda.ipc_collect()
            if before is not None:
                freed_mb = round((before - torch.cuda.memory_reserved()) / 1048576, 1)
    except Exception:
        pass
    return {"ok": True, "gc_objects": n, "soft": not hard,
            "gpu_reserved_freed_mb": freed_mb}


@app.post("/warmup")
def warmup_endpoint():
    """重新预热 UNet/VAE(在 GPU 工作线程内跑一次假生成)。供 Hub 在「LLM 已载入显存后」
    调用：LLM 占用 ~17.8GB 会挤动 MuseTalk 显存分配，启动期 lipsync 自预热可能被随后
    的 LLM 载入打乱→首句对话冷启 ~33s。LLM 常驻后再 warmup 一次，使热态稳定持久。"""
    if not _models_loaded:
        return {"ok": False, "reason": "models not loaded yet"}
    t0 = time.time()
    try:
        _gpu_run_sync(_warmup_inference, priority=GPU_PRIO_WARM)
        return {"ok": True, "elapsed": round(time.time() - t0, 2)}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


@app.get("/lipsync/status")
def lipsync_status():
    return {
        "models_loaded": _models_loaded,
        "load_error": _load_error,
        "device": _device
    }


@app.post("/lipsync/preload")
def lipsync_preload(background_tasks: BackgroundTasks):
    if _models_loaded:
        return {"ok": True, "detail": "已加载"}
    background_tasks.add_task(_load_models)
    return {"ok": True, "detail": "后台加载中"}


@app.post("/lipsync/generate")
async def lipsync_generate(
    audio: UploadFile = File(..., description="WAV/MP3 音频文件"),
    face:  UploadFile = File(None, description="人脸图片 JPG/PNG（与 face_id 二选一）"),
    face_id:      str = Form(""),   # 已预计算的人脸缓存 ID
    fps:          int = Form(25),
    bbox_shift:   int = Form(0),
    batch_size:   int = Form(8),
    extra_margin: int = Form(10),
    parsing_mode: str = Form("jaw"),
    enhance:      str = Form(""),   # "gfpgan"→人脸增强复原（更清晰，约 +30ms/帧）
):
    """
    输入: audio (WAV) + face (JPG/PNG)
    输出: MP4 视频字节流 (Content-Type: video/mp4)
    """
    _note_live()                                    # 标记直播活跃：后台 GPU 任务让路
    _ensure_models()

    # 优先使用缓存的人脸数据
    cached_face_data = _face_cache_get(face_id) if face_id else None
    if cached_face_data:
        face_img = None
        logger.info(f"Cache hit for face_id={face_id}")
    else:
        if face is None:
            raise HTTPException(400, "未提供人脸图片，且 face_id 缓存未命中")
        face_bytes = await face.read()
        face_arr = np.frombuffer(face_bytes, np.uint8)
        face_img = cv2.imdecode(face_arr, cv2.IMREAD_COLOR)
        if face_img is None:
            raise HTTPException(400, "无法解码人脸图片")

    # 保存音频到临时文件
    audio_bytes = await audio.read()
    suffix = os.path.splitext(audio.filename or "audio.wav")[1] or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(audio_bytes)
        tmp_audio = f.name

    try:
        t0 = time.time()
        video_bytes = await _run_in_gpu(
            _run_lipsync,
            face_img, tmp_audio,
            fps=fps, bbox_shift=bbox_shift,
            batch_size=batch_size, extra_margin=extra_margin,
            parsing_mode=parsing_mode,
            face_data=cached_face_data,
            enhance=enhance,
            priority=GPU_PRIO_LIVE,                     # 整句生成(对话)=直播优先,排在后台预计算前
        )
        elapsed = time.time() - t0
        logger.info(f"lipsync done in {elapsed:.1f}s, {len(video_bytes)//1024}KB")
        return Response(
            content=video_bytes,
            media_type="video/mp4",
            headers={"X-Processing-Time": f"{elapsed:.2f}s"}
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("lipsync error")
        raise HTTPException(500, str(e))
    finally:
        try: os.unlink(tmp_audio)
        except: pass


@app.post("/lipsync/generate_stream")
async def lipsync_generate_stream(
    audio: UploadFile = File(..., description="WAV/MP3 音频文件"),
    face:  UploadFile = File(None),
    face_id:      str = Form(""),
    fps:          int = Form(25),
    batch_size:   int = Form(8),
    extra_margin: int = Form(10),
    parsing_mode: str = Form("jaw"),
    seg_frames:   int = Form(25),   # 每段帧数（默认 1s）
    first_seg_frames: int = Form(15),  # 首段帧数（更短→首帧更快）
    vcam_url:     str = Form(""),   # 段落直推的广播中枢；空=用默认 VCAM_URL
    enhance:      str = Form(""),   # "gfpgan"→流式 HD：每段过 GFPGAN 复原(更清晰)；空=不增强
    push_segs:    bool = Form(True),  # False=只生成不推 vcam(热启动预热 UNet 编译，不污染直播画面)
):
    """流式生成：边生成边把无声小片段推到广播中枢(/play)，首帧延迟≈首段(~1s)。
    整句音频挂到第 0 段，由中枢喂 WebRTC 音轨 + OBS 桌面声，全程 A/V 同步。"""
    _note_live()                                    # 标记直播活跃：后台 GPU 任务让路
    _ensure_models()
    target = (vcam_url or VCAM_URL).rstrip("/")
    # P-Harden3: 中枢已确认不可达(断路器打开且探活仍不通) → 503 快速失败，
    # 不渲染注定推不出去的句子，把 GPU 留给能出活的任务（vcam 恢复后自动闭合放行）。
    if push_segs and not await asyncio.to_thread(_push_breaker_gate, target):
        raise HTTPException(503, f"广播中枢(vcam {target})不可达：推流断路器打开，"
                                 f"本句拒绝渲染以保 GPU；恢复 vcam 后自动闭合")

    cached_face_data = _face_cache_get(face_id) if face_id else None
    if cached_face_data:
        face_img = None
    else:
        if face is None:
            raise HTTPException(400, "未提供人脸图片，且 face_id 缓存未命中")
        face_img = cv2.imdecode(np.frombuffer(await face.read(), np.uint8), cv2.IMREAD_COLOR)
        if face_img is None:
            raise HTTPException(400, "无法解码人脸图片")

    audio_bytes = await audio.read()
    suffix = os.path.splitext(audio.filename or "audio.wav")[1] or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
        f.write(audio_bytes); tmp_audio = f.name

    try:
        t0 = time.time()
        seg_timings = []
        _prof = {}                                   # 9s尖峰专查：GPU队列等待/whisper/首批/各批max/GFPGAN/编码/首段 分段计时

        def _sink(seg_bytes: bytes, seg_idx: int):
            seg_timings.append({"idx": seg_idx, "t_ms": round((time.time() - t0) * 1000, 1)})
            if not push_segs:                             # 热启动预热：只计时，不推 vcam
                return
            # 在 GPU 线程内同步推送；中枢 /play 仅落盘入队即返回(~毫秒)，不阻塞生成
            try:
                files = {"video": (f"seg{seg_idx}.mp4", seg_bytes, "video/mp4")}
                if seg_idx == 0 and audio_bytes:          # 整句音频只挂第 0 段
                    files["audio"] = ("a.wav", audio_bytes, "audio/wav")
                r = requests.post(f"{target}/play", files=files,
                                  headers=_vcam_push_headers(), timeout=10)
                if r.status_code != 200:                  # 4xx/5xx 不抛异常：显式亮出来，勿静默丢段
                    logger.warning(f"[stream] 段{seg_idx} 被拒 HTTP {r.status_code}: {r.text[:120]}")
                else:
                    _push_note(True)                      # P-Harden3: 连接恢复 → 断路器闭合/清零
            except Exception as e:
                _push_note(False, target)                 # P-Harden3: 连接级失败计入断路器
                logger.warning(f"[stream] 段{seg_idx} 推送失败: {e}")

        _t_submit = time.time()
        await _run_in_gpu(
            _run_lipsync, face_img, tmp_audio,
            fps=fps, batch_size=batch_size, extra_margin=extra_margin,
            parsing_mode=parsing_mode, face_data=cached_face_data,
            enhance=enhance,
            seg_sink=_sink, seg_frames=seg_frames, first_seg_frames=first_seg_frames,
            prof=_prof,
            priority=GPU_PRIO_LIVE,                     # 直播流式生成=最高优先,后台预计算让路
        )
        _note_live()                                    # 收尾续标记：覆盖句间空档，后台任务持续让路
        # GPU 队列等待：submit→任务真正开始执行(捕捉「排在运行中的后台预计算/活体导出后」型首帧尖峰)
        if "t_enter" in _prof:
            _prof["gpu_wait_ms"] = round((_prof.pop("t_enter") - _t_submit) * 1000, 1)
        gaps = [seg_timings[i + 1]["t_ms"] - seg_timings[i]["t_ms"]
                for i in range(len(seg_timings) - 1)] if len(seg_timings) > 1 else []
        logger.info(f"[stream/prof] ttfv={seg_timings[0]['t_ms'] if seg_timings else None}ms {_prof}")
        return {"ok": True, "elapsed": round(time.time() - t0, 2), "target": target,
                "seg_count": len(seg_timings), "seg_timings": seg_timings,
                "seg_gap_ms": round(sum(gaps) / len(gaps), 1) if gaps else None,
                "ttfv_ms": seg_timings[0]["t_ms"] if seg_timings else None,
                "prof": _prof}
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("lipsync stream error")
        raise HTTPException(500, str(e))
    finally:
        try: os.unlink(tmp_audio)
        except: pass


# ── 启动时预加载 ───────────────────────────────────────────────────────────
@app.post("/lipsync/precompute_face")
async def lipsync_precompute_face(
    face: UploadFile = File(..., description="人脸图片"),
    face_id: str = Form("default"),
    bbox_shift: int = Form(0),
    extra_margin: int = Form(10),
):
    """
    预计算人脸 latents 并缓存 face_id，后续 /lipsync/generate 传相同 face_id 可跳过人脸检测
    通常在角色激活时调用，节省 15-20s 首次推理时间
    """
    _ensure_models()
    face_bytes = await face.read()
    face_arr = np.frombuffer(face_bytes, np.uint8)
    face_img = cv2.imdecode(face_arr, cv2.IMREAD_COLOR)
    if face_img is None:
        raise HTTPException(400, "无法解码人脸图片")

    await _await_live_idle("precompute_face")       # 直播活跃则让路，避免占住 GPU worker 卡住实时口型
    n = await _run_in_gpu(_precompute_face_sync, face_img, face_id, bbox_shift, extra_margin,
                          priority=GPU_PRIO_BG)         # 人脸预计算=后台低优先,直播句到达即让路
    if n == 0:
        raise HTTPException(400, "未检测到人脸")
    logger.info(f"Face precomputed for id={face_id}: {n} latents")
    return {"ok": True, "face_id": face_id, "latents": n}


def _decode_video_frames(data: bytes, max_frames: int):
    """把视频字节解码成 BGR 帧列表(沿时间轴等距采样到 max_frames，保持正向顺序)。
    供路线A「视频底口型合成」用：真人半身视频帧作为 MuseTalk 基底。"""
    tmp = tempfile.mktemp(suffix=".mp4")
    frames = []
    try:
        with open(tmp, "wb") as f:
            f.write(data)
        cap = cv2.VideoCapture(tmp)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total <= 0:                       # 帧数未知：顺序读到上限
            while len(frames) < max_frames:
                ok, fr = cap.read()
                if not ok:
                    break
                frames.append(fr)
        else:
            if total <= max_frames:
                idxs = list(range(total))
            else:                            # 等距采样(覆盖整段，含自然头动变化)
                step = total / float(max_frames)
                idxs = sorted(set(int(i * step) for i in range(max_frames)))
            for t in idxs:
                cap.set(cv2.CAP_PROP_POS_FRAMES, t)
                ok, fr = cap.read()
                if ok:
                    frames.append(fr)
        cap.release()
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass
    return frames


@app.post("/lipsync/precompute_video")
async def lipsync_precompute_video(
    video: UploadFile = File(..., description="真人半身底视频"),
    face_id: str = Form("default"),
    bbox_shift: int = Form(0),
    extra_margin: int = Form(10),
    max_frames: int = Form(_ALIVE_FRAMES),
):
    """路线A：把真人半身视频解码成基底帧序列并预计算 latents 缓存。
    之后 /lipsync/generate(_stream) 传相同 face_id → 口型直接贴到真人视频帧上
    (保留真人头动/肩动/身体/背景，远胜单图伪活体)。一次性预计算，落盘缓存秒加载。"""
    _ensure_models()
    raw = await video.read()
    mf = max(2, min(int(max_frames), 160))
    frames = await asyncio.to_thread(_decode_video_frames, raw, mf)
    if not frames:
        raise HTTPException(400, "无法解码视频/未取到帧")
    await _await_live_idle("precompute_video")      # 直播活跃则让路，避免占住 GPU worker 卡住实时口型
    n = await _run_in_gpu(_precompute_face_sync, None, face_id,
                          bbox_shift, extra_margin, base_frames=frames, priority=GPU_PRIO_BG)
    if n == 0:
        raise HTTPException(400, "视频中未检测到人脸")
    logger.info(f"Video face precomputed id={face_id}: {n}/{len(frames)} 帧")
    return {"ok": True, "face_id": face_id, "latents": n, "frames": len(frames)}


# ── 待机变体：偶尔「看向别处」(纯头转，程序化驱动，无需驱动视频/模板) ──
#   懒生成 + 进程内缓存(按 face_id)；待机循环拼接在主基底之后，降低「一直正脸」的呆板感。
_IDLE_LOOKAWAY = os.environ.get("LIPSYNC_IDLE_LOOKAWAY", "1") == "1"
_LOOKAWAY_FRAMES = int(os.environ.get("LIPSYNC_LOOKAWAY_FRAMES", "24"))
_LOOKAWAY_YAW = float(os.environ.get("LIPSYNC_LOOKAWAY_YAW", "16"))
_idle_extra_cache = {}   # face_id -> [BGR frame, ...]


def _idle_extra_for(face_id: str):
    """生成/取「看向别处」变体帧（半周期 yaw：正脸→偏转→正脸，首尾≈正脸便于无缝拼接）。
    以基底首帧(≈原始正脸)为源再驱动；进程内按 face_id 缓存，避免重复算。"""
    if not _IDLE_LOOKAWAY:
        return []
    cached = _idle_extra_cache.get(face_id)
    if cached is not None:
        return cached
    base = _idle_frames_for(face_id)
    if not base:
        return []
    try:
        import sys as _sys
        _wd = os.path.dirname(os.path.abspath(__file__))
        if _wd not in _sys.path:
            _sys.path.insert(0, _wd)
        import live_base
        motion = live_base.build_lookaway_motion(_LOOKAWAY_FRAMES, _LOOKAWAY_YAW, 4.0)
        frames = live_base.generate_alive_frames(base[0], motion=motion)
        _idle_extra_cache[face_id] = frames or []
        logger.info(f"[idle] 看向别处变体 {len(frames or [])} 帧 ({face_id})")
        return _idle_extra_cache[face_id]
    except Exception as e:
        logger.warning(f"[idle] 看向别处变体生成失败(忽略): {e}")
        _idle_extra_cache[face_id] = []
        return []


def _encode_idle_mp4(frames, fps: int = 18, max_w: int = 480, max_frames: int = 120,
                     pingpong: bool = True) -> bytes:
    """把活体基底帧编码为无声循环 MP4（待机展示用）。
    pingpong=True: 用满活体帧(上限 max_frames) + 乒乓倒放，循环更长更自然。
    pingpong=False: frames 已是预拼接好的完整循环序列，按原样编码(不再截断/倒放)。"""
    if not frames:
        return b""
    h0, w0 = frames[0].shape[:2]
    scale = min(1.0, max_w / max(w0, 1))
    w, h = max(2, int(w0 * scale)), max(2, int(h0 * scale))
    if pingpong:
        # 用满活体帧 + 乒乓倒放，降低循环感（78 帧 → 乒乓 154 帧 ≈ 8.5s @18fps）
        seq = list(frames[: min(max_frames, len(frames))])
        if len(seq) > 2:
            seq = seq + seq[-2:0:-1]
    else:
        seq = list(frames)
    tmp_path = tempfile.mktemp(suffix=".mp4")
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    wr = cv2.VideoWriter(tmp_path, fourcc, fps, (w, h))
    if not wr.isOpened():
        return b""
    try:
        for fr in seq:
            if fr.shape[1] != w or fr.shape[0] != h:
                fr = cv2.resize(fr, (w, h), interpolation=cv2.INTER_AREA)
            wr.write(fr)
    finally:
        wr.release()
    try:
        with open(tmp_path, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def _idle_frames_for(face_id: str):
    """从内存/磁盘 alive 缓存取 frame_list。"""
    data = _face_cache_get(face_id)
    if data and data.get("frame_list"):
        return data["frame_list"]
    if _ALIVE_DISK and face_id:
        cp = _alive_cache_path(face_id)
        if os.path.exists(cp):
            try:
                data = _load_alive_cache(cp)
                _face_cache_put(face_id, data)
                return data.get("frame_list") or []
            except Exception:
                pass
    return []


@app.get("/lipsync/idle_loop/{face_id}")
async def lipsync_idle_loop(face_id: str, fps: int = 18, max_frames: int = 120):
    """导出活体基底循环 MP4（无声），供 phone 待机展示。需先 precompute_face。
    fps: 播放帧率(默认18,眨眼自然不拖沓); max_frames: 用满的最大基底帧数。"""
    await _await_live_idle("idle_loop")             # 直播活跃则让路，避免占住 GPU worker 卡住实时口型
    frames = await _run_in_gpu(lambda: _idle_frames_for(face_id), priority=GPU_PRIO_BG)
    if not frames:
        raise HTTPException(404, "无活体缓存，请先 precompute_face")
    if len(frames) < 2:
        raise HTTPException(404, "仅静态单帧，无法生成循环视频")

    def _pp(seq):
        return seq + seq[-2:0:-1] if len(seq) > 2 else list(seq)

    extra = await _run_in_gpu(lambda: _idle_extra_for(face_id), priority=GPU_PRIO_BG) if _IDLE_LOOKAWAY else []
    if extra and len(extra) > 1:
        # 主基底(摆头/眨眼) 乒乓 → 再接「看向别处」乒乓，首尾≈正脸无缝衔接，整段一次循环
        composed = _pp(list(frames[: min(max_frames, len(frames))])) + _pp(extra)
        mp4 = await _run_in_gpu(lambda: _encode_idle_mp4(composed, fps, 480, max_frames, pingpong=False), priority=GPU_PRIO_BG)
    else:
        mp4 = await _run_in_gpu(lambda: _encode_idle_mp4(frames, fps, 480, max_frames), priority=GPU_PRIO_BG)
    if not mp4:
        raise HTTPException(500, "MP4 编码失败")
    from fastapi.responses import Response
    return Response(content=mp4, media_type="video/mp4",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.on_event("startup")
async def startup():
    logger.info("Starting LipSync Server, preloading models in background...")
    t = threading.Thread(target=_load_models, daemon=True)
    t.start()
    if _IDLE_UNLOAD > 0:
        logger.info(f"空闲自动卸载已启用: {_IDLE_UNLOAD:.0f}s")
        threading.Thread(target=_idle_watch, daemon=True).start()


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0",
                port=int(os.environ.get("LIPSYNC_PORT", "8090")), log_level="info")
