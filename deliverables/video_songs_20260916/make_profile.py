#!/usr/bin/env python3
"""在 176 hub 建「品牌数字人」档（合成脸，非真人；供口型 MV 用）。

  python make_profile.py xiaojie_brand_A avatar/xiaojie_candidate_A_female.png "小界·品牌歌手 A（合成脸，2026-09-16）"

只写 name/description/face_b64；不挂声音（营销歌由 ACE 直出人声，MV 只用脸做口型）。
已存在同名档则 PATCH 换脸。
"""
from __future__ import annotations

import base64
import json
import sys
import urllib.error
import urllib.request

HUB = "http://192.168.0.176:9000"


def call(method: str, path: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    r = urllib.request.Request(HUB + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    name, img, desc = sys.argv[1], sys.argv[2], (sys.argv[3] if len(sys.argv) > 3 else "")
    raw = open(img, "rb").read()
    if not raw.startswith(b"\x89PNG") and raw[:2] != b"\xff\xd8":
        raise SystemExit("[FAIL] 不是 PNG/JPEG")
    b64 = base64.b64encode(raw).decode()
    try:
        exists = call("GET", f"/profiles/{name}")
    except urllib.error.HTTPError:
        exists = None
    if exists and exists.get("name") == name:
        r = call("PATCH", f"/profiles/{name}", {"face_b64": b64, "description": desc})
        print("PATCH", json.dumps(r, ensure_ascii=False)[:300])
    else:
        r = call("POST", "/profiles", {"name": name, "description": desc, "face_b64": b64})
        print("POST", json.dumps(r, ensure_ascii=False)[:300])
    d = call("GET", f"/profiles/{name}")
    print("verify:", {k: d.get(k) for k in ("name", "description", "has_face", "face_gallery_count", "lib")})
    return 0


if __name__ == "__main__":
    sys.exit(main())
