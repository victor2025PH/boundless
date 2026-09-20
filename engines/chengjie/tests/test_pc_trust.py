# -*- coding: utf-8 -*-
"""小智「电脑操控」信任档门禁（实施91 P2-C，2026-08-31）。

守信任档的时间窗安全属性：授信计时、过期自动失效、revoke 立即收回；
默认全不受信（最保守）。纯进程内逻辑，非 Windows 也跑。
"""
from __future__ import annotations

import time

import pytest

from src.assistant import pc_trust as t


@pytest.fixture(autouse=True)
def _clean():
    t._reset_for_test()
    yield
    t._reset_for_test()


def test_untrusted_by_default():
    assert t.is_trusted("zhuji") is False
    assert t.expires_at("zhuji") == 0.0


def test_grant_and_trust():
    exp = t.grant("zhuji", 100)
    assert exp > time.time()
    assert t.is_trusted("zhuji") is True
    assert t.expires_at("zhuji") > time.time()


def test_grant_rejects_bad_input():
    assert t.grant("", 100) == 0.0
    assert t.grant("zhuji", 0) == 0.0
    assert t.grant("zhuji", -5) == 0.0
    assert t.is_trusted("zhuji") is False


def test_expiry_auto_clears():
    t.grant("zhuji", 100)
    t._TRUST["zhuji"] = time.time() - 1     # 强制过期
    assert t.is_trusted("zhuji") is False
    assert "zhuji" not in t._TRUST          # 惰性自清
    assert t.expires_at("zhuji") == 0.0


def test_revoke_is_immediate():
    t.grant("zhuji", 100)
    assert t.revoke("zhuji") is True
    assert t.is_trusted("zhuji") is False
    assert t.revoke("zhuji") is False       # 已无=False


def test_list_trusted_excludes_expired():
    t.grant("a", 100)
    t.grant("b", 100)
    t._TRUST["b"] = time.time() - 1
    rows = t.list_trusted()
    assert [r["machine"] for r in rows] == ["a"]
    assert "b" not in t._TRUST              # 列举顺带清过期


def test_grant_is_renew():
    e1 = t.grant("zhuji", 10)
    e2 = t.grant("zhuji", 1000)             # 续期覆盖
    assert e2 > e1
