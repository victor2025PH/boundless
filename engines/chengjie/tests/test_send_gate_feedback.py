"""发送护栏可见化契约（P0 2026-08-12）。

当日实锤：坐席「填入并发送」被 ``send_gate:warmup_cap`` 拦下，编排器把拦截
当**数据**返回（``{delivered:false, blocked}``），人工发送路由包成 ``ok:true``
→ 前端按成功处理，乐观气泡静默消失，坐席以为产品坏了（22:37-22:42 八连点）。

钉住的不变量：
1. 拦截/未送达必须显式回执——``blocked`` → 409 ``code=send_blocked``；
   worker 显式 ``delivered=False`` → 502（绝不再包成 ok:true）。
2. 被拦的发送**不产生副作用**：不打坐席接管标（切 manual）、幂等占位释放
   （同 id 重试不会被去重表挡住）。
3. 预检与护栏同源：``send_gate_snapshot`` 的判定走 ``send_blocked`` 同一函数，
   额度数字与 ``companion_send_gate.evaluate`` 同口径（used/cap 就是闸门比较的
   那两个数）——预判横幅绝不另算一套。
4. 额度是**滚动 24h 窗**：``quota_frees_at`` 给出首个空位释放时刻（第
   day_used-cap+1 老的发送记录 ts+24h），横幅不许承诺「明天零点恢复」。
5. 语音路由被拦要发生在 TTS 合成**之前**（省 GPU），且对账登记表记
   ``send_blocked``（超时对账端点不再谎报 sent）。
6. ``/api/unified-inbox/automation`` 搭便车带 ``send_gate`` 段（事前预判横幅
   的数据源，零新增轮询）。
"""
from __future__ import annotations

import time

import pytest

from src.integrations.protocol_autoreply_limits import (
    AutoReplyLimiter,
    SendCountStore,
)

_DAY = 86400.0


# ── 1. 滚动窗「空位释放时刻」纯函数 ─────────────────────────────────────────


def test_quota_frees_at_memory_mode():
    lim = AutoReplyLimiter(hourly=0, daily=0, store=None)
    now = 1_000_000.0
    # 3 条发送：t0-100, t0-50, t0-10；cap=2 → 需 3-2+1=2 条过期 → 第 2 老（t0-50）+24h
    for dt in (100.0, 50.0, 10.0):
        lim.record_sent("telegram:acct", now=now - dt)
    frees = lim.quota_frees_at("telegram:acct", 2, now=now)
    assert frees == pytest.approx(now - 50.0 + _DAY)


def test_quota_frees_at_not_over_cap_returns_none():
    lim = AutoReplyLimiter(store=None)
    now = 1_000_000.0
    lim.record_sent("k", now=now - 5)
    assert lim.quota_frees_at("k", 2, now=now) is None   # 1 < 2 未超限
    assert lim.quota_frees_at("k", 0, now=now) is None   # cap=0 无意义
    assert lim.quota_frees_at("nobody", 1, now=now) is None


def test_quota_frees_at_store_mode(tmp_path):
    store = SendCountStore(tmp_path / "sends.db")
    lim = AutoReplyLimiter(store=store)
    now = 2_000_000.0
    # store 与内存都会被写；故意只写 store（模拟重启后内存为空的跨进程真值）
    for dt in (300.0, 200.0, 100.0):
        store.record("k", now - dt)
    # day_used=3, cap=1 → 需 3 条过期 → 第 3 老（now-100）+24h
    frees = lim.quota_frees_at("k", 1, now=now)
    assert frees == pytest.approx(now - 100.0 + _DAY)
    # nth_oldest_since 越界 → None → frees_at 回退 None（不足 n 条）
    assert store.nth_oldest_since("k", now - _DAY, 4) is None
    assert store.nth_oldest_since("k", now - _DAY, 0) is None


# ── 2. 快照与护栏同源 ──────────────────────────────────────────────────────


class _FakeRegistry:
    """满 ramp 的老号：ramp 后 recommended_cap == target_cap。"""

    def __init__(self, created_days_ago: float = 30.0):
        self._created = time.time() - created_days_ago * _DAY

    def get(self, platform, account_id):
        return {"created_at": self._created, "proxy_id": "p1",
                "status": "active", "meta": {}}


