# -*- coding: utf-8 -*-
"""打招呼(send_greeting) + 加好友一体化(add_friend_and_greet) 相关单元测试。

2026-04-23 新增，覆盖:
  * fb_store 的 count_outgoing_messages_since(sent_at 迁移)
  * fb_playbook 的 send_greeting 段 phase 解析
  * fb_content_assets.get_greeting_message_with_id 模板 ID 行为
  * fb_add_friend_gate.check_send_greeting_gate 闸行为
  * get_funnel_metrics 的 greeting 维度(stage_greetings_sent / rate_greet_after_add /
    greeting_template_distribution)
  * record_inbox_message 写 sent_at / template_id 的语义

*不*覆盖:需要真机 adb/u2 的 automation 层行为(send_greeting_after_add_friend,
add_friend_and_greet 本体) —— 这些归属 smoke_facebook_realdevice.py。
"""
from __future__ import annotations

import datetime as _dt
import pytest


# ─── fb_playbook.resolve_send_greeting_params ─────────────────────────────────
class TestPlaybookSendGreeting:
    def test_defaults_nonempty(self):
        from src.host.fb_playbook import resolve_send_greeting_params
        c = resolve_send_greeting_params()
        assert c["max_greetings_per_run"] > 0
        assert isinstance(c["inter_greeting_sec"], tuple) and len(c["inter_greeting_sec"]) == 2
        assert c["require_persona_template"] is True
        assert "allow_messenger_fallback" in c

    def test_cold_start_blocks(self):
        from src.host.fb_playbook import resolve_send_greeting_params
        c = resolve_send_greeting_params(phase="cold_start")
        assert c["max_greetings_per_run"] == 0
        assert c["daily_cap_per_account"] == 0
        assert c["enabled_probability"] == 0.0

    def test_growth_moderate(self):
        from src.host.fb_playbook import resolve_send_greeting_params
        c = resolve_send_greeting_params(phase="growth")
        assert 0 < c["max_greetings_per_run"] <= 3
        assert c["daily_cap_per_account"] <= 8

    def test_cooldown_zeros(self):
        from src.host.fb_playbook import resolve_send_greeting_params
        c = resolve_send_greeting_params(phase="cooldown")
        assert c["max_greetings_per_run"] == 0
        assert c["enabled_probability"] == 0.0

    def test_tuple_coercion_for_list_fields(self):
        from src.host.fb_playbook import resolve_send_greeting_params
        c = resolve_send_greeting_params(phase="growth")
        # YAML 里是 list,playbook resolver 应转成 tuple
        for k in ("inter_greeting_sec", "post_add_friend_wait_sec",
                  "think_before_type_sec"):
            assert isinstance(c[k], tuple)


# ─── fb_content_assets.get_greeting_message_with_id ───────────────────────────
class TestContentAssetsGreeting:
    def test_jp_returns_nonempty_with_id(self):
        from src.app_automation.fb_content_assets import get_greeting_message_with_id
        text, tid = get_greeting_message_with_id(persona_key="jp_female_midlife",
                                                 name="hanako")
        assert text  # 不空
        # tid 格式 "<src>:<cc_or_lang>:<idx>"
        assert tid.count(":") == 2
        src, key, idx = tid.split(":")
        assert src in ("yaml", "fallback")
        assert idx.isdigit()

    def test_backward_compat_get_greeting_message(self):
        from src.app_automation.fb_content_assets import get_greeting_message
        text = get_greeting_message(persona_key="jp_female_midlife", name="hanako")
        assert isinstance(text, str)
        assert text

    def test_fallback_when_no_country_bundle(self):
        """显式 language='en' 无 YAML 覆盖时走 fallback.en 兜底。"""
        from src.app_automation.fb_content_assets import get_greeting_message_with_id
        text, tid = get_greeting_message_with_id(language="en", name="Alice")
        assert text
        # 当前 en 没 countries 包 → fallback.default(英文)
        assert tid.startswith(("fallback:", "yaml:"))

    def test_different_calls_may_pick_different_templates(self):
        """多次调用样本里至少能见到 >1 个不同 tid(非绝对保证,但样本 30 应够)。"""
        from src.app_automation.fb_content_assets import get_greeting_message_with_id
        tids = set()
        for _ in range(30):
            _, tid = get_greeting_message_with_id(persona_key="jp_female_midlife")
            tids.add(tid)
        # 至少 2 个不同的模板索引
        assert len(tids) >= 2


