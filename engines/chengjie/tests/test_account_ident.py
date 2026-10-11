# -*- coding: utf-8 -*-
"""账号菜单身份行（P1-4，2026-09-29）门禁。

账号菜单一眼能看见「这个号正在做客服，知识库必查，档位半自动」。字段由
``fill_account_ident`` 写进 platform_status，前端只做 i18n 展示。
"""
from src.web.routes.unified_inbox_read_routes import fill_account_ident


def test_fill_support_persona_must_kb():
    v = {"platform": "telegram", "account_id": "q567"}
    fill_account_ident(
        v, cfg={"business_domain": "companion"},
        persona={"id": "q567", "role": "售后支持专员", "tags": ["客服"]},
        unselected=False)
    assert v["persona_kind"] == "support"
    assert v["ident_kb"] == "must"
    assert v["ident_mode"] == "global"


def test_fill_unselected_hides_kind():
    v = {"platform": "telegram", "account_id": "new"}
    fill_account_ident(
        v, cfg={"business_domain": "companion"},
        persona={"kind": "support", "role": "售后支持专员"},
        unselected=True)
    assert v["persona_kind"] == ""
    assert v["ident_kb"] == "unselected"


def test_fill_pending_login_overrides_mode():
    v = {"platform": "telegram", "account_id": "a",
         "gate": {"login_pending_confirm": True}}
    fill_account_ident(v, cfg={}, persona={"kind": "companion"}, unselected=False)
    assert v["ident_mode"] == "pending"
    assert v["ident_kb"] == "skip"


def test_inbox_template_uses_static_ident_i18n_keys():
    """扫描器只抽 T('字面量')；拼接前缀会被当成缺译 key（inbox.acct.kind_）。"""
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
            / "unified_inbox.html").read_text(encoding="utf-8")
    assert "function _acctIdentBits" in html
    assert "inbox.acct.kind_support" in html
    assert "T('inbox.acct.kind_'+kind)" not in html
    assert "T('inbox.acct.kb_'+kb)" not in html
    assert "T('inbox.acct.mode_'+mode)" not in html
