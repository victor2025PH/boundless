#!/usr/bin/env python3
"""养成线种子就绪闸：ZH_DEMO 须有偏好/物流类消息可读。

  python gate_nurture_ready.py
  python gate_nurture_ready.py --seed   # 自动铺 R1+R2+R3 到 ZH_DEMO
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from chatx_session import BASE, token  # noqa: E402
from seed_demo_chats import DEMO_THREADS, seed_episode  # noqa: E402

NEEDLES = ("雾霾蓝", "包裹", "店长", "养成", "偏好", "尺码")


def history(cid: str, limit: int = 30) -> list[str]:
    q = urllib.parse.urlencode({"conversation_id": cid, "limit": limit})
    req = urllib.request.Request(
        BASE + "/api/unified-inbox/history?" + q,
        headers={"Authorization": "Bearer " + token(), "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read().decode())
    out = []
    for m in d.get("messages") or []:
        t = (m.get("text") or m.get("content") or "").strip()
        if t:
            out.append(t)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true")
    a = ap.parse_args()
    th = DEMO_THREADS["ZH_DEMO"]
    if a.seed:
        for ep in ("R1", "R2", "R3"):
            seed_episode(ep, "ZH_DEMO")
    texts = history(th["conversation_id"])
    blob = "\n".join(texts)
    hits = [n for n in NEEDLES if n in blob]
    print(f"ZH_DEMO msgs={len(texts)} needle_hits={hits}")
    if len(hits) < 2:
        print("RESULT FAIL (偏好种子不足，先: python gate_nurture_ready.py --seed)")
        return 1
    # 禁恋爱向
    for w in ("恋爱", "女友", "男友", "暧昧"):
        if w in blob:
            print(f"RESULT FAIL 种子含禁词「{w}」")
            return 1
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
