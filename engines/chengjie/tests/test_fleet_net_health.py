"""Read-only net_health parsing and the operator-alert line (agent 0.3.18)."""
from __future__ import annotations

import json

from src.fleet import agent as agent_mod
from src.fleet import operator_alert as oa
from src.fleet.net_health import (
    REMAINING_NOTE, probe_phone, public_row, run_net_health, sanitize_net_health_result,
)
from src.fleet.phone_ops import check_adb_args
from src.fleet.phones import PROTECTED_SERIALS
from src.fleet.protocol import (
    LEGACY_ALLOWED_KINDS, REMOTE_PHONE_KINDS, STATUS_DONE, STATUS_FAILED, STATUS_REJECTED,
    TASK_KINDS, TASK_NET_HEALTH,
)

SERIAL = "ZZPHONE99"
OTHER = "OTHERPHONE1"
PROTECTED = PROTECTED_SERIALS[0]

WIFI_CONN = """
Active default network: 100
NetworkAgentInfo{network{100} handle=1}
  NetworkCapabilities: [ Transports: WIFI Capabilities: INTERNET&VALIDATED SSID: "SecretNet" serialno=ZZPHONE99 ]
"""
NONE_CONN = """
Active default network: none
NetworkAgentInfo{network{4}}
  NetworkCapabilities: [ Transports: CELLULAR Capabilities: INTERNET ]
"""
MOBILE_CONN = """
Active default network: 12
NetworkAgentInfo{network{12}}
  NetworkCapabilities: [ Transports: CELLULAR Capabilities: INTERNET&VALIDATED ]
"""
TELE_READY = "mSimState=READY\nmAirplaneMode=false\nCellSignalStrengthLte: rssi=-70 level=3\n"
TELE_WEAK = "mSimState=READY\nmAirplaneMode=false\nCellSignalStrengthLte: rssi=-100 level=1 gsmSignalStrength=4\n"
TELE_ABSENT = "mSimState=ABSENT\nmAirplaneMode=false\n"
TELE_AIR = "mSimState=READY\nmAirplaneMode=true\nCellSignalStrengthLte: level=4\n"
STATS = """
ident=[{type=WIFI, subType=0}]
  rb=999 tb=999
ident=[{type=MOBILE, subType=0}]
  rb=1000 tb=250
iface=wlan0
  rxBytes=8000 txBytes=8000
iface=rmnet0
  rxBytes=5 txBytes=6
"""
CURL_OK = ("204 0.042\n", 0)
CURL_FAIL = ("curl: (7) Failed to connect\n", 7)
PING_LOSS = ("1 packets transmitted, 0 packets received, 100% packet loss\n", 1)
PING_OK = ("64 bytes from 8.8.8.8: icmp_seq=1 ttl=117 time=12.3 ms\n1 packets transmitted, 1 received, 0% packet loss\n", 0)
DNS_FAIL = ("ping: unknown host connectivitycheck.gstatic.com\n", 2)
PM_YES = ("package:/data/app/com.facebook.katana-1/base.apk\n", 0)
PM_NO = ("", 1)
ACT_APP = "mResumedActivity: ActivityRecord{abc u0 com.facebook.katana/.FbMainTabActivity t9}\n"
ACT_LOGIN = (
    "mResumedActivity: ActivityRecord{abc u0 com.facebook.katana/.LoginActivity t9}\n"
    "mCurrentFocus=Window{abc u0 com.facebook.katana/com.facebook.katana.LoginActivity}\n"
)
ACT_LOGIN_ONCE = "mResumedActivity: ActivityRecord{abc u0 com.facebook.katana/.LoginActivity t9}\n"
ACT_FEED_INTENT = (
    "mResumedActivity: ActivityRecord{abc u0 com.facebook.katana/.FbMainTabActivity t9}"
    " Intent { cmp=com.facebook.katana/.LoginActivity }\n"
)
ACT_HOME = "mResumedActivity: ActivityRecord{abc u0 com.android.launcher3/.Launcher t2}\n"
WM = ("Physical size: 1080x1920\n", 0)
_CURL = ("curl", "-sS", "-o", "/dev/null", "-w", "%{http_code} %{time_total}", "--max-time", "8")


