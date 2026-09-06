# -*- coding: utf-8 -*-
"""L-2 D（#204，P2）：人设卡片「服务中 / 未启用」胶囊与账号侧绑定同源（2026-09-06）。

事故（QRHNGM，skuio）：steven 已绑定 Telegram 账号（「应用到 → 账号默认」写的是账号注册表
``meta.persona_ids``），卡片仍显「未启用」；筛选条「已绑定(1)/未绑定(13)」只有 Mizuki 算服务中。
根因：``list_profiles_summary`` 的 ``binding_count`` 只数会话级绑定（``_chat_personas`` +
``_chat_bindings``），不含账号注册表 / config 各平台 ``persona_ids``。

钉住：
- ``collect_binding_usage_map`` 四源一次扫完，与 ``collect_binding_usage`` 逐 pid 结果一致；
- ``list_profiles_summary(full_config)``：只有账号默认绑定的人设 → binding_accounts=1、
  binding_chats=0、binding_count=1（旧筛选 >0 判据仍成立）；只有会话绑定 → 0/1；都无 → 0/0；
- 前端胶囊文案「服务中 a 个账号 · c 个会话」/ 皆 0 → 「未绑定」（不是「未启用」）；
- GET /api/personas/profiles 把 config 喂进 summary（config 平台级 persona_ids 也算）。
"""
from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest

import src.companion.persona_stock as ps
from src.utils.persona_manager import PersonaManager

_JS = Path(__file__).resolve().parents[1] / "src" / "web" / "static" / "js" / "persona_studio_core.js"
_I18N = Path(__file__).resolve().parents[1] / "src" / "web" / "i18n_packs" / "persona_studio.py"


@pytest.fixture
def fake_registry(monkeypatch):
    class _Reg:
        def list(self, plat):
            if plat == "telegram":
                return [{"account_id": "tg_login_46b1a42d292e", "meta": {"persona_ids": ["steven"]}},
                        {"account_id": "tg_removed", "status": "removed", "meta": {"persona_ids": ["steven"]}}]
            if plat == "whatsapp":
                return [{"account_id": "wa1", "meta": {"persona_ids": ["mizuki", "steven"]}}]
            return []

    fake_mod = types.SimpleNamespace(
        get_account_registry=lambda: _Reg(),
        parse_persona_ids=lambda meta: (
            [meta["persona_ids"]] if isinstance((meta or {}).get("persona_ids"), str)
            else list((meta or {}).get("persona_ids") or [])),
    )
    monkeypatch.setitem(sys.modules, "src.integrations.account_registry", fake_mod)
    return fake_mod


class _PM:
    _domain_persona = {"id": "mizuki"}

    def get_all_chat_bindings(self):
        return {"c1": {"id": "mizuki"}, "c2": {"id": "x", "_profile_ref": "mizuki"}, "c3": {"id": "lonely"}}


def test_usage_map_matches_per_pid(fake_registry):
    cfg = {"messenger_rpa": {"accounts": [{"account_id": "ms1", "persona_ids": ["steven"]}]}}
    m = ps.collect_binding_usage_map(_PM(), cfg)
    assert m["steven"]["account_count"] == 3        # tg + wa1 + ms1（removed 行不算）
    assert m["steven"]["chat_count"] == 0 and m["steven"]["is_default"] is False
    assert m["mizuki"] == {"account_count": 1, "chat_count": 2, "is_default": True,
                           "accounts": [["whatsapp", "wa1"]]}
    assert m["lonely"]["chat_count"] == 1 and m["lonely"]["account_count"] == 0
    assert "nobody" not in m
    for pid in ("steven", "mizuki", "lonely", "nobody"):
        assert ps.collect_binding_usage(pid, _PM(), cfg) == (m.get(pid) or ps._empty_usage()), pid


def test_summary_counts_account_default_binding(fake_registry):
    """QRHNGM 复现：只有账号默认绑定的 steven → 胶囊绿 1 个账号 · 0 个会话。"""
    PersonaManager.reset()
    try:
        pm = PersonaManager.get_instance()
        for pid in ("steven", "mizuki", "lonely", "nobody"):
            pm.upsert_profile(pid, {"id": pid, "name": pid}, _track_history=False)
        pm._chat_bindings["chat-1"] = "lonely"          # 只有会话级绑定
        rows = {s["id"]: s for s in pm.list_profiles_summary({})}
        st = rows["steven"]
        assert (st["binding_accounts"], st["binding_chats"]) == (2, 0)   # tg + wa1
        assert st["binding_count"] == 2 and st["binding_default"] is False
        assert rows["lonely"]["binding_accounts"] == 0 and rows["lonely"]["binding_chats"] == 1
        assert rows["lonely"]["binding_count"] == 1
        assert rows["nobody"]["binding_count"] == 0
        assert (rows["nobody"]["binding_accounts"], rows["nobody"]["binding_chats"]) == (0, 0)
        # config 平台级 persona_ids 也算账号绑定
        rows2 = {s["id"]: s for s in pm.list_profiles_summary(
            {"telegram": {"persona_ids": ["nobody"]}})}
        assert rows2["nobody"]["binding_accounts"] == 1 and rows2["nobody"]["binding_count"] == 1
    finally:
        PersonaManager.reset()


def test_summary_falls_back_to_chat_counts_when_usage_fails(monkeypatch):
    PersonaManager.reset()
    try:
        pm = PersonaManager.get_instance()
        pm.upsert_profile("p", {"id": "p", "name": "p"}, _track_history=False)
        pm._chat_bindings["c"] = "p"

        def _boom(*a, **k):
            raise RuntimeError("registry down")

        monkeypatch.setattr(ps, "collect_binding_usage_map", _boom)
        rows = {s["id"]: s for s in pm.list_profiles_summary()}
        assert rows["p"]["binding_count"] == 1 and rows["p"]["binding_chats"] == 1
    finally:
        PersonaManager.reset()


def test_pill_copy_uses_split_and_not_bound():
    js = _JS.read_text(encoding="utf-8")
    seg = js[js.index("var _bAcc = Number(p.binding_accounts)"):js.index("var usagePill")]
    assert "psn_serving_split" in seg and "binding_accounts" in seg and "binding_chats" in seg
    assert "psn_not_bound" in seg and "psn_not_live'" not in seg, "皆 0 文案是「未绑定」不是「未启用」"
    i18n = _I18N.read_text(encoding="utf-8")
    for key in ("psn_serving_split", "psn_serving_split_t", "psn_not_bound", "psn_not_bound_t"):
        assert len(re.findall(r'"%s":' % key, i18n)) == 2, key   # zh + en
    assert '"psn_serving_split": "服务中 {a} 个账号 · {c} 个会话"' in i18n
    assert '"psn_not_bound": "未绑定"' in i18n
