# -*- coding: utf-8 -*-
"""双面板融合 P0 门禁（surface_fusion，2026-08-13）。

钉住四层不变量：
- **能力注册表契约**：状态枚举合法 / bridge 必有合法目标 / 每个 capability
  的 i18n 标签 zh+en 双语齐平（pack surface_fusion）/ Messenger 核心行不漂移
  （send_text 双 ok、autosend=workspace 独有、calls=永久桥接）；
- **驾驶权锁语义**：无记录默认 workspace（零行为变更基线）/ 切换持久化 +
  审计历史 capped / 非法 owner 拒绝 / 跨进程可见（mtime 缓存失效）；
- **autosend 让位判定**：总开关关＝恒放行；开且 owner=native 才拦；异常 fail-open；
- **AutosendWorker guard 接线**：owner=native → L2 取消（decided_by=pilot_native）
  不 resolve 不投递；guard 未注入/返 False/抛异常 → 零行为变更；快照带计数。

路由契约（/api/surface/*）用最小 FastAPI app 端到端打（无 session 中间件时
写保护按「无角色」放行——生产 app 的 viewer 拦截逻辑单测在 _require_write
的 try/except 语义内，此处不重复建全套会话栈）。
"""
from __future__ import annotations

import json

import pytest

from src.integrations import surface_fusion as sf
from src.inbox.autosend_worker import AutosendWorker


@pytest.fixture()
def pilot_env(tmp_path, monkeypatch):
    """锁文件隔离进本测试的 tmp（config_dir 按 AITR_DATA_DIR 解析）+ 缓存复位。"""
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("AITR_CONFIG_PATH", raising=False)
    sf._reset_cache_for_tests()
    yield tmp_path
    sf._reset_cache_for_tests()


# ── 能力注册表 ────────────────────────────────────────────────────────────────


def test_registry_status_enums_and_bridge_targets():
    """全部策划平台（messenger/telegram/whatsapp/…）逐行契约——第二批平台加进
    _REGISTRY 后自动纳入本门禁，坏行（非法状态/桥接无去处）立即变红。"""
    assert sf.curated_platforms(), "至少一个策划平台"
    for plat in sf.curated_platforms():
        rows = sf.capability_matrix(plat)
        assert rows, f"{plat} 策划了却空表"
        for r in rows:
            assert r["workspace"] in ("ok", "bridge", "none"), (plat, r)
            assert r["native"] in ("ok", "assist", "none"), (plat, r)
            if r["workspace"] == "bridge":
                # 桥接必须有去处，且去处面板真的具备该能力
                assert r["bridge_to"] == sf.SURFACE_NATIVE, (plat, r)
                assert r["native"] == "ok", (plat, r)
            assert r["phase"] in ("P0", "P1", "P2", "P3"), (plat, r)


def test_registry_labels_bilingual():
    """所有策划平台的每个能力都必须有 sf.cap.* 双语标签（第二批平台复用同 id 集）。"""
    from src.web.i18n_packs import surface_fusion as pack
    for plat in sf.curated_platforms():
        for r in sf.capability_matrix(plat):
            key = r["label_key"]
            assert key in pack.ZH and str(pack.ZH[key]).strip(), (plat, key)
            assert key in pack.EN and str(pack.EN[key]).strip(), (plat, key)


def test_registry_all_curated_summaries_partition_cleanly():
    """每个策划平台的四分组并集无重叠、且全是注册表合法 id（tg/wa 第二批同样过闸）。"""
    for plat in sf.curated_platforms():
        s = sf.capability_summary(plat)
        allocated = [cid for g in ("both", "workspace_only", "native_only", "bridge")
                     for cid in s[g]]
        assert len(allocated) == len(set(allocated)), (plat, "能力落进多个组")
        assert set(allocated) <= {r["id"] for r in sf.capability_matrix(plat)}


def test_registry_messenger_core_rows_pinned():
    by_id = {r["id"]: r for r in sf.capability_matrix("messenger")}
    # 双面板已对齐的核心能力
    assert by_id["send_text"]["workspace"] == "ok"
    assert by_id["send_text"]["native"] == "ok"
    # 全自动＝工作台独有（原生受控出站属 P2，上线前 native=none 是诚实态）
    assert by_id["autosend"]["workspace"] == "ok"
    assert by_id["autosend"]["native"] == "none"
    # 通话＝永久桥接（sidecar 技术上不可能）
    assert by_id["calls"]["workspace"] == "bridge"
    assert by_id["calls"]["bridge_to"] == "native"