class _Col:
    def __init__(self, phones, err=""):
        self.phones = phones
        self.err = err
        self.manage_server = False
        self.adb_path = ""

    def collect(self):
        return self.phones, self.err


def _table(extra=None):
    base = {
        ("dumpsys", "connectivity"): (WIFI_CONN, 0),
        ("dumpsys", "telephony.registry"): (TELE_READY, 0),
        ("dumpsys", "netstats"): (STATS, 0),
        ("settings", "get", "global", "mobile_data"): ("1\n", 0),
        ("settings", "get", "global", "airplane_mode_on"): ("0\n", 0),
        _CURL + ("http://connectivitycheck.gstatic.com/generate_204",): CURL_OK,
        ("pm", "path", "com.facebook.katana"): PM_YES,
        ("pm", "path", "com.facebook.lite"): PM_NO,
        ("dumpsys", "activity", "activities"): (ACT_APP, 0),
        ("wm", "size"): WM,
    }
    if extra:
        base.update(extra)
    return base


def _run(table, phones=None, cfg=None, target=None, payload=None, live_stream=False):
    calls = []

    def factory(_serial):
        def invoke(args, timeout=12):
            calls.append(tuple(args))
            return table.get(tuple(args), ("", 1))
        return invoke

    col = _Col(phones if phones is not None else [{"serial": SERIAL, "state": "device", "model": "M"}])
    status, result, detail = run_net_health(
        col, cfg if cfg is not None else {"wallpaper_map": {SERIAL: "07"}},
        target or {}, payload or {}, live_stream=live_stream, shell_for=factory,
    )
    return status, result, detail, calls


def _blob(result):
    return json.dumps(result, ensure_ascii=False)


def test_wifi_ok_phone():
    status, result, detail, calls = _run(_table())
    assert (status, detail) == (STATUS_DONE, "ok")
    row = result["phones"][0]
    assert row["wallpaper_no"] == "07"
    assert row["reachable"] is True
    assert row["reach_via"] == "generate_204"
    assert row["latency_ms"] == 42
    assert row["transport"] == "wifi"
    assert row["sim"] == "present"
    assert row["mobile_rx_bytes"] == 1000
    assert row["mobile_tx_bytes"] == 250
    assert row["mobile_bytes"] == 1250
    assert row["fb_installed"] is True
    assert row["fb_screen"] == "app"
    assert row["screen"] == "1080x1920"
    assert row["status_zh"] == "网络✓ wifi"
    assert row["status_en"] == "net ok wifi"
    assert row["alert"] is False
    assert row["remaining_data"]["available"] is False
    assert "not available by default" in row["remaining_data"]["note"]
    assert "SecretNet" not in _blob(result)
    assert SERIAL not in _blob(result)
    assert "serial" not in row
    joined = " ".join(" ".join(cmd) for cmd in calls)
    assert "settings put" not in joined and "force-stop" not in joined
    assert not any(cmd[:1] == ("svc",) or cmd[:2] == ("am", "start") for cmd in calls)
    assert ("getprop", "ro.serialno") not in calls
    assert ("settings", "get", "secure", "android_id") not in calls
    for cmd in calls:
        check_adb_args(("-s", SERIAL, "shell", *cmd))


