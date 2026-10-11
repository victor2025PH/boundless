"""智语 2026-10-08：博彩内部包与智安公开/内部包门禁的对接校验。

内部包放在 ``config/presets/internal/``。智安把它登记进 ``desktop/build/editions.json``
的 ``internal_only.seed_paths`` 之前，本文件跳过；登记之后自动生效，校验：
- 门禁政策自洽（check_policy 无错，公开包 omitted 已声明该路径）；
- 公开包 manifest 只要带上内部包文件就红；
- 公开包种子树只要含内部包目录就红；只带公开模板包时不因该路径而红。
"""

import importlib.util
import json
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_POLICY = _ROOT / "desktop" / "build" / "editions.json"
_GATE = _ROOT / "desktop" / "build" / "edition_gate.py"
_INTERNAL = "config/presets/internal"


def _norm(p):
    return str(p or "").replace("\\", "/").strip("/")


def _policy():
    if not _POLICY.exists() or not _GATE.exists():
        pytest.skip("editions.json / edition_gate.py 尚未在本分支")
    return json.loads(_POLICY.read_text(encoding="utf-8"))


def _registered(policy):
    for sp in (policy.get("internal_only") or {}).get("seed_paths") or []:
        n = _norm(sp)
        if n == _INTERNAL or n.endswith("/" + _INTERNAL):
            return n
    return None


def _gate():
    spec = importlib.util.spec_from_file_location("zhiyu_edition_gate", _GATE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _need_registered():
    policy = _policy()
    sp = _registered(policy)
    if not sp:
        pytest.skip("等智安把 config/presets/internal 加进 editions.json internal_only.seed_paths")
    return policy, sp


def test_internal_pack_dir_exists_and_is_only_gambling_home():
    assert (_ROOT / _INTERNAL / "gambling_operator" / "pack.yaml").exists()
    for d in (_ROOT / "config" / "presets" / "packs").iterdir():
        if d.is_dir():
            assert "gambl" not in d.name and "casino" not in d.name, d.name


def test_policy_consistent_with_internal_pack():
    policy, sp = _need_registered()
    bad = _gate().check_policy(policy)
    assert bad == [], bad
    omitted = {_norm(p) for p in ((policy.get("editions") or {}).get("public") or {}).get("omitted") or []}
    assert sp in omitted


def test_public_manifest_with_internal_pack_is_rejected():
    policy, sp = _need_registered()
    gate = _gate()
    base = {"edition": "public", "personas": [], "voices": [], "switches": []}
    leak = dict(base, files=[sp + "/gambling_operator/pack.yaml"])
    assert any("internal" in m or sp in m for m in gate.check_manifest(leak, policy)), "门禁没拦住内部包文件"
    clean = dict(base, files=["config/presets/packs/agency/pack.yaml"])
    assert not [m for m in gate.check_manifest(clean, policy) if sp in m]


def test_public_seed_tree_with_internal_pack_is_rejected(tmp_path):
    policy, sp = _need_registered()
    gate = _gate()
    leak = tmp_path / "leak"
    (leak / sp / "gambling_operator").mkdir(parents=True)
    (leak / sp / "gambling_operator" / "pack.yaml").write_text("edition: internal_only\n", encoding="utf-8")
    assert any(sp in m for m in gate.scan_seed_tree(leak, policy, edition="public"))
    assert gate.scan_seed_tree(leak, policy, edition="internal") == []
    clean = tmp_path / "clean"
    (clean / "config" / "presets" / "packs" / "agency").mkdir(parents=True)
    (clean / "config" / "presets" / "packs" / "agency" / "pack.yaml").write_text("edition: public\n", encoding="utf-8")
    assert not [m for m in gate.scan_seed_tree(clean, policy, edition="public") if sp in m]
