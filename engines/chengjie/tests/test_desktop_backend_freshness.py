"""backend-dist 源码指纹门禁 —— 防「改了 src 直接 dist 打出旧后端」。

事故：ChatX 1.0.16 在 ``_cancelMedia`` 双定义修复落盘前 52 分钟打包，198 自动更新
仍带 bug。根因＝predist 不重打 PyInstaller、也不验 sidecar 新鲜度。
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

_ENGINE = Path(__file__).resolve().parents[1]
_BUILD = _ENGINE / "desktop" / "build"
_FP_SCRIPT = _BUILD / "backend_source_fingerprint.py"
_CHECK_SCRIPT = _BUILD / "check_backend_freshness.py"
_PKG = _ENGINE / "desktop" / "package.json"
_INBOX = _ENGINE / "src" / "web" / "templates" / "unified_inbox.html"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def fp_mod():
    if not _FP_SCRIPT.is_file():
        pytest.skip(f"missing {_FP_SCRIPT}")
    return _load("_bd_src_fp", _FP_SCRIPT)


def test_fingerprint_stable_and_content_sensitive(fp_mod, tmp_path):
    tree = tmp_path / "src"
    tree.mkdir()
    f = tree / "a.py"
    f.write_text("x=1\n", encoding="utf-8")
    d1 = fp_mod.fingerprint_tree("src", tree)["digest"]
    d2 = fp_mod.fingerprint_tree("src", tree)["digest"]
    assert d1 == d2
    f.write_text("x=2\n", encoding="utf-8")
    d3 = fp_mod.fingerprint_tree("src", tree)["digest"]
    assert d3 != d1


def _fake_backend_dist(tmp_path: Path) -> Path:
    """verify_stamp 先认当前平台的 sidecar（Windows ``backend.exe``，其余 ``backend``），
    缺了它会在盖章检查之前报 missing dist。假 dist 必须先摆上那份产物。"""
    dist = tmp_path / "backend-dist"
    dist.mkdir()
    name = "backend.exe" if os.name == "nt" else "backend"
    (dist / name).write_bytes(b"MZ")
    return dist


def test_verify_rejects_missing_stamp(fp_mod, tmp_path):
    dist = _fake_backend_dist(tmp_path)
    ok, reason, _, _ = fp_mod.verify_stamp(_ENGINE, dist)
    assert ok is False
    assert "source-fingerprint" in reason or "unstamped" in reason


def test_verify_rejects_stale_stamp(fp_mod, tmp_path):
    dist = _fake_backend_dist(tmp_path)
    fp_mod.write_stamp(dist, {
        "version": 1,
        "algorithm": "sha256",
        "aggregate": "0" * 64,
        "built_at": "2026-01-01T00:00:00Z",
        "file_count": 0,
        "roots": [],
    })
    ok, reason, cur, stamped = fp_mod.verify_stamp(_ENGINE, dist)
    assert ok is False
    assert "STALE" in reason
    assert stamped and stamped["aggregate"] == "0" * 64
    assert cur and len(cur["aggregate"]) == 64


def test_predist_scripts_call_freshness_check():
    pkg = json.loads(_PKG.read_text(encoding="utf-8"))
    for key in ("predist", "predist:win"):
        script = pkg["scripts"][key]
        assert "check_backend_freshness.py" in script, (
            f"{key} 未挂 freshness 门禁——改完 src 直接 dist 会再打出旧 sidecar"
        )
    assert "check:backend-fresh" in pkg["scripts"]
    assert "dist:win:fresh" in pkg["scripts"]
    assert _CHECK_SCRIPT.is_file()


def test_build_backend_writes_stamp_after_success():
    text = (_BUILD / "build_backend.py").read_text(encoding="utf-8")
    assert "write_stamp" in text
    assert "compute_fingerprint" in text


def test_cancel_media_single_definition_in_source():
    """回归钉：unified_inbox 只允许一个 _cancelMedia（双定义＝复制回复带图事故）。"""
    if not _INBOX.is_file():
        pytest.skip("unified_inbox.html missing")
    n = _INBOX.read_text(encoding="utf-8").count("function _cancelMedia")
    assert n == 1, (
        f"unified_inbox.html 有 {n} 个 function _cancelMedia；"
        f"必须恰好 1 个（后者覆盖前者会只藏预览不清 _pendingMedia）"
    )
