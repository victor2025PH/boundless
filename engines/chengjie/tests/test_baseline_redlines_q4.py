"""Q-4（#267 #265，2026-09-09）：基线三条红线 + D-Q1/D-Q2 撤回 + 升级回读 + 人工永不限额。

事故（KYHGSZ / CSTCJT / 65UQRE）：1.0.78 把 ``inbox.work_schedule`` 08:20–01:00 按服务器
本机时区补进每台桌面机 overlay → 美英客户的下午被静默扣留；额度闸门「300 · 安装包默认」
连人工也拦；用户 9/7 显式设 ``proactive_care.enabled=true``，升级后派发器首拍 False。

本文件钉住：
1. 红线①：``baseline_patch`` 只补缺席键；``ConfigManager.load()`` 全部迁移跑完后 overlay
   里升级前就存在的显式叶子逐字不变，并落 ``[upgrade] user_flags_preserved=N changed=[]``；
2. 红线②：会改变发送行为的键（班表 / 额度 / 拆条 / 关怀 / 全自动）不得进 A 类——
   ``_validate`` fail-fast，且现表里没有一个 A 类命中；
3. 红线③：时区键不得进 A 类、不得以本机哨兵作 baseline；
4. D-Q1/D-Q2 撤回：只回滚「1.0.78 基线原样形态」，用户改过一个字都不动；幂等；落
   ``[baseline] rollback <key> reason=D-Qn``；服务器实例（无 AITR_DESKTOP_MODE）不动；
5. D-Q2：``companion_send_gate.gate_decision`` origin=manual 在额度用尽后仍放行。
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

ENGINE_ROOT = Path(__file__).resolve().parent.parent
DESKTOP_MIN = ENGINE_ROOT / "config" / "config.desktop.min.yaml"
_LOGGER = "ai_chat_assistant.ConfigManager"

# 1.0.78 基线 / 种子写进机器的原样形态（与 feature_registry.ROLLED_BACK_BASELINES 同源）
_WS_1078 = {"enabled": True, "default": {"start": "08:20", "end": "01:00"}}
_SG_1078 = {"enabled": True, "target_cap": 300, "warmup_start_cap": 100, "warmup_ramp_days": 3}


def _dig(doc, path: str):
    node = doc
    for k in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(k)
    return node


def _seed_1077_text() -> str:
    """1.0.77 种子（O-1 D 之前）——升级机的 config.yaml 基座；git 不可用时用当前种子去掉
    D-Q 撤回块模拟。"""
    try:
        return subprocess.check_output(
            ["git", "show", "2ddbb3ec~1:./config/config.desktop.min.yaml"],
            cwd=str(ENGINE_ROOT), stderr=subprocess.DEVNULL).decode("utf-8")
    except Exception:
        cfg = yaml.safe_load(DESKTOP_MIN.read_text(encoding="utf-8"))
        (cfg.get("inbox") or {}).pop("work_schedule", None)
        cfg.pop("companion_send_gate", None)
        return yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)


def _mk(tmp_path: Path, monkeypatch, *, base_text: str, overlay: dict | None,
        desktop: bool = True):
    from src.utils.config_manager import ConfigManager

    cfg_path = tmp_path / "config" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(base_text, encoding="utf-8")
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


# ── 红线①：只补缺席键 / 升级不得改变用户显式值 ──────────────────────────────

_USER_OVERLAY = {
    "ai": {"api_key": "k", "provider": "openai_compatible", "model": "m", "base_url": "http://h"},
    "companion": {"proactive_care": {"enabled": True, "dry_run": False},
                  "goals": {"enabled": True, "sprint": {"enabled": False}}},
    "inbox": {"auto_draft": {"automation_mode": "auto_ai"},
              "reply_style": {"bubbles": {"enabled": True, "explicit_newline_only": False}}},
    "memory": {"extract": {"intents": []}},
}


def test_upgrade_1077_to_1079_preserves_every_explicit_user_value(tmp_path, monkeypatch, caplog):
    """65UQRE 复现：1.0.77 数据目录 + 用户显式 ``proactive_care.enabled=true`` 升到当前代码
    ——所有显式叶子逐字不变，``[upgrade] user_flags_preserved=N changed=[]``。"""
    cm, cfg_path = _mk(tmp_path, monkeypatch, base_text=_seed_1077_text(), overlay=_USER_OVERLAY)
    before = cm._overlay_leaves()
    assert before["companion.proactive_care.enabled"] is True
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        assert asyncio.run(cm.load()) is True
    # 合并视图：用户值全部在
    assert _dig(cm.config, "companion.proactive_care.enabled") is True
    assert _dig(cm.config, "companion.proactive_care.dry_run") is False
    assert _dig(cm.config, "companion.goals.sprint.enabled") is False
    assert _dig(cm.config, "inbox.auto_draft.automation_mode") == "auto_ai"
    assert _dig(cm.config, "memory.extract.intents") == []
    # overlay 文件：升级前每个显式叶子逐字相等（基线只追加缺席键）
    after = cm._overlay_leaves()
    for k, v in before.items():
        assert after.get(k) == v, f"红线①被破：{k} {v!r} → {after.get(k)!r}"
    # 回读日志：changed 为空
    hits = [r for r in caplog.records if "[upgrade] user_flags_preserved=" in r.getMessage()]
    assert hits, "升级回读日志缺席"
    assert "changed=[]" in hits[-1].getMessage() and hits[-1].levelno == logging.INFO
    # 基线补齐仍发生（缺席键被补），且班表 / 额度不在其中（红线②）
    assert _dig(cm.config, "companion.goals.profile_llm.enabled") is True
    assert _dig(_overlay(cfg_path), "inbox.work_schedule.enabled") is None


def test_upgrade_readback_flags_a_changed_explicit_value(tmp_path, monkeypatch, caplog):
    """回读本身要能红：人为把 overlay 里一个显式值改掉，WARNING + changed 列出该键。"""
    cm, cfg_path = _mk(tmp_path, monkeypatch, base_text=_seed_1077_text(), overlay=_USER_OVERLAY)
    before = cm._overlay_leaves()
    ov = _overlay(cfg_path)
    ov["companion"]["proactive_care"]["enabled"] = False
    (cfg_path.parent / "config.local.yaml").write_text(
        yaml.safe_dump(ov, allow_unicode=True, sort_keys=False), encoding="utf-8")
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        summary = cm._log_upgrade_readback(before, [])
    assert [c["key"] for c in summary["changed"]] == ["companion.proactive_care.enabled"]
    warn = [r for r in caplog.records if r.levelno == logging.WARNING
            and "[upgrade] user_flags_preserved=" in r.getMessage()]
    assert warn and "companion.proactive_care.enabled" in warn[0].getMessage()


def test_baseline_patch_never_touches_explicit_values():
    from src.utils.feature_registry import FEATURES, baseline_patch, dig

    # 每个 A 类键显式写成「非基线值」→ patch 里一个都不出现
    cfg: dict = {}
    for f in FEATURES:
        if f.cls != "A":
            continue
        node = cfg
        parts = f.key.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = (False if isinstance(f.baseline, bool) else
                           [] if isinstance(f.baseline, list) else "user")
    assert baseline_patch(cfg) == {}
    for f in FEATURES:
        if f.cls == "A":
            assert dig(cfg, f.key) is not None


# ── 红线②③：结构性门禁 ──────────────────────────────────────────────────────

def test_redline2_no_send_behavior_key_is_a_class():
    from src.utils.feature_registry import FEATURES, is_send_behavior_key

    bad = [f.key for f in FEATURES if f.cls == "A" and is_send_behavior_key(f.key)]
    assert not bad, f"红线②：会改变发送行为的开关进了 A 类基线: {bad}"
    # 名单覆盖事故五类
    for k in ("inbox.work_schedule.enabled", "companion_send_gate.enabled",
              "inbox.reply_style.bubbles.enabled", "companion.proactive_care.enabled",
              "inbox.l2_autosend.deliver", "inbox.auto_draft.automation_mode"):
        assert is_send_behavior_key(k), k
    # 拟人节奏 / 入站合并不是「多发少发换时间」——不在名单（否则 D-O4 的 natural 会被误伤）
    assert not is_send_behavior_key("inbox.l2_autosend.deliver_delay.profile")
    assert not is_send_behavior_key("inbox.auto_draft.inbound_merge.enabled")


def test_redline2_validate_rejects_a_class_send_behavior_feature():
    import src.utils.feature_registry as fr

    bad = fr.Feature(key="inbox.work_schedule.enabled", cls="A", slug="x_ws",
                     note="test", baseline=True, show=False)
    orig = fr.FEATURES
    try:
        fr.FEATURES = tuple(f for f in orig if f.key != bad.key) + (bad,)
        with pytest.raises(ValueError, match="红线②"):
            fr._validate()
    finally:
        fr.FEATURES = orig


def test_redline3_timezone_keys_never_baseline_to_server_local():
    import src.utils.feature_registry as fr

    assert fr.is_timezone_key("inbox.work_schedule.timezone")
    assert fr.is_timezone_key("companion.proactive_care.tz")
    assert not fr.is_timezone_key("inbox.work_schedule.enabled")
    assert not [f.key for f in fr.FEATURES if f.cls == "A" and fr.is_timezone_key(f.key)]
    orig = fr.FEATURES
    try:
        # A 类时区键（用不在红线②名单里的路径，确保是红线③在拒）：拒
        fr.FEATURES = orig + (fr.Feature(key="contacts.timezone", cls="A",
                                         slug="x_tz", note="t", baseline="Asia/Shanghai",
                                         show=False),)
        with pytest.raises(ValueError, match="红线③"):
            fr._validate()
        # B 类时区键 baseline 写成本机哨兵：拒
        fr.FEATURES = orig + (fr.Feature(key="contacts.timezone", cls="B",
                                         slug="x_tz", note="t", baseline="local", show=False),)
        with pytest.raises(ValueError, match="红线③"):
            fr._validate()
    finally:
        fr.FEATURES = orig


def test_seed_has_work_schedule_and_send_gate_off_and_profile_llm_on():
    seed = yaml.safe_load(DESKTOP_MIN.read_text(encoding="utf-8"))
    assert _dig(seed, "inbox.work_schedule.enabled") is False
    assert _dig(seed, "inbox.work_schedule.timezone") in (None, "")  # 不预填本机
    assert _dig(seed, "companion_send_gate.enabled") is False
    assert _dig(seed, "companion_send_gate.target_cap") == 300   # 推荐值仍预填
    assert _dig(seed, "companion.goals.profile_llm.enabled") is True


# ── D-Q1 / D-Q2 撤回：只回滚基线原样形态 ─────────────────────────────────────

def _base_1078_with_seed_blocks() -> str:
    """1.0.78 全新安装的 config.yaml：种子把班表 / 额度块原样带进主配置。"""
    cfg = yaml.safe_load(_seed_1077_text())
    cfg.setdefault("inbox", {})["work_schedule"] = dict(_WS_1078)
    cfg["companion_send_gate"] = dict(_SG_1078)
    return yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)


def test_rollback_baseline_inserted_work_schedule_and_send_gate(tmp_path, monkeypatch, caplog):
    """1.0.78 升级机形态：overlay 里躺着 _ensure_baseline 补进去的班表三键 + 主配置种子额度块
    → 两键都回滚为 false、写标记、各一行 WARNING；用户其它显式值一字不动。"""
    overlay = {"companion": {"proactive_care": {"enabled": True}},
               "inbox": {"work_schedule": dict(_WS_1078),
                         "l2_autosend": {"deliver_delay": {"profile": "natural"}}}}
    cm, cfg_path = _mk(tmp_path, monkeypatch, base_text=_base_1078_with_seed_blocks(),
                       overlay=overlay)
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        assert asyncio.run(cm.load()) is True
    msgs = [r.getMessage() for r in caplog.records]
    assert any("[baseline] rollback inbox.work_schedule.enabled reason=D-Q1" in m for m in msgs)
    assert any("[baseline] rollback companion_send_gate.enabled reason=D-Q2" in m for m in msgs)
    assert _dig(cm.config, "inbox.work_schedule.enabled") is False
    assert _dig(cm.config, "companion_send_gate.enabled") is False
    ov = _overlay(cfg_path)
    assert _dig(ov, "inbox.work_schedule.enabled") is False
    assert _dig(ov, "inbox.work_schedule.baseline_rollback") == "1.0.79"
    assert _dig(ov, "companion_send_gate.enabled") is False
    assert _dig(ov, "companion_send_gate.baseline_rollback") == "1.0.79"
    # 建议班次保留（页面预填用），用户显式值不动
    assert _dig(ov, "inbox.work_schedule.default.start") == "08:20"
    assert _dig(ov, "companion.proactive_care.enabled") is True
    # 回读：回滚键单列 rolled_back，changed 为空
    up = [m for m in msgs if "[upgrade] user_flags_preserved=" in m]
    assert up and "changed=[]" in up[-1] and "inbox.work_schedule.enabled" in up[-1]
    # 幂等：再 load 字节不变、不再告警
    before = (cfg_path.parent / "config.local.yaml").read_bytes()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        asyncio.run(cm.load())
    assert (cfg_path.parent / "config.local.yaml").read_bytes() == before
    assert not [r for r in caplog.records if "[baseline] rollback" in r.getMessage()]


@pytest.mark.parametrize("user_ws", [
    # 用户填了时区
    {"enabled": True, "timezone": "America/New_York",
     "default": {"start": "08:20", "end": "01:00"}},
    # 用户改了班次
    {"enabled": True, "default": {"start": "09:00", "end": "23:00"}},
    # 用户显式关
    {"enabled": False, "default": {"start": "08:20", "end": "01:00"}},
    # 用户加了账号覆写
    {"enabled": True, "default": {"start": "08:20", "end": "01:00"},
     "accounts": {"telegram:a": {"start": "10:00", "end": "22:00"}}},
])
def test_rollback_never_touches_user_configured_work_schedule(tmp_path, monkeypatch, caplog, user_ws):
    overlay = {"inbox": {"work_schedule": user_ws},
               "companion_send_gate": {"enabled": True, "target_cap": 50}}
    cm, cfg_path = _mk(tmp_path, monkeypatch, base_text=_seed_1077_text(), overlay=overlay)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        asyncio.run(cm.load())
    assert not [r for r in caplog.records if "[baseline] rollback" in r.getMessage()]
    ov = _overlay(cfg_path)
    assert ov["inbox"]["work_schedule"] == user_ws
    assert ov["companion_send_gate"] == {"enabled": True, "target_cap": 50}
    assert cm.baseline_rollback_patch() == {}


def test_rollback_is_desktop_only(tmp_path, monkeypatch, caplog):
    """服务器实例（无 AITR_DESKTOP_MODE）：1.0.78 基线从未跑过，撤回也不跑。"""
    overlay = {"inbox": {"work_schedule": dict(_WS_1078)}}
    cm, cfg_path = _mk(tmp_path, monkeypatch, base_text=_base_1078_with_seed_blocks(),
                       overlay=overlay, desktop=False)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        asyncio.run(cm.load())
    assert not [r for r in caplog.records if "[baseline] rollback" in r.getMessage()]
    assert _dig(_overlay(cfg_path), "inbox.work_schedule.enabled") is True


def test_rollback_patch_shape_is_pure_and_marker_stops_it(tmp_path, monkeypatch):
    cm, _ = _mk(tmp_path, monkeypatch, base_text=_seed_1077_text(), overlay=None)
    cm.config = {"inbox": {"work_schedule": dict(_WS_1078)}, "companion_send_gate": dict(_SG_1078)}
    plan = cm.baseline_rollback_patch()
    assert set(plan) == {"inbox.work_schedule.enabled", "companion_send_gate.enabled"}
    assert plan["inbox.work_schedule.enabled"]["patch"] == {
        "inbox": {"work_schedule": {"enabled": False, "baseline_rollback": "1.0.79"}}}
    assert plan["companion_send_gate.enabled"]["reason"] == "D-Q2"
    cm.config["inbox"]["work_schedule"]["baseline_rollback"] = "1.0.79"
    assert "inbox.work_schedule.enabled" not in cm.baseline_rollback_patch()


def test_factory_defaults_banner_logs_gate_state(tmp_path, monkeypatch, caplog):
    cm, _ = _mk(tmp_path, monkeypatch, base_text=_seed_1077_text(), overlay=None)
    cm.config = {"inbox": {"work_schedule": {"enabled": False}},
                 "companion_send_gate": {"enabled": False, "target_cap": 300},
                 "companion": {"goals": {"profile_llm": {"enabled": True}}},
                 "memory": {"extract": {"use_llm": True}}}
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        info = cm.log_factory_defaults_banner()
    assert info["send_gate.enabled"] is False and info["goals.profile_llm"] is True
    line = [r.getMessage() for r in caplog.records if "[baseline] work_schedule.enabled=" in r.getMessage()]
    assert line and "goals.profile_llm=True memory.extract.use_llm=True" in line[0]


# ── D-Q2：额度永不限制人工 ───────────────────────────────────────────────────

def test_send_gate_manual_never_blocked_by_daily_cap():
    from src.skills.companion_send_gate import evaluate, gate_decision

    sig = {"sends_today": 999, "age_days": 400}
    auto = gate_decision(sig, target_cap=15, origin="auto")
    manual = gate_decision(sig, target_cap=15, origin="manual")
    assert auto["allowed"] is False and auto["reason"] == "daily_cap"
    assert manual["allowed"] is True and manual["reason"] == "ok"
    # 走 evaluate（config 驱动）同口径
    cfg = {"companion_send_gate": {"enabled": True, "target_cap": 15}}
    assert evaluate(sig, cfg, origin="auto")["allowed"] is False
    assert evaluate(sig, cfg, origin="manual")["allowed"] is True
    # 封号仍拦人工（物理事实，不是额度）
    assert gate_decision({"sends_today": 0, "banned": True}, origin="manual")["reason"] == "banned"
