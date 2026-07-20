# -*- coding: utf-8 -*-
"""admin_client.py — 产品端「联网注册」上报（官网后台机器名单的数据源）。

定位：产品启动/联网时，向官网后台（usdt2026.cc 的 POST /api/register）上报一份【机器管理信息】，
      让后台 /console 的「机器名单」自动建档并保持 last_seen 新鲜。这是【授权/资产管理通道】，
      与匿名遥测(telemetry_client)是两回事：这里如实带机器指纹（本就是授权身份），便于客服按机管理。

红线（与遥测同源加严）：
  * 只报机器管理面信息：指纹 / 主机名 / 系统 / GPU 型号 / 显存 / 版本 / 授权档位 / 匿名ID。
  * 绝不报：人脸/声音/视频内容、文件路径明细、令牌、license 私钥、聊天内容。
  * best-effort、后台线程、限频（默认 6 小时一次）；无地址/失败/异常一律静默，绝不拖累宿主。
  * 可关：AVATARHUB_ADMIN_REGISTER=0 一票关闭。

接入（一行，缺文件/异常完全无感）：
    try:
        import admin_client; admin_client.install("hub")
    except Exception:
        pass
"""
from __future__ import annotations

import json
import os
import platform
import socket
import threading
import time
import urllib.request
from pathlib import Path

try:
    import app_config
    BASE = Path(app_config.BASE)
except Exception:
    BASE = Path(__file__).resolve().parent

try:
    import license as _lic
except Exception:
    _lic = None

try:
    import telemetry as _tele
except Exception:
    _tele = None

STATE_FILE = BASE / "runtime" / "telemetry" / "admin_reg.json"
MIN_INTERVAL_H = 6


def enabled() -> bool:
    """联网注册开关：AVATARHUB_ADMIN_REGISTER=0/off/false 关；默认开（管理通道，非内容）。"""
    v = os.environ.get("AVATARHUB_ADMIN_REGISTER", "").strip().lower()
    return v not in ("0", "off", "false", "no")


def _admin_url() -> str:
    """后台地址：AVATARHUB_ADMIN_URL > config.json.admin_url > 复用激活地址（license._activation_url）。
    返回 "" = 未配置（不上报）。"""
    u = os.environ.get("AVATARHUB_ADMIN_URL", "").strip()
    if u:
        return "" if u.lower() in ("off", "none", "0") else u
    try:
        cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
        u = (cfg.get("admin_url") or "").strip()
        if u:
            return "" if u.lower() in ("off", "none", "0") else u
    except Exception:
        pass
    if _lic is not None:
        try:
            return _lic._activation_url()
        except Exception:
            pass
    return ""


def _gpu_info() -> tuple[str, float]:
    """(GPU 型号, 显存GB)。best-effort：nvidia-smi 一把，2s 超时，失败返回空。"""
    try:
        import subprocess
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 0 and r.stdout.strip():
            first = r.stdout.strip().splitlines()[0]
            parts = [x.strip() for x in first.split(",")]
            name = parts[0] if parts else ""
            vram = round(int(parts[1]) / 1024, 1) if len(parts) > 1 and parts[1].isdigit() else 0.0
            return name, vram
    except Exception:
        pass
    return "", 0.0


def _app_version() -> str:
    for name in ("app_build.json", "manifest.json"):
        try:
            d = json.loads((BASE / name).read_text(encoding="utf-8"))
            v = str(d.get("version", "") or "")
            if v:
                return v
        except Exception:
            pass
    return ""


def collect() -> dict:
    """采集一份机器管理信息（无内容、无 PII 明细）。"""
    fp, edition, status, anon = "", "", "", ""
    if _lic is not None:
        try:
            fp = _lic.machine_fingerprint()
            st = _lic.load_state()
            edition, status = st.edition, st.status
        except Exception:
            pass
    if _tele is not None:
        try:
            anon = _tele.anon_id()
        except Exception:
            pass
    gpu, vram = _gpu_info()
    try:
        osname = f"{platform.system()} {platform.release()}"
    except Exception:
        osname = ""
    try:
        host = socket.gethostname()
    except Exception:
        host = ""
    return {"fingerprint": fp, "hostname": host, "os": osname, "gpu": gpu, "vram_gb": vram,
            "app_version": _app_version(), "edition": edition, "status": status, "anon_id": anon}


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(d: dict):
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def register(service: str = "hub", force: bool = False, timeout: float = 8.0) -> bool:
    """上报一次（限频，默认 6h）。返回是否真的发出。绝不抛异常。"""
    try:
        if not enabled():
            return False
        url = _admin_url()
        if not url.startswith(("http://", "https://")):
            return False
        now = time.time()
        st = _load_state()
        if not force and now - float(st.get("ts", 0)) < MIN_INTERVAL_H * 3600:
            return False
        info = collect()
        if not info.get("fingerprint"):
            return False
        info["service"] = service
        endpoint = url.rstrip("/") + "/api/register"
        body = json.dumps(info).encode("utf-8")
        req = urllib.request.Request(endpoint, data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ok = 200 <= r.status < 300
        if ok:
            st["ts"] = now
            _save_state(st)
        return ok
    except Exception:
        return False


def install(service: str = "hub"):
    """后台【心跳】线程：启动即登记，之后每 MIN_INTERVAL_H 小时刷新一次 last_seen——
    让长期运行的机器在后台「机器」页保持"在线"（一次性登记会 24h 后显示离线）。
    register() 自带 6h 文件级限频，故频繁重启也不会刷屏；缺地址/失败/异常全静默，不阻塞宿主。"""
    if not enabled():
        return

    def _loop():
        while True:
            try:
                register(service)          # 非强制：首次即登记，之后靠 6h 限频节流
            except Exception:
                pass
            time.sleep(max(1, MIN_INTERVAL_H) * 3600)

    threading.Thread(target=_loop, daemon=True, name="admin-register").start()


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(json.dumps(collect(), ensure_ascii=False, indent=2))
    print("上报结果:", register("cli", force=True))
