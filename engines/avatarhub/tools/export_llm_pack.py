# -*- coding: utf-8 -*-
"""把本机 Ollama 模型导出成「可随交付介质分发」的离线包（内部伙伴部署用）。

背景：打包/分发体系（build_packs.py / pack_installer.py）只冻结 conda 环境与 AI 模型，
**不含 LLM（Ollama）**。内部伙伴若走本地离线 LLM（数据不出机房、无需公网），需要把母机
已 `ollama pull` 好的模型随介质带过去。手动挑 blob 要读 manifest 找 digest、极易漏/错，
本工具据 manifest 精确收集「指定 tag 的 manifest + 其引用的全部 blob」，保持 Ollama 目录
相对结构导出，并生成伙伴机一键导入脚本。

用法：
  # 只算清单与体积，不复制（先看要带多大）
  python tools/export_llm_pack.py --plan qwen2.5:14b bge-m3

  # 实际导出到 dist/llm_pack/（默认）
  python tools/export_llm_pack.py qwen2.5:14b bge-m3:latest

  # 指定源模型目录 / 输出目录
  python tools/export_llm_pack.py --models-dir D:\ollama\models --out E:\media\llm_pack qwen2.5:14b

伙伴机导入：把导出目录整个拷到介质，运行其中的 import_llm_pack.bat（把 blobs/ 与
manifests/ 合并进 %USERPROFILE%\.ollama\models），再 `ollama list` 即可见、`ollama serve` 起服务。

ollama tag 语法：[host/]namespace/name[:tag]，默认 host=registry.ollama.ai、namespace=library、tag=latest。
blob 文件名 = digest 把 ':' 换 '-'（如 sha256:ab.. -> sha256-ab..）。
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path


def default_models_dir() -> Path:
    env = os.environ.get("OLLAMA_MODELS")
    if env:
        return Path(env)
    return Path(os.path.expanduser("~")) / ".ollama" / "models"


def parse_ref(ref: str):
    """把 ollama 引用解析为 (host, namespace, name, tag)。"""
    host = "registry.ollama.ai"
    namespace = "library"
    tag = "latest"
    body = ref
    if ":" in body.split("/")[-1]:
        body, tag = body.rsplit(":", 1)
    parts = body.split("/")
    if len(parts) == 1:
        name = parts[0]
    elif len(parts) == 2:
        namespace, name = parts
    else:
        host, namespace, name = parts[0], parts[1], "/".join(parts[2:])
    return host, namespace, name, tag


def manifest_path(models_dir: Path, ref: str) -> Path:
    host, namespace, name, tag = parse_ref(ref)
    return models_dir / "manifests" / host / namespace / name / tag


def blob_path(models_dir: Path, digest: str) -> Path:
    return models_dir / "blobs" / digest.replace(":", "-")


def collect(models_dir: Path, refs):
    """返回 (files, total_bytes, errors)。files: [(rel_path:str, size:int)]，去重。"""
    files = {}
    errors = []
    for ref in refs:
        mp = manifest_path(models_dir, ref)
        if not mp.is_file():
            errors.append(f"未找到模型清单：{ref}  （查 `ollama list` 确认已 pull，路径 {mp}）")
            continue
        rel_m = mp.relative_to(models_dir).as_posix()
        files[rel_m] = mp.stat().st_size
        try:
            man = json.loads(mp.read_text(encoding="utf-8"))
        except Exception as e:
            errors.append(f"清单解析失败 {ref}: {e}")
            continue
        digests = []
        cfg = man.get("config") or {}
        if cfg.get("digest"):
            digests.append(cfg["digest"])
        for layer in man.get("layers") or []:
            if layer.get("digest"):
                digests.append(layer["digest"])
        for dg in digests:
            bp = blob_path(models_dir, dg)
            if not bp.is_file():
                errors.append(f"{ref} 引用的 blob 缺失：{dg}")
                continue
            files[bp.relative_to(models_dir).as_posix()] = bp.stat().st_size
    total = sum(files.values())
    return files, total, errors


IMPORT_BAT = """@echo off
rem === AvatarHub offline LLM pack importer (ASCII-only, encoding-safe) ===
rem Merges bundled Ollama blobs/manifests into this user's Ollama store.
setlocal
set DEST=%USERPROFILE%\\.ollama\\models
echo Importing LLM pack into %DEST% ...
if not exist "%DEST%\\blobs" mkdir "%DEST%\\blobs"
if not exist "%DEST%\\manifests" mkdir "%DEST%\\manifests"
robocopy "%~dp0blobs" "%DEST%\\blobs" /E /NFL /NDL /NJH /NJS /NP
robocopy "%~dp0manifests" "%DEST%\\manifests" /E /NFL /NDL /NJH /NJS /NP
echo.
echo Done. Verify with:  ollama list
echo Start service with:  ollama serve   (or the Ollama tray app)
endlocal
"""


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024


def main():
    ap = argparse.ArgumentParser(description="导出 Ollama 模型为离线介质包")
    ap.add_argument("refs", nargs="+", help="ollama 模型 tag，如 qwen2.5:14b bge-m3")
    ap.add_argument("--models-dir", default="", help="源 Ollama 模型目录（默认自动探测）")
    ap.add_argument("--out", default="", help="输出目录（默认 dist/llm_pack）")
    ap.add_argument("--plan", action="store_true", help="只列清单与体积，不复制")
    args = ap.parse_args()

    models_dir = Path(args.models_dir) if args.models_dir else default_models_dir()
    if not models_dir.is_dir():
        print(f"[错误] Ollama 模型目录不存在：{models_dir}")
        return 2
    base = Path(__file__).resolve().parent.parent
    out = Path(args.out) if args.out else base / "dist" / "llm_pack"

    files, total, errors = collect(models_dir, args.refs)
    print(f"源目录：{models_dir}")
    print(f"模型：{', '.join(args.refs)}")
    print(f"文件数：{len(files)}  总体积：{human(total)}")
    for e in errors:
        print(f"  [警告] {e}")
    if errors and not files:
        return 2

    if args.plan:
        print("\n将导出以下文件（--plan 预演，未复制）：")
        for rel, sz in sorted(files.items(), key=lambda kv: -kv[1]):
            print(f"  {human(sz):>10}  {rel}")
        print(f"\n伙伴机导入：把导出目录拷到介质→运行 import_llm_pack.bat→`ollama list` 核对。")
        return 0 if not errors else 1

    out.mkdir(parents=True, exist_ok=True)
    copied = 0
    for rel, sz in files.items():
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists() or dst.stat().st_size != sz:
            shutil.copy2(models_dir / rel, dst)
        copied += 1
        print(f"  [{copied}/{len(files)}] {human(sz):>10}  {rel}")
    (out / "import_llm_pack.bat").write_text(IMPORT_BAT, encoding="ascii")
    manifest_txt = {
        "refs": args.refs, "files": len(files), "total_bytes": total,
        "source_models_dir": str(models_dir),
    }
    (out / "llm_pack.json").write_text(
        json.dumps(manifest_txt, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已导出到：{out}")
    print(f"体积：{human(total)}  ·  含 import_llm_pack.bat（伙伴机一键导入）")
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())