def _gate_cfg(target_cap: int = 3) -> dict:
    return {"companion_send_gate": {
        "enabled": True, "target_cap": target_cap,
        "warmup_start_cap": 1, "warmup_ramp_days": 7,
    }}


def _patched_limiter(monkeypatch, sends: int, now: float) -> AutoReplyLimiter:
    """把进程单例出口换成受控实例（send_guard 与 send_gate_status 都从
    protocol_autoreply_limits 模块取，单点替换两处同吃）。"""
    import src.integrations.protocol_autoreply_limits as pal
    lim = AutoReplyLimiter(hourly=0, daily=0, store=None)
    for i in range(sends):
        lim.record_sent("telegram:default", now=now - 60.0 * (sends - i))
    monkeypatch.setattr(pal, "get_autoreply_limiter", lambda *_a, **_k: lim)
    return lim


def test_snapshot_blocked_numbers_match_gate(monkeypatch):
    from src.inbox.send_gate_status import send_gate_snapshot
    now = time.time()
    _patched_limiter(monkeypatch, sends=3, now=now)   # used=3 >= cap=3 → 拦
    snap = send_gate_snapshot(
        "telegram", "default", "999",
        config=_gate_cfg(target_cap=3), registry=_FakeRegistry(), now=now)
    assert snap is not None
    assert snap["blocked"] is True
    assert snap["reason"] == "send_gate:daily_cap"   # P3 更名后的新发射值
    # used/cap 与闸门 evaluate 同口径（cap=满 ramp 的 target_cap；
    # reserve 缺省 0 → auto_cap==cap）
    assert snap["quota"]["used"] == 3
    assert snap["quota"]["cap"] == 3
    assert snap["quota"]["auto_cap"] == 3
    assert snap["quota"]["reserve"] == 0
    # 滚动窗释放时刻：第 3-3+1=1 老（now-180s）+24h
    assert snap["frees_at"] == pytest.approx(now - 180.0 + _DAY, abs=1.0)


def test_snapshot_allowed_has_quota_but_not_blocked(monkeypatch):
    from src.inbox.send_gate_status import send_gate_snapshot
    now = time.time()
    _patched_limiter(monkeypatch, sends=1, now=now)
    snap = send_gate_snapshot(
        "telegram", "default", "999",
        config=_gate_cfg(target_cap=3), registry=_FakeRegistry(), now=now)
    assert snap is not None and snap["blocked"] is False
    assert snap["quota"]["used"] == 1 and snap["quota"]["cap"] == 3
    assert snap["frees_at"] is None


def test_snapshot_exempt_peer_not_blocked(monkeypatch):
    from src.inbox.send_gate_status import send_gate_snapshot
    now = time.time()
    _patched_limiter(monkeypatch, sends=5, now=now)
    cfg = _gate_cfg(target_cap=3)
    cfg["companion_send_gate"]["exempt_peers"] = ["999"]
    snap = send_gate_snapshot(
        "telegram", "default", "999",
        config=cfg, registry=_FakeRegistry(), now=now)
    # 白名单豁免 → 不拦（quota 段仍在，供横幅显示用量）
    assert snap is not None and snap["blocked"] is False


def test_snapshot_gate_disabled_returns_none(monkeypatch):
    from src.inbox.send_gate_status import send_gate_snapshot
    snap = send_gate_snapshot(
        "telegram", "default", "999", config={}, registry=_FakeRegistry())
    assert snap is None


def test_blocked_reason_key_families():
    from src.inbox.send_gate_status import blocked_reason_key
    # P3 更名双值：daily_cap=现发射值 / warmup_cap=历史审计行——永远都认
    assert blocked_reason_key("send_gate:daily_cap") == "quota"
    assert blocked_reason_key("send_gate:warmup_cap") == "quota"
    assert blocked_reason_key("send_gate:health_red") == "health"
    assert blocked_reason_key("send_gate:banned") == "banned"
    assert blocked_reason_key("send_gate:other") == "gate"
    assert blocked_reason_key("kill_switch:global") == "killswitch"
    assert blocked_reason_key("canary_hold") == "canary"
    assert blocked_reason_key("license_readonly") == "license"
    assert blocked_reason_key("session_unhealthy") == "session"
    assert blocked_reason_key("") == "generic"


