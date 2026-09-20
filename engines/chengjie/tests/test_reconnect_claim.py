# -*- coding: utf-8 -*-
"""回连认领（账号资产保全 P1）门禁：匹配纯函数 / 台账 / 认领执行 / 幂等。

重点覆盖「不该误认领」的边界（弱信号不自动、bot/群/自会话排除、幂等短路），
与记忆接地同哲学：宁可漏认不可错认。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.contacts import reconnect_claim as rc


# ── 归一化 ───────────────────────────────────────────────────────────────────

def test_normalize_username():
    assert rc.normalize_username("@Alice_88 ") == "alice_88"
    assert rc.normalize_username("BOB") == "bob"
    assert rc.normalize_username("@ab") == ""          # 过短
    assert rc.normalize_username(None) == ""


def test_normalize_phone_tail_alignment():
    # 国家码书写差异：+86 138... 与裸 138... 尾 10 位一致
    a = rc.normalize_phone("+86 138-0013-8000")
    b = rc.normalize_phone("13800138000")
    assert a and a == b
    assert rc.normalize_phone("12345") == ""            # 不足 8 位
    assert rc.normalize_phone("") == ""


def test_normalize_name():
    assert rc.normalize_name("  Ann   Lee ") == "ann lee"
    assert rc.normalize_name("A") == ""                 # 单字符=噪声


def test_is_claimable_row_exclusions():
    ok = {"chat_type": "private", "peer_is_bot": 0, "chat_key": "123"}
    assert rc.is_claimable_row(ok)
    assert not rc.is_claimable_row(dict(ok, chat_type="group"))
    assert not rc.is_claimable_row(dict(ok, peer_is_bot=1))
    assert not rc.is_claimable_row(dict(ok, chat_key="me"))
    assert not rc.is_claimable_row(dict(ok, chat_key=""))


# ── 覆盖率读数 ───────────────────────────────────────────────────────────────

def test_handle_coverage():
    rows = [
        {"username": "alice", "phone": ""},
        {"username": "", "phone": "+86 13800138000"},
        {"username": "", "phone": ""},
    ]
    cov = rc.handle_coverage(rows)
    assert cov["total"] == 3
    assert cov["with_username"] == 1
    assert cov["with_phone"] == 1
    assert cov["with_any_handle"] == 2
    assert cov["coverage"] == pytest.approx(2 / 3, abs=1e-3)
    assert rc.handle_coverage([])["coverage"] == 0.0


# ── 匹配索引与候选 ───────────────────────────────────────────────────────────

def _row(cid, ck, *, name="", username="", phone="", last_ts=0.0,
         chat_type="private", bot=0):
    return {
        "conversation_id": cid, "chat_key": ck, "display_name": name,
        "username": username, "phone": phone, "last_ts": last_ts,
        "chat_type": chat_type, "peer_is_bot": bot,
    }


def test_build_match_index_conflict_keeps_recent():
    old = [
        _row("telegram:dead:1", "1", name="Ann", last_ts=100),
        _row("telegram:dead:2", "2", name="Ann", last_ts=200),
    ]
    idx = rc.build_match_index(old)
    assert idx["name"]["ann"]["conversation_id"] == "telegram:dead:2"


def test_match_exact_peer_beats_username():
    old = [
        _row("telegram:dead:111", "111", username="alice", last_ts=50),
        _row("telegram:dead:222", "222", username="bob", last_ts=60),
    ]
    idx = rc.build_match_index(old)
    new = [_row("telegram:new:111", "111", username="bob")]  # chat_key 中 111、username 中 222
    cands = rc.match_candidates(new, idx)
    assert len(cands) == 1
    c = cands[0]
    assert c["old_conversation_id"] == "telegram:dead:111"   # exact_peer 胜
    assert c["matched_on"][0] == "exact_peer"
    assert c["confidence"] == 1.0


def test_match_display_name_only_is_advisory():
    old = [_row("telegram:dead:9", "9", name="Ann Lee")]
    idx = rc.build_match_index(old)
    cands = rc.match_candidates([_row("telegram:new:77", "77", name="ann  lee")], idx)
    assert len(cands) == 1
    assert cands[0]["confidence"] == 0.5
    assert cands[0]["confidence"] < rc.AUTO_CLAIM_MIN   # 绝不够自动认领


def test_match_skips_group_bot_and_no_signal():
    old = [_row("telegram:dead:1", "1", username="alice")]
    idx = rc.build_match_index(old)
    new = [
        _row("telegram:new:1", "1", chat_type="group"),   # 群：排除
        _row("telegram:new:2", "2", bot=1, username="alice"),  # bot：排除
        _row("telegram:new:3", "3", name="nobody"),       # 无信号
    ]
    assert rc.match_candidates(new, idx) == []


class _FakeCpi:
    """resolve 缺省=独立 canonical；linked_pairs 内的键共享 canonical。"""

    def __init__(self, linked=None):
        self._linked = set(linked or [])

    def resolve(self, platform, uid):
        key = f"{platform}:{uid}"
        if key in self._linked:
            return "shared-canon"
        return key


def test_match_marks_already_linked():
    old = [_row("telegram:dead:111", "111")]
    idx = rc.build_match_index(old)
    new = [_row("telegram:new:111", "111")]
    cpi = _FakeCpi(linked={"telegram:dead:111", "telegram:new:111"})
    cands = rc.match_candidates(new, idx, cpi=cpi)
    assert cands[0]["already_linked"] is True
    # cpi 缺席 → 未知（None），不装作没链过
    assert rc.match_candidates(new, idx)[0]["already_linked"] is None


# ── 分页拉取 ─────────────────────────────────────────────────────────────────

class _FakeStore:
    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = 0

    def list_conversations(self, *, platform, account_id, limit, before_ts=None):
        self.calls += 1
        return self._pages.pop(0) if self._pages else []


def test_fetch_account_conversations_paginates_and_filters(monkeypatch):
    monkeypatch.setattr(rc, "_PAGE", 2)
    p1 = [_row("t:d:1", "1", last_ts=30), _row("t:d:g", "g", chat_type="group", last_ts=20)]
    p2 = [_row("t:d:2", "2", last_ts=10)]
    store = _FakeStore([p1, p2])
    rows = rc.fetch_account_conversations(store, "telegram", "dead")
    assert [r["conversation_id"] for r in rows] == ["t:d:1", "t:d:2"]
    assert store.calls == 2


# ── 台账 ─────────────────────────────────────────────────────────────────────

def test_ledger_persist_reload_and_cap(tmp_path, monkeypatch):
    path = tmp_path / "claims.json"
    led = rc.ClaimLedger(path)
    led.record({"old_conversation_id": "a", "new_conversation_id": "b", "ok": True})
    assert led.has("a", "b")
    assert not led.has("a", "c")
    # 重新加载读同一文件
    led2 = rc.ClaimLedger(path)
    assert led2.has("a", "b")
    assert led2.all()[0]["old_conversation_id"] == "a"
    # 裁旧上限
    monkeypatch.setattr(rc, "_LEDGER_CAP", 3)
    for i in range(5):
        led.record({"old_conversation_id": f"o{i}", "new_conversation_id": "n", "ok": True})
    assert len(led.all(limit=100)) == 3


def test_default_ledger_path_honors_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AITR_DATA_DIR", str(tmp_path))
    p = rc.default_ledger_path()
    assert str(p).startswith(str(tmp_path))


# ── 认领执行 ─────────────────────────────────────────────────────────────────

def _mk_ledger(tmp_path):
    return rc.ClaimLedger(tmp_path / "led.json")


def test_claim_rejects_bad_input(tmp_path):
    led = _mk_ledger(tmp_path)
    r = rc.claim(inbox_store=None, cpi=_FakeCpi(), episodic_store=None,
                 old_conversation_id="x", new_conversation_id="x", ledger=led)
    assert r["error"] == "bad_pair"
    r = rc.claim(inbox_store=None, cpi=_FakeCpi(), episodic_store=None,
                 old_conversation_id="notacid", new_conversation_id="t:a:1",
                 ledger=led)
    assert r["error"] == "bad_conversation_id"
    r = rc.claim(inbox_store=None, cpi=None, episodic_store=None,
                 old_conversation_id="t:d:1", new_conversation_id="t:n:1",
                 ledger=led)
    assert r["error"] == "no_cpi"


def test_claim_success_links_and_records(tmp_path):
    led = _mk_ledger(tmp_path)
    calls = []

    def fake_link(cpi, epi, pa, ua, pb, ub):
        calls.append((pa, ua, pb, ub))
        return {"already_linked": False, "memory_rows_merged": 7}

    r = rc.claim(
        inbox_store=None, cpi=_FakeCpi(), episodic_store=None,
        old_conversation_id="telegram:dead:111",
        new_conversation_id="telegram:new:111",
        matched_on="exact_peer", operator="tester",
        ledger=led, link_fn=fake_link)
    assert r["ok"] and not r["already_linked"]
    assert r["rows_merged"] == 7
    # 锚=旧会话：旧键在前
    assert calls == [("telegram", "dead:111", "telegram", "new:111")]
    assert led.has("telegram:dead:111", "telegram:new:111")
    row = led.all()[0]
    assert row["matched_on"] == "exact_peer" and row["operator"] == "tester"


def test_claim_idempotent_when_already_linked(tmp_path):
    led = _mk_ledger(tmp_path)
    cpi = _FakeCpi(linked={"telegram:dead:111", "telegram:new:111"})
    called = []

    def fake_link(*a):
        called.append(a)
        return {}

    r = rc.claim(
        inbox_store=None, cpi=cpi, episodic_store=None,
        old_conversation_id="telegram:dead:111",
        new_conversation_id="telegram:new:111",
        ledger=led, link_fn=fake_link)
    assert r["ok"] and r["already_linked"]
    assert called == []          # 已同 canonical：绝不重复搬记忆


def test_claim_link_failure_reported(tmp_path):
    led = _mk_ledger(tmp_path)

    def boom(*a):
        raise RuntimeError("db locked")

    r = rc.claim(
        inbox_store=None, cpi=_FakeCpi(), episodic_store=None,
        old_conversation_id="telegram:dead:1",
        new_conversation_id="telegram:new:1",
        ledger=led, link_fn=boom)
    assert not r["ok"]
    assert r["error"].startswith("link_failed:")
    assert not led.has("telegram:dead:1", "telegram:new:1")


# ── contacts 可选层 ──────────────────────────────────────────────────────────

class _CI:
    def __init__(self, ci_id, contact_id):
        self.channel_identity_id = ci_id
        self.contact_id = contact_id


class _FakeContactsStore:
    def __init__(self, mapping):
        self._m = mapping   # (platform, account, chat_key) -> _CI

    def get_ci_by_external(self, platform, account_id, chat_key):
        return self._m.get((platform, account_id, chat_key))


class _FakeGateway:
    def __init__(self):
        self.merges = []

    def manual_merge_identity(self, *, ci_id, target_contact_id, operator):
        self.merges.append((ci_id, target_contact_id, operator))
        return True


def _claim_with_contacts(tmp_path, contacts_store, gateway):
    return rc.claim(
        inbox_store=None, cpi=_FakeCpi(), episodic_store=None,
        old_conversation_id="telegram:dead:111",
        new_conversation_id="telegram:new:111",
        contacts_store=contacts_store, gateway=gateway, operator="op1",
        ledger=_mk_ledger(tmp_path),
        link_fn=lambda *a: {"already_linked": False, "memory_rows_merged": 0})


def test_claim_contacts_layer_merges(tmp_path):
    cs = _FakeContactsStore({
        ("telegram", "dead", "111"): _CI("ci-old", "contact-A"),
        ("telegram", "new", "111"): _CI("ci-new", "contact-B"),
    })
    gw = _FakeGateway()
    r = _claim_with_contacts(tmp_path, cs, gw)
    assert r["ok"] and r["contacts"] == "merged"
    assert gw.merges == [("ci-new", "contact-A", "op1")]


def test_claim_contacts_layer_skips_gracefully(tmp_path):
    # contacts 未启用
    r = _claim_with_contacts(tmp_path, None, None)
    assert r["ok"] and r["contacts"] == "skipped:contacts_disabled"
    # 老会话无档案
    cs = _FakeContactsStore({("telegram", "new", "111"): _CI("ci-new", "contact-B")})
    r = _claim_with_contacts(tmp_path, cs, _FakeGateway())
    assert r["ok"] and r["contacts"] == "skipped:no_old_contact"
    # 已同档
    cs = _FakeContactsStore({
        ("telegram", "dead", "111"): _CI("ci-old", "contact-A"),
        ("telegram", "new", "111"): _CI("ci-new", "contact-A"),
    })
    r = _claim_with_contacts(tmp_path, cs, _FakeGateway())
    assert r["ok"] and r["contacts"] == "already_merged"
    # gateway 抛异常：记忆合流的成功不被推翻
    class _BoomGw:
        def manual_merge_identity(self, **kw):
            raise RuntimeError("nope")
    cs = _FakeContactsStore({
        ("telegram", "dead", "111"): _CI("ci-old", "contact-A"),
        ("telegram", "new", "111"): _CI("ci-new", "contact-B"),
    })
    r = _claim_with_contacts(tmp_path, cs, _BoomGw())
    assert r["ok"] and r["contacts"] == "skipped:RuntimeError"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