# ─── fb_store 端到端 ───────────────────────────────────────────────────────────
class TestFbStoreOutgoingAndFunnel:
    def test_record_outgoing_sets_sent_at(self, tmp_db):
        from src.host.fb_store import (record_inbox_message,
                                        count_outgoing_messages_since)
        rid = record_inbox_message("d1", "Alice",
                                   peer_type="friend_request",
                                   message_text="hi",
                                   direction="outgoing",
                                   ai_decision="greeting",
                                   template_id="yaml:jp:2")
        assert rid > 0
        # count_outgoing_messages_since 能匹配到
        n = count_outgoing_messages_since("d1", hours=24, ai_decision="greeting")
        assert n == 1

    def test_record_incoming_no_sent_at(self, tmp_db):
        from src.host.fb_store import (record_inbox_message,
                                        count_outgoing_messages_since)
        record_inbox_message("d1", "Bob", direction="incoming",
                             message_text="hey")
        # incoming 不算 outgoing
        n = count_outgoing_messages_since("d1", hours=24)
        assert n == 0

    def test_daily_cap_filter_by_ai_decision(self, tmp_db):
        from src.host.fb_store import (record_inbox_message,
                                        count_outgoing_messages_since)
        record_inbox_message("d1", "X", direction="outgoing",
                             ai_decision="greeting")
        record_inbox_message("d1", "Y", direction="outgoing",
                             ai_decision="wa_referral")
        record_inbox_message("d1", "Z", direction="outgoing",
                             ai_decision="reply")
        assert count_outgoing_messages_since("d1", 24, "greeting") == 1
        assert count_outgoing_messages_since("d1", 24, "wa_referral") == 1
        assert count_outgoing_messages_since("d1", 24) == 3

    def test_funnel_greeting_fields_present(self, tmp_db):
        from src.host.fb_store import (record_friend_request,
                                        record_inbox_message,
                                        get_funnel_metrics)
        # 构造 2 个好友请求 + 1 个打招呼(yaml:jp:0) + 1 个 fallback
        record_friend_request("d1", "Alice", note="hi")
        record_friend_request("d1", "Bob", note="hi")
        record_inbox_message("d1", "Alice", direction="outgoing",
                             ai_decision="greeting",
                             template_id="yaml:jp:0")
        record_inbox_message("d1", "Bob", direction="outgoing",
                             ai_decision="greeting",
                             template_id="yaml:jp:0|fallback")
        m = get_funnel_metrics(device_id="d1")
        assert m["stage_friend_request_sent"] == 2
        assert m["stage_greetings_sent"] == 2
        assert m["stage_greetings_fallback"] == 1
        # rate_greet_after_add = 2/2 = 1.0
        assert abs(m["rate_greet_after_add"] - 1.0) < 1e-6
        # template 分布: yaml:jp:0 被两次计数(fallback 在 |fallback 左边截断)
        dist = dict(m["greeting_template_distribution"])
        assert dist.get("yaml:jp:0") == 2

    def test_funnel_empty_zero_divisions(self, tmp_db):
        from src.host.fb_store import get_funnel_metrics
        m = get_funnel_metrics(device_id="empty_dev")
        assert m["stage_friend_request_sent"] == 0
        assert m["stage_greetings_sent"] == 0
        assert m["rate_greet_after_add"] == 0.0
        assert m["greeting_template_distribution"] == []


