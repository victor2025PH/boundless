"""坐席接待状态语义单源（presence_policy）门禁——2026-08-19 状态系统收口批。

钉住四件事：
1. 行为矩阵纯函数（派单资格 / 席位占用 / 名单可见）；
2. assignment._eligible 消费 policy（online_only 两档语义不漂移）；
3. AgentCoordinator：内存分支名单口径与 store 统一（手选「离开」如实显示）
   + 状态变化写审计、同态保活不写；
4. presence 路由静态接线（席位口径=counts_license_seat：POST 升态检查、
   占席统计、heartbeat 升态补缝三处缺一即红）。
"""
from __future__ import annotations

from pathlib import Path

from src.workspace.presence_policy import (
    accepts_new_assignments,
    counts_license_seat,
    normalize_status,
    roster_visible,
)

REPO = Path(__file__).resolve().parents[1]


# ── 1. 行为矩阵 ────────────────────────────────────────────────

def test_normalize_status_conservative():
    assert normalize_status("online") == "online"
    assert normalize_status(" BUSY ") == "busy"
    assert normalize_status("offline") == "offline"
    # 空/非法一律按「离开」（不派单不计席）——旧 _eligible 在放宽档会放行空状态
    assert normalize_status("") == "offline"
    assert normalize_status(None) == "offline"
    assert normalize_status("away") == "offline"


def test_assignment_matrix():
    assert accepts_new_assignments("online") is True
    assert accepts_new_assignments("online", allow_busy=True) is True
    # 忙碌：默认不派新单；放宽档（auto_assign.online_only=False）才派
    assert accepts_new_assignments("busy") is False
    assert accepts_new_assignments("busy", allow_busy=True) is True
    # 离开：任何档位都不参与
    assert accepts_new_assignments("offline") is False
    assert accepts_new_assignments("offline", allow_busy=True) is False
    assert accepts_new_assignments("", allow_busy=True) is False


def test_seat_matrix():
    # 在线/忙碌都是「人在用软件」⇒ 都占席位（堵住旧「busy 不计席」授权绕过缝）
    assert counts_license_seat("online") is True
    assert counts_license_seat("busy") is True
    assert counts_license_seat("offline") is False
    assert counts_license_seat("") is False


def test_roster_matrix():
    # 心跳窗口内三态一律如实显示（含手选「离开」；真正消失=心跳超时断线）
    assert roster_visible("online") and roster_visible("busy") and roster_visible("offline")


# ── 2. assignment 接线 ────────────────────────────────────────

def _presence_rows():
    return [
        {"agent_id": "a1", "status": "online"},
        {"agent_id": "a2", "status": "busy"},
        {"agent_id": "a3", "status": "offline"},
        {"agent_id": "a4", "status": ""},          # 空状态：保守按离开
        {"agent_id": "", "status": "online"},      # 无 id：剔除
    ]


def test_eligible_online_only_true():
    from src.workspace.assignment import AssignmentService
    svc = AssignmentService({"enabled": True, "online_only": True})
    got = {p["agent_id"] for p in svc.eligible_agents(_presence_rows())}
    assert got == {"a1"}


def test_eligible_online_only_false_admits_busy_never_offline():
    from src.workspace.assignment import AssignmentService
    svc = AssignmentService({"enabled": True, "online_only": False})
    got = {p["agent_id"] for p in svc.eligible_agents(_presence_rows())}
    assert got == {"a1", "a2"}


# ── 3. AgentCoordinator：名单口径 + 审计 ──────────────────────

def _mem_coord():
    from src.workspace.agent_coordinator import AgentCoordinator
    return AgentCoordinator(store=None)


def test_mem_roster_keeps_manual_offline():
    coord = _mem_coord()
    coord.set_presence("a1", display_name="A1", status="online")
    coord.set_presence("a2", display_name="A2", status="offline")
    ids = {r["agent_id"] for r in coord.list_presence()}
    # 口径统一回归钉：内存分支此前剔 offline、SQLite 分支不剔（两环境各看一套）
    assert ids == {"a1", "a2"}


def test_mem_roster_drops_stale_heartbeat():
    coord = _mem_coord()
    coord.set_presence("a1", status="online")
    coord._presence["a1"]["last_seen_at"] = 0  # 心跳超时=断线，才从名单消失
    assert coord.list_presence() == []


class _FakeEvStore:
    def __init__(self):
        self.rows = []

    def record(self, kind, **kw):
        self.rows.append({"kind": kind, **kw})


def test_presence_change_audited_transitions_only(monkeypatch):
    import src.ops.ops_events as ops_events
    fake = _FakeEvStore()
    monkeypatch.setattr(ops_events, "get_ops_event_store", lambda *a, **k: fake)
    coord = _mem_coord()
    coord.set_presence("a1", display_name="A1", status="online")   # new->online 记
    coord.set_presence("a1", display_name="A1", status="online")   # 同态（心跳保活）不记
    coord.heartbeat("a1", display_name="A1", status="")            # 空状态保活不记
    coord.set_presence("a1", display_name="A1", status="busy")     # online->busy 记
    kinds = [(r["kind"], r["reason"]) for r in fake.rows]
    assert kinds == [
        ("presence_change", "new->online"),
        ("presence_change", "online->busy"),
    ]
    assert all(r["platform"] == "workspace" and r["account_id"] == "a1" for r in fake.rows)


def test_presence_audit_never_blocks(monkeypatch):
    import src.ops.ops_events as ops_events

    def _boom(*a, **k):
        raise RuntimeError("audit down")

    monkeypatch.setattr(ops_events, "get_ops_event_store", _boom)
    coord = _mem_coord()
    row = coord.set_presence("a1", status="online")  # 审计炸不影响写入
    assert row["status"] == "online"


def test_get_presence_mem_branch():
    coord = _mem_coord()
    assert coord.get_presence("nobody") is None
    coord.set_presence("a1", status="busy")
    assert (coord.get_presence("a1") or {}).get("status") == "busy"


# ── 4. 路由静态接线钉（席位口径三处缺一即红）──────────────────

def test_presence_routes_wired_to_seat_policy():
    src = (REPO / "src/web/routes/unified_inbox_workspace_presence_routes.py").read_text(
        encoding="utf-8")
    # 占席统计按 policy 过滤（不再硬编码 status=="online"）
    assert "counts_license_seat(r.get(\"status\"))" in src
    # POST set：进入占席状态才过席检（busy 同样计席）
    assert "if counts_license_seat(status) and _seat_block(" in src
    # heartbeat 升态补缝：不占席→占席的心跳升态同样过席检，拦下降为 '' 保留现状
    assert "not counts_license_seat(prev_st)" in src
    assert 'hb_status = ""' in src


def test_assignment_eligible_wired_to_policy():
    src = (REPO / "src/workspace/assignment.py").read_text(encoding="utf-8")
    assert "from src.workspace.presence_policy import accepts_new_assignments" in src
    assert "accepts_new_assignments(p.get(\"status\"), allow_busy=allow_busy)" in src
