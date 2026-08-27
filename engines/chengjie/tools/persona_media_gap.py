# -*- coding: utf-8 -*-
"""人设媒体资产缺口体检（实施69 P1-2，只读）——「人设说得出、相册拿不出」清单。

背景（2026-08-24 夜市摊事故的因果根）：08-22 人设整版改「哈尔滨烧烤店老板娘」
（role/life_arc/成名记忆全是 烧烤/夜市/大腰子），相册却还是改版前的 81 张通用
自拍——职业型人设的职业场景天然高频被要图，供给为零＝每次被问都只能靠嘴圆。
**人设改版 checklist 必须跑本工具**：改文案的同时看它点名的场景类有没有货。

判定口径：
- 需求侧＝人设卡文本（role/background/tags/life_arc/photo_style_prefs/hobbies/
  specific_memories）按 ``persona_media._SCENE_CLASS_WORDS`` 场景类词表扫描——
  与选图归一层同一张表（单一事实源，表外场景本就无法定向，不算数）；
- 供给侧＝文件系统相册（``companion.selfie.provider.album_dir/<persona>/``）按
  策展文件名 + ``_meta.json`` 归类（注册相册 DB 属运营触发词精配，不在本表——
  它有货时 Stage 0 早于通用池命中，缺口语义不受影响）；
- 缺口＝需求命中 ≥1 处而供给 0 张；照单补货（外采/生成后人审入册，命名走
  ``<场景>_<系列>_<nn>.jpg`` 策展约定）。

用法：
    python tools/persona_media_gap.py                # 全部活跃实例
    python tools/persona_media_gap.py --persona lin_xiaoyu --json
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path
from typing import Any, Dict, List

_ENGINE_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ENGINE_ROOT))
sys.path.insert(0, str(_ENGINE_ROOT / "scripts"))


def _persona_text_fields(p: Dict[str, Any]) -> Dict[str, str]:
    """人设卡里「会被聊出来/会被要求拍到」的文本面（字段名→拼接文本）。"""
    out: Dict[str, str] = {}

    def _s(v: Any) -> str:
        if isinstance(v, str):
            return v
        if isinstance(v, (list, tuple)):
            return " ".join(_s(x) for x in v)
        if isinstance(v, dict):
            return " ".join(_s(x) for x in v.values())
        return ""

    for key in ("role", "background", "tags", "photo_style_prefs"):
        t = _s(p.get(key))
        if t.strip():
            out[key] = t
    la = p.get("life_arc")
    if isinstance(la, dict):
        t = _s(la.get("theme")) + " " + _s(la.get("beats"))
        if t.strip():
            out["life_arc"] = t
    ctx = p.get("context")
    if isinstance(ctx, dict):
        t = _s(ctx.get("hobbies")) + " " + _s(ctx.get("specific_memories"))
        if t.strip():
            out["context"] = t
    return out


def demanded_scene_classes(p: Dict[str, Any]) -> Dict[str, List[str]]:
    """人设文本 × 场景类词表 → {场景类: [命中的字段名…]}（纯函数可单测）。"""
    from src.companion.persona_media import _CAR_RE, _SCENE_CLASS_WORDS
    fields = _persona_text_fields(p)
    hits: Dict[str, List[str]] = {}
    for cls, words in _SCENE_CLASS_WORDS:
        for fname, text in fields.items():
            low = text.lower()
            if any(w in low for w in words):
                hits.setdefault(cls, []).append(fname)
    for fname, text in fields.items():
        if _CAR_RE.search(text.lower()):
            hits.setdefault("car", []).append(fname)
    return hits


def album_supply(album_dir: Path, persona_id: str) -> Dict[str, int]:
    """人设分册的场景类库存计数（策展文件名 + _meta.json 归类；未知场景归 ""）。"""
    from src.ai.companion_selfie import album_file_meta, load_album_meta
    d = Path(album_dir) / persona_id
    files = []
    if d.is_dir():
        files = [f for f in d.iterdir()
                 if f.is_file() and f.stem.lower() != "face_ref"
                 and f.suffix.lower() in
                 (".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov")]
    meta = load_album_meta(d) if files else {}
    supply: Dict[str, int] = {}
    for f in files:
        cls = str((album_file_meta(str(f), meta) or {}).get("scene") or "")
        supply[cls] = supply.get(cls, 0) + 1
    return supply


def gap_report_for_root(root: Path, only_persona: str = "") -> Dict[str, Any]:
    import yaml

    from _data_root import load_merged_config, profiles_runtime_path
    cfg = load_merged_config(root)
    scfg = ((cfg.get("companion") or {}).get("selfie") or {})
    album_rel = str(((scfg.get("provider") or {}).get("album_dir"))
                    or "config/persona_albums")
    album_dir = Path(album_rel)
    if not album_dir.is_absolute():
        album_dir = Path(root) / album_rel   # 服务进程 CWD=数据根 契约
    prof_f = profiles_runtime_path(root)
    personas: Dict[str, Any] = {}
    if prof_f.is_file():
        raw = yaml.safe_load(prof_f.read_text(encoding="utf-8")) or {}
        # profiles_runtime 顶层是 {profiles: {persona_id: {...}}, _history, …}；
        # 兼容无包裹层的旧形态（直接 {persona_id: {...}}）。
        if isinstance(raw, dict):
            body = raw.get("profiles") if isinstance(
                raw.get("profiles"), dict) else raw
            personas = {k: v for k, v in body.items()
                        if isinstance(v, dict) and v.get("id")}
    rep = {"root": str(root), "album_dir": str(album_dir), "personas": []}
    for pid, p in sorted(personas.items()):
        if only_persona and pid != only_persona:
            continue
        # 人设级发图闸（capabilities.photos，默认关）：能力关的人设不会被系统
        # 发图，零库存是常态不是缺口——缺口只对「真会被要图」的人设有意义。
        caps = p.get("capabilities") if isinstance(
            p.get("capabilities"), dict) else {}
        photos_on = bool(caps.get("photos", False))
        demand = demanded_scene_classes(p)
        supply = album_supply(album_dir, pid)
        gaps = sorted(cls for cls in demand
                      if supply.get(cls, 0) <= 0) if photos_on else []
        rep["personas"].append({
            "persona": pid,
            "photos_enabled": photos_on,
            "album_total": sum(supply.values()),
            "demand": {k: v for k, v in sorted(demand.items())},
            "supply": {k: v for k, v in sorted(supply.items()) if k},
            "unclassified": supply.get("", 0),
            "gaps": gaps,
        })
    return rep


def main() -> int:
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="人设媒体资产缺口体检（只读）")
    ap.add_argument("--data-root", default="")
    ap.add_argument("--persona", default="", help="只看指定人设 id")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from _data_root import resolve_data_roots
    roots = resolve_data_roots(args.data_root)
    reports = [gap_report_for_root(r, args.persona) for r in roots]
    if args.json:
        print(json.dumps(reports, ensure_ascii=False, indent=2))
        return 0
    for rep in reports:
        print(f"=== {rep['root']} (相册根 {rep['album_dir']}) ===")
        for p in rep["personas"]:
            flag = "  ⚠ GAP" if p["gaps"] else ""
            cap = "" if p["photos_enabled"] else "（发图能力关，缺口不计）"
            print(f"  {p['persona']}: 相册 {p['album_total']} 张"
                  f"（未归类 {p['unclassified']}）{cap}{flag}")
            if p["demand"]:
                print(f"    人设点名场景类: "
                      + ", ".join(f"{k}({'/'.join(v)})"
                                  for k, v in p["demand"].items()))
            if p["supply"]:
                print(f"    库存: " + ", ".join(
                    f"{k}={v}" for k, v in p["supply"].items()))
            if p["gaps"]:
                print(f"    >>> 缺口（人设说得出、相册拿不出）: "
                      + ", ".join(p["gaps"]))
        if not rep["personas"]:
            print("  （无 profiles_runtime 人设）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
