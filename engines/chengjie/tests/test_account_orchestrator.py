"""M5：账号池编排器 单测（假 worker + 假时钟，确定性驱动监督）。"""

from __future__ import annotations

import os
import tempfile

import pytest

from src.integrations import account_orchestrator as orch
from src.integrations.account_orchestrator import AccountOrchestrator, account_key
from src.integrations.account_registry import AccountRegistry


class FakeWorker:
    last = None

    def __init__(self, account, config):
        self.account = account
        self.started = 0
        self.stopped = 0
        self.fail_start = False
        self._healthy = True
        FakeWorker.last = self

    async def start(self):
        self.started += 1
        if self.fail_start:
            raise RuntimeError("boom")

    async def stop(self):
        self.stopped += 1

    async def healthy(self):
        return self._healthy

    def status(self):
        return {"type": "fake", "healthy": self._healthy}


@pytest.fixture()
def registry():
    return AccountRegistry(os.path.join(tempfile.mkdtemp(), "acc.db"))


@pytest.fixture(autouse=True)
def _fake_worker_registered():
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)
    orch.register_worker("telegram", "protocol", lambda a, c: FakeWorker(a, c))
    FakeWorker.last = None
    yield
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)


def _clock():
    state = {"t": 0.0}
    return state, (lambda: state["t"])


def test_worker_supported_gating():
    assert orch.worker_supported("telegram", "protocol") is True
    assert orch.worker_supported("telegram", "device") is False   # device 不编排
    assert orch.worker_supported("line", "protocol") is False     # 无 factory


async def test_sync_starts_protocol_ignores_device(registry):
    registry.upsert("telegram", "1", mode="protocol", status="online")
    registry.upsert("line", "2", mode="device", status="online")
    o = AccountOrchestrator(registry=registry)
    await o.sync()
    st = o.status()
    assert st["total"] == 1                         # 仅 protocol 被接管
    assert st["by_state"].get("running") == 1
    assert account_key("telegram", "1") in {a["key"] for a in st["accounts"]}


async def test_sync_skips_offline_and_pending(registry):
    """offline/pending 不进期望集，避免无绑定号与在线号串话。"""
    registry.upsert("telegram", "1", mode="protocol", status="online")
    registry.upsert("telegram", "2", mode="protocol", status="offline")
    registry.upsert("telegram", "3", mode="protocol", status="pending")
    o = AccountOrchestrator(registry=registry)
    await o.sync()
    keys = {a["key"] for a in o.status()["accounts"]}
    assert keys == {account_key("telegram", "1")}


async def test_remove_account_stops_worker(registry):
    registry.upsert("telegram", "1", mode="protocol", status="online")
    o = AccountOrchestrator(registry=registry)
    await o.sync()
    assert o.status()["by_state"].get("running") == 1
    registry.remove("telegram", "1")
    await o.sync()
    assert o.status()["by_state"].get("stopped") == 1


async def test_unhealthy_triggers_backoff_then_restart(registry):
    registry.upsert("telegram", "1", mode="protocol", status="online")
    state, now = _clock()
    o = AccountOrchestrator(registry=registry, now=now)
    await o.sync()
    w = FakeWorker.last
    m = o._managed[account_key("telegram", "1")]
    assert m.state == "running"

    # 变不健康 → tick 标 error + 安排退避
    w._healthy = False
    await o.tick()
    assert m.state == "error"
    assert m.restarts == 1
    assert m.backoff_until > 0

    # 未到退避时间 → 不重启
    await o.tick()
    assert m.state == "error"

    # 恢复健康 + 推进时钟越过退避 → tick 重启成功
    w._healthy = True
    state["t"] = m.backoff_until + 1
    await o.tick()
    assert m.state == "running"
    assert m.restarts == 0


async def test_start_failure_and_circuit_breaker(registry):
    registry.upsert("telegram", "1", mode="protocol", status="online")
    state, now = _clock()
    o = AccountOrchestrator(registry=registry, now=now)
    await o.sync()                       # 首次启动成功
    w = FakeWorker.last
    m = o._managed[account_key("telegram", "1")]
    # 之后变不健康且重启必失败 → 进入退避重试直至熔断
    w.fail_start = True
    w._healthy = False
    await o.tick()                       # running → unhealthy → error（仅标记，下一 tick 重试）
    for _ in range(40):
        state["t"] = m.backoff_until + 1
        await o.tick()
    assert m.restarts >= orch.MAX_RESTARTS
    assert m.state == "error"
    # 熔断后不再增加启动次数
    started_at_break = w.started
    state["t"] = m.backoff_until + 1000
    await o.tick()
    assert w.started == started_at_break


