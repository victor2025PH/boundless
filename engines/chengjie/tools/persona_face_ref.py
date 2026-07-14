# -*- coding: utf-8 -*-
"""为每个人设生成高质量「正脸身份照」作 face_ref（换脸源）。

face_ref 是整条视觉链的脸基准：PuLID 锁脸参考、faceswap 换脸源、AvatarHub 口播形象。
换脸/锁脸对源图的要求 ≠ 自拍：要**清晰正脸、五官舒展、中性背景、看镜头、无遮挡**，
这样 inswapper 提取的人脸嵌入才稳。本工具用专门的证件照式 prompt 生成，并经
vision_gate 体检（单人/性别年龄/非动物），不合格自动换种子重试。

用法（项目根目录）：
    python tools/persona_face_ref.py                    # 全部人设（覆盖旧 face_ref）
    python tools/persona_face_ref.py --personas lin_xiaoyu,marcus_wei
    python tools/persona_face_ref.py --keep-existing    # 已有 face_ref 的跳过
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from src.ai.companion_selfie import (  # noqa: E402
    SelfieProvider, _persona_smart_base, _persona_visual, stable_selfie_seed,
)
from src.ai.image_gate import check_image, persona_expectations  # noqa: E402

_ID_STYLE = ("front-facing headshot portrait, looking straight at the camera, "
             "neutral soft grey studio background, even soft lighting, relaxed "
             "natural expression, face fully visible unobstructed, sharp focus, "
             "photorealistic, high detail, 85mm portrait lens")


def _log(m: str) -> None:
    print("[face_ref] " + m, flush=True)


def build_id_prompt(persona: dict) -> str:
    base = _persona_visual(persona) or _persona_smart_base(persona) \
        or "a warm friendly young person"
    return (f"{base}, {_ID_STYLE}, solo, one person")


async def gen_one(prov: SelfieProvider, persona: dict, pid: str,
                  root_cfg: dict, out_dir: Path, *, retries: int = 2) -> Path | None:
    prompt = build_id_prompt(persona)
    seed0 = stable_selfie_seed(pid + "_face_ref")
    eg, age = persona_expectations(persona)
    _log(f"{pid}: {prompt[:110]}…")
    for attempt in range(retries + 1):
        seed = (seed0 + 7919 * attempt) % (2 ** 31)
        res = await prov.generate(prompt, seed=seed)
        if not (res and res.ok and res.image_path):
            _log(f"  attempt {attempt}: 生成失败 {getattr(res, 'error', '?')}")
            continue
        ok, reason = await check_image(res.image_path, persona, root_cfg,
                                       age_tolerance=15)
        if ok:
            dst = out_dir / "face_ref.png"
            # 先清掉旧 face_ref.*（多扩展名并存会让 reference_image 选择歧义）
            for old in out_dir.glob("face_ref.*"):
                try:
                    old.unlink()
                except Exception:
                    pass
            shutil.copy2(res.image_path, dst)
            _log(f"  OK seed={seed} → {dst.relative_to(ROOT)}")
            return dst
        _log(f"  attempt {attempt} 体检不过：{reason}（换种子重试）")
    _log(f"!! {pid}: 多次不合格，跳过")
    return None


async def main_async(args) -> int:
    profiles = (yaml.safe_load(
        (ROOT / "config" / "profiles_runtime.yaml").read_text(encoding="utf-8"))
        or {}).get("profiles") or {}
    cfg = yaml.safe_load(
        (ROOT / "config" / "config.local.yaml").read_text(encoding="utf-8")) or {}
    scfg = ((cfg.get("companion") or {}).get("selfie") or {})
    prov = SelfieProvider(scfg.get("provider") or {})
    album_root = ROOT / str((scfg.get("provider") or {}).get(
        "album_dir") or "assets/persona_media")
    want = [s.strip() for s in args.personas.split(",") if s.strip()] or list(profiles)

    failed = 0
    for pid in want:
        p = profiles.get(pid)
        if not isinstance(p, dict):
            _log(f"跳过 {pid}：无此人设")
            failed += 1
            continue
        d = album_root / pid
        d.mkdir(parents=True, exist_ok=True)
        if args.keep_existing and list(d.glob("face_ref.*")):
            _log(f"跳过 {pid}：已有 face_ref（--keep-existing）")
            continue
        if await gen_one(prov, p, pid, cfg, d, retries=args.retries) is None:
            failed += 1
    _log(f"完成：{len(want)} 目标，失败 {failed}")
    return failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--personas", default="")
    ap.add_argument("--keep-existing", action="store_true")
    ap.add_argument("--retries", type=int, default=2)
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
