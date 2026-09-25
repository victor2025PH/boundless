# -*- coding: utf-8 -*-
"""统一收件箱会话列表 scoped 查询（按平台/账号过滤）。

背景根因：前端全局轮询 /api/unified-inbox/chats 只拿最近 100 条（live 聚合快照 +
top100 截断），点开某账号时其老会话不在窗口内 → 「点账号看不到老会话」。
修复＝chats API 支持 platform/account_id scoped 查询（直查 InboxStore 事实源，
绕过全局截断），翻页（before_ts）同口径透传；无参数请求响应逐字段不变（零回归）。

fixture 风格与 test_inbox_readpath_a1 / test_inbox_list_pagination 同源：
自建 FastAPI app + 临时 InboxStore，不依赖常驻服务。
"""

from __future__ import annotations

import time

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.inbox.models import InboxConversation
from src.inbox.store import InboxStore
from src.web.routes.unified_inbox_read_routes import register_read_routes


def _client(store):
    """最小 chats API 客户端：只挂主读路径路由 + 临时 store（无 live 服务）。"""
    app = FastAPI()

    def api_auth(request: Request):
        return True

    register_read_routes(app, api_auth=api_auth, config_manager=None)
    app.state.inbox_store = store
    return TestClient(app)


def _seed_store(tmp_path, name="inbox.db"):
    """多平台×多账号会话池（i 全局递增 → last_ts 递减，i 越大越旧）。

    telegram:tg-a ×4（最新）、telegram:tg-b ×3、line:line-a ×3、
    whatsapp:WA-Mix ×2（账号 id 含大小写，验证 account_id 过滤保大小写）。
    """
    store = InboxStore(tmp_path / name)
    base = time.time()
    i = 0
    for platform, acct, n in [
        ("telegram", "tg-a", 4),
        ("telegram", "tg-b", 3),
        ("line", "line-a", 3),
        ("whatsapp", "WA-Mix", 2),
    ]:
        for k in range(n):
            store.upsert_conversation(InboxConversation(
                conversation_id=f"{platform}:{acct}:{k}",
                platform=platform, account_id=acct, chat_key=str(k),
                display_name=f"{acct}-客户{k}",
                last_text=f"msg {acct} {k}",
                last_ts=base - i * 60,
            ))
            i += 1
    return store, base


# ── ⑥ store 层：account_id 过滤单测 ─────────────────────────────────────────

def test_store_list_conversations_account_id_filter(tmp_path):
    store, _ = _seed_store(tmp_path)
    # account_id 单独过滤
    rows = store.list_conversations(limit=50, account_id="tg-a")
    assert len(rows) == 4
    assert all(r["account_id"] == "tg-a" for r in rows)
    # platform + account_id 组合
    both = store.list_conversations(limit=50, platform="telegram", account_id="tg-b")
    assert len(both) == 3
    assert all(r["platform"] == "telegram" and r["account_id"] == "tg-b"
               for r in both)
    # account_id 大小写敏感（保大小写）
    assert len(store.list_conversations(limit=50, account_id="WA-Mix")) == 2
    assert store.list_conversations(limit=50, account_id="wa-mix") == []
    # 无参数 = 旧行为全量
    assert len(store.list_conversations(limit=50)) == 12
    store.close()


def test_store_count_older_than_account_id_filter(tmp_path):
    store, base = _seed_store(tmp_path)
    # 以 tg-a 第二新（base-60）为界：更旧的 = tg-a 2 条 + tg-b 3 + line-a 3 + WA-Mix 2
    boundary = base - 60
    assert store.count_conversations_older_than(boundary) == 10
    assert store.count_conversations_older_than(boundary, platform="telegram") == 5
    assert store.count_conversations_older_than(boundary, account_id="tg-a") == 2
    assert store.count_conversations_older_than(
        boundary, platform="telegram", account_id="tg-a") == 2
    assert store.count_conversations_older_than(boundary, account_id="nope") == 0
    store.close()


# ── ① platform 过滤 ────────────────────────────────────────────────────────

