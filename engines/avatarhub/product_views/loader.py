#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""avatarhub 产品视图加载器（配置驱动菜单裁剪脚手架）。

读环境变量 AVATARHUB_PRODUCT_ID → 加载同名 yaml → 返回规范化 dict。
缺省 / 未知 product → mode=full（不过滤侧栏）。

用法：
  from product_views.loader import load_product_view
  view = load_product_view()          # 读 env
  view = load_product_view("huansheng")

  python product_views/loader.py --selftest
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

_DIR = Path(__file__).resolve().parent
_ENV_KEY = "AVATARHUB_PRODUCT_ID"

# 与 static/hub.js `tabs[].id` 对齐（2026-07 侦察）
KNOWN_TABS: tuple[str, ...] = (
    "profiles",
    "clone",
    "voice",
    "sing",
    "batch",
    "dashboard",
    "stream",
    "interp",
    "history",
    "selfcheck",
    "logs",
    "settings",
)

KNOWN_PRODUCTS: tuple[str, ...] = (
    "huansheng",
    "huanying",
    "huanyan",
    "tongchuan",
)

_FULL_VIEW: dict[str, Any] = {
    "product_id": None,
    "mode": "full",
    "brand": {"zh": "无界 AvatarHub", "en": "BOUNDLESS AvatarHub"},
    "allowed_tabs": list(KNOWN_TABS),
    "hide_tabs": [],
    "default_tab": "profiles",
    "feature_flags": {},
    "capability_claims": [],
    "routes_note": "",
}


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        import yaml  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "需要 PyYAML：pip install PyYAML（avatarhub 子环境已普遍携带）"
        ) from e
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError(f"视图文件顶层须为 mapping: {path}")
    return data


def _as_str_list(val: Any) -> list[str]:
    if val is None:
        return []
    if not isinstance(val, (list, tuple)):
        raise ValueError(f"期望字符串列表，得到 {type(val).__name__}")
    out: list[str] = []
    for x in val:
        s = str(x).strip()
        if s:
            out.append(s)
    return out


def _normalize(raw: dict[str, Any], product_id: str) -> dict[str, Any]:
    brand = raw.get("brand") or {}
    if isinstance(brand, str):
        brand = {"zh": brand, "en": brand}
    elif not isinstance(brand, dict):
        brand = {}

    allowed = _as_str_list(raw.get("allowed_tabs"))
    hide = _as_str_list(raw.get("hide_tabs"))

    # 允许只写 hide 或只写 allowed；二者都写时以 allowed 为准再并 hide
    if allowed:
        tab_set = [t for t in allowed if t in KNOWN_TABS]
        # 保留 yaml 里未知键（逻辑能力组），供接线方识别
        extras = [t for t in allowed if t not in KNOWN_TABS]
        visible = tab_set
        for h in hide:
            if h in visible:
                visible = [t for t in visible if t != h]
        hide_effective = [t for t in KNOWN_TABS if t not in visible]
        allowed_effective = visible + extras
    elif hide:
        hide_known = [t for t in hide if t in KNOWN_TABS]
        allowed_effective = [t for t in KNOWN_TABS if t not in hide_known]
        hide_effective = hide_known
    else:
        allowed_effective = list(KNOWN_TABS)
        hide_effective = []

    default_tab = str(raw.get("default_tab") or "profiles").strip()
    if default_tab not in allowed_effective and allowed_effective:
        # 默认页被裁掉时回落到可见列表首项（优先已知 tab）
        known_vis = [t for t in allowed_effective if t in KNOWN_TABS]
        default_tab = known_vis[0] if known_vis else allowed_effective[0]

    flags = raw.get("feature_flags") or {}
    if not isinstance(flags, dict):
        flags = {}
    # 规范化为 bool
    feature_flags = {str(k): bool(v) for k, v in flags.items()}

    claims = _as_str_list(raw.get("capability_claims"))

    return {
        "product_id": product_id,
        "mode": "filtered",
        "brand": {
            "zh": str(brand.get("zh") or product_id),
            "en": str(brand.get("en") or product_id),
        },
        "allowed_tabs": allowed_effective,
        "hide_tabs": hide_effective,
        "default_tab": default_tab,
        "feature_flags": feature_flags,
        "capability_claims": claims,
        "routes_note": str(raw.get("routes_note") or "").strip(),
        "product_yaml": str(raw.get("product_yaml") or f"products/{product_id}/product.yaml"),
    }