def test_no_network_no_data_phone():
    table = _table({
        ("dumpsys", "connectivity"): (NONE_CONN, 0),
        ("dumpsys", "telephony.registry"): (TELE_ABSENT, 0),
        ("settings", "get", "global", "mobile_data"): ("0\n", 0),
        _CURL + ("http://connectivitycheck.gstatic.com/generate_204",): CURL_FAIL,
        _CURL + ("http://clients3.google.com/generate_204",): CURL_FAIL,
        ("ping", "-c", "1", "-W", "3", "8.8.8.8"): PING_LOSS,
        ("ping", "-c", "1", "-W", "3", "connectivitycheck.gstatic.com"): DNS_FAIL,
        ("pm", "path", "com.facebook.katana"): PM_NO,
        ("dumpsys", "activity", "activities"): (ACT_HOME, 0),
    })
    status, result, _detail, _calls = _run(table)
    row = result["phones"][0]
    assert status == STATUS_DONE
    assert row["reachable"] is False
    assert row["transport"] == "none"
    assert row["sim"] == "absent"
    assert row["mobile_data"] is False
    assert row["dns_ok"] is False
    assert row["status_zh"] == "网络✗ 无流量"
    assert row["status_en"] == "net down no data"
    assert row["alert"] is True
    assert row["remaining_data"]["available"] is False
    assert result["remaining_data_note"] == REMAINING_NOTE
    assert row["mobile_bytes"] == 1250  # counters still read; they are not the remaining balance
    assert SERIAL not in _blob(result)


def test_mobile_weak_signal():
    table = _table({
        ("dumpsys", "connectivity"): (MOBILE_CONN, 0),
        ("dumpsys", "telephony.registry"): (TELE_WEAK, 0),
    })
    _status, result, _detail, _calls = _run(table)
    row = result["phones"][0]
    assert row["transport"] == "mobile"
    assert row["signal"] == "weak"
    assert row["signal_level"] == 1
    assert row["reachable"] is True
    assert row["status_zh"] == "移动数据弱信号"
    assert row["status_en"] == "mobile weak signal"
    assert row["alert"] is True


def test_airplane_and_logged_out_and_not_installed():
    air = _table({
        ("dumpsys", "telephony.registry"): (TELE_AIR, 0),
        ("settings", "get", "global", "airplane_mode_on"): ("1\n", 0),
    })
    row = _run(air)[1]["phones"][0]
    assert row["airplane"] is True
    assert row["status_zh"] == "网络✗ 飞行模式"

    login = _table({("dumpsys", "activity", "activities"): (ACT_LOGIN, 0)})
    row = _run(login)[1]["phones"][0]
    assert row["fb_screen"] == "login"
    assert row["fb_confirm"] == "login"
    assert row["status_zh"] == "Facebook未登录"
    assert row["reachable"] is True and row["transport"] == "wifi"

    missing = _table({("pm", "path", "com.facebook.katana"): PM_NO, ("pm", "path", "com.facebook.lite"): PM_NO})
    row = _run(missing)[1]["phones"][0]
    assert row["fb_installed"] is False
    assert row["status_zh"] == "Facebook未安装"


def test_feed_intent_and_single_login_are_not_logged_out():
    feed = _table({("dumpsys", "activity", "activities"): (ACT_FEED_INTENT, 0)})
    row = _run(feed)[1]["phones"][0]
    assert row["fb_screen"] == "app"
    assert row["fb_confirm"] != "login"
    assert row["status_zh"] != "Facebook未登录"
    assert "LoginActivity" not in row["status_zh"]

    once = _table({("dumpsys", "activity", "activities"): (ACT_LOGIN_ONCE, 0)})
    row = _run(once)[1]["phones"][0]
    assert row["fb_screen"] == "app"
    assert row["fb_confirm"] == ""
    assert row["status_zh"] == "网络✓ wifi"


