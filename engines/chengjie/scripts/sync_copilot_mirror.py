# -*- coding: utf-8 -*-
"""copilot 双树镜像一键同步（2026-08-22 编码损毁事故链的最后一块补丁）。

背景：``shared/copilot/``（编辑源，web 经 /copilot 静态服务）与
``desktop/renderer/shared/copilot/``（桌面壳打包用）是同一组件库的两份拷贝，
此前靠人肉「改完记得拷过去」——既产生漂移（``test_copilot_shared_sync`` 门禁抓），
也正是编码事故的高危动作（用 PS5.1 ``Get-Content|Set-Content`` 拷贝＝按 ANSI 误读
UTF-8，2026-08-22 把镜像 app.html 存成乱码，差点随下次打包出货）。

本工具把这个动作变成一条安全命令：

    python -m scripts.sync_copilot_mirror              # 源 → 镜像，二进制语义
    python -m scripts.sync_copilot_mirror --dry-run    # 只看差异不动盘
    python -m scripts.sync_copilot_mirror --check      # 校验模式：不同步，漂移则退出 1

三条铁律：
  1. **方向单一**：只允许 源(shared/copilot) → 镜像(desktop/renderer/...)，镜像上的
     任何独立改动都会被覆盖（那本来就是漂移——门禁语义如此）；
  2. **二进制拷贝**（shutil.copyfile 字节级），全程零编码解释；
  3. **源树先体检**：源树若含乱码指纹/U+FFFD（判定口径对齐
     tests/test_encoding_integrity.py，那边是权威门禁、这边是同一常量的前置便检）
     → 拒绝同步，防把损毁扩散进打包树。

同步后提醒：改了 .js 组件记得 bump 宿主 ``?v=`` 双戳（unified_inbox.html +
shared/copilot/app.html 两处）+ ``scripts/bump_ui_build.py``。
"""
from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = _ROOT / "shared" / "copilot"
DST_ROOT = _ROOT / "desktop" / "renderer" / "shared" / "copilot"

# 与 tests/test_encoding_integrity.py 同一套指纹常量（那边是权威门禁）。
_MOJIBAKE_MARKS = ("鐨", "锛", "銆", "鈥", "涔", "鍦", "鎴", "娓", "宸ヤ", "锟斤拷")
_TEXT_EXT = {".html", ".js", ".css", ".json", ".md", ".txt"}


def source_encoding_problems(src_root: Path) -> list:
    """源树编码体检：返回问题清单（空=干净）。只检文本扩展名。"""
    problems = []
    for p in sorted(src_root.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in _TEXT_EXT:
            continue
        rel = p.relative_to(src_root).as_posix()
        raw = p.read_bytes()
        try:
            text = raw.decode("utf-8", errors="strict")
        except UnicodeDecodeError as e:
            problems.append(f"{rel}: 非法 UTF-8（{str(e)[:60]}）")
            continue
        if "\ufffd" in text:
            problems.append(f"{rel}: 含 U+FFFD 有损转码化石")
            continue
        hits = {m: text.count(m) for m in _MOJIBAKE_MARKS}
        total = sum(hits.values())
        distinct = sum(1 for v in hits.values() if v > 0)
        if distinct >= 2 and total >= 4:
            problems.append(f"{rel}: 乱码指纹 total={total} distinct={distinct}")
    return problems


def _sha1(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()


def plan_sync(src_root: Path, dst_root: Path) -> list:
    """产出同步计划：[('copy'|'delete', 相对路径)]。copy=新增或字节不同；delete=镜像多余。"""
    src = {p.relative_to(src_root).as_posix(): p
           for p in src_root.rglob("*") if p.is_file()}
    dst = {p.relative_to(dst_root).as_posix(): p
           for p in dst_root.rglob("*") if p.is_file()} if dst_root.is_dir() else {}
    plan = []
    for rel in sorted(src):
        if rel not in dst or _sha1(src[rel]) != _sha1(dst[rel]):
            plan.append(("copy", rel))
    for rel in sorted(set(dst) - set(src)):
        plan.append(("delete", rel))
    return plan


def apply_sync(src_root: Path, dst_root: Path, plan: list) -> None:
    for action, rel in plan:
        target = dst_root / rel
        if action == "copy":
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src_root / rel, target)   # 字节级，零编码解释
        elif action == "delete":
            target.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="copilot 双树镜像一键同步（源→镜像，二进制语义）")
    ap.add_argument("--dry-run", action="store_true", help="只打印差异，不写盘")
    ap.add_argument("--check", action="store_true", help="校验模式：有漂移退出 1（不写盘）")
    args = ap.parse_args()

    if not SRC_ROOT.is_dir():
        print(f"[ABORT] 编辑源不存在：{SRC_ROOT}")
        return 2
    problems = source_encoding_problems(SRC_ROOT)
    if problems:
        print("[ABORT] 源树编码体检不过，拒绝同步（防把损毁扩散进打包树）：")
        for x in problems:
            print("   ", x)
        print("  先修源树（参考 docs/模板快照恢复与编码损毁防线_2026-08.md），再同步。")
        return 3

    plan = plan_sync(SRC_ROOT, DST_ROOT)
    if not plan:
        print("[OK] 双树已一致，无需同步。")
        return 0
    for action, rel in plan:
        print(f"  {action:>6}  {rel}")
    if args.check:
        print(f"[DRIFT] 双树漂移 {len(plan)} 项（--check 模式不写盘）。")
        return 1
    if args.dry_run:
        print(f"[DRY-RUN] 共 {len(plan)} 项，未写盘。")
        return 0
    apply_sync(SRC_ROOT, DST_ROOT, plan)
    print(f"[DONE] 已同步 {len(plan)} 项（二进制语义）。")
    print("  收尾：改了 .js 组件请 bump 宿主 ?v= 双处 + python scripts/bump_ui_build.py；")
    print("  验证：python -m pytest tests/test_copilot_shared_sync.py -q")
    return 0


if __name__ == "__main__":
    sys.exit(main())