# ─── Gate: check_send_greeting_gate ───────────────────────────────────────────
class TestSendGreetingGate:
    def test_cold_start_rejected(self, tmp_db):
        from src.host.fb_add_friend_gate import check_send_greeting_gate
        err, meta = check_send_greeting_gate("d1", {"phase": "cold_start"})
        assert err is not None
        assert "cold_start" in err
        assert meta["max_greetings_per_run"] == 0

    def test_mature_passes_when_below_cap(self, tmp_db):
        from src.host.fb_add_friend_gate import check_send_greeting_gate
        err, meta = check_send_greeting_gate("d1", {"phase": "mature"})
        assert err is None
        assert meta["daily_cap_per_account"] > 0

    def test_cap_hit_rejected(self, tmp_db):
        """填满 daily_cap 后 gate 应拒绝。"""
        from src.host.fb_store import record_inbox_message
        from src.host.fb_add_friend_gate import check_send_greeting_gate
        # mature 的 cap=8 → 填 10 条 outgoing+greeting,gate 必拒
        for i in range(10):
            record_inbox_message("d2", f"target{i}",
                                 direction="outgoing",
                                 ai_decision="greeting")
        err, meta = check_send_greeting_gate("d2", {"phase": "mature"})
        assert err is not None
        assert "上限" in err
        assert meta["greetings_24h"] >= meta["daily_cap_per_account"]

    def test_skip_flag_bypasses(self, tmp_db):
        from src.host.fb_add_friend_gate import check_send_greeting_gate
        err, meta = check_send_greeting_gate(
            "d1", {"phase": "cold_start", "skip_send_greeting_gate": True})
        assert err is None
        assert meta.get("skipped") is True

    def test_no_device_id_passes_through(self):
        from src.host.fb_add_friend_gate import check_send_greeting_gate
        err, meta = check_send_greeting_gate("", {"phase": "mature"})
        assert err is None
        assert meta.get("reason") == "no_device_id"


# ─── Executor 分发(纯参数校验,不跑真机) ──────────────────────────────────────
class TestExecutorDispatchParamsValidation:
    """_execute_facebook 对新 task_type 的参数校验分支覆盖。

    不 mock 真机,只走到"target 参数缺失"的早退分支。
    """

    def test_add_friend_and_greet_missing_target(self, monkeypatch, tmp_db):
        from src.host.executor import _execute_facebook

        class _StubMgr:
            pass

        # _fresh_facebook 里会 new FacebookAutomation → 避免启动,monkeypatch 掉
        def _fake_fresh(manager, resolved):
            class _F:
                def __getattr__(self, n):
                    raise AssertionError(f"shouldn't call {n} — params 验证失败应提前返回")
            return _F()

        import src.host.executor as ex
        monkeypatch.setattr(ex, "_fresh_facebook", _fake_fresh)

        ok, err, meta = _execute_facebook(
            _StubMgr(), "test_dev", "facebook_add_friend_and_greet",
            {"persona_key": "jp_female_midlife"})
        assert ok is False
        assert "target" in err

    def test_send_greeting_missing_target(self, monkeypatch, tmp_db):
        from src.host.executor import _execute_facebook

        class _StubMgr:
            pass

        def _fake_fresh(manager, resolved):
            class _F:
                def __getattr__(self, n):
                    raise AssertionError("不应调 automation")
            return _F()

        import src.host.executor as ex
        monkeypatch.setattr(ex, "_fresh_facebook", _fake_fresh)

        ok, err, meta = _execute_facebook(
            _StubMgr(), "test_dev", "facebook_send_greeting", {})
        assert ok is False
        assert "target" in err


# ─── Schemas / Task registry 注册 ─────────────────────────────────────────────
class TestSchemasRegistry:
    def test_new_task_types_enrolled(self):
        from src.host.schemas import TaskType
        vals = {t.value for t in TaskType}
        assert "facebook_add_friend_and_greet" in vals
        assert "facebook_send_greeting" in vals
        assert "facebook_group_member_greet" in vals

    def test_task_labels_zh(self):
        from src.host.task_labels_zh import task_label_zh
        assert "加好友" in task_label_zh("facebook_add_friend_and_greet")
        assert "打招呼" in task_label_zh("facebook_send_greeting")
        assert "群成员" in task_label_zh("facebook_group_member_greet")


