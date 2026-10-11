# -*- coding: utf-8 -*-
"""打包前安装种子边车依赖。不要放进单元回归。

WhatsApp 协议边车：``npm ci``
Messenger 网页边车：``npm ci``，然后
``PLAYWRIGHT_BROWSERS_PATH=0 npx playwright install chromium``
（完整 Chromium，不是 headless shell；浏览器落在该包的 node_modules 里，
与 ``test_seed_modes_dependencies_are_bundled`` 的判据一致）。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _npm() -> str:
    name = "npm.cmd" if os.name == "nt" else "npm"
    found = shutil.which(name) or shutil.which("npm")
    if not found:
        raise SystemExit("npm 不在 PATH 上，无法安装种子边车")
    return found


def _run(cwd: Path, args: list, env: dict | None = None) -> None:
    merged = os.environ.copy()
    if env:
        merged.update(env)
    print("+", " ".join(args), "cwd=", cwd)
    subprocess.check_call(args, cwd=str(cwd), env=merged)


def main() -> int:
    npm = _npm()
    wa = ROOT / "services" / "whatsapp-baileys"
    msg = ROOT / "services" / "messenger-web"
    if not (wa / "package.json").is_file() or not (msg / "package.json").is_file():
        raise SystemExit(f"边车目录不完整：{wa} / {msg}")
    _run(wa, [npm, "ci"])
    # postinstall 会再跑一遍 playwright install；环境变量必须在 npm ci 时就在，
    # 否则浏览器落到缓存目录，不进 node_modules/playwright-core/.local-browsers。
    browsers = {"PLAYWRIGHT_BROWSERS_PATH": "0"}
    _run(msg, [npm, "ci"], browsers)
    _run(msg, [npm, "exec", "--", "playwright", "install", "chromium"], browsers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
