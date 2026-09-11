# -*- coding: utf-8 -*-
"""运维群降噪 P1.3 / P2.1（2026-09-10）：卡片模板 v2 契约 + 「已处理 / 静音」闭环。

- 卡片 v2：首行「级别 · 类别」、末行「智聊 · 时间 · #事件」，无 ━━━ 分隔线、无「原始事件（仅排查用）」、
  无连续空行；巡检提醒卡带一条 /ops/act 处置链接，恢复通知不带，links=off 不带。
- 账本：mute 期内 decide()=HOLD；ack 后同指纹 HOLD、指纹变了照常提；resolve 把静音一起清掉。
- /ops/act 页：链接令牌一次性进页 → 动作令牌提交 → 账本落静音/认领；坏令牌 403/410。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.inbox.remind_ledger import FIRST, HOLD, REMIND, RemindLedger  # noqa: E402
from src.inbox.webhook_notifier import _build_card, _build_message, card_body_lines  # noqa: E402
from src.utils import ops_glance_token as ogt  # noqa: E402

BASE = "https://katie.example.cc"


@pytest.fixture(autouse=True)
def _secret():
    ogt._reset_for_tests()
    ogt.configure("unit-test-secret-0123456789")
    yield
    ogt._reset_for_tests()


def _card(etype, data, links="login", base=BASE):
    title, text = _build_message(etype, data)
    return _build_card(etype, data, title, text, base, links)


_REMIND_CARDS = [
    ("draft_backlog_alert", {"remind_key": "draft_backlog", "stale_count": 5, "oldest_hours": 712.6,
                             "min_age_hours": 24, "sla_uncovered": 5, "by_level": {"L1": 5},
                             "already_replied": 0, "reminder": True, "unchanged": True}),
    ("case_backlog_alert", {"remind_key": "case_backlog", "urgent_count": 0, "stale_count": 3,
                            "oldest_hours": 884.2, "by_source": {"ai_doubt": 3}, "min_age_hours": 4,
                            "media_stale_count": 0, "reminder": False}),
    ("unanswered_inbound_alert", {"remind_key": "unanswered_inbound", "count": 1, "oldest_hours": 30.4,
                                  "reminder": True, "unchanged": False,
                                  "samples": [{"platform": "messenger", "conversation_id": "messenger:1:x",
                                               "account_id": "msg_1", "age_hours": 30.4}]}),
    ("avatar_voice_alert", {"remind_key": "avatar_voice", "reachable": False, "models_loaded": False,
                            "url": "http://127.0.0.1:7852", "error": "", "hang": False,
                            "down_minutes": 30, "reminder": False, "rescue_broken": []}),
    ("lan_gpu_alert", {"remind_key": "lan_gpu:http://192.168.0.176:11434", "host": "192.168.0.176:11434",
                       "url": "http://192.168.0.176:11434", "error": "timeout", "down_minutes": 65,
                       "reminder": True, "unchanged": True}),
]


# ── 卡片 v2 契约 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("etype,data", _REMIND_CARDS)
def test_card_v2_shape_and_action_link(etype, data):
    card = _card(etype, data)
    lines = card.splitlines()
    assert re.match(r"^(🔴 严重|🟠 警告|🔵 提示|✅ 恢复|📊 日报|💰 业务) · \S+$", lines[0]), lines[0]
    assert re.match(rf"^智聊 · \d\d-\d\d \d\d:\d\d · #{etype}$", lines[-1]), lines[-1]
    assert "━━━" not in card and "原始事件" not in card and "仅排查用" not in card
    assert "\n\n" not in card, card
    assert "**" not in card and "`" not in card and "](/" not in card
    assert len(card_body_lines(card)) <= 12, card
    act = [ln for ln in lines if ln.startswith("🛠 处置")]
    assert len(act) == 1, card
    url = act[0].split(": ", 1)[1]
    assert url.startswith(BASE + "/ops/act?k=")
    qs = parse_qs(urlsplit(url).query)
    assert qs["k"] == [data["remind_key"]]
    from src.web.routes.ops_act_routes import link_target
    assert ogt.verify(qs["t"][0], link_target(data["remind_key"])) == (True, "ok")


def test_card_v2_no_action_link_for_recovery_off_links_or_missing_base():
    rec = _card("lan_gpu_alert", {"remind_key": "lan_gpu:x", "recovered": True, "host": "h", "url": "u"})
    assert "🛠" not in rec and rec.startswith("✅ 恢复 · 算力")
    off = _card("draft_backlog_alert", _REMIND_CARDS[0][1], links="off")
    assert "🛠" not in off and "http" not in off
    nobase = _card("draft_backlog_alert", _REMIND_CARDS[0][1], base="")
    assert "🛠" not in nobase
    plain = _card("host_alert", {"title": "真活探针失败｜vision", "message": "vision 连续 3 次失败。"})
    assert "🛠" not in plain and plain.splitlines()[-1].endswith("#host_alert")


def test_card_v2_unconfigured_secret_falls_back_to_login_link():
    ogt.configure("change-me-in-production")
    card = _card("draft_backlog_alert", _REMIND_CARDS[0][1])
    act = [ln for ln in card.splitlines() if ln.startswith("🛠 处置")][0]
    assert act.endswith(f"{BASE}/ops/act?k=draft_backlog"), act


# ── 账本：静音 / 认领 ────────────────────────────────────────────────────────

def test_ledger_mute_holds_then_resumes_and_resolve_clears():
    led = RemindLedger(None)
    t0 = 1_000_000.0
    assert led.decide("k", now=t0, after_sec=0, interval_sec=3600, fp="a") == FIRST
    led.mark_sent("k", now=t0, fp="a", summary="s")
    until = led.mute("k", 4, by="tester", now=t0 + 10)
    assert until == t0 + 10 + 4 * 3600
    assert led.decide("k", now=t0 + 2 * 3600, after_sec=0, interval_sec=3600, fp="b") == HOLD
    assert led.is_muted("k", now=t0 + 2 * 3600)
    assert led.decide("k", now=t0 + 5 * 3600, after_sec=0, interval_sec=3600, fp="b") == REMIND
    item = led.open_items(now=t0 + 3600)[0]
    assert item["muted_until"] == until and item["by"] == "tester"
    assert led.resolve("k") is True
    assert not led.is_muted("k", now=t0 + 3600)
    assert led.mute("k", 24 * 30, now=t0) <= t0 + 7 * 24 * 3600      # 上限 7 天


def test_ledger_ack_silences_same_fingerprint_only():
    led = RemindLedger(None)
    t0 = 2_000_000.0
    led.decide("d", now=t0, after_sec=0, interval_sec=3600, fp="five")
    led.mark_sent("d", now=t0, fp="five", summary="5 条")
    assert led.ack("d", by="ops", now=t0 + 5) == "acked"
    # 同指纹：过了正常间隔也不提
    assert led.decide("d", now=t0 + 48 * 3600, after_sec=0, interval_sec=3600, fp="five") == HOLD
    assert led.open_items(now=t0 + 10)[0]["acked"] is True
    # 内容变了（多了一条）→ 照常提醒
    assert led.decide("d", now=t0 + 48 * 3600, after_sec=0, interval_sec=3600, fp="six") == REMIND
    led.unmute("d")
    assert led.decide("d", now=t0 + 48 * 3600, after_sec=0, interval_sec=3600, fp="five") == REMIND


def test_ledger_ack_without_fingerprint_degrades_to_24h_mute():
    led = RemindLedger(None)
    t0 = 3_000_000.0
    led.decide("n", now=t0, after_sec=0, interval_sec=60)
    led.mark_sent("n", now=t0)
    assert led.ack("n", now=t0) == "muted"
    assert led.is_muted("n", now=t0 + 23 * 3600) and not led.is_muted("n", now=t0 + 25 * 3600)


def test_ledger_ack_recognizes_lan_gpu_meta_fp():
    led = RemindLedger(None)
    led.set_alerted("lan_gpu:http://h", True)
    led.set_meta("lan_gpu:http://h", "fp", "abc")
    assert led.ack("lan_gpu:http://h", now=1.0) == "acked"
    assert led.meta("lan_gpu:http://h", "acked_fp") == "abc"


# ── /ops/act 页 ────────────────────────────────────────────────────────────

@pytest.fixture
def client_and_ledger():
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates
    from fastapi.testclient import TestClient
    from src.web.routes.ops_act_routes import register_ops_act_routes

    led = RemindLedger(None)
    led.decide("draft_backlog", now=100.0, after_sec=0, interval_sec=60, fp="five")
    led.mark_sent("draft_backlog", now=100.0, fp="five", summary="待审草稿 5 条无人处理（最久 30 天）")
    app = FastAPI()
    app.state.health_watchdog = SimpleNamespace(_remind=led)
    templates = Jinja2Templates(directory=str(ROOT / "src" / "web" / "templates"))

    def _deny(request):
        raise RuntimeError("no session")

    register_ops_act_routes(app, templates=templates, page_auth=_deny)
    return TestClient(app), led


def _action_token(html: str) -> str:
    m = re.search(r'name="at" value="([^"]+)"', html)
    assert m, html
    return m.group(1)


def test_act_page_link_token_then_mute(client_and_ledger):
    client, led = client_and_ledger
    from src.web.routes.ops_act_routes import act_url
    url = act_url("http://testserver", "draft_backlog")
    path = url[len("http://testserver"):]
    r = client.get(path)
    assert r.status_code == 200
    assert "待审草稿 5 条无人处理" in r.text and "待审草稿积压" in r.text
    at = _action_token(r.text)
    assert client.get(path).status_code == 410                     # 链接令牌一次性
    r2 = client.post("/ops/act", data={"k": "draft_backlog", "action": "mute", "hours": "24", "at": at})
    assert r2.status_code == 200
    assert "已静音至" in r2.text
    assert led.is_muted("draft_backlog")
    assert led.meta("draft_backlog", "muted_by", "").startswith("卡片链接")
    # 动作令牌也是一次性
    r3 = client.post("/ops/act", data={"k": "draft_backlog", "action": "mute", "hours": "24", "at": at})
    assert r3.status_code == 403


def test_act_page_ack_and_bad_tokens(client_and_ledger):
    client, led = client_and_ledger
    from src.web.routes.ops_act_routes import act_url
    r = client.get(act_url("http://testserver", "draft_backlog")[len("http://testserver"):])
    at = _action_token(r.text)
    r2 = client.post("/ops/act", data={"k": "draft_backlog", "action": "ack", "at": at})
    assert r2.status_code == 200 and "已处理" in r2.text
    assert led.meta("draft_backlog", "acked_fp") == "five"
    assert client.get("/ops/act?k=draft_backlog&t=garbage").status_code == 403
    assert client.get("/ops/act?k=&t=").status_code == 403
    # 动作令牌绑定 key：换 key 提交无效
    r = client.get(act_url("http://testserver", "draft_backlog")[len("http://testserver"):])
    at = _action_token(r.text)
    assert client.post("/ops/act", data={"k": "case_backlog", "action": "ack", "at": at}).status_code == 403


def test_act_page_unavailable_without_watchdog():
    from fastapi import FastAPI
    from fastapi.templating import Jinja2Templates
    from fastapi.testclient import TestClient
    from src.web.routes.ops_act_routes import act_url, register_ops_act_routes

    app = FastAPI()
    templates = Jinja2Templates(directory=str(ROOT / "src" / "web" / "templates"))
    register_ops_act_routes(app, templates=templates)
    c = TestClient(app)
    r = c.get(act_url("http://testserver", "draft_backlog")[len("http://testserver"):])
    assert r.status_code == 503
