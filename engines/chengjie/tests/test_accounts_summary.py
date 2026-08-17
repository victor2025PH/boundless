"""账号真相单源（P0 2026-08-17）契约钉：accounts_summary + store.account_directory。

事故背景：顶部账号 dock 显示 10 个、管理抽屉只有 5 个——dock 从会话窗口反推
「幽灵号」、抽屉只读 platform_status，两边各算一套。修复＝服务端一次收拢三层
账号（已接入 / 注册表非活跃 / 仅历史）为 ``accounts_summary``，dock「历史 N」
与抽屉历史分区同源消费。这里钉住：

1. ``InboxStore.account_directory``：全库 (platform, account_id) → {count, last_ts}；
2. ``_accounts_summary_list`` 纯函数：三层归并去重、状态归一
   （running→online / starting→connecting / error→error / 其余→offline；
   注册表 offline→logged_out、removed→removed）；
3. dead 账号（logged_out/removed）unread/attn 强制 0（不可回复的未读不亮灯），
   history_only 存量未读如实带出；
4. /chats 响应契约：全量分支带 ``accounts_summary`` 字段（静态接线钉）。
"""

from __future__ import annotations

import time

from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.store import InboxStore
from src.web.routes.unified_inbox_read_routes import _accounts_summary_list


def _seed_conv(store: InboxStore, plat: str, acct: str, ck: str, ts: float):
    cid = f"{plat}:{acct}:{ck}"
    store.ingest_batch(
        InboxConversation(conversation_id=cid, platform=plat, account_id=acct,
                          chat_key=ck, display_name=ck,
                          last_text="hi", last_ts=ts),
        [InboxMessage(conversation_id=cid, direction="in", text="hi", ts=ts)],
    )


# ── 1) store.account_directory ─────────────────────────────────────────────

def test_account_directory_counts_and_last_ts(tmp_path):
    s = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    _seed_conv(s, "telegram", "a1", "u1", now - 100)
    _seed_conv(s, "telegram", "a1", "u2", now - 50)
    _seed_conv(s, "line", "b1", "u3", now - 10)
    d = s.account_directory()
    assert d[("telegram", "a1")]["count"] == 2
    assert abs(d[("telegram", "a1")]["last_ts"] - (now - 50)) < 2
    assert d[("line", "b1")]["count"] == 1
    assert ("whatsapp", "zzz") not in d


def test_account_directory_empty_store(tmp_path):
    s = InboxStore(tmp_path / "inbox.db")
    assert s.account_directory() == {}


# ── 2) _accounts_summary_list 纯函数 ────────────────────────────────────────

def _ps(pl, aid, *, running=False, state=""):
    return {"platform": pl, "account_id": aid, "running": running, "state": state}


def test_summary_paired_status_normalization():
    ps = {
        "k1": _ps("telegram", "on1", running=True),
        "k2": _ps("telegram", "st1", state="starting"),
        "k3": _ps("line", "er1", state="error"),
        "k4": _ps("line", "of1", state="stopped"),
    }
    rows = _accounts_summary_list(ps, {}, {}, {}, {})
    by = {(r["platform"], r["account_id"]): r for r in rows}
    assert by[("telegram", "on1")]["status"] == "online"
    assert by[("telegram", "st1")]["status"] == "connecting"
    assert by[("line", "er1")]["status"] == "error"
    assert by[("line", "of1")]["status"] == "offline"
    assert all(r["paired"] for r in rows)


