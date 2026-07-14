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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(project_root / "logs" / "host_api.log"),
    ],
)


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
