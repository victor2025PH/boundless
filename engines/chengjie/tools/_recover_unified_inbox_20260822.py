# -*- coding: utf-8 -*-
"""unified_inbox.html 乱码事故一次性恢复脚本 v2（2026-08-22，实施56 P1 期间发现）。

事故：12:05–13:42 窗口内某线以「utf8 字节按 gb18030 解码」的方式整存模板 →
全文件中文乱码 + 无效序列处**连吞下一字节**（'?' 替位，含吃掉引号等 ASCII）→
Jinja 编译失败 → 收件箱页 500。

v2 算法（v1 的 corrupt() 少吞一字节导致 3586 行没对齐）：
1. 逐行**严格逆转**（encode gb18030 → decode utf-8 strict）：无字节丢失的行可
   无损还原——包括 03:20 快照之后、腐蚀之前的当日真实改动（standby 胶囊 / 升级
   弹窗 / 徽章改造等），这是比「回滚快照」保真得多的主路径。
2. 逆转失败的行（字节已丢）：在 difflib opcode 给定的基底候选区间内按
   **ASCII 骨架**（剔除 CJK/PUA/'?' 后的骨架）对齐，相似度过阈即还原基底原文
   （03:20 快照）；对齐不上的点名人工。
3. 纯 ASCII / 本批(14:20)插入的真 UTF-8 中文行原样保留。

用法：python tools/_recover_unified_inbox_20260822.py [--apply]
默认干跑出报告。恢复后本脚本即事故现场记录，勿复用于其它文件。
"""
from __future__ import annotations

import difflib
import sys

BASE = "desktop/build/backend-dist/_internal/src/web/templates/unified_inbox.html"
# 输入钉死到腐蚀现场备份（14:38 快照，含当日全部内容的乱码态）——живой文件
# 已被肇事线 14:49 用旧缓冲重存（乱码清了、当日增量也丢了），不再是恢复输入。
import glob as _glob
CUR = sorted(_glob.glob(
    r"C:\Users\Administrator\AppData\Local\Temp\unified_inbox.corrupted.*.html"))[-1]
OUT = r"C:\Users\Administrator\AppData\Local\Temp\unified_inbox.reconstructed.html"

#: 本批（14:20，腐蚀之后）插入的真中文行标记——不得被逆转
MY_MARKERS = ("实施56 P1", "media_block:1", "ws.delivblock.d.'+fmt.domain")


def corrupt_v1(s: str) -> str:
    return s.encode("utf-8").decode("gb18030", errors="replace").replace(
        "\ufffd", "?")


def try_reverse(s: str):
    try:
        return s.encode("gb18030").decode("utf-8"), True
    except Exception:
        return s, False


def skel(s: str) -> str:
    """ASCII 骨架：剔 CJK/PUA/替换符/问号（腐蚀只伤这些位置附近）。"""
    return "".join(
        c for c in s
        if ord(c) < 128 and c != "?"
    )


def has_cjk(s: str) -> bool:
    return any(0x3000 <= ord(c) <= 0x9FFF or 0xE000 <= ord(c) <= 0xF8FF
               or c == "\ufffd" for c in s)


def main() -> int:
    apply = "--apply" in sys.argv
    base = open(BASE, "rb").read().decode("utf-8").split("\n")
    cur = open(CUR, "rb").read().decode("utf-8").split("\n")
    tb = [corrupt_v1(b) for b in base]
    sm = difflib.SequenceMatcher(None, tb, cur, autojunk=False)

    restored: list[str] = []
    stats = {"equal": 0, "reversed": 0, "ascii_kept": 0, "mine_kept": 0,
             "base_aligned": 0, "flagged": 0}
    flagged: list[tuple[int, str]] = []
    aligned_rows: list[tuple[int, str, str]] = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            restored.extend(base[i1:i2])
            stats["equal"] += i2 - i1
            continue
        if tag == "delete":
            continue  # 现场已不存在的基底行（真实删除或对齐噪声，尊重现场）
        cand = base[i1:i2]           # 本区间的基底候选（replace 时非空）
        cand_used = [False] * len(cand)
        for j in range(j1, j2):
            ln = cur[j]
            if any(m in ln for m in MY_MARKERS):
                restored.append(ln)
                stats["mine_kept"] += 1
                continue
            if not has_cjk(ln):
                restored.append(ln)
                stats["ascii_kept"] += 1
                continue
            rev, ok = try_reverse(ln)
            if ok:
                restored.append(rev)
                stats["reversed"] += 1
                continue
            # 字节已丢：在候选区间按骨架对齐还原基底原文
            sk = skel(ln)
            best, best_r, best_k = None, 0.0, -1
            for k, b in enumerate(cand):
                if cand_used[k]:
                    continue
                r = difflib.SequenceMatcher(None, sk, skel(b)).ratio()
                if r > best_r:
                    best, best_r, best_k = b, r, k
            if best is not None and best_r >= 0.75:
                restored.append(best)
                cand_used[best_k] = True
                stats["base_aligned"] += 1
                aligned_rows.append((j + 1, f"{best_r:.2f}", best.strip()[:90]))
            else:
                restored.append(ln)
                stats["flagged"] += 1
                flagged.append((j + 1, ln.strip()[:110]))

    print("stats:", stats)
    print("--- base-aligned (lossy lines restored from 03:20 snapshot) ---")
    for row in aligned_rows[:60]:
        print("  ", *row)
    if len(aligned_rows) > 60:
        print("   ... +", len(aligned_rows) - 60, "more")
    print("--- FLAGGED（残余乱码，需人工）---")
    for row in flagged:
        print("  !!", *row)

    out = "\n".join(restored)
    # 结果自检：还原文本不得再命中乱码签名（PUA/替换符）
    leftover = [i + 1 for i, l in enumerate(restored)
                if any(0xE000 <= ord(c) <= 0xF8FF for c in l) or "\ufffd" in l]
    print("leftover mojibake-signature lines:", len(leftover), leftover[:20])
    if apply:
        open(OUT, "wb").write(out.encode("utf-8"))
        print("RECONSTRUCTED ->", OUT, "lines:", len(restored))
    else:
        print("dry-run only (use --apply)")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
