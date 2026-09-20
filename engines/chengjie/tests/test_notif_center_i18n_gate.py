# -*- coding: utf-8 -*-
"""通知中心裸事件名门禁（工单#142，2026-09-02 钧机实锤）。

背景：通知中心「其它」标签曾连排 7 条裸英文事件名（orchestrator_worker_alert×5、
stage_advance_pending×2）——运营者完全不知道是什么、该不该慌。与 #117 裸键直显、
#115/#124 内部机器话泄漏同族病：面向运营的界面出现工程内部标识。

三道防线（本文件逐条钉死，防新事件类型再裸奔）：
① 进 notif_queue 白名单的每个事件类型必须有 i18n 人话标题+一句话说明
   （_NOTIF_TYPE_I18N，zh/en 齐备）+ 前端 _TYPE_META 渲染映射；
② 缺映射的类型服务端拦截不进用户可见通知（落 ops 日志），前端兜底不渲染；
③ orchestrator_worker_alert 按「类型+账号集合」合并计数（不再连排刷屏），
   流程心跳类收进「技术详情」折叠区。
禁区（同样钉死）：白名单语义（谁进 SSE/铃铛）不动；webhook 对外推送格式不动。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.web.routes.unified_inbox_realtime_routes import (  # noqa: E402
    _COALESCE_NOTIF_TYPES,
    _NOTIF_EVENT_TYPES,
    _NOTIF_TYPE_I18N,
    _SSE_EVENT_TYPES,
    _notif_coalesce_key,
    _queue_notif,
)

_TPL = REPO / "src/web/templates/workspace_base.html"


def _tpl_text() -> str:
    return _TPL.read_text(encoding="utf-8", errors="replace")


# ── ① i18n 准入：白名单 × 映射表 × 词条 × 前端渲染 四处联检 ─────────────────

def test_every_whitelisted_type_has_i18n_mapping():
    """新事件类型进 _NOTIF_EVENT_TYPES 白名单却没登记人话映射 → 这里立刻红。"""
    missing = sorted(set(_NOTIF_EVENT_TYPES) - set(_NOTIF_TYPE_I18N))
    assert not missing, (
        f"以下通知白名单事件类型缺 i18n 人话映射（会被服务端拦截、用户看不到）：{missing}。"
        "请在 _NOTIF_TYPE_I18N 登记（标题key, 说明key）并补 zh/en 词条 + 前端 _TYPE_META。"
    )


def test_mapping_i18n_keys_exist_zh_en():
    """映射到的标题/说明词条必须 zh/en 双语齐备且非空（缺 en 会回落裸 key）。"""
    from src.web.web_i18n import get_translations

    zh, en = get_translations("zh"), get_translations("en")
    bad = []
    for etype, (title_key, desc_key) in sorted(_NOTIF_TYPE_I18N.items()):
        for k in (title_key, desc_key):
            if not zh.get(k) or not en.get(k):
                bad.append(f"{etype} → {k}")
    assert not bad, f"以下事件类型的 i18n 词条缺 zh/en：{bad}"


def test_frontend_type_meta_covers_all_mapped_types():
    """前端 _TYPE_META 必须覆盖映射表全部类型——否则渲染端 fallback 会隐藏该通知。"""
    tpl = _tpl_text()
    missing = [
        t for t in sorted(_NOTIF_TYPE_I18N)
        if not re.search(rf"^\s*{re.escape(t)}\s*:\s*\{{", tpl, re.M)
    ]
    assert not missing, (
        f"workspace_base.html 的 _TYPE_META 缺以下类型的渲染映射：{missing}"
    )


def test_frontend_bare_type_fallback_removed():
    """旧兜底 label:n.type（裸事件名直显）必须消失；新兜底=缺映射不渲染。"""
    tpl = _tpl_text()
    assert "label:n.type||" not in tpl, "裸事件名兜底回潮：_renderItem 又把 n.type 当标题显示了"
    assert "_unmappedWarned" in tpl, "缺映射不渲染的前端兜底被移除"


def test_unmapped_type_blocked_and_ops_logged(monkeypatch):
    """缺映射类型：不进队列 + ops_events 落账（首见一次，去抖）。"""
    import src.ops.ops_events as ops_events
    from src.web.routes import unified_inbox_realtime_routes as rt

    recorded = []

    class _Store:
        def record(self, kind, **kw):
            recorded.append((kind, kw))

    monkeypatch.setattr(ops_events, "get_ops_event_store", lambda *a, **k: _Store())
    rt._UNMAPPED_NOTIF_LOGGED.clear()

    assert rt._notif_type_registered("brand_new_internal_evt") is False
    assert recorded and recorded[0][0] == "notif_type_unmapped"
    assert recorded[0][1].get("reason") == "brand_new_internal_evt"
    # 去抖：同类型再来不重复落账
    assert rt._notif_type_registered("brand_new_internal_evt") is False
    assert len(recorded) == 1
    # 已映射类型放行、不落账
    assert rt._notif_type_registered("orchestrator_worker_alert") is True
    assert len(recorded) == 1
    rt._UNMAPPED_NOTIF_LOGGED.clear()


# ── ② 复发型告警合并：按「类型+账号集合」只留最新一条 + ×N 计数 ────────────

def test_orch_worker_alert_in_coalesce_set():
    assert "orchestrator_worker_alert" in _COALESCE_NOTIF_TYPES


def test_orch_worker_coalesce_key_by_account_set():
    key = _notif_coalesce_key({
        "type": "orchestrator_worker_alert",
        "data": {"problems": [{"id": "line:U2"}, {"id": "line:U1"}]},
    })
    assert key == "line:U1|line:U2"  # 账号集合排序后拼接（顺序无关）
    # 恢复事件（problems 空）自成一键，不与告警混排
    assert _notif_coalesce_key({
        "type": "orchestrator_worker_alert",
        "data": {"problems": [], "recovered": True},
    }) == "recovered"
    # 其余类型沿用会话/草稿旧口径
    assert _notif_coalesce_key(
        {"type": "sla_alert", "data": {"conversation_id": "c1"}}) == "c1"


def test_orch_worker_alerts_merge_with_count_and_replay_safe():
    nq: list = []
    mk = lambda light: {  # noqa: E731
        "type": "orchestrator_worker_alert",
        "data": {"light": light, "problems": [{"id": "line:U1"}], "recovered": False},
    }
    e1, e2 = mk("yellow"), mk("red")
    _queue_notif(nq, e1)
    _queue_notif(nq, e2)
    assert len(nq) == 1, "同账号集合的连排告警应合并为一条"
    assert nq[0]["_notif_count"] == 2
    assert nq[0]["data"]["light"] == "red"  # 保最新
    # SSE 重连重放同一事件对象：不加计数、沿用时戳（已读不被顶回未读）
    ts = nq[0]["_notif_ts"]
    _queue_notif(nq, e2)
    assert len(nq) == 1 and nq[0]["_notif_count"] == 2 and nq[0]["_notif_ts"] == ts
    # 不同账号集合是不同一批问题，不互相吞
    e3 = {"type": "orchestrator_worker_alert",
          "data": {"light": "yellow", "problems": [{"id": "telegram:A9"}],
                   "recovered": False}}
    _queue_notif(nq, e3)
    assert len(nq) == 2


def test_conv_note_dedupe_preserved():
    """重构 _queue_notif 后 conv_note 的 note_id 幂等语义不得丢。"""
    nq: list = []
    note = {"type": "conv_note", "data": {"note_id": "n1", "body": "hi"}}
    _queue_notif(nq, note)
    _queue_notif(nq, {"type": "conv_note", "data": {"note_id": "n1", "body": "hi"}})
    assert len(nq) == 1


# ── ③ 前端：技术详情折叠区 + 告警级归「告警」标签 ──────────────────────────

def test_frontend_tech_fold_wired():
    tpl = _tpl_text()
    assert "_TECH_TYPES" in tpl and "base.notif.tech_hdr" in tpl, \
        "流程心跳类的「技术详情」折叠区被移除"
    for t in ("stage_advance_pending", "stage_sync", "workflow_step", "ops_report"):
        assert re.search(rf"\b{t}:1", tpl), f"{t} 应收进 _TECH_TYPES 折叠区"
    # 告警级归「告警」标签（不再挤「其它」）；前端合并集合含编排器告警
    assert "orchestrator_worker_alert:'alert'" in tpl
    assert "orchestrator_worker_alert:1" in tpl


# ── 禁区钉子：白名单语义不动 ────────────────────────────────────────────────

def test_whitelist_semantics_untouched():
    """谁进 SSE / 谁进铃铛的集合语义不因本修改缩水（webhook 推送在 webhook_notifier，
    与本链路无耦合，格式自然不动）。"""
    for t in ("orchestrator_worker_alert", "stage_advance_pending", "stage_advance",
              "ops_report", "health_alert", "billing_alert"):
        assert t in _SSE_EVENT_TYPES
        assert t in _NOTIF_EVENT_TYPES