def test_summary_three_layers_merge_and_dedupe():
    ps = {"k1": _ps("telegram", "live", running=True)}
    status_map = {
        ("telegram", "live"): "offline",   # 与 platform_status 重复 → paired 胜出
        ("telegram", "out1"): "offline",   # 已退出
        ("line", "rm1"): "removed",        # 已移除
    }
    directory = {
        ("telegram", "live"): {"count": 9, "last_ts": 900.0},
        ("telegram", "out1"): {"count": 4, "last_ts": 400.0},
        ("telegram", "ghost"): {"count": 2, "last_ts": 200.0},  # 仅历史
    }
    rows = _accounts_summary_list(ps, status_map, directory, {}, {})
    by = {(r["platform"], r["account_id"]): r for r in rows}
    assert len(rows) == 4  # live/out1/rm1/ghost，无重复行
    assert by[("telegram", "live")]["paired"] and \
        by[("telegram", "live")]["status"] == "online"
    assert by[("telegram", "live")]["conv_count"] == 9  # paired 也带全库名录
    assert by[("telegram", "out1")]["status"] == "logged_out"
    assert by[("line", "rm1")]["status"] == "removed"
    assert by[("telegram", "ghost")]["status"] == "history_only"
    assert not by[("telegram", "ghost")]["paired"]


def test_summary_dead_unread_zero_history_unread_kept():
    status_map = {("telegram", "out1"): "offline", ("line", "rm1"): "removed"}
    directory = {
        ("telegram", "out1"): {"count": 3, "last_ts": 1.0},
        ("line", "rm1"): {"count": 2, "last_ts": 1.0},
        ("telegram", "ghost"): {"count": 5, "last_ts": 2.0},
    }
    unread = {"telegram:out1": 7, "line:rm1": 4, "telegram:ghost": 6}
    attn = {"telegram:out1": 1, "telegram:ghost": 2}
    rows = _accounts_summary_list({}, status_map, directory, unread, attn)
    by = {(r["platform"], r["account_id"]): r for r in rows}
    assert by[("telegram", "out1")]["unread"] == 0     # 不可回复的未读不亮灯
    assert by[("telegram", "out1")]["attn"] == 0
    assert by[("line", "rm1")]["unread"] == 0
    assert by[("telegram", "ghost")]["unread"] == 6    # 存量未读如实带出
    assert by[("telegram", "ghost")]["attn"] == 2


def test_summary_empty_inputs():
    assert _accounts_summary_list({}, {}, {}, {}, {}) == []


def test_summary_desktop_and_registered_not_history_only():
    """P4：注册表活跃但无 worker 的号不得坠成 history_only。"""
    directory = {
        ("telegram", "tg-desktop"): {"count": 7, "last_ts": 3.0},
        ("telegram", "idle1"): {"count": 2, "last_ts": 2.0},
        ("telegram", "ghost"): {"count": 1, "last_ts": 1.0},
    }
    unread = {"telegram:tg-desktop": 4, "telegram:idle1": 1, "telegram:ghost": 2}
    active = {
        ("telegram", "tg-desktop"): {"mode": "desktop", "label": "BOUNDLESS"},
        ("telegram", "idle1"): {"mode": "protocol", "label": ""},
    }
    rows = _accounts_summary_list({}, {}, directory, unread, {}, active)
    by = {(r["platform"], r["account_id"]): r for r in rows}
    assert by[("telegram", "tg-desktop")]["status"] == "desktop"
    assert by[("telegram", "tg-desktop")]["label"] == "BOUNDLESS"
    assert by[("telegram", "tg-desktop")]["unread"] == 4
    assert not by[("telegram", "tg-desktop")]["paired"]
    assert by[("telegram", "idle1")]["status"] == "registered"
    assert by[("telegram", "ghost")]["status"] == "history_only"
    assert by[("telegram", "ghost")]["unread"] == 2


def test_summary_paired_wins_over_registry_active():
    """已在 platform_status 的桌面号保持 paired，不重复成 desktop 行。"""
    ps = {"k1": _ps("telegram", "tg-desktop", running=True)}
    active = {("telegram", "tg-desktop"): {"mode": "desktop", "label": "X"}}
    rows = _accounts_summary_list(ps, {}, {}, {}, {}, active)
    assert len(rows) == 1
    assert rows[0]["paired"] and rows[0]["status"] == "online"


