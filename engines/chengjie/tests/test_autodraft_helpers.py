# -*- coding: utf-8 -*-
"""autodraft_helpers 抽取回归测试（Stage 2）。

enrich_auto_draft：主体重依赖，守护可导入性 + 签名契约。
make_auto_draft_cb：纯分支逻辑，跑真实行为覆盖(过滤/档位/生成/调度)，
能抓出抽取时的改名错误(如 cfg.skip / cfg.min_len 误写)。"""
import inspect
from unittest.mock import MagicMock, patch

from src.inbox.autodraft_helpers import (
    AutoDraftConfig,
    enrich_auto_draft,
    make_auto_draft_cb,
    setup_auto_draft,
)


def test_enrich_is_coroutine():
    assert inspect.iscoroutinefunction(enrich_auto_draft)


def test_enrich_signature_contract():
    params = list(inspect.signature(enrich_auto_draft).parameters)
    assert params == [
        "assistant", "draft_svc", "_ad_app", "_ad_store",
        "conv", "text", "draft_id", "mode",
    ]


def _cfg(**kw):
    base = dict(mode="auto_ai", min_len=0, skip=set(),
                platform_ceilings={}, skip_groups=False, enrich=False)
    base.update(kw)
    return AutoDraftConfig(**base)


def _make(cfg, draft_svc=None, store=None, loop=None, enrich_fn=None):
    if store is None:
        store = MagicMock()
        store.get_automation_mode_if_set.return_value = None
    return make_auto_draft_cb(
        cfg,
        draft_svc or MagicMock(),
        store,
        loop or MagicMock(),
        enrich_fn or MagicMock(),
        MagicMock(),
    )


def test_skip_platform():
    ds = MagicMock()
    cb = _make(_cfg(skip={"messenger"}), draft_svc=ds)
    cb({"platform": "messenger", "conversation_id": "c1"}, "hello there")
    ds.auto_generate_draft.assert_not_called()


def test_min_len_too_short():
    ds = MagicMock()
    cb = _make(_cfg(min_len=10), draft_svc=ds)
    cb({"platform": "tg", "conversation_id": "c1"}, "hi")
    ds.auto_generate_draft.assert_not_called()


def test_manual_mode_global():
    ds = MagicMock()
    cb = _make(_cfg(mode="manual"), draft_svc=ds)
    cb({"platform": "tg", "conversation_id": "c1"}, "hello there")
    ds.auto_generate_draft.assert_not_called()


def test_per_conv_manual_override():
    ds = MagicMock()
    store = MagicMock()
    store.get_automation_mode_if_set.return_value = "manual"
    cb = _make(_cfg(mode="auto_ai"), draft_svc=ds, store=store)
    cb({"platform": "tg", "conversation_id": "c1"}, "hello there")
    ds.auto_generate_draft.assert_not_called()


def test_generates_draft_no_enrich():
    ds = MagicMock()
    ds.auto_generate_draft.return_value = "d1"
    cb = _make(_cfg(mode="auto_ai", enrich=False), draft_svc=ds)
    cb({"platform": "tg", "conversation_id": "c1"}, "hello there")
    ds.auto_generate_draft.assert_called_once()
    _, kw = ds.auto_generate_draft.call_args
    assert kw["automation_mode"] == "auto_ai"
    assert kw["enrich"] is False


def test_generates_and_schedules_enrich():
    ds = MagicMock()
    ds.auto_generate_draft.return_value = "d1"
    enrich_fn = MagicMock()
    loop = MagicMock()
    cb = _make(_cfg(mode="auto_ai", enrich=True), draft_svc=ds,
               loop=loop, enrich_fn=enrich_fn)
    with patch(
        "src.inbox.autodraft_helpers.asyncio.run_coroutine_threadsafe"
    ) as rct:
        cb({"platform": "tg", "conversation_id": "c1"}, "hello there")
    ds.auto_generate_draft.assert_called_once()
    rct.assert_called_once()


def _assistant(auto_draft_cfg):
    a = MagicMock()
    a.config.config = {"inbox": {"auto_draft": auto_draft_cfg}}
    return a


def test_setup_auto_draft_enabled_registers_cb():
    a = _assistant({"enabled": True})
    with patch(
        "src.inbox.autodraft_helpers.asyncio.get_running_loop",
        return_value=MagicMock(),
    ):
        setup_auto_draft(a, MagicMock(), MagicMock())
    a.inbox_store.register_new_inbound_cb.assert_called_once()


def test_setup_auto_draft_disabled_skips():
    a = _assistant({"enabled": False})
    setup_auto_draft(a, MagicMock(), MagicMock())
    a.inbox_store.register_new_inbound_cb.assert_not_called()
    a.logger.info.assert_called()


def _companion_app_cfg():
    return {
        "inbox": {"auto_draft": {"automation_mode": "auto_ai"}},
        "platform_login": {"telegram": {"companion_runtime": True}},
    }


def _orch_owns_tg():
    orch = MagicMock()
    orch.owns.side_effect = (
        lambda p, a: str(p).lower() == "telegram"
    )
    return orch


