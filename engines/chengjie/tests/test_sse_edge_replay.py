# -*- coding: utf-8 -*-
"""SSE 连接期重放治理门禁（2026-08-05「冷启动请求风暴」事故沉淀）。

事故链：SSE 连接首轮把**全部在途** SLA/升级项当「新边沿」逐条发帧（生产积压时
45+45 帧），前端每帧各触发一次快照刷新 → 冷启动瞬间 ~90 个并发 GET 占满浏览器
同源 6 连接 → 页面上其余请求（含 ?conv= 深链救援）整段饿死。

治理（两端各一半，互为纵深）：
- 前端：refreshSla/refreshEsc in-flight 合并 + 尾随补刷（workspace_base.html）；
- 服务端（本门禁）：连接首轮 ``emit=False`` 静默 prime——只登记 seen 集
  （升级审计副作用照跑），不发帧；连接存续期间的**真边沿**照常推送。

钉住的不变量：
1. ``_edge_pick`` 纯函数语义：首轮全量=全 fresh、稳态零 fresh、恢复后再越线可再报；
2. ``_gen`` 首轮以 emit=False 调用两个推送器（防将来改回逐条重放）；
3. ``_esc_pushes`` 的审计副作用不受 emit 控制（重启窗口越线的升级仍要有人记账）。
"""
import inspect

import src.web.routes.unified_inbox_realtime_routes as rt
from src.web.routes.unified_inbox_realtime_routes import _edge_pick


def _items(*cids):
    return [{"conversation_id": c, "wait_sec": 1} for c in cids]


def test_edge_pick_first_round_all_fresh_and_seen_primed():
    seen: set = set()
    fresh = _edge_pick(_items("a", "b", "c"), seen)
    assert [it["conversation_id"] for it in fresh] == ["a", "b", "c"]
    assert seen == {"a", "b", "c"}


def test_edge_pick_steady_state_no_fresh():
    seen = {"a", "b"}
    assert _edge_pick(_items("a", "b"), seen) == []
    assert seen == {"a", "b"}


def test_edge_pick_new_item_is_fresh_only_once():
    seen = {"a"}
    fresh = _edge_pick(_items("a", "b"), seen)
    assert [it["conversation_id"] for it in fresh] == ["b"]
    assert _edge_pick(_items("a", "b"), seen) == []


def test_edge_pick_recovered_then_breach_again_reports_again():
    seen = {"a", "b"}
    # b 恢复（离开快照）→ seen 收缩
    assert _edge_pick(_items("a"), seen) == []
    assert seen == {"a"}
    # b 再次越线 → 再报（边沿语义：恢复后可再报）
    fresh = _edge_pick(_items("a", "b"), seen)
    assert [it["conversation_id"] for it in fresh] == ["b"]


def test_stream_first_round_is_silent_prime():
    """接线静态钉：连接首轮必须 emit=False（存量重放对新连接零信息量，曾打满连接池）。"""
    src = inspect.getsource(rt)
    assert "_sla_pushes(emit=False)" in src, "SLA 首轮丢失静默 prime"
    assert "_esc_pushes(emit=False)" in src, "升级首轮丢失静默 prime"
    # 心跳轮仍是边沿真推送（默认 emit=True 的裸调用存在）
    assert "_sla_pushes():" in src or "_sla_pushes()\n" in src or "for fr in _sla_pushes():" in src


def test_esc_audit_side_effect_not_gated_by_emit():
    """审计/自动指派须在 emit 判定之前执行（重启窗口越线的升级仍要记账+派人）。"""
    src = inspect.getsource(rt)
    esc = src[src.index("def _esc_pushes"):src.index("def _maybe_push_notif")]
    assert esc.index("record_escalation") < esc.index("if emit:"), \
        "审计副作用被 emit 闸住——静默 prime 轮会丢升级记账"
