#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按 hunk 选择性暂存（共享工作树 · 同文件混有他人在途改动时用）。

    python tools/stage_hunks.py --file <path> --mine <regex> [--apply] [--sync-worktree]
    python tools/stage_hunks.py --file <path> --theirs <regex> [--apply]

- ``--mine``：只暂存「每一行改动都匹配 regex」的 hunk（我的键/注释有统一前缀时用，
  如 i18n 生成物里的 ``cs8_``）。
- ``--theirs``：暂存「没有任何一行改动匹配 regex」的 hunk（排除他人 hunk，如 QQ 线
  在 ``background_tasks.py`` 里的三行）。
- 缺省只打印将暂存哪些 hunk（dry-run）；``--apply`` 真 ``git apply --cached``。
- ``--sync-worktree``：暂存后把工作树该文件改写成 index 版本（HEAD + 我的 hunk）——
  用于**生成物**（zh_hant_auto.py 再生会把别人未提交的键一并吐出来，不该留在共享
  工作树里让人误卷）。手写源码文件不要加这个开关。
只碰指定的一个文件；不 add -A、不 stash、不 checkout。全程按字节处理（CRLF 原样）。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _git(args: list, data: bytes | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(ROOT), input=data, capture_output=True)


def split_hunks(diff: bytes):
    lines = diff.splitlines(keepends=True)
    header, hunks, cur = [], [], None
    for ln in lines:
        if ln.startswith(b"@@"):
            if cur is not None:
                hunks.append(cur)
            cur = [ln]
        elif cur is None:
            header.append(ln)
        else:
            cur.append(ln)
    if cur is not None:
        hunks.append(cur)
    return header, hunks


def changed_lines(hunk):
    out = []
    for ln in hunk[1:]:
        if ln.startswith((b"+", b"-")) and not ln.startswith((b"+++", b"---")):
            out.append(ln[1:].decode("utf-8", "replace"))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", required=True)
    ap.add_argument("--mine", help="regex：每行改动都匹配才算我的 hunk")
    ap.add_argument("--theirs", help="regex：任一行改动匹配即视为他人 hunk（排除）")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--sync-worktree", action="store_true")
    a = ap.parse_args()
    if not a.mine and not a.theirs:
        print("need --mine or --theirs", file=sys.stderr)
        return 2
    rel = Path(a.file).as_posix()
    d = _git(["diff", "--", rel])
    if d.returncode != 0:
        print(d.stderr.decode("utf-8", "replace"), file=sys.stderr)
        return d.returncode
    header, hunks = split_hunks(d.stdout)
    if not hunks:
        print("no unstaged hunks in", rel)
        return 0
    keep = []
    for h in hunks:
        ch = changed_lines(h)
        if a.mine:
            ok = bool(ch) and all(re.search(a.mine, ln) for ln in ch)
        else:
            ok = not any(re.search(a.theirs, ln) for ln in ch)
        print(("KEEP " if ok else "SKIP ") + h[0].decode("utf-8", "replace").rstrip()
              + f"  ({len(ch)} changed lines)")
        if ok:
            keep.append(h)
    if not keep:
        print("nothing to stage")
        return 0
    patch = b"".join(header) + b"".join(b"".join(h) for h in keep)
    if not a.apply:
        print(f"[dry-run] would stage {len(keep)}/{len(hunks)} hunks; add --apply")
        return 0
    r = _git(["apply", "--cached", "--recount", "-"], patch)
    if r.returncode != 0:
        print(r.stderr.decode("utf-8", "replace"), file=sys.stderr)
        return r.returncode
    print(f"staged {len(keep)}/{len(hunks)} hunks of {rel}")
    if a.sync_worktree:
        blob = _git(["show", f":{rel}"])
        if blob.returncode != 0:
            print(blob.stderr.decode("utf-8", "replace"), file=sys.stderr)
            return blob.returncode
        (ROOT / rel).write_bytes(blob.stdout)
        print("worktree synced to index for", rel)
    return 0


if __name__ == "__main__":
    sys.exit(main())