def test_registry_unknown_platform_empty_and_curated_list():
    # line 无可内嵌官方网页（renderer.js 刻意不嵌）＝长期的「未策划」样例；
    # telegram/whatsapp 已入注册表（第二批 2026-08-13，见 test_surface_fusion_platforms）。
    assert sf.capability_matrix("line") == []
    assert sf.capability_matrix("") == []
    assert set(sf.curated_platforms()) == {"messenger", "telegram", "whatsapp"}
    assert "messenger" in sf.EMBEDDABLE_PLATFORMS


def test_capability_summary_partitions_all_rows_once():
    """能力四分组：并集覆盖有归属的行、组间不重叠、messenger 关键分组钉死。"""
    rows = sf.capability_matrix("messenger")
    s = sf.capability_summary("messenger")
    groups = ["both", "workspace_only", "native_only", "bridge"]
    assert set(s.keys()) == set(groups)
    allocated = [cid for g in groups for cid in s[g]]
    assert len(allocated) == len(set(allocated)), "能力不得落进多个组"
    # 全自动＝工作台独有；转发/通话＝跳原生页。能力分组钉死：
    # P3 表情出站/已读（真相翻转）→ both；native_only=typing（网页版无 presence API，
    # 刻意不做）+ quote_reply（2026-08-14 工作台引用入口整体下线，老板拍板——
    # 原生页自带引用不受影响，故落 native_only 是诚实态）。
    assert "autosend" in s["workspace_only"]
    assert "forward" in s["bridge"] and "calls" in s["bridge"]
    assert set(s["native_only"]) == {"typing", "quote_reply"}
    for cap in ("reaction_out", "mark_read"):
        assert cap in s["both"], cap
    assert "send_text" in s["both"] and "translate_text" in s["both"]
    # 每个被归组的能力都是注册表里的合法 id
    ids = {r["id"] for r in rows}
    assert set(allocated) <= ids


def test_capability_summary_unknown_platform_empty():
    s = sf.capability_summary("line")
    assert s == {"both": [], "workspace_only": [], "native_only": [], "bridge": []}


# ── 驾驶权锁 ──────────────────────────────────────────────────────────────────


def test_pilot_default_workspace(pilot_env):
    p = sf.get_pilot("messenger", "acct1")
    assert p["owner"] == "workspace"
    assert p["since"] == 0.0


def test_pilot_set_get_roundtrip_and_persistence(pilot_env):
    out = sf.set_pilot("messenger", "acct1", "native", by="boss")
    assert out["owner"] == "native" and out["by"] == "boss"
    # 缓存失效后从磁盘重读仍一致（跨进程可见性等价物）
    sf._reset_cache_for_tests()
    assert sf.get_pilot("messenger", "acct1")["owner"] == "native"
    # 其它账号不受影响（键隔离）
    assert sf.get_pilot("messenger", "acct2")["owner"] == "workspace"
    # 落盘文件形态：pilots + history
    raw = json.loads((pilot_env / "config" / "surface_pilot.json")
                     .read_text(encoding="utf-8"))
    assert raw["pilots"]["messenger:acct1"]["owner"] == "native"
    assert raw["history"][-1]["prev"] == "workspace"


def test_pilot_invalid_owner_rejected(pilot_env):
    with pytest.raises(ValueError):
        sf.set_pilot("messenger", "acct1", "browser")
    with pytest.raises(ValueError):
        sf.set_pilot("messenger", "acct1", "")


def test_pilot_history_capped(pilot_env):
    for i in range(60):
        sf.set_pilot("messenger", f"a{i % 3}",
                     "native" if i % 2 else "workspace")
    raw = json.loads((pilot_env / "config" / "surface_pilot.json")
                     .read_text(encoding="utf-8"))
    assert len(raw["history"]) <= 50


def test_pilot_corrupt_file_fails_open(pilot_env):
    p = pilot_env / "config"
    p.mkdir(parents=True, exist_ok=True)
    (p / "surface_pilot.json").write_text("{not json", encoding="utf-8")
    sf._reset_cache_for_tests()
    assert sf.get_pilot("messenger", "acct1")["owner"] == "workspace"


# ── autosend 让位判定 ─────────────────────────────────────────────────────────