def test_numeric_sim_and_operator_property_count_as_present():
    ready = _table({("dumpsys", "telephony.registry"): ("mSimState=5\nmAirplaneMode=false\nCellSignalStrengthLte: level=3\n", 0)})
    row = _run(ready)[1]["phones"][0]
    assert row["sim"] == "present"

    unknown = _table({
        ("dumpsys", "telephony.registry"): ("mSimState=UNKNOWN\nmAirplaneMode=false\nCellSignalStrengthLte: level=3\n", 0),
        ("getprop", "gsm.sim.state"): ("UNKNOWN,UNKNOWN\n", 0),
        ("getprop", "gsm.sim.operator.numeric"): ("51502\n", 0),
    })
    row = _run(unknown)[1]["phones"][0]
    assert row["sim"] == "present"
    assert "51502" not in _blob(row)
    card = _table({("dumpsys", "telephony.registry"): ("CARDSTATE_PRESENT\n", 0)})
    assert _run(card)[1]["phones"][0]["sim"] == "present"


def test_no_signal_status_does_not_cover_wifi():
    mobile = _table({
        ("dumpsys", "connectivity"): (MOBILE_CONN, 0),
        ("dumpsys", "telephony.registry"): (
            "mSimState=READY\nmAirplaneMode=false\nCellSignalStrengthLte: rssi=-120 level=0\n", 0),
    })
    row = _run(mobile)[1]["phones"][0]
    assert row["signal"] == "none"
    assert row["status_zh"] == "网络✗ 没信号"
    assert row["status_en"] == "net no signal"
    assert row["alert"] is True

    wifi = _table({
        ("dumpsys", "telephony.registry"): (
            "mSimState=READY\nmAirplaneMode=false\nCellSignalStrengthLte: rssi=-120 level=0\n", 0),
    })
    row = _run(wifi)[1]["phones"][0]
    assert row["transport"] == "wifi"
    assert row["status_zh"] == "网络✓ wifi"


def test_denied_command_is_unavailable_not_a_failure():
    def invoke(args, timeout=12):
        return None

    row = probe_phone(invoke)
    assert row["reachable"] == "unavailable"
    assert row["transport"] == "unavailable"
    assert row["sim"] == "unavailable"
    assert row["usage"] == "unavailable"
    assert row["fb_installed"] == "unavailable"
    assert row["status_zh"] == "网络状态不可用"
    assert row["alert"] is False

    def partial(args, timeout=12):
        if tuple(args)[:2] == ("dumpsys", "connectivity"):
            return None
        return _table().get(tuple(args), ("", 1))

    row = public_row(probe_phone(partial), wallpaper_no="07", key="07", unnumbered=False)
    assert row["transport"] == "unavailable"
    assert row["reachable"] is True
    assert row["status_zh"] == "网络✓"


def test_ussd_is_off_unless_configured_and_never_fakes_a_balance():
    _status, result, _detail, calls = _run(_table())
    assert result["ussd_enabled"] is False
    assert not any(cmd[:2] == ("am", "start") for cmd in calls)

    cfg = {
        "wallpaper_map": {SERIAL: "07"},
        "net_health_ussd_enabled": True,
        "net_health_ussd_codes": {"07": "*123#", "08": "not-a-code"},
    }
    table = _table({
        ("am", "start", "-a", "android.intent.action.CALL", "-d", "tel:*123%23"): ("", 0),
        ("dumpsys", "notification"): ("tickerText=USSD balance line " + SERIAL + "\n", 0),
    })
    _status, result, _detail, calls = _run(table, cfg=cfg)
    assert ("am", "start", "-a", "android.intent.action.CALL", "-d", "tel:*123%23") in calls
    check_adb_args(("-s", SERIAL, "shell") + calls[calls.index(
        ("am", "start", "-a", "android.intent.action.CALL", "-d", "tel:*123%23"))],
        allow_experimental_ussd=True)
    rem = result["phones"][0]["remaining_data"]
    assert rem["available"] is False and rem["experimental"] is True and rem["source"] == "ussd"
    assert SERIAL not in _blob(result)
    assert "not a stable balance API" in rem["note"]

    bad = dict(cfg)
    bad["net_health_ussd_codes"] = {"07": "*#06#"}
    _status, _result, _detail, calls = _run(_table(), cfg=bad)
    assert not any(cmd[:2] == ("am", "start") for cmd in calls)

    _status, result, _detail, calls = _run(table, cfg=cfg, live_stream=True)
    assert result["ussd_enabled"] is False
    assert not any(cmd[:2] == ("am", "start") for cmd in calls)