# ─── Presets registry ─────────────────────────────────────────────────────────
class TestPresets:
    def test_name_hunter_preset_exists(self):
        from src.host.routers.facebook import FB_FLOW_PRESETS
        keys = {p["key"] for p in FB_FLOW_PRESETS}
        assert "name_hunter" in keys

    def test_name_hunter_needs_input(self):
        from src.host.routers.facebook import FB_FLOW_PRESETS
        p = next(p for p in FB_FLOW_PRESETS if p["key"] == "name_hunter")
        assert "add_friend_targets" in (p.get("needs_input") or [])
        # send_greeting_inline 默认开启
        step_params = p["steps"][0]["params"]
        assert step_params.get("send_greeting_inline") is True
        assert step_params.get("require_high_match") is True
        assert step_params.get("min_seed_score") == 80
        assert step_params.get("do_l2_gate") is True
        assert step_params.get("strict_persona_gate") is True

    def test_friend_growth_still_runs_add_friends(self):
        from src.host.routers.facebook import FB_FLOW_PRESETS
        p = next(p for p in FB_FLOW_PRESETS if p["key"] == "friend_growth")
        assert len(p["steps"]) == 1
        assert p["steps"][0]["type"] == "facebook_group_member_greet"
        step_params = p["steps"][0]["params"]
        assert "extract_members" in step_params["steps"]
        assert "add_friends" in step_params["steps"]
        assert step_params.get("send_greeting_inline") is True


