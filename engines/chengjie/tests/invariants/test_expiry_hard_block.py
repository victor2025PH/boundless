"""商用不变量 · 到期硬阻断外发（Sprint2）。

固定：licensing.enforce 开启且授权失效(expired/invalid) → 引擎外发中心护栏 send_blocked
返回 license_readonly；enforce 关（默认）→ 恒放行（零破坏）。
"""
import types

import pytest

from src.integrations.shared import send_guard
from src.licensing import gate


class _St:
    def __init__(self, read_only):
        self.read_only = read_only


def test_is_outbound_blocked_maps_to_read_only():
    assert gate.is_outbound_blocked(_St(True)) is True
    assert gate.is_outbound_blocked(_St(False)) is False
    assert gate.is_outbound_blocked(object()) is False  # 无属性 → 放行


def _patch_license(monkeypatch, read_only):
    fake_mgr = types.SimpleNamespace(status=lambda *a, **k: _St(read_only))
    # send_guard 内部 from src.licensing.license_manager import get_license_manager
    import src.licensing.license_manager as lm
    monkeypatch.setattr(lm, "get_license_manager", lambda *a, **k: fake_mgr)


def test_send_blocked_when_license_readonly(monkeypatch):
    _patch_license(monkeypatch, read_only=True)
    blocked, reason = send_guard.send_blocked("telegram", "acc")
    assert blocked is True
    assert reason == "license_readonly"


def test_send_allowed_when_not_readonly(monkeypatch):
    _patch_license(monkeypatch, read_only=False)
    blocked, reason = send_guard.send_blocked("telegram", "acc")
    # 未只读 → 不因 license 拦（可能因其它护栏，但默认全关 → False）
    assert blocked is False
    assert reason == ""


def test_default_enforce_false_never_blocks(monkeypatch):
    """默认 enforce=false：真实 LicenseManager 单例 read_only=False → 不拦（零破坏）。"""
    from src.licensing.license_manager import reset_license_manager, get_license_manager
    reset_license_manager()
    st = get_license_manager().status()
    assert st.read_only is False
    blocked, reason = send_guard.send_blocked("telegram", "acc")
    assert blocked is False
    reset_license_manager()
