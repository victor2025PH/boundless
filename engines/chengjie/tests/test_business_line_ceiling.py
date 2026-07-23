# -*- coding: utf-8 -*-
"""账号业务线档位封顶门禁（融合实例 P1）。

不变量：翻译业务线账号在合并部署里**绝不被陪伴线全自动波及**——
① autodraft：translation 账号 → 档位封顶 review（AI 仍拟稿、强制人审、不自动发），
   封顶链 = 会话显式 > 账号业务线 > 平台 > 全局，各级只降不升；
② proactive：translation 账号一票否决，永不主动"想你了"；
③ 未标注账号 / 注册表未就绪 → 全链路零行为变更。
"""
from unittest.mock import MagicMock

import pytest

import src.integrations.account_registry as ar
from src.inbox.autodraft_helpers import AutoDraftConfig, make_auto_draft_cb


@pytest.fixture(autouse=True)
def _fresh_bl_cache():
    ar._BL_CACHE.clear()
    yield
    ar._BL_CACHE.clear()


# ── 注册表列 + 缓存 ────────────────────────────────────────────────────────

def test_registry_business_line_column_roundtrip():
    reg = ar._registry  # conftest autouse 已重定向到临时库
    row = reg.upsert("telegram", "acct1", mode="protocol",
                     business_line="translation")
    assert row["business_line"] == "translation"
    # 不传 business_line 的后续 upsert 不得清掉标签（None=沿用语义）
    row2 = reg.upsert("telegram", "acct1", status="online")
    assert row2["business_line"] == "translation"
    # set_business_line 改写 + 非法值拒绝
    assert reg.set_business_line("telegram", "acct1", "companion") is True
    assert reg.get("telegram", "acct1")["business_line"] == "companion"
    assert reg.set_business_line("telegram", "acct1", "weird") is False
    assert reg.get("telegram", "acct1")["business_line"] == "companion"
    assert reg.set_business_line("telegram", "acct1", "") is True
    assert reg.get("telegram", "acct1")["business_line"] == ""


def test_migration_adds_column_to_legacy_db(tmp_path):
    """旧库（无 business_line 列）打开即自动补列（ALTER 幂等迁移）。"""
    import sqlite3
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """CREATE TABLE platform_accounts (
             id INTEGER PRIMARY KEY AUTOINCREMENT,
             platform TEXT NOT NULL, account_id TEXT NOT NULL,
             mode TEXT NOT NULL DEFAULT 'device',
             label TEXT NOT NULL DEFAULT '',
             proxy_id TEXT NOT NULL DEFAULT '',
             fingerprint_id TEXT NOT NULL DEFAULT '',
             status TEXT NOT NULL DEFAULT 'pending',
             meta_json TEXT NOT NULL DEFAULT '{}',
             created_at REAL NOT NULL DEFAULT 0,
             updated_at REAL NOT NULL DEFAULT 0,
             last_online_at REAL NOT NULL DEFAULT 0,
             UNIQUE(platform, account_id));
           INSERT INTO platform_accounts (platform, account_id)
             VALUES ('telegram', 'old1');"""
    )
    conn.commit()
    conn.close()
    reg = ar.AccountRegistry(db)
    row = reg.get("telegram", "old1")
    assert row is not None and row["business_line"] == ""
    assert reg.set_business_line("telegram", "old1", "translation") is True
    assert reg.get("telegram", "old1")["business_line"] == "translation"


def test_cached_business_line_ttl_and_invalidation():
    reg = ar._registry
    reg.upsert("telegram", "bl1", business_line="translation")
    assert ar.cached_business_line("telegram", "bl1") == "translation"
    # 写路径即时失效缓存
    reg.set_business_line("telegram", "bl1", "")
    assert ar.cached_business_line("telegram", "bl1") == ""
    # 未标注/不存在账号 → ''
    assert ar.cached_business_line("telegram", "ghost") == ""
    # 单例未初始化（如独立进程）→ '' 且不建库
    old = ar._registry
    ar._registry = None
    ar._BL_CACHE.clear()
    try:
        assert ar.cached_business_line("telegram", "bl1") == ""
    finally:
        ar._registry = old


# ── autodraft 账号级封顶 ──────────────────────────────────────────────────

def _cfg(**kw):
    base = dict(mode="auto_ai", min_len=0, skip=set(),
                platform_ceilings={}, skip_groups=False, enrich=False)
    base.update(kw)
    return AutoDraftConfig(**base)


