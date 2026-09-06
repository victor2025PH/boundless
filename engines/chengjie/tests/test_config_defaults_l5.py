"""L-5 A/C（2026-09-06，D-L1「两者」）：出厂默认进基础配置 + 种子缺席可见。

事实链（K-5 ③ / 值守 09-06 03:0x）：老板决策 D1（#166 冲刺推进器出厂开）与 D7
（#185 客户安全预警留痕+人工升级出厂开）此前只写在两处——
  · config/profiles/cloud_light.yaml：``_ensure_deploy_profile`` 只在**全新播种**时应用；
  · config/config.desktop.internal.yaml：随 **smart 包** resources/seed-data 首启播种。
两台内测机走应用内更新拿的是 **clean 包 + 升级安装**，两条路都不经过 → 1.0.74 启动
仍 ``goals.sprint.enabled=False``、危机留痕关。本批把三键升为 feature_registry A 类
基线：``_ensure_baseline`` 在**任何**桌面存量安装首启按「合并视图缺键才补」写进
overlay（显式 false 永远尊重），不再取决于装的是哪种包。

本文件钉四件事：
1. 全新桌面安装（min 种子 + 基线，**不带** cloud_light）→ 三键 True；
2. 升级安装且旧 config 无三键（skuio 机 1.0.74 clean 形态）→ 首启补进 overlay 为 True；
3. 用户显式 false → 一个字不动（三态语义）；
4. 种子缺席：桌面态首启打一行 WARNING（服务器实例不打）；
5. 五份交付配置（example / desktop.min / desktop.internal / cloud_light / 注册表）
   三键同值 True——出厂默认只允许有一个答案。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import yaml

from src.utils.feature_registry import by_key, dig

ENGINE_ROOT = Path(__file__).resolve().parent.parent
CFG = ENGINE_ROOT / "config"
DESKTOP_MIN = CFG / "config.desktop.min.yaml"

TRIO = (
    "companion.goals.sprint.enabled",
    "companion.wellbeing.crisis_audit",
    "companion.wellbeing.crisis_escalation",
)


def _mk_manager(tmp_path: Path, monkeypatch, *, existing: dict | None):
    """AITR_CONFIG_PATH 指 tmp 的真 ConfigManager（桌面态、无部署档、无种子目录）。

    existing=None → 全新安装（首启从 config.desktop.min.yaml 播种）；
    existing=dict → 先落一份既有 config.yaml（升级安装 / 老机器）。
    """
    from src.utils.config_manager import ConfigManager

    cfg_path = tmp_path / "config" / "config.yaml"
    if existing is not None:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(yaml.safe_dump(existing, allow_unicode=True),
                            encoding="utf-8")
    monkeypatch.setenv("AITR_CONFIG_PATH", str(cfg_path))
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    monkeypatch.delenv("AITR_DEPLOY_PROFILE", raising=False)
    monkeypatch.delenv("AITR_SEED_DATA_DIR", raising=False)
    return ConfigManager(), cfg_path


def _overlay_of(cfg_path: Path) -> dict:
    p = cfg_path.parent / "config.local.yaml"
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _min_seed_without_trio() -> dict:
    """桌面 min 种子去掉三键 = 「老种子快照」（skuio 机 1.0.74 之前播种的 config）。"""
    cfg = yaml.safe_load(DESKTOP_MIN.read_text(encoding="utf-8"))
    comp = cfg["companion"]
    comp["goals"].pop("sprint", None)
    comp.pop("wellbeing", None)
    for k in TRIO:
        assert dig(cfg, k) is None
    return cfg


# ── 1. 全新安装 ─────────────────────────────────────────────────────────────

def test_fresh_desktop_install_has_factory_defaults_on(tmp_path, monkeypatch):
    cm, cfg_path = _mk_manager(tmp_path, monkeypatch, existing=None)
    asyncio.run(cm.load())
    for k in TRIO:
        assert dig(cm.config, k) is True, k
    # 首启播种自 min 种子（不是 example、不是 cloud_light）——种子本身就带三键
    seeded = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    for k in TRIO:
        assert dig(seeded, k) is True, k


# ── 2. 升级安装：旧 config 无三键（clean 包内测机形态）──────────────────────

def test_upgrade_install_without_keys_gets_baseline_into_overlay(tmp_path, monkeypatch):
    cm, cfg_path = _mk_manager(tmp_path, monkeypatch,
                               existing=_min_seed_without_trio())
    asyncio.run(cm.load())
    for k in TRIO:
        assert dig(cm.config, k) is True, k
    overlay = _overlay_of(cfg_path)
    for k in TRIO:
        assert dig(overlay, k) is True, f"{k} 应被 _ensure_baseline 补进 overlay"
    # 主 config.yaml 一个字不动（补齐只写 overlay）
    assert yaml.safe_load(cfg_path.read_text(encoding="utf-8")) == _min_seed_without_trio()
    # 幂等：再 load 一次 overlay 字节不变
    before = (cfg_path.parent / "config.local.yaml").read_bytes()
    asyncio.run(cm.load())
    assert (cfg_path.parent / "config.local.yaml").read_bytes() == before


# ── 3. 显式 false 尊重 ──────────────────────────────────────────────────────

def test_explicit_false_is_respected(tmp_path, monkeypatch):
    cfg = _min_seed_without_trio()
    cfg["companion"]["goals"]["sprint"] = {"enabled": False}
    cfg["companion"]["wellbeing"] = {"crisis_audit": False}
    cm, cfg_path = _mk_manager(tmp_path, monkeypatch, existing=cfg)
    asyncio.run(cm.load())
    assert dig(cm.config, "companion.goals.sprint.enabled") is False
    assert dig(cm.config, "companion.wellbeing.crisis_audit") is False
    # 没表态的那一键仍单独补
    assert dig(cm.config, "companion.wellbeing.crisis_escalation") is True
    overlay = _overlay_of(cfg_path)
    assert dig(overlay, "companion.goals.sprint.enabled") is None
    assert dig(overlay, "companion.wellbeing.crisis_audit") is None
    assert dig(overlay, "companion.wellbeing.crisis_escalation") is True


# ── 4. 种子缺席可见（L-5 C）──────────────────────────────────────────────────

def test_seed_missing_warns_once_on_desktop(tmp_path, monkeypatch, caplog):
    from src.utils.config_manager import ConfigManager

    monkeypatch.setenv("AITR_CONFIG_PATH", str(tmp_path / "config" / "config.yaml"))
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    monkeypatch.delenv("AITR_SEED_DATA_DIR", raising=False)
    with caplog.at_level(logging.WARNING, logger="ai_chat_assistant.ConfigManager"):
        ConfigManager()
    hits = [r for r in caplog.records if "随包种子缺席" in r.getMessage()]
    assert len(hits) == 1
    assert hits[0].levelno == logging.WARNING
    assert "clean 包" in hits[0].getMessage()


def test_seed_missing_silent_on_server(tmp_path, monkeypatch, caplog):
    """服务器实例（zhiliao 等）同样走 AITR_DATA_DIR 但无桌面态——不该被这行噪音打扰。"""
    from src.utils.config_manager import ConfigManager

    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AITR_CONFIG_PATH", raising=False)
    monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    monkeypatch.delenv("AITR_SEED_DATA_DIR", raising=False)
    with caplog.at_level(logging.WARNING, logger="ai_chat_assistant.ConfigManager"):
        ConfigManager()
    assert not [r for r in caplog.records if "随包种子缺席" in r.getMessage()]


def test_seed_present_does_not_warn(tmp_path, monkeypatch, caplog):
    """有种子目录（smart 包）→ 不打「缺席」行（否则内测机日志天天误报）。"""
    from src.utils.config_manager import ConfigManager

    seed = tmp_path / "seed-data"
    seed.mkdir()
    monkeypatch.setenv("AITR_CONFIG_PATH", str(tmp_path / "data" / "config" / "config.yaml"))
    monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    monkeypatch.setenv("AITR_SEED_DATA_DIR", str(seed))
    with caplog.at_level(logging.WARNING, logger="ai_chat_assistant.ConfigManager"):
        ConfigManager()
    assert not [r for r in caplog.records if "随包种子缺席" in r.getMessage()]


# ── 5. 五份交付配置同值 ─────────────────────────────────────────────────────

def test_factory_default_single_answer_across_ship_configs():
    files = {
        "config.example.yaml": CFG / "config.example.yaml",
        "config.desktop.min.yaml": DESKTOP_MIN,
        "config.desktop.internal.yaml": CFG / "config.desktop.internal.yaml",
        "profiles/cloud_light.yaml": CFG / "profiles" / "cloud_light.yaml",
    }
    bad = []
    for name, p in files.items():
        doc = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        for k in TRIO:
            if dig(doc, k) is not True:
                bad.append(f"{name}: {k}={dig(doc, k)!r}")
    for k in TRIO:
        f = by_key(k)
        if f is None or f.cls != "A" or f.baseline is not True:
            bad.append(f"feature_registry: {k} 应为 A 类 baseline=True，实际 {f!r}")
    assert not bad, "出厂默认在各交付配置里必须只有一个答案（True）：\n  " + "\n  ".join(bad)