async def test_manual_start_stop_restart(registry):
    registry.upsert("telegram", "1", mode="protocol", status="online")
    o = AccountOrchestrator(registry=registry)
    acc = registry.get("telegram", "1")
    assert await o.start_account(acc) is True
    key = account_key("telegram", "1")
    assert o._managed[key].state == "running"
    await o.stop_account(key)
    assert o._managed[key].state == "stopped"
    assert await o.restart_account(key) is True
    assert o._managed[key].state == "running"


class FakeSendWorker(FakeWorker):
    """带 send 的假 worker：记录收到的 reply_to（P4-5B 引用回复透传测试）。"""
    def __init__(self, account, config):
        super().__init__(account, config)
        self.sent = []

    async def send(self, chat_key, text, *, reply_to=None, chat_type=None):
        qa = bool(reply_to and reply_to.get("id"))
        self.sent.append({"chat_key": chat_key, "text": text,
                          "reply_to": reply_to, "chat_type": chat_type})
        return {"delivered": True, "message_id": "WAMID_REPLY_1",
                "quote_applied": qa}


async def test_send_threads_reply_to_and_writes_source(registry, monkeypatch):
    """orch.send 携 reply_to → worker 收到 kwarg，且出站回写带 source.reply_to。"""
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)
    orch.register_worker("telegram", "protocol",
                         lambda a, c: FakeSendWorker(a, c))
    registry.upsert("telegram", "1", mode="protocol", status="online")
    o = AccountOrchestrator(registry=registry)
    await o.sync()
    w = FakeSendWorker.last
    captured = {}
    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(pb, "emit_incoming", lambda msg: captured.update(msg))

    reply_to = {"id": "WAMID_ORIG", "from_me": False,
                "text": "原始消息", "sender": "客户"}
    res = await o.send("telegram", "1", "639111", "引用回复内容",
                       reply_to=reply_to)
    assert res.get("delivered") is True
    # worker 收到了 reply_to kwarg
    assert w.sent and w.sent[-1]["reply_to"] == reply_to
    # 出站回写带上 source.reply_to（本端气泡也能渲染引用条）
    assert captured.get("source", {}).get("reply_to", {}).get("id") == "WAMID_ORIG"
    assert captured["source"]["reply_to"]["text"] == "原始消息"
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)


class FakeSendNoQuoteFlagWorker(FakeWorker):
    """接了 reply_to 但不回 quote_applied——模拟 LINE/旧 worker 静默丢引用。"""

    def __init__(self, account, config):
        super().__init__(account, config)
        self.sent = []

    async def send(self, chat_key, text, *, reply_to=None):
        self.sent.append({"chat_key": chat_key, "text": text, "reply_to": reply_to})
        return {"delivered": True, "message_id": "NOFLAG"}


async def test_send_does_not_mirror_quote_without_receipt(registry, monkeypatch):
    """worker 未回 quote_applied → 出站镜像不得画引用条（173 所见非所发）。"""
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)
    orch.register_worker("telegram", "protocol",
                         lambda a, c: FakeSendNoQuoteFlagWorker(a, c))
    registry.upsert("telegram", "1", mode="protocol", status="online")
    o = AccountOrchestrator(registry=registry)
    await o.sync()
    captured = {}
    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(pb, "emit_incoming", lambda msg: captured.update(msg))
    await o.send("telegram", "1", "639111", "引用回复内容",
                 reply_to={"id": "WAMID_ORIG", "text": "原始消息", "sender": "客户"})
    src = captured.get("source") or {}
    assert not src.get("reply_to")
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)


async def test_send_without_reply_to_no_source(registry, monkeypatch):
    """普通发送（无 reply_to）→ 出站回写不带 source（向后兼容）。"""
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)
    orch.register_worker("telegram", "protocol",
                         lambda a, c: FakeSendWorker(a, c))
    registry.upsert("telegram", "1", mode="protocol", status="online")
    o = AccountOrchestrator(registry=registry)
    await o.sync()
    w = FakeSendWorker.last
    captured = {}
    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(pb, "emit_incoming", lambda msg: captured.update(msg))
    await o.send("telegram", "1", "639111", "普通消息")
    assert w.sent[-1]["reply_to"] is None
    assert "source" not in captured or not captured.get("source")
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)


