# -*- coding: utf-8 -*-
"""告警链路自检只读 API（/api/admin/alert-link-status）+ 配套地基的门禁。

三层被钉：
1. **路由 verdict 分档**：no_channel（0 启用通道，今日生产现状）/ divergent（文件与
   运行中 notifier 指纹不一致——手改文件绕过面板的形态）/ uncovered（有通道但关注
   别名有洞）/ healthy（真接通）。响应**零密钥**（token/url/target 明文不得出现）。
2. **配置指纹**（alert_link_audit.config_fingerprint）：events 顺序无关、密钥轮换
   不改指纹（只记 *_set 布尔）、结构性变更（名字/enabled/events）必改指纹。
3. **store mtime 失效缓存**（notify_webhooks_store.load）：手改文件下一次 load 读到
   新内容、删文件回 None——「文件真相」与磁盘一致，分歧检测才有意义（2026-08-02 前
   _cache 一经装载永不失效，正是自检会说谎的形态）。
"""
import json
import os

import pytest

from src.integrations import notify_webhooks_store as store
from src.integrations.alert_link_audit import config_fingerprint


@pytest.fixture()
def tmp_store(tmp_path):
    """把 webhook store 指到临时文件，测试后复位默认锚定。"""
    p = tmp_path / "notify_webhooks.json"
    store.set_store_path(p)
    yield p
    store.set_store_path(None)


def _mk(name="boss-tg", events=None, enabled=True, token="secret-token-123"):
    return {
        "name": name,
        "format": "telegram",
        "url": "",
        "token": token,
        "target": "-1001234",
        "events": events if events is not None else ["all"],
        "enabled": enabled,
    }


# ── 1. 路由 verdict 分档 ─────────────────────────────────────────────────────

def _get(auth_client):
    r = auth_client.get("/api/admin/alert-link-status")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d.get("ok") is True
    return d


def test_route_no_channel_is_the_shipped_default(auth_client, tmp_store):
    store.save_list([])  # 显式空 overlay：0 启用通道（与本机生产现状同形态）
    d = _get(auth_client)
    assert d["verdict"] == "no_channel"
    assert d["audit"]["has_any_channel"] is False
    assert d["audit"]["uncovered"] == d["audit"]["focus_aliases"]
    assert d["store"]["source"] == "overlay"


def test_route_healthy_when_covered_and_process_in_sync(auth_client, tmp_store):
    saved = store.save_list([_mk(events=["all"])])
    from src.inbox.webhook_notifier import WebhookNotifier
    n = WebhookNotifier(config=saved)
    n._running = True  # 模拟事件循环在消费（run() 需真 loop，这里只验判定逻辑）
    auth_client.app.state.webhook_notifier = n
    try:
        d = _get(auth_client)
        assert d["verdict"] == "healthy"
        assert d["audit"]["healthy"] is True
        assert d["divergence"] is False
        assert d["process"]["config_fp"] == d["file_fp"]
    finally:
        auth_client.app.state.webhook_notifier = None


def test_route_divergent_when_process_loaded_a_different_config(auth_client, tmp_store):
    store.save_list([_mk(events=["all"])])
    from src.inbox.webhook_notifier import WebhookNotifier
    n = WebhookNotifier(config=[])  # 进程装的是空配置（如手改文件后未热更的镜像形态）
    n._running = True
    auth_client.app.state.webhook_notifier = n
    try:
        d = _get(auth_client)
        assert d["verdict"] == "divergent"
        assert d["divergence"] is True
    finally:
        auth_client.app.state.webhook_notifier = None


def test_route_uncovered_when_focus_alias_has_no_subscriber(auth_client, tmp_store):
    saved = store.save_list([_mk(events=["draft_backlog"])])  # 只订一个关注别名
    from src.inbox.webhook_notifier import WebhookNotifier
    n = WebhookNotifier(config=saved)
    n._running = True
    auth_client.app.state.webhook_notifier = n
    try:
        d = _get(auth_client)
        assert d["verdict"] == "uncovered"
        assert "draft_backlog" not in d["audit"]["uncovered"]
        assert "host_alert" in d["audit"]["uncovered"]
    finally:
        auth_client.app.state.webhook_notifier = None


def test_route_response_leaks_no_secrets(auth_client, tmp_store):
    store.save_list([_mk(token="super-secret-token-abcdef")])
    r = auth_client.get("/api/admin/alert-link-status")
    assert "super-secret-token-abcdef" not in r.text
    assert "-1001234" not in r.text  # target（群地址）也不得出现


# ── 2. 配置指纹性质 ─────────────────────────────────────────────────────────

def test_fingerprint_events_order_insensitive_and_secret_blind():
    a = _mk(events=["draft_backlog", "host_alert"])
    b = _mk(events=["host_alert", "draft_backlog"])
    assert config_fingerprint([a]) == config_fingerprint([b])
    # 密钥轮换（同名同结构）不改指纹——指纹只记 *_set 布尔，输入面零敏感
    c = _mk(token="rotated-token-999")
    assert config_fingerprint([_mk()]) == config_fingerprint([c])


def test_fingerprint_changes_on_structural_edits():
    base = config_fingerprint([_mk()])
    assert config_fingerprint([_mk(name="other")]) != base
    assert config_fingerprint([_mk(enabled=False)]) != base
    assert config_fingerprint([_mk(events=["host_alert"])]) != base
    assert config_fingerprint([]) != base


# ── 3. store mtime 失效缓存 ──────────────────────────────────────────────────

def test_load_sees_manual_file_edit(tmp_store):
    store.save_list([_mk(name="first")])
    assert [w["name"] for w in store.load()] == ["first"]
    # 手改文件（绕过面板）：mtime 强制前推，防同 tick 写入共享 mtime
    tmp_store.write_text(json.dumps([_mk(name="second")]), encoding="utf-8")
    st = tmp_store.stat()
    os.utime(tmp_store, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
    assert [w["name"] for w in store.load()] == ["second"]


def test_load_returns_none_after_file_deleted(tmp_store):
    store.save_list([_mk()])
    assert store.load() is not None
    tmp_store.unlink()
    assert store.load() is None  # 旧实现会返回陈旧缓存——覆盖层已不存在必须如实说


def test_load_unchanged_file_serves_cache(tmp_store):
    store.save_list([_mk(name="cached")])
    one = store.load()
    two = store.load()
    assert one == two and [w["name"] for w in two] == ["cached"]


def test_store_path_accessor_follows_override(tmp_store):
    assert store.store_path() == tmp_store
    store.set_store_path(None)
    assert store.store_path().name == "notify_webhooks.json"
    store.set_store_path(tmp_store)  # 交还 fixture 复位
