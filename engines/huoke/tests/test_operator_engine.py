# -*- coding: utf-8 -*-
"""AI 操盘手引擎契约测试（2026-08-16 P4 MVP）。

四块：
  1. decide() 纯规则红绿双向——每条规则一正一反，规则序即优先级
  2. 审计落库 round-trip + 日频控计数只认「非 dry_run 的 execute」
  3. tick() 回路——双闸/演习不执行/真执行落 run_id/下发失败降级不崩
  4. 与真实链库（config/task_chains.yaml）的集成咬合
"""
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.host.operator_engine import (  # noqa: E402
    DeviceSnapshot, Decision, decide, record_decision, get_decisions,
    _launched_today, _recent_fail_streak,
)
import src.host.operator_engine as op  # noqa: E402


# ── 夹具 ──────────────────────────────────────────────────────────────

def _chains():
    """合成链库：facebook 三级漏斗 + 一条高危 + tiktok 一条。
    funnel_stage 只用合法词表（见 TestVocabularyAlignment）。"""
    return [
        {"chain_id": "fb_warm", "platform": "facebook",
         "funnel_stage": "warmup", "risk_level": "low"},
        {"chain_id": "fb_harvest", "platform": "facebook",
         "funnel_stage": "followup", "risk_level": "medium"},
        {"chain_id": "fb_grow", "platform": "facebook",
         "funnel_stage": "discover", "risk_level": "medium"},
        {"chain_id": "fb_full", "platform": "facebook",
         "funnel_stage": "full", "risk_level": "high"},
        {"chain_id": "tt_warm", "platform": "tiktok",
         "funnel_stage": "warmup", "risk_level": "low"},
    ]


def _goal(**kw):
    g = {"id": "g1", "platform": "facebook", "device_ids": ["d1"],
         "preferred_chains": [], "max_chains_per_device_per_day": 3,
         "allow_high_risk": False}
    g.update(kw)
    return g


def _snap(**kw):
    s = dict(device_id="d1", platform="facebook", online=True,
             phase="active", recovering=False, busy=False,
             launched_today=0, responded_leads=0, recent_fail_streak=0)
    s.update(kw)
    return DeviceSnapshot(**s)


# ── 1. 纯规则红绿双向 ────────────────────────────────────────────────

