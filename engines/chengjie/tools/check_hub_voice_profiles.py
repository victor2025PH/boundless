# -*- coding: utf-8 -*-
"""hub 音色档核对（2026-08-02「chengjie_* 档名 404 断档」事故沉淀的防呆工具）。

用途：**任何换档/改 profile_map 操作的前后必跑**。核对 overlay 里
``avatar_voice.hub_fish.persona_allowlist`` 中每个人设经 ``profile_map`` 映射后的
hub 档名是否真实存在于 hub（``GET {base_url}/profiles``）。缺档=上线即 404 →
``voice_consistency=strict`` 拒发 → 全语音静默回落文字（本次事故断档 5 天）。

只读：仅 GET hub 与读 overlay YAML，零写入零合成。

用法：
    python tools/check_hub_voice_profiles.py                 # 默认查 zhiliao overlay
    python tools/check_hub_voice_profiles.py --overlay <路径> [--base-url http://...] [--json]

退出码：0=全部命中；1=有缺档；2=hub 不可达/配置读不到。
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import urllib.request
from typing import Any, Dict, List, Tuple

DEFAULT_OVERLAY = r"D:\chengjie-instances\zhiliao\data\config\config.local.yaml"


def diff_profiles(
    allowlist: List[str],
    profile_map: Dict[str, str],
    hub_names: List[str],
) -> Dict[str, List[Tuple[str, str]]]:
    """纯函数：白名单人设 × 映射档名 与 hub 实际档名集合的比对。

    返回 ``{"ok": [(pid, mapped)], "missing": [(pid, mapped)]}``。
    映射缺失时按本仓 hub_fish 语义回落 ``pid`` 本身作档名。
    """
    names = {str(n).strip() for n in (hub_names or []) if str(n).strip()}
    ok: List[Tuple[str, str]] = []
    missing: List[Tuple[str, str]] = []
    for pid in allowlist or []:
        p = str(pid).strip()
        if not p:
            continue
        mapped = str((profile_map or {}).get(p) or p).strip()
        (ok if mapped in names else missing).append((p, mapped))
    return {"ok": ok, "missing": missing}


def _load_hub_cfg(overlay_path: str) -> Dict[str, Any]:
    import yaml
    with open(overlay_path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    hf = ((data.get("avatar_voice") or {}).get("hub_fish") or {})
    if not isinstance(hf, dict):
        hf = {}
    return hf


def _fetch_hub_names(base_url: str, timeout: float = 10.0) -> List[str]:
    url = base_url.rstrip("/") + "/profiles?fields=name"
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # nosec B310
        data = json.loads(resp.read())
    rows = data if isinstance(data, list) else (data.get("profiles") or [])
    return [str((r or {}).get("name") or "") for r in rows]


def main(argv: List[str] | None = None) -> int:
    try:
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="hub 音色档核对（只读）")
    ap.add_argument("--overlay", default=DEFAULT_OVERLAY)
    ap.add_argument("--base-url", default="",
                    help="缺省读 overlay 的 hub_fish.base_url（首个）")
    ap.add_argument("--json", action="store_true", dest="as_json")
    args = ap.parse_args(argv)

    try:
        hf = _load_hub_cfg(args.overlay)
    except Exception as ex:
        print(f"[FAIL] overlay 读取失败: {type(ex).__name__}: {ex}")
        return 2
    allowlist = [str(x) for x in (hf.get("persona_allowlist") or [])]
    profile_map = hf.get("profile_map") or {}
    base_url = args.base_url or str(
        hf.get("base_url") or "http://192.168.0.176:9000")
    if not allowlist:
        print("[WARN] persona_allowlist 为空（hub_fish 未按名单灰度）——无可核对项")
        return 0
    try:
        names = _fetch_hub_names(base_url)
    except Exception as ex:
        print(f"[FAIL] hub 不可达 {base_url}: {type(ex).__name__}: {ex}")
        return 2

    res = diff_profiles(allowlist, profile_map, names)
    if args.as_json:
        print(json.dumps({
            "base_url": base_url, "hub_profile_count": len(names),
            "ok": res["ok"], "missing": res["missing"],
        }, ensure_ascii=False, indent=2))
    else:
        print(f"hub={base_url}  档总数={len(names)}  核对人设={len(allowlist)}")
        for pid, mapped in res["ok"]:
            print(f"  OK       {pid:<24} -> {mapped}")
        for pid, mapped in res["missing"]:
            print(f"  MISSING  {pid:<24} -> {mapped}   ← hub 无此档，上线即 404 拒发")
        if res["missing"]:
            print("[FAIL] 存在缺档：修 profile_map 或在 hub 补档后重跑本工具")
        else:
            print("[PASS] 全部命中")
    return 1 if res["missing"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
