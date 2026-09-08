# -*- coding: utf-8 -*-
"""P-2 B / C / D / E（#259 #252 · D-P1 72h · D-P4 账号级停联名单，2026-09-08）。

B：4 天前入站 → auto_generate_draft 不起草 + 清单 +1 + `[draft] skip=stale_inbound`；3 天内正常。
D：三按钮 ignore / manual 落地；清单快照；login_review 登记一次。
E：freeze → 名单行；unfreeze → unfrozen_ts 不删行；导出 → 清库 → 导入 → 名单在；
   登录回填结算：Sinue「Never write me again」回填 → 会话冻结 + 名单有行；名单在场的 peer 复冻。
C：split_targets 四栏计数（active / dormant / frozen / self_chat）。
"""
from __future__ import annotations

import logging
import time

import pytest

from src.ai.chat_assistant_service import quick_risk
from src.inbox import account_blocklist as ab
from src.inbox import dormant_review as dr
from src.inbox import stop_contact as sc
from src.inbox.drafts import DraftService
from src.inbox.store import InboxStore
from src.integrations.protocol_bridge import ingest_incoming

PLAT, ACCT = "whatsapp", "17345893728"
SINUE = "12134989840"
CID_S = f"{PLAT}:{ACCT}:{SINUE}"


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_AUTOSEND_SHADOW_DIR", str(tmp_path / "shadow"))
    from src.inbox import autosend_policy as pol
    monkeypatch.delenv(pol.ENV_POLICY_MODE, raising=False)
    monkeypatch.setattr(pol, "current_policy_mode", lambda: pol.POLICY_SHADOW)
    ab.reset_for_tests()
    dr.reset_for_tests()
    yield
    ab.reset_for_tests()
    dr.reset_for_tests()


@pytest.fixture
def store(tmp_path):
    s = InboxStore(tmp_path / "inbox.db")
    yield s
    s.close()


def _conv(ck: str, name: str = "Sinue"):
    return {"conversation_id": f"{PLAT}:{ACCT}:{ck}", "platform": PLAT, "account_id": ACCT,
            "chat_key": ck, "display_name": name}


def _push(store, ck, text, ts, mid, direction="in", backfill=False, name="Sinue"):
    return ingest_incoming(store, platform=PLAT, account_id=ACCT, chat_key=ck, name=name,
                           text=text, ts=ts, msg_id=mid, direction=direction,
                           backfill=backfill, backfill_source="history_set" if backfill else "")


# ═══════════════ B 年龄闸 ═══════════════

def test_stale_inbound_4d_not_drafted_and_listed(store, caplog):
    svc = DraftService(inbox_store=store, risk_fn=quick_risk)
    store.register_new_inbound_cb(lambda c, t: None)   # 不装拟稿回调，手动调
    ts4d = time.time() - 4 * 86400
    _push(store, "447349041791", "hello are you there", ts4d, "m1", name="Ben")
    conv = _conv("447349041791", "Ben")
    with caplog.at_level(logging.INFO):
        did = svc.auto_generate_draft(conv, "hello are you there", automation_mode="auto_ai")
    assert did is None, "4 天前的入站不该起草"
    assert any("[draft] skip=stale_inbound" in r.getMessage() and "age_h=" in r.getMessage()
               for r in caplog.records)
    snap = dr.snapshot(store)
    assert snap["count"] == 1 and snap["items"][0]["conversation_id"] == conv["conversation_id"]
    assert snap["items"][0]["reason"] == dr.REASON_STALE
    assert not [d for d in store.list_drafts(conversation_id=conv["conversation_id"], limit=5)]


def test_fresh_inbound_within_3d_drafts_normally(store):
    svc = DraftService(inbox_store=store, risk_fn=quick_risk)
    ts = time.time() - 2 * 86400
    _push(store, "447825777531", "hi there", ts, "m1", name="Amy")
    conv = _conv("447825777531", "Amy")
    did = svc.auto_generate_draft(conv, "hi there", automation_mode="review")
    assert did, "3 天内入站应正常起草"
    assert dr.snapshot(store)["count"] == 0


