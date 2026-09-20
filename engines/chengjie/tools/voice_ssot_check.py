# -*- coding: utf-8 -*-
"""音色 SSOT 三方对账：本地参考音 × hub 声纹档 × prerender 指纹。

P0/P1（2026-08-02 音色一致化）确立的不变量：**本地 ``config/voice_refs/<pid>.wav``
是每个人设音色的唯一事实源**，hub 的 ``chengjie_<pid>`` 档与预渲染库存都必须与它
同源。历史上三处各自为政导致过两类实锤事故：
  - 「同人设两种声」：预渲染=本地旧 ref、动态=hub 另一份参考音（林佳欣）；
  - 「借声漂移」：hub 档被映射到别人的档（苏婉→Mizuki）。

本工具逐人设核对（只读，零副作用）：
  ① 本地 ref 存在 + sidecar 逐字稿存在；
  ② profile_map 指向的 hub 档存在、有声音、且 **sha1(hub voice_b64) == sha1(本地 ref)**；
  ③ prerender ``_ref.json`` 指纹与本地 ref 一致（不一致=WARN：运行时会拒命中回落
     现场合成（音色仍正确），夜间任务自动重渲——属自愈中而非事故）。

用法（引擎根）：
    python tools/voice_ssot_check.py [--json]
退出码：0=全绿（WARN 不算红）；1=存在同源性 FAIL（音色分裂，需要人来）。
建议挂进 AvatarPrerenderNightly 渲染后顺带跑（探针 jsonl 之外的第二只眼）。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:
    pass


def _sha1(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def _hub_get(base: str, path: str, timeout: float = 30.0) -> dict:
    req = urllib.request.Request(base + path)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def check(root: Path, *, hub_override: str = "") -> dict:
    """对账一个数据根。返回 {rows, fails, warns}。"""
    from scripts._data_root import load_merged_config, profiles_runtime_path

    import yaml

    cfg = load_merged_config(root)
    av = cfg.get("avatar_voice") or {}
    hf = av.get("hub_fish") if isinstance(av.get("hub_fish"), dict) else {}
    hub = (hub_override or str(hf.get("base_url") or "")).rstrip("/")
    allow = [str(x) for x in (hf.get("persona_allowlist") or [])]
    pmap = hf.get("profile_map") if isinstance(hf.get("profile_map"), dict) else {}

    profiles = {}
    rt = profiles_runtime_path(root)
    if rt.is_file():
        profiles = (yaml.safe_load(rt.read_text(encoding="utf-8")) or {}).get(
            "profiles") or {}

    rows, fails, warns = [], 0, 0
    for pid, p in sorted(profiles.items()):
        vp = (p or {}).get("voice_profile") or {}
        backend = str(vp.get("backend") or "").strip()
        ref = str(vp.get("reference_audio_path") or "").strip()
        if backend not in ("avatar_clone", "minicpm_clone", "fish_speech"):
            continue
        row = {"persona": pid, "ref": ref, "issues": []}

        # ① 本地 ref + sidecar
        ref_p = Path(ref) if ref and Path(ref).is_absolute() else (root / ref)
        local_sha = ""
        if not ref or not ref_p.is_file():
            row["issues"].append("FAIL:本地参考音缺失")
        else:
            local_sha = _sha1(ref_p.read_bytes())
            row["local_sha1"] = local_sha[:12]
            if not ref_p.with_suffix(".txt").is_file():
                row["issues"].append("WARN:缺 sidecar 逐字稿")

        # ② hub 档同源
        if hub and pid in allow:
            profile = str(pmap.get(pid) or pid)
            row["hub_profile"] = profile
            try:
                full = _hub_get(
                    hub, "/profiles/" + urllib.parse.quote(profile)
                    + "?include_face=true")
                vb = full.get("voice_b64") or ""
                if not vb:
                    row["issues"].append("FAIL:hub 档无声音")
                elif local_sha:
                    hub_sha = _sha1(base64.b64decode(vb))
                    row["hub_sha1"] = hub_sha[:12]
                    if hub_sha != local_sha:
                        row["issues"].append("FAIL:hub 档与本地参考音不同源")
            except Exception as e:
                row["issues"].append(f"FAIL:hub 档取档失败 {type(e).__name__}")

        # ③ prerender 指纹（WARN 级：运行时拒命中+夜间自愈）
        reg = root / "assets" / "voices" / pid / "prerendered" / "_ref.json"
        if reg.is_file() and local_sha:
            try:
                reg_sha = str((json.loads(reg.read_text(encoding="utf-8"))
                               or {}).get("ref_sha1") or "")
                if reg_sha and reg_sha != local_sha:
                    row["issues"].append("WARN:prerender 库存陈旧（等夜间重渲）")
            except Exception:
                row["issues"].append("WARN:prerender 指纹不可读")

        fails += sum(1 for i in row["issues"] if i.startswith("FAIL"))
        warns += sum(1 for i in row["issues"] if i.startswith("WARN"))
        rows.append(row)
    return {"root": str(root), "rows": rows, "fails": fails, "warns": warns}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="", help="数据根（缺省=首个活跃实例）")
    ap.add_argument("--hub", default="", help="覆写 hub base_url")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    from scripts._data_root import resolve_data_roots

    root = Path(args.root) if args.root else resolve_data_roots()[0]
    out = check(root, hub_override=args.hub)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print(f"音色 SSOT 对账 @ {out['root']}")
        for r in out["rows"]:
            mark = "✗" if any(i.startswith("FAIL") for i in r["issues"]) else (
                "!" if r["issues"] else "✓")
            extra = ("  " + "; ".join(r["issues"])) if r["issues"] else ""
            print(f" {mark} {r['persona']:<22} ref={r.get('local_sha1', '-'):<12} "
                  f"hub={r.get('hub_sha1', '-'):<12}{extra}")
        print(f"FAIL={out['fails']} WARN={out['warns']}（共 {len(out['rows'])} 人设）")
    return 1 if out["fails"] else 0


if __name__ == "__main__":
    sys.exit(main())
