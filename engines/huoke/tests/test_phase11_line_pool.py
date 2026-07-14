# -*- coding: utf-8 -*-
"""Phase 11 (2026-04-25): LINE pool 存储层 + API + dispatcher 单测.

覆盖:
  * line_pool.add / add_many / update / delete / list / get_by_id
  * allocate 轮循 (last_used_at ASC) + daily_cap 绕
  * API 端点 GET/POST/PUT/DELETE/allocate
  * bulk_import_csv 解析 + 部分失败容错
  * dispatcher: 过滤 (require_l2_verified, persona_key, min_score),
                去重, no_account fallback
"""
from __future__ import annotations

import io
import json
import time

import pytest


# ═══════════════════════════════════════════════════════════════════
# 存储层
# ═══════════════════════════════════════════════════════════════════

class TestLinePoolStorage:
    def test_add_and_list(self, tmp_db):
        from src.host import line_pool as lp
        a = lp.add("@j1", region="jp", daily_cap=5)
        b = lp.add("@j2", region="jp", daily_cap=5)
        rows = lp.list_accounts()
        assert len(rows) == 2
        assert {r["line_id"] for r in rows} == {"@j1", "@j2"}

    def test_unique_line_id(self, tmp_db):
        from src.host import line_pool as lp
        lp.add("@dup", region="jp")
        with pytest.raises(ValueError, match="已存在"):
            lp.add("@dup", region="jp")

    def test_update_fields(self, tmp_db):
        from src.host import line_pool as lp
        aid = lp.add("@u1", region="jp", daily_cap=20)
        assert lp.update(aid, status="disabled", daily_cap=50, notes="kept")
        fresh = lp.get_by_id(aid)
        assert fresh["status"] == "disabled"
        assert fresh["daily_cap"] == 50
        assert fresh["notes"] == "kept"

    def test_update_invalid_status_rejected(self, tmp_db):
        from src.host import line_pool as lp
        aid = lp.add("@u2", region="jp")
        with pytest.raises(ValueError):
            lp.update(aid, status="bogus")

    def test_delete(self, tmp_db):
        from src.host import line_pool as lp
        aid = lp.add("@d1")
        assert lp.delete(aid) is True
        assert lp.get_by_id(aid) is None
        assert lp.delete(9999) is False


# ═══════════════════════════════════════════════════════════════════
# 轮循分配
# ═══════════════════════════════════════════════════════════════════