def test_age_gate_config_and_bypass(store, monkeypatch):
    assert dr.max_inbound_age_hours({}) == 72.0
    assert dr.max_inbound_age_hours({"inbox": {"auto_draft": {"max_inbound_age_hours": 24}}}) == 24.0
    assert dr.max_inbound_age_hours({"inbox": {"auto_draft": {"max_inbound_age_hours": 0}}}) == 0.0
    ts = time.time() - 4 * 86400
    _push(store, "u9", "old msg", ts, "m1")
    conv = _conv("u9")
    assert dr.check_stale_inbound(store, conv, "old msg") is not None
    # 关闭 → 放行
    assert dr.check_stale_inbound(store, conv, "old msg",
                                  config={"inbox": {"auto_draft": {"max_inbound_age_hours": 0}}}) is None
    # 清单预览稿旁路
    assert dr.check_stale_inbound(store, dict(conv, dormant_review=True), "old msg") is None
    # 无消息行 → 判不出 → 放行
    assert dr.check_stale_inbound(store, _conv("nobody"), "x") is None


# ═══════════════ D 清单三按钮 ═══════════════

def test_dormant_actions_ignore_and_manual(store):
    ts = time.time() - 5 * 86400
    _push(store, "u1", "old", ts, "m1")
    _push(store, "u2", "old2", ts, "m2")
    dr.record_dormant(store, _conv("u1"), text="old", inbound_ts=ts, age_h=120, reason=dr.REASON_STALE)
    dr.record_dormant(store, _conv("u2"), text="old2", inbound_ts=ts, age_h=120, reason=dr.REASON_STALE)
    assert dr.snapshot(store)["count"] == 2
    r = dr.apply_action(store, _conv("u1")["conversation_id"], "ignore", actor="agent")
    assert r["ok"] and dr.IGNORED_TAG in store.get_conv_tags(_conv("u1")["conversation_id"])
    r = dr.apply_action(store, _conv("u2")["conversation_id"], "manual", actor="agent")
    assert r["ok"] and store.get_automation_mode_if_set(_conv("u2")["conversation_id"]) == "manual"
    assert dr.snapshot(store)["count"] == 0
    assert dr.get_dormant_store(store).get(_conv("u1")["conversation_id"])["status"] == "ignored"
    assert dr.apply_action(store, "x", "explode")["ok"] is False
    # 已忽略的不因同一条旧入站复活；更新的入站可复活
    assert dr.record_dormant(store, _conv("u1"), text="old", inbound_ts=ts, age_h=120,
                             reason=dr.REASON_STALE) is False
    assert dr.record_dormant(store, _conv("u1"), text="newer", inbound_ts=ts + 100, age_h=119,
                             reason=dr.REASON_STALE) is True


# ═══════════════ E 名单持久 + 登录扫描 ═══════════════

def test_freeze_writes_blocklist_unfreeze_marks_not_deletes(store):
    sc.freeze_conversation(store, platform=PLAT, account_id=ACCT, chat_key=SINUE,
                           conversation_id=CID_S, reason="stop_contact", hits=["never write me again"])
    bl = ab.get_blocklist(store)
    row = bl.get(PLAT, ACCT, SINUE)
    assert row and row["reason"] == "stop_contact" and float(row["unfrozen_ts"]) == 0
    assert bl.is_blocked(PLAT, ACCT, SINUE)
    sc.unfreeze_conversation(store, CID_S, actor="agent")
    row = bl.get(PLAT, ACCT, SINUE)
    assert row is not None, "解冻不删名单行"
    assert float(row["unfrozen_ts"]) > 0 and row["unfrozen_by"] == "agent"
    assert not bl.is_blocked(PLAT, ACCT, SINUE)
    # 再次命中 → 重新冻结（unfrozen 归 0，hits+1）
    bl.add(PLAT, ACCT, SINUE, hit_text="stop texting")
    row = bl.get(PLAT, ACCT, SINUE)
    assert bl.is_blocked(PLAT, ACCT, SINUE) and int(row["hits"]) == 2
    # 自伤不进名单
    sc.freeze_conversation(store, platform=PLAT, account_id=ACCT, chat_key="u7",
                           conversation_id=f"{PLAT}:{ACCT}:u7", reason="self_harm")
    assert bl.get(PLAT, ACCT, "u7") is None


