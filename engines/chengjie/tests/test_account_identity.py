# -*- coding: utf-8 -*-
"""登录身份决议层契约（实施72，2026-08-27 账号身份错乱事故根治）。

事故原型：登录位 msg_nxl3iqxn 原属 Calixa（61584070255403），人工重登时被改登成
Wisley（61591632195604）——系统静默接受换人、自动补挂人设、会话进全自动。
本文件钉住四道防线：

1. 登录映射登记 + 换人检测（record_login_identity / resolve_login_identity）；
2. 隔离态（quarantine：幂等、告警一次、已转正不回打）；
3. 人设自动补挂对 pending 账号跳过（ensure_account_default_persona）；
4. 自动化封顶 compute_mode_caps ④（identity_pending → review，fail-open）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from src.integrations import account_identity as ai_mod
from src.integrations.account_identity import (
    confirm_account_identity,
    fleet_candidates,
    identity_pending,
    invalidate_fleet_candidates_cache,
    invalidate_pending_cache,
    is_identity_pending_row,
    login_meta_key,
    pending_accounts,
    quarantine_account,
    record_login_identity,
    resolve_login_identity,
)


class FakeRegistry:
    """最小注册表假件（get/upsert/list 与真实接口同形）。"""

    def __init__(self) -> None:
        self.rows: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _k(platform: str, account_id: str) -> str:
        return f"{str(platform).lower()}:{account_id}"

    def get(self, platform: str, account_id: str) -> Optional[Dict[str, Any]]:
        row = self.rows.get(self._k(platform, account_id))
        return dict(row) if row else None

    def upsert(self, platform: str, account_id: str, *, meta: Optional[Dict] = None,
               merge_meta: bool = False, **kw: Any) -> Dict[str, Any]:
        k = self._k(platform, account_id)
        row = self.rows.setdefault(k, {
            "platform": str(platform).lower(), "account_id": str(account_id),
            "status": "online", "meta": {},
        })
        if kw.get("status"):
            row["status"] = kw["status"]
        if meta:
            if merge_meta:
                row["meta"].update(meta)
            else:
                row["meta"] = dict(meta)
        return dict(row)

    def list(self, platform: Optional[str] = None, *,
             include_removed: bool = False) -> List[Dict[str, Any]]:
        out = []
        for row in self.rows.values():
            if platform and row["platform"] != str(platform).lower():
                continue
            if not include_removed and row.get("status") == "removed":
                continue
            out.append(dict(row))
        return out


@pytest.fixture(autouse=True)
def _clean_cache():
    invalidate_pending_cache()
    invalidate_fleet_candidates_cache()
    yield
    invalidate_pending_cache()
    invalidate_fleet_candidates_cache()


@pytest.fixture()
def _silent_alerts(monkeypatch):
    """屏蔽 ops 告警/审计出口并计数（best-effort 出口不该阻断，也不该刷真库）。"""
    calls: Dict[str, int] = {"alert": 0, "audit": 0}

    monkeypatch.setattr(
        "src.ops.ops_alert.notify",
        lambda *a, **k: calls.__setitem__("alert", calls["alert"] + 1),
        raising=False)

    class _St:
        def record(self, *a, **k):
            calls["audit"] += 1

    monkeypatch.setattr(
        "src.ops.ops_events.get_ops_event_store", lambda: _St(), raising=False)
    return calls


# ── 1. 登录映射 + 换人检测 ────────────────────────────────────────────────


def test_login_meta_key_platform_map():
    assert login_meta_key("messenger") == "messenger_login_id"
    assert login_meta_key("whatsapp") == "baileys_login_id"
    assert login_meta_key("zalo") == "zca_login_id"
    assert login_meta_key("newplat") == "newplat_login_id"
    assert login_meta_key("") == ""


def test_record_login_identity_writes_mapping_and_detects_prev():
    reg = FakeRegistry()
    reg.upsert("messenger", "OLD", meta={"messenger_login_id": "msg_x"},
               merge_meta=True)
    out = record_login_identity("messenger", "NEW", "msg_x", registry=reg)
    assert out == {"changed": True, "prev_accounts": ["OLD"]}
    assert reg.get("messenger", "NEW")["meta"]["messenger_login_id"] == "msg_x"


def test_record_login_identity_same_account_no_change():
    reg = FakeRegistry()
    reg.upsert("messenger", "A", meta={"messenger_login_id": "msg_x"},
               merge_meta=True)
    out = record_login_identity("messenger", "A", "msg_x", registry=reg)
    assert out == {"changed": False, "prev_accounts": []}


def test_record_login_identity_sees_removed_prev_holder():
    """前任已被归档（removed）也要算换人证据——事故里旧号正是 offline/removed。"""
    reg = FakeRegistry()
    reg.upsert("messenger", "OLD", status="removed",
               meta={"messenger_login_id": "msg_x"}, merge_meta=True)
    out = record_login_identity("messenger", "NEW", "msg_x", registry=reg)
    assert out["prev_accounts"] == ["OLD"]


def test_record_login_identity_fail_open():
    assert record_login_identity("", "A", "x") == {
        "changed": False, "prev_accounts": []}
    assert record_login_identity("messenger", "A", "", registry=FakeRegistry()) == {
        "changed": False, "prev_accounts": []}


# ── 2. 隔离态 ────────────────────────────────────────────────────────────


def test_quarantine_marks_pending_and_alerts_once(_silent_alerts):
    reg = FakeRegistry()
    reg.upsert("messenger", "NEW")
    assert quarantine_account("messenger", "NEW", ["OLD"], registry=reg) is True
    meta = reg.get("messenger", "NEW")["meta"]
    assert meta["identity_pending"] is True
    assert meta["identity_prev_accounts"] == ["OLD"]
    assert float(meta["identity_changed_at"]) > 0
    assert _silent_alerts["alert"] == 1 and _silent_alerts["audit"] == 1
    # 幂等：已 pending 不重复告警
    assert quarantine_account("messenger", "NEW", ["OLD"], registry=reg) is False
    assert _silent_alerts["alert"] == 1


def test_quarantine_respects_prior_confirmation(_silent_alerts):
    """人已对同一批前任显式转正 → 后续同证据不再打回隔离（尊重人的决定）。"""
    reg = FakeRegistry()
    reg.upsert("messenger", "NEW", meta={
        "identity_pending": False,
        "identity_confirmed_prev": ["OLD"],
    }, merge_meta=True)
    assert quarantine_account("messenger", "NEW", ["OLD"], registry=reg) is False
    # 出现**新的**前任证据（另一个号）→ 仍要重新隔离
    assert quarantine_account("messenger", "NEW", ["OTHER"], registry=reg) is True


def test_resolve_login_identity_merges_session_health_evidence(_silent_alerts):
    """注册表映射缺失（存量常态）时，session_health 的 superseded 清单兜底。"""
    reg = FakeRegistry()
    reg.upsert("messenger", "NEW")
    out = resolve_login_identity(
        "messenger", "NEW", "msg_x", registry=reg, extra_prev=["OLD"])
    assert out["changed"] is True
    assert out["prev_accounts"] == ["OLD"]
    assert out["quarantined"] is True
    assert reg.get("messenger", "NEW")["meta"]["identity_pending"] is True


def test_resolve_login_identity_same_account_resume(_silent_alerts):
    reg = FakeRegistry()
    reg.upsert("messenger", "A", meta={"messenger_login_id": "msg_x"},
               merge_meta=True)
    out = resolve_login_identity("messenger", "A", "msg_x", registry=reg)
    assert out == {"changed": False, "prev_accounts": [], "quarantined": False}
    assert not is_identity_pending_row(reg.get("messenger", "A"))


# ── 3. 转正 ──────────────────────────────────────────────────────────────


def test_confirm_clears_pending_and_binds_explicit_persona(_silent_alerts):
    reg = FakeRegistry()
    reg.upsert("messenger", "NEW", meta={
        "identity_pending": True, "identity_prev_accounts": ["OLD"],
    }, merge_meta=True)
    res = confirm_account_identity(
        "messenger", "NEW", persona_id="lin_xiaoyu", registry=reg, actor="boss")
    assert res == {"ok": True, "persona_id": "lin_xiaoyu"}
    meta = reg.get("messenger", "NEW")["meta"]
    assert meta["identity_pending"] is False
    assert meta["identity_confirmed_prev"] == ["OLD"]
    assert meta["identity_confirmed_by"] == "boss"
    assert meta["persona_id"] == "lin_xiaoyu"
    assert meta["persona_ids"] == ["lin_xiaoyu"]


def test_confirm_without_persona_falls_to_default_attach(monkeypatch, _silent_alerts):
    reg = FakeRegistry()
    reg.upsert("messenger", "NEW", meta={"identity_pending": True}, merge_meta=True)
    monkeypatch.setattr(
        "src.ai.persona_voice.ensure_account_default_persona",
        lambda registry, plat, acct, cfg: "default_p")
    res = confirm_account_identity("messenger", "NEW", registry=reg)
    assert res == {"ok": True, "persona_id": "default_p"}
    assert reg.get("messenger", "NEW")["meta"]["identity_pending"] is False


def test_confirm_unknown_account():
    assert confirm_account_identity(
        "messenger", "NOPE", registry=FakeRegistry()) == {
            "ok": False, "persona_id": ""}


def test_pending_accounts_lists_only_pending():
    reg = FakeRegistry()
    reg.upsert("messenger", "P1", meta={
        "identity_pending": True, "self_name": "Wisley",
        "identity_prev_accounts": ["OLD"], "identity_changed_at": 123.0,
    }, merge_meta=True)
    reg.upsert("messenger", "OK1", meta={"identity_pending": False}, merge_meta=True)
    reg.upsert("telegram", "T1")
    rows = pending_accounts(registry=reg)
    assert [r["account_id"] for r in rows] == ["P1"]
    assert rows[0]["prev_accounts"] == ["OLD"]
    assert rows[0]["self_name"] == "Wisley"


# ── 4. 热路径判定 + 自动化封顶 ────────────────────────────────────────────


def test_identity_pending_reads_peek_and_caches(monkeypatch):
    calls = {"n": 0}

    def fake_peek(platform, account_id):
        calls["n"] += 1
        return {"meta": {"identity_pending": True}}

    monkeypatch.setattr(
        "src.integrations.account_registry.peek_account", fake_peek)
    invalidate_pending_cache()
    assert identity_pending("messenger", "X") is True
    assert identity_pending("messenger", "X") is True
    assert calls["n"] == 1          # 第二次走缓存
    invalidate_pending_cache("messenger", "X")
    assert identity_pending("messenger", "X") is True
    assert calls["n"] == 2


def test_identity_pending_fail_open(monkeypatch):
    def boom(platform, account_id):
        raise RuntimeError("registry down")

    monkeypatch.setattr(
        "src.integrations.account_registry.peek_account", boom)
    invalidate_pending_cache()
    assert identity_pending("messenger", "X") is False


def test_compute_mode_caps_identity_layer(monkeypatch):
    from src.inbox.effective_automation import apply_mode_caps, compute_mode_caps
    monkeypatch.setattr(ai_mod, "identity_pending",
                        lambda plat, acct: acct == "PENDING")
    caps = compute_mode_caps(
        platform="messenger", account_id="PENDING", config={},
        business_line="", connected_at=1.0)  # 远古接入 → 预热层不叠加
    layers = [c.layer for c in caps]
    assert "identity_pending" in layers
    eff, applied = apply_mode_caps("auto_ai", caps)
    assert eff == "review"
    assert any(c.layer == "identity_pending" for c in applied)
    # 非 pending 账号零影响
    caps2 = compute_mode_caps(
        platform="messenger", account_id="NORMAL", config={},
        business_line="", connected_at=1.0)
    assert "identity_pending" not in [c.layer for c in caps2]


def test_compute_mode_caps_identity_layer_fail_open(monkeypatch):
    from src.inbox.effective_automation import compute_mode_caps

    def boom(plat, acct):
        raise RuntimeError("x")

    monkeypatch.setattr(ai_mod, "identity_pending", boom)
    caps = compute_mode_caps(
        platform="messenger", account_id="X", config={},
        business_line="", connected_at=1.0)
    assert "identity_pending" not in [c.layer for c in caps]


# ── 5. 人设自动补挂对 pending 跳过 ────────────────────────────────────────


def test_ensure_default_persona_skips_pending(monkeypatch):
    from src.ai.persona_voice import ensure_account_default_persona
    reg = FakeRegistry()
    reg.upsert("messenger", "NEW", meta={"identity_pending": True}, merge_meta=True)
    cfg = {"platform_login": {"default_persona_id": "gu_jia"}}
    assert ensure_account_default_persona(reg, "messenger", "NEW", cfg) == ""
    assert "persona_id" not in reg.get("messenger", "NEW")["meta"]


def test_ensure_default_persona_pending_keeps_explicit_binding():
    """pending 但人已显式绑过 → 如实返回绑定（隔离态不抹人的决定）。"""
    from src.ai.persona_voice import ensure_account_default_persona
    reg = FakeRegistry()
    reg.upsert("messenger", "NEW", meta={
        "identity_pending": True, "persona_id": "lin_xiaoyu",
    }, merge_meta=True)
    assert ensure_account_default_persona(
        reg, "messenger", "NEW", {}) == "lin_xiaoyu"


def test_ensure_default_persona_normal_account_unaffected(monkeypatch):
    from src.ai import persona_voice as pv
    reg = FakeRegistry()
    reg.upsert("messenger", "A")
    monkeypatch.setattr(pv, "default_account_persona_id",
                        lambda cfg, plat: "gu_jia")
    assert pv.ensure_account_default_persona(reg, "messenger", "A", {}) == "gu_jia"
    assert reg.get("messenger", "A")["meta"]["persona_id"] == "gu_jia"


# ── 6. session-status 接线（wiring 契约）────────────────────────────────


def test_session_status_authorized_invokes_resolution(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import src.web.routes.unified_inbox_account_routes as uar

    seen: Dict[str, Any] = {}

    def fake_resolve(plat, acct, login_id, **kw):
        seen.update({"plat": plat, "acct": acct, "login": login_id,
                     "extra_prev": kw.get("extra_prev")})
        return {"changed": True, "prev_accounts": ["OLD"], "quarantined": True}

    monkeypatch.setattr(ai_mod, "resolve_login_identity", fake_resolve)

    class _Health:
        def record(self, plat, acct, status, **kw):
            return {"changed": True, "went_unhealthy": False,
                    "recovered": False, "prev": "", "status": status,
                    "superseded": ["OLD"]}

    monkeypatch.setattr(
        "src.integrations.platform_session_health.get_platform_session_health",
        lambda: _Health())
    monkeypatch.setattr(
        "src.integrations.platform_session_health.mark_superseded_accounts",
        lambda *a, **k: None)

    app = FastAPI()
    uar.register_account_routes(
        app, api_auth=lambda request: None,
        config_manager=SimpleNamespace(config={}))
    c = TestClient(app)
    r = c.post("/api/internal/protocol/session-status", json={
        "platform": "messenger", "account_id": "NEW",
        "status": "authorized", "login_id": "msg_x",
    })
    assert r.status_code == 200, r.text
    assert seen == {"plat": "messenger", "acct": "NEW", "login": "msg_x",
                    "extra_prev": ["OLD"]}


def test_identity_admin_routes(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import src.web.routes.unified_inbox_account_routes as uar

    reg = FakeRegistry()
    reg.upsert("messenger", "P1", meta={
        "identity_pending": True, "identity_prev_accounts": ["OLD"],
    }, merge_meta=True)
    monkeypatch.setattr(ai_mod, "_get_registry", lambda registry=None: reg)

    app = FastAPI()
    uar.register_account_routes(
        app, api_auth=lambda request: None,
        config_manager=SimpleNamespace(config={}))
    c = TestClient(app)

    r = c.get("/api/admin/account-identity/pending")
    assert r.status_code == 200
    assert [x["account_id"] for x in r.json()["pending"]] == ["P1"]

    r2 = c.post("/api/admin/account-identity/confirm", json={
        "platform": "messenger", "account_id": "P1",
        "persona_id": "lin_xiaoyu",
    })
    assert r2.status_code == 200, r2.text
    assert r2.json() == {"ok": True, "persona_id": "lin_xiaoyu"}
    assert reg.get("messenger", "P1")["meta"]["identity_pending"] is False

    r3 = c.get("/api/admin/account-identity/pending")
    assert r3.json()["pending"] == []
    # 字段契约：候选检测段常在（无 removed 账号/无 store ＝ 空表，不缺键）
    assert r3.json()["fleet_candidates"] == []


# ── 7. 疑似自有号未登记候选检测（实施72 下一阶段 P1）────────────────────────
#
# 靶：跨机自有号互聊只靠人工登记 own_fleet.extra——漏登记零提示。检测面＝
# 对端命中本机注册表 removed 归档账号（账密迁去别机的痕迹，Calixa 原型）。
# 铁律：只匹 removed（现役同库对子=老板扮客户常态流，提示登记会诱导误封）；
# 已登记 extra 的不再提示；只读，绝无写口。


def _fleet_rows() -> List[Dict[str, Any]]:
    return [
        {"platform": "messenger", "account_id": "CAL", "status": "removed",
         "meta": {"self_name": "Calixa Lopez"}},
        {"platform": "messenger", "account_id": "ACT", "status": "online",
         "meta": {"self_name": "Active Own"}},
    ]


def _conv(ck: str, name: str = "", owner: str = "W",
          plat: str = "messenger", **kw: Any) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "platform": plat, "account_id": owner, "chat_key": ck,
        "display_name": name, "chat_type": "private", "last_ts": 100.0}
    row.update(kw)
    return row


def test_own_fleet_candidates_matches_removed_by_id_and_name():
    from src.companion.proactive_peer_hygiene import own_fleet_candidates
    convs = [
        _conv("TH1", "Calixa  Lopez", last_ts=50.0),   # 名字命中（空白归一）
        _conv("CAL", "whoever", last_ts=200.0),        # id 命中
        _conv("TH3", "Random Customer"),               # 无命中
    ]
    out = own_fleet_candidates(_fleet_rows(), convs, {})
    assert [(c["chat_key"], c["match"]) for c in out] == [
        ("CAL", "id"), ("TH1", "name")]                # last_ts 降序
    assert all(c["matched_account"] == "CAL" for c in out)
    assert out[1]["matched_name"] == "Calixa Lopez"
    assert out[1]["peer_name"] == "Calixa  Lopez"


def test_own_fleet_candidates_active_account_deliberately_ignored():
    """对端命中**现役**账号＝同库对子（老板扮客户测全自动），绝不提示——
    提示会诱导运营把现役号登进 own_fleet.extra 白吃 review 封顶。"""
    from src.companion.proactive_peer_hygiene import own_fleet_candidates
    convs = [_conv("ACT", "whoever"), _conv("TH2", "Active Own")]
    assert own_fleet_candidates(_fleet_rows(), convs, {}) == []


def test_own_fleet_candidates_registered_extra_excluded():
    """已登记 own_fleet.extra ＝ 封顶层已接管 → 不再提示（本机 Calixa 现状）。"""
    from src.companion.proactive_peer_hygiene import own_fleet_candidates
    cfg = {"companion": {"own_fleet": {"extra": [
        {"platform": "messenger", "account_id": "CAL", "name": "Calixa Lopez"},
    ]}}}
    convs = [_conv("TH1", "Calixa Lopez"), _conv("CAL", "whoever")]
    assert own_fleet_candidates(_fleet_rows(), convs, cfg) == []


def test_own_fleet_candidates_dismissed_excluded():
    """已裁决（own_fleet.dismissed）＝运营看过说「测试/废号」→ 消音不再提示。

    与 extra 语义相反：dismissed 零行为变更（不封顶），只按**账号**消音；
    支持 "plat:acct" 字符串与 {platform, account_id, reason} dict 两种条目，
    跨平台不误消（whatsapp:CAL 不消 messenger:CAL）。2026-08-27 老板裁决三条
    候选全为测试/废号即首个消费场景。
    """
    from src.companion.proactive_peer_hygiene import (
        own_fleet_candidates,
        own_fleet_dismissed_from_config,
    )
    convs = [_conv("TH1", "Calixa Lopez"), _conv("CAL", "whoever")]
    # 字符串条目（大小写归一）
    cfg = {"companion": {"own_fleet": {"dismissed": ["messenger:cal"]}}}
    assert own_fleet_candidates(_fleet_rows(), convs, cfg) == []
    # dict 条目（自带 reason 存档，代码只认 platform/account_id）
    cfg2 = {"companion": {"own_fleet": {"dismissed": [
        {"platform": "messenger", "account_id": "CAL", "reason": "测试号"},
    ]}}}
    assert own_fleet_candidates(_fleet_rows(), convs, cfg2) == []
    # 跨平台不误消：whatsapp:CAL 消不掉 messenger 的候选
    cfg3 = {"companion": {"own_fleet": {"dismissed": ["whatsapp:cal"]}}}
    assert len(own_fleet_candidates(_fleet_rows(), convs, cfg3)) == 2
    # 解析护栏：坏结构/无冒号条目一律忽略
    assert own_fleet_dismissed_from_config(
        {"companion": {"own_fleet": {"dismissed": [
            "nocolon", {"platform": "", "account_id": "X"}, 42]}}}) == set()
    assert own_fleet_dismissed_from_config({}) == set()
    assert own_fleet_dismissed_from_config(None) == set()


def test_own_fleet_candidates_scope_guards():
    """群聊 / 自聊（me、chat_key==owner）/ 归档号自己的会话目录 / 跨平台不匹。"""
    from src.companion.proactive_peer_hygiene import own_fleet_candidates
    convs = [
        _conv("CAL", "x", chat_type="group"),          # 群聊不判
        _conv("me", "Calixa Lopez"),                   # 自聊
        _conv("W", "Calixa Lopez", owner="W"),         # chat_key==owner 自聊
        _conv("TH1", "Calixa Lopez", owner="CAL"),     # 归档号遗留目录（owner==命中）
        _conv("CAL", "x", plat="whatsapp"),            # 平台不同不匹
    ]
    assert own_fleet_candidates(_fleet_rows(), convs, {}) == []


def test_own_fleet_candidates_dedup_and_cap():
    from src.companion.proactive_peer_hygiene import own_fleet_candidates
    convs = [_conv("CAL", "x", last_ts=float(1000 + i))
             for i in range(5)]                                # 同键重复
    convs += [_conv(f"T{i}", "Calixa Lopez", owner=f"o{i}",
                    last_ts=float(i)) for i in range(30)]
    out = own_fleet_candidates(_fleet_rows(), convs, {}, max_items=10)
    assert len(out) == 10
    assert sum(1 for c in out if c["chat_key"] == "CAL") == 1  # 去重
    assert out == sorted(out, key=lambda x: -x["last_ts"])


class _FakeInboxStore:
    """最小 inbox store 假件：只实现候选收集器要的 list_conversations。"""

    def __init__(self, convs: List[Dict[str, Any]]) -> None:
        self.convs = convs
        self.calls: List[str] = []

    def list_conversations(self, *, limit: int = 50, platform: str = "",
                           **kw: Any) -> List[Dict[str, Any]]:
        self.calls.append(platform)
        return [dict(c) for c in self.convs
                if not platform or c["platform"] == platform]


def test_fleet_candidates_collector_and_cache(monkeypatch):
    reg = FakeRegistry()
    reg.upsert("messenger", "CAL", status="removed",
               meta={"self_name": "Calixa Lopez"}, merge_meta=True)
    reg.upsert("messenger", "W")
    monkeypatch.setattr(ai_mod, "_get_registry", lambda registry=None: reg)
    store = _FakeInboxStore([_conv("TH1", "Calixa Lopez")])

    out = fleet_candidates({}, inbox_store=store)
    assert [c["chat_key"] for c in out] == ["TH1"]
    assert store.calls == ["messenger"]  # 只扫有 removed 账号的平台

    # 300s TTL：二次调用吃缓存，不再扫库
    out2 = fleet_candidates({}, inbox_store=store)
    assert out2 == out and store.calls == ["messenger"]
    invalidate_fleet_candidates_cache()
    fleet_candidates({}, inbox_store=store)
    assert store.calls == ["messenger", "messenger"]


def test_fleet_candidates_fast_path_no_removed(monkeypatch):
    """注册表无 removed 行 → 零会话扫描（快路径），store 完全不被触碰。"""
    reg = FakeRegistry()
    reg.upsert("messenger", "W")
    monkeypatch.setattr(ai_mod, "_get_registry", lambda registry=None: reg)
    store = _FakeInboxStore([_conv("TH1", "Calixa Lopez")])
    assert fleet_candidates({}, inbox_store=store) == []
    assert store.calls == []


def test_fleet_candidates_never_raises(monkeypatch):
    """registry / store 任何异常 → 空表（绝不拖垮 pending 主体）。"""

    class _Boom:
        def list(self, *a: Any, **k: Any):
            raise RuntimeError("boom")

    monkeypatch.setattr(ai_mod, "_get_registry", lambda registry=None: _Boom())
    assert fleet_candidates({}, inbox_store=object()) == []


def test_identity_pending_route_carries_fleet_candidates(monkeypatch):
    """路由契约：pending 端点带 fleet_candidates（app.state.inbox_store 注入）。"""
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import src.web.routes.unified_inbox_account_routes as uar

    reg = FakeRegistry()
    reg.upsert("messenger", "CAL", status="removed",
               meta={"self_name": "Calixa Lopez"}, merge_meta=True)
    monkeypatch.setattr(ai_mod, "_get_registry", lambda registry=None: reg)

    app = FastAPI()
    uar.register_account_routes(
        app, api_auth=lambda request: None,
        config_manager=SimpleNamespace(config={}))
    app.state.inbox_store = _FakeInboxStore([_conv("TH1", "Calixa Lopez")])
    c = TestClient(app)

    d = c.get("/api/admin/account-identity/pending").json()
    assert [x["chat_key"] for x in d["fleet_candidates"]] == ["TH1"]
    assert d["fleet_candidates"][0]["matched_account"] == "CAL"
    assert d["fleet_candidates"][0]["match"] == "name"
