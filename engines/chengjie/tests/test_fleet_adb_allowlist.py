"""Categorized adb allowlist (agent 0.3.18).

Open categories run with no flag. Guarded writes and the USSD dial stay
closed unless the matching flag is passed. Flags do not admit anything else.
"""
from __future__ import annotations

import pytest

from src.fleet.adb_allowlist import CATALOG, DENIED_FOREVER, classify_adb_args
from src.fleet.phone_ops import check_adb_args
from src.fleet.phone_rules import PhoneOpError

_OPEN = {"phone_control", "read_only", "app_launch"}
_CLOSED = {"guarded_write", "experimental_ussd"}


def test_catalog_examples_match_their_category():
    families = set()
    for row in CATALOG:
        found = classify_adb_args(row["example"])
        assert (found.category, found.family) == (row["category"], row["family"]), row["example"]
        assert row["note"] and row["since"] in {"0.3.7", "0.3.18", "0.3.21", "0.3.23", "0.3.26", "0.3.27"}
        families.add(row["family"])
    for required in (
        "dumpsys", "uiautomator", "media_pause", "cmd_connectivity", "cmd_activity", "cmd_package", "settings_get",
        "pm_query", "ping", "connectivity_204", "getprop", "ip", "ifconfig", "wm",
        "am_start_facebook", "ussd_dial", "settings_put", "svc_radio", "reboot",
        "pm_mutate", "force_stop", "facebook_force_stop", "airplane_mode", "portrait_lock", "screen_wake",
    ):
        assert required in families


@pytest.mark.parametrize("row", [row for row in CATALOG if row["category"] in _OPEN],
                         ids=lambda row: row["since"] + "-" + row["family"] + "-" + str(CATALOG.index(row)))
def test_open_forms_need_no_flag(row):
    check_adb_args(row["example"])


@pytest.mark.parametrize("row", [row for row in CATALOG if row["category"] in _CLOSED],
                         ids=lambda row: row["family"] + "-" + str(CATALOG.index(row)))
def test_closed_forms_need_their_flag(row):
    with pytest.raises(PhoneOpError) as err:
        check_adb_args(row["example"])
    assert err.value.code == "adb_args_not_allowed"
    if row["category"] == "guarded_write":
        check_adb_args(row["example"], allow_guarded_writes=True)
        with pytest.raises(PhoneOpError):
            check_adb_args(row["example"], allow_experimental_ussd=True)
    else:
        check_adb_args(row["example"], allow_experimental_ussd=True)
        with pytest.raises(PhoneOpError):
            check_adb_args(row["example"], allow_guarded_writes=True)


@pytest.mark.parametrize("args", DENIED_FOREVER)
def test_flags_do_not_open_unlisted_forms(args):
    with pytest.raises(PhoneOpError) as err:
        check_adb_args(args, allow_guarded_writes=True, allow_experimental_ussd=True,
                       allow_app_restart=True)
    assert err.value.code == "adb_args_not_allowed"


def test_portrait_lock_is_only_rotation_zero():
    check_adb_args(("-s", "S1", "shell", "settings", "put", "system", "user_rotation", "0"))
    check_adb_args(("-s", "S1", "shell", "settings", "put", "system", "accelerometer_rotation", "0"))
    for args in (
        ("-s", "S1", "shell", "settings", "put", "system", "user_rotation", "1"),
        ("-s", "S1", "shell", "settings", "put", "system", "accelerometer_rotation", "1"),
        ("-s", "S1", "shell", "settings", "put", "global", "user_rotation", "0"),
        ("-s", "S1", "shell", "settings", "put", "system", "screen_brightness", "0"),
    ):
        assert classify_adb_args(args).category == "guarded_write"
        with pytest.raises(PhoneOpError):
            check_adb_args(args)


def test_settings_get_namespaces_and_package_queries_are_read_only():
    for args in (
        ("-s", "S1", "shell", "settings", "get", "global", "airplane_mode_on"),
        ("-s", "S1", "shell", "settings", "get", "system", "screen_brightness"),
        ("-s", "S1", "shell", "settings", "get", "secure", "android_id"),
        ("-s", "S1", "shell", "pm", "path", "com.android.settings"),
        ("-s", "S1", "shell", "pm", "list", "packages", "-3"),
        ("-s", "S1", "shell", "cmd", "package", "list", "packages"),
        ("-s", "S1", "shell", "dumpsys", "activity"),
        ("-s", "S1", "shell", "dumpsys", "window"),
        ("-s", "S1", "shell", "ip", "link"),
        ("-s", "S1", "shell", "ifconfig", "-a"),
    ):
        assert classify_adb_args(args).category == "read_only"
        check_adb_args(args)


def test_writes_stay_closed_by_default():
    for args in (
        ("-s", "S1", "shell", "settings", "put", "global", "mobile_data", "1"),
        ("-s", "S1", "shell", "svc", "wifi", "disable"),
        ("-s", "S1", "shell", "svc", "data", "enable"),
        ("-s", "S1", "reboot"),
        ("-s", "S1", "shell", "pm", "uninstall", "com.example.app"),
        ("-s", "S1", "shell", "pm", "clear", "com.example.app"),
        ("-s", "S1", "shell", "am", "force-stop", "com.example.app"),
    ):
        with pytest.raises(PhoneOpError):
            check_adb_args(args)
        assert classify_adb_args(args).category == "guarded_write"


def test_facebook_force_stop_needs_the_app_restart_flag():
    katana = ("-s", "S1", "shell", "am", "force-stop", "com.facebook.katana")
    lite = ("-s", "S1", "shell", "cmd", "activity", "force-stop", "com.facebook.lite")
    other = ("-s", "S1", "shell", "am", "force-stop", "com.example.app")
    for args in (katana, lite):
        assert classify_adb_args(args).category == "app_restart"
        assert classify_adb_args(args).family == "facebook_force_stop"
        with pytest.raises(PhoneOpError):
            check_adb_args(args)
        with pytest.raises(PhoneOpError):
            check_adb_args(args, allow_guarded_writes=True)
        check_adb_args(args, allow_app_restart=True)
    assert classify_adb_args(other).category == "guarded_write"
    with pytest.raises(PhoneOpError):
        check_adb_args(other)
    with pytest.raises(PhoneOpError):
        check_adb_args(other, allow_app_restart=True)
    check_adb_args(other, allow_guarded_writes=True)
