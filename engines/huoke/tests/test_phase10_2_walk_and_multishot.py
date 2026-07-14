# -*- coding: utf-8 -*-
"""Phase 10.2 additive: walk_candidates + l2_gate_shots 单测 (2026-04-24).

覆盖:
  * walk_candidates 默认 False → 走现有单结果路径, B 测试契约不变
  * walk_candidates=True + 候选有男性名 → 跳过男性
  * walk_candidates=True + 候选已联系过 → 跳过
  * walk_candidates=True + 无候选 → 回退 _first_search_result_element
  * l2_gate_shots=1 (默认) → 一次 classify (B 单图路径)
  * l2_gate_shots=4 + 首图 match → 早退, 只调 1 次 classify
  * l2_gate_shots=4 + 明确 REJECT → 也早退
  * l2_gate_shots=4 + 全部 classify 失败 → 返 True (保守阻止)
  * l2_gate_shots=4 + L2 PASS → 写 canonical metadata

全部 mock VLM (classify) + capture_profile_snapshots, 不依赖真机/ollama.
依赖 fixture ``tmp_db`` (conftest).
"""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, patch


def _stub_fb():
    from src.app_automation.facebook import FacebookAutomation
    from contextlib import contextmanager
    fb = FacebookAutomation.__new__(FacebookAutomation)
    fb._current_device = "D_p10_2"
    fb._current_account = ""
    fb._last_greet_skip_reason = ""
    fb._current_lead_cid = ""
    fb._messenger_installed_cache = {}
    fb._did = lambda dev=None: dev or "D_p10_2"
    fb._u2 = lambda dev=None: MagicMock()

    # stub guarded() 避免 base_automation 依赖 (self.guard / risk store)
    @contextmanager
    def _noop_guard(*a, **kw):
        yield
    fb.guarded = _noop_guard
    return fb


@pytest.fixture(autouse=True)
def _patch_sleep():
    with patch("src.app_automation.facebook.time.sleep"), \
         patch("src.app_automation.facebook.random.randint", return_value=1), \
         patch("src.app_automation.facebook.random.uniform", return_value=0.01):
        yield


# ─── walk_candidates 路由 ─────────────────────────────────────────────────

