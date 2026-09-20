# -*- coding: utf-8 -*-
"""#333 视觉记忆 API 薄测试：查看 / 确认本人 / 确认关系人 / 否认 / 退休 / 删除 / 503 / 词条齐平。"""
from __future__ import annotations

import math
import random
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.companion import visual_memory as vm
from src.web.routes.visual_memory_routes import register_visual_memory_routes


def _vec(seed: int, dim: int = 8):
    rnd = random.Random(seed)
    v = [rnd.uniform(-1, 1) for _ in range(dim)]
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


class _Audit:
    def __init__(self):
        self.rows = []

    def log(self, actor, action, target, _x, detail):
        self.rows.append((actor, action, target, detail))


def _client(store, audit=None, config=None):
    app = FastAPI()

    def api_auth(request: Request):
        return True

    register_visual_memory_routes(
        app, auth_dep=api_auth, audit_store=audit,
        config_manager=SimpleNamespace(config=config or {}),
        store_getter=lambda: store)
    return TestClient(app)


CID = "whatsapp:19892968016:15635715247"


def test_get_confirm_deny_retire_delete_roundtrip():
    st = vm.VisualMemoryStore(":memory:")
    audit = _Audit()
    c = _client(st, audit)
    st.record_observation(CID, message_id="m1", label="unknown", embedding=_vec(1), summary="自拍")
    r = c.get(f"/api/visual-memory/{CID}")
    assert r.status_code == 200
    body = r.json()
    assert body["self_prototype"] == {"present": False, "source": ""}
    assert len(body["observations"]) == 1 and body["observations"][0]["has_embedding"] is True
    assert "embedding" not in body["observations"][0]           # 向量不出 API

    # 人工确认本人
    r = c.post(f"/api/visual-memory/{CID}/confirm", json={"kind": "self"})
    assert r.status_code == 200 and r.json()["kind"] == "self"
    body = c.get(f"/api/visual-memory/{CID}").json()
    assert body["self_prototype"]["source"] == "user_confirmed"
    assert body["entities"][0]["entity_type"] == "self"

    # 关系人：先来一张新脸
    st.record_observation(CID, message_id="m2", label="unknown", embedding=_vec(2))
    r = c.post(f"/api/visual-memory/{CID}/confirm", json={"kind": "relation", "relation": "sister"})
    assert r.status_code == 200
    ents = c.get(f"/api/visual-memory/{CID}").json()["entities"]
    rel = next(e for e in ents if e["entity_type"] == "relation")
    assert rel["relation"] == "sister"

    # 参数校验
    assert c.post(f"/api/visual-memory/{CID}/confirm", json={"kind": "relation"}).status_code == 400
    assert c.post(f"/api/visual-memory/{CID}/confirm", json={"kind": "boss"}).status_code == 400

    # 否认最近一张 → unknown
    assert c.post(f"/api/visual-memory/{CID}/deny").status_code == 200
    assert c.get(f"/api/visual-memory/{CID}").json()["observations"][0]["label"] == "unknown"

    # 退休关系人实体；不存在 → 404 语义由 store 返回 True（UPDATE 0 行也 True）→ 接口仍 200；空会话删 → 0
    assert c.post(f"/api/visual-memory/{CID}/entities/{rel['id']}/retire").status_code == 200
    assert all(e["entity_type"] != "relation" for e in c.get(f"/api/visual-memory/{CID}").json()["entities"])
    r = c.delete(f"/api/visual-memory/{CID}")
    assert r.status_code == 200 and r.json()["deleted_observations"] == 2
    assert c.get(f"/api/visual-memory/{CID}").json()["observations"] == []

    actions = [a for _, a, _, _ in audit.rows]
    assert actions == ["vmem_confirm", "vmem_confirm", "vmem_deny", "vmem_retire", "vmem_delete"]


def test_confirm_without_face_observation_is_409_and_store_missing_is_503():
    st = vm.VisualMemoryStore(":memory:")
    c = _client(st)
    assert c.post(f"/api/visual-memory/{CID}/confirm", json={"kind": "self"}).status_code == 409
    assert c.post(f"/api/visual-memory/{CID}/deny").status_code == 409
    c2 = _client(None)
    assert c2.get(f"/api/visual-memory/{CID}").status_code == 503


def test_status_reports_disabled_without_probing():
    c = _client(vm.VisualMemoryStore(":memory:"), config={"vision": {"face_identity": {"enabled": False}}})
    r = c.get("/api/visual-memory/status")
    assert r.status_code == 200
    assert r.json()["enabled"] is False and r.json()["service_healthy"] is None


def test_status_exposes_process_stats_and_store_totals():
    """2026-09-18 首验沉淀：status 带进程级退出原因计数 + 边车耗时分位 + 库侧总量（跨重启）。"""
    from src.companion import face_identity as fi
    fi.reset_stats()
    st = vm.VisualMemoryStore(":memory:")
    st.record_observation(CID, label="unknown", embedding=_vec(1))
    st.record_observation(CID, label="no_face")
    st.confirm_self(CID)
    # 走一次 annotate 的 enabled_off 出口，让 calls 计数非零
    fi.annotate_inbound_sync(config={"vision": {"face_identity": {"enabled": False}}}, conversation_id=CID,
                             media_type="image", media_ref="x", memory=st)
    c = _client(st, config={"vision": {"face_identity": {"enabled": False}}})
    body = c.get("/api/visual-memory/status").json()
    assert body["stats"]["counts"]["calls"] == 1 and body["stats"]["counts"]["enabled_off"] == 1
    assert set(body["stats"]["embed_ms"]) == {"n", "p50", "p95", "max"}
    assert body["totals"] == {"observations": 2, "with_face": 1, "confirmed_observations": 1,
                              "confirmed_entities": 1}
    fi.reset_stats()


def test_i18n_pack_bilingual_and_keys_used():
    from src.web.i18n_packs import visual_memory as pack
    assert set(pack.ZH) == set(pack.EN) == set(pack.ZH_HANT)
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "src" / "web" / "routes" / "visual_memory_routes.py").read_text(encoding="utf-8")
    for k in pack.ZH:
        assert f'"{k}"' in src, k
