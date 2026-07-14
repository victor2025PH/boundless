# -*- coding: utf-8 -*-
"""
gen_linux_requirements.py — 从 Windows 基线 requirements/<env>.txt 生成 Linux 容器版
                            docker/requirements/<env>.linux.txt（云 GPU 部署用）。

为什么要生成而不是手维护两份：requirements/*.txt 是 Windows 交付的单一真相（pip freeze
基线，随功能演进持续更新）。手工维护 Linux 副本必然漂移；这里用确定性规则一键再生，
让"云端跑同一套代码"（云服务与远程代部署方案.md 第三节）有可复现的依赖地基。

转换规则（保持其余行原样、注释与顺序不动）：
  1. 剔除 Windows-only 包：pywin32/pyreadline3/win32_setctime/pyvirtualcam/PyAudio/
     sounddevice/winsound 等（GPU 推理服务是纯 HTTP 面，不碰声卡/虚拟摄像头）。
  2. 可编辑安装 `-e c:\\...\\<pkg>` → 移入伴生文件 <env>.linux.editable.txt（路径重写为
     /app/<pkg>）。镜像构建期装不了 /app 挂载卷里的源码树 → 容器启动时由 entrypoint
     `pip install --no-deps -r` 秒装（依赖已在主文件装齐）。伴生文件恒生成（可为空），
     Dockerfile COPY 不会因缺文件而失败。
  3. torch/torchvision/torchaudio 带 +cuXXX 本地版本 → 文件头补对应
     `--extra-index-url https://download.pytorch.org/whl/<cuXXX>`；
     nightly（含 .dev）→ nightly 轮子源。无法在 PyPI/官源找到的 dev 钉版本降级为
     同 CUDA 家族的稳定版提示行（注释保留原始钉版，人工复核）。
  4. onnxruntime-gpu 保留（Linux 有官方 GPU 轮子）。

用法：
  python docker/gen_linux_requirements.py              # 生成全部（fishspeech/cosytts/musethepeak/facefusion）
  python docker/gen_linux_requirements.py --only fishspeech
  python docker/gen_linux_requirements.py --selftest   # 离线自测转换规则
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # docker/
ROOT = HERE.parent                              # 项目根
SRC_DIR = ROOT / "requirements"
DST_DIR = HERE / "requirements"

# 容器目标环境（与 docker-compose.yml 的四个 GPU 服务一一对应）
DEFAULT_ENVS = ["fishspeech", "cosytts", "musethepeak", "facefusion"]

# Windows-only / 桌面-only：容器内不装（小写比较；键=规范化包名）
STRIP_PKGS = {
    "pywin32", "pyreadline3", "win32-setctime", "win32_setctime",
    "pyvirtualcam", "pyaudio", "sounddevice", "winsound", "pywinpty",
    "windows-curses", "wmi",
}

_TORCH_PKGS = {"torch", "torchvision", "torchaudio"}
_NAME_RE = re.compile(r"^\s*([A-Za-z0-9_.\-]+)\s*(?:[=<>!~;\[]|$)")


def _pkg_name(line: str) -> str:
    """提取行首包名（规范化为小写、_→-）。非包行（注释/选项/-e）返回空串。"""
    s = line.strip()
    if not s or s.startswith(("#", "-")):
        return ""
    m = _NAME_RE.match(s)
    return (m.group(1) if m else "").lower().replace("_", "-")


def _cuda_tag(line: str) -> str:
    """torch==2.8.0+cu128 → cu128；无本地版本段返回空。"""
    m = re.search(r"\+((?:cu|rocm)[\w.]+)", line)
    return m.group(1) if m else ""


def convert(text: str, env: str) -> tuple[str, str]:
    """单文件转换（纯函数，selftest 直接喂字符串）。返回 (主清单, 可编辑安装伴生清单)。"""
    out: list[str] = []
    editables: list[str] = []
    extra_indexes: list[str] = []
    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        s = line.strip()
        # 幂等：跳过上一轮生成头（重新计算），已有 pytorch 源行收编进 header 去重
        if s.startswith("# 由 docker/gen_linux_requirements.py") or \
           s.startswith("# 改上游 Windows 基线后重新生成"):
            continue
        if s.startswith("--extra-index-url"):
            if s not in extra_indexes:
                extra_indexes.append(s)
            continue
        # 可编辑安装：盘符路径 → 容器内 /app 挂载点，移入伴生文件（启动时装）
        m = re.match(r"^-e\s+.*[\\/]([^\\/]+)\s*$", s)
        if m and re.match(r"^-e\s+[A-Za-z]:", s):
            editables.append(f"-e /app/{m.group(1)}")
            out.append(f"# [linux-editable→启动时装] {s}")
            continue
        if s.startswith("-e /app/"):                  # 幂等：已重写过的转伴生
            if s not in editables:
                editables.append(s)
            continue
        name = _pkg_name(line)
        if name in STRIP_PKGS:
            out.append(f"# [linux-strip] {s}")
            continue
        if name in _TORCH_PKGS:
            tag = _cuda_tag(s)
            if tag:
                nightly = ".dev" in s
                idx = (f"--extra-index-url https://download.pytorch.org/whl/"
                       f"{'nightly/' if nightly else ''}{tag}")
                if idx not in extra_indexes:
                    extra_indexes.append(idx)
                if nightly:
                    # nightly 钉版轮子会滚动下架 → 记录原始钉版，改为家族内最新 nightly
                    pin = s.split("==", 1)[1] if "==" in s else ""
                    out.append(f"# [linux-nightly] 原钉版 {name}=={pin}（nightly 轮子滚动下架,按家族取最新）")
                    out.append(f"--pre")
                    out.append(name)
                    continue
        out.append(line)
    head = [
        f"# 由 docker/gen_linux_requirements.py 从 requirements/{env}.txt 自动生成（勿手改；",
        "# 改上游 Windows 基线后重新生成）。目标：Linux x86_64 + NVIDIA GPU 容器。",
    ] + extra_indexes
    ed_head = [f"# {env} 可编辑安装（源码树在运行时挂载卷 /app 内 → entrypoint 启动时",
               "# `pip install --no-deps -r 本文件`；依赖已在主清单装齐）。可为空。"]
    return "\n".join(head + out) + "\n", "\n".join(ed_head + editables) + "\n"


def generate(envs: list[str]) -> int:
    DST_DIR.mkdir(parents=True, exist_ok=True)
    rc = 0
    for env in envs:
        src = SRC_DIR / f"{env}.txt"
        if not src.exists():
            print(f"[skip] {env}: 缺少 requirements/{env}.txt")
            rc = 1
            continue
        dst = DST_DIR / f"{env}.linux.txt"
        # utf-8-sig: Windows 基线可能带 BOM，避免首包名被污染成 \ufeffabsl-py
        main_txt, ed_txt = convert(src.read_text(encoding="utf-8-sig"), env)
        dst.write_text(main_txt, encoding="utf-8")
        (DST_DIR / f"{env}.linux.editable.txt").write_text(ed_txt, encoding="utf-8")
        print(f"[done] {dst.relative_to(ROOT)} (+editable)")
    return rc


def _selftest() -> int:
    sample = "\n".join([
        "# comment kept",
        "fastapi==0.136.3",
        "pywin32==311",
        "pyreadline3==3.5.6",
        "PyAudio==0.2.14",
        "-e c:\\模仿音色\\fish-speech",
        "torch==2.8.0+cu128",
        "torchaudio==2.8.0+cu128",
        "torchvision==0.27.0.dev20260407+cu128",
        "onnxruntime-gpu==1.23.2",
        "win32_setctime==1.2.0",
    ])
    got, ed = convert(sample, "fishspeech")
    lines = got.splitlines()
    assert "--extra-index-url https://download.pytorch.org/whl/cu128" in lines, "缺 cu128 源"
    assert "--extra-index-url https://download.pytorch.org/whl/nightly/cu128" in lines, "缺 nightly 源"
    assert "-e /app/fish-speech" in ed.splitlines(), "编辑安装未移入伴生文件"
    assert not any(l.startswith("-e ") for l in lines), "主清单不应残留 -e 行"
    assert "torch==2.8.0+cu128" in lines, "稳定钉版应保留"
    assert "onnxruntime-gpu==1.23.2" in lines, "onnxruntime-gpu 应保留"
    assert not any(_pkg_name(l) in STRIP_PKGS for l in lines), "Windows-only 包未剔干净"
    assert any(l.startswith("# [linux-strip] pywin32") for l in lines), "剔除应留痕"
    assert "# comment kept" in lines, "注释应原样保留"
    # nightly 钉版被替换为 --pre + 裸包名
    i = lines.index("--pre")
    assert lines[i + 1] == "torchvision", "nightly 应转 --pre + 裸包名"
    # 幂等：对输出再转一次结果一致(剔除行已是注释、索引/编辑行被收编去重)
    again, ed2 = convert(got, "fishspeech")
    assert again.count("--extra-index-url https://download.pytorch.org/whl/cu128") == 1
    assert convert(again, "fishspeech")[0] == again, "应达到不动点"
    print("gen_linux_requirements selftest 全部通过")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", help="只生成指定环境")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    return generate(args.only or DEFAULT_ENVS)


if __name__ == "__main__":
    sys.exit(main())
