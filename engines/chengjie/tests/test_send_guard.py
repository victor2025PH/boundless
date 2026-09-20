"""Stage M 发送入口审计：编排器中心护栏 + A 线 send_message 纳入统一发送栈。

覆盖此前的旁路风控缺口——主动问候/唤醒/关怀经 ``orchestrator.send`` 或
``CompanionWorker.send→send_message`` 直发裸 client，绕过 Kill-Switch/反封号。
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
import types

import pytest

import src.ops.kill_switch as ks_mod
from src.integrations import account_orchestrator as orch
from src.integrations.account_orchestrator import AccountOrchestrator, account_key
from src.integrations.account_registry import AccountRegistry
from src.integrations.shared.send_guard import send_blocked
from src.ops.kill_switch import KillSwitch


@pytest.fixture
def fresh_ks(tmp_path, monkeypatch):
    """干净的 Kill-Switch 单例（隔离全局状态，测后还原）。"""
    ks = KillSwitch(tmp_path / "runtime_flags.db")
    monkeypatch.setattr(ks_mod, "_singleton", ks)
    return ks


# ── send_blocked 纯逻辑 ──────────────────────────────────────────────────────

def test_send_blocked_kill_switch_account(fresh_ks):
    fresh_ks.set("account:telegram:1", reason="手动急停")
    blocked, reason = send_blocked("telegram", "1")
    assert blocked is True
    assert reason.startswith("kill_switch:")


def test_send_blocked_kill_switch_global(fresh_ks):
    fresh_ks.set("global", reason="全局冻结")
    blocked, reason = send_blocked("whatsapp", "x")
    assert blocked is True
    assert reason == "kill_switch:global"


def test_send_blocked_clear_when_no_flag(fresh_ks):
    blocked, reason = send_blocked("telegram", "1")
    assert blocked is False and reason == ""


def test_send_blocked_gate_disabled_passes(fresh_ks):
    # gate 默认关 → 即便信号差也不拦（零破坏）
    blocked, _ = send_blocked("telegram", "1", config={"companion_send_gate": {"enabled": False}})
    assert blocked is False


def test_send_blocked_gate_enabled_blocks_banned(fresh_ks):
    class _Reg:
        def get(self, p, a):
            return {"meta": {"banned": True}, "status": "removed"}

    blocked, reason = send_blocked(
        "telegram", "1",
        config={"companion_send_gate": {"enabled": True}},
        registry=_Reg())
    assert blocked is True
    assert reason.startswith("send_gate:")


def test_send_blocked_failopen_on_error(fresh_ks, monkeypatch):
    # 守卫自身异常 → 放行（broken guard 不得卡死所有发送）
    def _boom(*a, **k):
        raise RuntimeError("ks down")
    monkeypatch.setattr(ks_mod, "is_blocked", _boom)
    blocked, reason = send_blocked("telegram", "1")
    assert blocked is False and reason == ""


# ── 金丝雀放量：编排器发送入口统一护栏（此前只接 B 线/官方链，编排器路径绕过）──

def test_send_blocked_canary_disabled_passes(fresh_ks):
    # canary 默认关 → 任何账号都不被 hold（零破坏）
    blocked, _ = send_blocked("telegram", "8244899900",
                              config={"ops": {"canary": {"enabled": False}}})
    assert blocked is False


def test_send_blocked_canary_holds_non_cohort(fresh_ks):
    # canary 开 + 该号不在 pinned cohort → hold（放量爆炸半径控制对编排器路径生效）
    cfg = {"ops": {"canary": {"enabled": True, "mode": "manual",
                              "pinned_accounts": ["telegram:8244899900"]}}}
    blocked, reason = send_blocked("telegram", "9999999999", config=cfg)
    assert blocked is True
    assert reason == "canary_hold"


def test_send_blocked_canary_passes_pinned(fresh_ks):
    # cohort 内账号放行（钉住的真号可继续发）
    cfg = {"ops": {"canary": {"enabled": True, "mode": "manual",
                              "pinned_accounts": ["telegram:8244899900"]}}}
    blocked, reason = send_blocked("telegram", "8244899900", config=cfg)
    assert blocked is False and reason == ""


def test_send_blocked_canary_empty_cohort_holds_all(fresh_ks):
    # canary 开但 cohort 空 → 全 hold（最保守，符合「先不放」语义）
    cfg = {"ops": {"canary": {"enabled": True, "mode": "manual", "pinned_accounts": []}}}
    blocked, reason = send_blocked("telegram", "8244899900", config=cfg)
    assert blocked is True and reason == "canary_hold"


def test_send_blocked_kill_switch_precedes_canary(fresh_ks):
    # 急停优先于金丝雀：即便账号在 cohort，global 急停仍拦（查序＝KS→canary→gate）
    fresh_ks.set("global", reason="全局冻结")
    cfg = {"ops": {"canary": {"enabled": True, "mode": "manual",
                              "pinned_accounts": ["telegram:8244899900"]}}}
    blocked, reason = send_blocked("telegram", "8244899900", config=cfg)
    assert blocked is True and reason == "kill_switch:global"


# ── 编排器中心护栏 ───────────────────────────────────────────────────────────

class _SendWorker:
    def __init__(self, account, config):
        self.sent = []

    async def start(self):
        pass

    async def stop(self):
        pass

    async def healthy(self):
        return True

    def status(self):
        return {"type": "fake_send"}

    async def send(self, chat_key, text):
        self.sent.append(("text", chat_key, text))
        return {"delivered": True, "message_id": "m1"}

    async def send_media(self, chat_key, *, media_path, media_type, caption=""):
        self.sent.append(("media", chat_key, media_type))
        return {"delivered": True}


@pytest.fixture
def registry():
    return AccountRegistry(os.path.join(tempfile.mkdtemp(), "acc.db"))


@pytest.fixture(autouse=True)
def _send_worker_registered():
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)
    orch.register_worker("telegram", "protocol", lambda a, c: _SendWorker(a, c))
    yield
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)


async def _started_orch(registry):
    registry.upsert("telegram", "1", mode="protocol", status="online")
    o = AccountOrchestrator(registry=registry)
    await o.sync()
    return o, o._managed[account_key("telegram", "1")].worker


@pytest.mark.asyncio
async def test_orchestrator_send_blocked_by_kill_switch(registry, fresh_ks, monkeypatch):
    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(pb, "emit_incoming", lambda *a, **k: None)
    o, worker = await _started_orch(registry)
    fresh_ks.set("account:telegram:1", reason="freeze")
    res = await o.send("telegram", "1", "chat1", "hi")
    assert res.get("delivered") is False
    assert str(res.get("blocked", "")).startswith("kill_switch:")
    assert worker.sent == []  # 护栏拦下，worker 未真发


@pytest.mark.asyncio
async def test_orchestrator_send_allowed_when_clear(registry, fresh_ks, monkeypatch):
    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(pb, "emit_incoming", lambda *a, **k: None)
    o, worker = await _started_orch(registry)
    res = await o.send("telegram", "1", "chat1", "hi")
    assert res.get("delivered") is True
    assert worker.sent == [("text", "chat1", "hi")]


@pytest.mark.asyncio
async def test_orchestrator_send_media_blocked_by_kill_switch(registry, fresh_ks, monkeypatch):
    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(pb, "emit_incoming", lambda *a, **k: None)
    o, worker = await _started_orch(registry)
    fresh_ks.set("platform:telegram", reason="平台冻结")
    res = await o.send_media(
        "telegram", "1", "chat1",
        media_path="/x.png", media_url="/static/x.png", media_type="image", caption="c")
    assert res.get("delivered") is False
    assert str(res.get("blocked", "")).startswith("kill_switch:")
    assert worker.sent == []


# ── A 线 send_message 纳入统一发送栈 ─────────────────────────────────────────

class _TextCli:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    async def send_message(self, chat_id, text):
        if self.fail:
            raise RuntimeError("rpc")
        self.calls.append((chat_id, text))


def _text_sender(cli, *, min_interval=0, last_send=0.0):
    from src.client.sender import TelegramSenderMixin

    class _Cfg:
        def get(self, k, d=None):
            if k == "reply":
                return {"split_send": {"min_interval_seconds": min_interval}}
            return d if d is not None else {}

    class _S(TelegramSenderMixin):
        def __init__(self):
            self.client = cli
            self.logger = logging.getLogger("test_send_msg")
            self.account_id = "a"
            self.config = _Cfg()
            self._last_send_wallclock = last_send

    s = _S()
    s._shared_send_limiter = lambda cfg: None
    return s


@pytest.mark.asyncio
async def test_send_message_blocked_by_presend_guard(monkeypatch):
    s = _text_sender(_TextCli())
    monkeypatch.setattr(s, "_presend_blocked", lambda **_kw: True)
    assert await s.send_message(7, "hi") is False
    assert s.client.calls == []  # 冻结/被闸门拦 → 不真发（不绕过风控）


@pytest.mark.asyncio
async def test_send_message_success_records_count(monkeypatch):
    s = _text_sender(_TextCli())
    monkeypatch.setattr(s, "_presend_blocked", lambda **_kw: False)
    assert await s.send_message(7, "hi") is True
    assert s.client.calls == [(7, "hi")]
    assert s._last_send_wallclock > 0  # 记账刷新墙钟（喂下次节流 + 共用计数器）


@pytest.mark.asyncio
async def test_send_message_paces_against_wallclock(monkeypatch):
    slept = {}

    async def _fake_sleep(sec):
        slept["sec"] = sec

    monkeypatch.setattr("src.client.sender.asyncio.sleep", _fake_sleep)
    s = _text_sender(_TextCli(), min_interval=5, last_send=time.time())
    monkeypatch.setattr(s, "_presend_blocked", lambda **_kw: False)
    assert await s.send_message(7, "hi") is True
    assert slept.get("sec") is not None and slept["sec"] > 0


@pytest.mark.asyncio
async def test_send_message_no_client_returns_false(monkeypatch):
    s = _text_sender(None)
    monkeypatch.setattr(s, "_presend_blocked", lambda **_kw: False)
    assert await s.send_message(7, "hi") is False


@pytest.mark.asyncio
async def test_send_message_failure_returns_false(monkeypatch):
    s = _text_sender(_TextCli(fail=True))
    monkeypatch.setattr(s, "_presend_blocked", lambda **_kw: False)
    assert await s.send_message(7, "hi") is False  # RPC 抛 → False、不冒泡


class _MsgTextCli:
    """底层 send_message 返回带 .id 的 Message（贴近真实 pyrogram）。"""
    def __init__(self, mid=4242):
        self._mid = mid
        self.calls = []

    async def send_message(self, chat_id, text):
        self.calls.append((chat_id, text))
        return types.SimpleNamespace(id=self._mid)


@pytest.mark.asyncio
async def test_send_message_return_id_success(monkeypatch):
    """P4-4：send_message_return_id 成功 → (True, 真实 id 字符串)。"""
    s = _text_sender(_MsgTextCli(mid=7788))
    monkeypatch.setattr(s, "_presend_blocked", lambda **_kw: False)
    ok, mid = await s.send_message_return_id(7, "hi")
    assert ok is True and mid == "7788"


@pytest.mark.asyncio
async def test_send_message_return_id_blocked(monkeypatch):
    """被护栏拦 → (False, "")，且不真发。"""
    cli = _MsgTextCli()
    s = _text_sender(cli)
    monkeypatch.setattr(s, "_presend_blocked", lambda **_kw: True)
    ok, mid = await s.send_message_return_id(7, "hi")
    assert ok is False and mid == ""
    assert cli.calls == []


@pytest.mark.asyncio
async def test_send_message_return_id_no_message_object(monkeypatch):
    """底层桩返回 None（无 Message）→ ok=True 但 id 空串，绝不抛。"""
    s = _text_sender(_TextCli())
    monkeypatch.setattr(s, "_presend_blocked", lambda **_kw: False)
    ok, mid = await s.send_message_return_id(7, "hi")
    assert ok is True and mid == ""


# ── #77（0830 AW7MUV 实锤）：白名单豁免可观测 + 拦截日志带 peer ──────────────

def test_send_blocked_exempt_peer_passes_and_counted_77(fresh_ks):
    """白名单 peer：闸门不评估直接放行，且豁免命中进计数（正面证据）。"""
    from src.integrations.shared import send_guard as sg

    class _Reg:
        def get(self, p, a):
            return {"meta": {"banned": True}, "status": "removed"}

    cfg = {"companion_send_gate": {"enabled": True,
                                   "exempt_peers": ["6206360305"]}}
    before = sg.block_stats_snapshot()["exempt_hits"]
    blocked, reason = send_blocked(
        "telegram", "1", config=cfg, registry=_Reg(),
        chat_key="6206360305")
    assert blocked is False and reason == ""
    assert sg.block_stats_snapshot()["exempt_hits"] == before + 1
    # 非白名单 peer 照常被拦（豁免只对那一位，不解锁整个账号）
    blocked2, reason2 = send_blocked(
        "telegram", "1", config=cfg, registry=_Reg(),
        chat_key="7331000000")
    assert blocked2 is True and reason2.startswith("send_gate:")


def test_send_blocked_exempt_notify_false_not_counted_77(fresh_ks):
    """预判/横幅读路径（notify=False）不进豁免计数——防轮询刷成噪音。"""
    from src.integrations.shared import send_guard as sg
    cfg = {"companion_send_gate": {"enabled": True,
                                   "exempt_peers": ["123"]}}
    before = sg.block_stats_snapshot()["exempt_hits"]
    send_blocked("telegram", "1", config=cfg, chat_key="123", notify=False)
    assert sg.block_stats_snapshot()["exempt_hits"] == before


def test_intercept_logs_carry_peer_77():
    """静态契约：三处拦截/豁免日志都必须带目标 peer（AW7MUV 定性不了的根因）。"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    orch_src = (root / "src" / "integrations"
                / "account_orchestrator.py").read_text(encoding="utf-8")
    assert orch_src.count("→ peer=%s") >= 2      # send + send_media 两处
    sender_src = (root / "src" / "client" / "sender.py").read_text(
        encoding="utf-8")
    assert "白名单豁免命中" in sender_src            # A 线豁免接入 + 留痕
    assert "被反封号闸门拦截 → peer=" in sender_src
    pa_src = (root / "src" / "integrations"
              / "protocol_autoreply.py").read_text(encoding="utf-8")
    assert "白名单豁免命中" in pa_src
    assert "|peer={chat_key}" in pa_src


@pytest.mark.asyncio
async def test_presend_gate_exempt_peer_77(monkeypatch):
    """A 线行为：白名单 peer 的发送不被 daily_cap 类闸门拦（此前 A 线没接豁免）。"""
    cli = _TextCli()
    s = _text_sender(cli)

    class _GateCfg:
        config = {"companion_send_gate": {"enabled": True,
                                          "exempt_peers": ["777"]},
                  "reply": {"split_send": {"min_interval_seconds": 0}}}

        def get(self, k, d=None):
            return self.config.get(k, d if d is not None else {})

    s.config = _GateCfg()
    # 让闸门评估必拦（banned 信号）——豁免命中时根本走不到评估
    import src.skills.companion_send_gate as gate_mod
    monkeypatch.setattr(
        gate_mod, "evaluate",
        lambda sig, cfg, **kw: {"allowed": False, "reason": "daily_cap",
                                "light": "red", "score": 0})
    assert s._presend_blocked(peer="777") is False      # 白名单放行
    assert s._presend_blocked(peer="888") is True       # 其他客户照拦
