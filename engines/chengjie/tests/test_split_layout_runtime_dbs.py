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


_LEGACY_CONFIG_FILES = (
    "persona_media.db",
    "group_members.db",
    "fatex.db",
    "persona_bio.db",
    "persona_quiz.db",
    "persona_proposals.db",
    "sticker_packs.db",
    "group_show.db",
    "deep_persona.db",
    "ops_events.db",
    "desktop_outbound.db",
    "vision_metrics.db",
    "account_sends.db",
    "album_restock_plan.json",
    "daily_topics_cache.json",
    "voice_iou.json",
    "persona_lora.json",
)

_DATA_DIR_DBS = (
    "account_risk_events.db",
    "automation_coverage_trend.db",
    "visual_memory.db",
)


def test_legacy_config_paths_stay_literal_without_split(monkeypatch, tmp_path):
    """只设 AITR_DATA_DIR 时，历史 ``config/<name>`` 必须仍是这条相对路径。"""
    _fresh(monkeypatch, AITR_DATA_DIR=str(tmp_path / "root"))
    for name in _LEGACY_CONFIG_FILES:
        assert data_paths.resolve_legacy_config_path(f"config/{name}") == f"config/{name}"
    explicit = tmp_path / "explicit.db"
    assert data_paths.resolve_legacy_config_path(":memory:") == ":memory:"
    assert data_paths.resolve_legacy_config_path(str(explicit)) == str(explicit)
    assert data_paths.resolve_legacy_config_path("goals.db") == "goals.db"


