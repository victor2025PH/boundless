# -*- coding: utf-8 -*-
"""人群预设合并与 prepare_task_params 路径测试。"""

import pytest

from src.host.audience_preset import audience_presets_etag, list_presets, merge_audience_preset
from src.host.task_param_rules import maybe_normalize_for_task, normalize_params, prepare_task_params


def test_audience_presets_etag_format():
    etag, ver, mtime = audience_presets_etag()
    assert isinstance(ver, int)
    assert isinstance(mtime, float)
    parts = etag.split(":")
    assert len(parts) == 2
    assert parts[0] == str(ver)


def test_list_presets_has_ids():
    ids = {p["id"] for p in list_presets()}
    assert "italy_male_30p" in ids
    assert "usa_broad" in ids


def test_merge_italy_preset_tiktok_follow():
    merged, notes = merge_audience_preset(
        "tiktok_follow",
        {"audience_preset": "italy_male_30p", "max_follows": 12},
    )
    assert merged.get("target_country") == "italy"
    assert merged.get("max_follows") == 12
    assert merged.get("_audience_preset") == "italy_male_30p"
    assert any("预设" in n for n in notes)


def test_prepare_task_params_applies_preset_and_normalizes():
    norm, warns = prepare_task_params(
        "tiktok_follow",
        {"audience_preset": "italy_male_30p", "max_follows": "9"},
    )
    assert norm["max_follows"] == 9
    assert norm.get("target_country") == "italy"
    assert norm.get("_audience_preset") == "italy_male_30p"


def test_strict_task_params_removes_unknown(monkeypatch):
    import src.host.task_policy as task_policy

    monkeypatch.setattr(
        task_policy,
        "load_task_execution_policy",
        lambda force_reload=False: {**task_policy._DEFAULTS, "strict_task_params": True},
    )
    norm, warns = normalize_params(
        "tiktok_follow",
        {"max_follows": 5, "not_a_schema_field": 123},
    )
    assert "not_a_schema_field" not in norm
    assert any("未知" in w for w in warns)


def test_maybe_normalize_disabled_still_merges_preset(monkeypatch):
    import src.host.task_policy as task_policy

    monkeypatch.setattr(
        task_policy,
        "load_task_execution_policy",
        lambda force_reload=False: {**task_policy._DEFAULTS, "normalize_task_params": False},
    )
    out = maybe_normalize_for_task(
        "tiktok_follow",
        {"audience_preset": "usa_broad"},
    )
    assert out.get("target_country") == "usa"
