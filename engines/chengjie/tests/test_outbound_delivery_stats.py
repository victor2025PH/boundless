"""出站投递观测：分类器纯函数 + 编排器漏斗接线。

分成两层测：
- 分类器是纯函数，用真实 worker 会返回的形状喂（不是我想象的形状）；
- 接线走**真编排器 + 假 worker**——「模块自己算得对」和「生产路径真的会调它」
  是两件事，只测前者的观测模块历史上一次次地静默没接上。
"""

from __future__ import annotations

import os
import tempfile

import pytest

from src.integrations import account_orchestrator as orch
from src.integrations.account_orchestrator import AccountOrchestrator
from src.integrations.account_registry import AccountRegistry
from src.integrations.shared import outbound_delivery_stats as ods
from src.integrations.shared.outbound_delivery_stats import (
    OutboundDeliveryStats,
    classify_exception,
    classify_result,
    record_send_result,
)

# ---------------------------------------------------------------- 分类器


def test_non_dict_return_counts_as_sent():
    """老 worker 契约：不返回 dict 即成功。与编排器「要不要回写收件箱」同口径。"""
    assert classify_result(None) == ("sent", "")
    assert classify_result("ok") == ("sent", "")


def test_missing_delivered_key_is_optimistic():
    assert classify_result({"message_id": "1"}) == ("sent", "")
    assert classify_result({"delivered": True}) == ("sent", "")


def test_structured_failure_keeps_its_reason():
    assert classify_result(
        {"delivered": False, "error": "forbidden"}) == ("failed", "forbidden")


def test_failure_without_error_token_is_not_silently_dropped():
    """没写 error 的失败仍要计数——归到 unspecified，让人看得见「谁没写原因」。"""
    assert classify_result({"delivered": False}) == ("failed", "unspecified")


def test_blocked_keeps_only_the_family_not_the_scope():
    """kill_switch:telegram:acct-7 → kill_switch。冒号后是账号/scope，会炸维度基数。"""
    assert classify_result(
        {"delivered": False, "blocked": "kill_switch:global"}) == ("blocked", "kill_switch")
    assert classify_result(
        {"delivered": False, "blocked": "send_gate:hourly_cap"}) == ("blocked", "send_gate")


def test_aliases_merge_so_the_card_does_not_need_mental_arithmetic():
    """同一个失败在不同 worker 里叫不同名字，看板上必须是一行。"""
    for token in ("too_large", "media_too_large", "file_too_large"):
        assert classify_result(
            {"delivered": False, "error": token})[1] == "media_too_large"
    for token in ("chat_not_found", "not_found", "PEER_ID_INVALID", "user_deactivated"):
        assert classify_result(
            {"delivered": False, "error": token})[1] == "peer_invalid"


@pytest.mark.parametrize("msg,expect", [
    ("Read timed out after 30s", "timeout"),
    ("403 Forbidden: Cannot send messages to this user", "forbidden"),
    ("error code 50007", "forbidden"),
    ("429 Too Many Requests", "rate_limited"),
    ("FLOOD_WAIT_42", "rate_limited"),
    ("PEER_ID_INVALID", "peer_invalid"),
    ("Request entity too large (413)", "media_too_large"),
    ("Connection reset by peer", "network"),
])
def test_exception_messages_map_to_bounded_reasons(msg, expect):
    assert classify_exception(RuntimeError(msg)) == expect


def test_unknown_exception_keeps_its_class_name_instead_of_other():
    """折叠成 other 等于放弃归因——"other 涨了 5000" 什么也没说。类名由代码决定、有界。"""
    class WeirdProtocolError(Exception):
        pass

    assert classify_exception(WeirdProtocolError("???")) == "exc_weirdprotocolerror"


def test_exception_text_never_leaks_into_the_dimension():
    """异常串含 peer id / 文件路径 / 平台原文——绝不能进维度值。"""
    r = classify_exception(RuntimeError("failed for user 8244899900 at D:/x/y.jpg"))
    assert "8244899900" not in r and "/" not in r


def test_timeout_beats_network_when_both_words_appear():
    """顺序敏感：'connection timeout' 是超时，不是断网——归错桶会误导排障方向。"""
    assert classify_exception(OSError("connection timeout")) == "timeout"


# ---------------------------------------------------------------- 计数器


def test_failure_rate_counts_blocked_as_not_delivered():
    """护栏拦下的那条客户同样没收到。分母含 blocked 是刻意的口径选择。"""
    s = OutboundDeliveryStats()
    for _ in range(8):
        s.record("telegram", outcome="sent")
    s.record("telegram", outcome="failed", reason="forbidden")
    s.record("telegram", outcome="blocked", reason="kill_switch")
    d = s.dump()
    assert d["attempts"] == 10 and d["sent"] == 8
    assert d["failure_rate"] == 0.2