def test_send_blocked_notify_flag(monkeypatch):
    """notify=False（读路径）绝不发运营告警；默认 True 保持发送路径旧行为。"""
    import src.integrations.shared.send_guard as sg
    calls = {"n": 0}
    monkeypatch.setattr(sg, "notify_send_blocked",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))
    now = time.time()
    _patched_limiter(monkeypatch, sends=3, now=now)
    blocked, reason = sg.send_blocked(
        "telegram", "default", config=_gate_cfg(3),
        registry=_FakeRegistry(), chat_key="999", notify=False)
    assert blocked is True and reason == "send_gate:daily_cap"
    assert calls["n"] == 0
    blocked, _ = sg.send_blocked(
        "telegram", "default", config=_gate_cfg(3),
        registry=_FakeRegistry(), chat_key="999")
    assert blocked is True
    assert calls["n"] == 1


# ── 3. 发送路由契约 ────────────────────────────────────────────────────────


def _no_orch(monkeypatch):
    """预检层视为「编排器不拥有」→ 预检跳过，走 post-adapter 判定路径。"""
    import src.integrations.account_orchestrator as _ao
    monkeypatch.setattr(_ao, "get_orchestrator_if_running", lambda: None)


def _fake_adapter(monkeypatch, result: dict):
    import src.web.routes.unified_inbox_send_routes as sr
    calls = {"n": 0}

    async def _fake(request, platform, account_id, chat_key, text, adapters,
                    **kw):
        calls["n"] += 1
        return dict(result)

    monkeypatch.setattr(sr, "send_via_adapters", _fake)
    return calls


def _takeover_counter(monkeypatch):
    import src.inbox.takeover_rearm as trm
    calls = {"n": 0}
    monkeypatch.setattr(trm, "record_agent_takeover",
                        lambda *a, **k: calls.__setitem__("n", calls["n"] + 1))
    return calls


def test_send_blocked_result_returns_409_no_side_effects(auth_client, monkeypatch):
    _no_orch(monkeypatch)
    adapter = _fake_adapter(
        monkeypatch, {"delivered": False, "blocked": "send_gate:warmup_cap"})
    takeover = _takeover_counter(monkeypatch)
    r = auth_client.post(
        "/api/unified-inbox/send",
        json={"platform": "telegram", "account_id": "default",
              "chat_key": "999", "text": "hello quota world",
              "client_msg_id": "same-id-1"},
        follow_redirects=False,
    )
    assert r.status_code == 409
    det = r.json()["detail"]
    assert det["code"] == "send_blocked"
    assert det["reason"] == "send_gate:warmup_cap"
    assert det["message"]
    assert takeover["n"] == 0          # 没发出去就不许打接管标
    # 幂等占位已释放：同 id 重试必须再次到达适配器（而非 duplicate 短路）
    r2 = auth_client.post(
        "/api/unified-inbox/send",
        json={"platform": "telegram", "account_id": "default",
              "chat_key": "999", "text": "hello quota world",
              "client_msg_id": "same-id-1"},
        follow_redirects=False,
    )
    assert r2.status_code == 409
    assert adapter["n"] == 2


def test_send_delivered_false_returns_502(auth_client, monkeypatch):
    _no_orch(monkeypatch)
    _fake_adapter(monkeypatch, {"delivered": False, "error": "socket boom"})
    takeover = _takeover_counter(monkeypatch)
    r = auth_client.post(
        "/api/unified-inbox/send",
        json={"platform": "telegram", "account_id": "default",
              "chat_key": "999", "text": "hello failure world"},
        follow_redirects=False,
    )
    assert r.status_code == 502
    det = str(r.json()["detail"])
    assert det
    # 共享树防波动：i18n 合并可能被**其它 pack 的半存盘态**整体打回单体
    # （collect_packs 任一模块坏 → 全部 pack 键回落裸键）。本测试钉的是发送
    # 语义，不给别人的编辑窗口当人质：合并健康时验证完整格式化文案；
    # 键与 {msg} 占位符契约另行直读本 pack 钉住（不经全局合并）。
    if not det.startswith("err."):
        assert "socket boom" in det
    from src.web.i18n_packs import errors as _errors_pack
    for _d in (_errors_pack.ZH, _errors_pack.EN):
        assert "socket boom" in _d["err.inbox.send_not_delivered"].format(
            msg="socket boom")
    assert takeover["n"] == 0


