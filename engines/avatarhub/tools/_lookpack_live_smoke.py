# -*- coding: utf-8 -*-
"""Look Pack 全链路真机冒烟（不重启任何服务，用临时角色，跑完即删）。
链路：创建临时角色(带脸) → makeup_preset → look_pack → tryon_preset(FitDiT+Ditto)
→ 校验产物 → 删角色。任何一步失败打印并继续（收集全景）。"""
import sys, io, json, time, base64
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import requests

BASE = Path(r"c:\模仿音色")
HUB = "http://127.0.0.1:9000"
NAME = "_lookpack_smoke"
FACE = BASE / "faces" / "刘德华.jpg"
FULLBODY = BASE / "logs" / "_fullbody_sim.jpg"

results = {}


def step(tag, fn):
    t0 = time.time()
    try:
        out = fn()
        results[tag] = {"ok": True, "elapsed_s": round(time.time() - t0, 1), **(out or {})}
        print(f"[OK ] {tag} ({results[tag]['elapsed_s']}s) {out or ''}")
    except Exception as e:
        results[tag] = {"ok": False, "error": str(e)[:200]}
        print(f"[FAIL] {tag}: {str(e)[:200]}")


def main():
    face_b64 = base64.b64encode(FACE.read_bytes()).decode()

    def create():
        r = requests.post(f"{HUB}/profiles",
                          json={"name": NAME, "face_b64": face_b64,
                                "description": "look pack 冒烟临时角色"}, timeout=30)
        assert r.status_code == 200, r.text[:200]
        return {}

    def makeup():
        r = requests.post(f"{HUB}/api/profiles/{NAME}/makeup_preset",
                          json={"style": "自然裸妆"}, timeout=120)
        assert r.status_code == 200, r.text[:300]
        j = r.json()
        return {"style": j.get("style"), "hint": str(j.get("hint"))[:60]}

    def look_pack():
        r = requests.post(f"{HUB}/api/profiles/{NAME}/look_pack",
                          json={"makeup_style": "复古红唇"}, timeout=300)
        assert r.status_code == 200, r.text[:300]
        j = r.json()
        return {"steps": j.get("steps"), "hint": str(j.get("hint"))[:60]}

    def tryon():
        body_b64 = base64.b64encode(FULLBODY.read_bytes()).decode()
        r = requests.post(f"{HUB}/api/profiles/{NAME}/tryon_preset",
                          json={"person_image_b64": body_b64, "cloth_name": "白色上衣",
                                "video_secs": 3}, timeout=420)
        assert r.status_code == 200, r.text[:300]
        j = r.json()
        return {"animated": j.get("animated"), "video": bool(j.get("video")),
                "elapsed_ms": j.get("elapsed_ms")}

    def detail():
        r = requests.get(f"{HUB}/profiles/{NAME}", timeout=10)
        assert r.status_code == 200
        j = r.json()
        return {"has_styled_face": j.get("has_styled_face"),
                "makeup_style": j.get("makeup_style"),
                "live_makeup_enabled": bool((j.get("live_makeup") or {}).get("enabled")),
                "body_video": bool(j.get("body_video"))}

    def cleanup():
        r = requests.delete(f"{HUB}/profiles/{NAME}", timeout=10)
        return {"status": r.status_code}

    step("1.创建临时角色", create)
    step("2.妆容定妆", makeup)
    step("3.一键定妆包", look_pack)
    step("4.试衣定妆(FitDiT+Ditto)", tryon)
    step("5.角色字段校验", detail)
    step("6.清理", cleanup)

    out = BASE / "logs" / "lookpack_live_smoke.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n[report] -> {out}")
    fails = [k for k, v in results.items() if not v.get("ok")]
    print("[SMOKE]", "全部通过" if not fails else f"失败项: {fails}")


if __name__ == "__main__":
    main()
