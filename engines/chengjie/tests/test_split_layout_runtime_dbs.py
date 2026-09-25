"""VPS 分裂布局：运行时库不得落在 YAML 目录。

``AITR_CONFIG_PATH=/etc/chatx-fleet/config.yaml`` + ``AITR_DATA_DIR=/var/lib/...``
时 ``ProtectSystem=strict`` 让 ``/etc`` 只读。``proactive_care`` / ``deferred_outbox``
若仍用 ``config_path.parent``，启动日志是 ``unable to open database file``。
同树布局（YAML 在数据根 ``config/`` 里）路径必须与改前一致。
"""
from __future__ import annotations

import logging
from pathlib import Path

from src.licensing import data_paths


def _fresh(monkeypatch, **env):
    for k in ("AITR_CONFIG_PATH", "AITR_DATA_DIR"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)


def _split(monkeypatch, tmp_path):
    etc = tmp_path / "etc" / "chatx-fleet"
    data = tmp_path / "var" / "lib" / "chatx-fleet"
    cfg = etc / "config.yaml"
    etc.mkdir(parents=True)
    cfg.write_text("companion: {}\n", encoding="utf-8")
    _fresh(monkeypatch, AITR_CONFIG_PATH=str(cfg), AITR_DATA_DIR=str(data))
    return etc, data, cfg


def test_cwd_relative_defaults_stay_put_without_split(monkeypatch, tmp_path):
    """只设 AITR_DATA_DIR（pytest / 桌面同树之前）不得改掉相对 config/ 默认。"""
    _fresh(monkeypatch, AITR_DATA_DIR=str(tmp_path / "root"))
    assert data_paths.cwd_or_data_file("account_registry.db") == Path(
        "config/account_registry.db")
    assert data_paths.data_dir() == data_paths.config_dir()


def test_cwd_relative_defaults_follow_data_root_when_split(monkeypatch, tmp_path):
    etc, data, _cfg = _split(monkeypatch, tmp_path)
    for name in (
        "account_registry.db", "registry.key", "proxy_pool.db",
        "fingerprints.db", "proxy_subscriptions.db", "autoreply_audit.db",
        "runtime_flags.db", "protocol_autoreply.json", "nurture_ledger.json",
    ):
        p = data_paths.cwd_or_data_file(name)
        assert p == data / name
        assert etc not in p.parents


def test_care_deferred_and_siblings_follow_runtime_dir(monkeypatch, tmp_path):
    etc, data, cfg = _split(monkeypatch, tmp_path)

    from src.contacts.care_schedule import default_care_db_path
    from src.companion.goals.service import resolve_db_path, DEFAULT_DB_NAME
    from src.integrations.account_registry import default_registry_db_path
    from src.integrations.line_rpa.state_store import (
        default_state_db_path as line_db,
    )
    from src.integrations.messenger_rpa.state_store import (
        default_state_db_path as msg_db,
    )
    from src.integrations.registry_crypto import default_key_path
    from src.integrations.whatsapp_rpa.state_store import (
        default_state_db_path as wa_db,
    )
    from src.utils.kb_registry import _db_path

    care = Path(default_care_db_path(cfg))
    assert care == data / "care_schedule.db"
    assert etc not in care.parents

    class _CM:
        config_path = str(cfg)

    kb = _db_path(_CM())
    assert kb == data / "knowledge_base.db"

    goals = Path(resolve_db_path({"companion": {"goals": {}}}, cfg))
    assert goals == data / DEFAULT_DB_NAME
    rel = Path(resolve_db_path(
        {"companion": {"goals": {"db_path": "goals.db"}}}, cfg))
    assert rel == data / "goals.db"

    assert line_db(cfg) == data / "line_rpa_state.db"
    assert line_db(cfg, "phone_2") == data / "line_rpa_state_phone_2.db"
    assert wa_db(cfg) == data / "wa_rpa_state.db"
    assert msg_db(cfg, "bg_phone_2") == data / "messenger_rpa_state_bg_phone_2.db"
    assert default_registry_db_path() == data / "account_registry.db"
    assert default_key_path() == data / "registry.key"
    assert data_paths.plugin_dir(cfg).parent / "logs" / "care_shadow" == (
        data / "logs" / "care_shadow")


