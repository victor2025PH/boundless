# -*- coding: utf-8 -*-
r"""platform/chain_selftest.py — 产业链条『总线可选/各自独立』总闸自测。

一句话：把 platform 下每条契约的 stdlib 瘦客户端在【总线不可达】下各跑一遍，
断言它们**全部优雅降级、无一抛异常、退出码 0**——这就是"每个项目都能各自独立
运行"的机器可证明版本（获客→承接→赋能→授权 整条链断开总线也不崩）。

自动发现：扫描 platform/*/ 下的 client.py 与 *_client.py，逐个以子进程跑其自测
（`--selftest`，不支持则裸跑），因此新增契约（如 replybus/licensing）落地后无需改本文件。

依赖铁律：只用 stdlib（subprocess/os/sys）；不 import 任何契约客户端（各客户端有
标准库外零依赖，但用子进程隔离，避免顶层目录 platform 与标准库同名的 import 陷阱）。

用法：
    python platform/chain_selftest.py            # 单机模式跑全链降级自测
    python platform/chain_selftest.py --list     # 只列出发现的契约客户端
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_PLATFORM_DIR = Path(__file__).resolve().parent

# 已知契约客户端的调用方式（label, 相对路径, 传给脚本的参数）。
# 未在此登记但符合 *client*.py 命名的会被自动补充（裸跑）。
_KNOWN = [
    ("compliance 合规验真", "compliance/client.py", []),
    ("leadbus 线索总线", "leadbus/client.py", ["--selftest"]),
    ("enable 赋能网关", "enable/client.py", []),
    ("replybus 决策回执", "replybus/client.py", ["--selftest"]),
    ("licensing 授权收款", "licensing/license_client.py", ["--selftest"]),
    ("observability 事件发射", "observability/emitter.py", ["--selftest"]),
]


def _discover():
    """返回 [(label, abspath, args)]；已知的按登记顺序，其余 client 自动追加。"""
    found = []
    seen = set()
    for label, rel, args in _KNOWN:
        p = _PLATFORM_DIR / rel
        if p.exists():
            found.append((label, p, args))
            seen.add(p.resolve())
    # 自动发现未登记的 *client*.py（新契约落地即被覆盖）
    for sub in sorted(_PLATFORM_DIR.iterdir()):
        if not sub.is_dir():
            continue
        for f in sorted(sub.glob("*client*.py")):
            if f.resolve() not in seen:
                found.append((f"{sub.name}/{f.name}（自动发现）", f, []))
                seen.add(f.resolve())
    return found


def _run_one(path: Path, args) -> tuple[int, str]:
    """在【单机模式】跑一个客户端自测：清掉总线环境变量，子进程执行。"""
    env = dict(os.environ)
    # 抹掉所有可能让客户端进入"联网模式"的变量，强制单机降级路径
    for var in ("BOUNDLESS_BUS_URL", "AVATARHUB_BASE_URL", "CHENGJIE_BASE_URL",
                "LICENSE_SERVER_URL", "EVENT_INGEST_KEY"):
        env.pop(var, None)
    # 让子进程 stdout 用 UTF-8（各客户端 __main__ 已自处理，这里再兜一层）
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            [sys.executable, str(path), *args],
            cwd=str(path.parent), env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=60,
        )
        return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except Exception as e:  # 子进程都起不来才算失败
        return 99, f"子进程异常: {e!r}"


def main(argv) -> int:
    clients = _discover()
    print("== 无界产业链条 · 总线可选/各自独立 总闸自测 ==")
    print(f"platform 目录: {_PLATFORM_DIR}")
    print(f"发现契约客户端 {len(clients)} 个：")
    for label, path, args in clients:
        print(f"  · {label}  ({path.name} {' '.join(args)})")
    if argv[:1] == ["--list"]:
        return 0
    print("\n-- 单机模式（已清空总线环境变量）逐个降级自测 --")
    failures = []
    for label, path, args in clients:
        code, out = _run_one(path, args)
        ok = code == 0
        tag = "PASS" if ok else "FAIL"
        # 只取输出末行做摘要，避免刷屏
        last = ""
        for line in reversed(out.splitlines()):
            if line.strip():
                last = line.strip()
                break
        print(f"  [{tag}] {label}  exit={code}  «{last[:80]}»")
        if not ok:
            failures.append(label)
    print()
    if failures:
        print(f"== 结果：{len(failures)} 条契约未通过降级自测：{', '.join(failures)} ==")
        return 1
    print(f"== 结果：{len(clients)} 条契约全部优雅降级、无一抛异常 —— 断总线可各自独立运行 ✓ ==")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass
    raise SystemExit(main(sys.argv[1:]))
