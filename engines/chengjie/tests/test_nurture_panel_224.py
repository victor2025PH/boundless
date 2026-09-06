# -*- coding: utf-8 -*-
"""M-5 B（#224，2026-09-06）：养号面板矛盾文案 + 灰置原因 + 三阶段人话说明。

6TGCPC / 9TJCFK 实录：机群概览判词「有账号需要留意」配「10 正常 / 0 起号期 / 0 需关注」
自相矛盾（判词读 account_health 灯、数字读 lifecycle restricted+banned；断线 3 天的号两边
都不算）；「模拟运行」灰置只有悬浮 title 说原因；自动阶段做什么/频率/上限/回退无说明。
钉住：

- ``attention_fields`` / ``fleet_summary``：需关注 = 灯黄红 ∪ 掉线/待登录/受限/被封；
  正常 + 需关注 = 总数；判词由同一集合推出（有 red/banned → red，有需关注 → amber）；
- ``nurture_explain_facts``：数字取自 scheduler 常量 + 配置（8/15/25、60/40/25 分、三时段、
  退避阈值 1/5），不写死在文案里；
- ``/api/nurture/status`` 带 ``summary`` / ``explain`` / 每号 ``attention*``；
- cp-nurture：优先读 summary、灰置原因就地一行、三阶段说明可展开；i18n 双语齐；不改引擎。
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.web.routes.nurture_routes import (attention_fields, fleet_summary,
                                           nurture_explain_facts,
                                           register_nurture_routes)

REPO = Path(__file__).resolve().parents[1]


# ── 纯函数 ──────────────────────────────────────────────────────────────────

def test_attention_fields_offline_counts_even_with_green_light():
    f = attention_fields({"stage": "offline"}, {"light": "green", "reasons": ["账号健康，无风控信号"]})
    assert f["attention"] is True and f["attention_reasons"] == ["stage_offline"]
    assert f["light"] == "green"


def test_attention_fields_amber_reasons_and_clean_active():
    f = attention_fields({"stage": "active"},
                         {"light": "amber", "reasons": ["未绑定独立代理（多号共出口 IP 极易被关联封号）"]})
    assert f["attention"] is True and f["attention_reasons"][0].startswith("未绑定独立代理")
    ok = attention_fields({"stage": "active"}, {"light": "green", "reasons": []})
    assert ok["attention"] is False and ok["attention_reasons"] == []
    # 灯缺失（旧后端 / 探不到）只按生命周期
    assert attention_fields({"stage": "warming"}, None)["attention"] is False
    assert attention_fields({"stage": "pending"}, None)["attention_reasons"] == ["stage_pending"]


def _acct(stage, light="green", key="telegram:x", label="", reasons=None):
    item = {"stage": stage, "nurture_key": key, "label": label}
    item.update(attention_fields(item, {"light": light, "reasons": reasons or []}))
    return item


def test_fleet_summary_consistent_with_verdict():
    accts = [_acct("active") for _ in range(9)] + [_acct("offline", key="telegram:alixia", label="Alixia")]
    s = fleet_summary(accts, {"active": 9, "offline": 1})
    assert s["total"] == 10 and s["normal"] == 9 and s["attention"] == 1
    assert s["normal"] + s["attention"] == s["total"]
    assert s["verdict"] == "amber"                       # 有需关注 → 黄；不再「黄灯配 0 需关注」
    assert s["attention_list"] == [{"key": "telegram:alixia", "label": "Alixia",
                                    "reasons": ["stage_offline"]}]
    assert fleet_summary([_acct("active")] * 3, {})["verdict"] == "green"
    assert fleet_summary([], {})["verdict"] == "unknown"
    red = fleet_summary([_acct("active"), _acct("banned", light="red", reasons=["账号已被封禁"])], {})
    assert red["verdict"] == "red" and red["attention"] == 1
    amber_light = fleet_summary([_acct("active", light="amber", reasons=["近 24h 触发 2 次限频"])], {})
    assert amber_light["verdict"] == "amber" and amber_light["normal"] == 0


def test_explain_facts_from_scheduler_constants():
    ex = nurture_explain_facts({})
    assert ex["cadence"]["conservative"] == {"daily_budget": 8, "min_gap_min": 60}
    assert ex["cadence"]["balanced"] == {"daily_budget": 15, "min_gap_min": 40}
    assert ex["cadence"]["aggressive"] == {"daily_budget": 25, "min_gap_min": 25}
    assert ex["hours"] == ["9-11", "13-14", "18-21"]
    assert ex["behaviors"] == ["online", "read", "browse", "react"] and ex["self_chat_enabled"] is False
    assert ex["risk_backoff"] == {"enabled": True, "flood_threshold": 1, "error_threshold": 5}
    assert set(ex["skip_stages"]) == {"offline", "restricted", "banned", "pending"}
    ex2 = nurture_explain_facts({"self_chat": {"enabled": True}, "hours": ["10-12"],
                                 "risk_backoff": {"enabled": False}})
    assert "self_chat" in ex2["behaviors"] and ex2["hours"] == ["10-12"]
    assert ex2["risk_backoff"]["enabled"] is False


# ── 路由 ────────────────────────────────────────────────────────────────────

class _CM:
    def __init__(self):
        self.config = {"ops": {"nurture": {"enabled": False, "dry_run": True}}}


def _status(monkeypatch):
    import src.skills.account_signals as sig

    def _fake_overview(accounts, **kw):
        return {
            "fleet": {"fleet_light": "amber", "counts": {"green": 2, "amber": 1, "red": 0},
                      "accounts": [
                          {"account_id": "a1", "light": "green", "reasons": ["账号健康，无风控信号"]},
                          {"account_id": "alixia", "light": "green", "reasons": ["账号健康，无风控信号"]},
                          {"account_id": "a3", "light": "amber", "reasons": ["未绑定独立代理（多号共出口 IP 极易被关联封号）"]},
                      ]},
            "lifecycle": {"active": 2, "offline": 1},
            "accounts": [
                {"platform": "telegram", "account_id": "a1", "stage": "active"},
                {"platform": "telegram", "account_id": "alixia", "stage": "offline"},
                {"platform": "whatsapp", "account_id": "a3", "stage": "active"},
            ],
            "total": 3,
        }

    monkeypatch.setattr(sig, "fleet_overview", _fake_overview)
    app = FastAPI()

    async def _auth():
        return True

    register_nurture_routes(app, _auth, audit_store=None, config_manager=_CM())
    return TestClient(app).get("/api/nurture/status").json()


def test_status_carries_consistent_summary_and_explain(monkeypatch):
    d = _status(monkeypatch)
    assert d["ok"]
    s = d["summary"]
    assert s == {
        "total": 3, "normal": 1, "warming": 0, "attention": 2, "verdict": "amber",
        "attention_list": [
            {"key": "telegram:alixia", "label": "", "reasons": ["stage_offline"]},
            {"key": "whatsapp:a3", "label": "", "reasons": ["未绑定独立代理（多号共出口 IP 极易被关联封号）"]},
        ],
    }
    by = {a["nurture_key"]: a for a in d["accounts"]}
    assert by["telegram:alixia"]["attention"] is True and by["telegram:alixia"]["light"] == "green"
    assert by["telegram:a1"]["attention"] is False
    assert d["explain"]["cadence"]["balanced"]["daily_budget"] == 15
    # 引擎逻辑未动：仍 enabled=False / dry_run=True
    assert d["enabled"] is False and d["dry_run"] is True


# ── 前端 / i18n ─────────────────────────────────────────────────────────────

def test_cp_nurture_reads_summary_and_shows_reason_inline():
    a = (REPO / "shared" / "copilot" / "components" / "cp-nurture.js").read_bytes()
    b = (REPO / "desktop" / "renderer" / "shared" / "copilot" / "components" / "cp-nurture.js").read_bytes()
    assert a == b, "cp-nurture.js 双树不同步"
    js = a.decode("utf-8")
    assert "d.summary" in js and "sm.attention" in js and "sm.verdict" in js
    assert 'data-role="eng-try-why"' in js                      # 灰置原因就地一行
    assert "_explainHtml(d)" in js and 'data-act="explain-toggle"' in js
    for k in ("cp.nurture.explain_hd", "cp.nurture.explain_s1", "cp.nurture.explain_s2", "cp.nurture.explain_s3",
              "cp.nurture.explain_actions", "cp.nurture.explain_limits", "cp.nurture.explain_hours",
              "cp.nurture.explain_backoff", "cp.nurture.risk_hint", "cp.nurture.att_"):
        assert k in js, k


def test_cp_i18n_bilingual_nurture_keys():
    i18n = (REPO / "shared" / "copilot" / "i18n" / "cp-i18n.js").read_text(encoding="utf-8")
    assert i18n == (REPO / "desktop" / "renderer" / "shared" / "copilot" / "i18n" / "cp-i18n.js").read_text(
        encoding="utf-8")
    for k in ("cp.nurture.active_hint", "cp.nurture.risk_hint", "cp.nurture.att_stage_offline",
              "cp.nurture.att_stage_pending", "cp.nurture.att_stage_restricted", "cp.nurture.att_stage_banned",
              "cp.nurture.att_light_amber", "cp.nurture.att_light_red", "cp.nurture.explain_hd",
              "cp.nurture.explain_s1", "cp.nurture.explain_s2", "cp.nurture.explain_s3",
              "cp.nurture.explain_actions", "cp.nurture.explain_limits", "cp.nurture.explain_prof",
              "cp.nurture.explain_hours", "cp.nurture.explain_backoff", "cp.nurture.explain_backoff_generic",
              "cp.nurture.explain_default_off"):
        assert i18n.count(f'"{k}"') == 2, f"{k} 需 zh/en 各一条"
    hant = (REPO / "shared" / "copilot" / "i18n" / "cp-i18n-ext.zh_hant.js").read_text(encoding="utf-8")
    assert '"cp.nurture.explain_hd"' in hant
    # 数字不写死在文案里（由后端 explain 提供）
    zh = i18n.split('"cp.nurture.explain_limits"', 1)[1].split("\n", 1)[0]
    assert "{profs}" in zh and "15" not in zh
