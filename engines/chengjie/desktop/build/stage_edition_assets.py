#!/usr/bin/env python3
"""按 editions.json 白名单暂存公开包 / 内部包种子（智安 2026-10-08）。

    python build/stage_edition_assets.py --edition public     # = npm run stage:public
    python build/stage_edition_assets.py --edition internal   # = npm run stage:edition:internal

public（对外发行）——白名单制，清单外一概不带：
    · config/profiles_runtime.yaml   只保留 editions.public.personas（且人设不得带内部类标签）
    · config/voice_refs/<id>.*       只带 editions.public.voices（.bak* 不带）
    · config/prerender_lines         _common.txt + 白名单人设专属库
    · assets/voices/<id>/            只带白名单人设目录
    · config.local.internal.yaml     仓库 overlay 种子**按开关白名单过滤顶层键**
      （博彩 / 陪伴 / 克隆音色 / 内网端点类开关全在 internal_only，进不来）
    · 相册 / persona_media.db / persona_bio.db / knowledge_base.db 一概不带（manifest.omitted）
    · seed-manifest.json：edition=public + personas / voices / switches / files 清点
  暂存完立即跑 edition_gate（manifest + 种子树双检），不过 → 删掉种子、退出 1。

internal（内部 / 持牌运营商定制）——沿用 stage_internal_assets.py 标准档全量种子，
  再把 edition=internal 与清点补进 manifest（after-pack / 看板据此区分）。

本脚本只暂存 build/seed-data，**不打包、不发布**。
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import edition_gate  # noqa: E402

DEFAULT_SOURCE = Path(r"D:/chengjie-instances/zhiliao/data")
OUT_DEFAULT = HERE / "seed-data"
OVERLAY_SEED_DEFAULT = HERE.parent.parent / "config" / "config.desktop.internal.yaml"


def _files(root: Path) -> List[str]:
    return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


def _filter_overlay(src: Path, dst: Path, allowed: set, forbidden: set) -> List[str]:
    import yaml
    data = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
    kept = {k: v for k, v in data.items() if k in allowed and k not in forbidden}
    header = ("# 公开包 overlay 种子（stage_edition_assets.py 按 editions.json 开关白名单生成；\n"
              "# 内部清单开关一概不带。构建产物，勿手改）\n")
    dst.write_text(header + yaml.safe_dump(kept, allow_unicode=True, sort_keys=False, width=100),
                   encoding="utf-8")
    return list(kept.keys())


def stage_public(source: Path, out: Path, policy: Dict[str, Any], overlay_seed: Path) -> int:
    import yaml
    pub = policy["editions"]["public"]
    io = policy.get("internal_only") or {}
    personas = [str(p) for p in pub.get("personas") or []]
    voices = [str(v) for v in pub.get("voices") or []]
    allowed_sw = set(pub.get("switches") or [])
    forbidden_sw = set(io.get("switches") or [])
    bad_tags = {str(t).lower() for t in io.get("persona_tags") or []}
    if not (source / "config").is_dir():
        print(f"✗ 源数据根不存在或无 config/: {source}", file=sys.stderr)
        return 2
    if not overlay_seed.exists():
        print(f"✗ overlay 种子缺失: {overlay_seed}", file=sys.stderr)
        return 2
    shutil.rmtree(out, ignore_errors=True)
    (out / "config").mkdir(parents=True, exist_ok=True)
    problems: List[str] = []

    switches = _filter_overlay(overlay_seed, out / edition_gate.OVERLAY_NAME, allowed_sw, forbidden_sw)

    picked: Dict[str, Any] = {}
    persona_tags: Dict[str, List[str]] = {}
    prof_src = source / "config" / "profiles_runtime.yaml"
    if prof_src.exists():
        data = yaml.safe_load(prof_src.read_text(encoding="utf-8")) or {}
        profs = data.get("profiles")
        by_id = ({str(k): v for k, v in profs.items()} if isinstance(profs, dict)
                 else {str(p.get("id")): p for p in (profs or []) if isinstance(p, dict)})
        for pid in personas:
            prof = by_id.get(pid)
            if prof is None:
                print(f"  ! 源数据无白名单人设 {pid}（跳过）")
                continue
            tags = [str(t) for t in (prof.get("tags") or [])] if isinstance(prof, dict) else []
            hit = [t for t in tags if t.lower() in bad_tags]
            if hit:
                problems.append(f"白名单人设 {pid} 带内部类标签 {hit}——先改分类再进公开包")
                continue
            picked[pid] = prof
            persona_tags[pid] = tags
        staged = dict(data)
        staged["profiles"] = (picked if isinstance(profs, dict) else list(picked.values()))
        (out / "config" / "profiles_runtime.yaml").write_text(
            yaml.safe_dump(staged, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8")
    else:
        problems.append(f"缺 {prof_src}")

    refs_out = out / "config" / "voice_refs"
    refs_out.mkdir(parents=True, exist_ok=True)
    got_voices: List[str] = []
    for vid in voices:
        hit = False
        for ext in (".wav", ".txt", ".mp3", ".json"):
            src = source / "config" / "voice_refs" / f"{vid}{ext}"
            if src.exists():
                shutil.copyfile(src, refs_out / src.name)
                hit = True
        vdir = source / "assets" / "voices" / vid
        if vdir.is_dir():
            shutil.copytree(vdir, out / "assets" / "voices" / vid)
            hit = True
        if hit:
            got_voices.append(vid)

    lines_out = out / "config" / "prerender_lines"
    lines_out.mkdir(parents=True, exist_ok=True)
    for name in ["_common.txt"] + [f"{pid}.txt" for pid in picked]:
        src = source / "config" / "prerender_lines" / name
        if src.exists():
            shutil.copyfile(src, lines_out / name)
    # after-pack 的 SEED_REQUIRED 要求这几个目录在场；electron-builder 不拷空目录 → 占位
    for d in (refs_out, lines_out, out / "assets" / "voices"):
        d.mkdir(parents=True, exist_ok=True)
        if not any(d.iterdir()):
            (d / ".keep").write_text("", encoding="utf-8")

    manifest = {
        "edition": "public",
        "flavor": "public",
        "profile": "public",
        "source": str(source),
        "personas": sorted(picked),
        "persona_tags": persona_tags,
        "voices": sorted(got_voices),
        "switches": sorted(switches),
        "omitted": list(pub.get("omitted") or []),
        "files": _files(out),
        "counts": {"personas": len(picked), "voices": len(got_voices), "switches": len(switches)},
    }
    (out / "seed-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
    # 与内测种子同一道文本安全闸（公开分发物：不得含绝对盘符路径 / 密钥形态字段）
    try:
        from stage_internal_assets import _scan_text_gate
        for f in (out / edition_gate.OVERLAY_NAME, out / "config" / "profiles_runtime.yaml"):
            if f.exists():
                problems += _scan_text_gate(f)
    except ImportError:
        problems.append("stage_internal_assets._scan_text_gate 不可用（文本安全闸缺席）")
    problems += edition_gate.check_manifest(manifest, policy, edition="public")
    problems += edition_gate.scan_seed_tree(out, policy, edition="public")
    if problems:
        print(f"✗ 公开包暂存门禁 {len(problems)} 项未过（已删除半成品种子）：", file=sys.stderr)
        for p in problems:
            print("   - " + p, file=sys.stderr)
        shutil.rmtree(out, ignore_errors=True)
        return 1
    print(f"✓ 公开包种子已暂存：{out}（人设 {','.join(sorted(picked)) or '-'}；"
          f"声音 {len(got_voices)}；开关 {len(switches)}）")
    return 0


def stage_internal(source: Path, out: Path) -> int:
    rc = subprocess.call([sys.executable, str(HERE / "stage_internal_assets.py"),
                          "--source", str(source), "--out", str(out)])
    if rc != 0:
        return rc
    mf_path = out / "seed-manifest.json"
    mf = json.loads(mf_path.read_text(encoding="utf-8")) if mf_path.exists() else {}
    try:
        mf["personas"] = sorted(edition_gate._profiles(out).keys())
        mf["voices"] = sorted(edition_gate._voice_ids(out))
        mf["switches"] = sorted(edition_gate._overlay_keys(out))
    except Exception as exc:  # noqa: BLE001
        print(f"  ! 内部包清点失败（不影响种子）：{exc}")
    mf["edition"] = "internal"
    mf_path.write_text(json.dumps(mf, ensure_ascii=False, indent=2), encoding="utf-8")
    print("✓ 内部包种子已暂存（edition=internal）")
    return 0


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edition", choices=("public", "internal"), required=True)
    ap.add_argument("--source", default=str(DEFAULT_SOURCE))
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    ap.add_argument("--policy", default=str(edition_gate.POLICY_PATH))
    ap.add_argument("--overlay-seed", default=str(OVERLAY_SEED_DEFAULT))
    a = ap.parse_args(argv)
    policy = edition_gate.load_policy(Path(a.policy))
    bad = edition_gate.check_policy(policy)
    if bad:
        print("✗ editions.json 自洽检查未过：", file=sys.stderr)
        for p in bad:
            print("   - " + p, file=sys.stderr)
        return 1
    if a.edition == "public":
        return stage_public(Path(a.source), Path(a.out), policy, Path(a.overlay_seed))
    return stage_internal(Path(a.source), Path(a.out))


if __name__ == "__main__":
    raise SystemExit(main())
