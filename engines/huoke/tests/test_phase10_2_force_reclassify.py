# -*- coding: utf-8 -*-
"""Phase 10.2: classify(force_reclassify=True) 跳过 7 天去重缓存单测.

背景: classify() 默认查 _db_get_recent 7 天 cache (`dedup_window_hours=168`),
命中直接返不重跑 L1/L2. 但 use case:
  * peer 真改头像 → 想重新判
  * 上次 launcher 截图被 Phase 10.1 REJECT 写入 cache → 真 profile 重判想跳 cache
  * debug / re-tune persona → 强制重判
加 force_reclassify 参数解决.

Phase 10.2 datapoint (真 ollama 验证, docs/PHASE10_PARTIAL_SMOKE_*.md 接续):
  * 同 (persona, target_key) 第 2 次调 latency = 0.01s (vs 第 1 次 45.3s, 8151x)
  * cache 已工作好, force_reclassify 是 escape hatch 不是 fix
"""
from __future__ import annotations

from unittest.mock import patch, MagicMock

import pytest


class TestForceReclassifySkipCache:
    def test_default_uses_cache(self, tmp_db):
        """force_reclassify 不传 → _db_get_recent 被调."""
        from src.host import fb_profile_classifier as fpc
        with patch.object(fpc, "_db_get_recent",
                          return_value={"stage": "L1", "match": True,
                                        "score": 50.0, "insights": {},
                                        "at": "2026-04-24"}) as m_cache:
            with patch.object(fpc.fb_personas, "get_persona",
                              return_value={"persona_key": "test"}):
                with patch.object(fpc.fb_personas, "get_quotas", return_value={}):
                    with patch.object(fpc.fb_personas, "get_risk_guard", return_value={}):
                        with patch.object(fpc.fb_personas,
                                          "get_dedup_window_hours",
                                          return_value=168):
                            r = fpc.classify(
                                device_id="d1",
                                persona_key="test",
                                target_key="t1",
                                do_l2=False,
                            )
        assert m_cache.called, "默认应查 cache"
        assert r["from_cache"] is True

    def test_force_reclassify_skips_cache(self, tmp_db):
        """force_reclassify=True → _db_get_recent 不被调, 重新跑 L1."""
        from src.host import fb_profile_classifier as fpc
        with patch.object(fpc, "_db_get_recent",
                          return_value={"stage": "L1", "match": True,
                                        "score": 99.0, "insights": {},
                                        "at": "2026-04-24"}) as m_cache:
            with patch.object(fpc.fb_personas, "get_persona",
                              return_value={"persona_key": "test"}):
                with patch.object(fpc.fb_personas, "get_quotas", return_value={}):
                    with patch.object(fpc.fb_personas, "get_risk_guard", return_value={}):
                        with patch.object(fpc.fb_personas,
                                          "get_dedup_window_hours",
                                          return_value=168):
                            with patch.object(fpc, "score_l1",
                                              return_value=(50.0, ["ok"])):
                                with patch.object(fpc, "_db_insert_insight",
                                                  return_value=1):
                                    r = fpc.classify(
                                        device_id="d1",
                                        persona_key="test",
                                        target_key="t1",
                                        do_l2=False,
                                        force_reclassify=True,
                                    )
        assert not m_cache.called, "force_reclassify=True 时不应查 cache"
        assert r["from_cache"] is False
        assert r["score"] == 50.0, "应跑真 L1, 不返 cached score 99"

    def test_default_value_is_false(self, tmp_db):
        """默认 force_reclassify=False (向后兼容)."""
        from src.host.fb_profile_classifier import classify
        import inspect
        sig = inspect.signature(classify)
        assert sig.parameters["force_reclassify"].default is False, \
            "默认必须 False, 否则破老 caller (强制每次跑 VLM = 性能爆炸)"