def test_other_config_and_colocated_yaml_are_not_relocated(monkeypatch, tmp_path):
    etc, data, cfg = _split(monkeypatch, tmp_path)
    other = tmp_path / "case" / "config.yaml"
    other.parent.mkdir(parents=True)

    from src.contacts.care_schedule import default_care_db_path
    from src.integrations.line_rpa.state_store import default_state_db_path

    assert Path(default_care_db_path(other)) == other.resolve().parent / "care_schedule.db"
    assert default_state_db_path(other) == other.resolve().parent / "line_rpa_state.db"
    assert etc not in Path(default_care_db_path(cfg)).parents

    root = tmp_path / "userdata"
    desk = root / "config" / "config.yaml"
    _fresh(monkeypatch, AITR_DATA_DIR=str(root), AITR_CONFIG_PATH=str(desk))
    assert Path(default_care_db_path(desk)) == desk.parent / "care_schedule.db"
    assert default_state_db_path(desk) == desk.resolve().parent / "line_rpa_state.db"
    assert data_paths.cwd_or_data_file("account_registry.db") == Path(
        "config/account_registry.db")
    # data 是上一个夹具的目录，同树布局不应再指向它
    assert data not in Path(default_care_db_path(desk)).parents


def test_deferred_outbox_opens_sqlite_under_data_root(monkeypatch, tmp_path):
    etc, data, cfg = _split(monkeypatch, tmp_path)

    class _Cfg:
        config_path = str(cfg)
        config: dict = {}

    class _Asst:
        config = _Cfg()
        _deferred_outbox_dispatcher = None
        _web_app = None
        logger = logging.getLogger("split-layout-deferred")
        inbox_store = None
        telegram_client = None

    from src.bootstrap.background_tasks import ensure_deferred_outbox

    disp = ensure_deferred_outbox(_Asst())
    try:
        landed = data / "deferred_outbox.db"
        assert disp is not None
        assert landed.is_file()
        assert landed.read_bytes()[:15] == b"SQLite format 3"
        assert not (etc / "deferred_outbox.db").exists()
    finally:
        if disp is not None:
            disp._store.close()


def test_care_store_opens_sqlite_under_data_root(monkeypatch, tmp_path):
    etc, data, cfg = _split(monkeypatch, tmp_path)
    import src.contacts.care_schedule as cs

    prev = cs._singleton
    cs._singleton = None
    store = None
    try:
        store = cs.get_care_schedule_store(cs.default_care_db_path(cfg))
        landed = Path(store._db_path)
        assert landed == data / "care_schedule.db"
        assert landed.read_bytes()[:15] == b"SQLite format 3"
        assert not (etc / "care_schedule.db").exists()
    finally:
        if store is not None:
            store.close()
        cs._singleton = prev


def test_contacts_db_follows_runtime_dir_yaml_dir_unchanged(monkeypatch, tmp_path):
    etc, data, cfg = _split(monkeypatch, tmp_path)

    class _Cfg:
        config_path = str(cfg)
        config = {"contacts": {"enabled": True}}

    from src.contacts.bootstrap import bootstrap_contacts_subsystem

    sub = bootstrap_contacts_subsystem(_Cfg(), etc)
    try:
        assert sub is not None
        landed = Path(sub.store._db_path)
        assert landed == data / "contacts.db"
        assert landed.read_bytes()[:15] == b"SQLite format 3"
        assert not (etc / "contacts.db").exists()
    finally:
        if sub is not None:
            sub.close()


def test_startup_sources_do_not_pin_runtime_dbs_to_yaml_dir():
    root = Path(__file__).resolve().parents[1] / "src"
    bg = (root / "bootstrap" / "background_tasks.py").read_text(encoding="utf-8")
    assert "default_care_db_path" in bg
    assert 'runtime_file("deferred_outbox.db"' in bg
    assert 'runtime_file("nurture_ledger.json"' in bg
    assert "Path(assistant.config.config_path).parent" not in bg
    care = (root / "web" / "routes" / "care_routes.py").read_text(encoding="utf-8")
    assert "default_care_db_path" in care
    assert 'base / "care_schedule.db"' not in care
