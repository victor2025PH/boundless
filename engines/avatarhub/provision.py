# -*- coding: utf-8 -*-
"""
provision.py — 环境体检 / 一键准备（让安装好的程序真正能跑起来）

本程序的 AI 微服务运行在多个 conda 环境中（安装包不含环境与模型）。本脚本：
  - --check（默认）：体检所需 conda 环境与关键模型目录是否就位，输出可操作清单（只读，安全）。
  - --create     ：对缺失的环境，按 requirements/<env>.txt 基线自动创建并安装依赖（幂等）。
  - --force      ：删除并重建指定/全部环境（危险，需确认）。

用法：
  python provision.py                 # 体检
  python provision.py --create        # 创建所有缺失环境
  python provision.py --create --only fishspeech cosytts
  python provision.py --with-selfcheck        # 给自检环境装完整回归的浏览器工具(playwright+chromium)
  python provision.py --create --with-selfcheck   # 建环境并一并装好自检浏览器工具
"""
import sys, io, os, argparse, subprocess
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import app_config

# 每个环境的 Python 版本（默认值，可按需在此调整；ML 栈对版本敏感）。
ENV_PY = {
    "facefusion": "3.10",
    "rvc": "3.10",
    "fishspeech": "3.10",
    "cosytts": "3.10",
    "musethepeak": "3.10",
    "latentsync": "3.10",
    "voxcpm": "3.11",
    "nemoasr": "3.11",
}

# 部分环境的 requirements 基线「故意不含 torch」，避免 PyPI 默认装错 CUDA 版本（新卡如
# 50/40 系需 cu128，否则推理直接报 sm_120 不支持）。这里在装 requirements 之前先按显卡
# 装对 torch，让 `provision.py --create` 一条命令即可在目标机产出可用环境。
# 覆盖 CUDA 轮子源： set AVATARHUB_TORCH_INDEX=https://download.pytorch.org/whl/cu121
TORCH_INDEX = os.environ.get("AVATARHUB_TORCH_INDEX", "https://download.pytorch.org/whl/cu128")
# 每个环境的「预装步骤」：在装 requirements 之前按序执行。每步 (包列表, 是否用 torch 轮子源)。
# nemoasr 还需 Cython/packaging 才能从 git main 源码构建 NeMo（requirements 里用 git URL）。
ENV_PRE_PIP = {
    "voxcpm":  [(["torch"], True)],
    "nemoasr": [(["torch"], True), (["Cython", "packaging"], False)],
}

# 关键模型 / 第三方源码树目录（相对项目根），仅做存在性体检提示。
MODEL_DIRS = [
    "fish-speech", "MuseTalk", "LivePortrait", "CosyVoice",
    "facefusion", "GPT-SoVITS", "LatentSync",
]

# 跑完整回归自检（deliver_check/acceptance 的浏览器 E2E）的环境——与 deliver_check.bat 的 FACEFUSION_PY 一致。
SELFCHECK_ENV = "facefusion"


def needed_envs() -> list[str]:
    """从服务清单派生所需的 conda 环境（去重，含 rvc）。"""
    envs = {s["env"] for s in app_config.SERVICES.values()}
    envs.add("rvc")  # tts_api(XTTS) 用
    return sorted(envs)


def env_python(env: str) -> Path:
    return Path(app_config.conda_python(env))


def env_exists(env: str) -> bool:
    return env_python(env).exists()


def _conda_exe() -> str | None:
    root = app_config.CONDA_ROOT
    if root:
        for c in (root / "Scripts" / "conda.exe", root / "condabin" / "conda.bat",
                  root / "Library" / "bin" / "conda.bat"):
            if c.exists():
                return str(c)
    import shutil
    return shutil.which("conda")


