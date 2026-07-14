"""
FaceSwap REST API v2 - 模型常驻内存版
- 启动时加载一次 insightface 模型
- 每次请求直接在内存处理，无子进程开销
- 使用 DirectML GPU 加速（无需 CUDA Toolkit）
GET  /healthz
  Lightweight liveness probe (does not touch model/GPU state)
POST /faceswap
  Body: { "source_image": "<base64>", "target_image": "<base64>" }
  Return: { "result_image": "<base64>", "elapsed_ms": <int> }
"""
import sys, io
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import base64
import time
import os
import threading
import cv2
import numpy as np
import torch

# ── onnxruntime CUDA EP 依赖 cublasLt64_12.dll 等 CUDA12 运行库；torch(cu128) 已自带于
#    torch/lib，但默认不在 DLL 搜索路径 → CUDA EP 静默回退 CPU(4.4fps)。此处显式登记，
#    让 onnxruntime 走 CUDAExecutionProvider（实测换脸 480p ~43fps）。
try:
    _torch_lib = os.path.join(os.path.dirname(torch.__file__), "lib")
    if os.path.isdir(_torch_lib):
        os.add_dll_directory(_torch_lib)
        os.environ["PATH"] = _torch_lib + os.pathsep + os.environ.get("PATH", "")
except Exception as _e:
    print(f"[FaceSwap API] 登记 torch CUDA DLL 路径失败（将可能回退 CPU/DML）: {_e}")

# 2026-07-10 TensorRT 就绪：pip 的 tensorrt_cu12_libs 把 nvinfer_10.dll 放在 site-packages/
# tensorrt_libs，默认不在 DLL 搜索路径 → onnxruntime 的 TensorrtExecutionProvider 加载 nvinfer
# 失败(Error 126)静默回退 CUDA(这正是历史 trt_available=False 的根因)。此处显式登记该目录，
# 让 TRT EP 真正可建会话。缺包(未装 TRT)时静默跳过——不影响 CUDA 主路。
try:
    import importlib.util as _ilu
    _trt_spec = _ilu.find_spec("tensorrt_libs")
    if _trt_spec is not None and _trt_spec.submodule_search_locations:
        _trt_lib = _trt_spec.submodule_search_locations[0]
        if os.path.isdir(_trt_lib):
            os.add_dll_directory(_trt_lib)
            os.environ["PATH"] = _trt_lib + os.pathsep + os.environ.get("PATH", "")
except Exception:
    pass

import sys
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import uvicorn
import onnxruntime
import insightface
from insightface.app import FaceAnalysis
from gfpgan import GFPGANer

# CodeFormer 路径加入 sys.path
import app_config
_BASE = str(app_config.BASE)
CODEFORMER_DIR = rf"{_BASE}\CodeFormer"
if CODEFORMER_DIR not in sys.path:
    sys.path.insert(0, CODEFORMER_DIR)
# facelib 是 CodeFormer 目录的内置包——.104 等未随包部署 CodeFormer 的机器上不存在，
# 无条件导入=服务起不来(2026-07-05 .104 重启即踩雷)。改为可缺失降级：缺则 codeformer
# 路径自动禁用(原本就有 codeformer_net=None 的优雅降级)，gfpgan 主路/快路不受影响
# (只依赖 pip 的 basicsr/torchvision)。
try:
    from facelib.utils.face_restoration_helper import FaceRestoreHelper
    from basicsr.utils.registry import ARCH_REGISTRY
    # [2026-07-06 根因修复] 上面 gfpgan 导入已把 **pip 版 basicsr** 载入 sys.modules，
    # 这里的 ARCH_REGISTRY 因而永远是 pip 注册表——里面没有 CodeFormer 架构(它只在
    # CodeFormer 仓库自带的 basicsr fork 里)。历史上"能用"的机器是 site-packages 直接装了
    # fork；标准 pip 环境(本机/.104)一直静默 KeyError→codeformer:false。
    # 修复=把 fork 的 vqgan_arch/codeformer_arch 两个模块按原包名嫁接进已加载的 pip basicsr
    # 命名空间(@ARCH_REGISTRY.register() 装饰器随 exec_module 把架构注册进 pip 注册表)，
    # gfpgan 所依赖的 pip basicsr 其余部分零改动。
    try:
        ARCH_REGISTRY.get('CodeFormer')
    except Exception:
        import importlib.util as _ilu
        for _an in ("vqgan_arch", "codeformer_arch"):     # vqgan 在前：codeformer_arch import 它
            _ap = os.path.join(CODEFORMER_DIR, "basicsr", "archs", f"{_an}.py")
            _spec = _ilu.spec_from_file_location(f"basicsr.archs.{_an}", _ap)
            _mod = _ilu.module_from_spec(_spec)
            sys.modules[f"basicsr.archs.{_an}"] = _mod
            _spec.loader.exec_module(_mod)
        ARCH_REGISTRY.get('CodeFormer')                   # 嫁接后自检：仍取不到就走 except 降级
        print("[FaceSwap API] CodeFormer 架构已嫁接进 pip basicsr 注册表(fork archs)")
except Exception as _cf_e:
    print(f"[FaceSwap API] CodeFormer 依赖缺失(仅禁用 codeformer 增强,gfpgan 不受影响): {_cf_e}")
    FaceRestoreHelper = None
    ARCH_REGISTRY = None
try:
    from basicsr.utils import img2tensor, tensor2img     # pip basicsr(gfpgan 依赖)即可满足
except Exception as _bs_e:
    print(f"[FaceSwap API] basicsr 缺失(gfpgan 快路将回退旧路): {_bs_e}")
    img2tensor = tensor2img = None
try:
    from torchvision.transforms.functional import normalize as _tv_normalize
except Exception:
    _tv_normalize = None

# ── 模型路径 ────────────────────────────────────────────────────
INSWAPPER_MODEL  = rf"{_BASE}\Deep-Live-Cam\models\inswapper_128.onnx"
GFPGAN_MODEL     = rf"{_BASE}\GFPGANv1.4.pth"
CODEFORMER_MODEL = rf"{_BASE}\CodeFormer\weights\CodeFormer\codeformer.pth"
FACES_DIR        = Path(rf"{_BASE}\faces")

# ── 可插拔换脸模型（2026 升级 · Phase 8 视觉升级）────────────────────
#   inswapper_128 内部仅 128×128，是 2023 老基线。2026 SOTA 同格式 ONNX（hyperswap_256 /
#   ghost_256 / simswap_512 等，均经 InsightFace model_zoo 同款 INSwapper 加载器加载）画质更高。
#   FACESWAP_MODEL=<某.onnx 绝对路径> 即切换；缺省或文件不存在/加载失败 → 自动回退 inswapper_128。
#   多副本/双引擎：另起一实例 set FACESWAP_MODEL=...&FACESWAP_PORT=8003，Hub 经 SVC_FACESWAP2 路由。
FACESWAP_MODEL_PATH = os.environ.get("FACESWAP_MODEL", "").strip()
# 预设档(与 realtime_stream 的 SWAP_PRESET 同名共享)：hd=高清档 —— 未显式指定 FACESWAP_MODEL 时
# 自动探测并优先 HyperSwap-256(存在才用；缺失则静默保持 inswapper_128 实时基线)，且默认开 TensorRT。
FACESWAP_PRESET = os.environ.get("FACESWAP_PRESET", os.environ.get("SWAP_PRESET", "")).strip().lower()


def _detect_hyperswap() -> str:
    """在换脸模型目录探测 HyperSwap ONNX(优先带 256 的)；找不到返回空串。"""
    import glob as _g
    dirs = [str(Path(INSWAPPER_MODEL).parent)]
    try:
        dirs.append(str(app_config.BASE / "models"))
    except Exception:
        pass
    cands = []
    for d in dirs:
        cands += _g.glob(os.path.join(d, "*yper*wap*.onnx"))
    cands = [c for c in dict.fromkeys(cands) if Path(c).is_file()]
    if not cands:
        return ""
    cands.sort(key=lambda p: (0 if "256" in Path(p).name else 1, len(p)))
    return cands[0]


def _base_swap_model() -> str:
    """换脸 ONNX 选择：显式 FACESWAP_MODEL(存在)优先；hd 预设或主引擎 HD 核开关探测 HyperSwap-256；
    否则 inswapper_128。
    2026-07-10：主引擎默认用 HyperSwap-256 高清核(256² 生成，脸区清晰度实测 ~2× inswapper_128
    的 128²，身份 0.999/0.74 同/跨脸均不低于 inswapper)。仅主引擎(非 8003 副本)且模型在位且
    FACESWAP_HD_CORE≠0 时启用——副本保持 inswapper(轻量、失联秒接管，不付 HS 的 TRT 构建/显存)。"""
    if FACESWAP_MODEL_PATH and Path(FACESWAP_MODEL_PATH).is_file():
        return FACESWAP_MODEL_PATH
    # 副本(8003)恒 inswapper：失联秒接管、省显存,绝不因继承 env 的 SWAP_PRESET=hd 而误载 HS
    # (那会让副本吃 HS 的显存+CUDA 107ms,违背"轻量备胎"初衷)。仅主引擎用 HD 核。
    if not _IS_REPLICA and (FACESWAP_PRESET == "hd" or os.environ.get("FACESWAP_HD_CORE", "1") == "1"):
        hs = _detect_hyperswap()
        if hs:
            print(f"[FaceSwap API] 主引擎 HD 核 → 启用 HyperSwap: {hs}")
            return hs
    return INSWAPPER_MODEL

_swap_model_name = "inswapper_128"   # 实际加载成功后据 _swap_model_path 校正（/health 展示）

# ── 明星脸管理 ───────────────────────────────────────────────
import glob

def scan_faces():
    """扫描 faces 文件夹，返回 {名字: base64} 字典"""
    result = {}
    for ext in ('*.jpg','*.jpeg','*.png','*.webp'):
        for f in FACES_DIR.glob(ext):
            with open(f,'rb') as fp:
                result[f.stem] = base64.b64encode(fp.read()).decode()
    return result

# 当前激活的源脸
_faces_cache: dict = {}
_active_face_name: str = ""
_active_face_b64: str = ""

# 2026-07-09 直连补件：激活态(脸名/妆容)落盘——引擎被看门狗自愈重启后 /faceswap_raw 直连
# 通道的源脸/妆容不回退到"目录第一张/无妆"（JSON 通道每帧注入不受影响，此为直连专属韧性）。
_ACTIVE_STATE_FILE = Path(rf"{_BASE}\data\faceswap_active.json")
_active_state_lock = threading.Lock()


