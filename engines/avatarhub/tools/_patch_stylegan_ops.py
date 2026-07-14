# -*- coding: utf-8 -*-
"""HairFastGAN StyleGAN2 op 免编译补丁（2026-07-08 阶段7）。

背景：repo 自带三份 rosinality 风格 op（fused_act/upfirdn2d），import 时
torch.utils.cpp_extension.load() 现场编译 CUDA 扩展——本机无 nvcc/ninja，
直接 RuntimeError 阻断 hair_swap 导入。

方案：模块级 load() 包 try/except；失败置 _HAS_COMPILED=False，
把 CUDA 分支也引到文件里现成的纯 PyTorch native 实现（推理场景数值等价，
5090 上慢一点但可接受；比装 3GB CUDA Toolkit + 改系统 PATH 干净得多）。

幂等：已打过补丁的文件自动跳过。"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(r"c:\模仿音色\HairFastGAN")
MARK = "_HAS_COMPILED"

FUSED_OLD = """module_path = os.path.dirname(__file__)
fused = load(
    "fused",
    sources=[
        os.path.join(module_path, "fused_bias_act.cpp"),
        os.path.join(module_path, "fused_bias_act_kernel.cu"),
    ],
)"""
FUSED_NEW = """module_path = os.path.dirname(__file__)
try:
    fused = load(
        "fused",
        sources=[
            os.path.join(module_path, "fused_bias_act.cpp"),
            os.path.join(module_path, "fused_bias_act_kernel.cu"),
        ],
    )
    _HAS_COMPILED = True
except Exception as _e:  # 无 nvcc/ninja：走纯 PyTorch 兜底（见 fused_leaky_relu）
    print(f"[stylegan2.op] fused_act 编译不可用，纯 PyTorch 兜底: {str(_e)[:120]}")
    fused = None
    _HAS_COMPILED = False"""

FUSED_DISPATCH_OLD = """def fused_leaky_relu(input, bias, negative_slope=0.2, scale=2 ** 0.5):
    if input.device.type == "cpu":"""
FUSED_DISPATCH_NEW = """def fused_leaky_relu(input, bias, negative_slope=0.2, scale=2 ** 0.5):
    if input.device.type == "cpu" or not _HAS_COMPILED:"""

UP_OLD = """upfirdn2d_op = load(
    "upfirdn2d",
    sources=[
        os.path.join(module_path, "upfirdn2d.cpp"),
        os.path.join(module_path, "upfirdn2d_kernel.cu"),
    ],
)"""
UP_NEW = """try:
    upfirdn2d_op = load(
        "upfirdn2d",
        sources=[
            os.path.join(module_path, "upfirdn2d.cpp"),
            os.path.join(module_path, "upfirdn2d_kernel.cu"),
        ],
    )
    _HAS_COMPILED = True
except Exception as _e:  # 无 nvcc/ninja：走 upfirdn2d_native 兜底
    print(f"[stylegan2.op] upfirdn2d 编译不可用，纯 PyTorch 兜底: {str(_e)[:120]}")
    upfirdn2d_op = None
    _HAS_COMPILED = False"""

UP_DISPATCH_OLD = """def upfirdn2d(input, kernel, up=1, down=1, pad=(0, 0)):
    if input.device.type == "cpu":"""
UP_DISPATCH_NEW = """def upfirdn2d(input, kernel, up=1, down=1, pad=(0, 0)):
    if input.device.type == "cpu" or not _HAS_COMPILED:"""


def patch(path: Path, pairs: list) -> str:
    s = path.read_text(encoding="utf-8")
    if MARK in s:
        return "skip(已打过)"
    n = 0
    for old, new in pairs:
        if old in s:
            s = s.replace(old, new, 1)
            n += 1
    if n < len(pairs):
        return f"FAIL(只命中 {n}/{len(pairs)} 处，文件结构与预期不符)"
    path.write_text(s, encoding="utf-8")
    return "patched"


def main():
    ok = True
    for f in sorted(BASE.rglob("op/fused_act.py")):
        r = patch(f, [(FUSED_OLD, FUSED_NEW), (FUSED_DISPATCH_OLD, FUSED_DISPATCH_NEW)])
        print(f"{f.relative_to(BASE)}: {r}")
        ok &= not r.startswith("FAIL")
    for f in sorted(BASE.rglob("op/upfirdn2d.py")):
        r = patch(f, [(UP_OLD, UP_NEW), (UP_DISPATCH_OLD, UP_DISPATCH_NEW)])
        print(f"{f.relative_to(BASE)}: {r}")
        ok &= not r.startswith("FAIL")
    print("[done]" if ok else "[FAIL]")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
