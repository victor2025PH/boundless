"""个人号 RPA 线索账号归属（P7）门禁：桥接 huoke → chengjie 账号治理。

守：
1. 纯函数 resolve_lead_account——带 source.account_id 归真实账号；不带回落 product
   （向后兼容，现有 leadbus 门禁 account_id==product 继续成立）。
2. **安全不变量**：PERSONAL_RPA_MODE 不在 ORCHESTRATED_MODES——本机编排器绝不拉起
   外部个人号（否则 no-serial 反复重启 + 告警刷屏）。
3. register_lead_account 幂等登记 + best-effort（registry 缺失/写失败不抛）。
4. leadbus ingest 端到端：带 account_id → 收件箱按真实账号落库 + 注册表出现该
   personal_rpa 账号；不带 → 旧行为（product 顶替、注册表不新增）。
"""
from __future__ import annotations

# Request 须在模块级 import：本文件用 from __future__ annotations（注解=字符串），
# leadbus 的 Depends(api_auth) 靠 get_type_hints 从模块 globalns 解析 auth 形参的
# ``Request`` 注解；放函数内 import 会解析不到 → FastAPI 当 query 参数报 422。
from fastapi import Request

from src.integrations.leadbus_account import (
    PERSONAL_RPA_MODE, register_lead_account, resolve_lead_account,
)


# ── 1. 纯函数归属 ────────────────────────────────────────────────


def test_resolve_prefers_explicit_account_id():
    aid, real = resolve_lead_account({"account_id": "wa_dev_7", "product": "huoke"})
    assert aid == "wa_dev_7" and real is True


def test_resolve_falls_back_to_product():
    aid, real = resolve_lead_account({"product": "zhituo"})
    assert aid == "zhituo" and real is False


def test_resolve_blank_account_id_falls_back():
    aid, real = resolve_lead_account({"account_id": "  ", "product": "huoke"})
    assert aid == "huoke" and real is False


def test_resolve_handles_none():
    assert resolve_lead_account(None) == ("", False)


# ── 2. 安全不变量：编排器不接管个人号 ───────────────────────────


def test_personal_rpa_mode_not_orchestrated():
    from src.integrations.account_orchestrator import ORCHESTRATED_MODES
    assert PERSONAL_RPA_MODE not in ORCHESTRATED_MODES, (
        "个人号由 huoke 外部进程驱动，本机编排器一旦接管会去拉起无本地设备的账号 "
        "→ 反复重启 + 告警刷屏（假账号事故同类）")


# ── 3. register_lead_account 幂等 + best-effort ─────────────────


class _FakeRegistry:
    def __init__(self):
        self.rows = {}

    def upsert(self, platform, account_id, *, mode=None, label=None,
               business_line=None, **kw):
        key = (platform, account_id)
        self.rows[key] = {"mode": mode, "label": label}


def test_register_upserts_personal_rpa():
    reg = _FakeRegistry()
    assert register_lead_account(reg, "whatsapp", "wa_dev_7") is True
    row = reg.rows[("whatsapp", "wa_dev_7")]
    assert row["mode"] == PERSONAL_RPA_MODE
    assert row["label"] == "wa_dev_7"       # account_id 兜底名


def test_register_none_registry_is_false_not_raise():
    assert register_lead_account(None, "whatsapp", "wa_dev_7") is False


def test_register_blank_ids_rejected():
    reg = _FakeRegistry()
    assert register_lead_account(reg, "", "wa_dev_7") is False
    assert register_lead_account(reg, "whatsapp", "  ") is False
    assert reg.rows == {}


def test_register_swallows_upsert_error():
    class _Boom:
        def upsert(self, *a, **k):
            raise RuntimeError("db locked")
    assert register_lead_account(_Boom(), "whatsapp", "wa_dev_7") is False


# ── 4. leadbus ingest 端到端 ────────────────────────────────────


def _auth(request: Request):
    return True


def _client(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.inbox.store import InboxStore
    from src.web.routes.leadbus_routes import register_leadbus_routes

    app = FastAPI()
    app.state.inbox_store = InboxStore(tmp_path / "inbox.db")
    register_leadbus_routes(app, api_auth=_auth, config_manager=None)
    return TestClient(app)


def _envelope(**source_extra):
    src = {"product": "huoke", "platform": "whatsapp"}
    src.update(source_extra)
    return {"lead": {
        "lead_id": "L1", "source": src,
        "lead": {"external_id": "wa:peer99", "handle": "阿强"},
    }}


def test_ingest_with_account_id_attributes_and_registers(tmp_path, monkeypatch):
    reg = _FakeRegistry()
    monkeypatch.setattr(
        "src.integrations.account_registry.get_account_registry", lambda: reg)
    client = _client(tmp_path)
    r = client.post("/api/leadbus/ingest",
                    json=_envelope(account_id="wa_dev_7")).json()
    assert r["ready"] is True
    # 收件箱按真实设备账号落库（不再是 product 顶替）
    store = client.app.state.inbox_store
    convs = store.list_conversations(limit=10)
    accounts = {c.get("account_id") for c in convs}
    assert "wa_dev_7" in accounts, "带 account_id 的线索须归属真实设备账号"
    # 注册表出现该 personal_rpa 账号（接上治理链）
    assert reg.rows.get(("whatsapp", "wa_dev_7"), {}).get("mode") == PERSONAL_RPA_MODE


def test_ingest_without_account_id_keeps_legacy_product(tmp_path, monkeypatch):
    reg = _FakeRegistry()
    monkeypatch.setattr(
        "src.integrations.account_registry.get_account_registry", lambda: reg)
    client = _client(tmp_path)
    r = client.post("/api/leadbus/ingest", json=_envelope()).json()
    assert r["ready"] is True
    store = client.app.state.inbox_store
    convs = store.list_conversations(limit=10)
    accounts = {c.get("account_id") for c in convs}
    assert "huoke" in accounts, "不带 account_id 时回落 product（向后兼容）"
    assert reg.rows == {}, "无真实账号时不得往注册表塞 product 顶替值"
