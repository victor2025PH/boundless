# -*- coding: utf-8 -*-
"""全局默认人设「生效范围」门禁（实施49 P0-3，内测反馈 B3）。

守的核心不变量只有一条：**账号级绑定压制域默认**，因此
``accounts_following`` 必须只数「meta 里一个 persona 绑定都没有」的账号。
自动补绑（``persona_id_auto=True``，账号上线时按
``platform_login.default_persona_id`` 写入）同样算覆盖——解析链不区分它是
谁写的，这里若算成「跟随」，读数就会比现实乐观，正是 B3 那种
「保存成功但聊天里没变」的错觉来源。
"""

import pytest

from src.utils.persona_scope import summarize_default_persona_scope


def _acct(platform="telegram", aid="1", meta=None, label=""):
    return {"platform": platform, "account_id": aid, "label": label,
            "meta": meta or {}}


def test_no_bindings_means_fully_effective():
    s = summarize_default_persona_scope([_acct(aid="1"), _acct(aid="2")])
    assert s["accounts_total"] == 2
    assert s["accounts_following"] == 2
    assert s["accounts_overridden"] == 0
    assert s["fully_effective"] is True
    assert s["reaches_nobody"] is False


def test_account_binding_counts_as_override():
    s = summarize_default_persona_scope([
        _acct(aid="1", meta={"persona_id": "su_wan"}),
        _acct(aid="2"),
    ])
    assert s["accounts_following"] == 1
    assert s["accounts_overridden"] == 1
    assert s["fully_effective"] is False
    assert s["overrides"][0]["profile_id"] == "su_wan"
    assert s["overrides"][0]["auto"] is False


def test_auto_bound_account_is_override_not_following():
    """自动补绑也是覆盖——把它算成跟随＝重演 B3 的乐观读数。"""
    s = summarize_default_persona_scope([
        _acct(aid="1", meta={"persona_id": "lin_xiaoyu", "persona_id_auto": True}),
    ], default_persona_id="lin_xiaoyu")
    assert s["accounts_following"] == 0
    assert s["accounts_overridden"] == 1
    assert s["reaches_nobody"] is True
    assert s["overrides"][0]["auto"] is True
    assert s["auto_bind_profile_id"] == "lin_xiaoyu"


def test_legacy_str_list_meta_form_is_parsed():
    """registry 里 persona_ids 有 ``"['su_wan']"`` 这种历史形态。"""
    s = summarize_default_persona_scope([
        _acct(aid="1", meta={"persona_ids": "['su_wan']"}),
        _acct(aid="2", meta={"persona_ids": ["mizuki"]}),
    ])
    assert s["accounts_overridden"] == 2
    assert {p["profile_id"] for p in s["override_profiles"]} == {"su_wan", "mizuki"}


def test_conv_overrides_only_count_when_feature_enabled():
    """会话覆写总开关关掉时，已写入的覆写键全部失效——不许吓唬客户。"""
    off = summarize_default_persona_scope([_acct()], conv_overrides=5,
                                          conv_override_enabled=False)
    on = summarize_default_persona_scope([_acct()], conv_overrides=5,
                                         conv_override_enabled=True)
    assert off["conv_overrides"] == 0 and off["fully_effective"] is True
    assert on["conv_overrides"] == 5 and on["fully_effective"] is False


def test_chat_bindings_break_full_effectiveness():
    s = summarize_default_persona_scope([_acct()], chat_bindings=3)
    assert s["chat_bindings"] == 3
    assert s["fully_effective"] is False


def test_zero_accounts_is_neither_verdict():
    """还没登录账号：既不是「全量生效」也不是「谁都管不到」，别下结论。"""
    s = summarize_default_persona_scope([])
    assert s["accounts_total"] == 0
    assert s["fully_effective"] is False
    assert s["reaches_nobody"] is False


def test_override_samples_are_capped():
    rows = [_acct(aid=str(i), meta={"persona_id": "su_wan"}) for i in range(20)]
    s = summarize_default_persona_scope(rows, sample_cap=3)
    assert len(s["overrides"]) == 3
    assert s["accounts_overridden"] == 20
    assert s["override_profiles"][0]["count"] == 20


def test_profile_name_resolver_is_used_and_failsafe():
    s = summarize_default_persona_scope(
        [_acct(meta={"persona_id": "su_wan"})],
        profile_name=lambda pid: {"su_wan": "苏婉"}.get(pid, ""),
    )
    assert s["overrides"][0]["profile_name"] == "苏婉"


def test_collect_is_fail_open_when_backends_missing(monkeypatch):
    """取数任何一环炸了都只能说「说不出」，绝不让保存链路 500。"""
    import src.utils.persona_scope as ps
    monkeypatch.setattr(
        "src.integrations.account_registry.get_account_registry",
        lambda: (_ for _ in ()).throw(RuntimeError("no registry")),
    )
    out = ps.collect_default_persona_scope({})
    assert out == {"available": False}


def test_routes_expose_effective_scope():
    """静态接线：GET /api/persona 与保存端点都必须回 effective_scope。

    只回其一 = 客户打开页面看不到覆盖情况、或保存后没有提示，B3 只修一半。
    """
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "src/web/routes/persona_routes.py"
    text = src.read_text(encoding="utf-8")
    assert text.count('"effective_scope"') >= 2
    assert "_default_persona_scope" in text
