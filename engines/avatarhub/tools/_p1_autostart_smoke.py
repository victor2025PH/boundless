# -*- coding: utf-8 -*-
"""P1 自动拉起冒烟（2026-07-17）：8001/8004 离线时点「定妆」应由 Hub 代启动并完成，不再 503。
前置：手动停掉 hair/makeup（/api/engine/stop）。临时角色测完即删，不碰真实角色。"""
import base64
import sys
import time
from pathlib import Path

import requests

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
BASE = Path(r"c:\模仿音色")
HUB = "http://127.0.0.1:9000"
PROF = "_p1_autostart_smoke"


def main():
    # 确认离线前提
    for port, name in ((8001, "hair"), (8004, "makeup")):
        try:
            requests.get(f"http://127.0.0.1:{port}/health", timeout=2)
            print(f"[pre] 警告：{name}({port}) 已在线——本测将退化为普通调用验证")
        except Exception:
            print(f"[pre] {name}({port}) 离线 ✓（符合测试前提）")

    face = base64.b64encode((BASE / "faces" / "刘德华.jpg").read_bytes()).decode()
    requests.delete(f"{HUB}/profiles/{PROF}", timeout=10)
    r = requests.post(f"{HUB}/profiles", json={"name": PROF, "face_b64": face}, timeout=30)
    assert r.status_code == 200, f"建角色失败 {r.text[:200]}"
    print(f"[prof] 临时角色 {PROF} 已建")

    ok = True
    try:
        # ① 妆容定妆（8004 离线 → 应自动拉起，CPU 服务快）
        t0 = time.time()
        r = requests.post(f"{HUB}/api/profiles/{PROF}/makeup_preset",
                          json={"style": "斩男红梨", "apply": False}, timeout=120)
        j = r.json()
        print(f"[makeup_preset] http={r.status_code} {time.time()-t0:.1f}s "
              f"ok={j.get('ok')} detail={str(j.get('detail'))[:160]}")
        ok &= r.status_code == 200 and bool(j.get("ok"))

        # ② 发型定妆（8001 离线 → 自动拉起 + 懒加载首载 ~90s，走 180s 放宽超时）
        t0 = time.time()
        r = requests.post(f"{HUB}/api/profiles/{PROF}/hair_preset",
                          json={"apply": False}, timeout=400)
        j = r.json()
        print(f"[hair_preset] http={r.status_code} {time.time()-t0:.1f}s "
              f"ok={j.get('ok')} style={j.get('hair_style')} detail={str(j.get('detail'))[:160]}")
        ok &= r.status_code == 200 and bool(j.get("ok"))
        if j.get("preview_image"):
            (BASE / "logs" / "_p1_autostart_hair.jpg").write_bytes(
                base64.b64decode(j["preview_image"]))
            print("[hair_preset] 预览已存 logs/_p1_autostart_hair.jpg")
    finally:
        requests.delete(f"{HUB}/profiles/{PROF}", timeout=10)
        print("[done] 临时角色已清理")
    print("[result]", "全部通过 ✅" if ok else "存在失败 ❌")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
