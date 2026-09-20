"""Q-23 硬名单的显式对端豁免 ``inbox.peer_bot_guard.never_auto_reply.exempt_peers``（2026-09-19）。

背景：全自动教学片的两方舞台都是本租户账号（Katie 扮客户 → 智聊支持），零配置推导把 Katie
拦成 ``colleague:<id>``，A/B 两线都不拟稿不真发。豁免只作用于「作为对端」一侧：豁免账号自己的
镜像会话里对端仍是同事，仍拦——两侧不会互相自动回成死循环。默认空 = 行为不变。
"""
from __future__ import annotations

import pytest

from src.inbox import peer_bot_guard as pbg


def _cfg(exempt=None, accounts=("8244899900", "6834964252"), groups=("-1004345824259",)):
    node = {"accounts": list(accounts), "groups": list(groups)}
    if exempt is not None:
        node["exempt_peers"] = exempt
    return {"inbox": {"peer_bot_guard": {"enabled": True, "never_auto_reply": node}}}


@pytest.fixture(autouse=True)
def _fresh_cache():
    pbg._invalidate_never_auto_cache()
    yield
    pbg._invalidate_never_auto_cache()


def test_default_no_exempt_keeps_blocking_colleague():
    cfg = _cfg()
    r = pbg.never_auto_reply_reason(cfg, platform="telegram", account_id="6834964252",
                                    chat_key="8244899900", sender_id="8244899900")
    assert r == "colleague:8244899900"
    assert pbg.never_auto_reply_ids(cfg)["exempt"] == frozenset()


def test_exempt_peer_passes_only_as_peer():
    cfg = _cfg(exempt=["8244899900"])
    # 卖家账号视角：对端 Katie 被豁免 → 放行（全自动可真发）
    assert pbg.never_auto_reply_reason(cfg, platform="telegram", account_id="6834964252",
                                       chat_key="8244899900", sender_id="8244899900") == ""
    # Katie 自己的镜像会话：对端是卖家（未豁免）→ 仍拦，不会互相自动回
    assert pbg.never_auto_reply_reason(cfg, platform="telegram", account_id="8244899900",
                                       chat_key="6834964252", sender_id="6834964252") == "colleague:6834964252"


def test_exempt_accepts_csv_and_at_prefix():
    cfg = _cfg(exempt="@8244899900, other_id")
    assert pbg.never_auto_reply_ids(cfg)["exempt"] == frozenset({"8244899900", "other_id"})
    assert pbg.never_auto_reply_reason(cfg, platform="telegram", account_id="6834964252",
                                       chat_key="8244899900") == ""


def test_exempt_does_not_open_ops_groups():
    cfg = _cfg(exempt=["8244899900", "-1004345824259"])
    # 群名单不受豁免影响：Katie 在报障群里发言仍是 ops_group
    assert pbg.never_auto_reply_reason(cfg, platform="telegram", account_id="6834964252",
                                       chat_key="-1004345824259", sender_id="8244899900") == "ops_group:-1004345824259"


def test_b_line_guard_passes_exempt_peer(monkeypatch):
    """B 线 guard_auto_draft_action：豁免后不再在硬名单层 return，往下走常规判定（enabled=False → 放行）。"""
    pbg._reset_for_tests()
    cfg = {"inbox": {"peer_bot_guard": {"enabled": False,
                                        "never_auto_reply": {"accounts": ["8244899900"], "exempt_peers": ["8244899900"]}}}}
    conv = {"conversation_id": "telegram:6834964252:8244899900", "platform": "telegram",
            "account_id": "6834964252", "chat_key": "8244899900", "sender_id": "8244899900"}
    reason, soft = pbg.guard_auto_draft_action(conv=conv, store=None, config=cfg)
    assert (reason, soft) == ("", False)
    assert not pbg.stats_snapshot()["suppressed"].get("never_auto_reply")
    pbg._reset_for_tests()
