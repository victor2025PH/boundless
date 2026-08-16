#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenClaw 主机任务 API 入口。"""

import os
import sys
import logging
from pathlib import Path

project_root = Path(__file__).resolve().parent
sys.path.insert(0, str(project_root))

(project_root / "logs").mkdir(exist_ok=True)
(project_root / "logs" / "screenshots").mkdir(exist_ok=True)


def _load_launch_env():
    """把 config/launch.env（KEY=VAL）补进进程环境（已有的环境变量优先）。

    2026-08-17 实锤：launch.env 此前只被 start.ps1 加载——手动 `python server.py`
    直起时读不到，端口回落默认 18080，而文档/旧习惯记的是 8000；两种起法端口
    会漂，重启一次端口一变，打开着的控制台整页变孤儿（Failed to fetch）。
    server.py 自己也加载它之后，无论哪种起法端口都同源于 launch.env。"""
    try:
        env_file = project_root / "config" / "launch.env"
        if not env_file.exists():
            return
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except Exception as e:
        logging.getLogger("openclaw").warning("launch.env 加载跳过: %s", e)


def _bootstrap_adb_path():
    """把 config/devices.yaml 的 adb_path 所在目录前插进程 PATH。

    2026-08-17 真机灰度首跑实锤：preflight/watchdog/health_monitor 等
    7 个文件几十处 subprocess 裸调 "adb"（要求在 PATH），而 DeviceManager
    是从配置读完整路径——服务进程 PATH 无 adb 时，任务预检门全体
    WinError 2 拦截（4 台设备 tiktok_warmup 全军覆没）。
    逐处改超范围且易复发，入口一处注入 PATH = 所有裸调一次全治，
    adb 位置的单一真相仍在 devices.yaml。
    """
    try:
        import yaml
        cfg_file = project_root / "config" / "devices.yaml"
        with open(cfg_file, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        adb_path = str(data.get("adb_path") or "")
        if not adb_path:
            for section in data.values():
                if isinstance(section, dict) and section.get("adb_path"):
                    adb_path = str(section["adb_path"])
                    break
        if adb_path and adb_path.lower() not in ("adb", "adb.exe"):
            adb_dir = str(Path(adb_path).parent)
            if Path(adb_dir).is_dir() and adb_dir not in os.environ.get("PATH", ""):
                os.environ["PATH"] = adb_dir + os.pathsep + os.environ.get("PATH", "")
                logging.getLogger("openclaw").info("PATH 已注入 adb 目录: %s", adb_dir)
    except Exception as e:
        logging.getLogger("openclaw").warning("adb PATH 注入跳过: %s", e)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(project_root / "logs" / "host_api.log"),
    ],
)

_load_launch_env()
_bootstrap_adb_path()


def main():
    import uvicorn

    from src.openclaw_env import openclaw_port

    host = os.environ.get("OPENCLAW_HOST", "0.0.0.0")
    port = openclaw_port()

    # TLS 配置
    ssl_keyfile = None
    ssl_certfile = None
    cert_dir = project_root / "config" / "certs"

    if os.environ.get("OPENCLAW_TLS", "").lower() in ("1", "true", "yes"):
        key_path = cert_dir / "server.key"
        crt_path = cert_dir / "server.crt"
        if key_path.exists() and crt_path.exists():
            ssl_keyfile = str(key_path)
            ssl_certfile = str(crt_path)
            logging.getLogger("openclaw").info(f"TLS 已启用: {crt_path}")
        else:
            logging.getLogger("openclaw").warning(
                "OPENCLAW_TLS=1 但未找到证书文件。"
                "运行 python scripts/generate_certs.py 生成。"
            )

    uvicorn.run(
        "src.host.api:app",
        host=host,
        port=port,
        reload=False,
        ssl_keyfile=ssl_keyfile,
        ssl_certfile=ssl_certfile,
        log_level="info",
    )


if __name__ == "__main__":
    main()