class TestWalkCandidatesRouting:
    def test_default_false_uses_single_result_path(self, tmp_db):
        """walk_candidates 不传 (默认 False) → 不调 _list_top_search_result_cards."""
        fb = _stub_fb()
        list_called = {"flag": False}
        fb._list_top_search_result_cards = lambda *a, **kw: (
            list_called.update(flag=True) or [])
        # 让单结果路径直接走 safe_interaction
        captured = {}

        def _spy(self, d, did, profile_name, note, *,
                 persona_key, source, preset_key, do_l2_gate=False, **kw):
            captured["called_single"] = True
            return True

        fb.search_people = lambda *a, **kw: [{"name": "Anyone"}]
        fb._first_search_result_element = lambda d, query_hint: MagicMock()
        fb.hb = MagicMock()
        fb._el_center = lambda el: (100, 100)
        fb._is_likely_fb_profile_page_xml = lambda xml: True
        fb._search_result_name_plausible = lambda a, b: True
        fb._adb = lambda *a, **kw: True

        with patch.object(type(fb), "_add_friend_safe_interaction_on_profile",
                          new=_spy), \
             patch("src.host.fb_concurrency.device_section_lock") as ml:
            ml.return_value.__enter__ = lambda *a: None
            ml.return_value.__exit__ = lambda *a: None
            fb._ensure_foreground = lambda *a, **kw: True
            fb.add_friend_with_note(
                "Anyone", persona_key=None, phase="growth",
                safe_mode=True, device_id="D_p10_2",
                # walk_candidates 不传
            )
        assert list_called["flag"] is False, \
            "默认 walk_candidates=False 不应调 _list_top_search_result_cards"
        assert captured.get("called_single") is True

    def test_walk_true_calls_list_cards(self, tmp_db):
        """walk_candidates=True → 调 _list_top_search_result_cards."""
        fb = _stub_fb()
        list_called = {"flag": False}
        fb._list_top_search_result_cards = lambda *a, **kw: (
            list_called.update(flag=True) or [
                {"name": "山田花子", "bounds": (0, 100, 500, 180),
                 "male_hint": False},
            ])
        fb._peer_already_contacted = lambda n: (False, "")
        fb.hb = MagicMock()
        fb.hb.tap = MagicMock()
        fb._is_likely_fb_profile_page_xml = lambda xml: True
        fb._adb = lambda *a, **kw: True
        fb.search_people = lambda *a, **kw: [{"name": "山田花子"}]

        reached_safe = {"flag": False}

        def _spy(self, d, did, profile_name, note, *,
                 persona_key, source, preset_key, do_l2_gate=False, **kw):
            reached_safe["flag"] = True
            return True

        with patch.object(type(fb), "_add_friend_safe_interaction_on_profile",
                          new=_spy), \
             patch("src.host.fb_concurrency.device_section_lock") as ml:
            ml.return_value.__enter__ = lambda *a: None
            ml.return_value.__exit__ = lambda *a: None
            fb._ensure_foreground = lambda *a, **kw: True
            fb.add_friend_with_note(
                "山田", persona_key=None, phase="growth",
                safe_mode=True, walk_candidates=True,
                device_id="D_p10_2",
            )
        assert list_called["flag"] is True
        assert reached_safe["flag"] is True, "女性候选应走到 safe_interaction"

    def test_walk_skips_male_hint(self, tmp_db):
        """walk=True + 候选 #1 male_hint=True → 跳过, 落到候选 #2."""
        fb = _stub_fb()
        fb._list_top_search_result_cards = lambda *a, **kw: [
            {"name": "山田太郎", "bounds": (0, 100, 500, 180),
             "male_hint": True},
            {"name": "山田花子", "bounds": (0, 200, 500, 280),
             "male_hint": False},
        ]
        fb._peer_already_contacted = lambda n: (False, "")
        fb.hb = MagicMock()
        fb.hb.tap = MagicMock()
        fb._is_likely_fb_profile_page_xml = lambda xml: True
        fb.search_people = lambda *a, **kw: [{"name": "山田"}]

        tapped_names = []

        def _spy(self, d, did, profile_name, note, **kw):
            tapped_names.append(profile_name)
            return True

        with patch.object(type(fb), "_add_friend_safe_interaction_on_profile",
                          new=_spy), \
             patch("src.host.fb_concurrency.device_section_lock") as ml:
            ml.return_value.__enter__ = lambda *a: None
            ml.return_value.__exit__ = lambda *a: None
            fb._ensure_foreground = lambda *a, **kw: True
            fb.add_friend_with_note(
                "山田", persona_key=None, phase="growth",
                safe_mode=True, walk_candidates=True,
                device_id="D_p10_2",
            )
        assert tapped_names == ["山田花子"], \
            f"期望跳过男性, 只点女性候选. 实际: {tapped_names}"

    def test_walk_skips_already_contacted(self, tmp_db):
        """walk=True + 候选 #1 已联系过 → 跳, 落到候选 #2."""
        fb = _stub_fb()
        fb._list_top_search_result_cards = lambda *a, **kw: [
            {"name": "山田花子", "bounds": (0, 100, 500, 180),
             "male_hint": False},
            {"name": "佐藤美咲", "bounds": (0, 200, 500, 280),
             "male_hint": False},
        ]
        # 第一个已联系, 第二个未联系
        def _peer(n):
            return (True, "already_greeted") if n == "山田花子" else (False, "")
        fb._peer_already_contacted = _peer
        fb.hb = MagicMock()
        fb.hb.tap = MagicMock()
        fb._is_likely_fb_profile_page_xml = lambda xml: True
        fb.search_people = lambda *a, **kw: [{"name": "日本"}]

        tapped = []

        def _spy(self, d, did, profile_name, note, **kw):
            tapped.append(profile_name)
            return True

        with patch.object(type(fb), "_add_friend_safe_interaction_on_profile",
                          new=_spy), \
             patch("src.host.fb_concurrency.device_section_lock") as ml:
            ml.return_value.__enter__ = lambda *a: None
            ml.return_value.__exit__ = lambda *a: None
            fb._ensure_foreground = lambda *a, **kw: True
            fb.add_friend_with_note(
                "日本", persona_key=None, phase="growth",
                safe_mode=True, walk_candidates=True,
                device_id="D_p10_2",
            )
        assert tapped == ["佐藤美咲"], \
            f"期望跳过已联系, 只点新候选. 实际: {tapped}"

    def test_walk_max_l2_calls_budget_stops_early(self, tmp_db):
        """Phase 10.3: walk 跑满 max_l2_calls=2 后 break, 剩下候选不试."""
        fb = _stub_fb()
        # 5 个女性候选, 全部无已联系记录 → 理论会全部试
        fb._list_top_search_result_cards = lambda *a, **kw: [
            {"name": f"女士{i}", "bounds": (0, 100 * i, 500, 100 * i + 80),
             "male_hint": False} for i in range(1, 6)
        ]
        fb._peer_already_contacted = lambda n: (False, "")
        fb.hb = MagicMock()
        fb.hb.tap = MagicMock()
        fb._is_likely_fb_profile_page_xml = lambda xml: True
        fb.search_people = lambda *a, **kw: [{"name": "女"}]

        tapped = []

        def _spy(self, d, did, profile_name, note, **kw):
            tapped.append(profile_name)
            return False  # 每次 L2 REJECT, 触发 BACK 下一位

        with patch.object(type(fb), "_add_friend_safe_interaction_on_profile",
                          new=_spy), \
             patch("src.host.fb_concurrency.device_section_lock") as ml:
            ml.return_value.__enter__ = lambda *a: None
            ml.return_value.__exit__ = lambda *a: None
            fb._ensure_foreground = lambda *a, **kw: True
            fb.add_friend_with_note(
                "女", persona_key=None, phase="growth",
                safe_mode=True, walk_candidates=True,
                max_l2_calls=2,
                device_id="D_p10_2",
            )
        assert len(tapped) == 2, \
            f"budget=2 应只试 2 个候选, 实际试了 {len(tapped)}: {tapped}"

    def test_walk_empty_falls_back_to_single_result(self, tmp_db):
        """walk=True + _list_top_search_result_cards 返空 → 回退 _first_search_result_element."""
        fb = _stub_fb()
        fb._list_top_search_result_cards = lambda *a, **kw: []  # 空
        first_called = {"flag": False}

        def _fake_first(d, query_hint):
            first_called["flag"] = True
            return MagicMock()

        fb._first_search_result_element = _fake_first
        fb.hb = MagicMock()
        fb._el_center = lambda el: (100, 100)
        fb._is_likely_fb_profile_page_xml = lambda xml: True
        fb._search_result_name_plausible = lambda a, b: True
        fb._adb = lambda *a, **kw: True
        fb.search_people = lambda *a, **kw: [{"name": "Anyone"}]

        def _spy(self, d, did, profile_name, note, **kw):
            return True

        with patch.object(type(fb), "_add_friend_safe_interaction_on_profile",
                          new=_spy), \
             patch("src.host.fb_concurrency.device_section_lock") as ml:
            ml.return_value.__enter__ = lambda *a: None
            ml.return_value.__exit__ = lambda *a: None
            fb._ensure_foreground = lambda *a, **kw: True
            fb.add_friend_with_note(
                "Anyone", persona_key=None, phase="growth",
                safe_mode=True, walk_candidates=True,
                device_id="D_p10_2",
            )
        assert first_called["flag"] is True, \
            "walk 空候选应回退 _first_search_result_element"