def test_chats_platform_filter_returns_only_platform(tmp_path):
    store, _ = _seed_store(tmp_path)
    c = _client(store)
    d = c.get("/api/unified-inbox/chats?platform=telegram").json()
    assert d["ok"] is True
    assert len(d["chats"]) == 7  # tg-a 4 + tg-b 3
    assert all(r["platform"] == "telegram" for r in d["chats"])
    assert all(r.get("from_store") for r in d["chats"])  # 直查 store 事实源
    assert d["scope"] == {"platform": "telegram", "account_id": ""}
    # platform 规范化：大写入参 → strip().lower() 后同结果
    d2 = c.get("/api/unified-inbox/chats?platform=TELEGRAM").json()
    assert len(d2["chats"]) == 7
    assert d2["scope"]["platform"] == "telegram"
    store.close()


# ── ② account_id 过滤 ──────────────────────────────────────────────────────

def test_chats_account_filter_returns_only_account(tmp_path):
    store, _ = _seed_store(tmp_path)
    c = _client(store)
    d = c.get("/api/unified-inbox/chats?account_id=tg-a").json()
    assert d["ok"] is True
    assert len(d["chats"]) == 4
    assert all(r["account_id"] == "tg-a" for r in d["chats"])
    assert d["scope"] == {"platform": "", "account_id": "tg-a"}
    # account_id 保大小写：混合大小写账号按原样命中
    dm = c.get("/api/unified-inbox/chats?account_id=WA-Mix").json()
    assert len(dm["chats"]) == 2
    assert dm["scope"]["account_id"] == "WA-Mix"
    store.close()


# ── ③ platform + account 组合 ──────────────────────────────────────────────

def test_chats_platform_and_account_combo(tmp_path):
    store, _ = _seed_store(tmp_path)
    c = _client(store)
    d = c.get(
        "/api/unified-inbox/chats?platform=telegram&account_id=tg-b").json()
    assert len(d["chats"]) == 3
    assert all(r["platform"] == "telegram" and r["account_id"] == "tg-b"
               for r in d["chats"])
    assert d["scope"] == {"platform": "telegram", "account_id": "tg-b"}
    # 组合不匹配（平台对、账号属于另一平台）→ 空集但 ok
    dx = c.get(
        "/api/unified-inbox/chats?platform=telegram&account_id=line-a").json()
    assert dx["ok"] is True and dx["chats"] == []
    store.close()


# ── ④ 响应形状：scoped 带 scope 不带 platform_status；无参数版反之 ───────────

def test_scoped_shape_vs_unscoped_shape(tmp_path):
    store, _ = _seed_store(tmp_path)
    c = _client(store)
    scoped = c.get("/api/unified-inbox/chats?platform=line").json()
    # scoped：带 scope，刻意不带 platform_status（省 orchestrator 聚合开销）；
    # hidden_included＝历史只读视角特性探测标记（2026-08-17，前端据此区分
    # 「后端已放行隐藏行」vs「旧后端忽略了 include_hidden 参数」）
    assert set(scoped.keys()) == {"ok", "ts", "chats", "has_more",
                                  "oldest_ts", "scope", "hidden_included",
                                  # 账号视角同样要能画「真发已暂停」黄态
                                  "deliver_paused", "deliver_paused_meta"}
    # 未显式带 include_hidden / 无 account_id scoped → 标记必须为 False（默认口径不变）
    assert scoped["hidden_included"] is False
    # 无参数：与现有响应逐字段一致（零回归），含 platform_status、无 scope
    plain = c.get("/api/unified-inbox/chats").json()
    assert set(plain.keys()) == {
        "ok", "ts", "chats", "platform_status", "has_more", "oldest_ts",
        "unread_by_platform", "unread_by_account",  # P6 全库有效未读聚合
        "attn_by_account",                          # P8 近窗需人工聚合
        "accounts_summary",                         # P0 账号真相单源（2026-08-17）
        "archived_unread_by_account",               # 归档里还有未读
        "unread_breakdown_by_account",              # 四桶明细（store 能算时才带）
        "ai_skip_groups",                           # 群/频道是否跳过拟稿
        "deliver_paused", "deliver_paused_meta",    # 全局真发暂停
    }
    assert isinstance(plain["unread_by_platform"], dict)
    assert isinstance(plain["unread_by_account"], dict)
    assert isinstance(plain["attn_by_account"], dict)
    assert plain["ok"] is True
    store.close()


# ── ⑤ scoped 翻页（before_ts + has_more 同口径）────────────────────────────

