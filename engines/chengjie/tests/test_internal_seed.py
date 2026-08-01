"""内测包随包数据种子（ConfigManager._ensure_seeded_extras）门禁。

背景（2026-07-31 v1.001 内测包）：安装包把生产机的人设/语音/相册/KB 暂存进
resources/seed-data/（desktop/build/stage_internal_assets.py），后端首启播种到
用户数据区。本文件钉住播种语义：

    · 无 AITR_SEED_DATA_DIR（标准包/服务器部署）= 全程 no-op；
    · 全新安装：overlay 整份拷入 + 资产逐项落位 + 相册注册表路径绝对化；
    · 已有安装：目标存在的文件/目录一概不动；overlay 只补缺失键
      （显式 false 必须被尊重——与 _ensure_baseline 同一条三态语义）；
    · 坏种子（损坏 DB 等）绝不拖垮启动，其余项照常播种。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import yaml


def _make_seed(root: Path) -> Path:
    """最小但形态完整的种子目录。"""
    seed = root / "seed"
    (seed / "config" / "voice_refs").mkdir(parents=True)
    (seed / "config" / "persona_albums" / "lin_xiaoyu").mkdir(parents=True)
    (seed / "config" / "prerender_lines").mkdir(parents=True)
    (seed / "assets" / "voices" / "lin_xiaoyu" / "prerendered").mkdir(parents=True)

    (seed / "config.local.internal.yaml").write_text(
        yaml.safe_dump({
            "avatar_voice": {"enabled": True, "retries": 0},
            "companion": {"selfie": {"enabled": True}},
            "inbox": {"l2_autosend": {"enabled": True, "deliver": True}},
        }, allow_unicode=True), encoding="utf-8")
    (seed / "config" / "profiles_runtime.yaml").write_text(
        yaml.safe_dump({"profiles": [{"id": "lin_xiaoyu", "name": "林小雨"}]},
                       allow_unicode=True), encoding="utf-8")
    (seed / "config" / "voice_refs" / "lin_xiaoyu.wav").write_bytes(b"RIFFfake")
    (seed / "config" / "voice_refs" / "lin_xiaoyu.txt").write_text("你好", encoding="utf-8")
    (seed / "config" / "persona_albums" / "lin_xiaoyu" / "beach_a_01.jpg").write_bytes(b"\xff\xd8fake")
    (seed / "config" / "prerender_lines" / "_common.txt").write_text("早安", encoding="utf-8")
    (seed / "assets" / "voices" / "lin_xiaoyu" / "prerendered" / "abcd1234.ogg").write_bytes(b"OggS")

    for name in ("knowledge_base.db", "persona_bio.db"):
        con = sqlite3.connect(str(seed / "config" / name))
        con.execute("CREATE TABLE t (x)")
        con.commit()
        con.close()
    con = sqlite3.connect(str(seed / "config" / "persona_media.db"))
    con.execute("CREATE TABLE persona_media (id TEXT PRIMARY KEY, file_path TEXT)")
    con.execute("INSERT INTO persona_media VALUES ('m1', ?)",
                (r"config\persona_albums\lin_xiaoyu\beach_a_01.jpg",))
    con.commit()
    con.close()
    return seed


def _boot(tmp_path: Path, monkeypatch, *, seed: Path | None) -> Path:
    """构造 ConfigManager（构造即播种）；返回数据根。"""
    from src.utils.config_manager import ConfigManager

    data = tmp_path / "data"
    (data / "config").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AITR_CONFIG_PATH", str(data / "config" / "config.yaml"))
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    if seed is not None:
        monkeypatch.setenv("AITR_SEED_DATA_DIR", str(seed))
    else:
        monkeypatch.delenv("AITR_SEED_DATA_DIR", raising=False)
    ConfigManager()
    return data


class TestFreshInstall:
    def test_overlay_and_assets_seeded(self, tmp_path, monkeypatch):
        seed = _make_seed(tmp_path)
        data = _boot(tmp_path, monkeypatch, seed=seed)

        overlay = yaml.safe_load(
            (data / "config" / "config.local.yaml").read_text(encoding="utf-8"))
        assert overlay["avatar_voice"]["enabled"] is True
        assert overlay["companion"]["selfie"]["enabled"] is True

        assert (data / "config" / "profiles_runtime.yaml").exists()
        assert (data / "config" / "voice_refs" / "lin_xiaoyu.wav").exists()
        assert (data / "config" / "persona_albums" / "lin_xiaoyu" / "beach_a_01.jpg").exists()
        assert (data / "assets" / "voices" / "lin_xiaoyu" / "prerendered" / "abcd1234.ogg").exists()
        assert (data / "config" / "knowledge_base.db").exists()

    def test_media_db_paths_absolutized(self, tmp_path, monkeypatch):
        seed = _make_seed(tmp_path)
        data = _boot(tmp_path, monkeypatch, seed=seed)
        con = sqlite3.connect(str(data / "config" / "persona_media.db"))
        try:
            (fp,) = con.execute(
                "SELECT file_path FROM persona_media WHERE id='m1'").fetchone()
        finally:
            con.close()
        assert Path(fp).is_absolute()
        assert Path(fp).is_file()
        assert Path(fp) == (
            data / "config" / "persona_albums" / "lin_xiaoyu" / "beach_a_01.jpg").resolve()


class TestNoSeedNoop:
    def test_server_mode_untouched(self, tmp_path, monkeypatch):
        data = _boot(tmp_path, monkeypatch, seed=None)
        assert not (data / "config" / "config.local.yaml").exists()
        assert not (data / "config" / "voice_refs").exists()
        assert not (data / "assets").exists()

    def test_missing_seed_dir_untouched(self, tmp_path, monkeypatch):
        data = _boot(tmp_path, monkeypatch, seed=tmp_path / "nope")
        assert not (data / "config" / "config.local.yaml").exists()


class TestUpgradeInstall:
    def test_existing_overlay_merges_missing_keys_only(self, tmp_path, monkeypatch):
        seed = _make_seed(tmp_path)
        data = tmp_path / "data"
        (data / "config").mkdir(parents=True)
        # 用户显式关掉 avatar_voice + 自己的 AI Key：都必须原样保住
        (data / "config" / "config.local.yaml").write_text(
            yaml.safe_dump({
                "avatar_voice": {"enabled": False},
                "ai": {"api_key": "sk-user-own"},
            }, allow_unicode=True), encoding="utf-8")
        _boot(tmp_path, monkeypatch, seed=seed)

        overlay = yaml.safe_load(
            (data / "config" / "config.local.yaml").read_text(encoding="utf-8"))
        assert overlay["avatar_voice"]["enabled"] is False          # 显式值不动
        assert overlay["ai"]["api_key"] == "sk-user-own"            # 用户凭据不动
        assert overlay["avatar_voice"]["retries"] == 0              # 缺失键补入
        assert overlay["companion"]["selfie"]["enabled"] is True    # 缺失子树补入
        assert overlay["inbox"]["l2_autosend"]["deliver"] is True

    def test_existing_assets_never_overwritten(self, tmp_path, monkeypatch):
        seed = _make_seed(tmp_path)
        data = tmp_path / "data"
        (data / "config" / "voice_refs").mkdir(parents=True)
        own = data / "config" / "voice_refs" / "own.wav"
        own.write_bytes(b"user")
        _boot(tmp_path, monkeypatch, seed=seed)
        # 目录已存在 → 整树跳过：用户文件在、种子文件不进
        assert own.read_bytes() == b"user"
        assert not (data / "config" / "voice_refs" / "lin_xiaoyu.wav").exists()

    def test_second_boot_idempotent(self, tmp_path, monkeypatch):
        seed = _make_seed(tmp_path)
        data = _boot(tmp_path, monkeypatch, seed=seed)
        overlay = data / "config" / "config.local.yaml"
        first = overlay.stat().st_mtime_ns
        _boot(tmp_path, monkeypatch, seed=seed)  # 二次构造
        assert overlay.stat().st_mtime_ns == first  # 无缺失键=零写盘


class TestBrokenSeedNeverBlocksBoot:
    def test_corrupt_media_db_skips_only_that_item(self, tmp_path, monkeypatch):
        seed = _make_seed(tmp_path)
        (seed / "config" / "persona_media.db").write_bytes(b"not a sqlite file")
        data = _boot(tmp_path, monkeypatch, seed=seed)  # 不得抛
        # 其余项照常播种
        assert (data / "config" / "config.local.yaml").exists()
        assert (data / "config" / "voice_refs" / "lin_xiaoyu.wav").exists()
