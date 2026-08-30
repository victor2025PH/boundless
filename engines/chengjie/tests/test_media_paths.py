# -*- coding: utf-8 -*-
"""实施86 0830 午批·批C 门禁：相册落盘数据根迁移 + 内容校验 + 归因修正（#67）。

金标=skuio 机 28DTZS backend.log 三层实锤：①上传落进打包安装目录（更新即清空）
②损坏文件原样落盘（发送时 pyrogram decode 失败）③decode 失败被误导性归因。
"""
from __future__ import annotations

from pathlib import Path

import src.companion.media_paths as mp

_ENGINE_ROOT = Path(__file__).resolve().parents[1]


# ── resolve_album_root：env 数据根优先，裸引擎回落旧树 ──────────────────────

def test_root_follows_data_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("AITR_CONFIG_PATH", raising=False)
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path / "data"))
    assert mp.resolve_album_root() == tmp_path / "data" / "persona_albums"


def test_root_follows_config_path(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "AITR_CONFIG_PATH", str(tmp_path / "data" / "config" / "config.yaml"))
    monkeypatch.delenv("AITR_DATA_DIR", raising=False)
    assert mp.resolve_album_root() == tmp_path / "data" / "persona_albums"


def test_root_falls_back_to_legacy(monkeypatch):
    """裸引擎/CI（无 env 契约）→ 旧引擎树位置，零行为变化。"""
    monkeypatch.delenv("AITR_CONFIG_PATH", raising=False)
    monkeypatch.delenv("AITR_DATA_DIR", raising=False)
    assert mp.resolve_album_root() == mp.LEGACY_ALBUM_ROOT
    assert "persona_albums" in str(mp.LEGACY_ALBUM_ROOT)


# ── sniff_media_bytes：媒体产物验证纪律的上传侧落地 ─────────────────────────

def test_sniff_accepts_real_magics():
    ok = mp.sniff_media_bytes
    assert ok(b"\xff\xd8\xff\xe0" + b"x" * 12, ".jpg") == ""
    assert ok(b"\x89PNG\r\n\x1a\n" + b"x" * 8, ".png") == ""
    assert ok(b"GIF89a" + b"x" * 10, ".gif") == ""
    assert ok(b"RIFF\x00\x00\x00\x00WEBPVP8 ", ".webp") == ""
    assert ok(b"\x00\x00\x00\x18ftypmp42" + b"x" * 8, ".mp4") == ""
    assert ok(b"\x1a\x45\xdf\xa3" + b"x" * 12, ".webm") == ""


def test_sniff_rejects_corrupt_and_tiny():
    assert mp.sniff_media_bytes(b"\x00\x01garbage-bytes-here", ".jpg")
    assert mp.sniff_media_bytes(b"\x00\x01no-ftyp-anywhere!!", ".mp4")
    assert mp.sniff_media_bytes(b"tiny", ".png")


def test_sniff_cross_family_and_unknown_ext_pass():
    # 扩展名拍错但内容是合法图 → 放行（内容为准）；未知扩展名交白名单拦
    assert mp.sniff_media_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 8, ".jpg") == ""
    assert mp.sniff_media_bytes(b"anything-at-all-here", ".xyz") == ""


# ── migrate_legacy_album_tree：复制 + DB 前缀改写，幂等 ─────────────────────

class _FakeStore:
    def __init__(self):
        self.calls = []

    def rewrite_file_path_prefix(self, old, new):
        self.calls.append((old, new))
        return 3


def test_migration_copies_and_rewrites(monkeypatch, tmp_path):
    legacy = tmp_path / "legacy"
    newroot = tmp_path / "data" / "persona_albums"
    (legacy / "lin").mkdir(parents=True)
    (legacy / "lin" / "a.jpg").write_bytes(b"\xff\xd8\xffcontent")
    monkeypatch.setattr(mp, "LEGACY_ALBUM_ROOT", legacy)
    monkeypatch.setattr(mp, "resolve_album_root", lambda: newroot)
    st = _FakeStore()
    out = mp.migrate_legacy_album_tree(st)
    assert out["active"] and out["migrated"] == 1
    assert (newroot / "lin" / "a.jpg").read_bytes() == b"\xff\xd8\xffcontent"
    assert st.calls == [(str(legacy), str(newroot))]
    # 幂等：第二次全 skipped，不重复复制
    out2 = mp.migrate_legacy_album_tree(None)
    assert out2["migrated"] == 0 and out2["skipped"] == 1


