# -*- coding: utf-8 -*-
"""
预构建 faceswap「hd 档」TensorRT FP16 引擎缓存（轻量·仅加载换脸网）。

为什么需要：onnxruntime 的 TensorRT EP 在「首次推理」才据 ONNX 现场编译 FP16 引擎（分钟级），
之后落盘到 TRT 缓存目录秒级复用。不预热则线上第一路换脸的首帧会卡在这次编译上。
本脚本在有 GPU + ONNX 模型就位的实机跑一次，把引擎编译好落盘，之后 faceswap 服务直接命中缓存。

轻量：只加载「换脸 ONNX」这一个模型（不加载 buffalo_l 检测器），省显存、更快，
即使显存吃紧构建失败也只影响本进程、不动正在运行的其它服务。
缓存目录/模型选择/Provider 选项均与 faceswap_api 对齐 → 引擎缓存键 100% 通用。

用法（facefusion 环境，实机）：
  set FACESWAP_PRESET=hd
  python build_faceswap_trt.py
可选：
  set FACESWAP_MODEL=D:\models\hyperswap_256.onnx   # 指定高清换脸 ONNX（否则 hd 自动探测，缺失回退 inswapper_128）
  set FACESWAP_TRT_FP16=0                            # 走 FP32（默认 1=FP16）
  python build_faceswap_trt.py --runs 3
"""
import argparse
import glob
import os
import sys
import time
from pathlib import Path

import app_config

# ── 与 faceswap_api 对齐的路径/选择逻辑（保持同步）───────────────────────────
_BASE = str(app_config.BASE)
INSWAPPER_MODEL = os.path.join(_BASE, "Deep-Live-Cam", "models", "inswapper_128.onnx")
FACESWAP_MODEL_PATH = os.environ.get("FACESWAP_MODEL", "").strip()
FACESWAP_PRESET = os.environ.get("FACESWAP_PRESET", os.environ.get("SWAP_PRESET", "")).strip().lower()
TRT_FP16 = os.environ.get("FACESWAP_TRT_FP16", "1") != "0"
TRT_CACHE_DIR = (os.environ.get("FACESWAP_TRT_CACHE", "").strip()
                 or str(app_config.BASE / "models" / "trt_cache" / "faceswap"))


def _log(m: str):
    print(f"[TRT预构建] {m}", flush=True)


def _detect_hyperswap() -> str:
    dirs = [str(Path(INSWAPPER_MODEL).parent), str(app_config.BASE / "models")]
    cands = []
    for d in dirs:
        cands += glob.glob(os.path.join(d, "*yper*wap*.onnx"))
    cands = [c for c in dict.fromkeys(cands) if Path(c).is_file()]
    if not cands:
        return ""
    cands.sort(key=lambda p: (0 if "256" in Path(p).name else 1, len(p)))
    return cands[0]


def _base_swap_model() -> str:
    if FACESWAP_MODEL_PATH and Path(FACESWAP_MODEL_PATH).is_file():
        return FACESWAP_MODEL_PATH
    if FACESWAP_PRESET == "hd":
        hs = _detect_hyperswap()
        if hs:
            _log(f"预设 hd → 自动选用 HyperSwap: {hs}")
            return hs
    return INSWAPPER_MODEL


def _trt_provider_options() -> dict:
    try:
        os.makedirs(TRT_CACHE_DIR, exist_ok=True)
    except Exception:
        pass
    return {"trt_fp16_enable": TRT_FP16, "trt_engine_cache_enable": True,
            "trt_engine_cache_path": TRT_CACHE_DIR, "trt_timing_cache_enable": True}


def _dummy_feeds(sess, swapper):
    """据 session 输入形状造随机 float32 输入；动态维按换脸网惯例兜底(N=1,C=3,H/W=input_size)。"""
    import numpy as np
    size = getattr(swapper, "input_size", None) or (128, 128)   # (W, H)
    feeds = {}
    for x in sess.get_inputs():
        dims = []
        for i, d in enumerate(x.shape):
            if isinstance(d, int) and d > 0:
                dims.append(d)
            elif len(x.shape) == 4 and i == 0:
                dims.append(1)
            elif len(x.shape) == 4 and i == 1:
                dims.append(3)
            elif len(x.shape) == 4 and i == 2:
                dims.append(int(size[1]))
            elif len(x.shape) == 4 and i == 3:
                dims.append(int(size[0]))
            else:
                dims.append(1)
        feeds[x.name] = np.random.rand(*dims).astype(np.float32)
    return feeds


