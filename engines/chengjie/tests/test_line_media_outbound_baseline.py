"""LINE 出站媒体公共包默认开（老板例外 2026-09-24）：新装种子 + 存量 A 类补齐 + 显式值不动。

worker 侧 ``send_media`` 随开关挂载的契约见 test_line_media.py；本文件只钉交付面：
公共包种子与基线让 ``resolve_line_media_cfg(...)["outbound"]`` 为真，而用户显式 false
与服务器实例（无 AITR_DESKTOP_MODE）不受影响。
"""
from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import yaml

from src.integrations.line_media import resolve_line_media_cfg
from src.utils.feature_registry import baseline_patch, by_key, is_send_behavior_key

ENGINE_ROOT = Path(__file__).resolve().parent.parent
SEED = ENGINE_ROOT / "config" / "config.desktop.min.yaml"
KEY = "platform_login.line.media.outbound"
_AI = {"api_key": "k", "provider": "openai_compatible", "model": "m", "base_url": "http://h"}


def _seed() -> dict:
    return yaml.safe_load(SEED.read_text(encoding="utf-8"))


def _seed_1097() -> dict:
    """1.0.97 形态：种子 LINE 段没有 media 块。"""
    cfg = copy.deepcopy(_seed())
    cfg["platform_login"]["line"].pop("media", None)
    return cfg


def _mk(tmp_path: Path, monkeypatch, *, overlay: dict, desktop: bool = True):
    from src.utils.config_manager import ConfigManager

    cfg_path = tmp_path / "config" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump(_seed_1097(), allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
    (cfg_path.parent / "config.local.yaml").write_text(
        yaml.safe_dump(overlay, allow_unicode=True, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("AITR_CONFIG_PATH", str(cfg_path))
    if desktop:
        monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    else:
        monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    monkeypatch.delenv("AITR_DEPLOY_PROFILE", raising=False)
    monkeypatch.delenv("AITR_SEED_DATA_DIR", raising=False)
    cm = ConfigManager()
    loaded = asyncio.run(cm.load())
    if desktop:
        assert loaded is True
    return cm, cfg_path


def _overlay(cfg_path: Path) -> dict:
    return yaml.safe_load((cfg_path.parent / "config.local.yaml").read_text(encoding="utf-8")) or {}


def test_registry_entry_is_a_class_true_and_not_send_behavior_prefixed():
    f = by_key(KEY)
    assert f is not None and f.cls == "A" and f.baseline is True
    assert not is_send_behavior_key(KEY)


def test_fresh_install_seed_enables_line_outbound():
    assert resolve_line_media_cfg(_seed())["outbound"] is True


def test_code_default_still_off_for_server_instances():
    assert resolve_line_media_cfg({})["outbound"] is False


def test_baseline_patch_fills_missing_only():
    assert baseline_patch(_seed_1097()).get(KEY) is True
    explicit_off = copy.deepcopy(_seed_1097())
    explicit_off["platform_login"]["line"]["media"] = {"outbound": False}
    assert KEY not in baseline_patch(explicit_off)


def test_upgrade_desktop_missing_key_gets_outbound(tmp_path, monkeypatch):
    cm, cfg_path = _mk(tmp_path, monkeypatch, overlay={"ai": _AI})
    assert resolve_line_media_cfg(cm.config)["outbound"] is True
    ov = _overlay(cfg_path)
    assert ov["platform_login"]["line"]["media"]["outbound"] is True


def test_upgrade_desktop_explicit_false_preserved(tmp_path, monkeypatch):
    overlay = {"ai": _AI, "platform_login": {"line": {"media": {"outbound": False}}}}
    cm, cfg_path = _mk(tmp_path, monkeypatch, overlay=overlay)
    assert resolve_line_media_cfg(cm.config)["outbound"] is False
    assert _overlay(cfg_path)["platform_login"]["line"]["media"]["outbound"] is False


def test_server_instance_not_patched(tmp_path, monkeypatch):
    cm, cfg_path = _mk(tmp_path, monkeypatch, overlay={"ai": _AI}, desktop=False)
    assert resolve_line_media_cfg(cm.config)["outbound"] is False
    assert "platform_login" not in _overlay(cfg_path)
