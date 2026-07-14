# -*- coding: utf-8 -*-
"""C-2 face_map 功能验证：桩掉检测/换脸引擎，精确断言槽位映射/排序/回退语义。
(引擎数学是既有久经考验路径；本测保「新增的映射逻辑」七分支正确)"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np

import faceswap_api as fs

FAILS = []


def ok(m):
    print(f"  [OK] {m}")


def ng(m):
    print(f"  [NG] {m}")
    FAILS.append(m)


class FakeFace:
    def __init__(self, bbox, color=None):
        self.bbox = np.array(bbox, dtype=np.float32)
        self.det_score = 0.9
        self.kps = None
        self.color = color          # 源脸=涂色标识；目标脸=None


class FakeAnalyser:
    def get(self, img):
        h, w = img.shape[:2]
        if (h, w) == (10, 10):      # 源图：用像素色标识身份
            b, g, r = (int(v) for v in img[5, 5])
            return [FakeFace([0, 0, 10, 10], color=(b, g, r))]
        if (h, w) == (100, 300):    # 目标图：三张脸,刻意乱序返回(考排序)
            return [FakeFace([140, 20, 160, 80]),
                    FakeFace([240, 20, 260, 80]),
                    FakeFace([40, 20, 60, 80])]
        return []


class FakeSwapper:
    def get(self, result, tgt, src, paste_back=True):
        x1, y1, x2, y2 = (int(v) for v in tgt.bbox)
        result[y1:y2, x1:x2] = src.color
        return result


def solid(color):
    img = np.zeros((10, 10, 3), np.uint8)
    img[:] = color
    return fs.img_to_b64(img, 95)


def region_color(img, cx):
    return tuple(int(v) for v in img[50, cx])


def run_case(name, req_kw, expect, expect_map_used):
    req = fs.SwapRequest(target_image=TGT_B64, smooth_mode="off", enhance="none", **req_kw)
    resp = fs.faceswap(req)
    out = fs.b64_to_img(resp.result_image)
    got = {pos: region_color(out, cx) for pos, cx in (("left", 50), ("mid", 150), ("right", 250))}
    bad = [f"{pos}={got[pos]}≠{expect[pos]}" for pos in expect if _off(got[pos], expect[pos])]
    if bad:
        ng(f"{name}: " + "; ".join(bad))
    else:
        ok(f"{name}: 左{got['left']} 中{got['mid']} 右{got['right']}")
    if resp.face_map_used != expect_map_used:
        ng(f"{name}: face_map_used={resp.face_map_used} 期望 {expect_map_used}")
    else:
        ok(f"{name}: face_map_used={resp.face_map_used}")
    return resp


def _off(a, b, tol=30):
    return any(abs(x - y) > tol for x, y in zip(a, b))


RED = (0, 0, 220)
BLUE = (220, 0, 0)

fs.face_analyser = FakeAnalyser()
fs.face_swapper = FakeSwapper()
for k in ("enable_poisson", "enable_color_corr", "enable_codeformer", "enable_gfpgan"):
    fs.PARAMS[k] = False

RED_B64 = solid(RED)
BLUE_B64 = solid(BLUE)
TGT = np.full((100, 300, 3), 128, np.uint8)
TGT_B64 = fs.img_to_b64(TGT, 95)

print("C-2 face_map 功能验证")
# 1) 旧行为：单源换所有脸（零回归）
run_case("单源(旧行为)", {"source_image": RED_B64},
         {"left": RED, "mid": RED, "right": RED}, None)
# 2) 双槽映射：左→红 右(中)→蓝，第三张(右)回退槽0红
r = run_case("双槽映射+第三人回退", {"source_map": [RED_B64, BLUE_B64]},
             {"left": RED, "mid": BLUE, "right": RED}, 2)
if r.faces_boxes and [b[0] for b in r.faces_boxes] == sorted(b[0] for b in r.faces_boxes):
    ok("faces_boxes 按左→右排序")
else:
    ng(f"faces_boxes 未排序: {r.faces_boxes}")
# 3) 空槽回退 source_image（空槽解析到激活脸仍算「已映射」→ used=2）
run_case("空槽0回退激活脸", {"source_image": RED_B64, "source_map": ["", BLUE_B64]},
         {"left": RED, "mid": BLUE, "right": RED}, 2)
# 4) 坏槽回退：槽0图坏 → 全部回退槽1蓝
run_case("坏槽0回退", {"source_map": ["!not-b64!", BLUE_B64]},
         {"left": BLUE, "mid": BLUE, "right": BLUE}, 1)
# 5) 全坏 → 400
try:
    fs.faceswap(fs.SwapRequest(target_image=TGT_B64, source_map=["!x!", "!y!"],
                               smooth_mode="off", enhance="none"))
    ng("全坏 source_map 未拒绝")
except Exception as e:
    ok(f"全坏 source_map → 拒绝({getattr(e, 'status_code', '?')})")

if FAILS:
    print(f"\nFAIL {len(FAILS)}")
    sys.exit(1)
print("\n全部 PASS")
