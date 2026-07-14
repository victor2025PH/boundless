# -*- coding: utf-8 -*-
"""
OBS 虚拟摄像头一键自检 / 引导
================================
直播/换脸出画依赖「OBS Virtual Camera」这一 DirectShow 虚拟设备(pyvirtualcam 后端)。
它是最常见的“开播失败”根因：未装 OBS、装了但从未点过一次“启动虚拟摄像头”、或被其他程序独占。

本模块提供：
  • check_obs(...) -> dict   结构化自检结果(供 Hub / UI / 验收脚本调用)
  • 命令行:  python obs_selfcheck.py [--json] [--no-probe]
             探测通过 exit 0；不可用 exit 2。

探测策略：真正“打开一次”虚拟摄像头是最可靠的判据；据异常信息细分“未安装 / 被占用 / 其他”，
并给出可执行的中文指引。探测仅持续毫秒级，随即释放，不影响其他程序。
"""
from __future__ import annotations
import sys
import platform


def _detect_obs_install() -> dict:
    """尽力探测 OBS 是否安装(仅作辅助指引，真正判据是能否打开虚拟摄像头)。"""
    info = {"installed": None, "paths": []}
    sysname = platform.system()
    try:
        if sysname == "Windows":
            import os
            cand = [
                r"C:\Program Files\obs-studio",
                r"C:\Program Files (x86)\obs-studio",
            ]
            pf = os.environ.get("ProgramFiles")
            if pf:
                cand.append(os.path.join(pf, "obs-studio"))
            found = [p for p in dict.fromkeys(cand) if os.path.isdir(p)]
            info["paths"] = found
            reg_ok = False
            try:
                import winreg
                for hive, key in ((winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\OBS Studio"),
                                  (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\OBS Studio")):
                    try:
                        with winreg.OpenKey(hive, key):
                            reg_ok = True
                            break
                    except OSError:
                        continue
            except Exception:
                pass
            info["installed"] = bool(found) or reg_ok
        else:
            info["installed"] = None  # 非 Windows 不判定
    except Exception as e:
        info["error"] = str(e)
    return info


def _classify_probe_error(msg: str) -> tuple[str, list[str]]:
    """据打开虚拟摄像头的异常信息，判定状态并给出指引。返回 (status, guidance[])"""
    m = (msg or "").lower()
    busy_kw = ("in use", "already", "busy", "access", "被占用", "占用")
    missing_kw = ("not found", "no such", "no supported backend", "backend",
                  "could not find", "obs virtual", "not installed", "no virtual")
    if any(k in m for k in busy_kw):
        return "in_use", [
            "虚拟摄像头设备已存在，但当前被其他程序占用(很可能是本项目的换脸/数字人推流正在出画)。",
            "若要在本机再开一路，请先停止占用它的程序；直播软件里直接选「OBS Virtual Camera」即可取流。",
        ]
    if any(k in m for k in missing_kw):
        return "missing", [
            "未检测到「OBS Virtual Camera」设备。请安装 OBS Studio(≥26)。",
            "安装后打开 OBS，点右下角「开始虚拟摄像头(Start Virtual Camera)」至少一次以完成设备注册，随后可关闭 OBS。",
            "注册成功后本项目即可把换脸/数字人画面推入该虚拟摄像头。",
        ]
    return "error", [
        f"打开虚拟摄像头失败：{msg}",
        "常见修复：以管理员重装 OBS、重启电脑、或确认杀软未拦截虚拟摄像头驱动。",
    ]


def check_obs(width: int = 1280, height: int = 720, fps: int = 30, probe: bool = True) -> dict:
    """自检虚拟摄像头可用性。返回结构化结果 dict。"""
    res = {
        "ok": False,
        "os": platform.system(),
        "pyvirtualcam": False,
        "device": None,
        "status": "unknown",     # ok / in_use / missing / error / no_pyvirtualcam
        "obs": _detect_obs_install(),
        "guidance": [],
        "detail": "",
    }

    try:
        import pyvirtualcam  # noqa
        res["pyvirtualcam"] = True
    except Exception as e:
        res["status"] = "no_pyvirtualcam"
        res["detail"] = str(e)
        res["guidance"] = [
            "当前 Python 环境缺少 pyvirtualcam 库。",
            "请在 facefusion 环境安装：pip install pyvirtualcam",
        ]
        return res

    if not probe:
        res["status"] = "unknown"
        res["guidance"] = ["未执行打开探测(--no-probe)；pyvirtualcam 可用。"]
        return res

    try:
        import pyvirtualcam
        with pyvirtualcam.Camera(width=width, height=height, fps=fps, print_fps=False) as cam:
            res["device"] = getattr(cam, "device", None) or "OBS Virtual Camera"
            res["ok"] = True
            res["status"] = "ok"
            res["guidance"] = [
                f"虚拟摄像头就绪：{res['device']} ({width}x{height}@{fps})。",
                "直播/会议软件的「摄像头」里选择该设备即可看到换脸/数字人画面。",
            ]
    except Exception as e:
        status, guide = _classify_probe_error(str(e))
        res["status"] = status
        res["ok"] = (status == "in_use")   # 被占用视为“设备存在且可用”
        res["device"] = "OBS Virtual Camera" if status == "in_use" else None
        res["detail"] = str(e)
        res["guidance"] = guide
    return res


def _print_human(res: dict) -> None:
    icon = {"ok": "[OK]", "in_use": "[OK]", "missing": "[X]", "error": "[!]",
            "no_pyvirtualcam": "[X]", "unknown": "[i]"}.get(res["status"], "[i]")
    print(f"{icon} OBS 虚拟摄像头自检：{res['status']}  (设备={res.get('device') or '-'})")
    obs = res.get("obs") or {}
    if obs.get("installed") is True:
        print(f"   OBS 安装：已检测到 {('· ' + '; '.join(obs.get('paths', []))) if obs.get('paths') else ''}")
    elif obs.get("installed") is False:
        print("   OBS 安装：未在常见位置检测到")
    for g in res.get("guidance", []):
        print(f"   - {g}")


if __name__ == "__main__":
    try:                                   # 控制台可能是 GBK：兜底避免中文/符号编码崩溃
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = sys.argv[1:]
    as_json = "--json" in args
    do_probe = "--no-probe" not in args
    result = check_obs(probe=do_probe)
    if as_json:
        import json
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_human(result)
    sys.exit(0 if result["ok"] else 2)
