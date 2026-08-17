# -*- coding: utf-8 -*-
"""双面板融合第二批门禁（telegram / whatsapp，2026-08-13）。

钉住三层不变量（msgr 首批语义见 test_surface_fusion.py，勿重复）：

- **tg/wa 注册表契约**：状态枚举合法 / bridge 必有合法目标 / i18n 双语齐平
  （能力 id 全复用首批词表＝零新键，此处防有人加新 id 忘补词条）；
- **承重事实钉**（与 worker 代码实测同源，改 worker 能力必须同步这里）：
  quote_reply workspace=none（2026-08-14 三平台工作台引用入口整体下线，老板拍板；
  worker 的 reply_to 透传代码保留，恢复前置=quote_applied 回执）；
  两平台原生页为完整注入档（受控出站
  链在）→ autosend native=assist（≠ msgr 的 none）——两面都能自动化，正是
  tg/wa 必须上驾驶权锁的原因；
- **A 线（protocol_autoreply 协议直发链）驾驶权让位**：fusion 开 + owner=native
  → 决策期早退（不生成不发、不打「需人工」、不进审计）；fusion 关（默认档）
  → 零行为变更；owner=workspace → 照发。P0 只闸了 AutosendWorker，而 ChatX
  独立包默认档（deliver=false）下 A 线才是自动发送主力——本组测试钉住锁闭合。
"""
from __future__ import annotations

import pytest

from src.integrations import protocol_autoreply as pa
from src.integrations import surface_fusion as sf


@pytest.fixture()
def pilot_env(tmp_path, monkeypatch):
    """锁文件隔离进 tmp + 缓存复位（与 test_surface_fusion.py 同款）。"""
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AITR_CONFIG_PATH", raising=False)
    sf._reset_cache_for_tests()
    yield tmp_path
    sf._reset_cache_for_tests()


# ── tg/wa 注册表契约 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("platform", ["telegram", "whatsapp"])
def test_registry_status_enums_and_bridge_targets(platform):
    rows = sf.capability_matrix(platform)
    assert rows, f"{platform} 必须有策划注册表（第二批）"
    for r in rows:
        assert r["workspace"] in ("ok", "bridge", "none"), r
        assert r["native"] in ("ok", "assist", "none"), r
        if r["workspace"] == "bridge":
            assert r["bridge_to"] == sf.SURFACE_NATIVE, r
            assert r["native"] == "ok", r
        assert r["phase"] in ("P0", "P1", "P2", "P3"), r


@pytest.mark.parametrize("platform", ["telegram", "whatsapp"])
def test_registry_labels_bilingual(platform):
    from src.web.i18n_packs import surface_fusion as pack
    for r in sf.capability_matrix(platform):
        key = r["label_key"]
        assert key in pack.ZH and str(pack.ZH[key]).strip(), key
        assert key in pack.EN and str(pack.EN[key]).strip(), key


def test_registry_telegram_core_rows_pinned():
    by_id = {r["id"]: r for r in sf.capability_matrix("telegram")}
    # 双面板已对齐的核心能力（协议 worker：send/send_media/mark_read/send_chat_action）
    for cap in ("send_text", "send_media", "mark_read", "typing"):
        assert by_id[cap]["workspace"] == "ok", cap
        assert by_id[cap]["native"] == "ok", cap
    # 引用回复（2026-08-14 工作台入口整体下线，老板拍板）：链路无 quote_applied 回执、
    # 镜像无条件渲染引用条 →「坐席见引用、手机端没有」（173 实录）→ UI 入口已删，
    # workspace=none 是诚实态（worker send(reply_to=) 代码保留，恢复前置=回执落地）。
    assert by_id["quote_reply"]["workspace"] == "none"
    assert by_id["quote_reply"]["native"] == "ok"
    # 表情回应：两侧 worker 均无方法，原生页先顶
    assert by_id["reaction_out"]["workspace"] == "none"
    # 原生页=完整注入档（桌面受控出站链在）→ autosend assist（≠ msgr 的 none）
    assert by_id["autosend"]["workspace"] == "ok"
    assert by_id["autosend"]["native"] == "assist"
    # 通话/转发/置顶/举报＝桥接去原生页
    for cap in ("calls", "forward", "pin_manage", "report"):
        assert by_id[cap]["workspace"] == "bridge", cap
        assert by_id[cap]["bridge_to"] == "native", cap
    # messenger 专属概念不得混入（消息请求/E2EE PIN 是 msgr 语义）
    assert "message_requests" not in by_id
    assert "e2ee_pin" not in by_id


def test_registry_whatsapp_core_rows_pinned():
    by_id = {r["id"]: r for r in sf.capability_matrix("whatsapp")}
    for cap in ("send_text", "send_media", "mark_read", "typing"):
        assert by_id[cap]["workspace"] == "ok", cap
        assert by_id[cap]["native"] == "ok", cap
    # 引用回复（2026-08-14 三平台同刀下线）：WA Baileys quoted 透传可用但工作台
    # 不再提供引用入口 → workspace=none（worker 代码保留）。
    assert by_id["quote_reply"]["workspace"] == "none"
    assert by_id["reaction_out"]["workspace"] == "none"
    assert by_id["autosend"]["workspace"] == "ok"
    assert by_id["autosend"]["native"] == "assist"
    for cap in ("calls", "forward", "pin_manage", "report"):
        assert by_id[cap]["workspace"] == "bridge", cap
    assert "message_requests" not in by_id
    assert "e2ee_pin" not in by_id


