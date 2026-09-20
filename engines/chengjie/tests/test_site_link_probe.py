# -*- coding: utf-8 -*-
"""官网连通性探针门禁（实施86 域A-2②，#17/#51）。"""
from __future__ import annotations

import pytest

from src.utils import site_link_probe as slp


class _CM:
    def __init__(self, hosted: bool = True):
        self.config = {
            "licensing": {
                "hosted_ai": {"enabled": hosted, "site_url": "https://example.test"},
            },
        }


@pytest.fixture(autouse=True)
def _reset():
    slp._reset_for_tests()
    yield
    slp._reset_for_tests()


def test_not_hosted_returns_none():
    assert slp.site_link_snapshot(_CM(hosted=False), probe=lambda s: True) is None


def test_hosted_ok():
    out = slp.site_link_snapshot(_CM(), probe=lambda s: True, now=1000.0)
    assert out == {"reachable": True, "site": "https://example.test", "down_min": 0}


def test_single_blip_not_reported():
    """单次失败不报（防抖动闪横幅）——连续 2 次才转不可达。"""
    t = 1000.0
    out = slp.site_link_snapshot(_CM(), probe=lambda s: False, now=t)
    assert out["reachable"] is True
    # 第二次失败（过 TTL 后再探）→ 转不可达
    t += slp.PROBE_TTL_SEC + 1
    out = slp.site_link_snapshot(_CM(), probe=lambda s: False, now=t)
    assert out["reachable"] is False
    # down_min 从首次失败起算
    t += 600
    out = slp.site_link_snapshot(_CM(), probe=lambda s: False, now=t)
    assert out["reachable"] is False
    assert out["down_min"] >= 10


def test_recovery_immediate():
    t = 1000.0
    slp.site_link_snapshot(_CM(), probe=lambda s: False, now=t)
    t += slp.PROBE_TTL_SEC + 1
    out = slp.site_link_snapshot(_CM(), probe=lambda s: False, now=t)
    assert out["reachable"] is False
    t += slp.PROBE_TTL_SEC + 1
    out = slp.site_link_snapshot(_CM(), probe=lambda s: True, now=t)
    assert out["reachable"] is True
    assert out["down_min"] == 0


def test_ttl_caches_probe():
    calls = {"n": 0}

    def _p(site):
        calls["n"] += 1
        return True

    t = 1000.0
    slp.site_link_snapshot(_CM(), probe=_p, now=t)
    slp.site_link_snapshot(_CM(), probe=_p, now=t + 5)
    slp.site_link_snapshot(_CM(), probe=_p, now=t + 10)
    assert calls["n"] == 1
    slp.site_link_snapshot(_CM(), probe=_p, now=t + slp.PROBE_TTL_SEC + 1)
    assert calls["n"] == 2


def test_http_error_counts_as_reachable(monkeypatch):
    """4xx/5xx＝网络路径通（应用层坏是另一类问题，别混进「连不上」）。"""
    import urllib.error

    def _raise_http(*a, **k):
        raise urllib.error.HTTPError("u", 503, "svc", None, None)

    monkeypatch.setattr("urllib.request.urlopen", _raise_http)
    assert slp.probe_site_once("https://example.test") is True

    def _raise_net(*a, **k):
        raise urllib.error.URLError("down")

    monkeypatch.setattr("urllib.request.urlopen", _raise_net)
    assert slp.probe_site_once("https://example.test") is False
