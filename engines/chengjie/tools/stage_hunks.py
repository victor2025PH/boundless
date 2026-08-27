# -*- coding: utf-8 -*-
"""共享工作树上的**安全部分暂存**：只把自己的改动块放进 index（2026-08-27）。

## 为什么需要它

本机工作树长期有 1000+ 文件未提交，同一个文件里常混着多条 agent 线跨批次的成果。
于是想提交自己那点改动时只有两条死路：

  · **整文件提交** → 把别人未完成/未署名的活一起吞掉（实测 `health_watchdog.py`
    的未提交 diff 里是**十二个完整功能**：stderr storm / proxy managed / image
    models / hub engine / token wallet…，我的只占 99 行）；
  · **不提交** → 分支 HEAD 上自己的测试引用不存在的函数，CI 红。

2026-08-27 我手工走了第三条路（按 hunk 切 + 临时 worktree 检出 HEAD 复验），确实
两全，但**一次要 20 分钟**，没法要求每条线都这么干——于是「大家各自提交」这件事
的真实阻塞不是意愿而是成本。本工具就是把那 20 分钟压成几条命令。

## 那次手工施工踩到的三个坑，都已内建规避

1. **必须全程按字节处理**。首版用 ``text=True`` 解码再写回补丁，含中文上下文的
   hunk 应用失败、纯 ASCII 上下文那块却成功——git 是按**字节**比对上下文的，任何
   一次解码/编码往返都可能改变字节。本工具从不 decode 补丁内容。
2. **「这个文件的 diff 全是我的」必须逐行验证，不能按文件推定**。那次整文件提交
   `tests/*.py` 的 +419 行里混进了他线一行断言（依赖他们未提交的模板 → HEAD 必红）。
   故 ``--theirs`` 的反向自证是**默认要跑**的，不是可选项。
3. **验证要在提交之前**。那次是先提交、再检出 HEAD 发现红、只能再补一个提交。
   ``--preview`` 用 ``write-tree`` + ``commit-tree`` 把 **index 现状**造成一个游离
   commit 并检出到独立 worktree——**不动 HEAD、不产生提交历史**，在那里跑门禁，绿了
   再真提交。

## 用法

    # ① 看切分（默认干跑，不动 index）
    python tools/stage_hunks.py --file src/inbox/health_watchdog.py \
        --mine _check_probe_stalled --mine restore_strike_state \
        --theirs _check_token_wallet --theirs _check_hub_engine

    # ② 确认无误后暂存
    python tools/stage_hunks.py --file src/inbox/health_watchdog.py \
        --mine _check_probe_stalled --mine restore_strike_state --apply

    # ③ 整文件暂存（仅当已逐行确认全是自己的）
    python tools/stage_hunks.py --file tests/test_x.py --whole --apply

    # ④ 提交前预览：把 index 检出到独立 worktree，在那里跑门禁
    python tools/stage_hunks.py --preview D:\\_wt_preview
    cd D:\\_wt_preview\\engines\\chengjie && python -m pytest tests/test_x.py -q

    # ⑤ 门禁绿了 → 回主树 git commit；然后清理预览
    python tools/stage_hunks.py --preview-clean D:\\_wt_preview

退出码：0 正常 ／ 1 自证不通过或 git 失败（此时 index 零改动，可安全重来）。
"""

from __future__ import annotations

import argparse
import ast
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

ENGINE_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = ENGINE_ROOT.parent.parent


# ── git 薄封装（全程 bytes）────────────────────────────────────────────────


def git(*args: str, data: Optional[bytes] = None,
        check: bool = True) -> Tuple[int, bytes, bytes]:
    r = subprocess.run(["git", *args], cwd=str(REPO_ROOT),
                       capture_output=True, input=data)
    if check and r.returncode:
        raise RuntimeError(f"git {' '.join(args)} 失败：\n"
                           f"{r.stderr.decode('utf-8', 'replace')}")
    return r.returncode, r.stdout, r.stderr


# ── 纯函数：hunk 切分与过滤（离线可单测）───────────────────────────────────


def split_hunks(diff: bytes) -> Tuple[bytes, List[bytes]]:
    """unified diff → (文件头, [hunk, ...])。按 ``@@`` 行切，全程不 decode。"""
    head: List[bytes] = []
    hunks: List[bytes] = []
    cur: Optional[List[bytes]] = None
    for line in diff.splitlines(keepends=True):
        if line.startswith(b"@@"):
            if cur is not None:
                hunks.append(b"".join(cur))
            cur = [line]
        elif cur is None:
            head.append(line)
        else:
            cur.append(line)
    if cur is not None:
        hunks.append(b"".join(cur))
    return b"".join(head), hunks


def hunk_added(hunk: bytes) -> bytes:
    """hunk 里新增行的正文（去掉前导 ``+``；跳过 ``+++`` 文件头）。"""
    return b"\n".join(ln[1:] for ln in hunk.splitlines()
                      if ln.startswith(b"+") and not ln.startswith(b"+++"))


