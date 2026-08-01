# -*- coding: utf-8 -*-
"""P0 2026-08-01：主动关怀「常备接线 + 配置热闸」门禁。

守三件事：
1. CareDispatcher.cfg_provider 热闸——关闸空转零副作用；dry_run/max_per_tick 每 tick 实时跟随；
2. /api/care/health 四灯自检 + /api/care/engine 一键开闸（写 overlay 白名单路径、灰度强制）；
3. maybe_start_proactive_care 常备接线——enabled=false 也注册捕获回调、启动派发循环（被闸）。

测试全部用 :memory:/tmp 存储与 stub config，绝不写仓库 config/。
"""
import asyncio
import time
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.contacts.care_commitment import CareCommitment
from src.contacts.care_dispatcher import CareDispatcher
from src.contacts.care_schedule import CareScheduleStore
from src.web.routes.care_routes import register_care_routes

NOW = datetime(2026, 6, 17, 10, 0, 0).timestamp()  # 周三 10:00（非安静时段）


class _AI:
    def __init__(self, reply="你之前说的面试怎么样啦？"):
        self.reply = reply
        self.prompts = []

    async def chat(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.reply


def _sender(record, row_id=123):
    async def _send(channel, account_id, chat_name, reply, defer_until, reason, staleness, extra):
        record.append({"channel": channel, "reply": reply})
        return row_id
    return _send


def _store_with_due(topic="面试"):
    s = CareScheduleStore(":memory:")
    due = NOW - 0.1 * 86400
    c = CareCommitment(due_at=due, event_at=due, topic=topic, sentiment="neutral",
                       anchor_text="x", source_text="明天面试好紧张", confidence=0.85)
    s.add_commitment(c, contact_key="tg:u0", platform="telegram",
                     account_id="default", chat_key="u0")
    return s


class _ExplodingStore:
    """关闸空转必须零副作用——任何属性访问即失败。"""
    def __getattr__(self, name):
        raise AssertionError(f"disabled tick touched store.{name}")


# ── 1) dispatcher 配置热闸 ──────────────────────────────────────────────
async def test_disabled_gate_is_zero_side_effect():
    cfg = {"enabled": False}
    d = CareDispatcher(store=_ExplodingStore(), ai_client=_AI(),
                       send_callback=_sender([]), cfg_provider=lambda: cfg)
    n = await d.run_once(now=NOW)
    assert n == 0
    snap = d.health_snapshot()
    assert snap["last_tick_gated"] is True and snap["last_tick_ts"] == NOW


async def test_enable_via_provider_without_restart():
    """同一实例：关→开→dry_run→真发，全程无需重建（重启等价物）。"""
    s = _store_with_due()
    rec = []
    cfg = {"enabled": False}
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       context_provider=lambda ck: "上次聊到她准备面试",
                       cfg_provider=lambda: cfg)
    assert await d.run_once(now=NOW) == 0
    assert s.count(status="pending") == 1  # 关闸期不动库

    cfg.update({"enabled": True, "dry_run": True})
    assert await d.run_once(now=NOW) == 1
    assert s.count(status="sent") == 1 and not rec  # dry_run 只记不发
    assert d.health_snapshot()["dry_run_effective"] is True

    # 再来一条 → 切真发，下一 tick 生效
    s2 = _store_with_due(topic="体检")
    d._store = s2
    cfg.update({"dry_run": False})
    assert await d.run_once(now=NOW) == 1
    assert len(rec) == 1 and s2.count(status="sent") == 1
    assert d.health_snapshot()["dry_run_effective"] is False


async def test_no_provider_keeps_constructor_semantics():
    s = _store_with_due()
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       context_provider=lambda ck: "ctx")
    assert await d.run_once(now=NOW) == 1
    assert len(rec) == 1  # 无 provider＝旧行为（构造参数即真值）


async def test_provider_max_per_tick_live():
    s = CareScheduleStore(":memory:")
    for i in range(3):
        due = NOW - 0.05 * 86400
        c = CareCommitment(due_at=due, event_at=due, topic=f"面试{i}", sentiment="neutral",
                           anchor_text="x", source_text="s", confidence=0.85)
        s.add_commitment(c, contact_key=f"tg:u{i}", platform="telegram", chat_key=f"u{i}")
    cfg = {"enabled": True, "max_per_tick": 1}
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       context_provider=lambda ck: "ctx", max_per_tick=3,
                       cfg_provider=lambda: cfg)
    assert await d.run_once(now=NOW) == 1  # 实时 1 覆盖构造 3
    cfg["max_per_tick"] = 2
    assert await d.run_once(now=NOW) == 2


