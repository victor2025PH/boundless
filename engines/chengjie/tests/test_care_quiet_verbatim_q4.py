"""Q-4（#267 D）：关怀 verbatim 人工时刻默认照发 + 顺延错峰 + 时区依据 + 排期预判。

验收（docs/指令_Q-4）：
- 01:20 verbatim → 按时发（defer_until == now，不被安静时段顺延；投递队列也不二次顺延）
- 两条同顺延（quiet_policy=defer）→ 间隔 ≥ 5 min
- quiet_check_for_due：落在安静时段回 in_quiet + 两个本地时刻
- add_verbatim 后缀 / verbatim_quiet_policy 判别
- _resolve_clock：客户钟 → 人设/账号回落 → 服务器
"""
from datetime import datetime

from src.contacts.care_dispatcher import (
    CareDispatcher, QUIET_STAGGER_MAX_SEC, QUIET_STAGGER_SLOT_SEC, quiet_stagger_offset,
)
from src.contacts.care_schedule import (
    CareScheduleStore, VERBATIM_QUIET_DEFER_SUFFIX, is_verbatim_care, verbatim_quiet_policy,
)

LATE = datetime(2026, 6, 17, 1, 20, 0).timestamp()  # 01:20 服务器本地（安静窗 23→8 内）


class _AI:
    def __init__(self, reply="x"):
        self.reply = reply
        self.prompts = []

    async def chat(self, prompt, **kw):
        self.prompts.append(prompt)
        return self.reply


def _sender(record):
    async def _send(channel, account_id, chat_name, reply, defer_until, reason, staleness, extra):
        record.append({"channel": channel, "chat_name": chat_name, "reply": reply,
                       "defer_until": defer_until, "reason": reason, "extra": extra})
        return 100 + len(record)
    return _send


def _store(*rows):
    """rows: (contact, text, quiet_policy)"""
    s = CareScheduleStore(":memory:")
    ids = []
    for ck, text, pol in rows:
        ids.append(s.add_verbatim(contact_key=ck, due_at=LATE - 60, text=text,
                                  platform="telegram", account_id="default", chat_key=ck,
                                  quiet_policy=pol))
    return s, ids


# ── store 标记 ────────────────────────────────────────────────────────────
def test_add_verbatim_default_keep_and_defer_suffix():
    s, (a, b) = _store(("u1", "晚安，明天见", "keep"), ("u2", "晚安，明天见", "defer"))
    ra, rb = s.get(a), s.get(b)
    assert is_verbatim_care(ra) and is_verbatim_care(rb)
    assert verbatim_quiet_policy(ra) == "keep"
    assert verbatim_quiet_policy(rb) == "defer"
    assert rb["topic_norm"].endswith(VERBATIM_QUIET_DEFER_SUFFIX)
    assert len(rb["topic_norm"]) <= 32  # 列宽口径不被后缀撑破
    # 非 verbatim 行 → ""（按普通规则）
    assert verbatim_quiet_policy({"topic_norm": "面试"}) == ""
    # 缺省参数 = keep
    c = s.add_verbatim(contact_key="u3", due_at=LATE, text="hi")
    assert verbatim_quiet_policy(s.get(c)) == "keep"


# ── 派发：01:20 verbatim 照发 ─────────────────────────────────────────────
async def test_verbatim_keep_dispatches_on_time_in_quiet_hours():
    s, (rid,) = _store(("u1", "晚安，明天见", "keep"))
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       quiet_start_hour=23, quiet_end_hour=8)
    assert await d.run_once(now=LATE) == 1
    assert len(rec) == 1
    assert rec[0]["defer_until"] == LATE          # 不顺延到 08:00
    assert rec[0]["extra"]["ignore_quiet"] is True  # 投递层也不得二次顺延
    assert rec[0]["extra"]["quiet_policy"] == "keep"
    assert s.count(status="sent") == 1


async def test_verbatim_defer_shifts_to_quiet_end():
    s, (rid,) = _store(("u1", "晚安", "defer"))
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       quiet_start_hour=23, quiet_end_hour=8)
    assert await d.run_once(now=LATE) == 1
    du = datetime.fromtimestamp(rec[0]["defer_until"])
    assert (du.day, du.hour) == (17, 8)
    assert rec[0]["extra"]["ignore_quiet"] is False
    assert rec[0]["extra"]["quiet_policy"] == "defer"


