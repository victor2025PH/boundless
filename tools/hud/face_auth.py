# -*- coding: utf-8 -*-
"""刷脸授权（P2b · 2026-08-08）——隔空指挥 T2 点火的「系统认识主人」层。

出处《未来感机房展演…方案》§16.5 / 《集群实况板…方案》§14.2 护栏 3。
判据同源：insightface buffalo_l + 余弦阈值 0.40，与 `tools/gallery_audit.py`
（角色库身份抽检）同库同模型同阈值——集群用同一双眼睛认角色和认主人。

语义（叠加不是替换）：登记表非空 → T2 点火在「在场信号」之上**再**要求当帧刷脸；
登记表空 → 纯在场信号（保守降级，决策点 5/7 的两个档位由登记状态自动切换，零配置）。
帧只进内存验证即弃，不落盘（与手势/语音同一隐私口径）。

登记表：C:/模仿音色/secrets/ops_operators.json（secrets/ 已 gitignore，红线 5）。
用法：
  python face_auth.py enroll --name 老板 --image 正脸照.jpg   # 登记操作员
  python face_auth.py list / remove --name 老板 / test --image 某照.jpg
环境闸：HUD_OPS_AUTH=presence 可强制关闭刷脸层（应急）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import threading
import time
from pathlib import Path

OPERATORS = Path(r"C:\模仿音色\secrets\ops_operators.json")
FACE_THRESH = 0.40          # gallery_audit 同源阈值
_lock = threading.Lock()
_app = None
_state = {"ready": False, "reason": ""}


def _load_operators() -> list[dict]:
    try:
        d = json.loads(OPERATORS.read_text(encoding="utf-8"))
        return [o for o in d.get("operators", []) if o.get("emb")]
    except Exception:
        return []


def enabled() -> bool:
    """登记即生效：有操作员 → 刷脸叠加层开；HUD_OPS_AUTH=presence 强制关（应急阀）。"""
    if os.environ.get("HUD_OPS_AUTH", "").lower() == "presence":
        return False
    return bool(_load_operators())


def _ensure_app() -> bool:
    global _app
    if _app is not None:
        return True
    with _lock:
        if _app is not None:
            return True
        try:
            from insightface.app import FaceAnalysis  # noqa: PLC0415 惰性：不拖 hud_server 启动
            app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
            app.prepare(ctx_id=-1, det_size=(640, 640), det_thresh=0.3)
            _app = app
            _state.update(ready=True, reason="")
        except Exception as e:  # noqa: BLE001
            _state.update(ready=False, reason=f"{type(e).__name__}: {str(e)[:100]}")
    return _app is not None


def _emb_from_b64(b64: str):
    import cv2  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415
    raw = base64.b64decode(b64.split(",")[-1] if b64.startswith("data:") else b64)
    img = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    if max(h, w) > 640:
        s = 640 / max(h, w)
        img = cv2.resize(img, (int(w * s), int(h * s)))
    faces = _app.get(img)
    if not faces:
        return None
    f = max(faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
    return f.normed_embedding


def verify(frame_b64: str) -> tuple[bool, str, float, str]:
    """当帧 vs 登记表。返回 (通过?, 操作员名, 余弦, 人话原因)。帧在函数栈上，出栈即弃。"""
    ops = _load_operators()
    if not ops:
        return False, "", 0.0, "登记表空（先 face_auth.py enroll）"
    if not frame_b64:
        return False, "", 0.0, "刷脸模式需要指挥台画面：竖拇指（手势层开着）或回车点火"
    if not _ensure_app():
        return False, "", 0.0, f"识别器不可用：{_state['reason']}"
    try:
        emb = _emb_from_b64(frame_b64)
    except Exception as e:  # noqa: BLE001
        return False, "", 0.0, f"帧解析失败：{type(e).__name__}"
    if emb is None:
        return False, "", 0.0, "画面里没有检出人脸——正对摄像头再点火"
    best_name, best_cos = "", -1.0
    for o in ops:
        import numpy as np  # noqa: PLC0415
        cos = float(np.dot(emb, np.array(o["emb"], dtype=float)))
        if cos > best_cos:
            best_name, best_cos = o.get("name", "?"), cos
    if best_cos >= FACE_THRESH:
        return True, best_name, best_cos, ""
    return False, "", best_cos, f"我不认识这张脸（相似度 {best_cos:.2f} < {FACE_THRESH}）"


def status() -> dict:
    return {"enabled": enabled(), "operators": [o.get("name") for o in _load_operators()],
            "ready": _state["ready"], "thresh": FACE_THRESH}


# ---------------- CLI（登记/查看/移除/自测） ----------------

def _save(ops: list[dict]) -> None:
    OPERATORS.parent.mkdir(parents=True, exist_ok=True)
    OPERATORS.write_text(json.dumps({"operators": ops, "thresh": FACE_THRESH},
                                    ensure_ascii=False), encoding="utf-8")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="隔空指挥·刷脸授权登记")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_en = sub.add_parser("enroll")
    p_en.add_argument("--name", required=True)
    p_en.add_argument("--image", required=True, help="正脸照路径")
    sub.add_parser("list")
    p_rm = sub.add_parser("remove")
    p_rm.add_argument("--name", required=True)
    p_ts = sub.add_parser("test")
    p_ts.add_argument("--image", required=True)
    a = ap.parse_args()
    if a.cmd == "list":
        st = status()
        print(f"刷脸层：{'开' if st['enabled'] else '关（登记表空或被 HUD_OPS_AUTH 压制）'}"
              f" · 操作员：{st['operators'] or '无'} · 阈值 {st['thresh']}")
        return 0
    if a.cmd == "remove":
        ops = [o for o in _load_operators() if o.get("name") != a.name]
        _save(ops)
        print(f"已移除「{a.name}」，剩 {len(ops)} 人")
        return 0
    if not _ensure_app():
        print("识别器不可用：", _state["reason"])
        return 1
    b64 = base64.b64encode(Path(a.image).read_bytes()).decode()
    if a.cmd == "enroll":
        emb = _emb_from_b64(b64)
        if emb is None:
            print("照片里没检出人脸")
            return 1
        ops = [o for o in _load_operators() if o.get("name") != a.name]
        ops.append({"name": a.name, "emb": [float(x) for x in emb], "ts": round(time.time())})
        _save(ops)
        print(f"已登记「{a.name}」（共 {len(ops)} 人）——T2 点火即刻要求刷脸")
        return 0
    ok, who, cos, why = verify(b64)
    print(f"test: {'通过 ' + who if ok else '拒绝'} cos={cos:.3f} {why}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