def test_foreground_facebook_is_opt_in():
    _status, _result, _detail, calls = _run(_table(), payload={"foreground_facebook": False})
    assert not any(cmd[:2] == ("am", "start") for cmd in calls)
    table = _table()
    _status, result, _detail, calls = _run(table, payload={"foreground_facebook": True})
    launch = ("am", "start", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER",
              "-p", "com.facebook.katana")
    assert launch in calls
    check_adb_args(("-s", SERIAL, "shell", *launch))
    _status, _result, _detail, calls = _run(table, payload={"foreground_facebook": True}, live_stream=True)
    assert launch not in calls


def test_unnumbered_and_sanitize_drop_serials():
    status, result, _detail, _calls = _run(_table(), cfg={})
    assert status == STATUS_DONE
    row = result["phones"][0]
    assert row["unnumbered"] is True and row["key"].startswith("unnumbered:")
    assert SERIAL not in _blob(result) and OTHER not in _blob(result)

    poisoned = {
        "phones": [{
            "serial": SERIAL, "ssid": "SecretNet", "wallpaper_no": "07", "reachable": True,
            "transport": "wifi", "fb_installed": True, "fb_screen": "app", "alert": False,
            "status_zh": SERIAL, "remaining_data": {"available": True, "text": "left " + SERIAL},
        }],
        "error": "boom " + SERIAL,
    }
    clean = sanitize_net_health_result(poisoned)
    assert "serial" not in clean["phones"][0]
    assert clean["phones"][0]["remaining_data"]["available"] is False
    assert SERIAL not in _blob(clean)
    assert "SecretNet" not in _blob(clean)


