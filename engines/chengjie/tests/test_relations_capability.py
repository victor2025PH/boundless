"""RH-P1：流失预警页能力探测 + 健康榜缓存门禁。

三块不变量：
1. ``build_capability`` 纯函数——mode 三态（full/lite/none）、pending_restart
   （flag 开了没重启）、cold_start（榜可用但 contacts 零数据）、脏 contact_count 兜底。
   这是「菜单可见 ≠ 接口可用」事故（2026-08-01 实锤：contacts.enabled=false 时页面
   裸 404）的防回归钉：页面开局必须能问出「为什么不可用、还能用什么」。
2. ``/api/relations/health-board`` 的 60s TTL 缓存——命中回 ``cached:true``、
   ``force=1`` 绕过、mark-sent 显式失效；缓存是**注册级**实例（每次 register 各一份），
   测试之间不得互串。
3. 上榜行 ``contact_name``/``contact_id`` 富集——任务台的最小可读单元是姓名，
   不是 journey 哈希。
4. 模板静态钉：页面必须走 capability 探测 + 保留降级/重试分支（防止改回「裸调
   接口，404 死等」的旧形态）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from starlette.testclient import TestClient

from src.web.relations_capability import (
    BOARD_ROUTE,
    REACTIVATION_ROUTE,
    build_capability,
    route_paths_of,
)

_TEMPLATE = (Path(__file__).resolve().parent.parent
             / "src" / "web" / "templates" / "relations_health.html")


# ── 1) 纯函数：能力快照 ──────────────────────────────────────────────


def test_capability_mode_full():
    cap = build_capability(
        route_paths={BOARD_ROUTE, REACTIVATION_ROUTE},
        config={"contacts": {"enabled": True}},
        has_inbox_store=True,
        contact_count=12,
    )
    assert cap["mode"] == "full"
    assert cap["board_registered"] is True
    assert cap["reactivation_registered"] is True
    assert cap["lite_available"] is True
    assert cap["pending_restart"] is False
    assert cap["cold_start"] is False


def test_capability_mode_lite_when_board_missing():
    cap = build_capability(
        route_paths={"/api/workspace/churn-risks"},
        config={"contacts": {"enabled": False}},
        has_inbox_store=True,
    )
    assert cap["mode"] == "lite"
    assert cap["board_registered"] is False
    assert cap["contacts_enabled"] is False
    assert cap["pending_restart"] is False
    assert cap["flag_path"] == "contacts.enabled"


def test_capability_mode_none_when_nothing_available():
    cap = build_capability(
        route_paths=set(), config={}, has_inbox_store=False)
    assert cap["mode"] == "none"
    assert cap["lite_available"] is False


def test_capability_pending_restart_flag_on_but_not_loaded():
    """overlay 热加载把 flag 翻真、但子系统只在进程启动时装配 → 前端要能区分
    「没开」和「开了等重启」，两种引导文案完全不同。"""
    cap = build_capability(
        route_paths={"/api/workspace/churn-risks"},
        config={"contacts": {"enabled": True}},
        has_inbox_store=True,
    )
    assert cap["mode"] == "lite"
    assert cap["pending_restart"] is True


def test_capability_cold_start_zero_contacts():
    cap = build_capability(
        route_paths={BOARD_ROUTE},
        config={"contacts": {"enabled": True}},
        has_inbox_store=True,
        contact_count=0,
    )
    assert cap["cold_start"] is True
    # 有数据就不算冷启动
    cap2 = build_capability(
        route_paths={BOARD_ROUTE},
        config={"contacts": {"enabled": True}},
        has_inbox_store=True,
        contact_count=3,
    )
    assert cap2["cold_start"] is False
    # 数不出来（None）不猜——宁可不出冷启动条也不误报
    cap3 = build_capability(
        route_paths={BOARD_ROUTE},
        config={"contacts": {"enabled": True}},
        has_inbox_store=True,
        contact_count=None,
    )
    assert cap3["cold_start"] is False


def test_capability_contact_count_sanitized():
    cap = build_capability(
        route_paths={BOARD_ROUTE}, config={}, has_inbox_store=False,
        contact_count="not-a-number",  # type: ignore[arg-type]
    )
    assert cap["contact_count"] is None
    cap2 = build_capability(
        route_paths={BOARD_ROUTE}, config={}, has_inbox_store=False,
        contact_count=-5,
    )
    assert cap2["contact_count"] == 0


def test_route_paths_of_reads_fastapi_routes():
    app = FastAPI()

    @app.get("/api/relations/health-board")
    async def _board():  # noqa: D401
        return {}

    paths = route_paths_of(app)
    assert BOARD_ROUTE in paths


# ── 2/3) 健康榜缓存 + 姓名富集（真 ContactStore + 裸 FastAPI）──────────


class _FakeScheduler:
    """mark-sent 端点只用到 mark_sent / list_candidates 两个口。"""

    def __init__(self):
        self.sent = []

    def mark_sent(self, journey_id, note=""):
        self.sent.append(journey_id)

    def list_candidates(self):
        return []


@pytest.fixture
def client(tmp_path):
    from src.contacts.gateway import ContactGateway
    from src.contacts.handoff import HandoffTokenService
    from src.contacts.merge import MergeService
    from src.contacts.store import ContactStore
    from src.skills.intimacy_engine import IntimacyEngine
    from src.web.routes.contacts_routes import register_contacts_routes

    store = ContactStore(db_path=tmp_path / "contacts.db")
    handoff = HandoffTokenService(store, ttl_seconds=3600)
    merge = MergeService(store)
    gw = ContactGateway(store, handoff, merge)
    intim = IntimacyEngine(store)

    app = FastAPI()

    def noop_auth():
        return None

    register_contacts_routes(
        app, api_auth=noop_auth, contacts_store=store, merge_service=merge,
        intimacy_engine=intim, reactivation_scheduler=_FakeScheduler(),
    )
    tc = TestClient(app)
    tc.store = store      # type: ignore[attr-defined]
    tc.gateway = gw       # type: ignore[attr-defined]
    yield tc
    store.close()


def _seed(client, ext, name=""):
    from src.contacts.models import CHANNEL_MESSENGER

    ctx = client.gateway.on_peer_seen(
        channel=CHANNEL_MESSENGER, account_id="a", external_id=ext)
    if name:
        with client.store._lock:  # noqa: SLF001
            client.store._conn.execute(  # noqa: SLF001
                "UPDATE contacts SET primary_name=? WHERE contact_id=?",
                (name, ctx.journey.contact_id))
            client.store._conn.commit()  # noqa: SLF001
    return ctx.journey.journey_id


def test_board_cache_hit_then_force_bypass(client):
    _seed(client, "cache_a")
    r1 = client.get("/api/relations/health-board").json()
    assert r1["cached"] is False
    assert len(r1["items"]) == 1

    # 新增一段关系：TTL 窗内普通请求仍读缓存（陈旧证明缓存真的生效）
    _seed(client, "cache_b")
    r2 = client.get("/api/relations/health-board").json()
    assert r2["cached"] is True
    assert len(r2["items"]) == 1

    # force=1 绕过缓存 → 看到新数据，且回填缓存
    r3 = client.get("/api/relations/health-board?force=1").json()
    assert r3["cached"] is False
    assert len(r3["items"]) == 2

    # 不同查询参数=不同 key，不误命中
    r4 = client.get("/api/relations/health-board?limit=5").json()
    assert r4["cached"] is False


def test_mark_sent_invalidates_board_cache(client):
    jid = _seed(client, "cache_inv")
    client.get("/api/relations/health-board")
    warm = client.get("/api/relations/health-board").json()
    assert warm["cached"] is True

    r = client.post(f"/api/reactivation/{jid}/mark-sent")
    assert r.status_code == 200

    after = client.get("/api/relations/health-board").json()
    assert after["cached"] is False


def test_board_items_carry_contact_name(client):
    _seed(client, "named_1", name="王小美")
    d = client.get("/api/relations/health-board?force=1").json()
    assert d["items"], "seeded journey should be listed"
    it = d["items"][0]
    assert it["contact_name"] == "王小美"
    assert it["contact_id"]


# ── 4) 模板静态钉：三态防呆不许回退 ─────────────────────────────────


def test_template_probes_capability_and_degrades():
    text = _TEMPLATE.read_text(encoding="utf-8")
    # 开局必须探测能力端点，而不是裸调数据接口
    assert "/api/relations/capability" in text
    # 未启用引导 / 轻量榜回退 / 错误重试 三个分支的锚点都必须在
    for anchor in ("rh2_guide_body", "rh2_lite_only_hint",
                   "rh2_err_load", "rh2_retry",
                   "/api/workspace/churn-risks"):
        assert anchor in text, f"missing degradation anchor: {anchor}"