def _active_state_update(**kw):
    try:
        import json as _json
        with _active_state_lock:
            st = {}
            try:
                st = _json.loads(_ACTIVE_STATE_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
            st.update(kw)
            _ACTIVE_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _ACTIVE_STATE_FILE.write_text(_json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _active_state_load() -> dict:
    try:
        import json as _json
        return _json.loads(_ACTIVE_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def reload_faces():
    global _faces_cache, _active_face_name, _active_face_b64
    _faces_cache = scan_faces()
    if _faces_cache:
        if _active_face_name not in _faces_cache:
            # 冷启/活动脸文件消失：优先恢复上次激活标记(直连通道重启不换错脸)，否则取第一张
            _mk = str(_active_state_load().get("face") or "")
            _active_face_name = _mk if _mk in _faces_cache else list(_faces_cache.keys())[0]
        _active_face_b64 = _faces_cache[_active_face_name]
        print(f"[FaceSwap API] 已加载 {len(_faces_cache)} 张明星脸，当前: {_active_face_name}")
    else:
        print("[FaceSwap API] faces 文件夹为空！")

reload_faces()

# ── 执行后端（Phase E / 8-1：CUDA / TensorRT FP16 加速）────────────────────
#   onnxruntime 的 TensorRT EP 在「首次推理」据 onnx 现场构建 FP16 引擎并缓存到磁盘(秒级复用)，
#   无需外部 trtexec / 预编 engine 文件——故仍喂普通 inswapper onnx。首次启动构建较慢(分钟级)，
#   由 _warmup() 在开机阶段触发；落盘后再启即秒级。实测换脸网 FP16 引擎较 CUDA FP32 显著提速。
# 2026-07-10 TRT 默认策略：主引擎(8000)默认试开 TRT(装了库才真启，_TRT_AVAILABLE 兜底)；
#   容灾副本(8003)默认关——失联回切时若副本首帧现建 TRT 引擎(冷 ~86s)会把接管拖成灾难。
#   FACESWAP_TRT 显式给值(0/1)一律优先。
_IS_REPLICA = os.environ.get("FACESWAP_PORT", "8000").strip() == "8003"
_use_trt_env = os.environ.get("FACESWAP_TRT", "").strip()
USE_TRT  = (_use_trt_env == "1") if _use_trt_env else (not _IS_REPLICA)
TRT_FP16 = os.environ.get("FACESWAP_TRT_FP16", "1") != "0"
TRT_DET  = os.environ.get("FACESWAP_TRT_DET", "0") == "1"   # 是否也对检测/识别(buffalo_l)走 TRT
# TRT 引擎缓存目录必须是纯 ASCII 路径：TRT 缓存写入器不认非 ASCII(项目根 "C:\模仿音色" 含中文
# → 首帧建缓存时 'utf-8 codec' 崩溃、静默回退 CPU，这正是历史 trt 一直用不上的第二层根因)。
# 默认落 C:\packs\trt_cache\faceswap(与本仓 C:\packs 暂存约定一致，Administrator 纯 ASCII)。
_trt_cache_env = os.environ.get("FACESWAP_TRT_CACHE", "").strip()
def _ascii_ok(p): return all(ord(c) < 128 for c in p)
TRT_CACHE_DIR = _trt_cache_env or r"C:\packs\trt_cache\faceswap"
if not _ascii_ok(TRT_CACHE_DIR):
    TRT_CACHE_DIR = r"C:\packs\trt_cache\faceswap"    # 用户给了中文路径 → 强制回 ASCII，防崩
TRT_MODEL_PATH = os.environ.get("FACESWAP_TRT_MODEL", "")   # 向后兼容：指向已嵌 TRT 的 onnx 时作模型路径


def _trt_usable() -> bool:
    """TRT EP 被 onnxruntime「列出」≠「真能用」：常见缺 TensorRT 运行库(nvinfer_*.dll 不在 PATH)时，
    provider 仍在 get_available_providers() 里，但建会话会静默回退——若回退到 CPU，换脸会掉到个位数 fps。
    这里用一个极小模型试建 TRT 会话，只有实际启用了 TensorrtExecutionProvider 才算可用；否则一律 False
    → 让 _execution_backend 直接选 CUDA，规避“列出即用”的假阳性。"""
    if "TensorrtExecutionProvider" not in onnxruntime.get_available_providers():
        return False
    try:
        from onnx import helper as _h, TensorProto as _T
        g = _h.make_graph([_h.make_node("Identity", ["x"], ["y"])], "trtprobe",
                          [_h.make_tensor_value_info("x", _T.FLOAT, [1, 1])],
                          [_h.make_tensor_value_info("y", _T.FLOAT, [1, 1])])
        m = _h.make_model(g, opset_imports=[_h.make_opsetid("", 11)])
        m.ir_version = 10                  # 兼容旧 onnxruntime(部分构建 max IR=11)，避免探测模型自身加载失败造成假阴性
        so = onnxruntime.SessionOptions()
        so.log_severity_level = 4          # 静默探测(EP 注册失败的 C++ 告警仍可能打印，属正常提示)
        sess = onnxruntime.InferenceSession(
            m.SerializeToString(), sess_options=so,
            providers=["TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"])
        return "TensorrtExecutionProvider" in sess.get_providers()
    except Exception:
        return False


_TRT_AVAILABLE = _trt_usable()


def _trt_provider_options() -> dict:
    """TensorRT EP 选项：FP16 + 引擎/计时缓存（首次构建慢，落盘后秒级复用）。"""
    try:
        os.makedirs(TRT_CACHE_DIR, exist_ok=True)
    except Exception:
        pass
    return {
        "trt_fp16_enable": TRT_FP16,
        "trt_engine_cache_enable": True,
        "trt_engine_cache_path": TRT_CACHE_DIR,
        "trt_timing_cache_enable": True,
    }


def _execution_backend():
    """返回换脸网的 (providers, provider_options|None, model_path, backend_label)。"""
    onnx_path = _base_swap_model()       # 自定义高清模型(若配置) > inswapper_128
    if USE_TRT and torch.cuda.is_available() and _TRT_AVAILABLE:
        model = TRT_MODEL_PATH if (TRT_MODEL_PATH and Path(TRT_MODEL_PATH).is_file()) else onnx_path
        return (["TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"],
                [_trt_provider_options(), {}, {}], model, "tensorrt")
    if USE_TRT and not _TRT_AVAILABLE:
        print("[FaceSwap API] 已请求 TRT 但 onnxruntime 无 TensorrtExecutionProvider → 回退 CUDA")
    if torch.cuda.is_available() and os.environ.get("FACESWAP_CUDA", "1") != "0":
        return (["CUDAExecutionProvider", "DmlExecutionProvider", "CPUExecutionProvider"],
                None, onnx_path, "cuda")
    return (["DmlExecutionProvider", "CPUExecutionProvider"], None, onnx_path, "dml")


# ── HyperSwap 适配器（2026-07-07 高清换脸核）────────────────────────────────────
#   FaceFusion 的 hyperswap_1a_256 与 inswapper 的 ONNX 契约**不兼容**，不能用 INSwapper 加载器：
#     · inswapper: 输入 (归一化图, latent=embedding·emap)；模型内含 emap 矩阵(graph.initializer[-1])
#     · hyperswap: 输入 source=[1,512] L2 归一 ArcFace embedding(**无 emap**) + target=[1,3,256,256]；
#                  输出 output=[1,3,256,256] + mask；对齐用 arcface_128 模板；归一 mean/std=0.5(即 [-1,1])。
#   下方前后处理逐行对齐 FaceFusion 官方推理(processors/face_swapper/core.py)，
#   embedding 空间与 insightface buffalo_l 的 w600k_r50 一致(同款识别网)→ 现有 face_analyser 直接可用。
#   .get() 签名与 INSwapper 完全一致 → 换脸主循环零改动，是 face_swapper 的 drop-in 替换。
_ARCFACE_128_TEMPLATE = np.array([
    [0.36167656, 0.40387734], [0.63696719, 0.40235469], [0.50019687, 0.56044219],
    [0.38710391, 0.72160547], [0.61507734, 0.72034453]], dtype=np.float32)


class HyperSwap:
    """FaceFusion HyperSwap-256 换脸网适配器（drop-in 替代 INSwapper.get 接口）。"""
    def __init__(self, model_file, session=None, providers=None, provider_options=None):
        import onnxruntime as _ort
        self.model_file = model_file
        if session is None:
            if provider_options is None:
                session = _ort.InferenceSession(model_file, providers=providers)
            else:
                session = _ort.InferenceSession(model_file, providers=providers,
                                                provider_options=provider_options)
        self.session = session
        self.input_names = [i.name for i in session.get_inputs()]     # ['source','target']
        self.output_names = [o.name for o in session.get_outputs()]   # ['output','mask']
        tgt_shape = next(i.shape for i in session.get_inputs() if i.name == "target")
        self.input_size = (int(tgt_shape[3]), int(tgt_shape[2]))       # (W,H)=(256,256)
        self._mean = 0.5
        self._std = 0.5
        # ── 2026-07-11 灰边根治（A/B 循证：temp\_hs_mask_ab.py）────────────────
        #   ① 模型自带 mask 输出=精确脸形掩码(实测连脸颊发丝都抠掉)，此前被丢弃、用矩形
        #      box mask 羽化贴回——羽化带扫过下巴/发际线外的背景，与原图混合出灰边。
        #      改为 min(box 羽化, 模型脸形掩码) 贴回，混合边界沿真实脸缘走。
        #   ② 贴回前做 LAB 色彩迁移(与 DFM 路径同一 _lab_color_transfer)：换脸输出与目标
        #      肤色/光照的统计差正是羽化带"发灰"的另一半成因。
        #   开关：FACESWAP_HS_MODEL_MASK=0 / FACESWAP_HS_COLORFIX=0 单独禁用；
        #   模型无 mask 输出或新路径异常 → 自动回退旧 box mask 贴回(零回归)。
        self.has_mask_out = len(self.output_names) > 1
        self.use_model_mask = self.has_mask_out and os.environ.get("FACESWAP_HS_MODEL_MASK", "1") == "1"
        self.colorfix = os.environ.get("FACESWAP_HS_COLORFIX", "1") == "1"
        print(f"[FaceSwap API] HyperSwap 适配器就绪: {Path(model_file).name} "
              f"input_size={self.input_size} inputs={self.input_names} "
              f"model_mask={self.use_model_mask} colorfix={self.colorfix}")

    @staticmethod
    def _stretch_mask(model_mask):
        """模型 mask 后处理：σ5 blur → clip(0.5,1) 拉伸(FaceFusion occlusion mask 同款口径)。"""
        mm = model_mask.clip(0, 1).astype(np.float32)
        return (cv2.GaussianBlur(mm, (0, 0), 5).clip(0.5, 1) - 0.5) * 2

    def get(self, img, target_face, source_face, paste_back=True, return_mask=False):
        S = self.input_size[0]
        # 1) 目标脸按 arcface_128 模板 + 5 点 landmark 仿射对齐到 256（对齐 FaceFusion warp）
        tpl = _ARCFACE_128_TEMPLATE * S
        M = cv2.estimateAffinePartial2D(np.asarray(target_face.kps, dtype=np.float32), tpl,
                                        method=cv2.RANSAC, ransacReprojThreshold=100)[0]
        aimg = cv2.warpAffine(img, M, (S, S), borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_AREA)
        # 2) target 预处理：BGR→RGB、/255、(x-0.5)/0.5 → [-1,1]、CHW、batch
        blob = ((aimg[:, :, ::-1].astype(np.float32) / 255.0 - self._mean) / self._std)
        blob = np.expand_dims(blob.transpose(2, 0, 1), axis=0).astype(np.float32)
        # 3) source：L2 归一 ArcFace embedding（无 emap 投影）→ [1,512]
        latent = np.asarray(source_face.normed_embedding, dtype=np.float32).reshape(1, -1)
        # 4) 前向：output + (可用时)模型自带脸形 mask（2026-07-11 灰边根治，见 __init__ 注释）
        feed = {self.input_names[0]: latent, self.input_names[1]: blob}
        if self.use_model_mask:
            preds = self.session.run(None, feed)
            pred, model_mask = preds[0][0], preds[1][0]
            model_mask = model_mask[0] if model_mask.ndim == 3 else model_mask   # [1,S,S]→[S,S]
        else:
            pred = self.session.run([self.output_names[0]], feed)[0][0]
            model_mask = None
        # 5) output 反归一：CHW→HWC、*0.5+0.5、clip、RGB→BGR、×255
        out = pred.transpose(1, 2, 0)
        out = (out * self._std + self._mean).clip(0, 1)
        bgr_fake = (out[:, :, ::-1] * 255).astype(np.uint8)
        if not paste_back:
            if return_mask:
                # 自定义贴回(feather/遮挡)路径也吃模型脸形掩码+校色：返回三元组
                mm = None
                if model_mask is not None:
                    try:
                        mm = self._stretch_mask(model_mask)
                        if self.colorfix:
                            bgr_fake = _lab_color_transfer(bgr_fake, aimg, mm)
                    except Exception as e:
                        print(f"[FaceSwap API] HyperSwap 掩码/校色预处理失败(退纯羽化): {e}")
                        mm = None
                return bgr_fake, M, mm
            return bgr_fake, M
        if model_mask is not None:
            try:
                return self._paste_model_mask(img, bgr_fake, aimg, M, model_mask)
            except Exception as e:
                print(f"[FaceSwap API] HyperSwap 模型掩码贴回失败(本脸回退 box mask): {e}")
        # 旧贴回（沿用 INSwapper 的羽化 box mask + 差分 mask 口径，保持既有贴缝观感/下游平滑一致）
        target_img = img
        fake_diff = np.abs(bgr_fake.astype(np.float32) - aimg.astype(np.float32)).mean(axis=2)
        fake_diff[:2, :] = 0; fake_diff[-2:, :] = 0; fake_diff[:, :2] = 0; fake_diff[:, -2:] = 0
        IM = cv2.invertAffineTransform(M)
        img_white = np.full((aimg.shape[0], aimg.shape[1]), 255, dtype=np.float32)
        bgr_fake = cv2.warpAffine(bgr_fake, IM, (target_img.shape[1], target_img.shape[0]), borderValue=0.0)
        img_white = cv2.warpAffine(img_white, IM, (target_img.shape[1], target_img.shape[0]), borderValue=0.0)
        fake_diff = cv2.warpAffine(fake_diff, IM, (target_img.shape[1], target_img.shape[0]), borderValue=0.0)
        img_white[img_white > 20] = 255
        fthresh = 10
        fake_diff[fake_diff < fthresh] = 0
        fake_diff[fake_diff >= fthresh] = 255
        img_mask = img_white
        mask_h_inds, mask_w_inds = np.where(img_mask == 255)
        if len(mask_h_inds) == 0:
            return target_img
        mask_h = np.max(mask_h_inds) - np.min(mask_h_inds)
        mask_w = np.max(mask_w_inds) - np.min(mask_w_inds)
        mask_size = int(np.sqrt(mask_h * mask_w))
        k = max(mask_size // 10, 10)
        img_mask = cv2.erode(img_mask, np.ones((k, k), np.uint8), iterations=1)
        k = max(mask_size // 20, 5)
        blur_size = tuple(2 * i + 1 for i in (k, k))
        img_mask = cv2.GaussianBlur(img_mask, blur_size, 0) / 255
        img_mask = np.reshape(img_mask, [img_mask.shape[0], img_mask.shape[1], 1])
        fake_merged = img_mask * bgr_fake + (1 - img_mask) * target_img.astype(np.float32)
        return fake_merged.astype(np.uint8)

    def _paste_model_mask(self, img, bgr_fake, aimg, M, model_mask):
        """模型脸形掩码贴回：min(box 羽化, 脸形掩码) 定混合边界 + LAB 校色消灰晕。
        A/B 实测(temp\\_hs_mask_ab.py)：下巴/鬓角灰边消失、背景/头发零侵入；校色后
        羽化带肤色连续。掩码后处理口径与 FaceFusion occlusion/region mask 相同
        (σ5 blur → clip(0.5,1) 拉伸)，box mask 口径与 _box_feather_mask(blur 0.3) 相同。"""
        S = bgr_fake.shape[0]
        mask = np.minimum(_box_feather_mask((S, S)), self._stretch_mask(model_mask))
        if self.colorfix:
            bgr_fake = _lab_color_transfer(bgr_fake, aimg, mask)
        return _paste_crop_back(img.copy(), bgr_fake, mask, M)


# ── DFM（DeepFaceLive 每角色专属模型）适配器 ────────────────────────────────
#   与 inswapper/hyperswap 的根本不同：DFM 是「per-identity」模型——模型本身即某个人，
#   吃任意脸、吐该名人脸（含脸型/骨相/皮肤纹理，非仅内脸五官）→ 辨识度天花板。
#   ONNX 契约(iperov/DeepFaceLive)：in_face:0 = NHWC BGR [0,1] SxS(224/320…)，可选 morph_value:0；
#   输出 out_face_mask / out_celeb_face / out_celeb_face_mask（均 NHWC [0,1]）。
#   对齐：DeepFaceLab WHOLE_FACE（68 landmark 子集 umeyama→模板，padding0.40 + 额头 7%）。
#   贴回前做 LAB 色彩迁移（把名人肤色/光照统计对齐到目标脸），消除跨库色偏。
_DFL_L2D_NEW = np.array([
    [0.000213256,0.106454],[0.0752622,0.038915],[0.18113,0.0187482],[0.29077,0.0344891],[0.393397,0.0773906],
    [0.586856,0.0773906],[0.689483,0.0344891],[0.799124,0.0187482],[0.904991,0.038915],[0.98004,0.106454],
    [0.490127,0.203352],[0.490127,0.307009],[0.490127,0.409805],[0.490127,0.515625],
    [0.36688,0.587326],[0.426036,0.609345],[0.490127,0.628106],[0.554217,0.609345],[0.613373,0.587326],
    [0.121737,0.216423],[0.187122,0.178758],[0.265825,0.179852],[0.334606,0.231733],[0.260918,0.245099],[0.182743,0.244077],
    [0.645647,0.231733],[0.714428,0.179852],[0.793132,0.178758],[0.858516,0.216423],[0.79751,0.244077],[0.719335,0.245099],
    [0.254149,0.780233],[0.726104,0.780233]], dtype=np.float32)


def _umeyama(src, dst, estimate_scale=True):
    num, dim = src.shape
    sm, dm = src.mean(0), dst.mean(0)
    sd, dd = src - sm, dst - dm
    A = dd.T @ sd / num
    d = np.ones((dim,))
    if np.linalg.det(A) < 0:
        d[dim - 1] = -1
    T = np.eye(dim + 1)
    U, S, V = np.linalg.svd(A)
    r = np.linalg.matrix_rank(A)
    if r == dim - 1:
        if np.linalg.det(U) * np.linalg.det(V) > 0:
            T[:dim, :dim] = U @ V
        else:
            s = d[dim - 1]; d[dim - 1] = -1; T[:dim, :dim] = U @ np.diag(d) @ V; d[dim - 1] = s
    else:
        T[:dim, :dim] = U @ np.diag(d) @ V
    scale = 1.0 / sd.var(0).sum() * (S @ d) if estimate_scale else 1.0
    T[:dim, dim] = dm - scale * (T[:dim, :dim] @ sm)
    T[:dim, :dim] *= scale
    return T


def _dfl_whole_face_mat(lm68, S, pad=0.40, fore=0.07):
    """DeepFaceLab WHOLE_FACE 对齐矩阵：68 点子集 umeyama→模板，再按 padding+额头偏移取方框。"""
    sub = np.concatenate([lm68[17:49], lm68[54:55]]).astype(np.float32)
    mat = _umeyama(sub, _DFL_L2D_NEW, True)[0:2].astype(np.float32)
    def _t(pts):
        p = np.expand_dims(np.asarray(pts, dtype=np.float32), 1)
        return np.squeeze(cv2.transform(p, cv2.invertAffineTransform(mat), p.shape))
    g = _t([(0,0),(1,0),(1,1),(0,1),(0.5,0.5)]); gc = g[4].astype(np.float32)
    tb = (g[2]-g[0]).astype(np.float32); tb /= (np.linalg.norm(tb)+1e-8)
    bt = (g[1]-g[3]).astype(np.float32); bt /= (np.linalg.norm(bt)+1e-8)
    mod = np.linalg.norm(g[0]-g[2]) * (pad*np.sqrt(2.0)+0.5)
    vec = (g[0]-g[3]).astype(np.float32); vl = np.linalg.norm(vec)+1e-8; vec /= vl; gc = gc + vec*vl*fore
    l_t = np.array([gc-tb*mod, gc+bt*mod, gc+tb*mod], dtype=np.float32)
    return cv2.getAffineTransform(l_t, np.float32([(0,0),(S,0),(S,S)]))


def _lab_color_transfer(src, ref, mask):
    """把 src(名人输出) 的 LAB 颜色统计迁到 ref(对齐目标脸)，仅在 mask>0.5 内估计→消色偏。"""
    m = mask > 0.5
    if m.sum() < 50:
        return src
    s = cv2.cvtColor(src, cv2.COLOR_BGR2LAB).astype(np.float32)
    r = cv2.cvtColor(ref, cv2.COLOR_BGR2LAB).astype(np.float32)
    out = s.copy()
    for c in range(3):
        ss = s[:, :, c][m].std() + 1e-6; sm = s[:, :, c][m].mean()
        rs = r[:, :, c][m].std() + 1e-6; rm = r[:, :, c][m].mean()
        out[:, :, c] = (s[:, :, c] - sm) / ss * rs + rm
    return cv2.cvtColor(np.clip(out, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)


class DFMSwap:
    """DeepFaceLive .dfm 每角色模型适配器（drop-in 替代 INSwapper.get；source_face 被忽略——
    模型自身即身份）。目标脸需 landmark_3d_68（buffalo_l 提供）。"""
    def __init__(self, model_file, session=None, providers=None, provider_options=None):
        import onnxruntime as _ort
        self.model_file = model_file
        if session is None:
            if provider_options is None:
                session = _ort.InferenceSession(model_file, providers=providers)
            else:
                session = _ort.InferenceSession(model_file, providers=providers, provider_options=provider_options)
        self.session = session
        ins = session.get_inputs()
        self.in_name = next((i.name for i in ins if "in_face" in i.name), ins[0].name)
        self.morph_name = next((i.name for i in ins if "morph" in i.name), None)
        self.morph = self.morph_name is not None         # 形态可调模型带 morph_value:0
        _shape = ins[0].shape
        self.H, self.W = int(_shape[1]), int(_shape[2])
        self.input_size = (self.W, self.H)
        self.onames = [o.name for o in session.get_outputs()]
        # 社区 DFM 输出张量名并不统一（out_celeb_face:0 / celeb_face / 无 :0 后缀等）——按语义解析，
        # 避免硬编码名字在异名模型上 KeyError（实测部分 384/352 模型即因此 500）。
        self.o_celeb = next((n for n in self.onames if "celeb_face" in n and "mask" not in n),
                            self.onames[0])
        self.o_celeb_mask = next((n for n in self.onames if "celeb" in n and "mask" in n), None)
        self.o_face_mask = next((n for n in self.onames if "face_mask" in n and "celeb" not in n), None)
        # 实测的 GPU 单帧换脸耗时（ms）：由 /model/reload 或探针填入，供 /model 观测「是否实时可用」。
        self.gpu_swap_ms = None
        try:
            self.on_cpu_only = all("CPU" in p for p in session.get_providers())
        except Exception:
            self.on_cpu_only = False
        print(f"[FaceSwap API] DFM 适配器就绪: {Path(model_file).name} size={self.W}x{self.H} "
              f"morph={self.morph} outs={len(self.onames)}{' [CPU!]' if self.on_cpu_only else ''}")

    def get(self, img, target_face, source_face=None, paste_back=True, mask_padding=None):
        S = self.W
        lm = getattr(target_face, "landmark_3d_68", None)
        if lm is None:
            raise RuntimeError("DFM 需要 landmark_3d_68（检测器未提供）")
        M = _dfl_whole_face_mat(np.asarray(lm[:, :2], dtype=np.float32), S)
        aimg = cv2.warpAffine(img, M, (S, S), flags=cv2.INTER_CUBIC)
        blob = np.expand_dims(aimg.astype(np.float32) / 255.0, 0)     # BGR NHWC [0,1]，无通道交换
        feed = {self.in_name: blob}
        if self.morph:
            feed[self.morph_name] = np.float32([float(PARAMS.get("dfm_morph", 0.75))])
        outs = self.session.run(None, feed)
        omap = {n: o[0] for n, o in zip(self.onames, outs)}
        celeb = (np.clip(omap[self.o_celeb], 0, 1) * 255).astype(np.uint8)
        cm = omap[self.o_celeb_mask] if self.o_celeb_mask else np.ones_like(omap[self.o_celeb][:, :, :1])
        fm = omap[self.o_face_mask] if self.o_face_mask else np.ones_like(cm)
        mask = np.clip(cm * fm, 0, 1)[:, :, 0]
        # 掩码内缩(mask_padding=[上,右,下,左]%)：光头目标 × 有发角色时，模型 celeb/face 掩码常
        # 松到把名人发际/鬓角包进来 → 贴回后头两侧现深色发带。按百分比清零掩码边缘，下方
        # GaussianBlur σ3 顺带把切口羽化，无额外成本。None/全 0 = 零回归。
        if mask_padding:
            _t = int(S * mask_padding[0] / 100.0); _r = int(S * mask_padding[1] / 100.0)
            _b = int(S * mask_padding[2] / 100.0); _l = int(S * mask_padding[3] / 100.0)
            if _t: mask[:_t, :] = 0
            if _b: mask[-_b:, :] = 0
            if _l: mask[:, :_l] = 0
            if _r: mask[:, -_r:] = 0
        if PARAMS.get("dfm_color_match", True):
            celeb = _lab_color_transfer(celeb, aimg, mask)
        if not paste_back:
            return celeb, M
        IM = cv2.invertAffineTransform(M)
        h, w = img.shape[:2]
        back = cv2.warpAffine(celeb, IM, (w, h), flags=cv2.INTER_CUBIC)
        mb = cv2.warpAffine(mask, IM, (w, h), flags=cv2.INTER_CUBIC)
        mb = cv2.GaussianBlur(mb, (0, 0), 3)[..., None]
        return (back.astype(np.float32) * mb + img.astype(np.float32) * (1 - mb)).astype(np.uint8)


def _is_dfm_model(model_path: str) -> bool:
    return Path(model_path).name.lower().endswith(".dfm")


def _is_hyperswap_model(model_path: str) -> bool:
    return "yperswap" in Path(model_path).name.lower()


def _get_swap_model(model_path, prov, popts):
    """加载换脸网。三条路径：
      · 默认 inswapper_128 → model_zoo.get_model（逐字节零回归）。
      · hyperswap_*（FaceFusion 高清核）→ HyperSwap 适配器（契约与 INSwapper 不同：无 emap、
        source=归一 embedding、[-1,1] 归一、arcface_128 对齐）。
      · 其它自定义 swapper onnx（如 reswapper_256，INSwapper 同格式）→ 直构 INSwapper 绕过 model_zoo
        按输出维度误判成 ArcFace 的路由 bug。"""
    _is_default = (os.path.normcase(os.path.abspath(model_path))
                   == os.path.normcase(os.path.abspath(INSWAPPER_MODEL)))
    if _is_default:
        if popts is None:
            return insightface.model_zoo.get_model(model_path, providers=prov)
        return insightface.model_zoo.get_model(model_path, providers=prov, provider_options=popts)
    if _is_dfm_model(model_path):
        return DFMSwap(model_file=model_path, providers=prov, provider_options=popts)
    if _is_hyperswap_model(model_path):
        return HyperSwap(model_file=model_path, providers=prov, provider_options=popts)
    from insightface.model_zoo.inswapper import INSwapper
    import onnxruntime as _ort
    if popts is None:
        _sess = _ort.InferenceSession(model_path, providers=prov)
    else:
        _sess = _ort.InferenceSession(model_path, providers=prov, provider_options=popts)
    m = INSwapper(model_file=model_path, session=_sess)
    print(f"[FaceSwap API] 自定义换脸模型直构 INSwapper: {Path(model_path).name} input_size={m.input_size}")
    return m


providers, _swap_popts, _swap_model_path, _backend_label = _execution_backend()

# 检测/识别(buffalo_l 含 5 子模型)后端：TRT 模式下默认走 CUDA，避免一次性构建多个引擎拖慢首启；
#   换脸网(inswapper)才是逐帧瓶颈，优先 TRT。FACESWAP_TRT_DET=1 时检测也走 TRT。
if _backend_label == "tensorrt" and not TRT_DET:
    _det_providers, _det_popts = ["CUDAExecutionProvider", "CPUExecutionProvider"], None
else:
    _det_providers, _det_popts = providers, _swap_popts

print(f"[FaceSwap API] 执行后端: {_backend_label} providers={providers[:2]}"
      + (f" FP16={TRT_FP16} cache={TRT_CACHE_DIR}" if _backend_label == "tensorrt" else ""))


def _insightface_root_kw() -> dict:
    """buffalo_l 检测/识别模型的查找根（2026-07-13 140 装机复盘）：
    insightface 默认在 ~/.insightface 找，缺失时联网从 GitHub 下载——客户机（尤其国内网络）
    首启大概率下不动 → 检测器直接加载失败。改为优先随包/随部署的本地目录，全部缺席才回落
    默认（保留自动下载兜底）。root 语义：insightface 在 <root>/models/buffalo_l 下找模型。
      1) BASE\\models\\buffalo_l      —— 分发包 swapcore 的落位（root=BASE）
      2) BASE\\_home\\.insightface    —— 集群机（.104 等 USERPROFILE=_home 启动）的既有落位
    """
    if (Path(_BASE) / "models" / "buffalo_l" / "det_10g.onnx").is_file():
        return {"root": str(_BASE)}
    if (Path(_BASE) / "_home" / ".insightface" / "models" / "buffalo_l" / "det_10g.onnx").is_file():
        return {"root": str(Path(_BASE) / "_home" / ".insightface")}
    return {}


_IF_ROOT_KW = _insightface_root_kw()
if _IF_ROOT_KW:
    print(f"[FaceSwap API] buffalo_l 使用本地模型根: {_IF_ROOT_KW['root']}")
print("[FaceSwap API] 正在加载人脸检测模型...")
try:
    if _det_popts is None:
        face_analyser = FaceAnalysis(name='buffalo_l', providers=_det_providers, **_IF_ROOT_KW)
    else:
        face_analyser = FaceAnalysis(name='buffalo_l', providers=_det_providers,
                                     provider_options=_det_popts, **_IF_ROOT_KW)
    face_analyser.prepare(ctx_id=0, det_size=(640, 640))
    print("[FaceSwap API] 人脸检测模型加载完成")
except Exception as e:
    print(f"[FaceSwap API] 人脸检测加载失败: {e}")
    face_analyser = None

# ── 目标脸专用分析器（2026-07-10 检测提速，两轮循证）─────────────────────────
#   目标脸只读 bbox/kps(换脸)、landmark_2d_106(妆容)、landmark_3d_68(仅 DFM)——从不读
#   normed_embedding/sex/age(全代码 grep 确认)。共享分析器每帧白跑 recognition(ArcFace
#   r50 112²)+genderage。剥掉 → 44ms 档降到 ~12ms(det+2d106+3d68 @512)。
#   二轮(.104 实测): 再砍 landmark_3d_68 又省 ~4ms(12→8ms)——它只喂 DFMSwap 的整脸对齐。
#   故默认目标分析器 = det+2d106(非 DFM 直播的快路)；激活 DFM 角色时 faceswap() 自动回退
#   全模块 face_analyser(它含 3d68，正确性不减)。det_size 保持 512：实测 320 会漏检全帧
#   里占比小的脸(裁剪回退/发现路径致命)，省的 2ms 不值这风险。
#   构建失败/被显式关 → 回退共享分析器，零风险。
_DET_SIZE = int(os.environ.get("FACESWAP_DET_SIZE", "512"))
_tgt_analyser = face_analyser
if face_analyser is not None and os.environ.get("FACESWAP_TGT_ANALYSER", "1") == "1":
    try:
        _mods = ['detection', 'landmark_2d_106']   # 无 3d68(DFM 才需,见下方回退)/无 recognition/genderage
        if _det_popts is None:
            _ta = FaceAnalysis(name='buffalo_l', providers=_det_providers, allowed_modules=_mods,
                               **_IF_ROOT_KW)
        else:
            _ta = FaceAnalysis(name='buffalo_l', providers=_det_providers,
                               provider_options=_det_popts, allowed_modules=_mods, **_IF_ROOT_KW)
        _ta.prepare(ctx_id=0, det_size=(_DET_SIZE, _DET_SIZE))
        _tgt_analyser = _ta
        print(f"[FaceSwap API] 目标脸专用分析器就绪 det_size={_DET_SIZE} mods={_mods}"
              f"(省 recognition+genderage+3d68；DFM 帧自动回退全模块)")
    except Exception as e:
        print(f"[FaceSwap API] 目标脸专用分析器构建失败(回退共享): {e}")
        _tgt_analyser = face_analyser

print(f"[FaceSwap API] 正在加载换脸模型: {Path(_swap_model_path).name} ...")
try:
    face_swapper = _get_swap_model(_swap_model_path, providers, _swap_popts)
    _swap_model_name = Path(_swap_model_path).stem
    print(f"[FaceSwap API] 换脸模型加载完成 ({_backend_label}, {_swap_model_name})")
except Exception as e:
    print(f"[FaceSwap API] 换脸模型加载失败 ({_backend_label}, {Path(_swap_model_path).name}): {e}")
    face_swapper = None
    # ① 自定义高清模型失败 → 同后端回退 inswapper_128（画质降级但绝不挂，保实时基线）
    if _swap_model_path != INSWAPPER_MODEL:
        print("[FaceSwap API] 回退默认 inswapper_128（同执行后端）…")
        try:
            face_swapper = _get_swap_model(INSWAPPER_MODEL, providers, _swap_popts)
            _swap_model_path = INSWAPPER_MODEL
            _swap_model_name = "inswapper_128"
            print("[FaceSwap API] inswapper_128 回退成功")
        except Exception as e1:
            print(f"[FaceSwap API] inswapper_128 同后端回退失败: {e1}")
    # ② TRT 构建/加载失败 → 退 CUDA + inswapper_128（保留 GPU 加速，仅去掉 TRT）
    if face_swapper is None and _backend_label == "tensorrt":
        print("[FaceSwap API] TRT 失败，回退 CUDAExecutionProvider…")
        try:
            face_swapper = _get_swap_model(INSWAPPER_MODEL,
                                           ["CUDAExecutionProvider", "CPUExecutionProvider"], None)
            _backend_label = "cuda_fallback"
            _swap_model_path = INSWAPPER_MODEL
            _swap_model_name = "inswapper_128"
            print("[FaceSwap API] CUDA 回退成功")
        except Exception as ec:
            print(f"[FaceSwap API] CUDA 回退失败: {ec}")
    # ③ 仍失败且非 DML → 最后回退 DML + inswapper_128
    if face_swapper is None and _backend_label not in ("dml", "dml_fallback"):
        print("[FaceSwap API] 回退 DmlExecutionProvider…")
        try:
            face_swapper = insightface.model_zoo.get_model(INSWAPPER_MODEL,
                                                           providers=['DmlExecutionProvider', 'CPUExecutionProvider'])
            _backend_label = "dml_fallback"
            _swap_model_path = INSWAPPER_MODEL
            _swap_model_name = "inswapper_128"
            print("[FaceSwap API] 换脸模型 DML 回退成功")
        except Exception as e2:
            print(f"[FaceSwap API] DML 回退也失败: {e2}")
            face_swapper = None

# 静默回退兜底：ORT 在某 GPU EP 加载失败时可能「不抛异常」直接回退 CPU（上面的 except 便不触发），
# 换脸会静默跑在 CPU（~150ms/帧，实时不可用）。这里校验实际生效的 providers，只剩 CPU 就强制重载 CUDA。
try:
    _real_prov = list(face_swapper.session.get_providers()) if face_swapper is not None else []
except Exception:
    _real_prov = []
if face_swapper is not None and _real_prov and all("CPU" in p for p in _real_prov):
    print(f"[FaceSwap API] ⚠ 换脸实际后端仅 CPU {_real_prov} — 某 GPU EP 静默回退，强制重载 CUDA…")
    try:
        face_swapper = _get_swap_model(_swap_model_path, ["CUDAExecutionProvider", "CPUExecutionProvider"], None)
        _backend_label = "cuda_fallback"
        print(f"[FaceSwap API] 强制 CUDA 重载完成，providers={face_swapper.session.get_providers()}")
    except Exception as _ec:
        print(f"[FaceSwap API] 强制 CUDA 重载失败（换脸将维持 CPU，性能受限）: {_ec}")


# ── S2 运行时热切换（DFM 每角色模型直播落地）───────────────────────────────
#   痛点：一个进程只常驻一个换脸网，DFM 的「身份即模型」意味着换角色=换模型，而模型此前只在
#   启动时读一次 → DFM 永远上不了直播。这里加 LRU 模型缓存 + /model/reload，让 Hub 激活某角色时
#   把生产实例热切到该角色的 .dfm，且 inswapper_128 基线常驻不被淘汰（回退/降级永远可用）。
#   线程安全：换绑只改全局引用（原子）；在途 /faceswap 在函数开头快照 swapper 局部引用，
#   即便同时被淘汰出缓存，对象仍被局部引用持有、GC 不回收 → 无 use-after-free。
import json
from collections import OrderedDict as _OD
_MODEL_LRU_CAP = max(2, int(os.environ.get("FACESWAP_MODEL_LRU", "3")))
_DFM_LIB_DIR   = Path(rf"{_BASE}\_pending_models\community")
_INSWAPPER_AP  = os.path.normcase(os.path.abspath(INSWAPPER_MODEL))
# 基线模型绝对路径(主引擎=HyperSwap-256,副本=inswapper_128)。default 解析到它、且不被 LRU 淘汰——
# 否则切 DFM 再切回"通用换脸"时基线被逐出→回退 CUDA 重载(丢 TRT、慢)。
_BASE_AP       = os.path.normcase(os.path.abspath(_swap_model_path)) if _swap_model_path else _INSWAPPER_AP
_model_cache   = _OD()                 # abspath -> swapper（含基线，常驻不淘汰）
_model_lock    = threading.RLock()
if face_swapper is not None:
    _model_cache[_BASE_AP] = face_swapper


def _dfm_lib_index():
    """社区 DFM 库文件名 → 绝对路径（拍平，末次命中优先）。缺库目录返回空。"""
    idx = {}
    if _DFM_LIB_DIR.exists():
        for p in _DFM_LIB_DIR.rglob("*.dfm"):
            idx[p.name.lower()] = str(p)
    return idx


def _resolve_swap_model(name: str) -> str:
    """把 /model/reload|/faceswap 的 model 参数解析成一个存在的模型绝对路径。
    接受：''/default/inswapper/base → inswapper_128；<xxx.dfm 文件名> → 库内查找；<绝对路径> → 原样。"""
    n = (name or "").strip()
    # "default"/"base" → 基线换脸核(主引擎=HyperSwap-256 高清核，副本=inswapper)。2026-07-10：
    # 此前硬编码 inswapper_128，导致 Hub 每次激活非 DFM 角色推 default 都把主引擎从 HS 打回
    # inswapper(丢高清+丢 TRT)。显式 "inswapper"/"inswapper_128" 仍精确指 inswapper(容灾/调试用)。
    if n == "" or n.lower() in ("default", "base"):
        return _base_swap_model()
    if n.lower() in ("inswapper", "inswapper_128"):
        return INSWAPPER_MODEL
    if os.path.isabs(n) and Path(n).is_file():
        return n
    key = Path(n).name.lower()
    if not key.endswith(".dfm") and not key.endswith(".onnx"):
        key += ".dfm"
    hit = _dfm_lib_index().get(key)
    if hit:
        return hit
    raise FileNotFoundError(f"未找到模型: {name}（库目录 {_DFM_LIB_DIR}）")


def _evict_models(keep_ap: str):
    """LRU 淘汰：超出容量时丢最旧的、且非 inswapper 基线、非当前活动模型。"""
    with _model_lock:
        while len(_model_cache) > _MODEL_LRU_CAP:
            victim = None
            for k in _model_cache:                     # OrderedDict 从旧到新
                if k != _INSWAPPER_AP and k != _BASE_AP and k != keep_ap:
                    victim = k; break
            if victim is None:
                break
            _model_cache.pop(victim, None)


def _hotload_providers():
    """热加载(非启动默认)模型的 provider：沿用实例启动后端，但**剥掉 TensorRT**。
    原因：TRT 每个模型首次加载要现场构建引擎(数分钟 + 缓存膨胀)。per-identity DFM 有上百个，
    逐个建引擎既不可行又会冻结直播；启动后端里 TRT 之后恒跟 CUDA(见 _execution_backend)，
    剥掉 TRT 即自然落到 CUDA(GPU 实例)或 DML/CPU(按实例本身的设备选择)——不越权改设备。
    启动默认模型仍按 _execution_backend()（可 TRT，逐帧瓶颈值得一次性构建）——此处只管热切的那些。"""
    base = [p for p in providers if p != "TensorrtExecutionProvider"]
    if not base:
        base = ["CPUExecutionProvider"]
    if "CPUExecutionProvider" not in base:
        base = base + ["CPUExecutionProvider"]
    return base


def _warm_swapper(m):
    """热切后跑一次真实换脸，让 cuDNN autotune 在切换时(而非首帧上屏时)完成——避免直播换角色时首帧卡顿。
    需带 landmark 的目标脸：复用激活明星脸 / 捆绑 _warmup_face.jpg。全程 best-effort，失败静默。"""
    try:
        if face_analyser is None:
            return
        wb = _active_face_b64
        if not wb:
            wf = Path(__file__).resolve().parent / "_warmup_face.jpg"
            if wf.exists():
                wb = base64.b64encode(wf.read_bytes()).decode()
        if not wb:
            return
        img = b64_to_img(wb)
        faces = face_analyser.get(img)
        if faces:
            m.get(img.copy(), faces[0], faces[0], paste_back=True)
    except Exception:
        pass


def _get_or_load_swapper(path: str, warm: bool = False):
    """按需加载 + LRU 缓存换脸网。命中即返回（毫秒级）；未命中在锁外加载（数秒），避免堵塞在途推理。
    warm=True（热切换用）：加载后同步预热一次，让直播换角色首帧不吃冷启。"""
    ap = os.path.normcase(os.path.abspath(path))
    with _model_lock:
        m = _model_cache.get(ap)
        if m is not None:
            _model_cache.move_to_end(ap)
            return m
    # 热加载走 CUDA（跳过 TRT 的 per-model 引擎构建）；默认模型已在启动时按其后端建好并常驻缓存。
    m = _get_swap_model(path, _hotload_providers(), None)  # 锁外加载
    if warm:
        _warm_swapper(m)
    with _model_lock:
        _model_cache[ap] = m
        _model_cache.move_to_end(ap)
        _evict_models(ap)
    return m

# ── 增强提速开关（必须先于下方模型装载定义——GFPGAN 装载即读 ENH_HALF；
#    2026-07-05 教训:定义放装载之后=NameError 被装载 try 静默吞掉→增强整体失效）──
#   实测 .104(4070) hd 单发: detect 45 + swap 22 + enhance 216ms —— enhance 内部近半是
#   GFPGANer.enhance() 用 RetinaFace-ResNet50 对换脸结果**重复检测**(insightface 的 5 点
#   kps 明明现成)。各项不换模型/不降分辨率,可独立关闭:
#   FACESWAP_ENH_REUSE=1    增强复用换脸阶段 kps,跳过 RetinaFace 二次检测(异常自动回退旧路)
#   FACESWAP_ENH_AUTOCAST=1 GFPGAN/parsing 前向走 torch.autocast fp16(权重仍 fp32)
#   FACESWAP_ENH_HALF=1     GFPGAN 权重整体 fp16(免逐算子 cast;增强在共享锁内,锁内时长决定并发吞吐)
#   FACESWAP_SRC_CACHE=1    源脸 buffalo_l 分析按内容哈希缓存(直播中源脸=同一张明星照,逐帧重析纯浪费)
ENH_REUSE_KPS  = os.environ.get("FACESWAP_ENH_REUSE", "1") == "1"
ENH_AUTOCAST   = os.environ.get("FACESWAP_ENH_AUTOCAST", "1") == "1"
# ENH_HALF 默认改 0（2026-07-07 循证）：GFPGANv1.4(clean/StyleGAN2) 权重整体 .half() 后前向
# 数值塌缩(输出范围收敛到 -0.45~-0.15，正常 -0.91~1.06)→ 修复脸=废图 → 贴回 parsing 网在废图上
# 找不到脸 → mask 全零 → 增强【静默变直通】(输出=原图,像素 diff=0.0)且每帧白烧 ~350ms。
# autocast(fp16 算子级)与 fp32 基准 diff 仅 0.2，速度同样吃 Tensor Core——半精度只走 autocast。
ENH_HALF       = os.environ.get("FACESWAP_ENH_HALF", "0") == "1" and torch.cuda.is_available()
SRC_CACHE_ON   = os.environ.get("FACESWAP_SRC_CACHE", "1") == "1"

# S6: FACESWAP_LOAD_ENHANCE=0 → 完全不加载 GFPGAN/CodeFormer（省 ~2GB 显存）。
#   供「容灾瘦身副本」(5090 与 TTS/LLM 共卡，余量紧)：副本只保直播 natural 档(无增强)，
#   enhance 请求走 face_enhancer=None 的既有降级路径（跳过增强，不报错）。生产 .104 不设=不变。
_LOAD_ENHANCE = os.environ.get("FACESWAP_LOAD_ENHANCE", "1") == "1"

face_enhancer = None
if _LOAD_ENHANCE:
    print("[FaceSwap API] 正在加载 GFPGAN 人脸增强模型...")
    try:
        import urllib.request
        if not Path(GFPGAN_MODEL).exists():
            print("[FaceSwap API] 下载 GFPGANv1.4 模型...")
            urllib.request.urlretrieve(
                "https://github.com/TencentARC/GFPGAN/releases/download/v1.3.4/GFPGANv1.4.pth",
                GFPGAN_MODEL)
        face_enhancer = GFPGANer(
            model_path=GFPGAN_MODEL, upscale=1, arch='clean',
            channel_multiplier=2, bg_upsampler=None,
            device=torch.device('cuda' if torch.cuda.is_available() else 'cpu'))
        if ENH_HALF:
            face_enhancer.gfpgan.half()          # 权重整体 fp16(见 ENH_HALF 注释);快路喂 half 输入
        print(f"[FaceSwap API] GFPGAN 加载完成 (fp16={ENH_HALF})")
    except Exception as e:
        print(f"[FaceSwap API] GFPGAN 加载失败: {e}")
        face_enhancer = None
else:
    print("[FaceSwap API] FACESWAP_LOAD_ENHANCE=0 → 跳过 GFPGAN（瘦身副本模式）")

codeformer_net = None
face_helper = None
DEVICE = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
if _LOAD_ENHANCE and (ARCH_REGISTRY is None or not Path(CODEFORMER_MODEL).exists()):
    # Lite 档/未部署 CodeFormer 仓库的机器：这是预期形态，给可读日志而不是
    # 抛 "'NoneType' object has no attribute 'get'" 这种像事故的报错（140 装机复盘）。
    print("[FaceSwap API] CodeFormer 未随部署（缺仓库或权重），跳过该增强（GFPGAN 不受影响）")
elif _LOAD_ENHANCE:
    print("[FaceSwap API] 正在加载 CodeFormer 人脸增强模型...")
    try:
        # 加载 CodeFormer 网络
        codeformer_net = ARCH_REGISTRY.get('CodeFormer')(
            dim_embd=512, codebook_size=1024, n_head=8, n_layers=9,
            connect_list=['32', '64', '128', '256']).to(DEVICE)
        ckpt = torch.load(CODEFORMER_MODEL, map_location=DEVICE)
        codeformer_net.load_state_dict(ckpt['params_ema'])
        codeformer_net.eval()
        # 人脸辅助工具（用于对齐、裁剪、贴回）
        face_helper = FaceRestoreHelper(
            1, face_size=512, crop_ratio=(1,1),
            det_model='retinaface_resnet50', save_ext='png',
            use_parse=True, device=DEVICE)
        print(f"[FaceSwap API] CodeFormer 加载完成 ({DEVICE})")
    except Exception as e:
        print(f"[FaceSwap API] CodeFormer 加载失败: {e}")
        codeformer_net = None
        face_helper = None
else:
    print("[FaceSwap API] FACESWAP_LOAD_ENHANCE=0 → 跳过 CodeFormer（瘦身副本模式）")

# ── 增强并发池（2026-07-06 P4：拆 _enhance_lock 串行瓶颈）──────────────────────
#   痛点：GFPGAN/CodeFormer 前向在 _enhance_lock 内串行，多 worker 只能排队(实测 GFPGAN 1→6
#   worker 仅 2.35→4.79fps，全被锁憋住)。根因不是神经网权重(eval+no_grad 下同一 module 并发前向
#   是安全的，CUDA kernel 本就异步并发)，而是 **FaceRestoreHelper 的逐调用可变状态**
#   (cropped_faces/affine/input_img 存实例属性)不可并发共享。
#   解法：给每个并发槽一份**独立 helper**(隔离可变状态)，共享重型网权重；用 Queue 同时充当
#   「并发上限 + 空闲 helper 分配器」。concurrency=1 → 完全走旧路(锁+内置 helper)，逐字节零回归。
#   VRAM：池内 helper 只走 kps 快路(从不调自带 RetinaFace)→构造后释放其 face_det 省显存；
#   parsing 网(贴回用)保留。每槽增量 ~85MB，4070 余量(~5G)可稳撑 concurrency≤4。
import contextlib as _ctxlib          # 供下方 _enh_slot 上下文管理器(定义时即需)
_ENH_CONCURRENCY = max(1, int(os.environ.get("FACESWAP_ENH_CONCURRENCY", "1")))
_gf_pool = None
_cf_pool = None

def _build_enh_pools():
    global _gf_pool, _cf_pool
    if _ENH_CONCURRENCY <= 1:
        return
    import queue as _q
    if face_enhancer is not None:
        _gf_pool = _q.Queue()
        _gf_cls = type(face_enhancer.face_helper)
        # model_rootpath 必须绝对路径：facexlib 按它找/下权重，相对路径遇 cwd≠工作区就触发
        # 全量重下载(2026-07-06 探针实例卡死正是这个)。权重与 GFPGANer 自建 helper 同址即秒加载。
        _gf_root = str(Path(__file__).resolve().parent / "gfpgan" / "weights")
        for _ in range(_ENH_CONCURRENCY):
            try:
                h = _gf_cls(1, face_size=512, crop_ratio=(1, 1), det_model='retinaface_resnet50',
                            save_ext='png', use_parse=True, device=face_enhancer.device,
                            model_rootpath=_gf_root)
                h.face_det = None      # 只用 kps 快路，检测器是死重，释放省显存
                _gf_pool.put(h)
            except Exception as _e:
                print(f"[FaceSwap API] GFPGAN 增强池构建第{_+1}份失败(降级到锁): {_e}")
                _gf_pool = None
                break
    if codeformer_net is not None and face_helper is not None:
        _cf_pool = _q.Queue()
        _cf_cls = type(face_helper)
        for _ in range(_ENH_CONCURRENCY):
            try:
                h = _cf_cls(1, face_size=512, crop_ratio=(1, 1), det_model='retinaface_resnet50',
                            save_ext='png', use_parse=True, device=DEVICE)
                h.face_det = None
                _cf_pool.put(h)
            except Exception as _e:
                print(f"[FaceSwap API] CodeFormer 增强池构建第{_+1}份失败(降级到锁): {_e}")
                _cf_pool = None
                break
    print(f"[FaceSwap API] 增强并发池就绪 concurrency={_ENH_CONCURRENCY} "
          f"gf_pool={'on' if _gf_pool else 'off'} cf_pool={'on' if _cf_pool else 'off'}")

_build_enh_pools()


@_ctxlib.contextmanager
def _enh_slot(kind):
    """增强临界区：concurrency=1 → 旧锁+内置 helper(零回归)；>1 → 从池取独占 helper 并发。
    yield (helper 或 None, pooled:bool)。helper=None 表示走内置(GFPGANer 自带)helper。"""
    pool = _gf_pool if kind == "gf" else _cf_pool
    if pool is None:
        with _enhance_lock:
            yield None, False
    else:
        h = pool.get()
        try:
            yield h, True
        finally:
            pool.put(h)

# ── FastAPI 应用 ────────────────────────────────────────────────
app = FastAPI(title="FaceSwap API v2")
import service_auth                                  # GPU 服务面加固：鉴权 + CORS 收敛
service_auth.secure(app, name="faceswap")            # 替代原 CORS:* 无鉴权
# 崩溃匿名上报（P0 遥测，2026-07-13）：未捕获异常→栈签名级脱敏事件（默认开可关，
# 24h 去重+日限频+离线排队）；模块缺失（老部署机）=完全无感。
try:
    import telemetry_client as _tc
    _tc.install("faceswap")
except Exception:
    pass

# 联网注册（后台机器名单数据源）：分机跑换脸服务即自动登记本机 + 周期心跳保鲜。
# 管理通道非内容，可 AVATARHUB_ADMIN_REGISTER=0 关。模块缺失（老部署机）=完全无感。
try:
    import admin_client as _ac
    _ac.install("faceswap")
except Exception:
    pass

class SwapRequest(BaseModel):
    source_image: str = ""   # 留空则使用当前激活的明星脸
    target_image: str
    blend: float | None = None
    threshold: float | None = None
    smooth_alpha: float | None = None
    smooth_mode: str | None = None  # motion(默认)/flow(光流补偿)/off；单次覆盖 PARAMS
    enhance: str | None = None  # none/gfpgan/codeformer/gpen(2026-07-09 ONNX 轻精修,直播档低时延)
    # 2026-07-09 贴回/遮挡单次覆盖(None=按 PARAMS 默认；直播链路可逐帧热切,零重启)：
    #   blend_mode: "poisson"|"feather"；occlusion: XSeg 遮挡掩码开关。
    blend_mode: str | None = None
    occlusion: bool | None = None
    # 口型区保护(2026-07-10)：贴回时把目标脸真实嘴区(106点唇部凸包外扩+羽化)从换脸掩码里
    # 扣掉——说话口型/牙齿/舌 100% 真实(带货口播刚需)。None=按 PARAMS.mouth_mask 默认(关)。
    # 仅羽化贴回路径生效(开启会自动升级 custom paste)；DFM 有自己的贴回,暂不支持。
    mouth_mask: bool | None = None
    # 小脸不换门槛(2026-07-10)：目标脸最长边 < min_face_px(收到图坐标) → 不换,原样返回。
    # 远处走动时脸只有一二十像素,换脸只会糊成一团反而暴露"有假";远景保原画、近景才换。
    # 0/None=不设限(旧行为)。裁剪通道送的是紧邻脸 crop(脸占满),不会误伤——只对全帧远景路径生效。
    min_face_px: int | None = None
    # 贴回掩码内缩 [上,右,下,左] 百分比(2026-07-10 光头主播修缮)：目标是光头而源脸/角色有
    # 头发时，对齐框边缘的源发(鬓角/发际)会随贴回糊上头皮 → 头两侧深色发带。内缩把掩码
    # 边缘按百分比扣掉，头发不进贴回区。开启后自动走羽化贴回(掩码要乘进贴回)；DFM 在其
    # 自有贴回内同样生效。None=按 PARAMS.mask_padding 默认；[0,0,0,0]=关(零回归)。
    mask_padding: list[float] | None = None
    # Phase 12 C-2 双人/多人 face_map：目标脸按 x 中心从左到右排序，槽 i 用 source_map[i] 源脸；
    # 超出映射的脸回退首个可用槽——绝不留未换的真脸上屏。None=旧单源行为(未带字段时回退
    # 引擎粘滞槽位,见 /face_map/active)；显式 []=强制关(直连帧可覆盖粘滞态)。
    # 槽数上限 FACE_MAP_MAX_SLOTS(默认 4)。
    source_map: list[str] | None = None
    # 仅换主脸(2026-07-06p 直播实测)：多脸场景只换最大脸，其余不换不修——时延回单脸水平、
    # 海报/路人不被误换、faces_boxes 只回主脸框(客户端裁剪通道因此能在多脸场景保持咬合)。
    # None/False=旧行为(全换)；显式 source_map(双人档)优先，不受此开关影响。
    main_face_only: bool | None = None
    # 主脸滞回提示(2026-07-06s)：上一帧主脸中心 [cx,cy]（目标图坐标系，即发来的这张图的像素坐标）。
    # 配 main_face_only 用：两人近等大时在位者优先，挑战者面积 ≥1.3× 才换主（防身份闪切）。
    # 不传=纯最大脸（旧行为）。见 _pick_main_face。
    main_face_hint: list[float] | None = None
    # 多照片源脸(2026-07-07 辨识度增强)：同一角色多角度照片的 ArcFace embedding 平均，
    # 比单张更鲁棒、更贴近目标人身份 → 换脸辨识度提升(缓解"甲换乙看不出来")。两种投递：
    #   source_images: 直接给多图 b64(图片换脸/预览用，现算平均，按列表内容哈希缓存)；
    #   source_key   : 用 /faces/switch 预存的命名平均 embedding(直播链路用，每帧只传短 key，零额外传输)。
    # 优先级：source_map(双人) > source_images > source_key > source_image(单图，旧行为，零回归)。
    source_images: list[str] | None = None
    source_key: str | None = None
    # C-5 直播妆容层(2026-07-08)：换脸/增强完成后在输出帧的目标脸区上妆。
    # 为什么在这里而不是源脸：inswapper 走 ArcFace 身份向量，源脸上的妆在输出中基本
    # 不存留——妆容必须画在输出端才可见。字段：{lip_color:[B,G,R], lip:0~1,
    # blush_color, blush, eye_color, eye}。None=零回归；复用已检测的 tgt_face
    # landmark/kps，单脸开销 ~1-3ms。
    makeup: dict | None = None
    # S2 每请求模型覆盖：{model:'<xxx.dfm>|default'}。仅本请求用该模型（LRU 缓存，首次加载数秒后毫秒级），
    # 不改实例默认（区别于 /model/reload 的持久换绑）→ 支持 A/B、多角色并发试换而互不干扰。
    # 命中 DFM 时自动旁路源脸检测（DFM 身份即模型，忽略 source_*）。None=用实例当前默认（零回归）。
    model: str | None = None

class SwapResponse(BaseModel):
    result_image: str
    elapsed_ms: int
    faces_src: Optional[int] = None
    faces_tgt: Optional[int] = None
    faces_used: Optional[int] = None
    faces_filtered: Optional[int] = None
    detect_ms: Optional[int] = None
    swap_ms: Optional[int] = None
    enhance_ms: Optional[int] = None
    smooth_ms: Optional[int] = None
    faces_boxes: Optional[list] = None     # 真被换过的脸 bbox [[x1,y1,x2,y2],...]（结果图坐标系，供画质体检定位脸区）
    face_map_used: Optional[int] = None    # C-2: 本次按槽映射换的脸数(None=未启用 face_map)
    makeup_ms: Optional[int] = None        # C-5: 直播妆容层耗时(None=未启用)

def b64_to_img(b64_str: str) -> np.ndarray:
    if "," in b64_str:
        b64_str = b64_str.split(",", 1)[1]
    data = base64.b64decode(b64_str)
    arr = np.frombuffer(data, np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)

def img_to_b64(img: np.ndarray, quality: int = 90) -> str:
    _, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return base64.b64encode(buf).decode()

_MAKEUP_WORK_SIDE = 288      # 妆容工作区长边上限：色移是低频量，小图算 delta 再上采样，纹理零损

def _apply_live_makeup(img: np.ndarray, faces, spec: dict) -> np.ndarray:
    """C-5 直播妆容层：在换脸输出帧的目标脸区上妆（唇/腮红/眼影）。
    复用换脸阶段已检测的 tgt_face（landmark_2d_106/kps/bbox），零二次检测。
    性能关键（2026-07-08 实测迭代）：全分辨率 ROI 大核模糊 61ms/帧不可接受 →
    改「降采样工作区算色差 delta，双线性上采样叠回」——妆容是低频颜色偏移，
    delta 上采样视觉无损、原图纹理原封不动，稳态 ~2ms/帧。
    唇色按红度加权（LAB a 通道 132~142 门限）——张嘴露齿时牙齿不被染色。
    任何异常原图返回（软降级）。"""
    try:
        lip_c = spec.get("lip_color");   lip_s = float(spec.get("lip", 0) or 0)
        blush_c = spec.get("blush_color"); blush_s = float(spec.get("blush", 0) or 0)
        eye_c = spec.get("eye_color");   eye_s = float(spec.get("eye", 0) or 0)
        if not ((lip_c and lip_s > 0) or (blush_c and blush_s > 0) or (eye_c and eye_s > 0)):
            return img
        out = img
        h, w = img.shape[:2]
        for face in faces:
            kps = getattr(face, "kps", None)
            bb = getattr(face, "bbox", None)
            if kps is None or bb is None:
                continue
            kps = np.asarray(kps, np.float32)
            ed = float(np.linalg.norm(kps[1] - kps[0])) or 1.0
            x1, y1, x2, y2 = [int(v) for v in bb]
            pad = int(ed * 0.9)
            x1 = max(0, x1 - pad); y1 = max(0, y1 - pad)
            x2 = min(w, x2 + pad); y2 = min(h, y2 + pad)
            rw, rh = x2 - x1, y2 - y1
            if rw < 8 or rh < 8:
                continue
            roi = out[y1:y2, x1:x2]

            # 降采样工作区（长边 ≤ _MAKEUP_WORK_SIDE），所有掩码/模糊/移色都在小图上
            sc = min(1.0, _MAKEUP_WORK_SIDE / max(rw, rh))
            sw, sh = max(2, int(rw * sc)), max(2, int(rh * sc))
            small = cv2.resize(roi, (sw, sh), interpolation=cv2.INTER_AREA) if sc < 1.0 else roi
            lab = cv2.cvtColor(small, cv2.COLOR_BGR2LAB).astype(np.float32)
            sed = ed * sc
            off = np.float32([x1, y1])

            def _shift(mask, color, strength, l_factor):
                tgt = cv2.cvtColor(np.uint8([[list(color)]]),
                                   cv2.COLOR_BGR2LAB)[0, 0].astype(np.float32)
                m = (mask * strength)[..., None]
                lab[..., 1:] += m * (tgt[1:] - lab[..., 1:])
                lab[..., 0:1] += m * l_factor * (tgt[0] - lab[..., 0:1])

            if lip_c and lip_s > 0:
                lm106 = getattr(face, "landmark_2d_106", None)
                mask = np.zeros((sh, sw), np.float32)
                if lm106 is not None and len(lm106) >= 72:
                    hull = cv2.convexHull(np.round(
                        (np.asarray(lm106, np.float32)[52:72] - off) * sc).astype(np.int32))
                    cv2.fillConvexPoly(mask, hull, 1.0)
                else:                       # 无 106 点回退：嘴角 kps 椭圆
                    c = ((kps[3] + kps[4]) / 2 - off) * sc
                    cv2.ellipse(mask, (int(c[0]), int(c[1])),
                                (int(sed * 0.42), int(sed * 0.22)), 0, 0, 360, 1.0, -1)
                k = max(3, int(sed * 0.10)) | 1
                mask = cv2.GaussianBlur(mask, (k, k), 0)
                mask = mask * np.clip((lab[..., 1] - 132.0) / 10.0, 0.0, 1.0)
                _shift(mask, lip_c, min(lip_s, 1.0), 0.35)

            if blush_c and blush_s > 0:
                mask = np.zeros((sh, sw), np.float32)
                for ei, mi in ((0, 3), (1, 4)):    # 眼→同侧嘴角连线 55% 处外推=颧骨
                    c = (kps[ei] + 0.55 * (kps[mi] - kps[ei]) - off) * sc
                    cx = c[0] + (-1 if ei == 0 else 1) * sed * 0.28
                    cv2.ellipse(mask, (int(cx), int(c[1])),
                                (int(sed * 0.30), int(sed * 0.20)), 0, 0, 360, 1.0, -1)
                k = max(9, int(sed * 0.8)) | 1
                mask = cv2.GaussianBlur(mask, (k, k), 0)
                _shift(mask, blush_c, min(blush_s, 1.0), 0.10)

            if eye_c and eye_s > 0:
                mask = np.zeros((sh, sw), np.float32)
                for ei in (0, 1):
                    c = (kps[ei] - off) * sc
                    cv2.ellipse(mask, (int(c[0]), int(c[1] - sed * 0.24)),
                                (int(sed * 0.30), int(sed * 0.15)), 0, 0, 360, 1.0, -1)
                k = max(5, int(sed * 0.25)) | 1
                mask = cv2.GaussianBlur(mask, (k, k), 0)
                _shift(mask, eye_c, min(eye_s, 1.0), 0.40)

            small_out = cv2.cvtColor(np.clip(lab, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR)
            if sc < 1.0:                    # delta 上采样叠回：全分辨率纹理不动，只加低频色移
                delta = small_out.astype(np.int16) - small.astype(np.int16)
                delta = cv2.resize(delta, (rw, rh), interpolation=cv2.INTER_LINEAR)
                out[y1:y2, x1:x2] = np.clip(roi.astype(np.int16) + delta, 0, 255).astype(np.uint8)
            else:
                out[y1:y2, x1:x2] = small_out
        return out
    except Exception as e:
        print(f"[FaceSwap API] 直播妆容层失败(跳过): {e}")
        return img


def poisson_blend_face(original: np.ndarray, swapped: np.ndarray,
                       faces) -> np.ndarray:
    """对每张检测到的人脸区域做 Poisson 无缝融合，消除硬边"""
    try:
        result = swapped.copy()
        for face in faces:
            bbox = face.bbox.astype(int)
            x1, y1, x2, y2 = bbox
            # 扩展边界框留出羽化空间
            pad = 20
            x1 = max(0, x1 - pad); y1 = max(0, y1 - pad)
            x2 = min(swapped.shape[1], x2 + pad)
            y2 = min(swapped.shape[0], y2 + pad)
            w, h = x2 - x1, y2 - y1
            if w < 20 or h < 20:
                continue
            # 椭圆遮罩
            mask = np.zeros((h, w), dtype=np.uint8)
            cx, cy = w // 2, h // 2
            cv2.ellipse(mask, (cx, cy), (cx - 5, cy - 5), 0, 0, 360, 255, -1)
            center = (x1 + cx, y1 + cy)
            result = cv2.seamlessClone(
                swapped[y1:y2, x1:x2], original, mask, center,
                cv2.NORMAL_CLONE)
        return result
    except Exception as e:
        return swapped

# 时序平滑：运动自适应版本
_prev_result: np.ndarray = None
_prev_face_center: tuple = None
MOTION_THRESHOLD = 15  # 人脸中心移动超过15像素视为运动，跳过平滑
# 人脸增强(CodeFormer/GFPGAN)使用共享 face_helper，非线程安全；
# 多并发换脸请求时必须串行化，否则共享状态会被串导致崩溃/花屏。
_enhance_lock = threading.Lock()

# ── 增强提速：源脸缓存容器(开关定义见上方 GFPGAN 装载前) ────────────────────
_src_face_cache: dict = {}          # (len,hash(b64)) -> insightface Face；换源脸=新键,天然失效
_SRC_CACHE_MAX = 8

# ── 多照片源脸 embedding 平均（2026-07-07 辨识度增强）───────────────────────────
#   单张源照的 ArcFace embedding 受角度/光照/表情扰动；同一角色多张照片各自 L2 归一后平均，
#   得到更稳、更贴近该身份的向量 → 换脸更"像"目标人（缓解「甲换乙看不出来」的辨识度短板）。
#   平均发生在 emap 投影之前，与 inswapper 的 latent=emb·emap 流程正交；单张时平均=自身，零回归。
_named_embeddings: dict = {}        # source_key -> np.ndarray(平均 embedding)；直播链路每帧只传短 key
_avg_emb_cache: dict = {}           # (n,hash) -> np.ndarray；source_images 现算平均的结果缓存
_AVG_EMB_CACHE_MAX = 16
# 命名平均落盘：引擎被看门狗自愈重启后不丢已注册平均(否则静默退回单照直到下次激活)。
# 每条 512×float32≈2KB，npz 整存整取。
_AVG_STORE = Path(rf"{_BASE}\data\avg_embeddings.npz")
_avg_store_lock = threading.Lock()


def _save_named_embeddings():
    try:
        with _avg_store_lock:
            _AVG_STORE.parent.mkdir(parents=True, exist_ok=True)
            if _named_embeddings:
                np.savez(str(_AVG_STORE), **_named_embeddings)
            elif _AVG_STORE.exists():
                _AVG_STORE.unlink()
    except Exception as e:
        print(f"[FaceSwap API] 命名平均源脸落盘失败(忽略): {e}")


def _load_named_embeddings():
    try:
        if _AVG_STORE.exists():
            d = np.load(str(_AVG_STORE))
            for k in d.files:
                _named_embeddings[k] = d[k]
            if d.files:
                print(f"[FaceSwap API] 载入命名平均源脸 {len(d.files)} 个")
    except Exception as e:
        print(f"[FaceSwap API] 命名平均源脸载入失败(忽略): {e}")


_load_named_embeddings()

# ── 身份放大 anchor（2026-07-07 辨识度杠杆；见 _id_amplify_decisive）────────────
#   inswapper 会把源身份"回归"向目标脸→辨识度被削。把源 embedding 沿"远离人群平均脸"方向外推
#   E' = normalize(E + β·(E - anchor))，抵消回归：实测 β=0.3 两明星互相可区分度 cross 0.09→-0.00，
#   各自仍像本人(0.91→0.89)、无畸变、零延迟。anchor = 全体角色脸 embedding 均值(由 Hub 汇总推送)。
_id_anchor = None                    # np.ndarray(512,) 归一；None=未设→放大自动跳过(零回归)
_ID_ANCHOR_STORE = Path(rf"{_BASE}\data\id_anchor.npy")


def _save_id_anchor():
    try:
        with _avg_store_lock:
            _ID_ANCHOR_STORE.parent.mkdir(parents=True, exist_ok=True)
            if _id_anchor is not None:
                np.save(str(_ID_ANCHOR_STORE), _id_anchor)
            elif _ID_ANCHOR_STORE.exists():
                _ID_ANCHOR_STORE.unlink()
    except Exception as e:
        print(f"[FaceSwap API] 身份 anchor 落盘失败(忽略): {e}")


def _load_id_anchor():
    global _id_anchor
    try:
        if _ID_ANCHOR_STORE.exists():
            _id_anchor = np.load(str(_ID_ANCHOR_STORE))
            print("[FaceSwap API] 载入身份放大 anchor")
    except Exception as e:
        print(f"[FaceSwap API] 身份 anchor 载入失败(忽略): {e}")


_load_id_anchor()


def _amplify_src_faces(src_faces):
    """身份放大：把每个源脸 embedding 沿远离人群 anchor 方向外推，增强辨识度。
    PARAMS['id_amplify']<=0 或 anchor 未设 → 原样返回(零回归)。β 上限保护防畸变。"""
    beta = float(PARAMS.get("id_amplify", 0.0) or 0.0)
    if beta <= 0 or _id_anchor is None or not src_faces:
        return src_faces
    beta = min(beta, float(PARAMS.get("id_amplify_max", 0.6)))
    a = _id_anchor / (np.linalg.norm(_id_anchor) + 1e-8)
    out = []
    for f in src_faces:
        try:
            e = np.asarray(f.normed_embedding, dtype=np.float32)
            e = e / (np.linalg.norm(e) + 1e-8)
            ep = e + beta * (e - a)
            out.append(_face_from_embedding(ep / (np.linalg.norm(ep) + 1e-8)))
        except Exception:
            out.append(f)
    return out


def _avg_embedding(b64_list):
    """多张源图 → 每张取主脸 ArcFace embedding、各自 L2 归一后平均。
    ≥3 张时按留一法(LOO)剔除离群照：cos(该照, 其余照均值方向) < 0.35 判为疑似他人/误传，
    不参与平均——实测同人多视角 LOO≈0.88、异人≈0.09，0.35 两侧余量都大；
    防止一张错图静默拉偏整个身份向量(均值对单张离群非常敏感)。
    返回 (avg_emb ndarray 或 None, 实际参与平均的图数)。全未检出 → (None, 0)。"""
    embs = []
    for b in (b64_list or []):
        if not b:
            continue
        img = b64_to_img(b)
        if img is None:
            continue
        fs = face_analyser.get(img)
        if fs:
            # 取面积最大脸作该照片主体（多人照/带路人时不被小脸污染身份向量）
            f = max(fs, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
            embs.append(np.asarray(f.normed_embedding, dtype=np.float32))
    if not embs:
        return None, 0
    if len(embs) >= 3:
        S = np.stack(embs, axis=0)
        tot = S.sum(axis=0)
        keep = []
        for i in range(len(embs)):
            loo = tot - S[i]
            loo = loo / (np.linalg.norm(loo) + 1e-8)
            if float(np.dot(S[i], loo)) >= 0.35:
                keep.append(embs[i])
        if 2 <= len(keep) < len(embs):
            print(f"[FaceSwap API] 多照平均：LOO 剔除 {len(embs)-len(keep)} 张离群照(疑似他人/误传)，保留 {len(keep)} 张")
            return np.mean(np.stack(keep, axis=0), axis=0), len(keep)
    return np.mean(np.stack(embs, axis=0), axis=0), len(embs)


def _face_from_embedding(emb):
    """把平均 embedding 包装成 insightface Face（换脸 .get() 只读 source_face.normed_embedding；
    Face.normed_embedding 会对传入 embedding 再做 L2 归一，得到单位向量喂给 emap 投影）。"""
    from insightface.app.common import Face
    return Face(embedding=np.asarray(emb, dtype=np.float32))


def _delta_highpass_merge(raw: np.ndarray, enhanced: np.ndarray) -> np.ndarray:
    """身份保护增强重组：final = raw + k·highpass(enhanced - raw)。
    增强器(GFPGAN)对换脸结果的改动 delta=enhanced-raw 只在脸区非零，含两部分：
      · 高频细节(想要的"清晰")  · 低频漂移(色/形均值化 → 把源明星身份稀释回目标脸)。
    只叠回 delta 的高频、丢弃低频漂移 → 清晰度拿满、身份留在 raw(最准)、背景零改动(delta 离脸为 0)。
    实测(448/512 帧)：身份 0.881→0.898、清晰度持平 gfpgan、削波/光晕 3.4%→2.0%。仅 ~1ms(一次高斯)。"""
    try:
        k = float(PARAMS.get("enh_id_hp_k", 1.0))
        sigma = float(PARAMS.get("enh_id_hp_sigma", 2.0))
        if k <= 0:
            return enhanced
        rf = raw.astype(np.float32)
        delta = enhanced.astype(np.float32) - rf
        hp = delta - cv2.GaussianBlur(delta, (0, 0), sigma)   # delta 的高频分量
        return np.clip(rf + k * hp, 0, 255).astype(np.uint8)
    except Exception as e:
        print(f"[FaceSwap API] deltaHP 重组失败(回退增强图): {e}")
        return enhanced


# ── 2026-07-09 直播链路升级：GPEN-256 ONNX 轻精修 + XSeg 遮挡掩码 + 羽化贴回 ──────
#   动机(时延+白斑+遮挡三箭齐发)：
#   · GPEN-BFR-256：FaceFusion 同款 ONNX 精修器(arcface_128×256 对齐,单次前向,无 parsing
#     贴回)。直播档 GFPGAN(PyTorch 512+facexlib helper)单脸 ~90-200ms 是 hd 档时延大头,
#     GPEN-256 CUDA 同效果 ~10ms 级。前后处理逐行对齐 FaceFusion face_enhancer 官方实现。
#   · XSeg：DeepFaceLab 官方遮挡分割(FaceFusion 3.x 同款 xseg_1)。话筒/手/刘海挡脸时把
#     遮挡物从贴回掩码里抠掉——换脸不再糊在障碍物上。输入=对齐脸 crop BGR NHWC [0,1] 256。
#   · 羽化贴回(blend_mode=feather)：用 box 羽化掩码自定义贴回替代 seamlessClone(Poisson)。
#     Poisson 会把周边亮度"渗"进脸区(绿幕强光下发白的根因之一)且 ~15ms/帧；羽化贴回
#     数学上不改脸区亮度、~2ms，是 FaceFusion/Deep-Live-Cam 直播路径的业界标准做法。
#   部署韧性：三者全部惰性加载、独立于 FACESWAP_LOAD_ENHANCE(重型增强)开关——瘦身容灾
#   副本也能享受轻精修(容灾接管期从"无增强"升级为"轻增强")；模型文件缺失/加载失败 →
#   静默降级(gpen 跳过、遮挡掩码=全1、feather 回退内置贴回)，绝不 500。
_GPEN_MODEL = rf"{_BASE}\models\gpen_bfr_256.onnx"
_XSEG_MODEL = os.environ.get("FACESWAP_XSEG_MODEL", rf"{_BASE}\models\xseg_1.onnx")
_AUX_PROVIDERS = None      # 轻量 ONNX 的 provider(懒求值：CUDA 可用则 CUDA，否则 CPU)


def _aux_providers():
    global _AUX_PROVIDERS
    if _AUX_PROVIDERS is None:
        avail = onnxruntime.get_available_providers()
        if os.environ.get("FACESWAP_CUDA", "1") == "0":     # CPU 实例(测试/无卡机)不抢显存
            avail = [p for p in avail if "CUDA" not in p]
        _AUX_PROVIDERS = [p for p in ("CUDAExecutionProvider", "CPUExecutionProvider") if p in avail] \
                         or ["CPUExecutionProvider"]
    return _AUX_PROVIDERS


def _aux_session(model_file: str, force_cpu: bool = False) -> "onnxruntime.InferenceSession":
    """轻量 ONNX(XSeg/GPEN)专用会话：显存池限额 + kSameAsRequested + HEURISTIC 卷积调优。
    2026-07-10 根因修复：默认 CUDA 会话的 arena 无上限 & kNextPowerOfTwo 倍增策略，在 .104
    这类显存高压(9.2/12GB)机器上与主换脸/GFPGAN 争用 → 每次扩池都触发驱动级碎片整理，
    XSeg 服务内实测 300-900ms/帧(独立进程仅 19ms)。限额小池(默认 768MB)一次建好不再伸缩。
    2026-07-10 二轮结论：辅助网 TRT **默认关**(FACESWAP_AUX_TRT=1 显式开)。全链循证：
      · fp16 数值崩：GPEN PSNR 27.9/maxDiff 207(可见伪影)、XSeg 掩码 meanDiff 0.30(全错)
        ——InstanceNorm/GAP+Sqrt 链是半精度重灾区。fp16 永久禁用(FACESWAP_AUX_TRT_FP16 保险丝)。
      · fp32 TRT 隔离虽快 2.3×(9.9→4.3ms)，但**生产 3 并发下净亏**：enh 41→47ms、
        连主换脸 TRT 引擎也被拖慢 18→22ms、单帧 78→91ms——多 TRT 引擎并发争用比
        CUDA 更凶(kernels 更宽、上下文切换更贵)。结论：12GB 单卡上 TRT 只给主换脸网；
        辅助网留 CUDA 限额小池。引擎缓存已预建留盘(auxnets/)，迁大卡/降并发后可再评。"""
    so = onnxruntime.SessionOptions()
    so.log_severity_level = 3
    if force_cpu:
        so.intra_op_num_threads = 2    # 后台低频推理：限 2 线程，绝不打满 CPU 干扰编解码
    provs = ["CPUExecutionProvider"] if force_cpu else list(_aux_providers())
    popts = []
    for p in provs:
        if p == "CUDAExecutionProvider":
            popts.append({"arena_extend_strategy": "kSameAsRequested",
                          "gpu_mem_limit": int(os.environ.get("FACESWAP_AUX_VRAM_MB", "768")) * 1024 * 1024,
                          "cudnn_conv_algo_search": "HEURISTIC"})
        else:
            popts.append({})
    _aux_trt_on = (os.environ.get("FACESWAP_AUX_TRT", "0") == "1"
                   and USE_TRT and _TRT_AVAILABLE and "CUDAExecutionProvider" in provs)
    if _aux_trt_on:
        # 目录名避开 Windows 保留设备名(AUX/CON/NUL/...)——"aux" 会 WinError 267 建不出来
        _aux_cache = os.path.join(os.path.dirname(TRT_CACHE_DIR), "auxnets")
        try:
            os.makedirs(_aux_cache, exist_ok=True)
        except Exception:
            pass
        provs = ["TensorrtExecutionProvider"] + provs
        popts = [{"trt_fp16_enable": os.environ.get("FACESWAP_AUX_TRT_FP16", "0") == "1",
                  "trt_engine_cache_enable": True,
                  "trt_engine_cache_path": _aux_cache,
                  "trt_timing_cache_enable": True}] + popts
    return onnxruntime.InferenceSession(model_file, sess_options=so,
                                        providers=provs, provider_options=popts)


_box_mask_cache: dict = {}


def _mask_padding_current(req_pad=None):
    """贴回掩码内缩(百分比 [上,右,下,左]，FaceFusion face_mask_padding 同义)。
    2026-07-10 光头主播根治「源脸有头发→头两侧黑边」：对齐裁剪框边缘落在光头头皮上，
    换脸网在框边生成的源发/鬓角会被方框掩码原样贴回(实测两侧深色竖带)。内缩把掩码
    边缘按百分比扣掉，头发根本不进贴回区。req 显式 > PARAMS 默认；全 0/畸形 → None(零回归)。"""
    pad = req_pad if req_pad is not None else PARAMS.get("mask_padding")
    if not pad:
        return None
    try:
        vals = [max(0.0, min(40.0, float(x))) for x in list(pad)[:4]]
    except Exception:
        return None
    vals += [0.0] * (4 - len(vals))
    return tuple(vals) if any(v > 0 for v in vals) else None


def _box_feather_mask(size_hw: tuple, padding=None) -> np.ndarray:
    """FaceFusion create_box_mask(blur=0.3) 同款羽化方框掩码；padding=[上,右,下,左]%
    内缩(消源发黑边)。按 (尺寸,padding) 缓存(只读共享；组合有限不膨胀)。"""
    key = (int(size_hw[0]), int(size_hw[1]), tuple(padding) if padding else None)
    m = _box_mask_cache.get(key)
    if m is None:
        h, w = key[0], key[1]
        blur_amount = int(w * 0.5 * 0.3)
        blur_area = max(blur_amount // 2, 1)
        t = r = b = l = blur_area
        if padding:
            t = max(t, int(h * padding[0] / 100.0))
            r = max(r, int(w * padding[1] / 100.0))
            b = max(b, int(h * padding[2] / 100.0))
            l = max(l, int(w * padding[3] / 100.0))
        m = np.ones((h, w), dtype=np.float32)
        m[:t, :] = 0; m[-b:, :] = 0
        m[:, :l] = 0; m[:, -r:] = 0
        if blur_amount > 0:
            m = cv2.GaussianBlur(m, (0, 0), blur_amount * 0.25)
        _box_mask_cache[key] = m
    return m


def _paste_crop_back(frame: np.ndarray, crop: np.ndarray, mask: np.ndarray, M: np.ndarray) -> np.ndarray:
    """对齐 crop 按 [0,1] 掩码贴回全帧（FaceFusion paste_back 同款：只 warp 贴回外接矩形，
    不做全帧 warp——512 宽直播帧单脸 ~1-2ms）。原地写 frame 并返回之。"""
    h, w = frame.shape[:2]
    ch, cw = crop.shape[:2]
    IM = cv2.invertAffineTransform(M)
    pts = np.array([[0, 0], [cw, 0], [cw, ch], [0, ch]], dtype=np.float32)
    proj = np.hstack([pts, np.ones((4, 1), dtype=np.float32)]) @ IM.T
    x1, y1 = np.clip(np.floor(proj.min(axis=0)).astype(int), 0, [w, h])
    x2, y2 = np.clip(np.ceil(proj.max(axis=0)).astype(int), 0, [w, h])
    if x2 <= x1 or y2 <= y1:
        return frame
    PM = IM.copy()
    PM[0, 2] -= x1
    PM[1, 2] -= y1
    pw, ph = int(x2 - x1), int(y2 - y1)
    inv_mask = cv2.warpAffine(mask, PM, (pw, ph)).clip(0, 1)[..., None]
    inv_crop = cv2.warpAffine(crop, PM, (pw, ph), borderMode=cv2.BORDER_REPLICATE)
    region = frame[y1:y2, x1:x2].astype(np.float32)
    frame[y1:y2, x1:x2] = (region * (1 - inv_mask) + inv_crop.astype(np.float32) * inv_mask).astype(frame.dtype)
    return frame


class _XSegOccluder:
    """XSeg 遮挡分割：吃任意对齐脸 crop(BGR)，吐 [0,1] 掩码(1=脸可见,0=遮挡物)。
    后处理与 FaceFusion create_occlusion_mask 一致：blur σ5 → clip(0.5,1) 拉伸。
    2026-07-10 四轮定稿——**异步后台刷新**：前三轮循证证明凡在帧路径内联推理必拖垮直播
    (GPU 内联: 12GB 卡与主换脸/GPEN 算力争用,swap 16→200-500ms；CPU 内联: 3 worker 把弱 CPU
    打满,全链 1470ms 熔毁)。遮挡物(话筒/手)时域高度稳定 → 掩码由独立后台线程按
    FACESWAP_XSEG_HZ(默认 3Hz)限频刷新，帧路径只读最新缓存**永不等待**：
      · GPU 占空比 ~6%(19ms/333ms)，主链无感；
      · 陈旧度 ≤ ~350ms，对在位遮挡物观感无差；
      · 无掩码/超龄(>FACESWAP_XSEG_STALE_S) → 全1(等效遮挡关)，绝不阻塞绝不出错帧；
      · 多脸帧(cache_ok=False)直接全1——异步缓存只有主脸一张,跨脸复用会串掩码。"""
    def __init__(self, model_file: str):
        # 默认 CPU(2026-07-10 四轮终版)：GPU 即便 3Hz 后台推理，contended 单发拉长到 200ms+、
        # 每秒 3 次即覆盖大半帧窗，直播 enh 45→96ms——12GB 卡在 12fps 满载下没有"顺带"余量。
        # CPU 异步(51ms/次×3Hz,限2线程)＝GPU 零占用、CPU ~15% 单核，主链实测无感。
        _cpu = os.environ.get("FACESWAP_XSEG_CPU", "1") == "1"
        self.session = _aux_session(model_file, force_cpu=_cpu)
        self.size = 256
        # 自适应频率(2026-07-10 五轮)：掩码帧间变化大(手/话筒在动)提到 hz_max 跟手，
        # 静止衰减到 hz_min 省 CPU。基准 3Hz 起步。
        self.hz_min = max(0.2, float(os.environ.get("FACESWAP_XSEG_HZ_MIN", "1")))
        self.hz_max = max(self.hz_min, float(os.environ.get("FACESWAP_XSEG_HZ_MAX", "6")))
        self._hz = min(self.hz_max, max(self.hz_min, float(os.environ.get("FACESWAP_XSEG_HZ", "3"))))
        self.max_stale = float(os.environ.get("FACESWAP_XSEG_STALE_S", "1.5"))
        self._pending = None           # 最新待推理 crop(只保留最后一张)
        self._mask = None              # 最新 256² 原始掩码
        self._mask_ts = 0.0
        self._lock = threading.Lock()
        self._event = threading.Event()
        threading.Thread(target=self._worker, daemon=True, name="xseg-refresh").start()
        print(f"[FaceSwap API] XSeg 遮挡掩码就绪(异步自适应 {self.hz_min:.0f}-{self.hz_max:.0f}Hz): "
              f"{Path(model_file).name} prov={self.session.get_providers()[:1]}")

    def tick(self):
        pass                            # 兼容旧调用点(序号缓存已由异步时间戳取代)

    def _infer(self, crop_bgr: np.ndarray) -> np.ndarray:
        x = cv2.resize(crop_bgr, (self.size, self.size))
        x = np.expand_dims(x.astype(np.float32) / 255.0, 0)          # NHWC BGR [0,1]
        m = self.session.run(None, {"input": x})[0][0]               # HxWx1
        return m[:, :, 0].clip(0, 1).astype(np.float32)

    def _worker(self):
        while True:
            self._event.wait()
            self._event.clear()
            with self._lock:
                crop = self._pending
                self._pending = None
            if crop is None:
                continue
            t0 = time.time()
            try:
                m = self._infer(crop)
                with self._lock:
                    prev = self._mask
                    self._mask = m
                    self._mask_ts = time.time()
                # 自适应：掩码变化率>2% 视为遮挡物在动 → 提频跟手；静止指数衰减降频省 CPU
                try:
                    if prev is not None and prev.shape == m.shape:
                        _delta = float(np.mean(np.abs(m - prev)))
                        self._hz = self.hz_max if _delta > 0.02 else max(self.hz_min, self._hz * 0.8)
                except Exception:
                    pass
            except Exception as e:
                print(f"[FaceSwap API] XSeg 后台推理失败(掩码保持旧值): {e}")
            # 限频：不足最小间隔则补睡，占空比恒有界
            dt = 1.0 / self._hz - (time.time() - t0)
            if dt > 0:
                time.sleep(dt)

    def mask_for_crop(self, crop_bgr: np.ndarray, cache_ok: bool = False) -> np.ndarray:
        if not cache_ok:                       # 多脸帧：异步缓存无法按脸区分 → 不遮挡(全1)
            return np.ones(crop_bgr.shape[:2], dtype=np.float32)
        with self._lock:
            self._pending = crop_bgr           # 非阻塞投递(只留最新)
            m = self._mask
            fresh = (time.time() - self._mask_ts) <= self.max_stale
        self._event.set()
        if m is None or not fresh:
            return np.ones(crop_bgr.shape[:2], dtype=np.float32)
        m = cv2.resize(m, (crop_bgr.shape[1], crop_bgr.shape[0]))
        return (cv2.GaussianBlur(m.clip(0, 1), (0, 0), 5).clip(0.5, 1) - 0.5) * 2


class _OnnxFaceRestorer:
    """GPEN-BFR-256 ONNX 精修器（FaceFusion face_enhancer 同款前后处理）。
    对齐: arcface_128 模板 × 输入尺寸；输入 RGB [-1,1] CHW；贴回=羽化 box 掩码(可乘遮挡)。
    非破坏式：enhance() 返回新帧，输入不变(供 deltaHP 保身份重组用 raw)。"""
    def __init__(self, model_file: str):
        self.session = _aux_session(model_file)   # 限额小池会话(见 _aux_session 根因注释)
        shp = self.session.get_inputs()[0].shape
        self.size = int(shp[3]) if isinstance(shp[3], int) else 256
        self.template = _ARCFACE_128_TEMPLATE * self.size
        print(f"[FaceSwap API] GPEN 精修器就绪: {Path(model_file).name} size={self.size} "
              f"prov={self.session.get_providers()[:1]}")

    def enhance(self, img: np.ndarray, kps_list, occluder=None) -> np.ndarray:
        out = img.copy()
        S = self.size
        cache_ok = len(kps_list) == 1      # 单脸流才允许掩码时域缓存(多脸防串)
        for kps in kps_list:
            M = cv2.estimateAffinePartial2D(np.asarray(kps, dtype=np.float32), self.template,
                                            method=cv2.RANSAC, ransacReprojThreshold=100)[0]
            if M is None:
                continue
            crop = cv2.warpAffine(out, M, (S, S), borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_AREA)
            blob = ((crop[:, :, ::-1].astype(np.float32) / 255.0 - 0.5) / 0.5)
            blob = np.expand_dims(blob.transpose(2, 0, 1), 0).astype(np.float32)
            pred = self.session.run(None, {"input": blob})[0][0]
            pred = np.clip(pred, -1, 1)
            restored = (((pred + 1) / 2).transpose(1, 2, 0) * 255).round().astype(np.uint8)[:, :, ::-1]
            mask = _box_feather_mask((S, S))
            if occluder is not None:
                try:
                    mask = np.minimum(mask, occluder.mask_for_crop(crop, cache_ok=cache_ok))
                except Exception:
                    pass
            out = _paste_crop_back(out, restored, mask, M)
        return out


_gpen = None
_gpen_state = 0        # 0=未尝试 1=就绪 -1=加载失败(不再重试)
_xseg = None
_xseg_state = 0
_aux_onnx_lock = threading.Lock()


def _get_gpen():
    global _gpen, _gpen_state
    if _gpen_state < 0:
        return None
    if _gpen is None:
        with _aux_onnx_lock:
            if _gpen is None and _gpen_state >= 0:
                try:
                    if not Path(_GPEN_MODEL).is_file():
                        raise FileNotFoundError(_GPEN_MODEL)
                    _gpen = _OnnxFaceRestorer(_GPEN_MODEL)
                    _gpen_state = 1
                except Exception as e:
                    _gpen_state = -1
                    print(f"[FaceSwap API] GPEN 加载失败(enhance=gpen 将静默跳过): {e}")
    return _gpen


def _get_xseg():
    global _xseg, _xseg_state
    if _xseg_state < 0:
        return None
    if _xseg is None:
        with _aux_onnx_lock:
            if _xseg is None and _xseg_state >= 0:
                try:
                    if not Path(_XSEG_MODEL).is_file():
                        raise FileNotFoundError(_XSEG_MODEL)
                    _xseg = _XSegOccluder(_XSEG_MODEL)
                    _xseg_state = 1
                except Exception as e:
                    _xseg_state = -1
                    print(f"[FaceSwap API] XSeg 加载失败(遮挡掩码将停用): {e}")
    return _xseg


def _mouth_protect_mask(S: int, M: np.ndarray, lmk106) -> "np.ndarray | None":
    """口型保护掩码(值域[0,1]，1=嘴区=保留原像素)：106 点唇部(52-71)凸包→质心外扩 1.18→
    羽化(σ=唇宽×0.12)。凸包免疫索引排序歧义(insightface 106 唇区排序无权威文档,2026-07-10
    实证 52-71 覆盖内外唇)；张嘴时点位跟唇动,掩码自适应。返回 None=点位缺失(不保护)。"""
    try:
        if lmk106 is None or len(lmk106) < 72:
            return None
        pts = np.asarray(lmk106[52:72], dtype=np.float32)
        pts_c = pts @ M[:, :2].T + M[:, 2]              # 帧坐标 → 对齐 crop 坐标
        hull = cv2.convexHull(pts_c.astype(np.float32))
        c = hull.reshape(-1, 2).mean(axis=0)
        hull_ex = (hull.reshape(-1, 2) - c) * 1.18 + c  # 质心外扩,给唇缘留保护边
        m = np.zeros((S, S), dtype=np.float32)
        cv2.fillConvexPoly(m, hull_ex.round().astype(np.int32), 1.0)
        if m.max() <= 0:                                # 嘴区完全在 crop 外(极端侧脸)
            return None
        w = float(np.linalg.norm(pts_c[:, 0].max() - pts_c[:, 0].min()))
        m = cv2.GaussianBlur(m, (0, 0), max(2.0, w * 0.12))
        return m.clip(0.0, 1.0)
    except Exception:
        return None


def _paste_swapped_feather(frame: np.ndarray, fake_crop: np.ndarray, M: np.ndarray,
                           occluder=None, occl_cache_ok: bool = False,
                           mouth_lmk=None, mask_padding=None, model_mask=None) -> np.ndarray:
    """换脸结果的羽化贴回（blend_mode=feather 的核心）：box 羽化掩码(可 padding 内缩，
    光头×有发源脸的黑边根治) ×(可选)模型脸形掩码(HyperSwap 灰边根治,2026-07-11)
    ×(可选)XSeg 遮挡掩码 ×(可选)口型保护(1-嘴区掩码)。
    与 INSwapper 内置贴回的差异：不做差分阈值 mask(直播中差分在光照突变时不稳)，
    边缘口径=FaceFusion 标准 box blur 0.3。返回新帧(不改 frame 本体)。"""
    S = int(fake_crop.shape[0])
    mask = _box_feather_mask((S, S), padding=mask_padding)
    if model_mask is not None:
        if model_mask.shape[:2] != (S, S):
            model_mask = cv2.resize(model_mask, (S, S))
        mask = np.minimum(mask, model_mask)
    if occluder is not None:
        try:
            tgt_crop = cv2.warpAffine(frame, M, (S, S), borderMode=cv2.BORDER_REPLICATE)
            mask = np.minimum(mask, occluder.mask_for_crop(tgt_crop, cache_ok=occl_cache_ok))
        except Exception as e:
            print(f"[FaceSwap API] XSeg 掩码失败(本帧退纯羽化): {e}")
    if mouth_lmk is not None:
        mm = _mouth_protect_mask(S, M, mouth_lmk)
        if mm is not None:
            mask = mask * (1.0 - mm)
    return _paste_crop_back(frame.copy(), fake_crop, mask, M)


import contextlib as _ctxlib


def _autocast_ctx(force: bool = False):
    """开着 autocast(或 force)且在 CUDA 上→混合精度上下文；否则空上下文(CPU 上 autocast 反而慢)。"""
    if (ENH_AUTOCAST or force) and torch.cuda.is_available():
        return torch.autocast("cuda", dtype=torch.float16)
    return _ctxlib.nullcontext()


def _gfpgan_enhance_fast(img: np.ndarray, kps_list, fh=None) -> np.ndarray:
    """GFPGAN 增强·复用换脸阶段的 5 点关键点：
    等价于 GFPGANer.enhance(has_aligned=False)，但跳过其内部 RetinaFace 全图二次检测
    (facexlib 对齐只吃 all_landmarks_5,坐标系=换脸结果全帧,与 insightface kps 一致同序)。
    仅增强【真被换过的脸】(kps_list 来自换脸循环),路人脸不动——比旧路径更合理。
    ENH_HALF 开→GFPGAN 权重已整体 fp16,喂 half 输入做纯半精度前向(免 autocast 逐算子 cast)。
    fh=增强池分配的独占 FaceRestoreHelper(并发用)；None 时回退 GFPGANer 内置 helper(单发/旧行为)。
    任何异常由调用方回退旧 enhance()。"""
    fh = fh if fh is not None else face_enhancer.face_helper
    fh.clean_all()
    fh.read_image(img)
    fh.all_landmarks_5 = [np.asarray(k, dtype=np.float32) for k in kps_list]
    fh.align_warp_face()
    with torch.no_grad():
        for cropped_face in fh.cropped_faces:
            face_t = img2tensor(cropped_face / 255., bgr2rgb=True, float32=True)
            _tv_normalize(face_t, (0.5, 0.5, 0.5), (0.5, 0.5, 0.5), inplace=True)
            face_t = face_t.unsqueeze(0).to(face_enhancer.device)
            if ENH_HALF:
                out = face_enhancer.gfpgan(face_t.half(), return_rgb=False, weight=0.5)[0]
            else:
                with _autocast_ctx():
                    out = face_enhancer.gfpgan(face_t, return_rgb=False, weight=0.5)[0]
            restored = tensor2img(out.float().squeeze(0), rgb2bgr=True, min_max=(-1, 1))
            fh.add_restored_face(restored.astype("uint8"))
        fh.get_inverse_affine(None)
        with _autocast_ctx():                 # use_parse=True: 贴回内部还有一只 parsing 网(保持 fp32 权重)
            restored_img = fh.paste_faces_to_input_image()
    return restored_img if restored_img is not None else img

def temporal_smooth(current: np.ndarray, faces=None) -> np.ndarray:
    """运动自适应平滑：静止时平滑，头部移动时直接输出当前帧"""
    global _prev_result, _prev_face_center

    # 检测人脸中心运动幅度
    motion_large = False
    if faces and len(faces) > 0:
        bbox = faces[0].bbox.astype(int)
        cx = int((bbox[0] + bbox[2]) / 2)
        cy = int((bbox[1] + bbox[3]) / 2)
        if _prev_face_center is not None:
            dx = abs(cx - _prev_face_center[0])
            dy = abs(cy - _prev_face_center[1])
            if dx > MOTION_THRESHOLD or dy > MOTION_THRESHOLD:
                motion_large = True
                _prev_result = None  # 重置，防止幻影
        _prev_face_center = (cx, cy)

    alpha = PARAMS["smooth_alpha"]
    if motion_large or alpha >= 0.99:
        # 运动中：直接输出，不混合
        _prev_result = current.copy()
        return current

    if _prev_result is None or _prev_result.shape != current.shape:
        _prev_result = current.copy()
        return current

    blended = cv2.addWeighted(current, alpha, _prev_result, 1.0 - alpha, 0)
    _prev_result = blended.copy()
    return blended

# 光流平滑用：上一帧灰度（估计帧间运动）
_prev_flow_gray: np.ndarray = None

def temporal_smooth_flow(current: np.ndarray, faces=None) -> np.ndarray:
    """光流时序平滑（Farneback，Phase 8-2）：先用稠密光流估计「当前帧→上一帧」的运动，
    把上一帧结果 warp 对齐到当前帧，再与当前帧混合。相比运动自适应版（运动时只能直出→闪烁回归），
    本法**运动中也能平滑**且不产生拖影/幻影——直接压住直播逐帧抖动。纯 cv2，零模型下载。

    选 Farneback 而非 RAFT：RAFT 精度更高但需下模型+逐帧 GPU 推理(慢)，而本场景要实时+零下载，
    经典稠密光流(CPU,~几 ms/帧)足以做帧间运动补偿，是该约束下的最优解（路线图列「RAFT/Farneback」）。"""
    global _prev_result, _prev_flow_gray
    alpha = PARAMS["smooth_alpha"]
    cur_gray = cv2.cvtColor(current, cv2.COLOR_BGR2GRAY)
    if (_prev_result is None or _prev_flow_gray is None
            or _prev_result.shape != current.shape
            or _prev_flow_gray.shape != cur_gray.shape
            or alpha >= 0.99):
        _prev_result = current.copy()
        _prev_flow_gray = cur_gray
        return current
    try:
        # flow(cur→prev)[y,x]=(dx,dy)：当前像素(x,y)在上一帧的位置偏移 → 据此采样上一帧=运动对齐
        flow = cv2.calcOpticalFlowFarneback(
            cur_gray, _prev_flow_gray, None,
            0.5, 3, 15, 3, 5, 1.2, 0)
        h, w = cur_gray.shape
        grid_x, grid_y = np.meshgrid(np.arange(w), np.arange(h))
        map_x = (grid_x + flow[..., 0]).astype(np.float32)
        map_y = (grid_y + flow[..., 1]).astype(np.float32)
        warped_prev = cv2.remap(_prev_result, map_x, map_y,
                                interpolation=cv2.INTER_LINEAR,
                                borderMode=cv2.BORDER_REPLICATE)
        blended = cv2.addWeighted(current, alpha, warped_prev, 1.0 - alpha, 0)
    except Exception as e:
        print(f"[光流平滑] 失败，回退直出: {e}")
        blended = current
    _prev_result = blended.copy()
    _prev_flow_gray = cur_gray
    return blended

def color_correction(source: np.ndarray, target: np.ndarray) -> np.ndarray:
    """将 target 的整体色调校正到与 source 接近，消除换脸色差"""
    try:
        src_lab = cv2.cvtColor(source.astype(np.float32) / 255.0, cv2.COLOR_BGR2Lab)
        tgt_lab = cv2.cvtColor(target.astype(np.float32) / 255.0, cv2.COLOR_BGR2Lab)

        # 计算均值和标准差
        src_mean, src_std = src_lab.mean(axis=(0,1)), src_lab.std(axis=(0,1))
        tgt_mean, tgt_std = tgt_lab.mean(axis=(0,1)), tgt_lab.std(axis=(0,1))

        # 只校正亮度和色度（L/a/b），保留换脸图细节
        corrected = tgt_lab.copy()
        for i in range(3):
            if tgt_std[i] > 1e-6:
                corrected[:,:,i] = (tgt_lab[:,:,i] - tgt_mean[i]) * (src_std[i] / tgt_std[i]) + src_mean[i]

        corrected = np.clip(corrected, 0, 100 if corrected.shape[2] == 1 else None)
        result = cv2.cvtColor(corrected.astype(np.float32), cv2.COLOR_Lab2BGR)
        result = np.clip(result * 255.0, 0, 255).astype(np.uint8)
        return result
    except Exception as e:
        print(f"[色彩校正] 失败（跳过）: {e}")
        return target

# ── 实时可调参数 ──────────────────────────────────────────────
def _env_mask_padding() -> list:
    """FACESWAP_MASK_PADDING="上,右,下,左"(百分比) → 默认掩码内缩；缺省/畸形 → 全 0(零回归)。"""
    raw = os.environ.get("FACESWAP_MASK_PADDING", "").strip()
    if not raw:
        return [0.0, 0.0, 0.0, 0.0]
    try:
        vals = [max(0.0, min(40.0, float(x))) for x in raw.split(",")[:4]]
        return (vals + [0.0] * 4)[:4]
    except Exception:
        print(f"[FaceSwap API] FACESWAP_MASK_PADDING 格式无效(需'上,右,下,左'): {raw!r} → 忽略")
        return [0.0, 0.0, 0.0, 0.0]


PARAMS = {
    "smooth_alpha":      0.6,   # 时序平滑强度 0~1
    # 时序平滑模式：motion(默认,运动自适应:静止混合/运动直出，零回归) /
    #   flow(光流补偿:Farneback warp 上一帧后再混合，运动中也平滑、无拖影—Phase8-2 消闪烁) / off(关)。
    #   可经 FACESWAP_SMOOTH_MODE 环境变量设默认，或 /params、单次请求 smooth_mode 覆盖。
    "smooth_mode":       os.environ.get("FACESWAP_SMOOTH_MODE", "motion"),
    "codeformer_w":      0.9,   # CodeFormer 保真度 0~1（2026-07-07 A/B：w0.9 比 w0.7 身份+0.04,清晰度更高,零成本）
    "enable_poisson":    True,  # Poisson 无缝融合
    "enable_color_corr": True,  # 肤色校正
    "enable_codeformer": True,  # CodeFormer 增强
    "enable_gfpgan":     True,  # GFPGAN 回退增强
    "jpeg_quality":      90,    # 输出图片质量 60~100
    # ── 增强·辨识度保护（2026-07-07 循证；见 _enh_deltahp/_enh_res_sweep）──
    #   实测：任何生成式增强都会把源明星身份"均值化"回退 ~0.03(cos)。两条对策：
    "enh_id_preserve":   bool(int(os.environ.get("FACESWAP_ENH_ID_PRESERVE", "1"))),
    #     ↑ deltaHP：增强后只取"改动量的高频"叠回身份正确的原换脸图(raw+k·highpass(enh-raw))。
    #       身份从 0.881→0.898(回收一半损失)、清晰度持平 gfpgan、削波/光晕反降(3.4%→2.0%)、背景零改动。
    "enh_id_hp_k":       float(os.environ.get("FACESWAP_ENH_ID_HP_K", "1.0")),   # 高频增益(1.0=清晰持平gfpgan;越大越锐但身份回落)
    "enh_id_hp_sigma":   float(os.environ.get("FACESWAP_ENH_ID_HP_SIGMA", "2.0")),
    #     ↓ 分辨率门控：脸够大时(GFPGAN 512 工作分辨率以下缩放)增强变"平滑"，身份清晰双亏→直接跳过。
    #       实测交叉点在 face 360~512px 间；≥450 时 raw 的身份(0.871)与清晰度(108)双胜 gfpgan(0.821/86)。
    "enh_gate_face_px":  int(os.environ.get("FACESWAP_ENH_GATE_FACE_PX", "450")),  # 0=关闭门控(永远增强)
    # ── 身份放大（2026-07-07 辨识度杠杆；沿远离人群 anchor 外推源 embedding）──
    #   β=0 关闭(零回归)。广谱实测(10 明星两两)：β=0.2 互相可区分度 cross 0.042→~0(拉开约一倍)、
    #   各自像本人仅回落 0.01(视觉无感、无畸变)——取曲线拐点作默认；anchor 未就绪时自动跳过(安全)。
    #   需 Hub 推送人群 anchor(全体角色脸均值)；引擎侧已落盘，重启自动载入。
    "id_amplify":        float(os.environ.get("FACESWAP_ID_AMPLIFY", "0.2")),
    "id_amplify_max":    float(os.environ.get("FACESWAP_ID_AMPLIFY_MAX", "0.6")),   # β 上限防畸变
    # ── DFM 每角色模型（辨识度终极方案；FACESWAP_MODEL=<x.dfm> 或按角色下发）──
    "dfm_morph":         float(os.environ.get("FACESWAP_DFM_MORPH", "0.75")),  # 形态可调模型的融合强度
    "dfm_color_match":   bool(int(os.environ.get("FACESWAP_DFM_COLOR_MATCH", "1"))),  # LAB 色迁移消色偏
    # ── 2026-07-09 贴回/遮挡（直播白斑与破脸根治）──────────────────────────
    #   blend_mode: poisson=旧行为(seamlessClone,默认零回归) / feather=羽化 box 掩码自定义贴回
    #     (跳过 Poisson——它会把周边亮度渗进脸区,绿幕强光下发白;羽化贴回 ~2ms 且不动脸区亮度)。
    #   enable_occlusion: XSeg 遮挡掩码(话筒/手挡脸不糊贴)。开启后自动走自定义贴回(掩码要乘进贴回)。
    "blend_mode":        os.environ.get("FACESWAP_BLEND_MODE", "poisson").strip().lower(),
    "enable_occlusion":  bool(int(os.environ.get("FACESWAP_OCCLUSION", "0"))),
    #   mouth_mask: 口型区保护(说话嘴型/牙齿100%真实)。默认关=零回归；直播链路随帧下发。
    "mouth_mask":        bool(int(os.environ.get("FACESWAP_MOUTH_MASK", "0"))),
    #   mask_padding: 贴回掩码内缩 [上,右,下,左]%(0~40)。光头主播 × 有发源脸的「头两侧
    #     发黑边」根治开关：掩码边缘按百分比扣掉,源发不进贴回区。默认全 0=零回归；
    #     环境变量 FACESWAP_MASK_PADDING="上,右,下,左"(如 "8,12,0,12") 设持久默认，
    #     /params 热调、单请求 mask_padding 逐帧覆盖。
    "mask_padding":      _env_mask_padding(),
}

# mask_padding 重启恢复：/params 热设的值落盘于 faceswap_active.json(与激活脸/妆容同机制)，
# 看门狗自愈重启后光头修缮不悄悄失效。显式环境变量优先(部署级配置 > 上次热设)。
if not os.environ.get("FACESWAP_MASK_PADDING", "").strip():
    try:
        _mp_saved = _active_state_load().get("mask_padding")
        if _mp_saved:
            _v = [max(0.0, min(40.0, float(x))) for x in list(_mp_saved)[:4]]
            PARAMS["mask_padding"] = (_v + [0.0] * 4)[:4]
    except Exception:
        pass

@app.get("/")
def root():
    return {"service": "faceswap-api", "version": "v2",
            "ui": "http://127.0.0.1:8000/ui"}

@app.get("/health")
def health():
    # 实际生效的 providers(真相)：区别于“请求的”providers —— ORT 可能因某 EP 加载失败而静默回退。
    # swap_cpu_only=True 是红旗：换脸掉到 CPU(~个位数 fps)，监控/自检应据此告警。
    try:
        _active = list(face_swapper.session.get_providers()) if face_swapper is not None else []
    except Exception:
        _active = []
    _cpu_only = bool(_active) and all("CPU" in p for p in _active)
    return {"status": "ok",
            "face_analyser": face_analyser is not None,
            "face_swapper":  face_swapper is not None,
            "swap_model":    _swap_model_name,
            "codeformer":    codeformer_net is not None,
            "gfpgan":        face_enhancer is not None,
            "enh_concurrency": _ENH_CONCURRENCY,
            "enh_pool":      {"gf": _gf_pool is not None, "cf": _cf_pool is not None},
            "execution_backend": _backend_label,
            "providers": providers[:3],                # 请求的 provider 优先级(非最终)
            "swap_providers_active": _active,          # 实际生效(真相)
            "swap_cpu_only": _cpu_only,                # True=换脸在 CPU(实时不可用)→排查 GPU EP
            "smooth_mode": PARAMS.get("smooth_mode", "motion"),
            # 2026-07-09 直播链路升级件观测：gpen/xseg 状态(0=未加载 1=就绪 -1=失败)与贴回模式
            "gpen": {"state": _gpen_state, "model": Path(_GPEN_MODEL).name if Path(_GPEN_MODEL).is_file() else None},
            "xseg": {"state": _xseg_state, "model": Path(_XSEG_MODEL).name if Path(_XSEG_MODEL).is_file() else None},
            "blend_mode": PARAMS.get("blend_mode"),
            "occlusion": PARAMS.get("enable_occlusion"),
            "mouth_mask": PARAMS.get("mouth_mask"),       # 口型区保护默认(请求级可覆盖)
            "mask_padding": PARAMS.get("mask_padding"),   # 掩码内缩默认[上,右,下,左]%(光头黑边)
            "active_face": _active_face_name,             # 直连通道源脸真相(Hub 激活时推送)
            "active_makeup": bool(_active_makeup),        # 直连通道粘滞妆容(空=无妆)
            "active_face_map": len(_active_face_map),     # 直连通道粘滞双人/多人槽位数(0=未启用)
            "face_map_max_slots": FACE_MAP_MAX_SLOTS,     # 槽位上限(多人换脸容量)
            "avg_src_keys": sorted(_named_embeddings.keys()),   # 已注册多照平均源脸(辨识度增强)
            "enh_id_preserve": PARAMS.get("enh_id_preserve"),   # deltaHP 保身份增强(辨识度)
            "enh_gate_face_px": PARAMS.get("enh_gate_face_px"), # 大脸跳过增强门控阈值(px,0=关)
            "id_amplify": PARAMS.get("id_amplify"),             # 身份放大 β(0=关)
            "id_anchor_set": _id_anchor is not None,            # 人群 anchor 是否就绪(放大前置)
            "enh_half": ENH_HALF,                     # True 是红旗：fp16 权重会导致 GFPGAN 静默直通

            "trt_enabled": "TensorrtExecutionProvider" in _active,   # 以真实生效为准
            "trt_requested": USE_TRT,
            "trt_available": _TRT_AVAILABLE,
            "trt_fp16": TRT_FP16 if _backend_label == "tensorrt" else None,
            "trt_det": TRT_DET if _backend_label == "tensorrt" else None,
            "trt_cache": TRT_CACHE_DIR if _backend_label == "tensorrt" else None}


@app.get("/healthz")
def healthz():
    return {"ok": True, "service": "faceswap"}

@app.get("/meminfo")
def meminfo():
    info = {"service": "faceswap"}
    try:
        import psutil, os as _os
        mi = psutil.Process(_os.getpid()).memory_info()
        info["rss_mb"] = round(mi.rss / 1048576, 1)
        info["vms_mb"] = round(getattr(mi, "vms", 0) / 1048576, 1)
    except Exception:
        pass
    try:
        import torch as _t
        if _t.cuda.is_available():
            info["gpu_alloc_mb"] = round(_t.cuda.memory_allocated() / 1048576, 1)
            info["gpu_reserved_mb"] = round(_t.cuda.memory_reserved() / 1048576, 1)
    except Exception:
        pass
    return info

@app.post("/gc")
def gc_endpoint():
    """非侵入式回收：gc + 释放显存缓存，不卸载模型。供看门狗优先调用以避免重启打断业务。"""
    import gc as _gc
    before = None
    try:
        import torch as _t
        if _t.cuda.is_available():
            before = _t.cuda.memory_reserved()
    except Exception:
        before = None
    n = _gc.collect()
    freed_mb = None
    try:
        import torch as _t
        if _t.cuda.is_available():
            _t.cuda.empty_cache()
            _t.cuda.ipc_collect()
            if before is not None:
                freed_mb = round((before - _t.cuda.memory_reserved()) / 1048576, 1)
    except Exception:
        pass
    return {"ok": True, "gc_objects": n, "gpu_reserved_freed_mb": freed_mb}

@app.get("/model")
def get_model():
    """当前换脸网 + 已缓存模型 + 库内可切换的 DFM 数量（供 Hub/lab 观测与选择）。"""
    _is_dfm = isinstance(face_swapper, DFMSwap)
    with _model_lock:
        cached = [Path(k).name for k in _model_cache]
    ent = _registry_entry(_swap_model_name) if _is_dfm else None
    return {"ok": True,
            "active": _swap_model_name,
            "active_path": _swap_model_path,
            "active_cn": ent.get("cn") if ent else None,
            "is_dfm": _is_dfm,
            "morph": getattr(face_swapper, "morph", False) if _is_dfm else False,
            "live_ok": ent.get("live_ok") if ent else (not _is_dfm),
            "gpu_swap_ms": ent.get("gpu_swap_ms") if ent else None,
            "lru_cap": _MODEL_LRU_CAP,
            "cached": cached,
            "dfm_library": len(_dfm_lib_index())}


@app.post("/model/reload")
def reload_model(data: dict):
    """运行时热切换换脸网（S2 核心）：{model:'<xxx.dfm>|default|<abs path>'}。
    inswapper 基线常驻，切回 default 即恢复通用换脸（配源脸/多照平均）。
    切到 .dfm → 该角色「身份即模型」，/faceswap 自动旁路源脸检测（DFM 忽略源脸）。"""
    global face_swapper, _swap_model_path, _swap_model_name
    name = (data.get("model") or "").strip()
    try:
        path = _resolve_swap_model(name)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))
    t0 = time.time()
    try:
        m = _get_or_load_swapper(path, warm=True)     # 热切同步预热，直播换角色首帧不吃冷启
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"模型加载失败: {str(e)[:160]}")
    with _model_lock:
        face_swapper = m                              # 原子换绑（在途请求已快照旧引用，安全）
        _swap_model_path = path
        _swap_model_name = Path(path).stem
    _prov = []
    try:
        _prov = list(m.session.get_providers())[:2]
    except Exception:
        pass
    ent = _registry_entry(_swap_model_name) if isinstance(m, DFMSwap) else None
    live_ok = ent.get("live_ok") if ent else (not isinstance(m, DFMSwap))
    resp = {"ok": True, "active": _swap_model_name, "is_dfm": isinstance(m, DFMSwap),
            "morph": getattr(m, "morph", False), "load_ms": int((time.time()-t0)*1000),
            "providers": _prov, "live_ok": live_ok,
            "gpu_swap_ms": ent.get("gpu_swap_ms") if ent else None,
            "cn": ent.get("cn") if ent else None}
    # 直播档绑了「仅离线高清」模型（CPU 回退数百 ms）→ 明确回警，调用方(Hub)可拦或降级到通用换脸。
    if isinstance(m, DFMSwap) and ent and ent.get("live_ok") is False:
        resp["warn"] = f"该角色 GPU 换脸 ~{ent.get('gpu_swap_ms')}ms，实时直播会卡；建议仅离线出图或改用实时档角色。"
    return resp


@app.post("/model/prewarm")
def prewarm_model(data: dict):
    """把某角色 .dfm 预加载进 LRU 并做 cuDNN autotune，但**不改当前激活模型**（不动直播画面）。
    循证依据(.104/4070 实测)：热切到未缓存 DFM 阻塞 ~4.5s 且会卡住并发帧 ~2.8s(直播可见冻结)；
    命中 LRU 的角色热切仅 ~16ms(无感)。故 Hub 在operator「选中/激活」某 DFM 角色的那一刻就调本接口
    后台预热——待真正上屏切换时该模型已常驻，/model/reload 秒切。幂等：已缓存直接秒回。
    与 /model/reload 的区别：reload 会持久换绑 face_swapper(改直播输出)；prewarm 只填缓存不换绑。"""
    name = (data.get("model") or "").strip()
    if not name or name.lower() in ("default", "inswapper", "inswapper_128", "base"):
        return {"ok": True, "prewarmed": "inswapper_128", "already": True, "skip": "基线常驻，无需预热"}
    try:
        path = _resolve_swap_model(name)
    except Exception as e:
        raise HTTPException(status_code=404, detail=str(e))
    ap = os.path.normcase(os.path.abspath(path))
    with _model_lock:
        already = ap in _model_cache
    t0 = time.time()
    try:
        _get_or_load_swapper(path, warm=not already)   # 已在缓存→仅 move_to_end(秒回)；未命中→锁外加载+预热
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"预热失败: {str(e)[:160]}")
    with _model_lock:
        cached = [Path(k).name for k in _model_cache]
    nm = Path(path).stem
    ent = _registry_entry(nm)
    return {"ok": True, "prewarmed": nm, "already": already,
            "load_ms": int((time.time() - t0) * 1000),
            "cached": cached, "lru_cap": _MODEL_LRU_CAP,
            "cn": ent.get("cn") if ent else None,
            "live_ok": ent.get("live_ok") if ent else True}


_REGISTRY_PATH = Path(rf"{_BASE}\dfm_workspace\dfm_registry.json")


def _registry_entry(model_name: str):
    """按文件名查注册表条目（中文名/合规/live_ok 等）。缺表/未收录→None。"""
    if not model_name or not _REGISTRY_PATH.exists():
        return None
    try:
        reg = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    key = Path(model_name).name.lower()
    for e in reg.get("entries", []):
        if e.get("file", "").lower() == key or Path(e.get("file", "")).stem.lower() == Path(model_name).stem.lower():
            return e
    return None


@app.get("/model/available")
def list_available_models(live_only: bool = False):
    """可热切换的角色模型清单（供 Hub 下拉/画廊选择）。以 dfm_registry.json 为准（含中文名/分类/
    合规分级/辨识度指标 + live_ok GPU 实时可用性）；缺注册表时回退直接扫库目录。
    blocked（政治/高风险）默认不返回。live_only=1：只返回 GPU 实时可用(≤60ms)的——直播绑定专用，
    挡掉那些 ONNX 导出含 CUDA 不支持算子、会 CPU 回退到数百 ms 的模型（离线画廊仍可用它们出高清图）。"""
    items, blocked = [], 0
    reg = None
    if _REGISTRY_PATH.exists():
        try:
            reg = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
        except Exception:
            reg = None
    if reg and reg.get("entries"):
        for e in reg["entries"]:
            ap = (Path(_BASE) / e["path"]) if e.get("path") else None
            if not ap or not ap.exists():
                continue
            if e.get("compliance") == "blocked":
                blocked += 1
                continue
            if live_only and not e.get("live_ok"):
                continue
            items.append({"model": e["file"], "cn": e.get("cn"), "category": e.get("category"),
                          "compliance": e.get("compliance"),   # S7: 透出合规级 → UI 标「需授权」(caution)
                          "res": e.get("res"), "self_id": e.get("self_id"),
                          "morphable": e.get("morphable", False), "verify_ok": e.get("verify_ok"),
                          "live_ok": e.get("live_ok"), "gpu_swap_ms": e.get("gpu_swap_ms")})
    else:
        for k, v in sorted(_dfm_lib_index().items()):
            items.append({"model": Path(v).name, "cn": None, "category": None})
    items.sort(key=lambda x: (x.get("category") or "z", -(x.get("self_id") or 0)))
    n_live = sum(1 for i in items if i.get("live_ok"))
    return {"ok": True, "n": len(items), "n_live": n_live, "blocked": blocked, "live_only": live_only,
            "default": "inswapper_128", "active": _swap_model_name, "models": items}


@app.get("/params")
def get_params():
    return PARAMS

@app.post("/params")
def set_params(data: dict):
    for k, v in data.items():
        if k in PARAMS:
            PARAMS[k] = v
    if "mask_padding" in data:               # 光头修缮值落盘(重启不丢，与激活脸同机制)
        _active_state_update(mask_padding=PARAMS.get("mask_padding"))
    return {"ok": True, "params": PARAMS}

@app.get("/faces")
def list_faces():
    reload_faces()
    return {"faces": list(_faces_cache.keys()), "active": _active_face_name}

@app.post("/faces/switch")
def switch_face(data: dict):
    global _active_face_name, _active_face_b64, _prev_result, _prev_flow_gray
    name = data.get("name","")
    img_b64 = (data.get("image") or "").strip()
    # 2026-07-09 直连正确性根基：Hub 激活推送历来带 image，但旧实现只认引擎 faces 目录里的
    # 同名文件(角色不在目录=404，激活脸悄悄没换)。JSON 通道每帧注入 source_image 掩盖了这一点；
    # /faceswap_raw 直连通道以"引擎激活脸"为源——推什么就得用什么。带图=热载并落盘(重启不丢)。
    if img_b64:
        raw = img_b64.split(",", 1)[-1] if "," in img_b64 else img_b64
        _safe = "".join(c if c.isalnum() or c in "_- " else "_" for c in name).strip() or "pushed_face"
        try:
            FACES_DIR.mkdir(parents=True, exist_ok=True)   # .104 等机器无 faces 目录(2026-07-09 实测)
            (FACES_DIR / f"{_safe}.jpg").write_bytes(base64.b64decode(raw))
        except Exception as e:
            print(f"[FaceSwap API] 激活脸落盘失败(内存热载仍生效): {e}")
        _faces_cache[name] = raw
        _active_face_name = name
        _active_face_b64  = raw
        _active_state_update(face=_safe)   # 重启恢复用落盘文件名(stem)，非推送名
    else:
        reload_faces()
        if name not in _faces_cache:
            raise HTTPException(status_code=404, detail=f"找不到: {name}")
        _active_face_name = name
        _active_face_b64  = _faces_cache[name]
        _active_state_update(face=name)
    _src_face_cache.clear()   # 键=b64 哈希，同名换图必须清，否则直连路径用旧 embedding
    _prev_result = None  # 切换脸时重置平滑缓存
    _prev_flow_gray = None  # 同时重置光流参考帧，防跨脸 warp 拖影
    return {"ok": True, "active": _active_face_name, "pushed": bool(img_b64)}


# ── C-5 直播妆容·引擎侧粘滞规范（2026-07-09 直连通道补件）────────────────────
#   JSON 通道妆容由 Hub 逐帧注入；/faceswap_raw 直连不过 Hub → 妆容会静默消失。
#   Hub 在激活角色时把 live_makeup 推到这里(开=规范,关=空)，直连帧未显式带 makeup 时用之。
#   显式 req.makeup 永远优先(调用方语义不变)；空 dict/None=无妆(零回归)。
_active_makeup: dict = dict(_active_state_load().get("makeup") or {})   # 重启恢复


@app.post("/makeup/active")
def set_active_makeup(data: dict):
    global _active_makeup
    mk = data.get("makeup")
    _active_makeup = dict(mk) if isinstance(mk, dict) and mk else {}
    _active_state_update(makeup=_active_makeup)
    return {"ok": True, "active_makeup": bool(_active_makeup)}


# ── C-2 双人/多人 face_map·引擎侧粘滞槽位（2026-07-10 直连通道补件）──────────────
#   source_map 历来由 Hub /faceswap 代理逐帧注入；/faceswap_raw 二进制直连不过 Hub →
#   双人档曾静默失效(直连只剩激活单脸+锁主脸，画面里永远只换一张脸)。与妆容同款粘滞态修法：
#   Hub 在保存映射/激活角色/引擎回切时把「槽位源脸 b64 列表」推到这里(关=推空清除)，
#   直连帧未显式带 source_map 时用之。显式 req.source_map 永远优先([]=显式关)；落盘重启恢复。
FACE_MAP_MAX_SLOTS = max(2, int(os.environ.get("FACE_MAP_MAX_SLOTS", "4")))
_active_face_map: list = [s for s in (_active_state_load().get("face_map") or [])
                          if isinstance(s, str)][:FACE_MAP_MAX_SLOTS]   # 重启恢复


@app.post("/face_map/active")
def set_active_face_map(data: dict):
    """Hub 双人/多人档粘滞推送：{enabled:bool, slots:[b64|"",...]}。
    enabled=False 或无槽 → 清除(直连回单脸行为)；空串槽=该槽回退引擎激活脸。"""
    global _active_face_map
    slots = [(s if isinstance(s, str) else "").strip()
             for s in (data.get("slots") or [])[:FACE_MAP_MAX_SLOTS]]   # 非法槽值按空槽处理,不移位
    _active_face_map = slots if (data.get("enabled", True) and slots) else []
    _active_state_update(face_map=_active_face_map)
    return {"ok": True, "active_face_map": bool(_active_face_map),
            "slots": [bool(s) for s in _active_face_map]}

@app.post("/faces/register_avg")
def register_avg_face(data: dict):
    """登记「命名平均源脸」：{key:'角色名', images:[b64,...]}。
    对多张照片求 ArcFace embedding 平均并缓存，之后直播链路每帧只需传 source_key=<key>，
    引擎用预存平均向量换脸（零逐帧检测、零额外传输）→ 辨识度增强的直播落地口子。
    images 为空/全无脸 → 清除该 key（回退单图行为）。"""
    key = (data.get("key") or "").strip()
    if not key:
        raise HTTPException(status_code=400, detail="需要 key")
    images = data.get("images") or []
    emb, n = _avg_embedding(images)
    if emb is None:
        _named_embeddings.pop(key, None)
        _save_named_embeddings()
        return {"ok": False, "key": key, "faces": 0, "detail": "未检出人脸，已清除该命名平均"}
    _named_embeddings[key] = emb
    _save_named_embeddings()
    return {"ok": True, "key": key, "faces": n, "registered": list(_named_embeddings.keys())}


@app.post("/faces/set_anchor")
def set_id_anchor(data: dict):
    """设置身份放大 anchor（人群平均脸）：{images:[b64,...]} 求平均 embedding 作 anchor；
    空列表/无脸 → 清除 anchor（放大自动停用，零回归）。由 Hub 汇总全体角色脸后推送。"""
    global _id_anchor
    images = data.get("images") or []
    emb, n = _avg_embedding(images)
    if emb is None:
        _id_anchor = None
        _save_id_anchor()
        return {"ok": False, "faces": 0, "detail": "未检出人脸，已清除 anchor"}
    _id_anchor = (emb / (np.linalg.norm(emb) + 1e-8)).astype(np.float32)
    _save_id_anchor()
    return {"ok": True, "faces": n, "anchor_set": True}

@app.post("/faces/upload")
async def upload_face(data: dict):
    """上传新明星脸：{name: '名字', image: 'base64'}"""
    from fastapi.responses import JSONResponse
    name  = data.get("name","").strip()
    img64 = data.get("image","")
    if not name or not img64:
        raise HTTPException(status_code=400, detail="需要 name 和 image")
    if "," in img64:
        img64 = img64.split(",",1)[1]
    img_bytes = base64.b64decode(img64)
    save_path = FACES_DIR / f"{name}.jpg"
    with open(save_path, 'wb') as f:
        f.write(img_bytes)
    reload_faces()
    return {"ok": True, "saved": str(save_path), "faces": list(_faces_cache.keys())}

# response_class=None 会让 openapi 生成 AssertionError→整站 /openapi.json 500
# （2026-07-08 阶段11 根因定位：路由本身能跑，只坏 schema）。给足 HTMLResponse。
from fastapi.responses import HTMLResponse as _HTMLResp


@app.get("/ui", response_class=_HTMLResp)
def control_ui():
    from fastapi.responses import HTMLResponse
    html = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>FaceSwap 控制面板</title>
<style>
  *{box-sizing:border-box}
  body{font-family:Arial,sans-serif;background:#1a1a2e;color:#eee;padding:20px;max-width:750px;margin:auto}
  h1{color:#e94560;text-align:center;margin-bottom:5px}
  .subtitle{text-align:center;color:#aaa;font-size:13px;margin-bottom:20px}
  .card{background:#16213e;border-radius:12px;padding:18px;margin:12px 0;box-shadow:0 4px 15px rgba(0,0,0,.3)}
  h3{color:#fff;background:#e94560;padding:7px 14px;border-radius:6px;margin:0 0 14px 0;font-size:14px}
  label{display:flex;justify-content:space-between;align-items:center;margin:10px 0;font-size:14px}
  span.val{color:#e94560;font-weight:bold;min-width:40px;text-align:right}
  input[type=range]{flex:1;margin:0 12px;accent-color:#e94560}
  input[type=checkbox]{width:18px;height:18px;accent-color:#e94560;cursor:pointer}
  .btn{background:#e94560;color:#fff;border:none;padding:11px 20px;border-radius:8px;
       cursor:pointer;font-size:14px;transition:.2s;font-weight:bold}
  .btn:hover{background:#c73652}
  .btn-full{width:100%;margin-top:8px}
  .btn-green{background:#2d7a2d}.btn-green:hover{background:#1e5c1e}
  .status{text-align:center;padding:8px;border-radius:6px;margin-top:10px;font-size:13px;display:none}
  .ok{background:#0d3b0d;color:#4caf50}.err{background:#3b0d0d;color:#f44336}
  /* 明星脸网格 */
  .face-grid{display:flex;flex-wrap:wrap;gap:10px;margin-top:10px}
  .face-card{background:#0f3460;border-radius:8px;padding:8px;text-align:center;
             cursor:pointer;border:2px solid transparent;transition:.2s;width:100px}
  .face-card:hover{border-color:#e94560}
  .face-card.active{border-color:#4caf50;background:#0d3b0d}
  .face-card img{width:80px;height:80px;object-fit:cover;border-radius:6px;display:block;margin:0 auto 5px}
  .face-card .fname{font-size:12px;word-break:break-all;color:#eee}
  .face-card .active-badge{color:#4caf50;font-size:11px;font-weight:bold}
  .upload-area{border:2px dashed #e94560;border-radius:8px;padding:20px;text-align:center;
               cursor:pointer;margin-top:10px;transition:.2s}
  .upload-area:hover{background:#0f3460}
  input[type=text]{background:#0f3460;color:#eee;border:1px solid #e94560;border-radius:6px;
                   padding:7px 10px;width:100%;font-size:14px;margin-top:6px}
</style>
</head>
<body>
<h1>🎭 FaceSwap 控制面板</h1>
<div class="subtitle">参数实时生效，无需重启</div>

<!-- 快速入口（实验室：离线 API，未接入实时直播链） -->
<div class="card" style="padding:12px">
  <h3>🔗 实验室功能 <span style="font-size:11px;background:#3d2e00;color:#fbbf24;padding:2px 8px;border-radius:999px;margin-left:6px">🧪 离线 · 非实时</span></h3>
  <p style="font-size:12px;color:#aaa;margin:0 0 10px 0">单次处理 5~30 秒，不会自动进入 OBS 直播画面。需先启动对应服务（发型 8001 / 试衣 8002）。</p>
  <div style="display:flex;gap:10px;flex-wrap:wrap">
    <a href="http://127.0.0.1:8001/ui" target="_blank" id="linkHair"
       style="flex:1;background:#0f3460;color:#eee;text-decoration:none;padding:12px;border-radius:8px;
              text-align:center;border:2px solid #e94560;font-size:14px;font-weight:bold;opacity:.85">
      💇 发型定妆<br><span style="font-size:11px;color:#aaa">HairFastGAN · 约 5~10s/次</span>
    </a>
    <a href="http://127.0.0.1:8002/ui" target="_blank" id="linkTryon"
       style="flex:1;background:#0f3460;color:#eee;text-decoration:none;padding:12px;border-radius:8px;
              text-align:center;border:2px solid #4caf50;font-size:14px;font-weight:bold;opacity:.85">
      👗 虚拟试衣<br><span style="font-size:11px;color:#aaa">IDM-VTON · 约 20~30s/次</span>
    </a>
  </div>
  <div id="labSvcHint" style="display:none;margin-top:8px;font-size:11px;color:#fbbf24"></div>
</div>
<script>
(async function(){
  async function ping(url){
    try{ const r=await fetch(url,{signal:AbortSignal.timeout(2500)}); return r.ok; }catch(e){ return false; }
  }
  const h=await ping('/health');  // faceswap 自身
  const hair=await ping('http://127.0.0.1:8001/health');
  const tryon=await ping('http://127.0.0.1:8002/health');
  const hints=[];
  if(!hair){ document.getElementById('linkHair').style.opacity='0.45';
    hints.push('发型服务(8001)未启动——在启动器勾选「发型」或运行 hair_api.py'); }
  if(!tryon){ document.getElementById('linkTryon').style.opacity='0.45';
    hints.push('试衣服务(8002)未启动——需 START_EXTRAS=1 或手动运行 tryon_api.py'); }
  if(hints.length){ const el=document.getElementById('labSvcHint'); el.style.display='block'; el.textContent='⚠ '+hints.join(' · '); }
})();
</script>

<!-- 换脸引擎/角色模型热切换（S2：DFM 每角色模型直播落地） -->
<div class="card">
  <h3>🎭 换脸引擎 / 角色模型 <span style="font-size:11px;background:#0d3b0d;color:#4caf50;padding:2px 8px;border-radius:999px;margin-left:6px">运行时热切换</span></h3>
  <p style="font-size:12px;color:#aaa;margin:0 0 10px 0">切「通用换脸」= inswapper 基线（配下方明星脸）；切某角色 = 该角色专属 DFM 整脸换（含骨相/轮廓，辨识度最强，自动忽略源脸）。切换即时生效于本实例，直播链路同步生效。</p>
  <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
    <select id="modelSel" style="flex:1;min-width:220px;background:#0f3460;color:#eee;border:1px solid #e94560;border-radius:6px;padding:8px">
      <option>加载中…</option>
    </select>
    <button class="btn" onclick="reloadModel()">切换</button>
  </div>
  <div id="modelNow" style="font-size:12px;color:#4caf50;margin-top:8px"></div>
  <div id="modelStatus" class="status"></div>
</div>

<!-- 明星脸切换 -->
<div class="card">
  <h3>👤 明星脸切换 <span style="font-size:11px;color:#aaa">（仅通用换脸档生效）</span></h3>
  <div class="face-grid" id="faceGrid">加载中...</div>
  <hr style="border-color:#0f3460;margin:14px 0">
  <h3 style="background:#0f3460">➕ 上传新明星脸</h3>
  <input type="text" id="newName" placeholder="输入名字（如：张曼玉）">
  <div class="upload-area" onclick="document.getElementById('fileInput').click()">
    📁 点击选择照片（JPG/PNG，需包含正脸）
    <input type="file" id="fileInput" accept="image/*" style="display:none" onchange="previewUpload(this)">
  </div>
  <div id="previewBox" style="display:none;margin-top:8px;text-align:center">
    <img id="previewImg" style="height:100px;border-radius:8px;border:2px solid #e94560">
  </div>
  <button class="btn btn-green btn-full" onclick="uploadFace()" style="margin-top:10px">✅ 上传并添加</button>
  <div id="uploadStatus" class="status"></div>
</div>

<!-- 画质参数 -->
<div class="card">
  <h3>🎨 画质增强</h3>
  <label>CodeFormer 保真度 (w) <span style="color:#aaa;font-size:12px">0=强增强 1=保留原貌</span>
    <input type="range" id="cf_w" min="0" max="1" step="0.05" value="0.7"
           oninput="document.getElementById('cf_w_v').textContent=this.value">
    <span class="val" id="cf_w_v">0.7</span>
  </label>
  <label>启用 CodeFormer <input type="checkbox" id="en_cf" checked></label>
  <label>启用 GFPGAN 回退 <input type="checkbox" id="en_gf" checked></label>
  <label>启用 Poisson 无缝融合 <input type="checkbox" id="en_ps" checked></label>
  <label>启用肤色校正 <input type="checkbox" id="en_cc" checked></label>
</div>

<div class="card">
  <h3>⚡ 稳定性 &amp; 质量</h3>
  <label>时序平滑 (α) <span style="color:#aaa;font-size:12px">越小越平滑，但动态幻影↑</span>
    <input type="range" id="sm_a" min="0" max="1" step="0.05" value="0.6"
           oninput="document.getElementById('sm_a_v').textContent=this.value">
    <span class="val" id="sm_a_v">0.6</span>
  </label>
  <label>JPEG 输出质量
    <input type="range" id="jq" min="60" max="100" step="5" value="90"
           oninput="document.getElementById('jq_v').textContent=this.value">
    <span class="val" id="jq_v">90</span>
  </label>
</div>

<div class="card">
  <h3>🦲 光头/发际修缮（掩码内缩）</h3>
  <p style="font-size:12px;color:#aaa;margin:0 0 10px 0">本人光头/短发而源脸有头发时，头两侧会糊上源脸的深色头发边。加大「两侧内缩」直到黑边消失（一般 8~14）；额头出现源发际线就加「顶部内缩」。0=关闭。</p>
  <label>两侧内缩 %
    <input type="range" id="mp_side" min="0" max="20" step="1" value="0"
           oninput="document.getElementById('mp_side_v').textContent=this.value">
    <span class="val" id="mp_side_v">0</span>
  </label>
  <label>顶部内缩 %
    <input type="range" id="mp_top" min="0" max="20" step="1" value="0"
           oninput="document.getElementById('mp_top_v').textContent=this.value">
    <span class="val" id="mp_top_v">0</span>
  </label>
</div>

<button class="btn btn-full" onclick="applyParams()">✅ 应用画质参数</button>
<div id="status" class="status"></div>

<script>
let uploadFile = null;

// ── 换脸引擎/角色模型热切换 ──
async function loadModels() {
  try {
    const [av, cur] = await Promise.all([
      fetch('/model/available').then(r=>r.json()),
      fetch('/model').then(r=>r.json())
    ]);
    const sel = document.getElementById('modelSel');
    let html = '<option value="default">🔄 通用换脸（inswapper 基线 · 配明星脸）</option>';
    const live = (av.models||[]).filter(m=>m.live_ok);
    const offline = (av.models||[]).filter(m=>!m.live_ok);
    const opt = m=>{
      const nm = m.cn || m.model.replace('.dfm','');
      const q = m.self_id!=null?` · 辨识${(m.self_id*100|0)}`:'';
      const mo = m.morphable?' · 可调形':'';
      const ms = m.gpu_swap_ms!=null?` · ${m.gpu_swap_ms}ms`:'';
      return `<option value="${m.model}">${nm} (${m.res||''}px${q}${mo}${ms})</option>`;
    };
    if(live.length){ html += `<optgroup label="⚡ 可直播（GPU 实时 ≤60ms，共 ${live.length}）">` + live.map(opt).join('') + '</optgroup>'; }
    if(offline.length){ html += `<optgroup label="🎬 仅离线高清（换脸数百ms，直播会卡，共 ${offline.length}）">` + offline.map(opt).join('') + '</optgroup>'; }
    sel.innerHTML = html;
    sel.value = cur.is_dfm ? (av.models.find(m=>m.model.replace('.dfm','')===cur.active)?.model || 'default') : 'default';
    const now = cur.is_dfm ? `当前：${cur.active}（DFM 整脸换${cur.morph?'·可调形':''}）` : `当前：通用换脸（${cur.active}）`;
    document.getElementById('modelNow').textContent = now + ` · 已缓存 ${cur.cached.length}/${cur.lru_cap} · 库存 ${cur.dfm_library} 角色`;
  } catch(e){ document.getElementById('modelNow').textContent = '模型信息加载失败: '+e; }
}

async function reloadModel() {
  const sel = document.getElementById('modelSel');
  const model = sel.value;
  const s = document.getElementById('modelStatus');
  s.className='status'; s.style.display='block'; s.textContent='切换中…（首次加载某角色需数秒，之后走缓存毫秒级）';
  try {
    const r = await fetch('/model/reload',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model})});
    const d = await r.json();
    if(!r.ok){ s.className='status err'; s.textContent='❌ '+(d.detail||'切换失败'); return; }
    if(d.warn){ s.className='status err'; s.textContent=`⚠ 已切到 ${d.cn||d.active} · 加载 ${d.load_ms}ms — ${d.warn}`; }
    else { s.className='status ok'; s.textContent=`✅ 已切到 ${d.cn||d.active}${d.is_dfm?'（DFM 整脸换·实时）':''} · 加载 ${d.load_ms}ms`; }
    loadModels();
  } catch(e){ s.className='status err'; s.textContent='❌ '+e; }
  setTimeout(()=>s.style.display='none',4000);
}

// ── 明星脸相关 ──
async function loadFaces() {
  const r = await fetch('/faces'); const d = await r.json();
  const grid = document.getElementById('faceGrid');
  if(!d.faces.length){grid.innerHTML='<span style="color:#aaa">faces 文件夹为空，请上传照片</span>';return;}
  grid.innerHTML = d.faces.map(name=>`
    <div class="face-card ${name===d.active?'active':''}" onclick="switchFace('${name}')" id="fc_${name.replace(/[^a-zA-Z0-9]/g,'_')}">
      <img src="/face_thumb?name=${encodeURIComponent(name)}" onerror="this.src='data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 width=%2280%22 height=%2280%22><rect fill=%22%230f3460%22 width=%2280%22 height=%2280%22/><text x=%2240%22 y=%2248%22 font-size=%2230%22 text-anchor=%22middle%22 fill=%22%23e94560%22>👤</text></svg>'">
      <div class="fname">${name}</div>
      ${name===d.active?'<div class="active-badge">✅ 当前</div>':''}
    </div>`).join('');
}

async function switchFace(name) {
  const r = await fetch('/faces/switch',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  const d = await r.json();
  if(d.ok) loadFaces();
}

function previewUpload(input) {
  uploadFile = input.files[0];
  if(!uploadFile) return;
  const reader = new FileReader();
  reader.onload = e=>{
    document.getElementById('previewImg').src = e.target.result;
    document.getElementById('previewBox').style.display='block';
  };
  reader.readAsDataURL(uploadFile);
}

async function uploadFace() {
  const name = document.getElementById('newName').value.trim();
  const s = document.getElementById('uploadStatus');
  if(!name){s.className='status err';s.textContent='请输入名字';s.style.display='block';return;}
  if(!uploadFile){s.className='status err';s.textContent='请选择图片';s.style.display='block';return;}
  const reader = new FileReader();
  reader.onload = async e=>{
    const b64 = e.target.result;
    const r = await fetch('/faces/upload',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({name,image:b64})});
    const d = await r.json();
    if(d.ok){
      s.className='status ok';s.textContent='✅ 上传成功！';s.style.display='block';
      document.getElementById('newName').value='';
      uploadFile=null; document.getElementById('previewBox').style.display='none';
      loadFaces();
    } else {
      s.className='status err';s.textContent='❌ 失败';s.style.display='block';
    }
    setTimeout(()=>s.style.display='none',3000);
  };
  reader.readAsDataURL(uploadFile);
}

// ── 画质参数 ──
async function applyParams() {
  const mpTop = parseFloat(document.getElementById('mp_top').value);
  const mpSide = parseFloat(document.getElementById('mp_side').value);
  const params = {
    smooth_alpha:parseFloat(document.getElementById('sm_a').value),
    codeformer_w:parseFloat(document.getElementById('cf_w').value),
    enable_poisson:document.getElementById('en_ps').checked,
    enable_color_corr:document.getElementById('en_cc').checked,
    enable_codeformer:document.getElementById('en_cf').checked,
    enable_gfpgan:document.getElementById('en_gf').checked,
    jpeg_quality:parseInt(document.getElementById('jq').value),
    mask_padding:[mpTop, mpSide, 0, mpSide],
  };
  const s = document.getElementById('status');
  const r = await fetch('/params',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(params)});
  const d = await r.json();
  s.className='status ok';s.textContent='✅ 参数已应用！';s.style.display='block';
  setTimeout(()=>s.style.display='none',3000);
}

// 启动加载
fetch('/params').then(r=>r.json()).then(p=>{
  document.getElementById('cf_w').value=p.codeformer_w;document.getElementById('cf_w_v').textContent=p.codeformer_w;
  document.getElementById('sm_a').value=p.smooth_alpha;document.getElementById('sm_a_v').textContent=p.smooth_alpha;
  document.getElementById('jq').value=p.jpeg_quality;document.getElementById('jq_v').textContent=p.jpeg_quality;
  document.getElementById('en_cf').checked=p.enable_codeformer;
  document.getElementById('en_gf').checked=p.enable_gfpgan;
  document.getElementById('en_ps').checked=p.enable_poisson;
  document.getElementById('en_cc').checked=p.enable_color_corr;
  const mp=p.mask_padding||[0,0,0,0];
  document.getElementById('mp_top').value=mp[0];document.getElementById('mp_top_v').textContent=mp[0];
  document.getElementById('mp_side').value=mp[1];document.getElementById('mp_side_v').textContent=mp[1];
});
loadFaces();
loadModels();
</script>
</body></html>"""
    return HTMLResponse(content=html)

@app.get("/face_thumb")
def face_thumb(name: str):
    from fastapi.responses import Response
    path = FACES_DIR / f"{name}.jpg"
    if not path.exists():
        for ext in ('.jpeg','.png','.webp'):
            p2 = FACES_DIR / f"{name}{ext}"
            if p2.exists(): path = p2; break
    if not path.exists():
        raise HTTPException(status_code=404)
    img = cv2.imread(str(path))
    img = cv2.resize(img, (80, 80))
    _, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return Response(content=buf.tobytes(), media_type="image/jpeg")

def _pick_main_face(faces: list, hint=None, hyst: float = 1.3):
    """仅换主脸的选脸规则（2026-07-06s：加大小滞回，防两人近等大时主脸身份闪切）。
    无 hint：最大脸即主脸（首帧/丢框后的"发现"语义，同 06r 旧行为）。
    有 hint（上一帧主脸中心 [cx,cy]，目标图坐标系）：离 hint 最近的脸为"在位者"，
    挑战者面积 ≥hyst× 在位者才换主——检测框逐帧抖动不再让换脸对象在两人间跳变；
    真正的"更大的人"（凑得更近/走到前排）仍会按倍数规则接管。
    刻意无跨请求状态：多客户端（生产流+压测+图片接口）共打同一引擎会互相污染，
    hint 由各客户端自带（realtime_stream 用上一帧回传的主脸框），天然隔离。"""
    def _area(tf):
        return float(max(0.0, tf.bbox[2] - tf.bbox[0]) * max(0.0, tf.bbox[3] - tf.bbox[1]))
    biggest = max(faces, key=_area)
    try:
        if hint is not None and len(hint) >= 2:
            hx, hy = float(hint[0]), float(hint[1])
            incumbent = min(faces, key=lambda tf: ((tf.bbox[0] + tf.bbox[2]) / 2.0 - hx) ** 2
                                                + ((tf.bbox[1] + tf.bbox[3]) / 2.0 - hy) ** 2)
            if _area(biggest) < hyst * _area(incumbent):
                return incumbent
    except Exception:
        pass                      # hint 畸形→回退最大脸，绝不因提示字段整帧失败
    return biggest


@app.post("/faceswap", response_model=SwapResponse)
def faceswap(req: SwapRequest):
    if face_analyser is None:
        raise HTTPException(status_code=503, detail="模型未加载")
    t0 = time.time()

    # S2：快照换脸网引用（贯穿本请求，隔离并发热切换）；带 model 覆盖则按需取该模型。
    swapper = face_swapper
    if req.model:
        try:
            swapper = _get_or_load_swapper(_resolve_swap_model(req.model))
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"模型加载失败: {str(e)[:160]}")
    if swapper is None:
        raise HTTPException(status_code=503, detail="模型未加载")
    _is_dfm_active = isinstance(swapper, DFMSwap)   # DFM=身份即模型，旁路源脸

    # 解码源脸（留空则用当前激活的明星脸）——DFM 忽略源脸，跳过该约束
    src_b64 = req.source_image if req.source_image else _active_face_b64
    # C-2 直连补件：未带 source_map 字段(None)时回退引擎粘滞槽位(Hub /face_map/active 推送)，
    # /faceswap_raw 直连帧因此也吃双人/多人档；显式 source_map(含 []=强制关)永远优先。
    _src_map = req.source_map if req.source_map is not None else (_active_face_map or None)
    _has_avg_src = bool(req.source_images) or bool(req.source_key and req.source_key in _named_embeddings)
    if not _is_dfm_active and not src_b64 and not _src_map and not _has_avg_src:
        raise HTTPException(status_code=400, detail="没有可用的源脸，请先放图片到 faces 文件夹")
    tgt_img = b64_to_img(req.target_image)
    if tgt_img is None:
        raise HTTPException(status_code=400, detail="图片解码失败")

    def _analyse_src(b64s: str):
        """源脸分析(带缓存)：直播链路源脸=同一照片逐帧重发，embedding 决定性可缓存。
        解码失败→400(与旧行为一致)；无脸→None(由调用方决定错误语义)。"""
        key = (len(b64s), hash(b64s)) if SRC_CACHE_ON else None
        cached = _src_face_cache.get(key) if key else None
        if cached is not None:
            return cached
        img = b64_to_img(b64s)
        if img is None:
            raise HTTPException(status_code=400, detail="图片解码失败")
        fs = face_analyser.get(img)
        if not fs:
            return None
        if key:
            if len(_src_face_cache) >= _SRC_CACHE_MAX:
                _src_face_cache.pop(next(iter(_src_face_cache)))
            _src_face_cache[key] = fs[0]
        return fs[0]

    # 检测人脸
    t_detect0 = time.time()
    map_faces = None                       # C-2: 槽位源脸列表(None=未启用 face_map)
    src_faces_n = 1                         # 参与身份的源照片数（多照片平均时>1，供 /health 观测）
    if _is_dfm_active:
        # DFM：身份来自模型本身，无需源脸——跳过全部源脸检测/平均/放大（每帧省一次 buffalo_l 前向）
        src_faces = [None]
    elif _src_map:
        map_faces = []
        for b in _src_map[:FACE_MAP_MAX_SLOTS]:   # ADR-12-02 扩容：至多 FACE_MAP_MAX_SLOTS 槽(默认4)
            f = None
            try:
                f = _analyse_src(b) if b else (_analyse_src(src_b64) if src_b64 else None)
            except Exception:
                f = None                   # 单槽图坏(解码/无脸)→按缺槽回退,不整帧失败(直播链路健壮性)
            map_faces.append(f)
        if not any(f is not None for f in map_faces):
            raise HTTPException(status_code=400, detail="face_map 源图中均未检测到人脸")
        src_faces = [f for f in map_faces if f is not None]
    elif req.source_key and req.source_key in _named_embeddings:
        # 直播链路：用预存命名平均 embedding（每帧只传短 key，零额外传输/零逐帧检测）
        src_faces = [_face_from_embedding(_named_embeddings[req.source_key])]
    elif req.source_images:
        # 图片换脸/预览：现算多照片平均（按列表内容哈希缓存，直播重发同组照不重复检测）
        _ak = (len(req.source_images), hash(tuple(req.source_images))) if SRC_CACHE_ON else None
        _emb = _avg_emb_cache.get(_ak) if _ak else None
        if _emb is None:
            _emb, _n = _avg_embedding(req.source_images)
            if _emb is None:
                # 多图全未检出脸 → 回退单图/激活脸，绝不整帧失败
                _emb_fallback = _analyse_src(src_b64) if src_b64 else None
                if _emb_fallback is None:
                    raise HTTPException(status_code=400, detail="多照片源脸中均未检测到人脸")
                src_faces = [_emb_fallback]
            else:
                if _ak is not None:
                    if len(_avg_emb_cache) >= _AVG_EMB_CACHE_MAX:
                        _avg_emb_cache.pop(next(iter(_avg_emb_cache)))
                    _avg_emb_cache[_ak] = _emb
                src_faces = [_face_from_embedding(_emb)]
                src_faces_n = _n
        else:
            src_faces = [_face_from_embedding(_emb)]
            src_faces_n = len(req.source_images)
    elif not req.source_image and _active_face_name in _named_embeddings:
        # 2026-07-09 直连补件：raw 通道不带 source_*，源=引擎激活脸。若该角色已注册多照平均
        # embedding(Hub 激活时推送)，用平均——与 JSON 通道 Hub 注入 source_key 的语义对齐，
        # 直连不再丢"多照辨识度增强"。
        src_faces = [_face_from_embedding(_named_embeddings[_active_face_name])]
        src_faces_n = -1   # 标记：命名平均(照片数引擎侧未存)
    else:
        f = _analyse_src(src_b64)
        if f is None:
            raise HTTPException(status_code=400, detail="源图中未检测到人脸")
        src_faces = [f]
    # 身份放大：沿远离人群 anchor 外推源身份，抵消 inswapper 向目标脸的回归(辨识度杠杆；β=0 时零回归)
    if not _is_dfm_active:
        src_faces = _amplify_src_faces(src_faces)
    # DFM 需要 landmark_3d_68(整脸对齐)——快路目标分析器不含它，故 DFM 帧走全模块 face_analyser。
    # 非 DFM(inswapper/hyperswap/gpen)只需 kps+2d106 → 走快路(省 recognition/genderage/3d68)。
    _tgt_an = face_analyser if _is_dfm_active else _tgt_analyser
    tgt_faces = _tgt_an.get(tgt_img)
    if not tgt_faces:
        # 2026-07-09 换灯兜底：灯光突变(强逆光/暗场/顶光过曝)下 RetinaFace 漏检 → CLAHE 亮度
        # 均衡后重检一次。只在失败帧触发(正常帧零开销)；CLAHE 不动几何，检出的 kps/bbox 与
        # 原图同坐标系——换脸仍在原始像素上进行，均衡图只用来"找脸"。
        try:
            _lab = cv2.cvtColor(tgt_img, cv2.COLOR_BGR2LAB)
            _lab[:, :, 0] = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(_lab[:, :, 0])
            tgt_faces = _tgt_an.get(cv2.cvtColor(_lab, cv2.COLOR_LAB2BGR))
            if tgt_faces:
                print("[FaceSwap API] 灯光兜底：CLAHE 均衡重检成功(原检漏脸)")
        except Exception:
            tgt_faces = tgt_faces or []
    detect_ms = int((time.time() - t_detect0) * 1000)

    if not tgt_faces:
        raise HTTPException(status_code=400, detail="目标图中未检测到人脸")

    # 小脸不换门槛(2026-07-10)：最大脸最长边 < min_face_px → 判无脸,原样返回(faces_used=0)。
    # 远景走动时 20px 的脸换了只会糊成一团、反而暴露"有假";此时保原画。近景(裁剪通道/坐桌前)
    # 脸远大于阈值,不受影响。DFM 亦适用(小脸 DFM 同样糊)。
    _min_px = int(req.min_face_px or PARAMS.get("min_face_px", 0) or 0)
    if _min_px > 0:
        _mx = max(max(f.bbox[2]-f.bbox[0], f.bbox[3]-f.bbox[1]) for f in tgt_faces)
        if _mx < _min_px:
            return SwapResponse(result_image=img_to_b64(tgt_img, int(PARAMS["jpeg_quality"])),
                                elapsed_ms=int((time.time()-t0)*1000),
                                faces_tgt=len(tgt_faces), faces_used=0, detect_ms=detect_ms,
                                swap_ms=0, faces_boxes=None)

    # 换脸：单源=源脸换到所有目标脸(旧行为)；face_map=目标脸按 x 从左到右映射槽位源脸
    result = tgt_img.copy()
    thresh = float(req.threshold) if req.threshold is not None else None
    faces_used = 0
    faces_filtered = 0
    face_map_used = None
    swapped_kps = []                       # 真被换过的脸的 5 点关键点(供增强复用,免二次检测)
    swapped_boxes = []                     # 真被换过的脸 bbox(供客户端画质体检按脸区计算)
    t_swap0 = time.time()
    valid_tgt = []
    for tgt_face in tgt_faces:
        if thresh is not None and tgt_face.det_score < thresh:
            faces_filtered += 1
            continue
        valid_tgt.append(tgt_face)
    # 仅换主脸：锁定主脸(默认最大脸；带 hint 时在位者优先+1.3×滞回)，其余脸保持原样。
    # 不计入 faces_filtered(那是置信度过滤)；faces_tgt/faces_used 差值即可见"锁走了几张"。
    if req.main_face_only and map_faces is None and len(valid_tgt) > 1:
        valid_tgt = [_pick_main_face(valid_tgt, req.main_face_hint)]
    if map_faces is not None:
        # C-2: 左→右排序对槽；超出映射的脸(第三人)回退首个可用槽源——绝不留未换的真脸上屏
        valid_tgt.sort(key=lambda tf: float(tf.bbox[0] + tf.bbox[2]))
        _fb = next(f for f in map_faces if f is not None)
        face_map_used = 0
        pairs = []
        for i, tf in enumerate(valid_tgt):
            mf = map_faces[i] if i < len(map_faces) else None
            if mf is not None:
                face_map_used += 1
            pairs.append((tf, mf if mf is not None else _fb))
    else:
        pairs = [(tf, src_faces[0]) for tf in valid_tgt]
    # 2026-07-09 贴回路径解析：feather(羽化)或遮挡开启 → 自定义贴回(paste_back=False 拿对齐
    # 输出自己贴)；否则沿用各 swapper 内置贴回(逐字节零回归)。DFM 有自己的 celeb_mask 贴回，
    # 不走自定义路径(其掩码语义不同)；遮挡对 DFM 暂不生效。
    _blend_mode = (req.blend_mode or PARAMS.get("blend_mode", "poisson")).strip().lower()
    _occl_on = bool(PARAMS.get("enable_occlusion")) if req.occlusion is None else bool(req.occlusion)
    _mouth_on = bool(PARAMS.get("mouth_mask")) if req.mouth_mask is None else bool(req.mouth_mask)
    _mask_pad = _mask_padding_current(req.mask_padding)   # 掩码内缩(光头×有发源脸黑边根治)
    _occluder = _get_xseg() if _occl_on else None
    if _occl_on and _occluder is None:
        _occl_on = False                     # 模型缺失/加载失败 → 静默停用，绝不 500
    _custom_paste = (_blend_mode == "feather" or _occl_on or _mouth_on
                     or _mask_pad is not None) and not _is_dfm_active
    if _occluder is not None:
        _occluder.tick()                     # 掩码时域缓存推进(每请求一次)
    _occl_cache_ok = len(pairs) == 1         # 单脸流才允许掩码缓存(多脸防串)
    for tgt_face, _sf in pairs:
        if _custom_paste:
            try:
                # HyperSwap 走 return_mask=True：拿模型脸形掩码(+已校色 crop)乘进羽化掩码，
                # 灰边根治与 feather/遮挡/口护叠加生效；其余 swapper 契约不变。
                if getattr(swapper, "use_model_mask", False):
                    _fake, _M, _mm = swapper.get(result, tgt_face, _sf,
                                                 paste_back=False, return_mask=True)
                else:
                    _fake, _M = swapper.get(result, tgt_face, _sf, paste_back=False)
                    _mm = None
                result = _paste_swapped_feather(
                    result, _fake, _M, occluder=_occluder, occl_cache_ok=_occl_cache_ok,
                    mouth_lmk=(getattr(tgt_face, "landmark_2d_106", None) if _mouth_on else None),
                    mask_padding=_mask_pad, model_mask=_mm)
            except Exception as _pe:
                print(f"[FaceSwap API] 自定义贴回失败(本脸回退内置贴回): {_pe}")
                result = swapper.get(result, tgt_face, _sf, paste_back=True)
        elif _is_dfm_active:
            # DFM 自有贴回(celeb×face 掩码)：内缩在其掩码上生效(签名扩展,None=零回归)
            result = swapper.get(result, tgt_face, _sf, paste_back=True, mask_padding=_mask_pad)
        else:
            result = swapper.get(result, tgt_face, _sf, paste_back=True)
        faces_used += 1
        if getattr(tgt_face, "kps", None) is not None:
            swapped_kps.append(tgt_face.kps)
        if getattr(tgt_face, "bbox", None) is not None:
            swapped_boxes.append([int(v) for v in tgt_face.bbox])
    swap_ms = int((time.time() - t_swap0) * 1000)

    # ① Poisson 无缝融合（feather 贴回时跳过——Poisson 的亮度渗漏正是 feather 要根治的）
    t_enh0 = time.time()
    if PARAMS["enable_poisson"] and not _custom_paste:
        result = poisson_blend_face(tgt_img, result, tgt_faces)

    # ② 肤色校正
    if PARAMS["enable_color_corr"]:
        result = color_correction(tgt_img, result)

    # ②.5 融合强度(blend)：控制新脸覆盖强度。1=完全换脸(默认，等于原行为)，越低越保留原脸。
    # 换脸只改动人脸区域，背景在 result 与 tgt_img 中逐像素一致，故全图 addWeighted 对背景为恒等，
    # 仅在脸部产生 blend*新脸 + (1-blend)*原脸 的柔和过渡——既实现"融合强度"又不影响背景。
    if req.blend is not None:
        _bl = max(0.0, min(1.0, float(req.blend)))
        if _bl < 1.0:
            result = cv2.addWeighted(result, _bl, tgt_img, 1.0 - _bl, 0)

    # ③ CodeFormer 或 GFPGAN 增强
    # [2026-07-07 身份A/B] 未显式指定增强器时优先 GFPGAN：448 直播帧实测
    #   gfpgan id_src=0.877/sharp=635/254ms vs codeformer(w0.3~0.9 全档) 最好 0.807/357/342ms
    #   ——身份保持、清晰度、速度三项全胜(codeformer 生成性更强,会"重画"五官稀释源身份)。
    #   显式 enhance=codeformer 仍尊重调用方选择。
    cf_w = PARAMS["codeformer_w"]
    enhance_mode = (req.enhance or "").lower()
    _gf_ok = PARAMS["enable_gfpgan"] and face_enhancer is not None
    _cf_ok = PARAMS["enable_codeformer"] and codeformer_net is not None and face_helper is not None
    # gpen(2026-07-09)：ONNX 单前向轻精修——显式点名才用(直播档的低时延选项)；
    # 模型未就绪 → 静默跳过增强(与增强器缺席的既有降级语义一致，绝不 500)。
    use_gpen = (enhance_mode == "gpen") and bool(swapped_kps) and _get_gpen() is not None
    use_gfpgan = _gf_ok and enhance_mode in ("", "gfpgan")
    use_codeformer = _cf_ok and (enhance_mode == "codeformer"
                                 or (enhance_mode == "" and not use_gfpgan))
    if use_gpen:
        use_gfpgan = use_codeformer = False
    # DFM 跳过精修(2026-07-10 循证)：DFM 是 per-identity 网,原生输出 320px+ 已清晰,
    # GPEN/GFPGAN/CodeFormer 这类"盲修复"施加其上只会把细节抹平(实测 sharp 145→60,腰斩)+白搭
    # ~100ms。DFM 身份/清晰度都自洽,增强纯负收益。显式 enhance 也不例外(DFM 恒不修)。
    if _is_dfm_active:
        use_gpen = use_gfpgan = use_codeformer = False

    # 分辨率门控：脸够大(≥enh_gate_face_px)时增强纯亏(身份↓清晰度↓)——跳过，白捡辨识度+省 ~250ms。
    # 只在"未显式点名增强器"时门控(调用方显式要 codeformer/gfpgan 则尊重)；multi-face 取最大脸判定。
    _gate_px = int(PARAMS.get("enh_gate_face_px", 0) or 0)
    _enh_gated = False
    if _gate_px > 0 and enhance_mode == "" and (use_gfpgan or use_codeformer) and tgt_faces:
        _max_face_px = max(max(f.bbox[2] - f.bbox[0], f.bbox[3] - f.bbox[1]) for f in tgt_faces)
        if _max_face_px >= _gate_px:
            use_gfpgan = use_codeformer = False
            _enh_gated = True

    if use_gpen:
        # GPEN-256 ONNX：复用换脸阶段 kps 对齐(零二次检测)，单脸一次前向+羽化贴回。
        # deltaHP 保身份重组照常适用(增强器无关)。异常回退 GFPGAN(若在)或跳过。
        _gp_raw_before = result if PARAMS.get("enh_id_preserve") else None
        try:
            result = _get_gpen().enhance(result, swapped_kps,
                                         occluder=_occluder if _occl_on else None)
            if _gp_raw_before is not None and result.shape == _gp_raw_before.shape:
                result = _delta_highpass_merge(_gp_raw_before, result)
        except Exception as e:
            print(f"[FaceSwap API] GPEN 精修失败(本帧跳过增强): {e}")
    elif use_codeformer:
        _cf_raw_before = result if PARAMS.get("enh_id_preserve") else None
        with _enh_slot("cf") as (_fh, _pooled):
            fh = _fh if _pooled else face_helper
            try:
                fh.clean_all()
                fh.read_image(result)
                # [2026-07-06 快路] 与 GFPGAN 同理：复用换脸阶段的 insightface 5点关键点，
                # 跳过 facelib 内置 RetinaFace 全图二次检测(~55ms/帧)；无 kps(照片路径等)回退原检测。
                if ENH_REUSE_KPS and swapped_kps:
                    fh.all_landmarks_5 = [np.asarray(k, dtype=np.float32) for k in swapped_kps]
                else:
                    fh.get_face_landmarks_5(only_center_face=False, resize=640, eye_dist_threshold=5)
                fh.align_warp_face()
                for face_crop in fh.cropped_faces:
                    face_t = img2tensor(face_crop / 255., bgr2rgb=True, float32=True)
                    face_t = face_t.unsqueeze(0).to(DEVICE)
                    # CodeFormer 是 VQ-Transformer，权重保持 fp32(半精度易伤码本查找)；
                    # autocast 只在算子边界降精度，Tensor Core 提速且数值稳。
                    with torch.no_grad(), _autocast_ctx():
                        out = codeformer_net(face_t, w=cf_w, adain=True)[0]
                    restored = tensor2img(out.float(), rgb2bgr=True, min_max=(-1, 1))
                    fh.add_restored_face(restored.astype('uint8'))
                fh.get_inverse_affine(None)
                result = fh.paste_faces_to_input_image()
                if _cf_raw_before is not None and result is not _cf_raw_before and result.shape == _cf_raw_before.shape:
                    result = _delta_highpass_merge(_cf_raw_before, result)
            except Exception as e:
                print(f"[FaceSwap API] CodeFormer 失败: {e}")
                if face_enhancer is not None:
                    try:
                        with _enhance_lock, _autocast_ctx(force=ENH_HALF):   # 回退走内置 helper，串行保线程安全
                            _, _, result = face_enhancer.enhance(result, has_aligned=False,
                                                                 only_center_face=False, paste_back=True)
                    except: pass
    elif use_gfpgan:
        # deltaHP 保身份：留存增强前的换脸图(身份最准),增强后只取"改动高频"叠回→身份≈raw、清晰≈gfpgan。
        _raw_before = result if PARAMS.get("enh_id_preserve") else None
        with _enh_slot("gf") as (_fh, _pooled):
            _fast_done = False
            if ENH_REUSE_KPS and swapped_kps:
                try:
                    result = _gfpgan_enhance_fast(result, swapped_kps, fh=_fh)   # _fh=None→内置 helper(concurrency=1)
                    _fast_done = True
                except Exception as e:
                    print(f"[FaceSwap API] GFPGAN 快路失败,回退旧路: {e}")
            if not _fast_done:
                try:
                    # 回退旧路用 GFPGANer 内置(单一)helper→并发下必须串行；ENH_HALF 时权重 fp16
                    with _enhance_lock, _autocast_ctx(force=ENH_HALF):
                        _, _, result = face_enhancer.enhance(result, has_aligned=False,
                                                             only_center_face=False, paste_back=True)
                except Exception as e:
                    print(f"[FaceSwap API] GFPGAN 增强失败: {e}")
        if _raw_before is not None and result is not _raw_before and result.shape == _raw_before.shape:
            result = _delta_highpass_merge(_raw_before, result)

    enhance_ms = int((time.time() - t_enh0) * 1000)

    # ④ 时序平滑（motion 运动自适应 / flow 光流补偿 / off）
    if req.smooth_alpha is not None:
        PARAMS["smooth_alpha"] = max(0.0, min(0.99, float(req.smooth_alpha)))
    _sm = (req.smooth_mode or PARAMS.get("smooth_mode", "motion")).lower()
    t_smooth0 = time.time()
    if _sm == "off":
        pass
    elif _sm == "flow":
        result = temporal_smooth_flow(result, faces=tgt_faces)
    else:
        result = temporal_smooth(result, faces=tgt_faces)
    smooth_ms = int((time.time() - t_smooth0) * 1000)

    # ⑤ C-5 直播妆容层：增强/平滑之后上妆（在最终像素上着色，不被增强洗掉）。
    #   只作用于真被换过的脸；未带 makeup 字段=回退引擎粘滞妆容(直连通道由 Hub 激活时推送)，
    #   两者皆空=零回归。显式 req.makeup 优先(调用方语义不变)。
    makeup_ms = None
    _mk = req.makeup if req.makeup is not None else (_active_makeup or None)
    if _mk and faces_used > 0:
        t_mk0 = time.time()
        result = _apply_live_makeup(result, [tf for tf, _ in pairs], _mk)
        makeup_ms = int((time.time() - t_mk0) * 1000)

    elapsed = int((time.time() - t0) * 1000)
    _map_note = f"，face_map 槽换 {face_map_used}" if face_map_used is not None else ""
    _avg_note = f"，源平均 {src_faces_n} 照" if src_faces_n > 1 else ""
    _gate_note = "，大脸跳过增强(保辨识度)" if _enh_gated else ""
    print(f"[FaceSwap API] 全流程完成 {elapsed}ms，检测{detect_ms}ms，换脸{swap_ms}ms，增强{enhance_ms}ms，平滑{smooth_ms}ms，脸数：src {len(src_faces)} / tgt {len(tgt_faces)} / 用 {faces_used} / 过滤 {faces_filtered}{_map_note}{_avg_note}{_gate_note}")
    jq = int(PARAMS["jpeg_quality"])
    return SwapResponse(
        result_image=img_to_b64(result, jq),
        elapsed_ms=elapsed,
        faces_src=len(src_faces),
        faces_tgt=len(tgt_faces),
        faces_used=faces_used,
        faces_filtered=faces_filtered,
        detect_ms=detect_ms,
        swap_ms=swap_ms,
        enhance_ms=enhance_ms,
        smooth_ms=smooth_ms,
        faces_boxes=swapped_boxes or None,
        face_map_used=face_map_used,
        makeup_ms=makeup_ms,
    )

@app.post("/faceswap_raw")
async def faceswap_raw(request: Request):
    """二进制直连换脸（2026-07-09 传输提速）：body=原始 JPEG/PNG 字节，参数走 query string，
    响应=原始 JPEG 字节+元数据响应头。对比 JSON+b64 通道：省掉请求/响应两侧的 base64 膨胀
    (+33% 体积)与客户端编解码 CPU，直连引擎时再省 Hub 代理一跳——为 15fps 直播留传输余量。
    参数(全可选)：enhance/smooth_alpha/smooth_mode/blend/threshold/main_face_only(0|1)/
    main_face_hint(x,y)/source_key/blend_mode/occlusion(0|1)/model。
    源脸语义：粘滞双人/多人槽位(/face_map/active，Hub 保存映射时推送) > source_key(已注册的
    命名平均) > 引擎当前激活脸——直连时 Hub 不逐帧注入，需先经 /faces/switch、
    /faces/register_avg 或 /face_map/active 绑定(Hub 激活角色/保存映射时已自动推送)。
    错误仍走 JSON(HTTPException)——客户端按 Content-Type 区分。"""
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="空请求体(需要 JPEG/PNG 字节)")
    q = request.query_params

    def _qs(k):
        v = q.get(k)
        return v if v not in (None, "") else None

    kw = {"target_image": base64.b64encode(raw).decode()}
    try:
        if _qs("enhance") is not None:      kw["enhance"] = _qs("enhance")
        if _qs("smooth_mode") is not None:  kw["smooth_mode"] = _qs("smooth_mode")
        if _qs("smooth_alpha") is not None: kw["smooth_alpha"] = float(_qs("smooth_alpha"))
        if _qs("blend") is not None:        kw["blend"] = float(_qs("blend"))
        if _qs("threshold") is not None:    kw["threshold"] = float(_qs("threshold"))
        if _qs("main_face_only") is not None:
            kw["main_face_only"] = _qs("main_face_only") in ("1", "true", "True")
        if _qs("main_face_hint") is not None:
            kw["main_face_hint"] = [float(x) for x in _qs("main_face_hint").split(",")[:2]]
        if _qs("source_key") is not None:   kw["source_key"] = _qs("source_key")
        if _qs("blend_mode") is not None:   kw["blend_mode"] = _qs("blend_mode")
        if _qs("occlusion") is not None:
            kw["occlusion"] = _qs("occlusion") in ("1", "true", "True")
        if _qs("mouth_mask") is not None:
            kw["mouth_mask"] = _qs("mouth_mask") in ("1", "true", "True")
        if _qs("min_face_px") is not None:
            kw["min_face_px"] = int(float(_qs("min_face_px")))
        if _qs("mask_padding") is not None:   # "上,右,下,左" 百分比(光头黑边内缩)
            kw["mask_padding"] = [float(x) for x in _qs("mask_padding").split(",")[:4]]
        if _qs("model") is not None:        kw["model"] = _qs("model")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"参数解析失败: {e}")
    # 复用全量管线(检测/换脸/贴回/增强/平滑/统计)；扔线程池跑——GPU 前向 ~百 ms 级，
    # 直接在 async 端点里同步调用会卡住事件循环(其它请求/健康探测全部排队)。
    from fastapi.concurrency import run_in_threadpool
    resp = await run_in_threadpool(faceswap, SwapRequest(**kw))
    jpg = base64.b64decode(resp.result_image)
    hdr = {"X-Elapsed-Ms": str(resp.elapsed_ms),
           "X-Faces-Tgt": str(resp.faces_tgt), "X-Faces-Used": str(resp.faces_used),
           "X-Detect-Ms": str(resp.detect_ms), "X-Swap-Ms": str(resp.swap_ms),
           "X-Enhance-Ms": str(resp.enhance_ms), "X-Smooth-Ms": str(resp.smooth_ms)}
    if resp.faces_boxes:
        hdr["X-Faces-Boxes"] = json.dumps(resp.faces_boxes, separators=(",", ":"))
    return Response(content=jpg, media_type="image/jpeg", headers=hdr)


def _warmup_enh_pools(img: np.ndarray, kps_list) -> None:
    """预热增强池每槽(GFPGAN kps 快路 + CodeFormer 前向),消除首帧并发冷启动。
    concurrency=1 时跳过；失败不影响服务启动。"""
    if _ENH_CONCURRENCY <= 1 or not kps_list:
        return
    t0 = time.time()
    n_gf = n_cf = 0

    def _drain(pool):
        items = []
        if pool is None:
            return items
        while True:
            try:
                items.append(pool.get_nowait())
            except Exception:
                break
        return items

    if _gf_pool is not None and face_enhancer is not None:
        for fh in _drain(_gf_pool):
            try:
                _gfpgan_enhance_fast(img, kps_list, fh=fh)
                n_gf += 1
            except Exception:
                pass
            _gf_pool.put(fh)

    if _cf_pool is not None and codeformer_net is not None and img2tensor is not None:
        for fh in _drain(_cf_pool):
            try:
                fh.clean_all()
                fh.read_image(img)
                fh.all_landmarks_5 = [np.asarray(k, dtype=np.float32) for k in kps_list]
                fh.align_warp_face()
                for face_crop in fh.cropped_faces:
                    face_t = img2tensor(face_crop / 255., bgr2rgb=True, float32=True).unsqueeze(0).to(DEVICE)
                    with torch.no_grad(), _autocast_ctx():
                        codeformer_net(face_t, w=PARAMS["codeformer_w"], adain=True)
                n_cf += 1
            except Exception:
                pass
            _cf_pool.put(fh)

    if n_gf or n_cf:
        print(f"[FaceSwap API] 增强池预热 gf={n_gf} cf={n_cf} slots={_ENH_CONCURRENCY} "
              f"{int((time.time()-t0)*1000)}ms", flush=True)


def _warmup():
    """启动时跑一次真实换脸，让 onnxruntime/cuDNN 的 EXHAUSTIVE 卷积自动调优在开机阶段
    完成，使用户的「首次真实请求」就已是热态（实测冷 6s→热 0.4s），同时保留 EXHAUSTIVE
    的最优内核（不牺牲稳态吞吐）。失败不影响服务启动。"""
    try:
        if face_analyser is None or face_swapper is None:
            print("[FaceSwap API] 跳过预热（模型未就绪）", flush=True)
            return
        # 源脸取激活明星脸；faces 目录为空(生产 .104 由 hub 每请求带 source_image)时回退
        # 捆绑预热脸 _warmup_face.jpg——否则 cuDNN autotune/增强池预热全部空转,首帧付 ~6s 冷启
        # (2026-07-06 生产日志实锤:「跳过预热(缺源脸)」)。预热脸不进 faces 列表,用户不可见。
        warm_b64 = _active_face_b64
        if not warm_b64:
            wf = Path(__file__).resolve().parent / "_warmup_face.jpg"
            if wf.exists():
                warm_b64 = base64.b64encode(wf.read_bytes()).decode()
                print("[FaceSwap API] faces 为空，使用捆绑预热脸 _warmup_face.jpg", flush=True)
        if not warm_b64:
            print("[FaceSwap API] 跳过预热（缺源脸：faces 为空且无 _warmup_face.jpg）", flush=True)
            return
        t0 = time.time()
        img = b64_to_img(warm_b64)
        faces = face_analyser.get(img)
        if faces:
            _kps = None   # S6 修根因：增强器缺席(加载失败/瘦身副本)时下方 if face_enhancer 整块跳过，
            #             旧代码 _kps 未定义 → UnboundLocalError → 整个预热被 except 吞掉(cuDNN 白冷)
            res = face_swapper.get(img.copy(), faces[0], faces[0], paste_back=True)
            if face_enhancer is not None:
                try:
                    # 走生产同款快路(复用 kps + fp16),让 cuDNN 在开机阶段就把增强网也调优热
                    _kps = getattr(faces[0], "kps", None)
                    if ENH_REUSE_KPS and _kps is not None and img2tensor is not None:
                        _gfpgan_enhance_fast(res, [_kps])
                    else:
                        with _autocast_ctx(force=ENH_HALF):
                            face_enhancer.enhance(res, has_aligned=False,
                                                  only_center_face=False, paste_back=True)
                except Exception:
                    pass
            if codeformer_net is not None and face_helper is not None:
                try:
                    face_helper.clean_all(); face_helper.read_image(res)
                    face_helper.get_face_landmarks_5(only_center_face=False, resize=640, eye_dist_threshold=5)
                    face_helper.align_warp_face()
                    for fc in face_helper.cropped_faces:
                        ft = img2tensor(fc / 255., bgr2rgb=True, float32=True).unsqueeze(0).to(DEVICE)
                        with torch.no_grad():
                            codeformer_net(ft, w=PARAMS["codeformer_w"], adain=True)
                except Exception:
                    pass
            if _kps is not None:
                _warmup_enh_pools(res, [_kps])
            # 2026-07-10 扩展预热：直播实际链路的新组件也在开机阶段做 cuDNN autotune，
            # 消除首帧/失联回切首帧卡顿(旧 _warmup 只热源脸分析器+inswapper+GFPGAN/CF)。
            #   · _tgt_analyser：直播用的目标脸检测器(剥了 recognition/genderage,与 face_analyser
            #     不是同一会话,不热则首个真帧付 det cuDNN 调优)
            #   · GPEN：hd 档默认精修器(惰性加载,不热则首个 gpen 帧付建会话+调优)
            #   · XSeg：仅当遮挡默认开时热(否则不加载,不白占显存)
            #   · feather 贴回：默认贴回路径
            _wkps = getattr(faces[0], "kps", None)
            try:
                if _tgt_analyser is not None and _tgt_analyser is not face_analyser:
                    _tf = _tgt_analyser.get(img)
                    if _tf and _wkps is None:
                        _wkps = getattr(_tf[0], "kps", None)
            except Exception:
                pass
            _gp = _get_gpen()
            if _gp is not None and _wkps is not None:
                try:
                    _gp.enhance(res, [_wkps])
                except Exception:
                    pass
            if bool(PARAMS.get("enable_occlusion")):
                _xs = _get_xseg()
                if _xs is not None:
                    try:
                        _xs.mask_for_crop(cv2.resize(res, (256, 256)))
                    except Exception:
                        pass
            # feather 贴回路径(默认)：跑一次 paste_back=False + 羽化贴回，热 warpAffine/掩码核
            try:
                _fake, _M = face_swapper.get(res.copy(), faces[0], faces[0], paste_back=False)
                _paste_swapped_feather(res, _fake, _M)
            except Exception:
                pass
        print(f"[FaceSwap API] 预热完成（cuDNN autotune 就绪·含 tgt检测/GPEN/feather）{int((time.time()-t0)*1000)}ms", flush=True)
    except Exception as e:
        print(f"[FaceSwap API] 预热跳过: {e}", flush=True)


if __name__ == "__main__":
    print("=" * 55)
    print(" FaceSwap API v2 启动（模型常驻内存）")
    print(f" 监听: http://0.0.0.0:{os.environ.get('FACESWAP_PORT', '8000')}")
    if _backend_label == "tensorrt":
        print(f" TensorRT FP16={TRT_FP16}（首次预热将现场构建引擎，可能数分钟；")
        print(f"   缓存至 {TRT_CACHE_DIR} → 再启秒级。检测TRT={TRT_DET}）")
    print("=" * 55)
    _warmup()
    # 端口可配(默认 8000)：修复历史文档承诺的「多实例/双引擎(FACESWAP_PORT=…)」——此前硬编码 8000，
    # 起第二实例必然撞端口起不来(2026-07-06 256 探针即因此没绑上)。默认值不变=零回归。
    _port = int(os.environ.get("FACESWAP_PORT", "8000"))
    # S6: 端口独占预检——Windows REUSE 语义下双实例会**静默双绑**（请求乱跳比崩溃难查）；
    #   supervisor 启动宽限窗内的重复拉起在此 fail-fast 退出，绝不带病上线。
    try:
        import port_guard
        port_guard.ensure_port_free(_port, f"faceswap_api:{_port}")
    except ImportError:
        pass
    uvicorn.run(app, host="0.0.0.0", port=_port, log_level="warning")