def test_protected_phone_is_rejected_and_adb_server_flag_stays_off(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("adb was prepared")

    monkeypatch.setattr("src.fleet.phones.prepare_adb", boom)
    col = _Col([{"serial": PROTECTED, "state": "device"}])
    status, result, detail = run_net_health(col, {}, {"serial": PROTECTED}, {})
    assert (status, detail) == (STATUS_REJECTED, "protected_phone")
    assert result["phones"] == []
    assert PROTECTED not in _blob(result)

    seen = {}

    def prep(*_a, **kw):
        seen["manage"] = kw.get("manage_server")
        return "adb", 41

    monkeypatch.setattr("src.fleet.phones.prepare_adb", prep)
    col = _Col([{"serial": SERIAL, "state": "device"}])
    col._run = lambda *_a, **_k: type("P", (), {"returncode": 0, "stdout": b"", "stderr": b""})()
    status, _result, detail = run_net_health(col, {"wallpaper_map": {SERIAL: "4"}})
    assert status == STATUS_DONE and detail == "ok"
    assert seen["manage"] is False and col.manage_server is False


def test_popup_lists_wallpaper_and_raises_only_when_newly_bad(tmp_path):
    assert oa.PANEL_SCRIPT.isascii()
    assert "status_zh" in oa.PANEL_SCRIPT and "status_en" in oa.PANEL_SCRIPT
    calls = []

    def present(_state, payload):
        calls.append(payload)
        return {"ok": True, "shown": False}

    alert = oa.OperatorAlert(tmp_path, clock=lambda: 1000.0, present=present)
    cfg = {"operator_alert_enabled": True, "wallpaper_map": {SERIAL: "07"}}
    ok = public_row({"reachable": True, "transport": "wifi", "fb_installed": True, "fb_screen": "app",
                     "airplane": False}, wallpaper_no="07", key="07", unnumbered=False)
    alert.note_network([dict(ok, serial=SERIAL)], cfg)
    assert calls == []
    snap = json.loads((tmp_path / oa.SNAP_NAME).read_text(encoding="utf-8"))
    assert snap["network"][0]["status_zh"] == "网络✓ wifi"
    assert snap["raised"] == []
    assert SERIAL not in json.dumps(snap)
    bad = public_row({"reachable": False, "transport": "none", "sim": "absent"}, wallpaper_no="07",
                     key="07", unnumbered=False)
    alert.note_network([bad], cfg)
    assert calls[-1]["raised"] == ["07"]
    assert calls[-1]["network"][0]["status_zh"] == "网络✗ 无流量"
    alert.note_network([bad], cfg)
    again = json.loads((tmp_path / oa.SNAP_NAME).read_text(encoding="utf-8"))
    assert again["raised"] == []
    assert len(calls) == 1
    alert.observe([{"serial": SERIAL, "state": "offline", "model": "M"}], "", cfg)
    assert calls[-1]["phones"][0]["wallpaper_no"] == "07"
    assert calls[-1]["network"][0]["status_zh"] == "网络✗ 无流量"
    assert SERIAL not in json.dumps(calls[-1])


def test_healthy_device_alert_is_unchanged_without_note_network(tmp_path):
    calls = []
    alert = oa.OperatorAlert(tmp_path, clock=lambda: 1000.0, present=lambda _s, payload: calls.append(payload))
    alert.observe([{"serial": SERIAL, "state": "device", "model": "M"}], "",
                  {"operator_alert_enabled": True, "wallpaper_map": {SERIAL: "07"}})
    assert calls == []


def test_agent_execute_net_health_and_push_config_does_not_enable_ussd(tmp_path, monkeypatch):
    assert TASK_NET_HEALTH in TASK_KINDS
    assert TASK_NET_HEALTH not in REMOTE_PHONE_KINDS
    assert TASK_NET_HEALTH not in LEGACY_ALLOWED_KINDS
    assert "net_health_ussd_enabled" in agent_mod.AgentConfig.OPERATOR_KEYS
    assert "net_health_ussd_enabled" not in agent_mod._PUSH_CONFIG_KEYS
    cfg = agent_mod.AgentConfig(tmp_path / "fleet")
    cfg.save()
    agent = agent_mod.NodeAgent(cfg, http=lambda *_a, **_k: (200, {}), app_version="t")
    agent.cfg.data["operator_alert_enabled"] = True
    agent.cfg.data["wallpaper_map"] = {SERIAL: "07"}

    def fake(*_a, **_k):
        return STATUS_DONE, sanitize_net_health_result({"phones": [{
            "wallpaper_no": "07", "key": "07", "unnumbered": False, "reachable": True,
            "transport": "wifi", "fb_installed": True, "fb_screen": "app", "alert": False,
            "status_zh": "网络✓ wifi", "status_en": "net ok wifi",
        }]}), "ok"

    monkeypatch.setattr("src.fleet.net_health.run_net_health", fake)
    status, result, detail = agent.execute({"kind": TASK_NET_HEALTH, "payload": {}, "target": {}})
    assert (status, detail) == (STATUS_DONE, "ok")
    assert result["phones"][0]["status_zh"] == "网络✓ wifi"
    assert SERIAL not in _blob(result)
    status, _result, detail = agent.execute({
        "kind": "push_config",
        "payload": {"patch": {"net_health_ussd_enabled": True}},
    })
    assert (status, detail) == (STATUS_REJECTED, "not_supported_in_agent_v1")


def test_collect_error_does_not_start_a_shell(monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("shell")

    monkeypatch.setattr("src.fleet.phones.prepare_adb", boom)
    status, result, detail = run_net_health(_Col([], "adb_server_not_running"), {})
    assert status == STATUS_FAILED and detail == "adb_server_not_running"
    assert result["phones"] == []
