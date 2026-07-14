# -*- coding: utf-8 -*-
"""人设「定妆照」批量生成器——给每个 persona 生成基准脸 + 注册相册种子照。

用法（项目根目录执行）：
    python tools/persona_photoshoot.py                 # 全部缺定妆照的人设
    python tools/persona_photoshoot.py --personas marcus_wei,zhao_laoshi
    python tools/persona_photoshoot.py --force         # 已有 face_ref 也重拍

每个人设做三件事（与线上 autosend「自动定妆」同规格，可重复执行幂等）：
  1. 用 persona.appearance + 场景轮换 + 固定种子（CRC32(pid)）经 ComfyUI FLUX 生成人像；
  2. 存为 assets/persona_media/<pid>/face_ref.png ——后续任何生成经 PuLID 用它锁脸；
  3. 复制一份 auto_selfie_*.png 登记进 persona_media.db 通用池（auto_generated 标签），
     客户要"近照/看看你"时注册相册秒发。

外部依赖仅 ComfyUI（沿用 comfy_infer.py 的显存闸门/互斥锁）；任何一步失败跳过该人设
继续下一个，退出码=失败数。
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from src.ai.companion_selfie import (  # noqa: E402
    build_selfie_prompt, pick_scene_hint, stable_selfie_seed,
)
from src.companion.persona_media_store import PersonaMediaStore  # noqa: E402

AUTO_REG_TAG = "auto_generated"


def _log(m: str) -> None:
    print("[photoshoot] " + m, flush=True)


def load_selfie_cfg() -> dict:
    """读 config.local.yaml 的 companion.selfie（本工具只需 style/scene/provider url）。"""
    p = ROOT / "config" / "config.local.yaml"
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return ((data.get("companion") or {}).get("selfie")) or {}
    except Exception as e:  # noqa: BLE001
        _log(f"config.local.yaml 读取失败({e})，用默认值")
        return {}


def load_profiles() -> dict:
    data = yaml.safe_load(
        (ROOT / "config" / "profiles_runtime.yaml").read_text(encoding="utf-8")) or {}
    return data.get("profiles") or {}


def comfy_generate(prompt: str, out: Path, *, url: str, seed: int,
                   face_ref: str = "", timeout: float = 900.0) -> bool:
    args = [sys.executable, str(ROOT / "tools" / "comfy_infer.py"),
            "--prompt", prompt, "--out", str(out), "--url", url,
            "--seed", str(seed), "--steps", "20", "--min-free-gb", "12",
            "--timeout", str(int(timeout))]
    if face_ref:
        args += ["--face-ref", face_ref]
    r = subprocess.run(args, capture_output=True, text=True,
                       timeout=timeout + 120, cwd=str(ROOT))
    for line in (r.stderr or "").splitlines():
        _log("  " + line.strip())
    return r.returncode == 0 and out.is_file() and out.stat().st_size > 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--personas", default="", help="逗号分隔的 persona id；空=全部")
    ap.add_argument("--force", action="store_true", help="已有 face_ref 也重拍")
    ap.add_argument("--url", default="http://192.168.0.176:8188")
    ap.add_argument("--album-root", default="assets/persona_media")
    ap.add_argument("--db", default="config/persona_media.db")
    ap.add_argument("--no-register", action="store_true", help="只生成不入册")
    args = ap.parse_args()

    scfg = load_selfie_cfg()
    style = str(scfg.get("style") or
                "photorealistic, candid iphone selfie, natural lighting, high detail")
    scene_default = str(scfg.get("scene_hint") or "")
    scene_pool = scfg.get("scene_rotation")

    profiles = load_profiles()
    want = [s.strip() for s in args.personas.split(",") if s.strip()] or list(profiles)
    album_root = (ROOT / args.album_root).resolve()
    st = None if args.no_register else PersonaMediaStore(str(ROOT / args.db))

    failed = 0
    for pid in want:
        p = profiles.get(pid)
        if not isinstance(p, dict):
            _log(f"跳过 {pid}：profiles_runtime.yaml 无此人设")
            failed += 1
            continue
        ddir = album_root / pid
        face_ref = ddir / "face_ref.png"
        if face_ref.is_file() and not args.force:
            _log(f"跳过 {pid}：face_ref 已存在（--force 重拍）")
            continue
        scene = pick_scene_hint(p, default_scene=scene_default,
                                fallback_scenes=scene_pool)
        prompt = build_selfie_prompt(p, scene_hint=scene, style=style)
        seed = stable_selfie_seed(pid)
        _log(f"== {pid} seed={seed}")
        _log(f"   prompt: {prompt}")
        ddir.mkdir(parents=True, exist_ok=True)
        tmp = ddir / f"_shoot_{uuid.uuid4().hex[:6]}.png"
        ok = comfy_generate(prompt, tmp, url=args.url, seed=seed)
        if not ok:
            _log(f"!! {pid} 生成失败，跳过")
            tmp.unlink(missing_ok=True)
            failed += 1
            continue
        shutil.copy2(tmp, face_ref)
        auto = ddir / f"auto_selfie_{int(time.time())}_{uuid.uuid4().hex[:6]}.png"
        tmp.rename(auto)
        _log(f"   face_ref: {face_ref.relative_to(ROOT)}")
        if st is not None:
            # 幂等：该人设已有 auto_generated 条目则不再重复登记（--force 重拍只换 face_ref）。
            has_auto = any(AUTO_REG_TAG in (r.get("tags") or [])
                           for r in st.list(pid))
            if has_auto and not args.force:
                _log("   已有入册 auto 照，跳过登记")
            else:
                row = st.add(pid, "photo", str(auto), "", triggers=[],
                             tags=[AUTO_REG_TAG], created_by="persona_photoshoot")
                _log(f"   已入册 id={row.get('id', '')[:8]}…")
    _log(f"完成：{len(want)} 个目标，失败 {failed}")
    return failed


if __name__ == "__main__":
    sys.exit(main())