class FakeFailingSendWorker(FakeWorker):
    """send 永远失败（delivered=False）的假 worker——P3-5 假镜像 bug 的钉子。"""
    def __init__(self, account, config):
        super().__init__(account, config)
        FakeFailingSendWorker.last = self
        self.sent = []

    async def send(self, chat_key, text, *, reply_to=None):
        self.sent.append({"chat_key": chat_key, "text": text})
        return {"delivered": False, "error": "peer unreachable"}


async def test_failed_send_is_not_mirrored_into_the_inbox(registry, monkeypatch):
    """delivered=False 的出站**不回写收件箱**。

    2026-07-27 群演灰度实锤：连炸三场的 6 条失败台词全进了线程——坐席视角
    「发了」、群里啥也没有，且群发言台账把它们算进暴露面（speaker budget 被
    虚占）。失败消息就该只留在日志里。
    """
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)
    orch.register_worker("telegram", "protocol",
                         lambda a, c: FakeFailingSendWorker(a, c))
    registry.upsert("telegram", "1", mode="protocol", status="online")
    o = AccountOrchestrator(registry=registry)
    await o.sync()
    mirrored = []
    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(pb, "emit_incoming", lambda msg: mirrored.append(msg))

    res = await o.send("telegram", "1", "-1003142518418", "这条根本没发出去")

    assert res.get("delivered") is False
    assert FakeFailingSendWorker.last.sent, "worker 确实被调过"
    assert mirrored == [], "失败出站被镜像进收件箱＝伪造暴露面"
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)


class FakePeerWorker(FakeWorker):
    """带 ensure_peer 能力的假 worker（开演前 preflight 分发测试）。"""
    def __init__(self, account, config):
        super().__init__(account, config)
        FakePeerWorker.last = self
        self.peer_ok = True
        self.peer_raises = False
        self.queries = []

    async def ensure_peer(self, chat_key):
        self.queries.append(chat_key)
        if self.peer_raises:
            raise RuntimeError("flood wait")
        return self.peer_ok


async def test_ensure_peer_routes_to_worker_capability(registry):
    """worker 有 ensure_peer → 编排器转发真查，可达/不可达如实透传。"""
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)
    orch.register_worker("telegram", "protocol",
                         lambda a, c: FakePeerWorker(a, c))
    registry.upsert("telegram", "1", mode="protocol", status="online")
    o = AccountOrchestrator(registry=registry)
    await o.sync()
    w = FakePeerWorker.last

    ok = await o.ensure_peer("telegram", "1", "-100grp")
    assert ok == {"ok": True, "checked": True} and w.queries == ["-100grp"]

    w.peer_ok = False
    bad = await o.ensure_peer("telegram", "1", "-100grp")
    assert bad == {"ok": False, "checked": True}
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)


async def test_ensure_peer_is_inconclusive_when_unsupported_or_broken(registry):
    """无能力 worker / 查询抛异常 → ok=True, checked=False（放行不误杀）。"""
    # 默认 FakeWorker 没有 ensure_peer
    registry.upsert("telegram", "1", mode="protocol", status="online")
    o = AccountOrchestrator(registry=registry)
    await o.sync()
    r = await o.ensure_peer("telegram", "1", "-100grp")
    assert r == {"ok": True, "checked": False}
    # 完全没接管的号同理
    r2 = await o.ensure_peer("telegram", "ghost", "-100grp")
    assert r2 == {"ok": True, "checked": False}

    # 有能力但查询自身炸了（限流/断网）→ 不确定，放行
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)
    orch.register_worker("telegram", "protocol",
                         lambda a, c: FakePeerWorker(a, c))
    registry.upsert("telegram", "2", mode="protocol", status="online")
    o2 = AccountOrchestrator(registry=registry)
    await o2.sync()
    w2 = o2._managed[account_key("telegram", "2")].worker
    w2.peer_raises = True
    r3 = await o2.ensure_peer("telegram", "2", "-100grp")
    assert r3.get("ok") is True and r3.get("checked") is False
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)


class FakeInviteWorker(FakeWorker):
    """带 invite_to_group 的假 worker（排班补位分发测试）。"""
    def __init__(self, account, config):
        super().__init__(account, config)
        FakeInviteWorker.last = self
        self.invites = []

    async def invite_to_group(self, chat_key, user_ref):
        self.invites.append((chat_key, user_ref))
        return {"ok": True, "kind": "invited", "error": ""}


