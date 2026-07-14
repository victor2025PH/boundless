# -*- coding: utf-8 -*-
"""C-1 虚拟背景功能烟测：真实 mediapipe 引擎跑三种模式，验证形状/耗时/热切。"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
import cv2

os.environ["BG_MODE"] = "none"
from bg_replace import BackgroundReplacer, _cover_resize, BG_IMAGE_DIR

def main():
    fails = []
    # 合成 720p 帧：中央画一个"人形"(椭圆头+矩形身)让分割器有物可分
    frame = np.full((720, 1280, 3), 60, np.uint8)
    cv2.ellipse(frame, (640, 260), (90, 120), 0, 0, 360, (150, 120, 100), -1)
    cv2.rectangle(frame, (520, 380), (760, 720), (90, 70, 140), -1)

    b = BackgroundReplacer()
    assert b.mode == "none"
    out = b.process(frame)
    if out is not frame:
        fails.append("none 模式应零开销直通(同对象)")
    print(f"[OK] none 直通")

    # cover resize
    img = np.zeros((400, 300, 3), np.uint8)
    r = _cover_resize(img, 1280, 720)
    if r.shape != (720, 1280, 3):
        fails.append(f"_cover_resize 形状错误: {r.shape}")
    else:
        print("[OK] _cover_resize 1280x720")

    # 准备背景图（imencode+tofile：中文路径下 cv2.imwrite 会静默失败）
    os.makedirs(BG_IMAGE_DIR, exist_ok=True)
    bg_path = os.path.join(BG_IMAGE_DIR, "_smoke_bg.jpg")
    _, buf = cv2.imencode(".jpg", np.full((360, 640, 3), (30, 160, 220), np.uint8))
    buf.tofile(bg_path)

    for mode, extra in [("green", {}), ("blur", {}), ("image", {"image": "_smoke_bg.jpg"})]:
        res = b.set_config(mode=mode, **extra)
        if not res.get("ok"):
            fails.append(f"set_config({mode}) 失败: {res}")
            continue
        t0 = time.time()
        n = 20
        for _ in range(n):
            out = b.process(frame)
        dt = (time.time() - t0) * 1000 / n
        if out.shape != frame.shape or out.dtype != np.uint8:
            fails.append(f"{mode} 输出形状/类型错误")
        else:
            print(f"[OK] {mode}: {dt:.1f}ms/帧 (20帧均值) 状态ms={b.status()['ms']}")
        if mode == "green":
            # 掩码朝向验证：人形中心须保留原像素(非绿)，角落须被绿幕替换
            person = out[300, 640]          # 人形躯干中心
            corner = out[20, 20]
            person_green = person[1] > 200 and person[0] < 60 and person[2] < 60
            corner_green = corner[1] > 200 and corner[0] < 60 and corner[2] < 60
            if person_green or not corner_green:
                fails.append(f"green 掩码朝向异常: 人中心={person.tolist()} 角落={corner.tolist()}")
            else:
                print(f"[OK] green 掩码朝向正确(人保留/角落绿) 人={person.tolist()} 角={corner.tolist()}")
    if b.status()["engine"] != "mediapipe":
        fails.append(f"引擎异常: {b.status()}")

    # 热切回 none + 设置持久化
    b.set_config(mode="none")
    b2 = BackgroundReplacer()
    if b2.mode != "none":
        fails.append(f"设置持久化失败: 重载后 mode={b2.mode}")
    else:
        print("[OK] 设置持久化(bg_settings.json)")
    b.close()

    try:
        os.remove(bg_path)
    except Exception:
        pass
    if fails:
        print("\nFAIL:")
        for f in fails:
            print(" -", f)
        return 1
    print("\n烟测全部 PASS")
    return 0

if __name__ == "__main__":
    sys.exit(main())
