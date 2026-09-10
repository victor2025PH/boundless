# -*- coding: utf-8 -*-
"""实施97 · 接入引导页（微信客服五步 / 个人微信 PC 副驾六步）——纯逻辑门禁。

端到端（真 app + 假企微）见 ``test_wechat_e2e_parity.py::test_wechat_kf_connect_guide_backend``。
"""
from __future__ import annotations

import pytest

from src.integrations import wechat_kf_setup as S
from src.integrations.wechat_pc.env_check import parse_version, version_ok
from src.web.routes.wechat_pc_setup_routes import policy_view, validate_policy_change


def test_describe_kf_errcode_human_readable():
    ip = S.describe_kf_errcode(60020, egress_ip="1.2.3.4")
    assert ip["kind"] == "ip_not_allowed" and "1.2.3.4" in ip["fix"] and "可信 IP" in ip["fix"]
    assert S.describe_kf_errcode(40013)["kind"] == "invalid_corpid"
    assert S.describe_kf_errcode("40014")["kind"] == "invalid_secret"
    assert S.describe_kf_errcode(95014)["kind"] == "servicer_not_active"
    assert S.describe_kf_errcode(0)["kind"] == "ok"
    assert S.describe_kf_errcode(-1)["kind"] == "network"
    unk = S.describe_kf_errcode(123456)
    assert unk["kind"] == "api_error" and "123456" in unk["problem"]
    assert S.describe_kf_errcode("x")["kind"] == "network"


def test_parse_ip_text_accepts_public_ipv4_only():
    assert S.parse_ip_text("8.8.4.4\n") == "8.8.4.4"
    assert S.parse_ip_text('{"ip": "1.1.1.1"}', "json:ip") == "1.1.1.1"
    assert S.parse_ip_text("192.168.0.149") == "", "私网地址不是出站公网 IP"
    assert S.parse_ip_text("127.0.0.1") == "" and S.parse_ip_text("garbage") == "" and S.parse_ip_text("", "json:ip") == ""
    assert S.parse_ip_text("not json", "json:ip") == ""


def test_fetch_egress_ip_rotates_providers_and_caches():
    S.reset_egress_cache()
    calls = []

    def http_get(url, timeout):
        calls.append(url)
        if "ipify" in url:
            raise RuntimeError("timeout")
        if "ifconfig" in url:
            return "10.0.0.1"          # 私网 → 不认
        return "8.8.8.8"
    r = S.fetch_egress_ip(http_get, now=1000.0)
    assert r["ok"] and r["ip"] == "8.8.8.8" and "ip.sb" in r["source"] and len(calls) == 3
    r2 = S.fetch_egress_ip(lambda u, t: "0.0.0.0", now=1030.0)
    assert r2["cached"] is True and r2["ip"] == "8.8.8.8", "60 秒内走缓存，不再打网络"
    S.reset_egress_cache()
    r3 = S.fetch_egress_ip(lambda u, t: "", now=2000.0)
    assert not r3["ok"] and r3["ip"] == "" and r3["errors"]


def test_assemble_checks_shapes():
    ok_tok = {"ok": True, "errcode": 0}
    lst = {"ok": True, "data": {"account_list": [
        {"open_kfid": "wkA", "name": "客服A", "manage_privilege": True},
        {"open_kfid": "wkB", "name": "客服B", "manage_privilege": False}]}}
    out = S.assemble_checks(ok_tok, lst, egress_ip="1.1.1.1")
    assert out["ok"] and [c["id"] for c in out["checks"]] == ["credentials", "kf_permission", "manageable_accounts"]
    assert out["checks"][2]["count"] == 1 and [a["open_kfid"] for a in out["accounts"]] == ["wkA", "wkB"]
    # 凭证错：只有一项且带人话
    bad = S.assemble_checks({"ok": False, "errcode": 40013}, None)
    assert not bad["ok"] and len(bad["checks"]) == 1 and bad["checks"][0]["problem"] == "CorpID 不正确"
    # 令牌对但列表被 60020 拦：第二项失败，怎么办里带 IP
    ipfail = S.assemble_checks(ok_tok, {"ok": False, "errcode": 60020}, egress_ip="9.9.9.9")
    assert not ipfail["ok"] and ipfail["checks"][1]["ok"] is False and "9.9.9.9" in ipfail["checks"][1]["fix"]
    # 有账号但都不可管理 / 完全没账号：第三项失败但文案不同
    nomanage = S.assemble_checks(ok_tok, {"ok": True, "data": {"account_list": [{"open_kfid": "wkB", "manage_privilege": False}]}})
    assert nomanage["checks"][2]["ok"] is False and "勾选" in nomanage["checks"][2]["fix"]
    empty = S.assemble_checks(ok_tok, {"ok": True, "data": {"account_list": []}})
    assert empty["checks"][2]["ok"] is False and "新建" in empty["checks"][2]["fix"]


def test_normalize_kf_accounts_marks_bound():
    rows = S.normalize_kf_accounts([{"open_kfid": "wkA", "name": " 客服A ", "avatar": "https://a"}, {"open_kfid": ""}, "junk"],
                                   bound_ids={"wkA"})
    assert rows == [{"open_kfid": "wkA", "name": "客服A", "avatar": "https://a", "manage_privilege": False, "bound": True}]


def test_pc_version_parse_and_gate():
    assert parse_version("4.1.12.55") == (4, 1, 12, 55) and version_ok("4.1.12.55")
    assert not version_ok("3.9.12.51") and not version_ok("") and parse_version("abc") == ()
    assert version_ok("4.0")


def test_pc_policy_view_and_validation():
    cur = policy_view({"platform_login": {"wechat_pc": {"tier": "semi", "work_hours": [0, 24]}}})
    assert cur == {"tier": "semi", "work_hours": [0, 24], "risk_ack": False, "risk_ack_by": "", "risk_ack_at": 0.0}
    assert policy_view({})["tier"] == "copilot" and policy_view({})["work_hours"] == [9, 22]
    assert validate_policy_change({"tier": "semi"}, cur) == {"tier": "semi"}
    # 全自动必须知情同意（服务端强制，不信任前端）
    with pytest.raises(ValueError, match="risk_ack_required"):
        validate_policy_change({"tier": "auto_reply"}, cur)
    assert validate_policy_change({"tier": "auto_reply", "risk_ack": True}, cur) == {"tier": "auto_reply", "risk_ack": True}
    # 此前已确认过 → 不必重复勾选
    assert validate_policy_change({"tier": "auto_reply"}, {**cur, "risk_ack": True}) == {"tier": "auto_reply"}
    with pytest.raises(ValueError, match="bad_tier"):
        validate_policy_change({"tier": "root"}, cur)
    with pytest.raises(ValueError, match="bad_work_hours"):
        validate_policy_change({"tier": "semi", "work_hours": [22, 9]}, cur)
    assert validate_policy_change({"tier": "copilot", "work_hours": [8, 20]}, cur) == {"tier": "copilot", "work_hours": [8, 20]}

