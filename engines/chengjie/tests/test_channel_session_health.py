# -*- coding: utf-8 -*-
"""渠道中心壳层「网页会话健康条」契约（P0）。

链路：messenger-web / whatsapp-baileys(Node) push 会话状态 →
``platform_session_health`` 登记表 → ``GET /api/workspace/channel-sessions``
（``summarize_channel_sessions`` 模块级纯函数整形，telegram/line 无外部 worker
恒空）→ ``workspace_channels.html`` 壳层 ``#chc-session-strip`` 健康条
（全健康=一行绿点；有掉线=黄/红条逐账号列出，messenger 行带「重新登录」按钮
直连既有 ``POST /api/admin/platform-sessions/relogin``，whatsapp 行指引去
聊天坐席账号面板扫码）。

三段契约：① 纯函数整形语义；② 模板静态断言（strip 容器 / __chcPlatformCaps
壳层契约名 / 全部请求走 window.apiFetch 零裸 fetch / onclick 顶层全局）；
③ i18n 新键 ZH/EN 齐平且被模板消费（防裸键上屏/死键）。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.integrations.platform_session_health import UNHEALTHY_STATUSES
from src.web.routes.unified_inbox_workspace_pages_routes import (
    register_workspace_pages_routes,
    summarize_channel_sessions,
)

_TPL = (Path(__file__).resolve().parents[1]
        / "src" / "web" / "templates" / "workspace_channels.html")

_NOW = 1_800_000_000.0


def _sess(status, *, ts=None, since=0.0, login_id=""):
    """按 PlatformSessionHealth._sessions 值的真实形状造条目。"""
    return {"status": status, "detail": "", "login_id": login_id,
            "ts": (_NOW - 60) if ts is None else ts, "changes": 1,
            "unhealthy_since": since, "last_remind_ts": 0.0}


def _dump(sessions):
    """按 PlatformSessionHealth.dump() 的真实形状造快照。"""
    unhealthy = [k for k, v in sessions.items()
                 if str(v.get("status")) in UNHEALTHY_STATUSES]
    return {"started_at": _NOW - 86400, "total_events": len(sessions),
            "by_status": {}, "sessions": sessions,
            "unhealthy": unhealthy, "unhealthy_count": len(unhealthy)}


# ── ① 纯函数：summarize_channel_sessions ────────────────────────────────────

def test_empty_or_malformed_dump_gives_empty_list():
    assert summarize_channel_sessions({}, "messenger") == []
    assert summarize_channel_sessions(None, "messenger") == []
    assert summarize_channel_sessions({"sessions": None}, "messenger") == []
    assert summarize_channel_sessions({"sessions": []}, "messenger") == []
    # 平台空串（防御口径：不整形任何行）
    d = _dump({"messenger:100": _sess("authorized")})
    assert summarize_channel_sessions(d, "") == []
    assert summarize_channel_sessions(d, None) == []


def test_platform_filter_and_account_id_parse():
    d = _dump({
        "messenger:100": _sess("authorized"),
        "whatsapp:200": _sess("authorized"),
        # account_id 可含冒号（_san 保留 ':'）——按首个冒号切平台
        "messenger:a@b:c": _sess("authorized"),
    })
    rows = summarize_channel_sessions(d, "messenger", now=_NOW)
    assert [r["account_id"] for r in rows] == ["100", "a@b:c"]  # sorted(key) 序
    assert all(r["key"].startswith("messenger:") for r in rows)
    rows_wa = summarize_channel_sessions(d, "whatsapp", now=_NOW)
    assert [r["account_id"] for r in rows_wa] == ["200"]
    # 无该平台记录 → 空（前端隐藏健康条）
    assert summarize_channel_sessions(d, "telegram", now=_NOW) == []


def test_unhealthy_flag_follows_status_enum():
    sessions = {
        "messenger:ok": _sess("authorized"),
        "messenger:s1": _sess("needs_login", since=_NOW - 10),
        "messenger:s2": _sess("expired", since=_NOW - 10),
        "messenger:s3": _sess("logged_out", since=_NOW - 10),
        "messenger:s4": _sess("failed", since=_NOW - 10),
    }
    rows = summarize_channel_sessions(_dump(sessions), "messenger", now=_NOW)
    by_acct = {r["account_id"]: r for r in rows}
    assert by_acct["ok"]["unhealthy"] is False
    for acct in ("s1", "s2", "s3", "s4"):
        assert by_acct[acct]["unhealthy"] is True
        assert by_acct[acct]["status"] in UNHEALTHY_STATUSES


def test_age_sec_semantics():
    sessions = {
        # 不健康：age = 掉线时长（unhealthy_since 起算，而非最近上报 ts）
        "messenger:down": _sess("needs_login", ts=_NOW - 10, since=_NOW - 300),
        # 不健康但缺 unhealthy_since（脏数据）→ 退回 ts
        "messenger:down2": _sess("expired", ts=_NOW - 120, since=0.0),
        # 健康：age = 距最近一次上报
        "messenger:ok": _sess("authorized", ts=_NOW - 42),
        # 时间戳缺失 → 0（不出负值/不抛）
        "messenger:zero": _sess("authorized", ts=0.0),
        # 未来时间戳（时钟漂移）→ 钳到 0
        "messenger:future": _sess("authorized", ts=_NOW + 999),
    }
    rows = summarize_channel_sessions(_dump(sessions), "messenger", now=_NOW)
    by_acct = {r["account_id"]: r for r in rows}
    assert by_acct["down"]["age_sec"] == 300
    assert by_acct["down2"]["age_sec"] == 120
    assert by_acct["ok"]["age_sec"] == 42
    assert by_acct["zero"]["age_sec"] == 0
    assert by_acct["future"]["age_sec"] == 0


def test_row_structure_keys_complete():
    d = _dump({"whatsapp:300": _sess("logged_out", since=_NOW - 5,
                                     login_id="u_300")})
    rows = summarize_channel_sessions(d, "whatsapp", now=_NOW)
    assert len(rows) == 1
    row = rows[0]
    # 端点契约字段齐全（login_id 为 relogin 识别附加字段）
    assert {"key", "account_id", "status", "unhealthy", "age_sec"} <= set(row)
    assert row["key"] == "whatsapp:300"
    assert row["account_id"] == "300"
    assert row["login_id"] == "u_300"
    assert isinstance(row["unhealthy"], bool)
    assert isinstance(row["age_sec"], int)


def test_dirty_entries_skipped_without_raise():
    d = _dump({"messenger:good": _sess("authorized")})
    d["sessions"]["messenger:bad"] = "not-a-dict"     # 值非 dict → 跳过
    d["sessions"]["nocolonkey"] = _sess("authorized")  # 键无冒号 → 跳过
    d["sessions"]["messenger:garbagets"] = {
        "status": "authorized", "ts": "garbage"}       # ts 非数值 → age 0 不抛
    rows = summarize_channel_sessions(d, "messenger", now=_NOW)
    assert [r["account_id"] for r in rows] == ["garbagets", "good"]
    assert {r["age_sec"] for r in rows if r["account_id"] == "garbagets"} == {0}


# ── ①b 端点：/api/workspace/channel-sessions ────────────────────────────────

@pytest.fixture()
def _fresh_health(monkeypatch):
    """每例独立的健康登记单例（防跨测试污染）。"""
    import src.integrations.platform_session_health as psh
    monkeypatch.setattr(psh, "_SINGLETON", None, raising=False)
    yield


def _client():
    from fastapi.responses import HTMLResponse

    class _Tpl:
        def TemplateResponse(self, request, name, ctx):  # pragma: no cover
            return HTMLResponse("")

    def _auth() -> None:  # 注意：lambda request 会被 FastAPI 当 query 参数
        return None

    app = FastAPI()
    register_workspace_pages_routes(
        app, page_auth=_auth, templates=_Tpl(), config_manager=None)
    return TestClient(app)


def test_endpoint_shape_platform_filter_and_relogin_flag(_fresh_health):
    from src.integrations.platform_session_health import (
        get_platform_session_health,
    )
    s = get_platform_session_health()
    s.record("messenger", "100", "authorized")
    s.record("messenger", "200", "needs_login", detail="cookies expired",
             login_id="u_200")
    s.record("whatsapp", "300", "logged_out")
    c = _client()

    d = c.get("/api/workspace/channel-sessions",
              params={"platform": "Messenger"}).json()  # 大小写归一
    assert d["ok"] is True and d["platform"] == "messenger"
    assert d["relogin_supported"] is True  # relogin 端点当前仅支持 messenger
    assert [r["account_id"] for r in d["sessions"]] == ["100", "200"]
    by_acct = {r["account_id"]: r for r in d["sessions"]}
    assert by_acct["100"]["unhealthy"] is False
    assert by_acct["200"]["unhealthy"] is True
    assert by_acct["200"]["login_id"] == "u_200"
    assert by_acct["200"]["age_sec"] >= 0

    d2 = c.get("/api/workspace/channel-sessions",
               params={"platform": "whatsapp"}).json()
    assert d2["ok"] is True and d2["relogin_supported"] is False
    assert [r["account_id"] for r in d2["sessions"]] == ["300"]

    # telegram 无外部 worker → 空列表（前端隐藏健康条）
    d3 = c.get("/api/workspace/channel-sessions",
               params={"platform": "telegram"}).json()
    assert d3["ok"] is True and d3["sessions"] == []


def test_endpoint_observability_bypass_never_500(monkeypatch, _fresh_health):
    import src.integrations.platform_session_health as psh

    def _boom():
        raise RuntimeError("boom")

    monkeypatch.setattr(psh, "get_platform_session_health", _boom)
    c = _client()
    r = c.get("/api/workspace/channel-sessions",
              params={"platform": "messenger"})
    assert r.status_code == 200
    assert r.json() == {"ok": False, "platform": "messenger",
                        "sessions": [], "relogin_supported": False}


# ── ② 模板静态断言 ───────────────────────────────────────────────────────────

_BARE_FETCH_RX = re.compile(r"(?<![A-Za-z0-9_$.])fetch\s*\(")


def test_template_session_strip_and_caps_contract():
    txt = _TPL.read_text(encoding="utf-8")
    # 健康条容器唯一（unique-id 门禁同口径）
    assert txt.count('id="chc-session-strip"') == 1
    # 壳层契约：各渠道正文消费的平台封顶表全局，名字一字不差
    assert "window.__chcPlatformCaps" in txt
    assert "platform_mode_caps" in txt and "platform_draft_skips" in txt
    # 数据端点 + 既有 relogin 端点接线
    assert "/api/workspace/channel-sessions" in txt
    assert "/api/admin/platform-sessions/relogin" in txt


def test_template_all_requests_via_api_fetch_no_bare_fetch():
    txt = _TPL.read_text(encoding="utf-8")
    assert not _BARE_FETCH_RX.search(txt), (
        "workspace_channels.html 出现裸 fetch——必须走 window.apiFetch"
        "（tests/test_template_bare_fetch_ratchet.py 同口径）")
    # 拉取 + 重登两处都走统一层
    assert txt.count("window.apiFetch(") >= 2
    assert re.search(
        r"window\.apiFetch\('/api/admin/platform-sessions/relogin'", txt), \
        "relogin 调用必须经 window.apiFetch"


def test_template_onclick_targets_are_top_level_globals():
    """动态行 onclick 目标必须是顶层全局函数（哑按钮门禁约定）。"""
    txt = _TPL.read_text(encoding="utf-8")
    assert 'onclick="chcSessRelogin(this)"' in txt
    assert re.search(r"^async function chcSessRelogin\(", txt, re.M), (
        "chcSessRelogin 必须定义在 <script> 顶层（IIFE 内定义点击即 ReferenceError）")
    assert re.search(r"^async function chcLoadSessions\(", txt, re.M)


# ── ③ i18n：新键 ZH/EN 齐平且被模板消费 ─────────────────────────────────────

_NEW_KEYS = (
    "chc_cap_capped", "chc_cap_skipped",
    "chc_sess_ok_line", "chc_sess_bad_title", "chc_sess_down_for",
    "chc_sess_relogin_btn", "chc_sess_relogin_ok", "chc_sess_relogin_fail",
    "chc_sess_wa_hint", "chc_sess_wa_link",
    "chc_sess_age_sec", "chc_sess_age_min", "chc_sess_age_hour",
    "chc_sess_age_day",
    # 账号入口轨（与 test_channel_acct_rail 同键；此处只守「模板已消费」）
    "chc_acct_title", "chc_acct_cta", "chc_acct_inbox",
    "chc_acct_badge_seat", "chc_acct_badge_main_ok",
    "chc_acct_badge_main_connecting", "chc_acct_badge_main_off",
    "chc_acct_body_tg", "chc_acct_body_line", "chc_acct_body_wa", "chc_acct_body_msg",
    "chc_acct_ops_sum", "chc_acct_ops_1", "chc_acct_ops_2", "chc_acct_ops_3", "chc_acct_ops_4",
)


def test_i18n_new_keys_bilingual_parity():
    from src.web.i18n_packs import channel_center as cc
    assert set(cc.ZH.keys()) == set(cc.EN.keys()), "pack ZH/EN 键集必须一致"
    ph = re.compile(r"\{[a-z_]+\}")
    for k in _NEW_KEYS:
        assert cc.ZH.get(k, "").strip(), f"ZH 缺键/空值：{k}"
        assert cc.EN.get(k, "").strip(), f"EN 缺键/空值：{k}"
        # 占位符两边齐平（{n}/{bad}/{total}/{t}/{mode}）
        assert set(ph.findall(cc.ZH[k])) == set(ph.findall(cc.EN[k])), (
            f"占位符 ZH/EN 不齐：{k}")


def test_i18n_keys_used_by_template_and_no_dead_keys():
    from src.web.i18n_packs import channel_center as cc
    txt = _TPL.read_text(encoding="utf-8")
    used = set(re.findall(r"'(chc_(?:sess|cap|acct)_[a-z0-9_]+)'", txt))
    assert used, "模板未消费任何 chc_sess_/chc_cap_/chc_acct_ 键（接线丢失？）"
    missing = used - set(cc.ZH)
    assert not missing, f"模板引用了 pack 缺失的键（裸键上屏）：{sorted(missing)}"
    dead = set(_NEW_KEYS) - used
    assert not dead, f"pack 新键未被模板消费（死键）：{sorted(dead)}"
