# -*- coding: utf-8 -*-
"""P13 运维一页纸巡检（离线门禁）：文档永不撒谎。

`授权运维一页纸_LICENSE_OPS.md` 是厂商侧交接底稿——里面的命令示例一旦与 CLI 脱节，
接手的人照抄就炸（P12 实锤：docstring 承诺的 serve --trial-days 曾是从未接线的空头支票，
serve 本体还因未定义常量 NameError 起不来）。本工具把文档拆成「可核验断言」逐条对账：

  1. 文档提到的 `license_server.py <子命令>` / `license_admin.py <子命令>` 必须真实存在
     （跑 `<tool> <sub> --help` 退出码 0）；
  2. 文档提到的所有 `--flag` 必须出现在对应工具的 argparse help 全集里；
  3. 文档提到的 HTTP 端点（/api/...、/dashboard）必须出现在 license_server.py 源码里。

纯离线（只跑 --help 不起服务），秒级，挂 gate Tier U。退出码：0=对账全平 1=文档撒谎。
"""
import re
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = Path(__file__).resolve().parent.parent
DOC = HERE / "授权运维一页纸_LICENSE_OPS.md"
PY = sys.executable

FAILS = []


def check(cond, name):
    print(("  ✓ " if cond else "  ✗ ") + name)
    if not cond:
        FAILS.append(name)


def helptext(tool: str, sub: str = "") -> str:
    """取 argparse help（--help 退出码 0 才算数）；失败返回空串。"""
    cmd = [PY, str(HERE / tool)] + ([sub] if sub else []) + ["--help"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=30, cwd=str(HERE))
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def main() -> int:
    if not DOC.is_file():
        print("SKIP: 一页纸不存在"); return 0
    doc = DOC.read_text(encoding="utf-8")

    # 1) 子命令存在性：文档里每个 `<tool> <sub>` 调用，--help 必须退出 0
    subs = {"license_server.py": sorted(set(re.findall(r"license_server\.py (\w[\w-]*)", doc))),
            "license_admin.py": sorted(set(re.findall(r"license_admin\.py (\w[\w-]*)", doc)))}
    corpus = {}   # tool -> 所有子命令 help 拼接（含主 help）
    for tool, ss in subs.items():
        corpus[tool] = helptext(tool)
        check(bool(corpus[tool]), f"{tool} --help 可运行")
        for s in ss:
            h = helptext(tool, s)
            check(bool(h), f"{tool} {s} 子命令存在且 --help 退出 0")
            corpus[tool] += h

    # 2) 旗标对账：文档中出现的 --flag 必须在「两工具 help 全集」中有出处
    #    （一页纸同段常混排两工具示例，按全集对账避免误伤；漏接线的旗标两边都查无此人）
    all_help = "".join(corpus.values())
    flags = sorted(set(re.findall(r"(--[a-z][a-z0-9-]+)", doc)))
    for fl in flags:
        check(fl in all_help, f"旗标 {fl} 在 CLI help 中有出处")

    # 3) 端点对账：文档提到的 HTTP 路径必须在 license_server.py 源码中存在
    src = (HERE / "license_server.py").read_text(encoding="utf-8")
    eps = sorted(set(re.findall(r"(/api/[a-z_/]+|/dashboard)", doc)))
    for ep in eps:
        check(ep in src, f"端点 {ep} 在 license_server.py 有实现")

    n_checks = len(subs['license_server.py']) + len(subs['license_admin.py']) + len(flags) + len(eps) + 2
    print(f"\n对账 {n_checks} 项 · 失败 {len(FAILS)}")
    if FAILS:
        print("文档与 CLI 脱节（修文档或补 CLI）：\n  - " + "\n  - ".join(FAILS))
        return 1
    print("== 一页纸对账全平（文档没撒谎）==")
    return 0


if __name__ == "__main__":
    sys.exit(main())
