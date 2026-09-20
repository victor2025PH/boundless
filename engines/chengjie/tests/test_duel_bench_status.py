# -*- coding: utf-8 -*-
"""对练台状态聚合纯函数门禁（零网络）。"""

from __future__ import annotations

import json

from src.utils.duel_bench_status import (
    build_status,
    load_json_file,
    read_trend,
    trend_direction,
)


def test_read_trend_skips_bad_lines(tmp_path):
    # ts 锚 now（时间炸弹加固）：本测试不带 days 窗暂不受日历影响，但若将来
    # read_trend 加默认窗，硬日期会静默变成第二颗 duel 型炸弹——统一锚 now。
    from datetime import datetime
    day = datetime.now().strftime("%Y-%m-%d")
    p = tmp_path / "t.jsonl"
    p.write_text(
        json.dumps({"ts": f"{day}T01:00:00", "defects_per_100_turns": 1})
        + "\nnot-json\n"
        + json.dumps({"ts": f"{day}T02:00:00", "defects_per_100_turns": 2})
        + "\n",
        encoding="utf-8")
    rows = read_trend(p)
    assert len(rows) == 2


def test_read_trend_missing_file():
    assert read_trend(__import__("pathlib").Path("/no/such/trend.jsonl")) == []


def test_trend_direction():
    assert trend_direction([]) == "unknown"
    assert trend_direction([{"defects_per_100_turns": 1}]) == "unknown"
    assert trend_direction([
        {"defects_per_100_turns": 8},
        {"defects_per_100_turns": 2},
    ]) == "better"
    assert trend_direction([
        {"defects_per_100_turns": 2},
        {"defects_per_100_turns": 8},
    ]) == "worse"
    assert trend_direction([
        {"defects_per_100_turns": 2},
        {"defects_per_100_turns": 2.005},
    ]) == "flat"


def test_build_status_empty_root(tmp_path):
    st = build_status(tmp_path)
    assert st["ok"] is True
    assert st["active"] is False
    assert st["nightly"]["direction"] == "unknown"


def test_default_root_is_engine_root():
    """DEFAULT_ROOT 必须落在引擎代码根（有 main.py + scripts/），否则夜跑写盘位置
    与 API 读盘位置分叉——搬过一次模块目录层级，这条钉住 parents[] 深度。"""
    from src.utils.duel_bench_status import DEFAULT_ROOT
    assert (DEFAULT_ROOT / "main.py").is_file()
    assert (DEFAULT_ROOT / "scripts" / "duel_nightly.ps1").is_file()


def test_build_status_with_artifacts(tmp_path):
    duel = tmp_path / "logs" / "duel"
    evald = tmp_path / "logs" / "eval"
    duel.mkdir(parents=True)
    evald.mkdir(parents=True)
    (duel / "latest_summary.json").write_text(json.dumps({
        "scenarios": 2, "turns": 16, "defects": 1,
        "by_kind": {"catalog_price": 1},
        "over_budget": {"price_haggler": ["catalog_price 1 处 > 允许 0"]},
    }), encoding="utf-8")
    (duel / "LAST_RUN.json").write_text(json.dumps({
        "exit_code": 1, "over_budget": True, "defects": 1,
    }), encoding="utf-8")
    # 时间戳锚定 now（时间炸弹修复，2026-08-12）：原硬编码 2026-07-27/28 在
    # build_status 的 14 天窗（read_trend days=14）里走到 2026-08-11 整体出窗
    # → points==0 自爆，红挂 sweep 两天无人认领（与谁的改动都无关）。
    # 锚 now-2d/-1d 后与日历永久解耦；direction=better 需要 旧10→新5 的顺序。
    from datetime import datetime, timedelta
    d0 = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%dT02:10:00")
    d1 = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT02:10:00")
    (duel / "duel_trend.jsonl").write_text(
        json.dumps({"ts": d0, "defects_per_100_turns": 10, "scenarios": 2})
        + "\n"
        + json.dumps({"ts": d1, "defects_per_100_turns": 5, "scenarios": 2})
        + "\n",
        encoding="utf-8")
    s0 = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT07:10:00")
    s1 = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT07:11:00")
    (evald / "duel_semantic_trend.jsonl").write_text(
        json.dumps({"ts": s0, "mode": "corpus", "passed": True}) + "\n"
        + json.dumps({"ts": s1, "mode": "llm", "passed": True,
                      "sycophancy_recall": 1.0}) + "\n",
        encoding="utf-8")
    st = build_status(tmp_path)
    assert st["active"] is True
    assert st["over_budget"] is True
    assert st["nightly"]["points"] == 2
    assert st["nightly"]["direction"] == "better"
    assert st["semantic"]["points"] == 1  # corpus 行不计
    assert st["semantic"]["last_passed"] is True
    assert load_json_file(duel / "LAST_RUN.json")["exit_code"] == 1


def test_load_expectations_bom_and_bad_card_isolated(tmp_path):
    """单卡 BOM/坏 JSON 不得清空整批期望（夜跑回归门禁）。"""
    from scripts.duel_judge import load_expectations
    good = {"id": "ok_sc", "expect_defects": {"catalog_price": 1}}
    (tmp_path / "ok.json").write_text(
        json.dumps(good), encoding="utf-8")
    # UTF-8 BOM + 合法 JSON
    (tmp_path / "bom.json").write_bytes(
        b"\xef\xbb\xbf" + json.dumps(
            {"id": "bom_sc", "expect_defects": {"gated_line": 2}}
        ).encode("utf-8"))
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    exp = load_expectations(str(tmp_path))
    assert exp["ok_sc"] == {"catalog_price": 1}
    assert exp["bom_sc"] == {"gated_line": 2}
    assert "broken" not in exp


def test_duel_bench_route_registered():
    import types
    from fastapi import FastAPI
    from src.web.routes.ops_overview_routes import register_ops_overview_routes

    ctx = types.SimpleNamespace(
        api_auth=lambda request: True,
        api_write=lambda perm: (lambda: True),
        page_auth=lambda request: True,
        templates=None,
        config_manager=types.SimpleNamespace(config={}),
        audit_store=None,
        user_store=None,
        token=None,
        telegram_client=None,
    )
    app = FastAPI()
    register_ops_overview_routes(app, ctx)
    paths = {getattr(r, "path", None) for r in app.routes}
    assert "/api/admin/duel-bench" in paths
