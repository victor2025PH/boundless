# -*- coding: utf-8 -*-
"""智聊可靠性修复（2026-10-08，与客群 / 渠道方向无关的部分）。

- P0-1：chengjie-tests 门禁覆盖 devin/** 发版分支；README 徽章指向 chengjie-tests.yml
- P0-5：/api/accounts 状态以桥接心跳为准（心跳过期 → stale，不再 online）
- P1-1：升级告警等待时长以 last_in_ts 为准 + 异常时间戳钳制；unclaimed 同会话 1 小时 1 条；
        存量脏数据只读体检脚本
- P0-4（归因部分）：新出站行一律带 sent_by（ai/agent/phone/script/system）
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.inbox.models import InboxConversation, InboxMessage
from src.inbox.store import (
    SENT_BY_DEFAULT_OUT, SENT_BY_VALUES, InboxStore, normalize_sent_by,
)

_ENGINE = Path(__file__).resolve().parents[1]
_REPO = _ENGINE.parents[1]
CID = "whatsapp:17345893506:13308422244"


# ── P0-1 ────────────────────────────────────────────────────────────────

def test_ci_workflow_covers_release_branches_and_badge():
    wf = _REPO / ".github" / "workflows" / "chengjie-tests.yml"
    if wf.is_file():   # 引擎单独打包时没有仓库根，跳过该半
        txt = wf.read_text(encoding="utf-8")
        assert "'devin/**'" in txt and "'feat-*'" in txt
    readme = (_ENGINE / "README.md").read_text(encoding="utf-8")
    assert "actions/workflows/chengjie-tests.yml/badge.svg" in readme


# ── P0-5 ────────────────────────────────────────────────────────────────

def test_heartbeat_display_status_pure():
    from src.web.desktop_bridge_presence import (
        ALIVE_WITHIN_SEC, STATUS_STALE, heartbeat_display_status,
    )
    now = 2_000_000_000.0
    # 没有心跳记录（普通桌面壳 / 非桥接）→ 不动
    assert heartbeat_display_status({}, status="online", now=now) is None
    assert heartbeat_display_status(None, status="online", now=now) is None
    fresh = {"bridge_heartbeat": {"ts": now - 10, "kind": "wechat-pc", "stats": {}}}
    assert heartbeat_display_status(fresh, status="online", now=now) == {"heartbeat_age_sec": 10}
    stale = {"bridge_heartbeat": {"ts": now - 16 * 86400, "kind": "wechat-pc", "stats": {}}}
    ov = heartbeat_display_status(stale, status="online", running=True, now=now)
    assert ov["status"] == STATUS_STALE == "stale" and ov["running"] is False
    assert ov["status_reason"] == "heartbeat_stale" and ov["heartbeat_age_sec"] == 16 * 86400
    # 刚过阈值也算过期
    edge = {"bridge_heartbeat": {"ts": now - ALIVE_WITHIN_SEC - 1}}
    assert heartbeat_display_status(edge, status="online", now=now)["status"] == "stale"
    # 运营明确设的 offline / removed / pending 不被心跳推翻
    for st in ("offline", "removed", "pending"):
        assert "status" not in heartbeat_display_status(stale, status=st, now=now)


def test_api_accounts_shows_stale_when_heartbeat_expired(auth_client):
    from src.integrations.account_registry import get_account_registry
    reg = get_account_registry()
    now = time.time()
    reg.upsert("wechat", "wx_hb_stale_t", mode="desktop", label="心跳过期号", status="online",
               meta={"bridge_heartbeat": {"ts": now - 16 * 86400, "kind": "wechat-pc",
                                          "tier": "copilot", "stats": {}}})
    reg.upsert("wechat", "wx_hb_fresh_t", mode="desktop", label="心跳正常号", status="online",
               meta={"bridge_heartbeat": {"ts": now, "kind": "wechat-pc",
                                          "tier": "copilot", "stats": {}}})
    d = auth_client.get("/api/accounts").json()
    by = {(a["platform"], a["account_id"]): a for a in d["accounts"]}
    s = by[("wechat", "wx_hb_stale_t")]
    assert s["status"] == "stale" and s["running"] is False
    assert s["status_reason"] == "heartbeat_stale" and s["heartbeat_age_sec"] >= 16 * 86400 - 5
    assert s.get("registry_status") == "online"
    f = by[("wechat", "wx_hb_fresh_t")]
    assert f["status"] == "online"                # 心跳新鲜 → 沿用注册表状态
    assert f.get("status_reason") != "heartbeat_stale"


# ── P1-1 ────────────────────────────────────────────────────────────────

def test_inbound_wait_base_clamps_bad_timestamps():
    from src.web.routes.unified_inbox_sla import MIN_VALID_INBOUND_TS, _inbound_wait_base
    now = 2_000_000_000.0
    assert MIN_VALID_INBOUND_TS == 1420070400.0
    assert _inbound_wait_base({"last_in_ts": 0}, 0, now) is None              # ≈0
    assert _inbound_wait_base({"last_in_ts": 5.0}, 1_000_000.0, now) is None  # 1970 年
    assert _inbound_wait_base({"last_in_ts": 1_400_000_000.0}, None, now) is None  # 早于 2015
    assert _inbound_wait_base({"last_in_ts": "bad"}, "x", now) is None
    # 以 last_in_ts 为准；末条消息 ts 是脏值时不被它拖成「等了 56 年」
    assert _inbound_wait_base({"last_in_ts": now - 3600}, 12.0, now) == now - 3600
    # last_in_ts 缺失 → 回落末条消息 ts
    assert _inbound_wait_base({}, now - 7200, now) == now - 7200
    # 两者都可信 → 取较新（不夸大等待）
    assert _inbound_wait_base({"last_in_ts": now - 9000}, now - 8000, now) == now - 8000
    # 未来时间戳钳到 now
    assert _inbound_wait_base({"last_in_ts": now + 999}, None, now) == now


def _esc_client(store):
    from src.web.routes.unified_inbox_workspace_escalation_routes import (
        register_workspace_escalation_routes,
    )
    app = FastAPI()
    register_workspace_escalation_routes(app, api_auth=lambda r: True)
    app.state.inbox_store = store
    return TestClient(app)


def _conv(store, ck, *, last_ts, unread):
    store.upsert_conversation(InboxConversation(
        conversation_id=f"line:a:{ck}", platform="line", account_id="a", chat_key=ck,
        display_name=ck, language="ja", last_text="hi", last_ts=last_ts, unread=unread))
    return f"line:a:{ck}"


def test_escalation_snapshot_uses_last_in_ts_and_skips_bad_ts(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    now = time.time()
    good = _conv(store, "good", last_ts=now - 3 * 3600, unread=1)
    store.ingest_message(InboxMessage(conversation_id=good, platform_msg_id="g1", direction="in",
                                      text="在吗", ts=now - 3 * 3600))
    bad = _conv(store, "bad", last_ts=5, unread=0)             # LINE 合成 ts≈0
    store.ingest_message(InboxMessage(conversation_id=bad, platform_msg_id="b1", direction="in",
                                      text="hello", ts=5.0))
    mixed = _conv(store, "mixed", last_ts=now - 4 * 3600, unread=1)  # last_in_ts 可信、消息 ts 脏
    store.ingest_message(InboxMessage(conversation_id=mixed, platform_msg_id="m1", direction="in",
                                      text="hey", ts=100.0))
    d = _esc_client(store).get("/api/workspace/escalations").json()
    by = {i["conversation_id"]: i for i in d["items"]}
    assert bad not in by                                         # 算不出等多久 → 不报
    assert d["skipped_bad_ts"] >= 1
    assert abs(by[good]["wait_sec"] - 3 * 3600) < 120 and by[good]["reason"] == "unclaimed"
    assert abs(by[mixed]["wait_sec"] - 4 * 3600) < 120         # 用 last_in_ts，不是 56 年
    assert all(i["wait_sec"] < 400 * 86400 for i in d["items"])
    store.close()


def test_unclaimed_alert_cooldown_one_per_hour():
    from src.web.routes.unified_inbox_realtime_routes import (
        UNCLAIMED_ALERT_COOLDOWN_SEC, _esc_alert_allowed,
    )
    assert UNCLAIMED_ALERT_COOLDOWN_SEC == 3600.0
    led: dict = {}
    t = 1_800_000_000.0
    assert _esc_alert_allowed(led, "c1", "unclaimed", t) is True
    assert _esc_alert_allowed(led, "c1", "unclaimed", t + 60) is False
    assert _esc_alert_allowed(led, "c1", "unclaimed", t + 3599) is False
    assert _esc_alert_allowed(led, "c2", "unclaimed", t + 60) is True      # 会话之间互不影响
    assert _esc_alert_allowed(led, "c1", "unclaimed", t + 3600) is True    # 满 1 小时再报
    # 其他原因不节流（保持原语义）
    assert _esc_alert_allowed(led, "c1", "holder_offline", t + 3601) is True
    assert _esc_alert_allowed(led, "c1", "holder_offline", t + 3602) is True
    # 软上限
    big: dict = {f"k{i}": t for i in range(2100)}
    _esc_alert_allowed(big, "new", "unclaimed", t + 1)
    assert len(big) <= 2048 and "new" in big


def test_realtime_wiring_records_with_cooldown():
    rt = (_ENGINE / "src" / "web" / "routes" / "unified_inbox_realtime_routes.py").read_text(encoding="utf-8")
    assert "dedup_sec=UNCLAIMED_ALERT_COOLDOWN_SEC" in rt
    assert "_esc_alert_allowed(\n" in rt.replace("\r\n", "\n")


def _mk_escalation_db(path: Path) -> None:
    store = InboxStore(path)
    now = time.time()
    good = _conv(store, "good", last_ts=now - 7200, unread=1)
    store.ingest_message(InboxMessage(conversation_id=good, platform_msg_id="g1", direction="in",
                                      text="在吗", ts=now - 7200))
    bad = _conv(store, "bad", last_ts=5, unread=0)
    store.ingest_message(InboxMessage(conversation_id=bad, platform_msg_id="b1", direction="in",
                                      text="x", ts=5.0))
    store.record_escalation(good, reason="unclaimed", wait_sec=7200, ts=now)            # 正确
    store.record_escalation(good, reason="unclaimed", wait_sec=99_999_999, ts=now + 4000)  # 偏大
    store.record_escalation(bad, reason="unclaimed", wait_sec=1_700_000_000, ts=now)    # 56 年
    store.close()


def test_repair_escalation_wait_sec_is_dry_run(tmp_path):
    import importlib.util
    p = _ENGINE / "tools" / "repair_escalation_wait_sec.py"
    spec = importlib.util.spec_from_file_location("repair_escalation_wait_sec", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    db = tmp_path / "inbox.db"
    _mk_escalation_db(db)
    before = db.read_bytes()
    conn = mod.open_ro(db)
    try:
        rep = mod.analyze(conn, sample=10)
        try:   # 只读打开：写必失败
            conn.execute("UPDATE escalations SET wait_sec=0")
            raise AssertionError("ro connection accepted a write")
        except sqlite3.OperationalError:
            pass
    finally:
        conn.close()
    c = rep["counts"]
    assert c["total"] == 3 and c["ok"] == 1 and c["recalc"] == 1 and c["bad_base"] == 1
    assert c["wait_over_1y"] == 2
    kinds = {p["kind"]: p for p in rep["_all"]}
    assert kinds["bad_base"]["new_wait_sec"] == 0
    assert abs(kinds["recalc"]["new_wait_sec"] - (7200 + 4000)) < 5
    sql_out = tmp_path / "fix.sql"
    assert mod.main(["--db", str(db), "--json", "--sql-out", str(sql_out)]) == 0
    sql = sql_out.read_text(encoding="utf-8")
    assert sql.count("UPDATE escalations SET wait_sec=") == 2 and "BEGIN;" in sql
    assert db.read_bytes() == before                         # 零写入
    assert "--apply" not in p.read_text(encoding="utf-8").split('"""', 2)[2]


