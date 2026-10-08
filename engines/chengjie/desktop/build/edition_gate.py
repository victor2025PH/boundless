#!/usr/bin/env python3
"""公开包 / 内部包拆分门禁（智安 2026-10-08）。

三层检查，任一不过 → 退出码 1（打包中止）：

1. **清单自洽**（``check_policy``）：``editions.json`` 里公开白名单与内部清单不得相交
   （人设 / 声音 / 开关 / 种子路径），公开包必须显式列白名单（不许 ``"*"``）。
2. **manifest 门禁**（``check_manifest``）：公开包 ``seed-manifest.json`` 的
   personas / voices / switches 必须是公开白名单的子集，且不得含任何内部清单项；
   edition 字段必须是 public。
3. **种子树门禁**（``scan_seed_tree``）：不信 manifest 自述，直接扫暂存 / 产物里的
   ``seed-data``——profiles_runtime 的人设 id 与 tags、voice_refs / assets/voices 的
   文件名、overlay 种子的顶层开关、内部专属种子路径。

用法（desktop/ 下）::

    python build/edition_gate.py --policy-only
    python build/edition_gate.py --edition public --seed build/seed-data

CI：``engines/chengjie/tests/compliance/test_edition_split_gate.py`` 直接 import 本模块。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

HERE = Path(__file__).resolve().parent
POLICY_PATH = HERE / "editions.json"
OVERLAY_NAME = "config.local.internal.yaml"


def load_policy(path: Optional[Path] = None) -> Dict[str, Any]:
    return json.loads(Path(path or POLICY_PATH).read_text(encoding="utf-8"))


def _set(v: Any) -> set:
    if isinstance(v, (list, tuple, set)):
        return {str(x).strip() for x in v if str(x).strip()}
    return set()


def _norm(p: str) -> str:
    return str(p or "").replace("\\", "/").strip("/")


def check_policy(policy: Dict[str, Any]) -> List[str]:
    bad: List[str] = []
    io = policy.get("internal_only") or {}
    pub = ((policy.get("editions") or {}).get("public") or {})
    if not pub:
        return ["editions.public 缺失"]
    for key in ("personas", "voices", "switches"):
        if not isinstance(pub.get(key), list):
            bad.append(f"公开包 {key} 必须是显式白名单列表（不许 '*' / 缺省）")
            continue
        hit = _set(pub.get(key)) & _set(io.get(key))
        if hit:
            bad.append(f"公开白名单 {key} 与内部清单相交: {sorted(hit)}")
    om = {_norm(p) for p in pub.get("omitted") or []}
    for sp in io.get("seed_paths") or []:
        if _norm(sp) not in om:
            bad.append(f"内部专属种子路径 {sp} 未在公开包 omitted 中声明")
    if (pub.get("publish_channel") or "") == ((policy.get("editions") or {}).get("internal") or {}).get("publish_channel"):
        bad.append("公开包与内部包发布通道相同")
    return bad


def check_manifest(manifest: Dict[str, Any], policy: Dict[str, Any], *,
                   edition: str = "public") -> List[str]:
    """公开包 manifest 不得含内部清单项；必须是公开白名单子集。internal → 恒 []。"""
    if edition != "public":
        return []
    bad: List[str] = []
    io = policy.get("internal_only") or {}
    pub = (policy.get("editions") or {}).get("public") or {}
    if str(manifest.get("edition") or "") != "public":
        bad.append(f"manifest.edition={manifest.get('edition')!r}，公开包必须是 'public'")
    for key in ("personas", "voices", "switches"):
        got = _set(manifest.get(key))
        leak = got & _set(io.get(key))
        if leak:
            bad.append(f"公开包 manifest.{key} 含内部清单项: {sorted(leak)}")
        extra = got - _set(pub.get(key))
        if extra:
            bad.append(f"公开包 manifest.{key} 超出公开白名单: {sorted(extra)}")
    for f in manifest.get("files") or []:
        nf = _norm(f)
        for sp in io.get("seed_paths") or []:
            if nf == _norm(sp) or nf.startswith(_norm(sp) + "/"):
                bad.append(f"公开包 manifest.files 含内部专属种子: {f}")
    tags = {str(t).lower() for t in io.get("persona_tags") or []}
    for pid, ptags in (manifest.get("persona_tags") or {}).items():
        hit = {str(t) for t in (ptags or []) if str(t).lower() in tags}
        if hit:
            bad.append(f"公开包人设 {pid} 带内部类标签: {sorted(hit)}")
    return bad


def _profiles(seed: Path) -> Dict[str, Any]:
    p = seed / "config" / "profiles_runtime.yaml"
    if not p.exists():
        return {}
    import yaml
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    profs = data.get("profiles") if isinstance(data, dict) else None
    if isinstance(profs, dict):
        return {str(k): (v or {}) for k, v in profs.items()}
    if isinstance(profs, list):
        return {str(x.get("id")): x for x in profs if isinstance(x, dict)}
    return {}


def _voice_ids(seed: Path) -> set:
    out: set = set()
    refs = seed / "config" / "voice_refs"
    if refs.is_dir():
        for f in refs.rglob("*"):
            if f.is_file() and not f.name.startswith("."):
                out.add(f.name.split(".")[0])
    voices = seed / "assets" / "voices"
    if voices.is_dir():
        for d in voices.iterdir():
            if d.is_dir() and not d.name.startswith("."):
                out.add(d.name)
            # 顶层文件＝共享成品（通用问候库），不属任何人设
    return out


def _overlay_keys(seed: Path) -> set:
    p = seed / OVERLAY_NAME
    if not p.exists():
        return set()
    import yaml
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {str(k) for k in data.keys()} if isinstance(data, dict) else set()


def scan_seed_tree(seed: Path, policy: Dict[str, Any], *, edition: str = "public") -> List[str]:
    """直接扫种子树（不信 manifest 自述）。internal → 恒 []；seed 不存在 → []（干净包）。"""
    seed = Path(seed)
    if edition != "public" or not seed.exists():
        return []
    io = policy.get("internal_only") or {}
    pub = (policy.get("editions") or {}).get("public") or {}
    bad: List[str] = []
    profs = _profiles(seed)
    tags = {str(t).lower() for t in io.get("persona_tags") or []}
    for pid, prof in profs.items():
        if pid in _set(io.get("personas")):
            bad.append(f"种子树含内部人设 {pid}")
        elif pid not in _set(pub.get("personas")):
            bad.append(f"种子树人设 {pid} 不在公开白名单")
        ptags = prof.get("tags") if isinstance(prof, dict) else None
        hit = {str(t) for t in (ptags or []) if str(t).lower() in tags}
        if hit:
            bad.append(f"种子树人设 {pid} 带内部类标签: {sorted(hit)}")
    for vid in _voice_ids(seed):
        if vid.startswith("_"):
            continue
        if vid in _set(io.get("voices")):
            bad.append(f"种子树含内部声音 {vid}")
        elif vid not in _set(pub.get("voices")):
            bad.append(f"种子树声音 {vid} 不在公开白名单")
    for k in _overlay_keys(seed):
        if k in _set(io.get("switches")):
            bad.append(f"overlay 种子含内部开关 {k}")
        elif k not in _set(pub.get("switches")):
            bad.append(f"overlay 种子开关 {k} 不在公开白名单")
    for sp in io.get("seed_paths") or []:
        if (seed / _norm(sp)).exists():
            bad.append(f"种子树含内部专属种子 {sp}")
    return bad


def run(edition: str, seed: Optional[Path], policy_path: Optional[Path] = None) -> List[str]:
    policy = load_policy(policy_path)
    problems = check_policy(policy)
    if seed is not None and edition == "public":
        mf_path = Path(seed) / "seed-manifest.json"
        if Path(seed).exists():
            if not mf_path.exists():
                problems.append(f"公开包种子缺 seed-manifest.json: {mf_path}")
            else:
                problems += check_manifest(json.loads(mf_path.read_text(encoding="utf-8")),
                                           policy, edition=edition)
        problems += scan_seed_tree(Path(seed), policy, edition=edition)
    return problems


def main(argv: Optional[Iterable[str]] = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--edition", choices=("public", "internal"), default="public")
    ap.add_argument("--seed", default=str(HERE / "seed-data"))
    ap.add_argument("--policy", default=str(POLICY_PATH))
    ap.add_argument("--policy-only", action="store_true")
    a = ap.parse_args(list(argv) if argv is not None else None)
    problems = run(a.edition, None if a.policy_only else Path(a.seed), Path(a.policy))
    if problems:
        print(f"✗ 公开/内部包拆分门禁 {len(problems)} 项未过（edition={a.edition}）：", file=sys.stderr)
        for p in problems:
            print("   - " + p, file=sys.stderr)
        return 1
    print(f"✓ 公开/内部包拆分门禁通过（edition={a.edition}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
