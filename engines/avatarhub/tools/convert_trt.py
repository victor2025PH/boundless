# -*- coding: utf-8 -*-
"""
tools/convert_trt.py — Phase E / 8-1：inswapper ONNX → TensorRT FP16

用法（需 CUDA + TensorRT + trtexec 在 PATH）:
  python tools/convert_trt.py
  python tools/convert_trt.py --onnx path/to/inswapper.onnx --out path/to/inswapper.trt

转换成功后，启动换脸服务时设置:
  set FACESWAP_TRT=1
  set FACESWAP_TRT_MODEL=C:\\模仿音色\\models\\inswapper_128.trt

若 trtexec 不可用，脚本会打印手动步骤并退出码 2。
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ONNX = ROOT / "Deep-Live-Cam" / "models" / "inswapper_128.onnx"
DEFAULT_OUT = ROOT / "models" / "inswapper_128.trt"


def find_trtexec() -> str | None:
    for name in ("trtexec", "trtexec.exe"):
        p = shutil.which(name)
        if p:
            return p
    # 常见 TensorRT 安装路径
    for base in (
        r"C:\TensorRT\bin\trtexec.exe",
        r"C:\Program Files\NVIDIA GPU Computing Toolkit\TensorRT\bin\trtexec.exe",
    ):
        if os.path.isfile(base):
            return base
    return None


def convert(onnx: Path, out: Path, fp16: bool = True, workspace_gb: int = 4) -> int:
    if not onnx.is_file():
        print(f"ONNX 不存在: {onnx}")
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    trt = find_trtexec()
    if not trt:
        print("未找到 trtexec。请安装 TensorRT 并将 bin 加入 PATH，然后运行:")
        print(f'  trtexec --onnx="{onnx}" --saveEngine="{out}" --fp16 --workspace={workspace_gb * 1024}')
        return 2
    cmd = [
        trt, f"--onnx={onnx}", f"--saveEngine={out}",
        f"--workspace={workspace_gb * 1024}",
    ]
    if fp16:
        cmd.append("--fp16")
    print("运行:", " ".join(cmd))
    r = subprocess.run(cmd)
    if r.returncode == 0 and out.is_file():
        print(f"完成: {out} ({out.stat().st_size // (1024 * 1024)} MB)")
        print("启动 faceswap_api 前: set FACESWAP_TRT=1")
        return 0
    print("trtexec 失败，回退使用 ONNX (DmlExecutionProvider / CUDAExecutionProvider)")
    return r.returncode or 1


def main():
    ap = argparse.ArgumentParser(description="inswapper ONNX → TensorRT")
    ap.add_argument("--onnx", type=Path, default=DEFAULT_ONNX)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--no-fp16", action="store_true")
    ap.add_argument("--workspace-gb", type=int, default=4)
    args = ap.parse_args()
    sys.exit(convert(args.onnx, args.out, fp16=not args.no_fp16, workspace_gb=args.workspace_gb))


if __name__ == "__main__":
    main()