def test_migration_noop_when_same_root(monkeypatch, tmp_path):
    monkeypatch.setattr(mp, "LEGACY_ALBUM_ROOT", tmp_path)
    monkeypatch.setattr(mp, "resolve_album_root", lambda: tmp_path)
    out = mp.migrate_legacy_album_tree(_FakeStore())
    assert out == {"migrated": 0, "skipped": 0, "db_rows": 0, "active": False}


# ── store.rewrite_file_path_prefix：真 DB 前缀改写 ──────────────────────────

def test_store_prefix_rewrite(tmp_path):
    from src.companion.persona_media_store import PersonaMediaStore
    st = PersonaMediaStore(tmp_path / "pm.db")
    row = st.add("lin", "photo", r"D:\old\root\lin\a.jpg",
                 "/static/persona_albums/lin/a.jpg")
    other = st.add("lin", "photo", r"E:\elsewhere\b.jpg",
                   "/static/persona_albums/lin/b.jpg")
    n = st.rewrite_file_path_prefix(r"D:\old\root", r"F:\new\root")
    assert n == 1
    assert st.get(row["id"])["file_path"] == r"F:\new\root\lin\a.jpg"
    assert st.get(other["id"])["file_path"] == r"E:\elsewhere\b.jpg"  # 不匹配不动
    assert st.rewrite_file_path_prefix(r"D:\old\root", r"F:\new\root") == 0  # 幂等


# ── 接线契约（静态） ────────────────────────────────────────────────────────

def test_upload_route_uses_resolver_and_sniff():
    src = (_ENGINE_ROOT / "src" / "web" / "routes"
           / "persona_media_routes.py").read_text(encoding="utf-8")
    assert "resolve_album_root" in src
    assert "sniff_media_bytes" in src or "_sniff_media" in src
    assert "migrate_legacy_album_tree" in src
    assert "err.pmedia.bad_content" in src


def test_admin_mounts_album_dual_root():
    src = (_ENGINE_ROOT / "src" / "web" / "admin.py").read_text(encoding="utf-8")
    assert '"/static/persona_albums"' in src
    assert "resolve_album_root" in src


def test_autosend_attributes_missing_file_67():
    """#67-③：DB 指向已蒸发路径 → 独立归因 album_file_missing（不再吞成泛化失败）。"""
    src = (_ENGINE_ROOT / "src" / "inbox"
           / "image_autosend.py").read_text(encoding="utf-8")
    assert "album_file_missing" in src


def test_diag_collects_renderer_log_70():
    """#70-②：诊断包收壳侧 renderer.log（前端层故障的第一现场）。"""
    src = (_ENGINE_ROOT / "src" / "utils" / "diag_upload.py").read_text(
        encoding="utf-8")
    assert "renderer.log" in src
    shell = (_ENGINE_ROOT / "desktop" / "main.js").read_text(encoding="utf-8")
    assert "rendererDiagLog" in shell and "renderer.log" in shell


def test_seed_configs_have_no_duplicate_keys_70():
    """#70-①：三份桌面种子零重复顶层键（1.061 种子 telegram 重复键实锤）。"""
    import re
    for name in ("config.desktop.min.yaml", "config.desktop.internal.yaml",
                 "config.example.yaml"):
        p = _ENGINE_ROOT / "config" / name
        if not p.is_file():
            continue
        seen: dict = {}
        dups = []
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):(\s|$)", line)
            if not m:
                continue
            k = m.group(1)
            if k in seen:
                dups.append((k, seen[k], i))
            seen[k] = i
        assert not dups, f"{name} 顶层键重复: {dups}"
