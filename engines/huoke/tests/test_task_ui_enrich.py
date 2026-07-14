# -*- coding: utf-8 -*-
"""task_ui_enrich 单元测试。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_infer_origin_explicit():
    from src.host.task_ui_enrich import build_task_ui_enrichment

    row = {
        "task_id": "x",
        "type": "tiktok_warmup",
        "device_id": "SERIAL12345678",
        "params": {"_created_via": "ai_chat", "phase": "auto"},
    }
    ui = build_task_ui_enrichment(row)
    assert ui["task_origin"] == "ai_chat"
    assert "AI 指令" in ui["task_origin_label_zh"]


def test_infer_origin_batch():
    from src.host.task_ui_enrich import build_task_ui_enrichment

    row = {
        "task_id": "x",
        "type": "tiktok_follow",
        "device_id": "ABCD",
        "batch_id": "abc12345",
        "params": {},
    }
    ui = build_task_ui_enrichment(row)
    assert ui["task_origin"] == "batch_api"


def test_phase_caption_auto():
    from src.host.task_ui_enrich import build_task_ui_enrichment

    row = {
        "task_id": "x",
        "type": "tiktok_warmup",
        "device_id": "Z",
        "params": {"phase": "auto"},
    }
    ui = build_task_ui_enrichment(row)
    assert ui.get("phase_caption")
    assert "定时调度" in ui["phase_caption"] or "不是" in ui["phase_caption"]


def test_params_for_display_hides_underscore():
    from src.host.task_ui_enrich import params_for_display

    d = params_for_display({"a": 1, "_created_via": "x"})
    assert d == {"a": 1}


def test_enrich_chat_response_batch():
    from src.host.task_ui_enrich import enrich_chat_response

    data = {
        "reply": "x",
        "actions_taken": [
            {
                "action": "tiktok_warmup",
                "batch_id": "b1",
                "task_ids": ["tidaaaaaaaaaa", "tidbbbbbbbbbb"],
                "device_ids": ["SERIAL_A", "SERIAL_B"],
                "device_labels": ["01号 · A", "02号 · B"],
                "count": 2,
            }
        ],
    }
    out = enrich_chat_response(data)
    assert "task_hints" in out
    assert out["task_hints"][0]["tasks"][0]["task_id"] == "tidaaaaaaaaaa"
    assert out["task_hints"][0]["tasks"][0]["device_label"] == "01号 · A"


def test_enrich_chat_response_single():
    from src.host.task_ui_enrich import enrich_chat_response

    data = {
        "actions_taken": [
            {
                "action": "tiktok_follow",
                "task_id": "fulluuidhere0001",
                "device_serial": "DEVSERIAL1",
                "device_label": "03号 · x",
            }
        ],
    }
    out = enrich_chat_response(data)
    assert out["task_hints"][0]["task_id"] == "fulluuidhere0001"
    assert out["task_hints"][0]["device_label"] == "03号 · x"


def test_to_response_includes_ui(monkeypatch):
    import src.host.task_ui_enrich as tue

    monkeypatch.setattr(tue, "_chat_alias_reverse", lambda: {"SERIALTEST123456": "7"})
    from src.host.api import _to_response

    r = _to_response(
        {
            "task_id": "tid",
            "type": "tiktok_warmup",
            "device_id": "SERIALTEST123456",
            "status": "failed",
            "params": {"_created_via": "ai_chat", "phase": "auto"},
            "result": {"error": "x"},
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
    )
    assert r.device_label
    assert "07号" in r.device_label or "7号" in r.device_label
    assert r.task_origin == "ai_chat"
