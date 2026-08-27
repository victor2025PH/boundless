# -*- coding: utf-8 -*-
"""入站识图 prompt 金标验证（只读生产配置，合成图，可重复跑）。

P0/P1 2026-08-19「乱码识图」事故的常驻验收工具：对**实际生效**的 vision 配置
（实例 overlay 优先）打两张合成金标图，验证两条互斥的硬指标没有互相吃掉：

  ① 合成票据 → 关键字段必须**逐字**出现在识别产出里（业务刚需不回退）；
  ② 合成键盘/键帽板 → 产出必须**短且连贯**（不逐字抄键帽），且不触发
     media_enrich.desc_looks_garbled 碎片汤闸门；
  ③ （v2 增强，WARN 级）首行「类型=A/B/C」标记合规——缺失只降级不失败。

环境缺失（vision 关 / 端点不可达 / 缺 requests、PIL）一律 SKIP exit 0，
不污染回归信号；断言失败 exit 1。用法：
    python tools/verify_image_desc.py [--data-root D:\\chengjie-instances\\zhiliao\\data]
"""

from __future__ import annotations

import argparse
import base64
import io
import sys
import time
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ENGINE_ROOT))

RECEIPT_FIELDS = ["ZHANG SAN", "G1234", "553.00", "EK2026082112345", "QRD881"]


def _skip(msg: str) -> "int":
    print(f"SKIP: {msg}")
    return 0


def _load_vision_cfg(data_root: str = "") -> dict:
    """引擎默认 config.yaml + （若有）实例 overlay 的 vision 段浅合并（overlay 键胜）。"""
    import yaml

    def _read(p: Path) -> dict:
        try:
            return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("vision") or {}
        except Exception:
            return {}

    cfg = _read(ENGINE_ROOT / "config" / "config.yaml")
    roots = []
    if data_root:
        roots = [Path(data_root)]
    else:
        try:  # 与其他 CLI 同契约：自动发现活跃实例数据根
            from scripts._data_root import resolve_data_roots  # type: ignore
            roots = [Path(r) for r in resolve_data_roots()]
        except Exception:
            roots = [Path(r"D:\chengjie-instances\zhiliao\data")]
    for r in roots:
        ov = _read(r / "config" / "config.local.yaml")
        if ov:
            cfg.update(ov)
            break
    return cfg


