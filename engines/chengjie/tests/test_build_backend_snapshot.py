# -*- coding: utf-8 -*-
"""发布快照构建门禁（P0 2026-08-29，v1.0.59「半途快照误发」事故后补）。

共享树常年 100+ 脏文件、多线并行——默认打包直读活树，发布时刻撞上别人保存
到一半＝把半成品发给全部客户（当天被迫重发 1.0.60）。``--ref`` 把发布内容钉在
git 提交点：脏文件**必然**不进包。

这里用临时 git 仓钉 ``export_ref_snapshot`` 的四个不变量：
1. 已提交内容全量导出、**未提交的脏文件绝不进快照**（本功能存在的全部理由）；
2. platform 瘦模块优先取 ref 内容；不在 ref 内 → 活树回落且 notes 必须播报
   （静默回落＝把「防搭车」偷偷变回「可搭车」）；
3. 坏 ref 报错不糊弄；
4. 引擎位于仓根布局（CI 单仓）绝不往快照目录外写文件。
默认模式（不带 --ref）行为零变化由既有打包门禁继续守。
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

ENGINE_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = ENGINE_ROOT / "desktop" / "build" / "build_backend.py"


def _mod():
    spec = importlib.util.spec_from_file_location("_bb_snap", _SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)  # type: ignore[union-attr]
    return m


def _git(args, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t",
         "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd), capture_output=True, text=True)


def _make_repo(tmp_path: Path, *, commit_platform: bool = True) -> Path:
    """boundless 同构布局：<root>/engines/chengjie + <root>/platform/credpool。"""
    root = tmp_path / "repo"
    eng = root / "engines" / "chengjie"
    (eng / "desktop" / "build").mkdir(parents=True)
    (eng / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (eng / "desktop" / "build" / "build_backend.py").write_text(
        "# marker\n", encoding="utf-8")
    (root / "platform" / "credpool").mkdir(parents=True)
    (root / "platform" / "credpool" / "client.py").write_text(
        "X = 1\n", encoding="utf-8")
    r = _git(["init", "-q"], root)
    if r.returncode != 0:
        pytest.skip("git 不可用")
    _git(["add", "engines"], root)
    if commit_platform:
        _git(["add", "platform"], root)
    c = _git(["commit", "-q", "-m", "seed"], root)
    assert c.returncode == 0, c.stderr
    return root


def test_dirty_files_never_enter_snapshot(tmp_path, monkeypatch):
    m = _mod()
    root = _make_repo(tmp_path)
    eng = root / "engines" / "chengjie"
    monkeypatch.setattr(m, "_PLATFORM_ROOT", root / "platform")
    monkeypatch.setattr(m, "PLATFORM_PKGS", ("credpool",))
    # 提交后的脏改动 + 全新未跟踪文件——都不许进快照
    (eng / "main.py").write_text("print('DIRTY EDIT')\n", encoding="utf-8")
    (eng / "half_saved.py").write_text("oops\n", encoding="utf-8")
    snap_engine, sha, notes = m.export_ref_snapshot(
        "HEAD", engine_root=eng, snapshot_dir=tmp_path / "snap")
    assert sha and len(sha) >= 7
    assert (snap_engine / "main.py").read_text(encoding="utf-8") \
        == "print('hi')\n"                       # 取的是提交点内容
    assert not (snap_engine / "half_saved.py").exists()
    # platform 在 ref 内 → 按仓库相对路径落位，零回落播报
    assert (snap_engine.parent.parent / "platform" / "credpool"
            / "client.py").is_file()
    assert notes == []


def test_uncommitted_platform_falls_back_with_note(tmp_path, monkeypatch):
    m = _mod()
    root = _make_repo(tmp_path, commit_platform=False)
    eng = root / "engines" / "chengjie"
    monkeypatch.setattr(m, "_PLATFORM_ROOT", root / "platform")
    monkeypatch.setattr(m, "PLATFORM_PKGS", ("credpool",))
    snap_engine, _sha, notes = m.export_ref_snapshot(
        "HEAD", engine_root=eng, snapshot_dir=tmp_path / "snap")
    assert (snap_engine.parent.parent / "platform" / "credpool"
            / "client.py").is_file()             # 活树回落把模块补齐
    assert any("回落" in n for n in notes), notes  # 但必须让发布者看见


def test_bad_ref_raises(tmp_path):
    m = _mod()
    root = _make_repo(tmp_path)
    with pytest.raises(RuntimeError):
        m.export_ref_snapshot(
            "no-such-ref", engine_root=root / "engines" / "chengjie",
            snapshot_dir=tmp_path / "snap")


def test_engine_at_toplevel_never_writes_outside_snapshot(tmp_path):
    """CI 单仓布局：引擎根＝git 仓根。platform 布局不适用 → 跳过并播报，
    绝不往快照目录外写文件。"""
    m = _mod()
    root = tmp_path / "solo"
    root.mkdir()
    (root / "main.py").write_text("print('solo')\n", encoding="utf-8")
    r = _git(["init", "-q"], root)
    if r.returncode != 0:
        pytest.skip("git 不可用")
    _git(["add", "-A"], root)
    assert _git(["commit", "-q", "-m", "seed"], root).returncode == 0
    snap_dir = tmp_path / "snap"
    snap_engine, _sha, notes = m.export_ref_snapshot(
        "HEAD", engine_root=root, snapshot_dir=snap_dir)
    assert snap_engine == snap_dir.resolve()
    assert (snap_engine / "main.py").is_file()
    assert any("仓根" in n for n in notes)
    # 逃逸形态＝往 snap.parent.parent/platform 写：全 tmp 树内不得出现任何
    # platform 目录（conftest 隔离夹具会在 tmp 根落别的文件，不在断言面内）
    assert list(tmp_path.rglob("platform")) == []


def test_cli_flags_pinned():
    """--ref / --export-only / SNAPSHOT_REF.txt / --clean 覆盖快照目录——
    发布 SOP 引用这些名字，改名必须同步文档与本门禁。"""
    text = _SCRIPT.read_text(encoding="utf-8")
    assert '"--ref"' in text
    assert '"--export-only"' in text
    assert "SNAPSHOT_REF.txt" in text
    assert "src-snapshot" in text
    assert "SNAPSHOT_DIR)" in text or "SNAPSHOT_DIR," in text  # --clean 名单
    m = _mod()
    assert m.SNAPSHOT_STAMP == "SNAPSHOT_REF.txt"
