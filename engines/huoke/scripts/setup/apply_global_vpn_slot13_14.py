# 为主控 13/14 号机从 vpn_pool.json 读取 SOCKS5，执行 setup_global_vpn（全局路由 + 连接）。
# 依赖：uiautomator2、手机已开 USB 调试；主控 API 非必须。
# 用法：在 mobile-auto-project 根目录执行: python scripts/apply_global_vpn_slot13_14.py

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_POOL = _ROOT / "src" / "config" / "vpn_pool.json"

PAIRS = [
    ("89NZVGKFD6BYUO5P", "proxy_ip2up_slot13"),
    ("QSVSMRXOXWCYFIX4", "proxy_ip2up_slot14"),
]


def main() -> int:
    if not _POOL.is_file():
        print("缺少", _POOL)
        return 1
    pool = json.loads(_POOL.read_text(encoding="utf-8"))
    cfgs = {c["id"]: c for c in pool.get("configs", [])}

    from src.behavior.vpn_manager import parse_uri, setup_global_vpn

    for serial, cfg_id in PAIRS:
        entry = cfgs.get(cfg_id)
        if not entry:
            print(cfg_id, "不在配置池")
            return 1
        uri = entry["uri"]
        cfg = parse_uri(uri)
        print("===", serial[:8], entry.get("label", cfg_id), "===")
        st = setup_global_vpn(serial, cfg)
        print("connected:", st.connected, "error:", st.error or "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