def _make_receipt() -> bytes:
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (760, 460), "white")
    d = ImageDraw.Draw(img)
    try:
        f_big = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 34)
        f = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 26)
        lines = [
            "铁路电子客票  E-Ticket",
            "姓名: 张三 (ZHANG SAN)",
            "车次: G1234    日期: 2026-08-21",
            "座位: 12车05A   票价: ¥553.00",
            "订单号: EK2026082112345",
            "取票码: QRD881   电话: 13800138000",
        ]
    except Exception:  # 无 CJK 字体环境：纯拉丁字段（断言值本就全拉丁/数字）
        f_big = f = ImageFont.load_default()
        lines = [
            "E-Ticket  Railway",
            "Name: ZHANG SAN",
            "Train: G1234    Date: 2026-08-21",
            "Seat: 05A   Price: 553.00",
            "Order: EK2026082112345",
            "Code: QRD881   Tel: 13800138000",
        ]
    y = 30
    d.text((40, y), lines[0], fill="black", font=f_big)
    y += 70
    for ln in lines[1:]:
        d.text((40, y), ln, fill="black", font=f)
        y += 56
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_keycap_board() -> bytes:
    """合成「键帽 + 侧栏卡片」图——事故同类（文字密集、零散、无叙事）。"""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.new("RGB", (980, 460), (24, 26, 32))
    d = ImageDraw.Draw(img)
    try:
        f = ImageFont.truetype("C:/Windows/Fonts/consola.ttf", 20)
    except Exception:
        f = ImageFont.load_default()
    rows = [
        ["Esc"] + [f"F{i}" for i in range(1, 13)],
        list("`1234567890-=") ,
        list("QWERTYUIOP") + ["[", "]"],
        list("ASDFGHJKL") + [";", "'"],
        list("ZXCVBNM") + [",", ".", "/"],
        ["Ctrl", "Fn", "Alt", "Space", "Alt", "Ctrl"],
    ]
    y = 24
    for row in rows:
        x = 24
        for key in row:
            w = max(34, 14 * len(key) + 14)
            d.rounded_rectangle([x, y, x + w, y + 52], radius=6,
                                fill=(52, 56, 66), outline=(90, 96, 110))
            d.text((x + 8, y + 14), key, fill=(230, 180, 90), font=f)
            x += w + 8
        y += 64
    # 侧栏假看板卡片（模拟事故图里的任务卡标题）
    cards = ["Feature removal advice 12h", "Floating hint tweak 12h",
             "Chat translation 12h", "Send follow-up 12h"]
    x0 = 700
    d.rectangle([x0 - 12, 16, 968, 444], outline=(90, 96, 110))
    yy = 30
    for c in cards:
        d.rounded_rectangle([x0, yy, 956, yy + 74], radius=8, fill=(40, 44, 54))
        d.text((x0 + 10, yy + 24), c, fill=(200, 205, 215), font=f)
        yy += 92
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _call_vlm(cfg: dict, prompt: str, png: bytes, timeout: int) -> str:
    import requests
    base = (cfg.get("base_urls") or [cfg.get("base_url") or ""])[0].rstrip("/")
    url = base + "/chat/completions" if base.endswith("/v1") else base + "/v1/chat/completions"
    body = {
        "model": cfg.get("model") or "",
        "temperature": 0,
        "max_tokens": int(cfg.get("max_tokens") or 700),
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(png).decode()}},
            ],
        }],
    }
    r = requests.post(url, json=body,
                      headers={"Authorization": f"Bearer {cfg.get('api_key') or 'x'}"},
                      timeout=timeout)
    r.raise_for_status()
    return ((r.json().get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", default="", help="实例数据根（缺省自动发现）")
    ap.add_argument("--timeout", type=int, default=150)
    args = ap.parse_args()

    try:
        import requests  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception as e:
        return _skip(f"依赖缺失 {e!r}")

    cfg = _load_vision_cfg(args.data_root)
    if not cfg.get("enabled", False):
        return _skip("vision.enabled=false")
    prompt = str(cfg.get("prompt") or "")
    if not prompt:
        return _skip("vision.prompt 为空")

    from src.inbox.media_enrich import desc_looks_garbled, parse_desc_type

    fails, warns = [], []

    def _check(name: str, ok: bool, detail: str, warn_only: bool = False) -> None:
        tag = "PASS" if ok else ("WARN" if warn_only else "FAIL")
        print(f"[{tag}] {name}: {detail}")
        if not ok:
            (warns if warn_only else fails).append(name)

    # ---- ① 票据：字段逐字 ----
    try:
        t0 = time.time()
        out_r = _call_vlm(cfg, prompt, _make_receipt(), args.timeout)
    except Exception as e:
        return _skip(f"VLM 端点不可达/调用失败 {e!r}")
    dtype_r, body_r = parse_desc_type(out_r)
    hit = [f for f in RECEIPT_FIELDS if f in out_r]
    _check("receipt.fields_verbatim", len(hit) >= 4,
           f"{len(hit)}/{len(RECEIPT_FIELDS)} 字段逐字命中 ({time.time()-t0:.1f}s, {len(out_r)}c)")
    _check("receipt.type_marker", dtype_r == "A",
           f"首行标记={dtype_r or '缺失'}（期望 A）", warn_only=True)

    # ---- ② 键帽板：短、连贯、不触闸门 ----
    t0 = time.time()
    try:
        out_k = _call_vlm(cfg, prompt, _make_keycap_board(), args.timeout)
    except Exception as e:
        return _skip(f"VLM 第二调失败 {e!r}")
    dtype_k, body_k = parse_desc_type(out_k)
    _check("keycap.short", len(body_k) <= 250,
           f"正文 {len(body_k)}c（≤250）({time.time()-t0:.1f}s)")
    _check("keycap.not_garbled", not desc_looks_garbled(out_k),
           "碎片汤闸门未触发" if not desc_looks_garbled(out_k) else "触发了碎片汤闸门（模型在逐字抄）")
    _check("keycap.no_row_dump", "qwertyuiop" not in out_k.lower().replace(" ", ""),
           "无键盘行逐字倾倒")
    _check("keycap.type_marker", dtype_k in ("B", "C"),
           f"首行标记={dtype_k or '缺失'}（期望 B/C）", warn_only=True)

    print()
    print("receipt out >>>", out_r.replace("\n", " ⏎ ")[:300])
    print("keycap  out >>>", out_k.replace("\n", " ⏎ ")[:300])
    if fails:
        print(f"\nVERDICT: FAIL ({', '.join(fails)})")
        return 1
    print(f"\nVERDICT: PASS ({len(warns)} warn)")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
