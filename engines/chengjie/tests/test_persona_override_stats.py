"""会话级人设覆写观测（persona_override_stats）门禁。

覆盖三层：
1. 纯计数器 —— tier 归档 / 平台上限溢出 / legacy 压制口径 / 动作白名单 /
   dump 与 dump_prom 契约。
2. 接线 —— ``resolve_effective_persona`` 每次出结果都打点（观测挂了也绝不
   影响解析本体）。
3. 暴露 —— ``/api/workspace/metrics`` JSON 段 + Prometheus 文本双出口。
"""

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.ai.persona_override_stats import (
    PersonaOverrideStats,
    get_persona_override_stats,
)
from src.ai.persona_voice import conv_binding_key, resolve_effective_persona
from src.utils.persona_manager import PersonaManager

_PROFILES = {
    "personas": {
        "profiles": [
            {"id": "chen_mo", "name": "陈默", "role": "陪伴"},
            {"id": "lin_xiaoyu", "name": "林小雨", "role": "陪伴"},
        ]
    }
}

_FLAG_ON = {"inbox": {"persona_conv_override": {"enabled": True}}}


class _FakeRegistry:
    def __init__(self, meta=None):
        self._meta = meta or {}

    def get(self, platform, account_id):
        return {"meta": dict(self._meta)}


@pytest.fixture()
def pm():
    PersonaManager.reset()
    m = PersonaManager.get_instance()
    m.load_profiles_from_config(_PROFILES)
    yield m
    PersonaManager.reset()


# ── 1. 纯计数器 ──────────────────────────────────────────────────────────────

def test_record_resolve_tier_archiving():
    s = PersonaOverrideStats()
    s.record_resolve("conv_override", "telegram")
    s.record_resolve("account_profile", "telegram")
    s.record_resolve("", "telegram")            # 空 tier → none
    s.record_resolve("bogus_tier", "telegram")  # 未知 tier → none（不炸不丢）
    d = s.dump()
    assert d["resolves"] == 4
    assert d["conv_hits"] == 1
    assert d["account_hits"] == 1
    assert d["fallback_hits"] == 2


def test_conv_by_platform_counts_and_sanitizes():
    s = PersonaOverrideStats()
    s.record_resolve("conv_override", "Telegram")   # 大小写归一
    s.record_resolve("conv_override", "telegram")
    s.record_resolve("conv_override", "")           # 空平台 → unknown
    s.record_resolve("account_profile", "line")     # 非覆写命中不进分布
    d = s.dump()
    assert d["conv_by_platform"] == {"telegram": 2, "unknown": 1}


def test_conv_by_platform_cap_overflows_to_other():
    s = PersonaOverrideStats()
    for i in range(25):
        s.record_resolve("conv_override", f"plat{i}")
    by = s.dump()["conv_by_platform"]
    assert len(by) <= 21  # 20 distinct + __other__
    assert by.get("__other__", 0) >= 5
    assert sum(by.values()) == 25  # 溢出不丢数


def test_legacy_suppressed_semantics():
    s = PersonaOverrideStats()
    # legacy 存在且上层赢了 → 计压制
    s.record_resolve("account_profile", "telegram", legacy_present=True)
    s.record_resolve("conv_override", "telegram", legacy_present=True)
    # tier 空 → legacy 会在调用方回落链生效，不算被压制
    s.record_resolve("", "telegram", legacy_present=True)
    # 无 legacy → 与压制无关
    s.record_resolve("account_profile", "telegram", legacy_present=False)
    assert s.dump()["legacy_suppressed"] == 2


def test_record_action_whitelist():
    s = PersonaOverrideStats()
    for k in ("bind_conv", "unbind_conv", "account_set",
              "legacy_upgrade", "legacy_remove"):
        s.record_action(k)
    s.record_action("drop_database")  # 白名单外 → 忽略
    s.record_action("")
    d = s.dump()
    assert d["actions_total"] == 5
    assert d["actions"]["bind_conv"] == 1
    assert "drop_database" not in d["actions"]


