# -*- coding: utf-8 -*-
"""M-5 A（#217 / D-M6，2026-09-06）：「转化成交」类目用户版隐藏 + 无产品禁报价
+ 目标首次真发强制预览。

48JB6U / ETR3XA 实录：「工作目标 → 转化成交」四张预置里两张是厂商卖智聊软件的
销售剧本、两张是无产品字段的空壳；1.0.75 冲刺引擎真发后，用户一键挂链就会让
AI 向陪聊客户推销智聊（#175「黄金市场」＝无产品可推时的编造）。

钉住：
- 模板注册表 ``list_templates(client_hide=True)`` 剔整个 ``kind=conversion``；
- ``/api/goals/templates``：client 形态不见四张，partner / internal / 开发者模式
  照旧；client 建该类目 403，custom 照建；存量目标视图带 ``template_retired``；
- ``product_guard``：无产品目标出站含报价 / 开户 / 支付 / 注册链接 → 拦
  （``goal_no_product``）；绑了产品 / custom note 明写价格 → 放行；闲聊不误伤；
- 首拍进 L1 草稿（``risk_reasons=[goal_first_send]``），第二拍真发；运营手改
  与非 goal 行原样透传；
- cp-goal 卡片渲染 ``template_retired`` + i18n zh/en 键齐。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from src.companion.goals import product_guard as pg
from src.companion.goals.store import get_goal_store, reset_goal_store
from src.companion.goals.templates import (
    CLIENT_HIDDEN_KINDS,
    TEMPLATES,
    client_hidden_template_ids,
    is_client_hidden_template,
    list_templates,
)
from src.web.routes.goal_routes import register_goal_routes

REPO = Path(__file__).resolve().parents[1]
CONV = "telegram:a1:100"
CONVERSION_IDS = {"conversion_unlock", "conversion_subscribe",
                  "acquire_and_convert", "retention_expand"}


@pytest.fixture(autouse=True)
def _hermetic_goal_env(monkeypatch):
    import src.integrations.protocol_bridge as pb
    import src.utils.companion_context as cc

    monkeypatch.setattr(cc, "_REL_PROVIDERS", {})
    monkeypatch.setattr(pb, "_inbox_store_getter", None)
    reset_goal_store()
    yield
    reset_goal_store()


def _build_client(flavor="client", developer_mode=False):
    goals = {"enabled": True, "db_path": ":memory:"}
    cfg = {"companion": {"goals": goals}, "ui_visibility": {"flavor": flavor}}
    sess = {"role": "", "user": "tester"}
    if developer_mode:
        sess.update({"developer_mode": True, "dev_unlocked": True})
    app = FastAPI()

    def auth_dep(request: Request) -> None:
        request.scope["session"] = dict(sess)

    register_goal_routes(
        app, auth_dep, SimpleNamespace(config=cfg, config_path=None))
    return TestClient(app), sess


# ── 模板注册表 ──────────────────────────────────────────────────────────────

def test_hidden_kind_is_exactly_conversion_category():
    assert CLIENT_HIDDEN_KINDS == ("conversion",)
    assert set(client_hidden_template_ids()) == CONVERSION_IDS
    for tid in CONVERSION_IDS:
        assert is_client_hidden_template(tid)
    for tid in ("relationship_stage", "relationship_intimacy",
                "engagement_reactivate", "profile_discovery", "custom"):
        assert not is_client_hidden_template(tid)
    assert not is_client_hidden_template("nope")


def test_list_templates_client_hide_only_drops_conversion():
    full = {t["id"] for t in list_templates()}
    assert full == set(TEMPLATES)
    shown = {t["id"] for t in list_templates(client_hide=True)}
    assert shown == full - CONVERSION_IDS
    assert "custom" in shown and "relationship_stage" in shown
    # 数据未删：注册表本体原样
    assert set(TEMPLATES) == full


# ── /api/goals/templates × 形态 ─────────────────────────────────────────────

def test_templates_endpoint_client_hides_four():
    client, _ = _build_client("client")
    d = client.get("/api/goals/templates").json()
    ids = {t["id"] for t in d["templates"]}
    assert ids.isdisjoint(CONVERSION_IDS)
    assert len(ids) == 5 and "custom" in ids
    # 分组渲染靠 kind：client 下没有任何 conversion kind 落到前端
    assert all(t["kind"] != "conversion" for t in d["templates"])


@pytest.mark.parametrize("flavor", ["partner", "internal"])
def test_templates_endpoint_partner_internal_keep_all(flavor):
    client, _ = _build_client(flavor)
    ids = {t["id"] for t in client.get("/api/goals/templates").json()["templates"]}
    assert ids == set(TEMPLATES)


def test_templates_endpoint_client_developer_mode_restores():
    client, _ = _build_client("client", developer_mode=True)
    ids = {t["id"] for t in client.get("/api/goals/templates").json()["templates"]}
    assert ids == set(TEMPLATES)


# ── 建目标闸 ────────────────────────────────────────────────────────────────

def test_client_cannot_create_conversion_goal_but_custom_ok():
    client, _ = _build_client("client")
    r = client.post("/api/goals", json={"template": "conversion_unlock",
                                         "conversation_id": CONV})
    assert r.status_code == 403
    assert "自定义目标" in r.json()["detail"]
    r2 = client.post("/api/goals", json={"template": "custom",
                                          "conversation_id": CONV,
                                          "params": {"note": "聊聊他的工作"}})
    assert r2.status_code == 200 and r2.json()["ok"] is True
    assert "template_retired" not in r2.json()["goal"]
    # 批量口同闸
    r3 = client.post("/api/goals/batch", json={
        "template": "acquire_and_convert", "targets": [{"conversation_id": CONV}]})
    assert r3.status_code == 403


def test_internal_creates_conversion_goal_and_no_retired_flag():
    client, _ = _build_client("internal")
    r = client.post("/api/goals", json={"template": "conversion_unlock",
                                         "conversation_id": CONV})
    assert r.status_code == 200
    assert "template_retired" not in r.json()["goal"]


def test_existing_conversion_goal_flagged_retired_only_for_client():
    # 存量目标（内部机建的，或升级前建的）：不删，client 视图标 retired
    store = get_goal_store(":memory:")
    g = store.create_goal(conversation_id=CONV, platform="telegram",
                          account_id="a1", chat_key="100",
                          template="acquire_and_convert", autonomy="auto",
                          deadline_days=10)
    assert g is not None
    client, _ = _build_client("client")
    d = client.get(f"/api/goals/for-conversation?conversation_id={CONV}").json()
    assert d["goal"]["template"] == "acquire_and_convert"
    assert d["goal"]["template_retired"] is True
    lst = client.get("/api/goals").json()["goals"]
    assert lst and lst[0]["template_retired"] is True
    det = client.get(f"/api/goals/{g['goal_id']}").json()["goal"]
    assert det["template_retired"] is True
    # partner 看不到该键；relationship 目标即便 client 也不标
    client2, _ = _build_client("partner")
    d2 = client2.get(f"/api/goals/for-conversation?conversation_id={CONV}").json()
    assert "template_retired" not in d2["goal"]


# ── 无产品禁报价（纯函数） ──────────────────────────────────────────────────

def _goal(template="custom", params=None, gid="g1"):
    return {"goal_id": gid, "template": template, "params": params or {},
            "conversation_id": CONV, "platform": "telegram",
            "account_id": "a1", "chat_key": "100"}


class TestProductBinding:
    def test_param_keys_bind(self):
        assert pg.goal_product_binding(_goal("conversion_unlock",
                                             {"item_id": "bazi"}))["bound"]
        assert pg.goal_product_binding(_goal("conversion_subscribe",
                                             {"tier": "vip"}))["bound"]
        assert pg.goal_product_binding(_goal("acquire_and_convert",
                                             {"product_id": "chatx_pro"}))["bound"]

    def test_empty_params_unbound(self):
        assert not pg.goal_product_binding(_goal("conversion_unlock", {}))["bound"]
        assert not pg.goal_product_binding(
            _goal("conversion_unlock", {"item_id": "", "item_label": ""}))["bound"]
        assert not pg.goal_product_binding(_goal("acquire_and_convert", {}))["bound"]

    def test_catalog_only_binds_catalog_templates(self):
        assert pg.goal_product_binding(_goal("acquire_and_convert", {}),
                                       catalog_has_products=True)["source"] == "catalog"
        # 非目录模板不吃目录信号
        assert not pg.goal_product_binding(_goal("conversion_unlock", {}),
                                           catalog_has_products=True)["bound"]

    def test_custom_note_with_price_binds_but_direction_only_does_not(self):
        assert pg.goal_product_binding(
            _goal("custom", {"note": "让他买我的 $50 课程"}))["source"] == "note"
        assert pg.goal_product_binding(
            _goal("custom", {"note": "推销一下我的手作项链"}))["bound"]
        assert not pg.goal_product_binding(
            _goal("custom", {"note": "获取客户职业和年龄"}))["bound"]
        assert not pg.goal_product_binding(_goal("custom", {}))["bound"]

    def test_never_raises(self):
        assert pg.goal_product_binding(None) == {"bound": False, "source": ""}
        assert not pg.goal_product_binding({"params": "junk"})["bound"]


class TestSalesTalk:
    @pytest.mark.parametrize("text", [
        "这个套餐报价是 299，你要的话我发你付款链接",
        "现在开通会员只要 ¥99，扫码支付就行",
        "The price is $9.99/mo — you can sign up here and get started today.",
        "It costs only $20, I'll send you the payment link.",
        "My assistant will reach out with the account details and a quick setup guide.",
        "先开个账户，我把开户资料发你",
        "Just open an account on the official site and place an order.",
        "支持 PayPal 或者 USDT 转账",
        "现在下单还有优惠价哦",
    ])
    def test_hits(self, text):
        assert pg.find_sales_talk(text), text

    @pytest.mark.parametrize("text", [
        "今天天气不错，你吃饭了吗？",
        "你之前说的面试怎么样啦？",
        "How was your run this morning? Still raining over there?",
        "你觉得这个价格的房子贵吗，我看了好久",
        "I paid attention to what you said about your mom, how is she?",
        "周末想去看场电影，你有推荐吗",
    ])
    def test_no_false_positive_on_chat(self, text):
        assert pg.find_sales_talk(text) == [], text

    def test_guard_blocks_only_unbound(self):
        text = "现在开通会员只要 ¥99，扫码支付就行"
        v = pg.guard_goal_outbound(_goal("conversion_subscribe", {}), text)
        assert v["ok"] is False and v["reason"] == "goal_no_product" and v["hits"]
        v2 = pg.guard_goal_outbound(
            _goal("conversion_subscribe", {"tier": "vip"}), text)
        assert v2["ok"] is True and v2["bound"] is True
        v3 = pg.guard_goal_outbound(_goal("custom", {"note": "聊聊他的工作"}),
                                    "你之前说的项目进展如何？")
        assert v3["ok"] is True and v3["bound"] is False


# ── 首次真发预览（store 事件口径） ──────────────────────────────────────────

def test_first_send_pending_flips_on_sent_or_preview_events():
    store = get_goal_store(":memory:")
    g = store.create_goal(conversation_id=CONV, platform="telegram",
                          account_id="a1", chat_key="100", template="custom",
                          params={"note": "x"}, autonomy="auto", deadline_days=3)
    gid = g["goal_id"]
    assert pg.first_send_pending(store, gid) is True
    store.add_event(gid, "care_sent", "冲刺拍 p0 已发出")
    assert pg.first_send_pending(store, gid) is False
    g2 = store.create_goal(conversation_id="telegram:a1:200", platform="telegram",
                           account_id="a1", chat_key="200", template="custom",
                           params={"note": "y"}, autonomy="auto", deadline_days=3)
    store.add_event(g2["goal_id"], pg.FIRST_SEND_EVENT, "已进预览")
    assert pg.first_send_pending(store, g2["goal_id"]) is False
    assert pg.first_send_pending(None, gid) is False
    assert pg.first_send_pending(store, "") is False


def test_build_first_send_draft_shape():
    d = pg.build_first_send_draft(_goal("custom", {"note": "x"}, gid="G9"),
                                  "你好呀", peer_text="hi", care_id=7)
    assert d["source_kind"] == "inbox" and d["source_id"] == "goal_first:G9"
    assert d["autopilot_level"] == "L1" and d["status"] == "pending"
    assert d["risk_reasons"] == ["goal_first_send"]
    assert d["conversation_id"] == CONV and d["platform"] == "telegram"
    assert d["account_id"] == "a1" and d["chat_key"] == "100"
    assert d["draft_text"] == "你好呀" and d["peer_text"] == "hi"
    assert d["trace_id"] == "goal_first_send:G9:care7"


# ── wrap_care_send 集成（假 care_store / 假 inbox / 真 goal store） ─────────

class _FakeCare:
    def __init__(self, rows):
        self.rows = dict(rows)
        self.skipped = []

    def get(self, sid):
        return self.rows.get(int(sid))

    def mark_skipped(self, sid, *, note=""):
        self.skipped.append((int(sid), note))
        return True


class _FakeInbox:
    def __init__(self):
        self.drafts = []

    def upsert_draft(self, d):
        self.drafts.append(dict(d))
        return f"inbox:{d['source_id']}"

    def list_recent_messages(self, conv, limit=6):
        return [{"direction": "in", "text": "在吗", "ts": 1.0}]


def _run(coro):
    return asyncio.run(coro)


def _wrapped(care_rows, inbox=None, catalog=False):
    store = get_goal_store(":memory:")
    calls = []

    async def send_cb(channel, account_id, chat_name, reply, defer_until,
                      reason, staleness_sec, extra):
        calls.append(reply)
        return 42

    care = _FakeCare(care_rows)
    fn = pg.wrap_care_send(
        send_cb, care_store=care, goal_store_getter=lambda: store,
        inbox_store_getter=(lambda: inbox), catalog_probe=lambda: catalog)
    return fn, store, care, calls


def _send(fn, reply, care_id, extra_more=None):
    extra = {"care": True, "care_id": care_id, "contact_key": CONV,
             "topic": "t", "crisis_care": False, "verbatim": False}
    extra.update(extra_more or {})
    return _run(fn("telegram", "a1", "100", reply, 0.0, "care:t", 86400.0, extra))


def test_wrapper_passes_through_non_goal_rows():
    fn, _store, care, calls = _wrapped({1: {"id": 1, "topic_norm": "interview"}})
    assert _send(fn, "面试怎么样啦", 1) == 42
    assert calls == ["面试怎么样啦"] and care.skipped == []


def test_wrapper_blocks_no_product_sales_talk():
    store = get_goal_store(":memory:")
    g = store.create_goal(conversation_id=CONV, platform="telegram",
                          account_id="a1", chat_key="100",
                          template="conversion_unlock", params={},
                          autonomy="auto", deadline_days=1)
    gid = g["goal_id"]
    inbox = _FakeInbox()
    fn, _, care, calls = _wrapped(
        {5: {"id": 5, "topic_norm": f"goal:{gid}:p1"}}, inbox=inbox)
    rid = _send(fn, "现在开通只要 ¥99，我发你付款链接", 5)
    assert rid == 0 and calls == []
    assert care.skipped == [(5, "goal_no_product")]
    kinds = [e["kind"] for e in store.list_events(gid)]
    assert "goal_no_product" in kinds
    assert inbox.drafts == []          # 拦下的话术绝不进草稿


def test_wrapper_first_send_goes_to_l1_then_live():
    store = get_goal_store(":memory:")
    g = store.create_goal(conversation_id=CONV, platform="telegram",
                          account_id="a1", chat_key="100", template="custom",
                          params={"note": "聊聊他的工作"}, autonomy="auto",
                          deadline_days=3)
    gid = g["goal_id"]
    inbox = _FakeInbox()
    fn, _, care, calls = _wrapped(
        {7: {"id": 7, "topic_norm": f"goal:{gid}:p0"},
         8: {"id": 8, "topic_norm": f"goal:{gid}:p1"}}, inbox=inbox)
    # 首拍 → L1 草稿，不真发
    assert _send(fn, "你之前说的项目怎么样啦？", 7) == 0
    assert calls == []
    assert len(inbox.drafts) == 1
    d = inbox.drafts[0]
    assert d["autopilot_level"] == "L1" and d["risk_reasons"] == ["goal_first_send"]
    assert d["draft_text"] == "你之前说的项目怎么样啦？" and d["peer_text"] == "在吗"
    assert care.skipped == [(7, "preview:draft:inbox:goal_first:" + gid)]
    kinds = [e["kind"] for e in store.list_events(gid)]
    assert pg.FIRST_SEND_EVENT in kinds
    # 第二拍 → 真发
    assert _send(fn, "周末有安排吗？", 8) == 42
    assert calls == ["周末有安排吗？"]
    assert len(inbox.drafts) == 1


def test_wrapper_existing_goal_with_sent_history_not_previewed():
    store = get_goal_store(":memory:")
    g = store.create_goal(conversation_id=CONV, platform="telegram",
                          account_id="a1", chat_key="100", template="custom",
                          params={"note": "x"}, autonomy="auto", deadline_days=3)
    gid = g["goal_id"]
    store.add_event(gid, "care_sent", "冲刺拍 p0 已发出")   # 1.0.75 存量已真发
    inbox = _FakeInbox()
    fn, _, care, calls = _wrapped(
        {9: {"id": 9, "topic_norm": f"goal:{gid}:p1"}}, inbox=inbox)
    assert _send(fn, "今天过得怎么样？", 9) == 42
    assert calls == ["今天过得怎么样？"] and inbox.drafts == [] and care.skipped == []


def test_wrapper_no_inbox_store_falls_back_to_send():
    store = get_goal_store(":memory:")
    g = store.create_goal(conversation_id=CONV, platform="telegram",
                          account_id="a1", chat_key="100", template="custom",
                          params={"note": "x"}, autonomy="auto", deadline_days=3)
    fn, _, care, calls = _wrapped(
        {3: {"id": 3, "topic_norm": f"goal:{g['goal_id']}:p0"}}, inbox=None)
    assert _send(fn, "嗨，最近好吗", 3) == 42
    assert calls == ["嗨，最近好吗"] and care.skipped == []


def test_wrapper_manual_rewrite_passes_through_even_for_goal_row():
    store = get_goal_store(":memory:")
    g = store.create_goal(conversation_id=CONV, platform="telegram",
                          account_id="a1", chat_key="100",
                          template="conversion_unlock", params={},
                          autonomy="auto", deadline_days=1)
    fn, _, care, calls = _wrapped(
        {4: {"id": 4, "topic_norm": f"goal:{g['goal_id']}:p0"}}, inbox=_FakeInbox())
    # 运营预览上手改的终稿：人写的话人负责，不过守卫、不进预览
    assert _send(fn, "报价 299，付款链接稍后发", 4, {"manual_rewrite": True}) == 42
    assert calls and care.skipped == []


# ── 前端 / i18n ─────────────────────────────────────────────────────────────

def test_cp_goal_renders_retired_flag_and_i18n_keys_exist():
    js = (REPO / "shared" / "copilot" / "components" / "cp-goal.js").read_text(
        encoding="utf-8")
    assert "g.template_retired" in js
    assert 'this.t("inbox.goal.template_retired")' in js
    from src.web.i18n_packs.goals import EN, ZH
    for k in ("inbox.goal.template_retired", "err.goals.template_retired_client"):
        assert k in ZH and k in EN and ZH[k] and EN[k]
    assert "自定义目标" in ZH["inbox.goal.template_retired"]


def test_background_tasks_wires_guard():
    src = (REPO / "src" / "bootstrap" / "background_tasks.py").read_text(
        encoding="utf-8")
    assert "wrap_care_send(" in src
    assert "send_callback=_care_send_guarded" in src
