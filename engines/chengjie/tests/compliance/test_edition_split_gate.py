# -*- coding: utf-8 -*-
"""公开包 / 内部包拆分门禁（CI）：公开包 manifest / 种子树不得含内部清单项。

只跑暂存脚本与门禁的纯 Python 逻辑（合成的假数据根，tmp 目录），不打包、不发布。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ENGINE = Path(__file__).resolve().parents[2]
BUILD = ENGINE / "desktop" / "build"
sys.path.insert(0, str(BUILD))

import edition_gate  # noqa: E402
import stage_edition_assets  # noqa: E402

POLICY = edition_gate.load_policy()
PUB = POLICY["editions"]["public"]
IO = POLICY["internal_only"]


# ── ① 清单自洽 ──────────────────────────────────────────────────────────

def test_policy_is_consistent():
    assert edition_gate.check_policy(POLICY) == []


def test_public_whitelist_is_explicit_and_disjoint():
    for key in ("personas", "voices", "switches"):
        assert isinstance(PUB[key], list) and PUB[key], key
        assert not set(PUB[key]) & set(IO[key]), key
    # 博彩 / 陪伴类开关一定在内部清单
    for k in ("companion", "player_gateway", "avatar_voice", "voice_clone_lan", "licensing", "ai"):
        assert k in IO["switches"]


def test_repo_personas_classification():
    """仓库人设：公开白名单里的不得带内部类标签；陪伴 / 成人向人设必须在内部清单。"""
    data = yaml.safe_load((ENGINE / "config" / "profiles_runtime.yaml").read_text(encoding="utf-8"))
    profs = data["profiles"]
    bad_tags = {t.lower() for t in IO["persona_tags"]}
    for pid in PUB["personas"]:
        if pid in profs:
            tags = {str(t).lower() for t in profs[pid].get("tags") or []}
            assert not tags & bad_tags, (pid, tags & bad_tags)
    for pid, prof in profs.items():
        tags = {str(t).lower() for t in prof.get("tags") or []}
        if tags & bad_tags:
            assert pid in IO["personas"], f"{pid} 带内部类标签却不在 internal_only.personas"
    assert "claire_brennan" in IO["personas"]   # persona_packs 成人向陪伴


def test_policy_violation_detected():
    broken = json.loads(json.dumps(POLICY))
    broken["editions"]["public"]["personas"].append("chen_mo")
    broken["editions"]["public"]["switches"].append("companion")
    broken["editions"]["public"]["voices"] = "*"
    probs = edition_gate.check_policy(broken)
    assert any("personas" in p and "chen_mo" in p for p in probs)
    assert any("switches" in p and "companion" in p for p in probs)
    assert any("voices" in p for p in probs)


# ── ② 暂存 + manifest / 种子树门禁（合成数据根）────────────────────────

def _fake_source(root: Path) -> Path:
    cfg = root / "config"
    (cfg / "voice_refs").mkdir(parents=True)
    (cfg / "prerender_lines").mkdir()
    (cfg / "persona_albums" / "chen_mo").mkdir(parents=True)
    profiles = {}
    for pid in PUB["personas"][:2] + IO["personas"][:3]:
        tags = ["陪伴"] if pid in IO["personas"] else ["职场"]
        profiles[pid] = {"id": pid, "name": pid, "tags": tags,
                         "voice_profile": {"ref_audio": f"config/voice_refs/{pid}.wav"}}
        (cfg / "voice_refs" / f"{pid}.wav").write_bytes(b"RIFF")
        (cfg / "voice_refs" / f"{pid}.txt").write_text("hello", encoding="utf-8")
        (cfg / "prerender_lines" / f"{pid}.txt").write_text("hi", encoding="utf-8")
        vd = root / "assets" / "voices" / pid
        vd.mkdir(parents=True)
        (vd / "a.ogg").write_bytes(b"OggS")
    (cfg / "prerender_lines" / "_common.txt").write_text("hi", encoding="utf-8")
    (cfg / "persona_albums" / "chen_mo" / "1.jpg").write_bytes(b"\xff\xd8")
    (cfg / "persona_media.db").write_bytes(b"")
    (cfg / "persona_bio.db").write_bytes(b"")
    (cfg / "knowledge_base.db").write_bytes(b"")
    (cfg / "profiles_runtime.yaml").write_text(
        yaml.safe_dump({"profiles": profiles}, allow_unicode=True), encoding="utf-8")
    return root


def _overlay(tmp: Path) -> Path:
    data = {k: {"enabled": True} for k in ["brand", "inbox", "workspace", "memory", "companion",
                                            "player_gateway", "avatar_voice", "licensing", "ai",
                                            "telegram", "something_new"]}
    p = tmp / "overlay.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return p


def test_stage_public_carries_only_whitelist(tmp_path):
    src = _fake_source(tmp_path / "data")
    out = tmp_path / "seed"
    rc = stage_edition_assets.stage_public(src, out, POLICY, _overlay(tmp_path))
    assert rc == 0
    mf = json.loads((out / "seed-manifest.json").read_text(encoding="utf-8"))
    assert mf["edition"] == "public"
    assert set(mf["personas"]) == set(PUB["personas"][:2])
    assert not set(mf["personas"]) & set(IO["personas"])
    assert not set(mf["voices"]) & set(IO["voices"])
    assert set(mf["switches"]) == {"brand", "inbox", "workspace", "memory"}
    for sp in IO["seed_paths"]:
        assert not (out / sp).exists(), sp
    assert not any(f.startswith("config/persona_albums") for f in mf["files"])
    names = {p.name for p in (out / "config" / "voice_refs").iterdir()}
    assert not any(n.split(".")[0] in IO["voices"] for n in names)
    assert edition_gate.check_manifest(mf, POLICY) == []
    assert edition_gate.scan_seed_tree(out, POLICY) == []
    assert edition_gate.run("public", out) == []


def test_gate_catches_tampered_manifest_and_tree(tmp_path):
    src = _fake_source(tmp_path / "data")
    out = tmp_path / "seed"
    assert stage_edition_assets.stage_public(src, out, POLICY, _overlay(tmp_path)) == 0
    mf_path = out / "seed-manifest.json"
    mf = json.loads(mf_path.read_text(encoding="utf-8"))
    mf["personas"].append("chen_mo")
    mf["switches"].append("companion")
    mf_path.write_text(json.dumps(mf), encoding="utf-8")
    probs = edition_gate.run("public", out)
    assert any("chen_mo" in p for p in probs) and any("companion" in p for p in probs)
    # 种子树被手工塞进内部声音 / 相册 → 不信 manifest 也抓得到
    (out / "config" / "voice_refs" / "lin_jiaxin.wav").write_bytes(b"RIFF")
    (out / "config" / "persona_albums").mkdir(parents=True)
    tree = edition_gate.scan_seed_tree(out, POLICY)
    assert any("lin_jiaxin" in p for p in tree)
    assert any("persona_albums" in p for p in tree)


def test_internal_tagged_whitelist_persona_is_refused(tmp_path):
    src = _fake_source(tmp_path / "data")
    prof_path = src / "config" / "profiles_runtime.yaml"
    data = yaml.safe_load(prof_path.read_text(encoding="utf-8"))
    data["profiles"][PUB["personas"][0]]["tags"] = ["陪伴"]
    prof_path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    out = tmp_path / "seed"
    assert stage_edition_assets.stage_public(src, out, POLICY, _overlay(tmp_path)) == 1
    assert not out.exists()      # 半成品种子已删


def test_internal_edition_not_restricted():
    assert edition_gate.check_manifest({"edition": "internal", "personas": ["chen_mo"]}, POLICY,
                                       edition="internal") == []


def test_cli_policy_only_exit_code():
    r = subprocess.run([sys.executable, str(BUILD / "edition_gate.py"), "--policy-only"],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr


# ── ③ 打包脚本接线 ──────────────────────────────────────────────────────

def test_package_json_has_public_and_internal_targets():
    pkg = json.loads((ENGINE / "desktop" / "package.json").read_text(encoding="utf-8"))
    sc = pkg["scripts"]
    for t in ("dist:win:public", "dist:win:internal", "predist:win:public", "predist:win:internal",
              "stage:public", "gate:edition"):
        assert t in sc, t
    assert "stage_edition_assets.py --edition public" in sc["predist:win:public"]
    assert "edition_gate.py --edition public" in sc["predist:win:public"]
    assert "stage_internal_assets" not in sc["predist:win:public"]
    assert "internal" not in sc["dist:win:public"].split("publish.url=")[-1]
    assert "dist-public" in sc["dist:win:public"]
    assert "stage_edition_assets.py --edition internal" in sc["predist:win:internal"]
    assert "latest-internal" in sc["dist:win:internal"]
    # 裸 dist 发默认公开通道 → 缺省必须是公开包（1.0.109 事故形态的根治）
    assert "--edition public" in sc["predist"] and "stage_internal_assets" not in sc["predist"]


def test_after_pack_enforces_edition_gate():
    js = (BUILD / "after-pack.js").read_text(encoding="utf-8")
    assert "editions.json" in js and "editionLeaks" in js
    assert 'mf.edition === "public"' in js
    assert "explicitInternal" in js
    assert 'flavor === "internal"' in js

# ── ⑤ 内部专属行业模板（博彩运营商 gambling_operator，config/presets/internal）────────────

PRESETS_INTERNAL = "config/presets/internal"


def test_presets_internal_is_internal_only_and_omitted_from_public():
    assert PRESETS_INTERNAL in IO["seed_paths"]
    assert PRESETS_INTERNAL in PUB["omitted"]
    assert edition_gate.check_policy(POLICY) == []


def test_policy_rejects_presets_internal_not_omitted():
    import copy
    pol = copy.deepcopy(POLICY)
    pol["editions"]["public"]["omitted"] = [p for p in pol["editions"]["public"]["omitted"]
                                           if p != PRESETS_INTERNAL]
    assert any(PRESETS_INTERNAL in p for p in edition_gate.check_policy(pol))


def test_public_seed_tree_with_presets_internal_is_red(tmp_path):
    seed = tmp_path / "seed-data"
    (seed / "config" / "presets" / "internal" / "gambling_operator").mkdir(parents=True)
    (seed / "config" / "presets" / "internal" / "gambling_operator" / "pack.yaml").write_text("id: x\n", encoding="utf-8")
    bad = edition_gate.scan_seed_tree(seed, POLICY, edition="public")
    assert any(PRESETS_INTERNAL in b for b in bad)
    assert edition_gate.scan_seed_tree(seed, POLICY, edition="internal") == []


def test_public_manifest_listing_presets_internal_is_red():
    mf = {"edition": "public", "personas": [], "voices": [], "switches": [],
          "files": ["config/presets/internal/gambling_operator/persona.yaml"]}
    assert any(PRESETS_INTERNAL in b for b in edition_gate.check_manifest(mf, POLICY, edition="public"))


@pytest.mark.parametrize("rel", ["config/presets/internal/gambling_operator/pack.yaml",
                                 "_internal/config/presets/internal/gambling_operator/pack.yaml"])
def test_public_backend_tree_with_presets_internal_is_red(tmp_path, rel):
    be = tmp_path / "backend-dist"
    f = be / rel
    f.parent.mkdir(parents=True)
    f.write_text("id: x\n", encoding="utf-8")
    assert any(PRESETS_INTERNAL in b for b in edition_gate.scan_backend_tree(be, POLICY, edition="public"))
    assert edition_gate.scan_backend_tree(be, POLICY, edition="internal") == []
    assert edition_gate.run("public", None, backend=be)  # CLI 路径同样拦


def test_public_backend_tree_with_public_presets_only_is_green(tmp_path):
    be = tmp_path / "backend-dist"
    (be / "_internal" / "config" / "presets" / "packs" / "agency").mkdir(parents=True)
    assert edition_gate.scan_backend_tree(be, POLICY, edition="public") == []


@pytest.mark.parametrize("dest,red", [
    ("config/presets", True),            # 整目录打包会连带 internal/
    ("config/presets/internal", True),
    ("config/presets/internal/gambling_operator", True),
    ("config/presets/packs", False),
    ("config/profiles", False),
])
def test_backend_datas_gate(dest, red):
    bad = edition_gate.check_backend_datas([(ENGINE / dest, dest)], POLICY, repo=ENGINE)
    assert bool(bad) is red


def test_build_backend_datas_do_not_ship_presets_internal():
    """当前 build_backend.DATAS 不得把内部专属目录（含 config/presets/internal）打进后端代码包。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("_bb_edition", BUILD / "build_backend.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    assert edition_gate.check_backend_datas(m.DATAS, POLICY, repo=m.REPO) == []


def test_after_pack_checks_backend_for_internal_dirs():
    js = (BUILD / "after-pack.js").read_text(encoding="utf-8")
    assert '"_internal"' in js and "公开包后端含内部专属目录" in js


def test_public_stage_never_copies_presets_internal(tmp_path):
    """公开包暂存是白名单拷贝：数据根里即使有 config/presets/internal，产物种子也不带。"""
    src = tmp_path / "src"
    (src / "config" / "presets" / "internal" / "gambling_operator").mkdir(parents=True)
    (src / "config" / "presets" / "internal" / "gambling_operator" / "pack.yaml").write_text("id: x\n", encoding="utf-8")
    (src / "config" / "profiles_runtime.yaml").write_text("profiles: {}\n", encoding="utf-8")
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text("brand: {}\n", encoding="utf-8")
    out = tmp_path / "out"
    stage_edition_assets.stage_public(src, out, POLICY, overlay)
    assert not (out / "config" / "presets" / "internal").exists()
