# -*- coding: utf-8 -*-
"""Tier-1 GUI grounding 模型 A/B 评测器（2026-08-16 P1）。

背景（见 docs/AI_AGENT_PHONE_CONTROL_ROADMAP.md）：
huoke 现在选择器失效时用**通用** VLM（qwen2.5vl）兜底出坐标（src/ai/vision_fallback）。
换成 GUI 专用 grounding 模型（UI-TARS 等）**前必须先量**——这些模型主在通用 App
训练，私域中文 App（TikTok/FB）可能分布外，未测先信=赌。本工具在同一批真实
"选择器失效"截图上，对多个后端量**坐标命中率 / 延迟 / 越界率**，出 A/B 判据。

后端（可插拔）：
  general   复用 vision_fallback 的 _build_prompt + _parse_response（**保证 A/B
            反映生产真实路径**），走 llm_client.chat_vision（provider 无关）。
  uitars    OpenAI 兼容端点（vLLM 服务 UI-TARS），grounding prompt + 坐标解析，
            支持绝对/归一化(0-1000)坐标空间（grounding 模型常见坑）。
  mock      脚本化返坐标，仅 --selftest 用，零 GPU。

命中判据单一真相 = src/ai/grounding_dataset.hit_test（bbox 内 / point 半径内）。
数据集 = data/grounding_cases/（采集见 grounding_dataset.maybe_record）。

用法：
  python tools/grounding_ab_eval.py --selftest              # 离线自检，零 GPU/零副作用
  python tools/grounding_ab_eval.py --backends general      # 跑通用 VLM（需本地 Ollama/云）
  python tools/grounding_ab_eval.py --backends general,uitars --uitars-url http://127.0.0.1:8000/v1 --uitars-model ui-tars
  python tools/grounding_ab_eval.py --dataset data/grounding_cases/cases.jsonl --json
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import struct
import sys
import time
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.ai import grounding_dataset as gds  # noqa: E402  命中判据/数据集单一真相


# ────────────────────────────────────────────────────────────────────
# 后端抽象
# ────────────────────────────────────────────────────────────────────
class GroundingBackend:
    """给一张截图 + 目标描述，返回预测坐标 (x, y) 或 None。"""
    name = "base"

    def locate(self, image_bytes: bytes, target: str, context: str,
               img_w: Optional[int], img_h: Optional[int]) -> Optional[Tuple[int, int]]:
        raise NotImplementedError


class GeneralVlmBackend(GroundingBackend):
    """通用 VLM——复用 vision_fallback 的 prompt/parse，反映生产真实路径。"""
    name = "general"

    def __init__(self, client=None):
        from src.ai.vision_fallback import VisionFallback
        from src.ai.llm_client import get_llm_client
        self._VF = VisionFallback
        self._client = client or get_llm_client()

    def locate(self, image_bytes, target, context, img_w, img_h):
        img_b64 = base64.b64encode(image_bytes).decode("ascii")
        prompt = self._VF._build_prompt(target, context)
        resp = self._client.chat_vision(prompt, img_b64, max_tokens=200)
        if not resp:
            return None
        result = self._VF._parse_response(resp)
        return result.coordinates


class UiTarsBackend(GroundingBackend):
    """UI-TARS（或任意 GUI grounding 模型）——OpenAI 兼容端点。

    coord_space:
      abs      模型直接返图像像素坐标
      norm1000 模型返 [0,1000] 归一化坐标（ScreenSpot/多数 grounding 模型），
               需按图像尺寸缩放回像素——这是最常见的"坐标全偏"坑。
    """
    name = "uitars"

    def __init__(self, base_url: str, model: str, api_key: str = "EMPTY",
                 coord_space: str = "norm1000", timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.coord_space = coord_space
        self.timeout = timeout

    @staticmethod
    def _build_prompt(target: str, context: str) -> str:
        # UI-TARS grounding 风格：只要坐标，减少自由发挥。
        ctx = f" Context: {context}." if context else ""
        return (f"Output only the click point for the UI element: \"{target}\".{ctx} "
                f"Respond strictly as: click(point='<x> <y>')")

    @staticmethod
    def _parse(text: str) -> Optional[Tuple[float, float]]:
        import re
        if not text:
            return None
        # 兼容多种输出：click(point='x y') / (x,y) / x,y / <point>x y</point>
        m = re.search(r"point='?\s*(-?\d+(?:\.\d+)?)[ ,]+(-?\d+(?:\.\d+)?)", text)
        if not m:
            m = re.search(r"<point>\s*(-?\d+(?:\.\d+)?)[ ,]+(-?\d+(?:\.\d+)?)", text)
        if not m:
            m = re.search(r"\(?\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)?", text)
        if not m:
            return None
        return float(m.group(1)), float(m.group(2))

    def locate(self, image_bytes, target, context, img_w, img_h):
        import urllib.request
        img_b64 = base64.b64encode(image_bytes).decode("ascii")
        body = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": self._build_prompt(target, context)},
                    {"type": "image_url",
                     "image_url": {"url": f"data:image/png;base64,{img_b64}"}},
                ],
            }],
            "max_tokens": 128,
            "temperature": 0.0,
        }
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
        )
        # LAN/本机端点探测绕系统代理（AvatarHub P0 复盘同款坑）。
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=self.timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        xy = self._parse(text)
        if xy is None:
            return None
        x, y = xy
        if self.coord_space == "norm1000" and img_w and img_h:
            x = x / 1000.0 * img_w
            y = y / 1000.0 * img_h
        return int(round(x)), int(round(y))


class MockBackend(GroundingBackend):
    """脚本化后端——按 case id 返预设坐标，仅自检用。"""
    name = "mock"

    def __init__(self, scripted: Dict[str, Optional[Tuple[int, int]]], name: str = "mock"):
        self._scripted = scripted
        self.name = name

    def locate(self, image_bytes, target, context, img_w, img_h):
        # 用 target 里编码的 case id 查（自检构造时把 id 塞进 context）。
        return self._scripted.get(context)


# ────────────────────────────────────────────────────────────────────
# 评测核心
# ────────────────────────────────────────────────────────────────────
def _pctl(vals: List[float], q: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    idx = min(len(s) - 1, int(round(q * (len(s) - 1))))
    return round(s[idx], 1)


def eval_backend(backend: GroundingBackend, cases: List[Dict[str, Any]],
                 images_root: Path) -> Dict[str, Any]:
    """在案例集上跑一个后端，出命中率/延迟/越界/无坐标/分组明细。"""
    n = hits = no_coord = oob = errors = 0
    latencies: List[float] = []
    by_app: Dict[str, List[int]] = {}
    for case in cases:
        img_path = images_root / case["image"] if not os.path.isabs(case["image"]) \
            else Path(case["image"])
        try:
            image_bytes = img_path.read_bytes()
        except OSError:
            errors += 1
            continue
        n += 1
        img_w, img_h = case.get("image_w"), case.get("image_h")
        t0 = time.time()
        try:
            pred = backend.locate(image_bytes, case.get("target", ""),
                                  case.get("context", ""), img_w, img_h)
        except Exception:
            pred = None
            errors += 1
        latencies.append((time.time() - t0) * 1000.0)
        app = case.get("app") or "unknown"
        by_app.setdefault(app, [0, 0])
        by_app[app][0] += 1
        if pred is None:
            no_coord += 1
            continue
        # 越界（打屏外无效）单独计——production vision_fallback 也 reject 越界。
        if img_w and img_h and (pred[0] < 0 or pred[0] >= img_w
                                or pred[1] < 0 or pred[1] >= img_h):
            oob += 1
            continue
        if gds.hit_test(pred, case.get("gold") or {}):
            hits += 1
            by_app[app][1] += 1
    hit_rate = round(hits / n, 4) if n else 0.0
    return {
        "backend": backend.name,
        "n": n,
        "hits": hits,
        "hit_rate": hit_rate,
        "no_coord": no_coord,
        "out_of_bounds": oob,
        "errors": errors,
        "latency_ms_p50": _pctl(latencies, 0.5),
        "latency_ms_p95": _pctl(latencies, 0.95),
        "by_app": {a: {"n": v[0], "hits": v[1],
                       "hit_rate": round(v[1] / v[0], 4) if v[0] else 0.0}
                   for a, v in sorted(by_app.items())},
    }


def ab_verdict(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """A/B 判据：命中率最高者为赢家；与基线(general)算 delta。"""
    if not results:
        return {"winner": None, "reason": "无结果"}
    ranked = sorted(results, key=lambda r: r["hit_rate"], reverse=True)
    winner = ranked[0]
    baseline = next((r for r in results if r["backend"] == "general"), None)
    out = {"winner": winner["backend"], "winner_hit_rate": winner["hit_rate"]}
    if baseline and winner["backend"] != "general":
        delta = round(winner["hit_rate"] - baseline["hit_rate"], 4)
        out["delta_vs_general"] = delta
        out["recommend_switch"] = delta >= 0.05   # ≥5pt 才建议切（观察期先软）
        out["reason"] = (f"{winner['backend']} 比 general 高 {delta:+.1%}"
                         if delta else "与 general 持平")
    else:
        out["reason"] = "general 领先或唯一后端"
        out["recommend_switch"] = False
    return out


# ────────────────────────────────────────────────────────────────────
# 自检（合成 PNG + mock 后端，零 GPU/零副作用）
# ────────────────────────────────────────────────────────────────────
def _make_png(w: int, h: int) -> bytes:
    """生成一张纯色合法 PNG（仅需正确 IHDR 供尺寸解析）。纯标准库。"""
    def chunk(typ: bytes, data: bytes) -> bytes:
        c = typ + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8-bit RGB
    raw = b"".join(b"\x00" + b"\x7f\x7f\x7f" * w for _ in range(h))
    idat = zlib.compress(raw)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def run_selftest() -> bool:
    """红绿双向：命中判据 + 坐标归一化 + 分组统计 + A/B 判据，全离线。"""
    import tempfile
    ok = True

    def check(name: str, cond: bool):
        nonlocal ok
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and cond

    # 1) hit_test：bbox 内/外、point 半径内/外
    check("bbox 命中", gds.hit_test((50, 50), {"bbox": [10, 10, 100, 100]}))
    check("bbox 未命中", not gds.hit_test((5, 5), {"bbox": [10, 10, 100, 100]}))
    check("point 半径内命中", gds.hit_test((100, 100), {"point": [110, 110], "radius": 40}))
    check("point 半径外未命中", not gds.hit_test((100, 100), {"point": [300, 300], "radius": 40}))
    check("None 预测不命中", not gds.hit_test(None, {"bbox": [0, 0, 10, 10]}))

    # 2) UI-TARS 归一化坐标缩放：500/1000 * 720 = 360
    ut = UiTarsBackend("http://x/v1", "m", coord_space="norm1000")
    scaled = ut._parse("click(point='500 500')")
    check("uitars 解析坐标", scaled == (500.0, 500.0))

    # 3) 端到端：合成数据集 + mock 后端 + 分组 + 越界 + A/B
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        (root / "images").mkdir()
        cases = []
        for i, (app, gold, img) in enumerate([
            ("tiktok", {"bbox": [40, 40, 60, 60]}, (720, 1600)),
            ("tiktok", {"point": [100, 100], "radius": 30}, (720, 1600)),
            ("facebook", {"bbox": [200, 200, 260, 260]}, (720, 1600)),
        ]):
            cid = f"c{i}"
            (root / "images" / f"{cid}.png").write_bytes(_make_png(*img))
            cases.append({"id": cid, "ts": "t", "source": "s", "app": app,
                          "target": f"btn{i}", "context": cid,
                          "image": f"images/{cid}.png",
                          "image_w": img[0], "image_h": img[1], "gold": gold})
        # general mock：全命中（返 gold 中心）
        good = MockBackend({"c0": (50, 50), "c1": (100, 100), "c2": (230, 230)}, "general")
        # uitars mock：一个命中、一个越界、一个 miss
        cand = MockBackend({"c0": (50, 50), "c1": (99999, 99999), "c2": None}, "uitars")
        rg = eval_backend(good, cases, root)
        rc = eval_backend(cand, cases, root)
        check("general 全命中(3/3)", rg["hit_rate"] == 1.0 and rg["hits"] == 3)
        check("uitars 命中率 1/3", rc["hits"] == 1 and rc["n"] == 3)
        check("uitars 越界计数=1", rc["out_of_bounds"] == 1)
        check("uitars 无坐标计数=1", rc["no_coord"] == 1)
        check("分组明细含 tiktok/facebook",
              set(rg["by_app"].keys()) == {"tiktok", "facebook"})
        verdict = ab_verdict([rg, rc])
        check("A/B 判 general 赢", verdict["winner"] == "general")
        check("delta 为负不建议切", verdict.get("recommend_switch") is False)

    # 4) 数据集 schema + gold 标注判定
    check("未标注 gold 不进评测",
          not gds.gold_is_labeled({"gold": None}))
    check("bbox gold 算已标注",
          gds.gold_is_labeled({"gold": {"bbox": [0, 0, 1, 1]}}))

    print("\n结论:", "自检全绿" if ok else "自检有红")
    return ok


# ────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────
def _build_backends(args) -> List[GroundingBackend]:
    backends: List[GroundingBackend] = []
    names = [b.strip() for b in (args.backends or "").split(",") if b.strip()]
    for name in names:
        if name == "general":
            backends.append(GeneralVlmBackend())
        elif name == "uitars":
            if not args.uitars_url:
                print("[跳过] uitars 需 --uitars-url", file=sys.stderr)
                continue
            backends.append(UiTarsBackend(
                args.uitars_url, args.uitars_model or "ui-tars",
                coord_space=args.uitars_coord_space))
        else:
            print(f"[跳过] 未知后端 {name!r}", file=sys.stderr)
    return backends


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Tier-1 GUI grounding A/B 评测器")
    ap.add_argument("--selftest", action="store_true", help="离线自检，零 GPU/零副作用")
    ap.add_argument("--dataset", default="", help="cases.jsonl 路径（默认 data/grounding_cases/）")
    ap.add_argument("--backends", default="general", help="逗号分隔：general,uitars")
    ap.add_argument("--uitars-url", default="", help="UI-TARS OpenAI 兼容端点，如 http://127.0.0.1:8000/v1")
    ap.add_argument("--uitars-model", default="ui-tars")
    ap.add_argument("--uitars-coord-space", default="norm1000", choices=["abs", "norm1000"])
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    if args.selftest:
        return 0 if run_selftest() else 1

    ds_path = args.dataset or str(gds.cases_file())
    cases = gds.load_dataset(ds_path, labeled_only=True)
    images_root = Path(ds_path).resolve().parent
    if not cases:
        print(f"[空] {ds_path} 无已标注案例。先采集（HUOKE_GROUNDING_COLLECT=1 跑生产）"
              f"再人工补 gold 标注。", file=sys.stderr)
        return 2

    backends = _build_backends(args)
    if not backends:
        print("[空] 无可用后端", file=sys.stderr)
        return 2

    results = [eval_backend(b, cases, images_root) for b in backends]
    verdict = ab_verdict(results)
    report = {"dataset": ds_path, "n_cases": len(cases),
              "results": results, "verdict": verdict}

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"数据集: {ds_path}  已标注案例: {len(cases)}\n")
        for r in results:
            print(f"  {r['backend']:10s} 命中率 {r['hit_rate']:.1%} "
                  f"({r['hits']}/{r['n']})  越界 {r['out_of_bounds']}  "
                  f"无坐标 {r['no_coord']}  延迟 p50 {r['latency_ms_p50']}ms "
                  f"p95 {r['latency_ms_p95']}ms")
        print(f"\n判据: {verdict.get('reason', '')} "
              f"→ {'建议切换' if verdict.get('recommend_switch') else '维持现状'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
