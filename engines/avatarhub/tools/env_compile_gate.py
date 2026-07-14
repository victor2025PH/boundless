# -*- coding: utf-8 -*-
"""环境完整性编译门禁：对指定 Python 环境的 site-packages 全量 py_compile。

背景（2026-07-05 两起同族事故）：
  - .140 conda-pack 解包后 huggingface_hub 等 3 个 .py 损坏（unterminated string literal），
    服务起不来但 /health 还 200 —— 全靠 compileall 定位。
  - .117 conda pkgs 缓存损坏（pip/setuptools CondaVerificationError）。
结论：环境迁移（conda-pack / scp / 解压）后，site-packages 不可信，先过这道闸再起服务。

用法：
  # 在目标机、用目标环境的解释器跑（推荐，零依赖）：
  D:\\faceX\\Miniconda3\\envs\\nemoasr\\python.exe tools/env_compile_gate.py
  # 或在任意环境指定要检的解释器：
  python tools/env_compile_gate.py --py D:\\faceX\\Miniconda3\\envs\\cosytts\\python.exe
  # 忽略已知噪声（某些包故意带坏语法的测试样张）：
  python tools/env_compile_gate.py --ignore "*badsyntax*" --ignore "*/lib2to3/tests/data/*"

退出码：0=全绿  1=有编译失败（输出损坏文件清单）  2=用法/环境错误
"""
import argparse
import fnmatch
import re
import subprocess
import sys
from pathlib import Path

# 已知良性噪声：标准库/常见包自带的「故意坏语法」测试样张 / 高版本专属语法文件
DEFAULT_IGNORES = [
    "*badsyntax*",
    "*/lib2to3/tests/data/*",
    "*/test/bad_coding*",
    "*/torch/testing/_internal/py312_intrinsics.py",   # py3.12 语法，低版本必然编不过（按需加载）
    "*/mediapipe/tasks/python/test/*",                 # 测试样张带 U+2202 等非法标识符字符
]


def site_packages_of(py: str) -> list[str]:
    """问目标解释器要它的 site-packages 路径列表。"""
    code = "import site, json; print(json.dumps(site.getsitepackages()))"
    r = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError(f"目标解释器不可用: {r.stderr.strip()[:200]}")
    import json
    return [p for p in json.loads(r.stdout) if Path(p).is_dir()]


def compile_tree(py: str, root: str) -> list[tuple[str, str]]:
    """用目标解释器对 root 做 compileall（-q 只报错误），返回 [(文件, 错误首行)]。"""
    r = subprocess.run([py, "-m", "compileall", "-q", root],
                       capture_output=True, text=True, timeout=1800)
    failures: list[tuple[str, str]] = []
    cur_file = ""
    for line in (r.stdout or "").splitlines() + (r.stderr or "").splitlines():
        s = line.strip()
        if s.startswith("*** Error compiling"):
            cur_file = s.split("'")[1] if "'" in s else s
        elif cur_file and ("Error:" in s or "error:" in s):
            failures.append((cur_file, s[:160]))
            cur_file = ""
    # compileall 返回非 0 但没解析到明细时，整树标记
    if r.returncode != 0 and not failures:
        failures.append((root, f"compileall 退出码 {r.returncode}（明细见手工复跑输出）"))
    return failures


def main() -> int:
    ap = argparse.ArgumentParser(description="site-packages 编译完整性门禁")
    ap.add_argument("--py", default=sys.executable, help="要体检的 Python 解释器（默认当前）")
    ap.add_argument("--ignore", action="append", default=[],
                    help="额外忽略的 glob（可多次），已内置常见坏语法测试样张")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    ignores = DEFAULT_IGNORES + list(args.ignore)
    try:
        roots = site_packages_of(args.py)
    except Exception as e:
        print(f"[gate] {e}")
        return 2
    if not roots:
        print("[gate] 未找到 site-packages")
        return 2

    all_fail: list[tuple[str, str]] = []
    for root in roots:
        print(f"[gate] compileall {root} ...", flush=True)
        for f, msg in compile_tree(args.py, root):
            # compileall 输出的是 repr 路径（\\ 双写）→ 归一化成单斜杠再比对
            norm = re.sub(r"[\\/]+", "/", f)
            if any(fnmatch.fnmatch(norm, pat) for pat in ignores):
                continue
            all_fail.append((f, msg))

    if all_fail:
        print(f"\n[gate] ✗ 发现 {len(all_fail)} 个损坏 .py（环境不可信，先修复再起服务）：")
        for f, msg in all_fail[:50]:
            print(f"  - {f}\n      {msg}")
        print("\n修复建议：从健康环境 scp 覆盖同名文件，或 pip install --force-reinstall 对应包。")
        return 1
    print("[gate] ✓ site-packages 全部可编译，环境完整。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
