# -*- coding: utf-8 -*-
"""hub 音色档核对工具的纯函数门禁（2026-08-02 chengjie_* 404 事故沉淀）。"""
import importlib.util
import sys
from pathlib import Path

_TOOL = Path(__file__).resolve().parents[1] / "tools" / "check_hub_voice_profiles.py"
_spec = importlib.util.spec_from_file_location("check_hub_voice_profiles", _TOOL)
_mod = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("check_hub_voice_profiles", _mod)
_spec.loader.exec_module(_mod)
diff_profiles = _mod.diff_profiles


def test_all_mapped_and_present():
    res = diff_profiles(
        ["lin_xiaoyu", "su_wan"],
        {"lin_xiaoyu": "林小雨-智聊", "su_wan": "苏婉"},
        ["林小雨-智聊", "苏婉", "陈默"],
    )
    assert res["missing"] == []
    assert ("lin_xiaoyu", "林小雨-智聊") in res["ok"]


def test_missing_mapped_profile_flagged():
    """事故形态：映射到 hub 不存在的档名必须被点名。"""
    res = diff_profiles(
        ["lin_xiaoyu"],
        {"lin_xiaoyu": "chengjie_lin_xiaoyu"},
        ["林小雨-智聊", "林小雨"],
    )
    assert res["ok"] == []
    assert res["missing"] == [("lin_xiaoyu", "chengjie_lin_xiaoyu")]


def test_unmapped_pid_falls_back_to_pid_itself():
    """无映射条目按 hub_fish 语义用 pid 本身当档名核对。"""
    res = diff_profiles(["陈默"], {}, ["陈默"])
    assert res["missing"] == []
    res2 = diff_profiles(["chen_mo"], {}, ["陈默"])
    assert res2["missing"] == [("chen_mo", "chen_mo")]


def test_blank_and_empty_inputs_safe():
    assert diff_profiles([], {}, []) == {"ok": [], "missing": []}
    res = diff_profiles(["", "  ", "a"], None, ["a"])
    assert res["ok"] == [("a", "a")] and res["missing"] == []
