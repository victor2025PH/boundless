"""个人号 / 协议 WhatsApp 缺省全自动；官方 Cloud 仍走原档位。模板只属于 Cloud。"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class _Reg:
    def __init__(self, modes):
        self.modes = modes

    def get(self, platform, account_id):
        mode = self.modes.get((platform, account_id))
        if mode is None:
            return None
        return {"mode": mode}


class _Store:
    def __init__(self, explicit=None):
        self.explicit = explicit

    def get_automation_mode_if_set(self, conversation_id):
        return self.explicit

    def get_conversation(self, conversation_id):
        return None


CFG = {"inbox": {"auto_draft": {"automation_mode": "review"}}}


@pytest.fixture
def reg(monkeypatch):
    def _install(modes):
        monkeypatch.setattr(
            "src.integrations.account_registry.get_account_registry",
            lambda: _Reg(modes),
        )
    return _install


def test_protocol_whatsapp_defaults_to_full_auto(reg):
    from src.inbox.automation_mode import resolve_automation_mode
    reg({("whatsapp", "p1"): "protocol", ("whatsapp", "cloud"): "official"})
    assert resolve_automation_mode(_Store(), "whatsapp:p1:chat", CFG) == "auto_ai"
    assert resolve_automation_mode(_Store(), "whatsapp:cloud:chat", CFG) == "review"
    assert resolve_automation_mode(_Store(), "telegram:p1:chat", CFG) == "review"


def test_explicit_review_and_group_stay_review(reg, monkeypatch):
    from src.inbox.automation_mode import resolve_automation_mode
    reg({("whatsapp", "p1"): "device"})
    assert resolve_automation_mode(_Store("review"), "whatsapp:p1:chat", CFG) == "review"
    monkeypatch.setattr(
        "src.inbox.automation_mode.conversation_is_group", lambda _s, _c: True)
    assert resolve_automation_mode(_Store(), "whatsapp:p1:group", CFG) == "review"


def test_account_decision_wins_over_personal_default(reg, monkeypatch):
    from src.inbox.automation_mode import resolve_automation_mode
    reg({("whatsapp", "p1"): "web"})
    monkeypatch.setattr(
        "src.inbox.account_mode_onboarding.decided_mode",
        lambda platform, account_id: "manual",
    )
    assert resolve_automation_mode(_Store(), "whatsapp:p1:chat", CFG) == "manual"


def test_personal_login_does_not_force_review(reg, monkeypatch, tmp_path):
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    import src.inbox.account_channel_gate as gate
    gate._reset_for_tests()
    reg({
        ("whatsapp", "wa"): "protocol",
        ("whatsapp", "cloud"): "official",
    })
    assert gate.note_login("whatsapp", "wa", login_id="1", config={}) is True
    assert gate.login_default_mode("whatsapp", "wa") is None
    assert gate.note_login("whatsapp", "cloud", login_id="1", config={}) is True
    assert gate.login_default_mode("whatsapp", "cloud") == "review"
    assert gate.note_login("messenger", "m", login_id="1", config={}) is True
    assert gate.login_default_mode("messenger", "m") == "review"
    gate._reset_for_tests()


def test_templates_are_documented_as_cloud_optional():
    doc = (ROOT / "docs" / "wa_cloud_window_fallback_templates.md").read_text(encoding="utf-8")
    example = (ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8")
    note = (ROOT / "src" / "web" / "i18n_packs" / "inbox_workspace.py").read_text(encoding="utf-8")
    for text in (doc, example, note):
        assert "可选" in text or "optional" in text
    assert "只属于官方 Cloud" in doc or "只给官方 Cloud" in example
    assert "个人号" in note and "不需要 Meta 模板" in note
