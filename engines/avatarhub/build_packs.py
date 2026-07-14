# -*- coding: utf-8 -*-
"""
build_packs.py — 分发包构建流水线（把 conda 环境冻结成可重定位归档 + 生成 manifest）

定位：替代「在用户机 pip 现装」（provision --create）的脆弱路径，改为在本机（或 CI）
      一次性把各 conda 环境用 conda-pack 冻结成可重定位 tar.gz，附 sha256 / 体积，
      产出 manifest.json。用户机的下载器据 manifest 按档位下载 → 解压 → conda-unpack → 即用，
      全程无需 conda、无需联网 pip。

为什么可行（已 PoC 实测，cosytts 5.28GB→3GB/41.9s，异路径 torch+cu128/CUDA 跑通）：
  本项目 app_config.py 用「直接 python.exe 绝对路径」探测解释器，而非 conda activate，
  所以解压出的环境只要 python.exe 在、跑过一次 conda-unpack 即可，用户根本不用装 conda。

关键工程坑（务必带下列开关，否则真实环境会因 conda/pip 文件冲突中止）：
  ignore_missing_files=True, ignore_editable_packages=True
  —— 真实环境里 pip 覆盖过 conda 管理的文件（wheel 等），裸 conda-pack 会报错。

用法（用 base 解释器跑，conda-pack 装在 base）：
  python build_packs.py                      # 体检：列出将打包的环境与体积（不实际打包）
  python build_packs.py --survey-models      # 勘察模型组子项体积/拆分候选（指导 split 配置）
  python build_packs.py --build              # 打包所有产品环境 + 生成 manifest
  python build_packs.py --build --only cosytts facefusion
  python build_packs.py --build --include-models   # 追加打包模型目录（大，默认关）
  python build_packs.py --build --version 1.2.0 --base-url https://dl.example.com/avatarhub/1.2.0
  python build_packs.py --build --force      # 重打已存在的包

产物：
  dist/packs/<env>-<arch_tag>.tar.gz         # 各环境可重定位归档
  dist/packs/model-<group>.tar.gz            # 各模型组归档（--include-models）
  dist/manifest.json                         # 版本/分档/各组件 file+sha256+size
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import app_config  # 纯标准库，复用其 CONDA_ROOT / SERVICES 探测

# ── 产品环境（来自 SERVICES 派生，外加 rvc=XTTS）；磁盘上的实验环境不在此列 ──
PRODUCT_ENVS = sorted({s["env"] for s in app_config.SERVICES.values()} | {"rvc"})

# 各环境 python 版本（仅用于命名/记录；ML 栈对版本敏感）。
ENV_PY = {
    "facefusion": "3.10", "rvc": "3.10", "fishspeech": "3.10",
    "cosytts": "3.10", "musethepeak": "3.10", "latentsync": "3.10",
    "voxcpm": "3.11", "nemoasr": "3.11",
}

# ── 档位 → 组件映射（可按商业策略改）。envs/models 均为「组件 id」。 ──
#   lite     : 换脸/发型/增强 + XTTS 音色 + hub 控制台（低显存，工具向）
#   standard : + 实时数字人标清核心链（克隆音TTS + STT + 口型 + 广播）
#   flagship : + 高清口型/活体（LatentSync）
EDITIONS = {
    "lite": {
        "label": "Lite·幻颜/幻声（入门，6–8GB 显存）",
        "envs": ["facefusion", "rvc"],
        "models": ["facefusion", "swapcore", "gfpgan", "rvc_app", "rvc_weights"],
    },
    "standard": {
        "label": "标准·幻影256（实时数字人标清，建议 4090）",
        "envs": ["facefusion", "rvc", "fishspeech", "cosytts", "musethepeak"],
        "models": ["facefusion", "swapcore", "gfpgan", "rvc_app", "rvc_weights", "cosyvoice", "musetalk", "fishspeech"],
    },
    "flagship": {
        "label": "旗舰·幻影HD（高清/活体 25fps，建议 5090）",
        "envs": ["facefusion", "rvc", "fishspeech", "cosytts", "musethepeak", "latentsync"],
        "models": ["facefusion", "swapcore", "gfpgan", "rvc_app", "rvc_weights", "cosyvoice", "musetalk",
                   "fishspeech", "musetalk_hd", "latentsync", "liveportrait"],
    },
}

# ── 模型组 → 相对项目根路径（与磁盘实测对齐）。每组：paths=要打包的目录/文件；
#   exclude=要从打包中剔除的子路径（避免重复打包 / 让标准档不带 HD 权重）。仅打包存在的路径。──
MODEL_GROUPS = {
    "facefusion":   {"paths": ["facefusion/.assets/models"]},
    # 换脸核心权重：faceswap_api 直读的固定路径（不在 facefusion/.assets 里！）。
    # 1.0.4 首发漏了这组 → 客户机换脸引擎起来了但一个模型都加载不到（198 Lite 实锤）：
    #   inswapper_128  基线换脸核（8003 容灾副本恒用它，缺失=副本空转）
    #   hyperswap_1a   主引擎 HD 核（_detect_hyperswap 在 BASE/models 自动发现）
    #   gpen_bfr_256   直播链路轻精修
    #   face_landmarker / selfie_segmenter / rvm  妆容与背景替换的小模型（有自动下载兜底，
    #                                              但客户机网络不可控，随包带上才是「即装即用」）
    #   buffalo_l      insightface 检测/识别 5 模型（2026-07-13 140 装机复盘：不随包时
    #                  客户机首启要现场从 GitHub 下载 ~340MB，国内网络大概率失败=检测器
    #                  加载不了。faceswap_api 已改为优先读 BASE\models\buffalo_l）
    "swapcore":     {"paths": ["Deep-Live-Cam/models/inswapper_128.onnx",
                               "models/hyperswap_1a_256.onnx",
                               "models/gpen_bfr_256.onnx",
                               "models/face_landmarker.task",
                               "models/selfie_segmenter_landscape.tflite",
                               "models/rvm_mobilenetv3_fp16.torchscript",
                               "models/buffalo_l"]},
    "gfpgan":       {"paths": ["GFPGANv1.4.pth"]},
    # RVC 变声引擎本体：api_240604/gui_v1 + infer/configs/tools 代码 + hubert/rmvpe 基座模型。
    # 1.0.4 首发漏了它 → 客户机点「开播」到启动变声一步必失败：
    #   Popen(cwd=Retrieval-based-Voice-Conversion-WebUI) 目录不存在 = WinError 267（198 实锤）。
    # weights 单独成包（rvc_app 改版不用重下 3.5GB 音色）。
    "rvc_app":      {"paths": ["Retrieval-based-Voice-Conversion-WebUI"],
                     "exclude": ["Retrieval-based-Voice-Conversion-WebUI/assets/weights",
                                 "Retrieval-based-Voice-Conversion-WebUI/assets/indices",
                                 "Retrieval-based-Voice-Conversion-WebUI/logs",
                                 "Retrieval-based-Voice-Conversion-WebUI/__pycache__",
                                 "Retrieval-based-Voice-Conversion-WebUI/TEMP"]},
    # 音色库：装到 WebUI 自己的 assets/weights —— hub 的 RVC_WEIGHTS_DIR/变声引擎都只认这里。
    # 旧路径 RVC/assets/weights 是打包时抄错的目录（hub 根本不读）→ 客户机 66 个音色全部“隐身”。
    "rvc_weights":  {"paths": ["Retrieval-based-Voice-Conversion-WebUI/assets/weights"]},
    "cosyvoice":    {"paths": ["CosyVoice/pretrained_models"]},
    # MuseTalk/models 已含 musetalkV15(HD)；基座组排除它，HD 单列，避免重复 + 标准档不白下 HD。
    "musetalk":     {"paths": ["MuseTalk/models"], "exclude": ["MuseTalk/models/musetalkV15"]},
    "musetalk_hd":  {"paths": ["MuseTalk/models/musetalkV15"]},
    "fishspeech":   {"paths": ["fish-speech/checkpoints"]},
    # LatentSync 旗舰专属 6.4G：4.72G 主权重单列，仅它变更时增量更新免重下其余 ~1.7G。
    # split 的子路径须为某 paths 目录的「直接子项」；未列入的子项自动归入 <group>__rest。
    "latentsync":   {"paths": ["LatentSync/checkpoints"],
                     "split": {"unet": ["LatentSync/checkpoints/latentsync_unet.pt"]}},
    "liveportrait": {"paths": ["LivePortrait/pretrained_weights"]},
    "hairfastgan":  {"paths": ["HairFastGAN/pretrained_models"]},
}

# ── 共享基座（--shared）：把跨环境重复的大二进制（torch/CUDA 运行库）抽成「只下一次」的
#   内容寻址包；各环境包 conda-pack 时 exclude 掉这些文件，装期再从基座硬链回各环境。
#   实测：cosytts 环境包从 ~3GB 降到 451MB，排除的 37 个 DLL 合计 4GB（详见方案文档 B-2）。──
SHARED_SPECS = {
    "torch-cuda": {
        "label": "torch/CUDA/ONNX 等共享运行库（pip 二进制、不含 conda 前缀，跨环境只下一次）",
        # 仅纳入「跨环境字节一致即证明不含环境前缀」的 pip 二进制目录（conda-unpack 不改它们 → 硬链共享安全）。
        "patterns": [
            "Lib/site-packages/torch/lib/*.dll",
            "Lib/site-packages/torch/lib/*.lib",
            "Lib/site-packages/onnxruntime/capi/*.dll",
            "Lib/site-packages/llvmlite/binding/*.dll",
            "Lib/site-packages/cv2/*.pyd",
            "Lib/site-packages/imageio_ffmpeg/binaries/*",
        ],
    },
}

# ── 分发期直接丢弃（--shared 时生效）：运行期用不到的死重量，丢弃比去重更省（去全部而非仅副本）。──
#   ~* ：pip 安装中断/卸载残留目录（如 ~orch），Python 不会导入；
#   torch_cu118_bak ：旧 cu118 torch 手工备份，运行期 import torch 走的是 torch，不碰它；
#   *.pdb ：MSVC 调试符号，运行期不需。丢弃只影响分发包，不动开发机环境（构建后烟测 import 验证）。
DROP_PATTERNS = [
    "Lib/site-packages/~*",
    "Lib/site-packages/torch_cu118_bak/*",
    "*.pdb",
]

HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / "dist" / "packs"
MANIFEST_PATH = HERE / "dist" / "manifest.json"


def _human(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024 or unit == "TB":
            return f"{f:.2f}{unit}" if unit != "B" else f"{int(f)}B"
        f /= 1024
    return f"{f:.2f}TB"


def _sha256(path: Path, buf: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(buf), b""):
            h.update(chunk)
    return h.hexdigest()


def _dir_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


def _env_prefix(env: str) -> Path:
    """环境目录（python.exe 的父目录）。"""
    py = Path(app_config.conda_python(env))
    return py.parent


def _require_conda_pack():
    try:
        import conda_pack  # noqa: F401
    except ImportError:
        print("[ERROR] 未找到 conda-pack。请用 base 解释器安装：")
        print('        "%s" -m pip install conda-pack' % sys.executable)
        sys.exit(2)


def _repack_tar_to_zst(src_tar: Path, out_zst: Path):
    """把 conda-pack 产出的未压缩 tar 以可复现方式重写为 zstd 包：成员按名排序 + 固定
    mtime/uid/gid → 同内容同字节（再发布时未变环境的 sha 不变，更新可跳过免重下），并用 L19+LDM
    比 gzip 更小、解压更快。保留文件 mode（脚本可执行位）与链接。"""
    import tarfile
    import zstandard
    params = zstandard.ZstdCompressionParameters.from_level(
        ZSTD_LEVEL, threads=ZSTD_THREADS, enable_ldm=True, window_log=ZSTD_WINDOW_LOG)
    tmp = out_zst.with_suffix(out_zst.suffix + ".tmp")
    with tarfile.open(src_tar, "r:") as tin:
        members = sorted(tin.getmembers(), key=lambda m: m.name)
        with open(tmp, "wb") as raw:
            comp = zstandard.ZstdCompressor(compression_params=params).stream_writer(raw, closefd=False)
            try:
                # GNU 格式 + 清 pax 扩展头：避免 conda-pack 读文件时更新的 atime/ctime 等
                # 经 PAX 扩展头泄漏进归档（同长度不同字节）→ 否则可复现失败。
                with tarfile.open(fileobj=comp, mode="w", format=tarfile.GNU_FORMAT) as tout:
                    for m in members:
                        m.mtime = 0
                        m.uid = m.gid = 0
                        m.uname = m.gname = ""
                        m.pax_headers.clear()
                        if m.isfile():
                            tout.addfile(m, tin.extractfile(m))
                        else:
                            tout.addfile(m)
            finally:
                comp.close()
    tmp.replace(out_zst)


# ── 环境打包 ──────────────────────────────────────────────────────
def pack_env(env: str, arch_tag: str, force: bool, filters=None) -> dict | None:
    import conda_pack
    prefix = _env_prefix(env)
    if not prefix.exists():
        print("  [skip] %-14s 环境不存在：%s" % (env, prefix))
        return None
    out = OUT_DIR / f"{env}-{arch_tag}.tar.zst"
    if out.exists() and not force:
        print("  [have] %-14s 已存在，跳过打包（--force 重打）：%s" % (env, out.name))
    else:
        src_gb = _human(_dir_size(prefix))
        ex_note = "（排除共享库）" if filters else ""
        print("  [pack] %-14s (%s)%s → %s …" % (env, src_gb, ex_note, out.name), flush=True)
        t0 = time.time()
        # 关键：忽略 conda/pip 文件冲突与 editable 包，否则真实环境会中止。
        # filters=[("exclude", pat)…]：--shared 时把 torch/CUDA 大库排除（改由共享基座下发）。
        # conda-pack 先出未压缩 tar（保留 conda-unpack 机制），再由我们可复现地重写为 zstd。
        tmp_tar = OUT_DIR / f"{env}-{arch_tag}.tar"
        conda_pack.pack(
            prefix=str(prefix), output=str(tmp_tar), format="tar", n_threads=-1, force=True,
            ignore_missing_files=True, ignore_editable_packages=True,
            filters=filters,
        )
        _repack_tar_to_zst(tmp_tar, out)
        tmp_tar.unlink(missing_ok=True)
        print("         done %s in %.1fs" % (_human(out.stat().st_size), time.time() - t0))
    return {
        "kind": "conda-env",
        "env": env,
        "python": ENV_PY.get(env, "3.10"),
        "file": f"packs/{out.name}",
        "size_bytes": out.stat().st_size,
        "size_human": _human(out.stat().st_size),
        "sha256": _sha256(out),
        "unpack": "conda-unpack",  # 解压后须执行 Scripts\\conda-unpack.exe 修正前缀
    }


# ── 共享基座分析/打包（--shared）──────────────────────────────────
def analyze_shared(envs: list[str]) -> tuple[dict, dict]:
    """扫描各环境，按 SHARED_SPECS 的 pattern 匹配出可共享的大库，内容寻址（按 sha 去重）。
    返回 (blobs, placements)：
      blobs[sid] = {sha: (size, 代表源文件路径)}；用于打基座包（每个 sha 只存一份）。
      placements[env] = [(relpath, sha, size, sid), …]；用于装期把基座 blob 硬链回该环境对应路径。"""
    blobs: dict[str, dict] = {sid: {} for sid in SHARED_SPECS}
    placements: dict[str, list] = {e: [] for e in envs}
    for env in envs:
        prefix = _env_prefix(env)
        if not prefix.exists():
            continue
        for p in prefix.rglob("*"):
            try:
                if not (p.is_file() and not p.is_symlink()):
                    continue
            except OSError:
                continue
            rel = str(p.relative_to(prefix)).replace("\\", "/")
            for sid, spec in SHARED_SPECS.items():
                if any(fnmatch.fnmatch(rel, pat) for pat in spec["patterns"]):
                    try:
                        sz = p.stat().st_size
                    except OSError:
                        break
                    sha = _sha256(p)
                    blobs[sid].setdefault(sha, (sz, p))
                    placements[env].append((rel, sha, sz, sid))
                    break
    return blobs, placements


# 共享分片压缩参数（zstd）：L19 + 长程匹配，比 gzip 小 28~40% 且解压 0.3~1.2s。
# 固定 threads（而非 -1）以保证「同内容同字节」可复现（多线程 zstd 的分帧取决于线程数）。
ZSTD_LEVEL = 19
ZSTD_THREADS = min(8, os.cpu_count() or 4)
ZSTD_WINDOW_LOG = 27


def _reproducible_tar(out: Path, members: list[tuple[str, Path]], codec: str = "zst"):
    """可复现打包：固定 mtime/uid/gid/权限 + 按 arcname 排序 + 无时间戳压缩。
    → 内容相同则字节相同、sha 相同，这是「分片增量更新」（未变分片免重下）的前提。
    codec="zst"（默认，zstd L19+LDM）或 "gz"（gzip mtime=0 回退）。"""
    import tarfile
    tmp = out.with_suffix(out.suffix + ".tmp")
    with open(tmp, "wb") as raw:
        if codec == "zst":
            import zstandard
            params = zstandard.ZstdCompressionParameters.from_level(
                ZSTD_LEVEL, threads=ZSTD_THREADS, enable_ldm=True, window_log=ZSTD_WINDOW_LOG)
            comp = zstandard.ZstdCompressor(compression_params=params).stream_writer(raw, closefd=False)
        else:
            import gzip
            comp = gzip.GzipFile(fileobj=raw, mode="wb", mtime=0)
        try:
            with tarfile.open(fileobj=comp, mode="w") as tar:
                for arcname, src in sorted(members):
                    ti = tar.gettarinfo(str(src), arcname=arcname)
                    ti.mtime = 0
                    ti.uid = ti.gid = 0
                    ti.uname = ti.gname = ""
                    ti.mode = 0o644
                    with open(src, "rb") as f:
                        tar.addfile(ti, f)
        finally:
            comp.close()
    tmp.replace(out)


# 共享基座分片粒度（混合稳定分桶，blob→分片仅取决于其自身 sha+size，故未变 blob 永不迁移）：
#  - 大 blob（≥SHARED_BIG_BLOB）各自独占一片 → 大库改一处只重下那一个，且不会有「巨片」。
#  - 小 blob 按 sha 前 SHARED_SHARD_HEX 位十六进制分桶 → 控制文件数（长尾不碎成几百个小包）。
SHARED_SHARD_HEX = 1
SHARED_BIG_BLOB = 256 * 1024 * 1024


def _shard_suffix(sha: str, size: int) -> str:
    return f"big.{sha[:12]}" if size >= SHARED_BIG_BLOB else sha[:SHARED_SHARD_HEX]


def shard_id_of(sid: str, sha: str, size: int) -> str:
    return f"{sid}.{_shard_suffix(sha, size)}"


def build_shared_shards(sid: str, sha_src: dict, arch_tag: str, force: bool) -> list[tuple[str, dict]]:
    """把共享组按混合稳定规则分桶为多个可复现分片包（每片 runtime/_store/<sha>）。
    分片让「torch 小升级只变少数 blob」时仅重下受影响分片，而非整个基座。返回 [(shard_id, info)…]。"""
    if not sha_src:
        return []
    buckets: dict[str, dict] = {}
    for sha, (sz, src) in sha_src.items():
        buckets.setdefault(_shard_suffix(sha, sz), {})[sha] = (sz, src)
    results: list[tuple[str, dict]] = []
    for bkey in sorted(buckets):
        sub = buckets[bkey]
        shard = f"{sid}.{bkey}"
        out = OUT_DIR / f"shared-{sid}-{bkey}-{arch_tag}.tar.zst"
        if out.exists() and not force:
            print("  [have] shared-%-14s 已存在，跳过：%s" % (shard, out.name))
        else:
            total = _human(sum(sz for sz, _ in sub.values()))
            print("  [pack] shared-%-14s (%d blob / %s) → %s …"
                  % (shard, len(sub), total, out.name), flush=True)
            t0 = time.time()
            _reproducible_tar(out, [(f"runtime/_store/{sha}", src) for sha, (_sz, src) in sub.items()], codec="zst")
            print("         done %s in %.1fs" % (_human(out.stat().st_size), time.time() - t0))
        results.append((shard, {
            "kind": "shared",
            "shared_id": sid,
            "shard": bkey,
            "label": SHARED_SPECS[sid].get("label", sid),
            "blobs": {sha: sz for sha, (sz, _) in sub.items()},
            "file": f"packs/{out.name}",
            "size_bytes": out.stat().st_size,
            "size_human": _human(out.stat().st_size),
            "sha256": _sha256(out),
            "unpack": "extract",   # 解压到项目根 → BASE/runtime/_store/<sha>
        }))
    return results


# ── 模型打包（可选，大）──────────────────────────────────────────
def _excluded_abs(p: Path, excludes: list[str]) -> bool:
    full = str(p.resolve())
    return any(full == ex or full.startswith(ex + os.sep) for ex in excludes)


def _pack_paths(out_name: str, abs_paths: list[Path], excludes: list[str],
                force: bool, label: str) -> dict | None:
    """把若干绝对路径（目录/文件）以可复现 zstd 打成包，arcname 保留相对项目根的路径，
    于是解压回项目根即精确还原原树。excludes 命中的子树会被剔除。返回组件 dict 或 None。"""
    existing = [p for p in abs_paths if p.exists()]
    if not existing:
        return None
    out = OUT_DIR / out_name
    if out.exists() and not force:
        print("  [have] %-20s 已存在，跳过：%s" % (label, out.name))
    else:
        members: list[tuple[str, Path]] = []           # (arcname 相对项目根, 源文件)
        for p in existing:
            if p.is_file():
                if not _excluded_abs(p, excludes):
                    members.append((str(p.relative_to(HERE)).replace("\\", "/"), p))
            else:
                for f in sorted(p.rglob("*")):
                    try:
                        if f.is_file() and not f.is_symlink() and not _excluded_abs(f, excludes):
                            members.append((str(f.relative_to(HERE)).replace("\\", "/"), f))
                    except OSError:
                        continue
        total = _human(sum(_dir_size(p) for p in existing))
        ex_note = (" 排除 %d 项" % len(excludes)) if excludes else ""
        print("  [pack] %-20s (%s%s) → %s …" % (label, total, ex_note, out.name), flush=True)
        t0 = time.time()
        _reproducible_tar(out, members, codec="zst")   # 可复现 + zstd（同 B-5/B-6）
        print("         done %s in %.1fs" % (_human(out.stat().st_size), time.time() - t0))
    return {
        "kind": "model",
        "members": [str(p.relative_to(HERE)).replace("\\", "/") for p in existing],
        "file": f"packs/{out.name}",
        "size_bytes": out.stat().st_size,
        "size_human": _human(out.stat().st_size),
        "sha256": _sha256(out),
        "unpack": "extract",  # 直接解压到项目根（保留相对路径）
    }


def pack_model_components(group: str, force: bool) -> list[tuple[str, dict]]:
    """打包一个模型组，返回 [(component_id, info), ...]。
    - 无 split：整组一个组件，id=group（向后兼容）。
    - 有 split：每个 tag 一个子组件 id=f"{group}__{tag}"，未覆盖的「直接子项」归入
      id=f"{group}__rest"。各子件解压回项目根可拼回原树（成员路径不重叠，零丢失）。"""
    spec = MODEL_GROUPS.get(group, {})
    base_paths = [HERE / rel for rel in spec.get("paths", [])]
    if not any(p.exists() for p in base_paths):
        print("  [skip] model-%-12s 无任何路径存在，跳过" % group)
        return []
    excludes = [str((HERE / e).resolve()) for e in spec.get("exclude", [])]
    split = spec.get("split")
    if not split:
        info = _pack_paths(f"model-{group}.tar.zst", base_paths, excludes, force, f"model-{group}")
        return [(group, info)] if info else []

    results: list[tuple[str, dict]] = []
    covered: set[Path] = set()
    for tag, subrels in split.items():
        sub_abs = [HERE / s for s in subrels]
        for p in sub_abs:
            covered.add(p.resolve())
        info = _pack_paths(f"model-{group}__{tag}.tar.zst", sub_abs, excludes,
                           force, f"model-{group}__{tag}")
        if info:
            results.append((f"{group}__{tag}", info))

    # __rest：base_paths 下未被任何 tag 覆盖、且未被 exclude 的直接子项（兜底，保证零丢失）
    rest_abs: list[Path] = []
    for bp in base_paths:
        if not bp.exists():
            continue
        if bp.is_dir():
            for child in sorted(bp.iterdir()):
                if child.resolve() in covered or _excluded_abs(child, excludes):
                    continue
                rest_abs.append(child)
        elif bp.resolve() not in covered and not _excluded_abs(bp, excludes):
            rest_abs.append(bp)
    if rest_abs:
        info = _pack_paths(f"model-{group}__rest.tar.zst", rest_abs, excludes,
                           force, f"model-{group}__rest")
        if info:
            results.append((f"{group}__rest", info))
    return results


# ── 程序本体组件（app:core）：让"改一行代码"走增量更新而不是重发安装包 ─────────
#   文件集与 installer\AvatarHub.iss 的 [Files] 保持同一语义（根 *.py/*.bat 不递归 +
#   static/tools/requirements/assets 图标 + 预热脸），排除表两处同步改（互为注释锚点）。
#   打包产物内含生成的 app_build.json 版本标记：客户端 pack_installer 据此对"无安装记录"
#   的存量装机判定新旧（Inno 装出来的机器第一次见到 app 组件时做一次全量代码同步）。
APP_PY_EXCLUDES = ("test_*.py", "run_all_tests.py", "build_packs.py", "make_portable.py",
                   "make_release.py", "pack_acceptance.py", "pack_gui_acceptance.py",
                   "gate.py", "make_manual.py", "license_admin.py", "license_server.py", "_*.py")
APP_BAT_EXCLUDES = ("secrets.bat", "build_launcher.bat", "sign_artifacts.bat",
                    "gate.bat", "env_config.bat", "deploy.env.bat")
APP_EXTRA_FILES = ("config.example.json", "_warmup_face.jpg", "assets/app.ico")
APP_TREES = ("static", "requirements",
             "data/starter_profiles")     # 启动角色包(占位形象+音色)：全新装机首启播种
# 递归整树；tools 单独(仅 *.py 且剔 _*)


def _match_any(name: str, pats) -> bool:
    return any(fnmatch.fnmatch(name.lower(), p.lower()) for p in pats)


def iter_app_files() -> list[tuple[str, Path]]:
    """(arcname 相对项目根, 源文件) 列表；与安装器铺盘的文件集一致。"""
    members: list[tuple[str, Path]] = []
    for p in sorted(HERE.glob("*.py")):
        if p.is_file() and not _match_any(p.name, APP_PY_EXCLUDES):
            members.append((p.name, p))
    for p in sorted(HERE.glob("*.bat")):
        if p.is_file() and not _match_any(p.name, APP_BAT_EXCLUDES):
            members.append((p.name, p))
    for rel in APP_TREES:
        root = HERE / rel
        if root.is_dir():
            for f in sorted(root.rglob("*")):
                if f.is_file() and not f.is_symlink():
                    members.append((str(f.relative_to(HERE)).replace("\\", "/"), f))
    tools = HERE / "tools"
    if tools.is_dir():
        for f in sorted(tools.glob("*.py")):
            if f.is_file() and not f.name.startswith("_"):
                members.append((f"tools/{f.name}", f))
    for rel in APP_EXTRA_FILES:
        p = HERE / rel
        if p.is_file():
            members.append((rel.replace("\\", "/"), p))
    return members


def pack_app_component(app_version: str, force: bool) -> tuple[str, dict] | None:
    """打 app-<ver>.tar.zst；返回 ("core", 组件信息) 供并入 manifest.components.app。"""
    members = iter_app_files()
    if not members:
        print("  [skip] app 组件无可打包文件")
        return None
    marker = OUT_DIR / f"_app_build_{app_version}.json"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    # 标记保持【确定性】：同版本同文件集 → 同字节 → 同 sha（复现打包=增量更新判定的地基；
    # 不写 built_at，否则每次打包 sha 都变，"同版本重打却报可更新"）。
    marker.write_text(json.dumps({
        "version": app_version,
        "files": len(members),
    }, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    members = members + [("app_build.json", marker)]
    out = OUT_DIR / f"app-{app_version}.tar.zst"
    if out.exists() and not force:
        print("  [have] app-%s 已存在，跳过（--force 重打）" % app_version)
    else:
        total = sum(p.stat().st_size for _, p in members)
        print("  [pack] app-%-14s (%s, %d 文件) → %s …" % (app_version, _human(total), len(members), out.name), flush=True)
        t0 = time.time()
        _reproducible_tar(out, members, codec="zst")
        print("         done %s in %.1fs" % (_human(out.stat().st_size), time.time() - t0))
    marker.unlink(missing_ok=True)
    info = {
        "kind": "app",
        "app_version": app_version,
        "members": [a for a, _ in members if a != "app_build.json"] + ["app_build.json"],
        "file": f"packs/{out.name}",
        "size_bytes": out.stat().st_size,
        "size_human": _human(out.stat().st_size),
        "sha256": _sha256(out),
        "unpack": "app",   # 客户端专用应用流程：暂存→快照→原子覆盖→可回滚
    }
    return ("core", info)


def _maybe_sign(mp: Path):
    """写完 manifest 后就地 Ed25519 签名（有私钥+cryptography 才签；否则跳过并提示）。
    任何改 manifest 的路径都必须末尾调它——否则 sig 与内容不符会被客户端判篡改。"""
    try:
        import release_sign
    except Exception:
        print("  [sign] 跳过（无 release_sign 模块）")
        return
    try:
        fp = release_sign.sign_manifest_file(mp)
        print("  [sign] %s 已签名（公钥指纹 %s）" % (mp.name, fp))
    except Exception as e:
        print("  [sign] 未签名（%s）——发布前须补签，否则客户端强制验签会拒绝" % e)


def build_app_cli(app_version: str, force: bool, manifest_paths: list[Path]):
    """--build-app 入口：打包 + 把 app:core 并进指定 manifest（默认 dist/manifest.json）+ 重签。"""
    r = pack_app_component(app_version, force)
    if not r:
        return 1
    name, info = r
    for mp in manifest_paths:
        if not mp.exists():
            print("  [skip] manifest 不存在:", mp)
            continue
        m = json.loads(mp.read_text(encoding="utf-8"))
        m.setdefault("components", {}).setdefault("app", {})[name] = info
        mp.write_text(json.dumps(m, ensure_ascii=False, indent=2), encoding="utf-8")
        print("  [manifest] %s: components.app.%s = %s (v%s)" % (mp.name, name, info["size_human"], app_version))
        _maybe_sign(mp)      # 改了 manifest 必须重签
    return 0


# ── 体检（不打包）──────────────────────────────────────────────
def survey(envs: list[str]):
    print("=" * 64)
    print(" 分发包构建 · 体检（不打包）")
    print("=" * 64)
    print(" conda 根： %s" % (app_config.CONDA_ROOT or "未探测到"))
    print("-" * 64)
    print(" 产品环境：")
    total = 0
    for env in envs:
        prefix = _env_prefix(env)
        if prefix.exists():
            sz = _dir_size(prefix)
            total += sz
            print("   [OK] %-14s %8s   %s" % (env, _human(sz), prefix))
        else:
            print("   [--] %-14s %8s   缺失：%s" % (env, "-", prefix))
    print("-" * 64)
    print(" 环境源体积合计： %s（压缩后约 55–60%%）" % _human(total))
    print(" 档位组件映射：")
    for ed, spec in EDITIONS.items():
        print("   - %-9s envs=%s" % (ed, ",".join(spec["envs"])))
    print("=" * 64)
    print(" 实际打包请加 --build")


def survey_envs(top: int = 18):
    """量化 6 个产品环境包之间的「跨环境重复字节」，给出可去重 GB 硬数字与最大重复文件 Top。
    性能：先按文件大小分组，只对「同尺寸 ≥2 个」的候选做 sha256（独尺寸文件必唯一，免哈希）。
    输出三类数：① 各环境/合计源体积；② 全局内容去重可省（上界）；③ 其中跨环境组贡献（共享基座机会）。"""
    from collections import defaultdict
    from concurrent.futures import ThreadPoolExecutor
    print("=" * 70)
    print(" 环境包跨环境去重勘察（torch/CUDA 等共享运行库冗余量化，不打包）")
    print("=" * 70)
    files: list[tuple[str, Path, str, int]] = []
    per_env: dict[str, int] = {}
    for env in PRODUCT_ENVS:
        prefix = _env_prefix(env)
        if not prefix.exists():
            print(" [--] %-14s 缺失：%s" % (env, prefix))
            continue
        t = n = 0
        for p in prefix.rglob("*"):
            try:
                if p.is_file() and not p.is_symlink():
                    sz = p.stat().st_size
                    rel = str(p.relative_to(prefix)).replace("\\", "/")
                    files.append((env, p, rel, sz))
                    t += sz
                    n += 1
            except OSError:
                pass
        per_env[env] = t
        print(" [OK] %-14s %10s  (%d 文件)" % (env, _human(t), n))
    total_bytes = sum(per_env.values())
    if not files:
        print(" 无可勘察环境。")
        return

    def _shared_match(rel: str) -> bool:
        return any(fnmatch.fnmatch(rel, pat)
                   for spec in SHARED_SPECS.values() for pat in spec["patterns"])

    by_size: dict[int, list[tuple[str, Path, str]]] = defaultdict(list)
    for env, p, rel, sz in files:
        by_size[sz].append((env, p, rel))
    cand = [(env, p, rel, sz) for sz, lst in by_size.items() if len(lst) > 1 and sz > 0
            for (env, p, rel) in lst]
    cand_bytes = sum(sz for _, _, _, sz in cand)
    print("-" * 70)
    print(" 候选（同尺寸≥2，需哈希）：%d 文件 / %s；正在 sha256 …" % (len(cand), _human(cand_bytes)), flush=True)

    def _h(item):
        env, p, rel, sz = item
        try:
            return (env, rel, sz, _sha256(p))
        except OSError:
            return None

    by_sha: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    with ThreadPoolExecutor(max_workers=8) as ex:
        for r in ex.map(_h, cand):
            if r:
                env, rel, sz, sha = r
                by_sha[sha].append((env, rel, sz))

    global_saved = 0       # 全局内容去重可省（每个 sha 只留 1 份）
    cross_saved = 0        # 跨环境组贡献（distinct env ≥2 的 sha 组的可省字节）
    cross_shared = 0       # 跨环境组「保留 1 份共享」所需的基座字节
    pat_saved = pat_pack = 0    # 当前 SHARED_SPECS pattern 覆盖的：可省 / 基座未压缩体积
    other_saved = 0             # 跨环境可省但 pattern 未覆盖（扩面候选）
    rows = []              # (saved, sz, n, nenv, envs)
    for sha, lst in by_sha.items():
        if len(lst) <= 1:
            continue
        sz = lst[0][2]
        n = len(lst)
        saved = (n - 1) * sz
        global_saved += saved
        envs = sorted({e for e, _, _ in lst})
        if len(envs) >= 2:
            cross_saved += saved
            cross_shared += sz
            matched = _shared_match(lst[0][1])
            if matched:
                pat_saved += saved
                pat_pack += sz
            else:
                other_saved += saved
            rows.append((saved, sz, n, len(envs), envs, lst[0][1], matched))

    print("=" * 70)
    print(" 源体积合计（未压缩）：           %s" % _human(total_bytes))
    print(" 全局内容去重 → 唯一字节：        %s" % _human(total_bytes - global_saved))
    print(" 全局内容去重可省（上界）：       %s  (%.1f%%)"
          % (_human(global_saved), 100.0 * global_saved / total_bytes if total_bytes else 0))
    print(" └ 跨环境组贡献（共享基座机会）： %s  (%.1f%%)"
          % (_human(cross_saved), 100.0 * cross_saved / total_bytes if total_bytes else 0))
    print("   抽出共享基座需占：             %s（每份重复内容只存 1 份）" % _human(cross_shared))
    print("-" * 70)
    print(" 当前 --shared pattern 覆盖情况（torch/lib/*.dll,*.lib）：")
    print("   pattern 命中可省：             %s  (占跨环境 %.1f%%)"
          % (_human(pat_saved), 100.0 * pat_saved / cross_saved if cross_saved else 0))
    print("   → 基座包未压缩体积约：         %s（只下一次）" % _human(pat_pack))
    print("   pattern 未覆盖的跨环境可省：   %s（扩面候选；为 0 即已吃满）" % _human(other_saved))
    print("-" * 70)
    print(" 最大跨环境重复文件 Top%d（按可省字节）：" % top)
    for saved, sz, n, nenv, envs, rel, matched in sorted(rows, reverse=True)[:top]:
        flag = "✓" if matched else " "
        print("   省 %9s  单份 %9s  ×%d/%denv %s %s"
              % (_human(saved), _human(sz), n, nenv, flag, rel))
    other_rows = [r for r in rows if not r[6]]
    if other_rows:
        print("-" * 70)
        print(" pattern 未覆盖的跨环境重复 Top%d（扩面候选，按可省字节）：" % top)
        for saved, sz, n, nenv, envs, rel, _m in sorted(other_rows, reverse=True)[:top]:
            print("   省 %9s  单份 %9s  ×%d/%denv  %s"
                  % (_human(saved), _human(sz), n, nenv, rel))
    print("=" * 70)
    print(" 解读：✓=当前 --shared 已覆盖；跨环境贡献越大→越值得抽共享基座/装期硬链去重。")


def survey_models():
    """勘察各模型组的子目录体积与拆分候选（指导是否/如何配置 split），不打包。
    把「先摸清磁盘结构再决定拆分」固化为常驻能力——模型随版本变化时复用。"""
    print("=" * 64)
    print(" 模型组勘察 · 子项体积 + 拆分候选（不打包）")
    print("=" * 64)
    seen: dict[str, list[tuple[str, int]]] = {}   # 子项名 → [(组, 体积)]，用于跨组同名提示
    for group, spec in MODEL_GROUPS.items():
        base_paths = [HERE / rel for rel in spec.get("paths", [])]
        existing = [p for p in base_paths if p.exists()]
        if not existing:
            print(" [--] %-12s 缺失（%s）" % (group, ",".join(spec.get("paths", []))))
            continue
        total = sum(_dir_size(p) for p in existing)
        split = spec.get("split")
        tag = (" [split: %s]" % ",".join(split)) if split else ""
        print("-" * 64)
        print(" %-12s %10s%s" % (group, _human(total), tag))
        for bp in existing:
            if bp.is_dir():
                for child in sorted(bp.iterdir(), key=lambda x: -_dir_size(x)):
                    sz = _dir_size(child)
                    print("     %10s  %s  %s" % (_human(sz), "d" if child.is_dir() else "f", child.name))
                    seen.setdefault(child.name, []).append((group, sz))
            else:
                print("     %10s  f  %s   (单文件，不可安全拆)" % (_human(_dir_size(bp)), bp.name))
    dup = {k: v for k, v in seen.items() if len({g for g, _ in v}) > 1}
    if dup:
        print("=" * 64)
        print(" 跨组同名子项（去重前务必核验字节是否一致；序列化格式不同则不可去重）：")
        for name, lst in sorted(dup.items()):
            print("   %-24s %s" % (name, ", ".join("%s(%s)" % (g, _human(s)) for g, s in lst)))
    print("=" * 64)
    print(" 提示：split 子路径须为某 paths 目录的「直接子项」；未列入的子项自动归入 <group>__rest，零丢失。")


def build(args):
    # 只打模型时无需 conda-pack；只在要打环境时才校验。
    envs = args.only if args.only is not None else PRODUCT_ENVS
    do_models = args.include_models or bool(args.only_models)
    if envs:
        _require_conda_pack()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("=" * 64)
    print(" 分发包构建 v%s  arch=%s" % (args.version, args.arch_tag))
    print("=" * 64)

    # 共享基座：先分析跨环境大库 → 打基座包 → 给环境包配 exclude filter。
    shared_components: dict[str, dict] = {}
    placements: dict[str, list] = {}
    env_filters = None
    if envs and args.shared:
        print(" 共享基座分析（torch/CUDA 大库内容寻址去重 + 可复现分片）：")
        blobs, placements = analyze_shared(envs)
        for sid, sha_src in blobs.items():
            for shard_id, info in build_shared_shards(sid, sha_src, args.arch_tag, args.force):
                shared_components[shard_id] = info
        # 环境包排除 = 直接丢弃的死重量 + 改由共享基座下发的库。
        env_filters = [("exclude", pat) for pat in DROP_PATTERNS]
        env_filters += [("exclude", pat)
                        for spec in SHARED_SPECS.values() for pat in spec["patterns"]]
        print("   丢弃死重量：%s" % "、".join(DROP_PATTERNS))

    env_components: dict[str, dict] = {}
    if envs:
        print(" 环境包：")
        for env in envs:
            info = pack_env(env, args.arch_tag, args.force, filters=env_filters)
            if info:
                if args.shared:
                    pl = placements.get(env, [])
                    info["placements"] = [[rel, sha] for (rel, sha, _sz, _sid) in pl]
                    # 该环境涉及哪些分片 = 其各 blob 按 sha+size 落入的分片 id 集合。
                    info["needs_shared"] = sorted({shard_id_of(sid, sha, sz) for (_r, sha, sz, sid) in pl})
                env_components[env] = info

    model_components: dict[str, dict] = {}
    built_groups: dict[str, list[str]] = {}
    if do_models:
        print(" 模型包：")
        all_groups = sorted({g for spec in EDITIONS.values() for g in spec["models"]})
        groups = args.only_models if args.only_models else all_groups
        for g in groups:
            comps = pack_model_components(g, args.force)
            for cid, info in comps:
                model_components[cid] = info
            built_groups[g] = [cid for cid, _ in comps]

    # 程序本体组件（全量构建时随发布版本号一并产出，避免重写 manifest 时把 app 丢掉）
    app_components: dict[str, dict] = {}
    _app = pack_app_component(args.version, args.force)
    if _app:
        app_components[_app[0]] = _app[1]

    # 档位 models 展开为实际构建出的组件 id：split 组→其全部子件；普通/未本轮构建的组→原名。
    # 于是装/查逻辑无需感知 split，照常按 id 查 components.model 即可。
    editions_out: dict[str, dict] = {}
    for ed, spec in EDITIONS.items():
        models_expanded: list[str] = []
        for g in spec["models"]:
            models_expanded.extend(built_groups.get(g, [g]))
        # 该档位需要的共享基座 = 其各环境 needs_shared 的并集（基座装在环境之前）。
        shared_ids = sorted({sid for e in spec["envs"]
                             for sid in env_components.get(e, {}).get("needs_shared", [])})
        editions_out[ed] = {**spec, "models": models_expanded, "shared": shared_ids}

    manifest = {
        "project": "AvatarHub",
        "version": args.version,
        "built_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "platform": "win-amd64",
        "arch_tag": args.arch_tag,          # 显卡/CUDA 变体标签（如 cu128 / cu121）
        "base_url": args.base_url,          # 自建下载站/Git 根 URL；下载器拼 base_url + component.file
        # 远端 manifest 规范地址（站点根）。客户端「更新检查」据此拉最新清单，免改本地包即可下发 pack 更新。
        "manifest_url": (args.base_url.rstrip("/") + "/manifest.json") if args.base_url else "",
        # 镜像源（可选）：与 base_url 同布局的备用根 URL 列表；下载器按延迟择优 + 失败自动切换。
        "mirrors": [m.rstrip("/") for m in (args.mirror or [])],
        # 匿名健康回执/崩溃上报端点（可选）：客户端开启遥测后 POST；为空则只在本地留回执。
        "telemetry_url": (args.telemetry_url or "").strip(),
        # 上报端点令牌（可选，与服务端 AH_INGEST_TOKEN 对应）：仅做接入去滥用，非机密级。
        "telemetry_token": (args.telemetry_token or "").strip(),
        "unpack_note": "conda-env 组件解压后须执行 <env>\\Scripts\\conda-unpack.exe 修正路径前缀",
        "editions": editions_out,
        "components": {
            "shared": shared_components,
            "env": env_components,
            "model": model_components,
            "app": app_components,   # 程序本体（全档位通用，pack_installer 恒纳入）
        },
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    _maybe_sign(MANIFEST_PATH)      # 发布清单出炉即签名（供应链防篡改）

    print("-" * 64)
    n_env, n_model, n_shared = len(env_components), len(model_components), len(shared_components)
    total = sum(c["size_bytes"] for c in env_components.values()) \
        + sum(c["size_bytes"] for c in model_components.values()) \
        + sum(c["size_bytes"] for c in shared_components.values())
    summary = "%d 个环境包" % n_env
    if n_shared:
        summary += " + %d 个共享基座" % n_shared
    if args.include_models:
        summary += " + %d 个模型包" % n_model
    print(" 完成：" + summary)
    print(" 产物总体积： %s" % _human(total))
    print(" manifest： %s" % MANIFEST_PATH)
    if not args.base_url:
        print(" 提示：发布前用 --base-url 写入你的下载站/Git 根 URL，或事后改 manifest.json。")


def survey_sharing():
    """读现有 manifest，量化共享基座的「真共享 vs 伪共享」：每个 blob 被几个环境引用，
    据此判断基座下载是否还有可削减的脂肪。仅 1 环境引用的 blob 是各自所需（多因不同 torch
    版本），并非可删的重复——本工具让这一判断可重复执行。"""
    import collections
    if not MANIFEST_PATH.exists():
        print("[ERROR] 无 manifest，先 --build --shared 再勘察。")
        return
    m = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    sh = m.get("components", {}).get("shared", {})
    if not sh:
        print("当前 manifest 无 shared 组件（未用 --shared 构建）。")
        return
    size = {s: sz for c in sh.values() for s, sz in c.get("blobs", {}).items()}
    envset = collections.defaultdict(set)
    for e, c in m["components"].get("env", {}).items():
        for _rel, s in c.get("placements", []):
            envset[s].add(e)
    byn_sz = collections.Counter()
    byn_ct = collections.Counter()
    for s, sz in size.items():
        n = len(envset.get(s, ()))
        byn_sz[n] += sz
        byn_ct[n] += 1
    tot = sum(size.values()) or 1
    print("=" * 64)
    print(" 共享基座「真/伪共享」勘察（基于现有 manifest）")
    print("=" * 64)
    print(" 共享 blob：%d 个 / %s（未压缩）" % (len(size), _human(tot)))
    print(" 按「被几个环境引用」分组：")
    for n in sorted(byn_sz):
        print("   被 %d 环境用：%3d blob，%9s（%.0f%%）"
              % (n, byn_ct[n], _human(byn_sz[n]), byn_sz[n] / tot * 100))
    single = byn_sz.get(1, 0)
    print("-" * 64)
    print(" 仅 1 环境使用（伪共享，多为各自的 torch 版本，非可删重复）：%s（%.0f%%）"
          % (_human(single), single / tot * 100))
    print(" 真正 ≥2 环境共享：%s（%.0f%%）" % (_human(tot - single), (tot - single) / tot * 100))
    print(" 结论：下载已最优——每 blob 仅按档位下发一次；伪共享部分是各环境真实所需，无法再省。")


def main():
    ap = argparse.ArgumentParser(description="分发包构建流水线（conda-pack 冻结 + manifest）")
    ap.add_argument("--build", action="store_true", help="实际打包（默认仅体检）")
    ap.add_argument("--survey-models", action="store_true", help="勘察模型组子项体积/拆分候选（不打包）")
    ap.add_argument("--survey-envs", action="store_true", help="量化 6 环境包跨环境重复字节/可去重量（不打包）")
    ap.add_argument("--survey-sharing", action="store_true", help="读 manifest 量化共享基座真/伪共享（判断下载是否还有可削减脂肪）")
    ap.add_argument("--only", nargs="*", help="只处理指定环境（给 --only 但不跟值=不打任何环境）")
    ap.add_argument("--shared", action="store_true",
                    help="抽出 torch/CUDA 共享库为「只下一次」基座包，环境包排除之（首装/下载大降；建议配 --force）")
    ap.add_argument("--include-models", action="store_true", help="追加打包模型目录（大）")
    ap.add_argument("--only-models", nargs="*", help="只打指定模型组（隐含开启模型打包）")
    ap.add_argument("--force", action="store_true", help="重打已存在的包")
    ap.add_argument("--version", default="1.0.0", help="发布版本号（写入 manifest）")
    ap.add_argument("--arch-tag", default="cu128", help="显卡/CUDA 变体标签（cu128/cu121…）")
    ap.add_argument("--base-url", default="", help="下载站/Git 根 URL（写入 manifest.base_url）")
    ap.add_argument("--mirror", action="append", default=[],
                    help="镜像根 URL（与 base_url 同布局，可重复）；写入 manifest.mirrors，客户端择优+failover")
    ap.add_argument("--telemetry-url", default="",
                    help="匿名健康回执上报端点 URL（写入 manifest.telemetry_url，可选）")
    ap.add_argument("--telemetry-token", default="",
                    help="上报端点令牌（写入 manifest.telemetry_token，与服务端 AH_INGEST_TOKEN 对应）")
    ap.add_argument("--build-app", action="store_true",
                    help="只打程序本体组件 app-<ver>.tar.zst 并并入 manifest（热修发布用）")
    ap.add_argument("--app-version", default="",
                    help="app 组件版本号（--build-app 必填，如 1.0.7）")
    ap.add_argument("--app-manifest", action="append", default=[],
                    help="要写入 app 组件的 manifest 路径（可重复；默认 dist/manifest.json）")
    args = ap.parse_args()

    if args.survey_models:
        survey_models()
        return 0
    if args.survey_envs:
        survey_envs()
        return 0
    if args.survey_sharing:
        survey_sharing()
        return 0
    if args.build_app:
        if not args.app_version:
            print("[ERROR] --build-app 需要 --app-version（如 1.0.7）")
            return 2
        mps = [Path(p) for p in args.app_manifest] or [MANIFEST_PATH]
        return build_app_cli(args.app_version, args.force, mps)
    if not args.build:
        survey(args.only or PRODUCT_ENVS)
        return 0
    build(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
