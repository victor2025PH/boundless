# -*- coding: utf-8 -*-
"""启动表情包资产门禁（starter-faces，2026-08-17 表情包 P2）。

资产是 scripts/gen_starter_stickers.py 程序化生成后**提交进仓**的产物——门禁钉三件事：
① 字节有效：RIFF/WEBP magic + 512×512 + 透明底（媒体产物验证纪律：文件存在/
  尺寸 KB 数不构成内容验证，必须验 magic bytes）；
② 生产规范化管线能吃（seed-official 播种时逐张过 normalize_sticker，这里先红
  就不用等线上播种才发现导不进去）；
③ manifest 与生成脚本 STICKERS 表逐项一致（文件名/emoji/keywords 单一事实源
  ＝脚本内表；改脚本忘改清单、或手改清单漂移，立刻点名）。

刻意不在门禁里重渲（单张 ~15s，全量 2min+ 拖垮 CI）：字节有效性 + 表一致性
足以保证「资产可用且与脚本同源」；重渲验证属 gen 脚本自带（写盘前逐张自验）。
"""
import io
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_OFFICIAL = _ROOT / "assets" / "sticker_packs" / "official"
_MANIFEST = _OFFICIAL / "manifest.json"

pytest.importorskip("PIL", reason="Pillow 未安装（requirements 既有依赖）")


def _starter_pack():
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))
    packs = [p for p in (manifest.get("packs") or [])
             if p.get("id") == "starter-faces"]
    assert packs, "manifest 缺 starter-faces 包"
    return packs[0]


def test_manifest_starter_entries_are_tagged_dicts():
    files = _starter_pack().get("files") or []
    assert len(files) == 10
    seen = set()
    for ent in files:
        assert isinstance(ent, dict), "starter 条目必须是字典形态（带检索标签）"
        rel = str(ent.get("file") or "")
        assert rel and rel not in seen, f"路径重复或为空: {rel!r}"
        seen.add(rel)
        assert str(ent.get("emoji") or "").strip(), f"{rel} 缺 emoji 标签"
        kws = [k for k in (ent.get("keywords") or []) if str(k).strip()]
        assert len(kws) >= 2, f"{rel} 关键词过少（面板搜索靠它过滤）"
        fp = (_OFFICIAL / rel).resolve()
        assert _OFFICIAL.resolve() in fp.parents, f"{rel} 越出官方目录"
        assert fp.is_file(), (
            f"{rel} 资产缺失——跑 python scripts/gen_starter_stickers.py")


def test_assets_valid_webp_512_transparent():
    from PIL import Image
    for ent in _starter_pack()["files"]:
        fp = _OFFICIAL / str(ent["file"])
        data = fp.read_bytes()
        assert data[:4] == b"RIFF" and data[8:12] == b"WEBP", (
            f"{fp.name} magic bytes 不是 WEBP")
        im = Image.open(io.BytesIO(data))
        im.load()
        assert im.size == (512, 512), f"{fp.name} 尺寸 {im.size} != (512,512)"
        assert im.convert("RGBA").getpixel((2, 2))[3] == 0, (
            f"{fp.name} 角落不透明（贴纸应为透明底）")


def test_assets_roundtrip_production_normalizer():
    from src.inbox.sticker_normalize import TARGET_WEBP_BYTES, normalize_sticker
    for ent in _starter_pack()["files"]:
        fp = _OFFICIAL / str(ent["file"])
        norm = normalize_sticker(fp.read_bytes(), source_ext=".webp")
        assert norm["animated"] is False, f"{fp.name} 被误判为动图"
        assert len(norm["webp"]) <= TARGET_WEBP_BYTES, f"{fp.name} 规范化后超目标体积"


def test_manifest_matches_generator_table():
    from scripts.gen_starter_stickers import STICKERS
    man = [(Path(str(e["file"])).stem, str(e.get("emoji") or ""),
            [str(k) for k in (e.get("keywords") or [])])
          for e in _starter_pack()["files"]]
    gen = [(n, e, list(kw)) for n, e, kw in STICKERS]
    assert man == gen, "manifest 与 gen_starter_stickers.STICKERS 漂移（两边要一起改）"
