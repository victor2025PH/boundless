# -*- coding: utf-8 -*-
"""每人设「角色 LoRA」训练集生成器（真人感根治管线第 1-2 步）。

做四件事（都可选/可重跑，幂等到 --out 目录）：
  1. **批量出多样图**：用现有 face_ref + PuLID（--pulid-start-at 松开姿态）+ 多样性池
     （每张 variety_salt=i，覆盖不同景别/角度/表情/视线）经 ComfyUI 生成 N 张 → --out/img_XX.png。
  2. **VLM 自动筛**（--curate）：每张问 176/140 的 qwen2.5vl「是否恰好一个真人、无动物/
     文字/水印」→ 不合格移到 --out/_rejected/（训练集质量优先，宁缺勿滥）。
  3. **VLM 自动打标**（--caption --trigger ohwx）：每张让 VLM 产客观外观/场景描述 →
     拼 "<trigger> woman, <desc>" 写同名 .txt sidecar（ai-toolkit/kohya 读取）。
  4. **发训练配置**（--emit-train-config）：生成 ai-toolkit(ostris) 的 FLUX LoRA config
     → --out/../<pid>_flux_lora.yaml，改改路径即可在 176 上开训（见 docs/PERSONA_CHARACTER_LORA.md）。

铁律遵守：本进程不加载任何 GPU/TTS 模型——出图走 comfy_infer 子进程、VLM 走
VisionClient 的 HTTP（176/140 双活）、训练是 176 上的**外部** launcher（本工具只生成配置）。

用法（项目根目录执行）：
    python tools/persona_lora_dataset.py --persona lin_xiaoyu --n 24 --curate \
        --caption --trigger linxy --emit-train-config
    # 出图不筛不标（先看图质量再决定）：
    python tools/persona_lora_dataset.py --persona lin_xiaoyu --n 8

任何单张失败跳过继续；退出码=生成失败数。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from src.ai.companion_selfie import pick_scene_hint  # noqa: E402
from src.ai import face_fidelity as ff  # noqa: E402
from src.ai import persona_lora as pl  # noqa: E402


def _log(m: str) -> None:
    print("[lora-dataset] " + m, flush=True)


def _load_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as e:  # noqa: BLE001
        _log(f"读取 {path.name} 失败({e})")
        return {}


def _deep_get(d: dict, *keys):
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def load_merged_config() -> dict:
    """config.yaml 叠加 config.local.yaml（只关心 vision / companion.selfie 两段，浅合并）。"""
    base = _load_yaml(ROOT / "config" / "config.yaml")
    local = _load_yaml(ROOT / "config" / "config.local.yaml")
    merged = dict(base)
    for top in ("vision", "companion"):
        b = base.get(top) if isinstance(base.get(top), dict) else {}
        l = local.get(top) if isinstance(local.get(top), dict) else {}
        if b or l:
            merged[top] = {**b, **l}
    return merged


def load_persona(pid: str) -> dict:
    profiles = (_load_yaml(ROOT / "config" / "profiles_runtime.yaml").get("profiles")) or {}
    p = profiles.get(pid)
    return p if isinstance(p, dict) else {}


def comfy_generate(prompt: str, out: Path, *, url: str, seed: int, face_ref: str,
                   pulid_start_at: float, face_weight: float,
                   timeout: float = 900.0) -> bool:
    """经 comfy_infer 子进程出一张图（沿用其显存闸门/互斥锁/PuLID 姿态松绑）。"""
    args = [sys.executable, str(ROOT / "tools" / "comfy_infer.py"),
            "--prompt", prompt, "--out", str(out), "--url", url,
            "--seed", str(seed), "--steps", "20", "--min-free-gb", "12",
            "--timeout", str(int(timeout))]
    if face_ref:
        args += ["--face-ref", face_ref,
                 "--pulid-start-at", str(pulid_start_at),
                 "--face-weight", str(face_weight)]
    try:
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout + 120, cwd=str(ROOT))
    except Exception as e:  # noqa: BLE001
        _log(f"  子进程异常: {e}")
        return False
    for line in (r.stderr or "").splitlines()[-3:]:
        _log("  " + line.strip())
    return r.returncode == 0 and out.is_file() and out.stat().st_size > 0


def make_vision_client(cfg: dict):
    """从 merged config 的 vision 段构造并初始化 VisionClient；不可用返回 None（VLM 步优雅跳过）。"""
    try:
        from src.vision_client import VisionClient, has_any_vision_backend
        vcfg = cfg.get("vision") if isinstance(cfg.get("vision"), dict) else {}
        if not vcfg or not has_any_vision_backend(vcfg, vcfg):
            _log("vision 未配置可用后端 → 跳过 VLM 筛选/打标")
            return None
        vc = VisionClient(config=vcfg)
        if not vc.initialize():
            _log("VisionClient 初始化失败 → 跳过 VLM 筛选/打标")
            return None
        return vc
    except Exception as e:  # noqa: BLE001
        _log(f"构造 VisionClient 异常({e}) → 跳过 VLM 步")
        return None


def run_for_persona(pid: str, args, cfg: dict, vc, out_dir: Path, embed=None) -> int:
    """为单个人设跑完整数据集流程（出图→筛→标→manifest→train-config）。返回生成失败数。

    ``vc``＝已初始化的 VisionClient（批量复用）或 None（不做 VLM 内容筛/打标）。
    ``embed``＝人脸嵌入器（批量复用）或 None（不做人脸身份筛）。
    """
    scfg = _deep_get(cfg, "companion", "selfie") or {}
    persona = load_persona(pid)
    if not persona and not args.appearance:
        _log(f"跳过 {pid}：profiles_runtime.yaml 无此人设且未给 --appearance")
        return 1
    appearance = args.appearance.strip() or str(scfg.get("appearance") or "")
    style = str(scfg.get("style") or "")
    scene_default = str(scfg.get("scene_hint") or "")
    scene_pool = scfg.get("scene_rotation")

    face_ref = args.face_ref.strip() or str(
        ROOT / "assets" / "persona_media" / pid / "face_ref.png")
    if not Path(face_ref).is_file():
        _log(f"⚠ {pid} 基准脸不存在: {face_ref}（无 PuLID 锁脸，人脸可能漂；建议先 persona_photoshoot）")
        face_ref = ""

    out_dir.mkdir(parents=True, exist_ok=True)
    _log(f"== persona={pid} n={args.n} out={out_dir} face_ref={'有' if face_ref else '无'}")

    # ① 批量出多样图 ----------------------------------------------------------
    made: list = []
    failed = 0
    for i in range(int(args.n)):
        scene = pick_scene_hint(persona or appearance, default_scene=scene_default,
                                fallback_scenes=scene_pool, salt=i)
        prompt = pl.dataset_prompt(persona or appearance, scene_hint=scene,
                                   style=style, default_appearance=appearance, index=i)
        seed = (args.seed_base + i) if args.seed_base > 0 else (uuid.uuid4().int % (2 ** 31))
        img = out_dir / f"img_{i:03d}.png"
        _log(f"[{pid} {i + 1}/{args.n}] seed={seed} scene={scene[:40]!r}")
        if comfy_generate(prompt, img, url=args.url, seed=seed, face_ref=face_ref,
                          pulid_start_at=args.pulid_start_at, face_weight=args.face_weight):
            made.append(img)
        else:
            _log(f"  !! {pid} 第 {i} 张失败，跳过")
            failed += 1
    if not made:
        _log(f"{pid} 没有生成任何图")
        return max(1, failed)

    # ② 自动筛（源头治理，训练集质量优先）——两道正交门，**先便宜后昂贵**：
    #   人脸身份门（本地 CPU 嵌入 ~200ms）先跑丢无脸/串脸 → 存活的再送 VLM 内容门
    #   （HTTP ~1-3s，判多人/动物/文字/非真人），省贵调用。清洗时算出的嵌入复用于
    #   「训练集身份健康度」摘要（训练前就知道数据集干不干净）。
    rej = out_dir / "_rejected"
    kept = list(made)
    emb_map: dict = {}          # img → 嵌入（复用给健康度摘要）
    ref_vec = None
    face_dropped = 0
    if args.curate and embed is not None and face_ref:
        ref_vec = embed(face_ref)
        if ref_vec is None:
            _log(f"⚠ {pid} face_ref 未检出脸 → 跳过人脸身份门（仅 VLM 门）")
    if args.curate and embed is not None and ref_vec is not None:
        survivors = []
        for img in kept:
            vec = embed(str(img))
            score = ff.cosine(vec, ref_vec) if vec is not None else None
            keep, reason = ff.curation_decision(score, min_score=args.face_min)
            if keep:
                emb_map[str(img)] = vec
                survivors.append(img)
            else:
                rej.mkdir(exist_ok=True)
                shutil.move(str(img), str(rej / img.name))
                face_dropped += 1
                _log(f"  人脸门筛除 {img.name}: {reason}")
        _log(f"{pid} 人脸身份门后保留 {len(survivors)}/{len(kept)}（丢 {face_dropped}）")
        kept = survivors

    vlm_dropped = 0
    if vc is not None and args.curate:
        survivors = []
        for img in kept:
            try:
                verdict = vc.describe_image_sync(str(img), pl.single_person_probe_prompt())
            except Exception:  # noqa: BLE001
                verdict = None
            if pl.parse_single_person_verdict(verdict or ""):
                survivors.append(img)
            else:
                rej.mkdir(exist_ok=True)
                shutil.move(str(img), str(rej / img.name))
                emb_map.pop(str(img), None)
                vlm_dropped += 1
                _log(f"  内容门筛除 {img.name}: {str(verdict or '')[:50]!r}")
        _log(f"{pid} VLM 内容门后保留 {len(survivors)}/{len(kept)}（丢 {vlm_dropped}）")
        kept = survivors

    # 训练集身份健康度（复用清洗时的嵌入，零额外算力）：kept 各图 vs face_ref 的
    # 保真度 + 两两自一致性 → 训练前就量出"数据集有多干净"。
    health = None
    if ref_vec is not None and kept:
        kvecs = [emb_map[str(img)] for img in kept if str(img) in emb_map]
        if kvecs:
            ref_scores = [ff.cosine(v, ref_vec) for v in kvecs]
            health = ff.summarize_fidelity(
                ref_scores, self_consistency=ff.pairwise_mean_cosine(kvecs),
                no_face=0, generated=len(kept))
            _log(f"{pid} 训练集身份健康度: ref_mean={health['ref_mean']} "
                 f"min={health['ref_min']} self={health['self_consistency']} "
                 f"（越高=数据集越纯，训练前预判 LoRA 上限）")

    captioned = 0
    if vc is not None and args.caption:
        probe = pl.caption_probe_prompt(args.subject_class)
        for img in kept:
            try:
                desc = vc.describe_image_sync(str(img), probe)
            except Exception:  # noqa: BLE001
                desc = None
            line = pl.build_lora_caption(args.trigger, desc or "",
                                         subject_class=args.subject_class)
            img.with_suffix(".txt").write_text(line, encoding="utf-8")
            captioned += 1
        _log(f"{pid} 已打标 {captioned} 张（trigger={pl.sanitize_trigger(args.trigger)}）")

    manifest = {
        "persona": pid, "trigger": pl.sanitize_trigger(args.trigger),
        "subject_class": args.subject_class, "kept": len(kept),
        "generated": len(made), "failed": failed, "face_ref": face_ref,
        "curated": bool(args.curate), "captioned": captioned,
        "face_dropped": face_dropped, "vlm_dropped": vlm_dropped,
        "identity_health": health,
        "images": [p.name for p in kept],
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # ④ 发训练配置 ------------------------------------------------------------
    if args.emit_train_config:
        conf = pl.build_aitoolkit_config(
            persona_id=pid, dataset_dir=str(out_dir),
            output_dir=str(out_dir.parent / "output"), trigger=args.trigger,
            base_model=args.base_model, steps=args.steps, rank=args.rank,
            subject_class=args.subject_class)
        cfg_path = out_dir.parent / f"{pid}_flux_lora.yaml"
        cfg_path.write_text(yaml.safe_dump(conf, allow_unicode=True, sort_keys=False),
                            encoding="utf-8")
        _log(f"{pid} 训练配置: {cfg_path}")
    _log(f"{pid} 完成：生成 {len(made)}、保留 {len(kept)}、失败 {failed}")
    return failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--persona", default="", help="persona id（profiles_runtime.yaml 的键）")
    ap.add_argument("--all-personas", action="store_true",
                    help="对 profiles_runtime.yaml 全部人设批量跑（各出到 datasets/lora/<pid>/）")
    ap.add_argument("--n", type=int, default=24, help="生成张数（角色 LoRA 常 20~40 张足够）")
    ap.add_argument("--out", default="", help="数据集目录（默认 datasets/lora/<persona>）")
    ap.add_argument("--url", default="http://192.168.0.176:8188")
    ap.add_argument("--face-ref", default="", help="基准脸（默认 assets/persona_media/<pid>/face_ref.png）")
    ap.add_argument("--appearance", default="", help="覆盖 persona.appearance（英文外貌描述）")
    ap.add_argument("--pulid-start-at", type=float, default=0.15,
                    help="数据集出图的 PuLID 起点：略高(0.15)让姿态更放开、多样性更足")
    ap.add_argument("--face-weight", type=float, default=0.85,
                    help="数据集出图锁脸强度：略高(0.85)保证训练集里都是同一个人")
    ap.add_argument("--seed-base", type=int, default=0, help="种子基（每张 seed=base+i，0=随机基）")
    ap.add_argument("--curate", action="store_true",
                    help="自动清洗：人脸身份门(嵌入 vs face_ref)+VLM 内容门(多人/动物/文字/非真人)")
    ap.add_argument("--face-min", type=float, default=0.35,
                    help="人脸身份门阈值：脸与 face_ref 余弦 < 此值即剔（默认 0.35，训练集偏严）")
    ap.add_argument("--model", default="buffalo_l", help="insightface 模型名（人脸身份门）")
    ap.add_argument("--caption", action="store_true", help="VLM 自动打标写 .txt sidecar")
    ap.add_argument("--trigger", default="ohwx", help="训练触发词（稀有 token，如 ohwx/linxy）")
    ap.add_argument("--subject-class", default="woman", help="主体类别词（woman/man）")
    ap.add_argument("--emit-train-config", action="store_true",
                    help="生成 ai-toolkit FLUX LoRA 训练 config yaml")
    ap.add_argument("--base-model", default="black-forest-labs/FLUX.1-dev",
                    help="训练基座（可填 176 上 FLUX.1-dev 本地目录）")
    ap.add_argument("--steps", type=int, default=2000)
    ap.add_argument("--rank", type=int, default=16)
    args = ap.parse_args()

    cfg = load_merged_config()
    # 人设清单：--all-personas 取 profiles_runtime 全部键；否则单个 --persona。
    if args.all_personas:
        profiles = (_load_yaml(ROOT / "config" / "profiles_runtime.yaml").get("profiles")) or {}
        pids = list(profiles)
        if not pids:
            _log("profiles_runtime.yaml 无任何人设 → 退出")
            return 1
        out_root = Path(args.out.strip()) if args.out.strip() else (ROOT / "datasets" / "lora")
    else:
        pid = args.persona.strip()
        if not pid:
            _log("需 --persona <id> 或 --all-personas → 退出")
            return 1
        pids = [pid]
        out_root = None  # 单人设用各自默认目录

    # VLM 客户端 + 人脸嵌入器各只建一次跨人设复用（批量省重复初始化）。
    vc = make_vision_client(cfg) if (args.curate or args.caption) else None
    embed = None
    if args.curate:
        embed = ff.load_face_embedder(model_name=args.model)
        if embed is None:
            _log("人脸身份门不可用（insightface 未装）→ --curate 仅走 VLM 内容门。"
                 "启用：pip install insightface onnxruntime")

    total_failed = 0
    for pid in pids:
        out_dir = (out_root / pid) if out_root is not None else Path(
            args.out.strip() or (ROOT / "datasets" / "lora" / pid))
        try:
            total_failed += run_for_persona(pid, args, cfg, vc, out_dir, embed=embed)
        except Exception as e:  # noqa: BLE001
            _log(f"!! {pid} 处理异常，跳过: {e}")
            total_failed += 1
    _log(f"全部完成：{len(pids)} 个人设，累计生成失败 {total_failed}")
    if not args.caption:
        _log("提示：未打标。可加 --caption --trigger <token>，或用 ai-toolkit 自带 caption。")
    return total_failed


if __name__ == "__main__":
    sys.exit(main())