def test_blocklist_export_wipe_import_roundtrip(tmp_path):
    bl = ab.AccountBlocklist(tmp_path / "a.db")
    bl.add(PLAT, ACCT, SINUE, hit_text="never write me again", ts=100.0)
    bl.add(PLAT, ACCT, "u2", hit_text="别再发了", ts=200.0)
    bl.mark_unfrozen(PLAT, ACCT, "u2", by="agent", ts=300.0)
    rows = bl.export_rows(PLAT, ACCT)
    assert len(rows) == 2 and all(r["type"] == "blocklist" for r in rows)
    bl.close()
    fresh = ab.AccountBlocklist(tmp_path / "b.db")      # 清库 = 新库
    st = fresh.import_rows(rows)
    assert st["inserted"] == 2
    assert fresh.is_blocked(PLAT, ACCT, SINUE)
    assert not fresh.is_blocked(PLAT, ACCT, "u2") and fresh.get(PLAT, ACCT, "u2")["unfrozen_by"] == "agent"
    # 重复导入幂等；导入的解冻行不会把本地已冻结的改成解冻
    fresh.add(PLAT, ACCT, "u2", hit_text="stop")
    st2 = fresh.import_rows(rows)
    assert st2["updated"] == 2 and fresh.is_blocked(PLAT, ACCT, "u2")
    # 重定向到新号
    st3 = fresh.import_rows(rows, account_id="newacct")
    assert st3["inserted"] == 2 and fresh.is_blocked(PLAT, "newacct", SINUE)
    fresh.close()


def test_migration_kit_carries_blocklist(tmp_path, store):
    """迁移包带 blocklist.jsonl；新库 import_from_kit 后名单在。"""
    ab.get_blocklist(store).add(PLAT, ACCT, SINUE, hit_text="never write me again")
    _push(store, SINUE, "Never write me again", time.time() - 86400, "m1")
    from src.inbox.migration_export import build_migration_kit
    kit = build_migration_kit(store, PLAT, ACCT, out_dir=tmp_path)
    import zipfile
    with zipfile.ZipFile(kit.path) as zf:
        assert ab.KIT_MEMBER in zf.namelist()
        lines = [l for l in zf.read(ab.KIT_MEMBER).decode("utf-8").splitlines() if l.strip()]
    assert len(lines) == 1 and SINUE in lines[0]
    fresh = ab.AccountBlocklist(tmp_path / "fresh.db")
    st = fresh.import_from_kit(kit.path)
    assert st["inserted"] == 1 and fresh.is_blocked(PLAT, ACCT, SINUE)
    fresh.close()


