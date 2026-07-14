# -*- coding: utf-8 -*-
"""
虚拟背景（Phase 12 C-1）—— 换脸之后、虚拟摄像头之前的背景替换层
================================================================
设计要点（ADR-12-01 / ADR-12-02）：
  · 接在 realtime_stream vcam_worker 的输出帧上：换脸融合区不受抠像影响；
  · 双引擎（BG_ENGINE=auto|rvm|mediapipe，默认 auto）：
    - rvm：RobustVideoMatting mobilenetv3-fp16 TorchScript @ CUDA，720p 全分辨率 α，
      全链 ~6ms/帧(5090)，自带时域记忆(免 EMA)，发丝级边缘；换脸已迁至 .104 后
      本机 GPU 有此余量（2026-07-07 跨机算力评估的第①阶段落地）。
      后端选型：ONNXRuntime-CUDA 实测全链 29ms——CPU 侧 fp16 预处理比推理还贵；
      TorchScript 把 BGR→RGB/归一化全搬进 GPU(uint8 上传后原地转)，快 4.8×，
      且 torch-cu128 自带全套 CUDA DLL(Blackwell 原生)。模型 ~8MB 缺失自动下载。
    - mediapipe：SelfieSegmentation（CPU/XNNPACK，~3-6ms@256px），零 GPU 占用。
    auto=RVM 后台预热，就绪前/失败时用 mediapipe 顶着，运行中出错也自动回退——
    任何情况下不断流；BG_ENGINE=mediapipe 一键回旧引擎。
    mediapipe 模型 selfie_segmenter_landscape.tflite(~250KB) 放 models/，缺失自动下载；
  · 掩码时域 EMA + 边缘羽化：防逐帧闪烁与硬边；EMA 后按模式收紧键控边缘
    （image/blur=窄软边，green=二值硬边给下游色键；BG_EDGE_TIGHT=0 回旧行为）；
  · 实体绿幕色度精修（BG_CHROMA_REFINE，默认开）：边缘带内绿像素判背景+去溢色，
    身后有真绿幕时边缘到发丝级；无绿幕时零作用；
  · 背景素材支持静图(jpg/png/webp)与动图/视频(gif/mp4/webm/mov/avi)——视频经
    VideoCapture 按源帧率墙钟节拍循环播放（2026-07-07 动态背景需求）；
  · 模式 none/blur/image/green，运行时热切（/bg/set），设置持久化 bg_settings.json；
  · 默认 none = 零回归零开销（process() 直接原样返回）。

线程模型：process() 仅由 vcam 输出线程调用（MediaPipe graph 线程亲和）；
set_config()/status() 可被 HTTP 线程调用（仅写普通标量，锁保护）。
"""
import os
import json
import time
import threading

import cv2
import numpy as np


def _log(msg: str):
    """GBK 控制台安全打印（emoji/特殊符号在 cp936 下会 UnicodeEncodeError 崩掉调用方）。"""
    try:
        print(msg, flush=True)
    except Exception:
        try:
            print(msg.encode("gbk", "replace").decode("gbk"), flush=True)
        except Exception:
            pass

try:
    import app_config
    _BASE = str(app_config.BASE)
except Exception:
    _BASE = os.path.dirname(os.path.abspath(__file__))

SETTINGS_PATH = os.path.join(_BASE, "bg_settings.json")
BG_IMAGE_DIR = os.path.join(_BASE, "bg_images")

MODES = ("none", "blur", "image", "green")
_GREEN_BGR = (0, 255, 0)          # OBS 色键抠像标准绿
_SEG_W = 256                      # 分割输入降采样宽(px)：精度/耗时平衡点

# 背景素材类型（image 模式统一收静图+动图/视频，按后缀分流；本机 OpenCV-ffmpeg
# 实测可解码 gif/mp4/webm/mov/avi 且中文路径直开 OK——见 tools/_bg_video_capability.py）
_IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp")
_VID_EXTS = (".gif", ".mp4", ".webm", ".mov", ".avi", ".m4v")

# 边缘收紧（2026-07-07 虚边反馈）：256px 模型掩码经 7px 羽化 + 上采样到 720p 后，
# 人物边缘过渡带宽达 ~30px（一圈"毛玻璃"晕边），移动时 EMA 拖尾进一步加宽。
# 对策：EMA 之后按模式做键控掩码——image/blur 斜率放大(软阈值)把过渡带压到几像素；
# green 是给下游色键(OBS)的，软边=色键后绿晕/吃边，须二值化+收边出硬边再 3×3 羽化抗锯齿。
_EDGE_TIGHT   = os.environ.get("BG_EDGE_TIGHT", "1") == "1"    # 逃生门：0=回旧版宽软边
_EDGE_SLOPE   = max(1.0, float(os.environ.get("BG_EDGE_SLOPE", "6")))
_GREEN_THRESH = min(0.95, max(0.05, float(os.environ.get("BG_GREEN_THRESH", "0.5"))))
_GREEN_ERODE  = max(0, int(os.environ.get("BG_GREEN_ERODE", "1")))

