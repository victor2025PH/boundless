#!/usr/bin/env python3
"""桌面随包种子「开关 ↔ 交付物」打包门禁（自 tests/test_desktop_seed_deliverable.py 迁入，2026-10-08）。

不变量（2026-07-30 客户侧实锤后定）：种子 ``config/config.desktop.min.yaml`` 里出现的
每一个非 device 接入方式，**它的运行时依赖必须真的在安装包里**——开了开关却没随包，
用户点下去等来 service_down，比灰着更让人困惑。

两档检查，同一张判据表 ``DELIVERABILITY``：

* ``declared``（CI 常驻，``tests/test_desktop_seed_deliverable.py`` 直接 import）：
  只看源码与声明——边车入口与 package-lock 在仓、``desktop/package.json`` 的
  ``build.extraResources`` 声明随包、requirements 启用对应 Python 依赖、
  build_backend 打进中央凭据池。CI 检出里天然没有 node_modules / 随包 Chromium，
  这一档不查它们。
* ``bundle``（打包流水线，``predist*`` 里紧跟 edition 门禁执行，**默认档**）：
  ``declared`` 全部 + 构建机上 ``services/*/node_modules`` 与 messenger-web 的
  随包兜底 Chromium 必须真实存在。任一缺失 → 退出码 1，打包中止。

产物侧还有 ``after-pack.js`` 再核一遍 ``resources/`` 里真有边车与 Chromium（第三道）。

用法（desktop/ 下）::

    python build/seed_deliverable_gate.py              # bundle 档（打包用）
    python build/seed_deliverable_gate.py --declared   # 只查声明（CI 同口径）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

ENGINE_ROOT = Path(__file__).resolve().parents[2]

LEVELS = ("declared", "bundle")


class _Paths:
    def __init__(self, root: Optional[Path] = None):
        self.root = Path(root or ENGINE_ROOT)
        self.seed = self.root / "config" / "config.desktop.min.yaml"
        self.desktop_pkg = self.root / "desktop" / "package.json"
        self.requirements = self.root / "requirements.txt"
        self.build_backend = self.root / "desktop" / "build" / "build_backend.py"

    def svc(self, name: str) -> Path:
        return self.root / "services" / name


def _extra_declared(p: _Paths, name: str) -> bool:
    pkg = json.loads(p.desktop_pkg.read_text(encoding="utf-8"))
    extra = ((pkg.get("build") or {}).get("extraResources") or [])
    return any(name in str(e.get("to") or e.get("from") or "")
               for e in extra if isinstance(e, dict))


def _node_sidecar(p: _Paths, name: str, level: str) -> List[str]:
    svc = p.svc(name)
    bad: List[str] = []
    if not (svc / "server.js").is_file():
        bad.append(f"services/{name}/server.js 不存在")
    if not (svc / "package-lock.json").is_file():
        bad.append(f"services/{name}/package-lock.json 不存在——构建机无法 `npm ci` 复现依赖")
    if not _extra_declared(p, name):
        bad.append(f"desktop/package.json 的 build.extraResources 没声明 {name}（不会进安装包）")
    if level == "bundle" and not (svc / "node_modules").is_dir():
        bad.append(f"services/{name}/node_modules 不存在——打包前须先 "
                   f"`cd services/{name} && npm ci`，否则边车随包后必 MODULE_NOT_FOUND")
    return bad


def wa_baileys(p: _Paths, level: str) -> List[str]:
    """WhatsApp 协议边车：源码 + 依赖 + extraResources 声明。"""
    return _node_sidecar(p, "whatsapp-baileys", level)


def messenger_web(p: _Paths, level: str) -> List[str]:
    """Messenger 托管登录：边车 + 依赖 + **兜底浏览器**。

    服务默认走系统真 Chrome，客户机没装 Chrome 时才回落随包 Chromium；构建机没跑过
    ``playwright install`` 就出包 = 没装 Chrome 的机器「点了登录、浏览器起不来」，
    而这在装了 Chrome 的开发机上永远复现不出来。
    """
    bad = _node_sidecar(p, "messenger-web", level)
    if level == "bundle" and (p.svc("messenger-web") / "node_modules").is_dir():
        browsers = p.svc("messenger-web") / "node_modules" / "playwright-core" / ".local-browsers"
        # `chromium-*` 只匹配完整（可 headed）构建；headless shell 是 chromium_headless_shell-*
        # （下划线），刻意不算——桌面登录是人工交互，要的是能显示窗口的那个。
        if not browsers.is_dir() or not any(browsers.glob("chromium-*")):
            bad.append("随包兜底 Chromium 缺失——打包前须在构建机跑 "
                       "`cd services/messenger-web && PLAYWRIGHT_BROWSERS_PATH=0 npx playwright "
                       "install chromium`（Windows 用 `$env:PLAYWRIGHT_BROWSERS_PATH='0'`）。"
                       "缺了它，没装 Chrome 的客户机点登录后浏览器起不来")
    return bad


def line_okline(p: _Paths, level: str) -> List[str]:
    """LINE 协议需 okline；它在 requirements.txt 里曾是**注释掉的**可选项。"""
    txt = p.requirements.read_text(encoding="utf-8", errors="replace")
    if not any(re.match(r"^\s*okline\b", ln) for ln in txt.splitlines()):
        return ["okline 未在 requirements.txt 启用（当前是注释行）→ 不会装进打包环境，"
                "LINE 协议方式在安装版恒不可用。它是逆向库、违反 LINE ToS 且有封号风险，"
                "是否随包分发属产品/法务决策"]
    return []


def telegram_protocol(p: _Paths, level: str) -> List[str]:
    """Telegram 协议：pyrogram 随包 + 中央凭据池瘦客户端随包（否则用户被逼自己申请 api_id）。"""
    txt = p.requirements.read_text(encoding="utf-8", errors="replace")
    if not any(re.match(r"^\s*pyrogram\b", ln) for ln in txt.splitlines()):
        return ["pyrogram 未在 requirements.txt 启用"]
    build = p.build_backend.read_text(encoding="utf-8", errors="replace")
    if "credpool" not in build:
        return ["desktop/build/build_backend.py 没把 platform/credpool 打进包 → 中央池静默失效，"
                "用户被逼去 my.telegram.org 自己申请 api_id"]
    return []


#: 每个非 device 方式的「运行时依赖在不在包里」判据（新方式进种子必须同时补判据）。
DELIVERABILITY: Dict[Tuple[str, str], Callable[[_Paths, str], List[str]]] = {
    ("telegram", "protocol"): telegram_protocol,
    ("whatsapp", "protocol"): wa_baileys,
    ("messenger", "web"): messenger_web,
    ("line", "protocol"): line_okline,
}


def seed_platform_login(root: Optional[Path] = None) -> dict:
    import yaml  # 构建机与 CI 都有（后端依赖）；缺了直接报错比静默放行好

    p = _Paths(root)
    return ((yaml.safe_load(p.seed.read_text(encoding="utf-8")) or {})
            .get("platform_login") or {})


def check(level: str = "bundle", root: Optional[Path] = None,
          platform_login: Optional[dict] = None) -> List[str]:
    """返回问题清单（空 = 通过）。"""
    if level not in LEVELS:
        raise ValueError(f"level 须为 {LEVELS}")
    p = _Paths(root)
    pl = seed_platform_login(root) if platform_login is None else platform_login
    problems: List[str] = []
    for platform, pcfg in pl.items():
        if not isinstance(pcfg, dict):
            continue
        for mode in (pcfg.get("modes") or []):
            if mode == "device":
                continue
            checker = DELIVERABILITY.get((platform, mode))
            if checker is None:
                problems.append(
                    f"{platform}/{mode}：本门禁没有它的「依赖在不在包里」判据，"
                    "请在 DELIVERABILITY 里补一条（新方式进种子必须同时补判据）")
                continue
            for why in checker(p, level):
                problems.append(f"{platform}/{mode}：{why}")
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--declared", action="store_true",
                    help="只查源码与声明（CI 同口径）；默认 bundle 档（打包用，含 node_modules/Chromium）")
    ap.add_argument("--root", type=Path, default=None, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    level = "declared" if args.declared else "bundle"
    problems = check(level, root=args.root)
    if problems:
        print(f"[seed_deliverable_gate] FAIL（{level}）：种子开了方式但交付物不在安装包里"
              "（用户点下去会等来 service_down）：", file=sys.stderr)
        for x in problems:
            print(f"  - {x}", file=sys.stderr)
        return 1
    print(f"[seed_deliverable_gate] OK（{level}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
