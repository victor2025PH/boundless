"""把节点 Agent（src/fleet/agent.py，标准库 + PyYAML）打成单文件 chatx-agent(.exe)，并产出发布清单。

    cd engines/chengjie
    python fleet_agent/build_agent.py [--clean] [--base-url https://bd2026.cc/downloads/fleet/]

产出（fleet_agent/dist/）：
    chatx-agent.exe            单文件（Windows 上打；Linux 上打出 chatx-agent）
    chatx-agent.exe.sha256
    manifest.json              {version, file, url, sha256, size, built_at}   ← admin upgrade / 下载页 / fleet_control.download 共用
    Install-ChatXAgent.ps1 / Uninstall-ChatXAgent.ps1   随包复制

与桌面端 build_backend.py 分开：Agent 不含任何业务代码与数据，包体 ~10MB，机房机不必装整套智聊也能受控。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENGINE = HERE.parent
DIST = HERE / "dist"
NAME = "chatx-agent"


def agent_version() -> str:
    m = re.search(r'^AGENT_VERSION\s*=\s*"([^"]+)"', (ENGINE / "src/fleet/agent.py").read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else "0.0.0"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(1 << 20), b""):
            h.update(c)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clean", action="store_true")
    ap.add_argument("--base-url", default="https://bd2026.cc/downloads/fleet/", help="manifest.url 的前缀")
    ap.add_argument("--skip-build", action="store_true", help="只重算 sha256 / manifest（已有 exe）")
    args = ap.parse_args()

    if args.clean:
        shutil.rmtree(DIST, ignore_errors=True)
        shutil.rmtree(HERE / "build", ignore_errors=True)
    DIST.mkdir(parents=True, exist_ok=True)
    exe = DIST / (NAME + (".exe" if os.name == "nt" else ""))

    if not args.skip_build:
        if importlib.util.find_spec("PyInstaller") is None:
            print("✗ 未安装 PyInstaller：pip install pyinstaller", file=sys.stderr)
            return 2
        cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--console",
               "--name", NAME, "--paths", str(ENGINE), "--hidden-import", "yaml",
               "--distpath", str(DIST), "--workpath", str(HERE / "build"), "--specpath", str(HERE / "build"),
               str(HERE / "entry.py")]
        print("→", " ".join(cmd))
        if subprocess.run(cmd, cwd=str(ENGINE)).returncode != 0:
            return 1
        if not exe.exists():
            print(f"✗ 未生成 {exe}", file=sys.stderr)
            return 1
        smoke = subprocess.run([str(exe), "--help"], capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=60)
        if smoke.returncode != 0 or "enroll" not in (smoke.stdout or ""):
            print(f"✗ smoke --help 失败 rc={smoke.returncode}\n{smoke.stdout}\n{smoke.stderr}", file=sys.stderr)
            return 1

    digest = sha256(exe)
    (DIST / (exe.name + ".sha256")).write_text(f"{digest}  {exe.name}\n", encoding="utf-8")
    for ps1 in ("Install-ChatXAgent.ps1", "Uninstall-ChatXAgent.ps1"):
        shutil.copy2(HERE / ps1, DIST / ps1)
    manifest = {
        "name": NAME, "version": agent_version(), "file": exe.name,
        "url": args.base_url.rstrip("/") + "/" + exe.name, "sha256": digest, "size": exe.stat().st_size,
        "os": platform.system().lower(), "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "installer": args.base_url.rstrip("/") + "/Install-ChatXAgent.ps1",
    }
    (DIST / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
