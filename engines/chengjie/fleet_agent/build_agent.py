"""把节点 Agent（src/fleet/agent.py，标准库 + PyYAML）打成单文件 chatx-agent(.exe)，并产出发布清单。

    cd engines/chengjie
    python fleet_agent/build_agent.py [--clean] [--base-url https://bd2026.cc/downloads/fleet/]

产出（fleet_agent/dist/）：
    chatx-agent.exe            单文件（Windows 上打；Linux 上打出 chatx-agent）
    chatx-agent.exe.sha256
    manifest.json              {version, file, url, sha256, size, built_at}   ← admin upgrade / 下载页 / fleet_control.download 共用
    Install-ChatXAgent.ps1 / Uninstall-ChatXAgent.ps1   随包复制

安装包 ChatXAgentSetup.exe 不在这一步里打（需要 Windows + 免费的 Inno Setup）：
    powershell -File fleet_agent\\build_setup.ps1
publish_agent.ps1 在找到 ISCC.exe 时会自己调用它，并把 setup_url / setup_sha256 写进 manifest.json。

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
# PyInstaller 6.22 onefile does not extract pure-Python modules. The frozen
# loader sets phone_flows.__file__ to <_MEIPASS>/src/fleet/phone_flows.py
# (a virtual path). --add-data DEST is a directory under _MEIPASS; joining
# the source basename puts the JSON at <_MEIPASS>/src/fleet/phone_ui_map.json,
# which Path(__file__).with_name(...) finds. DEST uses forward slashes;
# Windows os.path.normpath turns them into src\fleet, matching os.sep in
# __file__. The CLI separator is os.pathsep (';' on Windows, ':' on POSIX).
# Checked with a 6.22.3 onefile probe of src.fleet.phone_flows: sibling
# phone_ui_map.json existed and Path.with_name(...).is_file() was True.
UI_MAP_REL = Path("src") / "fleet" / "phone_ui_map.json"
UI_MAP_DEST = "src/fleet"


def ui_map_source() -> Path:
    return ENGINE / UI_MAP_REL


def ui_map_add_data() -> str:
    src = ui_map_source()
    if not src.is_file():
        raise SystemExit(f"missing {src}")
    return f"{src}{os.pathsep}{UI_MAP_DEST}"


def like_templates_add_data() -> str:
    """Light/dark thumbs-up rasters. Same _MEIPASS dir as phone_ui_map.json."""
    src = ENGINE / "src" / "fleet" / "like_templates"
    if not src.is_dir() or not any(src.glob("like_*.png")):
        raise SystemExit(f"missing like templates in {src}")
    return f"{src}{os.pathsep}{UI_MAP_DEST}/like_templates"


def ocr_collect_flags() -> list:
    """Bundle rapidocr only when it is installed. The exe still builds without it.

    Icon templates and the action-bar fallback do not need the package. Text
    (Like / Gusto / 赞) is an optional signal. Windows.Media.Ocr is not used:
    room PCs often lack the Tagalog and Chinese language packs.
    """
    flags = []
    for mod in ("rapidocr_onnxruntime", "onnxruntime", "cv2"):
        if importlib.util.find_spec(mod) is not None:
            flags.extend(["--collect-all", mod])
    if flags:
        print("rapidocr stack found; PyInstaller will collect it (onefile grows by the wheel set)")
    else:
        print("rapidocr_onnxruntime not installed; Facebook like keeps templates, text signal omitted")
    return flags


def assert_phone_flows_startup() -> None:
    """Refuse to package an agent whose PhoneFlows constructor drops state_dir.

    NodeAgent passes the fleet state directory at process start. A binary whose
    frozen PhoneFlows.__init__ does not accept that keyword exits immediately.
    """
    engine = str(ENGINE)
    if engine not in sys.path:
        sys.path.insert(0, engine)
    import inspect

    from src.fleet.agent import _phone_flows_settings
    from src.fleet.phone_flows import PhoneFlows

    settings = _phone_flows_settings({}, ENGINE)
    params = inspect.signature(PhoneFlows.__init__).parameters
    missing = [key for key in settings if key not in params]
    if missing or "state_dir" not in params:
        raise SystemExit(
            "PhoneFlows.__init__ must accept state_dir and the agent startup keys; "
            f"missing={missing or ['state_dir']}"
        )
    flows = PhoneFlows.from_agent_settings(settings, None)
    if Path(flows.state_dir or "") != ENGINE:
        raise SystemExit("PhoneFlows.from_agent_settings dropped state_dir")


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
    assert_phone_flows_startup()

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
               "--add-data", ui_map_add_data(),
               "--add-data", like_templates_add_data(),
               *ocr_collect_flags(),
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
    shutil.copy2(ui_map_source(), DIST / "phone_ui_map.json")
    platform_tools = HERE / "platform-tools"
    if (platform_tools / "adb.exe").is_file():
        dest_pt = DIST / "platform-tools"
        if dest_pt.exists():
            shutil.rmtree(dest_pt)
        shutil.copytree(platform_tools, dest_pt)
        print(f"staged platform-tools -> {dest_pt}")
    else:
        print("platform-tools/adb.exe not staged; phone-room bundle omitted "
              "(see fleet_agent/platform-tools/README.txt)")
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