@pytest.mark.parametrize("platform", ["telegram", "whatsapp"])
def test_capability_summary_partitions(platform):
    rows = sf.capability_matrix(platform)
    s = sf.capability_summary(platform)
    allocated = [cid for g in ("both", "workspace_only", "native_only", "bridge")
                 for cid in s[g]]
    assert len(allocated) == len(set(allocated)), "能力不得落进多个组"
    assert set(allocated) <= {r["id"] for r in rows}
    # tg/wa 语义核心：autosend 落「both」——两面都能自动化（workspace 全自动 +
    # native 受控出站 assist），驾驶权锁因此必要；msgr 那边是 workspace_only。
    assert "autosend" in s["both"]
    assert "calls" in s["bridge"]
    assert "reaction_out" in s["native_only"]


# ── A 线（protocol_autoreply）驾驶权让位 ─────────────────────────────────────


class _FakeRegistry:
    def __init__(self, row):
        self._row = row

    def get(self, platform, account_id):
        return self._row


def _payload(platform="telegram", account_id="tg1", chat_key="123"):
    return {
        "platform": platform, "account_id": account_id, "chat_key": chat_key,
        "text": "在吗", "direction": "in",
    }


def _row():
    return {"platform": "telegram", "account_id": "tg1",
            "meta": {"auto_reply": True}}


@pytest.fixture(autouse=True)
def _clear_pa_state():
    pa._last_reply.clear()
    pa._last_sent.clear()
    yield
    pa._last_reply.clear()
    pa._last_sent.clear()


def _make_send(sink):
    async def _send(**kw):
        sink.append(kw)
        return {"delivered": True}
    return _send


def _make_gen(reply, calls=None):
    async def _gen(**kw):
        if calls is not None:
            calls.append(kw)
        return reply
    return _gen


def _cfg(fusion_enabled):
    return {
        "protocol_autoreply": {"enabled": True},
        "surface_fusion": {"enabled": bool(fusion_enabled)},
    }


@pytest.mark.asyncio
async def test_a_chain_yields_when_pilot_native(pilot_env):
    """fusion 开 + owner=native → 决策期早退：不生成、不发、原因码 pilot_native。"""
    sf.set_pilot("telegram", "tg1", "native")
    sent, gen_calls = [], []
    res = await pa.run_autoreply(
        _payload(), registry=_FakeRegistry(_row()), cfg=_cfg(True),
        generate=_make_gen("在的", gen_calls), send=_make_send(sent),
        risk_fn=lambda t: "low",
    )
    assert res["skipped"] == "pilot_native"
    assert gen_calls == [] and sent == []
    # 归属让位不是故障：不打「需人工」、不进审计面
    assert not pa.needs_handoff(res)
    assert "pilot_native" not in pa.AUDIT_REASONS


@pytest.mark.asyncio
async def test_a_chain_default_off_zero_behavior_change(pilot_env):
    """fusion 关（默认档）：owner=native 也照发——锁未启用零行为变更。"""
    sf.set_pilot("telegram", "tg1", "native")
    sent = []
    res = await pa.run_autoreply(
        _payload(), registry=_FakeRegistry(_row()), cfg=_cfg(False),
        generate=_make_gen("在的"), send=_make_send(sent),
        risk_fn=lambda t: "low",
    )
    assert res.get("sent") is True
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_a_chain_workspace_owner_sends(pilot_env):
    """fusion 开 + owner=workspace（默认）→ 照发；账号键隔离（别的账号 native 不殃及）。"""
    sf.set_pilot("telegram", "other-acct", "native")
    sent = []
    res = await pa.run_autoreply(
        _payload(), registry=_FakeRegistry(_row()), cfg=_cfg(True),
        generate=_make_gen("在的"), send=_make_send(sent),
        risk_fn=lambda t: "low",
    )
    assert res.get("sent") is True
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_a_chain_pilot_switch_back_resumes(pilot_env):
    """native → workspace 切回后 A 线立即恢复（mtime 缓存失效语义）。"""
    sf.set_pilot("whatsapp", "wa1", "native")
    sent = []
    p = _payload(platform="whatsapp", account_id="wa1", chat_key="8613800138000")
    r1 = await pa.run_autoreply(
        p, registry=_FakeRegistry(_row()), cfg=_cfg(True),
        generate=_make_gen("hi"), send=_make_send(sent),
        risk_fn=lambda t: "low",
    )
    assert r1["skipped"] == "pilot_native" and sent == []
    sf.set_pilot("whatsapp", "wa1", "workspace")
    r2 = await pa.run_autoreply(
        p, registry=_FakeRegistry(_row()), cfg=_cfg(True),
        generate=_make_gen("hi"), send=_make_send(sent),
        risk_fn=lambda t: "low",
    )
    assert r2.get("sent") is True and len(sent) == 1
