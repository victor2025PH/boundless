"""老板锁 ``ai.primary_lock`` + 切换审计（2026-08-22 事故沉淀）。

守六条不变量：
1. **锁在生效点强制**：配置被任何途径改成与锁不符的档，AIClient 装载时强制回锁值
   （手改 overlay / 外部自动化绕过接口也拦得住——这是 08-21「翻回 local_only」
   事故的机制性收口）。
2. 锁**不凌驾**「本地端点缺失退 cloud」防变砖护栏（安全 > 治理）。
3. 非法锁值＝不锁（收紧动作解析不确定时宁可不收紧）。
4. 治理接口拒绝与锁不符的切换请求（err.setup.ai_primary_locked），且**留审计行**。
5. 审计台账：接口保存/拒绝、装载强制都有行可查（AITR_DATA_DIR 隔离，测试零污染）。
6. compute_mode CLI 的 local/local_only 档已按老板指令移除（传入即抛，绝不静默）。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from src.ai.ai_client import AIClient
from src.ai.ai_primary_audit import (
    append_event, audit_path, last_state, read_tail, resolve_lock,
)
from tests.test_ai_client_chat_fallback import _Cfg


# ── 纯函数：锁解析 ─────────────────────────────────────────────────────────

def test_resolve_lock_normalizes():
    assert resolve_lock({"primary_lock": "cloud"}) == "cloud"
    assert resolve_lock({"primary_lock": " LOCAL_ONLY "}) == "local_only"
    assert resolve_lock({"primary_lock": "local"}) == "local"


def test_resolve_lock_invalid_or_absent_means_unlocked():
    assert resolve_lock({}) == ""
    assert resolve_lock({"primary_lock": ""}) == ""
    assert resolve_lock({"primary_lock": "clouds"}) == ""      # 拼错=不锁
    assert resolve_lock({"primary_lock": True}) == ""          # 类型畸形=不锁
    assert resolve_lock(None) == ""


# ── 审计台账 ───────────────────────────────────────────────────────────────

def test_audit_append_and_read_tail(monkeypatch, tmp_path):
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    assert append_event("switch_saved", mode_from="cloud", mode_to="local",
                        actor="user:t", via="endpoint") is True
    assert append_event("lock_enforced", configured="local_only", lock="cloud",
                        via="ai_client_init") is True
    p = audit_path()
    assert str(tmp_path) in str(p), "台账必须落 AITR_DATA_DIR（测试隔离/生产数据根）"
    rows = read_tail(10)
    assert [r["event"] for r in rows] == ["switch_saved", "lock_enforced"]
    assert rows[0]["actor"] == "user:t"
    assert rows[1]["lock"] == "cloud"


def test_audit_read_tail_skips_bad_lines(monkeypatch, tmp_path):
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    append_event("resolve", effective="cloud", via="ai_client_init")
    p = audit_path()
    with p.open("a", encoding="utf-8") as f:
        f.write("not-json\n")
    rows = read_tail(10)
    assert len(rows) == 1 and rows[0]["event"] == "resolve"


def test_audit_never_raises_on_unwritable_path(monkeypatch, tmp_path):
    # 落点不可写（logs 位置被文件占住 → mkdir 必败）也不许抛——治理设施绝不能反噬主链
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    (tmp_path / "logs").write_text("not a dir", encoding="utf-8")
    assert append_event("resolve", via="t") is False
    assert read_tail(5) == []


# ── AIClient 生效点强制 ───────────────────────────────────────────────────

def _probe_cfg(primary: str, lock: str = "", with_fallback: bool = True):
    class _C(_Cfg):
        def get_ai_config(self):
            ai = {
                "provider": "openai_compatible",
                "api_key": "sk-test",
                "base_url": "https://api.deepseek.com",
                "model": "deepseek-chat",
                "primary": primary,
            }
            if lock:
                ai["primary_lock"] = lock
            if with_fallback:
                ai["fallback"] = {"enabled": True, "model": "qwen3:30b",
                                  "base_url": "http://192.168.0.176:11434"}
            return ai
    return _C()


def _patch_probes(monkeypatch, *, local_ok: bool, cloud_ok: bool):
    class _FakeOpenAI:
        def __init__(self, **kw):
            pass

    monkeypatch.setattr("src.ai.ai_client.AsyncOpenAI", _FakeOpenAI)

    async def _local(self):
        return local_ok

    async def _cloud(self):
        return cloud_ok

    monkeypatch.setattr(AIClient, "_test_local_connection", _local)
    monkeypatch.setattr(AIClient, "_test_openai_connection", _cloud)


async def test_lock_forces_cloud_over_configured_local_only(monkeypatch, tmp_path):
    """08-21 事故复刻：overlay 被翻成 local_only，锁=cloud → 装载强制回 cloud。"""
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    _patch_probes(monkeypatch, local_ok=False, cloud_ok=True)
    c = AIClient(_probe_cfg("local_only", lock="cloud"))
    assert await c.initialize() is True
    assert c._primary_mode == "cloud"
    assert c._primary_lock == "cloud"
    rows = [r for r in read_tail(20) if r["event"] == "lock_enforced"]
    assert rows and rows[-1]["configured"] == "local_only"
    assert rows[-1]["effective"] == "cloud"
    assert c.get_stats()["primary_lock"] == "cloud"


async def test_lock_forces_local_only_over_configured_cloud(monkeypatch, tmp_path):
    """锁是双向治理：老板若锁 local_only（隐私优先），配置漂回 cloud 同样被纠正。"""
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    _patch_probes(monkeypatch, local_ok=True, cloud_ok=False)
    c = AIClient(_probe_cfg("cloud", lock="local_only"))
    assert await c.initialize() is True
    assert c._primary_mode == "local_only"


async def test_lock_does_not_override_missing_endpoint_safety(monkeypatch, tmp_path):
    """锁到 local_only 但 ai.fallback 端点缺失 → 防变砖护栏仍退 cloud（安全>治理）。"""
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    _patch_probes(monkeypatch, local_ok=True, cloud_ok=True)
    c = AIClient(_probe_cfg("cloud", lock="local_only", with_fallback=False))
    await c.initialize()
    assert c._primary_mode == "cloud", "无本地端点时锁不得把聊天变砖"
    rows = [r for r in read_tail(20) if r["event"] == "lock_enforced"]
    assert rows and rows[-1]["effective"] == "cloud", "审计须如实记录锁未落成生效值"


async def test_invalid_lock_value_is_ignored(monkeypatch, tmp_path):
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    _patch_probes(monkeypatch, local_ok=True, cloud_ok=False)
    c = AIClient(_probe_cfg("local_only", lock="clouds"))   # 拼错的锁
    assert await c.initialize() is True
    assert c._primary_mode == "local_only", "非法锁值＝不锁，配置照常生效"
    assert c._primary_lock == ""


async def test_no_lock_keeps_old_behavior(monkeypatch, tmp_path):
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    _patch_probes(monkeypatch, local_ok=True, cloud_ok=False)
    c = AIClient(_probe_cfg("local_only"))
    assert await c.initialize() is True
    assert c._primary_mode == "local_only"


# ── 治理接口拒绝 + 审计 ────────────────────────────────────────────────────

def _locked_client(tmp_path, monkeypatch, *, lock: str, primary: str = "cloud"):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from src.utils.config_manager import ConfigManager

    import src.web.routes.unified_inbox_setup_routes as setup_mod
    from src.web.routes.unified_inbox_setup_routes import register_setup_routes

    async def _fake_reload(app, cm):
        return True

    monkeypatch.setattr(setup_mod, "reload_ai_runtime", _fake_reload)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("ai:\n  api_key: \"\"\n", encoding="utf-8")
    m = ConfigManager(str(cfg))
    m.config = {"ai": {
        "provider": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "api_key": "sk-primary", "model": "deepseek-chat",
        "primary": primary,
        "primary_lock": lock,
        "fallback": {"enabled": True, "base_url": "http://192.168.0.176:11434",
                     "model": "qwen3:30b"},
    }}
    app = FastAPI()
    register_setup_routes(app, api_auth=lambda request: None, config_manager=m)
    return TestClient(app), m


def test_endpoint_rejects_switch_against_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    client, m = _locked_client(tmp_path, monkeypatch, lock="cloud")
    r = client.post("/api/setup/ai-primary", json={"primary": "local_only"}).json()
    assert r["ok"] is False
    assert r.get("locked") is True
    assert r.get("detail")
    assert (m.config["ai"].get("primary") or "cloud") == "cloud", "拒绝时不得落盘"
    rows = [x for x in read_tail(20) if x["event"] == "switch_rejected"]
    assert rows and rows[-1]["requested"] == "local_only"
    assert rows[-1]["lock"] == "cloud"


def test_endpoint_allows_lock_conforming_save(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    client, m = _locked_client(tmp_path, monkeypatch, lock="cloud", primary="cloud")
    r = client.post("/api/setup/ai-primary", json={"primary": "cloud"}).json()
    assert r["ok"] is True
    rows = [x for x in read_tail(20) if x["event"] == "switch_saved"]
    assert rows and rows[-1]["mode_to"] == "cloud"


def test_endpoint_unlocked_save_still_audited(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    client, m = _locked_client(tmp_path, monkeypatch, lock="", primary="cloud")
    r = client.post("/api/setup/ai-primary", json={"primary": "local_only"}).json()
    assert r["ok"] is True, "无锁时旧行为不变"
    rows = [x for x in read_tail(20) if x["event"] == "switch_saved"]
    assert rows and rows[-1]["mode_from"] == "cloud"
    assert rows[-1]["mode_to"] == "local_only"
    assert rows[-1]["via"] == "endpoint"


def test_snapshot_carries_lock(tmp_path, monkeypatch):
    from src.web.routes.unified_inbox_setup_routes import _ai_primary_snapshot

    class _Mgr:
        config = {"ai": {"primary": "cloud", "primary_lock": "cloud"}}

    snap = _ai_primary_snapshot(_Mgr(), None)
    assert snap["lock"] == "cloud" and snap["locked"] is True

    class _Mgr2:
        config = {"ai": {"primary": "cloud"}}

    snap2 = _ai_primary_snapshot(_Mgr2(), None)
    assert snap2["lock"] == "" and snap2["locked"] is False


def test_audit_endpoint_returns_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    append_event("switch_saved", mode_from="cloud", mode_to="cloud",
                 actor="user:t", via="endpoint")
    client, _ = _locked_client(tmp_path, monkeypatch, lock="cloud")
    r = client.get("/api/setup/ai-primary/audit").json()
    assert r["ok"] is True
    assert r["lock"] == "cloud" and r["locked"] is True
    assert any(row["event"] == "switch_saved" for row in r["rows"])


def test_last_state_uses_resolve_not_switch_saved(monkeypatch, tmp_path):
    """切档通知只认装载点生效行；switch_saved 是「写了 overlay」不是生效态。"""
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    assert last_state() == {"effective": None, "lock": None}
    append_event("switch_saved", mode_from="cloud", mode_to="local", lock="local")
    assert last_state()["effective"] is None
    append_event("resolve", configured="local", effective="local", lock="local",
                 via="ai_client_init")
    assert last_state() == {"effective": "local", "lock": "local"}
    append_event("lock_enforced", configured="cloud", effective="local", lock="local",
                 via="ai_client_init")
    assert last_state()["effective"] == "local" and last_state()["lock"] == "local"


async def test_mode_switch_notifies_ops_then_silent_on_same_mode(monkeypatch, tmp_path):
    """装载点：台账里上次是 cloud，本次解析成 local → 发 mode_switched；同档再启不刷群。"""
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    _patch_probes(monkeypatch, local_ok=True, cloud_ok=True)
    events = []

    class _Bus:
        def publish(self, name, payload):
            events.append((name, payload))

    monkeypatch.setattr("src.integrations.shared.event_bus.get_event_bus", lambda: _Bus())
    nudge = tmp_path / "compute_pusher.nudge"
    monkeypatch.setattr("src.ai.ai_primary_summary.BOARD_NUDGE_PATH", nudge)
    append_event("resolve", configured="cloud", effective="cloud", lock="cloud",
                 via="ai_client_init")
    c = AIClient(_probe_cfg("local", lock="local"))
    assert await c.initialize() is True
    switched = [p for n, p in events
                if n == "ai_primary_guard_alert" and (p or {}).get("kind") == "mode_switched"]
    assert len(switched) == 1
    assert switched[0]["from_mode"] == "cloud" and switched[0]["to_mode"] == "local"
    assert switched[0]["lock"] == "local"
    assert "本地 vLLM" in str(switched[0].get("primary_text") or "")
    assert any(r["event"] == "switch_notified" for r in read_tail(20))
    assert nudge.exists()

    events.clear()
    c2 = AIClient(_probe_cfg("local", lock="local"))
    assert await c2.initialize() is True
    assert not [p for n, p in events
                if (p or {}).get("kind") == "mode_switched"]


# ── compute_mode CLI：local 档已移除 ──────────────────────────────────────

def _load_compute_mode():
    path = Path(__file__).resolve().parent.parent / "deploy" / "compute" / "compute_mode.py"
    spec = importlib.util.spec_from_file_location("compute_mode_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_compute_mode_local_modes_removed():
    mod = _load_compute_mode()
    assert "local" not in mod.MODES and "local_only" not in mod.MODES
    assert set(mod.REMOVED_MODES) == {"local", "local_only"}
    with pytest.raises(ValueError):
        mod.apply_mode({}, "local_only")
    with pytest.raises(ValueError):
        mod.apply_mode({}, "local")


def test_compute_mode_cloud_still_writes_primary():
    mod = _load_compute_mode()
    data = {"ai": {"primary": "local_only"}}
    notes = mod.apply_mode(data, "cloud")
    assert data["ai"]["primary"] == "cloud"
    assert any("ai.primary" in n for n in notes)