def test_companion_auto_ai_suppresses_system_z():
    """companion 持号 + auto_ai → System Z 让位（防与 A 线双发）。"""
    ds = MagicMock()
    store = MagicMock()
    store.get_automation_mode_if_set.return_value = "auto_ai"
    cb = make_auto_draft_cb(
        _cfg(mode="auto_ai"), ds, store, MagicMock(), MagicMock(),
        MagicMock(), app_config=_companion_app_cfg(),
    )
    with patch(
        "src.integrations.account_orchestrator.get_orchestrator_if_running",
        return_value=_orch_owns_tg(),
    ):
        cb({"platform": "telegram", "account_id": "katie",
            "conversation_id": "telegram:katie:1"}, "hello there")
    ds.auto_generate_draft.assert_not_called()


def test_companion_review_allows_system_z_draft():
    """companion 持号但坐席切「AI草稿我审」→ System Z 须拟稿（A 线已让位）。"""
    ds = MagicMock()
    ds.auto_generate_draft.return_value = "d1"
    store = MagicMock()
    store.get_automation_mode_if_set.return_value = "review"
    cb = make_auto_draft_cb(
        _cfg(mode="auto_ai"), ds, store, MagicMock(), MagicMock(),
        MagicMock(), app_config=_companion_app_cfg(),
    )
    with patch(
        "src.integrations.account_orchestrator.get_orchestrator_if_running",
        return_value=_orch_owns_tg(),
    ):
        cb({"platform": "telegram", "account_id": "katie",
            "conversation_id": "telegram:katie:1"}, "hello there")
    ds.auto_generate_draft.assert_called_once()
    _, kw = ds.auto_generate_draft.call_args
    assert kw["automation_mode"] == "review"


def test_manual_mode_schedules_media_enrich_but_no_draft():
    """接力记忆 P0-2：manual 档不拟稿，但调度入站媒体补识（切回全自动时历史不盲）。"""
    ds = MagicMock()
    store = MagicMock()
    store.get_automation_mode_if_set.return_value = "manual"
    cb = _make(_cfg(mode="auto_ai"), draft_svc=ds, store=store)
    with patch(
        "src.inbox.autodraft_helpers.asyncio.run_coroutine_threadsafe"
    ) as rct:
        cb({"platform": "tg", "conversation_id": "c1"}, "[图片]")
    ds.auto_generate_draft.assert_not_called()
    rct.assert_called_once()
    coro = rct.call_args[0][0]
    assert coro.__name__ == "enrich_manual_inbound_media"
    coro.close()


async def test_enrich_manual_inbound_media_writes_back_only_placeholder_inbound(monkeypatch):
    from src.inbox import autodraft_helpers as H

    store = MagicMock()
    store.list_recent_messages.return_value = [
        {"message_id": "m1", "direction": "in", "text": "[图片]", "media_type": "image",
         "media_ref": "/static/protocol_media/a.jpg"},
        {"message_id": "m2", "direction": "out", "text": "", "media_type": "image",
         "media_ref": "/static/protocol_media/o.jpg"},                      # 出站不识别
        {"message_id": "m3", "direction": "in", "text": "看这个", "media_type": "image",
         "media_ref": "/static/protocol_media/b.jpg"},                      # 有 caption 不重复识别
        {"message_id": "m4", "direction": "in", "text": "", "media_type": "voice",
         "media_ref": "/static/protocol_media/v.ogg"},
        {"message_id": "m5", "direction": "in", "text": "hello"},
    ]
    store.update_message_text.return_value = True
    calls = []

    async def _fake_enrich(**kw):
        calls.append(kw["media_ref"])
        return f"[图片内容] 描述{len(calls)}", f"描述{len(calls)}"

    monkeypatch.setattr("src.inbox.media_enrich.enrich_inbound_media_text", _fake_enrich)
    cfg = {"inbox": {"auto_draft": {"media_backscan": 5, "media_wait_sec": 0}}}
    n = await H.enrich_manual_inbound_media(store, {"conversation_id": "c1"}, cfg)
    assert n == 2
    # 最新优先：语音 v.ogg 先、图片 a.jpg 后；出站 / 有 caption 的都没碰
    assert calls == ["/static/protocol_media/v.ogg", "/static/protocol_media/a.jpg"]
    kws = [c.kwargs for c in store.update_message_text.call_args_list]
    assert [k["message_id"] for k in kws] == ["m4", "m1"]
    assert all(k["only_if_empty"] is True for k in kws)
    # 开关关 → 零动作
    calls.clear()
    store.update_message_text.reset_mock()
    cfg_off = {"inbox": {"auto_draft": {"manual_media_enrich": False}}}
    assert await H.enrich_manual_inbound_media(store, {"conversation_id": "c1"}, cfg_off) == 0
    assert calls == [] and not store.update_message_text.called
    # 无 store / 无 cid 软失败
    assert await H.enrich_manual_inbound_media(None, {"conversation_id": "c1"}, cfg) == 0
    assert await H.enrich_manual_inbound_media(store, {}, cfg) == 0


def test_companion_manual_still_silent():
    """companion 持号 + 手动 → 不拟稿不直发。"""
    ds = MagicMock()
    store = MagicMock()
    store.get_automation_mode_if_set.return_value = "manual"
    cb = make_auto_draft_cb(
        _cfg(mode="auto_ai"), ds, store, MagicMock(), MagicMock(),
        MagicMock(), app_config=_companion_app_cfg(),
    )
    with patch(
        "src.integrations.account_orchestrator.get_orchestrator_if_running",
        return_value=_orch_owns_tg(),
    ):
        cb({"platform": "telegram", "account_id": "katie",
            "conversation_id": "telegram:katie:1"}, "hello there")
    ds.auto_generate_draft.assert_not_called()
