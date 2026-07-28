# -*- coding: utf-8 -*-
"""删除类操作的「账号绑定」护栏（2026-07-28 误删实锤的流程固化）。

事故：pid=mizuki 的传记库存被当「孤儿」清掉——删除者查了档案与审计都没查
绑定表，而账号 8438080491 的 registry meta 明明写着 ``persona_id: mizuki``。
固化为：``persona_binding_refs`` 反查（registry SSOT）+ 两个 DELETE 路由的
409 护栏（``?force=1`` 显式越过；registry 不可用 fail-open 不挡运维）。
"""
import sys
import time
from pathlib import Path

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.integrations.account_registry import (  # noqa: E402
    AccountRegistry,
    parse_persona_ids,
    persona_binding_refs,
)

_HDRS = {"Authorization": "Bearer test-token"}
_JSON_HDRS = {**_HDRS, "Content-Type": "application/json"}


# ── 1. 纯函数：meta 三种历史形态 ─────────────────────────────────────────────

def test_parse_persona_ids_tolerates_all_storage_shapes():
    assert parse_persona_ids({"persona_id": "mizuki"}) == ["mizuki"]
    assert parse_persona_ids({"persona_ids": ["a", "b"]}) == ["a", "b"]
    # 真实数据实锤：str(list) 字符串形态 "['su_wan']"
    assert parse_persona_ids({"persona_ids": "['su_wan']"}) == ["su_wan"]
    assert parse_persona_ids(
        {"persona_id": "x", "persona_ids": "['x', 'y']"}) == ["x", "y"]
    assert parse_persona_ids({}) == []
    assert parse_persona_ids(None) == []
    assert parse_persona_ids({"persona_ids": 42}) == []


def test_binding_refs_from_real_registry(tmp_path, monkeypatch):
    reg = AccountRegistry(tmp_path / "reg.db")
    reg.upsert("telegram", "111", meta={"persona_id": "mizuki"})
    reg.upsert("whatsapp", "222", meta={"persona_ids": "['mizuki']"})
    reg.upsert("telegram", "333", meta={"persona_id": "other"})
    reg.upsert("telegram", "444", meta={"persona_id": "mizuki"})
    reg.remove("telegram", "444")          # removed 不算在用
    import src.integrations.account_registry as ar
    monkeypatch.setattr(ar, "get_account_registry", lambda *a, **k: reg)

    refs = persona_binding_refs("mizuki")
    assert set(refs) == {"telegram:111", "whatsapp:222"}
    assert persona_binding_refs("nobody") == []
    assert persona_binding_refs("") == []


def test_binding_refs_fail_open_when_registry_broken(monkeypatch):
    import src.integrations.account_registry as ar

    def _boom(*a, **k):
        raise RuntimeError("registry exploded")

    monkeypatch.setattr(ar, "get_account_registry", _boom)
    assert persona_binding_refs("mizuki") == []


# ── 2. 路由级：两个 DELETE 的 409 / force / 无绑定放行 ──────────────────────

async def _build_app(tmp_path):
    cfg = {
        "domain": "general",
        "telegram": {"api_id": "1", "api_hash": "x", "phone_number": "+1"},
        "ai": {"api_key": "k"},
        "skills": {"enabled": []},
        "web_admin": {"auth_token": "test-token", "secret_key": "test-secret"},
        "personas": {
            "profiles": [{"id": "bound_p", "name": "在用人设", "enabled": True}],
            "doc_import": {"enabled": True},
            "bio_retrieval": {"enabled": True, "semantic": False},
        },
    }
    (tmp_path / "config.yaml").write_text(
        yaml.dump(cfg, allow_unicode=True), encoding="utf-8")
    (tmp_path / "templates.yaml").write_text(
        yaml.dump({"greeting": ["hi"]}), encoding="utf-8")
    (tmp_path / "exchange_rates.yaml").write_text(
        yaml.dump({"channels": {}}), encoding="utf-8")

    from src.utils.config_manager import ConfigManager
    from src.utils.persona_manager import PersonaManager

    cm = ConfigManager(str(tmp_path / "config.yaml"))
    await cm.load()
    PersonaManager.reset()
    from src.web.admin import create_app
    return create_app(cm)


@pytest.fixture(autouse=True)
def _reset_pm():
    yield
    from src.utils.persona_manager import PersonaManager
    PersonaManager.reset()


@pytest.fixture
def bound_registry(tmp_path, monkeypatch):
    reg = AccountRegistry(tmp_path / "guard_reg.db")
    reg.upsert("telegram", "8438080491", meta={"persona_id": "bound_p"})
    import src.integrations.account_registry as ar
    monkeypatch.setattr(ar, "get_account_registry", lambda *a, **k: reg)
    return reg


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_bio_doc_delete_blocked_then_forced(tmp_path, monkeypatch,
                                                  bound_registry):
    from src.companion import persona_bio_store as pbs

    pbs.configure_persona_bio_store(str(tmp_path / "bio.db"), embed_fn=None)
    try:
        app = await _build_app(tmp_path)
        async with _client(app) as c:
            await c.post("/api/personas/bound_p/bio-doc", headers=_JSON_HDRS,
                         json={"text": "她在都灵长大，后来搬去了巴塞罗那。" * 20})
            r = await c.delete("/api/personas/bound_p/bio-doc", headers=_HDRS)
            assert r.status_code == 409
            assert "8438080491" in r.json()["detail"]
            assert (await c.get("/api/personas/bound_p/bio-doc",
                                headers=_HDRS)).json()["meta"]  # 没被删

            r = await c.delete("/api/personas/bound_p/bio-doc?force=1",
                               headers=_HDRS)
            assert r.status_code == 200 and r.json()["ok"] is True
    finally:
        pbs.reset_persona_bio_store()