def main() -> int:
    ap = argparse.ArgumentParser(description="预构建 faceswap hd 档 TensorRT 引擎缓存(轻量)")
    ap.add_argument("--runs", type=int, default=2, help="构建后再测几次缓存命中单帧耗时(默认 2)")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    t0 = time.time()
    import onnxruntime
    trt_listed = "TensorrtExecutionProvider" in onnxruntime.get_available_providers()
    try:
        import torch
        cuda_ok = torch.cuda.is_available()
    except Exception:
        cuda_ok = False

    model = _base_swap_model()
    _log(f"模型={Path(model).name}  存在={Path(model).is_file()}")
    _log(f"TRT被列出={trt_listed}  CUDA可用={cuda_ok}  FP16={TRT_FP16}")
    _log(f"引擎缓存目录：{TRT_CACHE_DIR}")
    if not Path(model).is_file():
        _log("换脸 ONNX 不存在 → 无法构建。请确认模型路径或 FACESWAP_MODEL。")
        return 2

    try:
        import insightface
    except Exception as e:
        _log(f"导入 insightface 失败：{e}")
        return 2

    prov = ["TensorrtExecutionProvider", "CUDAExecutionProvider", "CPUExecutionProvider"]
    popts = [_trt_provider_options(), {}, {}]
    _log("加载换脸 ONNX（配置 TensorRT EP；仅此一个模型，不加载检测器）…")
    try:
        swapper = insightface.model_zoo.get_model(model, providers=prov, provider_options=popts)
    except Exception as e:
        _log(f"模型加载失败：{type(e).__name__}: {e}")
        _log("若为显存不足(OOM)：先停掉部分占显存的服务或择空闲时段重跑；本失败不影响正在运行的其它服务。")
        return 2

    sess = getattr(swapper, "session", None)
    if sess is None:
        _log("未取到 ONNX session（insightface 加载器异常）。")
        return 2

    applied = list(sess.get_providers())
    trt_applied = "TensorrtExecutionProvider" in applied
    _log(f"实际生效 providers = {applied}")

    if not trt_applied:
        _log("⚠ TensorRT EP 被列出但未真正启用——通常是缺 TensorRT 运行库（nvinfer_*.dll 不在 PATH）。")
        _log(f"  → 不会产生 .engine 缓存；当前实跑后端={applied}。")
        if all("CPU" in p for p in applied):
            _log("  ⚠⚠ 且已回退到 CPU（换脸实时性能不可用）。线上务必确保走 CUDA。")
        _log("  方案A(推荐,省事)：不用 TRT，hd 档用 CUDA 即可（约 43fps）——faceswap_api 已修复为此情形自动走 CUDA。")
        _log("  方案B(要 TRT 提速)：安装与 onnxruntime-gpu 匹配的 TensorRT 10.x 运行库并加入 PATH，再重跑本脚本。")
        feeds = _dummy_feeds(sess, swapper)
        t2 = time.time(); sess.run(None, feeds)
        _log(f"当前后端单帧热身耗时 {(time.time() - t2) * 1000:.1f}ms（仅供参考，非 TRT）。")
        return 3

    feeds = _dummy_feeds(sess, swapper)
    _log(f"触发首次推理构建引擎（输入 { {k: list(v.shape) for k, v in feeds.items()} }）…首次可能数分钟…")
    tb = time.time()
    try:
        sess.run(None, feeds)
    except Exception as e:
        _log(f"首次推理(引擎构建)报错：{type(e).__name__}: {e}")
        _log("若为 OOM：释放显存后重跑；不影响其它服务。")
        return 2
    _log(f"引擎构建/首帧完成，耗时 {time.time() - tb:.1f}s")

    for i in range(max(0, args.runs)):
        t2 = time.time()
        sess.run(None, feeds)
        _log(f"缓存命中单帧 #{i + 1}: {(time.time() - t2) * 1000:.1f}ms")

    if os.path.isdir(TRT_CACHE_DIR):
        files = os.listdir(TRT_CACHE_DIR)
        eng = [f for f in files if f.endswith(".engine")]
        _log(f"缓存目录现有 {len(files)} 文件（.engine {len(eng)} 个）：{files[:8]}")
    _log(f"完成，总耗时 {time.time() - t0:.1f}s。之后以相同 hd 档启动 faceswap 即秒级复用该引擎。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