class TestRules:
    def test_r1_offline_skip(self):
        d = decide(_snap(online=False), _goal(), _chains())
        assert (d.action, d.rule_id) == ("skip", "R1")

    def test_r2_busy_hold(self):
        d = decide(_snap(busy=True), _goal(), _chains())
        assert (d.action, d.rule_id) == ("hold", "R2")

    def test_r2b_fail_backoff_holds(self):
        d = decide(_snap(recent_fail_streak=3), _goal(), _chains())
        assert (d.action, d.rule_id) == ("hold", "R2b")
        assert "连续" in d.reason

    def test_r2b_under_threshold_proceeds(self):
        d = decide(_snap(recent_fail_streak=2), _goal(), _chains())
        assert d.action == "execute"          # 没到阈值照常投

    def test_r2b_custom_threshold(self):
        d = decide(_snap(recent_fail_streak=2),
                   _goal(fail_backoff_threshold=2), _chains())
        assert (d.action, d.rule_id) == ("hold", "R2b")

    def test_r2b_disabled_when_threshold_zero(self):
        d = decide(_snap(recent_fail_streak=9),
                   _goal(fail_backoff_threshold=0), _chains())
        assert d.action == "execute"          # 阈值 0 = 关闭退避

    def test_r2_busy_beats_backoff(self):
        d = decide(_snap(busy=True, recent_fail_streak=9), _goal(), _chains())
        assert d.rule_id == "R2"              # 忙态优先于退避

    def test_r3_daily_cap_hold(self):
        d = decide(_snap(launched_today=3), _goal(), _chains())
        assert (d.action, d.rule_id) == ("hold", "R3")

    def test_r3_under_cap_passes(self):
        d = decide(_snap(launched_today=2), _goal(), _chains())
        assert d.action == "execute"          # 没到上限就继续往下走

    def test_r4_recovering_low_risk_warmup(self):
        d = decide(_snap(recovering=True), _goal(), _chains())
        assert (d.action, d.rule_id, d.chain_id) == ("execute", "R4", "fb_warm")

    def test_r4_recovering_no_safe_chain_holds(self):
        chains = [c for c in _chains() if c["chain_id"] != "fb_warm"]
        d = decide(_snap(recovering=True), _goal(), chains)
        assert (d.action, d.rule_id) == ("hold", "R4")

    def test_r5_cold_start_warmup(self):
        d = decide(_snap(phase="cold_start"), _goal(), _chains())
        assert (d.action, d.rule_id, d.chain_id) == ("execute", "R5", "fb_warm")

    def test_r5_high_risk_warmup_blocked(self):
        chains = [{"chain_id": "fb_warm_hi", "platform": "facebook",
                   "funnel_stage": "warmup", "risk_level": "high"}]
        d = decide(_snap(phase="cold_start"), _goal(), chains)
        assert (d.action, d.rule_id) == ("blocked", "R5")
        assert d.chain_id == "fb_warm_hi"

    def test_r6_harvest_beats_growth(self):
        d = decide(_snap(responded_leads=5), _goal(), _chains())
        assert (d.action, d.rule_id, d.chain_id) == ("execute", "R6", "fb_harvest")

    def test_r6_no_harvest_chain_falls_to_r7(self):
        chains = [c for c in _chains() if c["chain_id"] != "fb_harvest"]
        d = decide(_snap(responded_leads=5), _goal(), chains)
        assert (d.action, d.rule_id, d.chain_id) == ("execute", "R7", "fb_grow")

    def test_r7_default_growth(self):
        d = decide(_snap(), _goal(), _chains())
        assert (d.action, d.rule_id, d.chain_id) == ("execute", "R7", "fb_grow")

    def test_r7_high_risk_blocked_without_grant(self):
        chains = [c for c in _chains() if c["chain_id"] != "fb_grow"]
        d = decide(_snap(), _goal(), chains)
        assert (d.action, d.rule_id, d.chain_id) == ("blocked", "R7", "fb_full")

    def test_r7_high_risk_allowed_with_grant(self):
        chains = [c for c in _chains() if c["chain_id"] != "fb_grow"]
        d = decide(_snap(), _goal(allow_high_risk=True), chains)
        assert (d.action, d.chain_id) == ("execute", "fb_full")

    def test_preferred_chain_order_wins(self):
        # fb_full 高危被拦，preferred 把 fb_grow 排后也不影响；
        # 换低危场景验证偏好序：造两条同级链
        chains = _chains() + [{"chain_id": "fb_grow2", "platform": "facebook",
                               "funnel_stage": "discover",
                               "risk_level": "medium"}]
        d = decide(_snap(), _goal(preferred_chains=["fb_grow2"]), chains)
        assert d.chain_id == "fb_grow2"

    def test_platform_isolation(self):
        """tiktok 目标绝不会选到 facebook 的链。"""
        d = decide(_snap(platform="tiktok", phase="cold_start"),
                   _goal(platform="tiktok"), _chains())
        assert d.chain_id == "tt_warm"


# ── 2. 审计落库 ──────────────────────────────────────────────────────