async def test_deliver_text_keep_does_not_shift_in_quiet_hours():
    """#265：改稿点「发出」对 keep 原文不得再顺延到 08:00。"""
    s, (rid,) = _store(("u1", "晚安 keep", "keep"))
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       quiet_start_hour=23, quiet_end_hour=8)
    item = s.get(rid)
    res = await d.deliver_text(item, "改过的晚安", now=LATE)
    assert res["ok"] and abs(res["defer_until"] - LATE) < 1
    assert rec[0]["defer_until"] == LATE
    assert rec[0]["extra"]["ignore_quiet"] is True
    assert rec[0]["extra"]["quiet_policy"] == "keep"


async def test_deliver_text_defer_still_shifts():
    s, (rid,) = _store(("u1", "晚安 defer", "defer"))
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       quiet_start_hour=23, quiet_end_hour=8)
    res = await d.deliver_text(s.get(rid), "改过的晚安", now=LATE)
    assert res["ok"]
    du = datetime.fromtimestamp(res["defer_until"])
    assert du.hour >= 8
    assert rec[0]["extra"]["ignore_quiet"] is False


async def test_two_deferred_rows_are_staggered_at_least_5min():
    s, ids = _store(("u1", "晚安 a", "defer"), ("u2", "晚安 b", "defer"))
    rec = []
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender(rec),
                       quiet_start_hour=23, quiet_end_hour=8)
    assert await d.run_once(now=LATE) == 2
    assert len(rec) == 2
    a, b = sorted(r["defer_until"] for r in rec)
    assert b - a >= QUIET_STAGGER_SLOT_SEC        # ≥ 5 min
    assert b - a <= QUIET_STAGGER_MAX_SEC         # ≤ 40 min
    for r in rec:
        assert datetime.fromtimestamp(r["defer_until"]).hour >= 8


def test_quiet_stagger_offset_slots_then_random():
    assert quiet_stagger_offset(0) == 0.0
    assert quiet_stagger_offset(1) == QUIET_STAGGER_SLOT_SEC
    assert quiet_stagger_offset(8) == QUIET_STAGGER_MAX_SEC
    for _ in range(20):
        assert 0.0 <= quiet_stagger_offset(9) <= QUIET_STAGGER_MAX_SEC


# ── 排期预判（关怀页弹选择） ──────────────────────────────────────────────
def test_quiet_check_for_due_reports_both_times():
    d = CareDispatcher(store=CareScheduleStore(":memory:"), ai_client=_AI(),
                       send_callback=_sender([]), quiet_start_hour=23, quiet_end_hour=8)
    item = {"contact_key": "u1", "platform": "telegram", "account_id": "default",
            "chat_key": "u1", "topic_norm": "verbatim:pre"}
    r = d.quiet_check_for_due(item, LATE)
    assert r["in_quiet"] is True
    assert r["due_local"] == "01:20" and r["shifted_local"] == "08:00"
    assert r["tz_basis"] == "server"
    day = datetime(2026, 6, 17, 14, 0, 0).timestamp()
    r2 = d.quiet_check_for_due(item, day)
    assert r2["in_quiet"] is False and r2["due_local"] == "14:00"


def test_quiet_window_info_and_set_quiet_window():
    d = CareDispatcher(store=CareScheduleStore(":memory:"), ai_client=_AI(),
                       send_callback=_sender([]), quiet_start_hour=23, quiet_end_hour=8)
    q = d.quiet_window_info()
    assert q["enabled"] is True and q["start_hour"] == 23 and q["end_hour"] == 8
    d.set_quiet_window(8, 8)
    assert d.quiet_window_info()["enabled"] is False
    d.set_quiet_window(99, 8)   # 非法 → 保持
    assert d.quiet_window_info()["start_hour"] == 8
    d.set_quiet_window(22, 7)
    q = d.quiet_window_info({"contact_key": "u1", "platform": "telegram"})
    assert (q["start_hour"], q["end_hour"], q["tz_basis"]) == (22, 7, "server")


