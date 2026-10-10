# -*- coding: utf-8 -*-
"""Composer 草稿条：默认只露采用和一条原因。停联 / 要钱留在外面。"""
from __future__ import annotations

from pathlib import Path

from src.inbox.cdraft_fold import sticky_alert

_ROOT = Path(__file__).resolve().parents[1]
_TPL = (_ROOT / "src" / "web" / "templates" / "unified_inbox.html").read_text(encoding="utf-8")
_CSS = (_ROOT / "src" / "web" / "static" / "workspace" / "unified-inbox.css").read_text(encoding="utf-8")


def test_sticky_stop_beats_money_and_ignores_mention():
    assert sticky_alert(["stop_contact", "request:money"]) == "stop"
    assert sticky_alert(["money_mention", "first_contact"]) == ""
    assert sticky_alert(["request:money"]) == "money"
    assert sticky_alert(["credential_or_payment_request"]) == "money"
    assert sticky_alert('["commitment:money"]') == "money"
    assert sticky_alert(["stop_contact:user"]) == "stop"
    assert sticky_alert(None) == ""
    assert sticky_alert(["money"]) == "money"


def test_template_folds_risk_lang_kb_memory_under_view():
    head, _, rest = _TPL.partition('id="cdraft-bar"')
    bar, _, _after = rest.partition('id="identity-bar"')
    hd, _, more = bar.partition('id="cdraft-more"')
    assert "inbox.cdraft.accept" in hd
    assert 'id="cdraft-reason"' in hd
    assert 'id="cdraft-sticky"' in hd
    assert 'id="cdraft-risk"' not in hd
    assert 'id="lp-cdraft"' not in hd
    assert 'id="cdraft-kbd"' not in hd
    assert 'id="cdraft-mem"' not in hd
    assert 'id="cdraft-risk"' in more
    assert 'id="lp-cdraft"' in more
    assert 'id="cdraft-kbd"' in more
    assert 'id="cdraft-mem"' in more
    assert "inbox.cdraft.panel" in more
    assert "inbox.cdraft.dismiss" in more
    assert "d.sticky_alert" in _TPL
    assert "cdraft-open" in _TPL
    assert ".cdraft-more{display:none;}" in _CSS
    assert ".cdraft-bar.cdraft-open .cdraft-more{display:block;}" in _CSS
    assert ".cdraft-bar.slim .cdraft-more{display:none;}" in _CSS
    assert ".cdraft-bar.slim .mem-chips{display:none;}" in _CSS
