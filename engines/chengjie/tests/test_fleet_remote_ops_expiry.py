"""0.3.8 远程操作开关：有效期 / 到期自动关 / 通知 / 控制台剩余时间 / API 前缀（进程内，不碰真机）。

    python -m pytest tests/test_fleet_remote_ops_expiry.py -q -p no:cacheprovider
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time

import pytest

from src.fleet import remote_ops_watch
from src.fleet.protocol import STATUS_CANCELLED, STATUS_QUEUED, TASK_PHONE_SCREENSHOT, TASK_PING
from src.fleet.store import (
    REMOTE_OPS_AUTO_ACTOR, REMOTE_OPS_DEFAULT_MIN, REMOTE_OPS_MAX_MIN, clamp_remote_ops_min, remote_ops_live,
    resolve_fleet_cfg,
)
from tests.test_fleet_control import OP, _clean, _client, _enroll, st  # noqa: F401  (fixtures)
from tests.test_fleet_phone_ops import _CONSOLE7, _HARNESS7, _capable, _cnode


@pytest.fixture
def sent(monkeypatch):
    out = []
    monkeypatch.setattr(remote_ops_watch, "notify_hook", lambda text, nid, reason: out.append((reason, nid, text)))
    return out


# ── store ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("raw,want", [
    (None, 30), ("", 30), ("abc", 30), (True, 30), (0, 1), (-5, 1), (5, 5), ("15", 15), (12.7, 12),
    (1000, REMOTE_OPS_MAX_MIN), (REMOTE_OPS_MAX_MIN, REMOTE_OPS_MAX_MIN),
])
def test_clamp_remote_ops_min(raw, want):
    assert clamp_remote_ops_min(raw) == want


def test_remote_ops_live_shapes():
    assert remote_ops_live({}, 100) is False and remote_ops_live(None, 100) is False
    assert remote_ops_live({"remote_ops_enabled": True}, 100) is True          # 0.3.8 前的旧记录
    assert remote_ops_live({"remote_ops_enabled": True, "remote_ops_expires_at": 101}, 100) is True
    assert remote_ops_live({"remote_ops_enabled": True, "remote_ops_expires_at": 100}, 100) is False
    assert remote_ops_live({"remote_ops_enabled": True, "remote_ops_expires_at": "x"}, 100) is False


def test_enable_records_who_when_and_expiry(st):
    nid = _capable(st, remote_ops=False)
    now = time.time()
    assert st.set_remote_ops(nid, True, minutes=5, actor="operator via zhituo:alice", now=now)
    n = st.get_node(nid, now=now + 60)
    assert n["remote_ops_enabled"] is True
    assert n["remote_ops_expires_at"] == pytest.approx(now + 300)
    assert n["remote_ops_remaining_sec"] == 240
    assert n["remote_ops_enabled_by"] == "operator via zhituo:alice" and n["remote_ops_enabled_at"] == pytest.approx(now)
    # 默认 30 分钟、上限 4 小时
    st.set_remote_ops(nid, True, now=now)
    assert st.get_node(nid, now=now)["remote_ops_remaining_sec"] == REMOTE_OPS_DEFAULT_MIN * 60
    st.set_remote_ops(nid, True, minutes=99999, now=now)
    assert st.get_node(nid, now=now)["remote_ops_remaining_sec"] == REMOTE_OPS_MAX_MIN * 60


def test_expired_is_off_even_before_sweep(st):
    nid = _capable(st, remote_ops=False)
    past = time.time() - 3600
    st.set_remote_ops(nid, True, minutes=1, actor="op", now=past)
    n = st.get_node(nid)
    assert n["remote_ops_enabled"] is False and n["remote_ops_remaining_sec"] is None and n["remote_ops_enabled_by"] == ""
    assert st.task_refusal(nid, TASK_PHONE_SCREENSHOT) == "remote_ops_disabled"
    assert st.task_refusal(nid, TASK_PING) == ""
    assert st.enqueue(nid, TASK_PHONE_SCREENSHOT, target={"serial": "S1"}) is None


def test_sweep_auto_off_cancels_queued_and_is_idempotent(st):
    nid = _capable(st, remote_ops=False)
    now = time.time()
    st.set_remote_ops(nid, True, minutes=2, actor="operator via zhituo:bob", now=now)
    a = st.enqueue(nid, TASK_PHONE_SCREENSHOT, target={"serial": "S1"})
    ping = st.enqueue(nid, TASK_PING)
    assert st.expire_remote_ops(now=now + 60) == []                       # 没到期不动
    hits = st.expire_remote_ops(now=now + 121)
    assert len(hits) == 1
    h = hits[0]
    assert h["node_id"] == nid and h["enabled_by"] == "operator via zhituo:bob" and h["cancelled_tasks"] == 1
    assert h["expired_at"] == pytest.approx(now + 120) and h["enabled_at"] == pytest.approx(now)
    rec = st.get_task(a["task_id"])
    assert rec["status"] == STATUS_CANCELLED and rec["detail"] == "remote_ops_expired"
    assert st.get_task(ping["task_id"])["status"] == STATUS_QUEUED          # 非手机任务不受影响
    n = st.get_node(nid, now=now + 122)
    assert n["remote_ops_enabled"] is False
    assert n["remote_ops_last_off"]["by"] == REMOTE_OPS_AUTO_ACTOR and n["remote_ops_last_off"]["reason"] == "expired"
    assert n["remote_ops_last_off"]["enabled_by"] == "operator via zhituo:bob"
    assert st.expire_remote_ops(now=now + 200) == []                      # 幂等


def test_sweep_gives_legacy_record_an_expiry_without_turning_off(st):
    nid = _capable(st)                                                    # set_remote_ops(nid, True) → 30 分钟
    import json as _j
    st._conn.execute("UPDATE nodes SET meta_json=? WHERE node_id=?", (_j.dumps({"remote_ops_enabled": True}), nid))
    st._conn.commit()
    now = time.time()
    assert st.get_node(nid, now=now)["remote_ops_remaining_sec"] is None    # 旧记录：开着、无期限
    assert st.expire_remote_ops(now=now, default_minutes=10) == []
    n = st.get_node(nid, now=now)
    assert n["remote_ops_enabled"] is True and n["remote_ops_remaining_sec"] == 600


def test_manual_off_records_last_off(st):
    nid = _capable(st, remote_ops=False)
    st.set_remote_ops(nid, True, actor="op-a")
    a = st.enqueue(nid, TASK_PHONE_SCREENSHOT, target={"serial": "S1"})
    st.set_remote_ops(nid, False, actor="op-b")
    assert st.get_task(a["task_id"])["detail"] == "remote_ops_disabled"
    lo = st.get_node(nid)["remote_ops_last_off"]
    assert lo["by"] == "op-b" and lo["reason"] == "manual" and lo["enabled_by"] == "op-a"


def test_sweep_once_notifies_auto_off(st, sent):
    nid = _capable(st, remote_ops=False)
    now = time.time()
    st.set_remote_ops(nid, True, minutes=1, actor="operator via zhituo:alice", now=now)
    st.enqueue(nid, TASK_PHONE_SCREENSHOT, target={"serial": "S1"})
    hits = remote_ops_watch.sweep_once(st, now=now + 61)
    assert len(hits) == 1 and len(sent) == 1
    reason, who, text = sent[0]
    assert reason == "expired" and who == nid
    assert "到期自动关闭" in text and "zhituo:alice" in text and "取消排队手机操作 1 个" in text
    assert remote_ops_watch.sweep_once(st, now=now + 120) == [] and len(sent) == 1
    assert remote_ops_watch.sweep_once(None) == []


def test_notify_failure_never_breaks_sweep(st, monkeypatch):
    def boom(*a):
        raise RuntimeError("tg down")
    monkeypatch.setattr(remote_ops_watch, "notify_hook", boom)
    nid = _capable(st, remote_ops=False)
    now = time.time()
    st.set_remote_ops(nid, True, minutes=1, now=now)
    assert len(remote_ops_watch.sweep_once(st, now=now + 61)) == 1
    assert st.get_node(nid, now=now + 62)["remote_ops_enabled"] is False


def test_default_notify_uses_ops_alert_channel(monkeypatch):
    calls = []
    import src.ops.ops_alert as oa
    monkeypatch.setattr(oa, "notify", lambda kind, text, **kw: calls.append((kind, text, kw)) or False)
    remote_ops_watch._default_notify("hello", "n1", "enabled")
    assert calls == [("fleet_remote_ops", "hello", {"account_id": "n1", "reason": "enabled",
                                                    "source": "fleet-controller", "debounce_sec": 0})]


def test_start_sweep_runs_only_with_lock(st, tmp_path, monkeypatch):
    nid = _capable(st, remote_ops=False)
    st.set_remote_ops(nid, True, minutes=1, now=time.time() - 120)
    seen = []
    monkeypatch.setattr(remote_ops_watch, "notify_hook", lambda text, n, reason: seen.append(reason))
    import threading
    stop = threading.Event()
    th = remote_ops_watch.start_sweep(lambda: st, interval_sec=0.05, lock_path=str(tmp_path / "ro.lock"), stop=stop)
    try:
        deadline = time.time() + 5
        while not seen and time.time() < deadline:
            time.sleep(0.05)
    finally:
        stop.set()
        th.join(timeout=5)
    assert seen == ["expired"] and not th.is_alive()


# ── routes ────────────────────────────────────────────────────────────────────
def test_route_toggle_minutes_and_actor(st, sent):
    c = _client(st)
    nid = _capable(st, remote_ops=False)
    for bad in (0, 241, -1, "10", True, 1.5e9):
        r = c.post(f"/api/fleet/nodes/{nid}", headers=OP, json={"remote_ops_enabled": True, "remote_ops_minutes": bad})
        assert r.status_code == 400, bad
    assert st.get_node(nid)["remote_ops_enabled"] is False and sent == []
    r = c.post(f"/api/fleet/nodes/{nid}", headers=OP, json={"remote_ops_enabled": True, "remote_ops_minutes": 5})
    n = r.json()["node"]
    assert r.status_code == 200 and n["remote_ops_enabled"] is True and 295 <= n["remote_ops_remaining_sec"] <= 300
    assert n["remote_ops_enabled_by"] == "operator"
    assert [s[0] for s in sent] == ["enabled"] and "5 分钟后自动关闭" in sent[0][2]
    r = c.post(f"/api/fleet/nodes/{nid}", headers=OP, json={"remote_ops_enabled": False})
    assert r.json()["node"]["remote_ops_enabled"] is False
    assert [s[0] for s in sent] == ["enabled", "disabled"] and "已关闭远程操作" in sent[1][2]
    c.post(f"/api/fleet/nodes/{nid}", headers=OP, json={"remote_ops_enabled": False})   # 重复关不再通知
    assert len(sent) == 2


def test_route_uses_config_default_minutes(st, sent):
    c = _client(st, cfg={"fleet_control": {"public_url": "https://x.test/fleet", "remote_ops_default_min": 45}})
    nid = _capable(st, remote_ops=False)
    n = c.post(f"/api/fleet/nodes/{nid}", headers=OP, json={"remote_ops_enabled": True}).json()["node"]
    assert 2690 <= n["remote_ops_remaining_sec"] <= 2700


def test_route_console_injects_api_base_and_minutes(st):
    c = _client(st, cfg={"fleet_control": {"public_url": "https://x.test/fleet", "console_api_base": "/fleet/ui/",
                                           "remote_ops_default_min": 20}})
    body = c.get("/fleet/console", headers=OP).text
    ctx = json.loads(body.split(" ", 1)[1])
    assert ctx["fleet_api_base"] == "/fleet/ui" and ctx["remote_ops_default_min"] == 20
    assert ctx["remote_ops_max_min"] == REMOTE_OPS_MAX_MIN
    ctx2 = json.loads(_client(st).get("/fleet/console", headers=OP).text.split(" ", 1)[1])
    assert ctx2["fleet_api_base"] == "" and ctx2["remote_ops_default_min"] == REMOTE_OPS_DEFAULT_MIN


@pytest.mark.parametrize("raw,want", [
    ("", ""), (None, ""), ("/fleet/ui", "/fleet/ui"), ("/fleet/ui/", "/fleet/ui"), ("  /a-b_c.d  ", "/a-b_c.d"),
    ("fleet/ui", ""), ("javascript:alert(1)", ""), ("/a b", ""), ('/a"><script>', ""), ("//evil.com", ""),
    ("https://evil.com/x", ""),
])
def test_console_api_base_sanitized(raw, want):
    assert resolve_fleet_cfg({"fleet_control": {"console_api_base": raw}})["console_api_base"] == want


# ── 控制台：剩余时间 / 分钟输入 / API 前缀 ───────────────────────────────────
_HARNESS8 = _HARNESS7.replace(
    "eval(js);",
    "Object.assign(el('fc-cfg').attrs, step.cfg || {});\n"
    "if (step.mins !== undefined) el('fc-dlg-mins').value = String(step.mins);\n"
    "eval(js);",
).replace("toast: el('fc-toast').textContent}", "toast: el('fc-toast').textContent, err: el('fc-dlg-err').textContent}"
).replace("url.startsWith('/api/", "url.slice((step.cfg && step.cfg['data-api-base'] || '').length).startsWith('/api/")


def _run8(tmp_path, step, html=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    assert _HARNESS8.count("step.cfg") >= 4 and "err: el(" in _HARNESS8
    h = tmp_path / "h8.js"
    h.write_text(_HARNESS8, encoding="utf-8")
    out = subprocess.run([node, str(h), str(html or _CONSOLE7), json.dumps(step)], capture_output=True, text=True,
                         timeout=60, encoding="utf-8")
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_console_shows_remaining_time_and_who(tmp_path):
    nodes = [_cnode("n1", caps=["phone_ops_v1"], remote_ops_enabled=True, remote_ops_remaining_sec=1501,
                    remote_ops_expires_at=1.9e9, remote_ops_enabled_by="operator via zhituo:alice",
                    remote_ops_enabled_at=1.9e9 - 1500),
             _cnode("n2", caps=["phone_ops_v1"], remote_ops_enabled=True, remote_ops_remaining_sec=7260),
             _cnode("n3", caps=["phone_ops_v1"], remote_ops_enabled=True)]
    res = _run8(tmp_path, {"nodes": nodes})
    assert "远程操作已开 · 剩 26 分" in res["list"]
    assert "远程操作已开 · 剩 2 小时 1 分" in res["list"]
    assert "zhituo:alice 于" in res["list"] and "自动关闭" in res["list"]
    assert res["list"].count("远程操作已开") == 3                       # 旧主控没给剩余时间也照常显示


def test_console_enable_dialog_minutes(tmp_path):
    nodes = [_cnode("n1", caps=["phone_ops_v1"], remote_ops_enabled=False)]
    cfg = {"data-ro-min": "15", "data-ro-max": "60"}
    res = _run8(tmp_path, {"nodes": nodes, "click": "n1", "confirm": True, "cfg": cfg})
    assert "多少分钟后自动关闭（1–60）" in res["dlg"] and 'value="15"' in res["dlg"]
    assert [c for c in res["calls"] if c[0] == "POST"] == [
        ["POST", "/api/fleet/nodes/n1", {"remote_ops_enabled": True, "remote_ops_minutes": 15}]]
    res = _run8(tmp_path, {"nodes": nodes, "click": "n1", "confirm": True, "cfg": cfg, "mins": 45})
    assert [c[2] for c in res["calls"] if c[0] == "POST"] == [{"remote_ops_enabled": True, "remote_ops_minutes": 45}]
    for bad in (0, 61, 2.5, "abc"):
        res = _run8(tmp_path, {"nodes": nodes, "click": "n1", "confirm": True, "cfg": cfg, "mins": bad})
        assert [c for c in res["calls"] if c[0] == "POST"] == [], bad
        assert res["err"] == "自动关闭时间要填 1–60 的整数分钟", bad


def test_console_api_prefix_applies_to_every_call(tmp_path):
    nodes = [_cnode("n1", caps=["phone_ops_v1"], remote_ops_enabled=True)]
    res = _run8(tmp_path, {"nodes": nodes, "click": "n1", "confirm": False, "cfg": {"data-api-base": "/fleet/ui"}})
    urls = [c[1] for c in res["calls"]]
    assert len(urls) >= 6 and all(u.startswith("/fleet/ui/api/fleet/") for u in urls), urls
    assert "/fleet/ui/api/fleet/nodes/n1" in urls
    plain = _run8(tmp_path, {"nodes": nodes})
    assert all(c[1].startswith("/api/fleet/") for c in plain["calls"])
    bad = _run8(tmp_path, {"nodes": nodes, "cfg": {"data-api-base": "javascript:x"}})
    assert all(c[1].startswith("/api/fleet/") for c in bad["calls"])


def test_console_template_has_no_hardcoded_api_paths():
    html = _CONSOLE7.read_text(encoding="utf-8")
    import re
    assert re.findall(r"""(?:apiFetch|post)\(\s*['"]/""", html) == []
    assert 'id="fc-cfg"' in html and "fleet_api_base" in html


# VPS 的 HTML 改写中间件（admin.py FleetAssetPrefixMiddleware）会把 HTML 里所有带引号、以 / 开头的
# 根路径字面量补成 /fleet/ui/...；控制台脚本若再写 '/api/...' 或 '/revoke' 这类字面量，
# 会被改成 /fleet/ui/fleet/ui/api/... 或 .../nodes/n1/fleet/ui/revoke。所有 API 地址必须走 fa(...) 拼。
import re  # noqa: E402
_VPS_PAGE_LIKE = re.compile(r"""(["'`])/(?!/)[A-Za-z][^"'`\s\\]{0,200}\1""")


def _console_scripts():
    html = _CONSOLE7.read_text(encoding="utf-8")
    blocks = re.findall(r"<script\b[^>]*>(.*?)</script>", html, flags=re.S)
    assert blocks
    return "\n".join(blocks)


def test_console_script_has_no_root_relative_literals():
    js = _console_scripts()
    hits = [m.group(0) for m in _VPS_PAGE_LIKE.finditer(js)]
    assert hits == [], hits
    assert "API_ROOT" in js and js.count("fa(") >= 10


def test_console_script_survives_vps_style_rewrite():
    js = _console_scripts()

    def pref(m):
        return m.group(1) + "/fleet/ui" + m.group(0)[1:-1] + m.group(1)

    page = re.compile(r"""(["'`])/(?!/)(?!fleet(?:[/?#]|\1))(?!downloads(?:[/?#]|\1))[A-Za-z][^"'`\s\\]{0,200}\1""")
    assert page.sub(pref, js) == js