# ── P0-4 归因 ───────────────────────────────────────────────────────────

def _rows(store):
    return {r["platform_msg_id"] or r["message_id"]: r
            for r in store.list_recent_messages(CID, limit=100)}


def _batch(store, msgs):
    conv = InboxConversation(conversation_id=CID, platform="whatsapp", account_id="17345893506",
                             chat_key="13308422244")
    return store.ingest_batch(conv, msgs)


def test_sent_by_values_and_aliases():
    assert SENT_BY_VALUES == ("agent", "ai", "phone", "script", "system")
    assert SENT_BY_DEFAULT_OUT == "phone"
    assert normalize_sent_by(" AI ") == "ai"
    assert normalize_sent_by("manual") == "agent" and normalize_sent_by("bot") == "ai"
    assert normalize_sent_by("external") == "phone" and normalize_sent_by("drill") == "script"
    assert normalize_sent_by("sys") == "system"
    assert normalize_sent_by("robot") == "" and normalize_sent_by(None) == ""


def test_new_outbound_rows_always_attributed(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    t = 1_757_000_000.0
    for i in range(20):                                   # RPA / 手机回抄形态：不带 sent_by
        store.ingest_message(InboxMessage(conversation_id=CID, platform_msg_id=f"o{i}",
                                          direction="out", text=f"出站{i}", ts=t + i))
    _batch(store, [InboxMessage(conversation_id=CID, platform_msg_id="b1", direction="out",
                                text="批量出站", ts=t + 50),
                   InboxMessage(conversation_id=CID, platform_msg_id="s1", direction="out",
                                text="演练", ts=t + 51, sent_by="script"),
                   InboxMessage(conversation_id=CID, platform_msg_id="y1", direction="out",
                                text="系统通知", ts=t + 52, sent_by="system"),
                   InboxMessage(conversation_id=CID, platform_msg_id="i1", direction="in",
                                text="客户", ts=t + 53, sent_by="ai")])
    rows = _rows(store)
    outs = [r for r in rows.values() if r["direction"] == "out"]
    assert outs and all(r["sent_by"] in SENT_BY_VALUES for r in outs)   # 空 sent_by 占比 0%
    assert rows["o0"]["sent_by"] == "phone" and rows["b1"]["sent_by"] == "phone"
    assert rows["s1"]["sent_by"] == "script" and rows["y1"]["sent_by"] == "system"
    assert rows["i1"]["sent_by"] == ""                                   # 入站永远空
    store.close()


def test_hash_twin_sent_by_inherited_by_authoritative_row(tmp_path):
    """乐观 hash 行（A 线带 ai）先到、权威回显行（pmid、不带 sent_by）后到 → 删孪生时继承 ai。"""
    store = InboxStore(tmp_path / "inbox.db")
    t = 1_757_000_000.0
    _batch(store, [InboxMessage(conversation_id=CID, platform_msg_id="", direction="out",
                                text="晚安呀", ts=t, sent_by="ai")])
    assert [r["sent_by"] for r in _rows(store).values()] == ["ai"]
    _batch(store, [InboxMessage(conversation_id=CID, platform_msg_id="777", direction="out",
                                text="晚安呀", ts=t + 3)])
    rows = _rows(store)
    assert list(rows) == ["777"] and rows["777"]["sent_by"] == "ai"
    # 坐席打点曾认领孪生 → 认领指针挪到权威行
    store.record_agent_send(CID, "a1", agent_name="小王", ts=t + 100, text="人工一句")
    _batch(store, [InboxMessage(conversation_id=CID, platform_msg_id="", direction="out",
                                text="人工一句", ts=t + 101)])
    twin_mid = [r for r in _rows(store).values() if r["text"] == "人工一句"][0]["message_id"]
    assert store.list_agent_sends(CID)[0]["claimed_mid"] == twin_mid
    _batch(store, [InboxMessage(conversation_id=CID, platform_msg_id="778", direction="out",
                                text="人工一句", ts=t + 104)])
    rows = _rows(store)
    assert rows["778"]["sent_by"] == "agent"
    assert store.list_agent_sends(CID)[0]["claimed_mid"] == rows["778"]["message_id"]
    store.close()


def test_conflict_upgrades_default_phone_to_explicit(tmp_path):
    """手机回显先到（缺省 phone）、编排器镜像同主键后到（显式 ai）→ 升级；只升不降。"""
    store = InboxStore(tmp_path / "inbox.db")
    t = 1_757_000_000.0
    m = dict(conversation_id=CID, platform_msg_id="m1", direction="out", text="同一句", ts=t)
    store.ingest_message(InboxMessage(**m))
    assert _rows(store)["m1"]["sent_by"] == "phone"
    assert store.ingest_message(InboxMessage(**m, sent_by="ai")) is False
    assert _rows(store)["m1"]["sent_by"] == "ai"
    store.ingest_message(InboxMessage(**m, sent_by="agent"))       # 已是显式 ai → 不改
    assert _rows(store)["m1"]["sent_by"] == "ai"
    m2 = dict(conversation_id=CID, platform_msg_id="m2", direction="out", text="批量", ts=t + 5)
    _batch(store, [InboxMessage(**m2)])
    _batch(store, [InboxMessage(**m2, sent_by="manual")])
    assert _rows(store)["m2"]["sent_by"] == "agent"
    store.close()


def test_script_and_system_rows_never_claimed_by_agent_send(tmp_path):
    store = InboxStore(tmp_path / "inbox.db")
    t = 1_757_000_000.0
    store.record_agent_send(CID, "a1", agent_name="小王", ts=t, text="演练文本")
    store.ingest_message(InboxMessage(conversation_id=CID, platform_msg_id="s1", direction="out",
                                      text="演练文本", ts=t + 1, sent_by="script"))
    assert _rows(store)["s1"]["sent_by"] == "script"
    assert store.list_agent_sends(CID)[0]["claimed_mid"] == ""
    # 缺省 phone 行可被坐席打点改判为 agent
    store.ingest_message(InboxMessage(conversation_id=CID, platform_msg_id="p1", direction="out",
                                      text="演练文本", ts=t + 2))
    assert _rows(store)["p1"]["sent_by"] == "agent"
    store.close()


def test_orchestrator_origin_mapping():
    from src.integrations.account_orchestrator import _sent_by_for_origin
    assert _sent_by_for_origin("manual") == "agent"
    assert _sent_by_for_origin("auto") == "ai" and _sent_by_for_origin(None) == "ai"
    assert _sent_by_for_origin("script") == "script" and _sent_by_for_origin("system") == "system"


def test_aline_mirror_rows_tagged_ai():
    from src.client.sender import TelegramSenderMixin

    class _R(TelegramSenderMixin):
        def __init__(self):
            self.account_id = "acct1"
            self._mirror_inbox = False
            self.emitted = []

        def _emit_inbox(self, **kw):
            self.emitted.append(kw)

        def _record_contact_out(self, chat_id, preview):
            return None

    r = _R()
    r._postsend_mirror_and_record(1, "回复", msg_id="5", sent_by="ai")
    assert r.emitted[0]["sent_by"] == "ai"
    r2 = _R()
    r2._mirror_out_row(1, "念稿", media_type="voice", media_ref="/x.ogg")
    assert "sent_by" not in r2.emitted[0]                          # 不传 → emit 形状不变
    src = (_ENGINE / "src" / "client" / "sender.py").read_text(encoding="utf-8")
    assert src.count('sent_by="ai"') >= 4                          # 文本回复 + 3 处语音镜像
    tc = (_ENGINE / "src" / "client" / "telegram_client.py").read_text(encoding="utf-8")
    assert 'sent_by="phone",' in tc and '_src["sent_by"] = str(sent_by)' in tc


def test_script_sender_account_outbound_is_script(tmp_path, monkeypatch):
    """Morgan 2026-10-08：报障群支持号 6834964252 的出站是脚本测试 → sent_by=script。"""
    from src.inbox.store import script_sender_accounts
    monkeypatch.delenv("CHENGJIE_SCRIPT_SENDER_ACCOUNTS", raising=False)
    assert "6834964252" in script_sender_accounts()
    store = InboxStore(tmp_path / "inbox.db")
    cid = "telegram:6834964252:-1001234567890"
    t = 1_757_000_000.0
    store.ingest_message(InboxMessage(conversation_id=cid, platform_msg_id="d1", direction="out",
                                      text="值守播报", ts=t))
    store.ingest_message(InboxMessage(conversation_id=cid, platform_msg_id="d2", direction="out",
                                      text="自动链", ts=t + 1, sent_by="ai"))
    store.ingest_message(InboxMessage(conversation_id=cid, platform_msg_id="d3", direction="out",
                                      text="坐席亲手", ts=t + 2, sent_by="agent"))
    store.ingest_message(InboxMessage(conversation_id=cid, platform_msg_id="c1", direction="in",
                                      text="收到", ts=t + 3))
    conv = InboxConversation(conversation_id=cid, platform="telegram", account_id="6834964252",
                             chat_key="-1001234567890")
    store.ingest_batch(conv, [InboxMessage(conversation_id=cid, platform_msg_id="d4", direction="out",
                                           text="批量播报", ts=t + 4)])
    rows = {r["platform_msg_id"]: r for r in store.list_recent_messages(cid, limit=20)}
    assert rows["d1"]["sent_by"] == "script" and rows["d2"]["sent_by"] == "script"
    assert rows["d4"]["sent_by"] == "script"
    assert rows["d3"]["sent_by"] == "agent" and rows["c1"]["sent_by"] == ""
    # 其他账号不受影响；环境变量可覆盖 / 关闭
    store.ingest_message(InboxMessage(conversation_id=CID, platform_msg_id="n1", direction="out",
                                      text="普通号", ts=t))
    assert _rows(store)["n1"]["sent_by"] == "phone"
    monkeypatch.setenv("CHENGJIE_SCRIPT_SENDER_ACCOUNTS", "")
    assert script_sender_accounts() == frozenset()
    store.ingest_message(InboxMessage(conversation_id=cid, platform_msg_id="d5", direction="out",
                                      text="关闭后", ts=t + 9))
    assert {r["platform_msg_id"]: r for r in store.list_recent_messages(cid, limit=20)}["d5"]["sent_by"] == "phone"
    store.close()
