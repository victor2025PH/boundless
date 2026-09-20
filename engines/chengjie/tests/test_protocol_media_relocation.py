# -*- coding: utf-8 -*-
"""协议媒体迁入实例数据根（账号资产 P0 lane-B，2026-08-19）。

钉住三件套契约：
1. ``protocol_media_root`` 数据根解析（AITR_CONFIG_PATH > AITR_DATA_DIR > 旧引擎树回落）；
2. ``migrate_legacy_protocol_media`` 幂等搬迁（冲突保源绝不删数据、单文件失败不挡批、
   空壳目录清理、无契约 no-op）；
3. ``ProtocolMediaStatic`` 双根静态服务（主根优先、旧根兜底＝搬迁零 404 窗口）
   + ``instance_backup.should_exclude`` 不排除 protocol_media——「媒体随实例进备份」
   是整次迁移的产品动机，钉死防有人往 denylist 里加。

⚠ 所有搬迁用例必须 monkeypatch 两个根函数指向 tmp——绝不许对真实引擎树跑迁移
（生产机引擎树 static/protocol_media 里是真媒体）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import src.integrations.protocol_bridge as pb


# ── 根解析 ────────────────────────────────────────────────────────────────


def test_root_prefers_config_path_env(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "AITR_CONFIG_PATH", str(tmp_path / "data" / "config" / "config.yaml"))
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path / "other"))
    assert pb.protocol_media_root() == tmp_path / "data" / "protocol_media"


def test_root_uses_data_dir_env(monkeypatch, tmp_path):
    monkeypatch.delenv("AITR_CONFIG_PATH", raising=False)
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path / "d"))
    assert pb.protocol_media_root() == tmp_path / "d" / "protocol_media"


def test_root_falls_back_to_engine_static_without_contract(monkeypatch):
    monkeypatch.delenv("AITR_CONFIG_PATH", raising=False)
    monkeypatch.delenv("AITR_DATA_DIR", raising=False)
    d = pb.protocol_media_root()
    assert d == pb.legacy_protocol_media_root()
    assert d.is_absolute()
    # 无契约＝旧行为：引擎树 static 下（裸开发机零感知）
    assert (Path(pb.__file__).resolve().parents[1] / "web" / "static") in d.parents


# ── 搬迁器 ────────────────────────────────────────────────────────────────


def _wire_roots(monkeypatch, legacy: Path, new: Path) -> None:
    monkeypatch.setattr(pb, "legacy_protocol_media_root", lambda: legacy)
    monkeypatch.setattr(pb, "protocol_media_root", lambda: new)


def test_migrate_moves_preserves_tree_and_skips_conflicts(monkeypatch, tmp_path):
    legacy = tmp_path / "legacy"
    new = tmp_path / "new"
    (legacy / "telegram" / "avatars").mkdir(parents=True)
    (legacy / "telegram" / "a.jpg").write_bytes(b"A")
    (legacy / "telegram" / "avatars" / "b.jpg").write_bytes(b"B")
    (new / "telegram").mkdir(parents=True)
    (new / "telegram" / "a.jpg").write_bytes(b"NEW")  # 冲突：目标已存在

    _wire_roots(monkeypatch, legacy, new)
    st = pb.migrate_legacy_protocol_media()
    assert st == {"moved": 1, "skipped": 1, "failed": 0}
    # 冲突文件：目标保持原值、源保留（绝不删数据）
    assert (new / "telegram" / "a.jpg").read_bytes() == b"NEW"
    assert (legacy / "telegram" / "a.jpg").read_bytes() == b"A"
    # 搬走的文件：内容一致、树形保留、空壳目录被清
    assert (new / "telegram" / "avatars" / "b.jpg").read_bytes() == b"B"
    assert not (legacy / "telegram" / "avatars").exists()

    # 幂等：第二轮只剩冲突 skip，零 moved 零 failed
    st2 = pb.migrate_legacy_protocol_media()
    assert st2 == {"moved": 0, "skipped": 1, "failed": 0}


def test_migrate_noop_when_no_contract(monkeypatch, tmp_path):
    """根==旧根（无数据根契约）＝彻底 no-op，一个文件都不动。"""
    root = tmp_path / "same"
    (root / "x").mkdir(parents=True)
    (root / "x" / "f.bin").write_bytes(b"F")
    _wire_roots(monkeypatch, root, root)
    assert pb.migrate_legacy_protocol_media() == {
        "moved": 0, "skipped": 0, "failed": 0}
    assert (root / "x" / "f.bin").read_bytes() == b"F"


def test_migrate_noop_when_legacy_missing(monkeypatch, tmp_path):
    _wire_roots(monkeypatch, tmp_path / "nope", tmp_path / "new")
    assert pb.migrate_legacy_protocol_media() == {
        "moved": 0, "skipped": 0, "failed": 0}


# ── 双根静态服务 ──────────────────────────────────────────────────────────


def test_dual_root_static_prefers_new_and_falls_back(tmp_path):
    from src.web.admin import ProtocolMediaStatic

    new = tmp_path / "new"
    legacy = tmp_path / "legacy"
    new.mkdir()
    legacy.mkdir()
    (new / "x.txt").write_text("new", encoding="utf-8")
    (legacy / "x.txt").write_text("old", encoding="utf-8")
    (legacy / "only_legacy.txt").write_text("L", encoding="utf-8")

    sf = ProtocolMediaStatic(directory=str(new), fallback_directory=str(legacy))
    full, st = sf.lookup_path("x.txt")
    assert st is not None
    assert Path(full).read_text(encoding="utf-8") == "new"  # 主根优先
    full, st = sf.lookup_path("only_legacy.txt")
    assert st is not None
    assert Path(full).read_text(encoding="utf-8") == "L"    # 旧根兜底
    _, st = sf.lookup_path("missing.txt")
    assert st is None


def test_dual_root_static_without_fallback(tmp_path):
    from src.web.admin import ProtocolMediaStatic

    new = tmp_path / "new"
    new.mkdir()
    (new / "x.txt").write_text("new", encoding="utf-8")
    sf = ProtocolMediaStatic(directory=str(new))
    full, st = sf.lookup_path("x.txt")
    assert st is not None
    _, st = sf.lookup_path("nope.txt")
    assert st is None


# ── 备份契约 ──────────────────────────────────────────────────────────────


def test_backup_does_not_exclude_protocol_media():
    """媒体随实例进备份＝本次迁移的产品动机；denylist 加 protocol_media 即违约。"""
    from scripts.instance_backup import should_exclude

    assert not should_exclude("protocol_media", is_dir=True)
    assert not should_exclude("protocol_media/telegram/a.jpg", is_dir=False)
    assert not should_exclude(
        "protocol_media/telegram/avatars/b.jpg", is_dir=False)