def test_directory_ghost_keys_skips_registry_and_web():
    from src.web.routes.unified_inbox_aggregate import directory_ghost_keys
    directory = {
        ("web", "web"): {"count": 9},
        ("telegram", "tg-desktop"): {"count": 7},
        ("telegram", "leak"): {"count": 1},
    }
    ghosts = directory_ghost_keys(
        directory, {("telegram", "tg-desktop")})
    assert ghosts == [("telegram", "leak")]


# ── 3) /chats 响应契约（静态接线钉：别让字段被后续重构悄悄删掉） ─────────────

def test_chats_response_wires_accounts_summary():
    import inspect
    import src.web.routes.unified_inbox_read_routes as mod
    src = inspect.getsource(mod)
    assert '"accounts_summary": accounts_summary' in src, \
        "/chats 全量分支必须携带 accounts_summary（dock/抽屉单源依赖此字段）"


# ── 4) P2：store.mark_account_read（历史号存量未读批量清账） ─────────────────

def _seed_unread(store: InboxStore, plat: str, acct: str, ck: str,
                 ts: float, unread: int):
    cid = f"{plat}:{acct}:{ck}"
    store.ingest_batch(
        InboxConversation(conversation_id=cid, platform=plat, account_id=acct,
                          chat_key=ck, display_name=ck,
                          last_text="hi", last_ts=ts, unread=unread),
        [InboxMessage(conversation_id=cid, direction="in", text="hi", ts=ts)],
    )


def test_mark_account_read_batch_and_scope(tmp_path):
    s = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    _seed_unread(s, "telegram", "g1", "u1", now - 10, 3)
    _seed_unread(s, "telegram", "g1", "u2", now - 5, 7)
    _seed_unread(s, "telegram", "g2", "u9", now - 8, 2)   # 别的账号不许被波及
    assert s.mark_account_read("telegram", "g1") == 2
    for ck in ("u1", "u2"):
        row = s.get_conversation(f"telegram:g1:{ck}")
        assert s.effective_unread(row) == 0, "水位推进后有效未读必须归零"
    other = s.get_conversation("telegram:g2:u9")
    assert s.effective_unread(other) == 2, "只清目标账号，不许殃及别号"


def test_mark_account_read_idempotent_and_guards(tmp_path):
    s = InboxStore(tmp_path / "inbox.db")
    _seed_unread(s, "line", "b1", "u1", time.time() - 3, 4)
    assert s.mark_account_read("line", "b1") == 1
    assert s.mark_account_read("line", "b1") == 0, "二次调用无行可推（幂等）"
    assert s.mark_account_read("", "") == 0
    assert s.mark_account_read("line", "") == 0


# ── 5) P1：软删账号恢复（注册表语义 + 路由静态接线钉） ──────────────────────

def test_registry_restore_semantics(tmp_path):
    from src.integrations.account_registry import AccountRegistry
    reg = AccountRegistry(tmp_path / "reg.db")
    reg.upsert("telegram", "a1", status="offline")
    reg.remove("telegram", "a1")
    assert reg.get("telegram", "a1")["status"] == "removed"
    reg.set_status("telegram", "a1", "offline")   # restore 路由的底层操作
    assert reg.get("telegram", "a1")["status"] == "offline", \
        "恢复后回「已退出可重登」态"


def test_restore_and_mark_account_read_routes_wired():
    import inspect
    import src.web.routes.unified_inbox_account_routes as acct_mod
    import src.web.routes.unified_inbox_read_routes as read_mod
    acct_src = inspect.getsource(acct_mod)
    assert "/api/accounts/{platform}/{account_id}/restore" in acct_src
    assert "err.ws.account_not_removed" in acct_src, \
        "非 removed 号恢复必须 409 而非静默成功"
    read_src = inspect.getsource(read_mod)
    assert "/api/unified-inbox/mark-account-read" in read_src
    assert "mark_account_read" in read_src
