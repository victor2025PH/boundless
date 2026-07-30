# -*- coding: utf-8 -*-
"""换绑漏斗失败侧（P1，2026-07-31）：persona_override_stats.record_fail + 路由埋点。

市场侧的问题是「多少人想用会话级换绑但被挡住」——此前只有成功侧计数
（record_action），被拒的尝试消失得无影无踪。现在：
- 业务层拒绝（路由 4xx）→ record_fail(op, reason)，随 metrics.persona_override
  的 fails/fails_total 出看板 + Prometheus persona_override_fails_total；
- 安全层拒绝（CSRF 中间件，不进路由）→ csrf_stats 另计，ops 卡交叉引用。
"""

from src.ai.persona_override_stats import get_persona_override_stats


def setup_function(_fn):
    get_persona_override_stats().reset()


# ── 纯计数语义 ────────────────────────────────────────────────────────────

def test_record_fail_counts_by_op_and_reason():
    st = get_persona_override_stats()
    st.record_fail("bind_conv", "profile_missing")
    st.record_fail("bind_conv", "profile_missing")
    st.record_fail("account_set", "badref")
    d = st.dump()
    assert d["fails"] == {"bind_conv:profile_missing": 2, "account_set:badref": 1}
    assert d["fails_total"] == 3


def test_record_fail_whitelists_inputs():
    """op 白名单外整条丢弃；reason 白名单外归 error（宁可粗归类不丢计数）。"""
    st = get_persona_override_stats()
    st.record_fail("hack_op", "whatever")          # op 非法 → 不计
    st.record_fail("bind_conv", "weird_reason")    # reason 非法 → error
    d = st.dump()
    assert d["fails"] == {"bind_conv:error": 1}
    assert d["fails_total"] == 1


def test_fails_in_prometheus_dump():
    st = get_persona_override_stats()
    st.record_fail("unbind_conv", "badref")
    prom = st.dump_prom()
    assert 'persona_override_fails_total{op="unbind_conv",reason="badref"} 1' in prom


def test_reset_clears_fails():
    st = get_persona_override_stats()
    st.record_fail("bind_conv", "disabled")
    st.reset()
    d = st.dump()
    assert d["fails_total"] == 0 and d["fails"] == {}


# ── 路由埋点端到端（auth_client 带 Bearer，穿透 CSRF 直达业务层） ────────────

def test_unbind_badref_recorded(auth_client):
    st = get_persona_override_stats()
    st.reset()
    r = auth_client.post("/api/persona/unbind",
                         json={"scope": "conversation", "conversation_id": "no-parts"})
    assert r.status_code == 400
    assert st.dump()["fails"].get("unbind_conv:badref") == 1


def test_account_set_badref_recorded(auth_client):
    st = get_persona_override_stats()
    st.reset()
    r = auth_client.post("/api/persona/account-persona", json={})
    assert r.status_code == 400
    assert st.dump()["fails"].get("account_set:badref") == 1


def test_bind_conv_rejection_recorded(auth_client):
    """conv 换绑被业务层拒绝（开关关=disabled / 幽灵人设=profile_missing，
    取决于 fixture 配置，两者都属「被挡住的尝试」）必须落进漏斗。"""
    st = get_persona_override_stats()
    st.reset()
    r = auth_client.post("/api/persona/bind",
                         json={"scope": "conversation",
                               "conversation_id": "telegram:acct1:chat1",
                               "profile_id": "ghost_profile_never_exists"})
    assert r.status_code in (400, 404)
    fails = st.dump()["fails"]
    assert (fails.get("bind_conv:disabled", 0)
            + fails.get("bind_conv:profile_missing", 0)) == 1, fails