def test_success_is_recorded_so_the_card_has_a_denominator():
    """只报失败数的看板是骗人的：40/40 和 40/40000 是两种处境。"""
    s = OutboundDeliveryStats()
    s.record("discord", outcome="sent")
    assert s.dump()["by_platform"]["discord"]["sent"] == 1


def test_reasons_are_scoped_per_platform():
    """「哪个平台在拒」是第一个要回答的问题，原因不能全平台混成一坨。"""
    s = OutboundDeliveryStats()
    s.record("discord", outcome="failed", reason="forbidden")
    s.record("whatsapp", outcome="failed", reason="session_unhealthy")
    by = s.dump()["by_reason"]
    assert by["discord"] == {"forbidden": 1}
    assert by["whatsapp"] == {"session_unhealthy": 1}


def test_last_failure_is_kept_for_the_card_but_only_for_failures():
    s = OutboundDeliveryStats()
    s.record("discord", outcome="failed", reason="forbidden", kind="media")
    s.record("discord", outcome="sent")
    lf = s.dump()["last_failure"]
    assert lf["platform"] == "discord" and lf["reason"] == "forbidden"
    assert lf["kind"] == "media"


def test_distinct_key_flood_cannot_eat_memory_or_explode_prom_labels():
    """维度基数是这类模块的死法。上限之外归 __other__ 并计 overflow。"""
    s = OutboundDeliveryStats()
    for i in range(ods._MAX_KEYS + 25):
        s.record(f"plat{i}", outcome="failed", reason=f"reason{i}")
    d = s.dump()
    assert len(d["by_platform"]) <= ods._MAX_KEYS + 1
    assert len(s._by_reason) <= ods._MAX_KEYS + 1
    assert d["overflow"] > 0
    assert d["failed"] == ods._MAX_KEYS + 25      # 计数不因归并而丢


def test_prom_output_is_well_formed_and_escapes_labels():
    s = OutboundDeliveryStats()
    s.record("discord", outcome="failed", reason='we"ird')
    text = s.dump_prom()
    assert "# TYPE outbound_delivery_total counter" in text
    assert 'outbound_delivery_total{platform="discord",outcome="failed"} 1' in text
    assert '\\"' in text or "weird" in text     # 引号被转义或被消毒掉，二者皆可
    for line in text.strip().splitlines():
        assert line.startswith("#") or line.count(" ") >= 1


def test_recorder_never_raises_even_on_garbage():
    """观测绝不能弄坏发送。喂什么都不许抛。"""
    record_send_result("telegram", res=object())
    record_send_result("", kind="???", res={"delivered": False, "error": None})
    record_send_result("telegram", exc=KeyboardInterrupt())


# ---------------------------------------------------------------- 编排器接线


class _Worker:
    """可编排结果的假 worker。"""
    last = None
    result = {"delivered": True, "message_id": "M1"}
    raises = None

    def __init__(self, account, config):
        self.account = account
        _Worker.last = self

    async def start(self):
        return None

    async def stop(self):
        return None

    async def healthy(self):
        return True

    def status(self):
        return {"type": "fake", "healthy": True}

    async def send(self, chat_key, text):
        if _Worker.raises is not None:
            raise _Worker.raises
        return _Worker.result

    async def send_media(self, chat_key, *, media_path, media_type="", caption=""):
        if _Worker.raises is not None:
            raise _Worker.raises
        return _Worker.result


@pytest.fixture()
def wired(monkeypatch):
    """真编排器 + 假 worker + 干净计数器；顺带屏蔽收件箱回写。"""
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)
    orch.register_worker("telegram", "protocol", lambda a, c: _Worker(a, c))
    _Worker.result = {"delivered": True, "message_id": "M1"}
    _Worker.raises = None
    # 抓住真单例再 yield：有的用例会 monkeypatch 掉 getter（模拟观测层炸掉），
    # teardown 时再去查一次就会撞上那个假货。
    stats = ods.get_outbound_delivery_stats()
    stats.reset()
    import src.integrations.protocol_bridge as pb
    monkeypatch.setattr(pb, "emit_incoming", lambda msg: None)
    reg = AccountRegistry(os.path.join(tempfile.mkdtemp(), "acc.db"))
    reg.upsert("telegram", "1", mode="protocol", status="online")
    yield reg
    orch._WORKER_FACTORIES.pop("telegram:protocol", None)
    stats.reset()


async def _orchestrator(reg):
    o = AccountOrchestrator(registry=reg)
    await o.sync()
    return o


async def test_successful_send_moves_the_denominator(wired):
    o = await _orchestrator(wired)
    await o.send("telegram", "1", "chat-1", "hello")
    d = ods.get_outbound_delivery_stats().dump()
    assert d["sent"] == 1 and d["failed"] == 0
    assert d["by_kind"]["text"]["sent"] == 1


