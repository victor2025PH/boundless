"""bootstrap_contacts_subsystem 单元测试。"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.contacts import bootstrap_contacts_subsystem


REPO_CONFIG = Path(__file__).resolve().parent.parent / "config"


class _FakeConfig:
    def __init__(self, cfg: dict):
        self.config = cfg


@pytest.fixture
def cfg_dir(tmp_path):
    # 复制必要的 yaml 到 tmp 目录（HandoffRenderer / Compliance 需要）
    d = tmp_path / "config"
    d.mkdir()
    shutil.copy(REPO_CONFIG / "handoff_scripts.yaml", d / "handoff_scripts.yaml")
    shutil.copy(REPO_CONFIG / "handoff_compliance.yaml", d / "handoff_compliance.yaml")
    yield d


class TestFeatureFlag:
    def test_disabled_returns_none(self, cfg_dir):
        cfg = _FakeConfig({"contacts": {"enabled": False}})
        assert bootstrap_contacts_subsystem(cfg, cfg_dir) is None

    def test_missing_section_returns_none(self, cfg_dir):
        cfg = _FakeConfig({})
        assert bootstrap_contacts_subsystem(cfg, cfg_dir) is None

    def test_none_config_returns_none(self, cfg_dir):
        assert bootstrap_contacts_subsystem(None, cfg_dir) is None


class TestEnabled:
    def test_minimal_enabled(self, cfg_dir):
        cfg = _FakeConfig({"contacts": {"enabled": True}})
        sub = bootstrap_contacts_subsystem(cfg, cfg_dir)
        assert sub is not None
        assert sub.store is not None
        assert sub.gateway is not None
        assert sub.hooks is not None
        # 可选服务全部就位（yaml 都能读）
        assert sub.renderer is not None
        assert sub.compliance is not None
        assert sub.limiter is not None
        assert sub.intimacy_engine is not None
        assert sub.readiness_scorer is not None
        assert sub.reactivation is not None
        sub.close()

    def test_db_created(self, cfg_dir):
        cfg = _FakeConfig({"contacts": {"enabled": True}})
        sub = bootstrap_contacts_subsystem(cfg, cfg_dir)
        assert sub is not None
        # contacts.db 默认建到 cfg_dir 下
        db_files = list(cfg_dir.glob("contacts.db*"))
        assert any(f.name.startswith("contacts.db") for f in db_files)
        sub.close()

    def test_config_values_applied(self, cfg_dir):
        cfg = _FakeConfig({"contacts": {
            "enabled": True,
            "daily_cap": 7,
            "global_cap": 50,
            "token_ttl_hours": 48,
            "readiness_threshold": 60,
            "line_ids_by_account": {"acc-A": "@custom_line"},
        }})
        sub = bootstrap_contacts_subsystem(cfg, cfg_dir)
        # limiter 的 cap
        assert sub.limiter._daily_cap == 7
        assert sub.limiter._global_cap == 50
        # token ttl
        assert sub.handoff_svc._ttl == 48 * 3600
        # readiness threshold
        assert sub.readiness_scorer._threshold == 60.0
        # line_id provider 正确
        assert sub.gateway._line_id_provider("acc-A") == "@custom_line"
        assert sub.gateway._line_id_provider("unknown_acc") == "@our_line"
        sub.close()


class TestDictStyleConfig:
    def test_plain_dict_works(self, cfg_dir):
        # 直接传 dict 也能用
        sub = bootstrap_contacts_subsystem(
            {"contacts": {"enabled": True}}, cfg_dir,
        )
        assert sub is not None
        sub.close()


# ── 精简档（lite，实施49 P0-2 客户装机档）─────────────────────────────────────
# 背景：右栏「跨平台档案」卡整条链随包，只有总开关默认关 → 客户看到一张报错的卡。
# 拍板结果＝开精简档：本地库 + 档案读写/AI 注入（纯软件）随包交付，而这个子系统真正
# 的重运行时（三条周期任务 / RPA 逐条记账 hooks / Mobile Bridge 每 15s 打本机 18080
# 手机 rig）一律不启动——客户机上没有那套服务，起了只会刷失败日志、无声长库。
class TestLiteMode:
    def test_default_mode_is_full(self, cfg_dir):
        """缺 mode 键＝full。存量部署（生产实例/内测坐席）的 config 根本没这个键，
        默认成 lite 会把它们正在用的衰减/KPI/hooks 静默关掉＝配置解析越权。"""
        sub = bootstrap_contacts_subsystem({"contacts": {"enabled": True}}, cfg_dir)
        assert sub.mode == "full"
        assert sub.is_lite is False
        assert sub.heavy_integrations_enabled() is True
        sub.close()

    @pytest.mark.parametrize("raw", ["FULL", "", "  ", "nonsense", None, 3])
    def test_invalid_mode_falls_back_to_full(self, cfg_dir, raw):
        sub = bootstrap_contacts_subsystem(
            {"contacts": {"enabled": True, "mode": raw}}, cfg_dir)
        assert sub.is_lite is False, f"非法档位 {raw!r} 必须回落 full（不猜环境）"
        sub.close()

    def test_lite_keeps_store_and_origin_chain(self, cfg_dir):
        """lite 的全部价值＝store 与档案链照常在（否则右栏卡还是报错）。"""
        sub = bootstrap_contacts_subsystem(
            {"contacts": {"enabled": True, "mode": "LiTe"}}, cfg_dir)
        assert sub.mode == "lite" and sub.is_lite is True
        assert sub.store is not None      # 跨平台档案读写的落点
        assert sub.gateway is not None
        sub.close()

    def test_lite_starts_no_background_tasks(self, cfg_dir):
        """三条周期任务（衰减/KPI 告警/intimacy 物化）在 lite 档一条都不起。

        显式把间隔配成非零仍不起——判据是档位，不是「用户忘了关」。
        """
        sub = bootstrap_contacts_subsystem({"contacts": {
            "enabled": True, "mode": "lite",
            "decay_interval_hours": 1,
            "kpi_alert_interval_minutes": 5,
            "intimacy_refresh_interval_minutes": 60,
        }}, cfg_dir)
        sub.start_background_tasks()
        assert sub._bg_tasks == [], "lite 档不得起任何 contacts 周期任务"
        sub.close()

    def test_lite_disables_rpa_hooks_even_if_configured_true(self, cfg_dir):
        """hooks 会在每条入站消息上建 journey/推进漏斗（运营记账）。
        lite 恒 False，且显式 true 也压不过档位——否则客户库会无声长大。"""
        sub = bootstrap_contacts_subsystem({"contacts": {
            "enabled": True, "mode": "lite",
            "rpa_hooks": {"telegram": True, "messenger": True, "line": True},
        }}, cfg_dir)
        for ch in ("telegram", "messenger", "line", "whatsapp"):
            assert sub.is_rpa_hook_enabled(ch) is False, ch
        sub.close()

    def test_full_mode_hooks_and_tasks_unchanged(self, cfg_dir):
        """反向钉住：full 档行为与历史一致（本批改动不得偷偷改运营部署）。"""
        sub = bootstrap_contacts_subsystem({"contacts": {
            "enabled": True, "mode": "full",
            "rpa_hooks": {"line": False},
        }}, cfg_dir)
        assert sub.is_rpa_hook_enabled("messenger") is True   # 未配置=默认接
        assert sub.is_rpa_hook_enabled("line") is False       # 显式关仍尊重
        assert sub.heavy_integrations_enabled() is True
        sub.close()


def test_desktop_seed_delivers_lite():
    """客户桌面种子必须是 lite、内部种子必须是 full。

    两个种子写反＝客户机上起手机 rig 轮询 / 内部坐席丢掉运营记账，且两者都不报错。
    """
    import yaml

    seed = yaml.safe_load((REPO_CONFIG / "config.desktop.min.yaml").read_text("utf-8"))
    c = (seed or {}).get("contacts") or {}
    assert c.get("enabled") is True
    assert c.get("mode") == "lite"
    assert ((c.get("origin_profile") or {}).get("enabled")) is True

    internal = yaml.safe_load(
        (REPO_CONFIG / "config.desktop.internal.yaml").read_text("utf-8"))
    assert ((internal or {}).get("contacts") or {}).get("mode") == "full", (
        "内部/坐席包种子必须显式 full——否则存量内部安装会被产品基线补齐补成 lite")
