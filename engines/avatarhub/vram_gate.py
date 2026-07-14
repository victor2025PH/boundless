# -*- coding: utf-8 -*-
"""显存准入闸（公共模块，2026-07-08 阶段5，源自 tryon 生产事故复盘）。

背景：Windows WDDM 驱动在显存不足时**不报 OOM**，而是静默回落到共享系统内存
——权重页在 PCIe 上反复搬运，9s 的单变 15min+ 的 GPU 98% 假忙碌，还拖累共卡直播链
（实锤：2026-07-08 free≈4G 时 FitDiT 768 档 600s 未出图）。

原则：**宁可立刻 503 让调用方稍后再试，也不无声磨盘。**
适用：离线/定妆类 GPU 服务（tryon/hair/…）的重推理入口。
不要用于直播关键链（faceswap/lipsync）——那里的正确策略是常驻+预算制，不是拒单。

用法:
    import vram_gate
    vram_gate.gate(7.0, service="tryon")          # 不足→HTTPException(503)
    ok, free = vram_gate.check(7.0)               # 只查不抛（非 FastAPI 场景）
环境变量 VRAM_GATE_OFF=1 全局停用（应急逃生阀）。
"""
import os
import time

# 阶段15 事故复盘（2026-07-08 整机死机重启）：Windows WDDM 给每个进程虚拟化
# 显存预算，torch.cuda.mem_get_info() 报的是**本进程预算视图**，不是物理空闲——
# 实测物理只剩 6.3G 时它还报 30.2G。闸门读了假数放行 → 解码期物理显存爆穿 →
# 溢出共享内存 → 显示驱动饿死 → 整机冻结。
# 结论：必须读物理值。优先 NVML（微秒级），退 nvidia-smi（~150ms），再退 torch。
_cache = {"t": 0.0, "v": None}
_nvml = None


def _free_gb_physical() -> float:
    global _nvml
    # ① NVML（nvidia-ml-py，torch 发行版常自带）
    try:
        if _nvml is None:
            import pynvml
            pynvml.nvmlInit()
            _nvml = pynvml
        h = _nvml.nvmlDeviceGetHandleByIndex(0)
        return _nvml.nvmlDeviceGetMemoryInfo(h).free / 1024 ** 3
    except Exception:
        _nvml = None
    # ② nvidia-smi 子进程
    try:
        import subprocess
        # CREATE_NO_WINDOW：宿主服务本身无控制台时，缺此标志每次调用都会闪黑窗
        # （2026-07-13 实锤：videotryon 每 5s 探活 → 全屏黑窗雨）。非 Windows 平台取 0 无副作用。
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return float(out.stdout.strip().splitlines()[0]) / 1024
    except Exception:
        pass
    # ③ torch 进程视图（会虚高，聊胜于无）
    import torch
    if not torch.cuda.is_available():
        return float("inf")
    return torch.cuda.mem_get_info()[0] / 1024 ** 3


def free_gb() -> float:
    """当前 GPU **物理**空闲显存(GB)，1s 缓存；无 CUDA/无 NVIDIA 视为无限。"""
    now = time.time()
    if _cache["v"] is not None and now - _cache["t"] < 1.0:
        return _cache["v"]
    try:
        v = _free_gb_physical()
    except Exception:
        v = float("inf")
    _cache.update(t=now, v=v)
    return v


def check(need_gb: float) -> tuple[bool, float]:
    """返回 (是否放行, 当前空闲GB)。VRAM_GATE_OFF=1 时恒放行。"""
    if os.environ.get("VRAM_GATE_OFF", "0") == "1":
        return True, free_gb()
    f = free_gb()
    return f >= need_gb, f


def gate(need_gb: float, service: str = ""):
    """FastAPI 入口闸：空闲显存不足 need_gb 时抛 503（人话提示）。"""
    ok, f = check(need_gb)
    if not ok:
        from fastapi import HTTPException
        tag = f"[{service}] " if service else ""
        raise HTTPException(503, f"{tag}显卡空闲显存不足（{f:.1f}G < {need_gb}G，"
                                 f"直播/其他任务占用中）——请稍后再试或下播后再操作")
