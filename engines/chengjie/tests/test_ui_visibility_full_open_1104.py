# -*- coding: utf-8 -*-
"""1.0.104 内部功能显隐全开（老板决策 2026-10-01「内测用户都可以使用全功能」）门禁。

事故：1.0.103 花无缺机（内测种子装机）开发者页勾了「群成员提取」，工具箱卡出现又消失
——内测种子 ``config.desktop.internal.yaml`` 把 ``ui_visibility.*`` 以显式 false 播进
overlay，``_ensure_baseline`` 只补缺席键（红线①）永远翻不动它们。

钉住：
- 五键进 feature_registry A 类（种子显式 true，``test_desktop_seed_visibility`` 另钉）；
  matrix_nav / group_show **不**进（真机矩阵导航＝桌面包死入口）；
- 内测种子形态（五键显式 false）升级 → 五键翻 true + 标记 + 一行 WARNING；其它显式值
  一字不动；升级回读把翻过的键单列 intentional、changed 为空；
- 幂等：再 load 字节不变；标记在场后用户在开发者页关掉的键**不被翻回**；
- 服务器实例（无 AITR_DESKTOP_MODE）不动；
- 群成员提取后端闸：显隐键为真即放行（gate merge，test_group_members_routes 另钉）。
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import pytest
import yaml

ENGINE_ROOT = Path(__file__).resolve().parents[1]
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))

DESKTOP_MIN = ENGINE_ROOT / "config" / "config.desktop.min.yaml"
DESKTOP_INTERNAL = ENGINE_ROOT / "config" / "config.desktop.internal.yaml"
_LOGGER = "ai_chat_assistant.ConfigManager"

FULL_OPEN_KEYS = ("group_extract", "manual_console", "team_collab", "ai_settings", "cockpit")
STILL_HIDDEN_KEYS = ("matrix_nav", "group_show")


def _dig(d, dotted):
    node = d
    for k in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(k)
    return node


def _mk(tmp_path: Path, monkeypatch, *, overlay: dict | None, desktop: bool = True):
    from src.utils.config_manager import ConfigManager

    cfg_path = tmp_path / "config" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    # 基座用 1.0.103 之前的 min 种子形态：去掉本版新增的 ui_visibility 段，模拟升级机
    cfg = yaml.safe_load(DESKTOP_MIN.read_text(encoding="utf-8"))
    cfg.pop("ui_visibility", None)
    cfg_path.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
                        encoding="utf-8")
    if overlay is not None:
        (cfg_path.parent / "config.local.yaml").write_text(
            "# 用户手写注释\n" + yaml.safe_dump(overlay, allow_unicode=True, sort_keys=False),
            encoding="utf-8")
    monkeypatch.setenv("AITR_CONFIG_PATH", str(cfg_path))
    if desktop:
        monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    else:
        monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    monkeypatch.delenv("AITR_DEPLOY_PROFILE", raising=False)
    monkeypatch.delenv("AITR_SEED_DATA_DIR", raising=False)
    return ConfigManager(), cfg_path


def _overlay(cfg_path: Path) -> dict:
    p = cfg_path.parent / "config.local.yaml"
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {} if p.exists() else {}


# 内测种子 v1.005～1.0.103 写进 overlay 的原样形态（花无缺机）
_INTERNAL_SEED_UIV_1103 = {"group_extract": False, "group_show": False,
                           "manual_console": False, "matrix_nav": False, "team_collab": False}


# ── 注册表 / 种子 ────────────────────────────────────────────────────────────

def test_registry_a_class_covers_exactly_the_five_keys():
    from src.utils.feature_registry import FEATURES
    a_uiv = sorted(f.key.split(".", 1)[1] for f in FEATURES
                   if f.cls == "A" and f.key.startswith("ui_visibility."))
    assert a_uiv == sorted(FULL_OPEN_KEYS)
    for f in FEATURES:
        if f.key.startswith("ui_visibility."):
            assert f.baseline is True and f.show is False, f.key


def test_both_seeds_open_five_and_keep_matrix_hidden():
    for seed in (DESKTOP_MIN, DESKTOP_INTERNAL):
        cfg = yaml.safe_load(seed.read_text(encoding="utf-8"))
        uiv = cfg.get("ui_visibility") or {}
        for k in FULL_OPEN_KEYS:
            assert uiv.get(k) is True, f"{seed.name}: ui_visibility.{k} 须显式 true"
        for k in STILL_HIDDEN_KEYS:
            assert not uiv.get(k), f"{seed.name}: ui_visibility.{k} 不得默认开（真机矩阵死入口）"


# ── 存量升级 ─────────────────────────────────────────────────────────────────

def test_internal_seed_machine_upgrade_flips_five_keys_once(tmp_path, monkeypatch, caplog):
    """花无缺机形态：overlay 里躺着内测种子的五个 false（含用户勾过一次又关掉的痕迹都算
    种子形态）+ 其它用户显式值 → 五键翻 true、写标记、一行 WARNING；matrix/group_show 不动；
    其它显式值一字不动；回读 changed=[]、翻过的键单列 intentional。"""
    overlay = {"ui_visibility": dict(_INTERNAL_SEED_UIV_1103),
               "companion": {"proactive_care": {"enabled": True}},
               "ai": {"api_key": "k", "provider": "openai_compatible", "model": "m",
                      "base_url": "http://h"}}
    cm, cfg_path = _mk(tmp_path, monkeypatch, overlay=overlay)
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        assert asyncio.run(cm.load()) is True
    msgs = [r.getMessage() for r in caplog.records]
    warn = [m for m in msgs if "[upgrade] ui_visibility full-open (1.0.104)" in m]
    assert warn, "翻开必须可见（WARNING）"
    for k in ("group_extract", "manual_console", "team_collab"):
        assert f"ui_visibility.{k}" in warn[0]
    # 合并视图 + overlay 文件
    ov = _overlay(cfg_path)
    for k in FULL_OPEN_KEYS:
        assert _dig(cm.config, f"ui_visibility.{k}") is True, k
        assert _dig(ov, f"ui_visibility.{k}") is True, k
    for k in STILL_HIDDEN_KEYS:
        assert _dig(ov, f"ui_visibility.{k}") is False, k
    assert _dig(ov, "ui_visibility.full_open_1104") == "1.0.104"
    assert _dig(ov, "companion.proactive_care.enabled") is True
    # 显隐解析口径：五开两关，标记不透传
    from src.web.ui_visibility import resolve_ui_visibility
    flags = resolve_ui_visibility(cm.config)
    assert all(flags[k] for k in FULL_OPEN_KEYS) and not any(flags[k] for k in STILL_HIDDEN_KEYS)
    assert "full_open_1104" not in flags
    # 回读：changed 为空，翻过的三个显式 false 单列 intentional
    up = [m for m in msgs if "[upgrade] user_flags_preserved=" in m]
    assert up and "changed=[]" in up[-1]
    assert "intentional=" in up[-1] and "ui_visibility.group_extract" in up[-1]
    # 幂等：再 load 字节不变、不再告警
    before = (cfg_path.parent / "config.local.yaml").read_bytes()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        asyncio.run(cm.load())
    assert (cfg_path.parent / "config.local.yaml").read_bytes() == before
    assert not [r for r in caplog.records if "ui_visibility full-open" in r.getMessage()]


def test_user_turning_a_key_off_after_marker_is_respected(tmp_path, monkeypatch, caplog):
    """标记在场＝之后开发者页取消勾选是用户表态，升级/重载不得翻回。"""
    overlay = {"ui_visibility": {"group_extract": False, "manual_console": True,
                                 "team_collab": True, "ai_settings": True, "cockpit": True,
                                 "full_open_1104": "1.0.104"}}
    cm, cfg_path = _mk(tmp_path, monkeypatch, overlay=overlay)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert asyncio.run(cm.load()) is True
    assert not [r for r in caplog.records if "ui_visibility full-open" in r.getMessage()]
    assert _dig(cm.config, "ui_visibility.group_extract") is False
    assert _dig(_overlay(cfg_path), "ui_visibility.group_extract") is False
    assert cm.ui_visibility_full_open_patch() is None


def test_clean_1103_machine_gets_keys_via_baseline_and_only_marker_from_migration(
        tmp_path, monkeypatch, caplog):
    """1.0.103 clean 装机：overlay 无 ui_visibility 段 → _ensure_baseline 补五键 true，
    迁移只落标记（INFO，不 WARNING）。"""
    cm, cfg_path = _mk(tmp_path, monkeypatch, overlay={"ai": {"api_key": "k"}})
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        assert asyncio.run(cm.load()) is True
    ov = _overlay(cfg_path)
    for k in FULL_OPEN_KEYS:
        assert _dig(ov, f"ui_visibility.{k}") is True, k
    assert _dig(ov, "ui_visibility.full_open_1104") == "1.0.104"
    assert not [r for r in caplog.records if r.levelno == logging.WARNING
                and "ui_visibility full-open" in r.getMessage()]


def test_server_instance_untouched(tmp_path, monkeypatch, caplog):
    overlay = {"ui_visibility": dict(_INTERNAL_SEED_UIV_1103)}
    cm, cfg_path = _mk(tmp_path, monkeypatch, overlay=overlay, desktop=False)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        asyncio.run(cm.load())
    assert not [r for r in caplog.records if "ui_visibility full-open" in r.getMessage()]
    assert _overlay(cfg_path)["ui_visibility"] == _INTERNAL_SEED_UIV_1103


def test_patch_shape_is_pure(tmp_path, monkeypatch):
    cm, _ = _mk(tmp_path, monkeypatch, overlay=None)
    cm.config = {"ui_visibility": {"group_extract": False, "cockpit": True}}
    patch = cm.ui_visibility_full_open_patch()
    assert patch == {"ui_visibility": {
        "group_extract": True, "manual_console": True, "team_collab": True,
        "ai_settings": True, "full_open_1104": "1.0.104"}}
    cm.config["ui_visibility"]["full_open_1104"] = "1.0.104"
    assert cm.ui_visibility_full_open_patch() is None
