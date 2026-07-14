# -*- coding: utf-8 -*-
"""发型定妆端到端验证（阶段7）：演示发型 + 妆容 → 一键定妆包全链路。

直播中显存紧（hair 闸 4G）时发型步会被 503 软降级——本脚本兼作两用：
  · 直播中跑：验证链路布线 + 闸生效 + 软降级（makeup 仍成、定妆脸落库）；
  · 下播后跑：发型步真跑 HairFastGAN，验证 30 张演示发型可用（质量看 logs 图）。
用法: python tools/hair_e2e_test.py [发型名=演示发型001] [妆容=斩男红梨]
"""
import base64
import sys
import time
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(r"c:\模仿音色")
HUB = "http://127.0.0.1:9000"
PROF = "_hair_e2e_smoke"


def main():
    style = sys.argv[1] if len(sys.argv) > 1 else "演示发型001"
    makeup = sys.argv[2] if len(sys.argv) > 2 else "斩男红梨"

    j = requests.get("http://127.0.0.1:8001/hair_styles", timeout=8).json()
    demo = [s for s in j.get("styles", []) if s.startswith("演示发型")]
    print(f"[lib] 发型库 {len(j.get('styles', []))} 款（演示 {len(demo)}）active={j.get('active')}")
    assert style in j.get("styles", []), f"发型 {style} 不在库中"
    th = requests.get("http://127.0.0.1:8001/hair_thumb", params={"name": style}, timeout=8)
    print(f"[lib] 缩略图 {style}: {th.status_code} {len(th.content)}B")

    face = base64.b64encode((BASE / "faces" / "刘德华.jpg").read_bytes()).decode()
    requests.delete(f"{HUB}/profiles/{PROF}", timeout=10)
    r = requests.post(f"{HUB}/profiles", json={"name": PROF, "face_b64": face}, timeout=30)
    assert r.status_code == 200, f"建角色失败 {r.text[:200]}"

    t0 = time.time()
    r = requests.post(f"{HUB}/api/profiles/{PROF}/look_pack",
                      json={"hair_style": style, "makeup_style": makeup, "apply": False},
                      timeout=600)
    el = time.time() - t0
    j = r.json()
    print(f"[look_pack] http={r.status_code} {el:.1f}s ok={j.get('ok')}")
    for k, v in (j.get("steps") or {}).items():
        print(f"  step[{k}] ok={v.get('ok')} {str(v.get('detail') or v.get('style') or '')[:100]}")

    det = requests.get(f"{HUB}/profiles/{PROF}?include_face=true", timeout=15).json()
    for f in ("face_styled_b64", "face_hair_b64"):
        v = det.get(f) or ""
        print(f"[profile] {f}: {len(v)//1024}KB")
        if v:
            (BASE / "logs" / f"_hair_e2e_{f[:10]}.jpg").write_bytes(base64.b64decode(v))
    requests.delete(f"{HUB}/profiles/{PROF}", timeout=10)
    print("[done] 临时角色已清理；产物在 logs/_hair_e2e_*.jpg")


if __name__ == "__main__":
    main()