def test_login_backfill_settle_freezes_sinue_and_lists_dormant(store, caplog):
    """重装 + 登录：history_set 回填 6 个老会话（含 Sinue「Never write me again」）→
    零起草、Sinue 冻结 + 名单有行、其余未回复旧会话进清单、账号已全自动 → login_review 一条。"""
    calls = []
    store.register_new_inbound_cb(lambda c, t: calls.append(c))
    store.set_automation_mode(f"{PLAT}:{ACCT}:u1", "auto_ai", source="account_bulk")
    old = time.time() - 6 * 86400
    _push(store, SINUE, "Never write me again, please", old, "h1", backfill=True)
    _push(store, "u1", "are you there?", old - 100, "h2", backfill=True, name="U1")
    _push(store, "u2", "thanks bye", old - 200, "h3", backfill=True, name="U2")
    _push(store, "u2", "welcome", old - 100, "h4", direction="out", backfill=True, name="U2")
    _push(store, ACCT, "买牛奶", old, "h5", backfill=True, name="me")     # 自聊
    assert calls == [], "回填触发了拟稿回调"
    with caplog.at_level(logging.INFO):
        res = dr.settle_backfill(store, PLAT, ACCT)
    assert res["scan"]["hits"] == 1 and CID_S in res["scan"]["frozen"]
    assert sc.frozen_reason(store, CID_S) == "stop_contact"
    assert sc.STOP_CONTACT_TAG in store.get_conv_tags(CID_S)
    assert ab.get_blocklist(store).is_blocked(PLAT, ACCT, SINUE)
    assert any("[stop-contact] login_scan account=" in r.getMessage() and "hits=1" in r.getMessage()
               for r in caplog.records)
    items = {i["conversation_id"]: i for i in dr.snapshot(store)["items"]}
    assert f"{PLAT}:{ACCT}:u1" in items and items[f"{PLAT}:{ACCT}:u1"]["reason"] == dr.REASON_BACKFILL
    assert f"{PLAT}:{ACCT}:u2" not in items, "最后一条是我们的出站 = 已回复，不进清单"
    assert CID_S not in items, "已冻结不进清单"
    assert f"{PLAT}:{ACCT}:{ACCT}" not in items, "自聊不进清单"
    lr = dr.snapshot(store)["login_reviews"]
    assert len(lr) == 1 and lr[0]["platform"] == PLAT and lr[0]["account_id"] == ACCT
    assert dr.get_dormant_store(store).ack_login_review(lr[0]["id"], by="agent")
    assert dr.snapshot(store)["login_reviews"] == []
    # 结算是一次性的：再 settle 无桶
    assert dr.settle_backfill(store, PLAT, ACCT)["conversations"] == 0


def test_blocklist_peer_refrozen_on_login_without_conv_tag(store):
    """名单在场（上一台机器导入）、会话标记丢了 → 登录回填该会话即复冻。"""
    ab.get_blocklist(store).add(PLAT, ACCT, SINUE, hit_text="never write me again", source="import")
    _push(store, SINUE, "hey", time.time() - 86400, "h1", backfill=True)
    assert sc.frozen_reason(store, CID_S) == ""
    res = dr.settle_backfill(store, PLAT, ACCT)
    assert res["scan"]["relisted"] == 1 and sc.frozen_reason(store, CID_S) == "stop_contact"
    assert store.get_automation_mode_if_set(CID_S) == "manual"


def test_proactive_predicate_blocks_stop_contact_and_self_chat(store):
    """永不 opener / 关怀 / 目标：主动触达统一谓词对名单在场与自聊返回不可发。"""
    from src.companion.proactive_peer_hygiene import proactive_candidate_ok
    ab.get_blocklist(store).add(PLAT, ACCT, SINUE, hit_text="never write me again")
    ok, why = proactive_candidate_ok({"platform": PLAT, "account_id": ACCT, "chat_key": SINUE})
    assert (ok, why) == (False, "stop_contact")
    ok, why = proactive_candidate_ok({"platform": PLAT, "account_id": ACCT, "chat_key": ACCT})
    assert ok is False and why in ("self_chat", "own_fleet")   # 自家账号索引先拦也算
    ok, _ = proactive_candidate_ok({"platform": PLAT, "account_id": ACCT, "chat_key": "u1"})
    assert ok is True
    ab.get_blocklist(store).mark_unfrozen(PLAT, ACCT, SINUE, by="agent")
    assert proactive_candidate_ok({"platform": PLAT, "account_id": ACCT, "chat_key": SINUE})[0] is True


