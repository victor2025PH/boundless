# -*- coding: utf-8 -*-
"""#142 真发总闸「静默拦截」整改三件套（归 #63 族，2026-09-02）。

守三件事（钧 0902 07:55 报障口径的反面）：

1. **拦截不再静默**：GET /api/unified-inbox/automation 对「会话档=全自动/多选
   且非 telegram」在总闸关闭时带 ``deliver_paused`` 段（含 can_resume 能力位）；
   一键恢复 POST /api/companion/deliver-gate/resume 主管专属、恢复后即真发
   （worker+deliver 都被顺手关过时一并打开，不留半截）。
2. **翻动留痕**：总闸每次开/关经 ``_audit_toggle`` 落
   ``autosend_gate_state.json``（谁/从哪/何时，conversation_settings.source
   同风格），standby split 段回显 ``deliver_flip``/``pause`` 供设置页展示。
3. **应急态自动过期**（默认关）：``pause_auto_resume_hours`` > 0 且关满 N 小时
   → sweep 自动恢复 + 发 ``autosend_gate_alert``；无留痕（yaml 手改）绝不动
   ——自动恢复只撤销被记录的翻动，不替部署决策做主。

回归钉：总闸开着时 automation GET 不带 deliver_paused、split 不带 pause、
sweep 空转——「总闸开时一切行为与现状一致」是 #142 的验收 ③。
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.autosend_gate_state import (
    GATE_PATHS,
    auto_resume_hours,
    gate_flip_snapshot,
    gate_state_path,
    pause_meta,
    record_gate_flip,
    sweep_gate_auto_resume,
)
from src.web.routes.companion_capability_routes import (
    register_companion_capability_routes,
)


def _noop_auth(request: Request):
    return None


class _CM:
    """极简 ConfigManager 假件（与 test_standby_split 同款）。"""

    def __init__(self, tmp_path, config):
        cfg = tmp_path / "config.yaml"
        cfg.write_text("telegram: {enabled: false}\n", encoding="utf-8")
        self.config_path = str(cfg)
        self.config = config
        self.writes = []

    def set_overlay_flag(self, path, value):
        self.writes.append((path, value))
        node = self.config
        parts = str(path).split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = value
        return True, ""


class _Store:
    def __init__(self, modes):
        self._modes = dict(modes)

    def all_automation_modes(self):
        return dict(self._modes)


def _client(tmp_path, *, config=None, modes=None, role="master"):
    cfg = config if config is not None else {
        "inbox": {
            "l2_autosend": {"enabled": True, "deliver": True},
            "auto_draft": {"automation_mode": "auto_ai"},
        },
        "companion_send_gate": {"enabled": True},
    }
    cm = _CM(tmp_path, cfg)
    app = FastAPI()
    from starlette.middleware.sessions import SessionMiddleware

    app.add_middleware(SessionMiddleware, secret_key="t")
    app.state.config_manager = cm
    app.state.inbox_store = _Store(
        modes if modes is not None
        else {"line:a:u1": "auto_ai", "line:a:u2": "review"})
    register_companion_capability_routes(app, api_auth=_noop_auth)

    @app.get("/_login")
    async def _login(request: Request):  # 测试专用：种 session 身份
        request.session["user_id"] = "boss"
        request.session["display_name"] = "钧"
        request.session["role"] = role
        return {"ok": True}

    client = TestClient(app)
    client.get("/_login")
    return client, cm


# ── ① 纯核心：留痕读写 + 暂停元信息 ─────────────────────────────────────────

def test_record_and_snapshot_roundtrip(tmp_path):
    record_gate_flip(tmp_path, path="inbox.l2_autosend.deliver", value=False,
                     actor="钧", source="standby-split:deliver", ts=100.0)
    record_gate_flip(tmp_path, path="inbox.l2_autosend.deliver", value=True,
                     actor="值守", source="inbox_banner:resume", ts=200.0)
    snap = gate_flip_snapshot(tmp_path)
    last = snap["paths"]["inbox.l2_autosend.deliver"]
    assert last["value"] is True and last["actor"] == "值守"
    assert last["source"] == "inbox_banner:resume" and last["ts"] == 200.0
    # history 新→旧
    assert [h["ts"] for h in snap["history"]] == [200.0, 100.0]


def test_record_ignores_non_gate_paths(tmp_path):
    record_gate_flip(tmp_path, path="inbox.auto_draft.automation_mode",
                     value=False, actor="x", source="y")
    assert not gate_state_path(tmp_path).exists()


def test_pause_meta_none_when_gate_open(tmp_path):
    cfg = {"inbox": {"l2_autosend": {"enabled": True, "deliver": True}}}
    assert pause_meta(cfg, tmp_path) is None


def test_pause_meta_carries_reason_actor_and_eta(tmp_path):
    cfg = {"inbox": {"l2_autosend": {
        "enabled": True, "deliver": False, "pause_auto_resume_hours": 2}}}
    record_gate_flip(tmp_path, path="inbox.l2_autosend.deliver", value=False,
                     actor="钧", source="standby:off", ts=1000.0)
    m = pause_meta(cfg, tmp_path)
    assert m["reason"] == "l2_autosend.deliver=false"
    assert m["since"] == 1000.0 and m["actor"] == "钧"
    assert m["source"] == "standby:off"
    assert m["auto_resume_at"] == 1000.0 + 2 * 3600.0


def test_pause_meta_without_record_is_honest_unknown(tmp_path):
    """yaml 手改（无留痕）：暂停照报，但 since=0、不给 auto_resume_at——
    宁可说不知道，不编「谁关的」。"""
    cfg = {"inbox": {"l2_autosend": {
        "enabled": True, "deliver": False, "pause_auto_resume_hours": 2}}}
    m = pause_meta(cfg, tmp_path)
    assert m["reason"] and m["since"] == 0.0
    assert m["actor"] == "" and m["auto_resume_at"] == 0.0


def test_auto_resume_hours_default_off():
    assert auto_resume_hours({}) == 0.0
    assert auto_resume_hours({"inbox": {"l2_autosend": {}}}) == 0.0
    assert auto_resume_hours(
        {"inbox": {"l2_autosend": {"pause_auto_resume_hours": -3}}}) == 0.0


# ── ② 自动过期 sweep（默认关；只撤销有留痕的翻动）─────────────────────────

def _cm_paused(tmp_path, *, hours=0):
    l2 = {"enabled": True, "deliver": False}
    if hours:
        l2["pause_auto_resume_hours"] = hours
    return _CM(tmp_path, {"inbox": {"l2_autosend": l2}})


def test_sweep_noop_when_config_off(tmp_path):
    cm = _cm_paused(tmp_path, hours=0)
    record_gate_flip(tmp_path, path="inbox.l2_autosend.deliver", value=False,
                     actor="钧", source="standby:off", ts=0.0)
    res = sweep_gate_auto_resume(cm, now=10_000_000.0)
    assert res["resumed"] is False and cm.writes == []


def test_sweep_noop_before_deadline(tmp_path):
    cm = _cm_paused(tmp_path, hours=4)
    record_gate_flip(tmp_path, path="inbox.l2_autosend.deliver", value=False,
                     actor="钧", source="standby:off", ts=1000.0)
    res = sweep_gate_auto_resume(cm, now=1000.0 + 3 * 3600.0)
    assert res["resumed"] is False and cm.writes == []


def test_sweep_refuses_without_flip_record(tmp_path):
    """yaml 手改关的闸没有留痕 → 绝不自动打开（不替部署决策做主）。"""
    cm = _cm_paused(tmp_path, hours=1)
    res = sweep_gate_auto_resume(cm, now=10_000_000.0)
    assert res["resumed"] is False and cm.writes == []


def test_sweep_resumes_after_deadline_and_leaves_trail(tmp_path):
    cm = _cm_paused(tmp_path, hours=4)
    record_gate_flip(tmp_path, path="inbox.l2_autosend.deliver", value=False,
                     actor="钧", source="standby:off", ts=1000.0)
    rewired = []
    res = sweep_gate_auto_resume(
        cm, rewire=lambda: rewired.append(1) or {"rewired": True},
        now=1000.0 + 5 * 3600.0)
    assert res["resumed"] is True
    assert res["paths"] == ["inbox.l2_autosend.deliver"]
    assert ("inbox.l2_autosend.deliver", True) in cm.writes
    assert rewired == [1]
    # 恢复动作自身也留痕（actor=system / source=auto_expire）
    last = gate_flip_snapshot(tmp_path)["paths"]["inbox.l2_autosend.deliver"]
    assert last["value"] is True and last["actor"] == "system"
    assert last["source"] == "auto_expire"
    # 幂等：闸已开，再 sweep 空转
    res2 = sweep_gate_auto_resume(cm, now=1000.0 + 6 * 3600.0)
    assert res2["resumed"] is False


def test_sweep_reopens_both_gates_when_both_recorded_off(tmp_path):
    cm = _CM(tmp_path, {"inbox": {"l2_autosend": {
        "enabled": False, "deliver": False, "pause_auto_resume_hours": 1}}})
    for p in GATE_PATHS:
        record_gate_flip(tmp_path, path=p, value=False,
                         actor="钧", source="standby:off", ts=1000.0)
    res = sweep_gate_auto_resume(cm, now=1000.0 + 2 * 3600.0)
    assert res["resumed"] is True
    assert sorted(res["paths"]) == sorted(GATE_PATHS)
    assert cm.config["inbox"]["l2_autosend"]["enabled"] is True
    assert cm.config["inbox"]["l2_autosend"]["deliver"] is True


# ── ②b watchdog 接线（棘轮要求真调用，非源码断言）────────────────────────

def _wd_fake(cm, rewire=None):
    import types
    return types.SimpleNamespace(
        _config_manager=cm,
        _app=types.SimpleNamespace(
            state=types.SimpleNamespace(autosend_rewire=rewire)))


def test_watchdog_check_invokes_sweep_and_resumes(tmp_path):
    from src.inbox.health_watchdog import HealthWatchdog

    cm = _cm_paused(tmp_path, hours=1)
    record_gate_flip(tmp_path, path="inbox.l2_autosend.deliver", value=False,
                     actor="钧", source="standby:off", ts=1000.0)
    rewired = []
    HealthWatchdog._check_autosend_gate_expiry(
        _wd_fake(cm, rewire=lambda: rewired.append(1) or {"rewired": True}),
        now=1000.0 + 2 * 3600.0)
    assert ("inbox.l2_autosend.deliver", True) in cm.writes
    assert rewired == [1]


def test_watchdog_check_silent_when_config_off(tmp_path):
    """默认关（pause_auto_resume_hours 缺席）→ 巡检全程零写入零副作用。"""
    from src.inbox.health_watchdog import HealthWatchdog

    cm = _cm_paused(tmp_path, hours=0)
    record_gate_flip(tmp_path, path="inbox.l2_autosend.deliver", value=False,
                     actor="钧", source="standby:off", ts=0.0)
    HealthWatchdog._check_autosend_gate_expiry(_wd_fake(cm), now=9e9)
    assert cm.writes == []


def test_watchdog_check_no_config_manager_is_safe():
    from src.inbox.health_watchdog import HealthWatchdog

    HealthWatchdog._check_autosend_gate_expiry(_wd_fake(None))   # 不抛即通过


# ── ③ 路由整合：翻动留痕 + split 回显 + 一键恢复 ───────────────────────────

def test_split_toggle_leaves_flip_trail_with_session_actor(tmp_path):
    client, cm = _client(tmp_path)
    d = client.post("/api/companion/standby",
                    json={"set": {"deliver": False}}).json()
    assert d["ok"] is True
    # 留痕文件落在 config 目录，actor 取 session 坐席名（不再一律 web-admin）
    state = json.loads(
        gate_state_path(cm_dir(cm)).read_text(encoding="utf-8"))
    rec = state["paths"]["inbox.l2_autosend.deliver"]
    assert rec["value"] is False and rec["actor"] == "钧"
    assert rec["source"] == "standby-split:deliver"
    # split 段回显留痕 + 暂停元信息（设置页「谁关的」数据源）
    sp = d["split"]
    assert sp["deliver_flip"]["actor"] == "钧"
    assert sp["pause"]["reason"] == "l2_autosend.deliver=false"
    assert sp["pause"]["actor"] == "钧"


def cm_dir(cm):
    from pathlib import Path
    return Path(cm.config_path).parent


def test_get_standby_split_has_no_pause_when_gate_open(tmp_path):
    """回归钉（验收③）：总闸开着时 split 不带 pause，行为与现状一致。"""
    client, _cm = _client(tmp_path)
    sp = client.get("/api/companion/standby").json()["split"]
    assert "pause" not in sp


def test_deliver_gate_resume_requires_supervisor(tmp_path):
    client, cm = _client(tmp_path, role="agent")
    client.post("/api/companion/standby", json={"set": {"deliver": False}})
    r = client.post("/api/companion/deliver-gate/resume", json={})
    assert r.status_code == 403


def test_deliver_gate_resume_flips_back_and_leaves_trail(tmp_path):
    client, cm = _client(tmp_path)
    client.post("/api/companion/standby", json={"set": {"deliver": False}})
    cm.writes.clear()
    d = client.post("/api/companion/deliver-gate/resume", json={}).json()
    assert d["ok"] is True
    assert ("inbox.l2_autosend.deliver", True) in cm.writes
    assert d["split"]["deliver"] is True
    assert "pause" not in d["split"]        # 恢复后暂停元信息消失
    rec = json.loads(gate_state_path(cm_dir(cm)).read_text(
        encoding="utf-8"))["paths"]["inbox.l2_autosend.deliver"]
    assert rec["value"] is True and rec["source"] == "inbox_banner:resume"
    # 幂等：再点一次 → already_on，零写入
    cm.writes.clear()
    d2 = client.post("/api/companion/deliver-gate/resume", json={}).json()
    assert d2["ok"] is True and d2.get("already_on") is True
    assert cm.writes == []


def test_deliver_gate_resume_reopens_worker_too(tmp_path):
    """enabled+deliver 都被关过 → 一键恢复两个都开（恢复后即真发，不留半截）。"""
    cfg = {
        "inbox": {
            "l2_autosend": {"enabled": False, "deliver": False},
            "auto_draft": {"automation_mode": "auto_ai"},
        },
        "companion_send_gate": {"enabled": True},
    }
    client, cm = _client(tmp_path, config=cfg)
    d = client.post("/api/companion/deliver-gate/resume", json={}).json()
    assert d["ok"] is True
    paths = [w[0] for w in cm.writes]
    assert "inbox.l2_autosend.enabled" in paths
    assert "inbox.l2_autosend.deliver" in paths
    assert cm.config["inbox"]["l2_autosend"]["deliver"] is True


# ── ④ automation GET 的 deliver_paused 段 ──────────────────────────────────

def _automation_client(tmp_path, *, deliver, conv_mode="auto_ai", role="master"):
    from src.web.routes.unified_inbox_stored_read_routes import (
        register_stored_read_routes,
    )

    cfg = {"inbox": {"l2_autosend": {"enabled": True, "deliver": deliver}}}
    cm = _CM(tmp_path, cfg)
    app = FastAPI()
    from starlette.middleware.sessions import SessionMiddleware

    app.add_middleware(SessionMiddleware, secret_key="t")
    app.state.config_manager = cm

    class _ModeStore:
        def get_automation_mode_if_set(self, cid):
            return conv_mode

        def get_automation_mode(self, cid):
            return conv_mode

    app.state.inbox_store = _ModeStore()
    register_stored_read_routes(app, api_auth=_noop_auth)

    @app.get("/_login")
    async def _login(request: Request):
        request.session["user_id"] = "boss"
        request.session["role"] = role
        return {"ok": True}

    client = TestClient(app)
    client.get("/_login")
    return client, cm


@pytest.mark.parametrize("conv_mode", ["auto_ai", "multi_choice"])
def test_automation_get_flags_intercepted_conversation(tmp_path, conv_mode):
    client, cm = _automation_client(tmp_path, deliver=False, conv_mode=conv_mode)
    record_gate_flip(cm_dir(cm), path="inbox.l2_autosend.deliver", value=False,
                     actor="钧", source="standby:off", ts=1000.0)
    d = client.get("/api/unified-inbox/automation",
                   params={"platform": "line", "account_id": "a",
                           "chat_key": "u1"}).json()
    dp = d["deliver_paused"]
    assert dp["reason"] == "l2_autosend.deliver=false"
    assert dp["actor"] == "钧" and dp["can_resume"] is True


def test_automation_get_telegram_exempt(tmp_path):
    """telegram A 线直答不受总闸影响 → 不许对它亮「未真发」（反向撒谎）。"""
    client, _cm = _automation_client(tmp_path, deliver=False)
    d = client.get("/api/unified-inbox/automation",
                   params={"platform": "telegram", "account_id": "a",
                           "chat_key": "u1"}).json()
    assert d["deliver_paused"] is None


def test_automation_get_manual_mode_not_flagged(tmp_path):
    """manual/review 会话本就不自动发——总闸与它无关，不亮提示（别加戏）。"""
    client, _cm = _automation_client(tmp_path, deliver=False, conv_mode="manual")
    d = client.get("/api/unified-inbox/automation",
                   params={"platform": "line", "account_id": "a",
                           "chat_key": "u1"}).json()
    assert d["deliver_paused"] is None


def test_automation_get_gate_open_regression(tmp_path):
    """回归钉（验收③）：总闸开时响应不带拦截段，与现状逐字段一致。"""
    client, _cm = _automation_client(tmp_path, deliver=True)
    d = client.get("/api/unified-inbox/automation",
                   params={"platform": "line", "account_id": "a",
                           "chat_key": "u1"}).json()
    assert d["deliver_paused"] is None


def test_agent_role_cannot_resume_but_sees_notice(tmp_path):
    client, _cm = _automation_client(tmp_path, deliver=False, role="agent")
    d = client.get("/api/unified-inbox/automation",
                   params={"platform": "line", "account_id": "a",
                           "chat_key": "u1"}).json()
    dp = d["deliver_paused"]
    assert dp is not None and dp["can_resume"] is False