def test_scoped_pagination_before_ts_and_has_more(tmp_path):
    store = InboxStore(tmp_path / "page.db")
    base = time.time()
    # 目标账号 25 条（i 越大越旧）；干扰账号 5 条穿插同一时间带，验证翻页不串号
    for i in range(25):
        store.upsert_conversation(InboxConversation(
            conversation_id=f"telegram:tg-page:{i}", platform="telegram",
            account_id="tg-page", chat_key=str(i),
            display_name=f"P{i}", last_text=f"m{i}", last_ts=base - i * 60))
    for i in range(5):
        store.upsert_conversation(InboxConversation(
            conversation_id=f"telegram:tg-other:o{i}", platform="telegram",
            account_id="tg-other", chat_key=f"o{i}",
            display_name=f"O{i}", last_text=f"om{i}", last_ts=base - i * 60 - 30))
    c = _client(store)
    # scoped 首页：limit=5 → store 实取 min(200, 5*4)=20 条 → 还剩 5 条更旧
    p1 = c.get("/api/unified-inbox/chats"
               "?platform=telegram&account_id=tg-page&limit=5").json()
    assert len(p1["chats"]) == 20
    assert all(r["account_id"] == "tg-page" for r in p1["chats"])
    assert p1["has_more"] is True          # scoped 口径：tg-page 还有 5 条更旧
    assert p1["oldest_ts"] == min(float(r["last_ts"]) for r in p1["chats"])
    # scoped 翻页：before_ts=首页最旧 → 拿剩余 5 条，且不混入 tg-other
    p2 = c.get(
        "/api/unified-inbox/chats?platform=telegram&account_id=tg-page"
        f"&limit=5&before_ts={p1['oldest_ts']}").json()
    assert len(p2["chats"]) == 5
    assert all(r["account_id"] == "tg-page" for r in p2["chats"])
    assert p2["has_more"] is False
    # 不重不漏：两页无交集，合起来恰是 25 条
    ids1 = {r["conversation_id"] for r in p1["chats"]}
    ids2 = {r["conversation_id"] for r in p2["chats"]}
    assert not ids1 & ids2
    assert len(ids1 | ids2) == 25
    store.close()


def test_unscoped_pagination_unchanged(tmp_path):
    """回归钉：无 scope 参数的 before_ts 翻页行为与旧版一致（全局口径）。"""
    store, base = _seed_store(tmp_path)
    c = _client(store)
    # 以 tg-a 第二新（base-60）为界：全局更旧 10 条全部返回（limit=5 → 实取 20）
    d = c.get(f"/api/unified-inbox/chats?limit=5&before_ts={base - 60}").json()
    assert len(d["chats"]) == 10
    assert d["has_more"] is False
    assert set(d.keys()) == {"ok", "ts", "chats", "has_more", "oldest_ts"}
    store.close()


# ── ⑦ scoped 空集 → ok + 空 chats ──────────────────────────────────────────

def test_scoped_empty_result_ok(tmp_path):
    store, _ = _seed_store(tmp_path)
    c = _client(store)
    d = c.get("/api/unified-inbox/chats?platform=messenger").json()
    assert d["ok"] is True
    assert d["chats"] == []
    assert d["has_more"] is False
    assert d["oldest_ts"] is None
    assert d["scope"] == {"platform": "messenger", "account_id": ""}
    store.close()


# ── 回落护栏：store 不可用时 scoped 请求走全量路径（绝不 500）────────────────

def test_scoped_without_store_falls_back_to_full_path():
    c = _client(store=None)   # app.state.inbox_store = None → store 不可用
    d = c.get("/api/unified-inbox/chats?platform=telegram&account_id=tg-a")
    assert d.status_code == 200
    body = d.json()
    assert body["ok"] is True
    # 回落响应形状与无参数版完全一致（带 platform_status / 聚合字段、无 scope）
    assert set(body.keys()) == {
        "ok", "ts", "chats", "platform_status", "has_more", "oldest_ts",
        "unread_by_platform", "unread_by_account", "attn_by_account",
        "accounts_summary",   # P0 账号真相单源（2026-08-17）
        "archived_unread_by_account",
        "ai_skip_groups",
        "deliver_paused", "deliver_paused_meta",
    }