# ─── l2_gate_shots multi-shot ─────────────────────────────────────────────

class TestPhase10L2MultiShot:
    def test_shots_1_single_classify(self, tmp_db):
        """l2_gate_shots=1 (默认) → 只调 1 次 classify, B 路径."""
        fb = _stub_fb()
        fb.capture_profile_snapshots = lambda *a, **kw: {
            "image_paths": ["/tmp/a.png"], "shot_count": 1,
            "save_dir": "/tmp", "display_name": "", "bio_text": "",
        }
        n_calls = {"n": 0}

        def _fake(**kw):
            n_calls["n"] += 1
            return {"ok": True, "match": True, "stage_reached": "L2",
                    "score": 80,
                    "l2": {"pass": True, "score": 80, "reasons": []},
                    "insights": {}}

        with patch("src.host.fb_profile_classifier.classify",
                   side_effect=_fake):
            blocked = fb._phase10_l2_gate(
                d=MagicMock(), did="D_p10_2",
                profile_name="X", persona_key="jp_female_midlife",
            )  # shots 默认 1
        assert blocked is False
        assert n_calls["n"] == 1, \
            f"shots=1 应只调 1 次 classify, 实际: {n_calls['n']}"

    def test_shots_4_first_match_early_exit(self, tmp_db):
        """shots=4 + 首图 match → 只调 1 次 classify (早退)."""
        fb = _stub_fb()
        fb.capture_profile_snapshots = lambda *a, **kw: {
            "image_paths": ["/tmp/a.png", "/tmp/b.png",
                            "/tmp/c.png", "/tmp/d.png"],
            "shot_count": 4, "save_dir": "/tmp",
            "display_name": "", "bio_text": "",
        }
        n_calls = {"n": 0}

        def _fake(**kw):
            n_calls["n"] += 1
            return {"ok": True, "match": True, "stage_reached": "L2",
                    "score": 85,
                    "l2": {"pass": True, "score": 85, "reasons": ["女性"]},
                    "insights": {"gender": "female", "age_band": "40s",
                                 "is_japanese": True}}

        with patch("src.host.fb_profile_classifier.classify",
                   side_effect=_fake):
            blocked = fb._phase10_l2_gate(
                d=MagicMock(), did="D_p10_2",
                profile_name="Y", persona_key="jp_female_midlife",
                shots=4,
            )
        assert blocked is False
        assert n_calls["n"] == 1, \
            f"首图 match 应早退, 实际调 {n_calls['n']} 次"

    def test_shots_4_obvious_reject_early_exit(self, tmp_db):
        """shots=4 + 首图 gender=male → 明确 REJECT 也早退."""
        fb = _stub_fb()
        fb.capture_profile_snapshots = lambda *a, **kw: {
            "image_paths": ["/tmp/a.png", "/tmp/b.png",
                            "/tmp/c.png", "/tmp/d.png"],
            "shot_count": 4, "save_dir": "/tmp",
            "display_name": "", "bio_text": "",
        }
        n_calls = {"n": 0}

        def _fake(**kw):
            n_calls["n"] += 1
            return {"ok": True, "match": False, "stage_reached": "L2",
                    "score": 15,
                    "l2": {"pass": False, "score": 15, "reasons": ["男性"]},
                    "insights": {"gender": "male"}}

        with patch("src.host.fb_profile_classifier.classify",
                   side_effect=_fake):
            blocked = fb._phase10_l2_gate(
                d=MagicMock(), did="D_p10_2",
                profile_name="Bro", persona_key="jp_female_midlife",
                shots=4,
            )
        assert blocked is True, "明确男性 REJECT 应阻止"
        assert n_calls["n"] == 1, \
            f"明确 REJECT 早退, 应只调 1 次, 实际: {n_calls['n']}"

    def test_shots_4_all_fail_blocks_conservatively(self, tmp_db):
        """shots=4 + 所有 classify 抛异常 → 返 True (保守阻止 + journey)."""
        fb = _stub_fb()
        fb.capture_profile_snapshots = lambda *a, **kw: {
            "image_paths": ["/tmp/a.png", "/tmp/b.png"],
            "shot_count": 2, "save_dir": "/tmp",
            "display_name": "", "bio_text": "",
        }

        with patch("src.host.fb_profile_classifier.classify",
                   side_effect=RuntimeError("VLM down")):
            blocked = fb._phase10_l2_gate(
                d=MagicMock(), did="D_p10_2",
                profile_name="NoNet", persona_key="jp_female_midlife",
                shots=4,
            )
        assert blocked is True, "多 shot 全失败应保守阻止"
        # journey 写了 l2_all_shots_failed
        from src.host.lead_mesh import resolve_identity, get_journey
        cid = resolve_identity(platform="facebook",
                                account_id="fb:NoNet", display_name="NoNet")
        events = get_journey(cid)
        blocked_evts = [e for e in events if e["action"] == "add_friend_blocked"]
        assert any(e["data"].get("reason") == "l2_all_shots_failed"
                   for e in blocked_evts), \
            f"期望 l2_all_shots_failed event, 实际: {blocked_evts}"

    def test_shots_multi_match_overrides_reject_insights(self, tmp_db):
        """Phase 10.3: match=True shot 的 insights 覆盖先前 REJECT shot 的字段.

        回归 bug: shot1 REJECT 误报 gender=male, shot2 PASS 给 gender=female —
        旧"首次非空"策略会保留错的 male. 新策略: PASS shot 覆盖 REJECT shot.
        """
        fb = _stub_fb()
        fb.capture_profile_snapshots = lambda *a, **kw: {
            "image_paths": ["/tmp/a.png", "/tmp/b.png", "/tmp/c.png"],
            "shot_count": 3, "save_dir": "/tmp",
            "display_name": "", "bio_text": "",
        }

        results_iter = iter([
            # shot1: REJECT 但没触发"明确 REJECT"早退 (没 male 也没 jp=False),
            # 给了错误的 age_band
            {"ok": True, "match": False, "stage_reached": "L2",
             "score": 40,
             "l2": {"pass": False, "score": 40, "reasons": ["不清楚"]},
             "insights": {"age_band": "20s", "gender": ""}},
            # shot2: PASS, 真正 age_band=40s + gender=female
            {"ok": True, "match": True, "stage_reached": "L2",
             "score": 88,
             "l2": {"pass": True, "score": 88, "reasons": ["PASS"]},
             "insights": {"age_band": "40s", "gender": "female",
                          "is_japanese": True}},
        ])

        with patch("src.host.fb_profile_classifier.classify",
                   side_effect=lambda **kw: next(results_iter)):
            blocked = fb._phase10_l2_gate(
                d=MagicMock(), did="D_p10_2",
                profile_name="混图测试", persona_key="jp_female_midlife",
                shots=3,
            )
        assert blocked is False, "shot2 PASS 应让 gate 放行"

        from src.host.lead_mesh import resolve_identity
        from src.host.lead_mesh.canonical import _connect
        import json
        cid = resolve_identity(platform="facebook",
                                account_id="fb:混图测试",
                                display_name="混图测试")
        with _connect() as conn:
            row = conn.execute(
                "SELECT metadata_json FROM leads_canonical WHERE canonical_id=?",
                (cid,),
            ).fetchone()
        meta = json.loads((row["metadata_json"] if row else "") or "{}")
        assert meta.get("age_band") == "40s", \
            f"应以 PASS shot 的 age_band=40s 覆盖 REJECT shot 的 20s, 实际: {meta}"
        assert meta.get("gender") == "female"

    def test_shots_4_pass_writes_canonical_metadata(self, tmp_db):
        """shots=4 + L2 PASS → leads_canonical.metadata_json 聚合 insights."""
        fb = _stub_fb()
        fb.capture_profile_snapshots = lambda *a, **kw: {
            "image_paths": ["/tmp/a.png", "/tmp/b.png"],
            "shot_count": 2, "save_dir": "/tmp",
            "display_name": "", "bio_text": "",
        }

        def _fake(**kw):
            return {"ok": True, "match": True, "stage_reached": "L2",
                    "score": 90,
                    "l2": {"pass": True, "score": 90,
                           "reasons": ["女性", "日文"]},
                    "insights": {"gender": "female", "age_band": "40s",
                                 "is_japanese": True,
                                 "is_japanese_confidence": 0.92,
                                 "overall_confidence": 0.88}}

        with patch("src.host.fb_profile_classifier.classify",
                   side_effect=_fake):
            blocked = fb._phase10_l2_gate(
                d=MagicMock(), did="D_p10_2",
                profile_name="花子", persona_key="jp_female_midlife",
                shots=4,
            )
        assert blocked is False
        from src.host.lead_mesh import resolve_identity
        from src.host.lead_mesh.canonical import _connect
        import json
        cid = resolve_identity(platform="facebook",
                                account_id="fb:花子", display_name="花子")
        with _connect() as conn:
            row = conn.execute(
                "SELECT metadata_json, tags FROM leads_canonical WHERE canonical_id=?",
                (cid,),
            ).fetchone()
        assert row is not None, f"canonical {cid} 未入库"
        meta = json.loads(row["metadata_json"] or "{}")
        assert meta.get("age_band") == "40s", \
            f"canonical metadata 应写 age_band, 实际: {meta}"
        assert meta.get("gender") == "female"
        assert meta.get("is_japanese") is True
        assert meta.get("l2_shots") == 4
        tags_str = row["tags"] or ""
        tags = [t.strip() for t in tags_str.split(",") if t.strip()]
        assert "l2_verified" in tags, f"tags 应含 l2_verified, 实际: {tags}"