async def test_structured_failure_is_counted_with_its_reason(wired):
    o = await _orchestrator(wired)
    _Worker.result = {"delivered": False, "error": "forbidden"}
    await o.send("telegram", "1", "chat-1", "hello")
    d = ods.get_outbound_delivery_stats().dump()
    assert d["failed"] == 1
    assert d["by_reason"]["telegram"] == {"forbidden": 1}


async def test_raised_exception_is_counted_and_still_propagates(wired):
    """记账不吞异常——上游的重试/告警逻辑必须照旧收到它。"""
    o = await _orchestrator(wired)
    _Worker.raises = RuntimeError("PEER_ID_INVALID")
    with pytest.raises(RuntimeError):
        await o.send("telegram", "1", "chat-1", "hello")
    d = ods.get_outbound_delivery_stats().dump()
    assert d["failed"] == 1
    assert d["by_reason"]["telegram"] == {"peer_invalid": 1}


async def test_media_path_is_wired_too_not_just_text(wired, tmp_path):
    o = await _orchestrator(wired)
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x" * 16)
    _Worker.result = {"delivered": False, "error": "too_large"}
    await o.send_media("telegram", "1", "chat-1", media_path=str(f),
                       media_url="/static/a.jpg", media_type="image")
    d = ods.get_outbound_delivery_stats().dump()
    assert d["by_kind"]["media"]["failed"] == 1
    assert d["by_reason"]["telegram"] == {"media_too_large": 1}


async def test_guard_blocked_sends_are_visible_as_blocked_not_lost(wired, monkeypatch):
    """Kill-Switch 拦下的消息客户同样没收到，看板不能装作无事发生。"""
    monkeypatch.setattr(orch, "send_blocked",
                        lambda *a, **k: (True, "kill_switch:global"))
    o = await _orchestrator(wired)
    res = await o.send("telegram", "1", "chat-1", "hello")
    assert res.get("delivered") is False
    d = ods.get_outbound_delivery_stats().dump()
    assert d["blocked"] == 1 and d["sent"] == 0
    assert d["by_reason"]["telegram"] == {"kill_switch": 1}


async def test_missing_worker_is_attributed_before_the_raise(wired):
    """「号没起来」是最常见的发不出去，不能只表现为一个上抛的 RuntimeError。"""
    o = await _orchestrator(wired)
    with pytest.raises(RuntimeError):
        await o.send("telegram", "does-not-exist", "chat-1", "hello")
    d = ods.get_outbound_delivery_stats().dump()
    assert d["by_reason"]["telegram"] == {"no_worker": 1}


async def test_orchestrator_owned_send_via_adapters_is_counted_exactly_once(
        wired, monkeypatch):
    """双记录点的核心不变量：一次发送只产生一条记录。

    send_via_adapters 在编排器拥有该账号时转发给 orch.send（已计），否则走适配器
    （在那里计）。两条分支互斥返回——若哪天有人把它改成「先试编排器失败再落适配器」，
    同一次发送就会被记两次，失败率从此虚高，这条用例先红。
    """
    from src.inbox import channel_adapters as ca
    o = await _orchestrator(wired)
    monkeypatch.setattr("src.integrations.account_orchestrator.get_orchestrator",
                        lambda: o)
    res = await ca.send_via_adapters(None, "telegram", "1", "chat-1", "hi", [])
    assert res.get("delivered") is True
    d = ods.get_outbound_delivery_stats().dump()
    assert d["attempts"] == 1, f"一次发送被记了 {d['attempts']} 次"


async def test_adapter_fallback_is_counted_so_the_platform_is_not_a_silent_zero(
        wired, monkeypatch):
    """非编排器托管的账号（TG default / Messenger 网页 / RPA）也必须出现在看板上。

    不计的话该平台恒为 0 次出站，运维会读成「这平台没问题」——比没有看板更糟。
    """
    from src.inbox import channel_adapters as ca

    class _Adapter:
        platform = "messenger"

        async def send(self, request, account_id, chat_key, text):
            return {"delivered": False, "error": "session_unhealthy"}

    monkeypatch.setattr("src.integrations.account_orchestrator.get_orchestrator",
                        lambda: (_ for _ in ()).throw(RuntimeError("no orch")))
    res = await ca.send_via_adapters(None, "messenger", "acc", "c1", "hi", [_Adapter()])
    assert res.get("delivered") is False
    d = ods.get_outbound_delivery_stats().dump()
    assert d["failed"] == 1
    assert d["by_reason"]["messenger"] == {"session_unhealthy": 1}