# 物理绿幕色度精修（2026-07-07）：主播身后挂了实体绿幕时，沿模型轮廓划一圈
# 几何"重审带"(trimap ring：向人像内缩 BG_CHROMA_IN px、向背景外扩 BG_CHROMA_OUT px)，
# 带内一律按"绿色度"(G-max(R,B)) 重新裁决 α——纯绿归背景(连模型自信的误判也切掉)，
# 再对 0<α<1 的边缘像素做 AGED 式去溢色(G 压到 max(R,B)，内部像素零污染)。
# 画面无绿幕 → 绿色度≈0 → 数学上零作用，普通房间用户无感。BG_CHROMA_REFINE=0 关闭。
_CHROMA_REFINE = os.environ.get("BG_CHROMA_REFINE", "1") == "1"
_CHROMA_T0 = float(os.environ.get("BG_CHROMA_T0", "12"))   # 绿色度起算阈(低于视作非绿)
_CHROMA_T1 = float(os.environ.get("BG_CHROMA_T1", "60"))   # 绿色度饱和阈(高于视作纯绿幕)
_CHROMA_IN  = max(2, int(os.environ.get("BG_CHROMA_IN", "25")))   # 重审带向人像内伸(px)
_CHROMA_OUT = max(2, int(os.environ.get("BG_CHROMA_OUT", "9")))   # 重审带向背景外伸(px)

# MediaPipe Tasks 人像分割模型（landscape 变体针对 144×256 视频帧优化）
_SEG_MODEL_PATH = os.environ.get(
    "BG_SEG_MODEL", os.path.join(_BASE, "models", "selfie_segmenter_landscape.tflite"))
_SEG_MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/image_segmenter/"
                  "selfie_segmenter_landscape/float16/latest/selfie_segmenter_landscape.tflite")

# RVM 神经网络抠像引擎（ADR-12-02）：auto=可用即用 RVM,失败回 mediapipe；显式指定则强制
_ENGINE = os.environ.get("BG_ENGINE", "auto").strip().lower()
if _ENGINE not in ("auto", "rvm", "mediapipe"):
    _ENGINE = "auto"
_RVM_MODEL_PATH = os.environ.get(
    "BG_RVM_MODEL", os.path.join(_BASE, "models", "rvm_mobilenetv3_fp16.torchscript"))
_RVM_URLS = (
    "https://github.com/PeterL1n/RobustVideoMatting/releases/download/v1.0.0/rvm_mobilenetv3_fp16.torchscript",
    "https://ghfast.top/https://github.com/PeterL1n/RobustVideoMatting/releases/download/v1.0.0/rvm_mobilenetv3_fp16.torchscript",
    "https://gh-proxy.com/https://github.com/PeterL1n/RobustVideoMatting/releases/download/v1.0.0/rvm_mobilenetv3_fp16.torchscript",
)
_RVM_DS = float(os.environ.get("BG_RVM_DS", "0"))   # 0=按分辨率自选(720p→0.375,1080p→0.25)
# RVM 预热显存门禁（2026-07-10 事故复盘）：显卡近满(31/32G)时预热(建 CUDA 上下文 +
# cudnn.benchmark 试 720p 推理)会在原生层崩掉——Python 的 try/except 接不住，整个
# realtime_stream 进程被带走(直播断流)。故预热前先查空闲显存：不足 → 本轮不上 RVM，
# mediapipe(CPU) 顶着，每 _RVM_VRAM_RETRY_S 复查一次，显存宽裕后自动升级到 RVM。
# 查不到显存(无 nvidia-smi/超时)按旧行为放行，不误伤无 N 卡工具链的环境。
# BG_RVM_MIN_FREE_MB=0 关闭门禁(回旧行为)。
_RVM_MIN_FREE_MB = max(0, int(os.environ.get("BG_RVM_MIN_FREE_MB", "2048")))
_RVM_VRAM_RETRY_S = max(5.0, float(os.environ.get("BG_RVM_VRAM_RETRY_S", "30")))