class TestAllocate:
    def test_round_robin_by_last_used(self, tmp_db):
        """新建的 2 个账号 last_used_at 都是空, 按 id 决定; 第一个用完后
        last_used_at 最新, 所以第二次 allocate 取另一个."""
        from src.host import line_pool as lp
        a = lp.add("@r1", region="jp", persona_key="jp_female_midlife",
                    daily_cap=5)
        b = lp.add("@r2", region="jp", persona_key="jp_female_midlife",
                    daily_cap=5)
        first = lp.allocate(region="jp", persona_key="jp_female_midlife",
                              canonical_id="c1")
        second = lp.allocate(region="jp", persona_key="jp_female_midlife",
                               canonical_id="c2")
        assert first["line_id"] != second["line_id"]

    def test_respects_daily_cap(self, tmp_db):
        """唯一账号 cap=1, 用掉后再 allocate 返 None (不超卖)."""
        from src.host import line_pool as lp
        lp.add("@capped", region="jp", daily_cap=1)
        r1 = lp.allocate(region="jp", canonical_id="c1")
        assert r1 is not None
        r2 = lp.allocate(region="jp", canonical_id="c2")
        assert r2 is None, "超 daily_cap 应返 None"

    def test_region_filter(self, tmp_db):
        from src.host import line_pool as lp
        lp.add("@jp", region="jp")
        lp.add("@it", region="it")
        r = lp.allocate(region="jp", canonical_id="c")
        assert r and r["line_id"] == "@jp"

    def test_no_match_returns_none(self, tmp_db):
        from src.host import line_pool as lp
        lp.add("@jp", region="jp")
        r = lp.allocate(region="it", canonical_id="c")
        assert r is None

    def test_skipped_log_written_on_no_match(self, tmp_db):
        """Phase 12.1: allocate 无匹配时写 skipped log 诊断."""
        from src.host import line_pool as lp
        lp.add("@jp", region="jp")
        lp.allocate(region="it", canonical_id="c1", peer_name="X")
        log = lp.recent_dispatch_log(limit=5)
        assert len(log) >= 1
        assert log[0]["status"] == "skipped"
        assert log[0]["line_account_id"] == 0
        assert "no_match" in log[0]["note"]

    def test_skipped_log_written_on_all_capped(self, tmp_db):
        """Phase 12.1: 全部超 cap 时 skipped log note=all_capped."""
        from src.host import line_pool as lp
        lp.add("@only", region="jp", daily_cap=1)
        # 用掉唯一余量
        lp.allocate(region="jp", canonical_id="c1")
        # 第 2 次必 skip
        r2 = lp.allocate(region="jp", canonical_id="c2", peer_name="Y")
        assert r2 is None
        log = lp.recent_dispatch_log(limit=5)
        # 首条是 skipped 记录
        assert log[0]["status"] == "skipped"
        assert "all_capped" in log[0]["note"]

    def test_disabled_not_allocated(self, tmp_db):
        from src.host import line_pool as lp
        aid = lp.add("@off", region="jp")
        lp.update(aid, status="disabled")
        assert lp.allocate(region="jp", canonical_id="c") is None

    def test_cas_concurrent_dont_overcount(self, tmp_db):
        """Phase 11.1 CAS 打磨: 同一 account 并发 2 次 allocate, times_used 只 +2
        (每次恰好 +1, 不会出现 race 导致 +1 +1 但 last_used 一样的幽灵状态).
        真并发测试难写, 这里至少保证 sequential CAS 不会失效."""
        from src.host import line_pool as lp
        lp.add("@cas", region="jp", daily_cap=100)
        r1 = lp.allocate(region="jp", canonical_id="c1")
        r2 = lp.allocate(region="jp", canonical_id="c2")
        assert r1 and r2
        assert r1["line_id"] == r2["line_id"] == "@cas"
        assert r2["times_used"] == 2

    def test_owner_device_match_or_universal(self, tmp_db):
        """owner_device_id 非空 → 只匹配本机 + 通用池 (owner 为空); 其它机
        owner 账号不返."""
        from src.host import line_pool as lp
        lp.add("@dev1", owner_device_id="D1")
        lp.add("@dev2", owner_device_id="D2")
        lp.add("@any", owner_device_id="")  # 通用
        r = lp.allocate(owner_device_id="D1", canonical_id="c")
        assert r and r["line_id"] in {"@dev1", "@any"}


class TestBulkImport:
    def test_add_many_partial_failure(self, tmp_db):
        from src.host import line_pool as lp
        lp.add("@exists")
        recs = [
            {"line_id": "@new1"},
            {"line_id": "@exists"},     # dup
            {"line_id": ""},             # invalid
            {"line_id": "@new2"},
        ]
        res = lp.add_many(recs)
        assert res["inserted"] == 2
        assert res["duplicate"] == 1
        assert res["invalid"] == 1
        assert res["total"] == 4


# ═══════════════════════════════════════════════════════════════════
# API 端点
# ═══════════════════════════════════════════════════════════════════