@pytest.mark.asyncio
async def test_bio_doc_delete_unbound_passes(tmp_path, monkeypatch,
                                             bound_registry):
    from src.companion import persona_bio_store as pbs

    pbs.configure_persona_bio_store(str(tmp_path / "bio2.db"), embed_fn=None)
    try:
        app = await _build_app(tmp_path)
        async with _client(app) as c:
            await c.post("/api/personas/free_p/bio-doc", headers=_JSON_HDRS,
                         json={"text": "自由人设的传记。" * 30})
            r = await c.delete("/api/personas/free_p/bio-doc", headers=_HDRS)
            assert r.status_code == 200 and r.json()["ok"] is True
    finally:
        pbs.reset_persona_bio_store()


@pytest.mark.asyncio
async def test_profile_delete_blocked_then_forced(tmp_path, bound_registry):
    app = await _build_app(tmp_path)
    async with _client(app) as c:
        r = await c.delete("/api/personas/profiles/bound_p", headers=_HDRS)
        assert r.status_code == 409
        assert "8438080491" in r.json()["detail"]
        # 档案还在
        r = await c.get("/api/personas/profiles/bound_p", headers=_HDRS)
        assert r.status_code == 200

        r = await c.delete("/api/personas/profiles/bound_p?force=1",
                           headers=_HDRS)
        assert r.status_code == 200 and r.json()["ok"] is True


@pytest.mark.asyncio
async def test_bio_inventory_aligns_stock_profile_and_bindings(tmp_path,
                                                               bound_registry):
    """盘点三维对齐：有档案的 bound_p 带绑定账号；ghost_p 有库存无档案 → 孤儿。

    2026-07-28 两次踩坑各缺一维（删在用库存没看绑定 / 平行建档没看库存），
    这张视图就是把三张表对齐到一行。
    """
    from src.companion import persona_bio_store as pbs

    pbs.configure_persona_bio_store(str(tmp_path / "inv.db"), embed_fn=None)
    try:
        app = await _build_app(tmp_path)
        async with _client(app) as c:
            for pid in ("bound_p", "ghost_p"):
                await c.post(f"/api/personas/{pid}/bio-doc", headers=_JSON_HDRS,
                             json={"text": "传记正文。" * 60})
            r = await c.get("/api/personas/bio-inventory", headers=_HDRS)
            assert r.status_code == 200
            d = r.json()
            rows = {x["persona_id"]: x for x in d["rows"]}
            assert d["orphans"] == ["ghost_p"]
            assert rows["bound_p"]["has_profile"] is True
            assert rows["bound_p"]["bound_accounts"] == ["telegram:8438080491"]
            assert rows["ghost_p"]["has_profile"] is False
            assert rows["ghost_p"]["bound_accounts"] == []
            assert rows["bound_p"]["chunks"] > 0
    finally:
        pbs.reset_persona_bio_store()


@pytest.mark.asyncio
async def test_search_selftest_exposes_alias_visibility(tmp_path,
                                                        bound_registry):
    """检索自测响应带 aliases 段（键/值/命中）——运营调参不再翻代码。"""
    from src.companion import persona_bio_store as pbs

    pbs.configure_persona_bio_store(str(tmp_path / "als.db"), embed_fn=None)
    try:
        app = await _build_app(tmp_path)
        async with _client(app) as c:
            await c.post("/api/personas/bound_p/bio-doc", headers=_JSON_HDRS,
                         json={"text":
                               "2017年1月和一个西班牙人相恋，西班牙人（José Luis"
                               " Jerónimo）25岁，之后我们结婚了。" + "情。" * 60
                               + "\nI started a relationship with José Luis"
                               " Jerónimo and we got married. " + "more. " * 30})
            r = await c.post("/api/personas/bound_p/bio-doc/search",
                             headers=_JSON_HDRS,
                             json={"query": "你老公对你好吗", "top_k": 3})
            assert r.status_code == 200
            d = r.json()
            by_key = {x["key"]: x for x in d.get("aliases") or []}
            assert "老公" in by_key
            assert "josé luis jerónimo" in by_key["老公"]["values"]
            assert "josé luis jerónimo" in by_key["老公"]["hits"]
    finally:
        pbs.reset_persona_bio_store()


@pytest.mark.asyncio
async def test_profile_delete_fail_open_when_registry_broken(tmp_path,
                                                             monkeypatch):
    import src.integrations.account_registry as ar

    def _boom(*a, **k):
        raise RuntimeError("registry down")

    monkeypatch.setattr(ar, "get_account_registry", _boom)
    app = await _build_app(tmp_path)
    async with _client(app) as c:
        r = await c.delete("/api/personas/profiles/bound_p", headers=_HDRS)
        assert r.status_code == 200 and r.json()["ok"] is True