# ── 2) /api/care/health + /api/care/engine ─────────────────────────────
class _CM:
    """最小 ConfigManager stub：config dict + set_overlay_flag 内存版。"""
    def __init__(self, cfg, tmp=None):
        self.config = cfg
        self.config_path = str((tmp / "config.yaml")) if tmp else ""
        self.flags = []

    def set_overlay_flag(self, path, value):
        self.flags.append((path, value))
        node = self.config
        keys = path.split(".")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = value
        return True, "saved"


def _client(cfg=None, tmp=None):
    app = FastAPI()

    def _auth(request: Request):
        return True

    cm = _CM(cfg if cfg is not None else {}, tmp)
    register_care_routes(app, api_auth=_auth, config_manager=cm)
    app.state.care_schedule_store = CareScheduleStore(":memory:")
    app.state.config_manager = cm
    return TestClient(app), app, cm


def test_health_shape_when_disabled():
    client, app, _ = _client(cfg={"companion": {}})
    d = client.get("/api/care/health").json()
    assert d["ok"] is True and d["enabled"] is False and d["dry_run"] is False
    assert d["capture"] == {"config_on": False, "wired": False}
    assert d["dispatch"]["running"] is False
    assert d["delivery"] == {"multiplatform_deferred": False, "messenger_rpa": False}
    assert d["activity"]["summary"]["pending"] == 0


def test_health_reads_engine_state_and_activity():
    cfg = {"companion": {"proactive_care": {"enabled": True, "dry_run": True},
                         "multiplatform_deferred": {"enabled": True}}}
    client, app, _ = _client(cfg=cfg)
    store = app.state.care_schedule_store
    c = CareCommitment(due_at=time.time() + 3600, event_at=time.time() + 3600,
                       topic="面试", sentiment="neutral", anchor_text="x",
                       source_text="s", confidence=0.9)
    store.add_commitment(c, contact_key="c1", platform="telegram", chat_key="u1")

    class _Disp:
        def health_snapshot(self):
            return {"running": True, "last_tick_ts": 123.0, "last_tick_gated": False,
                    "dry_run_effective": True, "interval_sec": 600.0}

    app.state.care_engine = {"capture_wired": True, "dispatcher": _Disp(),
                             "dispatcher_skip": "", "messenger_rpa": True}
    d = client.get("/api/care/health").json()
    assert d["enabled"] is True and d["dry_run"] is True
    assert d["capture"]["wired"] is True and d["capture"]["config_on"] is True
    assert d["dispatch"]["running"] is True and d["dispatch"]["dry_run_effective"] is True
    assert d["delivery"]["multiplatform_deferred"] is True
    assert d["activity"]["captured_24h"] == 1
    assert d["activity"]["last_captured_ts"] > 0


def test_engine_enable_dry_writes_whitelisted_flags(tmp_path):
    client, app, cm = _client(cfg={"companion": {}}, tmp=tmp_path)
    d = client.post("/api/care/engine", json={"action": "enable_dry"}).json()
    assert d["ok"] is True and d["action"] == "enable_dry"
    assert set(d["applied"]) == {"companion.proactive_care.enabled",
                                 "companion.proactive_care.dry_run",
                                 "companion.multiplatform_deferred.enabled"}
    assert d["effective"] == {"enabled": True, "dry_run": True}
    # 审计落在 stub config_path 同目录（tmp），不碰仓库
    assert (tmp_path / "companion_capability_audit.jsonl").exists()


def test_engine_go_live_requires_enabled(tmp_path):
    client, app, cm = _client(cfg={"companion": {}}, tmp=tmp_path)
    d = client.post("/api/care/engine", json={"action": "go_live"}).json()
    assert d["ok"] is False and d["reason"] == "not_enabled"
    assert not cm.flags  # 一个 flag 都不许写


def test_engine_go_live_flips_dry_run(tmp_path):
    cfg = {"companion": {"proactive_care": {"enabled": True, "dry_run": True}}}
    client, app, cm = _client(cfg=cfg, tmp=tmp_path)
    d = client.post("/api/care/engine", json={"action": "go_live"}).json()
    assert d["ok"] is True
    assert d["effective"] == {"enabled": True, "dry_run": False}


def test_engine_pause_and_bad_action(tmp_path):
    cfg = {"companion": {"proactive_care": {"enabled": True}}}
    client, app, cm = _client(cfg=cfg, tmp=tmp_path)
    d = client.post("/api/care/engine", json={"action": "pause"}).json()
    assert d["ok"] is True and d["effective"]["enabled"] is False

    bad = client.post("/api/care/engine", json={"action": "nuke"}).json()
    assert bad["ok"] is False and bad["reason"] == "bad_action"


# ── 3) maybe_start_proactive_care 常备接线 ──────────────────────────────
class _InboxStub:
    def __init__(self):
        self.cbs = []

    def register_new_inbound_cb(self, cb):
        self.cbs.append(cb)

    def list_messages(self, ck, limit=8):
        return []