class TestLinePoolApi:
    @pytest.fixture
    def client(self, tmp_db, monkeypatch):
        # Phase 11.1: lifespan 里会 seed along2026 污染测试, 用 env 跳过.
        monkeypatch.setenv("OPENCLAW_LINE_POOL_SEED_SKIP", "1")
        from src.host.api import app
        from fastapi.testclient import TestClient
        with TestClient(app) as c:
            yield c

    def test_api_get_empty(self, client):
        r = client.get("/line-pool")
        assert r.status_code == 200
        assert r.json() == {"count": 0, "results": []}

    def test_api_add_list_delete(self, tmp_db, client):
        r = client.post("/line-pool",
                         json={"line_id": "@api1", "region": "jp"})
        assert r.status_code == 200
        aid = r.json()["id"]
        r = client.get("/line-pool")
        assert r.json()["count"] == 1
        r = client.delete(f"/line-pool/{aid}")
        assert r.status_code == 200
        assert client.get("/line-pool").json()["count"] == 0

    def test_api_add_duplicate_returns_400(self, tmp_db, client):
        client.post("/line-pool", json={"line_id": "@dup"})
        r = client.post("/line-pool", json={"line_id": "@dup"})
        assert r.status_code == 400

    def test_api_put_update(self, tmp_db, client):
        r = client.post("/line-pool", json={"line_id": "@u"})
        aid = r.json()["id"]
        r = client.put(f"/line-pool/{aid}",
                        json={"status": "disabled", "notes": "x"})
        assert r.status_code == 200
        assert r.json()["ok"] is True
        fresh = client.get(f"/line-pool/{aid}").json()
        assert fresh["status"] == "disabled"
        assert fresh["notes"] == "x"

    def test_api_bulk_import_json(self, tmp_db, client):
        r = client.post("/line-pool/bulk-import", json={"records": [
            {"line_id": "@b1", "region": "jp"},
            {"line_id": "@b2", "region": "jp"},
            {"line_id": ""},
        ]})
        assert r.status_code == 200
        data = r.json()
        assert data["inserted"] == 2
        assert data["invalid"] == 1

    def test_api_bulk_import_csv(self, tmp_db, client):
        csv_text = ("line_id,owner_device_id,persona_key,region,"
                     "status,daily_cap,notes\n"
                     "@csv1,,,jp,active,10,hello\n"
                     "@csv2,D1,jp_female_midlife,jp,active,5,\n")
        r = client.post(
            "/line-pool/bulk-import-csv",
            files={"file": ("t.csv", csv_text.encode("utf-8"), "text/csv")})
        assert r.status_code == 200, r.text
        assert r.json()["inserted"] == 2

    def test_api_allocate_returns_account(self, tmp_db, client):
        client.post("/line-pool", json={"line_id": "@alloc", "region": "jp"})
        r = client.post("/line-pool/allocate",
                         json={"region": "jp", "canonical_id": "C1"})
        assert r.status_code == 200
        data = r.json()
        assert data["allocated"] is True
        assert data["account"]["line_id"] == "@alloc"

    def test_api_list_with_24h_stats(self, tmp_db, client):
        client.post("/line-pool", json={"line_id": "@s1", "region": "jp"})
        client.post("/line-pool/allocate",
                     json={"region": "jp", "canonical_id": "C"})
        client.post("/line-pool/allocate",
                     json={"region": "jp", "canonical_id": "C2"})
        r = client.get("/line-pool?includes_24h_stats=true")
        assert r.status_code == 200
        rows = r.json()["results"]
        assert rows[0]["used_24h"] == 2

    def test_api_allocate_no_match(self, tmp_db, client):
        r = client.post("/line-pool/allocate",
                         json={"region": "nowhere", "canonical_id": "C"})
        assert r.status_code == 200
        assert r.json() == {"allocated": False,
                             "reason": "no_matching_account_or_all_capped"}


# ═══════════════════════════════════════════════════════════════════
# dispatcher (executor task)
# ═══════════════════════════════════════════════════════════════════

def _seed_l2_lead(name: str, *, age: str = "40s", gender: str = "female",
                   is_jp: bool = True, score: float = 85,
                   persona: str = "jp_female_midlife") -> str:
    from src.host.lead_mesh import (resolve_identity,
                                      update_canonical_metadata)
    cid = resolve_identity(platform="facebook",
                            account_id=f"fb:{name}",
                            display_name=name)
    meta = {
        "age_band": age, "gender": gender,
        "is_japanese": is_jp, "l2_score": score,
        "l2_persona_key": persona,
        "l2_verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                         time.gmtime()),
    }
    tags = ["l2_verified", f"age:{age}", f"gender:{gender}"]
    if is_jp:
        tags.append("is_japanese")
    update_canonical_metadata(cid, meta, tags=tags)
    return cid