def test_send_ok_result_still_ok(auth_client, monkeypatch):
    """回归护栏：正常送达（含无 delivered 键的历史返回体）不受新判定影响。"""
    _no_orch(monkeypatch)
    _fake_adapter(monkeypatch, {"queued": True, "item_id": 7})
    r = auth_client.post(
        "/api/unified-inbox/send",
        json={"platform": "telegram", "account_id": "default",
              "chat_key": "999", "text": "hello normal world"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_send_precheck_blocks_before_adapter(auth_client, monkeypatch):
    """编排器拥有 + 快照判拦 → 409 在适配器之前（省翻译/投递开销）。"""
    import src.integrations.account_orchestrator as _ao
    import src.inbox.send_gate_status as sgs

    class _Orch:
        def owns(self, p, a):
            return True

    monkeypatch.setattr(_ao, "get_orchestrator_if_running", lambda: _Orch())
    monkeypatch.setattr(
        sgs, "send_gate_snapshot",
        lambda *a, **k: {"blocked": True, "reason": "send_gate:warmup_cap",
                         "quota": {"used": 155, "cap": 150, "light": "amber"},
                         "frees_at": 1234567890.0})
    adapter = _fake_adapter(monkeypatch, {"delivered": True})
    r = auth_client.post(
        "/api/unified-inbox/send",
        json={"platform": "telegram", "account_id": "default",
              "chat_key": "999", "text": "hello precheck world"},
        follow_redirects=False,
    )
    assert r.status_code == 409
    det = r.json()["detail"]
    assert det["code"] == "send_blocked"
    assert det["used"] == 155 and det["cap"] == 150
    assert det["frees_at"] == pytest.approx(1234567890.0)
    assert adapter["n"] == 0


def test_send_precheck_failopen_on_snapshot_error(auth_client, monkeypatch):
    """预检自身故障绝不拦发送（fail-open）——护栏坏了不能把全部人工发送卡死。"""
    import src.integrations.account_orchestrator as _ao
    import src.inbox.send_gate_status as sgs

    class _Orch:
        def owns(self, p, a):
            return True

    monkeypatch.setattr(_ao, "get_orchestrator_if_running", lambda: _Orch())

    def _boom(*a, **k):
        raise RuntimeError("snapshot broken")

    monkeypatch.setattr(sgs, "send_gate_snapshot", _boom)
    _fake_adapter(monkeypatch, {"delivered": True})
    r = auth_client.post(
        "/api/unified-inbox/send",
        json={"platform": "telegram", "account_id": "default",
              "chat_key": "999", "text": "hello failopen world"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ── 4. /automation 搭便车段 ────────────────────────────────────────────────


def test_automation_get_carries_send_gate(auth_client, monkeypatch):
    import src.integrations.account_orchestrator as _ao
    import src.inbox.send_gate_status as sgs

    class _Orch:
        def owns(self, p, a):
            return True

    monkeypatch.setattr(_ao, "get_orchestrator_if_running", lambda: _Orch())
    monkeypatch.setattr(
        sgs, "send_gate_snapshot",
        lambda *a, **k: {"blocked": True, "reason": "send_gate:warmup_cap",
                         "quota": {"used": 5, "cap": 3, "light": "amber"},
                         "frees_at": None})
    r = auth_client.get(
        "/api/unified-inbox/automation"
        "?platform=telegram&account_id=default&chat_key=999")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["send_gate"]["blocked"] is True
    assert d["send_gate"]["quota"]["used"] == 5


def test_automation_get_send_gate_none_when_not_owned(auth_client, monkeypatch):
    _no_orch(monkeypatch)
    r = auth_client.get(
        "/api/unified-inbox/automation"
        "?platform=telegram&account_id=default&chat_key=999")
    assert r.status_code == 200
    assert r.json().get("send_gate") is None


# ── 5. 语音路由：拦在合成之前 + 对账表如实记败 ─────────────────────────────


def test_send_voice_blocked_before_synthesis(auth_client, monkeypatch):
    import src.integrations.account_orchestrator as _ao
    import src.inbox.send_gate_status as sgs
    from src.ai.tts_pipeline import TTSPipeline

    class _Orch:
        def owns_media(self, p, a):
            return True

        def owns(self, p, a):
            return True

    monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _Orch())
    monkeypatch.setattr(
        sgs, "send_gate_snapshot",
        lambda *a, **k: {"blocked": True, "reason": "send_gate:warmup_cap",
                         "quota": {"used": 9, "cap": 5, "light": "amber"},
                         "frees_at": None})
    synth = {"n": 0}

    async def _synth(self, *a, **k):
        synth["n"] += 1

    monkeypatch.setattr(TTSPipeline, "synthesize", _synth)
    r = auth_client.post(
        "/api/unified-inbox/send-voice",
        json={"platform": "telegram", "account_id": "default",
              "chat_key": "999", "text": "你好，这是语音额度测试",
              "client_msg_id": "voice-blk-1"},
        follow_redirects=False,
    )
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "send_blocked"
    assert synth["n"] == 0                 # 额度已满不许再烧一次 TTS
    # 对账表记 failed（send_blocked）——超时对账端点不得谎报 sent
    from src.inbox import voice_send_tracker as _vst
    st = _vst.get_status("voice:telegram:default:999", "voice-blk-1")
    assert st.get("state") == "failed"
    assert st.get("reason") == "send_blocked"


# ── 6. P1 人工预留额度（reserve_for_manual：自动链让路，人工用满额度）─────────


def test_gate_decision_reserve_splits_lanes():
    from src.skills.companion_send_gate import gate_decision
    sig = {"sends_today": 8, "age_days": 30.0, "proxy_bound": True}
    kw = dict(target_cap=10, warmup_start_cap=2, warmup_ramp_days=7,
              reserve_for_manual=3)
    # used=8 >= auto_cap(7) → 自动被拦；< cap(10) → 人工放行
    d_auto = gate_decision(sig, origin="auto", **kw)
    d_manual = gate_decision(sig, origin="manual", **kw)
    assert d_auto["allowed"] is False and d_auto["reason"] == "daily_cap"
    assert d_manual["allowed"] is True
    assert d_auto["auto_cap"] == 7 and d_auto["recommended_cap"] == 10
    assert d_manual["reserve_for_manual"] == 3
    # used=10 → 两道都拦（人工也不许超总额度）
    sig2 = dict(sig, sends_today=10)
    assert gate_decision(sig2, origin="manual", **kw)["allowed"] is False
    # reserve=0 → 两道同值（与旧行为一致）
    kw0 = dict(kw, reserve_for_manual=0)
    assert gate_decision(sig, origin="auto", **kw0)["allowed"] is True
    # reserve > cap → auto_cap=0（自动链全停，全部留给人工——合法配置）
    kwx = dict(kw, reserve_for_manual=99)
    dx = gate_decision({"sends_today": 0, "age_days": 30.0, "proxy_bound": True},
                       origin="auto", **kwx)
    assert dx["auto_cap"] == 0 and dx["allowed"] is False
    # 负数 clamp 0
    kwn = dict(kw, reserve_for_manual=-5)
    assert gate_decision(sig, origin="auto", **kwn)["allowed"] is True


def test_send_blocked_origin_lanes(monkeypatch):
    """同一账号同一时刻：自动链被预留额度拦下，人工链仍放行——同一个判定函数分道。"""
    import src.integrations.shared.send_guard as sg
    now = time.time()
    _patched_limiter(monkeypatch, sends=4, now=now)   # used=4
    cfg = _gate_cfg(target_cap=6)
    cfg["companion_send_gate"]["reserve_for_manual"] = 2   # auto_cap=4
    blocked_auto, reason_auto = sg.send_blocked(
        "telegram", "default", config=cfg, registry=_FakeRegistry(),
        chat_key="999", notify=False)
    blocked_manual, _ = sg.send_blocked(
        "telegram", "default", config=cfg, registry=_FakeRegistry(),
        chat_key="999", notify=False, origin="manual")
    assert blocked_auto is True and reason_auto == "send_gate:daily_cap"
    assert blocked_manual is False


def test_snapshot_auto_yield_visible(monkeypatch):
    """人工未拦 + 自动已让路 → quota.auto_blocked=True（横幅软信息态的数据源）。"""
    from src.inbox.send_gate_status import send_gate_snapshot
    now = time.time()
    _patched_limiter(monkeypatch, sends=4, now=now)
    cfg = _gate_cfg(target_cap=6)
    cfg["companion_send_gate"]["reserve_for_manual"] = 2
    snap = send_gate_snapshot(
        "telegram", "default", "999",
        config=cfg, registry=_FakeRegistry(), now=now)
    assert snap is not None
    assert snap["blocked"] is False                      # 人工视角未拦
    assert snap["quota"]["auto_blocked"] is True         # 自动链已让路
    assert snap["quota"]["auto_cap"] == 4
    assert snap["quota"]["cap"] == 6
    assert snap["quota"]["reserve"] == 2
    # P3：让路线恢复时刻——used=4 ≥ auto_cap=4 → 第 4-4+1=1 老（now-240s）+24h
    assert snap["quota"]["auto_frees_at"] == pytest.approx(
        now - 240.0 + _DAY, abs=1.0)


def test_send_via_adapters_threads_origin(monkeypatch):
    """origin 经 send_via_adapters 透传编排器；旧签名假编排器 TypeError 降级不炸。"""
    import asyncio

    from src.inbox.channel_adapters import send_via_adapters

    captured = {}

    class _OrchNew:
        def owns(self, p, a):
            return True

        async def send(self, p, a, ck, text, *, reply_to=None,
                       mentions=None, origin="auto"):
            captured["origin"] = origin
            return {"delivered": True}

    class _OrchOld:
        def owns(self, p, a):
            return True

        async def send(self, p, a, ck, text, *, reply_to=None):
            captured["old_called"] = True
            return {"delivered": True}

    import src.integrations.account_orchestrator as _ao
    monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _OrchNew())
    r = asyncio.run(send_via_adapters(
        None, "telegram", "default", "999", "hi", [], origin="manual"))
    assert r["delivered"] is True and captured["origin"] == "manual"
    monkeypatch.setattr(_ao, "get_orchestrator", lambda *a, **k: _OrchOld())
    r2 = asyncio.run(send_via_adapters(
        None, "telegram", "default", "999", "hi", [], origin="manual"))
    assert r2["delivered"] is True and captured.get("old_called") is True


def test_block_stats_counter(monkeypatch):
    """拦截计数：真实发送尝试（notify=True）才计，读路径轮询不计。"""
    import src.integrations.shared.send_guard as sg
    monkeypatch.setattr(sg, "notify_send_blocked", lambda *a, **k: None)
    now = time.time()
    _patched_limiter(monkeypatch, sends=3, now=now)
    before = sg.block_stats_snapshot()["total"]
    sg.send_blocked("telegram", "default", config=_gate_cfg(3),
                    registry=_FakeRegistry(), chat_key="999", notify=False)
    assert sg.block_stats_snapshot()["total"] == before      # 读路径不计
    sg.send_blocked("telegram", "default", config=_gate_cfg(3),
                    registry=_FakeRegistry(), chat_key="999")
    after = sg.block_stats_snapshot()
    assert after["total"] == before + 1
    assert any(k.startswith("telegram:default|send_gate:daily_cap")
               for k in after["by_key"])


# ── 7. P1 白名单直达端点 ──────────────────────────────────────────────────────


def test_gate_exempt_writes_overlay_and_unblocks(auth_client, monkeypatch):
    """master 白名单直达：写 overlay 热生效 → 同 chat_key 二次调用报 already。"""
    r = auth_client.post(
        "/api/unified-inbox/send-gate/exempt",
        json={"platform": "telegram", "account_id": "default",
              "chat_key": "exempt-target-77"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and not d.get("already")
    # 立即生效（set_overlay_flag 同步深合并进内存 config）→ 二次调用 already
    r2 = auth_client.post(
        "/api/unified-inbox/send-gate/exempt",
        json={"platform": "telegram", "account_id": "default",
              "chat_key": "exempt-target-77"},
        follow_redirects=False,
    )
    assert r2.status_code == 200
    assert r2.json().get("already") is True


def test_gate_exempt_requires_chat_key(auth_client):
    r = auth_client.post(
        "/api/unified-inbox/send-gate/exempt",
        json={"platform": "telegram", "account_id": "default"},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_gate_exempt_denies_viewer(app, config_dir):
    """viewer 拒写（与账号管理写口同排除法；master/桌面 Bearer 绝不误伤）。"""
    from starlette.testclient import TestClient

    from src.utils.web_user_store import ROLE_MASTER, ROLE_VIEWER, WebUserStore
    store = WebUserStore(config_dir / "web_users.db")
    if store.user_count() == 0:
        store.create_user("boss", "pw-master-123", ROLE_MASTER)
    store.create_user("u_viewer", "pw-123456", ROLE_VIEWER)
    c = TestClient(app)
    c.get("/login")
    c.post("/login", data={"username": "u_viewer", "password": "pw-123456"},
           follow_redirects=True)
    r = c.post(
        "/api/unified-inbox/send-gate/exempt",
        json={"platform": "telegram", "account_id": "default",
              "chat_key": "viewer-try"},
        headers={"Referer": "http://testserver/workspace"},
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_automation_send_gate_carries_can_exempt(auth_client, monkeypatch):
    """/automation 的 send_gate 段带 can_exempt 能力位（master=True）。"""
    import src.integrations.account_orchestrator as _ao
    import src.inbox.send_gate_status as sgs

    class _Orch:
        def owns(self, p, a):
            return True

    monkeypatch.setattr(_ao, "get_orchestrator_if_running", lambda: _Orch())
    monkeypatch.setattr(
        sgs, "send_gate_snapshot",
        lambda *a, **k: {"blocked": True, "reason": "send_gate:warmup_cap",
                         "quota": {"used": 5, "cap": 3, "light": "amber"},
                         "frees_at": None})
    r = auth_client.get(
        "/api/unified-inbox/automation"
        "?platform=telegram&account_id=default&chat_key=999")
    assert r.status_code == 200
    assert r.json()["send_gate"]["can_exempt"] is True


# ── 8. P2 观测面（机群卡额度随行 + 拦截计数暴露）────────────────────────────


def test_fleet_overview_carries_quota(monkeypatch):
    """fleet_overview 的 accounts 明细随行 quota 双道（闸门启用时）——
    机群卡显示的数与闸门比较的数同一 evaluate。闸门关 → quota=None。"""
    from src.skills.account_signals import fleet_overview
    now = time.time()
    lim = _patched_limiter(monkeypatch, sends=4, now=now)
    cfg = _gate_cfg(target_cap=6)
    cfg["companion_send_gate"]["reserve_for_manual"] = 2
    out = fleet_overview(
        [("telegram", "default", "online")],
        registry=_FakeRegistry(), limiter=lim, config=cfg, now=now)
    q = out["accounts"][0]["quota"]
    assert q == {"used": 4, "cap": 6, "auto_cap": 4, "reserve": 2,
                 "auto_blocked": True}
    out2 = fleet_overview(
        [("telegram", "default", "online")],
        registry=_FakeRegistry(), limiter=lim, config={}, now=now)
    assert out2["accounts"][0]["quota"] is None


def test_fleet_health_route_carries_send_blocks(auth_client):
    r = auth_client.get("/api/accounts/fleet-health")
    assert r.status_code == 200
    d = r.json()
    assert d.get("ok") is True
    assert "send_blocks" in d
    assert "total" in (d["send_blocks"] or {})


def test_workspace_metrics_carries_send_gate_blocks(auth_client):
    r = auth_client.get("/api/workspace/metrics")
    assert r.status_code == 200
    d = r.json()
    assert "send_gate_blocks" in d
    assert "total" in (d["send_gate_blocks"] or {})
