"""purge-history 路由端到端黑盒（历史账号治理 P0 合流验证，2026-08-17）。

姊妹文件 ``test_account_purge_history.py`` 钉的是原语（``registry.delete_row`` /
``store.purge_account_conversations`` / ``_collect_chats_from_store``）与路由
**源码**静态接线；本文件把真 ``register_account_routes`` 挂上最小 app，从 HTTP
面补钉「接了且真的生效」——本仓历史上「静态钉全绿、行为却断链」的教训
（人工通过只标记不发送、removed 标记恒空集）正是这类缝隙。

覆盖：

1. ``history_only``（注册表无行、只活在会话库）→ 200：数据清空、账号名录
   条目消失、``registry_deleted=false``；
2. ``removed`` 软删残行 → 200：连注册表行一起硬删（``registry_deleted=true``），
   「已移除 · 0 会话」死行不再可能；``offline``（已退出）同径一步到位；
3. 活跃在册（online）→ 409：会话数据一行不许少（防误删正在服务的号）；
4. config.yaml 声明的常驻号 → 409（删了下次启动又复活，语义上不是历史号）；
5. 内置合成号（web 工作台）→ 409；
6. 幂等：同号重复 purge → 200 且 ``purged_rows=0``（重复点不报错）；
7. viewer 角色 → 403（账号管理写口硬闸 ``_require_account_manager``）。

注册表/ops_events 均由 conftest autouse 夹具隔离到 tmp，不碰生产库。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.store import InboxStore
from src.integrations.account_registry import get_account_registry


def _seed_conv(store: InboxStore, plat: str, acct: str, ck: str, ts: float):
    cid = f"{plat}:{acct}:{ck}"
    store.ingest_batch(
        InboxConversation(conversation_id=cid, platform=plat, account_id=acct,
                          chat_key=ck, display_name=ck,
                          last_text="hi", last_ts=ts),
        [InboxMessage(conversation_id=cid, direction="in", text="hi", ts=ts)],
    )
    return cid


def _client(store: InboxStore, cfg=None, *, with_session: bool = False) -> TestClient:
    """最小 app：真路由注册 + tmp store；api_auth 放行（授权闸另测 403 用例）。"""
    from src.web.routes.unified_inbox_account_routes import register_account_routes

    app = FastAPI()
    if with_session:
        app.add_middleware(SessionMiddleware, secret_key="t")

        @app.post("/_t/role")
        async def _t_role(request: Request):  # 测试专用：把角色写进会话
            body = await request.json()
            request.session["role"] = str((body or {}).get("role") or "")
            return {"ok": True}

    register_account_routes(app, api_auth=lambda request: None,
                            config_manager=SimpleNamespace(config=cfg or {}))
    app.state.inbox_store = store
    return TestClient(app)


def _purge(c: TestClient, plat: str, acct: str):
    return c.post(f"/api/accounts/{plat}/{acct}/purge-history")


# ── 1) history_only：数据清空 + 名录消失，无注册表行可删 ────────────────────

def test_purge_history_only_account(tmp_path):
    s = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    _seed_conv(s, "telegram", "ghost1", "u1", now - 10)
    _seed_conv(s, "telegram", "keep1", "u2", now - 5)
    c = _client(s)
    r = _purge(c, "telegram", "ghost1")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True
    assert d["purged_rows"] > 0
    assert d["registry_deleted"] is False
    dirs = s.account_directory()
    assert ("telegram", "ghost1") not in dirs, "清库后历史条目必须自然消失"
    assert ("telegram", "keep1") in dirs, "旁号数据不许被株连"
    s.close()


# ── 2) removed / offline 注册表残行：数据 + 行一并清 ────────────────────────

def test_purge_removed_row_hard_deletes_registry(tmp_path):
    s = InboxStore(tmp_path / "inbox.db")
    reg = get_account_registry()
    reg.upsert("telegram", "gone1", mode="protocol")
    reg.set_status("telegram", "gone1", "removed")
    _seed_conv(s, "telegram", "gone1", "u1", time.time() - 10)
    c = _client(s)
    r = _purge(c, "telegram", "gone1")
    assert r.status_code == 200
    d = r.json()
    assert d["ok"] is True and d["registry_deleted"] is True
    assert reg.get("telegram", "gone1") is None, \
        "removed 残行必须硬删，否则历史区留「已移除 · 0 会话」死行"
    assert ("telegram", "gone1") not in s.account_directory()
    s.close()


def test_purge_offline_row_allowed_and_deleted(tmp_path):
    s = InboxStore(tmp_path / "inbox.db")
    reg = get_account_registry()
    reg.upsert("telegram", "out1", mode="protocol")
    reg.set_status("telegram", "out1", "offline")
    _seed_conv(s, "telegram", "out1", "u1", time.time() - 10)
    c = _client(s)
    r = _purge(c, "telegram", "out1")
    assert r.status_code == 200
    assert r.json()["registry_deleted"] is True
    assert reg.get("telegram", "out1") is None
    s.close()


# ── 3) 活跃在册：409 且数据毫发无损 ─────────────────────────────────────────

def test_purge_active_account_refused_data_intact(tmp_path):
    s = InboxStore(tmp_path / "inbox.db")
    reg = get_account_registry()
    reg.upsert("telegram", "live1", mode="protocol", status="online")
    cid = _seed_conv(s, "telegram", "live1", "u1", time.time() - 10)
    c = _client(s)
    r = _purge(c, "telegram", "live1")
    assert r.status_code == 409, "在线号必须拒绝（先登出/移除才许删历史）"
    assert ("telegram", "live1") in s.account_directory(), "409 后数据一行不许少"
    assert s.count_messages(cid) == 1
    assert reg.get("telegram", "live1") is not None
    s.close()


def test_purge_pending_account_refused(tmp_path):
    """pending/connecting 等中间态同属活跃语义：一样拒绝。"""
    s = InboxStore(tmp_path / "inbox.db")
    reg = get_account_registry()
    reg.upsert("telegram", "mid1", mode="protocol", status="pending")
    _seed_conv(s, "telegram", "mid1", "u1", time.time() - 10)
    c = _client(s)
    assert _purge(c, "telegram", "mid1").status_code == 409
    s.close()


# ── 4) config 声明的常驻号：409（删了会复活） ───────────────────────────────

def test_purge_config_declared_account_refused(tmp_path):
    s = InboxStore(tmp_path / "inbox.db")
    _seed_conv(s, "telegram", "cfgacct", "u1", time.time() - 10)
    cfg = {"telegram": {"accounts": [{"id": "cfgacct"}]}}
    c = _client(s, cfg=cfg)
    r = _purge(c, "telegram", "cfgacct")
    assert r.status_code == 409, "config 常驻号不是历史号，从这里删只会复活"
    assert ("telegram", "cfgacct") in s.account_directory()
    s.close()


# ── 5) 内置合成号：409 ──────────────────────────────────────────────────────

def test_purge_synthetic_platform_refused(tmp_path):
    s = InboxStore(tmp_path / "inbox.db")
    _seed_conv(s, "web", "workspace", "u1", time.time() - 10)
    c = _client(s)
    assert _purge(c, "web", "workspace").status_code == 409
    assert ("web", "workspace") in s.account_directory()
    s.close()


# ── 6) 幂等：重复删返回成功 + 零行 ──────────────────────────────────────────

def test_purge_idempotent_second_call(tmp_path):
    s = InboxStore(tmp_path / "inbox.db")
    _seed_conv(s, "telegram", "ghost2", "u1", time.time() - 10)
    c = _client(s)
    assert _purge(c, "telegram", "ghost2").status_code == 200
    r2 = _purge(c, "telegram", "ghost2")
    assert r2.status_code == 200, "已空/已删过必须按成功返回（重复点不报错）"
    d2 = r2.json()
    assert d2["ok"] is True and d2["purged_rows"] == 0
    assert d2["registry_deleted"] is False
    s.close()


# ── 7) viewer 角色：403 写口硬闸（删/预览/导出同闸） ────────────────────────

def test_purge_denied_for_viewer_role(tmp_path):
    s = InboxStore(tmp_path / "inbox.db")
    _seed_conv(s, "telegram", "ghost3", "u1", time.time() - 10)
    c = _client(s, with_session=True)
    assert c.post("/_t/role", json={"role": "viewer"}).status_code == 200
    r = _purge(c, "telegram", "ghost3")
    assert r.status_code == 403, "viewer 是只读观察员，删历史必须被写口硬闸拦下"
    assert ("telegram", "ghost3") in s.account_directory()
    # P1：预览与导出同属敏感面（体量侦察 / 数据外携），同闸拒绝
    assert c.get("/api/accounts/telegram/ghost3/purge-history").status_code == 403
    assert c.get("/api/accounts/telegram/ghost3/export-history").status_code == 403
    s.close()


# ══ P1（2026-08-17）：删除前体量预估 + JSONL 导出 ═══════════════════════════

def test_count_account_data_semantics(tmp_path):
    s = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    _seed_conv(s, "telegram", "a1", "u1", now - 30)
    _seed_conv(s, "telegram", "a1", "u2", now - 20)
    _seed_conv(s, "telegram", "other", "u9", now - 10)
    d = s.count_account_data("telegram", "a1")
    assert d == {"conversations": 2, "messages": 2}
    assert s.count_account_data("telegram", "nope") == {
        "conversations": 0, "messages": 0}
    assert s.count_account_data("", "") == {"conversations": 0, "messages": 0}
    s.close()


def test_preview_counts_and_purgeable_flag(tmp_path):
    s = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    _seed_conv(s, "telegram", "ghostp", "u1", now - 30)
    _seed_conv(s, "telegram", "ghostp", "u2", now - 20)
    reg = get_account_registry()
    reg.upsert("telegram", "livep", mode="protocol", status="online")
    _seed_conv(s, "telegram", "livep", "u1", now - 10)
    c = _client(s)
    # 幽灵号：可删 + 体量如实
    d = c.get("/api/accounts/telegram/ghostp/purge-history").json()
    assert d["ok"] is True and d["purgeable"] is True
    assert d["conversations"] == 2 and d["messages"] == 2
    # 活跃在册号：GET 是零副作用预览，返回 200 但 purgeable=false
    # （与 POST 的 409 同一判定单点——预览说不能删，真删也必然 409）
    d2 = c.get("/api/accounts/telegram/livep/purge-history").json()
    assert d2["ok"] is True and d2["purgeable"] is False
    assert d2["conversations"] == 1
    # 预览零副作用：数据一行未动
    assert ("telegram", "ghostp") in s.account_directory()
    s.close()


def _parse_jsonl(text: str):
    import json as _json
    return [_json.loads(ln) for ln in text.strip().splitlines() if ln.strip()]


def test_export_history_jsonl_shape(tmp_path):
    s = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    _seed_conv(s, "telegram", "exp1", "u1", now - 30)
    _seed_conv(s, "telegram", "exp1", "u2", now - 20)
    _seed_conv(s, "telegram", "other", "u9", now - 10)   # 旁号：绝不入包
    c = _client(s)
    r = c.get("/api/accounts/telegram/exp1/export-history")
    assert r.status_code == 200
    assert "application/x-ndjson" in str(r.headers.get("content-type") or "")
    cd = str(r.headers.get("content-disposition") or "")
    assert "attachment" in cd and "chatx-history_telegram_exp1_" in cd
    rows = _parse_jsonl(r.text)
    meta = rows[0]
    assert meta["type"] == "export_meta" and meta["schema"] == 1
    assert meta["conversations"] == 2 and meta["messages"] == 2
    convs = [x for x in rows if x["type"] == "conversation"]
    msgs = [x for x in rows if x["type"] == "message"]
    assert {c0["chat_key"] for c0 in convs} == {"u1", "u2"}
    assert len(msgs) == 2
    assert all(m["conversation_id"].startswith("telegram:exp1:") for m in msgs)
    assert all(m.get("text") == "hi" for m in msgs)
    # 旁号数据零泄漏
    assert not any("other" in str(x.get("conversation_id") or "") for x in rows)
    s.close()


def test_export_active_account_allowed(tmp_path):
    """导出=只读备份语义：活跃号也可导（与删除的 409 刻意不同闸）。"""
    s = InboxStore(tmp_path / "inbox.db")
    reg = get_account_registry()
    reg.upsert("telegram", "livee", mode="protocol", status="online")
    _seed_conv(s, "telegram", "livee", "u1", time.time() - 10)
    c = _client(s)
    r = c.get("/api/accounts/telegram/livee/export-history")
    assert r.status_code == 200
    rows = _parse_jsonl(r.text)
    assert any(x["type"] == "message" for x in rows)
    # 导出后数据原样在库（读操作零破坏）
    assert ("telegram", "livee") in s.account_directory()
    s.close()


def test_export_filename_sanitizes_account_id(tmp_path):
    """账号 id 里的空格/引号等字符不得进 Content-Disposition 文件名。

    （斜杠不必测：Starlette 路径参数不匹配 %2F，请求在路由层就 404——
    与 remove/logout 同边界，账号 id 上游消毒本就不含斜杠。）
    """
    s = InboxStore(tmp_path / "inbox.db")
    _seed_conv(s, "telegram", 'we ird"id', "u1", time.time() - 10)
    c = _client(s)
    r = c.get('/api/accounts/telegram/we%20ird%22id/export-history')
    assert r.status_code == 200
    cd = str(r.headers.get("content-disposition") or "")
    fname = cd.split("filename=", 1)[1]
    # 引号只允许出现在包裹文件名的两端；空格/引号一律折成下划线
    assert fname.startswith('"') and fname.endswith('"')
    assert " " not in fname and '"' not in fname.strip('"')
    assert "we_ird_id" in fname
    s.close()


# ══ P1 合流补钉（2026-08-17 下午）：一致性矩阵 / 白名单契约 / 审计 / 空号 ══


def _seed_conv_msgs(store: InboxStore, plat: str, acct: str, ck: str,
                    ts: float, texts):
    """一会话多消息种子（platform_msg_id 唯一，防 ingest 去重折叠）。"""
    cid = f"{plat}:{acct}:{ck}"
    store.ingest_batch(
        InboxConversation(conversation_id=cid, platform=plat, account_id=acct,
                          chat_key=ck, display_name=ck,
                          last_text=texts[-1], last_ts=ts + len(texts)),
        [InboxMessage(conversation_id=cid, platform_msg_id=f"{ck}-m{i}",
                      direction="in", text=t, ts=ts + i)
         for i, t in enumerate(texts)],
    )
    return cid


def test_preview_verdict_matches_post_matrix(tmp_path):
    """核心不变量：预览 ``purgeable`` ⇔ POST 放行（``_purge_history_blocked`` 单点）。

    预判与护栏各算一套＝坐席看着「可删」却撞 409，比没有预览更糟——与
    approve_blocked 徽标同款纪律。矩阵盖全七种账号形态（含 config/合成号，
    既有用例只点测了 ghost/live 两态）。
    """
    s = InboxStore(tmp_path / "inbox.db")
    reg = get_account_registry()
    now = time.time()
    # (platform, 账号, 注册表状态 or None, 期望可删)
    matrix = [
        ("telegram", "mx_hist", None, True),        # history_only
        ("telegram", "mx_off", "offline", True),    # 已退出
        ("telegram", "mx_rm", "removed", True),     # 已移除残行
        ("telegram", "mx_on", "online", False),     # 在线
        ("telegram", "mx_pend", "pending", False),  # 连接中间态
        ("telegram", "mx_cfg", None, False),        # config 常驻（下方 cfg 声明）
        ("web", "workspace", None, False),          # 内置合成号
    ]
    for plat, acct, st, _want in matrix:
        _seed_conv(s, plat, acct, "u1", now - 10)
        if st is not None:
            reg.upsert(plat, acct, mode="protocol")
            reg.set_status(plat, acct, st)
    c = _client(s, cfg={"telegram": {"accounts": [{"id": "mx_cfg"}]}})
    for plat, acct, _st, want in matrix:
        pv = c.get(f"/api/accounts/{plat}/{acct}/purge-history").json()
        assert pv["ok"] is True
        assert pv["purgeable"] is want, f"{plat}:{acct} 预览判定漂移"
        post = _purge(c, plat, acct)
        allowed = post.status_code != 409
        assert allowed is want, \
            f"{plat}:{acct} 预览 purgeable={want} 与 POST {post.status_code} 打架"
        if want:
            assert post.status_code == 200
            assert (plat, acct) not in s.account_directory()
        else:
            assert (plat, acct) in s.account_directory(), "拒删后数据必须原封不动"
    s.close()


_CONV_ALLOWED = {"type", "conversation_id", "chat_key", "display_name",
                 "username", "phone", "last_text", "last_ts", "unread",
                 "archived", "automation_mode", "funnel_stage"}
_MSG_ALLOWED = {"type", "conversation_id", "message_id", "platform_msg_id",
                "direction", "text", "original_text", "translated_text",
                "source_lang", "target_lang", "media_type", "media_ref",
                "ts", "deleted_at"}


def test_export_field_whitelist_and_stream_order(tmp_path):
    """schema=1 白名单契约：新增内部列不得静默泄进导出文件；
    多消息会话的流内顺序＝消息紧跟所属会话行（时间线可读性契约——
    既有 shape 用例每会话只有 1 条消息，顺序与白名单两条都没真正压过）。"""
    s = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    _seed_conv_msgs(s, "telegram", "expw", "u1", now - 100, ["hi", "yo", "ok"])
    _seed_conv_msgs(s, "telegram", "expw", "u2", now - 50, ["hey"])
    c = _client(s)
    r = c.get("/api/accounts/telegram/expw/export-history")
    assert r.status_code == 200
    rows = _parse_jsonl(r.text)
    assert rows[0]["type"] == "export_meta"
    assert rows[0]["conversations"] == 2 and rows[0]["messages"] == 4
    convs = [x for x in rows if x["type"] == "conversation"]
    msgs = [x for x in rows if x["type"] == "message"]
    assert len(convs) == 2 and len(msgs) == 4
    for c0 in convs:
        extra = set(c0.keys()) - _CONV_ALLOWED
        assert not extra, f"会话行泄漏白名单外字段: {extra}"
    for m0 in msgs:
        extra = set(m0.keys()) - _MSG_ALLOWED
        assert not extra, f"消息行泄漏白名单外字段: {extra}"
    # 流内顺序：任何消息行的 conversation_id 必须等于最近一条会话行
    cur = ""
    for x in rows[1:]:
        if x["type"] == "conversation":
            cur = str(x.get("conversation_id") or "")
        else:
            assert str(x.get("conversation_id") or "") == cur, \
                "消息行必须紧跟所属会话行"
    # 同会话内消息按 ts 升序（导出文件天然按时间线可读）
    u1 = [m for m in msgs if m["conversation_id"].endswith(":u1")]
    assert [m["text"] for m in u1] == ["hi", "yo", "ok"]
    s.close()


def test_export_empty_account_meta_only(tmp_path):
    """从未见过的账号：诚实导出（仅 meta 零计数），不装 404——备份语义下
    「空」也是一个如实的答案。"""
    s = InboxStore(tmp_path / "inbox.db")
    c = _client(s)
    r = c.get("/api/accounts/telegram/nobody/export-history")
    assert r.status_code == 200
    rows = _parse_jsonl(r.text)
    assert len(rows) == 1 and rows[0]["type"] == "export_meta"
    assert rows[0]["conversations"] == 0 and rows[0]["messages"] == 0
    s.close()


def test_export_audit_recorded_on_complete(tmp_path, monkeypatch):
    """完整下载完成才落 ``account_export`` 审计（行数不含内容）。

    路由在生成器内**call-time** import getter → monkeypatch 模块属性即可拦截。
    """
    import src.ops.ops_events as oe

    calls = []

    class _Cap:
        def record(self, kind, **kw):
            calls.append((kind, kw))

    monkeypatch.setattr(oe, "get_ops_event_store", lambda *a, **k: _Cap())
    s = InboxStore(tmp_path / "inbox.db")
    _seed_conv_msgs(s, "telegram", "expa", "u1", time.time() - 10, ["hi", "yo"])
    c = _client(s)
    r = c.get("/api/accounts/telegram/expa/export-history")
    assert r.status_code == 200 and r.text   # TestClient 读全量＝完整下载
    exports = [kw for kind, kw in calls if kind == "account_export"]
    assert len(exports) == 1
    assert exports[0]["account_id"] == "expa"
    assert "convs=1" in exports[0]["detail"]
    assert "msgs=2" in exports[0]["detail"]
    s.close()