class TestAudit:
    @pytest.fixture(autouse=True)
    def _fresh_table(self, tmp_db):
        op._table_ensured = False
        yield

    def test_roundtrip(self):
        snap = _snap()
        record_decision("g1", snap, Decision("hold", "R2", "忙"), True)
        rows = get_decisions(limit=10)
        assert len(rows) == 1
        r = rows[0]
        assert r["goal_id"] == "g1"
        assert r["action"] == "hold"
        assert r["rule_id"] == "R2"
        assert r["dry_run"] == 1
        assert '"device_id": "d1"' in r["snapshot"]

    def test_launched_today_counts_only_real_executes(self):
        snap = _snap()
        # dry_run 的 execute 不计数
        record_decision("g1", snap, Decision("execute", "R7", "拓新", "c1"), True)
        # hold 不计数
        record_decision("g1", snap, Decision("hold", "R2", "忙"), False)
        assert _launched_today("d1") == 0
        # 真实 execute 计数
        record_decision("g1", snap, Decision("execute", "R7", "拓新", "c1"),
                        False, run_id="r1")
        record_decision("g1", snap, Decision("execute", "R7", "拓新", "c2"),
                        False, run_id="r2")
        assert _launched_today("d1") == 2
        assert _launched_today("d2") == 0

    def test_device_filter(self):
        record_decision("g1", _snap(), Decision("hold", "R2", "忙"), True)
        record_decision("g1", _snap(device_id="d9"),
                        Decision("skip", "R1", "离线"), True)
        assert len(get_decisions(device_id="d9")) == 1

    def _put_chain(self, run_id, status):
        from src.host.database import get_conn
        with get_conn() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS chain_runs (run_id TEXT PRIMARY "
                "KEY, chain_id TEXT, device_id TEXT, status TEXT, data TEXT, "
                "created_at TEXT, finished_at TEXT)")
            conn.execute(
                "INSERT OR REPLACE INTO chain_runs (run_id, status) "
                "VALUES (?, ?)", (run_id, status))

    def _put_exec(self, device_id, run_id):
        record_decision("g1", _snap(device_id=device_id),
                        Decision("execute", "R5", "养号", "c1"),
                        False, run_id=run_id)

    def test_fail_streak_counts_consecutive_aborts(self):
        # 时间序（旧→新）：completed, aborted, aborted → 连击=2（成功截断）
        for i, st in enumerate(["completed", "aborted", "aborted"]):
            rid = f"run{i}"
            self._put_exec("d1", rid)
            self._put_chain(rid, st)
        assert _recent_fail_streak("d1") == 2

    def test_fail_streak_resets_on_success(self):
        # aborted, aborted, completed(最新) → 最新成功 = 连击 0（自动解退避）
        for i, st in enumerate(["aborted", "aborted", "completed"]):
            rid = f"run{i}"
            self._put_exec("d1", rid)
            self._put_chain(rid, st)
        assert _recent_fail_streak("d1") == 0

    def test_fail_streak_running_is_skipped(self):
        # aborted, aborted, running(最新) → running 不计不断 → 连击=2
        for i, st in enumerate(["aborted", "aborted", "running"]):
            rid = f"run{i}"
            self._put_exec("d1", rid)
            self._put_chain(rid, st)
        assert _recent_fail_streak("d1") == 2

    def test_fail_streak_ignores_dry_run(self):
        # dry_run 的 execute 不进反馈分母
        record_decision("g1", _snap(), Decision("execute", "R5", "养号", "c"),
                        True, run_id="dryrun")
        self._put_chain("dryrun", "aborted")
        assert _recent_fail_streak("d1") == 0


# ── 3. tick 回路 ─────────────────────────────────────────────────────

