"""InboxStore 单元测试（Phase A）。"""

from src.inbox.models import InboxConversation, InboxMessage, MessageAnalysis
from src.inbox.store import InboxStore, _message_pk


def _conv(cid="line:a:room1", **kw):
    base = dict(
        conversation_id=cid, platform="line", account_id="a", chat_key="room1",
        display_name="User", language="ja", last_text="こんにちは", last_ts=100, unread=2,
    )
    base.update(kw)
    return InboxConversation(**base)


def test_ddl_creates_tables_and_basic_roundtrip(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_conversation(_conv())
    rows = store.list_conversations()
    assert len(rows) == 1
    assert rows[0]["conversation_id"] == "line:a:room1"
    assert rows[0]["language"] == "ja"
    store.close()


def test_message_dedup_with_platform_id(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    m = InboxMessage(conversation_id="line:a:room1", platform_msg_id="m1", text="hi", ts=1)
    assert store.ingest_message(m) is True
    # 同 platform_msg_id 再 ingest → 不重复
    assert store.ingest_message(m) is False
    assert store.count_messages("line:a:room1") == 1
    store.close()


def test_message_dedup_without_platform_id_uses_hash(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    m1 = InboxMessage(conversation_id="c1", platform_msg_id="", text="same", ts=5)
    m2 = InboxMessage(conversation_id="c1", platform_msg_id="", text="same", ts=5)
    m3 = InboxMessage(conversation_id="c1", platform_msg_id="", text="different", ts=5)
    assert store.ingest_message(m1) is True
    assert store.ingest_message(m2) is False  # 同 text|ts → 同 hash 主键 → 去重
    assert store.ingest_message(m3) is True
    assert store.count_messages("c1") == 2
    store.close()


def test_cross_path_dedup_hash_after_pmid_skipped(tmp_path):
    """权威 pmid 行先落 → 之后无 id 的 hash 兜底重放（同 conv/text/ts）被跳过，不再多出 :h: 行。"""
    store = InboxStore(tmp_path / "inbox.db")
    conv = _conv(cid="telegram:8244899900:c", platform="telegram",
                 account_id="8244899900", chat_key="c")
    auth = InboxMessage(conversation_id="telegram:8244899900:c",
                        platform_msg_id="778", text="hello", ts=100)
    assert store.ingest_batch(conv, [auth]) == 1
    nopid = InboxMessage(conversation_id="telegram:8244899900:c",
                         platform_msg_id="", text="hello", ts=100)
    assert store.ingest_batch(conv, [nopid]) == 0   # 兜底重放被跨路径护栏跳过
    assert store.count_messages("telegram:8244899900:c") == 1
    store.close()


def test_cross_path_dedup_pmid_replaces_prior_hash(tmp_path):
    """无 id 的 hash 行先落 → 之后权威 pmid 行到达（同 conv/text/ts）：删旧 hash 孪生，保持单条。"""
    store = InboxStore(tmp_path / "inbox.db")
    conv = _conv(cid="telegram:acc:c", platform="telegram",
                 account_id="acc", chat_key="c")
    nopid = InboxMessage(conversation_id="telegram:acc:c",
                         platform_msg_id="", text="hello", ts=100)
    assert store.ingest_batch(conv, [nopid]) == 1
    auth = InboxMessage(conversation_id="telegram:acc:c",
                        platform_msg_id="778", text="hello", ts=100)
    assert store.ingest_batch(conv, [auth]) == 1     # 权威行落库
    assert store.count_messages("telegram:acc:c") == 1   # 旧 hash 孪生被取代，仍单条
    rows = store.list_messages("telegram:acc:c")
    assert rows[0]["platform_msg_id"] == "778"
    store.close()


def test_outbound_dedup_optimistic_hash_then_echo_pmid_drifted_ts(tmp_path):
    """出站近似重复：乐观 _emit_inbox(hash, send-time ts) 先落，自身已发消息被回显
    (pmid, message.date ts 有秒级漂移) 后到 → pmid 落库时按时间窗删早先 hash 孪生，收敛单条。"""
    store = InboxStore(tmp_path / "inbox.db")
    conv = _conv(cid="telegram:acc:c", platform="telegram",
                 account_id="acc", chat_key="c")
    optimistic = InboxMessage(conversation_id="telegram:acc:c",
                              platform_msg_id="", text="Hmm?", ts=1782665096.1984508,
                              direction="out")
    assert store.ingest_batch(conv, [optimistic]) == 1
    echo = InboxMessage(conversation_id="telegram:acc:c",
                        platform_msg_id="774", text="Hmm?", ts=1782665096.0,
                        direction="out")
    assert store.ingest_batch(conv, [echo]) == 1
    assert store.count_messages("telegram:acc:c") == 1   # 漂移 ts 仍被窗口折叠
    rows = store.list_messages("telegram:acc:c")
    assert rows[0]["platform_msg_id"] == "774"
    store.close()


def test_outbound_repeated_text_not_lost_when_echo_present(tmp_path):
    """安全不变量：同会话短时间内两次发同样文本，各自有回显 → 必须保留两条（不丢发言）。"""
    store = InboxStore(tmp_path / "inbox.db")
    conv = _conv(cid="telegram:acc:c", platform="telegram",
                 account_id="acc", chat_key="c")
    # 第一条：乐观 hash + 回显 pmid
    store.ingest_batch(conv, [InboxMessage(conversation_id="telegram:acc:c",
        platform_msg_id="", text="好的", ts=1000.2, direction="out")])
    store.ingest_batch(conv, [InboxMessage(conversation_id="telegram:acc:c",
        platform_msg_id="901", text="好的", ts=1000.0, direction="out")])
    # 第二条：5 秒后再发同样文本
    store.ingest_batch(conv, [InboxMessage(conversation_id="telegram:acc:c",
        platform_msg_id="", text="好的", ts=1005.3, direction="out")])
    store.ingest_batch(conv, [InboxMessage(conversation_id="telegram:acc:c",
        platform_msg_id="902", text="好的", ts=1005.0, direction="out")])
    assert store.count_messages("telegram:acc:c") == 2   # 两条权威发言都在
    store.close()


def test_dedup_stats_counts_collapsed_twins(tmp_path):
    """去重护栏可观测：skipped_hash（入站 hash 撞 pmid 跳过）/ deleted_hash（出站 pmid 删 hash）累计。"""
    store = InboxStore(tmp_path / "inbox.db")
    conv = _conv(cid="telegram:acc:c", platform="telegram",
                 account_id="acc", chat_key="c")
    # 入站：pmid 先、hash 后（精确 ts）→ skipped_hash
    store.ingest_batch(conv, [InboxMessage(conversation_id="telegram:acc:c",
        platform_msg_id="778", text="hi", ts=100, direction="in")])
    store.ingest_batch(conv, [InboxMessage(conversation_id="telegram:acc:c",
        platform_msg_id="", text="hi", ts=100, direction="in")])
    # 出站：hash 先、pmid 后（漂移 ts，窗内）→ deleted_hash
    store.ingest_batch(conv, [InboxMessage(conversation_id="telegram:acc:c",
        platform_msg_id="", text="yo", ts=200.3, direction="out")])
    store.ingest_batch(conv, [InboxMessage(conversation_id="telegram:acc:c",
        platform_msg_id="901", text="yo", ts=200.0, direction="out")])
    s = store.dedup_stats()
    assert s["skipped_hash"] == 1
    assert s["deleted_hash"] == 1
    store.close()


def test_message_pk_distinct_for_empty_platform_id():
    # 无 platform_msg_id 时不应折叠成 (conv, '')，靠 hash 区分
    a = _message_pk("c1", "", "hello", 1)
    b = _message_pk("c1", "", "world", 1)
    assert a != b
    assert a.startswith("c1:h:")


def test_last_ts_monotonic_no_regression(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_conversation(_conv(last_text="new", last_ts=200))
    # 旧 fetch（更小 ts）不应覆盖更新的 last_text/last_ts
    store.upsert_conversation(_conv(last_text="old", last_ts=50))
    row = store.get_conversation("line:a:room1")
    assert row["last_text"] == "new"
    assert row["last_ts"] == 200
    store.close()


def test_ingest_batch_returns_inserted_count(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    msgs = [
        InboxMessage(conversation_id="line:a:room1", platform_msg_id="m1", text="a", ts=1),
        InboxMessage(conversation_id="line:a:room1", platform_msg_id="m2", text="b", ts=2),
    ]
    assert store.ingest_batch(_conv(), msgs) == 2
    # 重放整批 → 0 新增（幂等）
    assert store.ingest_batch(_conv(), msgs) == 0
    store.close()


def test_automation_mode_persists_across_restart(tmp_path):
    db = tmp_path / "inbox.db"
    store = InboxStore(db)
    assert store.get_automation_mode("line:a:room1") == "review"  # 默认
    assert store.get_automation_mode_if_set("line:a:room1") is None  # 未显式设置
    store.set_automation_mode("line:a:room1", "auto_ai")
    assert store.get_automation_mode_if_set("line:a:room1") == "auto_ai"
    store.close()

    # 模拟重启：新实例指向同一 db
    store2 = InboxStore(db)
    assert store2.get_automation_mode("line:a:room1") == "auto_ai"
    store2.close()


def test_automation_mode_rejects_invalid(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.set_automation_mode("c1", "nonsense")
    assert store.get_automation_mode("c1") == "review"
    store.close()


def test_analysis_save_and_latest(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    store.save_analysis(MessageAnalysis(
        message_id="m1", conversation_id="c1", intent="提问", emotion="平稳",
        risk_level="high", risk_reasons=["money"], analyzer="llm", confidence=0.9,
    ))
    latest = store.latest_analysis("c1")
    assert latest["intent"] == "提问"
    assert latest["risk_level"] == "high"
    assert latest["risk_reasons"] == ["money"]
    assert latest["analyzer"] == "llm"
    store.close()


def test_migration_idempotent_reopen(tmp_path):
    db = tmp_path / "inbox.db"
    InboxStore(db).close()
    # 二次打开不应报错（CREATE TABLE IF NOT EXISTS 幂等）
    store = InboxStore(db)
    assert store.list_conversations() == []
    store.close()


def test_update_message_text_writeback_voice_transcript_by_media_ref(tmp_path):
    """优化①：入站语音行 text='' → 转录回写按 (conv, media_ref) 命中，坐席台可见内容。"""
    store = InboxStore(tmp_path / "inbox.db")
    conv = _conv(cid="telegram:acc:c", platform="telegram",
                 account_id="acc", chat_key="c")
    voice = InboxMessage(
        conversation_id="telegram:acc:c", platform_msg_id="920",
        text="", media_type="voice",
        media_ref="/static/protocol_media/telegram/920.ogg", ts=100, direction="in")
    assert store.ingest_batch(conv, [voice]) == 1
    ok = store.update_message_text(
        "telegram:acc:c",
        media_ref="/static/protocol_media/telegram/920.ogg",
        text="嗯我在的，今天挺好的",
        only_if_empty=True)
    assert ok is True
    rows = store.list_messages("telegram:acc:c")
    assert rows[0]["text"] == "嗯我在的，今天挺好的"
    assert rows[0]["original_text"] == "嗯我在的，今天挺好的"
    store.close()


def test_update_message_text_by_message_id(tmp_path):
    """按 message_id 精确定位回写（enrich 首选路径）。"""
    store = InboxStore(tmp_path / "inbox.db")
    conv = _conv(cid="telegram:acc:c", platform="telegram",
                 account_id="acc", chat_key="c")
    voice = InboxMessage(
        conversation_id="telegram:acc:c", platform_msg_id="921",
        text="", media_type="voice", media_ref="/x/921.ogg", ts=101, direction="in")
    store.ingest_batch(conv, [voice])
    mid = store.list_messages("telegram:acc:c")[0]["message_id"]
    assert store.update_message_text(
        "telegram:acc:c", message_id=mid, text="你好呀") is True
    assert store.list_messages("telegram:acc:c")[0]["text"] == "你好呀"
    store.close()


def test_update_message_text_only_if_empty_guards_existing(tmp_path):
    """only_if_empty=True 时不踩已有真实正文（幂等/防竞态安全不变量）。"""
    store = InboxStore(tmp_path / "inbox.db")
    conv = _conv(cid="telegram:acc:c", platform="telegram",
                 account_id="acc", chat_key="c")
    msg = InboxMessage(
        conversation_id="telegram:acc:c", platform_msg_id="m1",
        text="用户原话已在", media_type="", media_ref="", ts=100, direction="in")
    store.ingest_batch(conv, [msg])
    mid = store.list_messages("telegram:acc:c")[0]["message_id"]
    # only_if_empty=True → 不覆盖
    assert store.update_message_text(
        "telegram:acc:c", message_id=mid, text="不该覆盖", only_if_empty=True) is False
    assert store.list_messages("telegram:acc:c")[0]["text"] == "用户原话已在"
    # only_if_empty=False → 强制覆盖
    assert store.update_message_text(
        "telegram:acc:c", message_id=mid, text="强制改", only_if_empty=False) is True
    assert store.list_messages("telegram:acc:c")[0]["text"] == "强制改"
    store.close()


def test_update_message_text_empty_transcript_noop(tmp_path):
    """空转录不写（防把占位刷成空），无定位键也不写。"""
    store = InboxStore(tmp_path / "inbox.db")
    conv = _conv(cid="telegram:acc:c", platform="telegram",
                 account_id="acc", chat_key="c")
    store.ingest_batch(conv, [InboxMessage(
        conversation_id="telegram:acc:c", platform_msg_id="m1",
        text="", media_type="voice", media_ref="/x/1.ogg", ts=100, direction="in")])
    assert store.update_message_text(
        "telegram:acc:c", media_ref="/x/1.ogg", text="   ") is False
    assert store.update_message_text("telegram:acc:c", text="有内容但无定位键") is False
    store.close()


# ── P0 未读可信化：已读水位 + 有效未读 ────────────────────────────────────────

def test_mark_read_sets_water_and_clears_effective_unread(tmp_path):
    """打开会话 → 已读水位推到末条 → 有效未读归零；原始 unread（同步值）保留。"""
    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_conversation(_conv(cid="whatsapp:wa1:c", platform="whatsapp",
                                    account_id="wa1", chat_key="c",
                                    last_ts=100.0, unread=5))
    water = store.mark_conversation_read("whatsapp:wa1:c")
    assert water == 100.0
    row = store.get_conversation("whatsapp:wa1:c")
    assert row["last_read_ts"] == 100.0
    assert row["unread"] == 5                       # 原始同步值不动
    assert store.effective_unread(row) == 0          # 有效未读归零
    store.close()


def test_effective_unread_rebounds_only_on_newer_message(tmp_path):
    """已读后同步覆盖 unread（手机端数字回来）不该复燃；真有更新的消息才重新未读。"""
    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_conversation(_conv(cid="whatsapp:wa1:c", platform="whatsapp",
                                    account_id="wa1", chat_key="c",
                                    last_ts=100.0, unread=3))
    store.mark_conversation_read("whatsapp:wa1:c")
    # 模拟协议号下一轮 upsert_protocol_chats：同一末条时间戳，unread 被手机端数字覆盖回 3
    store.upsert_conversation(_conv(cid="whatsapp:wa1:c", platform="whatsapp",
                                    account_id="wa1", chat_key="c",
                                    last_ts=100.0, unread=3))
    row = store.get_conversation("whatsapp:wa1:c")
    assert store.effective_unread(row) == 0          # 末条未变 → 不复燃
    # 来了真正更新的消息（last_ts 前进）→ 重新算未读
    store.upsert_conversation(_conv(cid="whatsapp:wa1:c", platform="whatsapp",
                                    account_id="wa1", chat_key="c",
                                    last_ts=200.0, unread=4))
    row2 = store.get_conversation("whatsapp:wa1:c")
    assert store.effective_unread(row2) == 4         # last_ts>last_read → 复现未读
    store.close()


def test_mark_read_monotonic_and_clears_mention(tmp_path):
    """水位单调不回退；打开会话一并清「@我」旗标。"""
    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_conversation(_conv(cid="whatsapp:wa1:g", platform="whatsapp",
                                    account_id="wa1", chat_key="g",
                                    last_ts=300.0, unread=1, chat_type="group"))
    store.set_conversation_mentioned("whatsapp:wa1:g", True)
    store.mark_conversation_read("whatsapp:wa1:g")
    row = store.get_conversation("whatsapp:wa1:g")
    assert row["last_read_ts"] == 300.0
    assert row["mentioned_unread"] == 0              # @我 旗标随打开清零
    # 显式回退到更早水位 → 不生效（单调）
    store.mark_conversation_read("whatsapp:wa1:g", read_ts=50.0)
    assert store.get_conversation("whatsapp:wa1:g")["last_read_ts"] == 300.0
    store.close()


def test_mark_read_missing_conversation_is_noop(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    assert store.mark_conversation_read("nope:x:y") == 0.0
    store.close()


def test_effective_unread_zero_when_no_raw_unread(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    assert store.effective_unread(
        {"unread": 0, "last_ts": 10, "last_read_ts": 0}) == 0
    assert store.effective_unread(
        {"unread": 4, "last_ts": 10, "last_read_ts": 20}) == 0   # 已读覆盖
    assert store.effective_unread(
        {"unread": 4, "last_ts": 30, "last_read_ts": 20}) == 4   # 末条更新
    store.close()


# ── 群内发言台账（群脉暴露度量的真账来源） ──────────────────────────────────


def _speech_fixture(store):
    """两个号在两个群发过言 + 一堆**不该被算进去**的干扰行。"""
    rows = [
        ("telegram:a1:g1", "telegram", "a1", "g1", "group",  "out", 1000.0),
        ("telegram:a2:g1", "telegram", "a2", "g1", "group",  "out", 1000.0),
        ("telegram:a1:g2", "telegram", "a1", "g2", "group",  "out", 1000.0),
        ("telegram:a2:g2", "telegram", "a2", "g2", "group",  "in",  1000.0),  # 只收没发
        ("telegram:a3:p1", "telegram", "a3", "p1", "private", "out", 1000.0),  # 私聊
        ("telegram:a4:g3", "telegram", "a4", "g3", "group",  "out", 10.0),     # 太久以前
    ]
    for cid, plat, acct, key, ctype, direction, ts in rows:
        store.upsert_conversation(_conv(cid=cid, platform=plat, account_id=acct,
                                        chat_key=key, chat_type=ctype, last_ts=ts))
        store.ingest_message(InboxMessage(
            conversation_id=cid, platform_msg_id=f"{cid}:{direction}",
            text="x", ts=ts, direction=direction))


def test_group_speech_ledger_counts_only_outbound_group_messages(tmp_path):
    """台账要回答「平台能看见哪个号在哪个群说过话」——进向与私聊都不算暴露。"""
    store = InboxStore(tmp_path / "inbox.db")
    _speech_fixture(store)
    led = store.group_speech_ledger()
    assert sorted(led.get("g1") or []) == ["a1", "a2"]
    assert (led.get("g2") or []) == ["a1"]      # a2 在 g2 只收没发
    assert "p1" not in led                       # 私聊里同框对平台没意义
    store.close()


def test_group_speech_ledger_honours_the_time_window(tmp_path):
    """共现必须随时间淡出，否则跑几个月每一对都饱和，这个数就没有分辨力了。"""
    store = InboxStore(tmp_path / "inbox.db")
    _speech_fixture(store)
    assert "g3" in store.group_speech_ledger()
    assert "g3" not in store.group_speech_ledger(since_ts=500.0)
    store.close()


def test_group_speech_ledger_can_narrow_by_platform(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    _speech_fixture(store)
    assert store.group_speech_ledger(platform="telegram")
    assert store.group_speech_ledger(platform="line") == {}
    store.close()


def test_group_speech_ledger_shape_feeds_the_co_occurrence_matrix(tmp_path):
    """形状必须与 ``GroupShowStore.performance_ledger`` 一致，才能直接进共现矩阵。"""
    from src.companion.group_show.performance import performance_metrics

    store = InboxStore(tmp_path / "inbox.db")
    _speech_fixture(store)
    assert performance_metrics(store.group_speech_ledger())["max_pair_co"] == 1
    store.close()


def test_group_last_spoke_at_takes_the_newest_group_message_per_account(tmp_path):
    """跨群间隔闸门问的是「这个号离上次冒头过了多久」，只有最新那一刻算数。"""
    store = InboxStore(tmp_path / "inbox.db")
    _speech_fixture(store)
    store.upsert_conversation(_conv(cid="telegram:a1:g9", platform="telegram",
                                    account_id="a1", chat_key="g9",
                                    chat_type="group", last_ts=8000.0))
    store.ingest_message(InboxMessage(conversation_id="telegram:a1:g9",
                                      platform_msg_id="telegram:a1:g9:out",
                                      text="x", ts=8000.0, direction="out"))
    assert store.group_last_spoke_at()["a1"] == 8000.0
    store.close()


def test_group_last_spoke_at_ignores_inbound_and_private(tmp_path):
    """只收没发、私聊里说话，平台都看不到这个号在群里冒头，不该占用冷却窗。"""
    store = InboxStore(tmp_path / "inbox.db")
    _speech_fixture(store)
    seen = store.group_last_spoke_at()
    assert "a2" in seen and seen["a2"] == 1000.0   # a2 只在 g1 发过
    assert "a3" not in seen                        # a3 只发过私聊
    store.close()


def test_group_last_spoke_at_honours_window_and_platform(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    _speech_fixture(store)
    assert "a4" in store.group_last_spoke_at()                 # ts=10 的老号
    assert "a4" not in store.group_last_spoke_at(since_ts=500.0)
    assert store.group_last_spoke_at(platform="line") == {}


def test_group_last_spoke_at_can_leave_the_target_group_out(tmp_path):
    """闸门问的是「有没有在**别的**群刚冒过头」。

    同一个群里连着回两句是正常对话，不是跨群编排的痕迹。不排除本群的话，刚在这个群
    自动回复过的号会被自己挡住——这条拦截毫无风险意义，只会让运营把闸门关掉。
    """
    store = InboxStore(tmp_path / "inbox.db")
    _speech_fixture(store)
    assert store.group_last_spoke_at()["a1"] == 1000.0
    assert "a1" in store.group_last_spoke_at(exclude_group="g1")   # a1 还在 g2 说过
    assert "a2" not in store.group_last_spoke_at(exclude_group="g1")  # a2 只在 g1
    store.close()


def test_group_inbound_since_returns_humans_in_time_order(tmp_path):
    """真发让路要吃进向消息——只看本群、升序、可排除演员号。"""
    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_conversation(_conv(cid="telegram:bot:g1", platform="telegram",
                                    account_id="bot", chat_key="g1",
                                    chat_type="group", last_ts=2000.0))
    for i, (sid, name, ts, text) in enumerate([
        ("u1", "老王", 1500.0, "这是啥"),
        ("u2", "小李", 1600.0, "多少钱"),
        ("bot", "我方号", 1700.0, "镜像误标"),  # exclude
    ]):
        store.ingest_message(InboxMessage(
            conversation_id="telegram:bot:g1",
            platform_msg_id=f"in-{i}", text=text, ts=ts, direction="in",
            sender_id=sid, sender_name=name))
    rows = store.group_inbound_since("g1", since_ts=1400.0,
                                     exclude_senders=["bot"])
    assert [(r["sender_name"], r["text"]) for r in rows] == [
        ("老王", "这是啥"), ("小李", "多少钱")]
    assert store.group_inbound_since("g1", since_ts=1550.0)[0]["text"] == "多少钱"
    assert store.group_inbound_since("") == []
    store.close()


def test_account_activity_report_scopes_by_account_and_window(tmp_path):
    """每账号日报：只算本平台本账号的会话；窗口外/对端删的消息不计；未回复联系人 = peers - peers_replied；
    时间线按 ts 降序且文本截断。"""
    store = InboxStore(tmp_path / "inbox.db")
    store.upsert_conversation(_conv("wechat:A:zhang", platform="wechat", account_id="A", chat_key="zhang", display_name="张三"))
    store.upsert_conversation(_conv("wechat:A:li", platform="wechat", account_id="A", chat_key="li", display_name="李四"))
    store.upsert_conversation(_conv("wechat:B:wang", platform="wechat", account_id="B", chat_key="wang", display_name="王五"))
    now = 1_000_000.0
    store.ingest_message(InboxMessage(conversation_id="wechat:A:zhang", platform_msg_id="z1", text="在吗", ts=now - 100))
    store.ingest_message(InboxMessage(conversation_id="wechat:A:zhang", platform_msg_id="z2", direction="out", text="在的" * 60, ts=now - 50))
    store.ingest_message(InboxMessage(conversation_id="wechat:A:li", platform_msg_id="l1", text="报价多少", ts=now - 30))
    store.ingest_message(InboxMessage(conversation_id="wechat:A:li", platform_msg_id="old", text="很久以前", ts=now - 90_000))
    store.ingest_message(InboxMessage(conversation_id="wechat:A:li", platform_msg_id="del", text="撤回了", ts=now - 10))
    store.soft_delete_by_platform_msg_ids("wechat", "A", ["del"], deleted_by="peer")
    store.ingest_message(InboxMessage(conversation_id="wechat:B:wang", platform_msg_id="w1", text="B 号的消息", ts=now - 5))

    rep = store.account_activity_report("wechat", "A", since_ts=now - 86_400, limit=10, text_chars=8)
    assert (rep["in_n"], rep["out_n"], rep["peers"], rep["peers_replied"]) == (2, 1, 2, 1)
    assert rep["last_in_ts"] == now - 30 and rep["last_out_ts"] == now - 50
    tl = rep["timeline"]
    assert [m["display_name"] for m in tl] == ["李四", "张三", "张三"], "ts 降序，窗口外/对端删的不进时间线"
    assert tl[1]["direction"] == "out" and tl[1]["text"] == "在的在的在的在的…"
    assert "B 号" not in "".join(m["text"] for m in tl)

    rep_b = store.account_activity_report("wechat", "B", since_ts=now - 86_400)
    assert (rep_b["in_n"], rep_b["out_n"], rep_b["peers"], rep_b["peers_replied"]) == (1, 0, 1, 0)
    assert store.account_activity_report("wechat", "", since_ts=0)["timeline"] == []
    store.close()
