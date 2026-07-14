# -*- coding: utf-8 -*-
"""diag_pack.py — 一键诊断包（客服支持的「发截图」升级为「发诊断包」）。

设计红线：
  * 纯标准库，Hub 不在线也能完整收集（失败时刻恰恰是 Hub 挂了的时刻）；
  * 脱敏优先：config 中含 token/key/secret/password 的值一律替换；license.key
    （授权私料）与音色/人脸等内容资产【绝不】进包——诊断包只讲「环境与故障」；
  * 日志只取每文件尾部 512KB，包体控制在几 MB 内，TG/微信直传不超限；
  * 产物落桌面（小白找得到），文件名带时间戳，可重复生成互不覆盖。
"""
from __future__ import annotations

import io
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import app_config

_TAIL_BYTES = 512 * 1024          # 每份日志取尾部大小
_SENSITIVE = ("token", "secret", "password", "api_key", "apikey", "admin_key", "私钥")


def _redact(obj):
    """递归脱敏：命中敏感键名的值替换为占位符（结构保留，便于排查配置形状）。"""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(s in str(k).lower() for s in _SENSITIVE):
                out[k] = "«已脱敏»"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


def _tail_file(p: Path, limit: int = _TAIL_BYTES) -> bytes:
    try:
        size = p.stat().st_size
        with p.open("rb") as f:
            if size > limit:
                f.seek(size - limit)
                return b"...(truncated, tail %dKB)...\r\n" % (limit // 1024) + f.read()
            return f.read()
    except Exception as e:
        return f"<read failed: {e}>".encode("utf-8")


def _gpu_info() -> dict:
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,driver_version",
             "--format=csv,noheader"],
            capture_output=True, text=True, timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"ok": r.returncode == 0, "raw": (r.stdout or r.stderr).strip()}
    except Exception as e:
        return {"ok": False, "raw": f"nvidia-smi unavailable: {e}"}


def _port_probe() -> dict:
    """各服务端口的 TCP 可达性（不依赖 HTTP 语义，服务假死也能看出监听消失）。"""
    out = {}
    for name, svc in app_config.SERVICES.items():
        port = svc.get("port")
        try:
            with socket.create_connection(("127.0.0.1", int(port)), timeout=1.2):
                out[name] = {"port": port, "listening": True}
        except Exception:
            out[name] = {"port": port, "listening": False}
    return out


def _health_snapshot() -> dict:
    """Hub /health 快照（best-effort，2s 超时；拿不到就记原因）。"""
    try:
        from urllib.request import urlopen
        with urlopen(app_config.svc_url("hub") + "/health", timeout=2.5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return {"unavailable": str(e)}


def _license_public() -> dict:
    try:
        import license as lic
        return lic.summary()          # 公开视图：不含任何密钥材料
    except Exception as e:
        return {"unavailable": str(e)}


def build_diag_pack(app_version: str = "", out_dir: "Path | None" = None) -> Path:
    """收集并打包，返回 zip 路径。任何单项失败都不中断整体（残缺包好过没有包）。"""
    base: Path = app_config.BASE
    ts = time.strftime("%Y%m%d-%H%M%S")
    if out_dir is None:
        out_dir = Path.home() / "Desktop"
        if not out_dir.is_dir():
            out_dir = Path(os.environ.get("TEMP", str(base)))
    out = Path(out_dir) / f"无界诊断包-{ts}.zip"

    meta = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "app_version": app_version,
        "python": sys.version,
        "frozen": bool(getattr(sys, "frozen", False)),
        "os": f"{platform.system()} {platform.release()} ({platform.version()})",
        "machine": platform.machine(),
        "base_dir": str(base),
        "disk_free_gb": round(shutil.disk_usage(str(base)).free / 2**30, 1),
        "gpu": _gpu_info(),
    }
    try:
        import license as lic
        meta["fingerprint"] = lic.machine_fingerprint()   # 公开指纹：客服续费/查订单要用
    except Exception:
        pass

    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        def put_json(name: str, obj):
            try:
                z.writestr(name, json.dumps(obj, ensure_ascii=False, indent=2, default=str))
            except Exception as e:
                z.writestr(name, json.dumps({"collect_failed": str(e)}))

        put_json("meta.json", meta)
        put_json("license_public.json", _license_public())
        put_json("ports.json", _port_probe())
        put_json("hub_health.json", _health_snapshot())

        # 配置（脱敏）与组件登记：定位「装了什么版本的什么」
        for cfg_name in ("config.json", "dist/manifest.json", "manifest.json",
                         "data/brand.json"):
            p = base / cfg_name
            if p.exists():
                try:
                    put_json("config/" + cfg_name.replace("/", "_"),
                             _redact(json.loads(p.read_text(encoding="utf-8"))))
                except Exception as e:
                    z.writestr("config/" + cfg_name.replace("/", "_") + ".err", str(e))

        # 日志尾部（logs/*.log 与 *.log.1 一并带上，覆盖轮转边界的现场）。
        # 跳过 _ 前缀的开发/调试草稿日志（与 _*.py 同约定，客户机本就没有；只带产品运行日志）。
        logs = base / "logs"
        if logs.is_dir():
            for p in sorted(logs.glob("*.log*")):
                if p.is_file() and not p.name.startswith("_"):
                    z.writestr(f"logs/{p.name}", _tail_file(p))
    return out


def upload_diag_pack(zip_path: "Path | str", app_version: str = "",
                     timeout: float = 60.0) -> tuple[bool, str]:
    """把诊断包直传客服后端，成功返回 (True, 六位诊断码)；失败 (False, 原因)。
    端点复用授权服务器地址（同一后端，白标改一处即两处生效）；未配置/网络断=优雅失败，
    调用方回退「手动发文件」动线。绝不抛异常。"""
    try:
        import license as lic
        base = lic._activation_url("")
        if not base:
            return False, "未配置支持服务器"
        meta: dict = {"app": app_version}
        try:
            meta["fp"] = lic.machine_fingerprint()[:16]   # 前缀足够客服对人，不传全指纹
        except Exception:
            pass
        from urllib.request import Request, urlopen
        data = Path(zip_path).read_bytes()
        if len(data) > 15 * 1024 * 1024:
            return False, "诊断包超过 15MB，请手动发送文件"
        req = Request(base.rstrip("/") + "/api/diag-upload", data=data, method="POST",
                      headers={"Content-Type": "application/zip",
                               "X-Diag-Meta": json.dumps(meta, ensure_ascii=False)})
        with urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode("utf-8"))
        if resp.get("ok") and resp.get("code"):
            return True, str(resp["code"])
        return False, str(resp.get("error") or "服务器未接受")
    except Exception as e:
        return False, f"{e}"


if __name__ == "__main__":
    p = build_diag_pack(app_version="cli")
    print(p)
    ok, msg = upload_diag_pack(p, app_version="cli")
    print("upload:", ok, msg)