class TestTick:
    @pytest.fixture(autouse=True)
    def _env(self, tmp_db, tmp_path, monkeypatch):
        op._table_ensured = False
        self._cfg_path = tmp_path / "operator_goals.yaml"
        monkeypatch.setattr(op, "_CFG_PATH", self._cfg_path)
        op._cfg_cache = None
        op._cfg_cache_mtime = -1.0
        # 合成链库 + 打桩感知
        import src.host.task_chain as tc
        monkeypatch.setattr(tc, "list_chains", lambda: _chains())
        monkeypatch.setattr(op, "_perceive",
                            lambda did, plat: _snap(device_id=did,
                                                    platform=plat))
        self._launched = []
        monkeypatch.setattr(op, "_launch_chain",
                            lambda cid, did: (self._launched.append((cid, did))
                                              or "run-xyz"))
        yield

    def _write_cfg(self, enabled, dry_run, goals=None):
        cfg = {"enabled": enabled, "dry_run": dry_run,
               "goals": goals if goals is not None else [_goal()]}
        self._cfg_path.write_text(
            yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
        op._cfg_cache = None
        op._cfg_cache_mtime = -1.0

    def test_disabled_is_inert(self):
        self._write_cfg(enabled=False, dry_run=False)
        out = op.tick()
        assert out["enabled"] is False
        assert out["decisions"] == []
        assert self._launched == []
        assert get_decisions() == []          # 关着连审计都不写

    def test_missing_config_is_inert(self):
        out = op.tick()                        # 文件不存在
        assert out["enabled"] is False
        assert self._launched == []

    def test_dry_run_decides_but_never_launches(self):
        self._write_cfg(enabled=True, dry_run=True)
        out = op.tick()
        assert out["dry_run"] is True
        assert len(out["decisions"]) == 1
        assert out["decisions"][0]["action"] == "execute"
        assert out["decisions"][0]["run_id"] == ""
        assert self._launched == []            # 演习绝不下发
        rows = get_decisions()
        assert len(rows) == 1 and rows[0]["dry_run"] == 1

    def test_real_run_launches_and_audits_run_id(self):
        self._write_cfg(enabled=True, dry_run=False)
        out = op.tick()
        assert self._launched == [("fb_grow", "d1")]
        assert out["decisions"][0]["run_id"] == "run-xyz"
        rows = get_decisions()
        assert rows[0]["run_id"] == "run-xyz" and rows[0]["dry_run"] == 0

    def test_explicit_dry_run_overrides_config(self):
        self._write_cfg(enabled=True, dry_run=False)
        out = op.tick(dry_run=True)
        assert out["dry_run"] is True
        assert self._launched == []

    def test_launch_failure_degrades_to_hold(self, monkeypatch):
        self._write_cfg(enabled=True, dry_run=False)

        def _boom(cid, did):
            raise RuntimeError("设备拒绝")
        monkeypatch.setattr(op, "_launch_chain", _boom)
        out = op.tick()
        assert out["decisions"][0]["action"] == "hold"
        assert "下发失败" in out["decisions"][0]["reason"]
        rows = get_decisions()
        assert rows[0]["action"] == "hold"     # 失败也留痕

    def test_r2b_backoff_fires_alert(self, monkeypatch):
        """P4.5：R2b 退避决策必须触发告警通路（其余决策绝不触发）。"""
        self._write_cfg(enabled=True, dry_run=True)
        fired = []
        monkeypatch.setattr(op, "_alert_backoff",
                            lambda did, reason: fired.append((did, reason)))
        # 正向：连续失败达阈值 → R2b → 告警
        monkeypatch.setattr(op, "_perceive",
                            lambda did, plat: _snap(device_id=did, platform=plat,
                                                    recent_fail_streak=3))
        out = op.tick()
        assert out["decisions"][0]["rule_id"] == "R2b"
        assert fired == [("d1", out["decisions"][0]["reason"])]
        # 反向：正常 execute 决策不触发告警
        fired.clear()
        monkeypatch.setattr(op, "_perceive",
                            lambda did, plat: _snap(device_id=did, platform=plat))
        out = op.tick()
        assert out["decisions"][0]["action"] == "execute"
        assert fired == []


# ── 3.4 自带时钟 ─────────────────────────────────────────────────────

class TestClock:
    def test_interval_floor_5min(self, tmp_path, monkeypatch):
        p = tmp_path / "g.yaml"
        monkeypatch.setattr(op, "_CFG_PATH", p)
        p.write_text("tick_interval_minutes: 1\n", encoding="utf-8")
        assert op._clock_interval_s() == 300      # 手滑打太密被下限拦住
        p.write_text("tick_interval_minutes: 45\n", encoding="utf-8")
        assert op._clock_interval_s() == 2700
        p.unlink()
        assert op._clock_interval_s() == 1800     # 缺文件回缺省 30 分钟

    def test_start_is_idempotent(self):
        op.start_operator_clock()
        t1 = op._clock_thread
        op.start_operator_clock()
        assert op._clock_thread is t1             # 不会起第二条线程
        assert t1.daemon and t1.is_alive()


# ── 3.5 词表跨套件对齐（真机灰度日实锤的漂移点） ─────────────────────

class TestVocabularyAlignment:
    """引擎漏斗词表 ↔ 链模板契约合法词表 必须严格互覆盖。

    首版引擎写了不存在的 "inbox"/"discovery"，合成测试自洽看不出，
    与真实链库咬合才照出（tt_comment_engage 永远选不中）。此测钉死：
    并集 == 合法词表——缺词=有链永远选不中，多词=引擎里有死词。
    """

    def test_engine_stages_exactly_cover_legal_vocab(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "chain_templates_contract",
            Path(__file__).with_name("test_chain_templates.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _FUNNEL_STAGES = mod._FUNNEL_STAGES
        engine_union = (set(op._WARMUP_STAGES) | set(op._HARVEST_STAGES)
                        | set(op._GROWTH_STAGES))
        assert engine_union == set(_FUNNEL_STAGES), (
            f"缺词(链选不中): {set(_FUNNEL_STAGES) - engine_union}; "
            f"死词(引擎多余): {engine_union - set(_FUNNEL_STAGES)}")


# ── 4. 与真实链库咬合 ────────────────────────────────────────────────

class TestRealChainLibrary:
    """决策引擎必须与 config/task_chains.yaml 的 15 条剧本真实咬合。"""

    def _real_chains(self):
        from src.host.task_chain import list_chains
        return list_chains()

    def test_every_platform_has_warmup_path(self):
        """每个有链的平台，冷启动决策都能找到落点（execute 或 blocked，
        不许 hold『没有养号链』——那说明库有缺口）。"""
        chains = self._real_chains()
        platforms = sorted({c.get("platform") for c in chains
                            if c.get("platform")})
        assert len(platforms) >= 5             # P0 扩库后应覆盖 7 平台
        for plat in platforms:
            d = decide(_snap(platform=plat, phase="cold_start"),
                       _goal(platform=plat), chains)
            has_warmup = any(c.get("funnel_stage") in ("warmup",)
                             for c in chains if c.get("platform") == plat)
            if has_warmup:
                assert d.action in ("execute", "blocked"), \
                    f"{plat} 冷启动决策异常: {d}"
        # 舰队主力平台必须有养号链（真机灰度日实锤：tiktok 曾缺，
        # 4 台冷启动设备会全部 hold 空转）
        warmup_plats = {c.get("platform") for c in chains
                        if c.get("funnel_stage") == "warmup"}
        assert {"tiktok", "facebook"} <= warmup_plats, \
            f"主力平台缺养号链: {warmup_plats}"

    def test_high_risk_never_auto_executes_by_default(self):
        """缺省 goal（allow_high_risk=False）下，决策产出的 execute
        绝不指向 high 风险链——护栏契约。"""
        chains = self._real_chains()
        risk = {c["chain_id"]: c.get("risk_level", "medium") for c in chains}
        platforms = sorted({c.get("platform") for c in chains
                            if c.get("platform")})
        scenarios = [
            dict(phase="cold_start"),
            dict(phase="active", responded_leads=9),
            dict(phase="active"),
            dict(recovering=True),
        ]
        for plat in platforms:
            for sc in scenarios:
                d = decide(_snap(platform=plat, **sc),
                           _goal(platform=plat), chains)
                if d.action == "execute":
                    assert risk.get(d.chain_id) != "high", \
                        f"{plat} {sc} 自动执行了高危链 {d.chain_id}"


class TestDashboardWiring:
    """P4.5 前端接线契约：操盘手页在侧栏/页面容器/loader/脚本四处都必须在位。

    另一条刻意设计也钉死：operator.js 不许出现「扳实弹」类写配置动作——
    实弹开关只在 config/operator_goals.yaml（运维显式动作），网页只读+演习。
    """

    @classmethod
    def setup_class(cls):
        root = Path(__file__).resolve().parents[1]
        from src.host.dashboard import DASHBOARD_HTML
        cls.html = DASHBOARD_HTML
        cls.ov = (root / "src" / "host" / "static" / "js" /
                  "overview.js").read_text(encoding="utf-8")
        cls.opjs = (root / "src" / "host" / "static" / "js" /
                    "operator.js").read_text(encoding="utf-8")

    def test_four_wiring_points(self):
        assert 'data-page="operator"' in self.html, "侧栏缺操盘手菜单项"
        assert 'id="page-operator"' in self.html, "缺页面容器 div"
        assert "'operator':()=>loadOperatorPage()" in self.ov, \
            "_PAGE_LOADERS 缺 operator 映射"
        assert "/static/js/operator.js?v=" in self.html, "缺 operator.js 脚本引入"

    def test_page_js_contract(self):
        assert "function loadOperatorPage" in self.opjs
        assert "'/operator/status'" in self.opjs
        assert "'/operator/tick'" in self.opjs
        # 演习按钮只许 dry_run:true——网页绝不发实弹 tick
        assert "dry_run:true" in self.opjs
        assert "dry_run:false" not in self.opjs
        # 不许在网页上写 operator_goals（实弹开关是运维显式动作）
        for verb in ("'PUT'", "'DELETE'", "operator/config", "operator/enable"):
            assert verb not in self.opjs, f"operator.js 不应出现 {verb}"

    def test_menu_in_auto_group_not_admin_only(self):
        """操盘手端点是 admin/operator 双角色——菜单必须放非 admin-only 组。"""
        idx = self.html.find('data-admin-only="1"')
        assert idx > 0
        assert self.html.find('data-page="operator"') < idx, \
            "操盘手菜单跑进了管理员区（operator 角色会看不见）"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