def filter_hunks(hunks: Sequence[bytes], mine: Sequence[str],
                 theirs: Sequence[str] = ()) -> Dict[str, object]:
    """按标记切「我的」与「别人的」hunk，并做**双向自证**。

    双向是刻意的：只查一边会漏掉两类事故——留下的块里夹带了别人的功能（正向），
    或丢弃的块里其实有自己的改动（反向）。2026-08-27 那次就是反向假设出的错。
    """
    m = [s.encode("utf-8") for s in mine if s]
    t = [s.encode("utf-8") for s in theirs if s]
    keep, drop = [], []
    for h in hunks:
        (keep if any(x in hunk_added(h) for x in m) else drop).append(h)
    bleed = sorted({x.decode() for h in keep for x in t if x in hunk_added(h)})
    missed = sorted({x.decode() for h in drop for x in m if x in hunk_added(h)})
    return {"keep": keep, "drop": drop, "bleed": bleed, "missed": missed}


def hunk_label(hunk: bytes) -> str:
    """给人看的一行摘要：``@@ 头`` + 第一条新增行。"""
    lines = hunk.splitlines()
    head = lines[0].decode("utf-8", "replace") if lines else ""
    body = hunk_added(hunk).splitlines()
    first = body[0].decode("utf-8", "replace").strip() if body else ""
    return f"{head.split('@@')[1].strip() if '@@' in head else head}  |  {first[:76]}"


# ── 动作 ──────────────────────────────────────────────────────────────────


def _staged_others(exclude: Sequence[str]) -> List[str]:
    """index 里**不属于本次**的已暂存文件——共享树上这是真实风险面：
    sibling 一句 ``git commit -a`` 会把它们连带交掉（AGENTS.md 记录过该事故）。"""
    _, out, _ = git("diff", "--cached", "--name-only")
    names = [n for n in out.decode("utf-8", "replace").split("\n") if n.strip()]
    return [n for n in names if n not in set(exclude)]


def do_file(rel: str, mine: Sequence[str], theirs: Sequence[str],
            whole: bool, apply: bool) -> int:
    if whole:
        print(f"=== {rel} （整文件模式）===")
        if not apply:
            _, out, _ = git("diff", "--numstat", "--", rel)
            print(f"  干跑：将整文件暂存，工作树 diff = "
                  f"{out.decode('utf-8', 'replace').strip() or '(无改动)'}")
            print("  ⚠ 整文件模式不做自证——请先逐行确认这个文件里没有别人的改动")
            return 0
        git("add", "--", rel)
        print("  已整文件暂存")
        return 0

    if not mine:
        print("!! 非整文件模式必须给 --mine 标记", file=sys.stderr)
        return 1

    _, diff, _ = git("diff", "--", rel)
    if not diff.strip():
        print(f"=== {rel} ===\n  工作树相对 index 无改动，跳过")
        return 0
    head, hunks = split_hunks(diff)
    res = filter_hunks(hunks, mine, theirs)
    keep: List[bytes] = res["keep"]        # type: ignore[assignment]
    drop: List[bytes] = res["drop"]        # type: ignore[assignment]
    bleed: List[str] = res["bleed"]        # type: ignore[assignment]
    missed: List[str] = res["missed"]      # type: ignore[assignment]

    print(f"=== {rel} ===")
    print(f"  总 hunk {len(hunks)}  ->  我的 {len(keep)}  别人的 {len(drop)}")
    print(f"  自证① 我的块里夹带他人标记: {bleed or '无 (OK)'}")
    print(f"  自证② 丢弃块里有我的标记  : {missed or '无 (OK)'}")
    for h in keep:
        print(f"    保留  {hunk_label(h)}")
    if bleed or missed:
        print("!! 自证不通过，index 未改动。请调整 --mine/--theirs 标记后重来",
              file=sys.stderr)
        return 1
    if not keep:
        print("  没有命中任何 hunk（标记是否写错？）index 未改动")
        return 1
    if not apply:
        print("  干跑完成，index 未改动。确认无误后加 --apply")
        return 0

    patch = head + b"".join(keep)
    with tempfile.NamedTemporaryFile(suffix=".patch", delete=False) as fh:
        fh.write(patch)                     # bytes 原样落盘，绝不 decode
        tmp = fh.name
    try:
        rc, _, err = git("apply", "--cached", "--check", tmp, check=False)
        if rc:
            print(f"!! 补丁无法应用到 index（index 零改动）：\n"
                  f"{err.decode('utf-8', 'replace')}", file=sys.stderr)
            return 1
        git("apply", "--cached", tmp)
    finally:
        Path(tmp).unlink(missing_ok=True)

    # 暂存后立刻验暂存版本可解析——半个 hunk 造出语法错误必须当场发现
    if rel.endswith(".py"):
        _, blob, _ = git("show", f":{rel}")
        try:
            ast.parse(blob.decode("utf-8"), rel)
            print("  已暂存；暂存版本语法 OK")
        except SyntaxError as ex:
            print(f"!! 暂存版本语法错误（请 git reset -- {rel} 撤销）：{ex}",
                  file=sys.stderr)
            return 1
    else:
        print("  已暂存")
    return 0


