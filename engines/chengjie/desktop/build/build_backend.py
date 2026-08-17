#!/usr/bin/env python3
"""把仓库后端（main.py）打成自包含可执行，供桌面端作为 sidecar 随包分发（P0 本地自包含）。

用法（在 desktop/ 下）：
    npm run build:backend        # = python build/build_backend.py
或直接：
    python desktop/build/build_backend.py [--clean] [--onefile]

产出：desktop/build/backend-dist/backend(.exe)（onedir 默认，更稳）。
electron-builder 的 extraResources 会把 backend-dist/ → 安装包内 resources/backend/。

注意（重量级软依赖）：openai-whisper / torch / easyocr 体积巨大且为「可选」软依赖。
默认 EXCLUDE 之以控包体；若该机型需要本地 ASR/OCR，去掉对应 --exclude 重打或改走在线后端。
打包是「构建期」动作，需在装好 requirements.txt 的同款 Python 环境里跑，且与目标 OS 一致
（Windows 包要在 Windows 上打、mac 包在 mac 上打——PyInstaller 不跨平台交叉编译）。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

# Windows 默认 GBK 控制台无法编码 ✓/✗/⚠ 等状态符 → 打包成功后最后一行 print 会抛
# UnicodeEncodeError 使脚本 exit 1（CI 打包烟测即便构建成功也误报失败）。强制 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent          # desktop/build
DESKTOP = HERE.parent                            # desktop
REPO = DESKTOP.parent                            # 仓库根
OUT = HERE / "backend-dist"                      # 产出目录（electron-builder extraResources.from）
NAME = "backend"

# 后端运行需要的数据（模板/静态/示例配置）。格式：(源, 包内目标相对路径)
# 注意：src/web/static 不直接入包——见 _stage_static()（剔除运行时落地的真实媒体后再打）。
DATAS = [
    (REPO / "src" / "web" / "templates", "src/web/templates"),
    # 两端共享的 copilot 组件库。admin.py 用 Path(__file__).parents[2]/"shared"/"copilot"
    # 定位并挂到 /copilot——冻结后即 <_MEIPASS>/shared/copilot。漏打 → 目录不存在 →
    # mount 被 if 跳过 → /copilot/* 全 404：桌面右栏业务助手 iframe 显示 {"detail":"Not Found"}，
    # 网页工作台侧栏的 cp-* 组件与 tokens.css 一并失效（0.2.0/0.2.1 安装版实测中招）。
    (REPO / "shared" / "copilot", "shared/copilot"),
    # P0-1 A1：桌面随包种子 = 最小配置（无 YOUR_* 占位）。AITR_DESKTOP_MODE 下
    # ConfigManager._ensure_seeded 优先播种它 → 首启只差一个 AI Key（向导写 overlay）。
    # 完整 example 仍随包：供参考 + 非桌面模式回落种子。
    (REPO / "config" / "config.desktop.min.yaml", "config"),
    (REPO / "config" / "config.example.yaml", "config"),
    # WP-1：部署能力预设档（cloud_light 等；纯 YAML，门禁保证零内网 IP）。
    # launcher 注入 AITR_DEPLOY_PROFILE 后，首启由 ConfigManager 播种进 overlay。
    (REPO / "config" / "profiles", "config/profiles"),
]

# 集团底座 platform/ 下被引擎**按文件路径**加载的瘦模块。它们在引擎目录之外，
# PyInstaller 的静态分析看不见（没有 import 语句可追），必须显式登记：
#   · credpool  —— 中央凭据池瘦客户端；漏打 → 中央池静默失效，用户被逼回
#     my.telegram.org 自己申请 api_id（0.2.1 安装版实测漏打）。
#   · licensing —— 机器指纹（授权绑机 + 首启体验档归属）；漏打 → 绑机校验放行、
#     体验档退化为无机器归属，一样是静默降级。
# 冻结后落在 sys._MEIPASS/platform/<name>/，与各 bridge 的查找顺序一一对应。
#
# ⚠️ 不能整目录直打（2026-08-09 实锤）：这两个目录里除 .py 外还常年躺着**运行时/机密
# 数据**——credpool/config/account_registry.db（真实 Telegram 账号 + credpool_cred）、
# credpool/config/registry.key（Fernet 密钥，registry_crypto 用它加解密 meta.session_string
# 等）、credpool/data/tgmatrix.db。整目录 --add-data 会把它们卷进**公网安装包**＝真实账号
# ＋解密钥同包交付。而消费方（credpool_bridge / license_client）运行时**只按文件路径加载
# .py**，这些数据文件从不被读。故与 src/web/static 的 RUNTIME_STATIC_EXCLUDES 同哲学：
# 暂存清洗（剔除 config/data/__pycache__ 与 .db/.key 等机密后缀）后再打，见 _stage_platform_pkg。
_PLATFORM_ROOT = REPO.parent.parent / "platform"
PLATFORM_PKGS = ("credpool", "licensing")

# static/ 下的运行时落地目录：protocol_media＝客户聊天媒体（语音/照片/视频），
# persona_avatars＝运行时同步的账号/人设头像。均为 gitignore 的生产数据，随包分发
# ＝把真实客户隐私打进公网安装包（0.1.0 曾中招），必须剔除；两目录代码均按需重建。
RUNTIME_STATIC_EXCLUDES = {"protocol_media", "persona_avatars"}
STATIC_SRC = REPO / "src" / "web" / "static"
STATIC_STAGED = HERE / "static-staged"

# 领域包（只读代码资产：系统提示词/术语/KB 种子/看板挂件/域内模板）。
# 消费方经 domain_loader.resolve_domains_dir 定位：冻结态回落到 <_MEIPASS>/domains。
# 漏打 → 安装版日志出 "Domain 'xxx' has no manifest.yaml"，领域提示词与挂件静默丢失
# （0.2.1 实测），AI 回复质量无声降级且没有任何报错。
DOMAINS_SRC = REPO / "domains"
DOMAINS_STAGED = HERE / "domains-staged"

# 集团底座瘦模块（credpool/licensing）暂存目录与「机密数据」剔除规则（见上方 _PLATFORM_ROOT
# 注释的事故背景）。EXCLUDE_DIRS 整块剔（credpool 的运行时数据只在 config/ data/ 里，licensing
# 当前无运行时数据目录，剔了也不误伤代码/schema）；SECRET_SUFFIXES 逐文件剔（防将来任意子目录
# 里躺库/密钥/证书被顺带打进公网包）。
PLATFORM_STAGED = HERE / "platform-staged"
PLATFORM_EXCLUDE_DIRS = {"config", "data", "__pycache__"}
PLATFORM_SECRET_SUFFIXES = {".db", ".db-wal", ".db-shm", ".key", ".pem",
                            ".sqlite", ".sqlite3", ".pyc", ".pyo"}

# 需要「暂存清洗后再打」的目录的包内目标（打包完整性门禁读这个常量，防两边口径漂移）
STAGED_DESTS = ("src/web/static", "domains",
                "platform/credpool", "platform/licensing")


def _stage_static() -> Path:
    """把 src/web/static 复制到构建暂存目录，顶层剔除运行时媒体目录后供 --add-data 使用。"""
    shutil.rmtree(STATIC_STAGED, ignore_errors=True)

    def _ignore(dirpath: str, names: list[str]):
        if Path(dirpath).resolve() == STATIC_SRC.resolve():
            return set(names) & RUNTIME_STATIC_EXCLUDES
        return set()

    shutil.copytree(STATIC_SRC, STATIC_STAGED, ignore=_ignore)
    leaked = [n for n in RUNTIME_STATIC_EXCLUDES if (STATIC_STAGED / n).exists()]
    if leaked:
        raise RuntimeError(f"static 暂存仍含运行时目录（打包中止防隐私泄漏）: {leaked}")
    return STATIC_STAGED


def _stage_domains() -> Path:
    """domains/ 暂存：剔除 __pycache__（构建机路径与 magic 号无谓入包）。"""
    shutil.rmtree(DOMAINS_STAGED, ignore_errors=True)
    shutil.copytree(DOMAINS_SRC, DOMAINS_STAGED,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return DOMAINS_STAGED


def _stage_platform_pkg(pkg: str):
    """把 platform/<pkg> 复制到暂存目录，剔除运行时/机密数据后供 --add-data。

    落地前**断言零 .db/.key/.pem 幸存**——防「同目录恰好躺着账号库/解密钥」被打进公网
    安装包（2026-08-09 credpool 实锤：account_registry.db + registry.key + tgmatrix.db）。
    宁可打不出（RuntimeError 中止）也不泄账号。源不存在 → None（打包侧跳过，与旧 is_dir 判一致）。
    """
    src = _PLATFORM_ROOT / pkg
    if not src.is_dir():
        return None
    dest = PLATFORM_STAGED / pkg
    shutil.rmtree(dest, ignore_errors=True)

    def _ignore(dirpath: str, names: list[str]) -> set[str]:
        drop: set[str] = set()
        for n in names:
            full = Path(dirpath) / n
            if full.is_dir():
                if n in PLATFORM_EXCLUDE_DIRS:
                    drop.add(n)
            elif full.suffix.lower() in PLATFORM_SECRET_SUFFIXES:
                drop.add(n)
        return drop

    shutil.copytree(src, dest, ignore=_ignore)
    _hard = {".db", ".db-wal", ".db-shm", ".key", ".pem", ".sqlite", ".sqlite3"}
    leaked = [str(p.relative_to(dest)) for p in dest.rglob("*")
              if p.is_file() and p.suffix.lower() in _hard]
    if leaked:
        raise RuntimeError(
            f"platform/{pkg} 暂存仍含机密数据（打包中止防账号/密钥泄漏）: {leaked}")
    return dest


def _staged_datas() -> list:
    out = [(_stage_static(), "src/web/static")]
    if DOMAINS_SRC.is_dir():
        out.append((_stage_domains(), "domains"))
    # 集团底座瘦模块：清洗后再打（绝不整目录直打，见 _PLATFORM_ROOT 注释）
    for _pkg in PLATFORM_PKGS:
        staged = _stage_platform_pkg(_pkg)
        if staged is not None:
            out.append((staged, f"platform/{_pkg}"))
    return out

# 动态 import 的包，PyInstaller 静态分析抓不全 → 显式 collect。
COLLECT_SUBMODULES = ["src", "uvicorn", "pyrogram", "fastapi"]
# uvicorn 的 lifespan/loops/protocols 子模块按字符串加载；
# okline（LINE 协议登录）除子模块外还须连 ltsm/*.wasm + *.js（Node 桥资产）一并收进包，
# 否则冻结后 hmac_signer 找不到 ltsm_bridge.js → LINE 扫码在安装版恒失败（--collect-all
# 会把 collect_data_files 抓到的非 .py 数据一起打进 <_MEIPASS>/okline/ltsm/）。
COLLECT_ALL = ["uvicorn", "okline"]

# 重量级可选软依赖：默认排除以控包体（缺失时后端对应能力软降级）。
#
# 桌面端定位＝云 AI + 翻译 + 统一收件箱的轻客户端：本地 ASR/向量嵌入/视觉 OCR/
# 浏览器自动化一律走云或 LAN GPU 服务，故其重依赖不随包分发。经 grep 核验：src 全库
# 仅 3 处 import 到这些库（sentence_transformers / faster_whisper，且**全部函数内惰性
# import**），其余（ctranslate2/av/transformers/scipy/sklearn/onnxruntime/numba/llvmlite/
# pyarrow/pandas/playwright…）都是这两个「根」的传递依赖——排除它们不碰后端启动链，
# 缺失时对应可选功能在惰性 import 处软降级（已有 try/except）。
# 保留：numpy（众多库基础依赖）/ jieba（中文分词，KB 可能用）/ PIL（收件箱图片）。
EXCLUDES = [
    # 本地 ASR / 音频声学（桌面走云或 LAN GPU faster-whisper 服务，不做本地转写/分析）
    "whisper", "faster_whisper", "ctranslate2", "av", "librosa", "soundfile",
    "torch", "torchaudio",
    # 本地向量嵌入 / ML / 评测（桌面 embedding 走云 API；eval/训练不随桌面分发）
    "sentence_transformers", "transformers", "tokenizers", "onnxruntime",
    "scipy", "sklearn", "numba", "llvmlite", "datasets", "pyarrow", "pandas",
    # 视觉 OCR / 绘图 / GUI（桌面图像走云 Vision；无需本地 OCR/CV/绘图/Tk）
    "easyocr", "cv2", "matplotlib", "tkinter", "imageio", "imageio_ffmpeg",
    # 浏览器自动化（桌面端不跑网页抓取 / RPA）
    "playwright",
]


def _sep() -> str:
    return ";" if os.name == "nt" else ":"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true", help="打包前清空产出与缓存")
    ap.add_argument("--onefile", action="store_true", help="单文件模式（更慢、首启解压；默认 onedir 更稳）")
    ap.add_argument("--keep-heavy", action="store_true", help="不排除 whisper/torch 等重依赖")
    args = ap.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("✗ 未安装 PyInstaller。请先：pip install pyinstaller", file=sys.stderr)
        return 2

    if args.clean:
        for d in (OUT, HERE / "build", HERE / "__pycache__", STATIC_STAGED,
                  DOMAINS_STAGED, PLATFORM_STAGED):
            shutil.rmtree(d, ignore_errors=True)

    OUT.mkdir(parents=True, exist_ok=True)

    datas = _staged_datas() + DATAS

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--name", NAME,
        "--distpath", str(OUT),
        "--workpath", str(HERE / "build"),
        "--specpath", str(HERE),
        "--paths", str(REPO),
        ("--onefile" if args.onefile else "--onedir"),
        "--console",
    ]
    for mod in COLLECT_SUBMODULES:
        cmd += ["--collect-submodules", mod]
    for mod in COLLECT_ALL:
        cmd += ["--collect-all", mod]
    if not args.keep_heavy:
        for mod in EXCLUDES:
            cmd += ["--exclude-module", mod]
    for src, dst in datas:
        if Path(src).exists():
            cmd += ["--add-data", f"{src}{_sep()}{dst}"]
        else:
            print(f"  · 跳过不存在的数据：{src}")

    cmd.append(str(REPO / "main.py"))

    print("→ PyInstaller:\n  " + " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(REPO))
    if proc.returncode != 0:
        print("✗ 打包失败", file=sys.stderr)
        return proc.returncode

    # onedir 模式产出 backend-dist/backend/backend(.exe)；electron-builder 取整个 backend-dist。
    # 这里把 onedir 的内层目录提平，使 resources/backend/backend(.exe) 路径与 launcher 解析一致。
    inner = OUT / NAME
    exe = (NAME + ".exe") if os.name == "nt" else NAME
    if not args.onefile and inner.is_dir():
        for item in inner.iterdir():
            target = OUT / item.name
            if target.exists():
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink()
            shutil.move(str(item), str(target))
        shutil.rmtree(inner, ignore_errors=True)

    final = OUT / exe
    if not final.exists():
        print(f"⚠ 产出未在预期路径：{OUT}（请检查 PyInstaller 输出）", file=sys.stderr)
        return 1

    # 源码指纹戳：predist 用它拦「改了 src 却直接 dist」的陈旧 sidecar
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        from backend_source_fingerprint import compute_fingerprint, write_stamp
        fp = compute_fingerprint(REPO, datas=list(DATAS))
        stamp = write_stamp(OUT, fp)
        print(f"✓ 源码指纹已写入 {stamp.name} ({fp['aggregate'][:16]}… files={fp['file_count']})")
    except Exception as e:
        print(f"✗ 写入源码指纹失败（拒绝产出无戳 backend-dist）: {e}", file=sys.stderr)
        return 1

    print(f"✓ 完成：{final}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

