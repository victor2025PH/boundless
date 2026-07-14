# -*- coding: utf-8 -*-
"""任务参数归一化单元测试。"""

import pytest

from src.host.task_param_rules import (
    extract_json_object,
    normalize_params,
    resolve_schema_entry,
)


def test_resolve_tiktok_follow_schema():
    p, spec = resolve_schema_entry("tiktok_follow")
    assert p == "tiktok_follow"
    assert "max_follows" in (spec.get("fields") or {})


def test_normalize_follow_defaults():
    norm, warns = normalize_params("tiktok_follow", {"max_follows": "15"})
    assert norm["max_follows"] == 15
    assert norm.get("target_country") == "italy"


def test_normalize_clamp_max_follows():
    norm, _ = normalize_params("tiktok_follow", {"max_follows": 9999})
    assert norm["max_follows"] == 500


def test_extract_json():
    s = 'Here is JSON:\n```\n{"a": 1}\n```'
    j = extract_json_object(s)
    assert j == {"a": 1}
