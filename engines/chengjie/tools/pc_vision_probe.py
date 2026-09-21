# -*- coding: utf-8 -*-
"""P3-B grounding 只读探针（实施91，2026-08-31）。

盲写坐标解析/缩放 = 「对着假设修」。先合成一张**已知按钮中心**的图，打真实
Qwen3-VL（vision.base_urls 首个 LAN 端点），把原始返回逐字打印 + 与已知中心比对，
判定模型到底回**像素 / 归一化[0,1] / [0,1000] / bbox** 哪种坐标——拿到真实格式后
P3-B 的解析/缩放才据实写。

**只读**：不改配置、不落生产、不发消息、不碰 runner。合成图落 tmp_pcvision/
（/tmp_* 已 gitignore）。默认打 176 LAN（qwen3-vl:8b-instruct，Ollama /v1 兼容口，
api_key 随便给——Ollama 忽略 Bearer）。

用法：
  python -m tools.pc_vision_probe                # 默认 176 + 1280x800
  python -m tools.pc_vision_probe --base-url http://192.168.0.198:11434/v1 --model qwen3-vl:8b-instruct
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

# 已知按钮：label -> (中心x, 中心y, 颜色)。中心即 grounding 应命中的真值。
_BUTTONS: List[Tuple[str, int, int, Tuple[int, int, int]]] = [
    ("OK", 240, 160, (34, 160, 80)),
    ("Cancel", 1040, 160, (200, 60, 60)),
    ("Search", 640, 400, (70, 110, 200)),
    ("Submit", 640, 660, (150, 90, 190)),
]
_BTN_W, _BTN_H = 180, 64


def _build_image(w: int, h: int, out_dir: str) -> str:
    """白底画几个带标签的实心圆角按钮（中心=_BUTTONS 真值）。返回落盘路径。"""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (w, h), (245, 246, 248))
    dr = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 26)
    except Exception:
        font = ImageFont.load_default()
    for label, cx, cy, color in _BUTTONS:
        x0, y0 = cx - _BTN_W // 2, cy - _BTN_H // 2
        x1, y1 = cx + _BTN_W // 2, cy + _BTN_H // 2
        try:
            dr.rounded_rectangle([x0, y0, x1, y1], radius=12, fill=color)
        except Exception:
            dr.rectangle([x0, y0, x1, y1], fill=color)
        # 居中标签
        try:
            bb = dr.textbbox((0, 0), label, font=font)
            tw, th = bb[2] - bb[0], bb[3] - bb[1]
        except Exception:
            tw, th = 10 * len(label), 20
        dr.text((cx - tw / 2, cy - th / 2), label, fill=(255, 255, 255), font=font)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "probe.png")
    img.save(path, "PNG")
    return path


def _data_url(path: str) -> str:
    raw = open(path, "rb").read()
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def _ask(base_url: str, model: str, api_key: str, data_url: str,
         prompt: str, timeout: float = 30.0) -> str:
    """打一发 OpenAI 兼容 chat（含图），回文本内容。异常回 '__ERR__: ...'。"""
    import httpx

    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": data_url}},
            {"type": "text", "text": prompt},
        ]}],
        "temperature": 0,
        "max_tokens": 300,
    }
    headers = {"Authorization": "Bearer " + (api_key or "ollama")}
    try:
        r = httpx.post(url, json=payload, headers=headers,
                       timeout=httpx.Timeout(timeout, connect=5.0))
        r.raise_for_status()
        data = r.json()
        return (((data.get("choices") or [{}])[0].get("message") or {})
                .get("content") or "").strip()
    except Exception as ex:  # noqa: BLE001
        return "__ERR__: " + str(ex)[:200]


def _numbers(text: str) -> List[float]:
    return [float(x) for x in re.findall(r"-?\d+\.?\d*", text or "")]


def _interpret(nums: List[float], w: int, h: int,
               truth: Tuple[int, int]) -> List[Dict[str, Any]]:
    """把抽到的数按几种坐标约定映射成屏幕像素，各算与真值的距离。

    - 2 个数：point（pixel / norm01 / norm1000）
    - 4 个数：bbox 取中心（同三种缩放）
    返回按距离升序的候选列表。
    """
    tx, ty = truth
    cands: List[Dict[str, Any]] = []

    def _push(kind: str, px: float, py: float) -> None:
        d = ((px - tx) ** 2 + (py - ty) ** 2) ** 0.5
        cands.append({"kind": kind, "x": round(px, 1), "y": round(py, 1),
                      "err_px": round(d, 1)})

    pts: List[Tuple[str, float, float]] = []
    if len(nums) >= 4:
        x1, y1, x2, y2 = nums[0], nums[1], nums[2], nums[3]
        pts.append(("bbox_center", (x1 + x2) / 2.0, (y1 + y2) / 2.0))
    if len(nums) >= 2:
        pts.append(("point", nums[0], nums[1]))

    for base, ax, ay in pts:
        _push(base + ":pixel", ax, ay)
        if 0.0 <= ax <= 1.0 and 0.0 <= ay <= 1.0:
            _push(base + ":norm01", ax * w, ay * h)
        if 0.0 <= ax <= 1000.0 and 0.0 <= ay <= 1000.0:
            _push(base + ":norm1000", ax / 1000.0 * w, ay / 1000.0 * h)
    cands.sort(key=lambda c: c["err_px"])
    return cands


def main() -> int:
    ap = argparse.ArgumentParser()
    # 实施102（2026-09-22）：识图单点 198 视觉机
    ap.add_argument("--base-url", default="http://192.168.0.198:11434/v1")
    ap.add_argument("--model", default="qwen3-vl:8b-instruct")
    ap.add_argument("--api-key", default=os.environ.get("PC_VISION_KEY", "ollama"))
    ap.add_argument("--w", type=int, default=1280)
    ap.add_argument("--h", type=int, default=800)
    ap.add_argument("--out-dir", default="tmp_pcvision")
    args = ap.parse_args()

    w, h = args.w, args.h
    path = _build_image(w, h, args.out_dir)
    data_url = _data_url(path)
    print(f"[probe] image={path} dims={w}x{h} endpoint={args.base_url} model={args.model}")
    print(f"[probe] buttons(truth centers): "
          + ", ".join(f"{lb}=({cx},{cy})" for lb, cx, cy, _ in _BUTTONS))
    print("=" * 72)

    # 两种问法：A=直接要中心像素点 JSON；B=要 bbox（看模型原生 token 习惯）。
    prompt_a = (
        f"The image is exactly {w}x{h} pixels. Locate the on-screen button whose "
        f"visible text label is \"{{label}}\". Reply with ONLY one JSON object of the "
        f"CENTER pixel coordinates in this image, e.g. {{{{\"x\": 123, \"y\": 456}}}}. "
        f"No prose, no code fence."
    )
    prompt_b = (
        f"The image is {w}x{h} pixels. Give the bounding box of the button labeled "
        f"\"{{label}}\" as JSON {{{{\"bbox\": [x1,y1,x2,y2]}}}} in pixel coordinates."
    )

    fmt_votes: Dict[str, int] = {}
    for label, cx, cy, _ in _BUTTONS:
        for tag, tmpl in (("A/point", prompt_a), ("B/bbox", prompt_b)):
            raw = _ask(args.base_url, args.model, args.api_key, data_url,
                       tmpl.format(label=label))
            nums = _numbers(raw)
            cands = _interpret(nums, w, h, (cx, cy)) if not raw.startswith("__ERR__") else []
            best = cands[0] if cands else None
            print(f"[{label:7s}|{tag}] truth=({cx},{cy})")
            print(f"    raw: {raw[:220]}")
            if best:
                print(f"    best-fit: {best['kind']} -> ({best['x']},{best['y']}) "
                      f"err={best['err_px']}px")
                # 只把「误差 < 半个按钮宽」的判定计入格式投票（否则是没找对目标）
                if best["err_px"] <= _BTN_W:
                    fmt_votes[best["kind"]] = fmt_votes.get(best["kind"], 0) + 1
            print("-" * 72)

    print("=" * 72)
    print("[probe] 格式投票（err<按钮宽 才计）:", json.dumps(fmt_votes, ensure_ascii=False))
    if fmt_votes:
        win = max(fmt_votes.items(), key=lambda kv: kv[1])
        print(f"[probe] 判定：Qwen3-VL 此端点坐标格式 ≈ {win[0]}（命中 {win[1]}/"
              f"{len(_BUTTONS)*2}）")
    else:
        print("[probe] 未得到可信命中——检查端点/模型/prompt，或该模型 grounding 弱。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