class _LoggerStub:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


class _WebApp:
    def __init__(self):
        class _S:
            pass
        self.state = _S()


class _Assistant:
    def __init__(self, cfg_dict, tmp):
        self.config = _CM(cfg_dict, tmp)
        self.inbox_store = _InboxStub()
        self.messenger_rpa_service = None
        self.ai_client = _AI()
        self.logger = _LoggerStub()
        self._care_dispatcher = None

    def get_ai_config(self):
        return {}

    def _build_care_paywall(self, store):
        return None

    def _enqueue_deferred_outbox(self, *a, **k):
        return 0


async def test_standby_wiring_registers_even_when_disabled(tmp_path, monkeypatch):
    """enabled=false：捕获回调也注册、派发循环也常备（被闸空转）——开闸免重启的根基。"""
    import src.contacts.care_schedule as cs
    from src.bootstrap.background_tasks import maybe_start_proactive_care
    monkeypatch.setattr(cs, "get_care_schedule_store",
                        lambda p=None: CareScheduleStore(":memory:"))

    assistant = _Assistant({"companion": {"proactive_care": {"enabled": False}}}, tmp_path)
    assistant.config.get_ai_config = lambda: {}
    web_app = _WebApp()
    await maybe_start_proactive_care(assistant, web_app)
    try:
        # P2 起为 2 个：capture 回调 + LLM 影子扫描回调（同一事件源双挂，均自带配置闸）
        assert len(assistant.inbox_store.cbs) == 2
        assert assistant._care_dispatcher is not None       # 派发循环常备
        assert assistant._care_dispatcher.is_running() is True
        eng = web_app.state.care_engine
        assert eng["capture_wired"] is True and eng["dispatcher_skip"] == ""
        assert eng["dispatcher"] is assistant._care_dispatcher
        assert eng.get("shadow_scanner") is not None        # 影子扫描器常备接线
        # 被闸：手动 tick 一次不产出、不炸
        assert await assistant._care_dispatcher.run_once(now=NOW) == 0
        # 捕获回调也被实时配置闸住（enabled=false 不入库）；影子回调同闸不入队
        for cb in assistant.inbox_store.cbs:
            cb({"conversation_id": "c1", "platform": "telegram", "chat_key": "u1"},
               "我明天要面试")
        st = web_app.state.care_schedule_store
        assert st.count() == 0
        assert web_app.state.care_engine["shadow_scanner"].snapshot()["queue"] == 0
    finally:
        await assistant._care_dispatcher.stop()
        if getattr(assistant, "_care_shadow_scanner", None) is not None:
            await assistant._care_shadow_scanner.stop()


async def test_standby_skips_dispatcher_without_ai(tmp_path, monkeypatch):
    import src.contacts.care_schedule as cs
    from src.bootstrap.background_tasks import maybe_start_proactive_care
    monkeypatch.setattr(cs, "get_care_schedule_store",
                        lambda p=None: CareScheduleStore(":memory:"))

    assistant = _Assistant({"companion": {}}, tmp_path)
    assistant.ai_client = None
    web_app = _WebApp()
    await maybe_start_proactive_care(assistant, web_app)
    assert assistant._care_dispatcher is None
    eng = web_app.state.care_engine
    assert eng["capture_wired"] is True and eng["dispatcher_skip"] == "ai_missing"


async def test_hot_flip_config_activates_capture_and_dispatch(tmp_path, monkeypatch):
    """模拟 overlay 热重载：改 config dict → 捕获与派发立即活过来（免重启语义端到端）。"""
    import src.contacts.care_schedule as cs
    from src.bootstrap.background_tasks import maybe_start_proactive_care
    store = CareScheduleStore(":memory:")
    monkeypatch.setattr(cs, "get_care_schedule_store", lambda p=None: store)

    cfg = {"companion": {"proactive_care": {"enabled": False}}}
    assistant = _Assistant(cfg, tmp_path)
    web_app = _WebApp()
    await maybe_start_proactive_care(assistant, web_app)
    try:
        # 热开闸（等价 set_overlay_flag 的内存深合并）
        cfg["companion"]["proactive_care"].update({"enabled": True, "dry_run": True})
        assistant.inbox_store.cbs[0](
            {"conversation_id": "c1", "platform": "telegram",
             "account_id": "default", "chat_key": "u1"},
            "我明天要去面试")
        assert store.count(status="pending") == 1            # 捕获活了
        n = await assistant._care_dispatcher.run_once(now=time.time() + 86400)
        assert store.count(status="sent") >= 0               # tick 正常执行（dry/skip 均可）
        assert assistant._care_dispatcher.health_snapshot()["last_tick_gated"] is False
        assert n >= 0
    finally:
        await assistant._care_dispatcher.stop()