async def test_invite_to_group_dispatches_and_reports_unsupported(registry):
    """有能力 worker → 转发并透传回执；无能力 → unsupported 如实回报不装成功。"""
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)
    orch.register_worker("telegram", "protocol",
                         lambda a, c: FakeInviteWorker(a, c))
    registry.upsert("telegram", "1", mode="protocol", status="online")
    o = AccountOrchestrator(registry=registry)
    await o.sync()

    r = await o.invite_to_group("telegram", "1", "-100grp", "@newbie")
    assert r == {"ok": True, "kind": "invited", "error": ""}
    assert FakeInviteWorker.last.invites == [("-100grp", "@newbie")]

    r2 = await o.invite_to_group("telegram", "ghost", "-100grp", "@newbie")
    assert r2["ok"] is False and r2["kind"] == "unsupported"
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)


class FakeReadWorker(FakeWorker):
    """带 mark_read 的假 worker（拟人已读回执分发测试）。"""
    def __init__(self, account, config):
        super().__init__(account, config)
        self.read_chats = []

    async def mark_read(self, chat_key):
        self.read_chats.append(chat_key)
        return True


async def test_mark_read_dispatches_to_worker(registry):
    """worker 支持 mark_read → 编排器分发并返回 True。"""
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)
    orch.register_worker("telegram", "protocol",
                         lambda a, c: FakeReadWorker(a, c))
    registry.upsert("telegram", "1", mode="protocol", status="online")
    o = AccountOrchestrator(registry=registry)
    await o.sync()
    assert await o.mark_read("telegram", "1", "639111") is True
    assert FakeReadWorker.last.read_chats == ["639111"]
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)


async def test_mark_read_unsupported_worker_returns_false(registry):
    """worker 不支持 mark_read（如 WA/LINE）/ 无运行中 worker → False 且不抛。"""
    registry.upsert("telegram", "1", mode="protocol", status="online")
    o = AccountOrchestrator(registry=registry)
    await o.sync()
    # FakeWorker 无 mark_read
    assert await o.mark_read("telegram", "1", "639111") is False
    # 完全没有该账号
    assert await o.mark_read("telegram", "nope", "639111") is False


class FakeActionWorker(FakeWorker):
    """带 send_chat_action 的假 worker（打字状态分发测试）。"""
    def __init__(self, account, config):
        super().__init__(account, config)
        self.actions = []

    async def send_chat_action(self, chat_key, action="typing"):
        self.actions.append((chat_key, action))
        return True


async def test_send_chat_action_dispatches_to_worker(registry):
    """worker 支持 send_chat_action → 编排器分发并返回 True。"""
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)
    orch.register_worker("telegram", "protocol",
                         lambda a, c: FakeActionWorker(a, c))
    registry.upsert("telegram", "1", mode="protocol", status="online")
    o = AccountOrchestrator(registry=registry)
    await o.sync()
    assert await o.send_chat_action("telegram", "1", "639111", "typing") is True
    assert FakeActionWorker.last.actions == [("639111", "typing")]
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)


async def test_send_chat_action_unsupported_returns_false(registry):
    """worker 不支持 send_chat_action / 无运行中 worker → False 且不抛。"""
    registry.upsert("telegram", "1", mode="protocol", status="online")
    o = AccountOrchestrator(registry=registry)
    await o.sync()
    assert await o.send_chat_action("telegram", "1", "639111") is False
    assert await o.send_chat_action("telegram", "nope", "639111") is False


async def test_mark_read_typing_record_per_platform_metrics(registry):
    """已读/打字按平台记指标（成功计 ok、不支持计 fail），供 autosend-status 观测。"""
    import src.integrations.humanize_metrics as hm
    hm.reset()
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)
    orch.register_worker("telegram", "protocol",
                         lambda a, c: FakeReadWorker(a, c))  # 有 mark_read 无 send_chat_action
    registry.upsert("telegram", "1", mode="protocol", status="online")
    o = AccountOrchestrator(registry=registry)
    await o.sync()
    await o.mark_read("telegram", "1", "639111")       # ok
    await o.send_chat_action("telegram", "1", "639111")  # 不支持 → fail
    snap = hm.snapshot()
    assert snap["telegram"]["read_ok"] == 1
    assert snap["telegram"]["typing_fail"] == 1
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)
    hm.reset()
