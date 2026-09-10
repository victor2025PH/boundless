# -*- coding: utf-8 -*-
"""TikTok × 智聊 真机联调探针（TK-3）——薄壳，逻辑在 src/app_automation/tiktok_chengjie_probe.py。

    python scripts/tiktok_chengjie_probe.py                 # 配置 + 连通 + 鉴权 + 桥挂载（只读）
    python scripts/tiktok_chengjie_probe.py --bind          # + 设备绑定
    python scripts/tiktok_chengjie_probe.py --roundtrip     # + 探针私信进线 → pending
    python scripts/tiktok_chengjie_probe.py --json

退出码：0 全绿 / 1 配置 / 2 连通·鉴权·桥 / 3 绑定·往返。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.app_automation.tiktok_chengjie_probe import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