class TestNameHunterPreview:
    def test_suggest_jp_names_returns_scored_rows(self, tmp_db):
        from fastapi.testclient import TestClient
        from src.host.api import app

        with TestClient(app) as c:
            r = c.post("/facebook/name-hunter/suggest", json={
                "persona_key": "jp_female_midlife",
                "age_pack": "46_55",
                "count": 8,
            })
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["ok"] is True
        assert data["count"] == 8
        assert all(row["score"] >= 80 for row in data["names"])
        assert all(row["name"] for row in data["names"])

    def test_preview_dedupes_and_scores_launch_targets(self, tmp_db):
        from fastapi.testclient import TestClient
        from src.host.api import app

        with TestClient(app) as c:
            r = c.post("/facebook/name-hunter/preview", json={
                "persona_key": "jp_female_midlife",
                "names": "山田花子\n山田 花子, Alice",
            })
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["unique_count"] == 2
        names = {row["name"] for row in data["rows"]}
        assert names == {"山田花子", "Alice"}
        assert data["high_confidence_count"] == 1
        assert data["weak_count"] == 1
        assert [row["name"] for row in data["launch_targets"]] == ["山田花子"]
        assert data["policy"]["auto_touch_allowed"] is False
        assert data["policy"]["minimum_score_for_launch"] == 80
        assert data["policy"]["strict_profile_l2_required"] is True

    def test_executor_high_match_filter_keeps_only_seed_score_80_plus(self):
        from src.host.executor import _fb_filter_high_match_targets

        rows, meta = _fb_filter_high_match_targets(
            [
                {"name": "山田花子", "seed_score": 95},
                {"name": "Alice", "seed_score": 20},
            ],
            {"require_high_match": True, "min_seed_score": 80},
        )
        assert [r["name"] for r in rows] == ["山田花子"]
        assert meta["skipped"] == 1

    def test_preview_persists_candidate_pool(self, tmp_db):
        from fastapi.testclient import TestClient
        from src.host.api import app

        with TestClient(app) as c:
            r = c.post("/facebook/name-hunter/preview", json={
                "persona_key": "jp_female_midlife",
                "names": "山田花子\nAlice",
            })
            assert r.status_code == 200, r.text
            q = c.get("/facebook/name-hunter/candidates", params={
                "persona_key": "jp_female_midlife",
                "limit": 10,
            })
        assert q.status_code == 200, q.text
        items = q.json()["items"]
        by_name = {x["display_name"]: x for x in items}
        assert by_name["山田花子"]["status"] == "seeded"
        assert by_name["山田花子"]["insights"]["seed_score"] >= 80
        assert by_name["Alice"]["status"] == "weak_seed"

    def test_mark_profile_result_promotes_candidate_to_qualified(self, tmp_db):
        from src.host.fb_targets_store import (
            mark_name_hunter_profile_result,
            list_name_hunter_candidates,
            upsert_name_hunter_candidate,
            name_hunter_touch_targets,
        )

        upsert_name_hunter_candidate(
            name="山田花子",
            persona_key="jp_female_midlife",
            seed_score=95,
            seed_stage="high_confidence_seed",
            status="seeded",
        )
        mark_name_hunter_profile_result(
            name="山田花子",
            persona_key="jp_female_midlife",
            matched=True,
            score=91,
            insights={"age_band": "40s", "gender": "female", "is_japanese": True},
            device_id="D1",
        )
        rows = list_name_hunter_candidates(persona_key="jp_female_midlife")
        row = next(x for x in rows if x["display_name"] == "山田花子")
        assert row["status"] == "qualified"
        assert row["qualified"] == 1
        assert row["insights"]["seed_score"] == 95
        assert row["insights"]["profile_score"] == 91
        assert row["insights"]["qualification_evidence"]["age_37plus_confirmed"] is True
        q = name_hunter_touch_targets(persona_key="jp_female_midlife")
        assert [x["display_name"] for x in q] == ["山田花子"]

    def test_30s_profile_match_requires_manual_37plus_review(self, tmp_db):
        from src.host.fb_targets_store import (
            mark_name_hunter_profile_result,
            list_name_hunter_candidates,
            upsert_name_hunter_candidate,
            name_hunter_touch_targets,
        )

        upsert_name_hunter_candidate(
            name="佐藤美咲",
            persona_key="jp_female_midlife",
            seed_score=95,
            status="seeded",
        )
        mark_name_hunter_profile_result(
            name="佐藤美咲",
            persona_key="jp_female_midlife",
            matched=True,
            score=90,
            insights={"age_band": "30s", "gender": "female", "is_japanese": True},
            device_id="D1",
        )
        row = next(x for x in list_name_hunter_candidates(persona_key="jp_female_midlife")
                   if x["display_name"] == "佐藤美咲")
        assert row["status"] == "review_required"
        assert row["qualified"] == 0
        assert "age_30s_needs_manual_37plus_review" in row["insights"]["qualification_evidence"]["gaps"]
        assert name_hunter_touch_targets(persona_key="jp_female_midlife") == []

    def test_prescreen_and_touch_routes_create_tasks(self, monkeypatch, tmp_db):
        captured = []

        def _fake_create_task(task_type, device_id, params, **kwargs):
            captured.append((task_type, device_id, params))
            return "tid_" + str(len(captured))

        monkeypatch.setattr("src.host.task_store.create_task", _fake_create_task)
        from src.host.fb_targets_store import mark_name_hunter_profile_result, upsert_name_hunter_candidate

        upsert_name_hunter_candidate(
            name="山田花子",
            persona_key="jp_female_midlife",
            seed_score=95,
            seed_stage="high_confidence_seed",
            status="seeded",
        )
        mark_name_hunter_profile_result(
            name="山田花子",
            persona_key="jp_female_midlife",
            matched=True,
            score=91,
            insights={"age_band": "40s", "gender": "female", "is_japanese": True},
        )
        from fastapi.testclient import TestClient
        from src.host.api import app

        with TestClient(app) as c:
            r1 = c.post("/facebook/name-hunter/prescreen", json={
                "device_id": "D1",
                "persona_key": "jp_female_midlife",
            })
            r2 = c.post("/facebook/name-hunter/touch-qualified", json={
                "device_id": "D1",
                "persona_key": "jp_female_midlife",
            })
        assert r1.status_code == 200, r1.text
        assert r2.status_code == 200, r2.text
        assert captured[0][0] == "facebook_name_hunter_prescreen"
        assert captured[1][0] == "facebook_name_hunter_touch_qualified"
        assert captured[0][2]["status"] == "seeded"
        assert captured[1][2]["_preset_key"] == "name_hunter"

    def test_touch_route_blocks_when_no_qualified_candidates(self, monkeypatch, tmp_db):
        def _fake_create_task(*args, **kwargs):
            raise AssertionError("touch task should not be created without qualified candidates")

        monkeypatch.setattr("src.host.task_store.create_task", _fake_create_task)
        from fastapi.testclient import TestClient
        from src.host.api import app

        with TestClient(app) as c:
            r = c.post("/facebook/name-hunter/touch-qualified", json={
                "device_id": "D1",
                "persona_key": "jp_female_midlife",
                "min_qualified_ready": 1,
            })
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["qualified_ready"] == 0

    def test_candidate_action_and_stats_preserve_source_pack(self, tmp_db):
        from fastapi.testclient import TestClient
        from src.host.api import app
        from src.host.fb_targets_store import mark_name_hunter_profile_result

        with TestClient(app) as c:
            r = c.post("/facebook/name-hunter/preview", json={
                "persona_key": "jp_female_midlife",
                "source_ref": "jp_46_55_pack",
                "names": "山田花子\n佐藤美咲",
            })
            assert r.status_code == 200, r.text
            first_id = r.json()["rows"][0]["candidate_id"]

            a = c.post(f"/facebook/name-hunter/candidates/{first_id}/action", json={
                "action": "qualify",
            })
            assert a.status_code == 200, a.text

            mark_name_hunter_profile_result(
                name="佐藤美咲",
                persona_key="jp_female_midlife",
                matched=True,
                score=88,
                insights={"age_band": "50s", "gender": "female", "is_japanese": True},
            )
            s = c.get("/facebook/name-hunter/stats", params={
                "persona_key": "jp_female_midlife",
            })
        assert s.status_code == 200, s.text
        pack = next(x for x in s.json()["sources"] if x["source_ref"] == "jp_46_55_pack")
        assert pack["total"] == 2
        assert pack["qualified"] == 2

    def test_degraded_source_pack_is_auto_downranked(self, tmp_db):
        from fastapi.testclient import TestClient
        from src.host.api import app
        from src.host.fb_targets_store import upsert_name_hunter_candidate

        for i in range(5):
            upsert_name_hunter_candidate(
                name=f"低質{i}",
                persona_key="jp_female_midlife",
                seed_score=95,
                source_ref="poor_pack",
                status="rejected",
            )

        with TestClient(app) as c:
            r = c.post("/facebook/name-hunter/preview", json={
                "persona_key": "jp_female_midlife",
                "source_ref": "poor_pack",
                "names": "山田花子",
                "persist": False,
            })
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["source_quality"]["source_health"] == "degraded"
        assert data["rows"][0]["score"] == 85
        assert "自动降权" in " / ".join(data["rows"][0]["reasons"])