async def test_broken_observability_does_not_break_sending(wired, monkeypatch):
    """计数器炸了也必须把消息发出去——观测是附属品，不是前置条件。"""
    def _boom(*a, **k):
        raise RuntimeError("stats exploded")

    monkeypatch.setattr(ods, "get_outbound_delivery_stats", _boom)
    o = await _orchestrator(wired)
    res = await o.send("telegram", "1", "chat-1", "hello")
    assert res.get("delivered") is True


# ── P0 增量：queued / 占坑去重 / meter_send / 官方 API ok 形状 ──────────────


def test_queued_is_not_counted_as_sent_or_attempt():
    """入队 ≠ 已送达：按 sent 记会让「手机关机、队列积压」显示成 100% 成功。"""
    s = ods.get_outbound_delivery_stats()
    s.reset()
    s.record("line", kind="text", outcome="queued")
    d = s.dump()
    assert d["queued"] == 1
    assert d["attempts"] == 0
    assert d["sent"] == 0
    assert d["failure_rate"] == 0.0


def test_classify_result_marks_queued_even_when_delivered_true():
    assert ods.classify_result({"delivered": True, "queued": True}) == ("queued", "")


def test_classify_ok_result_pulls_error_from_rpa_parts():
    """RPA `_pace_and_send` 顶层常只有 ok；真实原因在 parts 里，丢掉就只剩 platform_error。"""
    out = {"ok": False, "parts": [{"ok": False, "error": "input_field_not_found"}]}
    assert ods.classify_ok_result(out) == ("failed", "input_field_not_found")


def test_classify_ok_result_skips_empty_noop():
    assert ods.classify_ok_result({"ok": True, "data": {"skipped": "empty"}}) == ("", "")


def test_inner_leaf_yields_when_outer_has_claimed():
    """编排器已占坑时，内层 only_if_outermost 必须让位——否则一次发送记两条。"""
    s = ods.get_outbound_delivery_stats()
    s.reset()
    with ods.claim_send():
        ods.record_send_result("telegram", kind="text",
                               res={"delivered": True}, only_if_outermost=True)
        assert s.dump()["attempts"] == 0
    # 外层自己记（模拟编排器在 claim 外/后的记账；实际编排器在 claim 内记，
    # 这里只验证「内层让位」本身）。
    ods.record_send_result("telegram", kind="text", res={"delivered": True})
    assert s.dump()["attempts"] == 1


def test_inner_leaf_records_when_no_outer_claim():
    """纯 A 线 / 纯 RPA 旁路没有外层占坑——叶子必须自己记，否则那条栈永久静默。"""
    s = ods.get_outbound_delivery_stats()
    s.reset()
    ods.record_send_result("telegram", kind="text", outcome="sent",
                            only_if_outermost=True)
    assert s.dump()["sent"] == 1


@pytest.mark.asyncio
async def test_meter_send_counts_ok_shape_and_respects_claim():
    """装饰器是叶子出口的标准接法——成功路径记账，外层占坑时让位。"""

    @ods.meter_send("whatsapp", kind="text", shape="ok")
    async def _leaf(ok=True, err=""):
        return {"ok": ok, "error": err}

    s = ods.get_outbound_delivery_stats()
    s.reset()
    assert (await _leaf()).get("ok") is True
    assert s.dump()["sent"] == 1

    s.reset()
    with ods.claim_send():
        await _leaf(ok=False, err="text_inject_fail")
    assert s.dump()["attempts"] == 0  # 让位给外层

    s.reset()
    assert (await _leaf(ok=False, err="text_inject_fail")).get("ok") is False
    assert s.dump()["by_reason"]["whatsapp"] == {"text_inject_fail": 1}


@pytest.mark.asyncio
async def test_meter_send_records_exception_then_rethrows():
    @ods.meter_send("line", kind="text", shape="ok")
    async def _boom():
        raise TimeoutError("connection timed out")

    s = ods.get_outbound_delivery_stats()
    s.reset()
    with pytest.raises(TimeoutError):
        await _boom()
    assert s.dump()["by_reason"]["line"] == {"timeout": 1}


@pytest.mark.asyncio
async def test_adapter_queued_result_does_not_inflate_success_rate(wired, monkeypatch):
    """RPA 适配器「已入队」不能抬高成功率——否则队列卡死时看板仍绿灯。"""
    from src.inbox import channel_adapters as ca

    class _Adapter:
        platform = "line"

        async def send(self, request, account_id, chat_key, text):
            return {"delivered": True, "queued": True}

    monkeypatch.setattr("src.integrations.account_orchestrator.get_orchestrator",
                        lambda: (_ for _ in ()).throw(RuntimeError("no orch")))
    res = await ca.send_via_adapters(None, "line", "acc", "c1", "hi", [_Adapter()])
    assert res.get("queued") is True
    d = ods.get_outbound_delivery_stats().dump()
    assert d["queued"] == 1
    assert d["sent"] == 0
    assert d["attempts"] == 0
