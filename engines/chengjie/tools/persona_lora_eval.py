# -*- coding: utf-8 -*-
"""角色 LoRA / 锁脸配置的**身份保真度**客观评测（真人版声纹探针）。

对每个人设：按**生产同款**参数（该人设 LoRA + PuLID + 多样性 prompt）生成 N 张 →
insightface 抽 ArcFace 脸向量 → 与 ``face_ref`` 比余弦（保真度）+ N 张两两比余弦
（自一致性）+ 统计无脸张数 → 出 ok/warn/fail 总判，追加 ``logs/lora_fidelity.jsonl``。
把"这个 LoRA 训得够不够像""锁脸配置正不正常"从人眼变成可回归的数。

铁律遵守：出图走 comfy_infer 子进程；人脸嵌入用 insightface **CPU** onnx（不违显存纪律，
与 176 PuLID 同族）——**opt-in**，未装则优雅跳过（`pip install insightface onnxruntime`）。

用法（项目根目录）：
    python tools/persona_lora_eval.py --persona lin_xiaoyu --n 12
    python tools/persona_lora_eval.py --persona lin_xiaoyu --baseline      # A/B：量 LoRA 边际增益
    python tools/persona_lora_eval.py --persona lin_xiaoyu --from-dir datasets/lora/lin_xiaoyu  # 零 GPU 复评已有图
    python tools/persona_lora_eval.py --all-personas

verdict=fail → 退出码 1（可接计划任务/CI 告警）；ok/warn → 0。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:
    pass

import yaml  # noqa: E402

from src.ai import face_fidelity as ff  # noqa: E402
from src.ai import persona_lora as pl  # noqa: E402
from src.ai.companion_selfie import (  # noqa: E402
    build_selfie_prompt, pick_scene_hint, resolve_persona_lora,
)

_IMG_EXT = {".png", ".jpg", ".jpeg", ".webp"}
OUT_JSONL = ROOT / "logs" / "lora_fidelity.jsonl"


def _log(m: str) -> None:
    print("[lora-eval] " + m, flush=True)


def _load_yaml(path: Path) -> dict:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def load_merged_config() -> dict:
    base = _load_yaml(ROOT / "config" / "config.yaml")
    local = _load_yaml(ROOT / "config" / "config.local.yaml")
    merged = dict(base)
    for top in ("companion",):
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
                   lora: str, lora_weight: float, timeout: float = 900.0) -> bool:
    args = [sys.executable, str(ROOT / "tools" / "comfy_infer.py"),
            "--prompt", prompt, "--out", str(out), "--url", url,
            "--seed", str(seed), "--steps", "20", "--min-free-gb", "12",
            "--timeout", str(int(timeout))]
    if face_ref:
        args += ["--face-ref", face_ref, "--pulid-start-at", str(pulid_start_at),
                 "--face-weight", str(face_weight)]
    if lora:
        args += ["--lora", lora, "--lora-weight", str(lora_weight)]
    try:
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout + 120, cwd=str(ROOT))
    except Exception as e:  # noqa: BLE001
        _log(f"  子进程异常: {e}")
        return False
    return r.returncode == 0 and out.is_file() and out.stat().st_size > 0


def _score_images(paths: list, ref_vec: list, embed) -> tuple:
    """对一组图片抽脸→与 ref 比余弦 + 收集向量算自一致性 + 无脸计数。返回 (ref_scores, vecs, no_face)。"""
    ref_scores: list = []
    vecs: list = []
    no_face = 0
    for p in paths:
        vec = embed(str(p))
        if vec is None:
            no_face += 1
            _log(f"  · {Path(p).name}: 未检出脸")
            continue
        sc = ff.cosine(vec, ref_vec)
        ref_scores.append(sc)
        vecs.append(vec)
        _log(f"  · {Path(p).name}: {sc:.4f} ({ff.classify_fidelity(sc)})")
    return ref_scores, vecs, no_face


def _gen_images(args, persona, scfg, spec, face_ref, n, tag, *, use_trigger=True):
    """按 spec 生成 n 张评测图（生产同款参数 + 多样性 salt）。返回落盘路径列表。"""
    style = str(scfg.get("style") or "")
    appearance = str(scfg.get("appearance") or "")
    tmp = ROOT / "tmp_lora_eval" / tag
    tmp.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(int(n)):
        scene = pick_scene_hint(persona or appearance,
                                default_scene=str(scfg.get("scene_hint") or ""),
                                fallback_scenes=scfg.get("scene_rotation"), salt=i)
        prompt = build_selfie_prompt(
            persona or appearance, scene_hint=scene, style=style,
            default_appearance=appearance, variety_salt=i,
            lora_trigger=(spec["trigger"] if use_trigger else ""))
        out = tmp / f"img_{i:03d}.png"
        seed = (args.seed_base + i) if args.seed_base > 0 else (uuid.uuid4().int % (2 ** 31))
        if comfy_generate(prompt, out, url=args.url, seed=seed, face_ref=face_ref,
                          pulid_start_at=args.pulid_start_at, face_weight=args.face_weight,
                          lora=spec["file"], lora_weight=spec["weight"]):
            paths.append(str(out))
        else:
            _log(f"  !! {tag} 第 {i} 张生成失败")
    return paths


def eval_persona(pid, args, cfg, embed, *, spec_override=None, n=None,
                 tag=None, do_baseline=None) -> dict:
    """评测单人设 → ``{verdict, summary, spec}``（verdict∈ok/warn/fail/skip）。写一行 jsonl。

    ``spec_override``＝指定 LoRA spec（checkpoint 选优时逐候选传入）；否则按配置解析
    （并受 --lora-file/--trigger/--lora-weight 覆写）。
    """
    scfg = ((cfg.get("companion") or {}).get("selfie")) or {}
    persona = load_persona(pid)
    if spec_override is not None:
        spec = dict(spec_override)
    else:
        spec = resolve_persona_lora(persona, scfg)
        if str(getattr(args, "lora_file", "") or "").strip():
            spec["file"] = args.lora_file.strip()
        if str(getattr(args, "trigger", "") or "").strip():
            spec["trigger"] = args.trigger.strip()
        if float(getattr(args, "lora_weight", 0) or 0) > 0:
            spec["weight"] = float(args.lora_weight)
    n = int(n or args.n)
    face_ref = args.face_ref.strip() or str(
        ROOT / "assets" / "persona_media" / pid / "face_ref.png")
    if not Path(face_ref).is_file():
        _log(f"{pid}: face_ref 不存在（{face_ref}）→ 跳过")
        return {"verdict": "skip", "spec": spec}
    ref_vec = embed(face_ref)
    if ref_vec is None:
        _log(f"{pid}: face_ref 未检出脸（{face_ref}）→ 跳过")
        return {"verdict": "skip", "spec": spec}

    # 取图：--from-dir 复评已有图（零 GPU，仅无 spec_override 时）；否则现生成。
    if args.from_dir and spec_override is None:
        d = Path(args.from_dir)
        paths = sorted(str(p) for p in d.iterdir()
                       if p.is_file() and p.suffix.lower() in _IMG_EXT
                       and not p.name.startswith("face_ref"))[: n]
    else:
        _log(f"{pid}: 生成 {n} 张（lora={spec['file'] or '-'} trigger={spec['trigger'] or '-'}）")
        paths = _gen_images(args, persona, scfg, spec, face_ref, n, tag or pid)
    if not paths:
        _log(f"{pid}: 无可评图 → 跳过")
        return {"verdict": "skip", "spec": spec}

    ref_scores, vecs, no_face = _score_images(paths, ref_vec, embed)
    self_cons = ff.pairwise_mean_cosine(vecs)
    summary = ff.summarize_fidelity(
        ref_scores, self_consistency=self_cons, no_face=no_face, generated=len(paths))
    verdict = ff.fidelity_verdict(summary)

    row = {
        "ts": time.time(), "date": time.strftime("%Y-%m-%d"), "persona": pid,
        "lora": spec["file"], "lora_weight": spec["weight"],
        "from_dir": bool(args.from_dir and spec_override is None),
        "verdict": verdict, **summary,
    }
    OUT_JSONL.parent.mkdir(parents=True, exist_ok=True)
    with OUT_JSONL.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

    mark = {"ok": "✓", "warn": "⚠", "fail": "✗"}.get(verdict, "?")
    sc_txt = f" self={self_cons:.3f}" if self_cons is not None else ""
    _log(f"{mark} {pid}: verdict={verdict} ref_mean={summary['ref_mean']} "
         f"p10={summary['ref_p10']} min={summary['ref_min']}{sc_txt} "
         f"no_face={no_face}/{summary['generated']} bands={summary['bands']}")

    do_ab = args.baseline if do_baseline is None else do_baseline
    if do_ab and spec["file"] and not (args.from_dir and spec_override is None):
        _log(f"{pid}: --baseline 生成无 LoRA 对照组（量 LoRA 边际增益）…")
        bspec = {"file": "", "trigger": "", "weight": 1.0}
        bpaths = _gen_images(args, persona, scfg, bspec, face_ref, n,
                             pid + "_baseline", use_trigger=False)
        if bpaths:
            bscores, _bv, bnf = _score_images(bpaths, ref_vec, embed)
            bsum = ff.summarize_fidelity(bscores, no_face=bnf, generated=len(bpaths))
            delta = round(summary["ref_mean"] - bsum["ref_mean"], 4)
            _log(f"{pid}: A/B LoRA={summary['ref_mean']} vs PuLID-only={bsum['ref_mean']} "
                 f"→ ΔLoRA={delta:+.4f}（正=LoRA 更像本人）")
    return {"verdict": verdict, "summary": summary, "spec": spec}


def select_checkpoints(pid, args, cfg, embed) -> str:
    """对 --checkpoints-dir 里每个 .safetensors 逐一评测 → 按保真度排名 → 选最优。

    ⚠ 候选须已放进 176 ComfyUI 的 models/loras（本工具按**文件名**当 lora 名传给 comfy）。
    --write-registry 时把最优写回 config/persona_lora.json（出图链自动生效）。返回最优 verdict。
    """
    d = Path(args.checkpoints_dir)
    cands = sorted(p for p in d.glob("*.safetensors")) if d.is_dir() else []
    if not cands:
        _log(f"{pid}: --checkpoints-dir 无 .safetensors → 跳过")
        return "skip"
    scfg = ((cfg.get("companion") or {}).get("selfie")) or {}
    base = resolve_persona_lora(load_persona(pid), scfg)
    trig = str(args.trigger or "").strip() or base["trigger"]
    weight = float(args.lora_weight) if float(args.lora_weight or 0) > 0 else (base["weight"] or 0.9)
    results = []
    for c in cands:
        _log(f"== [{pid}] 评测 checkpoint {c.name}")
        r = eval_persona(pid, args, cfg, embed, n=args.select_n, do_baseline=False,
                         tag=f"{pid}_{c.stem}",
                         spec_override={"file": c.name, "trigger": trig, "weight": weight})
        if r.get("summary"):
            results.append({"name": c.name, "verdict": r["verdict"], "summary": r["summary"]})
    if not results:
        _log(f"{pid}: 无有效候选结果 → 跳过")
        return "skip"
    ranked = pl.rank_checkpoints(results)
    _log(f"[{pid}] checkpoint 排名（best-first）：")
    for i, r in enumerate(ranked):
        s = r["summary"]
        _log(f"  {i + 1}. {r['name']} verdict={r['verdict']} ref_mean={s['ref_mean']} "
             f"self={s.get('self_consistency')} no_face={s['no_face_ratio']}")
    best = ranked[0]
    _log(f"[{pid}] ★ 最优: {best['name']}（ref_mean={best['summary']['ref_mean']} "
         f"verdict={best['verdict']}）")
    if args.write_registry:
        spec = {"file": best["name"], "trigger": trig, "weight": weight}
        pl.write_lora_registry_entry(args.registry_path, pid, spec)
        _log(f"[{pid}] ✍ 已写回注册表 {args.registry_path}: {spec}（出图链自动生效，无需改 YAML）")
    return best["verdict"]


def main() -> int:
    ap = argparse.ArgumentParser(description="角色 LoRA/锁脸 身份保真度评测 + checkpoint 选优")
    ap.add_argument("--persona", default="")
    ap.add_argument("--all-personas", action="store_true")
    ap.add_argument("--n", type=int, default=12, help="评测张数（默认 12）")
    ap.add_argument("--url", default="http://192.168.0.176:8188")
    ap.add_argument("--face-ref", default="", help="基准脸（默认 assets/persona_media/<pid>/face_ref.png）")
    ap.add_argument("--from-dir", default="", help="复评已有图目录（零 GPU，不再生成）")
    ap.add_argument("--baseline", action="store_true", help="额外跑无 LoRA 对照，量 LoRA 边际增益")
    ap.add_argument("--pulid-start-at", type=float, default=0.15)
    ap.add_argument("--face-weight", type=float, default=0.85)
    ap.add_argument("--seed-base", type=int, default=0)
    ap.add_argument("--model", default="buffalo_l", help="insightface 模型名")
    # checkpoint 选优 + 部署写回
    ap.add_argument("--checkpoints-dir", default="",
                    help="对该目录下每个 .safetensors 逐一评测选最优（须已在 ComfyUI models/loras）")
    ap.add_argument("--select-n", type=int, default=6, help="选优时每候选评测张数（省 GPU，默认 6）")
    ap.add_argument("--lora-file", default="", help="覆写待评 LoRA 文件名（普通评测/部署指定图）")
    ap.add_argument("--lora-weight", type=float, default=0.0, help="覆写 LoRA 权重（0=用配置/默认）")
    ap.add_argument("--trigger", default="", help="覆写触发词（评测未部署的新 LoRA 时给训练用触发词）")
    ap.add_argument("--write-registry", action="store_true",
                    help="把（选优/指定）LoRA 写回 config/persona_lora.json，出图链自动生效")
    ap.add_argument("--registry-path", default="config/persona_lora.json")
    args = ap.parse_args()

    embed = ff.load_face_embedder(model_name=args.model)
    if embed is None:
        _log("insightface 不可用（未安装或模型缺失）→ 跳过评测。"
             "启用：pip install insightface onnxruntime")
        return 0

    cfg = load_merged_config()
    if args.all_personas:
        profiles = (_load_yaml(ROOT / "config" / "profiles_runtime.yaml").get("profiles")) or {}
        pids = list(profiles)
    else:
        if not args.persona.strip():
            _log("需 --persona <id> 或 --all-personas")
            return 2
        pids = [args.persona.strip()]

    worst = "ok"
    order = {"skip": -1, "ok": 0, "warn": 1, "fail": 2}

    def _bump(v: str) -> None:
        nonlocal worst
        if order.get(v, 0) > order.get(worst, 0):
            worst = v

    for pid in pids:
        try:
            if args.checkpoints_dir.strip():
                _bump(select_checkpoints(pid, args, cfg, embed))
            else:
                res = eval_persona(pid, args, cfg, embed)
                _bump(res["verdict"])
                # 部署：普通评测 + --write-registry + 达标 → 把指定 LoRA 写回注册表。
                if (args.write_registry and res["verdict"] in ("ok", "warn")
                        and res.get("spec", {}).get("file")):
                    pl.write_lora_registry_entry(args.registry_path, pid, res["spec"])
                    _log(f"[{pid}] ✍ 已写回注册表 {args.registry_path}: {res['spec']}")
        except Exception as e:  # noqa: BLE001
            _log(f"!! {pid} 处理异常: {e}")
            _bump("fail")
    _log(f"完成，最差 verdict={worst}（ok=达标；warn=偏软建议加样本/调参/重训；fail=没达标/大量无脸）")
    return 1 if worst == "fail" else 0


if __name__ == "__main__":
    sys.exit(main())