def check():
    envs = needed_envs()
    print("=" * 60)
    print(" 环境体检")
    print("=" * 60)
    print(f" conda 根目录: {app_config.CONDA_ROOT or '未探测到（请先安装 Miniconda/Anaconda）'}")
    print("-" * 60)
    print(" conda 环境:")
    missing = []
    for env in envs:
        ok = env_exists(env)
        req = app_config.BASE / "requirements" / f"{env}.txt"
        rtag = "" if req.exists() else "  [缺少 requirements 基线]"
        print(f"   {'[OK]' if ok else '[--]'} {env:14s} {'已就位' if ok else '缺失'}{rtag}")
        if not ok:
            missing.append(env)
    print("-" * 60)
    print(" 关键模型 / 源码目录:")
    miss_models = []
    for d in MODEL_DIRS:
        ok = (app_config.BASE / d).exists()
        print(f"   {'[OK]' if ok else '[--]'} {d}")
        if not ok:
            miss_models.append(d)
    # 完整回归自检工具（可选，仅 deliver_check 浏览器 E2E 需要；基础自检无需）
    sc_py = env_python(SELFCHECK_ENV)
    if sc_py.exists():
        has_pw = subprocess.run(
            [str(sc_py), "-c", "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('playwright') else 3)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        print("-" * 60)
        print(" 完整回归自检工具(可选 · deliver_check 浏览器 E2E):")
        print(f"   {'[OK]' if has_pw else '[--]'} playwright @ {SELFCHECK_ENV}"
              + ("" if has_pw else "  → python provision.py --with-selfcheck"))
    print("=" * 60)
    if not missing and not miss_models:
        print(" 全部就位，可直接启动。")
    else:
        if missing:
            print(f" 缺失环境 {len(missing)} 个：{', '.join(missing)}")
            print(f"   → 运行：python provision.py --create")
        if miss_models:
            print(f" 缺失模型目录 {len(miss_models)} 个：{', '.join(miss_models)}")
            print(f"   → 请按 README 准备相应模型 / 第三方源码树。")
    return missing


def create(only: list[str] | None, force: bool):
    conda = _conda_exe()
    if not conda:
        print("[ERROR] 未找到 conda。请先安装 Miniconda/Anaconda 并确保可用。")
        return 1
    envs = only or needed_envs()
    rc = 0
    for env in envs:
        req = app_config.BASE / "requirements" / f"{env}.txt"
        if not req.exists():
            print(f"[skip] {env}: 缺少 requirements/{env}.txt，跳过。")
            continue
        if env_exists(env) and not force:
            print(f"[skip] {env}: 已存在（--force 可重建）。")
            continue
        pyver = ENV_PY.get(env, "3.10")
        if force and env_exists(env):
            print(f"[force] 删除环境 {env} …")
            subprocess.run([conda, "env", "remove", "-y", "-n", env])
        print(f"[create] {env} (python={pyver}) …")
        r = subprocess.run([conda, "create", "-y", "-n", env, f"python={pyver}"])
        if r.returncode != 0:
            print(f"[ERROR] 创建 {env} 失败。")
            rc = 1
            continue
        py = str(env_python(env))
        pre_ok = True
        for pkgs, use_torch_index in ENV_PRE_PIP.get(env, []):
            cmd = [py, "-m", "pip", "install", *pkgs]
            if use_torch_index:
                cmd += ["--index-url", TORCH_INDEX]
                print(f"[pre] pip install {' '.join(pkgs)}  (torch 轮子源 {TORCH_INDEX}) …")
            else:
                print(f"[pre] pip install {' '.join(pkgs)} …")
            rp = subprocess.run(cmd)
            if rp.returncode != 0:
                print(f"[WARN] {env}: 预装 {' '.join(pkgs)} 失败"
                      + ("（可设 AVATARHUB_TORCH_INDEX 换 CUDA 版本后重试）" if use_torch_index else ""))
                pre_ok = False
                rc = 1
        if not pre_ok:
            print(f"[skip] {env}: 因 torch 预装失败，跳过 requirements 安装，避免装上不匹配的 torch。")
            continue
        print(f"[deps] pip install -r {req.name} …")
        r = subprocess.run([py, "-m", "pip", "install", "-r", str(req)])
        if r.returncode != 0:
            print(f"[WARN] {env} 依赖安装有错误，请查看上面的日志（可能需按显卡调整 torch/CUDA）。")
            rc = 1
        else:
            print(f"[done] {env} 就绪。")
    return rc


def provision_selfcheck() -> int:
    """给自检环境（facefusion）装「完整回归」所需的浏览器自检工具：selfcheck.txt + playwright 浏览器。
    幂等：pip 与 `playwright install chromium` 均可重复执行。基础自检(doctor/pack_acceptance/一键验收)无需本步。"""
    req = app_config.BASE / "requirements" / "selfcheck.txt"
    if not req.exists():
        print("[skip] 缺少 requirements/selfcheck.txt。")
        return 1
    if not env_exists(SELFCHECK_ENV):
        print(f"[ERROR] 自检环境 {SELFCHECK_ENV} 不存在，请先：python provision.py --create --only {SELFCHECK_ENV}")
        return 1
    py = str(env_python(SELFCHECK_ENV))
    print(f"[selfcheck] pip install -r {req.name} → {SELFCHECK_ENV} …")
    if subprocess.run([py, "-m", "pip", "install", "-r", str(req)]).returncode != 0:
        print("[ERROR] selfcheck 依赖安装失败。")
        return 1
    print(f"[selfcheck] playwright install chromium → {SELFCHECK_ENV} …")
    if subprocess.run([py, "-m", "playwright", "install", "chromium"]).returncode != 0:
        print("[WARN] 浏览器二进制安装失败（可稍后重试：该环境 python -m playwright install chromium）。")
        return 1
    print("[done] 自检浏览器工具就绪：完整回归 deliver_check 可跑浏览器 E2E。")
    return 0


def main():
    ap = argparse.ArgumentParser(description="环境体检 / 一键准备")
    ap.add_argument("--create", action="store_true", help="创建缺失环境")
    ap.add_argument("--force", action="store_true", help="删除并重建（危险）")
    ap.add_argument("--only", nargs="*", help="只处理指定环境")
    ap.add_argument("--with-selfcheck", action="store_true",
                    help=f"给自检环境({SELFCHECK_ENV})装完整回归所需的浏览器工具(selfcheck.txt + playwright 浏览器)")
    args = ap.parse_args()

    if not args.create and not args.force and not args.with_selfcheck:
        sys.exit(1 if check() else 0)

    if args.force:
        tgt = ", ".join(args.only) if args.only else "全部所需环境"
        ans = input(f"确认删除并重建：{tgt} ？这会丢失环境内已装内容。输入 yes 继续：").strip().lower()
        if ans != "yes":
            print("已取消。")
            sys.exit(0)

    rc = 0
    if args.create or args.force:
        rc = create(args.only, args.force)
    if args.with_selfcheck:
        rc = provision_selfcheck() or rc
    sys.exit(rc)


if __name__ == "__main__":
    main()