# ─── Launch 端点注入 add_friend_targets (集成测试) ───────────────────────────
class TestLaunchTargetsInjection:
    """
    验证 POST /facebook/device/.../launch 把 body.add_friend_targets 正确注入
    到 campaign step.params.add_friend_targets。
    关键用例: name_hunter 预设预填 add_friend_targets=[] 空列表时,
    bug fix 后应被 body 的实际名字列表覆盖,而不是被 setdefault 吞掉。
    """

    def test_body_targets_override_empty_preset_list(self, monkeypatch, tmp_db):
        import src.host.routers.facebook as fb_router

        captured = []

        def _fake_post_create(base, payload):
            captured.append(payload)
            return {"task_id": "tid_" + str(len(captured))}

        monkeypatch.setattr(fb_router, "_post_create_task", _fake_post_create)
        # 走本地路径 (is_local=True),绕过 worker probe
        monkeypatch.setattr(fb_router, "_is_local_device", lambda d: True)

        from fastapi.testclient import TestClient
        from src.host.api import app
        with TestClient(app) as c:
            r = c.post("/facebook/device/test_dev/launch", json={
                "preset_key": "name_hunter",
                "persona_key": "jp_female_midlife",
                "add_friend_targets": ["山田花子", "佐藤美咲"],
                "greeting": "はじめまして🌸",
            })
        assert r.status_code == 200, r.text
        # name_hunter 只有一个 step: facebook_campaign_run
        assert len(captured) == 1
        payload = captured[0]
        assert payload["type"] == "facebook_campaign_run"
        params = payload["params"]
        tgt = params.get("add_friend_targets") or []
        assert len(tgt) == 2, f"期望 2 个名字被注入, 实际 {tgt}"
        names = {(t.get("name") if isinstance(t, dict) else str(t)) for t in tgt}
        assert names == {"山田花子", "佐藤美咲"}
        # greeting 也应注入(preset 预设值为空串 → 不会吞)
        assert params.get("greeting") == "はじめまして🌸"

    def test_body_targets_string_split(self, monkeypatch, tmp_db):
        """body.add_friend_targets 传 string 时后端应自动切分。"""
        import src.host.routers.facebook as fb_router

        captured = []
        monkeypatch.setattr(fb_router, "_post_create_task",
                            lambda b, p: captured.append(p) or {"task_id": "x"})
        monkeypatch.setattr(fb_router, "_is_local_device", lambda d: True)

        from fastapi.testclient import TestClient
        from src.host.api import app
        with TestClient(app) as c:
            r = c.post("/facebook/device/test_dev/launch", json={
                "preset_key": "name_hunter",
                "persona_key": "jp_female_midlife",
                "add_friend_targets": "A\nB, C;D",
            })
        assert r.status_code == 200
        names = {t["name"] for t in captured[0]["params"]["add_friend_targets"]}
        assert names == {"A", "B", "C", "D"}

    def test_campaign_send_greeting_triggers_greeting_gate(self, monkeypatch, tmp_db):
        """critical 漏洞修复: campaign_run + steps=['send_greeting'] 也必须过 greeting gate。

        预先填满 daily_cap,然后尝试创建 campaign 任务,应被 tasks.py 前置闸拒绝。
        """
        from src.host.fb_store import record_inbox_message
        # 把 mature phase daily_cap(=8) 填满
        for i in range(10):
            record_inbox_message("d_camp", f"x{i}",
                                 direction="outgoing",
                                 ai_decision="greeting")

        from fastapi.testclient import TestClient
        from src.host.api import app
        with TestClient(app) as c:
            r = c.post("/tasks", json={
                "type": "facebook_campaign_run",
                "device_id": "d_camp",
                "params": {
                    "steps": ["send_greeting"],
                    "phase": "mature",
                    "greeting_targets": [{"name": "test"}],
                },
            })
        # gate 应该拒绝,返回 400 + error 包含"打招呼"
        assert r.status_code == 400
        detail = r.json().get("detail") or {}
        assert "打招呼" in (detail.get("error") or "")

    def test_friend_growth_also_receives_targets(self, monkeypatch, tmp_db):
        """friend_growth 现在只创建一个闭环任务，手工 targets 注入到该任务。"""
        import src.host.routers.facebook as fb_router
        captured = []
        monkeypatch.setattr(fb_router, "_post_create_task",
                            lambda b, p: captured.append(p) or {"task_id": "x"})
        monkeypatch.setattr(fb_router, "_is_local_device", lambda d: True)

        from fastapi.testclient import TestClient
        from src.host.api import app
        with TestClient(app) as c:
            r = c.post("/facebook/device/test_dev/launch", json={
                "preset_key": "friend_growth",
                "persona_key": "jp_female_midlife",
                "target_groups": ["ペット"],
                "verification_note": "同じグループで拝見しました",
                "greeting": "よろしくお願いします",
                "add_friend_targets": ["Z1", "Z2"],
            })
        assert r.status_code == 200
        assert len(captured) == 1
        assert captured[0]["type"] == "facebook_group_member_greet"
        params = captured[0]["params"]
        assert len(params["add_friend_targets"]) == 2
        assert params["steps"] == ["extract_members", "add_friends"]
