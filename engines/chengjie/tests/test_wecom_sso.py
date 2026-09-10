# -*- coding: utf-8 -*-
"""企业微信成员扫码登录（实施97 P1）——纯逻辑门禁。端到端见 test_wechat_e2e_parity.py::test_wecom_member_sso_login。"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from src.integrations import wecom_sso as W


def test_sso_config_falls_back_to_kf_credentials_and_validates():
    cfg = W.sso_config({"wecom_login": {"enabled": True, "agentid": 1000002},
                        "wechat_kf": {"corpid": "wwabc", "secret": "s"}})
    assert cfg["corpid"] == "wwabc" and cfg["secret"] == "s" and cfg["agentid"] == "1000002"
    assert cfg["default_role"] == "agent" and cfg["auto_provision"] is True and cfg["allowed_userids"] == []
    assert W.sso_ready(cfg) == (True, "")
    assert W.sso_ready(W.sso_config({}))[1] == "disabled"
    assert W.sso_ready(W.sso_config({"wecom_login": {"enabled": True}}))[1] == "missing_corpid"
    bad_role = W.sso_config({"wecom_login": {"enabled": True, "default_role": "master", "allowed_userids": "a, b"}})
    assert bad_role["default_role"] == "agent", "不允许经 SSO 直接拿高权限角色"
    assert bad_role["allowed_userids"] == ["a", "b"]


def test_build_login_url_matches_official_shape():
    url = W.build_login_url("wwabc", "1000002", "https://chatx.example.com/login/wecom/callback", "1.n.sig", lang="zh")
    u = urlparse(url)
    assert u.scheme == "https" and u.netloc == "login.work.weixin.qq.com" and u.path == "/wwlogin/sso/login"
    q = parse_qs(u.query)
    assert q["login_type"] == ["CorpApp"] and q["appid"] == ["wwabc"] and q["agentid"] == ["1000002"]
    assert q["redirect_uri"] == ["https://chatx.example.com/login/wecom/callback"] and q["state"] == ["1.n.sig"] and q["lang"] == ["zh"]
    assert "redirect_uri=https%3A%2F%2F" in url, "redirect_uri 按文档 URLEncode"
    assert W.redirect_uri_for("https://chatx.example.com/", "http://127.0.0.1:18898") == "https://chatx.example.com/login/wecom/callback"
    assert W.redirect_uri_for("", "http://127.0.0.1:18898") == "http://127.0.0.1:18898/login/wecom/callback"


def test_state_sign_verify_ttl_and_tamper():
    st = W.sign_state("k", now=1000.0, nonce="n1")
    assert st.startswith("1000.n1.") and W.verify_state("k", st, now=1300.0)
    assert not W.verify_state("k", st, now=1000.0 + W.STATE_TTL_SEC + 1), "10 分钟过期"
    assert not W.verify_state("k", st, now=900.0), "时间倒流不认"
    assert not W.verify_state("other", st, now=1300.0), "别的实例签的不认"
    assert not W.verify_state("k", st[:-1] + ("0" if st[-1] != "0" else "1"), now=1300.0), "篡改签名不认"
    assert not W.verify_state("k", "garbage") and not W.verify_state("k", "")


def test_local_username_and_allowlist():
    assert W.local_username_for("zhang.san-01") == "wecom_zhang.san-01"
    assert W.local_username_for("张三 ") == "wecom___" and W.local_username_for("") == ""   # 非 ASCII 逐字替换成 _
    assert len(W.local_username_for("x" * 100)) == 40
    cfg = W.sso_config({"wecom_login": {"enabled": True, "allowed_userids": ["a"]}})
    assert W.userid_allowed(cfg, "a") and not W.userid_allowed(cfg, "b")
    assert W.userid_allowed(W.sso_config({}), "anyone")