# ── 时区依据：客户 → 人设/账号 → 服务器 ────────────────────────────────────
def test_resolve_clock_prefers_customer_then_fallback_basis():
    from src.companion.user_clock import UserClock
    cust = UserClock(tz_name="America/New_York", offset_hours=-4.0, source="profile",
                     confidence=0.9, country="US", city_slug="nyc", trust="replace")
    d = CareDispatcher(
        store=CareScheduleStore(":memory:"), ai_client=_AI(), send_callback=_sender([]),
        user_clock_provider=lambda it: cust if it.get("contact_key") == "ny" else None,
        tz_fallback_provider=lambda it: ({"tz_name": "Asia/Tokyo", "basis": "persona"}
                                         if it.get("account_id") == "acc-jp" else {}),
    )
    c1, b1 = d._resolve_clock({"contact_key": "ny", "account_id": "acc-jp"})
    assert b1 == "customer" and c1.tz_name == "America/New_York"
    c2, b2 = d._resolve_clock({"contact_key": "x", "account_id": "acc-jp"})
    assert b2 == "persona" and c2.tz_name == "Asia/Tokyo"
    c3, b3 = d._resolve_clock({"contact_key": "x", "account_id": "other"})
    assert c3 is None and b3 == ""


def test_projected_send_window_verbatim_keep_ignores_quiet():
    s, (k, df) = _store(("u1", "a", "keep"), ("u2", "b", "defer"))
    d = CareDispatcher(store=s, ai_client=_AI(), send_callback=_sender([]),
                       quiet_start_hour=23, quiet_end_hour=8)
    wk = d.projected_send_window(s.get(k), at=LATE)
    wd = d.projected_send_window(s.get(df), at=LATE)
    assert wk["quiet_policy"] == "keep" and not wk.get("quiet_shifted")
    assert abs(wk["eta_min"] - LATE) < 1
    assert wd["quiet_policy"] == "defer" and wd.get("quiet_shifted")
    assert datetime.fromtimestamp(wd["eta_min"]).hour >= 8


# ── 路由：排期弹选择 + 安静时段开关 ───────────────────────────────────────────
class _CM:
    def __init__(self):
        self.config = {"companion": {"proactive_care": {
            "enabled": True, "dry_run": False, "quiet_start_hour": 23, "quiet_end_hour": 8}}}
        self.config_path = ""
        self.flags = []

    def get_ai_config(self):
        return {"ai_name": "小雅"}

    def set_overlay_flag(self, path, value):
        self.flags.append((path, value))
        return True, ""


def _client():
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from src.web.routes.care_routes import register_care_routes
    app = FastAPI()
    store = CareScheduleStore(":memory:")
    disp = CareDispatcher(store=store, ai_client=_AI(), send_callback=_sender([]),
                          cfg_provider=lambda: {"enabled": True, "dry_run": False},
                          quiet_start_hour=23, quiet_end_hour=8)
    app.state.care_schedule_store = store
    app.state.care_engine = {"dispatcher": disp}
    cm = _CM()
    app.state.config_manager = cm

    def _auth(request: Request):
        return True
    register_care_routes(app, api_auth=_auth, config_manager=cm)
    return TestClient(app), store, disp, cm


def _base_payload(due):
    return {"contact_key": "telegram:acc:u1", "platform": "telegram", "account_id": "acc",
            "chat_key": "u1", "topic": "晚安，明天见", "text": "晚安，明天见",
            "mode": "verbatim", "due_at": due}


def test_route_schedule_verbatim_in_quiet_asks_then_accepts_policy():
    c, store, disp, cm = _client()
    due = datetime(2030, 1, 10, 1, 20, 0).timestamp()
    r = c.post("/api/care/schedule", json=_base_payload(due)).json()
    assert r["ok"] is False and r["reason"] == "in_quiet_hours"
    assert r["due_local"] == "01:20" and r["shifted_local"] == "08:00"
    assert r["tz_basis"] == "server" and r["shifted_at"] > due
    assert "01:20" in r["message"] and "08:00" in r["message"]
    assert store.count(status="pending") == 0          # 没入队
    # 选「仍按 01:20 发」
    k = c.post("/api/care/schedule", json={**_base_payload(due), "quiet_policy": "keep"}).json()
    assert k["ok"] is True and k["quiet_policy"] == "keep"
    assert verbatim_quiet_policy(store.get(k["id"])) == "keep"
    # 选「顺延到 08:00」
    d = c.post("/api/care/schedule", json={**_base_payload(due), "quiet_policy": "defer"}).json()
    assert d["ok"] is True and d["quiet_policy"] == "defer"
    assert verbatim_quiet_policy(store.get(d["id"])) == "defer"
    # 非法策略拒收
    bad = c.post("/api/care/schedule", json={**_base_payload(due), "quiet_policy": "x"}).json()
    assert bad["ok"] is False and bad["reason"] == "bad_quiet_policy"
    # 白天时刻：不问直接入队（默认 keep）
    day = c.post("/api/care/schedule",
                 json=_base_payload(datetime(2030, 1, 10, 14, 0, 0).timestamp())).json()
    assert day["ok"] is True and day["quiet_policy"] == "keep"