def test_account_scope_migration_blocklist_seed(store, tmp_path):
    """存量迁移 CLI 段：conv_tags「客户要求停联」→ account_blocklist.db（dry-run 只读、apply 只增）。"""
    from src.utils.account_scope_migration import apply_blocklist_seed, plan_blocklist_seed
    _push(store, SINUE, "x", time.time() - 100, "m1")
    _push(store, "u2", "y", time.time() - 100, "m2")
    store.set_conv_tags(CID_S, [sc.STOP_CONTACT_TAG])
    store.set_conv_tags(f"{PLAT}:{ACCT}:u2", ["不需客户要求停联确认"])   # 子串假阳性不进
    plan = plan_blocklist_seed(str(store._db_path))
    assert [p["peer"] for p in plan] == [SINUE]
    bl_db = tmp_path / "bl.db"
    res = apply_blocklist_seed(str(store._db_path), str(bl_db))
    assert res["inserted"] == 1
    res2 = apply_blocklist_seed(str(store._db_path), str(bl_db))
    assert res2["inserted"] == 0 and res2["updated"] == 1
    bl = ab.AccountBlocklist(bl_db)
    assert bl.is_blocked(PLAT, ACCT, SINUE) and bl.get(PLAT, ACCT, SINUE)["source"] == "seed:conv_tag"
    bl.close()


def test_seed_from_conv_tags(store):
    """存量迁移：库里已带「客户要求停联」标签的会话首次取用即补进名单。"""
    _push(store, SINUE, "x", time.time() - 100, "m1")
    store.set_conv_tags(CID_S, [sc.STOP_CONTACT_TAG])
    ab.reset_for_tests()
    assert ab.get_blocklist(store).is_blocked(PLAT, ACCT, SINUE)


# ═══════════════ C 切档分栏 ═══════════════

def test_split_targets_four_columns(store):
    now = time.time()
    _push(store, "a1", "recent", now - 3600, "m1", name="A1")
    _push(store, "a2", "recent replied", now - 7200, "m2", name="A2")
    _push(store, "a2", "ok", now - 3600, "m3", direction="out", name="A2")
    _push(store, "d1", "old unreplied", now - 5 * 86400, "m4", name="D1")
    _push(store, "d2", "old", now - 9 * 86400, "m5", name="D2")
    _push(store, "d2", "we replied", now - 8 * 86400, "m6", direction="out", name="D2")
    _push(store, SINUE, "Never write me again", now - 86400, "m7")
    sc.freeze_conversation(store, platform=PLAT, account_id=ACCT, chat_key=SINUE,
                           conversation_id=CID_S, reason="stop_contact")
    _push(store, ACCT, "memo", now - 60, "m8", name="me")
    sp = dr.split_targets(store, PLAT, ACCT, None, now=now)
    c = sp["counts"]
    assert c["active"] == 2 and c["dormant"] == 2 and c["dormant_unreplied"] == 1
    assert c["frozen"] == 1 and c["self_chat"] == 1
    names = {r["name"]: r for r in sp["dormant"]}
    assert names["D1"]["unreplied"] is True and names["D2"]["unreplied"] is False
    assert sp["frozen"][0]["chat_key"] == SINUE and sp["self_chat"][0]["chat_key"] == ACCT
    # 只分给定 targets
    sp2 = dr.split_targets(store, PLAT, ACCT, [f"{PLAT}:{ACCT}:a1", f"{PLAT}:{ACCT}:d1"], now=now)
    assert sp2["counts"]["active"] == 1 and sp2["counts"]["dormant"] == 1 and sp2["counts"]["frozen"] == 0