def resolve_product_id(explicit: str | None = None) -> str | None:
    """返回规范化 product_id；空/未知 → None（表示 full）。"""
    pid = (explicit if explicit is not None else os.environ.get(_ENV_KEY, "")).strip().lower()
    if not pid:
        return None
    if pid not in KNOWN_PRODUCTS:
        return None
    return pid


def load_product_view(product_id: str | None = None, *, views_dir: Path | None = None) -> dict[str, Any]:
    """加载并规范化产品视图。

    - product_id 为 None 时读 AVATARHUB_PRODUCT_ID
    - 缺省 / 未知 → 返回 full（不过滤）
    """
    pid = resolve_product_id(product_id)
    if pid is None:
        out = dict(_FULL_VIEW)
        # 若 env 写了未知值，保留痕迹便于运维排查
        raw_env = (product_id if product_id is not None else os.environ.get(_ENV_KEY, "")).strip()
        if raw_env and raw_env.lower() not in KNOWN_PRODUCTS:
            out["unresolved_product_id"] = raw_env
        return out

    base = views_dir or _DIR
    path = base / f"{pid}.yaml"
    if not path.is_file():
        out = dict(_FULL_VIEW)
        out["missing_view_file"] = str(path)
        return out

    raw = _load_yaml(path)
    return _normalize(raw, pid)


def selftest() -> int:
    """校验四份 yaml 可解析，且规范化结果自洽。"""
    errors: list[str] = []

    # 1) 缺省 → full
    os.environ.pop(_ENV_KEY, None)
    full = load_product_view()
    if full.get("mode") != "full" or full.get("hide_tabs"):
        errors.append(f"缺省应 full: {full}")

    # 2) 未知 → full
    bad = load_product_view("not-a-product")
    if bad.get("mode") != "full":
        errors.append(f"未知 product 应 full: {bad}")

    # 3) 四产品
    for pid in KNOWN_PRODUCTS:
        try:
            v = load_product_view(pid)
        except Exception as e:
            errors.append(f"{pid}: 加载失败 {e}")
            continue
        if v.get("mode") != "filtered":
            errors.append(f"{pid}: 期望 filtered，得到 {v.get('mode')}")
        if v.get("product_id") != pid:
            errors.append(f"{pid}: product_id 不匹配")
        hide = set(v.get("hide_tabs") or [])
        allowed_known = [t for t in (v.get("allowed_tabs") or []) if t in KNOWN_TABS]
        for t in allowed_known:
            if t in hide:
                errors.append(f"{pid}: {t} 同时在 allowed 与 hide")
        dt = v.get("default_tab")
        if dt not in (v.get("allowed_tabs") or []):
            errors.append(f"{pid}: default_tab={dt!r} 不在 allowed_tabs")
        # hide ∪ allowed(known) 应覆盖全部已知 tab
        if set(allowed_known) | hide != set(KNOWN_TABS):
            errors.append(
                f"{pid}: allowed∪hide 未覆盖 KNOWN_TABS "
                f"(miss={set(KNOWN_TABS) - set(allowed_known) - hide})"
            )

    # 4) env 路径
    os.environ[_ENV_KEY] = "tongchuan"
    env_v = load_product_view()
    if env_v.get("product_id") != "tongchuan":
        errors.append(f"env 注入失败: {env_v}")
    os.environ.pop(_ENV_KEY, None)

    if errors:
        print("SELFTEST FAIL")
        for e in errors:
            print(" -", e)
        return 1

    print("SELFTEST OK")
    for pid in KNOWN_PRODUCTS:
        v = load_product_view(pid)
        print(
            f"  {pid}: default={v['default_tab']} "
            f"hide={v['hide_tabs']} brand={v['brand']['zh']}"
        )
    print("  (unset) →", json.dumps({"mode": load_product_view()["mode"]}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--selftest" in argv:
        return selftest()
    pid = None
    if argv and not argv[0].startswith("-"):
        pid = argv[0]
    view = load_product_view(pid)
    print(json.dumps(view, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
