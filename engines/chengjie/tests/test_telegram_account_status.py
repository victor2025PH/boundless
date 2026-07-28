"""Gate tests for the Telegram account-info three-state ``status`` field.

Covers:
- ``derive_account_status`` pure-function truth table
  (connected / connecting / offline x2);
- static wiring assertion: the account-info endpoint body actually calls
  ``derive_account_status(`` and exposes ``stats_source`` (guards against
  the pure function existing but never being wired in).
"""
from pathlib import Path

from src.web.routes.telegram_routes import derive_account_status

_ROUTE_FILE = (
    Path(__file__).resolve().parents[1]
    / "src" / "web" / "routes" / "telegram_routes.py"
)


def test_running_with_identity_is_connected():
    assert derive_account_status(True, True) == "connected"


def test_running_without_identity_is_connecting():
    assert derive_account_status(True, False) == "connecting"


def test_not_running_without_identity_is_offline():
    assert derive_account_status(False, False) == "offline"


def test_not_running_with_stale_identity_is_offline():
    assert derive_account_status(False, True) == "offline"


def test_account_info_endpoint_wires_status_and_stats_source():
    src = _ROUTE_FILE.read_text(encoding="utf-8")
    marker = '@app.get("/api/telegram/account-info")'
    start = src.index(marker)
    # Endpoint body ends at the next route decorator.
    end = src.index("@app.", start + len(marker))
    body = src[start:end]
    assert "derive_account_status(" in body, (
        "account-info endpoint no longer calls derive_account_status()"
    )
    assert "stats_source" in body, (
        "account-info endpoint no longer sets stats_source"
    )
