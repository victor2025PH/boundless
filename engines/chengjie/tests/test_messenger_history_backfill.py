# -*- coding: utf-8 -*-
"""Messenger 历史回填闭环契约（P1，对齐 Telegram「接入即有上下文」）。

两条链路：
1. ``/api/internal/protocol/thread-history``——messenger-web 首连回填（登录后对
   top-N 线程各读末尾若干条推上来）。核心不变量（实施72 2026-08-27 改**合并语义**）：
   - **合并写入**：与库内既有消息按 ``platform_msg_id`` ∩ (方向, 归一化文本)
     双层去重后只收新条目——旧闸「只对空会话写入」被 2026-08-27 竞态实锤打穿
     （实时链先入 1 条 → 整批 14 条历史被拒收、13 条永久丢失）；
   - 既有消息判定必须走 ``list_recent_messages``（``get_oldest_message`` 只认带
     platform_msg_id 的消息，旧数据无 id 时会漏判）；
   - 历史语义 = ``ingest_thread``：不触发自动回复/SSE、unread 不制造新消息观感；
   - 消息缺 ts：空会话按数组序回推（base-n+i）；非空会话锚到库内最早之前（只保序）。
2. ``/api/platforms/messenger/{acct}/history``——会话内「拉更早」。核心不变量：
   - 与库内已有消息按（方向, 归一化文本）去重，只收带文本的；
   - 新写入的 ts 全部排在库内最早消息**之前**（顺序正确优先于时间真实性）；
   - 全部重复 → no_more；未启用 → protocol_disabled；node 404 → account_offline。
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.web.routes.unified_inbox_account_routes as uar
from src.inbox.store import InboxStore


def _client(tmp_path, *, messenger_enabled: bool = True):
    app = FastAPI()
    cfg = {"platform_login": {"messenger": {"web_enabled": messenger_enabled,
                                            "web_url": "http://127.0.0.1:8791"}}}
    uar.register_account_routes(app, api_auth=lambda request: None,
                                config_manager=SimpleNamespace(config=cfg))
    store = InboxStore(tmp_path / "inbox.db")
    app.state.inbox_store = store
    return TestClient(app), store


def _backfill(client, **overrides):
    payload = {
        "platform": "messenger", "account_id": "6158", "chat_key": "777",
        "name": "Bea", "avatar_url": "http://a/b.jpg", "ts": 1750000100,
        "messages": [
            {"direction": "in", "sender": "Bea", "text": "hi"},
            {"direction": "out", "sender": "你", "text": "hello"},
            {"direction": "in", "sender": "Bea", "text": "are you there?"},
        ],
    }
    payload.update(overrides)
    return client.post("/api/internal/protocol/thread-history", json=payload)


CID = "messenger:6158:777"


# ── thread-history（首连回填）────────────────────────────────────────────────

def test_backfill_writes_empty_conversation(tmp_path):
    c, store = _client(tmp_path)
    r = _backfill(c)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "inserted": 3, "corrected": 0}
    msgs = store.list_recent_messages(CID, limit=10)
    assert [m["text"] for m in msgs] == ["hi", "hello", "are you there?"]
    assert [m["direction"] for m in msgs] == ["in", "out", "in"]
    # ts 缺失 → 按数组序回推：严格递增（保序）
    tss = [float(m["ts"]) for m in msgs]
    assert tss == sorted(tss) and len(set(tss)) == 3
    conv = store.get_conversation(CID)
    assert conv is not None
    assert int(conv.get("unread") or 0) == 0        # 历史绝不制造「新消息」观感
    assert conv.get("display_name") == "Bea"
    assert conv.get("avatar_url") == "http://a/b.jpg"


def test_backfill_rebackfill_dedups_instead_of_refusing(tmp_path):
    """重启/重连后重建回填队列 → 同批重推按文本去重整批吸收，零重复零拒收。"""
    c, store = _client(tmp_path)
    assert _backfill(c).json()["inserted"] == 3
    r2 = _backfill(c)
    assert r2.json() == {"ok": True, "inserted": 0, "reason": "all_duplicates",
                         "corrected": 0}
    assert len(store.list_recent_messages(CID, limit=50)) == 3


def test_backfill_dedup_works_without_platform_msg_id(tmp_path):
    """去重必须认「无 id 的 messenger 消息」——get_oldest_message 会漏判。"""
    c, store = _client(tmp_path)
    _backfill(c)
    # 无 msg_id 的旧路径：get_oldest_message 恒 None；去重仍应靠 list_recent 生效
    assert store.get_oldest_message(CID) is None
    assert _backfill(c).json()["reason"] == "all_duplicates"


def test_backfill_merges_history_when_realtime_won_the_race(tmp_path):
    """实施72 回归钉（2026-08-27 事故原型）：实时链先入最新 1 条，随后到达的
    回填批（旧史 + 那条最新的重复）必须**合并**旧史而不是整批拒收。

    事故数据：Calixa 线程 backfill pushed n=14 → 库里只剩 1 条（13 条永久丢失）。
    """
    c, store = _client(tmp_path)
    from src.inbox.ingest import ingest_collected_chats
    ingest_collected_chats(store, [{
        "conversation_id": CID, "platform": "messenger", "account_id": "6158",
        "chat_key": "777", "name": "Bea", "unread": 1, "last_ts": 1750000500,
        "last_message": {"text": "newest", "ts": 1750000500, "direction": "in",
                         "msg_id": "m_new111111111111"},
    }], publish_events=False)
    assert len(store.list_recent_messages(CID, limit=10)) == 1
    r = _backfill(c, messages=[
        {"direction": "in", "sender": "Bea", "text": "old 1"},
        {"direction": "out", "sender": "你", "text": "old 2"},
        {"direction": "in", "sender": "Bea", "text": "newest"},   # 与实时重复
    ])
    assert r.json() == {"ok": True, "inserted": 2, "corrected": 0}
    msgs = store.list_recent_messages(CID, limit=10)
    texts = [m["text"] for m in msgs]
    assert texts == ["old 1", "old 2", "newest"]   # 旧史排在实时消息之前
    assert texts.count("newest") == 1              # 重复被滤掉
    tss = [float(m["ts"]) for m in msgs]
    assert tss == sorted(tss)
    assert tss[0] < 1750000500 and tss[1] < 1750000500
    conv = store.get_conversation(CID)
    # 合并旧史绝不制造「新消息」观感：未读保持实时链累计值、last 不被顶掉
    assert int(conv.get("unread") or 0) == 1
    assert float(conv.get("last_ts") or 0) == 1750000500.0


def test_backfill_stores_synth_msg_id(tmp_path):
    """P3：worker 带 synth msg_id → 落 platform_msg_id（表情/撤回挂点）。"""
    c, store = _client(tmp_path)
    r = _backfill(c, messages=[
        {"direction": "in", "sender": "Bea", "text": "hi", "msg_id": "m_abc1111111111111"},
        {"direction": "out", "sender": "你", "text": "hello", "msg_id": "m_def2222222222222"},
    ])
    assert r.json()["inserted"] == 2
    msgs = store.list_recent_messages(CID, limit=10)
    assert [m.get("platform_msg_id") for m in msgs] == [
        "m_abc1111111111111", "m_def2222222222222",
    ]
    # 有 id 后 get_oldest_message 可用；默认批重推：hi/hello 按文本去重滤掉，
    # 「are you there?」是真新条目 → 合并 1 条（这正是合并语义与旧拒收闸的区别）
    assert store.get_oldest_message(CID) is not None
    r2 = _backfill(c)
    assert r2.json() == {"ok": True, "inserted": 1, "corrected": 0}
    texts = [m["text"] for m in store.list_recent_messages(CID, limit=10)]
    assert texts.count("hi") == 1 and texts.count("hello") == 1
    assert "are you there?" in texts


def test_backfill_missing_fields_and_empty_messages(tmp_path):
    c, _ = _client(tmp_path)
    assert c.post("/api/internal/protocol/thread-history",
                  json={"platform": "messenger"}).json()["ok"] is False
    r = _backfill(c, messages=[])
    assert r.json()["reason"] == "no_messages"
    # 全部无正文无媒体 → 同样 no_messages（不落空壳）
    r = _backfill(c, messages=[{"direction": "in", "text": ""}])
    assert r.json()["reason"] == "no_messages"


def test_backfill_repush_corrects_synthesized_ts(tmp_path):
    """实施72 P5 自愈校正：库内是**合成时间**（approx_ts=1）的行，被 worker 带着
    真实 epoch 重推时必须就地修正 ts 并清标——而不是当重复整条丢弃把错时间留库。

    这是 8/27 事故的存量修复通道：P4 之前回填的行全体盖着「导入时刻」，
    worker 学会解析 aria 时间后每次重启都会重推，这一路顺手把旧行修对。
    """
    c, store = _client(tmp_path)
    assert _backfill(c).json()["inserted"] == 3          # 无 ts → 全体 approx=1
    before = store.list_recent_messages(CID, limit=10)
    assert [int(m["approx_ts"]) for m in before] == [1, 1, 1]
    real = {"hi": 1749100000.0, "hello": 1749100600.0,
            "are you there?": 1749200000.0}
    r = _backfill(c, messages=[
        {"direction": "in", "sender": "Bea", "text": "hi", "ts": real["hi"]},
        {"direction": "out", "sender": "你", "text": "hello",
         "ts": real["hello"]},
        {"direction": "in", "sender": "Bea", "text": "are you there?",
         "ts": real["are you there?"]},
    ])
    assert r.json() == {"ok": True, "inserted": 0, "reason": "all_duplicates",
                        "corrected": 3}
    after = {m["text"]: m for m in store.list_recent_messages(CID, limit=10)}
    for text, ts in real.items():
        assert float(after[text]["ts"]) == ts            # 真实时间已落库
        assert int(after[text]["approx_ts"]) == 0        # 标记已清（不再显示「约」）
    # 幂等：同一批再推一次已无 approx 行可改 → corrected 归零，ts 不变
    r2 = _backfill(c, messages=[
        {"direction": "in", "sender": "Bea", "text": "hi", "ts": 1749999999},
    ])
    assert r2.json()["corrected"] == 0
    again = {m["text"]: m for m in store.list_recent_messages(CID, limit=10)}
    assert float(again["hi"]["ts"]) == real["hi"]         # 真实时间行不被二次改写


def test_backfill_repush_never_touches_real_ts_rows(tmp_path):
    """真实时间行（approx_ts=0）永不被「校正」——把一个真值改成另一个真值是
    我们无权做的事（也会让重推变成不幂等的时间抖动源）。"""
    c, store = _client(tmp_path)
    _backfill(c, messages=[
        {"direction": "in", "sender": "Bea", "text": "real", "ts": 1749000000},
    ])
    row = store.list_recent_messages(CID, limit=5)[0]
    assert int(row["approx_ts"]) == 0
    r = _backfill(c, messages=[
        {"direction": "in", "sender": "Bea", "text": "real", "ts": 1700000000},
    ])
    assert r.json()["corrected"] == 0
    assert float(store.list_recent_messages(CID, limit=5)[0]["ts"]) == 1749000000.0


def test_backfill_repush_skips_ambiguous_text_match(tmp_path):
    """同方向同文本在库里有两条 approx 行 → 认不出真值该贴哪条，一律不改
    （宁可留着「约」标，也不把真实时间贴到错的行上）。"""
    c, store = _client(tmp_path)
    _backfill(c, messages=[
        {"direction": "in", "sender": "Bea", "text": "在吗"},
        {"direction": "out", "sender": "你", "text": "占位"},
    ])
    # 第二条同文本同方向：经「实时链」直接落库（绕开回填去重），制造歧义
    from src.inbox.ingest import ingest_thread
    ingest_thread(store, {"conversation_id": CID, "platform": "messenger",
                          "account_id": "6158", "chat_key": "777"},
                  [{"direction": "in", "text": "在吗", "ts": 1749000500,
                    "approx_ts": 1}])
    dupes = [m for m in store.list_recent_messages(CID, limit=10)
             if m["text"] == "在吗"]
    assert len(dupes) == 2 and all(int(m["approx_ts"]) == 1 for m in dupes)
    r = _backfill(c, messages=[
        {"direction": "in", "sender": "Bea", "text": "在吗", "ts": 1749100000},
    ])
    assert r.json()["corrected"] == 0
    still = [m for m in store.list_recent_messages(CID, limit=10)
             if m["text"] == "在吗"]
    assert all(int(m["approx_ts"]) == 1 for m in still)   # 两条都保持「约」标


def test_same_minute_messages_keep_chat_order(tmp_path):
    """实施72 P5：平台时间只到分钟 → 同分钟多条 ts **完全相同**，读路径必须用
    rowid（入库序）兜底排序，否则并列组被倒着给出＝聊天记录读起来是反的。

    这是 P5 校正把「1 秒等差的合成 ts」换成「真实但只到分钟」后暴露出来的老问题
    （旧账号的实时链数据里也早有并列组），修在读路径＝过去和将来一起对。
    """
    c, store = _client(tmp_path)
    minute = 1749000000.0            # 同一分钟三条，真实顺序＝推送数组序
    _backfill(c, messages=[
        {"direction": "out", "sender": "你", "text": "第一句", "ts": minute},
        {"direction": "in", "sender": "Bea", "text": "第二句", "ts": minute},
        {"direction": "in", "sender": "Bea", "text": "第三句", "ts": minute},
        {"direction": "in", "sender": "Bea", "text": "下一分钟", "ts": minute + 60},
    ])
    got = [m["text"] for m in store.list_recent_messages(CID, limit=10)]
    assert got == ["第一句", "第二句", "第三句", "下一分钟"]
    assert [m["text"] for m in store.list_messages(CID, limit=10)] == got
    # 游标翻页分支同样有序（before_ts 走另一条 SQL）
    page = store.list_recent_messages(CID, limit=10, before_ts=minute + 60)
    assert [m["text"] for m in page] == ["第一句", "第二句", "第三句"]


def test_correct_approx_ts_store_guards(tmp_path):
    """store 层护栏（SQL 内闸，不靠调用方自律）：只改 approx 行 / 拒非法 ts。"""
    c, store = _client(tmp_path)
    _backfill(c, messages=[
        {"direction": "in", "sender": "Bea", "text": "syn"},
        {"direction": "in", "sender": "Bea", "text": "true", "ts": 1749000000},
    ])
    rows = {m["text"]: m for m in store.list_recent_messages(CID, limit=10)}
    syn, true_row = rows["syn"], rows["true"]
    assert store.correct_approx_ts(syn["message_id"], 1749123456) is True
    assert store.correct_approx_ts(syn["message_id"], 1749999999) is False  # 已清标
    assert store.correct_approx_ts(true_row["message_id"], 1700000000) is False
    assert store.correct_approx_ts("", 1749123456) is False
    assert store.correct_approx_ts(syn["message_id"], 0) is False
    assert store.correct_approx_ts(syn["message_id"], -5) is False
    assert store.correct_approx_ts(syn["message_id"], "x") is False  # type: ignore[arg-type]
    fixed = {m["text"]: m for m in store.list_recent_messages(CID, limit=10)}
    assert float(fixed["syn"]["ts"]) == 1749123456.0
    assert float(fixed["true"]["ts"]) == 1749000000.0


def test_backfill_keeps_provided_ts_and_media(tmp_path):
    c, store = _client(tmp_path)
    r = _backfill(c, messages=[
        {"direction": "in", "text": "old", "ts": 1749990000},
        {"direction": "in", "text": "", "media_type": "image",
         "media_ref": "/static/protocol_media/messenger/x.jpg"},
    ])
    assert r.json()["inserted"] == 2
    msgs = store.list_recent_messages(CID, limit=10)
    assert float(msgs[0]["ts"]) == 1749990000.0     # 显式 ts 保留
    assert msgs[1]["media_type"] == "image"          # 无正文媒体照收


# ── /history messenger 分支（拉更早）────────────────────────────────────────

def _pull(client, count: int = 50):
    return client.post("/api/platforms/messenger/6158/history",
                       json={"chat_key": "777", "count": count})


def _fake_node(monkeypatch, messages, calls=None):
    async def fake_post(url, payload, timeout=20.0):
        if calls is not None:
            calls.append((url, dict(payload)))
        return {"ok": True, "messages": messages}
    monkeypatch.setattr(
        "src.integrations.messenger_web_login._post_json", fake_post)


def test_pull_history_dedups_and_orders_before_earliest(tmp_path, monkeypatch):
    c, store = _client(tmp_path)
    _backfill(c)  # 库内已有 hi / hello / are you there?
    earliest = min(float(m["ts"]) for m in store.list_recent_messages(CID, limit=10))
    calls = []
    _fake_node(monkeypatch, [
        {"direction": "in", "sender": "Bea", "text": "much older 1"},
        {"direction": "out", "sender": "你", "text": "much older 2"},
        {"direction": "in", "sender": "Bea", "text": "hi"},   # 与库内重复 → 滤掉
    ], calls)
    r = _pull(c)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "requested": 50, "inserted": 2,
                        "corrected": 0}
    assert calls and calls[0][0].endswith("/accounts/6158/thread-history")
    msgs = store.list_recent_messages(CID, limit=20)
    texts = [m["text"] for m in msgs]
    assert texts.count("hi") == 1                    # 去重生效
    assert texts[:2] == ["much older 1", "much older 2"]  # 排在最前
    pulled_ts = [float(m["ts"]) for m in msgs[:2]]
    assert all(t < earliest for t in pulled_ts)      # 全部在库内最早之前
    assert pulled_ts == sorted(pulled_ts)


def test_pull_history_all_duplicates_reports_no_more(tmp_path, monkeypatch):
    c, store = _client(tmp_path)
    _backfill(c)
    _fake_node(monkeypatch, [
        {"direction": "in", "text": "HI  "},   # 大小写/空白归一后仍是重复
        {"direction": "out", "text": "hello"},
    ])
    r = _pull(c)
    assert r.json()["ok"] is False
    assert r.json()["reason"] == "no_more"
    assert len(store.list_recent_messages(CID, limit=20)) == 3


def test_pull_history_corrects_synthesized_ts(tmp_path, monkeypatch):
    """实施72 P5：「拉更早」同样修合成时间——它比回填**够得更深**（worker 回填只读
    线程末尾 ~20 条，早已滚出该窗口的合成行只有这条路能修）。一条没新增但修对了
    时间时 ok=True（坐席点了「加载更早」不该只看到失败）。"""
    c, store = _client(tmp_path)
    _backfill(c)                                     # 三条合成 ts（approx=1）
    _fake_node(monkeypatch, [
        {"direction": "in", "text": "hi", "ts": 1749000001},
        {"direction": "out", "text": "hello", "ts": 1749000002},
    ])
    r = _pull(c)
    body = r.json()
    assert body["reason"] == "no_more"                # 无新条目
    assert body["corrected"] == 2 and body["ok"] is True
    rows = {m["text"]: m for m in store.list_recent_messages(CID, limit=20)}
    assert float(rows["hi"]["ts"]) == 1749000001.0
    assert int(rows["hi"]["approx_ts"]) == 0
    assert int(rows["are you there?"]["approx_ts"]) == 1   # 未被推到的行不动
    # 残余合成行仍排在校正后的真实史之前（锚点已按校正值重算）
    _fake_node(monkeypatch, [{"direction": "in", "text": "even older"}])
    assert _pull(c).json()["inserted"] == 1
    older = [m for m in store.list_recent_messages(CID, limit=20)
             if m["text"] == "even older"][0]
    assert float(older["ts"]) < 1749000001.0


def test_pull_history_disabled_and_offline(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, messenger_enabled=False)
    assert _pull(c).json()["reason"] == "protocol_disabled"

    c2, _ = _client(tmp_path.joinpath("b"))

    class _Resp:
        status_code = 404

    async def fake_404(url, payload, timeout=20.0):
        exc = Exception("404")
        exc.response = _Resp()
        raise exc
    monkeypatch.setattr(
        "src.integrations.messenger_web_login._post_json", fake_404)
    assert _pull(c2).json()["reason"] == "account_offline"


def test_pull_history_empty_conversation_uses_now_anchor(tmp_path, monkeypatch):
    """占位会话（目录同步建的、零消息）也能拉更早——锚点回落当前时刻。"""
    c, store = _client(tmp_path)
    _fake_node(monkeypatch, [{"direction": "in", "text": "first ever"}])
    r = _pull(c)
    assert r.json()["inserted"] == 1
    msgs = store.list_recent_messages(CID, limit=5)
    assert msgs[0]["text"] == "first ever"


def test_pull_history_prefers_real_ts_from_worker(tmp_path, monkeypatch):
    """实施72 P4：worker 随行下发 aria 解析的真实 epoch → 直存（approx=0）；
    认不出（ts=0/缺失）的行仍走「锚定在库内最早之前」合成 + approx=1。"""
    c, store = _client(tmp_path)
    _backfill(c)   # 库内 hi/hello/are you there?（合成 ts ≈ 1750000097+）
    real_ts = 1749000000.0
    _fake_node(monkeypatch, [
        {"direction": "in", "text": "dated history", "ts": real_ts},
        {"direction": "out", "text": "undated history"},          # 认不出 → 合成
    ])
    r = _pull(c)
    assert r.json()["inserted"] == 2
    rows = {m["text"]: m for m in store.list_recent_messages(CID, limit=20)}
    dated = rows["dated history"]
    assert float(dated["ts"]) == real_ts                 # 真实时间直存
    assert int(dated.get("approx_ts") or 0) == 0         # 不打合成标
    undated = rows["undated history"]
    assert int(undated.get("approx_ts") or 0) == 1       # 合成标照打
    earliest_existing = min(
        float(m["ts"]) for t, m in rows.items()
        if t in ("hi", "hello", "are you there?"))
    assert float(undated["ts"]) < earliest_existing      # 合成值仍锚定在最早之前


def test_pull_history_dedups_by_msg_id(tmp_path, monkeypatch):
    """P3：同 msg_id 即使文案微调也不重复灌库（id 优先于文本归一）。"""
    c, store = _client(tmp_path)
    _backfill(c, messages=[
        {"direction": "in", "text": "hi", "msg_id": "m_same000000000001"},
    ])
    _fake_node(monkeypatch, [
        {"direction": "in", "text": "hi!", "msg_id": "m_same000000000001"},  # 同 id
        {"direction": "in", "text": "brand new", "msg_id": "m_new0000000000002"},
    ])
    r = _pull(c)
    assert r.json()["inserted"] == 1
    texts = [m["text"] for m in store.list_recent_messages(CID, limit=10)]
    assert texts.count("hi") == 1
    assert "brand new" in texts
    assert "hi!" not in texts


# ── 目录同步的 unread 保护（P1 潜伏 bug 回归钉）─────────────────────────────

def test_directory_sync_without_unread_key_keeps_real_unread(tmp_path):
    """目录推送不带 unread 键时**绝不能**把 ingest 累计的真实未读清零。

    upsert_conversation 的 unread 是无条件覆盖语义；Telegram/WA 的目录行带云端
    真值从未踩到，Messenger 目录每 5 分钟推一轮、DOM 抓不到可靠计数——缺键按 0
    传的话，坐席的未读徽章会被周期性抹平（且极难归因）。
    """
    _, store = _client(tmp_path)
    from src.inbox.ingest import ingest_collected_chats
    ingest_collected_chats(store, [{
        "conversation_id": CID, "platform": "messenger", "account_id": "6158",
        "chat_key": "777", "name": "Bea", "last_msg": "hi", "last_ts": 1750000000,
        "unread": 3,
        "messages": [{"text": "hi", "ts": 1750000000, "direction": "in"}],
    }], publish_events=False)
    assert int(store.get_conversation(CID)["unread"] or 0) >= 1
    before = int(store.get_conversation(CID)["unread"] or 0)
    # 目录推送（无 unread 键）→ 未读保留；显式带 unread → 按传入值
    n = store.upsert_protocol_chats("messenger", "6158", [
        {"jid": "777", "name": "Bea Gaston", "avatar_url": "http://a/new.jpg"},
        {"jid": "888", "name": "New Peer"},
    ])
    assert n == 2
    after = store.get_conversation(CID)
    assert int(after["unread"] or 0) == before          # 未读未被清零
    assert after["display_name"] == "Bea Gaston"        # 名字/头像照常更新
    assert int(store.get_conversation("messenger:6158:888")["unread"] or 0) == 0
    n = store.upsert_protocol_chats("messenger", "6158", [
        {"jid": "777", "name": "Bea Gaston", "unread": 0},
    ])
    assert n == 1
    assert int(store.get_conversation(CID)["unread"] or 0) == 0  # 显式 0 仍可清