def _gpu_free_mb() -> int:
    """cuda:0 当前空闲显存(MB)。查不到(无 nvidia-smi/超时/输出异常)返回 -1=未知。"""
    try:
        import subprocess
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 0:
            return int(r.stdout.strip().splitlines()[0].strip())
    except Exception:
        pass
    return -1


def _ensure_rvm_model() -> str:
    """确保 RVM TorchScript 模型就位（~8MB），多镜像下载一次。失败返回空串。"""
    if os.path.exists(_RVM_MODEL_PATH) and os.path.getsize(_RVM_MODEL_PATH) > 1_000_000:
        return _RVM_MODEL_PATH
    os.makedirs(os.path.dirname(_RVM_MODEL_PATH), exist_ok=True)
    import urllib.request
    for url in _RVM_URLS:
        try:
            _log(f"[BG] 下载 RVM 抠像模型(~8MB) ← {url.split('/')[2]}")
            tmp = _RVM_MODEL_PATH + ".part"
            urllib.request.urlretrieve(url, tmp)
            os.replace(tmp, _RVM_MODEL_PATH)
            return _RVM_MODEL_PATH
        except Exception as e:
            _log(f"[BG] RVM 模型下载失败({str(e)[:60]})，换镜像重试")
    return ""


def _ensure_seg_model() -> str:
    """确保分割模型就位：本地有→直接用；无→自动下载一次(~250KB)。失败返回空串。"""
    if os.path.exists(_SEG_MODEL_PATH) and os.path.getsize(_SEG_MODEL_PATH) > 10000:
        return _SEG_MODEL_PATH
    try:
        os.makedirs(os.path.dirname(_SEG_MODEL_PATH), exist_ok=True)
        import urllib.request
        _log(f"[BG] 首次使用：下载人像分割模型(~250KB) → {_SEG_MODEL_PATH}")
        tmp = _SEG_MODEL_PATH + ".part"
        urllib.request.urlretrieve(_SEG_MODEL_URL, tmp)
        os.replace(tmp, _SEG_MODEL_PATH)
        return _SEG_MODEL_PATH
    except Exception as e:
        _log(f"[BG] 模型下载失败({str(e)[:80]})。可手动下载后放到 {_SEG_MODEL_PATH}\n"
             f"     URL: {_SEG_MODEL_URL}")
        return ""


