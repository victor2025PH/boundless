# -*- coding: utf-8 -*-
"""conv 级 relationship-stage 响应的 journey（漏斗）附加段门禁（P1-5，2026-08-12）。

背景：统一 App cp-rel-stage 的「旅程阶段」子分区数据源。附加逻辑在
``unified_inbox_relationship_routes`` 的 handler 层（刻意不改
``_build_relationship_stage_payload``——它被 collab-context 等多处复用）。

三个不变量：
1. contacts 有 journey（funnel_stage 非空）→ 响应带 ``journey.funnel_stage(+label)``；
2. 无 contacts store / 无 journey / funnel 为空 → 响应**不带** journey 键
   （前端按缺省不渲染子分区——「无痕」而不是空对象）；
3. contacts 侧任何异常 → 关系阶段主体响应不受伤（ok=True 且核心字段照常）。
"""

from fastapi import FastAPI
from starlette.testclient import TestClient

import src.web.routes.unified_inbox_relationship_routes as rel_routes
import src.web.routes.unified_inbox_services as services


class _Journey:
    def __init__(self, funnel_stage):
        self.funnel_stage = funnel_stage


class _FakeContacts:
    def __init__(self, journey):
        self._j = journey

    def get_journey_by_contact(self, _contact_id):
        return self._j


class _BoomContacts:
    def get_journey_by_contact(self, _contact_id):
        raise RuntimeError("contacts boom")


_BASE_PAYLOAD = {
    "stage": "warming",
    "stage_label": "升温",
    "progress_pct": 40,
    "context": {"contact_id": "c-1", "exchange_count": 12, "intimacy_score": 33.0},
}


def _app(monkeypatch, contacts_store):
    monkeypatch.setattr(
        rel_routes, "_build_relationship_stage_payload",
        lambda request, cid, store, emit_pending_event=False: dict(
            _BASE_PAYLOAD, context=dict(_BASE_PAYLOAD["context"])),
    )
    monkeypatch.setattr(rel_routes, "_inbox_store", lambda request: None)
    # handler 内是局部 import（from ..unified_inbox_services import _contacts_store），
    # patch services 模块属性即可覆盖每次调用的解析
    monkeypatch.setattr(services, "_contacts_store", lambda request: contacts_store)
    app = FastAPI()
    rel_routes.register_relationship_stage_routes(app, api_auth=lambda request: None)
    return TestClient(app)


def _get(client):
    r = client.get("/api/workspace/conv/telegram:default:u1/relationship-stage")
    assert r.status_code == 200
    return r.json()


def test_journey_attached_when_contacts_has_funnel(monkeypatch):
    d = _get(_app(monkeypatch, _FakeContacts(_Journey("QUALIFIED"))))
    assert d["ok"] is True
    assert d["journey"]["funnel_stage"] == "QUALIFIED"
    assert d["journey"]["funnel_stage_label"]  # label 走 FUNNEL_STAGE_LABELS 真值表（未知码回落原码）


def test_no_journey_key_when_contacts_absent(monkeypatch):
    d = _get(_app(monkeypatch, None))
    assert d["ok"] is True
    assert "journey" not in d, "无 contacts 时必须无痕（前端按缺省不渲染子分区）"


def test_no_journey_key_when_funnel_empty(monkeypatch):
    d = _get(_app(monkeypatch, _FakeContacts(_Journey(""))))
    assert d["ok"] is True
    assert "journey" not in d


def test_contacts_exception_never_hurts_main_payload(monkeypatch):
    d = _get(_app(monkeypatch, _BoomContacts()))
    assert d["ok"] is True
    assert d["stage"] == "warming"
    assert d["progress_pct"] == 40
    assert "journey" not in d
