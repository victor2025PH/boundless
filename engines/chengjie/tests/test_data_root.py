# -*- coding: utf-8 -*-
"""运行根契约门禁（scripts/_data_root.py）。

防回归点＝2026-07 实例迁移事故：夜间 CLI（预渲染/声纹抽检/参考音审计）按引擎根
解析 config/profiles/产物路径，与实例数据根（app CWD）分家 → 整链空转 10 天。
契约：CLI --data-root > env AITR_DATA_ROOT > 实例自动发现（尊重退役旗标）> 引擎根。
"""
from __future__ import annotations

from pathlib import Path

from scripts._data_root import (
    ENGINE_ROOT,
    discover_instance_roots,
    load_merged_config,
    profiles_runtime_path,
    resolve_data_roots,
)


def _mk_instance(base: Path, name: str, *, with_config: bool = True) -> Path:
    data = base / name / "data"
    (data / "config").mkdir(parents=True, exist_ok=True)
    if with_config:
        (data / "config" / "config.yaml").write_text(
            "web_admin:\n  port: 1\n", encoding="utf-8")
    return data


def test_discover_skips_retired_and_invalid(tmp_path):
    base = tmp_path / "instances"
    zhiliao = _mk_instance(base, "zhiliao")
    _mk_instance(base, "tongyi")                      # 有 config 但已退役
    _mk_instance(base, "junk", with_config=False)     # 无 config.yaml → 非实例
    (base / ".ops" / "retired").mkdir(parents=True)
    (base / ".ops" / "retired" / "tongyi.flag").write_text("retired", encoding="utf-8")

    roots = discover_instance_roots(base)
    assert roots == [zhiliao]


def test_discover_missing_base_returns_empty(tmp_path):
    assert discover_instance_roots(tmp_path / "nope") == []


def test_resolve_precedence_cli_env_discovery_fallback(tmp_path, monkeypatch):
    base = tmp_path / "instances"
    zhiliao = _mk_instance(base, "zhiliao")

    # ① CLI 显式值最高优先（env 同时在也无视）
    monkeypatch.setenv("AITR_DATA_ROOT", str(tmp_path / "envroot"))
    assert resolve_data_roots(str(tmp_path / "cliroot"), base=base) == [
        tmp_path / "cliroot"]

    # ② 无 CLI → env
    assert resolve_data_roots("", base=base) == [tmp_path / "envroot"]

    # ③ 无 CLI/env → 实例自动发现
    monkeypatch.delenv("AITR_DATA_ROOT", raising=False)
    assert resolve_data_roots("", base=base) == [zhiliao]

    # ④ 无实例部署 → 引擎根（开发机/CI 旧行为）
    assert resolve_data_roots("", base=tmp_path / "empty") == [ENGINE_ROOT]


def test_load_merged_config_overlay_deep_merge(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "config.yaml").write_text(
        "ai:\n  model: base\n  keep: yes\nweb_admin:\n  port: 18799\n",
        encoding="utf-8")
    (cfg / "config.local.yaml").write_text(
        "ai:\n  model: overlay\n", encoding="utf-8")

    merged = load_merged_config(tmp_path)
    assert merged["ai"]["model"] == "overlay"      # overlay 覆写
    assert merged["ai"]["keep"] is True            # 深合并保留 base 键
    assert merged["web_admin"]["port"] == 18799


def test_load_merged_config_missing_files_soft(tmp_path):
    assert load_merged_config(tmp_path) == {}      # 坏根软失败，不抛


def test_profiles_runtime_path_shape(tmp_path):
    assert profiles_runtime_path(tmp_path) == tmp_path / "config" / "profiles_runtime.yaml"


def test_prerender_cli_accepts_data_root_and_flags_empty_root(tmp_path, capsys):
    """CLI 契约：--data-root 指向空根 → exit 2（夜间日志可见「空转」而非静默 0）。"""
    from scripts.avatar_prerender import main

    empty = tmp_path / "data"
    (empty / "config").mkdir(parents=True)
    (empty / "config" / "config.yaml").write_text("{}", encoding="utf-8")

    rc = main(["--all-personas", "--data-root", str(empty)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "没有 avatar_clone 人设可渲染" in out


def test_collect_personas_resolves_relative_ref_against_root(tmp_path):
    """相对 reference_audio_path（app 按 CWD=数据根解析）在 CLI 侧必须锚定数据根，
    而不是 CLI 进程的 CWD——2026-07-29 实弹发现的第二层根 bug。"""
    from scripts.avatar_prerender import _collect_avatar_personas

    root = tmp_path / "data"
    (root / "config" / "voice_refs").mkdir(parents=True)
    (root / "config" / "voice_refs" / "a.wav").write_bytes(b"RIFF")
    (root / "config" / "profiles_runtime.yaml").write_text(
        "profiles:\n"
        "  a:\n"
        "    voice_profile:\n"
        "      backend: avatar_clone\n"
        "      reference_audio_path: config/voice_refs/a.wav\n"
        "  b:\n"
        "    voice_profile:\n"
        "      backend: avatar_clone\n"
        "      reference_audio_path: config/voice_refs/missing.wav\n",
        encoding="utf-8")

    got = _collect_avatar_personas({}, root=root)
    assert [(p, Path(r)) for p, r in got] == [
        ("a", root / "config" / "voice_refs" / "a.wav")]


def test_prerender_cli_single_persona_missing_ref_is_config_error(tmp_path, capsys):
    from scripts.avatar_prerender import main

    empty = tmp_path / "data"
    (empty / "config").mkdir(parents=True)
    (empty / "config" / "config.yaml").write_text("{}", encoding="utf-8")

    rc = main(["--persona", "ghost", "--lines", "你好",
               "--data-root", str(empty)])
    out = capsys.readouterr().out
    assert rc == 2
    assert "参考音不存在" in out