def test_legacy_config_files_open_under_data_root_not_cwd(monkeypatch, tmp_path):
    """WorkingDirectory 下的 ``config/`` 不得再长出这些运行时库。

    生产症状：``persona_media.db`` 在 ``/opt/chatx-fleet/app/config`` 被懒建成空库，
    数据根上的真库没人打开。这里把 CWD 指到假的安装树，断言打开落在数据根。
    """
    etc, data, cfg = _split(monkeypatch, tmp_path)
    app = tmp_path / "opt" / "chatx-fleet" / "app"
    (app / "config").mkdir(parents=True)
    monkeypatch.chdir(app)

    for name in _LEGACY_CONFIG_FILES:
        got = Path(data_paths.resolve_legacy_config_path(f"config/{name}"))
        assert got == data / name
        assert etc not in got.parents
        assert app not in got.parents

    import src.companion.persona_media_store as pms
    import src.companion.group_members_store as gms
    import src.fatex.store as fatex
    import src.companion.persona_bio_store as pbio
    import src.utils.persona_quiz_store as quiz
    import src.utils.persona_proposal_store as proposals
    import src.inbox.sticker_store as stickers
    import src.companion.group_show.store as gshow
    import src.companion.deep_persona_store as deep
    import src.companion.persona_self_memory as selfmem
    import src.companion.deep_persona_trend as trend
    import src.ops.ops_events as ops
    import src.inbox.desktop_outbound as dout
    import src.integrations.messenger_rpa.vision_metrics as vm
    import src.integrations.protocol_autoreply_limits as limits
    import src.ops.risk_events as risk
    import src.inbox.automation_coverage_trend as cov
    import src.companion.visual_memory as vmem
    import src.web.routes.group_show_routes as gsr
    import src.companion.media_restock as restock
    import src.companion.daily_topics as topics
    import src.client.voice_iou as vio
    import src.ai.persona_lora as lora

    def _sqlite_ok(path: Path) -> None:
        assert path.is_file(), path
        assert path.read_bytes()[:15] == b"SQLite format 3"

    pms.reset_persona_media_store()
    pms._DB_PATH = pms.DEFAULT_DB_PATH
    gms.reset_group_members_store()
    gms._DB_PATH = gms.DEFAULT_DB_PATH
    fatex.reset_fatex_store_for_tests()
    pbio.reset_persona_bio_store()
    pbio._DB_PATH = pbio.DEFAULT_DB_PATH
    quiz.reset()
    quiz._DB_PATH = quiz.DEFAULT_DB_PATH
    proposals.reset()
    proposals._DB_PATH = proposals.DEFAULT_DB_PATH
    stickers.reset_sticker_store()
    gshow.reset_group_show_store()
    deep.reset_deep_persona_store()
    selfmem.reset_persona_self_memory()
    trend.reset_deep_persona_trend()
    ops.reset_ops_event_store()
    dout.reset_desktop_outbound_queue()
    limits.reset_autoreply_limiter()
    prev_vm_path, prev_vm_init = vm._db_path, vm._initialized
    vm._db_path = None
    vm._initialized = False
    prev_risk, prev_cov, prev_vmem = risk._singleton, cov._singleton, vmem._STORE
    risk._singleton = None
    cov._singleton = None
    vmem._STORE = None
    prev_iou_state, prev_iou_loaded = dict(vio._STATE), vio._LOADED_FOR
    try:
        media = pms.get_persona_media_store()
        assert media is not None
        assert Path(pms._DB_PATH) == data / "persona_media.db"
        again = pms.configure_persona_media_store(
            data_paths.runtime_file("persona_media.db", cfg))
        assert again is media
        _sqlite_ok(data / "persona_media.db")

        assert gms.get_group_members_store() is not None
        _sqlite_ok(data / "group_members.db")
        assert fatex.get_fatex_store() is not None
        _sqlite_ok(data / "fatex.db")
        assert pbio.get_persona_bio_store() is not None
        _sqlite_ok(data / "persona_bio.db")
        assert quiz.get() is not None
        _sqlite_ok(data / "persona_quiz.db")
        assert proposals.get() is not None
        _sqlite_ok(data / "persona_proposals.db")
        assert stickers.get_sticker_store() is not None
        _sqlite_ok(data / "sticker_packs.db")

        class _CM:
            config_path = str(cfg)

        show = gsr._store(_CM())
        assert show is not None and show.available
        assert Path(show._db_path) == data / "group_show.db"
        _sqlite_ok(data / "group_show.db")

        assert deep.get_deep_persona_store("config/deep_persona.db") is not None
        assert selfmem.get_persona_self_memory("config/deep_persona.db") is not None
        assert trend.get_deep_persona_trend("config/deep_persona.db") is not None
        _sqlite_ok(data / "deep_persona.db")

        assert ops.get_ops_event_store() is not None
        _sqlite_ok(data / "ops_events.db")
        assert dout.get_desktop_outbound_queue() is not None
        _sqlite_ok(data / "desktop_outbound.db")

        vm._ensure_init()
        _sqlite_ok(data / "vision_metrics.db")

        limits.get_autoreply_limiter({})
        _sqlite_ok(data / "account_sends.db")

        risk_store = risk.get_risk_event_store()
        assert Path(risk_store._db_path) == data / "account_risk_events.db"
        _sqlite_ok(data / "account_risk_events.db")
        assert cov.get_coverage_trend_store() is not None
        _sqlite_ok(data / "automation_coverage_trend.db")
        assert vmem.get_visual_memory_store() is not None
        _sqlite_ok(data / "visual_memory.db")

        assert restock.save_plan(None, {"items": []}) is True
        assert (data / "album_restock_plan.json").is_file()
        topics._write_cache("", {"items": []})
        assert (data / "daily_topics_cache.json").is_file()
        vio.record_iou("split-layout-probe", "p", now=1.0)
        assert (data / "voice_iou.json").is_file()
        lora.write_lora_registry_entry(
            "config/persona_lora.json", "split-probe",
            {"file": "probe.safetensors", "trigger": "probe", "weight": 0.5})
        assert (data / "persona_lora.json").is_file()

        for name in _LEGACY_CONFIG_FILES + _DATA_DIR_DBS:
            assert not (app / "config" / name).exists(), name
            assert not (etc / name).exists(), name
            assert (data / name).is_file(), name
    finally:
        pms.reset_persona_media_store()
        pms._DB_PATH = pms.DEFAULT_DB_PATH
        gms.reset_group_members_store()
        gms._DB_PATH = gms.DEFAULT_DB_PATH
        fatex.reset_fatex_store_for_tests()
        pbio.reset_persona_bio_store()
        pbio._DB_PATH = pbio.DEFAULT_DB_PATH
        quiz.reset()
        quiz._DB_PATH = quiz.DEFAULT_DB_PATH
        proposals.reset()
        proposals._DB_PATH = proposals.DEFAULT_DB_PATH
        stickers.reset_sticker_store()
        gshow.reset_group_show_store()
        deep.reset_deep_persona_store()
        selfmem.reset_persona_self_memory()
        trend.reset_deep_persona_trend()
        ops.reset_ops_event_store()
        dout.reset_desktop_outbound_queue()
        limits.reset_autoreply_limiter()
        vm._db_path, vm._initialized = prev_vm_path, prev_vm_init
        if risk._singleton is not None and risk._singleton is not prev_risk:
            try:
                risk._singleton._conn.close()
            except Exception:
                pass
        risk._singleton = prev_risk
        if cov._singleton is not None and cov._singleton is not prev_cov:
            try:
                cov._singleton.close()
            except Exception:
                pass
        cov._singleton = prev_cov
        if vmem._STORE is not None and vmem._STORE is not prev_vmem:
            try:
                vmem._STORE.close()
            except Exception:
                pass
        vmem._STORE = prev_vmem
        vio._STATE.clear()
        vio._STATE.update(prev_iou_state)
        vio._LOADED_FOR = prev_iou_loaded


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
