# -*- coding: utf-8 -*-
"""用量页「一页两态」+ 坐席字符消耗归因接线门禁（2026-08-16）。

钉住的决策：
1. ``/workspace/usage`` 不再对非主管 307——主管看团队全量视图（字符额度区 +
   分坐席用量表），坐席/观察员看「我的用量」自视图（个人额度是切身信息）；
2. 三条坐席主动消耗链（手动翻译 / TTS 试听 / 语音发送真合成）在路由层接
   ``record_request_chars``/``record_named_chars`` 归因记账（静态钉，防将来被
   顺手删掉）；send-voice 的「复用试听产物」分支**不得记账**（tts-test 已计过，
   重复记＝双计），该决策注释必须存在；
3. 模板对未重启后端 fail-soft：新 API（/api/workspace/my-usage、
   /api/users/char-usage）404 时隐藏区块/中性空态，绝不把现网页面搞崩。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from starlette.testclient import TestClient

_REPO = Path(__file__).resolve().parents[1]
_ROUTES = _REPO / "src" / "web" / "routes"
_TPL = _REPO / "src" / "web" / "templates" / "workspace_usage.html"


# ── 一页两态：主管全量 / 坐席自视图 ──────────────────────────────────────────

def test_usage_page_supervisor_sees_team_view(auth_client):
    """master → 200 团队视图：有分坐席表/KPI 骨架，无自视图专属标记。"""
    r = auth_client.get("/workspace/usage", follow_redirects=False)
    assert r.status_code == 200
    html = r.text
    assert 'id="uq-agents"' in html, "主管视图应含分坐席用量表骨架"
    assert 'id="ug-cards"' in html, "主管视图应保留既有 KPI 卡骨架"
    assert 'id="uq-char"' in html, "主管视图应含字符额度区骨架"
    assert 'id="uq-self"' not in html, "主管视图不得渲染自视图专属区块"


@pytest.fixture()
def agent_client(app, config_dir):
    """已认证为 agent 角色的客户端（照 conftest viewer_client 模式：
    master 建 agent 号 → 登出 → agent 登录）。"""
    with TestClient(app, raise_server_exceptions=True) as c:
        c.headers.update({"Authorization": "Bearer test-token-123"})
        c.post("/login", data={"auth_token": "test-token-123"}, follow_redirects=True)
        c.post(
            "/users/create",
            data={"username": "uq_agent", "password": "agent123", "role": "agent"},
            follow_redirects=True,
        )
        c.get("/logout", follow_redirects=True)
        c.post(
            "/login",
            data={"username": "uq_agent", "password": "agent123"},
            follow_redirects=True,
        )
        yield c


def test_usage_page_agent_gets_self_view_not_redirect(agent_client):
    """agent → 200「我的用量」自视图（不再 307 弹回今日概览）。"""
    r = agent_client.get("/workspace/usage", follow_redirects=False)
    assert r.status_code == 200, "坐席应看到自视图，而不是被 307 弹走"
    html = r.text
    assert 'id="uq-self"' in html, "自视图应渲染「我的用量」卡"
    assert 'id="uq-agents"' not in html, "自视图不得渲染团队分坐席表"
    assert 'id="ug-cards"' not in html, "自视图不得渲染团队 KPI 区"


# ── 记账接线静态钉（防「顺手清理」断线）─────────────────────────────────────

def test_translate_route_records_agent_chars():
    src = (_ROUTES / "unified_inbox_translate_routes.py").read_text(encoding="utf-8")
    assert "record_request_chars" in src, "手动翻译主入口必须接坐席字符归因记账"
    assert '"translation"' in src, "翻译记账类目必须是 translation"


def test_voice_tts_test_records_agent_chars():
    src = (_ROUTES / "voice_routes.py").read_text(encoding="utf-8")
    assert ("record_request_chars" in src or "record_named_chars" in src), \
        "tts-test 必须接坐席字符归因记账（同步/后台 job 两路径）"
    assert '"tts"' in src, "试听记账类目必须是 tts"


def test_send_voice_records_agent_chars_and_skips_reuse():
    src = (_ROUTES / "unified_inbox_send_routes.py").read_text(encoding="utf-8")
    assert "record_request_chars" in src, "send-voice 真合成成功必须接坐席字符归因记账"
    assert '"tts"' in src, "语音发送记账类目必须是 tts"
    # 复用试听产物分支不得记账（tts-test 已计过），该决策注释必须留在代码里
    assert "复用" in src and ("不记账" in src or "已计过" in src), \
        "send-voice 复用试听分支的「不记账/已计过」决策注释被删——恢复注释或重新评估双计风险"


# ── 模板 fail-soft 静态钉（.py 未重启的中间态绝不搞崩现网页面）───────────────

def test_usage_template_two_state_and_failsoft():
    html = _TPL.read_text(encoding="utf-8")
    assert "usage_self_only" in html, "模板必须按 usage_self_only 分叉两态"
    assert "default(false)" in html, "变量未注入（.py 未重启中间态）必须回落主管旧视图"
    assert "/api/workspace/my-usage" in html, "自视图数据源接线丢失"
    assert "/api/users/char-usage" in html, "团队字符用量数据源接线丢失"
    assert "/api/workspace/quota" in html, "授权总池数据源接线丢失"
    assert ".catch(" in html, "新 API 404/异常必须 fail-soft（隐藏区块/空态），不得裸抛"
