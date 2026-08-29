# -*- coding: utf-8 -*-
"""「无界科技·在线顾问」内置入口隐藏门禁（impl85 阶段4，工单 #30/#42/#45）。

客户拍板：「要你把无界科技在线顾问做隐藏，我不要手工配置」。语义：
- 客户形态（桌面壳）**默认隐藏** web 渠道入口（账号卡 + platform=web 会话，
  含库里的存量残留会话）；
- internal 形态（服务器/运维工作台，厂商自己的官网在线客服运营面）默认可见；
- ``web_chat.show_in_workspace`` 显式覆写永远优先（两个方向都行）；
- 隐藏是**工作台可见性**：widget 公网路由/落库照旧（``web_chat.enabled`` 管那个），
  历史留库不删。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.integrations.web_chat.service import web_entry_visible  # noqa: E402

CLIENT_CFG = {"ui_visibility": {"flavor": "client"},
              "web_chat": {"enabled": True, "title": "无界科技 · 在线顾问"}}
INTERNAL_CFG = {"ui_visibility": {"flavor": "internal"},
                "web_chat": {"enabled": True, "title": "无界科技 · 在线顾问"}}


def _req(cfg, store=None):
    state = SimpleNamespace(config_manager=SimpleNamespace(config=cfg),
                            inbox_store=store)
    return SimpleNamespace(app=SimpleNamespace(state=state))


class _FakeStore:
    def list_conversations(self, limit=30, platform=""):
        assert platform == "web"
        return [{"conversation_id": "web:web:v1", "platform": "web",
                 "account_id": "web", "chat_key": "v1",
                 "display_name": "访客 v1", "last_text": "hi",
                 "last_ts": 1.0, "unread": 0}]

    def count_messages(self, cid):
        return 1


# ── 判定纯函数 ───────────────────────────────────────────────────────────────

def test_client_flavor_hidden_by_default_internal_visible():
    assert web_entry_visible(CLIENT_CFG) is False      # 客户机零配置即隐藏（拍板）
    assert web_entry_visible(INTERNAL_CFG) is True     # 厂商运维面不受影响
    assert web_entry_visible({}) is True               # 服务器缺省=internal=可见


def test_explicit_override_wins_both_ways():
    show_on_client = {**CLIENT_CFG,
                      "web_chat": {"enabled": True, "show_in_workspace": True}}
    hide_on_server = {**INTERNAL_CFG,
                      "web_chat": {"enabled": True, "show_in_workspace": False}}
    assert web_entry_visible(show_on_client) is True
    assert web_entry_visible(hide_on_server) is False


# ── 账号卡（status）与 live 会话（collect_chats） ─────────────────────────────

def test_adapter_status_hides_consultant_card_on_client():
    from src.inbox.channel_adapters import WebInboxAdapter
    ad = WebInboxAdapter()
    assert ad.status(_req(CLIENT_CFG)) == {}           # 「已连接」卡不再出现
    st = ad.status(_req(INTERNAL_CFG))
    assert st and st["web_web"]["label"] == "无界科技 · 在线顾问"


def test_adapter_collect_hides_web_conversations_on_client():
    from src.inbox.channel_adapters import WebInboxAdapter
    ad = WebInboxAdapter()
    assert ad.collect_chats(_req(CLIENT_CFG, _FakeStore()), 30) == []
    rows = ad.collect_chats(_req(INTERNAL_CFG, _FakeStore()), 30)
    assert len(rows) == 1 and rows[0]["platform"] == "web"


# ── store-backed 列表出口（存量残留会话同样隐藏） ─────────────────────────────

def test_store_backed_listing_filters_residual_web_rows():
    from src.web.routes.unified_inbox_aggregate import _exclude_hidden_web_chats
    chats = [
        {"platform": "web", "conversation_id": "web:web:v1"},
        {"platform": "telegram", "conversation_id": "telegram:d:42"},
    ]
    out = _exclude_hidden_web_chats(_req(CLIENT_CFG), list(chats))
    assert [c["platform"] for c in out] == ["telegram"]
    keep = _exclude_hidden_web_chats(_req(INTERNAL_CFG), list(chats))
    assert len(keep) == 2                              # 可见形态原样保留
    assert _exclude_hidden_web_chats(_req(CLIENT_CFG), []) == []


def test_listing_pipeline_applies_filter_on_both_paths():
    src = (REPO / "src/web/routes/unified_inbox_aggregate.py").read_text(
        encoding="utf-8", errors="replace")
    # live 与 store 两条返回路径都必须过隐藏过滤（store 视图会带出存量残留）
    assert src.count("_exclude_hidden_web_chats(") >= 3


def test_widget_public_routes_not_touched():
    """隐藏是工作台可见性——widget 公网面（/chat*）绝不受 show_in_workspace 影响。"""
    src = (REPO / "src/web/routes/web_chat_routes.py").read_text(
        encoding="utf-8", errors="replace")
    assert "show_in_workspace" not in src
    assert "web_entry_visible" not in src
