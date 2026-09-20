"""Q-4（#267 F）：升级后首开「这版改变了什么」弹窗——数据文件 / 判定 / 落地 / 路由 / 三语 / 随包。

验收：模拟 1.0.78 → 1.0.79 首启弹出（班表 + 额度两行由回滚 marker 命中，profile_llm 行 always，
regen 行 missing），选择落 overlay 为显式值；同版只弹一次；clean 装不弹；dev 不弹。
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.utils import release_defaults as rd

ENGINE_ROOT = Path(__file__).resolve().parent.parent
DESKTOP_MIN = ENGINE_ROOT / "config" / "config.desktop.min.yaml"
_WS_1078 = {"enabled": True, "default": {"start": "08:20", "end": "01:00"}}
_SG_1078 = {"enabled": True, "target_cap": 300, "warmup_start_cap": 100, "warmup_ramp_days": 3}
V = "1.0.79"


def _dig(doc, path):
    node = doc
    for k in path.split("."):
        if not isinstance(node, dict) or k not in node:
            return None
        node = node[k]
    return node


def _seed_1077_text() -> str:
    try:
        return subprocess.check_output(
            ["git", "show", "2ddbb3ec~1:./config/config.desktop.min.yaml"],
            cwd=str(ENGINE_ROOT), stderr=subprocess.DEVNULL).decode("utf-8")
    except Exception:
        cfg = yaml.safe_load(DESKTOP_MIN.read_text(encoding="utf-8"))
        (cfg.get("inbox") or {}).pop("work_schedule", None)
        cfg.pop("companion_send_gate", None)
        return yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)


def _base_1078() -> str:
    cfg = yaml.safe_load(_seed_1077_text())
    cfg.setdefault("inbox", {})["work_schedule"] = dict(_WS_1078)
    cfg["companion_send_gate"] = dict(_SG_1078)
    return yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False)


def _mk(tmp_path, monkeypatch, *, base_text, overlay, desktop=True):
    from src.utils.config_manager import ConfigManager
    cfg_path = tmp_path / "config" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(base_text, encoding="utf-8")
    if overlay is not None:
        (cfg_path.parent / "config.local.yaml").write_text(
            yaml.safe_dump(overlay, allow_unicode=True, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv("AITR_CONFIG_PATH", str(cfg_path))
    if desktop:
        monkeypatch.setenv("AITR_DESKTOP_MODE", "1")
    else:
        monkeypatch.delenv("AITR_DESKTOP_MODE", raising=False)
    monkeypatch.delenv("AITR_DEPLOY_PROFILE", raising=False)
    monkeypatch.delenv("AITR_SEED_DATA_DIR", raising=False)
    return ConfigManager(), cfg_path


def _overlay(cfg_path):
    p = cfg_path.parent / "config.local.yaml"
    return (yaml.safe_load(p.read_text(encoding="utf-8")) or {}) if p.exists() else {}


def _upgraded_1078_machine(tmp_path, monkeypatch):
    """1.0.78 升级机跑一次当前 load()：回滚写 marker → 这就是 1.0.79 首启的状态。"""
    overlay = {"companion": {"proactive_care": {"enabled": True}},
               "inbox": {"work_schedule": dict(_WS_1078)}}
    cm, cfg_path = _mk(tmp_path, monkeypatch, base_text=_base_1078(), overlay=overlay)
    assert asyncio.run(cm.load()) is True
    ov = _overlay(cfg_path)
    assert _dig(ov, "inbox.work_schedule.baseline_rollback") == V
    assert _dig(ov, "companion_send_gate.baseline_rollback") == V
    return cm, cfg_path


# ── 数据文件 ────────────────────────────────────────────────────────────────
def test_data_file_registers_1079_with_expected_rows():
    d = rd.load_release_defaults()
    assert d["schema"] == 1 and V in d["releases"]
    rows = {c["key"]: c for c in rd.release_changes(V, d)}
    assert rows["inbox.work_schedule.enabled"]["show_if"] == "marker"
    assert rows["inbox.work_schedule.enabled"]["needs_timezone"] is True
    assert rows["inbox.work_schedule.enabled"]["old"] is True
    assert rows["inbox.work_schedule.enabled"]["new"] is False
    assert rows["companion_send_gate.enabled"]["show_if"] == "marker"
    assert rows["inbox.work_schedule.off_hours.catch_up_regenerate_hours"] == {
        **rows["inbox.work_schedule.off_hours.catch_up_regenerate_hours"],
        "kind": "value", "old": 2, "new": 0, "show_if": "missing"}
    assert rows["companion.goals.profile_llm.enabled"]["show_if"] == "always"
    assert "1.0.85" in d["releases"]
    rows85 = {c["key"]: c for c in rd.release_changes("1.0.85", d)}
    assert rows85["inbox.sla_watcher.auto_expire_hours"]["old"] == 0
    assert rows85["inbox.sla_watcher.auto_expire_hours"]["new"] == 48
    assert rows85["inbox.sla_watcher.auto_expire_hours"]["show_if"] == "missing"
    assert rd.latest_registered_version(d) == "1.0.85"
    # 与 feature_registry 回滚表同源：marker 一致
    from src.utils.feature_registry import ROLLED_BACK_BASELINES
    for k, spec in ROLLED_BACK_BASELINES.items():
        assert rows[k]["marker"] == spec["marker"]
        assert rows[k]["old"] == spec["old"] and rows[k]["new"] == spec["new"]


def test_data_file_missing_or_broken_is_safe(tmp_path):
    assert rd.load_release_defaults(tmp_path / "nope.json") == {"schema": 0, "releases": {}}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert rd.load_release_defaults(bad)["releases"] == {}
    assert rd.release_changes(V, {"schema": 1, "releases": {}}) == []


def test_i18n_three_langs_cover_every_row():
    from src.web.i18n_packs.release_notice import EN, ZH, ZH_HANT
    import re
    cjk = re.compile(r"[\u4e00-\u9fff]")
    data = rd.load_release_defaults()
    for ver in data.get("releases") or {}:
        for c in rd.release_changes(ver, data):
            for suffix in ("_title", "_desc"):
                k = c["i18n"] + suffix
                assert k in ZH and k in EN and k in ZH_HANT, k
                assert not cjk.search(EN[k]), k
    for k in ("rn_title", "rn_sub", "rn_keep_off", "rn_turn_on", "rn_tz_label", "rn_tz_required",
              "rn_apply", "rn_later", "rn_done", "rn_fail"):
        assert k in ZH and k in EN and k in ZH_HANT, k
    assert set(ZH) == set(EN) == set(ZH_HANT)


# ── 判定：1.0.78 → 1.0.79 首启弹；同版只弹一次；clean 装不弹；dev 不弹 ─────────
def test_upgrade_1078_to_1079_first_open_pending_then_once(tmp_path, monkeypatch):
    cm, cfg_path = _upgraded_1078_machine(tmp_path, monkeypatch)
    info = rd.pending_notice(cm, V)
    assert info["pending"] is True and info["why"] == "upgrade"
    keys = [r["key"] for r in info["rows"]]
    assert "inbox.work_schedule.enabled" in keys
    assert "companion_send_gate.enabled" in keys
    assert "companion.goals.profile_llm.enabled" in keys          # always
    assert "inbox.work_schedule.off_hours.catch_up_regenerate_hours" in keys  # 用户没写过 → missing 显示
    ws = next(r for r in info["rows"] if r["key"] == "inbox.work_schedule.enabled")
    assert ws["current"] is False and ws["current_timezone"] == ""
    # 关闭 = 全部 keep → 写显式新默认 + seen
    res = rd.apply_choices(cm, V, {})
    assert res["ok"] and res["seen"]
    ov = _overlay(cfg_path)
    assert _dig(ov, "inbox.work_schedule.enabled") is False
    assert _dig(ov, "companion_send_gate.enabled") is False
    assert _dig(ov, "companion.goals.profile_llm.enabled") is True
    assert (cfg_path.parent / rd.STATE_FILENAME).exists()
    st = rd.read_state(cm)
    assert V in st["seen"] and st["last_version"] == V
    # 第二次打开：不弹
    assert rd.pending_notice(cm, V) == {**rd.pending_notice(cm, V), "pending": False, "why": "seen"}


def test_upgrade_choice_flip_writes_old_value_and_requires_timezone(tmp_path, monkeypatch):
    cm, cfg_path = _upgraded_1078_machine(tmp_path, monkeypatch)
    # 开启班表但没填时区 → tz_required，其余行照写，不 seen
    res = rd.apply_choices(cm, V, {"inbox.work_schedule.enabled": "flip",
                                   "companion_send_gate.enabled": "flip"})
    assert res["ok"] is False and res["seen"] is False
    assert {"key": "inbox.work_schedule.enabled", "reason": "tz_required"} in res["failed"]
    assert _dig(_overlay(cfg_path), "companion_send_gate.enabled") is True
    assert _dig(_overlay(cfg_path), "inbox.work_schedule.enabled") is False
    assert rd.pending_notice(cm, V)["pending"] is True          # 还会再弹
    # 服务器哨兵值也不算时区（红线③）
    for bad in ("local", "server", "", "Mars/Olympus"):
        r2 = rd.apply_choices(cm, V, {"inbox.work_schedule.enabled": "flip"}, timezone=bad)
        assert r2["ok"] is False and r2["failed"][0]["reason"] == "tz_required"
    # 带合法时区 → 开启 + 时区落 overlay + seen
    r3 = rd.apply_choices(cm, V, {"inbox.work_schedule.enabled": "flip"}, timezone="America/New_York")
    assert r3["ok"] and r3["seen"]
    ov = _overlay(cfg_path)
    assert _dig(ov, "inbox.work_schedule.enabled") is True
    assert _dig(ov, "inbox.work_schedule.timezone") == "America/New_York"
    assert _dig(cm.config, "inbox.work_schedule.timezone") == "America/New_York"
    # 内存合并视图也已生效
    assert _dig(cm.config, "inbox.work_schedule.enabled") is True
    # value 行不许 flip
    cm2, _ = _upgraded_1078_machine(tmp_path / "b", monkeypatch)
    r4 = rd.apply_choices(cm2, V, {"inbox.work_schedule.off_hours.catch_up_regenerate_hours": "flip"})
    assert r4["ok"] is False and r4["failed"][0]["reason"] == "not_a_switch"


def test_clean_install_never_pops_and_marks_seen(tmp_path, monkeypatch):
    """clean 装：种子已是新默认，overlay 只有基线 / 向导写的叶子，无 marker → 不弹、静默 seen。"""
    cm, cfg_path = _mk(tmp_path, monkeypatch, base_text=DESKTOP_MIN.read_text(encoding="utf-8"),
                       overlay={"ai": {"api_key": "k"}})
    assert asyncio.run(cm.load()) is True
    assert _overlay(cfg_path)  # overlay 非空（基线 / 用户 key）——不能拿它当升级信号
    info = rd.pending_notice(cm, V)
    assert info["pending"] is False and info["why"] == "clean_install"
    assert V in rd.read_state(cm)["seen"]
    # 之后再升到一个已登记的新版（模拟）：last_version ≠ 新版 → 判升级
    data = {"schema": 1, "releases": {V: rd.load_release_defaults()["releases"][V],
                                      "1.0.80": {"changes": [
                                          {"key": "companion.goals.profile_llm.enabled", "kind": "switch",
                                           "old": True, "new": False, "i18n": "rn_x", "show_if": "always"}]}}}
    assert rd.is_upgrade_install(cm, "1.0.80", data) is True
    assert rd.pending_notice(cm, "1.0.80", data)["pending"] is True


def test_user_configured_machine_skips_marker_rows(tmp_path, monkeypatch):
    """1.0.78 用户自己填了时区 / 改了额度 → 不回滚、无 marker → 班表 / 额度两行不弹。"""
    overlay = {"inbox": {"work_schedule": {**_WS_1078, "timezone": "Europe/London"}},
               "companion_send_gate": {**_SG_1078, "target_cap": 120}}
    cm, cfg_path = _mk(tmp_path, monkeypatch, base_text=_base_1078(), overlay=overlay)
    assert asyncio.run(cm.load()) is True
    assert _dig(_overlay(cfg_path), "inbox.work_schedule.baseline_rollback") is None
    rows = rd.pending_rows(cm, V)
    keys = [r["key"] for r in rows]
    assert "inbox.work_schedule.enabled" not in keys and "companion_send_gate.enabled" not in keys
    assert "companion.goals.profile_llm.enabled" in keys
    # 没 marker 也没 last_version → 这台机器整体不判升级（无可弹的默认变化）
    assert rd.pending_notice(cm, V)["pending"] is False


def test_dev_or_unregistered_version_never_pops(tmp_path, monkeypatch):
    cm, _ = _upgraded_1078_machine(tmp_path, monkeypatch)
    for v in ("dev", "", "9.9.9"):
        info = rd.pending_notice(cm, v)
        assert info["pending"] is False and info["why"] == "version_not_registered"
    assert not (Path(cm.config_path).parent / rd.STATE_FILENAME).exists()


# ── 路由 ────────────────────────────────────────────────────────────────────
def _client(cm):
    from src.web.routes.release_notice_routes import register_release_notice_routes
    app = FastAPI()
    app.state.config_manager = cm

    def _auth(request: Request):
        return True
    register_release_notice_routes(app, api_auth=_auth, config_manager=cm)
    return TestClient(app)


def test_routes_get_apply_dismiss(tmp_path, monkeypatch):
    cm, cfg_path = _upgraded_1078_machine(tmp_path, monkeypatch)
    monkeypatch.setenv("AITR_APP_VERSION", V)
    c = _client(cm)
    g = c.get("/api/release-notice").json()
    assert g["ok"] and g["pending"] is True and g["version"] == V
    assert g["labels"]["rn_title"].startswith("这版改变了什么") and V in g["labels"]["rn_title"]
    ws = next(r for r in g["rows"] if r["key"] == "inbox.work_schedule.enabled")
    assert ws["needs_timezone"] is True and ws["title"] and ws["desc"]
    assert ws["timezone_key"] == "inbox.work_schedule.timezone"
    # 开班表未填时区 → 拒 + 本地化 message
    a = c.post("/api/release-notice/apply", json={
        "version": V, "choices": {"inbox.work_schedule.enabled": "flip"}}).json()
    assert a["ok"] is False and a["failed"][0]["reason"] == "tz_required" and a["failed"][0]["message"]
    # 带时区 → 落 overlay
    a2 = c.post("/api/release-notice/apply", json={
        "version": V, "choices": {"inbox.work_schedule.enabled": "flip"},
        "timezone": "Asia/Shanghai"}).json()
    assert a2["ok"] and "inbox.work_schedule.timezone" in a2["applied"]
    assert _dig(_overlay(cfg_path), "inbox.work_schedule.enabled") is True
    assert c.get("/api/release-notice").json()["pending"] is False
    # dismiss 幂等
    dm = c.post("/api/release-notice/dismiss", json={"version": V}).json()
    assert dm["ok"] is True


def test_route_dev_version_silent(tmp_path, monkeypatch):
    cm, _ = _upgraded_1078_machine(tmp_path, monkeypatch)
    monkeypatch.delenv("AITR_APP_VERSION", raising=False)
    g = _client(cm).get("/api/release-notice").json()
    assert g["ok"] and g["pending"] is False and g["why"] == "version_not_registered"


# ── 装载点 / 随包 ───────────────────────────────────────────────────────────
def test_workspace_base_loads_script_for_master_only_and_js_has_no_cjk():
    import re
    tpl = (ENGINE_ROOT / "src" / "web" / "templates" / "workspace_base.html").read_text(encoding="utf-8")
    i = tpl.find("/static/workspace/release-notice.js")
    assert i > 0
    assert "user_role in ('master', 'operator')" in tpl[max(0, i - 300):i]
    js = (ENGINE_ROOT / "src" / "web" / "static" / "workspace" / "release-notice.js").read_text(encoding="utf-8")
    body = re.sub(r"/\*.*?\*/", "", js, flags=re.S)
    body = re.sub(r"//[^\n]*", "", body)
    assert not re.search(r"[\u4e00-\u9fff]", body), "release-notice.js 不得含语种字面量（文案全走 API labels）"
    for needle in ("/api/release-notice", "/api/release-notice/apply", "needs_timezone", "rn_tz_required",
                   "value=\"keep\"", "value=\"flip\"", "data-ws-embed"):
        assert needle in js, needle


def test_shipped_in_backend_datas_and_after_pack():
    bb = (ENGINE_ROOT / "desktop" / "build" / "build_backend.py").read_text(encoding="utf-8")
    assert '(REPO / "config" / "release_defaults_changed.json", "config")' in bb
    ap = (ENGINE_ROOT / "desktop" / "build" / "after-pack.js").read_text(encoding="utf-8")
    assert 'release_defaults_changed.json' in ap
    rf = (ENGINE_ROOT / "desktop" / "build" / "refresh_backend_datas.py").read_text(encoding="utf-8")
    assert "release_defaults_changed.json" in rf
    assert rd.DATA_FILE.exists()
    json.loads(rd.DATA_FILE.read_text(encoding="utf-8"))
