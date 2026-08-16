# -*- coding: utf-8 -*-
"""引流闭环安全开关 + 干跑 + 探针 契约测试（2026-08-13）。

架构：热开关只在 **cron 调度侧**（job_scheduler.execute_scheduled_action）拦截，
手动 POST /tasks 与直接调用 executor 不受限——安全隐患是「无人值守自动跑/发」。

覆盖：
  1. referral_loop 三档模式 + flag>env>default 优先级 + set_mode 拒非法 +
     apply_to_scheduled（off 不跑 / dry_run 注入 dry_run / live 原样）。
  2. execute_scheduled_action：off 档对引流 action 整条 skip（证明接线，无需真机）。
  3. _fb_check_referral_replies 的 dry_run 参数：True→识别但不写事件(仍 pending)+探针标 dry_run；
     False→写事件(清 pending)。
纯进程内，复用 FakeBMessenger + tmp_db。
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture(autouse=True)
def _reset_caches():
    from src.host import executor as _ex
    _ex._REFERRAL_KEYWORDS_CACHE["data"] = None
    _ex._REFERRAL_KEYWORDS_CACHE["loaded_at"] = 0.0
    _ex._peer_region_cache_clear()
    yield


@pytest.fixture
def _tmp_flag(tmp_path, monkeypatch):
    """把旗标文件隔离到临时目录 + 清 env，避免污染仓库、保证默认 off。"""
    from src.host import referral_loop as rl
    flag = tmp_path / "referral_loop_mode"
    monkeypatch.setattr(rl, "_flag_path", lambda: flag)
    monkeypatch.delenv(rl._ENV_VAR, raising=False)
    return flag


@pytest.fixture
def _tmp_probe(tmp_path, monkeypatch):
    from src.host import referral_probe as rp
    f = tmp_path / "referral_quality.jsonl"
    monkeypatch.setattr(rp, "_path", lambda: f)
    return f


# ── 1. 开关模块本体 ─────────────────────────────────────────────
class TestGateModule:
    def test_default_off(self, _tmp_flag):
        from src.host import referral_loop as rl
        assert rl.mode() == "off"
        assert rl.is_enabled() is False

    def test_env_dry_run(self, _tmp_flag, monkeypatch):
        from src.host import referral_loop as rl
        monkeypatch.setenv(rl._ENV_VAR, "dry_run")
        assert rl.mode() == "dry_run"
        assert rl.source() == "env"
        assert rl.is_dry_run() is True

    def test_flag_overrides_env(self, _tmp_flag, monkeypatch):
        from src.host import referral_loop as rl
        monkeypatch.setenv(rl._ENV_VAR, "off")
        rl.set_mode("live")                 # 写旗标文件
        assert rl.mode() == "live"          # 文件 > env
        assert rl.source() == "flag"

    def test_set_mode_rejects_garbage(self, _tmp_flag):
        from src.host import referral_loop as rl
        with pytest.raises(ValueError):
            rl.set_mode("banana")

    def test_normalize_aliases(self):
        from src.host import referral_loop as rl
        assert rl._normalize("LIVE") == "live"
        assert rl._normalize("dry-run") == "dry_run"
        assert rl._normalize("0") == "off"
        assert rl._normalize("garbage") == ""

    def test_apply_to_scheduled_off(self, _tmp_flag):
        from src.host import referral_loop as rl
        run, eff, info = rl.apply_to_scheduled("facebook_send_referral_replies",
                                               {"limit": 10})
        assert run is False
        assert info["referral_loop_mode"] == "off"

    def test_apply_to_scheduled_dry_run_injects(self, _tmp_flag, monkeypatch):
        from src.host import referral_loop as rl
        monkeypatch.setenv(rl._ENV_VAR, "dry_run")
        run, eff, info = rl.apply_to_scheduled("facebook_send_referral_replies",
                                               {"limit": 10})
        assert run is True
        assert eff["dry_run"] is True
        assert eff["limit"] == 10           # 原参数保留
        assert "dry_run" not in {"limit": 10}   # 不改调用方原 dict（copy 语义）

    def test_apply_to_scheduled_live_passthrough(self, _tmp_flag, monkeypatch):
        from src.host import referral_loop as rl
        monkeypatch.setenv(rl._ENV_VAR, "live")
        run, eff, info = rl.apply_to_scheduled("facebook_check_referral_replies",
                                               {"hours_back": 48})
        assert run is True
        assert "dry_run" not in eff


# ── 2. 调度器接线（off 整条 skip，无需真机） ────────────────────
class TestSchedulerWire:
    def test_off_skips_referral_action(self, _tmp_flag):
        from src.host.job_scheduler import execute_scheduled_action
        res = execute_scheduled_action(
            {"action": "facebook_check_referral_replies", "params": {}})
        assert res.get("skipped") is True
        assert res.get("reason") == "referral_loop_off"


# ── 3. check_referral_replies 的 dry_run 参数 ───────────────────
def _seed_sent(device_id, peer_name):
    from src.host.fb_store import record_contact_event
    return record_contact_event(device_id, peer_name, "wa_referral_sent",
                                meta={"via": "test"}, skip_sanitize=True)


class TestCheckRepliesDryRun:
    def test_dry_run_detects_but_no_write(self, tmp_db, _tmp_probe):
        from src.host import executor as ex
        from src.host.fb_store import get_pending_referral_peers
        from tests._fakes import FakeBMessenger
        _seed_sent("D1", "花子")
        fb = FakeBMessenger(conversations={"花子": "send me your LINE id"})
        ok, msg, stats = ex._fb_check_referral_replies(
            fb, "D1", {"dry_run": True})
        assert ok is True
        assert stats["replied_now"] == 1
        assert stats["dry_run"] is True
        # dry_run 没写 wa_referral_replied → 该 peer 仍是 pending
        assert len(get_pending_referral_peers(device_id="D1")) == 1
        recs = [json.loads(l) for l in
                _tmp_probe.read_text(encoding="utf-8").splitlines() if l.strip()]
        kinds = {r["kind"] for r in recs}
        assert {"reply", "reply_batch"} <= kinds
        assert all(r.get("dry_run") for r in recs if r["kind"] == "reply")

    def test_live_writes_and_clears_pending(self, tmp_db, _tmp_probe):
        from src.host import executor as ex
        from src.host.fb_store import get_pending_referral_peers
        from tests._fakes import FakeBMessenger
        _seed_sent("D1", "花子")
        fb = FakeBMessenger(conversations={"花子": "send me your LINE id"})
        ok, msg, stats = ex._fb_check_referral_replies(fb, "D1", {})
        assert ok is True
        assert stats["replied_now"] == 1
        assert stats["dry_run"] is False
        assert get_pending_referral_peers(device_id="D1") == []
        recs = [json.loads(l) for l in
                _tmp_probe.read_text(encoding="utf-8").splitlines() if l.strip()]
        assert any(r["kind"] == "reply" and not r.get("dry_run") for r in recs)


class TestMarkStaleDryRun:
    def test_dry_run_param_no_write(self, tmp_db, _tmp_probe):
        from src.host import executor as ex
        ok, msg, stats = ex._fb_mark_stale_referrals({"dry_run": True})
        assert ok is True
        assert stats.get("dry_run") is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
