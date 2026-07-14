# -*- coding: utf-8 -*-
"""试衣质量基线 harness —— 标准测试集跑当前 8002 后端，存档结果网格图+时延。

产物: logs/tryon_baseline_<backend>_<ts>/
  ├ case<i>_person.jpg / _garment.jpg / _result.jpg
  ├ grid.jpg           (person|garment|result 三联对比)
  └ report.json        (时延/后端/尺寸)

任何后端切换（inpaint→FitDiT→未来 CatV2TON 等）后重跑本脚本，对比 grid 即可。
测试集: FitDiT examples（标准 VITON 姿态半身模特 + 平铺服装）。
"""
import sys, io, json, time, base64
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import requests
import numpy as np
import cv2

BASE = Path(r"c:\模仿音色")
TRYON = "http://127.0.0.1:8002"
EXAMPLES = Path(r"C:\FitDiT\examples")

CASES = [  # (person, garment)
    ("model/0279.jpg", "garment/0012.jpg"),
    ("model/0083.jpg", "garment/0047.jpg"),
    ("model/0179.jpg", "garment/0023.jpg"),
]


def b64(p: Path) -> str:
    return base64.b64encode(p.read_bytes()).decode()


def to_img(b: str) -> np.ndarray:
    return cv2.imdecode(np.frombuffer(base64.b64decode(b), np.uint8), cv2.IMREAD_COLOR)


def fit_h(img: np.ndarray, h: int = 512) -> np.ndarray:
    return cv2.resize(img, (int(img.shape[1] * h / img.shape[0]), h))


def main():
    r = requests.get(f"{TRYON}/health", timeout=5)
    backend = r.json().get("backend", "unknown")
    ts = time.strftime("%H%M%S")
    out_dir = BASE / "logs" / f"tryon_baseline_{backend}_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"[baseline] backend={backend} -> {out_dir.name}")

    rows, report = [], {"backend": backend, "cases": []}
    for i, (pm, gm) in enumerate(CASES):
        pp, gp = EXAMPLES / pm, EXAMPLES / gm
        t0 = time.time()
        try:
            resp = requests.post(f"{TRYON}/tryon",
                                 json={"person_image": b64(pp), "cloth_image": b64(gp)},
                                 timeout=300)
            el = time.time() - t0
            if resp.status_code != 200:
                print(f"[case{i}] FAIL http={resp.status_code} {resp.text[:200]}")
                report["cases"].append({"case": i, "error": resp.text[:200]})
                continue
            res = to_img(resp.json()["result_image"])
        except Exception as e:
            print(f"[case{i}] FAIL {str(e)[:200]}")
            report["cases"].append({"case": i, "error": str(e)[:200]})
            continue

        person = cv2.imdecode(np.frombuffer(pp.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        garment = cv2.imdecode(np.frombuffer(gp.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
        for tag, img in (("person", person), ("garment", garment), ("result", res)):
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 92])
            (out_dir / f"case{i}_{tag}.jpg").write_bytes(buf.tobytes())
        row = np.hstack([fit_h(person), fit_h(garment), fit_h(res)])
        cv2.putText(row, f"case{i} {el:.1f}s", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2)
        rows.append(row)
        report["cases"].append({"case": i, "elapsed_s": round(el, 1),
                                "result_size": f"{res.shape[1]}x{res.shape[0]}"})
        print(f"[case{i}] OK {el:.1f}s")

    if rows:
        w = max(r.shape[1] for r in rows)
        rows = [cv2.copyMakeBorder(r, 0, 0, 0, w - r.shape[1], cv2.BORDER_CONSTANT) for r in rows]
        grid = np.vstack(rows)
        ok, buf = cv2.imencode(".jpg", grid, [cv2.IMWRITE_JPEG_QUALITY, 88])
        (out_dir / "grid.jpg").write_bytes(buf.tobytes())
        print(f"[baseline] grid -> {out_dir / 'grid.jpg'}")
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                         encoding="utf-8")


if __name__ == "__main__":
    main()