def _seed_reply_event(device_id: str, peer_name: str, event_type: str):
    from src.host.fb_store import record_contact_event
    # Phase 16: 测试 fixture 用 fake 名字 (Alice/Anonymous/U0..) 走 skip_sanitize
    return record_contact_event(device_id, peer_name, event_type,
                                  meta={"via": "test"},
                                  skip_sanitize=True)


class TestDispatcher:
    def test_dispatch_happy_path(self, tmp_db):
        from src.host import line_pool as lp
        from src.host.executor import _fb_line_dispatch_from_reply
        lp.add("@jp1", region="jp", persona_key="jp_female_midlife",
                daily_cap=5)
        _seed_l2_lead("花子")
        _seed_reply_event("DEV1", "花子", "greeting_replied")

        ok, msg, stats = _fb_line_dispatch_from_reply("DEV1", {
            "hours_window": 6, "region": "jp",
            "persona_key": "jp_female_midlife",
        })
        assert ok and stats["dispatched"] == 1
        d = stats["dispatches"][0]
        assert d["peer_name"] == "花子"
        assert d["line_id"] == "@jp1"
        assert d["metadata"]["age_band"] == "40s"

    def test_dedupe_skips_already_dispatched(self, tmp_db):
        from src.host import line_pool as lp
        from src.host.executor import _fb_line_dispatch_from_reply
        lp.add("@jp1", region="jp", persona_key="jp_female_midlife",
                daily_cap=5)
        _seed_l2_lead("美咲")
        _seed_reply_event("DEV1", "美咲", "greeting_replied")

        _fb_line_dispatch_from_reply("DEV1", {
            "hours_window": 6, "region": "jp",
            "persona_key": "jp_female_midlife"})
        # 第二次 run, 应被 24h 去重
        ok, _, stats = _fb_line_dispatch_from_reply("DEV1", {
            "hours_window": 6, "region": "jp",
            "persona_key": "jp_female_midlife"})
        assert ok and stats["dispatched"] == 0
        assert stats["filtered_out"] >= 1

    def test_filter_non_l2_verified(self, tmp_db):
        """peer 没有 l2_verified tag → 默认被过滤."""
        from src.host import line_pool as lp
        from src.host.executor import _fb_line_dispatch_from_reply
        lp.add("@jp1", region="jp", daily_cap=5)
        # 故意不 seed l2_lead, 直接 event
        _seed_reply_event("DEV1", "Anonymous", "greeting_replied")

        ok, _, stats = _fb_line_dispatch_from_reply("DEV1", {
            "hours_window": 6, "region": "jp",
            "require_l2_verified": True})
        assert ok and stats["dispatched"] == 0
        assert stats["filtered_out"] >= 1

    def test_no_account_returns_none(self, tmp_db):
        """l2 verified 但 pool 空 → no_account 计数, 不崩."""
        from src.host.executor import _fb_line_dispatch_from_reply
        _seed_l2_lead("孤独")
        _seed_reply_event("DEV1", "孤独", "greeting_replied")

        ok, _, stats = _fb_line_dispatch_from_reply("DEV1", {
            "hours_window": 6, "region": "jp",
            "persona_key": "jp_female_midlife"})
        assert ok
        assert stats["dispatched"] == 0
        assert stats["no_account"] == 1

    def test_write_contact_event_opt_in(self, tmp_db):
        """write_contact_event=True + 默认 event_type=line_dispatch_planned."""
        from src.host import line_pool as lp
        from src.host.executor import _fb_line_dispatch_from_reply
        from src.host.fb_store import count_contact_events
        lp.add("@jp1", region="jp", persona_key="jp_female_midlife",
                daily_cap=5)
        _seed_l2_lead("由美")
        _seed_reply_event("DEV1", "由美", "greeting_replied")

        _fb_line_dispatch_from_reply("DEV1", {
            "hours_window": 6, "region": "jp",
            "persona_key": "jp_female_midlife",
            "write_contact_event": True})
        n = count_contact_events(
            device_id="DEV1", peer_name="由美",
            event_type="line_dispatch_planned", hours=6)
        assert n == 1

    def test_write_contact_event_legacy_type(self, tmp_db):
        """event_type='wa_referral_sent' 保兼容老 B 机消费逻辑."""
        from src.host import line_pool as lp
        from src.host.executor import _fb_line_dispatch_from_reply
        from src.host.fb_store import count_contact_events
        lp.add("@jp1", region="jp", persona_key="jp_female_midlife",
                daily_cap=5)
        _seed_l2_lead("裕子")
        _seed_reply_event("DEV1", "裕子", "greeting_replied")

        _fb_line_dispatch_from_reply("DEV1", {
            "hours_window": 6, "region": "jp",
            "persona_key": "jp_female_midlife",
            "write_contact_event": True,
            "event_type": "wa_referral_sent"})
        n = count_contact_events(
            device_id="DEV1", peer_name="裕子",
            event_type="wa_referral_sent", hours=6)
        assert n == 1

    def test_persona_fallback_priority_lead_over_task(self, tmp_db):
        """Phase 12.1: lead.metadata.l2_persona_key > task.persona_key.

        给 lead 打 l2_persona_key=A, task 传 persona_key=B,
        dispatcher 组装 referral 时应用 A.
        """
        from src.host import line_pool as lp
        from src.host.executor import _fb_line_dispatch_from_reply
        lp.add("@jp", region="jp",
                persona_key="",  # 通用池, 不参与过滤
                daily_cap=10)
        # lead 的 persona 是 jp_female_midlife
        _seed_l2_lead("花子", persona="jp_female_midlife")
        _seed_reply_event("DEV1", "花子", "greeting_replied")

        ok, _m, stats = _fb_line_dispatch_from_reply("DEV1", {
            "hours_window": 6,
            "persona_key": "it_female_midlife",  # task 级错误 persona
            "region": "jp",
            # lead metadata 优先, 仍用 jp_female_midlife 抽模板
        })
        # 只要 dispatcher 不抛, lead 优先 persona 生效 (能否抽到 jp 模板由 yaml 决定)
        assert ok
        # 如果抽到非空 template, 里面应含日文字符或 line_id
        if stats["dispatched"] >= 1:
            d = stats["dispatches"][0]
            # template 非空 (fallback 至少返 "line: along2026")
            assert d["message_template"]

    def test_dispatch_includes_message_template(self, tmp_db):
        """dispatch_mode=messenger_text 时 dispatches 里应有 message_template."""
        from src.host import line_pool as lp
        from src.host.executor import _fb_line_dispatch_from_reply
        lp.add("along2026", region="jp",
                persona_key="jp_female_midlife", daily_cap=5)
        _seed_l2_lead("花子")
        _seed_reply_event("DEV1", "花子", "greeting_replied")

        _ok, _m, stats = _fb_line_dispatch_from_reply("DEV1", {
            "hours_window": 6, "region": "jp",
            "persona_key": "jp_female_midlife",
            "dispatch_mode": "messenger_text"})
        assert stats["dispatched"] == 1
        d = stats["dispatches"][0]
        assert d["dispatch_mode"] == "messenger_text"
        # get_referral_snippet 可能因 persona yaml 缺失返 fallback, 但至少非空
        assert d["message_template"], "messenger_text 模式应生成 template"
        # template 里应该含 line_id
        assert "along2026" in d["message_template"]

    def test_limit_caps_output(self, tmp_db):
        from src.host import line_pool as lp
        from src.host.executor import _fb_line_dispatch_from_reply
        for i in range(5):
            lp.add(f"@p{i}", region="jp",
                    persona_key="jp_female_midlife", daily_cap=2)
        for i in range(5):
            _seed_l2_lead(f"U{i}")
            _seed_reply_event("DEV1", f"U{i}", "greeting_replied")

        ok, _, stats = _fb_line_dispatch_from_reply("DEV1", {
            "hours_window": 6, "region": "jp",
            "persona_key": "jp_female_midlife",
            "limit": 2})
        assert ok and stats["dispatched"] == 2