def _run_cb(cfg, conv, store_mode=None):
    ds = MagicMock()
    ds.auto_generate_draft.return_value = "d1"
    store = MagicMock()
    store.get_automation_mode_if_set.return_value = store_mode
    cb = make_auto_draft_cb(cfg, ds, store, MagicMock(), MagicMock(),
                            MagicMock())
    cb(conv, "hello there friend")
    return ds


def test_translation_account_capped_to_review():
    ar._registry.upsert("telegram", "trans1", business_line="translation")
    ds = _run_cb(_cfg(), {"platform": "telegram", "account_id": "trans1",
                          "conversation_id": "c1"})
    ds.auto_generate_draft.assert_called_once()
    assert ds.auto_generate_draft.call_args.kwargs["automation_mode"] == "review"


def test_untagged_account_keeps_auto_ai():
    ar._registry.upsert("telegram", "plain1")  # 未标注
    ds = _run_cb(_cfg(), {"platform": "telegram", "account_id": "plain1",
                          "conversation_id": "c2"})
    assert ds.auto_generate_draft.call_args.kwargs["automation_mode"] == "auto_ai"


def test_companion_line_not_capped_by_default_map():
    ar._registry.upsert("telegram", "comp1", business_line="companion")
    ds = _run_cb(_cfg(), {"platform": "telegram", "account_id": "comp1",
                          "conversation_id": "c3"})
    assert ds.auto_generate_draft.call_args.kwargs["automation_mode"] == "auto_ai"


def test_conversation_manual_still_lowest():
    """会话显式 manual 比业务线封顶更低 → 仍然完全不拟稿。"""
    ar._registry.upsert("telegram", "trans2", business_line="translation")
    ds = _run_cb(_cfg(), {"platform": "telegram", "account_id": "trans2",
                          "conversation_id": "c4"}, store_mode="manual")
    ds.auto_generate_draft.assert_not_called()


def test_business_line_stacks_with_platform_ceiling():
    """平台封顶 multi_choice + 业务线 review → 取更低的 review。"""
    ar._registry.upsert("telegram", "trans3", business_line="translation")
    ds = _run_cb(
        _cfg(platform_ceilings={"telegram": "multi_choice"}),
        {"platform": "telegram", "account_id": "trans3",
         "conversation_id": "c5"})
    assert ds.auto_generate_draft.call_args.kwargs["automation_mode"] == "review"


def test_empty_ceiling_map_disables_capping():
    """显式置空 business_line_modes = 关闭业务线封顶（运维逃生门）。"""
    ar._registry.upsert("telegram", "trans4", business_line="translation")
    ds = _run_cb(_cfg(business_line_ceilings={}),
                 {"platform": "telegram", "account_id": "trans4",
                  "conversation_id": "c6"})
    assert ds.auto_generate_draft.call_args.kwargs["automation_mode"] == "auto_ai"


def test_setup_reads_business_line_modes_config():
    """setup_auto_draft 读 inbox.auto_draft.business_line_modes 进 cfg；
    未配置 → 内置默认 translation→review。"""
    from unittest.mock import patch
    from src.inbox.autodraft_helpers import setup_auto_draft

    captured = {}

    def _fake_make(cfg, *a, **kw):
        captured["cfg"] = cfg
        return lambda conv, text: None

    a = MagicMock()
    a.config.config = {"inbox": {"auto_draft": {"enabled": True}}}
    with patch("src.inbox.autodraft_helpers.make_auto_draft_cb",
               side_effect=_fake_make), \
         patch("src.inbox.autodraft_helpers.asyncio.get_running_loop",
               return_value=MagicMock()):
        setup_auto_draft(a, MagicMock(), MagicMock())
    assert captured["cfg"].business_line_ceilings == {"translation": "review"}

    a2 = MagicMock()
    a2.config.config = {"inbox": {"auto_draft": {
        "enabled": True,
        "business_line_modes": {"translation": "manual", "Companion": "REVIEW"},
    }}}
    with patch("src.inbox.autodraft_helpers.make_auto_draft_cb",
               side_effect=_fake_make), \
         patch("src.inbox.autodraft_helpers.asyncio.get_running_loop",
               return_value=MagicMock()):
        setup_auto_draft(a2, MagicMock(), MagicMock())
    assert captured["cfg"].business_line_ceilings == {
        "translation": "manual", "companion": "review"}


# ── proactive 过滤 ────────────────────────────────────────────────────────

def test_proactive_translation_account_blocked():
    from src.companion.proactive_topic import is_translation_account
    ar._registry.upsert("telegram", "trans5", business_line="translation")
    ar._registry.upsert("telegram", "comp5", business_line="companion")
    assert is_translation_account("telegram", "trans5") is True
    assert is_translation_account("telegram", "comp5") is False
    assert is_translation_account("telegram", "unknown") is False