def test_plan_account_bulk_split_and_apply_only_checked(store):
    """切全自动：plan 带四栏、frozen / self_chat 剔出 targets；confirm 只勾 2 个 → 只改 2 个，旧会话不碰。"""
    from src.inbox.account_bulk_mode import apply_account_bulk, plan_account_bulk
    now = time.time()
    _push(store, "a1", "recent", now - 3600, "m1", name="A1")
    _push(store, "a2", "recent2", now - 7200, "m2", name="A2")
    _push(store, "d1", "old unreplied", now - 5 * 86400, "m4", name="D1")
    _push(store, SINUE, "Never write me again", now - 86400, "m7")
    sc.freeze_conversation(store, platform=PLAT, account_id=ACCT, chat_key=SINUE,
                           conversation_id=CID_S, reason="stop_contact")
    _push(store, ACCT, "memo", now - 60, "m8", name="me")
    plan = plan_account_bulk(store, PLAT, ACCT, "auto_ai")
    assert plan["frozen"] == 1 and plan["self_chat"] == 1 and plan["dormant"] == 1
    assert set(plan["targets"]) == {f"{PLAT}:{ACCT}:a1", f"{PLAT}:{ACCT}:a2", f"{PLAT}:{ACCT}:d1"}
    assert CID_S not in plan["targets"] and f"{PLAT}:{ACCT}:{ACCT}" not in plan["targets"]
    assert set(plan["active_targets"]) == {f"{PLAT}:{ACCT}:a1", f"{PLAT}:{ACCT}:a2"}
    assert plan["dormant_targets"] == [f"{PLAT}:{ACCT}:d1"]
    res = apply_account_bulk(store, PLAT, ACCT, "auto_ai", actor="t",
                             only=[f"{PLAT}:{ACCT}:a1", f"{PLAT}:{ACCT}:a2"])
    assert res["changed"] == 2 and res["skipped_unchecked"] == 1
    assert store.get_automation_mode_if_set(f"{PLAT}:{ACCT}:a1") == "auto_ai"
    assert store.get_automation_mode_if_set(f"{PLAT}:{ACCT}:d1") is None, "未勾的旧会话不碰"
    assert store.get_automation_mode_if_set(CID_S) == "manual", "停联会话不碰"
    assert store.get_automation_mode_if_set(f"{PLAT}:{ACCT}:{ACCT}") is None, "自聊不碰"
    # 手动 / 半自动降档不分栏（旧口径）
    p2 = plan_account_bulk(store, PLAT, ACCT, "review")
    assert "split" not in p2 and p2["will_change"] >= 2


def test_frontend_dr_wiring_static():
    """前端接线静态钉：两栏确认框 / 横幅 / 三按钮 / 自聊名 / 登录轮询 / i18n zh+en 同键。"""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    html = (root / "src" / "web" / "templates" / "unified_inbox.html").read_text(encoding="utf-8")
    for needle in (
        'id="dr-banner"', "function _drBulkAutoConfirm(", "function _drRenderBanner(",
        "function _drAction(", "function _drLoginCheck(", "function _drPreviewInto(",
        "function _drConvName(", "_drSelfBottom(_applyPinAt(list))",
        "${esc(_drConvName(c))} ${_drSelfTag(c)}",
        "if(mode==='auto_ai' && plan.split){", "scope:'all'", "action:'ack'",
        "data-act=\"draft\"", "data-act=\"manual\"", "data-act=\"ignore\"",
        "/api/unified-inbox/dormant-review?limit=200",
        "unified-inbox.css?v=20260908dr",
    ):
        assert needle in html, needle
    css = (root / "src" / "web" / "static" / "workspace" / "unified-inbox.css").read_text(encoding="utf-8")
    for cls in (".dr-card", ".dr-cols", ".dr-banner", ".dr-self-tag", ".dr-row-draft"):
        assert cls in css, cls
    import re as _re
    keys = set(_re.findall(r"inbox\.dr\.[a-z_]+", html))
    assert keys, "模板里应引用 inbox.dr.* 键"
    from src.web.i18n_packs import inbox_workspace as pack
    zh = {k: v for k, v in vars(pack).items() if isinstance(v, dict) and "inbox.dr.self_name" in v}
    assert zh, "inbox_workspace 里找不到 inbox.dr.* 词包"
    dicts = [d for d in vars(pack).values() if isinstance(d, dict) and "inbox.dr.self_name" in d]
    assert len(dicts) >= 2, "zh / en 两包都应含 inbox.dr.*"
    for d in dicts:
        missing = sorted(k for k in keys if k not in d)
        assert not missing, missing
