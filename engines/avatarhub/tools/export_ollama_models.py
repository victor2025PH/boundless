# -*- coding: utf-8 -*-
"""export_ollama_models.py — 按模型名精确导出 Ollama 模型到离线介质（内部伙伴交付用）。

背景：母机 %USERPROFILE%\\.ollama\\models 混有大量实验模型（实测 110GB/8 模型），
而产品只依赖其中 2-3 个。整目录拷贝 = 多拷 ~96GB 且把不该给伙伴的模型带出去；
手工挑文件又极易漏 blob（Ollama 用 manifest→blobs 内容寻址，漏一个模型即损坏）。
本工具读 manifest 的 config/layers 引用，只复制目标模型的 manifest + blobs，
产物目录结构与 .ollama\\models 一致，伙伴机拷回同路径（或设 OLLAMA_MODELS）即用。

用法（纯标准库，任意 python3 可跑）：
  python tools\\export_ollama_models.py --list                       # 看本机有哪些模型/体积
  python tools\\export_ollama_models.py --models qwen2.5:14b bge-m3 --out E:\\media\\ollama_models
  # 产品最小集（对话兜底+同传主力 / 语义检索嵌入；可选加 hy-mt2-7b-official 低显存应急）：
  python tools\\export_ollama_models.py --models qwen2.5:14b bge-m3 hy-mt2-7b-official --out <介质>\\ollama_models

伙伴机导入（二选一）：
  A. 把 ollama_models 里的 blobs/ 与 manifests/ 合并拷到 %USERPROFILE%\\.ollama\\models\\
  B. 放任意盘后 setx OLLAMA_MODELS <该目录>，重启 ollama 服务
验证：ollama list 应出现导出的模型名。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


def models_root() -> Path:
    import os
    env = os.environ.get("OLLAMA_MODELS", "").strip()
    if env:
        return Path(env)
    return Path.home() / ".ollama" / "models"


def parse_name(name: str) -> tuple[str, str, str]:
    """qwen2.5:14b → (library, qwen2.5, 14b)；user/model:tag → (user, model, tag)。"""
    tag = "latest"
    if ":" in name:
        name, tag = name.rsplit(":", 1)
    ns = "library"
    if "/" in name:
        ns, name = name.split("/", 1)
    return ns, name, tag


def iter_manifests(root: Path):
    """遍历 manifests 树 → (registry, ns, model, tag, manifest_path)。"""
    mroot = root / "manifests"
    if not mroot.is_dir():
        return
    for reg in mroot.iterdir():
        if not reg.is_dir():
            continue
        for ns in reg.iterdir():
            if not ns.is_dir():
                continue
            for model in ns.iterdir():
                if not model.is_dir():
                    continue
                for tagf in model.iterdir():
                    if tagf.is_file():
                        yield reg.name, ns.name, model.name, tagf.name, tagf


def manifest_blobs(mf: Path) -> list[str]:
    """manifest 引用的全部 digest（config + layers）。"""
    data = json.loads(mf.read_text(encoding="utf-8"))
    digs = []
    cfg = (data.get("config") or {}).get("digest")
    if cfg:
        digs.append(cfg)
    for layer in data.get("layers") or []:
        d = layer.get("digest")
        if d:
            digs.append(d)
    return digs


def blob_path(root: Path, digest: str) -> Path:
    return root / "blobs" / digest.replace(":", "-")


def human(n: float) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def cmd_list(root: Path) -> int:
    rows = []
    for reg, ns, model, tag, mf in iter_manifests(root):
        size = 0
        for d in manifest_blobs(mf):
            bp = blob_path(root, d)
            if bp.is_file():
                size += bp.stat().st_size
        disp = (f"{ns}/{model}" if ns != "library" else model) + f":{tag}"
        rows.append((disp, size))
    if not rows:
        print(f"未在 {root} 找到任何模型 manifest。")
        return 1
    print(f"Ollama 模型目录：{root}")
    for disp, size in sorted(rows):
        print(f"  {disp:<48} {human(size)}")
    return 0


def find_manifest(root: Path, name: str) -> Path | None:
    ns, model, tag = parse_name(name)
    hits = []
    for reg, n2, m2, t2, mf in iter_manifests(root):
        if m2 == model and t2 == tag and (ns == "library" or n2 == ns):
            hits.append(mf)
    if not hits:
        # tag 未指定时（latest 没命中）放宽：同名任意 tag 唯一即取
        cands = [mf for reg, n2, m2, t2, mf in iter_manifests(root) if m2 == model]
        if len(cands) == 1:
            return cands[0]
        return None
    return hits[0]


def cmd_export(root: Path, names: list[str], out: Path) -> int:
    plan: list[tuple[str, Path, list[str]]] = []
    missing = []
    for nm in names:
        mf = find_manifest(root, nm)
        if mf is None:
            missing.append(nm)
            continue
        digs = manifest_blobs(mf)
        lost = [d for d in digs if not blob_path(root, d).is_file()]
        if lost:
            print(f"[错误] {nm} 缺 blob {len(lost)} 个（本机模型不完整）：{lost[:2]}…")
            missing.append(nm)
            continue
        plan.append((nm, mf, digs))
    if missing:
        print(f"[中止] 未找到/不完整的模型：{', '.join(missing)}（--list 查看可用名）")
        return 2

    # blobs 内容寻址天然去重（多模型共享底座层只拷一份）
    all_digs = {d for _, _, digs in plan for d in digs}
    total = sum(blob_path(root, d).stat().st_size for d in all_digs)
    print(f"将导出 {len(plan)} 个模型，{len(all_digs)} 个 blob，共 {human(total)} → {out}")

    copied = skipped = 0
    for d in sorted(all_digs):
        src = blob_path(root, d)
        dst = blob_path(out, d)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.is_file() and dst.stat().st_size == src.stat().st_size:
            skipped += 1
            continue
        print(f"  blob {d[:19]}…  {human(src.stat().st_size)}")
        shutil.copy2(src, dst)
        copied += 1
    for nm, mf, _ in plan:
        rel = mf.relative_to(root)
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(mf, dst)
        print(f"  manifest {rel}")

    note = out / "导入说明.txt"
    note.write_text(
        "Ollama 模型离线导入（伙伴机）\n"
        "================================\n"
        f"包含模型：{', '.join(nm for nm, _, _ in plan)}\n\n"
        "方式 A（推荐）：把本目录下 blobs\\ 与 manifests\\ 合并拷贝到\n"
        "  %USERPROFILE%\\.ollama\\models\\   （没有该目录就直接整个拷过去）\n"
        "方式 B：本目录放任意盘，然后（管理员或当前用户）执行\n"
        "  setx OLLAMA_MODELS <本目录完整路径>\n"
        "  重启 Ollama（托盘退出再开 / services 重启）\n\n"
        "验证：命令行执行 ollama list，应能看到上述模型名。\n",
        encoding="utf-8")
    print(f"完成：复制 {copied} 个 blob（跳过已存在 {skipped}），说明 → {note}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="按模型名导出 Ollama 模型到离线介质")
    ap.add_argument("--list", action="store_true", help="列出本机模型与体积")
    ap.add_argument("--models", nargs="*", default=[], help="要导出的模型名（如 qwen2.5:14b bge-m3）")
    ap.add_argument("--out", default="", help="导出目标目录（介质路径）")
    ap.add_argument("--root", default="", help="覆盖本机模型根（默认 %%OLLAMA_MODELS%% 或 ~/.ollama/models）")
    args = ap.parse_args()
    root = Path(args.root) if args.root else models_root()
    if not root.is_dir():
        print(f"[错误] 模型根不存在：{root}")
        return 2
    if args.list or not args.models:
        return cmd_list(root)
    if not args.out:
        print("[错误] 需要 --out 指定导出目录")
        return 2
    return cmd_export(root, args.models, Path(args.out))


if __name__ == "__main__":
    sys.exit(main())