def test_dump_prom_contract():
    s = PersonaOverrideStats()
    s.record_resolve("conv_override", "telegram", legacy_present=True)
    s.record_action("legacy_upgrade")
    text = s.dump_prom()
    assert "persona_override_resolves_total 1" in text
    assert 'persona_override_by_tier_total{tier="conv_override"} 1' in text
    assert "persona_override_legacy_suppressed_total 1" in text
    assert 'persona_override_conv_hits_by_platform_total{platform="telegram"} 1' in text
    assert 'persona_override_actions_total{action="legacy_upgrade"} 1' in text
    # 全部 5 个动作维度恒输出（Grafana 面板不因零值缺 series）
    for k in ("bind_conv", "unbind_conv", "account_set",
              "legacy_upgrade", "legacy_remove"):
        assert f'action="{k}"' in text


def test_reset_clears_everything():
    s = PersonaOverrideStats()
    s.record_resolve("conv_override", "telegram", legacy_present=True)
    s.record_action("bind_conv")
    s.reset()
    d = s.dump()
    assert d["resolves"] == 0 and d["actions_total"] == 0
    assert d["conv_by_platform"] == {} and d["legacy_suppressed"] == 0


# ── 2. resolve 接线 ──────────────────────────────────────────────────────────

def test_resolve_effective_persona_records_observation(pm):
    """出站解析必打点：覆写命中计 conv_override；legacy 共存计压制。"""
    stats = get_persona_override_stats()
    stats.reset()
    key = conv_binding_key("telegram", "acct1", "chat9")
    pm.bind_chat_persona_by_profile_id(key, "chen_mo")
    pm.bind_chat_persona_by_profile_id("chat9", "lin_xiaoyu")  # legacy 共存
    pid, tier = resolve_effective_persona(
        _FLAG_ON, "telegram", "acct1", "chat9",
        registry=_FakeRegistry())
    assert (pid, tier) == ("chen_mo", "conv_override")
    d = stats.dump()
    assert d["resolves"] == 1
    assert d["conv_hits"] == 1
    assert d["conv_by_platform"].get("telegram") == 1
    assert d["legacy_suppressed"] == 1
    stats.reset()


def test_resolve_observation_failure_never_breaks_resolution(pm, monkeypatch):
    """观测挂了（record_resolve 抛异常）→ 解析结果不受任何影响。"""

    def _boom(*a, **kw):
        raise RuntimeError("observability down")

    # __slots__ 类实例属性只读 → patch 类方法（对单例同样生效）
    monkeypatch.setattr(PersonaOverrideStats, "record_resolve", _boom)
    key = conv_binding_key("telegram", "acct1", "chat9")
    pm.bind_chat_persona_by_profile_id(key, "chen_mo")
    pid, tier = resolve_effective_persona(
        _FLAG_ON, "telegram", "acct1", "chat9", registry=_FakeRegistry())
    assert (pid, tier) == ("chen_mo", "conv_override")


# ── 3. metrics 暴露（最小挂载，风格同 test_frontend_error_stats）─────────────

def _metrics_client():
    from src.web.routes.drafts_routes import register_metrics_route
    app = FastAPI()

    @app.middleware("http")
    async def _inject(req: Request, call_next):
        req.scope["session"] = {"role": "admin", "user_id": "u1"}
        return await call_next(req)

    def api_auth(r: Request):
        return True

    register_metrics_route(app, api_auth=api_auth)
    return TestClient(app, raise_server_exceptions=True)


def test_metrics_endpoint_exposes_persona_override():
    stats = get_persona_override_stats()
    stats.reset()
    stats.record_resolve("conv_override", "telegram", legacy_present=True)
    stats.record_action("bind_conv")
    c = _metrics_client()

    po = c.get("/api/workspace/metrics").json().get("persona_override")
    assert isinstance(po, dict)
    assert po["resolves"] == 1 and po["conv_hits"] == 1
    assert po["legacy_suppressed"] == 1
    assert po["actions"]["bind_conv"] == 1
    # 活水位随计数器一起出（值取决于进程内 pm 状态，只验类型语义）
    assert isinstance(po.get("legacy_debt"), int) and po["legacy_debt"] >= 0

    r = c.get("/api/workspace/metrics?format=prometheus")
    assert r.status_code == 200
    assert "persona_override_resolves_total 1" in r.text
    assert 'persona_override_actions_total{action="bind_conv"} 1' in r.text
    assert "persona_override_legacy_debt " in r.text
    stats.reset()