def test_pilot_yield_stats_semantics(pilot_env):
    """让位计数（P4）：分链累计 / 键隔离 / 快照无共享引用 / reset 清零。"""
    sf.note_pilot_yield("messenger", "acct1", "a_line")
    sf.note_pilot_yield("messenger", "acct1", "autosend")
    sf.note_pilot_yield("messenger", "acct1", "a_line")
    sf.note_pilot_yield("telegram", "acct2", "autosend")
    s = sf.pilot_yield_stats()
    assert s["messenger:acct1"]["count"] == 3
    assert s["messenger:acct1"]["chains"] == {"a_line": 2, "autosend": 1}
    assert s["telegram:acct2"]["count"] == 1
    assert s["messenger:acct1"]["last_ts"] > 0
    # 快照是拷贝：改快照不得污染内部状态
    s["messenger:acct1"]["chains"]["a_line"] = 999
    assert sf.pilot_yield_stats()["messenger:acct1"]["chains"]["a_line"] == 2
    sf._reset_cache_for_tests()
    assert sf.pilot_yield_stats() == {}


def test_pilot_yield_wired_at_enforcement_sites():
    """让位计数必须接在两条执行链上（A 线 protocol_autoreply + B 线 bootstrap guard）——
    静态接线断言：挪走/漏接即红（计数只在谓词为真的执行点调用，谓词本身保持纯）。"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    a_line = (root / "src" / "integrations" / "protocol_autoreply.py").read_text(
        encoding="utf-8")
    assert "note_pilot_yield" in a_line and '"a_line"' in a_line
    boot = (root / "src" / "bootstrap" / "web_app.py").read_text(encoding="utf-8")
    assert "note_pilot_yield" in boot and '"autosend"' in boot
    # 谓词保持纯：autosend_blocked 本体不得内嵌计数调用（docstring 提及名字不算）
    import inspect
    src = inspect.getsource(sf.autosend_blocked)
    assert "note_pilot_yield(" not in src


def test_autosend_blocked_semantics(pilot_env):
    sf.set_pilot("messenger", "acct1", "native")
    # 开关关 → 恒放行（默认档零行为变更）
    assert sf.autosend_blocked({}, "messenger", "acct1") is False
    assert sf.autosend_blocked(
        {"surface_fusion": {"enabled": False}}, "messenger", "acct1") is False
    # 开关开 + owner=native → 拦
    on = {"surface_fusion": {"enabled": True}}
    assert sf.autosend_blocked(on, "messenger", "acct1") is True
    # 开关开 + owner=workspace（默认）→ 放行
    assert sf.autosend_blocked(on, "messenger", "acct2") is False
    # 切回 workspace → 立即放行（mtime 缓存失效语义）
    sf.set_pilot("messenger", "acct1", "workspace")
    assert sf.autosend_blocked(on, "messenger", "acct1") is False


# ── AutosendWorker guard 接线 ────────────────────────────────────────────────


class _FakeStore:
    def __init__(self):
        self.cancelled = []  # (draft_id, status, decided_by)

    def update_draft_status(self, draft_id, *, status, final_text="",
                            decided_by=""):
        self.cancelled.append((draft_id, status, decided_by))
        return True


class _Svc:
    def __init__(self, drafts):
        self._drafts = drafts
        self._store = _FakeStore()
        self.resolved = []

    def list_drafts(self, status="pending", limit=200):
        return [dict(d) for d in self._drafts]

    def resolve_with_audit(self, draft_id, action, by=""):
        self.resolved.append(draft_id)
        return {"ok": True}


def _draft(draft_id="d1"):
    import time as _t
    return {
        "draft_id": draft_id, "autopilot_level": "L2",
        "final_text": "在的呀~", "platform": "messenger",
        "account_id": "acct1", "chat_key": "1000123",
        "conversation_id": f"messenger:acct1:{draft_id}",
        "peer_text": "在吗？",
        "created_ts": _t.time() - 30,
    }


async def _noop_send(platform, account_id, chat_key, text):
    return {"delivered": True}


def test_worker_pilot_native_cancels_and_counts():
    svc = _Svc([_draft()])
    seen = []

    def guard(platform, account_id):
        seen.append((platform, account_id))
        return True

    w = AutosendWorker(draft_service=svc, send_callback=_noop_send,
                       config={}, pilot_guard=guard)
    sent, errors, to_deliver = w._process_batch()
    assert to_deliver == [] and sent == 0 and errors == 0
    assert svc.resolved == []  # 不 resolve
    assert svc._store.cancelled == [("d1", "cancelled", "pilot_native")]
    assert w.total_skipped_pilot == 1
    assert seen == [("messenger", "acct1")]
    snap = w.status_snapshot()
    assert snap["total_skipped_pilot"] == 1
    assert snap["pilot_guard_wired"] is True


def test_worker_guard_false_delivers_normally():
    svc = _Svc([_draft()])
    w = AutosendWorker(draft_service=svc, send_callback=_noop_send,
                       config={}, pilot_guard=lambda p, a: False)
    sent, _errors, to_deliver = w._process_batch()
    assert sent == 1 and len(to_deliver) == 1
    assert w.total_skipped_pilot == 0
    assert svc._store.cancelled == []


def test_worker_guard_exception_fails_open():
    svc = _Svc([_draft()])

    def bad_guard(platform, account_id):
        raise RuntimeError("lock backend down")

    w = AutosendWorker(draft_service=svc, send_callback=_noop_send,
                       config={}, pilot_guard=bad_guard)
    sent, _errors, to_deliver = w._process_batch()
    assert sent == 1 and len(to_deliver) == 1  # 锁故障绝不闸死自动回复
    assert w.total_skipped_pilot == 0


def test_worker_no_guard_zero_behavior_change():
    svc = _Svc([_draft()])
    w = AutosendWorker(draft_service=svc, send_callback=_noop_send, config={})
    sent, _errors, to_deliver = w._process_batch()
    assert sent == 1 and len(to_deliver) == 1
    assert w.status_snapshot()["pilot_guard_wired"] is False


def test_worker_guard_only_gates_auto_chain():
    """无 send_callback（仅标记模式）时不查 guard——锁只管「真投递的自动链」。"""
    svc = _Svc([_draft()])
    called = []
    w = AutosendWorker(draft_service=svc, send_callback=None, config={},
                       pilot_guard=lambda p, a: called.append(1) or True)
    w._process_batch()
    assert called == []  # 仅标记模式压根不咨询锁
    assert w.total_skipped_pilot == 0


# ── 路由契约 ──────────────────────────────────────────────────────────────────


def _noop_auth(request: "Request") -> None:  # noqa: F821 - 注解给 FastAPI 看
    return None


def _client(pilot_env, enabled=False):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient

    from src.web.routes.surface_fusion_routes import (
        register_surface_fusion_routes,
    )
    _noop_auth.__annotations__["request"] = Request

    class _CM:
        config = {"surface_fusion": {"enabled": bool(enabled)}}

    app = FastAPI()
    register_surface_fusion_routes(app, api_auth=_noop_auth,
                                   config_manager=_CM())
    return TestClient(app)


def test_routes_capabilities_shape(pilot_env):
    client = _client(pilot_env, enabled=False)
    r = client.get("/api/surface/capabilities",
                   params={"platform": "messenger", "account_id": "acct1"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["enabled"] is False
    assert body["platform"] == "messenger"
    assert any(c["id"] == "calls" for c in body["capabilities"])
    assert body["pilot"]["owner"] == "workspace"
    assert "messenger" in body["embeddable_platforms"]
    # P1：能力四分组随 capabilities 响应回显（前端能力总览浮层单一口径）
    assert "autosend" in body["summary"]["workspace_only"]
    assert set(body["summary"].keys()) == {"both", "workspace_only", "native_only", "bridge"}
    # P4：让位计数随响应回显（进程级读数；空态=空 dict）
    assert isinstance(body["pilot_yields"], dict)


def test_routes_pilot_get_requires_platform(pilot_env):
    client = _client(pilot_env)
    assert client.get("/api/surface/pilot").status_code == 400


def test_routes_pilot_switch_roundtrip(pilot_env):
    client = _client(pilot_env, enabled=True)
    r = client.post("/api/surface/pilot", json={
        "platform": "messenger", "account_id": "acct1", "owner": "native"})
    assert r.status_code == 200
    body = r.json()
    assert body["owner"] == "native" and body["enabled"] is True
    g = client.get("/api/surface/pilot",
                   params={"platform": "messenger", "account_id": "acct1"})
    assert g.json()["owner"] == "native"
    # 模块层同步可见（worker guard 走同一读口）
    assert sf.get_pilot("messenger", "acct1")["owner"] == "native"


def test_routes_pilot_invalid_owner_400(pilot_env):
    client = _client(pilot_env)
    r = client.post("/api/surface/pilot", json={
        "platform": "messenger", "owner": "browser"})
    assert r.status_code == 400
