# 在已登录会话或配置了 OPENCLAW_API_KEY 的情况下，向本机 API 提交 /vpn/pool/apply，把 vpn_pool.json 里的分配下发到指定手机。
# 用法: 先启动主控服务，再执行: python scripts/apply_vpn_pool_local.py
# 环境变量: OPENCLAW_API_BASE (默认 http://127.0.0.1:18080)、OPENCLAW_API_KEY (可选)

import json
import os
import ssl
import urllib.error
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_POOL = _ROOT / "src" / "config" / "vpn_pool.json"

# 主控 13 / 14 号当前序列号（与 device_aliases.json 一致）
DEFAULT_DEVICE_IDS = [
    "89NZVGKFD6BYUO5P",
    "QSVSMRXOXWCYFIX4",
]


def main():
    base = os.environ.get("OPENCLAW_API_BASE", "http://127.0.0.1:18080").rstrip("/")
    key = os.environ.get("OPENCLAW_API_KEY", "").strip()
    url = f"{base}/vpn/pool/apply"
    body = json.dumps({"device_ids": DEFAULT_DEVICE_IDS}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    if key:
        req.add_header("X-API-Key", key)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
            print(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        print(e.read().decode("utf-8", errors="replace"))
        raise SystemExit(1)
    if not _POOL.is_file():
        print(f"注意: 未找到 {_POOL}，请先在配置池添加代理与 assignments。")


if __name__ == "__main__":
    main()