def test_route_plan_exposes_quiet_and_toggle_writes_overlay():
    c, store, disp, cm = _client()
    p = c.get("/api/care/plan").json()
    assert p["ok"] and p["quiet"]["enabled"] is True
    assert (p["quiet"]["start_hour"], p["quiet"]["end_hour"]) == (23, 8)
    off = c.post("/api/care/quiet", json={"enabled": False}).json()
    assert off["ok"] and off["quiet"]["enabled"] is False
    assert ("companion.proactive_care.quiet_start_hour", 8) in cm.flags
    assert ("companion.proactive_care.quiet_end_hour", 8) in cm.flags
    assert disp.quiet_window_info()["enabled"] is False    # 派发器即时生效
    on = c.post("/api/care/quiet", json={"enabled": True, "start_hour": 22, "end_hour": 7}).json()
    assert on["ok"] and (on["quiet"]["start_hour"], on["quiet"]["end_hour"]) == (22, 7)
    assert disp.quiet_window_info()["start_hour"] == 22
    bad = c.post("/api/care/quiet", json={"enabled": True, "start_hour": 9, "end_hour": 9}).json()
    assert bad["ok"] is False and bad["reason"] == "bad_hours"


def test_care_page_template_and_i18n_contract():
    from pathlib import Path
    from src.web.i18n_packs.care_page import EN, ZH
    tpl = (Path(__file__).resolve().parents[1] / "src" / "web" / "templates"
           / "care_schedule.html").read_text(encoding="utf-8")
    for needle in ("id=\"cs-quiet\"", "_csRenderQuiet(d)", "csQuietToggle(", "/api/care/quiet",
                   "in_quiet_hours", "_csQuietChoiceShow(", "quiet_policy", "cs_quiet_keep",
                   "cs_quiet_defer", "a.code==='waiting_due'&&!eng.dry_run"):
        assert needle in tpl, needle
    for k in ("cs_quiet_title", "cs_quiet_range", "cs_quiet_off", "cs_quiet_basis",
              "cs_quiet_basis_customer", "cs_quiet_basis_persona", "cs_quiet_basis_account",
              "cs_quiet_basis_server", "cs_quiet_keep", "cs_quiet_defer", "cs_quiet_choice_msg",
              "cs6_add_ok_verbatim_defer"):
        assert k in ZH and k in EN, k
    # 「出手」→「发送」
    for k in ("cs8_fwd_done", "cs8_eta", "cs8_eta_t", "cs8_state_forwarded"):
        assert "出手" not in ZH[k], k
        assert "发送" in ZH[k], k


# ── 投递队列不二次顺延 ──────────────────────────────────────────────────────
async def test_deferred_outbox_respects_ignore_quiet_extra():
    """派发器裁定「照发」的行进了队列，队列在 01:20 drain 时不得再按服务器钟顺延到 08:00。"""
    from src.integrations.shared.deferred_outbox import DeferredDispatcher, DeferredOutboxStore
    s = DeferredOutboxStore(":memory:")
    s.enqueue(platform="telegram", account_id="a", chat_key="keep", reply_text="晚安",
              defer_until=LATE - 10, now=LATE - 100,
              extra={"care": True, "verbatim": True, "ignore_quiet": True})
    s.enqueue(platform="telegram", account_id="a", chat_key="auto", reply_text="hi",
              defer_until=LATE - 10, now=LATE - 100, extra={"care": True})
    sent = []

    async def _send(account_id, chat_key, text):
        sent.append(chat_key)
        return True
    d = DeferredDispatcher(store=s, senders={"telegram": _send},
                           kill_switch_check=lambda p, a: (False, "", ""),
                           quiet_start_hour=23, quiet_end_hour=8, max_per_tick=5)
    await d.run_once(now=LATE)
    assert sent == ["keep"]                       # 人工时刻照发
    row = s.list_recent(status="pending")[0]      # 自动行仍被顺延
    assert row["chat_key"] == "auto" and row["reason"] == "quiet_hours"
    assert datetime.fromtimestamp(row["defer_until"]).hour >= 8