class BackgroundReplacer:
    def __init__(self):
        self._lock = threading.Lock()
        self.mode = os.environ.get("BG_MODE", "none").strip().lower()
        if self.mode not in MODES:
            self.mode = "none"
        self.image_name = os.environ.get("BG_IMAGE", "").strip()
        self.blur_sigma = max(3, int(os.environ.get("BG_BLUR", "17")))
        self.seg_every = max(1, int(os.environ.get("BG_SEG_EVERY", "1")))
        self.mask_ema = min(0.95, max(0.1, float(os.environ.get("BG_MASK_EMA", "0.6"))))
        self.edge_blur = max(0, int(os.environ.get("BG_EDGE_BLUR", "7")))

        self._seg = None              # mediapipe Tasks ImageSegmenter（vcam 线程内惰性建）
        self._mp = None               # mediapipe 模块引用(建 Image 用)
        self._seg_failed = ""         # 引擎不可用原因（模型缺失等）→ 自动降级不替换
        self._rvm = None              # RVM 播放态：{"sess","rec","wh","last_t"}；就绪前为 None
        self._rvm_failed = ""         # RVM 不可用原因 → 永久回退 mediapipe(本次进程内)
        self._rvm_warming = False     # 后台预热中(防重复起线程)
        self._rvm_hold = ""           # 显存门禁暂缓原因(非永久失败)；空=未被暂缓
        self._rvm_next_try = 0.0      # 门禁复查时刻(monotonic)；之前不再起预热线程
        self._engine_live = ""        # 本帧实际用的引擎("rvm"/"mediapipe")，供状态与键控策略
        self._mask = None             # 输出分辨率的 float32 掩码(EMA 后)
        self._key = None              # (键控掩码, 反相) 缓存：EMA 掩码按模式收紧后的成品,掩码/模式不变免重算
        # 色度重审带的形态学核。矩形核走 van Herk 恒时算法,50px 级也只要 ~0.5ms(椭圆核会慢一个量级)
        self._k_in = cv2.getStructuringElement(cv2.MORPH_RECT, (_CHROMA_IN * 2 + 1, _CHROMA_IN * 2 + 1))
        self._k_out = cv2.getStructuringElement(cv2.MORPH_RECT, (_CHROMA_OUT * 2 + 1, _CHROMA_OUT * 2 + 1))
        self._frame_i = 0
        self._bg_cache = None         # (key, 图) 背景图缓存（按输出尺寸+文件名）
        self._vid = None              # 视频背景播放态(仅 vcam 线程触碰)：cap/fps/节拍/当前帧
        self._ms_ewma = 0.0           # 每帧处理耗时 EWMA
        self._load_settings()

    # ── 设置持久化 ─────────────────────────────────────────────
    def _load_settings(self):
        try:
            if os.path.exists(SETTINGS_PATH):
                with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                    s = json.load(f)
                if not os.environ.get("BG_MODE"):          # 显式 env 仍优先
                    m = str(s.get("mode", "")).lower()
                    if m in MODES:
                        self.mode = m
                if not os.environ.get("BG_IMAGE"):
                    self.image_name = str(s.get("image", "") or "")
                if s.get("blur_sigma"):
                    self.blur_sigma = max(3, int(s["blur_sigma"]))
                if s.get("seg_every"):
                    self.seg_every = max(1, int(s["seg_every"]))
        except Exception:
            pass

    def _save_settings(self):
        try:
            with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
                json.dump({"mode": self.mode, "image": self.image_name,
                           "blur_sigma": self.blur_sigma, "seg_every": self.seg_every},
                          f, ensure_ascii=False)
        except Exception:
            pass

    # ── 运行时控制（HTTP 线程调用）────────────────────────────
    def set_config(self, mode=None, image=None, blur=None, every=None) -> dict:
        with self._lock:
            if mode is not None:
                m = str(mode).strip().lower()
                if m not in MODES:
                    return {"ok": False, "detail": f"mode 须为 {'/'.join(MODES)}"}
                self.mode = m
            if image is not None:
                self.image_name = str(image).strip()
                self._bg_cache = None
            self._key = None          # 模式/参数变了→键控掩码按新模式重算(绿幕硬边 vs 图片软边)
            if blur is not None:
                try:
                    self.blur_sigma = max(3, min(99, int(blur)))
                except Exception:
                    pass
            if every is not None:
                try:
                    self.seg_every = max(1, min(10, int(every)))
                except Exception:
                    pass
            if self.mode == "image" and not self._resolve_image_path():
                return {"ok": False, "detail": f"背景图不存在：{self.image_name or '(未选)'}；"
                                               f"请放入 {BG_IMAGE_DIR}", **self.status()}
            self._save_settings()
        if self.mode != "none" and self._seg is None and not self._seg_failed:
            # 模型下载移出 vcam 热路径：后台先备好文件，process() 首帧只做本地加载
            threading.Thread(target=_ensure_seg_model, daemon=True).start()
        if self.mode != "none" and _ENGINE in ("auto", "rvm"):
            self._rvm_warmup_async()      # 开启即预热,首帧就能用上 RVM(而非顶几秒 mediapipe)
        return {"ok": True, **self.status()}

    def status(self) -> dict:
        imgs = []
        try:
            if os.path.isdir(BG_IMAGE_DIR):
                imgs = sorted(fn for fn in os.listdir(BG_IMAGE_DIR)
                              if fn.lower().endswith(_IMG_EXTS + _VID_EXTS))[:50]
        except Exception:
            pass
        kind = ("video" if self.image_name.lower().endswith(_VID_EXTS)
                else ("image" if self.image_name else ""))
        return {"enabled": self.mode != "none", "mode": self.mode,
                "image": self.image_name, "image_kind": kind, "images_available": imgs,
                "image_dir": BG_IMAGE_DIR, "blur_sigma": self.blur_sigma,
                "seg_every": self.seg_every, "ms": round(self._ms_ewma, 1),
                "edge": {"tight": _EDGE_TIGHT, "slope": _EDGE_SLOPE,
                         "green_thresh": _GREEN_THRESH, "green_erode": _GREEN_ERODE,
                         "chroma_refine": _CHROMA_REFINE},
                "engine": self._engine_live or ("mediapipe" if not self._seg_failed else ""),
                "engine_pref": _ENGINE,
                "rvm": {"ready": self._rvm is not None, "warming": self._rvm_warming,
                        "error": self._rvm_failed, "hold": self._rvm_hold},
                "error": self._seg_failed}

    # ── 内部：背景源 ───────────────────────────────────────────
    def _resolve_image_path(self):
        nm = self.image_name
        if not nm:
            return None
        p = nm if os.path.isabs(nm) else os.path.join(BG_IMAGE_DIR, nm)
        return p if os.path.exists(p) else None

    def _bg_for(self, frame):
        """按当前模式给出背景画布（与 frame 同尺寸 BGR）。"""
        h, w = frame.shape[:2]
        if self.mode == "green":
            key = ("green", w, h)
            if self._bg_cache and self._bg_cache[0] == key:
                return self._bg_cache[1]
            bg = np.full((h, w, 3), _GREEN_BGR, np.uint8)
            self._bg_cache = (key, bg)
            return bg
        if self.mode == "image":
            if self.image_name.lower().endswith(_VID_EXTS):
                return self._bg_video_frame(w, h)
            self._close_video()                   # 从视频背景切回静图→释放解码器/文件句柄
            key = ("img", self.image_name, w, h)
            if self._bg_cache and self._bg_cache[0] == key:
                return self._bg_cache[1]
            p = self._resolve_image_path()
            img = _imread_any(p) if p else None
            if img is None:                       # 图丢了→退化绿幕（可见即可察觉）
                img = np.full((h, w, 3), _GREEN_BGR, np.uint8)
            else:
                img = _cover_resize(img, w, h)
            self._bg_cache = (key, img)
            return img
        # blur：每帧都得算（背景=实时画面虚化）；stackBlur 比 Gaussian 快 ~3×
        s = self.blur_sigma | 1
        try:
            return cv2.stackBlur(frame, (s * 2 + 1, s * 2 + 1))
        except Exception:
            return cv2.GaussianBlur(frame, (0, 0), s)

    def _close_video(self):
        if self._vid is not None:
            try:
                if self._vid.get("cap") is not None:
                    self._vid["cap"].release()
            except Exception:
                pass
            self._vid = None

    def _bg_video_frame(self, w, h):
        """动图/视频背景：VideoCapture 流式解码，按源帧率的墙钟节拍推进、播完回卷循环。
        输出 fps 与素材 fps 解耦——素材慢则复用当前帧，素材快则单次最多追 3 帧防解码螺旋。
        仅 vcam 线程调用(与 process 同线程)，无锁。打不开/半路坏 → 退化绿幕(可见即可察觉)，
        每 5s 重试一次重开。"""
        p = self._resolve_image_path()
        now = time.monotonic()
        v = self._vid
        if p is None:
            self._close_video()
            return np.full((h, w, 3), _GREEN_BGR, np.uint8)
        if v is None or v["path"] != p:
            self._close_video()
            cap = cv2.VideoCapture(p)
            fps = cap.get(cv2.CAP_PROP_FPS) if cap.isOpened() else 0.0
            fps = fps if 1.0 <= fps <= 120.0 else 25.0     # 坏头/GIF 无标称→合理默认
            v = self._vid = {"path": p, "cap": cap if cap.isOpened() else None,
                             "period": 1.0 / fps, "next_t": now, "raw": None,
                             "frame": None, "wh": (0, 0), "retry_t": now + 5.0}
            if v["cap"] is None:
                cap.release()
                _log(f"[BG] 视频背景打不开: {os.path.basename(p)}")
        if v["cap"] is None:                               # 开失败 → 绿幕 + 周期性重试
            if now >= v["retry_t"]:
                self._vid = None
            return np.full((h, w, 3), _GREEN_BGR, np.uint8)
        if v["frame"] is None or now >= v["next_t"]:
            fr, reads = None, 0
            while True:
                ok, f2 = v["cap"].read()
                if not ok:                                  # 播完(或坏帧)→回卷重播
                    v["cap"].set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ok, f2 = v["cap"].read()
                    v["next_t"] = now                       # 回卷点重置节拍,防负漂移累积
                    if not ok:
                        break
                fr = f2
                reads += 1
                v["next_t"] += v["period"]
                if v["next_t"] > now or reads >= 3:
                    break
            if v["next_t"] <= now:                          # 解码追不上播放速度→重新对表
                v["next_t"] = now + v["period"]
            if fr is not None:
                v["raw"] = fr
                v["frame"] = _cover_resize(fr, w, h)
                v["wh"] = (w, h)
        if v["frame"] is None:                              # 首帧都读不出=文件坏
            return np.full((h, w, 3), _GREEN_BGR, np.uint8)
        if v["wh"] != (w, h):                               # 输出尺寸热变(罕见)→用留存原帧重铺
            v["frame"] = _cover_resize(v["raw"], w, h)
            v["wh"] = (w, h)
        return v["frame"]

    def _chroma_refine(self, frame, m):
        """实体绿幕精修：ML 掩码只认 256px 下的"人形"，边缘晕圈甚至头旁整条绿幕都可能
        被自信地划进人像(线上取证：肩侧 10~20px 纯绿条带 α≈1)。故不能只修"低置信带"——
        沿人像轮廓划几何重审带(内缩 _CHROMA_IN/外扩 _CHROMA_OUT)，带内不管模型多自信，
        一律按绿色度 g-max(r,b) 重新裁决：越绿 α 越低，发丝间隙也能透出新背景。
        只降不升(min)：人像核心区(带外)永不受影响；画面没绿幕时绿色度≈0,零作用。"""
        core = cv2.compare(m, 0.5, cv2.CMP_GT)                  # 人像核心(uint8 0/255)
        ring = cv2.subtract(cv2.dilate(core, self._k_out), cv2.erode(core, self._k_in))
        x, y, w, h = cv2.boundingRect(ring)
        if w == 0 or h == 0:
            return m
        sl = (slice(y, y + h), slice(x, x + w))                 # 逐像素运算收在外接矩形内
        b_ch, g_ch, r_ch = cv2.split(frame[sl])                 # split 出连续数组走 SIMD
        greenness = cv2.subtract(g_ch, cv2.max(b_ch, r_ch))     # 饱和减,非绿=0
        a_bg = np.clip((greenness.astype(np.float32) - _CHROMA_T0)
                       / max(1.0, _CHROMA_T1 - _CHROMA_T0), 0.0, 1.0)
        m2 = m.copy()                                          # 不可原地改 self._mask(EMA 历史)
        np.copyto(m2[sl], np.minimum(m2[sl], 1.0 - a_bg), where=(ring[sl] > 0))
        return m2

    def _despill_edges(self, frame, km):
        """AGED 式 alpha 门控去溢色：仅对 0<α<1 的边缘像素把 G 压到 max(R,B)，
        消掉头发/肩线上的残余绿反光；内部像素一概不动(绿衣服不掉色)。
        写时复制——入参可能是与其它线程共享的帧缓冲，不能原地污染。"""
        sel = cv2.inRange(km, 0.02, 0.98)
        x, y, w, h = cv2.boundingRect(sel)
        if w == 0 or h == 0:
            return frame
        frame = frame.copy()
        fr = frame[y:y + h, x:x + w]
        b_ch, g_ch, r_ch = cv2.split(fr)
        np.copyto(fr[:, :, 1], cv2.min(g_ch, cv2.max(b_ch, r_ch)),
                  where=(sel[y:y + h, x:x + w] > 0))
        return frame

    def _key_masks(self, frame):
        """掩码 → 本模式键控掩码(+反相)。掩码/模式没变时走缓存(免每帧重算)。
        先(可选)实体绿幕色度精修，再按模式收边：
        image/blur: mediapipe 掩码斜率放大压窄 ~30px 糊边；RVM 原生 α 过渡带仅 2~4px
        且发丝半透明是其价值所在——不做斜率放大(压了会秃边)；
        green: 二值化+收边+3×3 羽化——硬边才经得起下游色键,顺带把运动拖尾切干净。"""
        pair = self._key
        if pair is None:
            m = self._mask
            if _EDGE_TIGHT:
                if _CHROMA_REFINE:
                    m = self._chroma_refine(frame, m)
                if self.mode == "green":
                    m = (m >= _GREEN_THRESH).astype(np.float32)
                    if _GREEN_ERODE > 0:
                        m = cv2.erode(m, np.ones((3, 3), np.uint8), iterations=_GREEN_ERODE)
                    m = cv2.GaussianBlur(m, (3, 3), 0)
                elif self._engine_live != "rvm":
                    m = np.clip((m - 0.5) * _EDGE_SLOPE + 0.5, 0.0, 1.0)
                    m = cv2.GaussianBlur(m, (3, 3), 0)
            pair = (m, 1.0 - m)
            self._key = pair
        return pair

    # ── RVM 神经网络引擎（ADR-12-02）──────────────────────────
    def _rvm_warmup_async(self):
        """后台线程加载 RVM TorchScript 并预热。首帧含 CUDA 上下文/cuDNN 引擎选择(秒级)，
        绝不能在 vcam 热路径里做；预热完成前 process() 一直用 mediapipe 顶着。"""
        if self._rvm_warming or self._rvm is not None or self._rvm_failed:
            return
        if time.monotonic() < self._rvm_next_try:      # 门禁冷却中：mediapipe 顶着,到点再查
            return
        self._rvm_warming = True

        def _warm():
            try:
                # 显存门禁：余量不足就不碰 CUDA(近满显存上建上下文/benchmark 会原生崩进程)。
                # 非永久失败——只暂缓本轮,留 _rvm_next_try 周期复查,显存宽裕后自动升级 RVM。
                if _RVM_MIN_FREE_MB > 0:
                    free = _gpu_free_mb()
                    if 0 <= free < _RVM_MIN_FREE_MB:
                        self._rvm_hold = (f"显存不足(空闲{free}MB<需{_RVM_MIN_FREE_MB}MB)，"
                                          f"已用CPU抠像顶替，显存宽裕后自动启用RVM")
                        self._rvm_next_try = time.monotonic() + _RVM_VRAM_RETRY_S
                        _log(f"[BG] RVM 预热暂缓: {self._rvm_hold}"
                             f"（{int(_RVM_VRAM_RETRY_S)}s 后复查；BG_RVM_MIN_FREE_MB=0 可关门禁）")
                        return
                self._rvm_hold = ""
                model = _ensure_rvm_model()
                if not model:
                    raise RuntimeError("RVM 模型缺失(下载失败)")
                import io
                import torch
                if not torch.cuda.is_available():
                    raise RuntimeError("torch CUDA 不可用")
                # torch.jit.load 走 C 层 fopen,中文路径在 Windows 上直接 errno 2 → 经内存加载
                with open(model, "rb") as f:
                    net = torch.jit.load(io.BytesIO(f.read()), map_location="cuda").eval()
                torch.backends.cudnn.benchmark = True      # 固定 720p 输入,选最快卷积算法
                dummy = torch.zeros(1, 3, 720, 1280, dtype=torch.float16, device="cuda")
                ds = torch.tensor([0.375], device="cuda")
                with torch.inference_mode():
                    net(dummy, None, None, None, None, ds)
                torch.cuda.synchronize()
                self._rvm = {"net": net, "torch": torch, "rec": [None] * 4,
                             "wh": (0, 0), "last_t": 0.0}
                _log("[BG] RVM 神经网络抠像就绪(TorchScript CUDA fp16, 720p全分辨率α)")
            except Exception as e:
                self._rvm_failed = f"RVM 不可用: {str(e)[:100]}"
                _log(f"[BG] {self._rvm_failed}；继续用 mediapipe")
            finally:
                self._rvm_warming = False

        threading.Thread(target=_warm, daemon=True, name="bg-rvm-warm").start()

    def _rvm_mask(self, frame):
        """RVM 前向：输出与帧同分辨率的 float32 α(0..1)。
        预处理全在 GPU：uint8 帧直接上传(~2.7MB/帧),BGR→RGB(flip)+fp16 归一化在卡上做,
        CPU 只负担一次 α 回传(720p fp32 ~3.7MB)。时域记忆内建——rec 跨帧传递即防闪烁,
        无需 EMA；断流>1s 或分辨率变化时清记忆防陈旧鬼影。
        任何异常 → 标记失败回退 mediapipe(本进程内不再尝试)，绝不让 vcam 断流。"""
        v = self._rvm
        torch = v["torch"]
        try:
            h, w = frame.shape[:2]
            now = time.time()
            if v["wh"] != (w, h) or now - v["last_t"] > 1.0:
                v["rec"] = [None] * 4
                v["wh"] = (w, h)
                v["ds"] = torch.tensor(
                    [_RVM_DS if _RVM_DS > 0 else (0.375 if h <= 720 else 0.25)],
                    device="cuda")
            v["last_t"] = now
            with torch.inference_mode():
                t = torch.from_numpy(np.ascontiguousarray(frame)).cuda()
                src = t.permute(2, 0, 1)[None].flip(1).half().div_(255.0)
                fgr, pha, *rec = v["net"](src, *v["rec"], v["ds"])
                v["rec"] = rec
                return pha[0, 0].float().cpu().numpy()
        except Exception as e:
            self._rvm_failed = f"RVM 运行错误: {str(e)[:80]}"
            self._rvm = None
            _log(f"[BG] {self._rvm_failed}；已自动回退 mediapipe")
            return None

    def _ensure_seg(self):
        """惰性建 MediaPipe Tasks ImageSegmenter（IMAGE 模式,无状态,单线程消费）。"""
        if self._seg is not None or self._seg_failed:
            return self._seg
        try:
            model = _ensure_seg_model()
            if not model:
                raise RuntimeError("人像分割模型缺失(下载失败)")
            import mediapipe as mp
            from mediapipe.tasks.python import vision
            from mediapipe.tasks.python.core.base_options import BaseOptions
            self._mp = mp
            # 关键：传 buffer 而非 path——MediaPipe C++ 层在中文路径(GBK/UTF-8 不一致)下
            # 打不开文件("Unable to open file at C:\模仿音色\...")，读进内存绕过文件名编码。
            with open(model, "rb") as f:
                model_buf = f.read()
            opts = vision.ImageSegmenterOptions(
                base_options=BaseOptions(model_asset_buffer=model_buf),
                running_mode=vision.RunningMode.IMAGE,
                output_confidence_masks=True, output_category_mask=False)
            self._seg = vision.ImageSegmenter.create_from_options(opts)
        except Exception as e:
            self._seg_failed = f"分割引擎不可用: {str(e)[:100]}"
            _log(f"[BG] 虚拟背景引擎加载失败,已降级为不替换: {self._seg_failed}")
        return self._seg

    # ── 主处理（vcam 线程逐帧调用）────────────────────────────
    def process(self, frame):
        if self.mode == "none" or frame is None:
            return frame
        t0 = time.time()
        h, w = frame.shape[:2]
        self._frame_i += 1

        # 引擎选择：RVM 可用即用（auto/rvm）；预热中/失败 → mediapipe 顶着,永不断流
        m_rvm = None
        if _ENGINE in ("auto", "rvm") and not self._rvm_failed:
            if self._rvm is None:
                self._rvm_warmup_async()
            else:
                m_rvm = self._rvm_mask(frame)
        if m_rvm is not None:
            self._engine_live = "rvm"
            self._mask = m_rvm            # RVM 自带时域记忆：跨帧 rec 已防闪烁,免 EMA 免预羽化
            self._key = None
        else:
            seg = self._ensure_seg()
            if seg is None:
                return frame
            self._engine_live = "mediapipe"
            need_seg = (self._mask is None or self._mask.shape[:2] != (h, w)
                        or self._frame_i % self.seg_every == 0)
            if need_seg:
                sw = _SEG_W
                sh = max(16, int(h * sw / max(1, w)))
                small = cv2.resize(frame, (sw, sh), interpolation=cv2.INTER_AREA)
                rgb = np.ascontiguousarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
                mp_img = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
                res = seg.segment(mp_img)
                # selfie_segmenter: confidence_masks 末位=人像置信度([0,1],人=1)；
                # 单类别模型仅 1 张(即人像)，双类别(bg,person)取最后一张,两种发行版都对。
                m = res.confidence_masks[-1].numpy_view().astype(np.float32)
                if self.edge_blur > 0:
                    k = self.edge_blur | 1
                    m = cv2.GaussianBlur(m, (k, k), 0)
                m = cv2.resize(m, (w, h), interpolation=cv2.INTER_LINEAR)
                if self._mask is not None and self._mask.shape[:2] == (h, w):
                    # 时域 EMA 防闪烁（cv2.addWeighted 为 SIMD 路径,比 numpy 逐元素快 ~4×）
                    m = cv2.addWeighted(m, self.mask_ema, self._mask, 1.0 - self.mask_ema, 0)
                self._mask = m
                self._key = None                            # 掩码变了→键控缓存失效

        bg = self._bg_for(frame)
        km, km_inv = self._key_masks(frame)
        if _EDGE_TIGHT and _CHROMA_REFINE:
            frame = self._despill_edges(frame, km)   # 边缘去绿溢色(仅 0<α<1 像素)
        # blendLinear: C++/SIMD 逐像素线性混合,替代 numpy float 大数组三连(实测 720p 省 ~8ms/帧)
        out = cv2.blendLinear(frame, bg, km, km_inv)

        dt = (time.time() - t0) * 1000.0
        self._ms_ewma = dt if self._ms_ewma == 0 else (0.9 * self._ms_ewma + 0.1 * dt)
        return out

    def close(self):
        try:
            if self._seg is not None:
                self._seg.close()
        except Exception:
            pass
        self._seg = None
        self._rvm = None      # ORT 会话随 GC 释放显存
        self._close_video()


def _imread_any(path):
    """非 ASCII 路径安全读图：cv2.imread 在 Windows 中文路径下静默失败，走 bytes 解码。"""
    try:
        return cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_COLOR)
    except Exception:
        return None


def _cover_resize(img, w, h):
    """背景图按 cover 方式铺满输出（等比放大+中心裁剪，不变形）。"""
    ih, iw = img.shape[:2]
    scale = max(w / iw, h / ih)
    nw, nh = int(iw * scale + 0.5), int(ih * scale + 0.5)
    img = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    x = (nw - w) // 2
    y = (nh - h) // 2
    return img[y:y + h, x:x + w]
