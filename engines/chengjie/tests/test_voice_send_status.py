# -*- coding: utf-8 -*-
"""坐席语音发送对账主线门禁（P1-1/P1-2/P1-3 2026-08-11）。

三件事各自的不变量：
1. **对账表 + 端点**（voice_send_tracker / send-voice-status）：前端超时 ≠ 发送
   失败——服务端可能仍在合成并最终发出，坐席盲目重试＝客户收两条。表按
   (scope, client_msg_id) 记阶段与终局，端点只读查询；unknown 必须保守。
2. **前端接线**：sendVoiceReply 超时分支必须走 _reconcileVoiceSend（先对账再
   定论），等待期渲染秒表/真实阶段；_refreshVoiceRisk 必须含人设一致性告警
   （实录：文字以林小雨回复、语音是苏婉克隆声——localStorage 会话音色偏好在
   换绑人设后成为陈旧粘滞源）。
3. **录音脉冲 + 分段耗时**：send-voice 合成前/后各挂一次「正在录音」（orch
   能力探测内置，best-effort）；成功日志带 stage_ms（慢在哪一段可查）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.inbox import voice_send_tracker as vst

_ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ── 对账表纯函数 ─────────────────────────────────────────────────────────────
def test_tracker_lifecycle_and_stages():
    vst.reset_state()
    vst.record_start("voice:tg:a:c1", "id1")
    st = vst.get_status("voice:tg:a:c1", "id1")
    assert st["state"] == "in_flight" and st["stage"] == "synth"
    vst.record_stage("voice:tg:a:c1", "id1", "convert")
    assert vst.get_status("voice:tg:a:c1", "id1")["stage"] == "convert"
    vst.record_stage("voice:tg:a:c1", "id1", "send")
    vst.record_sent("voice:tg:a:c1", "id1",
                    {"voice_meta": {"provider": "hub_fish"},
                     "reused_preview": True})
    st = vst.get_status("voice:tg:a:c1", "id1")
    assert st["state"] == "sent"
    assert st["voice_meta"]["provider"] == "hub_fish"
    assert st["reused_preview"] is True
    vst.reset_state()


def test_tracker_failed_and_terminal_stage_frozen():
    vst.reset_state()
    vst.record_start("s", "i")
    vst.record_failed("s", "i", "lang_mismatch")
    st = vst.get_status("s", "i")
    assert st["state"] == "failed" and st["reason"] == "lang_mismatch"
    # 终局后阶段推进必须被忽略（防迟到的 stage 覆盖结论）
    vst.record_stage("s", "i", "send")
    assert vst.get_status("s", "i")["state"] == "failed"
    vst.reset_state()


def test_tracker_unknown_and_noop_on_empty_keys():
    vst.reset_state()
    assert vst.get_status("s", "missing")["state"] == "unknown"
    assert vst.get_status("", "")["state"] == "unknown"
    vst.record_start("", "")          # no-op，不得抛
    vst.record_sent("", "", {})
    vst.record_failed("", "")
    vst.reset_state()


def test_tracker_ttl_expiry_reports_unknown():
    vst.reset_state()
    vst.record_start("s", "old", now=1000.0)
    assert vst.get_status("s", "old", now=1000.0 + 14 * 60)["state"] == "in_flight"
    assert vst.get_status("s", "old", now=1000.0 + 16 * 60)["state"] == "unknown"
    vst.reset_state()


def test_tracker_cap_evicts_oldest():
    vst.reset_state()
    for i in range(420):
        vst.record_start("s", f"i{i}", now=1000.0 + i)
    assert vst.get_status("s", "i0", now=1500.0)["state"] == "unknown"    # 被挤出
    assert vst.get_status("s", "i419", now=1500.0)["state"] == "in_flight"
    vst.reset_state()


# ── 对账端点（薄包装：登记表真值 → 响应）──────────────────────────────────────
def test_send_voice_status_route(auth_client):
    vst.reset_state()
    scope = "voice:telegram:acct1:12345"
    vst.record_start(scope, "cid-1")
    vst.record_stage(scope, "cid-1", "send")
    r = auth_client.get(
        "/api/unified-inbox/send-voice-status",
        params={"platform": "telegram", "account_id": "acct1",
                "chat_key": "12345", "client_msg_id": "cid-1"})
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["state"] == "in_flight" and d["stage"] == "send"
    vst.record_sent(scope, "cid-1", {"voice_meta": {"provider": "avatar_clone"}})
    d2 = auth_client.get(
        "/api/unified-inbox/send-voice-status",
        params={"platform": "telegram", "account_id": "acct1",
                "chat_key": "12345", "client_msg_id": "cid-1"}).json()
    assert d2["state"] == "sent"
    assert d2["voice_meta"]["provider"] == "avatar_clone"
    # 查无此键：unknown（保守）
    d3 = auth_client.get(
        "/api/unified-inbox/send-voice-status",
        params={"platform": "telegram", "account_id": "acct1",
                "chat_key": "12345", "client_msg_id": "nope"}).json()
    assert d3["state"] == "unknown"
    vst.reset_state()


# ── 路由接线（源码断言：参数被摘掉即红）──────────────────────────────────────
def test_send_voice_route_wired_to_tracker_and_pulse():
    src = _read("src/web/routes/unified_inbox_send_routes.py")
    seg = src[src.index("async def api_unified_inbox_send_voice"):]
    seg = seg[:seg.index("async def api_unified_inbox_send_voice_status")]
    assert "record_start(_dedup_scope, _client_msg_id)" in seg
    assert seg.count("_vst.record_failed(") >= 6, "每个失败早退口都必须落终局"
    assert "record_sent(_dedup_scope, _client_msg_id" in seg
    assert seg.count("_fire_voice_recording_action(") >= 2, \
        "「正在录音」合成前/合成后各一次"
    assert "stage_ms=synth:%d/conv:%d/send:%d" in seg, "成功日志必须带分段耗时"


def test_status_route_registered_and_ledgered():
    src = _read("src/web/routes/unified_inbox_send_routes.py")
    assert '@app.get("/api/unified-inbox/send-voice-status")' in src
    ledger = _read("tests/test_admin_route_inventory.py")
    assert "/api/unified-inbox/send-voice-status\tGET" in ledger


# ── 前端接线 ────────────────────────────────────────────────────────────────
def test_composer_timeout_reconciles_not_blind_fail():
    html = _read("src/web/templates/unified_inbox.html")
    si = html.index("async function sendVoiceReply")
    seg = html[si:si + 6000]
    assert "_reconcileVoiceSend(" in seg, "超时分支必须先对账再定论"
    assert "inbox.voice.result_unknown" in seg, "unknown 必须保守提示（防盲目重发）"
    assert "inbox.voice.stage_synth" in seg, "等待期必须渲染阶段+秒表"
    assert "convKey(selectedChat)===convKey(sendChat)" in seg, \
        "切会话后不得清新会话的输入框草稿"
    hi = html.index("async function _reconcileVoiceSend")
    hseg = html[hi:hi + 2000]
    assert "send-voice-status" in html[html.index("async function _voiceSendStatus"):hi]
    assert "unknown" in hseg


def test_voice_risk_row_covers_persona_mismatch():
    html = _read("src/web/templates/unified_inbox.html")
    ri = html.index("async function _refreshVoiceRisk")
    rseg = html[ri:ri + 4000]
    assert "risk_persona_mismatch" in rseg
    assert "_convIdentityPersona(" in rseg
    assert "__system__" in rseg, "系统通用音色是刻意选择，不得误警"


def test_voice_pref_carries_identity_snapshot_and_invalidates():
    """粘滞偏好根治（P2 2026-08-11）：localStorage 会话音色偏好带「选择时的会话
    身份」快照；会话换绑人设 → 快照失配 → 旧选择自动作废——苏婉/林小雨实录
    （换了身份还粘着旧音色）的源头修复。三个写/读点缺一即红。"""
    html = _read("src/web/templates/unified_inbox.html")
    oi = html.index("function _onVoicePersonaChange")
    oseg = html[oi:oi + 1200]
    assert "ident:" in oseg, "下拉写偏好必须带身份快照"
    li = html.index("function _loadVoiceForConv")
    lseg = html[li:li + 2400]
    assert "raw.ident" in lseg and "raw.ident!==cur" in lseg, \
        "身份换绑必须作废旧偏好"
    assert "typeof raw==='string'" in lseg, "旧格式（裸字符串）必须兼容"
    vi = html.index("function _vcmSetConv")
    vseg = html[vi:vi + 1500]
    assert "ident:" in vseg, "克隆绑定弹窗写偏好也必须带身份快照"


def test_post_send_preview_tip_wired():
    """慢直发完成时的教学提示：>8s 且未复用试听 → 教「先试听再发送」工作流。"""
    html = _read("src/web/templates/unified_inbox.html")
    si = html.index("async function sendVoiceReply")
    seg = html[si:si + 7000]
    assert "inbox.voice.tip_preview_first" in seg
    assert "reused" in seg, "复用命中（本就秒回）不该再教学"


def test_ops_card_renders_guard_row():
    html = _read("src/web/templates/ops_overview.html")
    assert "guard_stats" in html
    assert "ov2_av_guard" in html


# ── 守卫计数器（P2：观测「改写了多少、拒了多少、为什么拒」）────────────────────
@pytest.mark.asyncio
async def test_guard_stats_counters_flow():
    from src.ai.voice_colloquial_llm import (
        health_signal, llm_colloquialize, reset_state)
    reset_state()

    class _OkAI:
        async def rewrite_local(self, system, user, *, timeout_sec=8.0, **kw):
            return "其实我今天过得还不错啦"

        async def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    src = "我今天其实过得挺不错的但是有点累"
    assert await llm_colloquialize(src, ai_client=_OkAI()) is not None
    gs = health_signal()["guard_stats"]
    assert gs["attempts"] == 1 and gs["accepted"] == 1
    # 缓存命中计数（同句第二次零 LLM）
    await llm_colloquialize(src, ai_client=_OkAI())
    assert health_signal()["guard_stats"]["cache_hits"] == 1
    # 问句主导跳过计数
    await llm_colloquialize("你晚上吃饭了吗，晚上有什么安排", ai_client=_OkAI())
    assert health_signal()["guard_stats"]["skipped_interrogative"] == 1
    reset_state()