def do_preview(target: Path) -> int:
    """把 **index 现状** 造成游离 commit 并检出到独立 worktree。

    刻意用 ``write-tree`` + ``commit-tree`` 而不是先真提交：验证必须在提交之前。
    2026-08-27 那次先提交再验证，结果 HEAD 上两条红，只能再补一个提交去修。
    该 commit 不被任何 ref 引用（HEAD/分支都不动），验完清掉即随 gc 回收。
    """
    if not _staged_any():
        print("!! index 为空，没什么可预览的（先 --apply 暂存）", file=sys.stderr)
        return 1
    _, tree, _ = git("write-tree")
    _, sha, _ = git("commit-tree", tree.decode().strip(), "-p", "HEAD",
                    "-m", "stage_hunks preview (not a real commit)")
    sha_s = sha.decode().strip()
    if target.exists():
        print(f"!! 目标目录已存在：{target}（先 --preview-clean）", file=sys.stderr)
        return 1
    git("worktree", "add", "--detach", str(target), sha_s)
    print(f"预览提交 {sha_s[:10]}（游离，HEAD 未动）已检出到：{target}")
    print(f"下一步在那里跑门禁，例如：")
    print(f"  cd {target / 'engines' / 'chengjie'} && python -m pytest tests/<你的门禁> -q")
    print(f"绿了 → 回主树 git commit；然后：python tools/stage_hunks.py "
          f"--preview-clean {target}")
    return 0


def _staged_any() -> bool:
    rc, _, _ = git("diff", "--cached", "--quiet", check=False)
    return rc != 0


def remove_tree_with_retry(path: Path, attempts: int = 4,
                           sleep_sec: float = 1.5) -> bool:
    """强删目录，带重试 → 是否真的删掉了。

    Windows 上刚跑过 pytest 的预览树会被 ``__pycache__`` / 索引器短暂占用：单次
    ``rmtree`` 失败是常态（2026-08-27 首版实测就卡在这里，还谎报了「已清理」）。
    只读属性用 chmod 兜，占用用重试兜，**最终以 path.exists() 为唯一判据**。
    """
    def _onerror(func, p, _exc):
        try:
            Path(p).chmod(stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    for i in range(max(1, attempts)):
        if not path.exists():
            return True
        shutil.rmtree(path, onerror=_onerror)
        if not path.exists():
            return True
        if i < attempts - 1:
            time.sleep(sleep_sec)
    return not path.exists()


def do_preview_clean(target: Path) -> int:
    """清理预览 worktree。

    两步都必要：``git worktree remove`` 收 git 侧登记（常因文件占用失败），
    再强删目录 + ``prune``。**报告以磁盘实况为准**——首版无条件打印「已清理」，
    而目录其实还在，这种谎报比不清理更坏。
    """
    git("worktree", "remove", "--force", str(target), check=False)
    gone = remove_tree_with_retry(target)
    git("worktree", "prune")
    _, wt, _ = git("worktree", "list")
    listed = str(target) in wt.decode("utf-8", "replace")
    if gone and not listed:
        print(f"已清理：{target}")
        return 0
    print(f"!! 未能完全清理：目录仍存在={target.exists()}  git 仍登记={listed}\n"
          f"   多为文件占用（刚在该树跑过 pytest）。稍后重跑本命令，或手动删除：\n"
          f"   Remove-Item '{target}' -Recurse -Force", file=sys.stderr)
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="共享工作树上的安全部分暂存（默认干跑）")
    ap.add_argument("--file", action="append", default=[],
                    help="相对仓库根的路径，可重复")
    ap.add_argument("--mine", action="append", default=[],
                    help="标记我的 hunk 的子串，可重复")
    ap.add_argument("--theirs", action="append", default=[],
                    help="标记他人 hunk 的子串（反向自证），可重复")
    ap.add_argument("--whole", action="store_true",
                    help="整文件暂存（跳过自证，仅当已逐行确认）")
    ap.add_argument("--apply", action="store_true", help="真写 index")
    ap.add_argument("--preview", help="把 index 检出到该目录（游离 commit）")
    ap.add_argument("--preview-clean", help="清理预览目录")
    args = ap.parse_args(argv)

    try:
        if args.preview_clean:
            return do_preview_clean(Path(args.preview_clean))
        if args.preview:
            return do_preview(Path(args.preview))
        if not args.file:
            ap.error("需要 --file / --preview / --preview-clean 之一")

        if args.apply:
            others = _staged_others(args.file)
            if others:
                print(f"⚠ index 里已有**不属于本次**的暂存文件 {len(others)} 个"
                      f"（sibling 的 git commit -a 会连带交掉它们）：")
                for n in others[:8]:
                    print(f"    {n}")
                print("  如非你自己放的，建议先 git reset 再来。\n")

        rc = 0
        for rel in args.file:
            rc |= do_file(rel, args.mine, args.theirs, args.whole, args.apply)
        if rc == 0 and args.apply:
            print("\n提交前请务必：python tools/stage_hunks.py --preview <目录>"
                  "  → 在那里跑门禁 → 绿了再 git commit")
        return rc
    except RuntimeError as ex:
        print(f"!! {ex}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
